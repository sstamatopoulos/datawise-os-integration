"""
datagates.core.cadence — how often a gate is supposed to produce data.

A summary entity whose `lastReadingAt` is two hours old is healthy for a
gate that runs at 03:00 and broken for one that runs every five minutes.
Anything that judges freshness — the verification script, an operator
dashboard, a health-check DAG — needs the gate's cadence, and the only
place it is written down is the cron expression in gates.yaml.

`expected_interval` turns that expression into the longest gap between
runs that is normal. It is deliberately approximate: a comparison against
`3 x` the interval separates "the upstream is down" from "the next run has
not happened yet", and no amount of cron arithmetic improves on that.
"""
from __future__ import annotations

from datetime import timedelta

ALIASES: dict[str, timedelta] = {
    "@yearly": timedelta(days=365), "@annually": timedelta(days=365),
    "@monthly": timedelta(days=31), "@weekly": timedelta(days=7),
    "@daily": timedelta(days=1), "@midnight": timedelta(days=1),
    "@hourly": timedelta(hours=1), "@continuous": timedelta(minutes=1),
}


def _field_interval(field: str, span: int) -> int:
    """The gap, in the field's own unit, between the values it matches.
    `span` is how many units the field covers (60 minutes, 24 hours)."""
    field = field.strip()
    if field in ("*", "?"):
        return 1
    if field.startswith("*/"):
        try:
            return max(1, int(field[2:]))
        except ValueError:
            return 1
    if "," in field:
        # Several fixed values: the average gap. The real worst case is the
        # widest gap between them (22 h for "12,14"), which the tolerance
        # factor of the caller covers.
        return max(1, span // max(1, len(field.split(","))))
    if "-" in field or "/" in field:                 # a range, possibly stepped
        step = field.split("/")[-1]
        try:
            return max(1, int(step))
        except ValueError:
            return 1
    return span                                      # one fixed value: once per span


def expected_interval(schedule: str | None) -> timedelta | None:
    """The longest normal gap between runs of this schedule, or None when
    the gate is not scheduled (it is triggered by hand)."""
    if not schedule:
        return None
    schedule = schedule.strip()
    if schedule.startswith("@"):
        return ALIASES.get(schedule.lower())
    fields = schedule.split()
    if len(fields) < 5:
        return None
    minute, hour, day_of_month, _month, day_of_week = fields[:5]
    minutes = _field_interval(minute, 60)
    if hour.strip() in ("*", "?"):
        return timedelta(minutes=minutes)
    hours = _field_interval(hour, 24)
    if day_of_month.strip() not in ("*", "?"):
        return timedelta(days=_field_interval(day_of_month, 31))
    if day_of_week.strip() not in ("*", "?"):
        return timedelta(days=7 if "," not in day_of_week else 7 // max(1, len(day_of_week.split(","))))
    return timedelta(hours=hours)


def describe(schedule: str | None) -> str:
    """A cadence in words, for reports read by people."""
    interval = expected_interval(schedule)
    if interval is None:
        return "on demand"
    minutes = interval.total_seconds() / 60
    if minutes < 60:
        return f"every {int(minutes)} min"
    if minutes < 1440:
        return f"every {interval.total_seconds() / 3600:.0f} h"
    return f"every {interval.days} d"
