import json
import logging
import os
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY")
_OPENAI_KEY = os.getenv("OPENAI_API_KEY")

if not _ANTHROPIC_KEY and not _OPENAI_KEY:
    raise EnvironmentError("Set ANTHROPIC_API_KEY or OPENAI_API_KEY in .env")

_PROVIDER = "anthropic" if _ANTHROPIC_KEY else "openai"
_MODEL = "claude-haiku-4-5-20251001" if _PROVIDER == "anthropic" else "gpt-4o-mini"

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


ANALYSIS_FIELDS = (
    "main_idea",
    "why_it_matters",
    "category",
    "quick_win",
    "bigger_play",
    "time_required",
    "verdict",
)

# Older stored items used a single `suggested_experiment`. Kept as a constant so
# renderers can fall back to it; new analyses populate quick_win + bigger_play.
LEGACY_EXPERIMENT_FIELD = "suggested_experiment"

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
        "category": {
            "type": "string",
            "description": "Short topic tag (kebab-case), e.g. 'ai-engineering', 'productivity', 'crypto-trading'.",
        },
        "quick_win": {
            "type": "string",
            "description": (
                "A concrete, scoped action this person can finish in 30–90 minutes "
                "THIS WEEKEND — low activation energy, no setup marathon. Specific to "
                "their situation, not generic advice."
            ),
        },
        "bigger_play": {
            "type": "string",
            "description": (
                "A more ambitious multi-session/multi-week project for when they're "
                "ready to commit — the deeper arc that builds real capability. Make "
                "the difference from the quick win clear."
            ),
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
    "required": list(ANALYSIS_FIELDS),
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
{profile_block}
Analyze the following content and produce a structured analysis covering the required fields. Be concrete and specific to this person — `why_it_matters` should speak to their situation, not give generic advice.

For the action, give TWO distinct tiers, both grounded in this content:
- `quick_win`: something they can actually finish in 30–90 minutes this weekend (low activation energy).
- `bigger_play`: the more ambitious multi-week arc for when they're ready to commit.
Make the difference between the two obvious; don't just restate one as the other.

CONTENT:
{text}"""

_OPENAI_JSON_SUFFIX = (
    "\n\nRespond with a single JSON object containing exactly these keys: "
    + ", ".join(ANALYSIS_FIELDS)
    + ". 'verdict' must be one of: "
    + ", ".join(VERDICTS)
    + "."
)


def _load_profile(user_id: int | None) -> str:
    if user_id is None:
        return DEFAULT_PROFILE
    from bot.db import get_user_profile
    return get_user_profile(user_id) or DEFAULT_PROFILE


def _normalize(raw: dict) -> dict:
    """Coerce LLM output into our exact schema (string fields, verdict in allowed set)."""
    out = {k: str(raw.get(k, "")).strip() for k in ANALYSIS_FIELDS}
    if out["verdict"].lower() not in VERDICTS:
        out["verdict"] = "skim"
    else:
        out["verdict"] = out["verdict"].lower()
    return out


def analyze(text: str, user_id: int | None = None) -> dict:
    """Returns a dict with keys: main_idea, why_it_matters, category, quick_win, bigger_play, time_required, verdict."""
    profile = _load_profile(user_id)
    profile_block = f"\nAbout the person you are analysing for:\n{profile}\n" if profile else ""
    prompt = _PROMPT.format(profile_block=profile_block, text=text)

    client = _get_client()
    if _PROVIDER == "anthropic":
        resp = client.messages.create(
            model=_MODEL,
            max_tokens=1024,
            tools=[_ANTHROPIC_TOOL],
            tool_choice={"type": "tool", "name": "record_analysis"},
            messages=[{"role": "user", "content": prompt}],
        )
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use" and block.name == "record_analysis":
                return _normalize(block.input)
        raise RuntimeError("Anthropic did not return a record_analysis tool_use block")

    resp = client.chat.completions.create(
        model=_MODEL,
        response_format={"type": "json_object"},
        messages=[{"role": "user", "content": prompt + _OPENAI_JSON_SUFFIX}],
    )
    content = resp.choices[0].message.content or "{}"
    try:
        raw = json.loads(content)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"OpenAI returned non-JSON content: {e}: {content[:200]}")
    return _normalize(raw)


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

CONTENT:
{text}"""

# Upper bound on a stored brief. Generous enough not to truncate a long
# multi-topic distillation, but still a derived brief — not a verbatim copy of
# the source (which keeps the data-minimisation / copyright posture).
SUMMARY_MAX_CHARS = 32_000
# Anthropic output-token ceiling we'll request. Scaled per source below.
_SUMMARY_MAX_OUTPUT_TOKENS = 8_000


def _summary_output_tokens(text: str) -> int:
    """Scale requested output length to the source (~4 chars/token), so short
    inputs get short briefs and long ones get room for a full sectioned brief."""
    approx_input_tokens = len(text) // 4
    # Target roughly a quarter of the input, floored/capped to sane bounds.
    return max(512, min(_SUMMARY_MAX_OUTPUT_TOKENS, approx_input_tokens // 4))


def summarize_content(text: str) -> str:
    """Distil source content into a faithful, length-scaled structured brief.

    Stored instead of the full fetched text: rich enough to re-derive verdicts
    under a different profile and to power retrieval/chat, but a derived brief
    rather than a verbatim copy. Falls back to a truncated slice if the model
    call fails, so persistence never breaks on an LLM hiccup.
    """
    text = (text or "").strip()
    if not text:
        return ""
    prompt = _SUMMARY_PROMPT.format(text=text)
    max_tokens = _summary_output_tokens(text)
    try:
        client = _get_client()
        if _PROVIDER == "anthropic":
            resp = client.messages.create(
                model=_MODEL,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            out = "".join(
                b.text for b in resp.content if getattr(b, "type", None) == "text"
            ).strip()
        else:
            resp = client.chat.completions.create(
                model=_MODEL,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            out = (resp.choices[0].message.content or "").strip()
        return out[:SUMMARY_MAX_CHARS] if out else text[:SUMMARY_MAX_CHARS]
    except Exception as e:
        logger.warning("summarize_content failed; storing truncated slice: %s", e)
        return text[:SUMMARY_MAX_CHARS]


def analyze_image(b64: str, caption: str = "") -> str:
    """Describe and extract key info from a base64-encoded JPEG image."""
    prompt = "Extract and describe all text and key information visible in this image."
    if caption:
        prompt += f" Context provided: {caption}"

    client = _get_client()
    if _PROVIDER == "anthropic":
        resp = client.messages.create(
            model=_MODEL,
            max_tokens=1024,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
                    {"type": "text", "text": prompt},
                ],
            }],
        )
        return resp.content[0].text

    resp = client.chat.completions.create(
        model=_MODEL,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ],
        }],
    )
    return resp.choices[0].message.content


# ---------------------------------------------------------------------------
# Storage / rendering helpers
# ---------------------------------------------------------------------------

_LEGACY_FIELD_LABELS = {
    "main_idea": "Main idea",
    "why_it_matters": "Why it matters",
    "category": "Category",
    "quick_win": "Quick win (30–90 min)",
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
    return "\n\n".join(lines)
