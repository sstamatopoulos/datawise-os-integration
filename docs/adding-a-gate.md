# Adding a gate

Most sources need no code. Before writing a class, check that none of the
twenty-four built-in types fits ([gates.md](gates.md),
[legacy-systems.md](legacy-systems.md)): `http_json`, `http_xml` and
`http_csv` cover most web APIs including SOAP, the drop gates cover files
however they arrive, `sql` covers anything with a database, and the field
protocols cover the plant room. Write a class when the upstream needs a login
dance, a pagination scheme they cannot express, or a payload shape none of
them can describe.

## The contract

```python
# my_gates/scada.py
from datetime import datetime
from datagates.gates import DeviceSpec, Gate, Sample

class ScadaGate(Gate):
    type_name = "scada"
    speaks = "Example SCADA 4.x"           # one line for the catalogue
    default_schedule = "*/15 * * * *"
    default_history_start = "2025-01-01"   # earliest the upstream serves
    default_max_window_days = 7            # longest span per upstream request
    default_min_interval_s = 0.5           # if the upstream counts requests
    cumulative = frozenset({"energy"})     # running totals: guarded at ingestion
    device_attrs = ("scadaTag",)           # Device attributes fetch() needs back

    def discover(self) -> list[DeviceSpec]:
        return [DeviceSpec(
            urn=self.urn(tag),                          # deterministic, keyed on the gate + identity
            name=name,
            properties={"energy": "KWH", "power": "KWT"},   # controlledProperty -> UN/CEFACT unit
            ref_building=building_urn,
            attrs={"scadaTag": tag},
        ) for tag, name, building_urn in self.option("tags", [])]

    def fetch(self, device: dict, start: datetime, end: datetime) -> list[Sample]:
        rows = self._client().history(device["scadaTag"], start, end)   # your call
        return [Sample(device["urn"], prop, value, iso_utc) for prop, value, iso_utc in rows]
```

Register it in `config/gates.yaml` by import path:

```yaml
  - key: plant
    type: "my_gates.scada:ScadaGate"
    options:
      tags: [...]
```

Put the module anywhere on `PYTHONPATH` (`plugins/` is), or `pip install -e .`
this repository and develop your gate in its own package. To contribute a gate
type, add it to `BUILTIN_TYPES` in `plugins/datagates/gates/registry.py`, ship
a disabled example in `config/gates.yaml`, a section in `docs/gates.md` and a
test on a recorded payload — `tests/test_registry.py` enforces the first two.

## Use the shared machinery

Six modules exist so that gates stay short and behave alike. Reach for them
before writing the same code again; the first three save the most.

| Need | Use | What it gives you |
|---|---|---|
| Map upstream fields to properties | `core.fieldmap.FieldMap` | the `fields:` block, `scale`/`offset`, `invalid:` sentinels, `min`/`max`, the cumulative set, decimal commas, NaN handling |
| Talk HTTP | `core.httpclient.client_for(self)` | retries on transient failures only, `Retry-After`, `min_interval_s` throttling, the `auth:` block, session reuse, a clear error when JSON was expected and HTML arrived |
| Parse a timestamp | `core.timeparse.parse_stamp` | `iso`/`epoch_s`/`epoch_ms`/`strptime`, `timezone:` for naive stamps, `None` instead of an exception on a bad row |
| Read registers or byte blocks | `core.binary` | typed decoding, word order, bit extraction |
| Read rows out of XML | `core.xmlrows` | namespace-insensitive matching and the small selector language |
| Know how often a gate should run | `core.cadence.expected_interval` | a cron expression as a `timedelta`, for freshness checks |

And two base classes that are worth subclassing rather than imitating:

- **`gates.polling.PollingGate`** — for an upstream with no history (a
  register, an object, an OID). Implement `read(device, entry, fields)`
  returning raw values keyed by field source; you get `rolling`,
  `backfill_mode = "none"`, per-device field maps, timestamp alignment and
  the "device dropped from the config" case handled.
- **`gates.file_drop.FileDropGate`** — for files in a directory. Implement
  `rows(path)` yielding flat dicts; you get globbing, device matching,
  timestamp parsing, the window filter, stateless backfill and the promise
  that one unreadable file does not spoil the run. Add `prepare()` to fetch
  the files first, as `remote_drop` does.

## What the framework does for you

| Concern | Where |
|---|---|
| Buildings, Devices, summaries created and re-registered idempotently | `<key>_init` |
| Per-device watermark (`lastIngestedAt`), only new samples stored | `<key>_run` |
| Long spans split into `max_window` requests | `<key>_run`, `<key>_backfill` |
| Retries and request pacing (`max_attempts`, `min_interval_s`) | `core/httpclient.py` |
| Readings a cumulative counter cannot have produced rejected | `core/counter_guard.py` |
| Summary snapshot and rolling 24 h stats refreshed, never moved backwards | `core/measurement_summary.py` |
| History walked backwards with resumable state per Device | `<key>_backfill` |
| A device's status attributes (battery, signal, last seen) | override `status()` |

## Four shapes of upstream

- **Live series** (default): `fetch()` returns samples in `(start, end]`;
  the run asks from the watermark to now. Backfill walks back to
  `history_start` in windows, keeping a cursor on the Device.
- **Forecast-like** (`rolling = True`, `backfill_mode = "none"`): the run
  calls `fetch(device, now, now)` once and stores whatever the gate returns;
  every returned hour overwrites the previous forecast for it.
- **Revised history** (`rewrite_lookback = timedelta(days=7)`): the run
  re-fetches that much before the watermark on every run, so upstream
  revisions land. Use `backfill_mode = "stateless"` when one pass over
  `[history_start, now)` is cheap and idempotent.
- **No history at all**: subclass `PollingGate`. The schedule becomes the
  sampling rate, and there is nothing to backfill.

A gate may decide its shape from its options — `opcua` and `ngsi_v2` do it,
by overriding `rolling` and `backfill_mode` as properties.

## Three things that bite

- Orion-LD returns a one-element array as a scalar. `device["properties"]`
  and anything you read back from an entity is normalised with
  `core.entities.as_list`; do not index raw values.
- `location` is the reserved NGSI-LD GeoProperty. Use `DeviceSpec.location`
  for coordinates and another attribute name for a place description.
- Import a third-party driver **inside the method that uses it**, never at
  module level. A worker without `pymodbus` must still load every other DAG,
  and `tests/test_registry.py` fails the build if a gate breaks that.

## Test it without a stack

```python
from datagates.gates.base import GateConfig
from datagates.core.entities import device_doc, device_entity

g = ScadaGate(GateConfig(key="plant", type="my_gates.scada:ScadaGate", options={...}))
doc = device_doc(device_entity(g.discover()[0], g), g)      # what fetch() will receive
samples = g.fetch(doc, start, end)                          # mock your client
```

Going through `device_entity` and `device_doc` is the point: that is where
Orion's scalar compaction and the `device_attrs` contract would break. The
`doc_of` fixture in `tests/conftest.py` does it for you, and the test files
do exactly this for every built-in gate:

- `tests/test_core.py` — the shared machinery
- `tests/test_gates.py`, `test_gates_field.py`, `test_gates_files.py`,
  `test_gates_web.py`, `test_gates_stores.py` — one section per gate, on
  recorded payloads
- `tests/test_registry.py` — the catalogue, the config, the lazy imports

Record the real payload once (a `curl` output, a saved XML document, three
rows of the CSV), trim it to the interesting cases — a null, a sentinel, a
row outside the window, a device that is not this one — and assert on the
samples. No test may touch the network.
