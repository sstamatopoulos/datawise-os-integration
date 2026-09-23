"""
plugins/datagates/core/measurement_summary.py

Per-(device, controlledProperty) summary DeviceMeasurement entities.

Background — why not one entity per observation
------------------------------------------------
An earlier version of this platform used to write one NGSI-LD `DeviceMeasurement`
entity per (device, property, timestamp) — resulting in millions of
entities per day in Orion. That model is bad for a graph-store backend:
count queries time out, Mongo indexes grow unbounded, entity listings
become paginated slogs.

New model:
  * Orion holds ONE `DeviceMeasurement` entity per (device, controlled
    property) pair — a lightweight pointer + latest snapshot + rolling
    stats. Updated in place, never appended.
  * The full time-series lives in InfluxDB v2 (one measurement per
    Device URN, tags include controlledProperty). The summary entity
    tells consumers where to look.

Entity shape
------------
    id:                   urn:ngsi-ld:DeviceMeasurement:<uuid5>   # (see summary_measurement_urn)
    type:                 "DeviceMeasurement"
    entityKind:           "summary"                                # discriminator for queries
    refDevice:            Relationship -> Device URN
    controlledProperty:   scalar string (e.g. "co2", "energy")
    unitCode:             UN/CEFACT unit code
    influxBucket:         the gate's bucket, e.g. "telemetry"
    influxMeasurement:    the URN of THIS entity (the Influx `_measurement`
                          name). Consumers can drop it verbatim into a
                          Flux `filter(fn: (r) => r._measurement == "…")`.
    dataProvider:         the gate's data_provider, e.g. "open-meteo"
    source:               canonical source id (e.g. "co2.mesh.example")
    # Refreshed on every run DAG:
    lastReadingAt:        ISO 8601 UTC
    lastReadingValue:     numeric
    rolling24hMin:        numeric | null
    rolling24hMax:        numeric | null
    rolling24hMean:       numeric | null
    rolling24hCount:      int
    rolling24hWindowStart: ISO 8601 UTC
    rolling24hWindowEnd:   ISO 8601 UTC

Usage
-----
    # In init DAGs (upsert ONCE per property):
    from datagates.core.measurement_summary import summary_entity
    orion.upsert_entity(summary_entity(...))

    # In run DAGs (PATCH after Influx write):
    from datagates.core.measurement_summary import (
        summary_measurement_urn, summary_patch_body,
    )
    body = summary_patch_body(last_at, last_val, stats_24h)
    orion.patch_entity(summary_measurement_urn(dev_urn, prop), body)
"""
from __future__ import annotations

import uuid as _uuid
from typing import Any

from datagates.core.settings import DEFAULT_CONTEXT

# Shared with per-observation URNs (namespace is arbitrary but fixed).
# The "summary|" prefix in the input key guarantees this URN is distinct
# from the observation URN scheme (which is "device_urn|prop|ts_ms").
_MEASUREMENT_NS = _uuid.UUID("9c3b0e7e-7a1e-4d9b-9e1a-2b6c4f1f0d2c")


def summary_measurement_urn(device_urn: str, controlled_property: str) -> str:
    """Deterministic URN for the summary entity of one (device, prop)."""
    key = f"summary|{device_urn}|{controlled_property}"
    return f"urn:ngsi-ld:DeviceMeasurement:{_uuid.uuid5(_MEASUREMENT_NS, key)}"


def summary_entity(
    device_urn: str,
    controlled_property: str,
    unit_code: str,
    influx_bucket: str,
    data_provider: str,
    source: str,
) -> dict[str, Any]:
    """
    Build the initial (empty-stats) entity for an init DAG to upsert.

    `influxMeasurement` = this entity's own URN, i.e. the Influx
    `_measurement` name. Consumers drop it into a Flux query verbatim.
    No `influxTagFilter` attribute exists — the Influx schema stores
    ONLY the numeric `value` field (no tags), so identity is fully
    captured by the measurement name alone.
    """
    urn = summary_measurement_urn(device_urn, controlled_property)
    # NOTE: NGSI-LD (Orion-LD 1.5.1) rejects Property values that are `null`.
    # We therefore OMIT the lastReading* / rolling24h* attributes at init
    # time — they're created on the first run DAG cycle via POST /attrs.
    return {
        "id":                 urn,
        "type":               "DeviceMeasurement",
        "entityKind":         {"type": "Property", "value": "summary"},
        "refDevice":          {"type": "Relationship", "object": device_urn},
        "controlledProperty": {"type": "Property", "value": controlled_property},
        "unitCode":           {"type": "Property", "value": unit_code},
        "influxBucket":       {"type": "Property", "value": influx_bucket},
        "influxMeasurement":  {"type": "Property", "value": urn},
        "dataProvider":       {"type": "Property", "value": data_provider},
        "source":             {"type": "Property", "value": source},
        "@context":           DEFAULT_CONTEXT,
    }


def summary_patch_body(
    last_reading_at: str | None,
    last_reading_value: float | None,
    stats_24h: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Build the NGSI-LD `POST /entities/<id>/attrs` body that refreshes a
    summary entity in place. Only includes attributes that have a value
    (so partial updates are cheap).

    stats_24h is the dict returned by InfluxWriter.rolling_stats() —
    may be None if the Flux query failed; then only the last-reading
    attrs are updated.
    """
    body: dict[str, Any] = {"@context": DEFAULT_CONTEXT}

    if last_reading_at is not None:
        body["lastReadingAt"]    = {"type": "Property", "value": last_reading_at}
    if last_reading_value is not None:
        body["lastReadingValue"] = {"type": "Property", "value": last_reading_value}

    if stats_24h:
        for src, dst in (
            ("min",           "rolling24hMin"),
            ("max",           "rolling24hMax"),
            ("mean",          "rolling24hMean"),
            ("count",         "rolling24hCount"),
            ("window_start",  "rolling24hWindowStart"),
            ("window_end",    "rolling24hWindowEnd"),
        ):
            if src in stats_24h and stats_24h[src] is not None:
                body[dst] = {"type": "Property", "value": stats_24h[src]}
    return body


def refresh_summaries(writer, orion, samples: list[dict[str, Any]],
                      *, only_if_newer: bool = True) -> tuple[int, int, int]:
    """Refresh the summary entity of every (device, property) in `samples`.

    Groups the samples, takes the newest reading per group, asks the
    InfluxWriter for the rolling 24 h statistics, and PATCHes the summary
    DeviceMeasurement in Orion. Returns (patched, skipped, failed).

    With `only_if_newer` (the default) a summary that already holds a
    newer reading is left alone — see OrionRegistry.patch_summary_if_newer.
    That makes this safe to call from a backfill: for a live device the
    run DAG's snapshot is newer and nothing happens; for a device whose
    only data ever arrives by backfill, this is what sets its snapshot at
    all. Fifty-one summaries sat with thousands of points and no
    lastReadingAt because no backfill did this.

    `samples` are the dicts handed to InfluxWriter.write_many():
    device_urn, controlled_property, value, observed_at.
    """
    import logging

    log = logging.getLogger(__name__)
    latest: dict[tuple[str, str], tuple[str, float]] = {}
    for s in samples:
        try:
            key = (s["device_urn"], s["controlled_property"])
            at, val = str(s["observed_at"]), float(s["value"])
        except (KeyError, TypeError, ValueError):
            continue
        prev = latest.get(key)
        if prev is None or at > prev[0]:
            latest[key] = (at, val)

    patched = skipped = failed = 0
    for (dev, prop), (last_at, last_val) in latest.items():
        try:
            stats24 = writer.rolling_stats(dev, prop, hours=24)
        except Exception as exc:                          # noqa: BLE001
            log.warning("rolling_stats failed for %s / %s: %s", dev, prop, exc)
            stats24 = None
        body = summary_patch_body(last_at, last_val, stats24)
        urn = summary_measurement_urn(dev, prop)
        try:
            if only_if_newer:
                if orion.patch_summary_if_newer(urn, last_at, body):
                    patched += 1
                else:
                    skipped += 1
            else:
                orion.patch_attrs(urn, body)
                patched += 1
        except Exception as exc:                          # noqa: BLE001
            failed += 1
            log.warning("summary PATCH failed for %s / %s: %s", dev, prop, exc)
    if failed:
        log.warning("refresh_summaries: %d patched, %d skipped, %d FAILED",
                    patched, skipped, failed)
    return patched, skipped, failed
