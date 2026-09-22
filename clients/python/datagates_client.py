"""
datagates_client.py — a small client for the data-gates platform (FIWARE Orion-LD + InfluxDB).

Standard library only. Drop this file next to your script and import it:

    from datagates_client import Client

    dw = Client()                       # reads env vars, see below
    for b in dw.buildings():
        print(b["id"], dw.value(b, "name"))

The platform stores metadata in the Orion-LD context broker and
time-series in InfluxDB. This class wraps both and implements the bridge
between them (see ../README.md §8): a summary DeviceMeasurement's `id` is
literally the InfluxDB `_measurement` name, so history needs no lookup
table.

Environment
-----------
    ORION_URL      default http://localhost:1026
    INFLUX_URL     default http://localhost:8086
    INFLUX_ORG     default datagates
    INFLUX_TOKEN   required only for time-series calls
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

DEFAULT_ORION = "http://localhost:1026"
DEFAULT_INFLUX = "http://localhost:8086"

# Properties whose values are running totals rather than per-period
# amounts. Summing these is meaningless — difference consecutive
# readings instead. Kept in sync with ../README.md §12.1.
CUMULATIVE_PROPERTIES = frozenset({
    "energy", "gasIndex", "waterIndex", "volume", "workingHours",
})

# How many series a single batched query may filter on.
#
# Batched reads build `r._measurement == "a" or r._measurement == "b" or …`.
# Flux parses that into a LEFT-NESTED binary tree, one level per term, and
# rejects the script once it gets too deep:
#
#     compilation failed: error @3:24-3:1370: Program is nested too deep
#
# Measured against this deployment: 165 terms compiled, 184 did not. The
# failure is a 400 at compile time, so it is loud rather than silent — but
# only if you do not swallow it. `contains(value:, set: [...])` avoids the
# nesting but is not pushed down into the storage engine: the same query
# that takes ~175 ms as an or-chain did not finish in 10 minutes. So:
# keep the or-chain, and chunk it well below the limit.
MAX_SERIES_PER_QUERY = 100


class DataGatesError(RuntimeError):
    """Any failure talking to Orion or InfluxDB."""


def cli_args(argv: list[str] | None = None,
             duration_opts: tuple[str, ...] = ("--start", "--stop", "--every"),
             ) -> list[str]:
    """Make `--start -7d` work as well as `--start=-7d`.

    Flux durations are naturally negative, but argparse sees the leading
    dash and treats the value as another option, failing with "expected
    one argument". Rewrite the separated form into the equals form before
    argparse ever sees it.

        args = ap.parse_args(cli_args())
    """
    import sys as _sys
    argv = list(argv if argv is not None else _sys.argv[1:])
    out: list[str] = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in duration_opts and i + 1 < len(argv):
            nxt = argv[i + 1]
            if nxt.startswith("-") and not nxt.startswith("--"):
                out.append(f"{tok}={nxt}")
                i += 2
                continue
        out.append(tok)
        i += 1
    return out


class Client:
    def __init__(
        self,
        orion: str | None = None,
        influx: str | None = None,
        org: str | None = None,
        token: str | None = None,
        timeout: int = 60,
        orion_api_key: str | None = None,
    ):
        self.orion = (orion or os.environ.get("ORION_URL") or DEFAULT_ORION).rstrip("/")
        self.influx = (influx or os.environ.get("INFLUX_URL") or DEFAULT_INFLUX).rstrip("/")
        self.org = org or os.environ.get("INFLUX_ORG") or "datagates"
        self.token = token or os.environ.get("INFLUX_TOKEN") or ""
        self.timeout = timeout
        self.orion_api_key = orion_api_key or os.environ.get("ORION_API_KEY", "")

    def _orion_headers(self) -> dict[str, str]:
        h = {"Accept": "application/ld+json"}
        if self.orion_api_key:
            h["X-API-Key"] = self.orion_api_key
        return h

    # ── Helpers for NGSI-LD's normalised shape ───────────────────────

    @staticmethod
    def value(entity: dict, attr: str, default=None):
        """Unwrap `{"type": "Property", "value": X}` down to X.

        NGSI-LD never returns bare values, so every attribute read goes
        through something like this. Relationships are unwrapped too
        (their payload is under `object` rather than `value`).
        """
        a = entity.get(attr)
        if not isinstance(a, dict):
            return default
        if "value" in a:
            return a["value"]
        if "object" in a:          # Relationship
            return a["object"]
        return default

    # ── Orion ────────────────────────────────────────────────────────

    def entities(self, entity_type: str, q: str = "", attrs: str = "",
                 limit: int = 1000, offset: int = 0) -> list[dict]:
        """GET /ngsi-ld/v1/entities.

        `q` is written in plain NGSI-LD syntax — quotes and semicolons
        are URL-encoded for you, which is the single most common source
        of "my filter returns nothing".
        """
        params = {"type": entity_type, "limit": limit}
        if q:
            params["q"] = q
        if attrs:
            params["attrs"] = attrs
        if offset:
            params["offset"] = offset
        url = f"{self.orion}/ngsi-ld/v1/entities?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers=self._orion_headers())
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read() or b"[]")
        except urllib.error.HTTPError as exc:
            raise DataGatesError(
                f"Orion {exc.code} for type={entity_type} q={q!r}: "
                f"{exc.read()[:200].decode(errors='replace')}") from exc

    def entity(self, urn: str) -> dict | None:
        """GET one entity by URN. None when it does not exist."""
        url = f"{self.orion}/ngsi-ld/v1/entities/{urllib.parse.quote(urn, safe=':')}"
        req = urllib.request.Request(url, headers=self._orion_headers())
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read() or b"null")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise DataGatesError(f"Orion {exc.code} for {urn}") from exc

    def count(self, entity_type: str, q: str = "") -> int:
        """Number of matching entities, without downloading them."""
        params = {"type": entity_type, "limit": 1, "count": "true"}
        if q:
            params["q"] = q
        url = f"{self.orion}/ngsi-ld/v1/entities?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers=self._orion_headers())
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            r.read()
            return int(r.headers.get("NGSILD-Results-Count") or 0)

    # ── Convenience readers ──────────────────────────────────────────

    def buildings(self, **kw) -> list[dict]:
        return self.entities("Building", **kw)

    def devices(self, building: str = "", source: str = "",
                provider: str = "", **kw) -> list[dict]:
        clauses = []
        if building:
            clauses.append(f'refBuilding=="{building}"')
        if source:
            clauses.append(f'source=="{source}"')
        if provider:
            clauses.append(f'dataProvider=="{provider}"')
        return self.entities("Device", q=";".join(clauses), **kw)

    def summaries(self, device: str = "", prop: str = "", source: str = "",
                  provider: str = "", with_data: bool = False, **kw) -> list[dict]:
        """Summary DeviceMeasurement entities — one per (device, property).

        with_data=True restricts to summaries that have actually reported
        (i.e. that carry a `lastReadingAt`). Note that 102 of the
        platform's 556 summaries are registered but have never produced
        data (README §11.1), so this flag changes results a lot. It says
        nothing about freshness: a decommissioned sensor keeps its last
        snapshot. Use reading_age_hours() for that.
        """
        clauses = ['entityKind=="summary"']
        if device:
            clauses.append(f'refDevice=="{device}"')
        if prop:
            clauses.append(f'controlledProperty=="{prop}"')
        if source:
            clauses.append(f'source=="{source}"')
        if provider:
            clauses.append(f'dataProvider=="{provider}"')
        if with_data:
            clauses.append("lastReadingAt")
        return self.entities("DeviceMeasurement", q=";".join(clauses), **kw)

    def summary_for(self, device: str, prop: str) -> dict:
        """The one summary for a (device, property). Raises if absent."""
        found = self.summaries(device=device, prop=prop, limit=1)
        if not found:
            raise DataGatesError(
                f"no summary for device={device} property={prop!r} — "
                f"use .summaries(device=...) to see what it measures")
        return found[0]

    # ── InfluxDB ─────────────────────────────────────────────────────

    def flux(self, script: str) -> list[dict[str, str]]:
        """Run a Flux query; return rows as dicts keyed by column NAME.

        Reading by name matters: Flux annotated-CSV column ORDER is not
        stable, because keep()/group()/sort() rearrange it. Positional
        indexing silently returns the wrong column.
        """
        if not self.token:
            raise DataGatesError(
                "INFLUX_TOKEN is not set — time-series calls need a read token")
        url = f"{self.influx}/api/v2/query?" + urllib.parse.urlencode({"org": self.org})
        req = urllib.request.Request(
            url,
            data=script.encode("utf-8"),
            headers={"Authorization": f"Token {self.token}",
                     "Content-Type": "application/vnd.flux"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout * 2) as r:
                text = r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raise DataGatesError(
                f"InfluxDB {exc.code}: "
                f"{exc.read()[:300].decode(errors='replace')}") from exc

        rows: list[dict[str, str]] = []
        header: list[str] = []
        for raw in text.splitlines():
            line = raw.rstrip("\r")
            if not line or line.startswith("#"):
                continue
            cells = line.split(",")
            if len(cells) > 1 and cells[1] == "result":
                header = cells                      # schema for rows below
            elif line.startswith(",_result") and header:
                rows.append({k: v for k, v in zip(header, cells, strict=False) if k})
        return rows

    def history(self, device: str, prop: str, start: str = "-7d",
                stop: str | None = None, every: str | None = None,
                agg: str = "mean") -> list[dict]:
        """Observations for one (device, property) as [{time, value}, ...].

        start/stop are Flux time expressions ("-7d", "now()", an RFC3339
        instant). Omitting `stop` means now() — which EXCLUDES weather
        forecasts, whose timestamps are in the future. Pass stop="2d"
        when you want those.

        `every` downsamples, e.g. every="1h", agg="mean".
        """
        summary = self.summary_for(device, prop)
        return self.history_of(summary, start=start, stop=stop,
                               every=every, agg=agg)

    def history_of(self, summary: dict, start: str = "-7d",
                   stop: str | None = None, every: str | None = None,
                   agg: str = "mean") -> list[dict]:
        """Same as history() when you already hold the summary entity.

        This is the bridge: the summary's own `id` is the InfluxDB
        `_measurement` name, and `influxBucket` names the bucket.
        """
        urn = summary["id"]
        bucket = self.value(summary, "influxBucket")
        if not bucket:
            raise DataGatesError(f"{urn} has no influxBucket")

        rng = f"start: {start}" + (f", stop: {stop}" if stop else "")
        script = (
            f'from(bucket: "{bucket}")\n'
            f'  |> range({rng})\n'
            f'  |> filter(fn: (r) => r._measurement == "{urn}")\n'
            f'  |> filter(fn: (r) => r._field == "value")\n'
        )
        if every:
            # timeSrc: "_start" labels each bucket with its START.
            #
            # Flux's default is "_stop", which has two annoyances when
            # you are building a report: a daily bucket covering the 6th
            # is labelled the 7th, and the final partial bucket is
            # labelled `now()` — a timestamp that differs by
            # microseconds between series, so rows that belong on one
            # line of a table scatter across several.
            script += (f'  |> aggregateWindow(every: {every}, fn: {agg}, '
                       f'createEmpty: false, timeSrc: "_start")\n')
        script += '  |> keep(columns: ["_time", "_value"])\n'

        out = []
        for r in self.flux(script):
            try:
                out.append({"time": r["_time"], "value": float(r["_value"])})
            except (KeyError, ValueError):
                continue
        return out

    def consumption(self, device: str, prop: str, start: str = "-7d",
                    every: str = "1h") -> list[dict]:
        """Per-period amounts, correct for both cumulative and delta props.

        For a cumulative counter (energy, gasIndex, ...) this takes the
        counter value at each window edge and differences them. For a
        delta property it simply sums within the window. Using the wrong
        one produces a plausible-looking wrong number, which is why this
        wrapper exists.
        """
        summary = self.summary_for(device, prop)
        urn = summary["id"]
        bucket = self.value(summary, "influxBucket")

        if prop in CUMULATIVE_PROPERTIES:
            # Counter value at each window edge, then difference the edges.
            # `last` before `difference`, never sum — see the module docstring
            # and ../README.md §7.5.
            tail = (f'  |> aggregateWindow(every: {every}, fn: last, '
                    f'createEmpty: false, timeSrc: "_start")\n'
                    f'  |> difference(nonNegative: true)\n')
        else:
            tail = (f'  |> aggregateWindow(every: {every}, fn: sum, '
                    f'createEmpty: false, timeSrc: "_start")\n')

        script = (
            f'from(bucket: "{bucket}")\n'
            f'  |> range(start: {start})\n'
            f'  |> filter(fn: (r) => r._measurement == "{urn}")\n'
            f'  |> filter(fn: (r) => r._field == "value")\n'
            f'{tail}'
            f'  |> keep(columns: ["_time", "_value"])\n'
        )
        out = []
        for r in self.flux(script):
            try:
                out.append({"time": r["_time"], "value": float(r["_value"])})
            except (KeyError, ValueError):
                continue
        return out

    # ── Batched reads: helpers ───────────────────────────────────────

    def _one_bucket(self, summaries: list[dict]) -> str:
        """The bucket shared by a batch. Raises if they disagree."""
        buckets = {self.value(s, "influxBucket") for s in summaries}
        if None in buckets or "" in buckets:
            missing = next(s["id"] for s in summaries
                           if not self.value(s, "influxBucket"))
            raise DataGatesError(f"{missing} has no influxBucket")
        if len(buckets) > 1:
            raise DataGatesError(
                "a batch must share one bucket, got "
                f"{sorted(buckets)} — group by influxBucket first")
        return buckets.pop()

    def _run_chunked(self, summaries: list[dict],
                     build: Callable[[str], str]) -> list[list[dict]]:
        """Run `build(predicate)` over chunks of summaries, in parallel.

        `build` receives an or-chained `_measurement` predicate and returns
        a complete Flux script. Results come back in chunk order.
        """
        chunks = [summaries[i:i + MAX_SERIES_PER_QUERY]
                  for i in range(0, len(summaries), MAX_SERIES_PER_QUERY)]
        scripts = [build(" or ".join(f'r._measurement == "{s["id"]}"' for s in c))
                   for c in chunks]
        if len(scripts) == 1:
            return [self.flux(scripts[0])]
        with ThreadPoolExecutor(max_workers=min(len(scripts), 6)) as pool:
            return list(pool.map(self.flux, scripts))

    def series_many(self, summaries: list[dict], start: str = "-7d",
                    stop: str | None = None, every: str = "1h",
                    agg: str = "mean",
                    difference: bool = False) -> dict[str, list[dict]]:
        """Windowed history for MANY series in one query.

        Returns {measurement_urn: [{time, value}, ...]}.

        The batched form of `history_of()` / `consumption()`. Set
        `difference=True` with `agg="last"` for cumulative counters — the
        `group(columns: ["_measurement"])` keeps each counter's
        differencing to its own series, so counters from different meters
        never subtract from one another.

        `aggregateWindow` is a pushdown aggregate, so asking for one
        hourly series or ninety costs about the same round trip. Looping
        `history_of()` over a fleet does not — that is one HTTP request
        and one full raw transfer per meter.

        Large fleets are split into chunks of MAX_SERIES_PER_QUERY and the
        chunks run in parallel; see that constant for why.

        All summaries must share a bucket; group by `influxBucket` first.
        """
        if not summaries:
            return {}
        bucket = self._one_bucket(summaries)
        rng = f"start: {start}" + (f", stop: {stop}" if stop else "")

        def build(pred: str) -> str:
            script = (
                f'from(bucket: "{bucket}")\n'
                f'  |> range({rng})\n'
                f'  |> filter(fn: (r) => {pred})\n'
                f'  |> filter(fn: (r) => r._field == "value")\n'
                f'  |> group(columns: ["_measurement"])\n'
                # timeSrc: "_start" — see history_of() for why the default
                # "_stop" scatters rows that belong on one line of a table.
                f'  |> aggregateWindow(every: {every}, fn: {agg}, '
                f'createEmpty: false, timeSrc: "_start")\n'
            )
            if difference:
                script += '  |> difference(nonNegative: true)\n'
            return script + '  |> keep(columns: ["_time", "_value", "_measurement"])\n'

        out: dict[str, list[dict]] = {}
        for rows in self._run_chunked(summaries, build):
            for r in rows:
                urn = r.get("_measurement")
                if not urn:
                    continue
                try:
                    out.setdefault(urn, []).append(
                        {"time": r["_time"], "value": float(r["_value"])})
                except (KeyError, ValueError):
                    continue
        return out

    def stats_many(self, summaries: list[dict], start: str = "-3d",
                   stop: str | None = None,
                   threshold: float | None = None) -> dict[str, dict]:
        """Summary statistics for MANY series, without moving the raw points.

        Returns {measurement_urn: {n, mean, min, max, over}}, where `over`
        counts readings strictly above `threshold` (0 when not given).
        Every figure is exact — these are full-precision aggregates over
        every point in the window, not a sample and not a re-average of
        pre-bucketed means.

        Why this exists: computing a fleet mean by pulling every raw point
        and averaging client-side moves megabytes to save nothing. Three
        days of 95 CO2 sensors is ~33 MiB of CSV; the same numbers come
        back in ~73 KiB. Measured figures are in section 12.5 of the guide.

        Implementation note worth knowing if you write your own: this sends
        five small `mean()/max()/min()/count()` queries in parallel rather
        than one `reduce()`. Both are correct, but the simple aggregates are
        pushed down into the storage engine while `reduce()` runs row by row
        in the Flux VM — measured 816 ms against 2421 ms for the same 95
        series. Prefer a pushdown aggregate over a clever one-pass reduce.

        Large fleets are split into chunks of MAX_SERIES_PER_QUERY.

        All summaries in one call must share a bucket; group them by
        `influxBucket` first if that is not guaranteed.
        """
        if not summaries:
            return {}
        bucket = self._one_bucket(summaries)
        rng = f"start: {start}" + (f", stop: {stop}" if stop else "")

        tails = {"mean": "  |> mean()\n",
                 "max": "  |> max()\n",
                 "min": "  |> min()\n",
                 "n": "  |> count()\n"}
        if threshold is not None:
            tails["over"] = (f"  |> filter(fn: (r) => r._value > {float(threshold)})\n"
                             "  |> count()\n")

        def run(key: str) -> tuple[str, list[list[dict]]]:
            def build(pred: str) -> str:
                return (f'from(bucket: "{bucket}")\n'
                        f'  |> range({rng})\n'
                        f'  |> filter(fn: (r) => {pred})\n'
                        f'  |> filter(fn: (r) => r._field == "value")\n'
                        f'  |> group(columns: ["_measurement"])\n'
                        + tails[key])
            return key, self._run_chunked(summaries, build)

        with ThreadPoolExecutor(max_workers=len(tails)) as pool:
            answers = dict(pool.map(run, list(tails)))

        out: dict[str, dict] = {}
        for key, chunked in answers.items():
            for rows in chunked:
                for r in rows:
                    urn = r.get("_measurement")
                    if not urn:
                        continue
                    try:
                        out.setdefault(urn, {})[key] = float(r["_value"])
                    except (KeyError, TypeError, ValueError):
                        continue

        # A series with no exceedances drops out of the `over` query
        # entirely, so default it rather than treating it as missing.
        result = {}
        for urn, got in out.items():
            n = int(got.get("n", 0))
            if n <= 0 or "mean" not in got:
                continue
            result[urn] = {"n": n, "mean": got["mean"],
                           "min": got.get("min", got["mean"]),
                           "max": got.get("max", got["mean"]),
                           "over": int(got.get("over", 0))}
        return result

    # ── Freshness ────────────────────────────────────────────────────

    @staticmethod
    def reading_age_hours(summary: dict) -> float | None:
        """Hours since this summary last reported. None if it never has.

        Negative for forecast data, whose readings are timestamped ahead
        of now. Timestamp formats vary across pipelines (`Z`, `+00:00`,
        and a `+03:00` local offset from a batch-fed gas source), so parse rather than
        compare the strings.
        """
        ts = Client.value(summary, "lastReadingAt")
        if not ts:
            return None
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return (datetime.now(UTC) - dt).total_seconds() / 3600.0
