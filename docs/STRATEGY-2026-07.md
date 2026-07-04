# filter.fyi — GTM & product strategy, July 2026

Goal: a realistic path to **$5k MRR** — 250 subscribers at $20/mo, or in
practice a mix (e.g. 150 × $20 + 200 founding × $10). This doc states the
positioning, the competitive read, what shipped in the July batch, and a
90-day distribution plan with checkpoints.

## 1. Positioning

**The filter between your feeds and your AI.**

- For **AI builders and AI-curious professionals** drowning in newsletters,
  X threads, YouTube, and podcasts.
- Free answers the pull question: *"is this one link worth my time?"* —
  verdict + 0–5 next steps, each with a copy-paste brief for ChatGPT /
  Claude / Cursor.
- Pro flips the direction (the reason to pay): **we watch what you follow
  and only interrupt when something clears your bar** — monitoring, personal
  lens, weekly digest, library memory, MCP/API.

Two guardrails already decided elsewhere and still binding:
- **Anti-task-manager** (#47): we subtract and dispatch, never accumulate
  work. No user-entered tasks; the doing happens in *their* AI, not here.
- **Narrow the wedge** (Jackson lens): "everyone keeping up with AI" is a
  theme, not a market. The paying wedge is *people who build with AI and
  bill for their time* — they feel the hour saved and already pay for
  Cursor/Claude/Copilot, so a $20 tool subscription is a normal line item.

## 2. Competition and the "just use ChatGPT" problem

| Player | Price | What they are | What they don't do |
|---|---|---|---|
| Readwise Reader | $9.99–12.99/mo | best-in-class read-later + AI (Ghostreader) | no verdict, no action briefs, no "don't read this" — it *collects* |
| Recall | $7/mo (free: 10 summaries/mo) | summarize → knowledge graph | second-brain storage, not triage or action |
| Glasp | ~$12/mo | highlight + summarize socially | same: capture, not decide |
| Readless & digest apps | ~$5–10/mo | merge newsletters into one digest | compress volume; don't judge against *you*, no actions |
| "Just use ChatGPT" | $0–20 they already pay | paste link, get summary | neutral, stateless, pull-only; no monitoring, no memory of what you read, no bar |

**Differentiation to hammer in every surface:** everyone else summarizes and
stores; filter.fyi **decides and dispatches**. The three moats, in order:
1. **The lens** — verdicts against *your* role/stack/goals, not a generic reader.
2. **The loop** — monitoring + digest runs while you don't (subscription-shaped value).
3. **Agent-native** — briefs designed to be pasted into your AI, plus MCP so
   your agent can call filter.fyi itself. None of the above have this.

Pricing read: category anchors sit at $7–13/mo for *storage* tools. $20
holds only if we sell the loop + agent angle, not summaries. Founding at
$10 lands exactly on category price while preserving the $20 sticker.

## 3. What shipped in the July batch (review branches)

| Piece | Repo / PR | GTM job |
|---|---|---|
| Public share pages `/s/:slug` | frontend #86 | the viral artifact — every share is a mini landing page with the verdict, the AI-ready prompts, and a CTA; zero LLM cost per view |
| `/pricing` + founding capture | frontend #87 | price the promise before building billing; waitlist `source=pricing` = willingness-to-pay signal |
| MCP server `/mcp` | backend #96 | agent-first distribution: "add filter.fyi to Claude Code in one command"; concrete plank of the $20 story |

## 4. Integration roadmap (beyond web + Telegram)

Ordered by effort-to-distribution ratio:

1. **MCP server** — shipped (backend #96). List it in the MCP registries and
   directories (the modelcontextprotocol servers repo, Smithery, mcp.so,
   Cursor's directory); each listing is a durable acquisition channel the
   competitors aren't in.
2. **Browser extension (MV3, ~2 days)** — one button: "filter this page" →
   opens filter.fyi with the URL prefilled (no content script, no scraping;
   reuses the whole pipeline). Chrome Web Store listing = SEO + daily-use
   habit hook. Ship after share pages prove the loop.
3. **Mobile via PWA share-target (~1 day)** — manifest + share_target lets
   Android "share to filter.fyi" from any app; iOS gets a Shortcut recipe on
   a `/mobile` page. Covers 80% of "mobile app" value at 2% of the cost. A
   native app is *not* justified at this stage.
4. **Self-serve API keys (~2 days)** — the token exists (`/token` on
   Telegram); surface it in `/me`, document 3 endpoints, and "filter.fyi
   API" becomes a builder feature and a Pro line item.
5. **Email-in address (later)** — forward a newsletter to `you@in.filter.fyi`
   → filtered result back. Strong habit surface, needs inbound-mail infra.

## 5. Distribution playbook (90 days)

**Weeks 1–2 — close the loop.** Merge the batch, run the migrations, list
the MCP server in directories. Instrument share-page → landing conversion
(UTMs already in place; views column already counted).

**Weeks 3–4 — launch moments.** Product Hunt + Show HN, angle: *"I built the
filter between my feeds and my AI — it tells me what to skip, and hands
what's left to Claude/Cursor as runnable briefs."* The MCP integration is
the hook HN hasn't seen from summarizer tools. Telegram bot directories too.

**Weeks 5–12 — compounding channels.**
- **Share-first content:** every notable AI release, post *our* share page
  for it on X/LinkedIn/Reddit ("here's the verdict + what to actually do")
  — the product marketing itself doing its job. 3–5/week, founder-voiced.
- **Public weekly digest** ("what cleared the bar this week") as a
  newsletter/page — SEO + proof of the monitoring loop, and each item links
  to a share page.
- **Newsletter-author co-marketing:** offer authors a standing share page
  per issue ("filtered: 2 of 14 items cleared the bar for builders") — they
  get a shareable artifact, we get their audience.
- **Founder edge:** lean into markets/crypto-builder communities first
  (existing credibility) rather than generic "AI Twitter".

**Funnel math to $5k MRR** (checkpoints, not promises):
- Assume 2% visitor→signup and 5% signup→paid (typical prosumer SaaS).
  250 paid ⇒ ~5,000 signups ⇒ ~250k visitors — too high for 90 days, so
  paid conversion has to come disproportionately from the **founding list**
  (warm, priced signal; convert at 25–40% when batches open) and **agent
  channels** (MCP users are pre-qualified builders).
- Realistic 90-day bar: 1,500 signups, 300 founding claims, first batch of
  75–100 founding members activated ⇒ **$0.75–1k MRR**, with the $5k point
  12 months out on the same curves. If founding claims stall <50 by week 6,
  the price/promise is wrong — revisit packaging before spending on reach.

**Kill criteria / honest risks:** share pages that nobody shares (measure:
shares/100 results < 2 by week 6 → redesign the artifact or the moment);
monitoring costs per Pro user exceeding ~$4/mo LLM spend (llm_calls table
already tracks this); and the standing incumbent risk that Readwise ships
verdicts — speed on the agent-native angle is the defense.

## 6. Sequencing note

Nothing in this plan requires new infrastructure: shares ride D1, founding
rides the waitlist, MCP rides the existing backend and auth. The next
engineering investments should follow *demand evidence*: extension after
share loop works, API keys after MCP sees third-party tokens, billing after
founding batch #1 fills.
