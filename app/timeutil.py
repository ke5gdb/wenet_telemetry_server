"""Timestamp normalisation.

Every timestamp in the database is stored as a UTC ISO-8601 string in one fixed
format, so that lexicographic ordering is chronological ordering and SQLite can
range-scan an index over it without any date parsing.

The payload firmware emits ISO *basic* format -- ``20260726T182204.512Z``, see
``get_timestamp()`` in Wenet_BLE_Sensor/main.py -- while the ground station and
the analysis logs use various other spellings. Everything funnels through
:func:`parse_ts`.
"""

from __future__ import annotations

from datetime import datetime, timezone

#: The one true on-disk format. Fixed width, UTC, so string sort == time sort.
_STORE_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts(value: str | datetime | int | float) -> datetime:
    """Parse anything we might plausibly be handed into an aware UTC datetime.

    Naive timestamps are assumed to be UTC: the payload RTC is set from GPS/UTC
    and carries no offset, and a wrong-by-hours guess would be far worse than
    the assumption.
    """
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        dt = datetime.fromtimestamp(value, tz=timezone.utc)
    elif isinstance(value, str):
        dt = _parse_str(value.strip())
    else:
        raise ValueError(f"cannot parse timestamp of type {type(value).__name__}")

    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_str(s: str) -> datetime:
    if not s:
        raise ValueError("empty timestamp")

    # fromisoformat on 3.11+ handles both basic (20251101T131220.025Z) and
    # extended (2025-11-01T13:12:20.025+00:00) forms, including the Z suffix.
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        pass

    # The CSV debug/log spelling used by wenet_ble_client and the SD card logs.
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y%m%dT%H%M%S.%fZ"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue

    raise ValueError(f"unrecognised timestamp: {s!r}")


def iso(value: str | datetime | int | float) -> str:
    """Normalise to the on-disk string format."""
    return parse_ts(value).strftime(_STORE_FMT)


def iso_now() -> str:
    return utcnow().strftime(_STORE_FMT)


def to_epoch(value: str | datetime | int | float) -> float:
    """Epoch seconds. Stored alongside the ISO string so that time arithmetic
    -- interpolating an altitude, say -- does not mean parsing dates in a loop
    or calling substr() in SQL, which would defeat the index."""
    return parse_ts(value).timestamp()
