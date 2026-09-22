"""
Prometheus gate — series from the monitoring stack that is already there.

Where a site has containers, IT equipment or a modern PV inverter with an
exporter, Prometheus already scrapes it. Its retention is short by design
(15 days is the default), which makes it a perfect source and a poor
archive: copying its series into InfluxDB gives them the retention the
rest of this platform has.

    - key: dc_power
      type: prometheus
      options:
        url: "http://prometheus.example.org:9090"
        step: 60s                          # resolution of the copied series
        devices:
          - id: rack_a
            name: Rack A
            ref_building: urn:ngsi-ld:Building:...
            fields:
              'sum(pdu_power_watts{rack="A"})':          {property: power, unit: WTT}
              'pdu_energy_kwh_total{rack="A"}':          {property: energy, unit: KWH, cumulative: true}

The field key is a PromQL expression, evaluated through
`/api/v1/query_range`. `{device_id}` and the device entry's other keys
are substituted into it, so one `fields:` block can serve a fleet:

        fields:
          'sum(pdu_power_watts{{rack="{device_id}"}})': {property: power, unit: WTT}

Note the doubled braces: the expression goes through Python's `format`,
so a literal `{` in PromQL is written `{{`.

Details:

- **`query_range` refuses more than 11 000 points per series.** Window
  divided by step must stay under it; with the default 60 s step that is
  about seven days, which is why `max_window_days` is 7 here.
- **A counter is a counter.** `*_total` series reset when the exporter
  restarts. Mark them `cumulative: true` so the guard sees the reset, or
  copy `rate(...)` instead and store a delta.
- **The result is a matrix**, one entry per label set. A query returning
  several series writes them all to the same property, which is rarely
  meant: aggregate in PromQL (`sum by ()`) so each expression yields one
  series.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from datagates.core.fieldmap import FieldMap
from datagates.core.httpclient import client_for
from datagates.core.timeparse import iso_z
from datagates.gates.base import DeviceSpec, Gate, Sample

log = logging.getLogger(__name__)


class PrometheusGate(Gate):
    type_name = "prometheus"
    speaks = "Prometheus / VictoriaMetrics endpoints"
    default_schedule = "*/15 * * * *"
    default_max_window_days = 7
    device_attrs = ("promDeviceId",)

    def __init__(self, config):
        super().__init__(config)
        self.url = str(self.required("url")).rstrip("/")
        self.step = str(self.option("step", "60s"))
        self.entries = self.devices_option()
        shared = self.option("fields") or {}
        self.maps = {str(e["id"]): FieldMap({**shared, **(e.get("fields") or {})},
                                            gate_key=self.key, block="fields") for e in self.entries}
        if not self.maps:
            raise ValueError(f"{self.key}: options.devices must list at least one device")
        self.cumulative = frozenset().union(*(m.cumulative for m in self.maps.values()))  # type: ignore[misc]
        self._client = None

    def discover(self) -> list[DeviceSpec]:
        return [DeviceSpec(
            urn=self.urn(entry["id"]), name=str(entry.get("name") or entry["id"]),
            properties=self.maps[str(entry["id"])].properties,
            ref_building=entry.get("ref_building"),
            category=list(entry.get("category", ["sensor"])),
            attrs={"promDeviceId": str(entry["id"])},
        ) for entry in self.entries]

    @property
    def client(self):
        if self._client is None:
            self._client = client_for(self)
        return self._client

    def _query_range(self, query: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        body = self.client.get_json(f"{self.url}/api/v1/query_range",
                                    params={"query": query, "start": int(start.timestamp()),
                                            "end": int(end.timestamp()), "step": self.step})
        if (body or {}).get("status") != "success":
            raise RuntimeError(f"{self.key}: {(body or {}).get('error', 'query_range failed')}")
        return body.get("data", {}).get("result", []) or []

    def fetch(self, device: dict[str, Any], start: datetime, end: datetime) -> list[Sample]:
        device_id = str(device.get("promDeviceId") or "")
        fields = self.maps.get(device_id)
        entry = next((e for e in self.entries if str(e["id"]) == device_id), None)
        if fields is None or entry is None:
            return []
        context = {"device_id": device_id, **{k: v for k, v in entry.items() if k != "fields"}}
        out: list[Sample] = []
        for field in fields:
            query = str(field.source).format(**context)
            series = self._query_range(query, start, end)
            if len(series) > 1:
                log.warning("%s: %s returned %d series; they all become %s",
                            self.key, query, len(series), field.property)
            for result in series:
                for stamp, raw in result.get("values", []) or []:
                    value = field.convert(raw)
                    when = datetime.fromtimestamp(float(stamp), UTC)
                    if value is not None and start < when <= end:
                        out.append(Sample(device["urn"], field.property, value, iso_z(when)))
        return out
