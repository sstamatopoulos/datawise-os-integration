"""
HTTP JSON gate — any REST endpoint that returns records with a timestamp
and numeric fields, described in YAML rather than code.

    - key: mesh_air
      type: http_json
      options:
        url: "https://api.example.org/device/{device_id}/sensor-data"
        params: {fromDate: "{start_date}", toDate: "{end_date}"}
        auth: {type: bearer, token: "${MESH_API_KEY}"}
        layout: columns            # "records" (list of objects) or "columns" (parallel arrays)
        records_path: ""           # dotted path to the list / object, "" = the body
        time_field: timestamp
        time_format: iso           # iso | epoch_s | epoch_ms | a strptime format
        fields:
          nco2:            {property: co2,         unit: "59"}
          temperatureReal: {property: temperature, unit: CEL}
          humidity:        {property: relativeHumidity, unit: P1}
        devices:
          - id: 25667
            name: Room 120B
            ref_building: urn:ngsi-ld:Building:...

Placeholders in url, params and body: {device_id}, {start_iso}, {end_iso},
{start_date}, {end_date}, {start_ms}, {end_ms}, {start_s}, {end_s}, and any
key of the device entry (e.g. {building_id}). Secrets come in through
${ENV_VAR}.

This is the gate to reach for first when an upstream has a REST API. A
dedicated gate type is only worth writing when the API needs a login
dance, pages in a way this one cannot express, or answers in something
other than JSON (then see http_xml and http_csv).

Paging, for the many legacy APIs that cap a response at N rows:

        paginate: {style: page, param: page, size_param: limit, size: 1000, start: 1, max_pages: 50}
        paginate: {style: offset, param: offset, size_param: limit, size: 1000}

Paging stops at the first empty page, at a short page, or at max_pages,
whichever comes first.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from datagates.core.fieldmap import FieldMap
from datagates.core.httpclient import client_for, query_auth
from datagates.core.timeparse import iso_z, parse_stamp
from datagates.gates.base import DeviceSpec, Gate, Sample

RESERVED = {"id", "name", "ref_building", "category"}


def dig(obj: Any, path: str) -> Any:
    """Follow a dotted path into nested dicts and lists."""
    for part in [p for p in (path or "").split(".") if p]:
        if isinstance(obj, dict):
            obj = obj.get(part)
        elif isinstance(obj, list) and part.isdigit():
            obj = obj[int(part)] if int(part) < len(obj) else None
        else:
            return None
    return obj


def window_context(start: datetime, end: datetime) -> dict[str, Any]:
    """The time placeholders every HTTP-ish gate offers."""
    return {"start_iso": iso_z(start), "end_iso": iso_z(end),
            "start_date": start.date().isoformat(), "end_date": end.date().isoformat(),
            "start_ms": int(start.timestamp() * 1000), "end_ms": int(end.timestamp() * 1000),
            "start_s": int(start.timestamp()), "end_s": int(end.timestamp())}


class HttpJsonGate(Gate):
    type_name = "http_json"
    speaks = "any REST / JSON API"
    default_schedule = "*/30 * * * *"
    default_max_window_days = 30

    def __init__(self, config):
        super().__init__(config)
        self.url = str(self.required("url"))
        self.method = str(self.option("method", "GET")).upper()
        self.params: dict[str, Any] = dict(self.option("params", {}))
        self.body: dict[str, Any] | None = self.option("body")
        self.layout = str(self.option("layout", "records"))
        self.records_path = str(self.option("records_path", ""))
        self.time_field = str(self.option("time_field", "timestamp"))
        self.time_format = str(self.option("time_format", "iso"))
        self.fields = FieldMap(self.option("fields"), gate_key=self.key, block="fields")
        self.cumulative = self.fields.cumulative             # type: ignore[misc]
        self.paginate: dict[str, Any] = dict(self.option("paginate") or {})
        self.entries = self.devices_option()
        self._extra_keys = sorted({k for d in self.entries for k in d} - RESERVED)
        self.device_attrs = ("httpDeviceId", *self._extra_keys)   # type: ignore[misc]
        self._client = None

    # -- registration ------------------------------------------------------
    def discover(self) -> list[DeviceSpec]:
        return [DeviceSpec(
            urn=self.urn(d["id"]), name=str(d.get("name") or d["id"]),
            properties=self.fields.properties,
            ref_building=d.get("ref_building"), category=list(d.get("category", ["sensor"])),
            attrs={"httpDeviceId": str(d["id"]), **{k: d[k] for k in self._extra_keys if k in d}},
        ) for d in self.entries]

    # -- upstream ----------------------------------------------------------
    @property
    def client(self):
        if self._client is None:
            self._client = client_for(self)
        return self._client

    def _request(self, url: str, params: dict[str, Any], body: Any = None) -> Any:
        kwargs: dict[str, Any] = {"params": params}
        if body is not None:
            kwargs["json"] = body
        client = self.client
        return client._json(client.request(self.method, url, **kwargs))

    def _pages(self, url: str, params: dict[str, Any], body: Any) -> list[Any]:
        if not self.paginate:
            return [self._request(url, params, body)]
        style = str(self.paginate.get("style", "page"))
        param = str(self.paginate.get("param", "page" if style == "page" else "offset"))
        size = int(self.paginate.get("size", 1000))
        size_param = self.paginate.get("size_param")
        cursor = int(self.paginate.get("start", 1 if style == "page" else 0))
        out: list[Any] = []
        for _ in range(int(self.paginate.get("max_pages", 50))):
            page_params = {**params, param: cursor}
            if size_param:
                page_params[str(size_param)] = size
            payload = self._request(url, page_params, body)
            rows = self._rows(payload)
            if not rows:
                break
            out.append(payload)
            if len(rows) < size:
                break
            cursor += 1 if style == "page" else size
        return out

    def _rows(self, payload: Any) -> list[Any]:
        root = dig(payload, self.records_path) if self.records_path else payload
        if not root:
            return []
        if self.layout == "columns":
            times = root.get(self.time_field) or []
            return [{self.time_field: t,
                     **{f.source: (root.get(f.source) or [None] * len(times))[i] for f in self.fields}}
                    for i, t in enumerate(times)]
        return list(root) if isinstance(root, list) else [root]

    def fetch(self, device: dict[str, Any], start: datetime, end: datetime) -> list[Sample]:
        ctx = {"device_id": device.get("httpDeviceId"), **window_context(start, end),
               **{k: device.get(k) for k in self._extra_keys}}
        url = self.url.format(**ctx)
        params = {k: str(v).format(**ctx) for k, v in self.params.items()}
        params.update(query_auth(self.options))
        body = self._format(self.body, ctx) if self.body else None
        out: list[Sample] = []
        for payload in self._pages(url, params, body):
            for row in self._rows(payload):
                if not isinstance(row, dict):
                    continue
                dt = parse_stamp(dig(row, self.time_field), self.time_format)
                if dt is None or not (start < dt <= end):
                    continue
                flat = {f.source: dig(row, f.source) for f in self.fields}
                out.extend(self.fields.samples(device["urn"], flat, iso_z(dt)))
        return out

    @classmethod
    def _format(cls, value: Any, ctx: dict[str, Any]) -> Any:
        if isinstance(value, str):
            return value.format(**ctx)
        if isinstance(value, list):
            return [cls._format(v, ctx) for v in value]
        if isinstance(value, dict):
            return {k: cls._format(v, ctx) for k, v in value.items()}
        return value
