"""
HTTP CSV gate — the export endpoint every legacy web application has.

Before there were APIs there were "download the report" links, and they
are still there: `?format=csv&from=…&to=…` on a meter portal, a district
heating operator's customer page, an old ERP report server. The response
is a CSV body, so this gate is the csv_drop reader with an HTTP fetch in
front of it.

    - key: heat_portal
      type: http_csv
      options:
        url: "https://portal.example.org/export"
        params: {meter: "{device_id}", from: "{start_date}", to: "{end_date}", format: csv}
        auth: {type: basic, username: "${PORTAL_USER}", password: "${PORTAL_PASSWORD}"}
        delimiter: ";"
        encoding: cp1257
        skip_rows: 2                     # a title block before the header
        timestamp_column: Laiks
        timestamp_format: "%d.%m.%Y %H:%M"
        timezone: Europe/Riga
        columns:
          "Patēriņš, kWh": {property: energyConsumption, unit: KWH}
        devices:
          - {id: "5432", name: Heat meter 5432}

Same placeholders as http_json: {device_id}, {start_iso}, {end_iso},
{start_date}, {end_date}, {start_ms}, {end_ms}, {start_s}, {end_s}, and
any extra key of the device entry.

Two habits of these endpoints:

- **They answer 200 with an HTML login page** when the session has
  expired, and a CSV parser will happily read that as one long row. The
  gate refuses a body that starts with `<` and says so, rather than
  writing nothing and reporting success.
- **They are encoded in whatever the server's locale was in 2008.** If
  values or headers look mangled, set `encoding:` (cp1257, cp1252,
  iso-8859-1 are the usual suspects) instead of fighting the header names.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime
from typing import Any

from datagates.core.fieldmap import FieldMap
from datagates.core.httpclient import client_for, query_auth
from datagates.core.timeparse import iso_z, parse_stamp, zone_of
from datagates.gates.base import DeviceSpec, Gate, Sample
from datagates.gates.http_json import RESERVED, window_context


class HttpCsvGate(Gate):
    type_name = "http_csv"
    speaks = "CSV export endpoints of portals and report servers"
    default_schedule = "0 * * * *"
    default_max_window_days = 31

    def __init__(self, config):
        super().__init__(config)
        self.url = str(self.required("url"))
        self.method = str(self.option("method", "GET")).upper()
        self.params: dict[str, Any] = dict(self.option("params", {}))
        self.delimiter = str(self.option("delimiter", ","))
        self.quotechar = str(self.option("quotechar", '"'))
        self.encoding = str(self.option("encoding", "utf-8-sig"))
        self.skip_rows = int(self.option("skip_rows", 0))
        self.header: list[str] | None = self.option("header")
        self.ts_col = str(self.option("timestamp_column", "timestamp"))
        self.ts_fmt = str(self.option("timestamp_format", "iso"))
        self.tz = zone_of(self.option("timezone"))
        self.device_col = self.option("device_column")
        self.columns = FieldMap(self.option("columns"), gate_key=self.key, block="columns")
        self.cumulative = self.columns.cumulative           # type: ignore[misc]
        self.entries = self.devices_option()
        self._extra_keys = sorted({k for d in self.entries for k in d} - RESERVED)
        self.device_attrs = ("httpDeviceId", *self._extra_keys)   # type: ignore[misc]
        self._client = None

    def discover(self) -> list[DeviceSpec]:
        return [DeviceSpec(
            urn=self.urn(d["id"]), name=str(d.get("name") or d["id"]),
            properties=self.columns.properties, ref_building=d.get("ref_building"),
            category=list(d.get("category", ["meter"])),
            attrs={"httpDeviceId": str(d["id"]), **{k: d[k] for k in self._extra_keys if k in d}},
        ) for d in self.entries]

    @property
    def client(self):
        if self._client is None:
            self._client = client_for(self)
        return self._client

    def _download(self, url: str, params: dict[str, Any]) -> str:
        response = self.client.request(self.method, url, params=params)
        response.encoding = self.encoding
        body = response.text
        if body.lstrip()[:1] == "<":
            raise RuntimeError(f"{self.key}: {url} answered markup, not CSV — the session or the "
                               f"credentials are probably no longer valid")
        return body

    def _rows(self, body: str) -> list[dict[str, Any]]:
        stream = io.StringIO(body)
        for _ in range(self.skip_rows):
            stream.readline()
        reader = csv.DictReader(stream, fieldnames=self.header, delimiter=self.delimiter,
                                quotechar=self.quotechar)
        return [{k: v for k, v in row.items() if isinstance(k, str)} for row in reader]

    def fetch(self, device: dict[str, Any], start: datetime, end: datetime) -> list[Sample]:
        ctx = {"device_id": device.get("httpDeviceId"), **window_context(start, end),
               **{k: device.get(k) for k in self._extra_keys}}
        params = {k: str(v).format(**ctx) for k, v in self.params.items()}
        params.update(query_auth(self.options))
        body = self._download(self.url.format(**ctx), params)
        wanted = str(device.get("httpDeviceId") or "")
        out: list[Sample] = []
        for row in self._rows(body):
            if self.device_col and str(row.get(self.device_col, "")).strip() != wanted:
                continue
            dt = parse_stamp(row.get(self.ts_col), self.ts_fmt, self.tz)
            if dt is None or not (start < dt <= end):
                continue
            out.extend(self.columns.samples(device["urn"], row, iso_z(dt)))
        return out
