"""
SNMP gate — OIDs of anything with a management agent.

The forgotten telemetry of a building: UPS units and their battery
charge, rack PDUs metering per-socket energy, generators, network-
attached power meters, chillers with a management card, and the room
sensors that ship with a monitoring agent and nothing else.

    - key: ups
      type: snmp
      schedule: "*/5 * * * *"
      options:
        version: 2c                       # 1 | 2c | 3
        community: ${SNMP_COMMUNITY}      # v1/v2c
        devices:
          - id: ups-main
            name: UPS main switchboard
            host: 10.0.10.7
            port: 161
            ref_building: urn:ngsi-ld:Building:...
            fields:
              "1.3.6.1.2.1.33.1.2.4.0":  {property: batteryLevel, unit: P1, scale: 0.01}
              "1.3.6.1.2.1.33.1.4.4.1.4.1": {property: power, unit: WTT}
              "1.3.6.1.4.1.318.1.1.1.4.2.1.0": {property: voltage, unit: VLT, scale: 0.1}

For SNMPv3 replace `community` with

        version: 3
        user: ${SNMP_USER}
        auth: {protocol: SHA, key: ${SNMP_AUTH_KEY}}
        privacy: {protocol: AES, key: ${SNMP_PRIV_KEY}}

The field key is a numeric OID, dotted, with its instance suffix (`.0`
for a scalar). Symbolic names are deliberately not supported: they need
the vendor's MIB files on the worker, and a MIB that is present in
testing and missing in production fails in a way that looks like a
network problem.

Two things worth knowing:

- **Values are integers with an implied scale.** SNMP has no decimals,
  so a tenth of a volt is reported as 2301. `scale: 0.1` is the rule
  rather than the exception; the MIB's DISPLAY-HINT says which.
- **Counters wrap.** A 32-bit counter of watt-hours wraps roughly every
  49 days of heavy use and reads as a huge negative delta afterwards.
  Mark such fields `cumulative: true` so the counter guard catches the
  wrap instead of writing a value no meter could have produced.

Needs the `pysnmp` extra (pip install -r requirements-gates.txt). The gate
uses pysnmp's synchronous high-level API, which release 6 reorganised into
`v1arch`/`v3arch`; requirements-gates.txt pins below 6, and adapting to it
means changing `_get`, the one method that touches the library.
"""
from __future__ import annotations

import logging
from typing import Any

from datagates.core.fieldmap import FieldMap
from datagates.gates.polling import PollingGate

log = logging.getLogger(__name__)


class SnmpGate(PollingGate):
    type_name = "snmp"
    speaks = "SNMP agents (UPS, PDU, generators, network-attached meters)"
    default_schedule = "*/5 * * * *"
    default_category = ("meter",)
    device_attrs = ("pollDeviceId", "snmpHost")

    def __init__(self, config):
        super().__init__(config)
        self.version = str(self.option("version", "2c")).lower()
        self.community = str(self.option("community", "public"))
        self.user = str(self.option("user", ""))
        self.auth = dict(self.option("auth") or {})
        self.privacy = dict(self.option("privacy") or {})
        if self.version == "3" and not self.user:
            raise ValueError(f"{self.key}: SNMPv3 needs options.user")
        for entry in self.specs.values():
            if not entry.get("host"):
                raise ValueError(f"{self.key}: device {entry['id']} needs a host")

    def device_attributes(self, entry: dict[str, Any]) -> dict[str, Any]:
        return {"snmpHost": f"{entry['host']}:{entry.get('port', 161)}"}

    # -- upstream ----------------------------------------------------------
    def _auth_data(self):
        """pysnmp's authentication object for the configured version."""
        from pysnmp.hlapi import CommunityData, UsmUserData, usmAesCfb128Protocol, usmHMACSHAAuthProtocol

        if self.version != "3":
            return CommunityData(self.community, mpModel=0 if self.version == "1" else 1)
        protocols = {"SHA": usmHMACSHAAuthProtocol, "AES": usmAesCfb128Protocol}
        return UsmUserData(self.user,
                           authKey=self.auth.get("key") or None,
                           privKey=self.privacy.get("key") or None,
                           authProtocol=protocols.get(str(self.auth.get("protocol", "SHA")).upper()),
                           privProtocol=protocols.get(str(self.privacy.get("protocol", "AES")).upper()))

    def _get(self, entry: dict[str, Any], oids: list[str]) -> dict[str, Any]:
        """One GET for every OID of a device. pysnmp's high-level API is
        a generator that yields once per request."""
        from pysnmp.hlapi import ContextData, ObjectIdentity, ObjectType, SnmpEngine, UdpTransportTarget, getCmd

        target = UdpTransportTarget((str(entry["host"]), int(entry.get("port", 161))), timeout=self.timeout)
        objects = [ObjectType(ObjectIdentity(oid)) for oid in oids]
        error_indication, error_status, _, var_binds = next(
            getCmd(SnmpEngine(), self._auth_data(), target, ContextData(), *objects))
        if error_indication or error_status:
            raise ConnectionError(f"{self.key}: {entry['host']}: {error_indication or error_status.prettyPrint()}")
        return {str(name): value for name, value in var_binds}

    def read(self, device: dict[str, Any], entry: dict[str, Any], fields: FieldMap) -> dict[str, Any]:
        oids = [str(f.source) for f in fields]
        answered = self._get(entry, oids)
        out: dict[str, Any] = {}
        for oid in oids:
            raw = answered.get(oid, answered.get(f".{oid}"))
            if raw is None:
                log.warning("%s: %s did not answer %s", self.key, entry["id"], oid)
                continue
            out[oid] = float(raw) if hasattr(raw, "__int__") else str(raw)
        return out
