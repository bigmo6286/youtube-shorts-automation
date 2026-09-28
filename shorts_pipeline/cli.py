"""Command line entry point. See README for the workflow."""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from . import analyze, fetch, judge, rank
from .config import OUTPUT_DIR, ROOT, has_typesafe, load_config
from .storage import RUNS_DIR, latest_run_dir, load_json, new_run_dir, now_iso, save_json

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
    from . import captions, footage, music, render, script_gen, tools, tts

    if not tools.ensure_ffmpeg_on_path():     # check before spending a script generation
        sys.exit(tools.MISSING_HELP)
    cfg = load_config()["production"]

    script_text = _read_custom_script(args)
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
                script = script_gen.enhance_script(script_text, max_attempts=cfg["script_max_attempts"],
                                                   min_hook_score=cfg["script_min_hook_score"],
                                                   backend=cfg.get("script_backend", "auto"), **kwargs)
                qa = script.get("qa") or {}
                if qa:
                    log.info("enhanced script: hook %.1f/3, clarity %.1f/2, payoff %.0f%%, faithful to original %.0f%%",
                             qa["hook_strength"]["score"], qa["clarity"]["score"], qa["has_payoff"]["noul"] * 100,
                             qa.get("faithful", {}).get("noul", 1.0) * 100)
            else:
                script = script_gen.custom_script(script_text, **kwargs)
        except (ValueError, RuntimeError) as exc:
            sys.exit(f"Script preparation failed: {exc}")
        blueprint = {"format": "custom", "topic": "custom", "hook_style": "enhanced" if enhance else "as written",
                     "why_it_works": "user-written script" + (" edited for hook, flow and payoff" if enhance else "")}
        run_name = "custom"
        log.info("custom script%s: %d words, title %r", " (enhanced)" if enhance else " (as written)", script["word_count"], script["title"])
        out_dir = OUTPUT_DIR / f"{now_iso()}_custom"
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
            script = script_gen.generate_script(blueprint, target_seconds=cfg["target_seconds"], angle=args.angle,
                                                max_attempts=cfg["script_max_attempts"], min_hook_score=cfg["script_min_hook_score"],
                                                backend=cfg.get("script_backend", "auto"), avoid_titles=_recent_titles())
        except RuntimeError as exc:
            sys.exit(f"Script writing failed: {exc}")

    out_dir.mkdir(parents=True, exist_ok=True)
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
    words = tts.synthesize(script["full_text"], voice_path, voice=cfg["voice"], rate=cfg["voice_rate"])
    body_seconds = max(render.probe_duration(voice_path), words[-1]["end"] if words else 1.0) + 0.4
    save_json(out_dir / "words.json", words)

    intro = _card(cfg.get("intro") or {}, "intro", script, out_dir, cfg, enabled=getattr(args, "intro", None))
    outro = _card(cfg.get("outro") or {}, "outro", script, out_dir, cfg, enabled=getattr(args, "outro", None))
    intro_seconds = intro["seconds"] if intro else 0.0
    total = intro_seconds + body_seconds + (outro["seconds"] if outro else 0.0)
    log.info("timeline: intro %.1fs + body %.1fs + outro %.1fs = %.1fs", intro_seconds, body_seconds,
             outro["seconds"] if outro else 0.0, total)

    # Captions are burned onto the body stream before the intro is concatenated in front of it, so their
    # clock is body-local: no offset here (the intro card carries its own subtitle file).
    ass_path = captions.write_ass(words, out_dir / "captions.ass", style=_caption_style(cfg), full_text=script["full_text"])
    segments = footage.plan_backgrounds(script, words, body_seconds, cfg["background_source"], out_dir)

    music_cfg = dict(cfg.get("music") or {})
    if "music_volume_db" in cfg and "volume_db" not in music_cfg:      # older config.yaml
        music_cfg["volume_db"] = cfg["music_volume_db"]
    track = music.pick_track(getattr(args, "music", None) or music_cfg.get("default", "random"))
    description = script["description"]
    if track:
        log.info("music: %s (%s%s)", track["file"], track["license"], f", by {track['creator']}" if track.get("creator") else "")
        credit = music.attribution_line(track)
        if credit and credit not in description:
            description = description.rstrip() + "\n\n" + credit
    else:
        log.info("music: none%s", "" if music.list_tracks() else " (assets/music is empty: fetch or upload tracks in Settings)")
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
                          fade_seconds=float(music_cfg.get("fade_seconds", 1.5)), intro=intro, outro=outro)
    meta = {"run": run_name, "blueprint": blueprint, "video": str(video),
            "title": script["title"], "description": description,
            "hashtags": script["hashtags"], "duration": total,
            "music": {k: track[k] for k in ("file", "title", "creator", "license")} if track else None,
            "intro": bool(intro), "outro": bool(outro), "thumbnail": str(thumb) if thumb else None,
            "segments": segments,
            "thumbnail_text": script.get("thumbnail_text", ""),
            "mode": "custom" if script.get("backend") == "custom" else ("enhanced" if script.get("original_text") else "blueprint")}
    save_json(out_dir / "meta.json", meta)
    print(f"\nRendered {video}  ({total:.1f}s)")
    if args.upload:
        _upload(out_dir)
    if getattr(args, "telegram", True):
        _notify_telegram(out_dir, meta)
    return out_dir


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


def _notify_telegram(out_dir: Path, meta: dict, *, force: bool = False) -> None:
    from . import notify

    tcfg = (load_config().get("notifications") or {}).get("telegram") or {}
    if not force and not tcfg.get("on_produce", True):
        return
    if not notify.telegram_configured():
        if force:
            sys.exit("Telegram is not configured: add the bot token and chat id in Settings.")
        return
    try:
        note = f"New Short ready ({meta.get('duration', 0):.0f}s)" + (f" · https://youtube.com/shorts/{meta['youtube_id']}" if meta.get("youtube_id") else "")
        notify.send_short(Path(meta["video"]), meta, note=note)
        print("Sent to Telegram.")
    except Exception as exc:  # noqa: BLE001 - delivery must never fail the render
        log.error("Telegram delivery failed: %s", exc)
        if force:
            sys.exit(f"Telegram delivery failed: {exc}")


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


def _recent_titles(limit: int = 20) -> list[str]:
    """Titles of the last produced Shorts, so the writer does not repeat subjects."""
    if not OUTPUT_DIR.exists():
        return []
    titles = []
    for d in sorted((p for p in OUTPUT_DIR.iterdir() if p.is_dir()), reverse=True)[:limit]:
        meta = load_json(d / "meta.json")
        if meta and meta.get("title"):
            titles.append(meta["title"])
    return titles


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
    meta["privacy"] = cfg["privacy"]
    save_json(out_dir / "meta.json", meta)
    print(f"Uploaded as {cfg['privacy']}: https://youtube.com/shorts/{vid}")
    if cfg["privacy"] == "private":
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
    _upload(out_dir)


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
    upload.set_privacy(meta["youtube_id"], privacy)
    meta["privacy"] = privacy
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
        for key, p in sorted(perf["blueprints"].items(), key=lambda kv: -kv[1]["factor"]):
            log.info("channel feedback %s: %d videos, %.1f views/h, factor x%.2f%s", key, p["videos"],
                     p["median_views_per_hour"], p["factor"], " (provisional)" if p["provisional"] else "")
    except Exception as exc:  # noqa: BLE001
        log.warning("channel sync failed: %s", exc)


def cmd_channel(args) -> None:
    from . import channel

    if args.action == "sync":
        if not channel.configured():
            sys.exit("Set YOUTUBE_API_KEY and YOUTUBE_CHANNEL (your @handle or channel id) in .env or Settings first.")
        report = channel.sync()
    else:
        report = channel.cached_report()
        if not report:
            sys.exit("No channel data yet. Run `python main.py channel sync`.")
    ch, perf = report["channel"], report["performance"]
    print(f"{ch['title']} ({ch['id']}): {report['uploads']} Shorts on the channel, {len(report['matched'])} matched to produced videos, "
          f"synced {report['fetched_at']}")
    print(f"channel median: {perf['channel_median_vph']} views/hour over {perf['videos']} mature videos")
    for key, p in sorted(perf["blueprints"].items(), key=lambda kv: -kv[1]["factor"]):
        print(f"  {key:<45} {p['videos']:>2} videos  {p['median_views_per_hour']:>8.1f} views/h  factor x{p['factor']:.2f}"
              f"{'  (provisional: needs 2+ videos)' if p['provisional'] else ''}")
    for m in report["matched"][:15]:
        print(f"  - {m['title'][:60]:<60} {m['views']:>7} views  {m['views_per_hour']:>7.1f}/h  [{m['key']}]")


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
    pr.add_argument("--script-file", help="use your own script (.txt) instead of generating one")
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

    ch = sub.add_parser("channel", help="your channel's stats feeding back into the ranking (needs YOUTUBE_API_KEY + YOUTUBE_CHANNEL)")
    ch.add_argument("action", choices=["sync", "report"])
    ch.set_defaults(func=cmd_channel)

    s = sub.add_parser("schedule", help="show today's scheduled slots (the scheduler itself runs inside `web`)")
    s.set_defaults(func=cmd_schedule)

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
    appdata = os.environ.get("APPDATA", "")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / AUTOSTART_NAME


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
    main_py = ROOT / "main.py"
    # WScript.Shell.Run with window style 0 = hidden; pythonw avoids a console window as well
    vbs = ('Set sh = CreateObject("WScript.Shell")\n'
           f'sh.CurrentDirectory = "{ROOT}"\n'
           f'sh.Run """{exe}"" ""{main_py}"" web --port {args.port}", 0, False\n')
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(vbs, encoding="utf-8")
    print(f"Installed {script}")
    print("The console now starts hidden at every logon. To start it right now without logging out, double-click that file")
    print("or run:  wscript \"" + str(script) + "\"")
    print("Keep the machine awake: Windows Settings -> System -> Power -> Sleep: Never (while plugged in).")


def cmd_setup_ffmpeg(args) -> Path:
    from . import tools
    path = tools.install_ffmpeg()
    print(f"ffmpeg ready: {path}")
    return path


def cmd_web(args) -> None:
    from .web.server import serve
    serve(host=args.host, port=args.port)


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    args.func(args)
