"""
plugins/datagates/core/influx_writer.py

Thin wrapper around the official `influxdb-client` (v2) with the data-gates
platform's Influx schema baked in:

    * one bucket per gate, or one shared bucket (INFLUX_DEFAULT_BUCKET)
    * one `_measurement` per (device, controlledProperty) pair — the
      measurement name equals the summary DeviceMeasurement URN, so a
      consumer who has the summary entity in hand can query Influx by
      dropping in its `id` verbatim.
    * NO tags on real observations — every attribute a consumer might
      want (unit, source, dataProvider, refDevice, etc.) lives on the
      summary DeviceMeasurement entity in Orion. Influx stores the
      numeric time-series only.
    * `_field == "value"` on real observations, `_field == "init"` on
      init sentinels. That single-attribute discriminator lets
      consumers ignore sentinels with `filter(fn: (r) => r._field == "value")`
      and lets init re-seeds coexist with real data trivially.

Canonical consumer query:

        from(bucket:"telemetry")
          |> range(start: -30d)
          |> filter(fn: (r) => r._measurement == "<summary URN>")
          |> filter(fn: (r) => r._field == "value")

Usage (from a DAG):

    from datagates.core.influx_writer import InfluxWriter
    from datagates.core.settings import INFLUX_DEFAULT_BUCKET

    with InfluxWriter(bucket=INFLUX_DEFAULT_BUCKET) as w:
        w.write_many([
            {"device_urn": dev_urn, "controlled_property": "gasConsumption",
             "value": 12.3, "observed_at": "2026-07-16T10:00:00Z"},
            ...
        ])
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from datagates.core.measurement_summary import summary_measurement_urn
from datagates.core.settings import (
    INFLUX_ORG,
    INFLUX_RETENTION_HOURS,
    INFLUX_TOKEN,
    INFLUX_URL,
)

logger = logging.getLogger(__name__)


class InfluxWriter:
    """
    One instance per DAG task run. Not thread-safe (the underlying
    influxdb-client SYNCHRONOUS write_api is called serially).
    """

    def __init__(self, bucket: str):
        self.bucket = bucket
        self._client = None
        self._write_api = None
        self._buckets_api = None
        self._enabled = bool(INFLUX_TOKEN)

        if not self._enabled:
            logger.warning(
                "InfluxWriter: INFLUX_TOKEN unset — all writes to bucket %r "
                "will no-op (returns 0 from write_many). Set INFLUX_TOKEN in "
                "the server .env to enable persistence.",
                bucket,
            )
            return

        # Import lazily so import-time failures don't break DAG parsing.
        from influxdb_client import InfluxDBClient
        from influxdb_client.client.write_api import SYNCHRONOUS

        self._client = InfluxDBClient(
            url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG,
            timeout=30_000,  # ms
        )
        self._write_api = self._client.write_api(write_options=SYNCHRONOUS)
        self._buckets_api = self._client.buckets_api()
        self._ensure_bucket()

    # ── Bucket ────────────────────────────────────────────────────────

    def _ensure_bucket(self) -> None:
        """Convenience — ensure `self.bucket` exists. See `ensure_bucket()`."""
        self.ensure_bucket(self.bucket)

    def ensure_bucket(self, bucket_name: str) -> bool:
        """
        Create the named bucket with the configured retention if it
        doesn't already exist. Idempotent. Returns True if the bucket
        exists (whether pre-existing or just created), False on error.

        Callable from init DAGs so all pilot buckets are ready before
        any run DAG tries to write.
        """
        if not self._enabled:
            return False
        try:
            from influxdb_client import BucketRetentionRules

            existing = self._buckets_api.find_bucket_by_name(bucket_name)
            if existing is not None:
                return True
            self._buckets_api.create_bucket(
                bucket_name=bucket_name,
                org=INFLUX_ORG,
                retention_rules=[BucketRetentionRules(
                    type="expire",
                    every_seconds=INFLUX_RETENTION_HOURS * 3600,
                )],
            )
            logger.info(
                "InfluxWriter: created bucket %r with %d h retention",
                bucket_name, INFLUX_RETENTION_HOURS,
            )
            return True
        except Exception as exc:
            logger.warning(
                "InfluxWriter: could not ensure bucket %r (writes will "
                "still be attempted): %s", bucket_name, exc,
            )
            return False

    # ── Measurement seeding (called from init DAGs) ──────────────────

    def ensure_measurement(
        self,
        device_urn: str,
        controlled_properties: list[str],
    ) -> int:
        """
        InfluxDB v2 has no `CREATE MEASUREMENT` API — measurements
        materialize implicitly on the first write. To make each
        (device, controlledProperty) measurement discoverable in the
        UI / Data Explorer *before* real telemetry flows, this method
        writes one sentinel point per property.

        Sentinels are trivially distinguished from real samples by
        living in a separate field (`init` vs. `value`), which means
        the canonical consumer filter — `_field == "value"` — already
        excludes them without any extra clause.

        Idempotent — re-running init overwrites the previous sentinel
        at the new timestamp (single sentinel per measurement).

        Returns the number of sentinel points actually written.
        """
        if not self._enabled or not controlled_properties:
            return 0

        from influxdb_client import Point

        now = datetime.now(UTC)

        batch: list[Point] = []
        for prop in controlled_properties:
            m = self.measurement_for(device_urn, prop)
            batch.append(
                Point(m)
                .field("init", 0.0)
                .time(now)
            )

        try:
            self._write_api.write(
                bucket=self.bucket, org=INFLUX_ORG, record=batch,
            )
            logger.info(
                "InfluxWriter: seeded %d sentinel(s) into %s for device %s",
                len(batch), self.bucket, device_urn,
            )
            return len(batch)
        except Exception as exc:
            logger.warning(
                "InfluxWriter: seeding sentinels for device %s in %s failed: %s",
                device_urn, self.bucket, exc,
            )
            return 0

    # ── Convention: measurement name = summary DeviceMeasurement URN ──

    @staticmethod
    def measurement_for(device_urn: str, controlled_property: str) -> str:
        """
        Convention: the InfluxDB `_measurement` value equals the URN of
        the summary `DeviceMeasurement` entity for this (device, prop).

        Consumers who have the summary entity in hand can query Influx
        by using its `id` verbatim as the measurement name — no lookup
        table, no tag joins.
        """
        return summary_measurement_urn(device_urn, controlled_property)

    # ── Time parsing ─────────────────────────────────────────────────

    @staticmethod
    def _parse_time(iso: str) -> datetime:
        if iso.endswith("Z"):
            iso = iso[:-1] + "+00:00"
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)

    # ── Writes ────────────────────────────────────────────────────────

    def write_many(self, points: list[dict[str, Any]]) -> int:
        """
        Write a batch of samples in one HTTP call. Returns the number of
        points accepted by the server (0 if the writer is disabled or the
        write failed).

        Each `points` entry MUST contain:
            device_urn:            str
            controlled_property:   str
            value:                 float → field `value`
            observed_at:           str  (ISO 8601)

        Any other keys (unit_code, tags, source, …) are ignored: unit
        and provenance metadata live on the summary DeviceMeasurement
        entity in Orion, never in Influx.
        """
        if not self._enabled or not points:
            return 0

        from influxdb_client import Point

        batch: list[Point] = []
        for d in points:
            try:
                p = (
                    Point(self.measurement_for(d["device_urn"], d["controlled_property"]))
                    .field("value", float(d["value"]))
                    .time(self._parse_time(d["observed_at"]))
                )
                batch.append(p)
            except (KeyError, ValueError, TypeError) as exc:
                logger.warning(
                    "InfluxWriter: dropping malformed sample %r: %s", d, exc,
                )
                continue

        if not batch:
            return 0

        try:
            self._write_api.write(bucket=self.bucket, org=INFLUX_ORG, record=batch)
            return len(batch)
        except Exception as exc:
            logger.warning(
                "InfluxWriter: batch of %d point(s) to bucket %r failed: %s",
                len(batch), self.bucket, exc,
            )
            return 0

    # ── Rolling aggregates (for the DeviceMeasurement summary entities) ─

    def rolling_stats(
        self, device_urn: str, controlled_property: str,
        hours: int = 24,
    ) -> dict[str, Any] | None:
        """
        Compute min / max / mean / count over the last `hours` for one
        (device, controlledProperty) in this bucket. Sentinels live in
        field `init`, so filtering `_field == "value"` excludes them
        without an extra clause.

        Uses Flux `reduce()` to do all four aggregates in one query, one
        pass over the data. Returns:

            {
              "min":          float | None,
              "max":          float | None,
              "mean":         float | None,
              "count":        int,
              "window_start": ISO 8601,
              "window_end":   ISO 8601,
            }

        Returns None when the writer is disabled or the query errors —
        caller falls back to whatever data it has in hand.
        """
        if not self._enabled:
            return None
        from datetime import timedelta

        window_end   = datetime.now(UTC).replace(microsecond=0)
        window_start = (window_end - timedelta(hours=hours)).replace(microsecond=0)

        # Escape " in the measurement name for the Flux string literal —
        # cheap defensive measure; NGSI URNs shouldn't contain quotes.
        m = self.measurement_for(device_urn, controlled_property).replace('"', r'\"')
        flux = (
            f'from(bucket:"{self.bucket}")\n'
            f'  |> range(start: -{int(hours)}h)\n'
            f'  |> filter(fn: (r) => r._measurement == "{m}")\n'
            f'  |> filter(fn: (r) => r._field == "value")\n'
            # Sentinel bounds for min/max init — Flux 2.7 rejects `1.0e18`
            # scientific notation ("undefined identifier e18"); use plain
            # decimals that comfortably exceed any physical sensor value.
            '  |> reduce(\n'
            '       identity: {mn: 1000000000.0, mx: -1000000000.0, sum: 0.0, n: 0},\n'
            '       fn: (r, accumulator) => ({\n'
            '         mn: if r._value < accumulator.mn then r._value else accumulator.mn,\n'
            '         mx: if r._value > accumulator.mx then r._value else accumulator.mx,\n'
            '         sum: accumulator.sum + r._value,\n'
            '         n:   accumulator.n   + 1,\n'
            '       })\n'
            '     )\n'
        )
        try:
            from influxdb_client.client.query_api import QueryApi
            qa: QueryApi = self._client.query_api()
            tables = qa.query(query=flux, org=INFLUX_ORG)
        except Exception as exc:
            logger.warning(
                "InfluxWriter.rolling_stats: query failed for %s/%s: %s",
                device_urn, controlled_property, exc,
            )
            return None

        # `reduce` yields one row per group. Our filter chain has no
        # `group()` after the initial series split — so we may get 0, 1
        # (single series) or > 1 rows (multiple series folded). Take the
        # aggregate across whatever came back.
        mn: float | None = None
        mx: float | None = None
        s = 0.0
        n = 0
        for table in tables:
            for rec in table.records:
                v = rec.values
                if v.get("n", 0) <= 0:
                    continue
                cur_mn = v.get("mn")
                cur_mx = v.get("mx")
                if cur_mn is not None:
                    mn = cur_mn if mn is None else min(mn, cur_mn)
                if cur_mx is not None:
                    mx = cur_mx if mx is None else max(mx, cur_mx)
                s += float(v.get("sum") or 0.0)
                n += int(v.get("n") or 0)

        mean: float | None = (s / n) if n else None
        return {
            "min":          mn,
            "max":          mx,
            "mean":         mean,
            "count":        n,
            "window_start": window_start.isoformat().replace("+00:00", "Z"),
            "window_end":   window_end.isoformat().replace("+00:00", "Z"),
        }

    # ── Raw point reads (for DQ + reporting consumers) ──────────────

    def query_range(
        self, device_urn: str, controlled_property: str,
        start_iso: str, stop_iso: str,
    ) -> list[dict[str, Any]]:
        """
        Return every real observation for one (device, controlledProperty)
        in [start_iso, stop_iso), sorted ascending by time. Sentinels
        (field == "init") are excluded — we filter `_field == "value"`.

        Each element:  {"time": ISO 8601 UTC, "value": float}

        Empty list when no points landed (offline sensor, empty window,
        or the query errored — check the log). This is the read-only
        counterpart to write_many() and is used by dq_weekly_report and
        any consumer that needs raw history.
        """
        if not self._enabled:
            return []

        m = self.measurement_for(device_urn, controlled_property).replace('"', r'\"')
        flux = (
            f'from(bucket:"{self.bucket}")\n'
            f'  |> range(start: {start_iso}, stop: {stop_iso})\n'
            f'  |> filter(fn: (r) => r._measurement == "{m}")\n'
            f'  |> filter(fn: (r) => r._field == "value")\n'
            f'  |> keep(columns: ["_time", "_value"])\n'
            f'  |> sort(columns: ["_time"], desc: false)\n'
        )
        try:
            from influxdb_client.client.query_api import QueryApi
            qa: QueryApi = self._client.query_api()
            tables = qa.query(query=flux, org=INFLUX_ORG)
        except Exception as exc:
            logger.warning(
                "InfluxWriter.query_range: query failed for %s/%s %s..%s: %s",
                device_urn, controlled_property, start_iso, stop_iso, exc,
            )
            return []

        out: list[dict[str, Any]] = []
        for table in tables:
            for rec in table.records:
                t = rec.get_time()
                v = rec.get_value()
                if t is None or v is None:
                    continue
                # Emit as ISO 8601 UTC 'Z' — matches how observedAt is
                # written into Orion so downstream comparisons align.
                iso = t.astimezone(UTC).isoformat().replace("+00:00", "Z")
                try:
                    out.append({"time": iso, "value": float(v)})
                except (TypeError, ValueError):
                    continue
        return out

    # ── Lifecycle ────────────────────────────────────────────────────

    def close(self) -> None:
        if self._write_api is not None:
            try:
                self._write_api.close()
            except Exception:
                pass
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass

    def __enter__(self) -> InfluxWriter:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


# ── Convenience: derive `influxBucket` for the Orion Device entity ────
#
# A Device has N summary DeviceMeasurement children, each with its own
# `_measurement` name — so the Device itself has no single measurement
# to advertise. It does have a single bucket (per pilot), which is a
# useful nav hint for tooling that starts from a Device.

def influx_pointer_attrs(bucket: str) -> dict[str, dict]:
    """
    Return the NGSI-LD Property attribute pack to attach to a Device
    entity for time-series findability. Merge into the entity dict at
    registration time:

        entity = {"id": device_urn, "type": "Device", ...}
        entity.update(influx_pointer_attrs("telemetry"))
    """
    return {
        "influxBucket": {"type": "Property", "value": bucket},
    }
