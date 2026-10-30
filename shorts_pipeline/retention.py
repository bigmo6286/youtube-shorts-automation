"""Drop-off learning: where viewers leave each Short, and what that says about how to write the next one.

YouTube Analytics gives a retention curve per video (audienceWatchRatio at 100 points of the video; above 1.0 at
the start means replays). Each Short's curve is lined up with its script (hook end, ending start, from the timeline
saved at production, else from word counts) to get: how many viewers are still there when the hook ends, how many
reach the ending, where the steepest loss is, and how strongly the Short replays. Across videos, code compares hook
length, hook style and section losses and writes plain guidance for the writer, which goes into the script prompt
next to the best openings. Curves are fetched a few at a time at each channel sync and cached.
"""
from __future__ import annotations

import logging
import statistics as st
import time
from typing import Any

from .config import DATA_DIR, OUTPUT_DIR
from .storage import load_json, save_json

log = logging.getLogger(__name__)

CURVES_PATH = DATA_DIR / "retention_curves.json"
MIN_VIEWS = 100                 # a curve from fewer views is noise
PER_SYNC = 15                   # curves fetched per sync (one API call each)
REFRESH_DAYS = 14               # a growing video's curve is fetched again after this long
MIN_VIDEOS = 6                  # guidance needs at least this many curves
MIN_GAP = 0.05                  # a difference between groups counts from 5 points


def fetch_curves(max_new: int = PER_SYNC) -> int:
    """Fetch retention curves for the most-viewed Shorts that have none yet (or a stale one). Returns how many."""
    from .analytics import _service, cached
    from .upload import with_retries
    from datetime import date, timedelta
    videos = (cached() or {}).get("videos") or {}
    store = load_json(CURVES_PATH) or {}
    now = time.time()
    todo = []
    for vid, v in sorted(videos.items(), key=lambda kv: -int(kv[1].get("views") or 0)):
        if int(v.get("views") or 0) < MIN_VIEWS:
            continue
        have = store.get(vid)
        if have and (now - float(have.get("at") or 0) < REFRESH_DAYS * 86400 or int(have.get("views") or 0) >= 5000):
            continue
        todo.append((vid, v))
        if len(todo) >= max_new:
            break
    if not todo:
        return 0
    yt = _service()
    start, end = (date.today() - timedelta(days=365)).isoformat(), date.today().isoformat()
    done = 0
    for vid, v in todo:
        try:
            resp = with_retries(lambda: yt.reports().query(
                ids="channel==MINE", startDate=start, endDate=end, metrics="audienceWatchRatio",
                dimensions="elapsedVideoTimeRatio", filters=f"video=={vid}").execute(), "read a retention curve")
        except Exception as exc:  # noqa: BLE001
            log.warning("retention curve for %s failed: %s", vid, str(exc)[:120])
            break
        rows = resp.get("rows") or []
        if len(rows) < 20:
            continue
        curve = [round(float(r[1]), 3) for r in sorted(rows, key=lambda r: float(r[0]))]
        store[vid] = {"curve": curve, "views": int(v.get("views") or 0), "at": now}
        done += 1
    save_json(CURVES_PATH, store)
    return done


def _at(curve: list[float], frac: float) -> float:
    if not curve:
        return 0.0
    i = min(len(curve) - 1, max(0, int(round(frac * (len(curve) - 1)))))
    return curve[i]


def _outputs_by_video() -> dict[str, tuple[dict[str, Any], dict[str, Any]]]:
    out = {}
    if not OUTPUT_DIR.exists():
        return out
    for d in OUTPUT_DIR.iterdir():
        meta = load_json(d / "meta.json") if d.is_dir() else None
        if meta and meta.get("youtube_id"):
            out[meta["youtube_id"]] = (meta, load_json(d / "script.json") or {})
    return out


def measure(curve: list[float], meta: dict[str, Any], script: dict[str, Any]) -> dict[str, Any] | None:
    """One video's numbers: hold at the hook end, at the ending start, the steepest-losing section, replay strength."""
    duration = float(meta.get("duration") or 0)
    hook = (script.get("hook") or "").split()
    cta = (script.get("cta") or "").split()
    body = [w for ln in script.get("lines") or [] for w in ln["text"].split()]
    total = len(hook) + len(body) + len(cta)
    if not curve or not total or not hook:
        return None
    tl = meta.get("timeline") or {}
    hook_end = float(tl["hook_end"]) / duration if tl.get("hook_end") and duration else len(hook) / total
    cta_start = float(tl["cta_start"]) / duration if tl.get("cta_start") and duration else 1 - len(cta) / total
    hook_end, cta_start = max(0.02, min(0.6, hook_end)), max(hook_end + 0.05, min(0.98, cta_start))
    start = _at(curve, 0.02)
    # Shorts replay, so YouTube's ratio can be above 1.0 at the start; holds are measured against each video's own
    # start (1.0 = everyone who began is still there) and the replay strength is kept separately.
    norm = start if start > 0.05 else 1.0
    curve = [v / norm for v in curve]
    sections = {"hook": (0.02, hook_end), "early body": (hook_end, (hook_end + cta_start) / 2),
                "late body": ((hook_end + cta_start) / 2, cta_start), "ending": (cta_start, 0.98)}
    losses = {}
    for name, (a, b) in sections.items():
        length = max(0.01, b - a)
        losses[name] = round((_at(curve, a) - _at(curve, b)) / length, 3)      # loss per unit of video
    return {"start": round(start, 3), "hook_hold": round(min(1.0, _at(curve, hook_end)), 3), "end_hold": round(min(1.0, _at(curve, cta_start)), 3),
            "finish": round(_at(curve, 0.98), 3), "worst_section": max(losses, key=losses.get), "losses": losses,
            "hook_words": len(hook), "cta_words": len(cta), "hook_style": (meta.get("blueprint") or {}).get("hook_style"),
            "format": (meta.get("blueprint") or {}).get("format"), "views": 0}


def learn() -> dict[str, Any]:
    """Guidance for the writer from every measured video, saved under tuning.json['retention']."""
    from .tuning import TUNING_PATH
    store = load_json(CURVES_PATH) or {}
    outputs = _outputs_by_video()
    rows = []
    for vid, rec in store.items():
        if vid in outputs:
            m = measure(rec["curve"], *outputs[vid])
            if m:
                m["views"] = rec.get("views", 0)
                rows.append(m)
    result: dict[str, Any] = {"videos": len(rows), "updated": time.time(), "guidance": []}
    tuning = load_json(TUNING_PATH) or {}
    if len(rows) < MIN_VIDEOS:
        result["note"] = f"{len(rows)} curves so far; guidance from {MIN_VIDEOS}"
        tuning["retention"] = result
        save_json(TUNING_PATH, tuning)
        return result
    hook_hold = st.mean(r["hook_hold"] for r in rows)
    end_hold = st.mean(r["end_hold"] for r in rows)
    result.update(hook_hold=round(hook_hold, 3), end_hold=round(end_hold, 3),
                  replay=round(st.mean(r["start"] for r in rows), 3))
    g = result["guidance"]

    # 1. hook length
    med_words = int(st.median(r["hook_words"] for r in rows))
    short = [r["hook_hold"] for r in rows if r["hook_words"] <= med_words]
    long_ = [r["hook_hold"] for r in rows if r["hook_words"] > med_words]
    if len(short) >= 3 and len(long_) >= 3:
        if st.mean(short) - st.mean(long_) >= MIN_GAP:
            g.append(f"Hooks of {med_words} words or fewer keep {st.mean(short):.0%} of viewers to the end of the hook; longer "
                     f"hooks keep {st.mean(long_):.0%}. Keep the hook to at most {med_words} words.")
        elif st.mean(long_) - st.mean(short) >= MIN_GAP:
            g.append(f"Longer hooks (over {med_words} words) hold viewers better here ({st.mean(long_):.0%} vs {st.mean(short):.0%}): "
                     "a hook may take a full sentence to set up its tension.")
    # 2. hook style
    by_style: dict[str, list[float]] = {}
    for r in rows:
        if r.get("hook_style"):
            by_style.setdefault(r["hook_style"], []).append(r["hook_hold"])
    styles = {k: st.mean(v) for k, v in by_style.items() if len(v) >= 3}
    if len(styles) >= 2:
        best, worst = max(styles, key=styles.get), min(styles, key=styles.get)
        if styles[best] - styles[worst] >= MIN_GAP:
            g.append(f"'{best.replace('_', ' ')}' hooks hold {styles[best]:.0%} of viewers past the hook, '{worst.replace('_', ' ')}' "
                     f"hooks {styles[worst]:.0%}: prefer the former unless the subject demands otherwise.")
    # 3. where viewers leave
    worst = {}
    for r in rows:
        worst[r["worst_section"]] = worst.get(r["worst_section"], 0) + 1
    section, n = max(worst.items(), key=lambda kv: kv[1])
    if n / len(rows) >= 0.4:
        advice = {"hook": "the first sentence must land the tension faster",
                  "early body": "the second and third lines must already deliver something, not set up",
                  "late body": "cut the weakest middle line and bring the payoff earlier",
                  "ending": "make the last line shorter and sharper so viewers reach the replay"}[section]
        g.append(f"Viewers leave fastest during the {section} in {n} of {len(rows)} Shorts: {advice}.")
    # 4. replays
    loopers = [r for r in rows if r["start"] >= 1.1]
    others = [r for r in rows if r["start"] < 1.1]
    if len(loopers) >= 3 and len(others) >= 3:
        lw, ow = st.mean(r["cta_words"] for r in loopers), st.mean(r["cta_words"] for r in others)
        if ow - lw >= 2:
            g.append(f"Shorts that replay most end on about {lw:.0f} words; the others on {ow:.0f}: keep the closing bridge "
                     f"to {int(lw) + 1} words or fewer.")
    tuning["retention"] = result
    save_json(TUNING_PATH, tuning)
    return result


def guidance() -> list[str]:
    from .tuning import TUNING_PATH
    return list(((load_json(TUNING_PATH) or {}).get("retention") or {}).get("guidance") or [])


def sync(max_new: int = PER_SYNC) -> str:
    try:
        n = fetch_curves(max_new)
    except Exception as exc:  # noqa: BLE001
        return f"retention curves: not fetched ({str(exc)[:120]})"
    r = learn()
    return (f"retention curves: {n} new, {r['videos']} measured"
            + (f"; hook hold {r['hook_hold']:.0%}, reach the ending {r['end_hold']:.0%}, {len(r['guidance'])} guidance lines"
               if r.get("hook_hold") is not None else f"; {r.get('note', '')}"))
