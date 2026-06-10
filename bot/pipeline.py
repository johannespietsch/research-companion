"""Unified URL → analysis pipeline.

Every URL-based entry point (web `/api/try`, `/api/job`, `/api/library/add`,
`/submit/url`, and Telegram URL handler) routes through this module so they
all share identical behavior:

  1. Fetch the URL (cached in `url_cache` by exact URL).
  2. For source types where inline images carry signal (articles, social
     posts, etc.), describe each image with `analyze_image` and append the
     descriptions to the text. Image analysis itself is content-addressed-
     cached, so repeats are free.
  3. Summarize the (text + image descriptions) into the canonical brief.
  4. Analyze the summary — this bounds the analyze prompt size for long
     content (transcripts, long PDFs) and keeps the cache key stable.
  5. Optionally persist as an `items` row for signed-in users.

Caller responsibilities: present the result (HTTP JSON, Telegram reply,
async-job result blob) and translate `PipelineError` into the right
status/UX for their surface.

The module is the single source of truth for the URL pipeline. Diverging
again (e.g. analyzing the raw text instead of the summary on some paths)
will silently break the content-addressed cache, because the cache key
hashes the exact input to `analyze` — see [[feedback_llm_chain_caching]].
"""
from __future__ import annotations

import asyncio
import base64
import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional

import httpx

from bot.analyzer import (
    UsageContext,
    analyze,
    analyze_image,
    summarize_content,
    to_json_str,
)
from bot.concurrency import CapacityError, heavy
from bot.db import record_processed_url, save_item
from bot.fetcher import fetch_url, whisper_cap_for

logger = logging.getLogger(__name__)

# Which source types benefit from inline image descriptions appended to the
# analyse input. Articles and social posts often have charts, screenshots, or
# image-driven content that adds real signal. Video/audio already have the
# transcript as the canonical content; their thumbnails/cards don't add
# enough to justify the per-image LLM call. Default for `unknown` is on —
# we'd rather pay for the extra context than miss it.
_INCLUDE_IMAGES_BY_DEFAULT: frozenset[str] = frozenset({
    "article", "social", "unknown",
})


# Caller-facing error codes — stable across HTTP and Telegram surfaces.
ERR_FETCH_FAILED = "fetch-failed"            # network / timeout / connector error
ERR_NO_TEXT = "extraction-failed"            # fetch ok but yielded empty text
ERR_NO_TRANSCRIPT = "no-transcript"          # video-with-no-transcript variant of NO_TEXT
ERR_ANALYZE_FAILED = "analyze-failed"        # LLM call failed mid-chain
ERR_BUSY = "busy"                            # shed: no heavy-work slot free (overload)

_VIDEO_SOURCE_TYPES: frozenset[str] = frozenset({"youtube", "video"})

# A degraded fetch (no transcript, too long for Whisper, etc.) flags itself
# with `reason` but may still carry a thin fallback — e.g. a captionless video
# with an empty description leaves just "Title\nBy: Channel" (~50 chars).
# Below this, there's no real content to analyse, so we surface the `reason`
# instead of fabricating an analysis from a title-only stub. Only applied when
# `reason` is set, so successful (unflagged) fetches are never gated.
_MIN_ANALYZABLE_CHARS = 200


class PipelineError(Exception):
    """Raised for any failure inside `analyze_url`. Carries an `error_code`
    (one of `ERR_*` above) so callers can map it to their own status surface,
    and `fetched` if the fetch succeeded but a later step failed — this lets
    error responses include the title/source_type for nicer messages."""

    def __init__(self, code: str, *, fetched: Optional[dict] = None, message: str = ""):
        super().__init__(message or code)
        self.code = code
        self.fetched = fetched or {}


@dataclass
class PipelineResult:
    fetched: dict
    summary: str
    analysis: dict
    saved_id: int | None = None

    @property
    def source_type(self) -> str:
        return self.fetched.get("source_type") or "article"

    @property
    def title(self) -> str:
        return self.fetched.get("title") or ""

    @property
    def image_urls(self) -> list[str]:
        return self.fetched.get("image_urls") or []


async def analyze_url(
    url: str,
    *,
    ctx: UsageContext,
    save_for_user_id: int | None = None,
    user_note: str = "",
    include_images: bool | None = None,
    max_whisper_duration: int | None = None,
    on_step: Optional[Callable[[str], None]] = None,
    capacity_timeout: float | None = None,
) -> PipelineResult:
    """Run the full URL → analysis chain.

    Raises `PipelineError` on any failure mode the caller should surface
    (fetch failed, no extractable text, analyse crashed). Summarization
    failures are absorbed by `summarize_content`'s own fallback — the
    pipeline carries on with a truncated slice rather than failing the
    whole request.

    `include_images=None` (default) picks based on `source_type` — see
    `_INCLUDE_IMAGES_BY_DEFAULT`. Pass `True`/`False` to override.

    `max_whisper_duration=None` (default) derives the captionless-video
    transcription ceiling from the caller's tier (signed-in → 2 h, anon →
    30 min). Every web request runs through the async job path, so the full
    tier cap applies; the override exists only for tests / future callers.

    `on_step` is an optional sync callback invoked with stable labels
    ("fetching" | "describing-images" | "summarizing" | "analyzing") so
    callers like the async job runner can surface progress to the UI.

    `capacity_timeout` caps how long we wait for a heavy-work slot before
    shedding with `PipelineError(ERR_BUSY)`. None → the limiter default (short;
    synchronous web callers shed fast so they don't blow the Worker's ~25s
    budget); the polling job runner passes a larger value to queue instead.
    """
    def _step(label: str) -> None:
        if on_step is not None:
            try:
                on_step(label)
            except Exception:
                logger.exception("pipeline on_step callback raised; ignoring")

    if max_whisper_duration is None:
        max_whisper_duration = whisper_cap_for(signed_in=ctx.user_id is not None)

    # Bound total concurrent heavy work across EVERY entry point (web /try, the
    # async job runner, /library/add, Telegram) by taking a slot before any
    # fetch/transcribe/LLM work — this is the one chokepoint they all share.
    # Acquired outside the audit `try` so a shed request isn't recorded as
    # processed; released in that try's `finally`.
    try:
        await heavy.acquire(capacity_timeout)
    except CapacityError:
        raise PipelineError(ERR_BUSY, message="Service is busy; please try again in a few seconds.")

    # Audit-log bookkeeping. Every successful AND failed call writes one row
    # to `processed_urls`, so the Usage tile can show "what we tried and how
    # it went" without joining `error_log`. `fetched` may stay empty (a
    # fetch failure) or get the partial dict from a downstream error.
    started = time.monotonic()
    fetched: dict = {}
    audit_status = "ok"
    audit_error_code = ""

    try:
        _step("fetching")
        try:
            fetched = await fetch_url(url, max_whisper_duration=max_whisper_duration)
        except Exception as e:
            logger.exception("pipeline: fetch_url crashed for %s", url)
            raise PipelineError(ERR_FETCH_FAILED, message=str(e))

        text = (fetched.get("text") or "").strip()
        source_type = fetched.get("source_type") or "article"
        reason = fetched.get("reason")

        # Bail before analysing when there's nothing usable: either no text at all,
        # or a degraded fetch (`reason` set) whose fallback is just a title-only
        # stub. Both surface the fetch `reason` to the caller rather than running
        # the analyser on a thin snippet — see _MIN_ANALYZABLE_CHARS.
        if not text or (reason and len(text) < _MIN_ANALYZABLE_CHARS):
            # Distinguish video-with-no-transcript from generic extraction
            # failure so callers can surface the right UX.
            code = ERR_NO_TRANSCRIPT if source_type in _VIDEO_SOURCE_TYPES else ERR_NO_TEXT
            raise PipelineError(code, fetched=fetched)

        # ctx may not have source_type set when the caller built it before
        # fetching — fill it in here so downstream rows carry the right tag.
        if not ctx.source_type:
            ctx.source_type = source_type

        # Inline image descriptions: append to text so they enter the summary
        # (and therefore the analyze input) deterministically. analyze_image()
        # is content-addressed-cached, so this is free on repeats.
        if include_images is None:
            include_images = source_type in _INCLUDE_IMAGES_BY_DEFAULT
        if include_images:
            image_urls = fetched.get("image_urls") or []
            if image_urls:
                _step("describing-images")
                text = text + await _describe_images(image_urls, ctx=ctx)

        # Summarise on a thread so we don't block the event loop on the LLM
        # call. The async wrapper around a sync call mirrors what _run_job did.
        # `published_at` (when available from yt-dlp) anchors the model's
        # interpretation of relative dates in the source — without it, models
        # silently default to their training-cutoff year.
        _step("summarizing")
        published_at = fetched.get("published_at") or ""
        loop = asyncio.get_running_loop()
        summary = await loop.run_in_executor(
            None,
            lambda: summarize_content(text, ctx=ctx, published_at=published_at),
        )

        _step("analyzing")
        try:
            analysis = await loop.run_in_executor(None, lambda: analyze(summary, ctx=ctx))
        except Exception as e:
            logger.exception("pipeline: analyze crashed for %s", url)
            raise PipelineError(ERR_ANALYZE_FAILED, fetched=fetched, message=str(e))

        saved_id: int | None = None
        if save_for_user_id is not None:
            saved_id = save_item(
                user_id=save_for_user_id,
                source_type=source_type,
                source=url,
                content=summary,
                analysis=to_json_str(analysis),
                user_note=user_note,
            )

        return PipelineResult(
            fetched=fetched, summary=summary, analysis=analysis, saved_id=saved_id,
        )
    except PipelineError as e:
        # Pull whatever the exception carries — for ERR_NO_TEXT / ERR_NO_TRANSCRIPT
        # / ERR_ANALYZE_FAILED the fetched dict is attached, so the audit row
        # still shows the title and source_type even on failure.
        if e.fetched:
            fetched = e.fetched
        audit_status = "error"
        audit_error_code = e.code
        raise
    finally:
        # Free the slot before the audit write so a waiter can start sooner.
        heavy.release()
        record_processed_url(
            url=url,
            title=fetched.get("title") or "",
            source_type=fetched.get("source_type") or "",
            user_id=ctx.user_id,
            anon_id=ctx.anon_id,
            job_id=ctx.job_id,
            status=audit_status,
            error_code=audit_error_code,
            transcript_source=fetched.get("transcript_source") or "",
            latency_ms=int((time.monotonic() - started) * 1000),
        )


async def _describe_images(image_urls: list[str], *, ctx: UsageContext) -> str:
    """Download each image URL and return a combined description block.

    Moved from `bot/handlers.py` and made cache-friendly: `analyze_image`
    is content-addressed-cached, so repeated calls for the same image
    bytes cost nothing. Per-image fetch failures are logged but never
    propagate — a single broken image URL shouldn't kill the analysis.
    """
    descriptions: list[str] = []
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        for url in image_urls:
            try:
                resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
                resp.raise_for_status()
                b64 = base64.b64encode(resp.content).decode()
                # analyze_image is sync; wrap in executor to keep the loop
                # responsive when there are several images.
                loop = asyncio.get_running_loop()
                desc = await loop.run_in_executor(
                    None, lambda: analyze_image(b64, ctx=ctx)
                )
                descriptions.append(desc)
            except Exception:
                logger.exception("describe_images: skipped %s", url)
    if not descriptions:
        return ""
    joined = "\n\n".join(f"[Image {i+1}]: {d}" for i, d in enumerate(descriptions))
    return f"\n\nIMAGE DESCRIPTIONS:\n{joined}"
