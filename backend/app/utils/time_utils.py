"""Time helpers for consistent UTC handling."""

from __future__ import annotations

from datetime import datetime, timezone


def utc_now() -> datetime:
    """Return a timezone-naive UTC datetime for DB compatibility."""
    return datetime.now(timezone.utc).replace(tzinfo=None)
