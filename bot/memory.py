"""Memory-infused handoff briefs (#71): give the user's assistant the library
history it can't know.

A handoff brief on its own (bot/agent_brief.py) is recreatable with a single
custom instruction. What makes it irreplaceable is the user's own accumulated
context — what they've already completed, tried, or explicitly decided against
on *related* topics. That lives in their filter.fyi library and nowhere their
assistant can reach. Folding it into the brief means the user can't reproduce
the same prompt elsewhere without re-typing their history.

Relatedness is scoped per-suggestion (a databases brief shouldn't drag in
trading-bot history) using the cheap deterministic token overlap from
bot.consolidate — no embeddings, no LLM calls.

Signed-in only: anon users have no history and their briefs are byte-identical
to before. Distinct consumer from bot.signals — that digest feeds the
*analyzer* to shape which suggestions get generated (global, day-coarsened for
cache stability); this feeds the *brief* to inform the user's own agent
(per-suggestion, real-time). They draw on the same tables for different ends.
"""
from __future__ import annotations

from bot.consolidate import similarity, suggestion_text

# Looser than consolidation's merge threshold — we want "same topic area", not
# "the same step", so cross-references surface ("you're working on the RAG
# pipeline; here's what you've already done on it").
RELATED_THRESHOLD = 0.18
HISTORY_CHARS = 600
# Per-category caps so one long history doesn't swamp the brief.
MAX_PER_CATEGORY = 4
_DISMISS_LOOKBACK = 40


def _quote(text: str, limit: int = 80) -> str:
    text = " ".join((text or "").split())
    return text[:limit] + ("…" if len(text) > limit else "")


def _dedup(items: list[str]) -> list[str]:
    seen, out = set(), []
    for it in items:
        key = it.lower()
        if key not in seen:
            seen.add(key)
            out.append(it)
    return out


def build_history_block(user_id: int | None, *, title: str, detail: str) -> str:
    """Render the user's library history relevant to one suggestion as a
    compact, user-voice block. Returns "" for anon users or when nothing
    related is found — callers drop the block and the brief is unchanged."""
    if user_id is None:
        return ""
    from bot.db import get_saved_suggestions, get_suggestion_signals

    target = suggestion_text(title, detail)
    if not target:
        return ""

    done: list[str] = []
    tried: list[str] = []
    against: list[str] = []

    for s in get_saved_suggestions(user_id):
        cand = suggestion_text(s["title"], s["detail"])
        # Skip the suggestion being handed off itself (near-identical), so we
        # don't tell the agent "you already did this" about this very action.
        sim = similarity(target, cand)
        if sim < RELATED_THRESHOLD or sim >= 0.95:
            continue
        if s["status"] == "done" and len(done) < MAX_PER_CATEGORY:
            done.append(_quote(s["title"] or s["detail"]))
        elif s["status"] == "tried" and len(tried) < MAX_PER_CATEGORY:
            tried.append(_quote(s["title"] or s["detail"]))

    for d in get_suggestion_signals(user_id, events=("dismiss",), limit=_DISMISS_LOOKBACK):
        cand = d["suggestion_text"] or ""
        if similarity(target, cand) < RELATED_THRESHOLD:
            continue
        if len(against) >= MAX_PER_CATEGORY:
            break
        reason = (d["reason"] or "").strip()
        label = _quote(cand)
        against.append(label + (f" — {_quote(reason, 60)}" if reason else ""))

    done, tried, against = _dedup(done), _dedup(tried), _dedup(against)
    if not (done or tried or against):
        return ""

    lines = [
        "From my own library — relevant things I've already acted on (factor "
        "this in: don't repeat what I've finished or re-pitch what I dropped):"
    ]
    if done:
        lines.append("- Already done: " + ", ".join(f'"{x}"' for x in done))
    if tried:
        lines.append("- Tried: " + ", ".join(f'"{x}"' for x in tried))
    if against:
        lines.append("- Decided against: " + "; ".join(f'"{x}"' for x in against))

    block = "\n".join(lines)
    if len(block) <= HISTORY_CHARS:
        return block
    # Trim from the end on a line boundary so we never cut mid-item.
    while len(block) > HISTORY_CHARS and "\n" in block:
        block = block.rsplit("\n", 1)[0]
    return block
