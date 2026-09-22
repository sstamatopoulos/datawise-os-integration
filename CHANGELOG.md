# Changelog

Notable changes per release. Dates are ISO. The project follows
[semantic versioning](https://semver.org) loosely: the gate contract
(`Gate`, `DeviceSpec`, `Sample`, the `gates.yaml` keys) and the data model are
what "public" means here, and a breaking change to either gets a major bump
and a migration note.

## Unreleased — 0.2.0

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

### Changed

- `csv_drop` and `http_json` now use the shared field map, timestamp parser and
  HTTP client. Existing configurations keep working; `csv_drop` additionally
  accepts `encoding`, `header`, `skip_rows`, `quotechar`, `recursive` and
  `max_file_age_days`, and `http_json` accepts `auth`, `body` and `paginate`.
- A file a drop gate cannot read is logged and skipped instead of failing the
  run — a corrupt upload must not cost the directory's other files.
- `docs/gates.md` rewritten around the catalogue, the shared blocks and the
  three kinds of gate.

### Fixed

- The CI check for the DAG bag counted instances of `airflow.models.DAG`,
  which in Airflow 3 no decorated DAG is: `@dag` produces an `airflow.sdk`
  DAG, so the check reported an empty bag while every DAG had been built. It
  now parses the folder with `DagBag`, as the scheduler does, and reports
  import errors with their file.

### Notes

- Verified in CI: the suite on Python 3.12 and 3.13, `pip install .`, every
  optional driver installing, and **the DAG factory under a real Airflow
  3.0.6** — 68 DAGs from all 25 catalogue entries.
- Still unverified: the compose stack end to end, one full ingest cycle, and
  every new gate against a real upstream. See §2 of `ROADMAP.md`.

## 0.1.0 — unreleased

The initial extraction from the DATAWiSE project's private integration
platform: the generic core (Orion-LD registry, InfluxDB writer, summary model,
counter guard), the gate contract and YAML loader, the three DAG templates,
five gate types (`open_meteo`, `nordpool`, `csv_drop`, `http_json`,
`thingsboard`), the secured compose stack behind Caddy, the consumer client,
and the data-model documentation.
