"""Pre-defined anon personas (filter.fyi#72).

Anonymous visitors have no saved lens, so every analysis runs against the
generic DEFAULT_PROFILE and the first result reads as one-size-fits-all. These
three personas let an anon pick the lens that fits them before their first try
— sharpening the audition-moment result and showcasing, before any signup,
that filter.fyi adapts to *who you are*.

The frontend owns the display copy (punchy labels); the only contract across
surfaces is the persona KEY. Unknown/empty keys resolve to None so callers
fall back to DEFAULT_PROFILE — adding/renaming a persona never breaks the API.
"""
from __future__ import annotations

# key -> the profile text fed to the analyzer as "about the person".
#
# These describe the reader's *posture* (how they want next steps framed), not a
# topic — so a lens works whatever they filter (AI, markets, a field they're
# learning, a hobby). The frontend labels them "See the big picture" / "Keep up,
# calmly" / "Get hands-on"; keep the prompts topic-agnostic to match.
ANON_PERSONAS: dict[str, str] = {
    "leader": (
        "The reader wants the big picture, not the how-to — a decision-maker or "
        "strategic thinker who cares what something means and what to do about "
        "it. Tailor every suggestion to decisions and direction, whatever the "
        "topic: what this changes, the trade-offs and risks to weigh, which "
        "option to pick, the questions to ask, and simple frameworks for "
        "deciding. Skip step-by-step implementation and hands-on detail. They "
        "value clear trade-offs, an honest read on signal vs. hype, and a "
        "confident sense of what actually matters."
    ),
    "explorer": (
        "The reader is curious but not an expert here, and feels some pressure "
        "to keep up. They want to build confidence gradually, without overwhelm. "
        "Tailor every suggestion to small, low-pressure, concrete first steps: a "
        "short experiment, one thing to try in what they already do, a gentle "
        "way to get started — no technical background assumed. Keep it "
        "reassuring and jargon-light. They value quick wins, feeling less "
        "behind, and momentum without pressure."
    ),
    "builder": (
        "The reader wants to get hands-on and actually do something with what "
        "they read. Tailor every suggestion to concrete, practical next steps "
        "and experiments they can run: things to try, test, prototype, or put "
        "into practice, specific enough to start today. When the topic is "
        "technical, go code-level — libraries, commands, architectures, "
        "benchmarks; when it isn't, give the equivalent hands-on move: a "
        "backtest, a template, a drill, a small project. Be specific and "
        "concrete. They're skeptical of hype and value real results over theory."
    ),
}

ANON_PERSONA_KEYS = frozenset(ANON_PERSONAS)


def resolve_anon_profile(key: str | None) -> str | None:
    """The profile text for a persona key, or None for unknown/empty keys
    (caller falls back to DEFAULT_PROFILE)."""
    if not key:
        return None
    return ANON_PERSONAS.get(key.strip().lower())
