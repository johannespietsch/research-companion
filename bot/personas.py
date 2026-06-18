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
ANON_PERSONAS: dict[str, str] = {
    "leader": (
        "The reader is a non-technical leader or decision-maker — a founder, "
        "executive, or manager weighing AI for their organisation. They do NOT "
        "write code and will not run hands-on engineering tasks. Tailor every "
        "suggestion to decisions and strategy: build-vs-buy calls, which "
        "workflows to pilot first, risks and governance, the questions to ask "
        "vendors and teams, and simple decision frameworks for AI adoption. "
        "Avoid code, libraries, and implementation detail. They value clear "
        "trade-offs, business impact, and an honest read on hype vs. substance."
    ),
    "explorer": (
        "The reader is curious but not technical and not a decision-maker. They "
        "feel some anxiety about keeping up with AI and want to build confidence "
        "gradually. Tailor every suggestion to small, low-pressure, concrete "
        "first steps that need no coding: a 10-minute experiment with a popular "
        "tool, one thing to try in their existing work, a gentle way to get "
        "hands-on. Keep it reassuring and jargon-light. They value quick wins, "
        "feeling less behind, and momentum without overwhelm."
    ),
    "builder": (
        "The reader is a hands-on technical practitioner — a developer, ML "
        "engineer, or builder who writes code and ships. Tailor every suggestion "
        "to concrete, code-level next steps: architectures to try, libraries and "
        "tools to wire in, benchmarks to run, experiments to prototype. Be "
        "specific and technical; assume they can read code and run commands. "
        "They're skeptical of hype and value practical patterns, real benchmarks, "
        "and implementation detail."
    ),
}

ANON_PERSONA_KEYS = frozenset(ANON_PERSONAS)


def resolve_anon_profile(key: str | None) -> str | None:
    """The profile text for a persona key, or None for unknown/empty keys
    (caller falls back to DEFAULT_PROFILE)."""
    if not key:
        return None
    return ANON_PERSONAS.get(key.strip().lower())
