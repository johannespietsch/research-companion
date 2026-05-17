#!/usr/bin/env python3
"""
Knowledge base CLI (admin view — sees all users).

Usage:
    python kb.py                        # list all items
    python kb.py --user <id>            # list items for one user
    python kb.py <id>                   # show full item (analysis + original content)
    python kb.py search <q>             # search across source, content, analysis
    python kb.py search <q> --user <id> # search within a single user's KB
    python kb.py delete <id>            # delete an item (admin, ignores user scope)
    python kb.py adduser <email>        # create a web-only user and print their API token
"""

import sys

from bot.analyzer import parse_stored, to_plain_text
from bot.db import get_all_items, get_item, search_items, delete_item

WIDTH = 80

_TYPE_ICONS = {
    "url": "🔗", "note": "📝", "voice_memo": "🎙", "audio": "🎵",
    "video": "🎬", "photo": "📷", "document": "📄", "unknown": "❓",
}


def _parse_user_flag(args: list[str]) -> tuple[str | None, list[str]]:
    """Extract --user <id> from args, return (user_id, remaining_args)."""
    if "--user" in args:
        idx = args.index("--user")
        if idx + 1 < len(args):
            user_id = args[idx + 1]
            remaining = args[:idx] + args[idx + 2:]
            return user_id, remaining
    return None, args


def cmd_list(user_id: str | None = None):
    rows = get_all_items(user_id)
    if not rows:
        print("No items in knowledge base yet.")
        return
    label = f"(user {user_id})" if user_id else "(all users)"
    print(f"\n{'ID':>4}  {'USER':<12}  {'TYPE':<12}  {'DATE':<16}  {'NOTE':<20}  SOURCE")
    print("-" * WIDTH)
    for r in rows:
        icon = _TYPE_ICONS.get(r["source_type"], "")
        stype = f"{icon} {r['source_type']}"
        uid = (r["user_id"] or "—")[:12]
        date = (r["created_at"] or "")[:16]
        note = (r["user_note"] or " - NA - ")[:20]
        source = (r["source"] or "")[:WIDTH - 68] or "-"
        print(f"{r['id']:>4}  {uid:<12}  {stype:<12} {date:<16}  {note:<20}  {source}")
    print(f"\n{len(rows)} item(s) {label}. Run `python kb.py <id>` to read one.")


def cmd_show(item_id: int):
    row = get_item(item_id)  # admin: no user scope
    if not row:
        print(f"No item with id {item_id}.")
        return
    icon = _TYPE_ICONS.get(row["source_type"], "")
    print(f"\n{'─' * WIDTH}")
    print(f"  #{row['id']}  {icon} {row['source_type']}  {row['source'] or ''}  [user: {row['user_id'] or '—'}]")
    print(f"  {row['created_at']}")
    if row["user_note"]:
        print(f"  Context: {row['user_note']}")
    print(f"{'─' * WIDTH}")

    if row["content"]:
        print(f"\n--- Original Content ({len(row['content'])} chars) ---")
        preview = row["content"][:500]
        print(preview)
        if len(row["content"]) > 500:
            print(f"  ... ({len(row['content']) - 500} more chars)")

    print(f"\n--- Analysis ---")
    stored = row["analysis"] or ""
    parsed = parse_stored(stored)
    print(to_plain_text(parsed) if parsed else (stored or "(no analysis)"))
    print()


def cmd_search(query: str, user_id: str | None = None):
    rows = search_items(query, user_id)
    if not rows:
        print(f"No results for '{query}'.")
        return
    label = f" (user {user_id})" if user_id else ""
    print(f"\n{len(rows)} match(es) for '{query}'{label}:\n")
    for r in rows:
        icon = _TYPE_ICONS.get(r["source_type"], "")
        date = (r["created_at"] or "")[:16]
        uid = r["user_id"] or "—"
        print(f"  #{r['id']:>4}  {icon} {r['source_type']:<10}  {date}  user:{uid}  {(r['source'] or '')[:WIDTH - 50]}")
        for field in ("source", "content", "analysis", "user_note"):
            text = r[field] or ""
            idx = text.lower().find(query.lower())
            if idx >= 0:
                start = max(0, idx - 60)
                snippet = text[start: idx + 120].replace("\n", " ")
                print(f"          ...{snippet}...")
                break
        print()


def cmd_delete(item_id: int):
    delete_item(item_id)  # admin: no user scope
    print(f"Deleted item #{item_id}.")


def cmd_adduser(email: str):
    """Create a new web-only user and print their API token."""
    from bot.auth import generate_token
    from bot.db import create_web_user
    token = generate_token()
    try:
        user_id = create_web_user(email=email, api_token=token)
    except ValueError as e:
        print(f"Error: {e}")
        return
    print(f"Created user : {user_id}")
    print(f"Email        : {email}")
    print(f"API token    : {token}")
    print("\nShare the token with the user — they enter it in the web UI login screen.")


def main():
    args = sys.argv[1:]
    user_id, args = _parse_user_flag(args)

    if not args:
        cmd_list(user_id)
    elif len(args) == 1 and args[0].isdigit():
        cmd_show(int(args[0]))
    elif len(args) == 2 and args[0] == "search":
        cmd_search(args[1], user_id)
    elif len(args) == 2 and args[0] == "delete" and args[1].isdigit():
        cmd_delete(int(args[1]))
    elif len(args) == 2 and args[0] == "adduser":
        cmd_adduser(args[1])
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
