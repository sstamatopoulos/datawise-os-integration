"""
InfluxDB source gate — series from another InfluxDB, 1.x or 2.x.

Two situations, both common: an InfluxDB 1.8 that a building's own
dashboard has been filling since 2019 and nobody wants to touch, and a
second platform whose data has to be federated into this one. Either way
the job is to copy series in, not to migrate a server.

    - key: legacy_influx
      type: influx_source
      options:
        url: "http://10.0.60.4:8086"
        version: 1                         # 1 = InfluxQL, 2 = Flux
        database: building                 # v1: database (and optional retention policy)
        username: ${LEGACY_INFLUX_USER}    # v1 auth
        password: ${LEGACY_INFLUX_PASSWORD}
        # version: 2 -> bucket: telemetry, token: ${LEGACY_INFLUX_TOKEN}, org: acme
        devices:
          - id: ahu1
            name: AHU 1
            measurement: hvac
            tags: {device: ahu1}
            ref_building: urn:ngsi-ld:Building:...
            fields:
              temp_supply: {property: temperature, unit: CEL}
              kwh_total:   {property: energy, unit: KWH, cumulative: true}

The field key is the InfluxDB field name; `tags:` narrows the series to
one device. Raw points are copied, not aggregated: set
`aggregate_every: 5m` (with `aggregate_fn: mean`) when the source is
finer than this platform needs.

Things that bite when copying between InfluxDBs:

- **Time precision.** v1 answers in RFC3339 when asked with
  `epoch=` omitted, and in the requested epoch otherwise; the gate asks
  for milliseconds explicitly, because a server-default precision of
  nanoseconds overflows a naive parser.
- **v1 exclusivity.** InfluxQL `WHERE time > a AND time <= b` matches the
  framework's half-open window; `>=` on both ends duplicates the boundary
  point on every run and then rewrites it forever.
- **Identifiers are quoted differently.** Measurement and field names go
  in double quotes, tag values in single quotes. A tag value in double
  quotes is read as a field reference and silently matches nothing, which
  looks exactly like "there is no data".
- **Chunked answers.** Large windows come back chunked; the gate keeps
  `max_window_days` at 7 rather than parsing chunk boundaries.
"""
from __future__ import annotations

import csv
import io
from datetime import UTC, datetime
from typing import Any

from datagates.core.fieldmap import FieldMap
from datagates.core.httpclient import HttpClient, client_for
from datagates.core.timeparse import iso_z, parse_stamp
from datagates.gates.base import DeviceSpec, Gate, Sample

FLUX = """
from(bucket: "{bucket}")
  |> range(start: {start}, stop: {stop})
  |> filter(fn: (r) => r._measurement == "{measurement}")
{filters}{aggregate}
  |> keep(columns: ["_time", "_field", "_value"])
"""


def escape(value: str) -> str:
    """Quote a value for an InfluxQL or Flux string literal."""
    return str(value).replace("\\", "\\\\").replace("'", "\\'").replace('"', '\\"')


class InfluxSourceGate(Gate):
    type_name = "influx_source"
    speaks = "another InfluxDB (1.x InfluxQL or 2.x Flux)"
    default_schedule = "*/30 * * * *"
    default_max_window_days = 7
    default_history_start = "2019-01-01"
    device_attrs = ("influxDeviceId",)

    def __init__(self, config):
        super().__init__(config)
        self.url = str(self.required("url")).rstrip("/")
        self.version = int(self.option("version", 2))
        self.database = str(self.option("database", ""))
        self.retention_policy = str(self.option("retention_policy", ""))
        self.bucket_in = str(self.option("bucket", ""))
        self.org = str(self.option("org", ""))
        self.token = str(self.option("token", ""))
        self.username = str(self.option("username", ""))
        self.password = str(self.option("password", ""))
        self.aggregate_every = str(self.option("aggregate_every", ""))
        self.aggregate_fn = str(self.option("aggregate_fn", "mean"))
        if self.version == 1 and not self.database:
            raise ValueError(f"{self.key}: InfluxDB 1.x needs options.database")
        if self.version == 2 and not self.bucket_in:
            raise ValueError(f"{self.key}: InfluxDB 2.x needs options.bucket")
        self.entries = self.devices_option()
        self.maps = {str(e["id"]): FieldMap(e.get("fields") or self.option("fields"),
                                            gate_key=self.key, block="fields") for e in self.entries}
        if not self.maps:
            raise ValueError(f"{self.key}: options.devices must list at least one device")
        self.cumulative = frozenset().union(*(m.cumulative for m in self.maps.values()))  # type: ignore[misc]
        self._client: HttpClient | None = None

    def discover(self) -> list[DeviceSpec]:
        return [DeviceSpec(
            urn=self.urn(entry["id"]), name=str(entry.get("name") or entry["id"]),
            properties=self.maps[str(entry["id"])].properties,
            ref_building=entry.get("ref_building"),
            category=list(entry.get("category", ["sensor"])),
            attrs={"influxDeviceId": str(entry["id"])},
        ) for entry in self.entries]

    @property
    def client(self) -> HttpClient:
        if self._client is None:
            headers = {"Authorization": f"Token {self.token}"} if self.version == 2 and self.token else {}
            self._client = client_for(self, headers=headers)
            if self.version == 1 and self.username:
                self._client.session.auth = (self.username, self.password)
        return self._client

    def _entry_of(self, device: dict[str, Any]) -> dict[str, Any] | None:
        device_id = str(device.get("influxDeviceId") or "")
        return next((e for e in self.entries if str(e["id"]) == device_id), None)

    # -- InfluxDB 1.x ------------------------------------------------------
    def _influxql(self, entry: dict[str, Any], fields: FieldMap, start: datetime, end: datetime) -> str:
        measurement = str(entry.get("measurement") or entry["id"])
        source = f'"{self.retention_policy}"."{measurement}"' if self.retention_policy else f'"{measurement}"'
        selected = ", ".join(f'"{escape(f.source)}"' for f in fields)
        where = [f"time > {int(start.timestamp() * 1000)}ms", f"time <= {int(end.timestamp() * 1000)}ms"]
        where += [f"\"{escape(k)}\" = '{escape(v)}'" for k, v in (entry.get("tags") or {}).items()]
        return f"SELECT {selected} FROM {source} WHERE {' AND '.join(where)}"

    def _query_v1(self, query: str) -> list[dict[str, Any]]:
        body = self.client.get_json(f"{self.url}/query", params={"db": self.database, "q": query,
                                                                 "epoch": "ms"})
        rows: list[dict[str, Any]] = []
        for result in (body or {}).get("results", []):
            for series in result.get("series", []) or []:
                columns = series.get("columns", [])
                for values in series.get("values", []) or []:
                    rows.append(dict(zip(columns, values, strict=False)))
        return rows

    # -- InfluxDB 2.x ------------------------------------------------------
    def _flux(self, entry: dict[str, Any], fields: FieldMap, start: datetime, end: datetime) -> str:
        filters = "".join(f'  |> filter(fn: (r) => r["{escape(k)}"] == "{escape(v)}")\n'
                          for k, v in (entry.get("tags") or {}).items())
        wanted = " or ".join(f'r._field == "{escape(f.source)}"' for f in fields)
        filters += f"  |> filter(fn: (r) => {wanted})\n"
        aggregate = (f'  |> aggregateWindow(every: {self.aggregate_every}, fn: {self.aggregate_fn}, '
                     f"createEmpty: false)\n" if self.aggregate_every else "")
        return FLUX.format(bucket=self.bucket_in, start=iso_z(start), stop=iso_z(end),
                           measurement=escape(str(entry.get("measurement") or entry["id"])),
                           filters=filters, aggregate=aggregate)

    def _query_v2(self, query: str) -> list[dict[str, Any]]:
        response = self.client.request("POST", f"{self.url}/api/v2/query", params={"org": self.org},
                                       headers={"Content-Type": "application/vnd.flux",
                                                "Accept": "application/csv"},
                                       data=query.encode("utf-8"))
        # The answer is *annotated* CSV: every datatype/group/default line
        # starts with "#" and precedes the real header. Handing the whole body
        # to a CSV reader makes the first annotation line the header, and then
        # nothing has a _time column and the copy silently yields nothing.
        body = "\n".join(line for line in response.text.splitlines()
                         if line.strip() and not line.lstrip().startswith("#"))
        rows: list[dict[str, Any]] = []
        for row in csv.DictReader(io.StringIO(body)):
            if not row.get("_time"):
                continue
            rows.append({"time": row["_time"], row.get("_field", "value"): row.get("_value")})
        return rows

    # -- the contract ------------------------------------------------------
    def fetch(self, device: dict[str, Any], start: datetime, end: datetime) -> list[Sample]:
        entry = self._entry_of(device)
        if entry is None:
            return []
        fields = self.maps[str(entry["id"])]
        if self.version == 1:
            rows = self._query_v1(self._influxql(entry, fields, start, end))
        else:
            rows = self._query_v2(self._flux(entry, fields, start, end))
        out: list[Sample] = []
        for row in rows:
            raw_time = row.get("time") or row.get("_time")
            stamp = (datetime.fromtimestamp(raw_time / 1000, UTC) if isinstance(raw_time, int | float)
                     else parse_stamp(raw_time))
            if stamp is None:
                continue
            out.extend(fields.samples(device["urn"], row, iso_z(stamp)))
        return out
