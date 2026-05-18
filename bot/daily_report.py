from bot.analyzer import parse_stored, to_plain_text
from bot.db import get_all_items


def generate_brief(user_id: int | None = None) -> str:
    items = get_all_items(user_id)
    report = "Daily Research Brief\n\n"
    for item in items[-5:]:
        source = item["source"] or "—"
        stored = item["analysis"] or ""
        parsed = parse_stored(stored)
        analysis = to_plain_text(parsed) if parsed else stored
        report += f"{source}\n{analysis}\n\n"
    return report
