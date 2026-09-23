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

- `ruff check .` clean and the full suite green **in CI on Python 3.12 and
  3.13** (137 tests; locally 134 pass and the three SQLite-backed `sql` tests
  skip without SQLAlchemy).
- **The DAG factory under a real Airflow 3.0.6**, in CI: `dags/` parses with
  no import errors, the three enabled gates produce exactly their eight DAG
  ids, and all 25 catalogue entries build 68 DAGs — with the six polling gates
  correctly producing `init` and `run` only. Run 35720991192.
- `pip install .` works and every built-in type resolves from the installed
  package; all eight optional drivers install alongside the platform's own
  dependencies (CI jobs `packaging` and `gate-extras`).
- Every one of the 24 types constructs from its shipped example and returns
  devices from `discover()`; every gate module imports with all optional
  drivers blocked (both asserted by `tests/test_registry.py`).
- The `sql` gate runs against a real SQLite database in tests — bind
  parameters, both layouts, the timezone path.
- `scripts/verify_platform.py` runs and reports a clean failure, with no stack
  trace, when no broker is reachable.
- The gate logic of the five original types against real upstreams was verified
  **in the private repository** before being ported.
- **The compose stack, end to end, on 2026-09-22** (Docker Desktop, Windows):
  all ten services healthy, Airflow registering the eight DAGs of the three
  enabled gates, `weather_forecast_init` -> `weather_forecast_run` ->
  48 forecast points in InfluxDB under the summary URN, summaries carrying
  `lastReadingAt`/`lastReadingValue`/rolling stats, and
  `weather_observed_backfill` loading 1249 hourly ERA5 points per property.
  `scripts/verify_platform.py` reports 0 failures and 0 warnings. The output
  is in `README.md` under "Verified output". It took the seven fixes listed
  below, none of which 137 unit tests or five CI jobs could have found.
- **All three gates the shipped config enables**, same day: `weather_forecast`
  (48 forecast points), `weather_observed` (1249 backfilled ERA5 points per
  property) and `prices` (193 Nord Pool day-ahead points at the 15-minute
  market time unit), each with `verify_platform.py` reporting no failures and
  no warnings. That covers the rolling, revised-history and stateless-backfill
  shapes, and two entity types beyond `Device`.
- **The counter guard, the cursor backfill and the `sql` gate**, 2026-09-22,
  against a SQLite meter series of 241 hourly readings built for the purpose
  (10 days, a register that only goes up, and one impossible reading of 7.0
  where the register stood at ~100 000): the guard reported "rejected 1
  reading(s): 72 accepted, 1 rejected as out-of-track" and the 7.0 never became
  `rolling24hMin`, which is the damage it exists to prevent; the cursor backfill
  walked 8 one-day windows, stopped at `windows_per_run`, **resumed from
  `backfillCursorAt` on the next run** and terminated with `backfillDoneAt` on
  "reached floor"; and 240 of the 241 readings are in InfluxDB, the only
  absentee being the one the guard refused. That number was 233 before the
  boundary bug below was fixed, measured on the same data.
- **The proxy, from outside the stack**, same day: Airflow over TLS through
  Caddy answers 200; the broker answers 401 with an NGSI-LD `ProblemDetails`
  body without `X-API-Key` and 200 with it, returning the summary entity. Only
  Caddy's internal CA was exercised -- ACME has never run here, because this
  machine already had something on 80/443 and the stack was brought up on
  `HTTP_PORT`/`HTTPS_PORT` instead.

### Not yet verified — do these first

1. **Eighteen of the nineteen new gates have not touched a real upstream.**
   `sql` has (SQLite, above), and getting it right took one configuration
   mistake and one framework fix. The rest are written from protocol and API
   knowledge and tested against recorded payloads with the transport mocked at
   one method. Expect the first real Modbus, BACnet, S7, oBIX or SOAP endpoint
   to need a fix; that is what the one-method seam is for. Prioritise by what
   the pilots actually have.
2. **Everything credentialed.** All three credential-free gates have run; no
   gate needing a token, a password or a network route to a device has.
3. **A gate that actually returns `status()` attributes.** The task runs and
   handles the empty case -- every weather run reports "no status attributes for
   this gate" -- but no built-in gate implements it, so nothing has ever patched
   battery or signal onto a Device. Implementing it for `http_json` and `snmp`
   is an M2 item; verifying it comes with that.

## 3. Decisions and why (do not relitigate without reading)

### The cursor backfill lost one reading per window boundary

Found 2026-09-22 by backfilling a dense series and counting what arrived: 233 of
241 readings stored, and the seven missing ones all sat exactly on a window
boundary. This is ported production code, so **the private platform has the same
hole**: every series backfilled with a cursor is missing one reading per window
boundary, for as long as the backfill ran.

The cause is one inequality. A gate's `fetch(device, a, b)` returns samples in
`(a, b]`, and the cursor walk went backwards filtering `observed < cursor`:

    window k    fetches (a_k, cursor_k]   and kept < cursor_k
    window k+1  fetches (a_k+1, a_k]      where cursor_k+1 = a_k

The sample at exactly `a_k` is window k's *exclusive lower bound*, so that
window never fetches it; window k+1 fetches it as its upper bound and then
discards it for being `>= cursor`. Neither window stores it and nothing reports
a gap. The rule is `<=` now, in `core/windows.py` with its reasoning and a
test, and re-storing a boundary sample costs nothing because writes are keyed
by time. Verified afterwards on the same data: 240 of 241, the absentee being
the reading the counter guard is supposed to refuse.

The lesson is not about the inequality. Both stores agreed with each other, the
DAG was green, the summaries looked right, and only counting against the source
found it. A backfill that silently drops a fraction of its samples is the worst
failure this platform can have, and the only defence is comparing a series with
the upstream it came from.

### What the first run cost, and what it taught

Seven fixes, 2026-09-22, in the order they were found. Every one was invisible
to the unit suite and to CI, which is the whole argument for running the thing.

1. **`core/http.py` shadowed the standard library.** Airflow imports every file
   under `plugins/` using its *basename* as the module name, so ours landed in
   `sys.modules` as `http`. Everything that then did `from http import
   HTTPStatus` -- urllib3, requests, Airflow itself -- broke, DAG parsing found
   zero files, and `airflow dags list-import-errors` said nothing at all,
   because the failure happened before any DAG file was read. Renamed to
   `httpclient.py`; `plugins/.airflowignore` now keeps the scanner out of the
   package. **Never name a module in `plugins/` after a stdlib module**: json,
   logging, types, select, socket, email, csv, queue, platform, secrets.
2. **`datagates/__main__.py` hijacked the Airflow CLI** by the same mechanism:
   imported as `__main__`, its `if __name__ == "__main__"` guard was true, and
   `airflow users create` died with "invalid choice: 'users'". The guard checks
   `__package__` too now.
3. **`dags/datagates_dags.py` did not contain the word "Airflow".** DAG
   discovery runs in safe mode and only parses files containing both "dag" and
   "airflow"; everything Airflow-related lives in `plugins/`, so the one file
   Airflow loads was the one file it skipped. The CI job had passed
   `safe_mode=False`, which is why it could not have caught this -- a test
   harness that disables the behaviour under test proves nothing.
4. **The gate extras upgraded SQLAlchemy out from under Airflow.** Airflow
   3.0.6 pins 1.4.54; `requirements-gates.txt` asked for `>=2.0`, and without
   the constraints file pip obliged, after which Airflow's ORM would not
   import. The image now installs the extras under the same constraints and the
   build asserts that `airflow` and `TaskInstance` still import. CI runs the
   `sql` gate's tests against 1.4 and 2.x, because the image and a standalone
   install now genuinely differ.
5. **Orion-LD 1.5.1 opens two Mongo connections with two drivers.**
   `-dbAuthDb admin` is appended to the C driver's URI as a bare query option,
   which it rejects ('URI option "admin" contains no "=" sign'); `-dbURI` fixes
   that driver but the legacy C++ pool ignores it and connects with no
   credentials. Passing neither leaves `mongodb://user:pass@mongo/`, whose
   authSource defaults to admin, where the root user is. Both drivers connect.
   Orion-LD also logs that URI, password included, whenever it cannot connect.
6. **Caddy's `email` directive cannot take an empty argument**, and compose
   passed `ACME_EMAIL` as an empty string for `PUBLIC_HOST=localhost`. The
   proxy restart-looped on a parse error while every service behind it merely
   looked unreachable. Compose now defaults it to an unusable address, which
   localhost never needs and which Let's Encrypt rejects loudly for a real
   host. The Caddyfile's own `{$VAR:default}` syntax does not help: it applies
   to *unset* variables, not empty ones.
7. **`get_all_devices` filtered on `type=Device`**, so the run DAG found none
   of open_meteo's `WeatherForecastLocation` devices (nor entsoe's
   `MarketPriceFeed`), mapped over zero devices, wrote nothing, and reported
   success. The two gates shipped enabled ingested nothing and nothing said so.
   The query keys on `dataGate=="<key>"` alone now, which is exact because
   summaries never carry that attribute. **A silent no-op is the worst failure
   mode this platform has**, and this class of bug -- a filter that excludes
   everything -- produces it; `tests/test_core.py` pins the query shape.

Its second run in CI found the one that matters for anyone scripting Airflow
3: **a task talks to the API server, so triggering a DAG before that server
accepts connections fails**, with `httpx.ConnectError: [Errno 111] Connection
refused` in the supervisor and then "DAG not found in serialized_dag table" in
the scheduler -- which reads like a parsing problem and is not one. `airflow
dags list` answers before either the API server is healthy or the DAG is
serialized, so the harness now waits on compose's health status for the API
server and the scheduler, and on `airflow dags details` for serialization. On a
machine where the stack has been up for a while none of this appears, which is
exactly why it took CI to find it.

The `integration` job's own first run cost two more, both about the
difference between a script that works here and a script that works anywhere:
`scripts/*.sh` were committed without the executable bit (git mode 100644), so
`./scripts/gen-secrets.sh` -- the command the README gives -- failed with exit
126 on a Linux runner and would have failed for the first person to clone the
repository; and `gen-secrets.sh` called python with the `cryptography` package
to make one random key, falling back to `docker run` on the 3 GB Airflow image,
which a runner with a system-managed python could not install. A Fernet key is
urlsafe-base64 of 32 random bytes, so `openssl rand -base64 32 | tr '+/' '-_'`
is exactly equivalent and needs nothing.

`scripts/integration_test.sh` then cost three more, all of them the same
shape -- a tool that works in the shell and not in the script:

- `python3 - <<'PY'` with the document piped in: the heredoc *is* stdin, so
  `json.load(sys.stdin)` read the program's own leftovers and the check
  declared a healthy platform broken. Data goes in a file or an argument.
- `mktemp -d` on Git Bash returns `/tmp/tmp.XXXX`, an MSYS path that the shell
  understands and the native `python.exe` and `curl.exe` do not. The file was
  written and could not be opened. The scratch directory is relative now.
- `curl -o /dev/null` exits 23 on Windows, and the InfluxDB `--raw` CSV puts
  `_value` in the middle of the row, not at the end, so "the last field" was
  the measurement name and every count read as zero.

`scripts/verify_platform.py` had two of its own, found by pointing it at a real
deployment: it counted InfluxDB points with `range(start: -90d)` and so saw 2
of 48 forecast points (a forecast is in the future -- the trap its own
`docs/data-model.md` warns consumers about), and it probed `type=Device`, so it
reported zero entities on a healthy deployment and taught the reader to ignore
warnings.


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
- **Verify the DAG bag with `DagBag`, not with `isinstance(x, airflow.models.DAG)`.**
  In Airflow 3 the `@dag` decorator produces an `airflow.sdk` DAG, a different
  class from `airflow.models.DAG`, so the obvious test reports "no DAGs built"
  while all of them were built. Cost the first CI run of this repository, which
  is exactly what that job is for.
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

- **Caddy, not nginx, in front of everything.** Confirmed 2026-09-22. It
  issues and renews certificates itself — its own CA for `localhost`, ACME for
  a real `PUBLIC_HOST` — so there is no certbot sidecar, no renewal cron and
  no "the certificate expired on a Sunday". The broker's API-key gate is four
  lines and can answer with a proper NGSI-LD `ProblemDetails` body, which a
  consumer's error handling will not choke on. The whole proxy is one file
  with environment substitution built in.
  The honest trade-off: nginx is more widely operated and more tunable, and
  plenty of organisations standardise on it. Nothing in the platform depends
  on Caddy-specific behaviour, so swapping is a contained change — one config
  file and one compose service, keeping the contract in `proxy/Caddyfile`'s
  header. Treat this as a preference with a reason, not as a constraint.
- **A query that maps nothing says so.** In the `sql` gate's `long` layout the
  keys of `columns` are the *values* of `property_column`, so a fleet needs one
  entry per tag. Adding a device without its mapping made the gate return
  nothing, which is indistinguishable from an upstream with no data, and cost
  half an hour of looking at the wrong thing during the verification above. The
  gate now logs when rows came back and none of their tags are mapped, and
  names the tags it saw. Prefer a loud no-op to a quiet one, everywhere.
- **The CLI constructs only the gate it was asked about.** `load_gates`
  constructs every entry in the file, so one disabled example with unset secrets
  made `datagates check my_gate` fail on somebody else's gate -- in the shipped
  configuration, which is mostly disabled examples. `datagates list` reports the
  ones that cannot be built instead of dying on them, which makes it the
  quickest configuration check available.
- **One vocabulary, derived rather than repeated.** `controlledProperty` names,
  their units, their kind and their aggregation rule live in `core/vocab.py`;
  the counter guard's cumulative set is computed from it. Three hand-maintained
  copies of that set had already drifted apart, and a registry nothing enforces
  is an appendix, so `tests/test_vocab.py` holds the gates, the docs and the
  consumer client to it. The client keeps a deliberate copy, because a consumer
  copies that one file and it must not import the platform; a test compares
  them.
- **Semantic models are export projections, never stored contexts.** SAREF and
  QUDT mappings live beside the vocabulary and are applied on the way out.
  Attaching such a context to what Orion-LD stores expands attribute names and
  breaks every consumer's `q=` filter, which is the same trap as the Smart Data
  Model contexts above. Time series stay in InfluxDB: an RDF export carries the
  model and a pointer, not observations as triples.
- **Env-only settings, YAML-only gate config, `${VAR}` for secrets.** No
  `config.ini` files, ever.
- **Apache-2.0** as the usual choice for Horizon Europe software outputs.
  Change the LICENSE file if the consortium decides otherwise.
- **Gates only read.** Nothing in this platform writes to an upstream system.
  That is the first question the owner of a control network asks, and it is
  worth keeping true.

## 4. Roadmap

Ordered by what unblocks what, not by size. Each item is small enough for one
session.

### How to work through it

One item at a time. An item is done when all five are true:

1. `ruff check .` and `pytest` are green;
2. **it has a test that would fail if the change were reverted** — a unit test
   on a recorded payload, or an assertion in the `integration` job. "It ran on
   my machine once" is not a test and does not survive the next refactor;
3. every document that mentions it is updated (`docs/gates.md`,
   `docs/legacy-systems.md`, `README.md`, `CHANGELOG.md`);
4. CI is green on `main`;
5. the box is ticked here, with the date, and anything surprising is written
   into §2 or §3. The next person reads this file, not the diff.

Two items carry the rest: **M1's first run**, because everything below it is
theory until the stack has started once, and **M1's `integration` job**,
because it is what lets every later item be verified by a machine instead of
by somebody remembering to check.

### M1 — First run (do before publishing)

- [x] `scripts/verify_platform.py`, reading the source list from
      `config/gates.yaml`: connectivity, model, bridge, freshness and query
      checks, with `--json` for automation. Done 2026-09-22, run against no
      live stack yet.
- [x] `docker compose up -d --build` on a clean machine; fix what breaks; record the fixes in this file. Done 2026-09-22; the seven fixes are in §3 under "What the first run cost".
- [x] Run the full weather cycle and paste the summary entity, a Flux result and the `verify_platform.py` output into `README.md` as "verified output". Done 2026-09-22, and the `prices` gate and the proxy with it.
- [x] Create the repository and push `main`. Done 2026-09-22:
      `github.com/sstamatopoulos/datawise-os-integration`, **private** until the
      first run below is verified, topics set, URLs in `pyproject.toml` and
      `CITATION.cff` pointed at it. Making it public is one setting; it is
      deliberately not yet done, because the two items above have not been.
- [x] `integration` CI job: build and start the stack in Actions, run the
      weather cycle, assert the summary carries `lastReadingAt` and points at
      its own InfluxDB measurement, count the points, run
      `verify_platform.py`, and probe the proxy's API-key gate from outside.
      Done 2026-09-22 as `scripts/integration_test.sh` plus the `integration`
      job; the script runs the same way on a laptop after
      `docker compose up -d`. Writing it found three more Windows-portability
      bugs (see §3), which is fitting for a harness whose job is to find what
      unit tests cannot.
- [x] Work through the rest of `docs/publishing-checklist.md`. Done 2026-09-23:
      description and topics, issues and discussions, squash-only merges,
      Dependabot alerts and automated security fixes, a read-only Actions token,
      a named security contact with a reporting process and scope in
      `SECURITY.md`, and authorship in `CITATION.cff`. Branch protection and
      private vulnerability reporting are refused on a private repository, so
      they are applied immediately after the switch to public. Still open in
      `CITATION.cff`: the funding programme and grant number, and the Zenodo
      DOI.
- [x] Confirm CI is green on `main`. Done 2026-09-22: all five jobs
      (`lint-and-test` 3.12 and 3.13, `packaging`, `gate-extras`, `dags-load`).
      The first run failed on `dags-load` and the fix is recorded in §3.
- [x] Make the repository public and tag `v0.2.0`. Done 2026-09-23, with all six
      CI jobs green, the stack verified end to end, and a pre-publication scan
      that found a pilot supplier's hostname in a core docstring and its product
      name in an example variable — both now neutral (§3).

### M2 — Operability

- [x] Rate limiting and retry policy per gate as YAML options
      (`min_interval_s`, `max_attempts`) handled by the framework, so gates
      stop re-implementing it (`core/httpclient.py`). Done 2026-09-22.
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
- [ ] Pulse-counter style "readers with inputs" as a documented `http_json` recipe (the production indoor-air sub-meter mapping), or a small gate if placeholders are not enough.
- [ ] Real-upstream verification pass over the new gates (§2 item 1), starting with whichever the pilots have.

### M4 — Consumers

- [ ] Port the production data-access guide (query recipes, 8 example scripts, benchmark) into `docs/consumer-guide.md` and `clients/python/examples/`, generalised: no pilot names, sources taken from `gates.yaml`.
- [ ] Pagination in `datagates_client.py` (Orion caps `limit` at 1000).
- [ ] A Grafana provisioning folder (InfluxDB datasource + one dashboard per gate type) as an optional compose profile.
- [ ] A `datagates` CLI (`python -m datagates list|check <key>|fetch <key> --dry-run`) so a gate can be tried without Airflow. **Do this early**: it turns a twenty-minute Airflow round trip into a two-second loop, which changes how fast every remaining item can be worked on, and it makes the playbook in `docs/legacy-systems.md` executable instead of aspirational. **Tested by** unit tests on the argument parsing plus one `integration` assertion that `check` exits non-zero for a gate whose upstream is unreachable.

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

### M7 — Ecosystem (proposed)

Turning a repository that has gates into a place where other people's gates
can live. Nothing here is needed by this deployment; all of it is needed by
the second and third deployment.

- [ ] **Entry-point discovery**: a `datagates.gates` entry-point group, so
      `pip install datagates-knx` makes `type: knx` resolvable while
      `module:Class` keeps working. This is the change that separates "a repo
      with gates" from "an ecosystem of gates": today a third-party gate is a
      path in someone's YAML, which nobody discovers, versions or trusts.
      **Tested by** a fixture wheel built in `tests/` whose gate appears in
      the catalogue and builds its DAGs.
- [ ] **`datagates.testing`**: export `doc_of` and an
      `assert_gate_contract(gate)` that checks what the built-ins are held to
      — discover() returns devices, every property has a unit, fetch()
      respects its window, no driver imported at module level. **Done when**
      the built-in suite uses it too, so it cannot rot, and an out-of-tree
      gate can import it.
- [ ] **PyPI and GHCR**: `pip install datagates` and a published image, so
      trying this does not start with a five-minute build. **Tested by** a
      release workflow that installs the artefact it just published into a
      clean container and resolves every built-in type.
- [ ] **`.devcontainer/` and a three-minute quickstart**: the credential-free
      weather and price gates, running in a Codespace with nothing installed
      locally. **Done when** somebody who has never seen FIWARE sees data.
- [ ] **`docs/tested-against.md`**: gate x product x firmware x who verified
      it and when. **Done when** every gate has a row, "not yet" included —
      the honesty is the point, and it turns every issue somebody opens into
      a durable entry instead of a closed thread.

### M8 — Model depth (proposed)

- [ ] **Building topology** (Brick or RealEstateCore): today there is a flat
      list of Devices with an optional `refBuilding`. A consumer cannot ask
      "every temperature on the second floor" without a naming convention,
      which is the thing naming conventions are worst at. **Tested by** a
      fixture building in the `integration` job and a query in the consumer
      guide.
- [x] **A controlled property vocabulary** (`core/vocab.py`,
      `docs/properties.md`). Done 2026-09-22, and it turned out to be the
      prerequisite for everything else here: the gates had been emitting
      `energy` and `energyConsumption`, `humidity` and `relativeHumidity`, and
      a `gasIndex` that meant what `energy` means, while three files kept
      separate copies of "which properties are running totals", each promising
      in a comment to stay in step. The registry carries unit, kind
      (instant / delta / register / state) and aggregation rule per property;
      the counter guard now derives its set from it; `tests/test_vocab.py`
      fails if a gate emits anything unregistered.
- [ ] **SAREF4BLDG / SAREF4ENER mapping**: the table exists in `core/vocab.py`
      with 7 of 35 properties confirmed against SAREF core and the other 28
      marked `candidate` or `none`. **Verify them against saref.etsi.org and
      qudt.org before any export is published** — a wrong IRI validates, which
      is worse than a missing one. Then the exporter (`scripts/export_saref.py`)
      is a small job: the model as Turtle or JSON-LD, with `influxMeasurement`
      as the pointer to the series, and no observations as triples.
- [ ] **Unit conversion helpers**: the UN/CEFACT codes are already a de facto
      vocabulary here; a small converter (Wh<->kWh, degC<->K, m3<->l) would stop
      every consumer writing their own, wrongly, in a notebook.

### M9 — Scale and cost (proposed)

- [ ] **Downsampling and per-gate retention** in InfluxDB. Raw data at five
      years is a default nobody chose; a five-minute series for a decade is a
      bill somebody eventually does choose to stop paying.
- [ ] **An Airflow pool per upstream**, so one slow API cannot starve the
      scheduler for everything else. The gates already declare
      `min_interval_s`; the pool is the other half.
- [ ] **A published benchmark**: devices, points per day, per worker, measured
      by the `integration` job against a synthetic gate. People ask this
      before adopting, and "it depends" loses to a number.

### M10 — Edge (proposed)

- [ ] **A slim runner for sites with no reliable link**: run the gates
      locally, spool, ship when the link returns. It generalises
      `scripts/mqtt_spool.py`, and it is the difference between a pilot
      building with a domestic broadband line being in the dataset or not.
      **Done when** a site can lose connectivity for a day and lose nothing.

### Non-goals (reopen only with a reason written down)

- **Writing to upstream systems.** "Gates only read" is why the owner of a
  control network says yes, and it is the first thing they ask. If setpoint
  control is ever wanted it needs its own mechanism, its own authorisation
  story and its own entry in §3 — not a quiet `write()` on the Gate contract.
- **Being a dashboard.** Grafana provisioning (M4) is a convenience; the
  platform's job ends at two well-documented stores.
- **Supporting every protocol speculatively.** M6 exists so that a gate is
  written when a site needs it, with that site's payload in a test.

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
- Use the shared machinery (`core.fieldmap`, `core.httpclient`, `core.timeparse`,
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
`dags/dq_weekly_report.py` are the sources for M2. Its indoor-air, mobile-operator, DSO metering and PV-inverter connectors show real-world API quirks worth turning into gate
docstrings, and the pilots' Modbus and BACnet point lists are the fastest way
to verify the new field-protocol gates against something real.
