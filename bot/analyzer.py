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
    "suggested_experiment",
    "time_required",
    "verdict",
)

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
        "suggested_experiment": {
            "type": "string",
            "description": "A concrete next step the user could try in under a day.",
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

_PROMPT = """You are my personal AI research analyst.
{profile_block}
Analyze the following content and produce a structured analysis covering the required fields. Be concrete and specific to this person.

CONTENT:
{text}"""

_OPENAI_JSON_SUFFIX = (
    "\n\nRespond with a single JSON object containing exactly these keys: "
    + ", ".join(ANALYSIS_FIELDS)
    + ". 'verdict' must be one of: "
    + ", ".join(VERDICTS)
    + "."
)


def _load_profile(user_id: str) -> str:
    from bot.db import get_profile
    return get_profile(user_id)


def _normalize(raw: dict) -> dict:
    """Coerce LLM output into our exact schema (string fields, verdict in allowed set)."""
    out = {k: str(raw.get(k, "")).strip() for k in ANALYSIS_FIELDS}
    if out["verdict"].lower() not in VERDICTS:
        out["verdict"] = "skim"
    else:
        out["verdict"] = out["verdict"].lower()
    return out


def analyze(text: str, user_id: str) -> dict:
    """Returns a dict with keys: main_idea, why_it_matters, category, suggested_experiment, time_required, verdict."""
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
    "suggested_experiment": "Suggested experiment",
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
