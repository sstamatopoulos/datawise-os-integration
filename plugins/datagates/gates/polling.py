"""
datagates.gates.polling — the shared base of the field-protocol gates.

Modbus, BACnet, OPC UA, S7 and SNMP have one thing in common that shapes
every integration built on them: **they have no history**. A register or
an object holds one value, the value it holds now. There is nothing to
ask for "between 09:00 and 10:00", so there is no watermark to advance
and no backfill to run — the gate polls, stamps what it read, and that
sample is the only record that this value ever existed.

Three consequences are handled here once, for every such gate:

- `rolling = True`, so the run DAG calls `fetch(device, now, now)` and
  stores everything returned; `backfill_mode = "none"`, so no backfill
  DAG is built for a source that cannot answer about the past.
- **The schedule is the sampling rate.** `schedule: "*/5 * * * *"` means
  a five-minute series, and nothing else in the system decides it. A
  missed Airflow run is a hole in the data that nothing can fill later.
- **Timestamps are aligned** (`align_s`, one minute by default) so that
  an Airflow retry after a partial failure overwrites the point it wrote
  before instead of adding a second one a few seconds later.

A subclass implements `read()` and declares its own device attributes;
the register / object / OID map stays in gates.yaml rather than being
copied onto the Orion entities, because it is configuration of the gate,
not a property of the thing measured.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from datagates.core.fieldmap import FieldMap
from datagates.core.timeparse import iso_z
from datagates.gates.base import DeviceSpec, Gate, Sample


class PollingGate(Gate):
    """Base for gates that can only read the present value."""

    default_schedule = "*/5 * * * *"
    rolling = True
    backfill_mode = "none"
    device_attrs = ("pollDeviceId",)
    default_category: tuple[str, ...] = ("sensor",)
    fields_block = "fields"

    def __init__(self, config) -> None:
        super().__init__(config)
        self.timeout = float(self.option("timeout", 10))
        self.align_s = int(self.option("align_s", 60))
        self.specs: dict[str, dict[str, Any]] = {}
        self.maps: dict[str, FieldMap] = {}
        shared = self.option(self.fields_block) or {}
        for entry in self.devices_option():
            device_id = str(entry["id"])
            spec = dict(shared)
            spec.update(entry.get(self.fields_block) or {})
            if not spec:
                raise ValueError(f"{self.key}: device {device_id} has no options.{self.fields_block} "
                                 f"and the gate declares none")
            self.specs[device_id] = entry
            self.maps[device_id] = FieldMap(spec, gate_key=self.key, block=self.fields_block)
        if not self.specs:
            raise ValueError(f"{self.key}: options.devices must list at least one device")
        self.cumulative = frozenset().union(*(m.cumulative for m in self.maps.values()))  # type: ignore[misc]

    # -- what a subclass provides -----------------------------------------
    def read(self, device: dict[str, Any], entry: dict[str, Any], fields: FieldMap) -> dict[str, Any]:
        """Raw values of one device, keyed by the field's source name (the
        register address, node id, OID ...). Absent keys are simply not
        stored; raising fails the task for that device only."""
        raise NotImplementedError

    def device_attributes(self, entry: dict[str, Any]) -> dict[str, Any]:
        """Extra Orion attributes for a device entry (host, unit id ...)."""
        return {}

    # -- the contract ------------------------------------------------------
    def discover(self) -> list[DeviceSpec]:
        out: list[DeviceSpec] = []
        for device_id, entry in self.specs.items():
            out.append(DeviceSpec(
                urn=self.urn(device_id), name=str(entry.get("name") or device_id),
                properties=self.maps[device_id].properties,
                ref_building=entry.get("ref_building"),
                category=list(entry.get("category", list(self.default_category))),
                location=self._location(entry),
                attrs={"pollDeviceId": device_id, **self.device_attributes(entry)},
            ))
        return out

    def fetch(self, device: dict[str, Any], start: datetime, end: datetime) -> list[Sample]:
        device_id = str(device.get("pollDeviceId") or "")
        entry, fields = self.specs.get(device_id), self.maps.get(device_id)
        if entry is None or fields is None:
            # The device is registered in Orion but gone from gates.yaml.
            # Not an error: a gate is edited more often than Orion is cleaned.
            return []
        raw = self.read(device, entry, fields)
        return fields.samples(device["urn"], raw, self.stamp())

    # -- helpers -----------------------------------------------------------
    def stamp(self) -> str:
        now = datetime.now(UTC)
        if self.align_s > 0:
            now = datetime.fromtimestamp((int(now.timestamp()) // self.align_s) * self.align_s, UTC)
        return iso_z(now)

    @staticmethod
    def _location(entry: dict[str, Any]) -> tuple[float, float] | None:
        if entry.get("lat") is None or entry.get("lon") is None:
            return None
        return float(entry["lon"]), float(entry["lat"])
