"""
Modbus gate — registers of a PLC, meter, inverter or gateway, over
Modbus/TCP or Modbus/RTU tunnelled through a serial-to-Ethernet gateway.

    - key: plant
      type: modbus
      schedule: "*/5 * * * *"          # the schedule IS the sampling rate
      options:
        devices:
          - id: ahu1
            name: AHU 1 supply
            host: 10.0.20.11
            port: 502
            unit: 1                    # slave / unit id, 1 by default
            framer: socket             # socket = Modbus/TCP, rtu = RTU over TCP
            ref_building: urn:ngsi-ld:Building:...
            fields:
              "30001": {property: temperature, unit: CEL, table: input, type: int16, scale: 0.1}
              "40010": {property: energy, unit: KWH, type: uint32, word_order: little, cumulative: true}
              "40020.3": {property: alarmState, unit: C62}      # bit 3 of register 40020

The field key is the register address, decimal and zero-based as the
device's own register map prints it — with the caveat below. `table` is
`holding` (default, function 3), `input` (function 4), `coil` (function 1)
or `discrete` (function 2); `type` is decoded by core.binary, where the
word-order trap is explained. `40020.3` reads bit 3 of that register.

What the manuals do not tell you:

- **Address bases differ by vendor.** A register printed as 40001 in a
  manual is usually holding register 0 on the wire. Set `base: 40001`
  (or `40000`, or `1`) per device and the gate subtracts it, so the YAML
  can quote the manual verbatim instead of the integrator doing the
  arithmetic in their head at the switchboard.
- **One request per field.** Block reads are faster but a single
  unreadable register in the block fails the whole block, and legacy
  devices scatter holes through their maps. With a five-minute schedule
  the round trips are free; if a device needs pacing, set
  `min_interval_s` on the gate.
- **A Modbus device has no clock and no history.** Samples are stamped
  when the gate read them (see datagates.gates.polling), so a missed run
  is a hole nothing can backfill. Poll faster than the phenomenon.
- **pymodbus renamed the slave argument.** It is `slave=` in 3.0 to 3.7
  and `device_id=` in newer releases; the gate tries one and falls back
  to the other rather than pinning the whole platform to one release.

Needs the `pymodbus` extra (pip install -r requirements-gates.txt).
"""
from __future__ import annotations

import logging
from typing import Any

from datagates.core.binary import decode_bits, decode_registers, register_count
from datagates.core.fieldmap import FieldMap
from datagates.gates.polling import PollingGate

log = logging.getLogger(__name__)

TABLES = ("holding", "input", "coil", "discrete")


class ModbusGate(PollingGate):
    type_name = "modbus"
    speaks = "Modbus/TCP and Modbus/RTU devices (PLCs, meters, inverters, gateways)"
    default_category = ("meter",)
    device_attrs = ("pollDeviceId", "modbusHost", "modbusUnitId")

    def __init__(self, config):
        super().__init__(config)
        self.retries = int(self.option("retries", 2))
        for entry in self.specs.values():
            if not entry.get("host"):
                raise ValueError(f"{self.key}: device {entry['id']} needs a host")

    def device_attributes(self, entry: dict[str, Any]) -> dict[str, Any]:
        return {"modbusHost": f"{entry['host']}:{entry.get('port', 502)}",
                "modbusUnitId": int(entry.get("unit", 1))}

    # -- upstream ----------------------------------------------------------
    def _client(self, entry: dict[str, Any]):
        from pymodbus.client import ModbusTcpClient  # imported here: optional dependency

        kwargs: dict[str, Any] = {"host": str(entry["host"]), "port": int(entry.get("port", 502)),
                                  "timeout": self.timeout}
        if str(entry.get("framer", "socket")).lower() == "rtu":
            try:
                from pymodbus.framer import FramerType
                kwargs["framer"] = FramerType.RTU
            except ImportError:                              # pragma: no cover - pymodbus < 3.7
                from pymodbus.framer.rtu_framer import ModbusRtuFramer
                kwargs["framer"] = ModbusRtuFramer
        return ModbusTcpClient(**kwargs)

    def _call(self, client, table: str, address: int, count: int, unit: int):
        """One read, across the pymodbus releases that renamed `slave`."""
        reader = {"holding": client.read_holding_registers, "input": client.read_input_registers,
                  "coil": client.read_coils, "discrete": client.read_discrete_inputs}[table]
        try:
            return reader(address, count=count, slave=unit)
        except TypeError:
            return reader(address, count=count, device_id=unit)

    def _read_points(self, entry: dict[str, Any], fields: FieldMap) -> dict[str, Any]:
        """Raw value per field source. Unreadable points are left out, so
        one dead register does not cost the whole device its sample."""
        client = self._client(entry)
        if not client.connect():
            raise ConnectionError(f"{self.key}: cannot reach {entry['host']}:{entry.get('port', 502)}")
        unit = int(entry.get("unit", 1))
        base = int(entry.get("base", 0))
        out: dict[str, Any] = {}
        try:
            for field in fields:
                table = str(field.get("table", "holding")).lower()
                if table not in TABLES:
                    raise ValueError(f"{self.key}: table {table!r} must be one of {', '.join(TABLES)}")
                address_text, _, bit_text = str(field.source).partition(".")
                dtype = str(field.get("type", "bool" if table in ("coil", "discrete") else "uint16"))
                address = int(address_text) - base
                count = 1 if bit_text or table in ("coil", "discrete") else register_count(dtype)
                result = self._call(client, table, address, count, unit)
                if result is None or (hasattr(result, "isError") and result.isError()):
                    log.warning("%s: %s %s@%s returned %s", self.key, entry["id"], table, field.source, result)
                    continue
                if table in ("coil", "discrete"):
                    out[field.source] = bool(result.bits[0])
                    continue
                registers = list(result.registers)
                if bit_text:
                    out[field.source] = decode_bits(registers, int(bit_text))
                else:
                    out[field.source] = decode_registers(
                        registers, dtype,
                        word_order=str(field.get("word_order", entry.get("word_order", "big"))),
                        byte_order=str(field.get("byte_order", entry.get("byte_order", "big"))))
        finally:
            client.close()
        return out

    def read(self, device: dict[str, Any], entry: dict[str, Any], fields: FieldMap) -> dict[str, Any]:
        return self._read_points(entry, fields)
