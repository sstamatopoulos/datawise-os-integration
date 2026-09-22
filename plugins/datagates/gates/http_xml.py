"""
HTTP XML / SOAP gate — the enterprise web service of the previous era.

Billing systems, utility back offices, older SCADA middleware and most
things sold with the word "platform" before 2015 answer SOAP or plain
XML over HTTP. They are perfectly usable; they just need an envelope and
a path language instead of a JSON key.

Plain XML over GET:

    - key: heat_readings
      type: http_xml
      options:
        url: "https://ws.example.org/readings"
        params: {meter: "{device_id}", from: "{start_iso}", to: "{end_iso}"}
        auth: {type: basic, username: "${WS_USER}", password: "${WS_PASSWORD}"}
        row_path: ".//Reading"
        timestamp_selector: "@time"
        fields:
          "Value":   {property: energyConsumption, unit: KWH}
          "Flow":    {property: flow, unit: MQH}
        devices:
          - {id: "5432", name: Heat meter 5432}

SOAP, which is the same thing with an envelope and a header:

        method: POST
        content_type: "text/xml; charset=utf-8"
        soap_action: "urn:GetMeterReadings"
        body: |
          <soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">
            <soapenv:Body>
              <GetMeterReadings>
                <meterId>{device_id}</meterId>
                <from>{start_iso}</from>
                <to>{end_iso}</to>
              </GetMeterReadings>
            </soapenv:Body>
          </soapenv:Envelope>
        row_path: ".//MeterReading"

Practicalities:

- **No WSDL is read.** Generating a client from the WSDL is the textbook
  approach and it breaks whenever the vendor regenerates the schema; a
  request template and a row path keep working, and what they return is
  visible in the YAML.
- **SOAP faults arrive with HTTP 500**, so a fault is a task failure with
  the fault string in the log, which is what it should be.
- Namespaces are stripped before matching; see core.xmlrows for why.
- The selectors are the same small path language as xml_drop: `Child`,
  `@attr`, `Child/@attr`, `.`.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from datagates.core.fieldmap import FieldMap
from datagates.core.httpclient import client_for, query_auth
from datagates.core.timeparse import iso_z, parse_stamp, zone_of
from datagates.core.xmlrows import parse_xml, pick, select
from datagates.gates.base import DeviceSpec, Gate, Sample
from datagates.gates.http_json import RESERVED, window_context


class HttpXmlGate(Gate):
    type_name = "http_xml"
    speaks = "SOAP and plain-XML web services"
    default_schedule = "0 * * * *"
    default_max_window_days = 31

    def __init__(self, config):
        super().__init__(config)
        self.url = str(self.required("url"))
        self.method = str(self.option("method", "GET")).upper()
        self.params: dict[str, Any] = dict(self.option("params", {}))
        self.body_template = self.option("body")
        self.soap_action = str(self.option("soap_action", ""))
        self.content_type = str(self.option("content_type", "text/xml; charset=utf-8"))
        self.row_path = str(self.option("row_path", ""))
        self.keep_namespaces = bool(self.option("keep_namespaces", False))
        self.timestamp_selector = str(self.option("timestamp_selector", "timestamp"))
        self.ts_fmt = str(self.option("timestamp_format", "iso"))
        self.tz = zone_of(self.option("timezone"))
        self.fields = FieldMap(self.option("fields"), gate_key=self.key, block="fields")
        self.cumulative = self.fields.cumulative            # type: ignore[misc]
        self.entries = self.devices_option()
        self._extra_keys = sorted({k for d in self.entries for k in d} - RESERVED)
        self.device_attrs = ("httpDeviceId", *self._extra_keys)   # type: ignore[misc]
        self._client = None

    def discover(self) -> list[DeviceSpec]:
        return [DeviceSpec(
            urn=self.urn(d["id"]), name=str(d.get("name") or d["id"]),
            properties=self.fields.properties, ref_building=d.get("ref_building"),
            category=list(d.get("category", ["meter"])),
            attrs={"httpDeviceId": str(d["id"]), **{k: d[k] for k in self._extra_keys if k in d}},
        ) for d in self.entries]

    @property
    def client(self):
        if self._client is None:
            headers = {"Content-Type": self.content_type} if self.body_template else {}
            if self.soap_action:
                headers["SOAPAction"] = f'"{self.soap_action}"'
            self._client = client_for(self, headers=headers)
        return self._client

    def _request(self, url: str, params: dict[str, Any], body: str | None) -> str:
        kwargs: dict[str, Any] = {"params": params}
        if body is not None:
            kwargs["data"] = body.encode("utf-8")
        response = self.client.request(self.method, url, **kwargs)
        response.encoding = response.encoding or "utf-8"
        return response.text

    def fetch(self, device: dict[str, Any], start: datetime, end: datetime) -> list[Sample]:
        ctx = {"device_id": device.get("httpDeviceId"), **window_context(start, end),
               **{k: device.get(k) for k in self._extra_keys}}
        params = {k: str(v).format(**ctx) for k, v in self.params.items()}
        params.update(query_auth(self.options))
        body = str(self.body_template).format(**ctx) if self.body_template else None
        document = self._request(self.url.format(**ctx), params, body)
        return self.samples(document, device["urn"], start, end)

    def samples(self, document: str, urn: str, start: datetime, end: datetime) -> list[Sample]:
        root = parse_xml(document, keep_namespaces=self.keep_namespaces)
        out: list[Sample] = []
        for element in select(root, self.row_path):
            dt = parse_stamp(pick(element, self.timestamp_selector), self.ts_fmt, self.tz)
            if dt is None or not (start < dt <= end):
                continue
            row = {f.source: pick(element, f.source) for f in self.fields}
            out.extend(self.fields.samples(urn, row, iso_z(dt)))
        return out
