# Changelog

Notable changes per release. Dates are ISO. The project follows
[semantic versioning](https://semver.org) loosely: the gate contract
(`Gate`, `DeviceSpec`, `Sample`, the `gates.yaml` keys) and the data model are
what "public" means here, and a breaking change to either gets a major bump
and a migration note.

## 0.2.1 — 2026-09-23

### Fixed

- **The Docker image could not be built from v0.2.0.** `requirements.txt` pinned
  `influxdb-client==1.50.0` while Airflow 3.0.6's constraints file pins 1.49.0,
  and pip refuses the contradiction rather than choosing: *"The user requested
  influxdb-client==1.50.0 / The user requested (constraint)
  influxdb-client==1.49.0"*. Only the image applies those constraints, so only
  the `integration` CI job could see it — every other check passed the
  dependency bump that introduced it. `requirements.txt` states floors now:
  inside the image the constraints file decides, outside it pip resolves freely.
  Nothing else changed. **Use this release rather than v0.2.0.**

## 0.2.0 — 2026-09-23

The release that turns three gate types into a catalogue, with the emphasis on
the systems already installed on a site.

### Added

- **Nineteen new gate types**, 24 in total:
  - field protocols with no history of their own: `modbus`, `bacnet`, `opcua`
    (with optional server-historian reads), `s7`, `snmp`;
  - files however they arrive: `excel_drop`, `xml_drop`, `remote_drop` over
    SFTP/FTPS/FTP;
  - web and messaging: `http_xml` (including SOAP), `http_csv`, `obix`
    (Tridium Niagara), `zabbix`, `mqtt`;
  - other stores: `sql` (SQL Server, Oracle, MySQL, PostgreSQL, ODBC
    historians), `influx_source` (InfluxDB 1.x and 2.x), `prometheus`,
    `ngsi_ld`, `ngsi_v2` (Orion v2 with QuantumLeap or STH-Comet);
  - markets: `entsoe`.
- **A working, disabled example of every gate type** in `config/gates.yaml`,
  enforced by a test: the catalogue cannot drift from the code.
- **Shared machinery** so gates stay short and behave alike: `core/fieldmap.py`
  (the `fields:` block, with `scale`, `offset`, `invalid:` sentinels and
  ranges), `core/httpclient.py` (retry policy, `Retry-After`, request pacing, the
  `auth:` block), `core/timeparse.py`, `core/binary.py` (register and byte
  decoding, word order), `core/xmlrows.py`, `core/cadence.py`, and the
  `PollingGate` and `FileDropGate` base classes.
- **`max_attempts` and `min_interval_s` as gate-level YAML keys**, applied by
  the framework rather than by each gate (ROADMAP M2).
- **`scripts/verify_platform.py`**: checks a live deployment the way a
  consumer would — connectivity, model, the Orion→InfluxDB bridge, freshness
  against each gate's cadence, and the `q=` filters (ROADMAP M1).
- **`scripts/mqtt_spool.py`**: the always-on subscriber for `mqtt` gates in
  spool mode, handing files over by rename so a message is stored exactly once.
- **`docs/legacy-systems.md`**: a survey checklist, a table from products
  (Desigo, Metasys, Niagara, Wonderware, Kamstrup, SolarEdge, ChirpStack, …)
  to gates, the local-protocol-versus-cloud-API trade-off, an integration
  playbook and the rules for working on somebody's OT network.
- **`requirements-gates.txt` and per-gate extras** in `pyproject.toml`; the
  image installs the drivers by default (`INSTALL_GATE_EXTRAS=0` to skip).
- **Packaging**: `pip install -e .` exposes the `datagates` package, so gate
  types can be developed in their own repository.
- CI: Python 3.12 and 3.13, a packaging job, a job that installs every optional
  driver, and a DAG-build job that covers the disabled examples too.

- **A controlled property vocabulary** (`plugins/datagates/core/vocab.py`,
  `docs/properties.md`): every `controlledProperty` a gate may emit, with its
  unit, its kind (instant / delta / register / state), how a consumer may
  aggregate it, and a mapping towards SAREF and QUDT for RDF export. The
  counter guard derives its cumulative set from it instead of keeping a copy,
  and `tests/test_vocab.py` fails if a gate, the documentation or the consumer
  client drifts from it.

### Changed

- `csv_drop` and `http_json` now use the shared field map, timestamp parser and
  HTTP client. Existing configurations keep working; `csv_drop` additionally
  accepts `encoding`, `header`, `skip_rows`, `quotechar`, `recursive` and
  `max_file_age_days`, and `http_json` accepts `auth`, `body` and `paginate`.
- A file a drop gate cannot read is logged and skipped instead of failing the
  run — a corrupt upload must not cost the directory's other files.
- `docs/gates.md` rewritten around the catalogue, the shared blocks and the
  three kinds of gate.
- **Property names corrected in the examples**: `humidity` is
  `relativeHumidity`, `gasIndex` is `gasVolume`, and the `xml_drop` example no
  longer labels a meter register `energyConsumption` while marking it
  `cumulative: true` — it emits `energy`, which is what a register is. Only
  example configuration was affected; renaming a property in a live deployment
  mints a new summary id and orphans the old series.

### Fixed

- The CI check for the DAG bag counted instances of `airflow.models.DAG`,
  which in Airflow 3 no decorated DAG is: `@dag` produces an `airflow.sdk`
  DAG, so the check reported an empty bag while every DAG had been built. It
  now parses the folder with `DagBag`, as the scheduler does, and reports
  import errors with their file.

- **The cursor backfill lost the reading on every window boundary.** A gate's
  `fetch` returns `(a, b]` and the backwards walk filtered `observed < cursor`,
  so the boundary sample was kept by no window: the exclusive lower bound of the
  earlier one, the discarded cursor of the later one. Measured on a 241-reading
  series: 233 stored before, 240 after, the absentee being one the counter guard
  correctly refused. The rule now lives in `core/windows.py` with a test.
  **Ported production code has the same hole**, so a platform sharing this
  factory should re-backfill.
- **The `sql` gate was silent when no tag matched.** In `long` layout the keys of
  `columns` are the values of `property_column`; a device whose tag is not mapped
  produced nothing, looking exactly like an upstream with no data. It now logs
  the tags it saw and the ones it knows.
- **`datagates check` died on other gates.** The CLI constructed every entry in
  the config, so one disabled example with unset secrets broke it — in the
  shipped configuration. It builds only what was asked for, and `list` reports
  what cannot be built.
- **The run DAG found no devices for any gate registering its own entity
  type.** `get_all_devices` filtered on `type=Device`, so `open_meteo`
  (`WeatherForecastLocation`) and `entsoe` (`MarketPriceFeed`) mapped over zero
  devices, wrote nothing and reported success — the two gates enabled by
  default ingested nothing at all. The query now keys on `dataGate` alone.
- **The image upgraded SQLAlchemy past what Airflow supports**, leaving an
  image that built cleanly and whose Airflow could not import. Extras install
  under Airflow's constraints now, and the build asserts the import.
- **Orion-LD crash-looped against an authenticated MongoDB**, and **Caddy
  restart-looped on an empty `email` argument**. Both are compose fixes with
  the reasoning recorded in `ROADMAP.md` §3.
- **A module named `http.py` under `plugins/` shadowed the standard library**
  once Airflow's plugin scanner imported it by basename, which broke DAG
  parsing with no import error to show. Renamed to `httpclient.py`, and
  `plugins/.airflowignore` keeps the scanner out.
- `scripts/verify_platform.py` counted forecast points with a past-only Flux
  range and probed `type=Device`, so it under-reported points and warned on a
  healthy deployment.

### Notes

- Verified in CI: the suite on Python 3.12 and 3.13, `pip install .`, every
  optional driver installing, and **the DAG factory under a real Airflow
  3.0.6** — 68 DAGs from all 25 catalogue entries.
- **Verified on a live stack** (2026-09-22): ten services up, the weather
  cycle end to end — 48 forecast points and 1249 backfilled ERA5 points per
  property — and `verify_platform.py` with no failures. Output in `README.md`.
- Still unverified: the proxy from outside, the counter guard / `status()` /
  cursor-backfill paths, and every gate that needs credentials or hardware.
  See §2 of `ROADMAP.md`.

## 0.1.0 — unreleased

The initial extraction from the DATAWiSE project's private integration
platform: the generic core (Orion-LD registry, InfluxDB writer, summary model,
counter guard), the gate contract and YAML loader, the three DAG templates,
five gate types (`open_meteo`, `nordpool`, `csv_drop`, `http_json`,
`thingsboard`), the secured compose stack behind Caddy, the consumer client,
and the data-model documentation.
