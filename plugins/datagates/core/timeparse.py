"""
datagates.core.timeparse — one timestamp parser for every gate.

Legacy systems stamp their rows in every way imaginable: ISO with and
without a zone, epoch seconds, epoch milliseconds, `%d.%m.%Y %H:%M`,
Excel's own datetime objects, a date with the time in another column.
Gates describe what they get in YAML (`timestamp_format`, `timezone`)
and call `parse_stamp`; nothing upstream-specific lives here.

The framework works in UTC only. A naive timestamp is read in the gate's
declared `timezone` (default UTC) and converted; an aware one is
converted and its own offset wins.
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ISO = "iso"
EPOCH_S = "epoch_s"
EPOCH_MS = "epoch_ms"
EPOCH_US = "epoch_us"


def zone_of(name: str | None) -> ZoneInfo:
    """`timezone:` option -> tzinfo. Unknown names fail loudly at gate
    construction rather than silently shifting a year of history."""
    if not name or str(name).upper() == "UTC":
        return ZoneInfo("UTC")
    try:
        return ZoneInfo(str(name))
    except ZoneInfoNotFoundError as exc:                      # pragma: no cover - platform tzdata
        raise ValueError(f"unknown timezone {name!r}; install tzdata or use an IANA name") from exc


def parse_stamp(raw: object, fmt: str = ISO, tz: ZoneInfo | None = None) -> datetime | None:
    """Aware UTC datetime, or None when `raw` is empty or unparsable.

    Unparsable is not an error: one bad row in a partner's export must not
    stop a run, and the gates log the count of skipped rows instead.
    """
    if raw is None:
        return None
    tz = tz or ZoneInfo("UTC")
    dt: datetime | None = None
    if isinstance(raw, datetime):
        dt = raw
    elif isinstance(raw, date):
        dt = datetime(raw.year, raw.month, raw.day)
    else:
        text = str(raw).strip()
        if not text:
            return None
        try:
            if fmt == EPOCH_S:
                dt = datetime.fromtimestamp(float(text), UTC)
            elif fmt == EPOCH_MS:
                dt = datetime.fromtimestamp(float(text) / 1_000, UTC)
            elif fmt == EPOCH_US:
                dt = datetime.fromtimestamp(float(text) / 1_000_000, UTC)
            elif fmt == ISO:
                dt = datetime.fromisoformat(text.replace("Z", "+00:00").replace(" UTC", "+00:00"))
            else:
                dt = datetime.strptime(text, fmt)
        except (TypeError, ValueError, OSError, OverflowError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(UTC)


def iso_z(dt: datetime) -> str:
    """The one timestamp format the framework stores: second precision,
    literal Z. Both stores key on it, so it must never drift."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def stamp_now() -> str:
    return iso_z(datetime.now(UTC))
