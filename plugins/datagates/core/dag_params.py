"""
plugins/datagates/core/dag_params.py

Parsing of free-text DAG params the Airflow trigger form hands us.

The form renders a string Param's description as a placeholder, and it
is easy to submit that text as the value: the first run of
rpr_mesh_meters_backfill failed on `end_date = 'now (UTC)'`, which was
the description of the field. Anything a person would type to mean
"up to now" is accepted here, and a real mistake fails with a message
that lists what would have worked.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

_NOW_WORDS = ("", "now", "today", "none", "null")


def parse_end_datetime(text: str | None, *, now: datetime | None = None) -> datetime:
    """'' / 'now' / 'now (UTC)' / 'today' -> now (UTC); 'yesterday' -> 24 h
    ago; 'YYYY-MM-DD' -> that day at 00:00 UTC; a full ISO datetime ->
    itself, made UTC-aware."""
    now = now or datetime.now(UTC)
    raw = (text or "").strip()
    head = raw.lower().split(" ")[0].split("(")[0]
    if head in _NOW_WORDS:
        return now
    if head == "yesterday":
        return now - timedelta(days=1)
    try:
        if len(raw) == 10:
            return datetime.combine(date.fromisoformat(raw), datetime.min.time(), tzinfo=UTC)
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except ValueError:
        raise ValueError(
            f"end_date {raw!r} is not a date. Leave it empty (or 'now') for now, "
            "or give YYYY-MM-DD / an ISO 8601 datetime."
        ) from None


def parse_start_date(text: str | None) -> date:
    raw = (text or "").strip()
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        raise ValueError(f"start_date {raw!r} is not a date; give YYYY-MM-DD.") from None
