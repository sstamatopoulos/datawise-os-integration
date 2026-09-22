# Roadmap and handover

**Written for:** whoever continues this repository, human or Claude worker.
It says what exists, what has been verified, why things are the way they are,
and what to do next, in order. Read it before touching code.

Last updated: 2026-09-22, by the session that built the gate catalogue
(five gate types to twenty-four) and prepared the repository for publication.

---

## 1. Where this comes from

This is the open-source extraction of the DATAWiSE project's data integration
platform (private repository `datawise-integration`, GitLab, ICCS/NTUA). That
platform ingests indoor-air sensors, electricity/heat/water/gas meters,
photovoltaics, market prices and weather for three pilot sites into FIWARE
Orion-LD and InfluxDB, run by Airflow. Its generic core was copied here;
everything pilot-specific was left out on purpose:

- no credentials, tokens, `.env`, `config.ini` files;
- no pilot registries (building UUIDs, meter serials, ThingsBoard keys);
- no git history (the private repository's history once contained a private key).

The private repository stays the production system. This one is the reusable
framework. Improvements made here can be ported back, and vice versa, by hand.

## 2. What exists now (v0.2.0, unreleased)

| Area | State |
|---|---|
| `plugins/datagates/core/` | Ported from production and extended: Orion-LD registry (`upsert_entity` appends attributes via POST /attrs), InfluxDB writer, summary model, counter guard, param parsing. New shared machinery: `fieldmap.py` (the `fields:` block), `http.py` (retry, `Retry-After`, pacing, `auth:`), `timeparse.py`, `binary.py` (register/byte decoding, word order), `xmlrows.py`, `cadence.py` (cron → expected interval). |
| `plugins/datagates/gates/base.py` | The Gate contract: `discover()`, `fetch()`, optional `status()`, ClassVar defaults, `rolling`, `rewrite_lookback`, `backfill_mode` (cursor / stateless / none), `max_attempts` / `min_interval_s`, `speaks`. |
| `plugins/datagates/gates/polling.py` | Base for upstreams with no history: `rolling`, no backfill DAG, per-device field maps, aligned timestamps. |
| `plugins/datagates/gates/file_drop.py` | Base for files in a directory: globbing, device matching, window filter, stateless backfill, one bad file does not spoil the run. |
| `plugins/datagates/gates/registry.py` | `gates.yaml` loader with `${VAR}` / `${VAR:-default}` expansion, built-in type map, custom types by `module:Class`. |
| `plugins/datagates/dags/factory.py` | Builds `<key>_init`, `<key>_run`, `<key>_backfill` per gate. Devices carry `dataGate: <key>` so a gate finds its own devices. Unchanged in this round. |
| Gate types (24) | field protocols `modbus`, `bacnet`, `opcua`, `s7`, `snmp`; files `csv_drop`, `excel_drop`, `xml_drop`, `remote_drop`; web and messaging `http_json`, `http_xml`, `http_csv`, `obix`, `zabbix`, `thingsboard`, `mqtt`; stores `sql`, `influx_source`, `prometheus`, `ngsi_ld`, `ngsi_v2`; weather and markets `open_meteo`, `nordpool`, `entsoe`. |
| `config/gates.yaml` | Three credential-free gates enabled, and a working disabled example of **every** type — the catalogue, enforced by a test. 25 entries, 68 DAGs. |
| `requirements-gates.txt`, `pyproject.toml` extras | The optional drivers, one extra per gate group. The image installs them by default (`INSTALL_GATE_EXTRAS=0` to skip). `pip install .` exposes the `datagates` package. |
| `docker-compose.yml` + `proxy/Caddyfile` | Caddy (TLS, HSTS, API-key gate for the broker) is the only published service. Airflow 3.0.6 LocalExecutor + Postgres, Orion-LD 1.5.1 + MongoDB 4.4 (auth on), InfluxDB 2.7 (token). Secrets from `scripts/gen-secrets.sh`. `./scripts` is mounted into the workers; gate `${VAR}`s are forwarded. See `SECURITY.md`. |
| `scripts/verify_platform.py` | Checks a live deployment: connectivity, model, the Orion→InfluxDB bridge (using the real `summary_measurement_urn`), freshness against each gate's cadence, and the `q=` filters. `--json`, `--gate`, `--section`, `--tolerance`; exit 1 on any failure. |
| `scripts/mqtt_spool.py` | The always-on subscriber for `mqtt` gates in spool mode; hands files over by rename. |
| `clients/python/datagates_client.py` | Consumer client ported from the production data-access guide. |
| `docs/` | `data-model.md`, `gates.md` (every option of every type), `legacy-systems.md` (products → gates, playbook, OT rules), `adding-a-gate.md`, `publishing-checklist.md`. |
| `tests/` | 137 tests, no network: shared machinery, every gate's `discover()`/`fetch()` on recorded payloads, the catalogue, the lazy-import rule, cadence. |
| `.github/` | CI (ruff + pytest on 3.12/3.13, `pip install .`, every optional driver installs, every configured gate builds its DAGs under Airflow), issue templates, PR template, dependabot. |

### Verified

- `ruff check .` clean; `pytest` 134 passed, 3 skipped (the SQLite-backed `sql`
  tests skip without SQLAlchemy, which CI installs), on Python 3.13 locally.
- Every one of the 24 types constructs from its shipped example and returns
  devices from `discover()`; every gate module imports with all optional
  drivers blocked (both asserted by `tests/test_registry.py`).
- The `sql` gate runs against a real SQLite database in tests — bind
  parameters, both layouts, the timezone path.
- `scripts/verify_platform.py` runs and reports a clean failure, with no stack
  trace, when no broker is reachable.
- The gate logic of the five original types against real upstreams was verified
  **in the private repository** before being ported.

### Not yet verified — do these first

1. **No new gate has touched a real upstream.** The nineteen added types are
   written from protocol and API knowledge and tested against recorded
   payloads with the transport mocked at one method. Expect the first real
   Modbus, BACnet, S7, oBIX or SOAP endpoint to need a fix; that is what the
   one-method seam is for. Prioritise by what the pilots actually have.
2. **The DAG factory under a real Airflow.** `factory.py` was written against
   Airflow 3.0.6's API as used in production but has not been imported by
   Airflow in this repository. CI's `dags-load` job does it for all 68 DAGs;
   so does `docker compose up`.
3. **The compose stack end to end.** Orion-LD 1.5.1 against an authenticated
   MongoDB 4.4 (`-dbuser/-dbpwd/-dbAuthDb`), InfluxDB first-boot setup, Airflow
   init with Fernet and JWT secrets, Caddy issuing localhost certificates and
   enforcing the API key. Expect small fixes (healthcheck commands, the mongo
   shell is `mongo` in 4.4 and `mongosh` in 6+, Airflow 3 base-URL settings
   behind a proxy).
4. **One full cycle**: `weather_forecast_init` → `weather_forecast_run` →
   summaries in Orion have `lastReadingAt` → InfluxDB has the series under the
   summary URN → `weather_observed_backfill` loads 2024 onward →
   `verify_platform.py` reports nothing failing.
5. **The optional drivers next to Airflow's dependency set.** CI's
   `gate-extras` job installs them against `requirements.txt`, not against the
   Airflow constraint file; the image build is the real test.

## 3. Decisions and why (do not relitigate without reading)

### The data model and the two stores

- **Core NGSI-LD context only.** Adding the Smart Data Model JSON-LD contexts
  makes Orion-LD store expanded attribute names; every `q=` filter then breaks
  unless consumers send Link headers. Tried and reverted in production.
  `verify_platform.py` has a check that fails if it ever comes back.
- **POST /attrs, never PATCH /attrs, for existing entities.** PATCH is
  update-only and silently drops attributes the entity lacks; a re-registration
  that adds attributes must append. Cost the production system a day.
- **Summary URN = InfluxDB measurement name** (uuid5 of device URN + property).
  Consumers need no lookup table. Never change the uuid5 namespace or key
  format; every existing series hangs off it. `verify_platform.py` imports
  `summary_measurement_urn` rather than restating the recipe, so the check
  cannot pass while consumers break.
- **Orion-LD compacts one-element arrays to scalars.** Always go through
  `core.entities.as_list` before indexing anything read back. Indexing the
  string once wrote 6,421 points under a measurement named `w`.
- **`location` is the reserved GeoProperty.** Coordinates only; place
  descriptions go elsewhere (`installationLocation`).
- **Delta series are stamped at period start**; cumulative series are guarded
  and must be differenced by consumers. Both conventions are in
  `docs/data-model.md`.

### Gates and the framework

- **Forecast-like gates are `rolling`**: the watermark is in the future, so the
  normal "from watermark to now" window is empty; the run calls
  `fetch(device, now, now)` once and stores everything returned.
- **An upstream with no past gets no backfill DAG.** `PollingGate` sets
  `rolling` and `backfill_mode = "none"`: a register, a BACnet object or an OID
  holds one value, the value it holds now. The schedule is therefore the
  sampling rate of the series, and a missed run is a hole nothing can fill.
  Say that in the handover to whoever owns the plant.
- **Polling timestamps are aligned** (`align_s`, 60 s by default) so an Airflow
  retry after a partial failure overwrites the point it already wrote instead
  of adding a second one seconds later.
- **Two gates change kind by option** (`opcua` with `history: true`, `ngsi_v2`
  with a history component): they override `rolling` and `backfill_mode` as
  properties, as `open_meteo` already did. Reading a SCADA historian is usually
  the fastest way to get a legacy plant's history in, so it is worth the
  branch.
- **Third-party drivers are imported inside the method that uses them.** A
  worker without `pymodbus` must still load every other DAG. Enforced by a test
  that blocks the imports and reloads every gate module; CI installs no field
  driver in the lint-and-test job for the same reason.
- **Retry only what is transient** (connection reset, timeout, 429, 5xx) and
  honour `Retry-After`. A 401 or 404 is a configuration error: retrying it
  fails slower and gets the client rate-limited. `max_attempts` and
  `min_interval_s` are framework-level YAML keys, so no gate implements this
  again.
- **Sentinels are configuration, not code.** Legacy systems mark "no reading"
  with -9999, 32767 or 9999.9; `invalid:` in the field map drops them. A
  sentinel stored as a value ruins every average a consumer computes, forever.
- **One Modbus request per field.** Block reads are faster, but one unreadable
  register fails the whole block and legacy maps are full of holes. At a
  five-minute schedule the round trips are free.
- **XML namespaces are stripped before matching.** The same vendor's export
  changes its namespace URI between software versions, and a configuration
  written against the old URI then matches nothing at all, silently.
  `keep_namespaces: true` is there for documents that genuinely need them.
- **No WSDL is read** by `http_xml`. A generated client breaks whenever the
  vendor regenerates their schema; a request template and a row path keep
  working, and what they return is visible in the YAML.
- **SQL takes bind parameters, never formatted strings.** A YAML file must not
  be able to inject SQL into somebody's production historian.
- **Files are never moved, renamed or deleted** by the drop gates. A gate that
  consumes its input cannot be re-run, and a partner who re-uploads a corrected
  file expects the correction to land. `remote_drop` mirrors instead, and its
  `delete_after_download` is off by default because deleting from someone
  else's server is irreversible and often against their retention rules.
- **A file that cannot be read is logged and skipped**, not fatal: partner
  exports arrive as not-a-zip, wrong encoding, malformed XML and Excel lock
  files, and one bad file must not cost the run every other file.
- **MQTT is drained through the broker's own persistent session** (fixed client
  id, `clean_session: false`, QoS 1), so the broker queues messages while
  Airflow is not connected. The `spool` alternative hands files over **by
  rename** and the gate deletes what it has read: truncating a file an
  always-on writer still has open loses whatever arrives in between, and nobody
  notices for a month.
- **`config/gates.yaml` is the catalogue and it is tested.** Every built-in
  type must have a working, disabled example there that constructs and
  discovers devices offline. Documentation that cannot drift from the code is
  worth the constraint on gate design (options-driven `discover()`).
- **`remote_drop` instead of an SFTP service in compose** (M3's original plan).
  Pulling from the partner's server needs no inbound port, no user management
  and no chroot on our side, and partners overwhelmingly already have a server.
  An inbound SFTP drop is still worth adding for partners who insist on
  pushing; `csv_drop` over a mounted volume already covers it.

### Project

- **Env-only settings, YAML-only gate config, `${VAR}` for secrets.** No
  `config.ini` files, ever.
- **Apache-2.0** as the usual choice for Horizon Europe software outputs.
  Change the LICENSE file if the consortium decides otherwise.
- **Gates only read.** Nothing in this platform writes to an upstream system.
  That is the first question the owner of a control network asks, and it is
  worth keeping true.

## 4. Roadmap

Ordered. Each item is small enough for one session; finish it end to end
(code, test, docs, CI green) before starting the next.

### M1 — First run (do before publishing)

- [x] `scripts/verify_platform.py`, reading the source list from
      `config/gates.yaml`: connectivity, model, bridge, freshness and query
      checks, with `--json` for automation. Done 2026-09-22, run against no
      live stack yet.
- [ ] `docker compose up -d --build` on a clean machine; fix what breaks; record the fixes in this file.
- [ ] Run the full weather cycle from §2 "Not yet verified" and paste the summary entity, a Flux result and the `verify_platform.py` output into `README.md` as "verified output".
- [ ] Work through `docs/publishing-checklist.md`: identity and URLs, the secret scan, repository settings.
- [ ] Tag `v0.2.0`, push to GitHub, confirm all four CI jobs are green.

### M2 — Operability

- [x] Rate limiting and retry policy per gate as YAML options
      (`min_interval_s`, `max_attempts`) handled by the framework, so gates
      stop re-implementing it (`core/http.py`). Done 2026-09-22.
- [ ] `dags/health_check.py`: daily DAG that counts entities per gate, flags summaries whose `lastReadingAt` is older than the gate's cadence (`core.cadence.expected_interval` already computes it), optionally emails (SMTP via env, off by default). Reuse the check functions in `scripts/verify_platform.py` rather than writing them twice.
- [ ] `dags/dq_weekly_report.py`: per-device data-quality report entities (`DataQualityReport`), ported from production: gaps, out-of-range values, counters running backwards.
- [ ] Gate `status()` for `http_json` and `snmp` (map a "latest values" endpoint or a set of health OIDs to Device attributes) so battery/signal style health lands on entities.
- [ ] Surface the counter guard's rejections as an entity attribute or a metric, not only a log line.

### M3 — More gates

- [x] `mqtt` gate: drained on a schedule through a persistent session, with an always-on spool subscriber as the alternative. Done 2026-09-22.
- [x] `influx_source` gate (1.x InfluxQL and 2.x Flux), `ngsi_ld` mirror, `ngsi_v2` (Orion v2 + QuantumLeap/STH), `prometheus`, `sql`. Done 2026-09-22.
- [x] Field protocols: `modbus`, `bacnet`, `opcua`, `s7`, `snmp`, with `PollingGate` as their shared base. Done 2026-09-22.
- [x] Files: `excel_drop`, `xml_drop`, `remote_drop` (SFTP/FTPS/FTP), with `FileDropGate` as their shared base. Done 2026-09-22; this supersedes the planned SFTP service in compose (see §3).
- [x] Web: `http_xml` (including SOAP), `http_csv`, `obix` (Tridium Niagara), `zabbix`, `entsoe`. Done 2026-09-22.
- [ ] Pulse-counter style "readers with inputs" as a documented `http_json` recipe (the production MESH sub-meter mapping), or a small gate if placeholders are not enough.
- [ ] Real-upstream verification pass over the new gates (§2 item 1), starting with whichever the pilots have.

### M4 — Consumers

- [ ] Port the production data-access guide (query recipes, 8 example scripts, benchmark) into `docs/consumer-guide.md` and `clients/python/examples/`, generalised: no pilot names, sources taken from `gates.yaml`.
- [ ] Pagination in `datagates_client.py` (Orion caps `limit` at 1000).
- [ ] A Grafana provisioning folder (InfluxDB datasource + one dashboard per gate type) as an optional compose profile.
- [ ] A `datagates` CLI (`python -m datagates list|check <key>|fetch <key> --dry-run`) so a gate can be tried without Airflow. The pieces exist; it is the missing step in the playbook in `docs/legacy-systems.md`.

### M5 — Security and packaging

- [x] Reverse proxy with TLS and access control in front of every service (Caddy, `proxy/Caddyfile`); backends unpublished; generated secrets. Done 2026-09-22, not yet run.
- [x] Packaging so gates can be developed outside this tree (`pip install -e .`, per-gate extras). Done 2026-09-22.
- [ ] Per-consumer identities: optional FIWARE PEP proxy (Wilma) + Keyrock compose profile behind Caddy, replacing the shared `ORION_API_KEY`.
- [ ] Read-only InfluxDB tokens per consumer created at init (`influx auth create`), documented in the consumer guide.
- [ ] Publish to PyPI (the remaining decision is the distribution name).
- [ ] Helm chart or Kubernetes manifests once the compose stack is stable. Note that `bacnet` needs broadcast-capable networking, which is the one gate that constrains the deployment model.

### M6 — Further legacy coverage (only with a real system to test against)

Each of these is a gate type nobody should write speculatively; write it when a
site needs it, and write the payload into a test.

- [ ] `iec104` (IEC 60870-5-104) for grid and district-heating telecontrol.
- [ ] `dnp3` for water and power utilities outside Europe.
- [ ] `knx` (KNXnet/IP) for lighting and room control.
- [ ] `mbus` direct (serial or TCP gateway) rather than through a concentrator's Modbus map.
- [ ] `dlms` / COSEM against a head-end, if any partner grants access.
- [ ] `lonworks`, `enocean`, `sigfox` — by demand only.
- [ ] Inbound SFTP drop service in compose (chrooted, key-only) for partners who insist on pushing.

## 5. Conventions for whoever continues

- Keep `ruff check .` and `pytest` green; add a test for every gate change on a
  recorded payload, never against the network.
- A gate ships with: class + docstring showing its YAML and what the upstream
  really does; drivers imported inside the method that uses them;
  `BUILTIN_TYPES` entry; a driver line in `requirements-gates.txt` and an extra
  in `pyproject.toml`; a **working** disabled example in `config/gates.yaml`;
  new `${VAR}`s in `.env.example` and `docker-compose.yml`; a section in
  `docs/gates.md`; a row in `docs/legacy-systems.md` if it maps to real
  products; tests.
- Use the shared machinery (`core.fieldmap`, `core.http`, `core.timeparse`,
  `core.binary`, `core.xmlrows`, `PollingGate`, `FileDropGate`) instead of
  reimplementing it. A built-in gate is 80 to 150 lines because of them; a gate
  that is 400 lines is usually re-solving a solved problem.
- Never commit secrets, partner exports, or anything naming a real site.
  Recorded payloads are anonymised, addresses are RFC 1918, hosts end in
  `.example`.
- Document every "the upstream does X although its docs say Y" finding in the
  gate's docstring; those findings are the most valuable part of a gate, and
  they are what makes the difference between 80 lines and a week of debugging.
- Commit messages explain the why; the diff shows the what.

## 6. Pointers back to production knowledge

Private repository `datawise-integration` (ask the DATAWiSE data integration
lead for access): `docs/data-access/README.md` is the consumer guide with the
query recipes and performance measurements to port for M4;
`docs/data-access/verify_platform.py` is the original of
`scripts/verify_platform.py`, with 22 checks in six sections — the operator and
query sections are the parts not yet ported; `dags/daily_health_check.py` and
`dags/dq_weekly_report.py` are the sources for M2. The MESH, LMT, e-st.lv and
Solinteg connectors there show real-world API quirks worth turning into gate
docstrings, and the pilots' Modbus and BACnet point lists are the fastest way
to verify the new field-protocol gates against something real.
