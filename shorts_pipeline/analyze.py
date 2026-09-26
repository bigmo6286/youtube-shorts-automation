"""Turn ranked, judged Shorts into "what should I make" blueprints."""
from __future__ import annotations

from collections import defaultdict
from statistics import median
from typing import Any


def build_blueprints(shorts: list[dict[str, Any]], ranking_cfg: dict[str, Any], top_n: int = 8) -> dict[str, Any]:
    min_conf = ranking_cfg.get("min_format_confidence", 0.35)
    usable = [s for s in shorts if s.get("judgment") and not s.get("excluded")]
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    hook_by_group: dict[tuple[str, str], dict[str, float]] = defaultdict(lambda: defaultdict(float))
    format_totals: dict[str, float] = defaultdict(float)
    topic_totals: dict[str, float] = defaultdict(float)

    for s in usable:
        j = s["judgment"]
        fmt, topic = j["format"], j["topic"]
        if fmt["confidence"] < min_conf or fmt["choice"] == "other":
            continue
        key = (fmt["choice"], topic["choice"])
        groups[key].append(s)
        format_totals[fmt["choice"]] += s["score"]
        topic_totals[topic["choice"]] += s["score"]
        # probability-weighted hook tally, so an uncertain hook read does not dominate
        for hook, p in j["hook_style"]["probabilities"].items():
            hook_by_group[key][hook] += p * s["score"]

    min_replic = ranking_cfg.get("min_blueprint_replicable", 0.6)
    per_channel_cap = ranking_cfg.get("max_per_channel", 2)
    blueprints = []
    for (fmt, topic), items in groups.items():
        items.sort(key=lambda x: -x["score"])
        # one channel reposting the same clip must not look like a trend: cap what each channel contributes
        counted: list[dict[str, Any]] = []
        per_channel: dict[str, int] = defaultdict(int)
        for s in items:
            ch = s.get("channel_id") or s.get("channel") or s["id"]
            if per_channel[ch] < per_channel_cap:
                per_channel[ch] += 1
                counted.append(s)
        items = counted
        opportunity = sum(s["score"] for s in items)          # volume x quality
        replic = median(s["signals"]["replicable"] for s in items)
        if replic < 0.34:                                       # the pipeline cannot make these at all
            continue
        exemplars, seen_channels = [], set()
        for s in items:                                         # distinct channels first
            ch = s.get("channel_id") or s.get("channel") or s["id"]
            if ch not in seen_channels or len(exemplars) < 1:
                seen_channels.add(ch)
                exemplars.append(s)
            if len(exemplars) == 3:
                break
        hooks = {h: v for h, v in hook_by_group[(fmt, topic)].items() if h not in ("no_hook", "visual_only")}
        best_hook = max(hooks, key=hooks.get) if hooks else "curiosity_gap"
        blueprints.append({
            "format": fmt,
            "topic": topic,
            "hook_style": best_hook,
            "stretch": replic < min_replic,   # trending, but needs on-camera or original visuals to work well
            "count": len(items),
            "channels": len(per_channel),
            "opportunity": round(opportunity, 3),
            "median_replicable": round(replic, 2),
            "median_hook": round(median(s["signals"]["hook"] for s in items), 2),
            "median_views_per_hour": round(median(s["views_per_hour"] for s in items), 1),
            "median_duration": median(s.get("duration") or 0 for s in items),
            "exemplars": [{"id": s["id"], "title": s["title"], "url": s["url"], "views": s["view_count"],
                           "views_per_hour": s["views_per_hour"], "channel": s.get("channel", ""),
                           "transcript": (s.get("transcript") or "")[:400]}
                          for s in exemplars],
        })
    # faceless-friendly formats first; stretch formats only fill in when there are too few of them
    blueprints.sort(key=lambda b: (b["stretch"], -b["opportunity"]))
    for b in blueprints:
        b["why_it_works"] = _why(b)
    return {
        "blueprints": blueprints[:top_n],
        "format_leaderboard": sorted(format_totals.items(), key=lambda kv: -kv[1]),
        "topic_leaderboard": sorted(topic_totals.items(), key=lambda kv: -kv[1]),
        "judged": sum(1 for s in shorts if s.get("judgment")),
        "usable": len(usable),
        "excluded": sum(1 for s in shorts if s.get("excluded")),
        "skipped": sum(1 for s in shorts if not s.get("judgment")),
    }


def _why(b: dict[str, Any]) -> str:
    if b["stretch"]:
        ease = "a stretch (the originals rely on a person on camera or original footage)"
    elif b["median_replicable"] >= 0.8:
        ease = "easy"
    else:
        ease = "feasible"
    return (f"{b['count']} trending Shorts from {b['channels']} channel(s) share the {b['format']} format on {b['topic']}; "
            f"they average {b['median_views_per_hour']:.0f} views/hour, remaking the format faceless is {ease}, "
            f"and the winning hook style is {b['hook_style']}.")


def render_report(analysis: dict[str, Any], shorts: list[dict[str, Any]], top_shorts: int = 15) -> str:
    lines = ["# Shorts trend analysis", ""]
    lines.append(f"{analysis['judged']} Shorts judged by TypeSafe, {analysis['usable']} usable after exclusions "
                 f"({analysis['excluded']} excluded as non-English or promotional, "
                 f"{analysis['skipped']} skipped: non-Latin script, no key, or fetch failed).")
    lines += ["", "## What to make next (blueprints)", ""]
    if not analysis["blueprints"]:
        lines.append("_No blueprints: run with TYPESAFE_API_KEY set so formats can be judged._")
    for i, b in enumerate(analysis["blueprints"], 1):
        tag = "  [stretch]" if b["stretch"] else ""
        lines.append(f"### {i}. {b['format']} x {b['topic']}  (opportunity {b['opportunity']}){tag}")
        lines.append(f"- Hook style: **{b['hook_style']}**, median hook strength {b['median_hook']}, "
                     f"replicability {b['median_replicable']}, median length {b['median_duration']:.0f}s")
        lines.append(f"- {b['why_it_works']}")
        for e in b["exemplars"]:
            lines.append(f"  - [{e['title']}]({e['url']}) - {e['views']:,} views, {e['views_per_hour']:.0f}/h")
        lines.append("")
    lines += ["## Format leaderboard", ""]
    for fmt, total in analysis["format_leaderboard"][:10]:
        lines.append(f"- {fmt}: {total:.2f}")
    lines += ["", "## Topic leaderboard", ""]
    for t, total in analysis["topic_leaderboard"][:10]:
        lines.append(f"- {t}: {total:.2f}")
    lines += ["", f"## Top {top_shorts} ranked Shorts", "",
              "| # | score | views/h | views | format | hook | title |", "|---|---|---|---|---|---|---|"]
    for i, s in enumerate([x for x in shorts if not x.get("excluded")][:top_shorts], 1):
        j = s.get("judgment") or {}
        fmt = j.get("format", {}).get("choice", "-")
        hook = j.get("hook_style", {}).get("choice", "-")
        title = s["title"].replace("|", "/")[:70]
        lines.append(f"| {i} | {s['score']:.3f} | {s['views_per_hour']:.0f} | {s['view_count']:,} | {fmt} | {hook} | [{title}]({s['url']}) |")
    return "\n".join(lines) + "\n"
