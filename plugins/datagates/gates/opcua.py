"""
OPC UA gate — nodes of a SCADA server, PLC or historian.

OPC UA is the polite face of industrial automation: most PLCs made after
2010 expose one, and most SCADA packages (WinCC, Ignition, Kepware and
every OPC Classic system behind a UA wrapper) can be reached through it.

    - key: scada
      type: opcua
      schedule: "*/5 * * * *"
      options:
        endpoint: "opc.tcp://10.0.30.9:4840"
        username: ${OPCUA_USER}            # optional
        password: ${OPCUA_PASSWORD}
        security: "Basic256Sha256,SignAndEncrypt,client-cert.der,client-key.pem"   # optional
        history: false                     # true = read the server's historian
        devices:
          - id: chiller1
            name: Chiller 1
            ref_building: urn:ngsi-ld:Building:...
            fields:
              "ns=2;i=1002": {property: temperature, unit: CEL}
              "ns=2;s=Chiller1.Power": {property: power, unit: KWT}

The field key is the node id in the standard string form (`ns=2;i=1002`,
`ns=3;s=Tag.Name`); copy it from the server's address space browser.

`history: true` changes the gate's whole character. A server with
historical access answers `read_raw_history` for a window, so the gate
stops being a poller: it gets a watermark, a cursor backfill and real
timestamps from the source, and it can recover the years a SCADA system
has been recording since long before this platform existed. That is
usually the fastest way to get a legacy plant's history in.

Notes from the field:

- **Not every node historises.** Ask for history on a node that does not
  and the server answers `BadHistoryOperationUnsupported` for that node
  only; the gate logs it and carries on with the others.
- **`read_raw_history` is capped by the server**, often at 1000 values
  per call. Keep `max_window_days` small (1 by default here) so a window
  fits, rather than silently losing the tail of each window.
- **Security strings are positional** and the certificate paths must be
  readable inside the Airflow worker, which is a different filesystem
  than the engineer's laptop where the string was tested.

Needs the `asyncua` extra (pip install -r requirements-gates.txt).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from datagates.core.fieldmap import FieldMap
from datagates.core.timeparse import iso_z
from datagates.gates.base import Sample
from datagates.gates.polling import PollingGate

log = logging.getLogger(__name__)


class OpcUaGate(PollingGate):
    type_name = "opcua"
    speaks = "OPC UA servers (PLCs, SCADA, historians, OPC Classic wrappers)"
    default_schedule = "*/5 * * * *"
    default_max_window_days = 1
    default_category = ("sensor",)
    device_attrs = ("pollDeviceId", "opcuaEndpoint")

    def __init__(self, config):
        super().__init__(config)
        self.endpoint = str(self.required("endpoint"))
        self.username = str(self.option("username", ""))
        self.password = str(self.option("password", ""))
        self.security = str(self.option("security", ""))
        self.history = bool(self.option("history", False))
        self.max_values = int(self.option("max_values", 1000))
        self._client = None

    # A historising server has a past; a plain one does not.
    @property
    def rolling(self) -> bool:                               # type: ignore[override]
        return not self.history

    @property
    def backfill_mode(self) -> str:                          # type: ignore[override]
        return "cursor" if self.history else "none"

    def device_attributes(self, entry: dict[str, Any]) -> dict[str, Any]:
        return {"opcuaEndpoint": str(entry.get("endpoint", self.endpoint))}

    # -- upstream ----------------------------------------------------------
    def _connect(self, endpoint: str):
        from asyncua.sync import Client  # imported here: optional dependency

        if self._client is None:
            client = Client(url=endpoint, timeout=self.timeout)
            if self.username:
                client.set_user(self.username)
                client.set_password(self.password)
            if self.security:
                client.set_security_string(self.security)
            client.connect()
            self._client = client
        return self._client

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.disconnect()
            finally:
                self._client = None

    def _read_values(self, endpoint: str, fields: FieldMap) -> dict[str, Any]:
        client = self._connect(endpoint)
        out: dict[str, Any] = {}
        try:
            for field in fields:
                try:
                    out[field.source] = client.get_node(str(field.source)).read_value()
                except Exception as exc:                    # noqa: BLE001 - one bad node
                    log.warning("%s: node %s unreadable: %s", self.key, field.source, exc)
        finally:
            self.close()
        return out

    def _read_history(self, endpoint: str, source: str, start: datetime,
                      end: datetime) -> list[tuple[datetime, Any]]:
        client = self._connect(endpoint)
        node = client.get_node(source)
        out: list[tuple[datetime, Any]] = []
        for item in node.read_raw_history(start, end, self.max_values) or []:
            stamp = getattr(item, "SourceTimestamp", None) or getattr(item, "ServerTimestamp", None)
            value = getattr(getattr(item, "Value", None), "Value", None)
            if stamp is not None and value is not None:
                out.append((stamp, value))
        return out

    def read(self, device: dict[str, Any], entry: dict[str, Any], fields: FieldMap) -> dict[str, Any]:
        return self._read_values(str(entry.get("endpoint", self.endpoint)), fields)

    # -- the windowed path, for historising servers -------------------------
    def fetch(self, device: dict[str, Any], start: datetime, end: datetime) -> list[Sample]:
        if not self.history:
            return super().fetch(device, start, end)
        device_id = str(device.get("pollDeviceId") or "")
        entry, fields = self.specs.get(device_id), self.maps.get(device_id)
        if entry is None or fields is None:
            return []
        endpoint = str(entry.get("endpoint", self.endpoint))
        out: list[Sample] = []
        try:
            for field in fields:
                try:
                    history = self._read_history(endpoint, str(field.source), start, end)
                except Exception as exc:                    # noqa: BLE001 - node without a historian
                    log.warning("%s: no history for %s: %s", self.key, field.source, exc)
                    continue
                for stamp, raw in history:
                    value = field.convert(raw)
                    if value is None:
                        continue
                    when = stamp if stamp.tzinfo else stamp.replace(tzinfo=start.tzinfo)
                    if start < when <= end + timedelta(seconds=1):
                        out.append(Sample(device["urn"], field.property, value, iso_z(when)))
        finally:
            self.close()
        return out
