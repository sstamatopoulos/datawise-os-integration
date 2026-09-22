# plugins/datagates/core/counter_guard.py
"""
Reject impossible readings from cumulative counters before they are stored.

Why this exists
---------------
A cumulative counter (energy, volume, gasIndex, …) only ever goes up. When
a reading breaks that — by falling, or by jumping to another meter's
magnitude and back — one of three things has happened, and they need
different treatment:

  1. Another meter's readings landed on this series. Observed on
     ThingsBoard meters in one deployment, as readings 2-4 s off the meter's steady cadence
     carrying a different meter's value entirely, in either direction:

         04:11:25   188,434.06     <- real
         04:11:27    75,408.99     <- foreign (below)
         04:21:25   188,435.73     <- real series, undisturbed

         17:45:33     1,846.13     <- real
         17:55:31     2,167.05     <- foreign (above)
         17:55:33     1,846.15     <- real series, undisturbed

     Sometimes it is one reading; sometimes the two meters interleave
     1:1 for hours (:05 real, :08 foreign, :05 real, ...). Either way
     the genuine series carries on underneath, undisturbed. This is the
     damaging case: `difference(nonNegative: true)` discards each fall
     but KEEPS each climb, so every foreign reading books a whole
     meter-reading of consumption. One meter summed to 2.66 GWh over
     90 days against a true 12.4 MWh; another that has consumed nothing
     all year showed 19,991 kWh from a single foreign reading.

  2. The meter was reset or replaced. The value drops and STAYS down,
     counting up again from the new base. A gateway zeroed six heat
     meters at the same instant:

         12:53:42   190,485.00
         12:54:49         0.00     <- and it stays there
         12:55:19         0.00
         12:55:49         0.00

     Legitimate. The readings must be kept, and the step recorded so a
     consumer can account for the lost quantity rather than silently
     under-reporting.

The distinction is *persistence*, and the old track is the known-good
one. A fall is a reset only if the new level holds for CONFIRM_SAMPLES
with no return to the old track; otherwise the fallen samples were
foreign. A rise is genuine only if the old track does not reappear at
all in the lookahead; if it does — even once, as it does every other
sample in an interleaved stretch — the risen sample was foreign. So no
sample is ever judged on its own, and a new level has to displace the
old one outright to be believed.

What this cannot decide
-----------------------
The last sample of a batch has nothing after it to be judged by, and a
fall on the second-to-last has one sample instead of CONFIRM_SAMPLES.
Those are stored as they are — dropping a reading we cannot assess would
lose real data far more often than it would catch a glitch — and the
next batch judges its own samples against the newest value on record
(`last_known`). A foreign reading that lands in that position therefore
gets through. The guide's hourly-`last` differencing (§7.5) sidesteps a
lone stored glitch in all but the hour it falls in, so the damage is
bounded.

A genuine reading immediately followed by a foreign one that sits
between it and the previous level (100, 105, 102, 106 — where 102 is the
intruder) is resolved the other way round: 105 is dropped and 102 kept.
For a counter that is harmless — the consumption across the pair is the
same either way — and it is the price of never adopting a foreign track.

Backfills
---------
Pass `last_known=None` when the batch is historical. `last_known` is the
NEWEST value on record; a backfill's samples are older and therefore
lower, so against that floor every one of them looks like a fall and the
batch gets logged as a "reset". Judged on its own, a historical window
is handled exactly like a live one.

This module is deliberately pure: no Airflow, no clients, no I/O. Feed it
samples, get back what to store.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Properties whose values are running totals — from core.vocab, which is the
# registry. This used to be a literal set here, a second copy in
# clients/python/datagates_client.py and a third list in docs/data-model.md,
# each with a comment promising to stay in step with the others. Deriving it
# means registering a cumulative property is the only step; the deprecated
# names (gasIndex, waterIndex) are still guarded, because a deployment that
# carries them from before the rename must not quietly lose its protection.
from datagates.core.vocab import CUMULATIVE_PROPERTIES  # noqa: F401  (re-exported)

# How many consecutive samples must agree with a new level before we
# believe it. Two is enough to reject a lone interloper while still
# accepting a replacement on its second reading; raising it delays
# acceptance of a genuine reset by that many polls.
CONFIRM_SAMPLES = 2

# Absolute slack, in the counter's own unit, for float noise and meters
# that jitter in their last digit. A step smaller than this is not treated
# as a step at all.
TOLERANCE = 0.001


@dataclass
class CounterReset:
    """A believed-genuine reset: the counter restarted from a lower base."""
    observed_at: str
    previous_value: float
    new_value: float

    @property
    def lost(self) -> float:
        """Quantity the old meter had accumulated and that the new base drops."""
        return self.previous_value - self.new_value


@dataclass
class GuardResult:
    accepted: list[dict] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)
    resets: list[CounterReset] = field(default_factory=list)

    @property
    def summary(self) -> str:
        bits = [f"{len(self.accepted)} accepted"]
        if self.rejected:
            bits.append(f"{len(self.rejected)} rejected as out-of-track")
        if self.resets:
            bits.append(f"{len(self.resets)} counter reset(s)")
        return ", ".join(bits)


def _num(sample: dict) -> float | None:
    try:
        return float(sample["value"])
    except (KeyError, TypeError, ValueError):
        return None


def guard_counter_samples(
    samples: list[dict],
    last_known: float | None = None,
    *,
    confirm: int = CONFIRM_SAMPLES,
    tolerance: float = TOLERANCE,
) -> GuardResult:
    """Split cumulative-counter samples into what to keep and what to drop.

    `samples` are dicts carrying at least `value` and `observed_at`; they
    are sorted here, so call order does not matter. `last_known` is the
    newest value already stored for this series (the summary entity's
    `lastReadingValue`), which lets the guard catch a glitch that lands on
    the first sample of a batch — otherwise the batch has nothing to judge
    it against.

    Returns every input sample in exactly one of `accepted` / `rejected`,
    plus a `resets` list describing the falls that were believed.

    A sample is rejected only when the readings after it return to the
    earlier track. When a new level persists for `confirm` samples it is
    believed: a lower level is recorded as a CounterReset, a higher one is
    simply consumption. When the batch ends before the question is
    settled, the samples are kept. This never invents or edits a value —
    it only decides whether to store one.
    """
    ordered = sorted(samples, key=lambda s: s["observed_at"])
    out = GuardResult()
    floor = last_known

    i = 0
    while i < len(ordered):
        s = ordered[i]
        v = _num(s)
        if v is None:
            out.rejected.append(s)
            i += 1
            continue

        if floor is None:
            out.accepted.append(s)
            floor = v
            i += 1
            continue

        if v > floor + tolerance:
            # A rise. Genuine readings rise; so does a value borrowed from
            # a bigger meter. Same test as for a fall: what comes next. If
            # the old track reappears at all within the lookahead — a
            # sample below this one but at or above the old floor — this
            # one was the intruder. "At all", not "exclusively": when a
            # foreign meter's readings interleave 1:1 with the genuine ones
            # for hours, every other sample keeps up with the intruder, and
            # a rule that needed the old track to come back on its own
            # would adopt the foreign track and throw the real one away.
            # The old track is the known-good one; a new level must displace
            # it outright. Samples below the old floor are a fall, not a
            # return, and are judged on their own turn.
            look = [_num(x) for x in ordered[i + 1:i + 1 + confirm]]
            if any(n is not None and floor - tolerance <= n < v - tolerance
                   for n in look):
                out.rejected.append(s)
                i += 1
                continue
            out.accepted.append(s)
            floor = v
            i += 1
            continue

        if v >= floor - tolerance:
            out.accepted.append(s)          # flat, or within tolerance
            i += 1
            continue

        # v is below the established floor. Glitch, or the new normal?
        run = [s]
        j = i + 1
        returned = False
        while j < len(ordered) and len(run) < confirm:
            nxt = _num(ordered[j])
            if nxt is None:
                break
            if nxt >= floor - tolerance:
                returned = True             # back on the old track
                break
            run.append(ordered[j])
            j += 1

        if len(run) >= confirm:
            out.resets.append(CounterReset(
                observed_at=str(s["observed_at"]),
                previous_value=float(floor),
                new_value=v,
            ))
            out.accepted.extend(run)
            floor = max(_num(x) for x in run)
        elif returned:
            out.rejected.extend(run)        # the series carried on without it
        else:
            out.accepted.extend(run)        # batch ended: undecided, keep
        i += len(run)

    return out


def guard_samples_by_property(
    samples: list[dict],
    last_known: dict[str, float] | None = None,
    cumulative: set[str] | frozenset[str] | None = None,
    **kw,
) -> tuple[list[dict], GuardResult]:
    """Apply the guard to the cumulative properties in a mixed batch.

    `samples` is the flat list a DAG builds for InfluxDB, each entry
    carrying `controlled_property`. Non-cumulative properties (power,
    temperature, …) pass through untouched — a gauge falling is just a
    gauge falling.

    Returns (samples_to_write, combined_result_for_logging).
    """
    last_known = last_known or {}
    cumulative = frozenset(cumulative) if cumulative is not None else CUMULATIVE_PROPERTIES
    passthrough = [s for s in samples
                   if s.get("controlled_property") not in cumulative]

    combined = GuardResult()
    for prop in sorted({s.get("controlled_property") for s in samples}
                       & cumulative):
        res = guard_counter_samples(
            [s for s in samples if s.get("controlled_property") == prop],
            last_known.get(prop), **kw)
        combined.accepted.extend(res.accepted)
        combined.rejected.extend(res.rejected)
        combined.resets.extend(res.resets)

    return passthrough + combined.accepted, combined
