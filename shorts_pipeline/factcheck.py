"""Fact-check pass: every claim in a script is checked against the web before it is voiced.

The writer works from memory. Here Claude (Claude Code with web search) lists the script's factual claims, searches
for each and quotes what it found; TypeSafe then judges, claim by claim, whether the quoted evidence supports or
contradicts the claim (a check on the checker). Code owns the policy: contradicted claims go back to the writer with
the corrections; a script with too many unverifiable core claims is held for the owner's review instead of being
uploaded automatically; the sources used go in the description.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from .config import has_typesafe, load_config

log = logging.getLogger(__name__)

DEFAULTS = {"enabled": True, "mode": "fix", "max_unverified": 2, "sources_in_description": True, "max_sources": 3,
            "timeout": 420}
SUPPORT_AT, CONTRADICT_AT = 0.6, 0.6

CHECK_SYSTEM = """You are a careful fact-checker for short educational videos. You have web search: use it. List every
checkable factual claim in the script (numbers, dates, names, places, events, scientific statements, quotes). For each
claim search the web, read a reliable source (encyclopedias, museums, universities, major news, official bodies, the
primary text itself for claims about a scripture or a book) and report:
- verdict: "supported" when a reliable source states it (minor rounding is fine); "contradicted" when a reliable source
  says otherwise; "unverifiable" when you could not find a source either way.
- evidence: the sentence or two from the source that decides it, quoted as closely as possible (max 300 characters).
- source_url: the page you used.
- correction: for a contradicted claim, the correct statement in one sentence; otherwise empty.
Claims the script already attributes to a text or tradition ("In Genesis...", "legend says...") are checked against
that text or tradition: supported when the text says so. Be strict about numbers and dates. Do not invent evidence:
if you did not find it, say unverifiable."""

CHECK_SCHEMA = {"type": "object", "properties": {"claims": {"type": "array", "items": {
    "type": "object", "properties": {
        "claim": {"type": "string"}, "verdict": {"type": "string", "enum": ["supported", "contradicted", "unverifiable"]},
        "evidence": {"type": "string"}, "source_url": {"type": "string"}, "correction": {"type": "string"},
        "core": {"type": "boolean", "description": "true when the video's point depends on this claim"}},
    "required": ["claim", "verdict", "evidence", "source_url", "correction", "core"]}}},
    "required": ["claims"]}


def config() -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    cfg.update(((load_config().get("production") or {}).get("fact_check")) or {})
    return cfg


def available() -> bool:
    from .claude_code_backend import find_claude_binary
    return bool(find_claude_binary())


def check(script: dict[str, Any]) -> dict[str, Any] | None:
    """{'claims': [...], 'supported': n, 'contradicted': [...], 'unverifiable': [...], 'sources': [...]} or None when the
    check could not run (no Claude Code, no web)."""
    from . import claude_code_backend
    cfg = config()
    prompt = ("Script of a short video. Check it.\n\nTitle: " + script.get("title", "") + "\n\n" + script.get("full_text", ""))
    try:
        data = claude_code_backend.generate_json(CHECK_SYSTEM, prompt, CHECK_SCHEMA, tools=["WebSearch", "WebFetch"],
                                                 max_turns=30, timeout=int(cfg["timeout"]))
    except Exception as exc:  # noqa: BLE001
        log.warning("fact-check could not run: %s", str(exc)[:200])
        return None
    claims = [c for c in (data.get("claims") or []) if c.get("claim")]
    if not claims:
        return {"claims": [], "supported": 0, "contradicted": [], "unverifiable": [], "sources": []}
    _verify_with_typesafe(claims)
    contradicted = [c for c in claims if c["verdict"] == "contradicted"]
    unverifiable = [c for c in claims if c["verdict"] == "unverifiable"]
    supported = [c for c in claims if c["verdict"] == "supported"]
    sources: list[str] = []
    for c in supported + contradicted:
        u = (c.get("source_url") or "").strip()
        if u.startswith("http") and u not in sources:
            sources.append(u)
    return {"claims": claims, "supported": len(supported), "contradicted": contradicted, "unverifiable": unverifiable,
            "sources": sources[: int(cfg["max_sources"])]}


def _verify_with_typesafe(claims: list[dict[str, Any]]) -> None:
    """Jev reads each quoted evidence against its claim. A 'supported' verdict without supporting evidence becomes
    unverifiable; evidence that contradicts the claim makes it contradicted whatever Claude said."""
    if not has_typesafe():
        return
    from typesafe_sdk import Noul, TypeSafeClient
    with_evidence = [c for c in claims if (c.get("evidence") or "").strip()]
    if not with_evidence:
        for c in claims:
            if c["verdict"] == "supported":
                c["verdict"] = "unverifiable"
        return
    state = {"claims": [{"id": i, "claim": c["claim"], "evidence": c["evidence"]} for i, c in enumerate(with_evidence)]}
    questions: dict[str, Any] = {}
    for i, _ in enumerate(with_evidence):
        questions[f"sup_{i}"] = Noul(instructions=f"Does the quoted `claims[{i}].evidence` state or clearly imply that `claims[{i}].claim` is true?")
        questions[f"con_{i}"] = Noul(instructions=f"Does the quoted `claims[{i}].evidence` state something that makes `claims[{i}].claim` false or inaccurate (a different number, date, name or outcome)?")
    try:
        with TypeSafeClient(timeout=90.0) as client:
            resp = client.system_one(state=state, questions=questions)
    except Exception as exc:  # noqa: BLE001
        log.warning("fact-check: TypeSafe verification skipped (%s)", str(exc)[:120])
        return
    for i, c in enumerate(with_evidence):
        sup, con = float(resp.answers[f"sup_{i}"].noul), float(resp.answers[f"con_{i}"].noul)
        c["evidence_supports"], c["evidence_contradicts"] = round(sup, 2), round(con, 2)
        if con >= CONTRADICT_AT:
            c["verdict"] = "contradicted"
        elif c["verdict"] == "supported" and sup < SUPPORT_AT:
            c["verdict"] = "unverifiable"
        elif c["verdict"] == "contradicted" and con < 0.3 and sup >= SUPPORT_AT:
            c["verdict"] = "supported"             # Claude misread its own evidence
    for c in claims:
        if not (c.get("evidence") or "").strip() and c["verdict"] == "supported":
            c["verdict"] = "unverifiable"


def summary(result: dict[str, Any] | None) -> str:
    if not result:
        return "not checked"
    n = len(result["claims"])
    return (f"{result['supported']}/{n} claims verified" + (f", {len(result['contradicted'])} corrected" if result["contradicted"] else "")
            + (f", {len(result['unverifiable'])} unverifiable" if result["unverifiable"] else ""))


def needs_review(result: dict[str, Any]) -> bool:
    core_unverified = [c for c in result["unverifiable"] if c.get("core")]
    return len(core_unverified) > int(config()["max_unverified"])


def sources_block(result: dict[str, Any]) -> str:
    urls = [u for u in result.get("sources") or [] if re.match(r"https?://", u)]
    return ("Sources:\n" + "\n".join(urls)) if urls else ""
