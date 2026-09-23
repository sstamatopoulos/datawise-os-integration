# datawise-os-integration

Configurable **data gates** for building and energy telemetry: Apache Airflow
pipelines that land any upstream source — including the legacy systems that
hold most of the data worth having — in a FIWARE **Orion-LD** context broker
(registry and latest state) and **InfluxDB** (time series), following the
FIWARE Smart Data Models. Add a source by writing a YAML entry, or a small
Python class for an API nobody has wrapped yet.

Extracted from the data integration platform of the DATAWiSE project, where
the same code ingests indoor-air sensors, utility meters, photovoltaics,
market prices and weather for three pilot sites.

```
   upstream systems                  gates (Airflow)                 the two stores
 ┌──────────────────────────┐   ┌───────────────────────────┐   ┌───────────────────────────┐
 │ BACnet · Modbus · S7     │   │ <key>_init                │   │ Orion-LD (NGSI-LD)        │
 │ OPC UA · SNMP · oBIX     │   │ <key>_run     (scheduled) │   │  Building, Device,        │
 │ SQL historians · Zabbix  │──▶│ <key>_backfill            │──▶│  summary DeviceMeasurement│
 │ CSV/XLSX/XML drops, SFTP │   │                           │   ├───────────────────────────┤
 │ REST · SOAP · MQTT       │   │  watermarks · idempotent   │   │ InfluxDB                  │
 │ NGSI-v2 · InfluxDB 1.x   │   │  writes · counter guard    │   │  one series per summary   │
 │ Open-Meteo · ENTSO-E     │   │  summary refresh           │   │                           │
 └──────────────────────────┘   └───────────────────────────┘   └───────────────────────────┘
```

## What you get

- **24 built-in gate types**, weighted towards the systems already installed on
  a site: BACnet, Modbus, Siemens S7, OPC UA (including server historians),
  SNMP, Tridium Niagara/oBIX, SQL historians, Zabbix, file drops over
  SFTP/FTP, SOAP, NGSI-v2 FIWARE, and the modern ones too. The full
  [catalogue](#the-gate-catalogue) is below.
- **A full, secured stack in one `docker compose up`**: Airflow 3, Orion-LD
  with MongoDB, InfluxDB 2, behind a TLS proxy with access control (see
  [Security](#security)).
- **One data model** for everything: a `Device` per upstream device, one
  summary `DeviceMeasurement` per measured property with its latest value and
  rolling 24 h statistics, and the full history in InfluxDB under the
  summary's id as measurement name. Consumers discover in Orion and read
  history in InfluxDB with no lookup table.
- **The operational lessons built in**: per-device watermarks, idempotent
  writes keyed by time, a guard that rejects readings a cumulative meter
  cannot have produced, backfills that walk history with resumable state,
  summaries that only ever move forward, retries that fire on the failures
  worth retrying and not on a misconfiguration.
- **A Python client** for consumers (`clients/python/datagates_client.py`) and
  a verification script (`scripts/verify_platform.py`) that checks a live
  deployment the way a consumer would.

## Quick start

```bash
./scripts/gen-secrets.sh        # writes .env with random secrets (PUBLIC_HOST=localhost)
docker compose up -d --build
```

Everything is behind one TLS reverse proxy: Airflow at https://localhost
(login printed by the script), the broker at https://orion.localhost
(`X-API-Key: $ORION_API_KEY` from `.env`), InfluxDB at https://influx.localhost.
For a server set `PUBLIC_HOST=your.domain ACME_EMAIL=you@example.org` before
running the script and certificates come from Let's Encrypt. See
[SECURITY.md](SECURITY.md) for what protects what.

The shipped `config/gates.yaml` enables three gates that need no credentials.
In Airflow, trigger `weather_forecast_init`, then unpause `weather_forecast_run`.
Within a minute:

```bash
curl -s 'http://localhost:1026/ngsi-ld/v1/entities?type=DeviceMeasurement&q=entityKind==%22summary%22&limit=3' \
     -H 'Accept: application/ld+json' | python -m json.tool
```

shows summaries with `lastReadingAt`, `lastReadingValue` and `influxMeasurement`,
and that measurement name queried in InfluxDB returns the 48-hour forecast.
`weather_observed_backfill` (trigger once) loads hourly reanalysis from 2024;
`prices_run` publishes tomorrow's prices every afternoon.

Then check the deployment:

```bash
python scripts/verify_platform.py            # connectivity, model, bridge, freshness, queries
```

## Verified output

From a first run on 2026-09-22 (Docker Desktop, Airflow 3.0.6, Orion-LD 1.5.1,
InfluxDB 2.7). `weather_forecast_init` then `weather_forecast_run`, and the
summary entity for one of the eleven properties:

```json
{
  "id": "urn:ngsi-ld:DeviceMeasurement:8a921cde-c4dc-589c-8054-dd1f0ea3d532",
  "type": "DeviceMeasurement",
  "entityKind":         {"type": "Property", "value": "summary"},
  "controlledProperty": {"type": "Property", "value": "temperature"},
  "unitCode":           {"type": "Property", "value": "CEL"},
  "refDevice":          {"type": "Relationship",
                         "object": "urn:ngsi-ld:WeatherForecastLocation:weather_forecast-riga"},
  "lastReadingAt":      {"type": "Property", "value": "2026-09-24T11:00:00Z"},
  "lastReadingValue":   {"type": "Property", "value": 12.2},
  "rolling24hMin":      {"type": "Property", "value": 15.2},
  "rolling24hMax":      {"type": "Property", "value": 15.2},
  "rolling24hMean":     {"type": "Property", "value": 15.2},
  "rolling24hCount":    {"type": "Property", "value": 1},
  "influxBucket":       {"type": "Property", "value": "telemetry"},
  "influxMeasurement":  {"type": "Property",
                         "value": "urn:ngsi-ld:DeviceMeasurement:8a921cde-c4dc-589c-8054-dd1f0ea3d532"}
}
```

`influxMeasurement` is the entity's own id — that is the convention, and it is
why a consumer needs no lookup table. Dropping it into Flux (note the `stop:`,
because a forecast is in the future):

```
$ influx query 'from(bucket:"telemetry")
    |> range(start: -3d, stop: 3d)
    |> filter(fn: (r) => r._measurement == "urn:ngsi-ld:DeviceMeasurement:8a921cde-c4dc-589c-8054-dd1f0ea3d532"
                      and r._field == "value")
    |> count()'

_field   _measurement                                                        _value
value    urn:ngsi-ld:DeviceMeasurement:8a921cde-c4dc-589c-8054-dd1f0ea3d532      48
```

48 points, the 48-hour forecast horizon. `weather_observed_backfill` over
August onward loaded 1249 hourly ERA5 points per property. And the deployment
check:

```
$ python scripts/verify_platform.py --gate weather_forecast
-- connectivity ----------------------------------------------
[  ok  ] Orion-LD answers: version 1.5.1
[  ok  ] InfluxDB is healthy
[  ok  ] bucket 'telemetry' exists
-- model -----------------------------------------------------
[  ok  ] weather_forecast: 1 configured device(s) registered: 1 in the broker
[  ok  ] weather_forecast: Riga location is a GeoProperty
-- bridge ----------------------------------------------------
[  ok  ] weather_forecast: temperature points at its own measurement
[  ok  ] weather_forecast: temperature has points in InfluxDB: 49 point(s)
     ... one pair per property ...
-- freshness -------------------------------------------------
[  ok  ] weather_forecast: summaries are within 18:00:00: 11 fresh, 0 stale, 0 never written (every 6 h)
-- queries ---------------------------------------------------
[  ok  ] type filter (WeatherForecastLocation) returns entities: 1 entity/entities
[  ok  ] summary filter returns entities: 1 entity/entities
[  ok  ] gate filter returns entities: 1 entity/entities
[  ok  ] attribute names are not expanded: short names only, as documented

31 ok, 0 warning(s), 0 failure(s)
```

Getting there took seven fixes that no unit test could have found; they are
listed in `ROADMAP.md` §2 and explained in §3. The gates that talk to
credentialed and field-protocol upstreams are still unverified against real
hardware — the same section says so.

## Configure a gate

```yaml
# config/gates.yaml
gates:
  - key: indoor_air                 # DAG prefix: indoor_air_init / _run / _backfill
    type: http_json
    schedule: "*/30 * * * *"
    source: mesh.example
    options:
      url: "https://api.example.org/device/{device_id}/sensor-data"
      params: {fromDate: "{start_date}", toDate: "{end_date}"}
      auth: {type: bearer, token: "${INDOOR_AIR_API_KEY}"}     # from .env
      layout: columns
      time_field: timestamp
      fields:
        co2:         {property: co2,         unit: "59"}
        temperature: {property: temperature, unit: CEL}
      devices:
        - {id: 25667, name: Room 120B, ref_building: "urn:ngsi-ld:Building:..."}
```

A legacy one looks no different — the plant room instead of an API:

```yaml
  - key: substation
    type: modbus
    schedule: "*/5 * * * *"         # for a polling gate this IS the sampling rate
    options:
      devices:
        - id: hs1
          name: Heat substation 1
          host: 10.0.40.12
          base: 40001               # the register numbering the manual uses
          fields:
            "40001": {property: temperature, unit: CEL, type: int16, scale: 0.1}
            "40010": {property: energy, unit: KWH, type: uint32, word_order: little, cumulative: true}
```

`config/gates.yaml` ships a working, disabled example of **every** gate type;
copy the one you need. Every option is documented in
[docs/gates.md](docs/gates.md), every property and unit a gate may emit in
[docs/properties.md](docs/properties.md), the entity model and the conventions
consumers rely on in [docs/data-model.md](docs/data-model.md), and writing a
new gate type is two methods: [docs/adding-a-gate.md](docs/adding-a-gate.md).

## The gate catalogue

Start from [docs/legacy-systems.md](docs/legacy-systems.md) if you know the
system but not the gate; it maps products (Desigo, Metasys, Niagara, Wonderware,
Kamstrup, SolarEdge, ChirpStack, …) to the entry to copy.

| Group | Types |
|---|---|
| **Field protocols** | `modbus`, `bacnet`, `opcua`, `s7`, `snmp` |
| **Files** | `csv_drop`, `excel_drop`, `xml_drop`, `remote_drop` (SFTP/FTPS/FTP) |
| **Web and messaging** | `http_json`, `http_xml` (incl. SOAP), `http_csv`, `obix` (Niagara), `zabbix`, `thingsboard`, `mqtt` |
| **Other stores** | `sql` (SQL Server, Oracle, MySQL, PostgreSQL, ODBC historians), `influx_source` (1.x and 2.x), `prometheus`, `ngsi_ld`, `ngsi_v2` (Orion v2, QuantumLeap, STH-Comet) |
| **Weather and markets** | `open_meteo`, `nordpool`, `entsoe` |

Gates that need a driver (`pymodbus`, `BAC0`, `asyncua`, `pysnmp`,
`python-snap7`, `openpyxl`, `paramiko`, `paho-mqtt`, `SQLAlchemy`) import it
inside the method that uses it, so a missing package costs you that one gate
type and nothing else. `requirements-gates.txt` has them all and the image
installs it by default.

## Repository layout

```
config/gates.yaml            which gates run, with what options — and an example of every type
dags/datagates_dags.py       builds the DAGs from the config (nothing else to write)
plugins/datagates/
  core/                      Orion-LD registry, InfluxDB writer, summaries, counter guard,
                             field maps, HTTP policy, timestamps, binary decoding, cadence
  gates/                     the Gate contract, the YAML loader, the 24 built-in types,
                             the PollingGate and FileDropGate bases
  dags/factory.py            init / run / backfill DAG templates
clients/python/              consumer client
scripts/gen-secrets.sh       creates .env with random secrets
scripts/verify_platform.py   checks a live deployment: model, bridge, freshness, queries
scripts/integration_test.sh  runs the weather cycle against a running stack and asserts it (CI runs this)
scripts/mqtt_spool.py        the always-on subscriber for mqtt gates in spool mode
tests/                       unit tests, no network
docker-compose.yml           Caddy (TLS, access control) + Airflow + Orion-LD + MongoDB + InfluxDB
proxy/Caddyfile              the proxy's routes and the broker's API-key check
```

## Roadmap and handover

`ROADMAP.md` records the state of the project, the decisions behind it and the
ordered next steps; `CONTRIBUTING.md` is how to send a gate; `CLAUDE.md` briefs
an AI worker. Start there.

## Development

```bash
pip install -r requirements-dev.txt
ruff check . && pytest
```

CI runs that on Python 3.12 and 3.13, checks that `pip install .` works, that
every optional driver installs, and that every gate in the catalogue builds its
DAGs under Airflow.

## Security

Nothing but the Caddy proxy publishes a port. TLS everywhere, an API key on
the broker, tokens on InfluxDB, logins on Airflow, passwords on MongoDB and
Postgres, all generated into a mode-600 `.env`. Details, the threat model
and what remains your job: [SECURITY.md](SECURITY.md). A developer override
(`docker-compose.dev.yml`) publishes the backends on loopback without TLS;
never use it on a server. Gates only ever read from upstream systems; if you
are pointing one at somebody's control network, read
[docs/legacy-systems.md](docs/legacy-systems.md#working-on-an-ot-network)
first.

## License

Apache License 2.0. See [LICENSE](LICENSE).
