# Gate reference

Twenty-four built-in gate types, every option of each. If you are starting from
"we have this old system, what do we use?", read
[legacy-systems.md](legacy-systems.md) first; it maps products to gates. If no
built-in type fits, [adding-a-gate.md](adding-a-gate.md) is two methods long.

Every gate lives in `config/gates.yaml`, and that file ships a working,
disabled example of each type. Copy the example, not the table.

- [Common keys](#common-keys)
- [Blocks every gate shares](#blocks-every-gate-shares): fields, timestamps, auth, placeholders
- [Three kinds of gate](#three-kinds-of-gate): windowed, rolling, polling
- [The catalogue](#the-catalogue)
- Field protocols: [modbus](#modbus) · [bacnet](#bacnet) · [opcua](#opcua) · [s7](#s7) · [snmp](#snmp)
- Files: [csv_drop](#csv_drop) · [excel_drop](#excel_drop) · [xml_drop](#xml_drop) · [remote_drop](#remote_drop)
- Web: [http_json](#http_json) · [http_xml](#http_xml) · [http_csv](#http_csv) · [obix](#obix) · [zabbix](#zabbix) · [thingsboard](#thingsboard) · [mqtt](#mqtt)
- Stores: [sql](#sql) · [influx_source](#influx_source) · [prometheus](#prometheus) · [ngsi_ld](#ngsi_ld) · [ngsi_v2](#ngsi_v2)
- Weather and markets: [open_meteo](#open_meteo) · [nordpool](#nordpool) · [entsoe](#entsoe)
- [Unit codes](#unit-codes)

## Common keys

| Key | Meaning | Default |
|---|---|---|
| `key` | DAG id prefix and identity namespace; `[a-z][a-z0-9_]*` | required |
| `type` | built-in type name or `module.path:ClassName` | required |
| `enabled` | build its DAGs | `true` |
| `schedule` | cron for `<key>_run`; for polling gates this *is* the sampling rate | per type |
| `source` | `source` attribute on entities | the type name |
| `data_provider` | `dataProvider` attribute on entities | the key |
| `bucket` | InfluxDB bucket (created if missing) | `INFLUX_DEFAULT_BUCKET` |
| `history_start` | earliest date the backfill walks to | per type |
| `live_lookback_days` | a Device with no watermark pulls this much | 7 |
| `max_window_days` | longest span per upstream request | per type |
| `max_attempts` | tries per upstream request before the task fails | 4 |
| `min_interval_s` | minimum delay between requests to this upstream | 0 |
| `tags` | extra Airflow tags | `[]` |
| `options` | type-specific, below; `${VAR}` and `${VAR:-default}` expand from the environment | |

`max_attempts` and `min_interval_s` are applied by the framework
(`core/http.py`), which retries only transient failures — connection reset,
timeout, 429, 5xx — and honours `Retry-After`. A 401 or 404 fails the task
immediately, because it is a configuration error and retrying it just
exhausts the upstream's rate limit. Set `min_interval_s` for any upstream
that counts requests per second; ENTSO-E defaults to 0.5 for that reason.

## Blocks every gate shares

### `fields:` / `columns:`

Whatever the upstream calls a value, and what it becomes here. The file gates
call the block `columns:` because that is what it is; everything else calls it
`fields:`. The semantics are identical (`core/fieldmap.py`).

```yaml
fields:
  T_ROOM_1: {property: temperature, unit: CEL, scale: 0.1}
  E_TOT:    {property: energy, unit: KWH, cumulative: true}
  CO2:      {property: co2, unit: "59", invalid: [-9999], min: 0, max: 5000}
  RH:       relativeHumidity          # shorthand: just the property, unit C62
```

| Key | Meaning |
|---|---|
| `property` | the controlledProperty, and therefore one summary entity and one InfluxDB series |
| `unit` | UN/CEFACT code, see [below](#unit-codes) |
| `scale`, `offset` | stored value is `raw * scale + offset`; SNMP and Modbus values nearly always need one |
| `cumulative` | a running total: the [counter guard](data-model.md) protects it and consumers difference it |
| `invalid` | values that mean "no reading" (`-9999`, `32767`); they are dropped, not stored |
| `min`, `max` | readings outside the range are dropped |

A sentinel stored as a value ruins every average computed downstream, which is
why `invalid:` exists at all. Decimal commas, thousands separators, `n/a`,
booleans, NaN and Inf are handled without configuration.

Per-gate extras live in the same entry: `type`, `table`, `word_order` (modbus),
`db`, `start` (s7), `bacnet_property` (bacnet).

### Timestamps

`timestamp_format` (or `time_format`) takes `iso` (the default, any offset
accepted), `epoch_s`, `epoch_ms`, `epoch_us`, or a `strptime` format such as
`"%d.%m.%Y %H:%M"`. `timezone:` (IANA name, default UTC) is the zone a *naive*
timestamp is in; an aware one keeps its own offset. Everything is stored in
UTC at second precision.

Getting `timezone:` wrong shifts a year of history by an hour, twice, and the
error is invisible until somebody compares two sources. Most legacy exports
are local time with no offset.

### `auth:`

```yaml
auth: {type: basic,  username: "${USER}", password: "${PASS}"}
auth: {type: bearer, token: "${TOKEN}"}
auth: {type: header, name: X-API-Key, value: "${KEY}"}
auth: {type: query,  name: apikey, value: "${KEY}"}
```

Available in every HTTP gate (`http_json`, `http_xml`, `http_csv`, `obix`,
`zabbix`, `prometheus`, `ngsi_ld`, `ngsi_v2`, `influx_source`). `headers:` is
still there for anything unusual. Credentials belong in `.env` and are
referenced as `${VAR}`; nothing secret goes in the YAML.

### Placeholders

In `url`, `params`, `body` and PromQL: `{device_id}`, `{start_iso}`,
`{end_iso}`, `{start_date}`, `{end_date}`, `{start_ms}`, `{end_ms}`,
`{start_s}`, `{end_s}`, plus every extra key of the device entry (so
`{building_id}` works if the entry has one). In SQL they are bind parameters
with the same names (`:device_id`, `:start`, `:end`, `:start_s`, …) — never
string formatting, because a YAML file must not be able to inject SQL.

## Three kinds of gate

| Kind | What the run DAG does | Backfill | Types |
|---|---|---|---|
| **windowed** | fetch (watermark, now] per device, in `max_window_days` slices | cursor walk or stateless | most gates |
| **rolling** | one `fetch(now, now)` per run; the gate chooses its window and everything returned is stored | none or stateless | forecasts, market prices, `mqtt` |
| **polling** | read the present value and stamp it | none: the upstream has no past | `modbus`, `bacnet`, `s7`, `snmp`, `opcua` and `obix` without history |

For polling gates the schedule is the sampling rate of the resulting series,
nothing else decides it, and a missed run is a hole no backfill can fill.
Timestamps are aligned to `align_s` (60 s by default) so that an Airflow
retry overwrites the point it already wrote instead of adding a second one.

Two gates change kind with an option: `opcua` with `history: true` and
`ngsi_v2` with `history: quantumleap|sth` become windowed and grow a backfill
DAG, because then the upstream does have a past. That is usually the fastest
way to get a legacy plant's history in.

## The catalogue

| Type | Speaks to | History | Needs |
|---|---|---|---|
| [`modbus`](#modbus) | Modbus/TCP and RTU-over-TCP: PLCs, meters, inverters, gateways | poll | `pymodbus` |
| [`bacnet`](#bacnet) | BACnet/IP building management systems | poll | `BAC0`, host networking |
| [`opcua`](#opcua) | OPC UA servers, SCADA, OPC Classic wrappers | poll or server historian | `asyncua` |
| [`s7`](#s7) | Siemens S7-300/400/1200/1500 | poll | `python-snap7` + native library |
| [`snmp`](#snmp) | UPS, PDUs, generators, network-attached meters | poll | `pysnmp` |
| [`csv_drop`](#csv_drop) | CSV/TSV files in a directory | full | — |
| [`excel_drop`](#excel_drop) | `.xlsx` workbooks | full | `openpyxl` |
| [`xml_drop`](#xml_drop) | XML meter exports, billing files | full | — |
| [`remote_drop`](#remote_drop) | SFTP / FTPS / FTP servers holding files | full | `paramiko` for SFTP |
| [`http_json`](#http_json) | any REST/JSON API | per API | — |
| [`http_xml`](#http_xml) | SOAP and plain-XML web services | per API | — |
| [`http_csv`](#http_csv) | CSV export endpoints of portals | per API | — |
| [`obix`](#obix) | Tridium Niagara AX/N4 and other oBIX servers | station histories | — |
| [`zabbix`](#zabbix) | Zabbix monitoring servers | until housekeeping | — |
| [`thingsboard`](#thingsboard) | ThingsBoard | full | — |
| [`mqtt`](#mqtt) | MQTT brokers, LoRaWAN network servers | none (stream) | `paho-mqtt` |
| [`sql`](#sql) | SQL Server, Oracle, MySQL, PostgreSQL, ODBC historians | full | `SQLAlchemy` + driver |
| [`influx_source`](#influx_source) | another InfluxDB, 1.x or 2.x | full | — |
| [`prometheus`](#prometheus) | Prometheus / VictoriaMetrics | its retention | — |
| [`ngsi_ld`](#ngsi_ld) | another NGSI-LD broker | temporal API | — |
| [`ngsi_v2`](#ngsi_v2) | FIWARE NGSI-v2, QuantumLeap, STH-Comet | per component | — |
| [`open_meteo`](#open_meteo) | Open-Meteo forecast and ERA5 | 1940 onward | — |
| [`nordpool`](#nordpool) | Nord Pool day-ahead prices | full | — |
| [`entsoe`](#entsoe) | ENTSO-E Transparency Platform | full | free token |

"Needs" is an optional Python package from `requirements-gates.txt`, installed
in the image by default. A missing one costs you that gate type only: the
drivers are imported inside the method that uses them, and CI proves the DAG
bag still builds without any of them.

---

## Field protocols

### modbus

Registers of a PLC, meter, inverter or serial gateway.

| Option | Meaning |
|---|---|
| `devices[]` | `id`, `name`, `host`, `port` (502), `unit` (1), `framer` (`socket` or `rtu`), `base`, `word_order`, `byte_order`, `ref_building`, `lat`/`lon`, `fields` |
| `base` | the register numbering the device's manual uses (`40001`, `40000`, `1`); it is subtracted, so the YAML can quote the manual |
| `align_s` | timestamp alignment, default 60 s |
| `timeout` | seconds per request, default 10 |
| field `table` | `holding` (default, FC3), `input` (FC4), `coil` (FC1), `discrete` (FC2) |
| field `type` | `int16`, `uint16`, `int32`, `uint32`, `int64`, `uint64`, `float32`, `float64`, `bool`, `string:<n>` |
| field `word_order` | `big` (default) or `little` — see below |
| field key | the register address; `"40020.3"` reads bit 3 of register 40020 |

**Word order is the single most common cause of nonsense readings.** Modbus
fixes the byte order inside a word but says nothing about the order of words
within a 32-bit value, and vendors split evenly. A counter that reads
plausibly but ~65 000 times too large, or as noise, is this. Try
`word_order: little`.

One request per field, deliberately: a block read is faster but one
unreadable register in the block fails the whole block, and legacy devices
scatter holes through their maps. Set `min_interval_s` if a device needs
pacing. An unreadable register is logged and skipped, so one dead point does
not cost the device its sample.

### bacnet

Points of a building management system: Desigo, Metasys, Trend, Delta,
Schneider, Honeywell.

| Option | Meaning |
|---|---|
| `bind` | this host's address on the BACnet network, CIDR form (`10.0.20.5/24`) |
| `bacnet_property` | BACnet property to read, default `presentValue` |
| `devices[]` | `id`, `name`, `address` (`10.0.20.41`, or `2001:12` for MS/TP behind a router), `ref_building`, `fields` |
| field key | `objectType:instance`, e.g. `analogInput:1`, `analogValue:12`, `binaryInput:3` |

Binary and multi-state points answer with text (`active`, `inactive`), which is
mapped to 1/0. A point that is out of service or unreliable still answers,
usually with its last good value or a sentinel: use `invalid:`, there is no
other way to tell.

**Deployment is the hard part.** BACnet relies on broadcasts, so the worker
needs a real address on the BACnet network: host networking or a routed
macvlan. A bridged container cannot receive them. This is the one gate whose
deployment is not "just another container".

### opcua

Nodes of an OPC UA server: PLCs, SCADA, historians, OPC Classic behind a
wrapper.

| Option | Meaning |
|---|---|
| `endpoint` | `opc.tcp://host:4840`; a device entry may override it |
| `username`, `password` | optional |
| `security` | asyncua security string, e.g. `Basic256Sha256,SignAndEncrypt,cert.der,key.pem` |
| `history` | `false` (poll) or `true` (read the server's historian: watermark + cursor backfill) |
| `max_values` | values per history call, default 1000 |
| `devices[]` | `id`, `name`, `endpoint`, `ref_building`, `fields` |
| field key | node id in string form: `ns=2;i=1002`, `ns=3;s=Tag.Name` |

With `history: true`, a node that does not historise answers
`BadHistoryOperationUnsupported` for that node only; it is logged and the rest
continue. Servers cap `read_raw_history` (often at 1000 values), so
`max_window_days` defaults to 1 here: a window has to fit in one answer or
its tail is silently lost.

### s7

Data blocks of a Siemens S7 PLC — district heating substations, pump
stations, air handling plant.

| Option | Meaning |
|---|---|
| `devices[]` | `id`, `name`, `host`, `rack` (0), `slot` (1 for S7-1200/1500, 2 for most S7-300/400), `ref_building`, `fields` |
| field `area` | `db` (default), `merker`, `input`, `output` |
| field `db`, `start` | data block number and byte offset, as the PLC's symbol table shows (`DB10.DBD8` is `db: 10, start: 8, type: float32`) |
| field `type` | as modbus; everything is big-endian |
| field `bit` | for `type: bool`, which bit of the byte |

Three things cost a day each: the **slot number** (a wrong one fails exactly
like a firewall); **optimised block access must be off** in TIA Portal and
PUT/GET communication enabled, both changes to the PLC program, so they need
the plant's automation contractor; and small CPUs accept only two or three
connections in total, so a gate polling every minute can lock out the
engineer trying to connect. Poll as slowly as the data allows, and say so in
the handover.

### snmp

OIDs of anything with a management agent.

| Option | Meaning |
|---|---|
| `version` | `1`, `2c` (default), `3` |
| `community` | v1/v2c community |
| `user`, `auth`, `privacy` | v3: `auth: {protocol: SHA, key: ...}`, `privacy: {protocol: AES, key: ...}` |
| `devices[]` | `id`, `name`, `host`, `port` (161), `ref_building`, `fields` |
| field key | numeric OID with its instance suffix (`.0` for a scalar) |

Symbolic OIDs are deliberately not supported: they need the vendor's MIB on
the worker, and a MIB present in testing and missing in production fails in a
way that looks like a network fault. SNMP has no decimals, so `scale: 0.1` is
the rule rather than the exception — the MIB's DISPLAY-HINT says which. 32-bit
counters wrap, so mark them `cumulative: true` and let the guard catch it.

---

## Files

All four share the options of `csv_drop` below, plus their own. Files are
never moved or deleted: every run re-reads what matches the pattern and the
Device watermark keeps out rows already stored, which makes these gates
idempotent, their backfill `stateless`, and a partner's corrected re-upload
land as a correction. A file that cannot be read is logged and skipped.

### csv_drop

| Option | Meaning |
|---|---|
| `directory` | default `/opt/airflow/drop/<key>` (compose mounts `./drop`) |
| `pattern`, `recursive` | default `*.csv`, `false` |
| `max_file_age_days` | skip files older than this, for directories that have grown |
| `delimiter`, `quotechar`, `encoding` | default `,`, `"`, `utf-8-sig`; `cp1257`/`cp1252`/`latin-1` for files from a Windows-era system |
| `skip_rows` | lines to drop before the header (title blocks) |
| `header` | column names, for files that have no header row |
| `timestamp_column`, `timestamp_format`, `timezone` | see [timestamps](#timestamps) |
| `device_column` | which row belongs to which device; omit for single-device files |
| `columns` | see [fields](#fields--columns) |
| `devices[]` | `id` (the value in `device_column`), `name`, `ref_building`, any extra attributes |

### excel_drop

| Option | Meaning |
|---|---|
| `sheet` | name, or index (`0` = first) |
| `header_row` | 1-based row holding the column names |
| `forward_fill` | columns whose merged cells should repeat downwards |

Date-formatted cells arrive as real `datetime` objects and are used as they
are, ignoring `timestamp_format`; a text column still needs one. Merged cells
read as empty in every cell but the first, hence `forward_fill`. The workbook
is opened with `data_only=True`, so formula cells give the result Excel last
cached — a file written by a tool that never opened Excel has none, and those
cells read empty. `.xls` is not supported; ask for `.xlsx`.

### xml_drop

| Option | Meaning |
|---|---|
| `row_path` | elements to iterate, e.g. `.//MeterReading` or a bare tag name |
| `device_selector` | selector for the device id within a row |
| `timestamp_selector` | selector for the timestamp within a row |
| `keep_namespaces` | `false` by default: tags are matched on local names |
| `columns` | keyed by selector: `Value/@kWh`, `Quality`, `@attr`, `.` |

Selectors are a small path language: `Child` (child text), `@attr`,
`Child/@attr`, `.` (the row element's own text). Namespaces are stripped
because the same vendor's export changes its namespace URI between software
versions, and a configuration written against the old URI then matches
nothing at all, silently.

### remote_drop

Downloads first, then parses with the CSV, Excel or XML reader.

| Option | Meaning |
|---|---|
| `protocol` | `sftp` (default), `ftps`, `ftp` |
| `host`, `port`, `username`, `password`, `key_file` | `key_file` for SFTP key auth |
| `remote_dir` | directory on the server |
| `directory` | the local mirror, which is the record of what was received |
| `format` | `csv`, `excel` or `xml`, plus that reader's own options |
| `delete_after_download` | `false` by default |

A file is downloaded unless the mirror already holds the same name at the same
size, so a corrected re-upload is fetched again. Deleting from a partner's
server is irreversible and often against their retention rules — turn
`delete_after_download` on only when they ask. Plain `ftp` sends the password
in clear text; it is supported because some systems offer nothing else, and
that belongs in the handover in writing.

---

## Web

### http_json

Any REST endpoint returning JSON. The first thing to try for a web API.

| Option | Meaning |
|---|---|
| `url` | with [placeholders](#placeholders) |
| `method`, `params`, `body`, `headers`, `auth` | `params` and `body` take placeholders too |
| `layout` | `records` (list of objects) or `columns` (parallel arrays keyed by field) |
| `records_path` | dotted path to the list or object inside the body |
| `time_field`, `time_format` | dotted paths work: `meta.ts` |
| `fields` | keyed by JSON field, dotted paths allowed |
| `paginate` | `{style: page|offset, param, size_param, size, start, max_pages}` |
| `devices[]` | `id`, `name`, `ref_building`, extra keys usable as placeholders |

Paging stops at the first empty page, at a short page, or at `max_pages`.

### http_xml

SOAP and plain XML over HTTP. No WSDL is read: a request template and a row
path keep working when the vendor regenerates their schema, and what they
return is visible in the YAML.

| Option | Meaning |
|---|---|
| `url`, `method`, `params`, `auth` | as `http_json` |
| `body` | the request document, with placeholders (a SOAP envelope, typically) |
| `soap_action`, `content_type` | `SOAPAction` header and content type |
| `row_path`, `timestamp_selector`, `timestamp_format`, `timezone` | as `xml_drop` |
| `fields` | keyed by selector |

SOAP faults arrive as HTTP 500, so a fault fails the task with the fault text
in the log, which is what should happen.

### http_csv

The "download the report" endpoint of a portal or report server.

Options: `url`, `method`, `params`, `auth`, plus `csv_drop`'s parsing options
(`delimiter`, `encoding`, `skip_rows`, `header`, `timestamp_column`,
`timestamp_format`, `timezone`, `device_column`, `columns`, `devices`).

These endpoints answer HTTP 200 with an HTML login page when the session has
expired, and a CSV parser reads that as one long row; the gate refuses a body
starting with `<` and says why. If values or headers look mangled, set
`encoding:`.

### obix

Tridium Niagara AX/N4 (the JACE in the plant room) and other oBIX servers.
Often the most productive gate to configure in an existing building: the
station already speaks BACnet, LonWorks, Modbus and half a dozen field buses
and has been logging every point for years, and one credential brings all of
it.

| Option | Meaning |
|---|---|
| `base_url` | e.g. `https://jace.example.org/obix` |
| `auth` | basic, with a station user that has HTTP Basic enabled explicitly |
| `history` | `true` (default): `~historyQuery` over the run's window. `false`: read present values on the schedule |
| `limit` | records per history query, default 1000 |
| `timezone` | zone for history records that carry no offset |
| `devices[]` | `id`, `name`, `ref_building`, `fields` |
| field key | with history, the history id under `/obix/histories/` (`<station>/<history>`); without, a point path under `/obix/config/` |

Niagara refuses HTTP Basic silently with a 401 that looks like a wrong
password until the user is configured for it. History timestamps carry the
station's local offset, which is honoured. `~historyQuery` is capped, so keep
`max_window_days` at 1 for minute-interval points.

### zabbix

Items the facility's monitoring server already collects — UPS, CRAC units,
generator fuel, room probes — with years of history.

| Option | Meaning |
|---|---|
| `url` | `https://zabbix.example.org/api_jsonrpc.php` |
| `token` | API token, sent as `Authorization: Bearer` — needs Zabbix 6.0+ |
| `user`, `password` | for older servers: exchanged for a session id per task |
| `limit` | rows per `history.get`, default 50 000 |
| `devices[]` | `id`, `name`, `ref_building`, `fields` |
| field key | the numeric item id (from the item's URL or `item.get`) |

Item *keys* are not accepted: they are unique per host, not globally, and
resolving them at run time breaks when a host is renamed. `history.get` needs
the right type — `0` for floats, `3` for unsigned — and returns nothing at
all, with no error, for the wrong one; the gate asks for floats and retries
as unsigned, like the web interface does. Housekeeping deletes history after
7 to 31 days, so a backfill stops at that horizon, not at `history_start`.

### thingsboard

Telemetry keys of ThingsBoard devices.

| Option | Meaning |
|---|---|
| `base_url` | e.g. `https://tb.example.org/api` |
| `username`, `password` | via `${TB_USERNAME}` / `${TB_PASSWORD}` |
| `page_size` | rows per request, default 10 000 |
| `devices[]` | `id`, `name`, `tb_device_id`, `ref_building`, `keys: {property: {key, unit, cumulative}}` |

### mqtt

A broker's topics, drained on a schedule.

| Option | Meaning |
|---|---|
| `mode` | `collect` (default: connect from the task) or `spool` (read what an always-on subscriber wrote) |
| `host`, `port`, `tls`, `username`, `password` | broker connection |
| `client_id` | **must be stable**: the broker remembers each device's queue by `<client_id>-<device id>` |
| `qos` | default 1 |
| `collect_seconds` | how long each run stays connected, default 30 |
| `spool_dir` | where `scripts/mqtt_spool.py` writes, default `/opt/airflow/drop/<key>-mqtt` |
| `devices[]` | `id`, `name`, `topic` (wildcards allowed), `payload` (`json` or `value`), `time_field`, `fields` |
| field key | dotted path into the JSON payload (`object.co2`) |

In `collect` mode the gate connects with `clean_session: false` at QoS 1, so
the **broker queues messages while the gate is not connected** and delivers
them at the next run. That is what makes a scheduled drain lossless — up to
the broker's queue limit (mosquitto's `max_queued_messages` is 1000).

There is **one session and one subscription per device**, named
`<client_id>-<device id>`, because the run DAG fans out over devices and
parallel tasks sharing a client id would disconnect each other, with the
first to connect receiving and then dropping the others' messages. For busy
topics use `mode: spool` and run `scripts/mqtt_spool.py` as a service: it
routes each message to the spool file of every device whose filter matches,
hands files over by rename, and the gate deletes what it has read, so a
message is stored exactly once per device.

Use `time_field` whenever the payload carries a timestamp: arrival time is
when the broker delivered a queued message, which after a missed run is
hours later than the measurement.

---

## Other stores

### sql

A historian, a billing database, a legacy application's own tables.

| Option | Meaning |
|---|---|
| `dsn` | SQLAlchemy URL, e.g. `mssql+pyodbc://…?driver=ODBC+Driver+18+for+SQL+Server`, `oracle+oracledb://…`, `postgresql+psycopg://…` |
| `query` | your SQL, with bind parameters `:device_id`, `:start`, `:end`, `:start_s`, `:end_s`, `:start_ms`, `:end_ms`, `:limit` |
| `layout` | `wide` (default: one row per timestamp, one column per property) or `long` (one row per reading) |
| `timestamp_column`, `timestamp_format`, `timezone` | which column is time, and in what zone |
| `property_column`, `value_column` | `long` layout only |
| `connect_args` | passed to the driver |
| `columns` | column names (`wide`) or the values of `property_column` (`long`) |
| `devices[]` | `id` is what `:device_id` binds to |

Give the gate an account with SELECT and nothing else, and always filter on
the window and order by time in the query: a backfill over an unbounded query
reads the whole table, repeatedly, and the database administrator notices
before it finishes. Historian columns are usually local time with no offset —
set `timezone:`.

### influx_source

Series from another InfluxDB.

| Option | Meaning |
|---|---|
| `url`, `version` | `1` (InfluxQL) or `2` (Flux) |
| `database`, `retention_policy` | v1 |
| `bucket`, `org`, `token` | v2 |
| `username`, `password` | v1 auth |
| `aggregate_every`, `aggregate_fn` | downsample on the way in (`5m`, `mean`) |
| `devices[]` | `id`, `name`, `measurement`, `tags: {}`, `ref_building`, `fields` |
| field key | the InfluxDB field name |

The gate asks for millisecond precision explicitly (a server default of
nanoseconds overflows naive parsers) and uses `time > a AND time <= b` to
match this platform's half-open window; `>=` on both ends would rewrite the
boundary point forever. Measurement and field names go in double quotes, tag
values in single — a tag value in double quotes is read as a field reference
and matches nothing, which looks exactly like "there is no data".

### prometheus

Short retention there, long retention here.

| Option | Meaning |
|---|---|
| `url` | `http://prometheus:9090` |
| `step` | resolution of the copied series, default `60s` |
| `devices[]` | `id`, `name`, `ref_building`, `fields` |
| field key | a PromQL expression; `{` is written `{{` because placeholders are substituted |

`query_range` refuses more than ~11 000 points per series, which is why
`max_window_days` is 7 at the default step. Aggregate in PromQL (`sum by ()`)
so each expression yields one series; several series would all be written to
the same property. `*_total` counters reset when an exporter restarts: mark
them `cumulative: true`, or copy `rate(...)` instead.

### ngsi_ld

Mirror entities from another NGSI-LD broker (Orion-LD, Scorpio, Stellio)
through its temporal API.

| Option | Meaning |
|---|---|
| `url` | the other broker |
| `auth`, `tenant` | `tenant` becomes `NGSILD-Tenant` |
| `context` | the `@context` sent as a Link header |
| `entity_type` | discover every entity of this type at init, instead of listing devices |
| `last_n` | values per attribute per request, default 1000 |
| `devices[]` | `id` is the remote entity id; `fields` keyed by remote attribute name |

`timerel=between` is inclusive at both ends, so the gate drops the lower
bound to keep the half-open window. The answer is the normalised temporal
representation (a list of `{value, observedAt}` per attribute), which every
broker implements, rather than `temporalValues`, which not all do. If the
mirror comes back empty, the other side stores attribute names expanded by a
Smart Data Model context: send the matching `context:`. That is the same trap
this platform avoids for its own entities — see the decision log in
`ROADMAP.md`.

### ngsi_v2

FIWARE as it was: Orion v2 with QuantumLeap or STH-Comet. Running both and
mirroring one is in practice the cheapest migration path.

| Option | Meaning |
|---|---|
| `url` | the v2 broker |
| `history` | `quantumleap` (default), `sth`, or `none` (poll current values) |
| `history_url` | where that component is |
| `service`, `service_path` | `Fiware-Service` and `Fiware-ServicePath` |
| `devices[]` | `id`, `name`, `entity_type`, `ref_building`, `fields` |

The tenant is two headers and they must match what the entity was created
with, exactly, including the leading slash; a wrong pair returns an empty
list rather than an error, so "no data" usually means "wrong service". STH
stores `recvTime` — when the broker received the notification, not when the
observation happened — so prefer QuantumLeap where both exist and treat an
STH backfill as approximate.

---

## Weather and markets

### open_meteo

Hourly weather per location. No key.

| Option | Meaning |
|---|---|
| `mode` | `forecast` (rolling 48 h, every 6 h, no backfill) or `observed` (ERA5 reanalysis, daily, stateless backfill, last 7 days re-fetched) |
| `forecast_hours` | 1 to 384, default 48 |
| `locations[]` | `id`, `name`, `lat`, `lon`, optional `ref_building` |

Devices are typed `WeatherForecastLocation` / `WeatherObservedLocation` and
carry coordinates as a GeoProperty. Properties: temperature,
feelsLikeTemperature, atmosphericPressure, relativeHumidity (0 to 1),
windSpeed, windDirection, gustSpeed, precipitation, and for forecasts also
visibility, precipitationProbability (0 to 1), uVIndexMax. Wind is requested
in m/s; the archive serves no visibility, precipitation probability or UV
index, whatever the documentation says.

### nordpool

Day-ahead electricity prices per delivery area, one Device per area, no
building.

| Option | Meaning |
|---|---|
| `areas` | e.g. `[LV, EE, LT]` |
| `currency` | default `EUR` |

Property `dayAheadPrice`, unit `EUR_MWH` (no UN/CEFACT code exists for
currency per energy). Samples are stamped at the start of the delivery
period. Runs at 12:00 and 14:00 UTC by default; the second run catches a late
publication. Market time unit is 15 minutes since 2025-10.

### entsoe

The European market's own data, free with a registered token.

| Option | Meaning |
|---|---|
| `token` | from the Transparency Platform |
| `document_type` | `A44` day-ahead price → `dayAheadPrice` (EUR_MWH); `A65` total load → `electricityLoad` (MAW); `A75` generation → `electricityGeneration` (MAW) |
| `areas[]` | `id`, `name`, `eic` (the EIC area code, e.g. `10YLV-1001A00074`) |
| `url` | override the API endpoint |

`A44` is about tomorrow, so that gate is rolling: it asks for today and
tomorrow every run. Timestamps are `yyyyMMddHHmm` in UTC with no separators.
There are no timestamps in the answer at all: a `Period` has an interval, a
resolution and `Point`s with a `position` starting at 1, and **missing
positions mean "unchanged since the last one"** — filling that gap is the
difference between 96 points a day and 40. An empty answer is HTTP 400 with
"No matching data found" in the body, which the gate treats as "not published
yet" rather than as a failure. The token allows 400 requests per minute and
the platform bans for an hour on abuse, so `min_interval_s` defaults to 0.5.

---

## Unit codes

UN/CEFACT Recommendation 20 where one exists: `CEL` °C, `KWH`, `KWT`, `WTT` W,
`MAW` MW, `VLT` V, `AMP` A, `MTQ` m³, `MQH` m³/h, `LTR` l, `P1` %, `59` ppm,
`A97` hPa, `MTR` m, `MTS` m/s, `DD` degrees, `MMT` mm, `2N` dB, `C62`
dimensionless, `EUR_MWH` (not a UN/CEFACT code: there is none for currency per
energy). Mark running totals `cumulative: true`; consumers difference them,
and the counter guard protects them.
