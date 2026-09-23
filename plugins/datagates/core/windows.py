"""
datagates.core.windows — splitting a span into requests, and the boundary rule.

Two lines of arithmetic that cost seven readings out of 241 the first time a
cursor backfill was run against a dense series, so they live here with their
reasoning and a test instead of inline in the DAG factory.

A gate's `fetch(device, a, b)` returns samples in **(a, b]** — the lower bound
exclusive, the upper inclusive. That is what makes consecutive windows tile a
span without storing anything twice, and it is why the backfill's filter has
to be inclusive at the cursor:

    window k     fetches (a_k, cursor_k]      and keeps  <= cursor_k
    window k+1   fetches (a_k+1, a_k]         where cursor_k+1 = a_k

The sample at exactly `a_k` is the *lower* bound of window k, so window k never
fetches it; window k+1 fetches it as its upper bound. If window k+1 then drops
everything `>= its cursor` — which is `a_k` — that sample is dropped by both
windows and is never stored. One reading lost per window boundary: with daily
windows, one a day, in every backfilled series, and nothing anywhere says so.

Re-storing a boundary sample instead is free: writes are keyed by time, so the
second write of the same instant overwrites the first with the same value.
When a choice is between a duplicate write and a lost reading, take the
duplicate.
"""
from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta


def windows(start: datetime, end: datetime, step: timedelta) -> Iterator[tuple[datetime, datetime]]:
    """Walk [start, end) forwards in `step`-long windows, the last one short.

    Consecutive windows share a bound: (a, b] then (b, c]. Because a gate's
    fetch excludes its lower bound, the shared instant belongs to exactly one
    window and nothing is stored twice.
    """
    a = start
    while a < end:
        b = min(a + step, end)
        yield a, b
        a = b


def keep_while_walking_back(observed_ms: int, cursor_ms: int) -> bool:
    """Should a sample fetched by a backwards-walking window be stored?

    Inclusive at the cursor, deliberately: see the module docstring. The
    alternative — a strict `<` — loses the sample sitting exactly on every
    window boundary, because no window ever fetches it twice.
    """
    return observed_ms <= cursor_ms
