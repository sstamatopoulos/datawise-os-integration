"""
Siemens S7 gate — data blocks of an S7-300/400/1200/1500 PLC.

The S7 family runs a large share of the district heating substations,
pump stations and air handling plants installed since the 1990s. Many of
them have no OPC UA server, no BACnet interface and no intention of
getting one; the ISO-on-TCP protocol their programming software uses is
the only way in.

    - key: substation
      type: s7
      schedule: "*/5 * * * *"
      options:
        devices:
          - id: hs1
            name: Heat substation 1
            host: 10.0.40.12
            rack: 0
            slot: 1                      # 1 for S7-1200/1500, 2 for most S7-300/400
            ref_building: urn:ngsi-ld:Building:...
            fields:
              supplyT:   {property: temperature, unit: CEL, db: 10, start: 0, type: float32}
              returnT:   {property: returnTemperature, unit: CEL, db: 10, start: 4, type: float32}
              heatTotal: {property: energy, unit: KWH, db: 10, start: 8, type: uint32, cumulative: true}
              pumpOn:    {property: pumpState, unit: C62, db: 10, start: 12, type: bool, bit: 0}

The field key is a name you choose; `db` and `start` are the data block
number and the byte offset inside it, exactly as the PLC program's
symbol table shows them (`DB10.DBD8` is `db: 10, start: 8, type: float32`).
`area: merker|input|output` reads M, I or Q instead of a data block.

Things that cost a day each if not known:

- **The slot number.** 2 for classic S7-300/400 CPUs, 1 for S7-1200/1500.
  A wrong slot fails with the same "connection refused" as a firewall.
- **Optimised block access must be off.** S7-1200/1500 data blocks are
  "optimised" by default and then have no stable byte offsets at all;
  the block has to be marked non-optimised in TIA Portal, and PUT/GET
  communication enabled on the CPU. Both are changes in the PLC program,
  so they need the plant's automation contractor, not the IT department.
- **Everything is big-endian** and a REAL is four bytes, a DINT four, an
  INT two; core.binary does the decoding and explains the word-order trap.
- **One connection at a time.** Small CPUs accept two or three PG
  connections in total, so a gate that polls every minute can lock out
  the engineer trying to connect with TIA Portal. Poll as slowly as the
  data allows and say so in the handover.

Needs the `python-snap7` extra and the native snap7 library on the
worker (see requirements-gates.txt).
"""
from __future__ import annotations

import logging
from typing import Any

from datagates.core.binary import decode, size_of
from datagates.core.fieldmap import FieldMap
from datagates.gates.polling import PollingGate

log = logging.getLogger(__name__)

AREAS = ("db", "merker", "input", "output")


class S7Gate(PollingGate):
    type_name = "s7"
    speaks = "Siemens S7-300/400/1200/1500 PLCs (ISO-on-TCP)"
    default_schedule = "*/5 * * * *"
    default_category = ("actuator", "sensor")
    device_attrs = ("pollDeviceId", "s7Host", "s7Rack", "s7Slot")

    def __init__(self, config):
        super().__init__(config)
        for entry in self.specs.values():
            if not entry.get("host"):
                raise ValueError(f"{self.key}: device {entry['id']} needs a host")

    def device_attributes(self, entry: dict[str, Any]) -> dict[str, Any]:
        return {"s7Host": str(entry["host"]), "s7Rack": int(entry.get("rack", 0)),
                "s7Slot": int(entry.get("slot", 1))}

    # -- upstream ----------------------------------------------------------
    def _connect(self, entry: dict[str, Any]):
        import snap7  # imported here: optional dependency

        client = snap7.client.Client()
        client.connect(str(entry["host"]), int(entry.get("rack", 0)), int(entry.get("slot", 1)))
        return client

    @staticmethod
    def _area_of(client, name: str):
        import snap7

        return {"merker": snap7.type.Area.MK, "input": snap7.type.Area.PE,
                "output": snap7.type.Area.PA}[name]

    def _read_block(self, client, area: str, db: int, start: int, size: int) -> bytes:
        if area == "db":
            return bytes(client.db_read(db, start, size))
        return bytes(client.read_area(self._area_of(client, area), 0, start, size))

    def read(self, device: dict[str, Any], entry: dict[str, Any], fields: FieldMap) -> dict[str, Any]:
        client = self._connect(entry)
        out: dict[str, Any] = {}
        try:
            for field in fields:
                area = str(field.get("area", "db")).lower()
                if area not in AREAS:
                    raise ValueError(f"{self.key}: area {area!r} must be one of {', '.join(AREAS)}")
                dtype = str(field.get("type", "float32"))
                start = int(field.get("start", 0))
                try:
                    block = self._read_block(client, area, int(field.get("db", 0)), start, size_of(dtype))
                except Exception as exc:                    # noqa: BLE001 - one bad address
                    log.warning("%s: %s %s unreadable: %s", self.key, entry["id"], field.source, exc)
                    continue
                if dtype == "bool":
                    out[field.source] = bool(block[0] >> int(field.get("bit", 0)) & 1) if block else None
                else:
                    out[field.source] = decode(block, dtype)
        finally:
            client.disconnect()
        return out
