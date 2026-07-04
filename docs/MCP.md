# MCP server — filter.fyi inside your agent

The backend exposes an MCP (Model Context Protocol) server at `/mcp`
(Streamable HTTP, stateless). It makes filter.fyi a first-class tool wherever
the user's agent lives — Claude Code, Claude Desktop, Cursor, or any MCP
client — instead of only in a browser tab or Telegram.

## Auth

Same per-user Bearer token as the REST API: `users.api_token`, minted with the
Telegram `/token` command. Every request must send
`Authorization: Bearer <token>`; there is no anonymous MCP access. Analyses
run through the caller's lens and land in their library, exactly like the web
and Telegram surfaces.

## Client setup

Claude Code:

```bash
claude mcp add --transport http filter-fyi https://<backend-host>/mcp \
  --header "Authorization: Bearer <token>"
```

Any other Streamable-HTTP MCP client: point it at `https://<backend-host>/mcp`
with the same header.

## Tools

| Tool | What it does |
|---|---|
| `analyze(url, text, note)` | Full pipeline on a URL **or** pasted text (exactly one); saves to the library; returns verdict + analysis + agent-handoff actions. Long sources can take up to a minute. |
| `search_library(query, limit)` | Lean rows over everything the user has filtered; empty query = most recent. |
| `get_library_item(item_id)` | One item in full: stored summary, complete analysis, user note. |
| `get_lens()` | The user's perspective text. |
| `set_lens(lens)` | Replace the perspective (≤ 4000 chars). |

## Implementation notes

- `bot/mcp_server.py` — FastMCP instance + a pure-ASGI Bearer middleware that
  resolves the token to a canonical `users.id` and seeds a request-scoped
  ContextVar the tools read. Mounted in `main.py`; the transport's session
  manager runs inside the FastAPI lifespan.
- Tool responses reuse the REST layer's action builder (`bot.api._actions_for`),
  so MCP and web hand out identical briefs (profile + library-history aware).
- Errors surface as structured `{error, message}` payloads (pipeline error
  codes like `fetch-failed`, `no-transcript`, `busy`), not exceptions.
