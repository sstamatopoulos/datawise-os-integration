"""Window arithmetic: the part that quietly loses readings when it is wrong."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from datagates.core.windows import keep_while_walking_back, windows

DAY = timedelta(days=1)


def test_windows_tile_a_span_without_gaps_or_overlaps():
    start, end = datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 4, tzinfo=UTC)
    got = list(windows(start, end, DAY))
    assert got == [(datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 2, tzinfo=UTC)),
                   (datetime(2026, 9, 2, tzinfo=UTC), datetime(2026, 9, 3, tzinfo=UTC)),
                   (datetime(2026, 9, 3, tzinfo=UTC), datetime(2026, 9, 4, tzinfo=UTC))]
    # Each window's upper bound is the next one's lower bound; since fetch()
    # excludes its lower bound, the shared instant belongs to exactly one.
    assert [a for a, _ in got[1:]] == [b for _, b in got[:-1]]


def test_the_last_window_is_short_rather_than_overshooting():
    start, end = datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 3, 6, tzinfo=UTC)
    got = list(windows(start, end, DAY))
    assert got[-1] == (datetime(2026, 9, 3, tzinfo=UTC), end)
    assert all(b <= end for _, b in got), "no window may ask for data beyond the span"


def test_an_empty_or_backwards_span_yields_nothing():
    now = datetime(2026, 9, 1, tzinfo=UTC)
    assert list(windows(now, now, DAY)) == []
    assert list(windows(now, now - DAY, DAY)) == []


def test_the_backfill_keeps_the_sample_on_the_boundary():
    """The regression this module exists for. A cursor backfill walks backwards
    in windows; the sample at exactly the cursor is the one the *previous*
    window could not fetch, because it was that window's exclusive lower
    bound. Dropping it here loses it entirely — one reading per boundary, per
    series, silently."""
    cursor = 1_789_000_000_000
    assert keep_while_walking_back(cursor, cursor), "the boundary sample must be stored"
    assert keep_while_walking_back(cursor - 1, cursor)
    assert not keep_while_walking_back(cursor + 1, cursor), "and nothing newer than the cursor"
