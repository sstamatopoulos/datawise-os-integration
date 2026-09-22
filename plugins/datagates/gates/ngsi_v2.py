"""
NGSI-v2 gate — the previous generation of FIWARE, still running.

Hundreds of smart-city and smart-building deployments from 2015 to 2021
run Orion (v2) with STH-Comet or QuantumLeap behind it. They are not
going to be migrated; their data is still worth having. This gate reads
the v2 world and lands it in the NGSI-LD model this platform uses, which
is in practice the cheapest possible migration path: run both, mirror
one, switch consumers when ready.

    - key: legacy_fiware
      type: ngsi_v2
      options:
        url: "http://orion-v2.example.org:1026"          # the v2 broker
        history: quantumleap                             # quantumleap | sth | none
        history_url: "http://quantumleap.example.org:8668"
        service: smartcity                               # Fiware-Service header
        service_path: "/buildings"                       # Fiware-ServicePath header
        devices:
          - id: "Sensor:001"
            name: Room 120B sensor
            entity_type: AirQualityObserved
            ref_building: urn:ngsi-ld:Building:...
            fields:
              co2:         {property: co2, unit: "59"}
              temperature: {property: temperature, unit: CEL}

`history: none` reads the broker's current values on the schedule
instead, which is all that is available when neither history component
was deployed — a surprisingly common situation.

What differs from NGSI-LD, and where it hurts:

- **The tenant is two headers, not one.** `Fiware-Service` and
  `Fiware-ServicePath` must match what the entity was created with,
  exactly, including the leading slash; the wrong pair returns an empty
  list rather than an error, so "no data" usually means "wrong service".
- **QuantumLeap and STH answer in different shapes.** QuantumLeap gives
  `{index: [...], attributes: [{attrName, values: [...]}]}`; STH gives
  `contextResponses[0].contextElement.attributes[0].values` with
  `recvTime`. Both are handled; the choice is a YAML key because the two
  are never both deployed.
- **STH stores `recvTime`, not the observation time.** It timestamps when
  the broker received the notification, which drifts from the real
  observation whenever the upstream batches. Prefer QuantumLeap where
  both exist, and treat an STH backfill as approximate.
- **v2 has no @context**; attribute names are plain, which is why the
  mapping here is simpler than the NGSI-LD gate's.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from datagates.core.fieldmap import FieldMap
from datagates.core.httpclient import client_for
from datagates.core.timeparse import iso_z, parse_stamp, stamp_now
from datagates.gates.base import DeviceSpec, Gate, Sample


class NgsiV2Gate(Gate):
    type_name = "ngsi_v2"
    speaks = "FIWARE NGSI-v2 (Orion v2, QuantumLeap, STH-Comet)"
    default_schedule = "*/30 * * * *"
    default_max_window_days = 7
    default_history_start = "2018-01-01"
    device_attrs = ("remoteEntityId", "remoteEntityType")

    def __init__(self, config):
        super().__init__(config)
        self.url = str(self.required("url")).rstrip("/")
        self.history = str(self.option("history", "quantumleap")).lower()
        if self.history not in ("quantumleap", "sth", "none"):
            raise ValueError(f"{self.key}: history must be quantumleap, sth or none")
        self.history_url = str(self.option("history_url", self.url)).rstrip("/")
        self.service = str(self.option("service", ""))
        self.service_path = str(self.option("service_path", ""))
        self.limit = int(self.option("limit", 10000))
        self.entries = self.devices_option()
        shared = self.option("fields") or {}
        self.maps = {str(e["id"]): FieldMap({**shared, **(e.get("fields") or {})},
                                            gate_key=self.key, block="fields") for e in self.entries}
        if not self.maps:
            raise ValueError(f"{self.key}: options.devices must list at least one device")
        self.cumulative = frozenset().union(*(m.cumulative for m in self.maps.values()))  # type: ignore[misc]
        self._client = None

    @property
    def rolling(self) -> bool:                               # type: ignore[override]
        return self.history == "none"

    @property
    def backfill_mode(self) -> str:                          # type: ignore[override]
        return "none" if self.history == "none" else "cursor"

    def discover(self) -> list[DeviceSpec]:
        return [DeviceSpec(
            urn=self.urn(entry["id"]), name=str(entry.get("name") or entry["id"]),
            properties=self.maps[str(entry["id"])].properties,
            ref_building=entry.get("ref_building"),
            category=list(entry.get("category", ["sensor"])),
            attrs={"remoteEntityId": str(entry["id"]),
                   "remoteEntityType": str(entry.get("entity_type", "Thing"))},
        ) for entry in self.entries]

    @property
    def client(self):
        if self._client is None:
            headers = {"Accept": "application/json"}
            if self.service:
                headers["Fiware-Service"] = self.service
            if self.service_path:
                headers["Fiware-ServicePath"] = self.service_path
            self._client = client_for(self, headers=headers)
        return self._client

    # -- the three upstream shapes ------------------------------------------
    def _quantumleap(self, entity_id: str, attributes: list[str], start: datetime,
                     end: datetime) -> list[tuple[str, str, Any]]:
        body = self.client.get_json(f"{self.history_url}/v2/entities/{entity_id}",
                                    params={"attrs": ",".join(attributes), "fromDate": iso_z(start),
                                            "toDate": iso_z(end), "limit": self.limit}) or {}
        index = body.get("index") or []
        out: list[tuple[str, str, Any]] = []
        for attribute in body.get("attributes") or []:
            name = attribute.get("attrName")
            for when, value in zip(index, attribute.get("values") or [], strict=False):
                out.append((name, when, value))
        return out

    def _sth(self, entity_id: str, entity_type: str, attribute: str, start: datetime,
             end: datetime) -> list[tuple[str, str, Any]]:
        body = self.client.get_json(
            f"{self.history_url}/STH/v1/contextEntities/type/{entity_type}/id/{entity_id}"
            f"/attributes/{attribute}",
            params={"dateFrom": iso_z(start), "dateTo": iso_z(end), "hLimit": self.limit, "hOffset": 0}) or {}
        responses = body.get("contextResponses") or []
        if not responses:
            return []
        attributes = responses[0].get("contextElement", {}).get("attributes") or []
        values = attributes[0].get("values") if attributes else []
        return [(attribute, row.get("recvTime"), row.get("attrValue")) for row in values or []]

    def _current(self, entity_id: str, attributes: list[str]) -> list[tuple[str, str, Any]]:
        body = self.client.get_json(f"{self.url}/v2/entities/{entity_id}",
                                    params={"options": "keyValues", "attrs": ",".join(attributes)}) or {}
        now = stamp_now()
        return [(name, now, body[name]) for name in attributes if name in body]

    # -- the contract -------------------------------------------------------
    def fetch(self, device: dict[str, Any], start: datetime, end: datetime) -> list[Sample]:
        entity_id = str(device.get("remoteEntityId") or "")
        fields = self.maps.get(entity_id)
        if fields is None:
            return []
        names = [str(f.source) for f in fields]
        if self.history == "quantumleap":
            rows = self._quantumleap(entity_id, names, start, end)
        elif self.history == "sth":
            entity_type = str(device.get("remoteEntityType") or "Thing")
            rows = [row for name in names for row in self._sth(entity_id, entity_type, name, start, end)]
        else:
            rows = self._current(entity_id, names)
        out: list[Sample] = []
        for name, when, raw in rows:
            field = fields.by_source(str(name))
            stamp = parse_stamp(when)
            if field is None or stamp is None:
                continue
            if self.history != "none" and not (start < stamp <= end):
                continue
            value = field.convert(raw)
            if value is not None:
                out.append(Sample(device["urn"], field.property, value, iso_z(stamp)))
        return out
