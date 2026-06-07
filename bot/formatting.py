import html
import re

from bot.analyzer import ANALYSIS_FIELDS, parse_stored

_SECTION_EMOJIS = {
    "main_idea": "💡",
    "why_it_matters": "🎯",
    "grounded_in": "📎",
    "category": "🏷",
    "quick_win": "⚡",
    "first_step": "👣",
    "bigger_play": "🚀",
    "suggested_experiment": "🧪",  # legacy rows
    "time_required": "⏱",
    "verdict": "🧭",
}

_FIELD_LABELS = {
    "main_idea": "Main idea",
    "why_it_matters": "Why it matters",
    "grounded_in": "Based on",
    "category": "Category",
    "quick_win": "Quick win (30–90 min)",
    "first_step": "First step",
    "bigger_play": "Bigger play (when you're ready)",
    "suggested_experiment": "Suggested experiment",  # legacy rows
    "time_required": "Time required to explore",
    "verdict": "Verdict",
}


def format_agent_brief(action: dict) -> str:
    """Render one handoff action as a Telegram-HTML message with a copyable block.

    The brief sits in a <pre> block so Telegram offers tap-to-copy — the user
    pastes it straight into whatever AI assistant they use.
    """
    label = (action.get("label") or "Action").strip()
    brief = (action.get("brief") or "").strip()
    if not brief:
        return ""
    return (
        f"📋 <b>{html.escape(label)} — do this with your AI</b>\n"
        f"<i>tap to copy, paste into ChatGPT / Claude / Cursor / Codex…</i>\n"
        f"<pre>{html.escape(brief)}</pre>"
    )


def format_analysis(analysis) -> str:
    """Render an analysis as Telegram HTML.

    Accepts the new structured dict, our JSON-string storage format, or legacy
    free-form text from older rows.
    """
    if isinstance(analysis, dict):
        return _format_dict(analysis)
    if isinstance(analysis, str):
        parsed = parse_stored(analysis)
        if parsed is not None:
            return _format_dict(parsed)
        return _format_legacy_text(analysis)
    return ""


def _format_dict(analysis: dict) -> str:
    parts: list[str] = []
    for key in ANALYSIS_FIELDS:
        value = (analysis.get(key) or "").strip()
        if not value:
            continue
        emoji = _SECTION_EMOJIS.get(key, "")
        label = _FIELD_LABELS.get(key, key)
        prefix = f"{emoji} " if emoji else ""
        parts.append(f"{prefix}<b>{html.escape(label)}</b>\n{html.escape(value)}")
    return "\n\n".join(parts)


# Legacy renderer for rows written before the JSON migration ---------------------

_LEGACY_LABEL_LOOKUP = {
    "main idea": "main_idea",
    "why it matters": "why_it_matters",
    "category": "category",
    "quick win": "quick_win",
    "bigger play": "bigger_play",
    "suggested experiment": "suggested_experiment",
    "time required to explore": "time_required",
    "verdict": "verdict",
}


def _format_legacy_text(analysis: str) -> str:
    lines = analysis.split("\n")
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            out.append("")
            continue

        normalised = re.sub(r"^\*\*(.+?)\*\*$", r"\1", stripped)

        header_match = re.match(r"^#{1,3}\s+(.*)", normalised)
        if header_match:
            raw = header_match.group(1).strip()
            lookup = re.sub(r"\*\*", "", raw).rstrip(":").strip().lower()
            key = _LEGACY_LABEL_LOOKUP.get(lookup)
            emoji = _SECTION_EMOJIS.get(key, "") if key else ""
            clean = re.sub(r"\*\*(.+?)\*\*", r"\1", raw)
            content = html.escape(clean)
            prefix = f"{emoji} " if emoji else ""
            out.append(f"\n{prefix}<b>{content}</b>")
            continue

        if normalised.endswith(":") and len(normalised) < 60:
            lookup = normalised.rstrip(":").strip().lower()
            key = _LEGACY_LABEL_LOOKUP.get(lookup)
            emoji = _SECTION_EMOJIS.get(key, "") if key else ""
            label = html.escape(normalised)
            prefix = f"{emoji} " if emoji else ""
            out.append(f"\n{prefix}<b>{label}</b>")
            continue

        escaped = html.escape(stripped)
        escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)
        escaped = re.sub(r"^[\-\*]\s", "• ", escaped)
        out.append(escaped)

    return "\n".join(out).strip()
