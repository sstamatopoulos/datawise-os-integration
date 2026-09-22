"""
NGSI-LD source gate — mirror entities from another context broker.

Federation, not migration: a partner, a city platform or a neighbouring
pilot runs its own Orion-LD, Scorpio or Stellio, and some of its entities
belong in this platform's history too. The gate reads the other broker's
temporal API and lands the series here under this platform's own model,
so consumers see one data model rather than two.

    - key: city_broker
      type: ngsi_ld
      options:
        url: "https://broker.city.example.org"
        auth: {type: header, name: X-API-Key, value: "${CITY_BROKER_KEY}"}
        tenant: smartcity                  # NGSILD-Tenant, if the broker uses one
        context: "https://uri.etsi.org/ngsi-ld/v1/ngsi-ld-core-context.jsonld"
        devices:
          - id: "urn:ngsi-ld:AirQualityObserved:Station-7"
            name: City air quality station 7
            ref_building: urn:ngsi-ld:Building:...
            fields:
              co2:         {property: co2, unit: "59"}
              temperature: {property: temperature, unit: CEL}

The device id is the remote entity id; the field key is the remote
attribute name. Leave `devices:` out and give `entity_type:` instead and
the gate discovers every entity of that type at init time, naming each
after its `name` attribute.

Interoperability notes, all of them learned against real brokers:

- **`timerel=between` needs both `timeAt` and `endTimeAt`**, and the
  bounds are inclusive at both ends; the gate drops the lower bound to
  keep this platform's half-open window and avoid rewriting the boundary
  point on every run.
- **The temporal answer is in "temporal representation"**: each attribute
  is a list of `{value, observedAt}` objects, not a value. Simplified
  representation (`options=temporalValues`) is faster but not every
  broker implements it; the gate reads the normalised form, which all of
  them do.
- **A broker with a Smart Data Model context expands attribute names.**
  If the mirror comes back empty, the attribute is stored expanded on
  the other side: send the right `context:` here, which is exactly the
  trap this platform avoids for its own entities (see core.settings).
- **Page size is capped at 1000** on most brokers; the gate pages by
  `lastN` over the window rather than assuming everything fits.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from datagates.core.fieldmap import FieldMap
from datagates.core.httpclient import client_for
from datagates.core.timeparse import iso_z, parse_stamp
from datagates.gates.base import DeviceSpec, Gate, Sample

log = logging.getLogger(__name__)


class NgsiLdGate(Gate):
    type_name = "ngsi_ld"
    speaks = "another NGSI-LD broker (Orion-LD, Scorpio, Stellio)"
    default_schedule = "*/30 * * * *"
    default_max_window_days = 7
    default_history_start = "2023-01-01"
    device_attrs = ("remoteEntityId",)

    def __init__(self, config):
        super().__init__(config)
        self.url = str(self.required("url")).rstrip("/")
        self.tenant = str(self.option("tenant", ""))
        self.context = str(self.option("context",
                                       "https://uri.etsi.org/ngsi-ld/v1/ngsi-ld-core-context.jsonld"))
        self.entity_type = str(self.option("entity_type", ""))
        self.last_n = int(self.option("last_n", 1000))
        self.entries = self.devices_option()
        shared = self.option("fields") or {}
        self.maps = {str(e["id"]): FieldMap({**shared, **(e.get("fields") or {})},
                                            gate_key=self.key, block="fields") for e in self.entries}
        if not self.entries and not self.entity_type:
            raise ValueError(f"{self.key}: give options.devices or options.entity_type")
        if not self.entries and not shared:
            raise ValueError(f"{self.key}: options.fields is required when discovering by entity_type")
        self.shared = FieldMap(shared, gate_key=self.key, block="fields", required=False)
        self.cumulative = frozenset().union(                 # type: ignore[misc]
            self.shared.cumulative, *(m.cumulative for m in self.maps.values()))
        self._client = None

    @property
    def client(self):
        if self._client is None:
            headers = {"Accept": "application/ld+json", "Link":
                       f'<{self.context}>; rel="http://www.w3.org/ns/json-ld#context"; type="application/ld+json"'}
            if self.tenant:
                headers["NGSILD-Tenant"] = self.tenant
            self._client = client_for(self, headers=headers)
        return self._client

    # -- registration ------------------------------------------------------
    def discover(self) -> list[DeviceSpec]:
        if self.entries:
            return [DeviceSpec(
                urn=self.urn(entry["id"]), name=str(entry.get("name") or entry["id"]),
                properties=self.maps[str(entry["id"])].properties,
                ref_building=entry.get("ref_building"),
                category=list(entry.get("category", ["sensor"])),
                attrs={"remoteEntityId": str(entry["id"])},
            ) for entry in self.entries]
        out: list[DeviceSpec] = []
        for entity in self._remote_entities():
            entity_id = entity.get("id")
            name = entity.get("name", {})
            out.append(DeviceSpec(
                urn=self.urn(entity_id), name=str(name.get("value", entity_id) if isinstance(name, dict) else name),
                properties=self.shared.properties, category=["sensor"],
                attrs={"remoteEntityId": entity_id},
            ))
        return out

    def _remote_entities(self) -> list[dict[str, Any]]:
        body = self.client.get_json(f"{self.url}/ngsi-ld/v1/entities",
                                    params={"type": self.entity_type, "limit": 1000})
        return body if isinstance(body, list) else []

    # -- upstream ----------------------------------------------------------
    def _temporal(self, entity_id: str, attributes: list[str], start: datetime, end: datetime) -> dict[str, Any]:
        return self.client.get_json(
            f"{self.url}/ngsi-ld/v1/temporal/entities/{entity_id}",
            params={"timerel": "between", "timeAt": iso_z(start), "endTimeAt": iso_z(end),
                    "attrs": ",".join(attributes), "lastN": self.last_n}) or {}

    def fetch(self, device: dict[str, Any], start: datetime, end: datetime) -> list[Sample]:
        entity_id = str(device.get("remoteEntityId") or "")
        fields = self.maps.get(entity_id) or self.shared
        if not len(fields):
            return []
        body = self._temporal(entity_id, [str(f.source) for f in fields], start, end)
        return self.samples(body, device["urn"], fields, start, end)

    def samples(self, body: dict[str, Any], urn: str, fields: FieldMap,
                start: datetime, end: datetime) -> list[Sample]:
        out: list[Sample] = []
        for field in fields:
            instances = body.get(field.source)
            if instances is None:
                continue
            for instance in (instances if isinstance(instances, list) else [instances]):
                if not isinstance(instance, dict):
                    continue
                stamp = parse_stamp(instance.get("observedAt") or instance.get("modifiedAt"))
                value = field.convert(instance.get("value"))
                if stamp is None or value is None or not (start < stamp <= end):
                    continue
                out.append(Sample(urn, field.property, value, iso_z(stamp)))
        return out
