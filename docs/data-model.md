# Data model

Two stores, each for what it is good at.

**Orion-LD** holds what a thing *is* and what it reads *now*: `Building`,
`Device` and one summary `DeviceMeasurement` per (Device, controlledProperty).
**InfluxDB** holds what it read *over time*. The bridge is the summary
entity's id: it is also the InfluxDB measurement name, so a consumer that
has the summary in hand can query history with no lookup table.

## Building

```json
{ "id": "urn:ngsi-ld:Building:…", "type": "Building",
  "name": {"type": "Property", "value": "Site A"},
  "dataProvider": {"type": "Property", "value": "…"},
  "source": {"type": "Property", "value": "…"} }
```

## Device

```json
{ "id": "urn:ngsi-ld:Device:…", "type": "Device",
  "name": {"type": "Property", "value": "Room 120B"},
  "category": {"type": "Property", "value": ["sensor"]},
  "controlledProperty": {"type": "Property", "value": ["co2", "temperature"]},
  "refBuilding": {"type": "Relationship", "object": "urn:ngsi-ld:Building:…"},
  "dataProvider": {"type": "Property", "value": "…"},
  "source": {"type": "Property", "value": "…"},
  "dataGate": {"type": "Property", "value": "indoor_air"},
  "influxBucket": {"type": "Property", "value": "telemetry"},
  "lastIngestedAt": {"type": "Property", "value": 1789580000000} }
```

`controlledProperty` is the contract: exactly one summary exists per name in
it. `dataGate` names the gate that owns the device. `lastIngestedAt` is the
ingestion watermark in epoch milliseconds, for the pipelines; consumers
should read a summary's `lastReadingAt` instead. Gate-specific attributes
(coordinates as the `location` GeoProperty, upstream ids, status such as
battery or signal) sit alongside.

## DeviceMeasurement (summary)

```json
{ "id": "urn:ngsi-ld:DeviceMeasurement:<uuid5>", "type": "DeviceMeasurement",
  "entityKind": {"type": "Property", "value": "summary"},
  "refDevice": {"type": "Relationship", "object": "urn:ngsi-ld:Device:…"},
  "controlledProperty": {"type": "Property", "value": "co2"},
  "unitCode": {"type": "Property", "value": "59"},
  "influxBucket": {"type": "Property", "value": "telemetry"},
  "influxMeasurement": {"type": "Property", "value": "urn:ngsi-ld:DeviceMeasurement:<uuid5>"},
  "lastReadingAt": {"type": "Property", "value": "2026-09-16T15:00:00Z"},
  "lastReadingValue": {"type": "Property", "value": 523},
  "rolling24hMin": …, "rolling24hMax": …, "rolling24hMean": …, "rolling24hCount": …,
  "dataProvider": …, "source": … }
```

The id is `uuid5(device URN, controlledProperty)`, so it is stable across
re-registration. Snapshot and rolling fields are absent until the first
ingest (Orion-LD rejects null values) and only ever move forward.

## InfluxDB

One bucket per gate (or shared), one measurement per summary URN, one field
`value`, no tags. Sentinel points written at registration use the field
`init`, so the canonical consumer filter `_field == "value"` excludes them.

```flux
from(bucket: "telemetry")
  |> range(start: -7d)
  |> filter(fn: (r) => r._measurement == "urn:ngsi-ld:DeviceMeasurement:…")
  |> filter(fn: (r) => r._field == "value")
```

Delta series (consumption per period) are stamped at the **start** of the
period they cover. Cumulative series (meter registers) must be differenced,
not summed; the summary's `unitCode` and the gate's `cumulative` declaration
tell them apart.

### Three kinds of series, and what a gap means

| Kind | Timestamp | A gap means |
|---|---|---|
| **reported** — the upstream has its own history (APIs, historians, files, brokers) | the upstream's own observation time | the upstream had no reading, or has not been asked yet; a backfill can still fill it |
| **snapshot** — a polled field protocol (Modbus, BACnet, S7, SNMP, OPC UA without a historian) | the moment the gate read the value, aligned to `align_s` (a minute by default) | that poll did not happen, and nothing can recover it: the value only ever existed in a register |
| **forecast** — a rolling feed (weather forecasts, day-ahead prices) | the instant the value is *about*, in the future | it has not been published yet; the next run overwrites what it supersedes |

A snapshot series therefore has the resolution of the gate's schedule, and its
gaps are the platform's own downtime rather than the upstream's. Consumers
that compute energy from a snapshot power series should integrate over the
actual timestamps rather than assume a fixed interval, and anyone judging data
quality should compare against the gate's cadence
(`core.cadence.expected_interval` computes it from the schedule) rather than
against a fixed threshold.

## Conventions consumers rely on

- Attribute names are short (core context only); `?type=Device&q=source=="x"`
  works without a Link header.
- Percentages that describe a fraction (relative humidity, probabilities)
  are stored as 0 to 1.
- Forecast data has timestamps in the future; set an explicit `stop:` in Flux.
- Orion-LD returns a one-element array as a scalar; normalise before indexing.
