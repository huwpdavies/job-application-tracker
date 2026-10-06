"""Time handling: everything is stored as UTC ISO-8601 and displayed in Europe/London."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .config import LONDON

LONDON_TZ = ZoneInfo(LONDON)
_FRACTION = re.compile(r"(\.\d{6})\d+")


def parse_dt(value: str, assume_tz: ZoneInfo | timezone = LONDON_TZ) -> datetime:
    """Parse an ISO-8601 string (Graph's 'Z' and 7-digit fractions included) to an aware datetime.

    A string with no offset is interpreted in `assume_tz` (Europe/London by default), which is what a
    human-written "2pm" in an email means. Graph values requested with outlook.timezone="UTC" should
    be parsed with assume_tz=timezone.utc.
    """
    s = value.strip().replace("Z", "+00:00")
    s = _FRACTION.sub(r"\1", s)
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=assume_tz)
    return dt


def to_utc_iso(value: str | datetime, assume_tz: ZoneInfo | timezone = LONDON_TZ) -> str:
    dt = parse_dt(value, assume_tz) if isinstance(value, str) else value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=assume_tz)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def to_london(value: str | datetime) -> datetime:
    dt = parse_dt(value, timezone.utc) if isinstance(value, str) else value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(LONDON_TZ)


def fmt_london(value: str | datetime | None, fmt: str = "%a %d %b %Y, %H:%M") -> str:
    return to_london(value).strftime(fmt) if value else ""
