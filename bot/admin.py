"""Admin observability endpoints — read-only aggregations over operational data.

Auth is intentionally separate from the try-secret used by /api/try and /api/job:
admin endpoints take their own `FILTER_FYI_ADMIN_SECRET` so the two can rotate
independently and a try-secret leak doesn't grant admin access. The only
legitimate caller is the Worker's admin.filter.fyi handler, which holds the
matching secret as `BOT_ADMIN_KEY` and passes it as `x-filter-fyi-admin-secret`.

Endpoints are kept "dumb SQL with one round-trip" — each returns one pre-rolled
shape the Worker can render directly. Where a chart needs a continuous day
series, this module fills in zero-call days so the renderer doesn't have to.

Currently exposes:
  GET /api/admin/cost-overview?days=N
    All cost/usage aggregations for the last N days (default 30, max 365),
    drawn from llm_calls. See `cost_overview()` for the response shape.
"""
from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Query

# llm_calls lives on the same Fly SQLite as everything else; bot.db's private
# connection helper is the canonical entry point. We don't re-implement it.
from bot.db import _get_conn

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin")

_MAX_DAYS = 365
_DEFAULT_DAYS = 30
_TOP_USERS_LIMIT = 10


def _require_admin_secret(
    x_filter_fyi_admin_secret: str | None = Header(default=None),
) -> None:
    """Validate the admin shared secret.

    Mirrors `_require_try_secret`'s shape but reads a different env var so the
    secrets can rotate independently. Fails closed when unset rather than
    accepting any value — an unconfigured admin surface must never serve.
    """
    expected = os.getenv("FILTER_FYI_ADMIN_SECRET")
    if not expected:
        raise HTTPException(status_code=503, detail={"error": "service-unavailable"})
    if not x_filter_fyi_admin_secret or not secrets.compare_digest(
        x_filter_fyi_admin_secret, expected
    ):
        raise HTTPException(status_code=401, detail={"error": "unauthorized"})


def _since_iso(days: int) -> str:
    """ISO-8601 cutoff for `days` ago in UTC, formatted to match the storage
    shape stamped by SQLite's `strftime('%Y-%m-%dT%H:%M:%S','now')` default."""
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )


def _day_range(days: int) -> list[str]:
    """All YYYY-MM-DD day strings in the window, oldest first, inclusive of
    today. Used to backfill 0-call days so the renderer always gets a
    contiguous series for the sparkline."""
    today = datetime.now(timezone.utc).date()
    return [(today - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]


def cost_overview(days: int) -> dict:
    """Build the cost-overview payload from llm_calls.

    Returned shape (all monetary values are USD, all token counts are integers):

        {
          "range_days": int,
          "as_of": ISO-8601 UTC,
          "kpis": {
            "total_calls": int,
            "total_cost_usd": float,
            "total_input_tokens": int,
            "total_output_tokens": int,
            "errors": int,
            "error_rate": float    # 0..1, 0 if total_calls == 0
          },
          "daily": [{day, calls, cost_usd, input_tokens, output_tokens, errors}, ...],
          "by_source_type": [{source_type, calls, cost_usd, tokens}, ...],
          "by_purpose":     [{purpose, calls, cost_usd, avg_latency_ms, errors}, ...],
          "by_identity":    [
              {"kind": "signed_in", "calls", "cost_usd", "unique_actors"},
              {"kind": "anon",      "calls", "cost_usd", "unique_actors"}
          ],
          "top_users": [{user_id, calls, cost_usd}, ...],
          "cache": {
            "hits": int,
            "cost_saved_usd": float,   # sum of estimated savings on hits
            "hit_rate": float,         # 0..1 over (hits + cacheable upstream)
            "by_purpose": [{purpose, hits, cost_saved_usd}, ...]
          }
        }
    """
    since = _since_iso(days)
    days_index = _day_range(days)

    with _get_conn() as conn:
        # KPIs (all statuses; error_rate is errors / all calls).
        kpi_row = conn.execute(
            """
            SELECT
                COUNT(*)                                                AS total_calls,
                COALESCE(SUM(cost_usd), 0)                              AS total_cost_usd,
                COALESCE(SUM(input_tokens), 0)                          AS total_input_tokens,
                COALESCE(SUM(output_tokens), 0)                         AS total_output_tokens,
                COALESCE(SUM(CASE WHEN status='error' THEN 1 ELSE 0 END), 0) AS errors
            FROM llm_calls
            WHERE ts >= ?
            """,
            (since,),
        ).fetchone()

        # Daily series. Group by date-prefix of ts (we stamp 'YYYY-MM-DDTHH:MM:SS'
        # so substr(1, 10) is the date) and backfill missing days below.
        daily_rows = conn.execute(
            """
            SELECT
                substr(ts, 1, 10)                                       AS day,
                COUNT(*)                                                AS calls,
                COALESCE(SUM(cost_usd), 0)                              AS cost_usd,
                COALESCE(SUM(input_tokens), 0)                          AS input_tokens,
                COALESCE(SUM(output_tokens), 0)                         AS output_tokens,
                SUM(CASE WHEN status='error' THEN 1 ELSE 0 END)         AS errors
            FROM llm_calls
            WHERE ts >= ?
            GROUP BY day
            ORDER BY day
            """,
            (since,),
        ).fetchall()
        by_day = {r["day"]: r for r in daily_rows}
        daily = [
            {
                "day": d,
                "calls": by_day[d]["calls"] if d in by_day else 0,
                "cost_usd": by_day[d]["cost_usd"] if d in by_day else 0.0,
                "input_tokens": by_day[d]["input_tokens"] if d in by_day else 0,
                "output_tokens": by_day[d]["output_tokens"] if d in by_day else 0,
                "errors": by_day[d]["errors"] if d in by_day else 0,
            }
            for d in days_index
        ]

        # Successful calls only — source_type breakdown is about what users are
        # analysing, not what's failing. Failures are surfaced separately.
        source_rows = conn.execute(
            """
            SELECT
                COALESCE(NULLIF(source_type, ''), '(unknown)')          AS source_type,
                COUNT(*)                                                AS calls,
                COALESCE(SUM(cost_usd), 0)                              AS cost_usd,
                COALESCE(SUM(input_tokens + output_tokens), 0)          AS tokens
            FROM llm_calls
            WHERE ts >= ? AND status = 'ok'
            GROUP BY source_type
            ORDER BY cost_usd DESC
            """,
            (since,),
        ).fetchall()

        # By purpose: include error count and avg latency (successful only —
        # error rows have latency_ms but zero tokens, so they'd skew the avg).
        purpose_rows = conn.execute(
            """
            SELECT
                purpose,
                COUNT(*)                                                AS calls,
                COALESCE(SUM(cost_usd), 0)                              AS cost_usd,
                CAST(COALESCE(AVG(CASE WHEN status='ok' THEN latency_ms END), 0) AS INTEGER) AS avg_latency_ms,
                SUM(CASE WHEN status='error' THEN 1 ELSE 0 END)         AS errors
            FROM llm_calls
            WHERE ts >= ?
            GROUP BY purpose
            ORDER BY cost_usd DESC
            """,
            (since,),
        ).fetchall()

        # Identity split. Two separate queries rather than a CASE-WHEN GROUP BY
        # keeps the unique-actor count clean (anon counts distinct anon_id,
        # signed-in counts distinct user_id; the dimensions aren't comparable).
        signed_in = conn.execute(
            """
            SELECT
                COUNT(*)                          AS calls,
                COALESCE(SUM(cost_usd), 0)        AS cost_usd,
                COUNT(DISTINCT user_id)           AS unique_actors
            FROM llm_calls
            WHERE ts >= ? AND status = 'ok' AND user_id IS NOT NULL
            """,
            (since,),
        ).fetchone()
        anon = conn.execute(
            """
            SELECT
                COUNT(*)                          AS calls,
                COALESCE(SUM(cost_usd), 0)        AS cost_usd,
                COUNT(DISTINCT anon_id)           AS unique_actors
            FROM llm_calls
            WHERE ts >= ? AND status = 'ok' AND user_id IS NULL
            """,
            (since,),
        ).fetchone()

        # Top users by total cost. user_id only — emails are deliberately not
        # exposed in this response (admin auth is gated by Cloudflare Access,
        # but we still keep PII out of the analytics surface).
        top_user_rows = conn.execute(
            """
            SELECT
                user_id,
                COUNT(*)                          AS calls,
                COALESCE(SUM(cost_usd), 0)        AS cost_usd
            FROM llm_calls
            WHERE ts >= ? AND status = 'ok' AND user_id IS NOT NULL
            GROUP BY user_id
            ORDER BY cost_usd DESC
            LIMIT ?
            """,
            (since, _TOP_USERS_LIMIT),
        ).fetchall()

        # Cache hits: total + per-purpose breakdown + estimated savings.
        # "Saved" is the sum of cost_saved_usd stamped at hit time (which
        # itself was the trailing 7-day average cost-per-call of that
        # purpose). Honest if pricing/usage have been stable; conservative
        # if the recent average was below the actual cost of THIS specific
        # input. Either way it's the right order of magnitude.
        cache_row = conn.execute(
            """
            SELECT
                COUNT(*)                              AS hits,
                COALESCE(SUM(cost_saved_usd), 0)      AS cost_saved_usd
            FROM llm_cache_hits
            WHERE ts >= ?
            """,
            (since,),
        ).fetchone()
        cache_by_purpose_rows = conn.execute(
            """
            SELECT
                purpose,
                COUNT(*)                              AS hits,
                COALESCE(SUM(cost_saved_usd), 0)      AS cost_saved_usd
            FROM llm_cache_hits
            WHERE ts >= ?
            GROUP BY purpose
            ORDER BY hits DESC
            """,
            (since,),
        ).fetchall()

    total_calls = kpi_row["total_calls"] or 0
    errors = kpi_row["errors"] or 0
    error_rate = (errors / total_calls) if total_calls > 0 else 0.0

    hits_total = cache_row["hits"] or 0
    cost_saved = cache_row["cost_saved_usd"] or 0.0
    # Hit rate = hits / (hits + upstream-calls). Upstream-calls here counts
    # only `purpose IN ('analyze', 'summary')` to keep the dimension honest
    # (image and other purposes don't run through the cache yet).
    cacheable_calls = sum(
        r["calls"] for r in purpose_rows if r["purpose"] in ("analyze", "summary")
    )
    hit_rate_denom = hits_total + cacheable_calls
    hit_rate = (hits_total / hit_rate_denom) if hit_rate_denom > 0 else 0.0

    return {
        "range_days": days,
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "kpis": {
            "total_calls": total_calls,
            "total_cost_usd": round(kpi_row["total_cost_usd"], 6),
            "total_input_tokens": kpi_row["total_input_tokens"] or 0,
            "total_output_tokens": kpi_row["total_output_tokens"] or 0,
            "errors": errors,
            "error_rate": round(error_rate, 4),
        },
        "daily": daily,
        "by_source_type": [
            {
                "source_type": r["source_type"],
                "calls": r["calls"],
                "cost_usd": round(r["cost_usd"], 6),
                "tokens": r["tokens"],
            }
            for r in source_rows
        ],
        "by_purpose": [
            {
                "purpose": r["purpose"],
                "calls": r["calls"],
                "cost_usd": round(r["cost_usd"], 6),
                "avg_latency_ms": r["avg_latency_ms"],
                "errors": r["errors"],
            }
            for r in purpose_rows
        ],
        "by_identity": [
            {
                "kind": "signed_in",
                "calls": signed_in["calls"] or 0,
                "cost_usd": round(signed_in["cost_usd"], 6),
                "unique_actors": signed_in["unique_actors"] or 0,
            },
            {
                "kind": "anon",
                "calls": anon["calls"] or 0,
                "cost_usd": round(anon["cost_usd"], 6),
                "unique_actors": anon["unique_actors"] or 0,
            },
        ],
        "top_users": [
            {
                "user_id": r["user_id"],
                "calls": r["calls"],
                "cost_usd": round(r["cost_usd"], 6),
            }
            for r in top_user_rows
        ],
        "cache": {
            "hits": hits_total,
            "cost_saved_usd": round(cost_saved, 6),
            "hit_rate": round(hit_rate, 4),
            "by_purpose": [
                {
                    "purpose": r["purpose"],
                    "hits": r["hits"],
                    "cost_saved_usd": round(r["cost_saved_usd"], 6),
                }
                for r in cache_by_purpose_rows
            ],
        },
    }


@router.get("/cost-overview")
async def cost_overview_endpoint(
    days: int = Query(default=_DEFAULT_DAYS, ge=1, le=_MAX_DAYS),
    _: None = Depends(_require_admin_secret),
) -> dict:
    """Cost & usage rollup across `llm_calls` for the last `days` days.

    Caller is the Worker's admin dashboard. Response is pre-shaped for direct
    rendering — see `cost_overview()` for the full shape.
    """
    return cost_overview(days)
