"""
services/rate_limit.py — Per-IP rate limiting for the public /chat endpoint.

Uses a `rate_limits` table (persisted in Postgres) with a fixed 24h window
per client IP. The limit is checked/incremented atomically so that the N-th
allowed request still gets its full response and only the (N+1)-th request is
blocked — matching the desired "reply one last time, then block" behavior.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from config import get_pool, settings

logger = logging.getLogger(__name__)


async def _fetch_status(ip: str, consume: bool) -> dict:
    """Return {allowed, limit, remaining, resets_in} for an IP.

    When `consume` is True the request is counted (atomic upsert with a
    sliding 24h window). When False the current status is read only.
    """
    limit = settings.rate_limit_max
    window_hours = settings.rate_limit_window_hours

    if not settings.rate_limit_enabled:
        return {
            "allowed": True,
            "limit": limit,
            "remaining": limit,
            "resets_in": window_hours * 3600,
        }

    pool = await get_pool()
    async with pool.acquire() as conn:
        if consume:
            row = await conn.fetchrow(
                """
                INSERT INTO rate_limits (ip, request_count, window_start)
                VALUES ($1, 1, now())
                ON CONFLICT (ip) DO UPDATE
                SET request_count = CASE
                        WHEN rate_limits.window_start < now() - make_interval(hours => $2) THEN 1
                        ELSE rate_limits.request_count + 1
                    END,
                    window_start = CASE
                        WHEN rate_limits.window_start < now() - make_interval(hours => $2) THEN now()
                        ELSE rate_limits.window_start
                    END
                RETURNING request_count, window_start
                """,
                ip,
                window_hours,
            )
            count = row["request_count"]
            window_start = row["window_start"]
        else:
            row = await conn.fetchrow(
                "SELECT request_count, window_start FROM rate_limits WHERE ip = $1",
                ip,
            )
            if row is None:
                return {
                    "allowed": True,
                    "limit": limit,
                    "remaining": limit,
                    "resets_in": window_hours * 3600,
                }
            count = row["request_count"]
            window_start = row["window_start"]

    if window_start.tzinfo is None:
        window_start = window_start.replace(tzinfo=timezone.utc)

    now = datetime.now(timezone.utc)
    resets_at = window_start + timedelta(hours=window_hours)
    resets_in = max(0, int((resets_at - now).total_seconds()))

    remaining = max(0, limit - count)
    return {
        "allowed": count <= limit,
        "limit": limit,
        "remaining": remaining,
        "resets_in": resets_in,
    }


async def check_rate_limit(ip: str) -> dict:
    """Read-only status for an IP (does not consume a request)."""
    return await _fetch_status(ip, consume=False)


async def consume_rate_limit(ip: str) -> dict:
    """Count one request for an IP and return whether it is still allowed."""
    return await _fetch_status(ip, consume=True)
