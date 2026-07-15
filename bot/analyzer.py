import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


@dataclass
class UsageContext:
    """Caller-supplied context for attributing one LLM call in `llm_calls`.

    All fields are optional: legacy callers that don't pass a context still get
    a row written, just with NULL user/anon/job and an empty source_type. The
    analyzer functions accept this as a keyword-only `ctx` arg so adding new
    attribution dimensions later is a non-breaking change.
    """
    user_id: int | None = None
    anon_id: str | None = None
    job_id: str | None = None
    source_type: str = ""
    # Anon-only: a pre-defined persona key (bot.personas) the visitor picked, so
    # their first analysis uses that lens instead of DEFAULT_PROFILE. Ignored
    # for signed-in users — their saved lens always wins (#72).
    persona: str = ""


def _record_usage(
    *,
    purpose: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    started_at: float,
    ctx: UsageContext | None,
    status: str = "ok",
    error: str = "",
) -> None:
    """Best-effort write of one row to llm_calls. Never raises.

    Pricing is stamped at write time from `bot.pricing` so historical rows keep
    the cost they had at the time of the call even if we later bump prices.
    """
    from bot import pricing
    from bot.db import insert_llm_call

    latency_ms = int((time.monotonic() - started_at) * 1000)
    cost = pricing.cost_usd(model, input_tokens, output_tokens)
    c = ctx or UsageContext()
    insert_llm_call(
        provider=_PROVIDER,
        model=model,
        purpose=purpose,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost,
        latency_ms=latency_ms,
        status=status,
        error=error,
        user_id=c.user_id,
        anon_id=c.anon_id,
        job_id=c.job_id,
        source_type=c.source_type,
    )


def _capture_trace(*, model: str, text: str, profile: str, output: dict, ctx: UsageContext | None) -> None:
    """Best-effort write of one row to analyze_traces. Gated by env flag inside
    `record_analyze_trace`; never raises into the caller."""
    from bot.db import record_analyze_trace

    c = ctx or UsageContext()
    record_analyze_trace(
        provider=_PROVIDER,
        model=model,
        source_type=c.source_type,
        input_text=text,
        profile_text=profile,
        output=output,
        user_id=c.user_id,
        anon_id=c.anon_id,
        job_id=c.job_id,
    )


def _anthropic_tokens(resp) -> tuple[int, int]:
    """Pull (input_tokens, output_tokens) off an Anthropic Messages response.
    Missing usage is treated as (0, 0) rather than raising — we'd rather log a
    zero-token row than drop the call entirely."""
    usage = getattr(resp, "usage", None)
    if usage is None:
        return 0, 0
    return int(getattr(usage, "input_tokens", 0) or 0), int(getattr(usage, "output_tokens", 0) or 0)


def _openai_tokens(resp) -> tuple[int, int]:
    """Pull (prompt_tokens, completion_tokens) off an OpenAI Chat response."""
    usage = getattr(resp, "usage", None)
    if usage is None:
        return 0, 0
    return int(getattr(usage, "prompt_tokens", 0) or 0), int(getattr(usage, "completion_tokens", 0) or 0)

_ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY")
_OPENAI_KEY = os.getenv("OPENAI_API_KEY")

if not _ANTHROPIC_KEY and not _OPENAI_KEY:
    raise EnvironmentError("Set ANTHROPIC_API_KEY or OPENAI_API_KEY in .env")

_PROVIDER = "anthropic" if _ANTHROPIC_KEY else "openai"
_MODEL = "claude-haiku-4-5-20251001" if _PROVIDER == "anthropic" else "gpt-4o-mini"

# Premium model used for signed-in users on the text-heavy steps where
# long-context fidelity meaningfully changes output quality. Anon users (and
# the image step for everyone) stay on `_MODEL` to keep landing-page tries
# cheap. Per-tier routing is Anthropic-only — the OpenAI fallback path is a
# single model.
_PREMIUM_MODEL = "claude-sonnet-4-6"
_PREMIUM_PURPOSES: frozenset[str] = frozenset({"summary", "analyze"})


def _resolve_model(purpose: str, ctx: UsageContext | None) -> str:
    """Pick the model for one LLM call based on (purpose, signed-in?).

    Signed-in callers are detected by `ctx.user_id is not None`. The
    cache key hashes the resolved model, so anon and signed-in results
    are partitioned naturally — exactly what we want, since different
    models produce different outputs."""
    if _PROVIDER != "anthropic":
        return _MODEL
    signed_in = ctx is not None and ctx.user_id is not None
    if signed_in and purpose in _PREMIUM_PURPOSES:
        return _PREMIUM_MODEL
    return _MODEL

_client = None


def _get_client():
    global _client
    if _client is not None:
        return _client
    if _PROVIDER == "anthropic":
        import anthropic
        _client = anthropic.Anthropic(api_key=_ANTHROPIC_KEY)
    else:
        from openai import OpenAI
        _client = OpenAI(api_key=_OPENAI_KEY)
    return _client


# Scalar (string) analysis fields. The actionable follow-ups live in a separate
# `suggestions` array (0–5 items) handled alongside these — see SUGGESTION_FIELDS.
ANALYSIS_FIELDS = (
    "main_idea",
    "why_it_matters",
    "grounded_in",
    "category",
    "time_required",
    "verdict",
)

# Each suggestion is a self-contained next step. 0–5 per analysis; an empty list
# is valid and expected when the content has no follow-up worth the reader's time.
SUGGESTION_FIELDS = ("title", "detail", "first_step", "effort")
MAX_SUGGESTIONS = 5

# Legacy fields from before the suggestions[] migration. Kept so _normalize can
# synthesize suggestions from older cached/stored analyses for uniform rendering.
LEGACY_EXPERIMENT_FIELD = "suggested_experiment"
_LEGACY_ACTION_FIELDS = ("quick_win", "bigger_play", "first_step", "suggested_experiment")

VERDICTS = ("watch", "skim", "skip")

_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "main_idea": {
            "type": "string",
            "description": "The single most important idea, in 1–2 sentences.",
        },
        "why_it_matters": {
            "type": "string",
            "description": "Why this is relevant to this person specifically.",
        },
        "grounded_in": {
            "type": "string",
            "description": (
                "The single most concrete thing IN THIS CONTENT that the actions "
                "rest on — a specific claim, result, quote, or timestamp/section. "
                "One sentence, quoted or closely paraphrased, so the action is "
                "traceable back to the source. Not a restatement of main_idea."
            ),
        },
        "category": {
            "type": "string",
            "description": "Short topic tag (kebab-case), e.g. 'ai-engineering', 'productivity', 'crypto-trading'.",
        },
        "suggestions": {
            "type": "array",
            "maxItems": MAX_SUGGESTIONS,
            "description": (
                "0 to 5 concrete next steps the reader could take, ordered best-first "
                "and varied in ambition (a 30-minute quick win through a multi-week "
                "project). Each must be specific to this person and genuinely worth "
                "their time. QUALITY OVER QUANTITY: return an EMPTY array if the "
                "content is purely informational/news with no follow-up worth doing — "
                "never invent busywork. We always respect the reader's time."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "A 3–6 word imperative label, e.g. 'Add a reranker'.",
                    },
                    "detail": {
                        "type": "string",
                        "description": "One sentence: what to do and why it pays off, specific to this person.",
                    },
                    "first_step": {
                        "type": "string",
                        "description": (
                            "The single most concrete first move — the exact command to run, "
                            "file/page to open, or first thing to write. Imperative, no preamble, "
                            "no meta-advice about using AI/assistants."
                        ),
                    },
                    "effort": {
                        "type": "string",
                        "description": "Rough commitment, e.g. '~30 min', '~2 hrs', 'a weekend', 'multi-week'.",
                    },
                },
                "required": list(SUGGESTION_FIELDS),
            },
        },
        "time_required": {
            "type": "string",
            "description": "Estimated time to engage, e.g. '12 min read', '8 min watch'.",
        },
        "verdict": {
            "type": "string",
            "enum": list(VERDICTS),
            "description": "How worth the user's time: 'watch' (engage fully), 'skim' (worth a quick look), 'skip' (not for them).",
        },
    },
    "required": list(ANALYSIS_FIELDS) + ["suggestions"],
}

_ANTHROPIC_TOOL = {
    "name": "record_analysis",
    "description": "Record the structured analysis of the content.",
    "input_schema": _TOOL_SCHEMA,
}

# Fallback profile used when no real one is available: the anonymous /api/try
# path, or a signed-in user who hasn't set their own profile yet. Mirrors the
# landing-page positioning ("For everyone trying to keep up with AI") so the
# LLM always has a real audience to be specific about.
DEFAULT_PROFILE = (
    "The reader is someone trying to keep up with AI — a developer, researcher, "
    "founder, or technical practitioner who wants signal, not noise. They value: "
    "practical relevance to AI/tech work, an honest time-to-value assessment, and "
    "clear \"watch / skim / skip\" verdicts. They're skeptical of hype, allergic "
    "to generic news takes, and engage best with concrete patterns, benchmarks, "
    "and architecture."
)


_PROMPT = """You are my personal AI research analyst.
{profile_block}{signals_block}
Analyze the following content and produce a structured analysis covering the required fields. Be concrete and specific to this person — `why_it_matters` should speak to their situation, not give generic advice.

The suggestions are the point of this analysis — make them the strongest part:
- `grounded_in`: name the one specific claim/result/quote/section in this content the suggestions build on, so they're traceable to the source.
- `suggestions`: 0 to 5 concrete next steps, ordered best-first, varied in ambition (a 30-minute quick win through a multi-week project). Each has a short `title`, a one-sentence `detail`, a concrete `first_step`, and an `effort` estimate. Every one must be specific to this person and genuinely worth their time — never generic advice.
  QUALITY OVER QUANTITY. Do not pad to hit a number. If the content is purely informational or news with no follow-up genuinely worth the reader's time, return an EMPTY `suggestions` array — we always respect the reader's time.

CONTENT:
{text}"""

_OPENAI_JSON_SUFFIX = (
    "\n\nRespond with a single JSON object containing these keys: "
    + ", ".join(ANALYSIS_FIELDS)
    + ", suggestions. 'verdict' must be one of: "
    + ", ".join(VERDICTS)
    + ". 'suggestions' is an array of 0 to 5 objects, each with: "
    + ", ".join(SUGGESTION_FIELDS)
    + " (return [] if there's no follow-up worth the reader's time)."
)


def _load_profile(user_id: int | None, persona: str = "") -> str:
    """Resolve the profile fed to the analyzer.

    Signed-in users always get their own saved lens (persona ignored). Anon
    users get the persona they picked, if any (#72); otherwise everyone falls
    back to DEFAULT_PROFILE.
    """
    if user_id is not None:
        from bot.db import get_user_profile
        return get_user_profile(user_id) or DEFAULT_PROFILE
    from bot.personas import resolve_anon_profile
    return resolve_anon_profile(persona) or DEFAULT_PROFILE


def _load_signals(user_id: int | None) -> str:
    """Behaviour-signal digest for signed-in users (see bot/signals.py).
    Empty for anon users and on any failure — signals are an enhancement and
    must never break analysis."""
    if user_id is None:
        return ""
    try:
        from bot.signals import build_signal_digest
        return build_signal_digest(user_id)
    except Exception:
        logger.exception("signal digest failed; analysing without signals")
        return ""


def _normalize(raw: dict) -> dict:
    """Coerce LLM output into our schema: scalar string fields + a suggestions list.

    Also upgrades legacy analyses (cached or stored before the suggestions[]
    migration) by synthesizing suggestions from their quick_win/bigger_play
    fields, so every downstream renderer can rely on `suggestions`.
    """
    out = {k: str(raw.get(k, "")).strip() for k in ANALYSIS_FIELDS}
    if out["verdict"].lower() not in VERDICTS:
        out["verdict"] = "skim"
    else:
        out["verdict"] = out["verdict"].lower()
    out["suggestions"] = _normalize_suggestions(raw)
    return out


def _normalize_suggestions(raw: dict) -> list[dict]:
    raw_list = raw.get("suggestions")
    if isinstance(raw_list, list):
        items: list[dict] = []
        for s in raw_list[:MAX_SUGGESTIONS]:
            if not isinstance(s, dict):
                continue
            item = {k: str(s.get(k, "")).strip() for k in SUGGESTION_FIELDS}
            if item["title"] or item["detail"]:  # drop empty rows
                items.append(item)
        return items
    return _legacy_suggestions(raw)


def _legacy_suggestions(raw: dict) -> list[dict]:
    """Build suggestions[] from the old quick_win/bigger_play/suggested_experiment
    fields, so analyses produced before the migration still render as boxes."""
    out: list[dict] = []
    quick_win = str(raw.get("quick_win", "")).strip()
    if quick_win:
        out.append({
            "title": "Quick win",
            "detail": quick_win,
            "first_step": str(raw.get("first_step", "")).strip(),
            "effort": "a weekend",
        })
    bigger_play = str(raw.get("bigger_play", "")).strip()
    if bigger_play:
        out.append({"title": "Bigger play", "detail": bigger_play, "first_step": "", "effort": "multi-week"})
    if not out:
        legacy = str(raw.get(LEGACY_EXPERIMENT_FIELD, "")).strip()
        if legacy:
            out.append({"title": "Try this", "detail": legacy, "first_step": "", "effort": ""})
    return out


# ---------------------------------------------------------------------------
# Result caching
# ---------------------------------------------------------------------------
#
# Both analyze() and summarize_content() check a content-addressed cache
# (bot.db.llm_cache) before calling the LLM. Cache keys hash every input that
# affects the output: provider, model, prompt template, response schema where
# applicable, profile text, and the input content. Any change to those inputs
# changes the key, so a prompt or model bump auto-invalidates without any
# explicit version flag. Cache misses pay the LLM cost as before; hits skip
# the upstream call entirely.
#
# Cache hits are not written to `llm_calls` — that table is "upstream API
# calls made". Hits are visible via Fly logs (logger.info) and we'll add a
# hit-rate tile to the admin dashboard separately once we have a feel for
# how often this fires.

def _cache_key_analyze(text: str, profile: str, model: str, signals: str = "") -> str:
    h = hashlib.sha256()
    h.update(b"analyze\x00")
    h.update(_PROVIDER.encode()); h.update(b"\x00")
    h.update(model.encode()); h.update(b"\x00")
    h.update(_PROMPT.encode()); h.update(b"\x00")
    h.update(json.dumps(_TOOL_SCHEMA, sort_keys=True).encode()); h.update(b"\x00")
    h.update((profile or "").encode()); h.update(b"\x00")
    # Behaviour-signal digest (#69). Day-coarsened upstream (bot/signals.py),
    # so the key is stable within a UTC day rather than churning per click.
    h.update((signals or "").encode()); h.update(b"\x00")
    h.update(text.encode())
    return h.hexdigest()


def _cache_key_summary(text: str, model: str) -> str:
    h = hashlib.sha256()
    h.update(b"summary\x00")
    h.update(_PROVIDER.encode()); h.update(b"\x00")
    h.update(model.encode()); h.update(b"\x00")
    h.update(_SUMMARY_PROMPT.encode()); h.update(b"\x00")
    h.update(text.encode())
    return h.hexdigest()


def _cache_key_image(b64: str, caption: str, model: str) -> str:
    """Cache key for `analyze_image()` calls.

    Keyed on the exact base64 image bytes + caption + model/provider.
    Same image + same caption → same description, deterministically.

    Why this matters even though analyze_image is rare: the Telegram URL
    handler appends image descriptions to the article text before calling
    `analyze()`. Without caching the image step, those descriptions vary
    between calls (LLM non-determinism), the combined analyze input differs,
    and the *analyze* cache misses too. So caching here is what makes the
    end-to-end Telegram path actually hit `llm_cache`.
    """
    h = hashlib.sha256()
    h.update(b"image\x00")
    h.update(_PROVIDER.encode()); h.update(b"\x00")
    h.update(model.encode()); h.update(b"\x00")
    h.update(caption.encode()); h.update(b"\x00")
    h.update(b64.encode())
    return h.hexdigest()


def _try_cache_get(cache_key: str):
    """Best-effort cache read. A DB blip should never break analysis — fall
    through to the LLM if the lookup fails for any reason."""
    try:
        from bot.db import get_cached_llm_result
        return get_cached_llm_result(cache_key)
    except Exception:
        logger.exception("llm_cache read failed; treating as miss")
        return None


def _try_cache_set(cache_key: str, purpose: str, payload: str) -> None:
    try:
        from bot.db import set_cached_llm_result
        set_cached_llm_result(cache_key, purpose, payload)
    except Exception:
        logger.exception("llm_cache write failed; result not cached")


def _record_cache_hit(*, purpose: str, ctx: UsageContext | None) -> None:
    """Log one cache hit to `llm_cache_hits` with an estimated cost-saved
    figure pulled from recent successful upstream calls of the same purpose.
    Best-effort: a failure here must never break the analyse path."""
    try:
        from bot.db import estimate_avg_cost_per_call, record_cache_hit
        c = ctx or UsageContext()
        avg = estimate_avg_cost_per_call(purpose)
        record_cache_hit(
            purpose=purpose,
            user_id=c.user_id,
            anon_id=c.anon_id,
            source_type=c.source_type,
            cost_saved_usd=avg,
        )
    except Exception:
        logger.exception("record_cache_hit failed; hit not logged")


def analyze(
    text: str,
    user_id: int | None = None,
    *,
    ctx: UsageContext | None = None,
    skip_cache: bool = False,
) -> dict:
    """Returns a dict with keys: main_idea, why_it_matters, category, quick_win, bigger_play, time_required, verdict."""
    # Back-compat: callers that still pass `user_id` positionally get it merged
    # into the context so usage rows still attribute to a user.
    if ctx is None:
        ctx = UsageContext(user_id=user_id)
    elif ctx.user_id is None and user_id is not None:
        ctx = UsageContext(user_id=user_id, anon_id=ctx.anon_id, job_id=ctx.job_id, source_type=ctx.source_type)

    profile = _load_profile(ctx.user_id, ctx.persona)
    signals = _load_signals(ctx.user_id)
    model = _resolve_model("analyze", ctx)

    # Content-addressed cache lookup. Key spans every input that affects the
    # output, so a prompt/model/schema change auto-invalidates. Skips the
    # upstream LLM call entirely on hit — no llm_calls row written because
    # nothing was actually called. `skip_cache` bypasses the read (but the
    # result below still overwrites the entry, refreshing it) — used by the
    # admin retrigger endpoint to force a fresh read past a stale cache key.
    cache_key = _cache_key_analyze(text, profile, model, signals)
    cached = None if skip_cache else _try_cache_get(cache_key)
    if cached is not None:
        try:
            result = json.loads(cached)
            if isinstance(result, dict) and all(k in result for k in ANALYSIS_FIELDS):
                logger.info("analyze cache hit (key=%s...)", cache_key[:12])
                _record_cache_hit(purpose="analyze", ctx=ctx)
                return result
        except json.JSONDecodeError:
            # Cache row was corrupt — fall through and overwrite below.
            logger.warning("analyze cache had non-JSON payload; refreshing")

    profile_block = f"\nAbout the person you are analysing for:\n{profile}\n" if profile else ""
    signals_block = f"\n{signals}\n" if signals else ""
    prompt = _PROMPT.format(profile_block=profile_block, signals_block=signals_block, text=text)

    # Traces feed the eval pipeline — they must capture the full
    # personalization context the model saw, signals included.
    trace_profile = profile + (f"\n\n[signals]\n{signals}" if signals else "")

    client = _get_client()
    started = time.monotonic()
    try:
        if _PROVIDER == "anthropic":
            resp = client.messages.create(
                model=model,
                max_tokens=2048,  # room for up to 5 suggestions + scalar fields
                tools=[_ANTHROPIC_TOOL],
                tool_choice={"type": "tool", "name": "record_analysis"},
                messages=[{"role": "user", "content": prompt}],
            )
            in_tok, out_tok = _anthropic_tokens(resp)
            _record_usage(purpose="analyze", model=model, input_tokens=in_tok, output_tokens=out_tok,
                          started_at=started, ctx=ctx)
            for block in resp.content:
                if getattr(block, "type", None) == "tool_use" and block.name == "record_analysis":
                    result = _normalize(block.input)
                    _capture_trace(model=model, text=text, profile=trace_profile, output=result, ctx=ctx)
                    _try_cache_set(cache_key, "analyze", json.dumps(result, ensure_ascii=False))
                    return result
            raise RuntimeError("Anthropic did not return a record_analysis tool_use block")

        resp = client.chat.completions.create(
            model=model,
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt + _OPENAI_JSON_SUFFIX}],
        )
        in_tok, out_tok = _openai_tokens(resp)
        _record_usage(purpose="analyze", model=model, input_tokens=in_tok, output_tokens=out_tok,
                      started_at=started, ctx=ctx)
        content = resp.choices[0].message.content or "{}"
        try:
            raw = json.loads(content)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"OpenAI returned non-JSON content: {e}: {content[:200]}")
        result = _normalize(raw)
        _capture_trace(model=model, text=text, profile=trace_profile, output=result, ctx=ctx)
        _try_cache_set(cache_key, "analyze", json.dumps(result, ensure_ascii=False))
        return result
    except Exception as e:
        # Log the failed attempt so failure rate shows up next to spend.
        _record_usage(purpose="analyze", model=model, input_tokens=0, output_tokens=0,
                      started_at=started, ctx=ctx, status="error", error=str(e)[:200])
        raise


_SUMMARY_PROMPT = """You are distilling source content into a faithful, reusable \
knowledge brief that will be STORED IN PLACE OF the original and later used both to \
generate personalized "watch / skim / skip" verdicts AND to power retrieval / \
"chat with your content". Fidelity matters more than brevity.

Rules:
- Preserve EVERY distinct topic, argument, claim, fact, figure, named entity, and \
conclusion. For long, multi-topic sources (e.g. a 2-hour podcast), cover every \
segment — do NOT collapse it to a single theme or a few bullets.
- Remove only genuine filler: greetings, chit-chat, repetition, ad reads / \
sponsor segments, and verbatim padding.
- Stay neutral and faithful — no opinions, no recommendations, no added framing.
- Organize by topic/section with clear headings and tight bullets. Quote sparingly \
and only short phrases (under ~25 words) where exact wording matters.
- Scale length to the source: a short article stays short; a long, dense, \
multi-topic source should yield a long, thoroughly sectioned brief.
- Write the brief in the SAME language as the source content. If the source mixes \
languages, use the dominant one. Do not translate.
- Anchor every relative time expression ("this year", "last week", "a few months \
ago", "recently") to the source date shown below — NOT to your own training \
cutoff. If the source itself states an absolute date, preserve that exact date. \
Do not invent specific years that the source left vague.

SOURCE METADATA:
- Published: {published_at}

CONTENT:
{text}"""

# Upper bound on a stored brief. Matches MAX_CONTENT_CHARS so a faithful brief
# can be as long as the source (the summary is still a derived brief — the
# prompt strips filler and reorganises — it's just not artificially capped).
SUMMARY_MAX_CHARS = 100_000
# Anthropic output-token ceiling we'll request. Sized to comfortably reach
# SUMMARY_MAX_CHARS at the model's average chars/token, and well under the
# Haiku 4.5 64k hard limit. Scaled per source below.
_SUMMARY_MAX_OUTPUT_TOKENS = 32_000


def _published_at_for_prompt(published_at: str | None) -> str:
    """Render the SOURCE METADATA Published line. Falls back to today tagged
    as a best estimate when the caller doesn't have a real date — this still
    anchors the model to the right year, which is what fixed the Haiku date
    hallucinations (e.g. Haiku writing "October 2023" for a transcript that
    said "Oktober 2025")."""
    if published_at and published_at.strip():
        return published_at.strip()
    from datetime import date
    return f"{date.today().isoformat()} (today; actual publication date unknown)"


def _summary_output_tokens(text: str) -> int:
    """Scale requested output length 1:1 to the source (~4 chars/token).

    The prompt asks the model to preserve every distinct claim and scale
    length to the source. A faithful brief is almost always shorter than
    verbatim input, so the model self-regulates and stops at end_turn well
    before the cap. Earlier ratios (//4, //2) were binding before the model
    finished and showed up as summaries ending mid-section. 1:1 makes
    truncation a rare edge case."""
    approx_input_tokens = len(text) // 4
    return max(512, min(_SUMMARY_MAX_OUTPUT_TOKENS, approx_input_tokens))


def summarize_content(
    text: str,
    *,
    ctx: UsageContext | None = None,
    published_at: str | None = None,
    skip_cache: bool = False,
) -> str:
    """Distil source content into a faithful, length-scaled structured brief.

    Stored instead of the full fetched text: rich enough to re-derive verdicts
    under a different profile and to power retrieval/chat, but a derived brief
    rather than a verbatim copy. Falls back to a truncated slice if the model
    call fails, so persistence never breaks on an LLM hiccup.

    `published_at` is the source's publication date (ISO YYYY-MM-DD), used to
    anchor relative time expressions ("this year") so the model doesn't fill
    them in from its own training cutoff. If unknown, today's date is passed
    in tagged as a best-estimate fallback — still better than letting the
    model default to its training year, which is what produced the date
    hallucinations seen on non-English transcripts.

    `skip_cache=True` bypasses the cache read and forces a fresh LLM call,
    overwriting the entry — see `analyze()`'s `skip_cache` for why.
    """
    text = (text or "").strip()
    if not text:
        return ""

    model = _resolve_model("summary", ctx)
    published_at_str = _published_at_for_prompt(published_at)

    # Cache: keyed by (provider, model, prompt template, content). Anon and
    # signed-in callers land on different models so their results are
    # partitioned naturally by the cache key. published_at is deliberately
    # NOT part of the key — a per-day fallback would otherwise blow the cache
    # daily for any source without a real publication date.
    cache_key = _cache_key_summary(text, model)
    cached = None if skip_cache else _try_cache_get(cache_key)
    if cached is not None:
        logger.info("summary cache hit (key=%s...)", cache_key[:12])
        _record_cache_hit(purpose="summary", ctx=ctx)
        return cached[:SUMMARY_MAX_CHARS]

    prompt = _SUMMARY_PROMPT.format(text=text, published_at=published_at_str)
    max_tokens = _summary_output_tokens(text)
    started = time.monotonic()
    try:
        client = _get_client()
        if _PROVIDER == "anthropic":
            # Stream the summary rather than blocking on one response. The SDK
            # rejects a *non-streaming* request whose `max_tokens` implies a
            # >10-minute worst case — which long-transcript briefs routinely
            # trip, since `_summary_output_tokens` scales the budget up to 32k
            # (fails instantly, latency 0, before any network call → the brief
            # silently degraded to the truncated-transcript fallback). Streaming
            # keeps the socket open incrementally; the model still stops at
            # end_turn when the brief is done, so real latency is unchanged.
            with client.messages.stream(
                model=model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                resp = stream.get_final_message()
            in_tok, out_tok = _anthropic_tokens(resp)
            stop_reason = getattr(resp, "stop_reason", None)
            out = "".join(
                b.text for b in resp.content if getattr(b, "type", None) == "text"
            ).strip()
        else:
            resp = client.chat.completions.create(
                model=model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            in_tok, out_tok = _openai_tokens(resp)
            stop_reason = getattr(resp.choices[0], "finish_reason", None)
            out = (resp.choices[0].message.content or "").strip()
        # Surface max-tokens truncation distinctly from the SUMMARY_MAX_CHARS
        # cap — they bound the output independently. The token cap is the
        # binding constraint for long inputs and shows up as a summary that
        # ends mid-word / mid-section even though `len(summary)` is well
        # under SUMMARY_MAX_CHARS.
        if stop_reason in ("max_tokens", "length"):
            logger.warning(
                "summary truncated by max_tokens (requested=%d, output_tokens=%d, "
                "output_chars=%d, input_chars=%d, source_type=%s) — bump "
                "_summary_output_tokens or raise _SUMMARY_MAX_OUTPUT_TOKENS",
                max_tokens, out_tok, len(out), len(text),
                (ctx.source_type if ctx else "") or "",
            )
        _record_usage(purpose="summary", model=model, input_tokens=in_tok, output_tokens=out_tok,
                      started_at=started, ctx=ctx)
        if out:
            _try_cache_set(cache_key, "summary", out[:SUMMARY_MAX_CHARS])
            return out[:SUMMARY_MAX_CHARS]
        # Empty model output — return the truncated source but don't cache.
        return text[:SUMMARY_MAX_CHARS]
    except Exception as e:
        _record_usage(purpose="summary", model=model, input_tokens=0, output_tokens=0,
                      started_at=started, ctx=ctx, status="error", error=str(e)[:200])
        logger.warning("summarize_content failed; storing truncated slice: %s", e)
        return text[:SUMMARY_MAX_CHARS]


def analyze_image(b64: str, caption: str = "", *, ctx: UsageContext | None = None) -> str:
    """Describe and extract key info from a base64-encoded JPEG image.

    Cached (since fix/cache-analyze-image) because the Telegram URL handler
    appends image descriptions to the article text before calling `analyze()`.
    Without this cache the inner LLM non-determinism propagates outward and
    `analyze()`'s cache misses every time, even on identical articles.
    """
    model = _resolve_model("image", ctx)
    cache_key = _cache_key_image(b64, caption, model)
    cached = _try_cache_get(cache_key)
    if cached is not None:
        logger.info("image cache hit (key=%s...)", cache_key[:12])
        _record_cache_hit(purpose="image", ctx=ctx)
        return cached

    prompt = "Extract and describe all text and key information visible in this image."
    if caption:
        prompt += f" Context provided: {caption}"

    client = _get_client()
    started = time.monotonic()
    try:
        if _PROVIDER == "anthropic":
            resp = client.messages.create(
                model=model,
                max_tokens=1024,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
                        {"type": "text", "text": prompt},
                    ],
                }],
            )
            in_tok, out_tok = _anthropic_tokens(resp)
            _record_usage(purpose="image", model=model, input_tokens=in_tok, output_tokens=out_tok,
                          started_at=started, ctx=ctx)
            out = resp.content[0].text
            _try_cache_set(cache_key, "image", out)
            return out

        resp = client.chat.completions.create(
            model=model,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ],
            }],
        )
        in_tok, out_tok = _openai_tokens(resp)
        _record_usage(purpose="image", model=model, input_tokens=in_tok, output_tokens=out_tok,
                      started_at=started, ctx=ctx)
        out = resp.choices[0].message.content
        _try_cache_set(cache_key, "image", out)
        return out
    except Exception as e:
        _record_usage(purpose="image", model=model, input_tokens=0, output_tokens=0,
                      started_at=started, ctx=ctx, status="error", error=str(e)[:200])
        raise


# ---------------------------------------------------------------------------
# Storage / rendering helpers
# ---------------------------------------------------------------------------

_LEGACY_FIELD_LABELS = {
    "main_idea": "Main idea",
    "why_it_matters": "Why it matters",
    "grounded_in": "Based on",
    "category": "Category",
    "quick_win": "Quick win (30–90 min)",
    "first_step": "First step",
    "bigger_play": "Bigger play (when you're ready)",
    "suggested_experiment": "Suggested experiment",  # legacy items
    "time_required": "Time required to explore",
    "verdict": "Verdict",
}


def to_json_str(analysis: dict) -> str:
    """JSON-serialize an analysis dict for storage in items.analysis."""
    return json.dumps(analysis, ensure_ascii=False)


def parse_stored(stored: str) -> dict | None:
    """Parse stored analysis text. Returns a dict if it's JSON in our schema, else None (legacy text row)."""
    if not stored:
        return None
    s = stored.lstrip()
    if not s.startswith("{"):
        return None
    try:
        d = json.loads(s)
    except json.JSONDecodeError:
        return None
    if not isinstance(d, dict):
        return None
    # Heuristic: it's our shape if it has at least one of our keys
    if not any(k in d for k in ANALYSIS_FIELDS):
        return None
    return _normalize(d)


def to_plain_text(analysis: dict) -> str:
    """Render a structured analysis dict as the labeled text our pre-existing UIs expect."""
    lines = []
    for key in ANALYSIS_FIELDS:
        value = analysis.get(key, "")
        if not value:
            continue
        lines.append(f"{_LEGACY_FIELD_LABELS[key]}: {value}")
    suggestions = analysis.get("suggestions") or []
    if suggestions:
        parts = ["Suggestions:"]
        for s in suggestions:
            title = (s.get("title") or "").strip()
            detail = (s.get("detail") or "").strip()
            effort = (s.get("effort") or "").strip()
            parts.append(f"- {title}" + (f" ({effort})" if effort else "") + (f": {detail}" if detail else ""))
            first_step = (s.get("first_step") or "").strip()
            if first_step:
                parts.append(f"  First step: {first_step}")
        lines.append("\n".join(parts))
    return "\n\n".join(lines)
