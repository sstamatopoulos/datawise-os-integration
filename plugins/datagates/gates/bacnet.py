"""
BACnet/IP gate — points of a building management system.

BACnet is what the HVAC in a building from the last thirty years speaks:
Siemens Desigo, Honeywell, Johnson Controls Metasys, Schneider, Trend,
Delta and every controller that ever claimed interoperability.

    - key: bms
      type: bacnet
      schedule: "*/10 * * * *"
      options:
        bind: 10.0.20.5/24               # this host's address on the BACnet network
        devices:
          - id: ahu1
            name: AHU 1
            address: 10.0.20.41          # or 10.0.20.41:47808, or 2001:12 for MS/TP behind a router
            ref_building: urn:ngsi-ld:Building:...
            fields:
              "analogInput:1":  {property: temperature, unit: CEL}
              "analogValue:12": {property: temperatureSetpoint, unit: CEL}
              "binaryInput:3":  {property: occupancy, unit: C62}
              "analogInput:7":  {property: co2, unit: "59", invalid: [-9999]}

The field key is `objectType:instance` as BACnet names it
(`analogInput`, `analogValue`, `analogOutput`, `binaryInput`,
`multiStateValue`, ...); `property:` inside the entry is the NGSI
controlledProperty, not the BACnet property, which is always
`presentValue` unless `bacnet_property` says otherwise.

Hard-won notes:

- **Binary and multi-state points come back as text** (`active`,
  `inactive`, `1`), not numbers. They are mapped to 1/0 here, because a
  summary entity holds a number.
- **A point that is out of service or unreliable still answers**, usually
  with its last good value or a sentinel like -9999. Use `invalid:` in
  the field entry; there is no other way to tell.
- **This is polling, not COV subscription.** Airflow is a batch
  scheduler, so the gate reads on a schedule; the schedule is the
  sampling rate of the resulting series.
- **The host needs a real address on the BACnet network.** `bind` must be
  an interface of the Airflow worker in CIDR form, and in Docker that
  means host networking or a routed macvlan — a bridged container cannot
  receive the broadcasts BACnet relies on. This is the one gate whose
  deployment is not "just another container".

Needs the `BAC0` extra (pip install -r requirements-gates.txt).
"""
from __future__ import annotations

import logging
from typing import Any

from datagates.core.fieldmap import FieldMap
from datagates.gates.polling import PollingGate

log = logging.getLogger(__name__)

TEXT_VALUES = {"active": 1.0, "inactive": 0.0, "true": 1.0, "false": 0.0,
               "on": 1.0, "off": 0.0, "occupied": 1.0, "unoccupied": 0.0}


def numeric(raw: Any) -> Any:
    """BACnet enumerations arrive as text; everything else passes through."""
    if isinstance(raw, str):
        return TEXT_VALUES.get(raw.strip().lower(), raw)
    return raw


class BacnetGate(PollingGate):
    type_name = "bacnet"
    speaks = "BACnet/IP building management systems (Desigo, Metasys, Trend, Delta, ...)"
    default_schedule = "*/10 * * * *"
    default_category = ("sensor",)
    device_attrs = ("pollDeviceId", "bacnetAddress")

    def __init__(self, config):
        super().__init__(config)
        self.bind = str(self.option("bind", ""))
        self.bacnet_property = str(self.option("bacnet_property", "presentValue"))
        for entry in self.specs.values():
            if not entry.get("address"):
                raise ValueError(f"{self.key}: device {entry['id']} needs a BACnet address")
        self._network = None

    def device_attributes(self, entry: dict[str, Any]) -> dict[str, Any]:
        return {"bacnetAddress": str(entry["address"])}

    # -- upstream ----------------------------------------------------------
    def _connect(self):
        """One BAC0 network object per gate; creating several on the same
        host fights over UDP 47808."""
        if self._network is None:
            import BAC0  # imported here: optional dependency

            self._network = BAC0.lite(ip=self.bind) if self.bind else BAC0.lite()
        return self._network

    def _read_point(self, address: str, point: str, bacnet_property: str) -> Any:
        """BAC0 takes one whitespace-separated request string, which is why
        an address with a port must stay unquoted and unsplit."""
        object_type, _, instance = point.partition(":")
        return self._connect().read(f"{address} {object_type} {instance} {bacnet_property}")

    def read(self, device: dict[str, Any], entry: dict[str, Any], fields: FieldMap) -> dict[str, Any]:
        address = str(entry["address"])
        out: dict[str, Any] = {}
        for field in fields:
            try:
                raw = self._read_point(address, str(field.source),
                                       str(field.get("bacnet_property", self.bacnet_property)))
            except Exception as exc:                        # noqa: BLE001 - one dead point, not a dead device
                log.warning("%s: %s %s unreadable: %s", self.key, entry["id"], field.source, exc)
                continue
            out[field.source] = numeric(raw)
        return out
