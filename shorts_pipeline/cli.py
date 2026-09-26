"""Command line entry point. See README for the workflow."""
from __future__ import annotations

import argparse
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from . import analyze, fetch, judge, rank
from .config import OUTPUT_DIR, has_typesafe, load_config
from .storage import latest_run_dir, load_json, new_run_dir, now_iso, save_json

log = logging.getLogger("shorts")


def _setup_logging(verbose: bool) -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # emoji-laden titles on Windows consoles
        except Exception:  # noqa: BLE001
            pass
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpx2").setLevel(logging.WARNING)


def _run_dir(args) -> Path:
    if getattr(args, "run", None):
        return Path(args.run)
    d = latest_run_dir()
    if not d:
        sys.exit("No runs yet. Run `python main.py discover` first.")
    return d


# ----------------------------------------------------------------------------- commands

def cmd_discover(args) -> Path:
    cfg = load_config()["discovery"]
    fetch.configure(cfg)
    run_dir = new_run_dir()
    log.info("run dir: %s", run_dir)
    candidates = fetch.discover(cfg["hashtags"], cfg["search_queries"], cfg["per_source_limit"])
    if args.api:
        candidates += fetch.discover_api_most_popular()
    save_json(run_dir / "candidates.json", candidates)

    enrich_opts = dict(workers=cfg["workers"], want_transcript=cfg["fetch_transcripts"],
                       transcript_chars=cfg["transcript_chars"])
    shorts = fetch.enrich(candidates, max_candidates=cfg["max_candidates"], **enrich_opts)
    shorts = [s for s in shorts if fetch.is_short(s, cfg["max_duration_seconds"])]

    # Stage 2: hashtag shelves skew to all-time hits, so follow the channels behind them to their newest Shorts.
    if cfg.get("channels_to_follow", 0) > 0 and not fetch.rate_limited:
        by_velocity = sorted(shorts, key=lambda s: (s.get("view_count") or 0) / (fetch.age_hours(s) or 1e9), reverse=True)
        leaders = by_velocity[: cfg["channels_to_follow"]]
        # entries cached before channel ids were stored: refetch just these few
        for s in leaders:
            if "channel_id" not in s and not fetch.rate_limited:
                fresh = fetch.fetch_full(s["id"], cfg["fetch_transcripts"], cfg["transcript_chars"], force=True)
                if fresh:
                    s.update(fresh)
        channel_ids = [s.get("channel_id") for s in leaders]
        known = {s["id"] for s in shorts}
        recent = [c for c in fetch.discover_channel_shorts(channel_ids, cfg["shorts_per_channel"]) if c["id"] not in known]
        candidates += recent
        extra = fetch.enrich(recent, max_candidates=cfg["max_candidates"], **enrich_opts)
        shorts += [s for s in extra if fetch.is_short(s, cfg["max_duration_seconds"])]
        save_json(run_dir / "candidates.json", candidates)
    fresh = [s for s in shorts if (fetch.age_hours(s) or 1e9) <= cfg["max_age_days"] * 24]
    if len(fresh) < cfg["min_candidates"]:
        log.info("only %d Shorts within %d days; relaxing to %d days", len(fresh), cfg["max_age_days"], cfg["fallback_max_age_days"])
        fresh = [s for s in shorts if (fetch.age_hours(s) or 1e9) <= cfg["fallback_max_age_days"] * 24]
    log.info("%d Shorts kept (%d fetched, %d discovered)", len(fresh), len(shorts), len(candidates))
    save_json(run_dir / "shorts.json", fresh)
    return run_dir


def cmd_judge(args) -> Path:
    run_dir = _run_dir(args)
    shorts = load_json(run_dir / "shorts.json", [])
    if not has_typesafe():
        log.warning("TYPESAFE_API_KEY not set: skipping judgments; ranking will use view velocity and engagement only")
        return run_dir
    todo = [s for s in shorts if (not s.get("judgment") or args.force) and not fetch.probably_non_english(s)]
    log.info("judging %d of %d Shorts with TypeSafe (rest cached or skipped by the script check)", len(todo), len(shorts))
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(judge.judge_short, s, use_cache=not args.force): s for s in todo}
        for fut in as_completed(futures):
            s = futures[fut]
            try:
                s["judgment"] = fut.result()
            except Exception as exc:  # noqa: BLE001
                log.warning("judgment failed for %s: %s", s["id"], exc)
    save_json(run_dir / "shorts.json", shorts)
    return run_dir


def cmd_rank(args) -> Path:
    run_dir = _run_dir(args)
    cfg = load_config()["ranking"]
    shorts = load_json(run_dir / "shorts.json", [])
    shorts = rank.score_shorts(shorts, cfg)
    save_json(run_dir / "shorts.json", shorts)
    kept = [s for s in shorts if not s.get("excluded")]
    print(f"\nTop {min(args.top, len(kept))} of {len(kept)} Shorts (run {run_dir.name}):\n")
    for i, s in enumerate(kept[:args.top], 1):
        j = s.get("judgment") or {}
        fmt = j.get("format", {}).get("choice", "?")
        print(f"{i:>2}. {s['score']:.3f}  {s['views_per_hour']:>8.0f}/h  {fmt:<22} {s['title'][:60]}  {s['url']}")
    return run_dir


def cmd_analyze(args) -> Path:
    run_dir = _run_dir(args)
    cfg = load_config()["ranking"]
    shorts = load_json(run_dir / "shorts.json", [])
    if not shorts or "score" not in shorts[0]:
        shorts = rank.score_shorts(shorts, cfg)
        save_json(run_dir / "shorts.json", shorts)
    analysis = analyze.build_blueprints(shorts, cfg)
    save_json(run_dir / "analysis.json", analysis)
    report = analyze.render_report(analysis, shorts)
    (run_dir / "analysis.md").write_text(report, encoding="utf-8")
    print(report)
    print(f"Saved {run_dir / 'analysis.md'}")
    return run_dir


def cmd_produce(args) -> Path:
    from . import captions, footage, render, script_gen, tts

    run_dir = _run_dir(args)
    cfg = load_config()["production"]
    analysis = load_json(run_dir / "analysis.json")
    if not analysis or not analysis.get("blueprints"):
        sys.exit("No blueprints in this run. Run `analyze` first (needs TYPESAFE_API_KEY for judgments).")
    try:
        script_gen.pick_backend(cfg.get("script_backend", "auto"))
    except RuntimeError as exc:
        sys.exit(str(exc))
    blueprint = analysis["blueprints"][args.blueprint - 1]
    log.info("blueprint %d: %s x %s (%s hook)", args.blueprint, blueprint["format"], blueprint["topic"], blueprint["hook_style"])

    out_dir = OUTPUT_DIR / f"{now_iso()}_{blueprint['format']}_{blueprint['topic']}"
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        script = script_gen.generate_script(blueprint, target_seconds=cfg["target_seconds"], angle=args.angle,
                                            max_attempts=cfg["script_max_attempts"], min_hook_score=cfg["script_min_hook_score"],
                                            backend=cfg.get("script_backend", "auto"))
    except RuntimeError as exc:
        sys.exit(f"Script writing failed: {exc}")
    save_json(out_dir / "script.json", script)
    print(f"\nTITLE: {script['title']}\n\n{script['full_text']}\n")
    if script.get("qa_problems"):
        log.warning("script accepted with remaining QA notes: %s", "; ".join(script["qa_problems"]))

    voice_path = out_dir / "voice.mp3"
    words = tts.synthesize(script["full_text"], voice_path, voice=cfg["voice"], rate=cfg["voice_rate"])
    total = max(render.probe_duration(voice_path), words[-1]["end"] if words else 1.0) + 0.4
    save_json(out_dir / "words.json", words)
    ass_path = captions.write_ass(words, out_dir / "captions.ass", words_per_caption=cfg["words_per_caption"],
                                  full_text=script["full_text"], font=cfg["font"], font_size=cfg["font_size"])
    segments = footage.plan_backgrounds(script, words, total, cfg["background_source"], out_dir)
    video = render.render(segments, voice_path, ass_path, out_dir / "short.mp4",
                          music_volume_db=cfg["music_volume_db"], total_seconds=total)
    save_json(out_dir / "meta.json", {"run": run_dir.name, "blueprint": blueprint, "video": str(video),
                                      "title": script["title"], "description": script["description"],
                                      "hashtags": script["hashtags"], "duration": total})
    print(f"\nRendered {video}  ({total:.1f}s)")
    if args.upload:
        _upload(out_dir)
    return out_dir


def _upload(out_dir: Path) -> None:
    from . import upload

    cfg = load_config()["upload"]
    meta = load_json(out_dir / "meta.json")
    tags = meta["hashtags"]
    desc = meta["description"]
    if "#shorts" not in desc.lower():
        desc += "\n\n#Shorts"
    resp = upload.upload_video(Path(meta["video"]), title=meta["title"], description=desc, tags=tags,
                               privacy=cfg["privacy"], category_id=cfg["category_id"])
    vid = resp.get("id")
    meta["youtube_id"] = vid
    save_json(out_dir / "meta.json", meta)
    print(f"Uploaded as {cfg['privacy']}: https://youtube.com/shorts/{vid}")


def cmd_upload(args) -> None:
    target = Path(args.path)
    out_dir = target if target.is_dir() else target.parent
    _upload(out_dir)


def cmd_run(args) -> None:
    run_dir = cmd_discover(args)
    args.run = str(run_dir)
    args.force = False
    cmd_judge(args)
    cmd_rank(args)
    cmd_analyze(args)
    if args.produce:
        cmd_produce(args)


# ----------------------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="shorts", description="Trending Shorts ranking, analysis and production")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("discover", help="find candidate Shorts and fetch metadata + transcripts")
    d.add_argument("--api", action="store_true", help="also pull the official mostPopular chart (needs YOUTUBE_API_KEY)")
    d.set_defaults(func=cmd_discover)

    j = sub.add_parser("judge", help="judge each Short with TypeSafe (format, topic, hook, replicability...)")
    j.add_argument("--run"); j.add_argument("--force", action="store_true", help="ignore cached judgments")
    j.set_defaults(func=cmd_judge)

    r = sub.add_parser("rank", help="composite ranking (edit weights in config.yaml, no re-judging needed)")
    r.add_argument("--run"); r.add_argument("--top", type=int, default=20)
    r.set_defaults(func=cmd_rank)

    a = sub.add_parser("analyze", help="which formats/topics to make: blueprints + markdown report")
    a.add_argument("--run")
    a.set_defaults(func=cmd_analyze)

    pr = sub.add_parser("produce", help="write, voice, caption and render a Short from a blueprint")
    pr.add_argument("--run"); pr.add_argument("--blueprint", type=int, default=1, help="1-based index from analyze")
    pr.add_argument("--angle", help="optional specific subject/angle for the script")
    pr.add_argument("--upload", action="store_true")
    pr.set_defaults(func=cmd_produce)

    u = sub.add_parser("upload", help="upload a produced Short (output/<dir> or its short.mp4)")
    u.add_argument("path")
    u.set_defaults(func=cmd_upload)

    ru = sub.add_parser("run", help="discover -> judge -> rank -> analyze [-> produce]")
    ru.add_argument("--api", action="store_true"); ru.add_argument("--top", type=int, default=20)
    ru.add_argument("--produce", action="store_true"); ru.add_argument("--blueprint", type=int, default=1)
    ru.add_argument("--angle"); ru.add_argument("--upload", action="store_true")
    ru.set_defaults(func=cmd_run)

    w = sub.add_parser("web", help="start the local web console (keys, settings, runs, studio)")
    w.add_argument("--port", type=int, default=8787); w.add_argument("--host", default="127.0.0.1")
    w.set_defaults(func=cmd_web)
    return p


def cmd_web(args) -> None:
    from .web.server import serve
    serve(host=args.host, port=args.port)


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    args.func(args)
