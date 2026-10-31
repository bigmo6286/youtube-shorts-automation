"""Command line entry point. See README for the workflow."""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from . import analyze, fetch, judge, rank
from .config import OUTPUT_DIR, ROOT, has_typesafe, load_config
from .storage import RUNS_DIR, latest_run_dir, load_json, new_run_dir, now_iso, save_json

log = logging.getLogger("shorts")


def _setup_logging(verbose: bool) -> None:
    if sys.stdout is None or sys.stderr is None:
        # pythonw (the autostart launcher) has no console: anything written to stdout/stderr would kill the
        # process, so both go to data/console.err instead.
        from .config import DATA_DIR

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        sink = open(DATA_DIR / "console.err", "a", encoding="utf-8", buffering=1)  # noqa: SIM115
        sys.stdout = sys.stdout or sink
        sys.stderr = sys.stderr or sink
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
    """Accept a full path (data/runs/<id>) or a bare run id (<id>, as the web console sends)."""
    wanted = getattr(args, "run", None)
    if wanted:
        candidates = [Path(wanted), RUNS_DIR / Path(wanted).name]
        for c in candidates:
            if (c / "shorts.json").exists():
                return c
        sys.exit(f"Run {wanted!r} not found (no shorts.json under {candidates[0]} or {candidates[1]}).")
    d = latest_run_dir()
    if not d:
        sys.exit("No runs yet. Run `python main.py discover` first.")
    return d


# ----------------------------------------------------------------------------- commands

def cmd_discover(args) -> Path:
    from . import api_discovery

    cfg = load_config()["discovery"]
    fetch.configure(cfg)
    run_dir = new_run_dir()
    log.info("run dir: %s", run_dir)
    source = cfg.get("source", "auto")                 # auto | api | ytdlp
    use_api = source in ("auto", "api") and api_discovery.available()
    use_ytdlp = source in ("auto", "ytdlp") or not use_api
    max_dur = cfg["max_duration_seconds"]
    enrich_opts = dict(workers=cfg["workers"], want_transcript=cfg["fetch_transcripts"],
                       transcript_chars=cfg["transcript_chars"])
    shorts: list[dict] = []
    candidates: list[dict] = []

    # Stage 1a: the official API (not blocked by YouTube's bot checks; budgeted searches + cheap metadata)
    if use_api:
        try:
            api_shorts = api_discovery.discover(cfg)
        except Exception as exc:  # noqa: BLE001 - yt-dlp below still runs
            log.warning("API discovery failed: %s", str(exc)[:200])
            api_shorts = []
        shorts += api_shorts
        candidates += [{"id": m["id"], "title": m["title"], "view_count": m["view_count"], "duration": m["duration"],
                        "source": m.get("source", "api")} for m in api_shorts]
    elif args.api:
        candidates += fetch.discover_api_most_popular()

    # Stage 1b: hashtag Shorts shelves through yt-dlp, while YouTube is not blocking this machine
    if use_ytdlp and not fetch.rate_limited:
        known = {m["id"] for m in candidates}
        flat = [c for c in fetch.discover(cfg["hashtags"], cfg["search_queries"], cfg["per_source_limit"])
                if c["id"] not in known]
        candidates += flat
        shorts += [s for s in fetch.enrich(flat, max_candidates=cfg["max_candidates"], **enrich_opts)
                   if fetch.is_short(s, max_dur)]
    save_json(run_dir / "candidates.json", candidates)
    shorts = [s for s in shorts if fetch.is_short(s, max_dur)]

    # Stage 2: hashtag shelves skew to all-time hits, so follow the channels behind them to their newest Shorts.
    if cfg.get("channels_to_follow", 0) > 0:
        by_velocity = sorted(shorts, key=lambda s: (s.get("view_count") or 0) / (fetch.age_hours(s) or 1e9), reverse=True)
        leaders = by_velocity[: cfg["channels_to_follow"]]
        known = {s["id"] for s in shorts}
        extra: list[dict] = []
        if use_api:
            try:
                extra = [m for m in api_discovery.channel_recent([s.get("channel_id") for s in leaders],
                                                                 cfg["shorts_per_channel"], max_dur) if m["id"] not in known]
                api_discovery._add_transcripts(extra, int(api_discovery.config()["transcripts_for_top"]) // 2)
            except Exception as exc:  # noqa: BLE001
                log.warning("API channel stage failed: %s", str(exc)[:200])
        elif not fetch.rate_limited:
            for s in leaders:            # entries cached before channel ids were stored: refetch just these few
                if "channel_id" not in s and not fetch.rate_limited:
                    fresh = fetch.fetch_full(s["id"], cfg["fetch_transcripts"], cfg["transcript_chars"], force=True)
                    if fresh:
                        s.update(fresh)
            recent = [c for c in fetch.discover_channel_shorts([s.get("channel_id") for s in leaders],
                                                               cfg["shorts_per_channel"]) if c["id"] not in known]
            extra = [s for s in fetch.enrich(recent, max_candidates=cfg["max_candidates"], **enrich_opts)
                     if fetch.is_short(s, max_dur)]
        candidates += [{"id": m["id"], "title": m.get("title"), "view_count": m.get("view_count"),
                        "duration": m.get("duration"), "source": m.get("source", "channel")} for m in extra]
        shorts += extra
        save_json(run_dir / "candidates.json", candidates)
    seen: set[str] = set()
    shorts = [s for s in shorts if not (s["id"] in seen or seen.add(s["id"]))]
    fresh = [s for s in shorts if (fetch.age_hours(s) or 1e9) <= cfg["max_age_days"] * 24]
    if len(fresh) < cfg["min_candidates"]:
        log.info("only %d Shorts within %d days; relaxing to %d days", len(fresh), cfg["max_age_days"], cfg["fallback_max_age_days"])
        fresh = [s for s in shorts if (fetch.age_hours(s) or 1e9) <= cfg["fallback_max_age_days"] * 24]
    with_tx = sum(1 for s in fresh if s.get("transcript"))
    log.info("%d Shorts kept (%d fetched, %d discovered; %d with transcripts)", len(fresh), len(shorts), len(candidates), with_tx)
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
    from . import captions, footage, music, render, script_gen, tools, tts

    if not tools.ensure_ffmpeg_on_path():     # check before spending a script generation
        sys.exit(tools.MISSING_HELP)
    from . import housekeeping
    try:
        housekeeping.ensure_free_space()        # a full disk leaves empty files behind; stop before that
    except RuntimeError as exc:
        sys.exit(str(exc))
    cfg = load_config()["production"]
    from . import frame
    long_video = bool(getattr(args, "long", False))
    lv_cfg = cfg.get("long_video") or {}
    frame.use(lv_cfg.get("orientation", "landscape") if long_video else "vertical", long=long_video)
    from . import imagegen
    imagegen.override("only" if getattr(args, "ai_images_only", False) else None)

    script_text = _read_custom_script(args)
    if long_video and not script_text:
        sys.exit("A long video needs your own script (--script-file, or paste it in the console).")
    if script_text:
        # User-written script: no blueprint or run needed. Enhanced (hook, flow, payoff; facts kept) unless --as-written.
        kwargs = dict(title=getattr(args, "title", "") or "", description=getattr(args, "description", "") or "",
                      hashtags=_listify(getattr(args, "hashtags", None)), keywords=_listify(getattr(args, "keywords", None)))
        enhance = getattr(args, "enhance", True)
        try:
            if enhance:
                try:
                    script_gen.pick_backend(cfg.get("script_backend", "auto"))
                except RuntimeError as exc:
                    sys.exit(str(exc))
                log.info("enhancing your script (%d words): adding a hook, tightening flow, keeping every fact", len(script_text.split()))
                if long_video:
                    log.info("long video: keeping the full script; only the opening, transitions and grammar are edited")
                script = script_gen.enhance_script(script_text, max_attempts=cfg["script_max_attempts"],
                                                   min_hook_score=cfg["script_min_hook_score"],
                                                   backend=cfg.get("script_backend", "auto"), long=long_video, **kwargs)
                qa = script.get("qa") or {}
                if qa:
                    log.info("enhanced script: hook %.1f/3, clarity %.1f/2, payoff %.0f%%, faithful to original %.0f%%",
                             qa["hook_strength"]["score"], qa["clarity"]["score"], qa["has_payoff"]["noul"] * 100,
                             qa.get("faithful", {}).get("noul", 1.0) * 100)
            else:
                script = script_gen.custom_script(script_text, **kwargs)
                if not kwargs["keywords"]:
                    script = script_gen.assign_visuals(script, cfg.get("script_backend", "auto"))
                if long_video:
                    script = script_gen.as_long_video(script)
        except (ValueError, RuntimeError) as exc:
            sys.exit(f"Script preparation failed: {exc}")
        blueprint = {"format": "custom", "topic": "custom", "hook_style": "enhanced" if enhance else "as written",
                     "why_it_works": "user-written script" + (" edited for hook, flow and payoff" if enhance else "")}
        run_name = "custom"
        log.info("custom script%s: %d words, title %r", " (enhanced)" if enhance else " (as written)", script["word_count"], script["title"])
        out_dir = OUTPUT_DIR / f"{now_iso()}_{'long' if long_video else 'custom'}"
    elif getattr(args, "profile", None):
        # Clone a channel's style: blueprint comes from the analysed profile, not from the trend run.
        from . import profile as profiles

        prof = profiles.load_profile(args.profile)
        if not prof:
            sys.exit(f"No profile for {args.profile!r}. Run `python main.py profile add <channel url or @handle>` first.")
        try:
            script_gen.pick_backend(cfg.get("script_backend", "auto"))
        except RuntimeError as exc:
            sys.exit(str(exc))
        prof = profiles.ensure_style_guide(prof)
        exemplar_id = getattr(args, "exemplar", None) or None
        try:
            blueprint = profiles.blueprint_for(prof, exemplar_id=exemplar_id)
        except RuntimeError as exc:
            sys.exit(str(exc))
        run_name = f"profile:{prof['handle']}"
        angle = args.angle
        if exemplar_id and not angle:
            mv = blueprint["model_video"]
            angle = (f"Remake this Short of theirs with the same subject and structure, in fresh words and facts of your "
                     f"own: title {mv.get('title')!r}" + (f"; its transcript: {mv['transcript'][:600]!r}" if mv.get("transcript") else ""))
            log.info("modelled on %r (%s views)", mv.get("title"), f"{int(mv.get('views') or 0):,}")
        log.info("style of %s: %s x %s (%s hook)", prof.get("channel"), blueprint["format"], blueprint["topic"], blueprint["hook_style"])
        out_dir = OUTPUT_DIR / f"{now_iso()}_{blueprint['format']}_{blueprint['topic']}"
        target = int(getattr(args, "seconds", None) or blueprint.get("typical_seconds") or cfg["target_seconds"])
        try:
            script = script_gen.generate_script(blueprint, target_seconds=max(20, min(90, target)), angle=angle,
                                                max_attempts=cfg["script_max_attempts"], min_hook_score=cfg["script_min_hook_score"],
                                                backend=cfg.get("script_backend", "auto"), avoid_titles=None)
        except RuntimeError as exc:
            sys.exit(f"Script writing failed: {exc}")
    elif getattr(args, "dub_of", None):
        # A dubbed version of one of the main channel's Shorts, for this (second-language) channel. See dub.py.
        from . import dub
        src_out, _ = dub._source_paths()
        src = src_out / args.dub_of
        original = load_json(src / "script.json")
        src_meta = load_json(src / "meta.json") or {}
        if not original:
            sys.exit(f"No script for {args.dub_of} in {src_out}")
        try:
            script = dub.translate(original)
        except RuntimeError as exc:
            sys.exit(f"Translation failed: {exc}")
        blueprint = {**(src_meta.get("blueprint") or {}), "source": "dub", "language": dub.config()["language"],
                     "dub_of": args.dub_of}
        run_name = f"dub_{dub.config()['language']}"
        log.info("dub (%s): %r -> %r", dub.config()["language_name"], original.get("title"), script["title"])
        out_dir = OUTPUT_DIR / f"{now_iso()}_dub_{dub.config()['language']}"
    elif getattr(args, "sequel_of", None) or getattr(args, "idea", None):
        # A sequel of a winning upload, or a Short a viewer asked for in the comments (see specials.py).
        from . import specials
        try:
            if args.sequel_of:
                blueprint, angle, source = specials.sequel_blueprint(args.sequel_of)
                allow = [source["title"]]
                run_name = "sequel"
            else:
                blueprint, angle, source = specials.idea_blueprint(args.idea)
                allow = [source.get("title", "")]
                run_name = "viewer_idea"
            script_gen.pick_backend(cfg.get("script_backend", "auto"))
        except RuntimeError as exc:
            sys.exit(str(exc))
        log.info("%s: %s x %s", run_name.replace("_", " "), blueprint["format"], blueprint["topic"])
        out_dir = OUTPUT_DIR / f"{now_iso()}_{run_name}_{blueprint['format']}"
        try:
            script = script_gen.generate_script(blueprint, target_seconds=_learned_seconds(blueprint, cfg), angle=angle,
                                                max_attempts=cfg["script_max_attempts"], min_hook_score=cfg["script_min_hook_score"],
                                                backend=cfg.get("script_backend", "auto"), avoid_titles=None, allow_repeat_of=allow)
        except RuntimeError as exc:
            sys.exit(f"Script writing failed: {exc}")
        if args.sequel_of:
            if "part 2" not in script["title"].lower():
                script["title"] = script_gen._shorten(script["title"], script_gen.TITLE_MAX - 9) + " (Part 2)"
            script["description"] = (f"Part 1: https://youtube.com/shorts/{args.sequel_of}\n\n" + script["description"]).strip()
    elif getattr(args, "topic", None):
        # One Short of a topic series ("bible stories"): see topics.py.
        from . import topics
        try:
            script_gen.pick_backend(cfg.get("script_backend", "auto"))
        except RuntimeError as exc:
            sys.exit(str(exc))
        blueprint = topics.plan(args.topic)
        run_name = f"topic:{topics.slug(args.topic)}"
        log.info("topic series %r: %s x %s (%s hook)%s", args.topic, blueprint["format"], blueprint["topic"],
                 blueprint["hook_style"], ", told as its source tells it" if blueprint.get("attribute_source") else "")
        out_dir = OUTPUT_DIR / f"{now_iso()}_{blueprint['format']}_{topics.slug(args.topic)}"
        try:
            script = script_gen.generate_script(blueprint, target_seconds=_learned_seconds(blueprint, cfg), angle=args.angle,
                                                max_attempts=cfg["script_max_attempts"], min_hook_score=cfg["script_min_hook_score"],
                                                backend=cfg.get("script_backend", "auto"), avoid_titles=None)
        except RuntimeError as exc:
            sys.exit(f"Script writing failed: {exc}")
    elif getattr(args, "blueprint_key", None):
        # One of your channel's own winners (format x topic that beats your channel median), independent of the trend run.
        from .channel import CHANNEL_BLUEPRINTS_PATH, channel_blueprints

        cbs = load_json(CHANNEL_BLUEPRINTS_PATH) or channel_blueprints()
        blueprint = next((b for b in cbs if b.get("key") == args.blueprint_key), None)
        if not blueprint:
            sys.exit(f"No channel blueprint {args.blueprint_key!r}; run `channel sync` first.")
        try:
            script_gen.pick_backend(cfg.get("script_backend", "auto"))
        except RuntimeError as exc:
            sys.exit(str(exc))
        run_name = "channel"
        log.info("channel winner: %s x %s (%s hook), x%s your channel median", blueprint["format"], blueprint["topic"],
                 blueprint["hook_style"], blueprint["opportunity"])
        out_dir = OUTPUT_DIR / f"{now_iso()}_{blueprint['format']}_{blueprint['topic']}"
        try:
            script = script_gen.generate_script(blueprint, target_seconds=_learned_seconds(blueprint, cfg), angle=args.angle,
                                                max_attempts=cfg["script_max_attempts"], min_hook_score=cfg["script_min_hook_score"],
                                                backend=cfg.get("script_backend", "auto"), avoid_titles=None)
        except RuntimeError as exc:
            sys.exit(f"Script writing failed: {exc}")
    else:
        run_dir = _run_dir(args)
        analysis = load_json(run_dir / "analysis.json")
        if analysis is None:
            sys.exit(f"Run {run_dir.name} has not been analysed yet. Run Judge, Rank and Analyze (or `run`) first.")
        if not analysis.get("blueprints"):
            sys.exit(f"Run {run_dir.name} was analysed but produced no blueprints: {analysis.get('judged', 0)} Shorts judged, "
                     f"{analysis.get('usable', 0)} usable. Check that TYPESAFE_API_KEY is set, then re-run Judge, Rank and Analyze.")
        if not 1 <= args.blueprint <= len(analysis["blueprints"]):
            sys.exit(f"Blueprint {args.blueprint} does not exist; this run has {len(analysis['blueprints'])}.")
        try:
            script_gen.pick_backend(cfg.get("script_backend", "auto"))
        except RuntimeError as exc:
            sys.exit(str(exc))
        blueprint = analysis["blueprints"][args.blueprint - 1]
        run_name = run_dir.name
        log.info("blueprint %d: %s x %s (%s hook)", args.blueprint, blueprint["format"], blueprint["topic"], blueprint["hook_style"])
        out_dir = OUTPUT_DIR / f"{now_iso()}_{blueprint['format']}_{blueprint['topic']}"
        try:
            script = script_gen.generate_script(blueprint, target_seconds=_learned_seconds(blueprint, cfg), angle=args.angle,
                                                max_attempts=cfg["script_max_attempts"], min_hook_score=cfg["script_min_hook_score"],
                                                backend=cfg.get("script_backend", "auto"), avoid_titles=None)
        except RuntimeError as exc:
            sys.exit(f"Script writing failed: {exc}")

    out_dir.mkdir(parents=True, exist_ok=True)
    if script.get("backend") not in ("custom", "dub") and not (script_text and getattr(args, "title", "")):
        from . import titles
        titles.improve(script)                         # search-driven title, picked by TypeSafe
        if getattr(args, "sequel_of", None) and "part 2" not in script["title"].lower():
            script["title"] = script_gen._shorten(script["title"], script_gen.TITLE_MAX - 9) + " (Part 2)"
    fact_result = None
    fc = __import__("shorts_pipeline.factcheck", fromlist=["config"])
    fcfg = fc.config()
    fc_mode = fc.mode_for(script, user_written=bool(script_text))
    if fc_mode != "off" and fc.available():
        log.info("fact-check: searching the web for every claim (Claude + TypeSafe)%s",
                 " - your own script: doubtful claims are flagged, your words are not changed" if fc_mode == "review" and script_text else "")
        fact_result = fc.check(script)
        if fact_result:
            for c in fact_result["contradicted"]:
                log.warning("fact-check: WRONG %r -> %s", c["claim"][:90], (c.get("correction") or "?")[:120])
            for c in fact_result["unverifiable"]:
                log.info("fact-check: unverified %r", c["claim"][:90])
            if fact_result["contradicted"] and fc_mode == "fix":
                try:
                    script = script_gen.revise_facts(script, fact_result["contradicted"], cfg.get("script_backend", "auto"))
                    fact_result["fixed"] = True
                    log.info("fact-check: %d claim(s) corrected in the script", len(fact_result["contradicted"]))
                except Exception as exc:  # noqa: BLE001
                    log.warning("fact-check: could not rewrite the wrong claims (%s); the video is held for review", str(exc)[:160])
                    fact_result["rewrite_failed"] = True
            log.info("fact-check: %s", fc.summary(fact_result))
            script["fact_check"] = {k: fact_result[k] for k in ("supported", "contradicted", "unverifiable", "sources")} | {
                "claims": len(fact_result["claims"]), "summary": fc.summary(fact_result), "mode": fc_mode,
                "doubts": fc.doubts_text(fact_result)}
        else:
            log.info("fact-check: could not run this time; the script is used as written")
    save_json(out_dir / "script.json", script)
    print(f"\nTITLE: {script['title']}\n\n{script['full_text']}\n")
    if script.get("qa_problems"):
        log.warning("script accepted with remaining QA notes: %s", "; ".join(script["qa_problems"]))
    if script.get("backend") == "custom" and script.get("qa"):
        qa = script["qa"]
        log.info("TypeSafe read of your script (advisory): hook %.1f/3, clarity %.1f/2, payoff %.0f%%, policy risk %.0f%%",
                 qa["hook_strength"]["score"], qa["clarity"]["score"], qa["has_payoff"]["noul"] * 100, qa["policy_risk"]["noul"] * 100)
    if script.get("original_text"):
        print(f"ORIGINAL:\n{script['original_text']}\n")

    voice_path = out_dir / "voice.mp3"
    from . import originality
    voice = originality.voice_for(script["title"], cfg["voice"])         # a few similar narrators, not one for every Short
    if script.get("backend") == "dub":
        from . import dub
        voice = dub.config()["voice"]
    words = tts.synthesize(script["full_text"], voice_path, voice=voice, rate=cfg["voice_rate"])
    outro_cfg = cfg.get("outro") or {}
    outro_on = outro_cfg.get("enabled", True) if getattr(args, "outro", None) is None else bool(args.outro)
    if script_gen.loop_endings() and not outro_on and not long_video and words:
        # a looping Short: almost no silence between the last word and the replayed first word
        words, body_seconds = tts.tighten_loop(voice_path, words)
        log.info("loop: voice trimmed, body ends %.2fs after the last word", tts.LOOP_TAIL)
    else:
        body_seconds = max(render.probe_duration(voice_path), words[-1]["end"] if words else 1.0) + 0.4
    save_json(out_dir / "words.json", words)
    n_hook, n_cta = len((script.get("hook") or "").split()), len((script.get("cta") or "").split())
    timeline = {"hook_end": words[min(len(words), n_hook) - 1]["end"] if words and n_hook else None,
                "cta_start": words[max(0, len(words) - n_cta)]["start"] if words and n_cta else None}

    intro = _card(cfg.get("intro") or {}, "intro", script, out_dir, cfg, enabled=getattr(args, "intro", None))
    outro = _card(cfg.get("outro") or {}, "outro", script, out_dir, cfg, enabled=getattr(args, "outro", None))
    intro_seconds = intro["seconds"] if intro else 0.0
    total = intro_seconds + body_seconds + (outro["seconds"] if outro else 0.0)
    log.info("timeline: intro %.1fs + body %.1fs + outro %.1fs = %.1fs", intro_seconds, body_seconds,
             outro["seconds"] if outro else 0.0, total)

    # Captions are burned onto the body stream before the intro is concatenated in front of it, so their
    # clock is body-local: no offset here (the intro card carries its own subtitle file).
    cap_style = _caption_style(cfg)
    if frame.landscape():                       # same look, sized for a 1080-high frame
        cap_style = {**cap_style, "size": int(round(float(cap_style.get("size") or 84) * float(lv_cfg.get("caption_scale", 0.7))))}
    ass_path = captions.write_ass(words, out_dir / "captions.ass", style=cap_style, full_text=script["full_text"],
                                  width=frame.size()[0], height=frame.size()[1])
    hook_cfg = cfg.get("hook_overlay") or {}
    if not long_video and hook_cfg.get("enabled", True) and (script.get("thumbnail_text") or script.get("hook")):
        n = len((script.get("hook") or "").split())
        hook_end = words[min(len(words), max(n, 1)) - 1]["end"] + 0.5 if words and n else 2.5
        seconds = max(1.5, min(float(hook_cfg.get("max_seconds", 3.5)), hook_end))
        captions.add_hook_overlay(ass_path, script.get("thumbnail_text") or script["hook"], seconds, style=_caption_style(cfg))
    photo_credits: list[dict] = []
    segments = footage.plan_backgrounds(script, words, body_seconds, cfg["background_source"], out_dir, credits=photo_credits)

    music_cfg = dict(cfg.get("music") or {})
    if "music_volume_db" in cfg and "volume_db" not in music_cfg:      # older config.yaml
        music_cfg["volume_db"] = cfg["music_volume_db"]
    track = (music.pick_for_script(script, getattr(args, "music", None) or music_cfg.get("default", "random"))
             if music_cfg.get("pick", "mood") == "mood" else music.pick_track(getattr(args, "music", None) or music_cfg.get("default", "random")))
    description = script["description"]
    if track:
        log.info("music: %s (%s%s)", track["file"], track["license"], f", by {track['creator']}" if track.get("creator") else "")
        credit = music.attribution_line(track)
        if credit and credit not in description:
            description = description.rstrip() + "\n\n" + credit
    else:
        log.info("music: none%s", "" if music.list_tracks() else " (assets/music is empty: fetch or upload tracks in Settings)")
    if script.get("fact_check") and fcfg.get("sources_in_description", True):
        block = fc.sources_block(script["fact_check"])
        if block:
            description = description.rstrip() + "\n\n" + block
    if long_video and lv_cfg.get("chapters", True) and words:
        chapters = script_gen.chapters_for(script, cfg.get("script_backend", "auto"))
        if len(chapters) >= 3:
            counts = [len((script.get("hook") or "").split()), *[len(ln["text"].split()) for ln in script.get("lines") or []]]
            starts = [sum(counts[:i]) for i in range(len(counts))]
            stamps = []
            for title_c, line in chapters:
                wi = starts[line] if line < len(starts) else None
                t = 0.0 if line == 0 or wi is None or wi >= len(words) else float(words[wi]["start"])
                stamps.append((t, title_c))
            stamps = [(t, c) for i, (t, c) in enumerate(stamps) if i == 0 or t - stamps[i - 1][0] >= 10]   # YouTube: >= 10 s each
            if len(stamps) >= 3:
                stamps[0] = (0.0, stamps[0][1])
                description = description.rstrip() + "\n\nChapters:\n" + "\n".join(
                    f"{int(t // 60)}:{int(t % 60):02d} {c}" for t, c in stamps)
                log.info("chapters: %d", len(stamps))
    if photo_credits:
        from .commons import credit_line
        description = description.rstrip() + "\n\n" + credit_line(photo_credits)     # what CC BY / BY-SA ask for
    thumb = None
    thumb_cfg = cfg.get("thumbnail") or {}
    if thumb_cfg.get("enabled", True):
        from . import thumbnail
        thumb = thumbnail.make_thumbnail(script, segments, out_dir, style=_caption_style(cfg),
                                         handle=thumb_cfg.get("handle") or (cfg.get("outro") or {}).get("handle", ""),
                                         band=thumb_cfg.get("band", True))
    video = render.render(segments, voice_path, ass_path, out_dir / "short.mp4", total_seconds=total,
                          music_path=Path(track["path"]) if track else None,
                          music_volume_db=float(music_cfg.get("volume_db", -18)), duck=bool(music_cfg.get("duck", True)),
                          fade_seconds=(min(0.3, float(music_cfg.get("fade_seconds", 1.5))) if script_gen.loop_endings() and not outro and not long_video
                                        else float(music_cfg.get("fade_seconds", 1.5))),     # a long fade breaks the loop
                          intro=intro, outro=outro)
    render_result = None
    from . import render_check
    if render_check.config()["enabled"]:
        try:
            render_result = render_check.check(video, total, script.get("title", ""))
            if render_result["issues"]:
                log.warning("render check (%s): %s", render_result["severity"], "; ".join(render_result["issues"]))
            else:
                log.info("render check: no problems in %d frames, %.1f s black", len(render_result["frames"]), render_result["black_seconds"])
        except Exception as exc:  # noqa: BLE001
            log.warning("render check skipped: %s", str(exc)[:120])
    for flag in ("explore",):
        if getattr(args, flag, False):
            blueprint = {**blueprint, flag: True}
    meta = {"run": run_name, "blueprint": blueprint, "video": str(video),
            "title": script["title"], "description": description,
            "hashtags": script["hashtags"], "duration": total,
            "music": {k: track[k] for k in ("file", "title", "creator", "license")} if track else None,
            "intro": bool(intro), "outro": bool(outro), "thumbnail": str(thumb) if thumb else None, "voice": voice,
            "segments": segments, "photo_credits": photo_credits, "timeline": timeline,
            "fact_check": script.get("fact_check", {}).get("summary") if script.get("fact_check") else None,
            "render_check": {k: render_result[k] for k in ("severity", "issues", "black_seconds")} if render_result else None,
            "thumbnail_text": script.get("thumbnail_text", ""),
            "mode": "custom" if script.get("backend") == "custom" else ("enhanced" if script.get("original_text") else "blueprint"),
            "long_video": long_video, "orientation": "landscape" if frame.landscape() else "vertical"}
    save_json(out_dir / "meta.json", meta)
    if getattr(args, "dub_of", None):
        from . import dub
        dub.record(args.dub_of, out_dir.name)
    if getattr(args, "sequel_of", None) or getattr(args, "idea", None):
        from . import specials
        specials.record("sequel" if args.sequel_of else "idea", args.sequel_of or args.idea, out_dir.name)
    from . import history
    try:
        housekeeping.tidy()
    except Exception as exc:  # noqa: BLE001
        log.warning("footage cache cleanup failed: %s", exc)
    history.record(script["title"], hook=script.get("hook", ""),
                   key=blueprint.get("key") if str(blueprint.get("key", "")).startswith("series:") else f"{blueprint.get('format')}|{blueprint.get('topic')}",
                   out_dir=out_dir, source=str(run_name))
    if getattr(args, "topic", None):
        from . import topics
        topics.record(args.topic, blueprint["format"], out_dir.name)
    print(f"\nRendered {video}  ({total:.1f}s)")
    upload_error = None
    queue_note = ""
    if args.upload:
        try:
            _upload(out_dir)
        except SystemExit:
            raise
        except Exception as exc:  # noqa: BLE001
            # The Short is rendered and saved; a failed upload (expired Google token, quota, network) must not
            # lose the Telegram delivery or mark the whole production as failed. Upload it from the card later.
            upload_error = f"{type(exc).__name__}: {str(exc)[:300]}"
            log.error("upload failed, the video is saved in %s and can be uploaded from its Studio card: %s", out_dir.name, upload_error)
    elif render_result and render_result["severity"] == "severe":
        meta["upload_state"] = "review"
        meta["review_reason"] = "render check: " + "; ".join(render_result["issues"])
        save_json(out_dir / "meta.json", meta)
        queue_note = "Held for your review: the render check found " + "; ".join(render_result["issues"]) + ". Watch it before uploading."
        print(queue_note)
    elif fact_result and fc.needs_review(fact_result, fc_mode):
        meta["upload_state"] = "review"
        meta["review_reason"] = "fact-check: " + fc.summary(fact_result)
        save_json(out_dir / "meta.json", meta)
        doubts = fc.doubts_text(fact_result)
        queue_note = (("Held for your review: the fact-check doubts some claims (" if fact_result["contradicted"]
                       else "Held for your review: the fact-check could not verify some of its main claims (")
                      + fc.summary(fact_result) + ")." + (f"\n{doubts}" if doubts else "")
                      + "\nCheck them, then use Upload on its card" + (" or fix the script and produce it again." if script_text else "."))
        print(queue_note)
    elif script.get("backend") == "ollama":
        meta["upload_state"] = "review"
        meta["written_by"] = "local model"
        save_json(out_dir / "meta.json", meta)
        queue_note = ("Written by the free local model because Claude was unavailable. Check the facts, then use "
                      "Upload on its card; it is not uploaded automatically.")
        print(queue_note)
    elif long_video:
        queue_note = "Long videos are not queued with the Shorts: upload it from its card or with /upload on Telegram."
        print(queue_note)
    else:
        # Every Short joins the upload queue, made by the schedule or by hand (the queue picks the time and order).
        from . import upload_queue
        if upload_queue.config()["auto"]:
            try:
                q = upload_queue.enqueue(out_dir)
                qcfg = upload_queue.config()
                queue_note = (f"Upload queue: priority {q['priority']}/100, #{q['rank']} of {q['waiting']} waiting"
                              + ("" if q["priority"] >= int(qcfg["min_priority"]) else
                                 f" (below {qcfg['min_priority']}: waits for you, not uploaded automatically)"))
                print(queue_note)
            except Exception as exc:  # noqa: BLE001 - the Short is fine; it can be uploaded by hand
                log.warning("could not add the Short to the upload queue: %s", exc)
        meta = load_json(out_dir / "meta.json") or meta
    if getattr(args, "telegram", True):
        _notify_telegram(out_dir, meta, extra=queue_note)
    if upload_error:
        print(f"Upload failed ({upload_error}); use Upload on the video's card once YouTube access works again.")
    return out_dir


def _learned_seconds(blueprint: dict, cfg: dict) -> int:
    """Script length learned from the channel's views and retention (per format when it has enough data)."""
    from . import tuning
    secs = tuning.target_seconds(blueprint.get("format"), int(cfg["target_seconds"]))
    if secs != int(cfg["target_seconds"]):
        log.info("length: %ds for %s (learned from your channel; configured %ds)", secs, blueprint.get("format"), cfg["target_seconds"])
    return secs


def _card(card_cfg: dict, kind: str, script: dict, out_dir: Path, cfg: dict, *, enabled: bool | None) -> dict | None:
    """Build the intro or outro: a generated title card (default) or the user's own clip from assets/."""
    from . import captions, footage, render

    on = card_cfg.get("enabled", True) if enabled is None else enabled
    if not on:
        return None
    seconds = float(card_cfg.get("seconds", 1.5 if kind == "intro" else 2.0))
    if card_cfg.get("mode", "card") == "clip":
        clip = Path(card_cfg.get("clip") or f"assets/{kind}.mp4")
        if not clip.is_absolute():
            clip = ROOT / clip
        if not clip.exists():
            log.warning("%s clip %s not found; using a generated card instead", kind, clip)
        else:
            return {"path": str(clip), "seconds": min(seconds, render.probe_duration(clip)) if seconds > 0 else render.probe_duration(clip), "ass": None}
    text = (card_cfg.get("text") or ("{title}" if kind == "intro" else "Follow for more")).replace("{title}", script["title"])
    sub = (card_cfg.get("sub_text") or ("" if kind == "intro" else card_cfg.get("handle", ""))).replace("{title}", script["title"])
    bg = footage.generated_background(out_dir / f"{kind}_bg.mp4", seconds + 0.5, seed=3 if kind == "intro" else 1)
    ass = captions.write_card_ass(text, seconds, out_dir / f"{kind}.ass", sub_text=sub, style=_caption_style(cfg))
    return {"path": str(bg), "seconds": seconds, "ass": str(ass)}


def _caption_style(cfg: dict) -> dict:
    """captions.* from config, with the legacy font / font_size / words_per_caption keys as fallbacks."""
    from .captions import resolve_style

    raw = dict(cfg.get("captions") or {})
    if "font" not in raw and cfg.get("font"):
        raw["font"] = cfg["font"]
    if "size" not in raw and cfg.get("font_size"):
        raw["size"] = cfg["font_size"]
    if "words_per_caption" not in raw and cfg.get("words_per_caption"):
        raw["words_per_caption"] = cfg["words_per_caption"]
    return resolve_style(raw)


def _notify_telegram(out_dir: Path, meta: dict, *, force: bool = False, extra: str = "") -> None:
    from . import notify

    tcfg = (load_config().get("notifications") or {}).get("telegram") or {}
    if not force and not tcfg.get("on_produce", True):
        return
    if not notify.telegram_configured():
        if force:
            sys.exit("Telegram is not configured: add the bot token and chat id in Settings.")
        return
    try:
        kind = "Weekly compilation ready" if meta.get("mode") == "compilation" else ("New long video ready" if meta.get("long_video") else "New Short ready")
        note = f"{kind} ({meta.get('duration', 0):.0f}s)" + (f" · https://youtube.com/shorts/{meta['youtube_id']}" if meta.get("youtube_id") else "")
        if extra:
            note += "\n" + extra
        buttons = None
        if meta.get("approval") == "pending":
            note += "\nWaiting for your approval before upload."
            buttons = [[{"text": "✅ Approve upload", "callback_data": f"approve:{out_dir.name}"},
                        {"text": "❌ Reject", "callback_data": f"reject:{out_dir.name}"}]]
        notify.send_short(Path(meta["video"]), meta, note=note, buttons=buttons)
        print("Sent to Telegram.")
    except Exception as exc:  # noqa: BLE001 - delivery must never fail the render
        log.error("Telegram delivery failed: %s", exc)
        if force:
            sys.exit(f"Telegram delivery failed: {exc}")


def cmd_report(args) -> None:
    from . import alerts, notify
    text = alerts.daily_report()
    print(text)
    if getattr(args, "send", False):
        if not notify.telegram_configured():
            sys.exit("Telegram is not configured.")
        notify.send_message(text)
        print("Sent to Telegram.")


def cmd_cleanup(args) -> None:
    from . import housekeeping
    s = housekeeping.cleanup_outputs(dry_run=bool(getattr(args, "dry_run", False)))
    c = housekeeping.cleanup_config()
    print(f"{'Would free' if s['dry_run'] else 'Freed'} {s['freed_gb']} GB in {s['folders']} folders: render pieces of uploaded "
          f"Shorts, {s['videos_removed']} video files on YouTube for {c['keep_video_days']}+ days or never uploaded for "
          f"{c['keep_unuploaded_days']}+ days. Scripts, details and thumbnails are kept.")
    _, removed = housekeeping.prune_cache()
    if removed:
        print(f"Footage cache trimmed by {removed:.1f} GB.")


def cmd_comments(args) -> None:
    from . import comments
    action = getattr(args, "action", "run") or "run"
    if action == "connect":
        print("A Google page opens: approve YouTube (including managing comments).")
        comments.connect()
        print("Comments connected.")
        action = "run"
    if action == "run":
        s = comments.run(interactive=False)
        print(f"comments: {s['new']} new, {s['drafted']} reply drafts, {s['posted']} posted, {s['needs_owner']} need you, "
              f"{s['spam']} spam ignored, {s['questions']} question comments posted")
    elif action == "list":
        for c in comments.pending():
            print(f"  [{c['status']}] {c['id']}  {c['author']}: {c['text'][:80]}\n      draft: {c.get('draft', '')}")
    elif action == "post":
        print("posted reply", comments.post_reply(args.target or "", getattr(args, "text", None)))
    elif action == "dismiss":
        comments.dismiss(args.target or "")


def cmd_vision_check(args) -> None:
    """Compare the text-only footage picks of existing Shorts with what TypeSafe sees in them."""
    from . import vision
    from .config import has_typesafe
    if not has_typesafe():
        sys.exit("Needs TYPESAFE_API_KEY.")
    dirs = ([OUTPUT_DIR / d for d in args.dirs] if args.dirs else
            sorted((d for d in OUTPUT_DIR.iterdir() if d.is_dir() and (d / "meta.json").exists()), reverse=True))
    checked = 0
    for d in dirs:
        rows = vision.audit(d)
        if not rows:
            continue
        checked += 1
        print(f"\n{(load_json(d / 'meta.json') or {}).get('title', d.name)}  ({d.name})")
        for r in rows:
            res = r["result"]
            mark = "?" if res is None else ("ok " if r["passes"] else "BAD")
            detail = "no frame" if res is None else f"shows {res['shows']:.0%}, contradicts {res['conflict']:.0%}, off-topic {res.get('off_topic', 0):.0%}"
            print(f"  {mark} shot {r['shot']}: {detail}  [{r['subject'][:40]}]  {r['line'][:70]}")
        if checked >= args.limit:
            break
    if not checked:
        print("No Short with its clips still on disk (uploaded Shorts lose them; check new ones before upload).")


def cmd_compile(args) -> Path:
    """This week's compilation: the best uploaded Shorts on one topic in one landscape video with chapters."""
    from . import compilation, tools
    if not tools.ensure_ffmpeg_on_path():
        sys.exit(tools.MISSING_HELP)
    try:
        return compilation.run(topic=getattr(args, "topic", None) or None, days=getattr(args, "days", None) or None,
                               upload=False if getattr(args, "no_upload", False) else None,
                               telegram=getattr(args, "telegram", True))
    except RuntimeError as exc:
        sys.exit(str(exc))


def cmd_update(args) -> None:
    """Pull the latest engine code and restart the console (or just report what is new with --check)."""
    from . import updater
    if getattr(args, "check", False):
        info = updater.check()
        if info.get("error"):
            sys.exit(f"Could not check: {info['error']}")
        print(f"This machine: {info['local']} · GitHub: {info['remote']} · "
              + (f"{info['behind']} update(s) available:" if info["behind"] else "up to date"))
        for c in info["changes"]:
            print(f"  - {c}")
        if info["dirty"]:
            print("Local changes (would block an update): " + ", ".join(info["dirty"]))
        return
    result = updater.apply(restart=not getattr(args, "no_restart", False))
    print(result["message"])
    if not result["ok"]:
        sys.exit(1)


def cmd_restart(args) -> None:
    from . import updater
    print(updater.restart_console(args.port, delay=args.delay))


def cmd_backup(args) -> None:
    from . import backup
    action = getattr(args, "action", "now") or "now"
    if action == "now":
        print(f"Backup written: {backup.make()}")
    elif action == "list":
        for p in sorted(backup.folder().glob("shorts-*.zip")):
            print(f"  {p.name}  {p.stat().st_size / 1e6:.1f} MB")
        print(f"(in {backup.folder()})")
    elif action == "restore":
        if not args.target:
            sys.exit("Give the zip to restore: python main.py backup restore <path>")
        print(f"Restored {backup.restore(Path(args.target))} files (tokens and keys are never in a backup).")


def cmd_watchdog(args) -> None:
    from . import watchdog
    port = args.port
    if port == 8787:
        from .config import CHANNEL, channels
        port = next((c["port"] for c in channels() if c["name"] == (CHANNEL or "main")), 8787)
    watchdog.run(port)


def cmd_setup_voice(args) -> None:
    from . import tts
    path = tts.download_kokoro()
    print(f"Kokoro voice files in {path}; available: {tts.kokoro_available()}")


def cmd_queue(args) -> None:
    from . import upload_queue
    s = upload_queue.summary()
    print(f"automatic upload: {'on' if s['auto'] else 'off'} · {s['queued']} waiting · uploaded {s['uploaded_24h']}/{s['limit']} in 24 h"
          + (f" · paused until {s['paused_until']} ({s['paused_reason']})" if s["paused_until"] else ""))
    print(f"next: {s['next']['priority']} {s['next']['title']}" if s["next"] else f"next: none ({s['waiting_reason']})")
    for i in upload_queue.queued():
        print(f"  {i['priority']:>3}  {i['dir']}  {i['title'][:60]}")


def cmd_telegram(args) -> None:
    from . import notify

    if args.action == "test":
        if not notify.telegram_configured():
            sys.exit("Telegram is not configured: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.")
        notify.send_message("Shorts console: Telegram is connected. New videos will arrive here with their title, description and hashtags.")
        print("Test message sent.")
    elif args.action == "discover":
        try:
            chats = notify.discover_chats()
        except Exception as exc:  # noqa: BLE001
            sys.exit(f"Could not read chats: {exc}")
        if not chats:
            print("No chats found yet. Send your bot any message in Telegram, then run this again.")
        for c in chats:
            print(f"- chat_id {c['id']}  ({c['type']}) {c['name']}")
    elif args.action == "send":
        target = Path(args.path)
        out_dir = target if target.is_dir() else target.parent
        meta = load_json(out_dir / "meta.json")
        if not meta:
            sys.exit(f"No meta.json in {out_dir}")
        _notify_telegram(out_dir, meta, force=True)


def _recent_titles(limit: int = 45) -> list[str]:
    """Titles already made (permanent history + your channel's uploads + existing outputs)."""
    from . import history
    return history.avoid_titles(None, limit=limit)


def _read_custom_script(args) -> str:
    text = getattr(args, "script_text", None) or ""
    path = getattr(args, "script_file", None)
    if path:
        p = Path(path)
        if not p.exists():
            sys.exit(f"Script file not found: {p}")
        text = p.read_text(encoding="utf-8", errors="replace")
    return text.strip()


def _listify(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [v for v in re.split(r"[,\n]", value) if v.strip()]
    return list(value)


def _upload(out_dir: Path, force: bool = False, interactive: bool = True) -> None:
    from . import upload, upload_queue

    cfg = load_config()["upload"]
    meta = load_json(out_dir / "meta.json")
    if meta.get("youtube_id") and meta.get("privacy") != "deleted" and not force:
        print(f"Already on YouTube: https://youtube.com/shorts/{meta['youtube_id']} (not uploaded again).")
        return
    tags = meta["hashtags"]
    desc = meta["description"]
    from . import series
    series_info = None
    try:
        series_info = series.prepare(out_dir, meta)
        if series_info and series_info["block"] not in desc:
            desc = desc.rstrip() + "\n\n" + series_info["block"]
    except Exception as exc:  # noqa: BLE001
        log.warning("series links skipped: %s", str(exc)[:160])
    if "#shorts" not in desc.lower() and not meta.get("long_video"):
        desc += "\n\n#Shorts"
    publish_at = None
    if not interactive and cfg["privacy"] == "private":
        from . import publish_times
        if publish_times.config()["schedule_publish"]:
            publish_at = publish_times.rfc3339(publish_times.choose())
    try:
        resp = upload.upload_video(Path(meta["video"]), title=meta["title"], description=desc, tags=tags,
                                   privacy=cfg["privacy"], category_id=cfg["category_id"], interactive=interactive,
                                   publish_at=publish_at)
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
        note = upload_queue.on_upload_error(out_dir, error)    # learns the daily limit, counts attempts
        raise RuntimeError(error[:600] + (f" | {note}" if note else "")) from exc
    upload_queue.record_upload(out_dir)
    vid = resp.get("id")
    meta = load_json(out_dir / "meta.json") or meta           # re-read: the queue may have touched it
    meta["youtube_id"] = vid
    meta["privacy"] = "private" if publish_at else cfg["privacy"]
    meta["publish_at"] = publish_at
    meta["uploaded_at"] = time.time()
    if meta.get("upload_state") in ("queued", "expired", "failed", None):
        meta["upload_state"] = "uploaded"
    if series_info:
        meta["description"] = desc.replace("\n\n#Shorts", "") if desc.endswith("\n\n#Shorts") else desc
    save_json(out_dir / "meta.json", meta)
    if series_info:
        try:
            done = series.link_after_upload(out_dir, meta, series_info)
            prev = series_info.get("previous")
            print(f"Series: part {series_info['part']} of {series_info['series']!r}"
                  + (f"; previous part linked both ways ({prev['title'][:40]!r})" if prev else " (the first part)")
                  + (f"; done now: {len(done)}" if done else "") + ". Comments follow once the videos are public.")
        except Exception as exc:  # noqa: BLE001
            log.warning("series links failed: %s", str(exc)[:160])
    from . import housekeeping
    if housekeeping.cleanup_config()["clean_after_upload"]:
        housekeeping.remove_intermediates(out_dir)        # the render pieces are not needed any more
    print(f"Uploaded as {cfg['privacy']}: https://youtube.com/shorts/{vid}")
    if publish_at:
        from datetime import datetime as _dt
        when = _dt.fromisoformat(publish_at.replace("Z", "+00:00")).astimezone().strftime("%a %H:%M")
        print(f"Goes public by itself on {when} (a good hour for this channel). Until then: 'Publish now' or "
              "'Keep private' on its card, or change it in YouTube Studio.")
    elif cfg["privacy"] == "private":
        print("It is PRIVATE until you publish it: use 'Make public' on the card, `python main.py publish <dir>`, "
              "or set upload.privacy to public in Settings to skip the review step.")
    thumb = meta.get("thumbnail")
    if thumb and Path(thumb).exists():
        try:
            if _set_thumbnail_or_defer(out_dir, meta):
                print("Thumbnail set.")
            else:
                print("Thumbnail deferred: YouTube's daily custom-thumbnail limit is reached; it will be set on a later refresh.")
        except Exception as exc:  # noqa: BLE001 - custom thumbnails need a phone-verified channel
            log.warning("thumbnail not set: %s (YouTube requires a phone-verified channel for custom thumbnails)", str(exc)[:160])


def cmd_upload(args) -> None:
    target = Path(args.path)
    out_dir = target if target.is_dir() else target.parent
    auto = bool(getattr(args, "auto", False))
    if auto:
        meta = load_json(out_dir / "meta.json") or {}
        log.info("automatic upload: %r (priority %s/100)", meta.get("title", out_dir.name)[:70], meta.get("upload_priority", "?"))
    _upload(out_dir, force=bool(getattr(args, "force", False)), interactive=not auto)


def cmd_thumbnail(args) -> None:
    """Regenerate an output's thumbnail (also for videos made before thumbnails existed) and, when the
    video is on YouTube, set it there."""
    from . import thumbnail, upload

    target = Path(args.path)
    out_dir = target if target.is_dir() else target.parent
    meta = load_json(out_dir / "meta.json")
    script = load_json(out_dir / "script.json") or {}
    if not meta:
        sys.exit(f"No meta.json in {out_dir}")
    from . import frame
    frame.use(meta.get("orientation", "vertical"), long=bool(meta.get("long_video")))   # a long video's is landscape
    cfg = load_config()["production"]
    thumb_cfg = cfg.get("thumbnail") or {}
    if getattr(args, "regenerate", True) or not (out_dir / "thumbnail.jpg").exists():
        src, seek = thumbnail.source_for_output(out_dir, meta)
        if not src:
            sys.exit("nothing to build a thumbnail from")
        legacy = src.name == "short.mp4"
        thumb = thumbnail.make_thumbnail(script or {"title": meta.get("title", "")}, meta.get("segments") or [], out_dir,
                                         style=_caption_style(cfg), handle=thumb_cfg.get("handle") or (cfg.get("outro") or {}).get("handle", ""),
                                         band=thumb_cfg.get("band", True), source=src, seek_at=seek,
                                         band_alpha=0.88 if legacy else 0.7)
        if not thumb:
            sys.exit("thumbnail generation failed")
        meta["thumbnail"] = str(thumb)
        meta["thumbnail_text"] = thumbnail.thumbnail_text(script or {"title": meta.get("title", "")})
        save_json(out_dir / "meta.json", meta)
        print(f"thumbnail written: {thumb}{' (from the rendered video)' if legacy else ''}")
    if meta.get("youtube_id") and getattr(args, "set", True):
        if _set_thumbnail_or_defer(out_dir, meta):
            print(f"thumbnail set on https://youtube.com/shorts/{meta['youtube_id']} (YouTube can take a few minutes to show it)")
        else:
            print("YouTube's daily limit for custom thumbnails is reached; this one is marked pending and will be set "
                  "automatically on a later trend refresh (or run `thumbnail --keep` again tomorrow).")


def _set_thumbnail_or_defer(out_dir: Path, meta: dict) -> bool:
    """Set the thumbnail on YouTube; on the daily rate limit (HTTP 429) mark it pending for a later retry."""
    from . import upload

    try:
        upload.set_thumbnail(meta["youtube_id"], Path(meta["thumbnail"]))
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if "429" in msg or "RateLimit" in msg or "rateLimit" in msg:
            meta["thumbnail_pending"] = True
            save_json(out_dir / "meta.json", meta)
            return False
        raise
    meta.pop("thumbnail_pending", None)
    meta["thumbnail_set"] = True
    save_json(out_dir / "meta.json", meta)
    return True


def retry_pending_thumbnails(limit: int = 10) -> int:
    """Set thumbnails that were deferred by YouTube's daily limit. Stops at the first new 429."""
    done = 0
    for d in sorted(OUTPUT_DIR.iterdir()) if OUTPUT_DIR.exists() else []:
        meta = load_json(d / "meta.json")
        if not meta or not meta.get("thumbnail_pending") or not meta.get("youtube_id") or not meta.get("thumbnail"):
            continue
        if not Path(meta["thumbnail"]).exists():
            continue
        if _set_thumbnail_or_defer(d, meta):
            done += 1
            log.info("pending thumbnail set for %s", meta["youtube_id"])
            if done >= limit:
                break
        else:
            log.info("thumbnail limit still reached; %s stays pending", meta["youtube_id"])
            break
    return done


def cmd_publish(args) -> None:
    """Change the visibility of an already uploaded Short (private -> public after you reviewed it)."""
    from . import upload

    target = Path(args.path)
    out_dir = target if target.is_dir() else target.parent
    meta = load_json(out_dir / "meta.json")
    if not meta or not meta.get("youtube_id"):
        sys.exit(f"{out_dir.name} has not been uploaded yet.")
    privacy = getattr(args, "privacy", None) or "public"
    upload.set_privacy(meta["youtube_id"], privacy)       # replaces the whole status: any planned publish time is dropped
    meta["privacy"] = privacy
    meta["publish_at"] = None
    save_json(out_dir / "meta.json", meta)
    print(f"{meta['youtube_id']} is now {privacy}: https://youtube.com/shorts/{meta['youtube_id']}")


def cmd_run(args) -> None:
    run_dir = cmd_discover(args)
    args.run = str(run_dir)
    args.force = False
    cmd_judge(args)
    cmd_rank(args)
    cmd_analyze(args)
    _sync_channel_quietly()
    if args.produce:
        cmd_produce(args)


def _sync_channel_quietly() -> None:
    """Refresh your channel's stats as part of a trend refresh, when the API key and channel are set."""
    from . import channel

    if not channel.configured():
        return
    try:
        n = retry_pending_thumbnails()
        if n:
            log.info("set %d pending thumbnail(s) on YouTube", n)
    except Exception as exc:  # noqa: BLE001
        log.warning("pending thumbnails: %s", str(exc)[:160])
    try:
        report = channel.sync()
        perf = report["performance"]
        winners = channel.channel_blueprints(perf)
        if winners:
            log.info("channel winners: %s", ", ".join(f"{w['key']} x{w['opportunity']}" for w in winners))
        for key, p in sorted(perf["blueprints"].items(), key=lambda kv: -kv[1]["factor"]):
            log.info("channel feedback %s: %d videos, %.1f views/h, factor x%.2f%s", key, p["videos"],
                     p["median_views_per_hour"], p["factor"], " (provisional)" if p["provisional"] else "")
    except Exception as exc:  # noqa: BLE001
        log.warning("channel sync failed: %s", exc)


def cmd_profile(args) -> None:
    from . import profile as profiles

    if args.action == "add":
        if not args.target:
            sys.exit("give a channel URL, @handle or channel id")
        p = profiles.add_profile(args.target, max_videos=args.videos)
        print(f"Profile {p['handle']} ({p['channel']}): {p['videos']} Shorts analysed, typical length {p['typical_seconds']}s")
        print("  formats:", ", ".join(f"{f['name']} {f['share']:.0%}" for f in p["top_formats"]))
        print("  topics: ", ", ".join(f"{t['name']} {t['share']:.0%}" for t in p["top_topics"]))
        print("  hooks:  ", ", ".join(f"{h['name']} {h['share']:.0%}" for h in p["top_hooks"]))
        if p.get("style_guide"):
            print("  voice:  ", p["style_guide"]["voice_and_tone"])
        print(f"Produce in this style:  python main.py produce --profile {p['handle']}")
    elif args.action == "list":
        for p in profiles.list_profiles():
            print(f"- {p['handle']:<28} {p['channel'] or '':<30} {p['videos']} videos, ~{p['typical_seconds']}s, "
                  f"{', '.join(f['name'] for f in (p['top_formats'] or [])[:2])}")
        if not profiles.list_profiles():
            print("no profiles yet: python main.py profile add <channel url or @handle>")
    else:
        p = profiles.load_profile(args.target or "")
        if not p:
            sys.exit("no such profile")
        print(json.dumps({k: v for k, v in p.items() if k != "exemplars"}, indent=2, ensure_ascii=False))
        print(f"\ntheir Shorts ({len(p.get('exemplars') or [])}, most viewed first); remake one with  produce --profile {p['handle']} --exemplar <id>")
        for e in p.get("exemplars") or []:
            print(f"  {e['id']:<12} {int(e.get('views') or 0):>12,}  {e.get('format') or '?'} x {e.get('topic') or '?'}  {e.get('title', '')[:60]}")


def cmd_channel(args) -> None:
    from . import channel

    if args.action in ("connect-analytics", "connect_analytics"):
        from . import analytics
        print("A Google page opens: approve YouTube and YouTube Analytics (read-only).")
        try:
            data = analytics.fetch(interactive=True)
        except RuntimeError as exc:
            sys.exit(str(exc))
        print(f"YouTube Analytics connected: retention for {len(data['videos'])} videos.")
        args.action = "sync"
    if args.action == "sync":
        if not channel.configured():
            sys.exit("Set YOUTUBE_API_KEY and YOUTUBE_CHANNEL (your @handle or channel id) in .env or Settings first.")
        report = channel.sync()
    else:
        report = channel.cached_report()
        if not report:
            sys.exit("No channel data yet. Run `python main.py channel sync`.")
    ch, perf = report["channel"], report["performance"]
    print(f"{ch['title']} ({ch['id']}): {report['uploads']} Shorts on the channel, {perf.get('uploads_labelled', 0)} labelled "
          f"(format/topic), {len(report['matched'])} matched to local outputs, synced {report['fetched_at']}")
    print(f"channel median: {perf['channel_median_vph']} views/hour over {perf['videos']} mature videos"
          + (f", {perf['channel_median_view_pct']}% viewed on average ({perf['retention_videos']} videos with analytics)"
             if perf.get("channel_median_view_pct") else " (connect YouTube Analytics for retention)"))
    for key, p in sorted(perf["blueprints"].items(), key=lambda kv: -kv[1]["factor"]):
        print(f"  {key:<45} {p['videos']:>2} videos  {p['median_views_per_hour']:>8.1f} views/h"
              + (f"  {p['median_view_pct']:>5.0f}% viewed" if p.get("median_view_pct") is not None else "")
              + f"  factor x{p['factor']:.2f}"
              f"{'  (provisional: needs 3+ videos)' if p['provisional'] else ''}")
    winners = channel.channel_blueprints(perf)
    if winners:
        print("channel winners (produced regardless of the trend run): " + ", ".join(f"{w['key']} x{w['opportunity']}" for w in winners))
    uploads = sorted(channel.labelled_uploads(), key=lambda u: -float(u.get("views_per_hour") or 0))
    for m in uploads[:15]:
        print(f"  - {m['title'][:60]:<60} {m['views']:>7} views  {m['views_per_hour']:>7.1f}/h  [{m['format']}|{m['topic']}]")


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
    pr.add_argument("--topic", help='one Short of a topic series, e.g. --topic "bible stories" (a fresh subject inside it each time)')
    pr.add_argument("--profile", help="produce in the style of an analysed channel (see `profile add`)")
    pr.add_argument("--exemplar", help="with --profile: remake one of that channel's Shorts by video id (see `profile show`)")
    pr.add_argument("--dub-of", dest="dub_of", help="dub one of the main channel's Shorts (its output folder name) for this channel")
    pr.add_argument("--sequel-of", dest="sequel_of", help="make a Part 2 of this upload (YouTube video id)")
    pr.add_argument("--idea", help="make the Short a viewer asked for (comment thread id, see `comments list`)")
    pr.add_argument("--blueprint-key", dest="blueprint_key", help="one of your channel's winners, e.g. storytime|psychology_mind (see `channel report`)")
    pr.add_argument("--seconds", type=int, help="target length for --profile (default: the channel's typical length)")
    pr.add_argument("--script-file", help="use your own script (.txt) instead of generating one")
    pr.add_argument("--ai-images-only", dest="ai_images_only", action="store_true",
                    help="every shot is an AI-generated image (no internet footage)")
    pr.add_argument("--long", action="store_true",
                    help="with your own script: a long landscape video (not a Short); the script keeps its full length")
    pr.add_argument("--as-written", dest="enhance", action="store_false", default=True,
                    help="with --script-file: voice the text exactly as written instead of enhancing hook and flow")
    pr.add_argument("--title"); pr.add_argument("--description")
    pr.add_argument("--hashtags", help="comma separated, for --script-file")
    pr.add_argument("--keywords", help="comma separated stock-footage search terms, for --script-file")
    pr.add_argument("--music", help="none | random | part of a track name (default from config.yaml)")
    pr.add_argument("--no-intro", dest="intro", action="store_false", default=None, help="skip the intro card")
    pr.add_argument("--no-outro", dest="outro", action="store_false", default=None, help="skip the outro card")
    pr.add_argument("--no-telegram", dest="telegram", action="store_false", default=True, help="do not send this one to Telegram")
    pr.add_argument("--upload", action="store_true")
    pr.set_defaults(func=cmd_produce)

    u = sub.add_parser("upload", help="upload a produced Short (output/<dir> or its short.mp4)")
    u.add_argument("path")
    u.add_argument("--force", action="store_true", help="upload again even if this Short is already on YouTube")
    u.set_defaults(func=cmd_upload)

    tn = sub.add_parser("thumbnail", help="regenerate an output's thumbnail and set it on YouTube if uploaded")
    tn.add_argument("path"); tn.add_argument("--keep", dest="regenerate", action="store_false", default=True, help="keep the existing thumbnail.jpg")
    tn.add_argument("--no-set", dest="set", action="store_false", default=True, help="do not push it to YouTube")
    tn.set_defaults(func=cmd_thumbnail)

    pb = sub.add_parser("publish", help="change an uploaded Short's visibility (default: public)")
    pb.add_argument("path"); pb.add_argument("--privacy", choices=["private", "unlisted", "public"], default="public")
    pb.set_defaults(func=cmd_publish)

    ru = sub.add_parser("run", help="discover -> judge -> rank -> analyze [-> produce]")
    ru.add_argument("--api", action="store_true"); ru.add_argument("--top", type=int, default=20)
    ru.add_argument("--produce", action="store_true"); ru.add_argument("--blueprint", type=int, default=1)
    ru.add_argument("--angle"); ru.add_argument("--upload", action="store_true")
    ru.set_defaults(func=cmd_run)

    f = sub.add_parser("setup-ffmpeg", help="download a portable ffmpeg into data/bin (Windows)")
    f.set_defaults(func=cmd_setup_ffmpeg)

    m = sub.add_parser("music", help="background music library (assets/music)")
    m.add_argument("action", choices=["list", "fetch"])
    m.add_argument("--query", default="lofi chill", help="fetch: what to search for on Openverse (CC0 / CC-BY only)")
    m.add_argument("--count", type=int, default=5)
    m.set_defaults(func=cmd_music)

    t = sub.add_parser("telegram", help="Telegram delivery: test, discover chat ids, or send a produced Short")
    t.add_argument("action", choices=["test", "discover", "send"])
    t.add_argument("path", nargs="?", help="send: output/<dir> or its short.mp4")
    t.set_defaults(func=cmd_telegram)

    rp = sub.add_parser("report", help="print today's report (produced, uploaded, queue, failures); --send posts it to Telegram")
    rp.add_argument("--send", action="store_true")
    rp.set_defaults(func=cmd_report)

    cm = sub.add_parser("comments", help="question comments, reply drafts for viewer comments: connect | run | list | post <id> | dismiss <id>")
    cm.add_argument("action", nargs="?", default="run", choices=["connect", "run", "list", "post", "dismiss"])
    cm.add_argument("target", nargs="?")
    cm.add_argument("--text")
    cm.set_defaults(func=cmd_comments)

    st = sub.add_parser("stop", help="stop the console and its watchdog until `start` or the next logon")
    st.add_argument("--port", type=int, default=8787)
    st.add_argument("--now", action="store_true", help="stop even while a job is running (that job is lost)")
    st.set_defaults(func=cmd_stop)
    sa = sub.add_parser("start", help="start the console and its watchdog again after `stop`")
    sa.add_argument("--port", type=int, default=8787)
    sa.set_defaults(func=cmd_start)
    sc = sub.add_parser("shortcuts", help="put Stop / Start Shorts console shortcuts on the desktop")
    sc.set_defaults(func=cmd_shortcuts)

    vc = sub.add_parser("vision-check", help="look at the footage of existing Shorts with TypeSafe images (compare with the text-only picks)")
    vc.add_argument("dirs", nargs="*", help="output folder names (default: the newest Shorts whose clips are on disk)")
    vc.add_argument("--limit", type=int, default=3)
    vc.set_defaults(func=cmd_vision_check)

    co = sub.add_parser("compile", help="this week's compilation: the best uploaded Shorts on one topic, one landscape video with chapters")
    co.add_argument("--topic", help="only this series or topic label (default: the topic with the most Shorts this week)")
    co.add_argument("--days", type=float, help="look back this many days (default: schedule.compilations.window_days)")
    co.add_argument("--no-upload", dest="no_upload", action="store_true", help="render only; upload from its card later")
    co.set_defaults(func=cmd_compile)

    up = sub.add_parser("update", help="pull the latest engine code from GitHub, install requirements, restart the console")
    up.add_argument("--check", action="store_true", help="only say what is new")
    up.add_argument("--no-restart", dest="no_restart", action="store_true")
    up.set_defaults(func=cmd_update)
    rs = sub.add_parser("restart", help="stop and start the console (used by the updater)")
    rs.add_argument("--port", type=int, default=8787)
    rs.add_argument("--delay", type=int, default=3)
    rs.set_defaults(func=cmd_restart)

    bk = sub.add_parser("backup", help="back up what the engine learned (nightly by itself): now | list | restore <zip>")
    bk.add_argument("action", nargs="?", default="now", choices=["now", "list", "restore"])
    bk.add_argument("target", nargs="?")
    bk.set_defaults(func=cmd_backup)

    wd = sub.add_parser("watchdog", help="start the console and restart it if it stops answering (used by autostart)")
    wd.add_argument("--port", type=int, default=8787)
    wd.set_defaults(func=cmd_watchdog)

    sv = sub.add_parser("setup-voice", help="download the free local Kokoro voice (~120 MB), used when edge-tts fails")
    sv.set_defaults(func=cmd_setup_voice)

    cl = sub.add_parser("cleanup", help="free disk: render pieces of uploaded Shorts, old video files (YouTube has them)")
    cl.add_argument("--dry-run", dest="dry_run", action="store_true")
    cl.set_defaults(func=cmd_cleanup)

    qp = sub.add_parser("queue", help="show the automatic upload queue")
    qp.set_defaults(func=cmd_queue)

    pf = sub.add_parser("profile", help="analyse a YouTube channel's style so videos can be made in that style")
    pf.add_argument("action", choices=["add", "list", "show"])
    pf.add_argument("target", nargs="?", help="channel URL, @handle or channel id")
    pf.add_argument("--videos", type=int, default=24, help="add: how many of its most-viewed Shorts to analyse")
    pf.set_defaults(func=cmd_profile)

    ch = sub.add_parser("channel", help="your channel's stats feeding back into the ranking (needs YOUTUBE_API_KEY + YOUTUBE_CHANNEL)")
    ch.add_argument("action", choices=["sync", "report", "connect-analytics", "connect_analytics"])
    ch.set_defaults(func=cmd_channel)

    s = sub.add_parser("schedule", help="show today's scheduled slots (the scheduler itself runs inside `web`)")
    s.set_defaults(func=cmd_schedule)

    chp = sub.add_parser("channels", help="run several YouTube channels from this install: list | add <name> | label <name>")
    chp.add_argument("action", choices=["list", "add", "label"])
    chp.add_argument("name", nargs="?")
    chp.add_argument("--label", default="")
    chp.add_argument("--port", type=int, default=0)
    chp.set_defaults(func=cmd_channels)

    a = sub.add_parser("autostart", help="Windows: start the console automatically at logon so the schedule runs")
    a.add_argument("action", choices=["install", "remove", "status"])
    a.add_argument("--port", type=int, default=8787)
    a.set_defaults(func=cmd_autostart)

    w = sub.add_parser("web", help="start the local web console (keys, settings, runs, studio)")
    w.add_argument("--port", type=int, default=8787); w.add_argument("--host", default="127.0.0.1")
    w.set_defaults(func=cmd_web)
    return p


def cmd_music(args) -> None:
    from . import music
    if args.action == "fetch":
        added = music.fetch_tracks(args.query, args.count)
        print(f"added {len(added)} track(s)")
    for t in music.list_tracks():
        credit = music.attribution_line(t)
        print(f"- {t['file']}  [{t['license']}] {t['title']}{' by ' + t['creator'] if t['creator'] else ''}"
              f"{'  (credit required)' if credit else ''}")


def cmd_schedule(args) -> None:
    from .scheduler import Scheduler
    plan = Scheduler(lambda *_: None, lambda *_: None).plan()
    cfg = plan["config"]
    print(f"scheduler {'ENABLED' if cfg['enabled'] else 'disabled'}: {cfg['produces_per_day']} produce/day, "
          f"{cfg['refresh_per_day']} refresh/day, window {cfg['start_hour']:02.0f}:00-{cfg['end_hour']:02.0f}:00, "
          f"rotating {cfg['blueprints_to_rotate']} blueprints")
    for slot in plan["today"]:
        mark = "done" if slot["done"] else (("missed" if cfg["enabled"] else "past") if slot["past"] else "")
        print(f"  {slot['time']}  {slot['kind']:<8} {mark}")
    print("The schedule runs while `python main.py web` is running; use `autostart install` to keep it running.")


AUTOSTART_NAME = "ShortsConsole.vbs"


def _startup_script() -> Path:
    from .config import CHANNEL
    appdata = os.environ.get("APPDATA", "")
    name = AUTOSTART_NAME if not CHANNEL else AUTOSTART_NAME.replace(".vbs", f"-{CHANNEL}.vbs")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / name


def cmd_autostart(args) -> None:
    """Per-user Startup folder entry (no admin rights needed) that launches the console hidden at logon."""
    import platform

    if platform.system() != "Windows":
        sys.exit("autostart is implemented for Windows; on macOS/Linux use launchd/systemd to run `python main.py web`.")
    script = _startup_script()
    if args.action == "status":
        print(f"installed: {script}" if script.exists() else "not installed")
        return
    if args.action == "remove":
        if script.exists():
            script.unlink()
            print(f"removed {script}")
        else:
            print("nothing to remove")
        return
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    exe = pythonw if pythonw.exists() else Path(sys.executable)
    from .config import CHANNEL, channels
    main_py = ROOT / "main.py"
    port = args.port
    if CHANNEL and port == 8787:                    # default port: use the one registered for this channel
        port = next((c["port"] for c in channels() if c["name"] == CHANNEL), port)
    channel_arg = f" --channel {CHANNEL}" if CHANNEL else ""
    # WScript.Shell.Run with window style 0 = hidden; pythonw avoids a console window as well
    vbs = ('Set sh = CreateObject("WScript.Shell")\n'
           f'sh.CurrentDirectory = "{ROOT}"\n'
           f'sh.Run """{exe}"" ""{main_py}""{channel_arg} watchdog --port {port}", 0, False\n')
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(vbs, encoding="utf-8")
    print(f"Installed {script}")
    print("At every logon a hidden watchdog starts the console and restarts it if it stops answering (Telegram alert).")
    print("To start it right now without logging out, double-click that file")
    print("or run:  wscript \"" + str(script) + "\"")
    print("Keep the machine awake: Windows Settings -> System -> Power -> Sleep: Never (while plugged in).")
    cmd_shortcuts(args)


def _console_port(port: int) -> int:
    from .config import CHANNEL, channels
    if CHANNEL and port == 8787:                    # default port: use the one registered for this channel
        port = next((c["port"] for c in channels() if c["name"] == CHANNEL), port)
    return port


def cmd_stop(args) -> None:
    from . import watchdog
    print(watchdog.stop(_console_port(args.port), now=args.now))


def cmd_start(args) -> None:
    from . import watchdog
    print(watchdog.start(_console_port(args.port)))


def _desktop() -> Path:
    """The real Desktop folder (OneDrive often moves it)."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as k:
            return Path(os.path.expandvars(winreg.QueryValueEx(k, "Desktop")[0]))
    except OSError:
        return Path.home() / "Desktop"


def cmd_shortcuts(args) -> None:
    """Double-click files on the desktop: stop the console (and its watchdog) or start them again."""
    from .config import CHANNEL, channel_label
    channel_arg = f" --channel {CHANNEL}" if CHANNEL else ""
    name = "Shorts console" + (f" ({channel_label()})" if CHANNEL else "")
    made = []
    for action in ("stop", "start"):
        path = _desktop() / f"{action.capitalize()} {name}.cmd"
        path.write_text("@echo off\r\n"
                        f'cd /d "{ROOT}"\r\n'
                        f'"{sys.executable}" main.py{channel_arg} {action}\r\n'
                        "echo.\r\npause\r\n", encoding="utf-8")
        made.append(path)
    print("On your desktop:\n" + "\n".join(f"  {p}" for p in made))


def cmd_setup_ffmpeg(args) -> Path:
    from . import tools
    path = tools.install_ffmpeg()
    print(f"ffmpeg ready: {path}")
    return path


def cmd_web(args) -> None:
    from .config import CHANNEL, channels
    from .web.server import serve
    port = args.port
    if CHANNEL and port == 8787:                    # each channel's console has its own port
        port = next((c["port"] for c in channels() if c["name"] == CHANNEL), 8788)
    serve(host=args.host, port=port)


def cmd_channels(args) -> None:
    """List the YouTube channels this install runs, or add one (its own folder, Google sign-in and console port)."""
    import json
    import socket
    from .config import CHANNELS_PATH, ROOT as _ROOT, channels

    data = json.loads(CHANNELS_PATH.read_text(encoding="utf-8")) if CHANNELS_PATH.exists() else {}
    if args.action == "list":
        for c in channels():
            with socket.socket() as sock:
                sock.settimeout(0.3)
                up = sock.connect_ex(("127.0.0.1", int(c["port"]))) == 0
            folder = _ROOT if c["name"] == "main" else _ROOT / "channels" / c["name"]
            print(f"  {c['name']:<12} {c.get('label', ''):<24} http://127.0.0.1:{c['port']}  {'running' if up else 'stopped'}  {folder}")
        return
    name = re.sub(r"[^a-z0-9_-]", "", (args.name or "").lower())
    if args.action == "label":
        if name == "main":
            data["main_label"] = args.label
        else:
            for c in data.get("channels", []):
                if c["name"] == name:
                    c["label"] = args.label
        CHANNELS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
        print(f"{name} is now labelled {args.label!r}")
        return
    if not name or name == "main":
        sys.exit("Give the new channel a short name: letters, digits, - or _ (not 'main').")
    if any(c["name"] == name for c in channels()):
        sys.exit(f"A channel named {name!r} already exists.")
    used = {int(c["port"]) for c in channels()}
    port = args.port or next(p for p in range(8788, 8900) if p not in used)
    folder = _ROOT / "channels" / name
    for sub in ("data", "output"):
        (folder / sub).mkdir(parents=True, exist_ok=True)
    data.setdefault("channels", []).append({"name": name, "label": args.label or name, "port": port})
    CHANNELS_PATH.parent.mkdir(parents=True, exist_ok=True)
    CHANNELS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"Added channel {name!r} ({args.label or name}) in {folder}, console port {port}.")
    print("Next:")
    print(f"  1. Put that channel's Google OAuth file in {folder / 'client_secrets.json'} (or upload it in its console's Settings).")
    print(f"  2. Start its console:   python main.py --channel {name} web")
    print(f"     and open http://127.0.0.1:{port} - Settings there are this channel's own (schedule, upload, Telegram chat...).")
    print(f"  3. Click Upload on a video or Sync channel now and sign in with THAT channel's Google account.")
    print(f"  4. Start it at logon:   python main.py --channel {name} autostart install")
    print("API keys (TypeSafe, Claude, Pexels...) are shared from the main .env unless you set them again in its Settings.")


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    args.func(args)
