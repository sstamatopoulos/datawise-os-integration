"""
Zabbix gate — items already monitored by the facility's monitoring server.

Zabbix is usually installed to watch servers and switches, and then
someone adds the UPS, the CRAC units, the generator fuel level and the
room temperature probes, because it is the tool that is already there.
That makes a Zabbix server an inventory of building telemetry with years
of history behind it, reachable with one API token.

    - key: facility
      type: zabbix
      options:
        url: "https://zabbix.example.org/api_jsonrpc.php"
        token: "${ZABBIX_TOKEN}"           # API token (Zabbix 5.4+), preferred
        # user: ${ZABBIX_USER}             # or username/password on older servers
        # password: ${ZABBIX_PASSWORD}
        devices:
          - id: crac1
            name: CRAC unit 1
            ref_building: urn:ngsi-ld:Building:...
            fields:
              "23451": {property: temperature, unit: CEL}          # item id
              "23452": {property: relativeHumidity, unit: P1, scale: 0.01}

The field key is the numeric item id, from the item's URL in the web
interface or from `item.get`. Item *keys* (`system.cpu.load[all,avg1]`)
are not accepted: they are unique per host, not globally, and a gate that
resolves them at run time breaks when a host is renamed.

Specifics that matter:

- **A token needs Zabbix 6.0 or newer**, because it is sent as
  `Authorization: Bearer`; before that the only mechanism was `user.login`,
  so give an older server `user` and `password` instead. The gate never puts
  the token in the request body: Zabbix 7.0 removed that property and answers
  "Invalid params", which reads like a broken query rather than a broken
  login.
- **History is typed.** `history.get` needs `history: 0` for floats and
  `3` for unsigned integers, and returns nothing at all — no error — for
  the wrong type. The gate asks for floats and retries as unsigned when
  a float query is empty, which is what the web interface does too.
- **Housekeeping deletes history.** A Zabbix server typically keeps 7 to
  31 days of history and longer trends. Backfill early, and expect a
  backfill to stop at the housekeeping horizon rather than at the
  configured `history_start`.
- **`limit` defaults are generous but not infinite**; the gate pages by
  time so a window returning exactly `limit` rows is split.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from datagates.core.fieldmap import FieldMap
from datagates.core.http import client_for
from datagates.core.timeparse import iso_z
from datagates.gates.base import DeviceSpec, Gate, Sample

log = logging.getLogger(__name__)


class ZabbixGate(Gate):
    type_name = "zabbix"
    speaks = "Zabbix monitoring servers (facility items, UPS, environment probes)"
    default_schedule = "*/15 * * * *"
    default_max_window_days = 7
    default_history_start = "2024-01-01"
    device_attrs = ("zabbixDeviceId",)

    def __init__(self, config):
        super().__init__(config)
        self.url = str(self.required("url"))
        self.token = str(self.option("token", ""))
        self.user = str(self.option("user", ""))
        self.password = str(self.option("password", ""))
        self.limit = int(self.option("limit", 50000))
        self.entries = self.devices_option()
        self.maps: dict[str, FieldMap] = {}
        for entry in self.entries:
            self.maps[str(entry["id"])] = FieldMap(entry.get("fields") or self.option("fields"),
                                                   gate_key=self.key, block="fields")
        if not self.maps:
            raise ValueError(f"{self.key}: options.devices must list at least one device")
        self.cumulative = frozenset().union(*(m.cumulative for m in self.maps.values()))  # type: ignore[misc]
        self._client = None
        self._auth: str | None = None

    def discover(self) -> list[DeviceSpec]:
        return [DeviceSpec(
            urn=self.urn(entry["id"]), name=str(entry.get("name") or entry["id"]),
            properties=self.maps[str(entry["id"])].properties,
            ref_building=entry.get("ref_building"),
            category=list(entry.get("category", ["sensor"])),
            attrs={"zabbixDeviceId": str(entry["id"])},
        ) for entry in self.entries]

    # -- upstream ----------------------------------------------------------
    @property
    def client(self):
        if self._client is None:
            headers = {"Content-Type": "application/json-rpc"}
            # An API token goes in the Authorization header, not in the request
            # body. Zabbix 6.0 added the header and 7.0 *removed* the body's
            # `auth` property outright, so a token in the body works on exactly
            # the versions between them and fails with "Invalid params" on the
            # newest servers, which reads like a wrong query rather than a
            # wrong authentication.
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
            self._client = client_for(self, headers=headers)
        return self._client

    def _login(self) -> str:
        """A username and password are exchanged for a session id once per
        task. `user.login` itself is the one unauthenticated call."""
        if self._auth is None:
            self._auth = str(self._call("user.login", {"username": self.user, "password": self.password},
                                        authenticated=False))
        return self._auth

    def _call(self, method: str, params: dict[str, Any], *, authenticated: bool = True) -> Any:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
        if authenticated and not self.token:
            payload["auth"] = self._login()
        body = self.client.post_json(self.url, json=payload)
        if isinstance(body, dict) and body.get("error"):
            error = body["error"]
            raise RuntimeError(f"{self.key}: {method}: {error.get('message')} {error.get('data', '')}".strip())
        return (body or {}).get("result")

    def _history(self, item_ids: list[str], start: datetime, end: datetime, kind: int) -> list[dict[str, Any]]:
        return self._call("history.get", {
            "output": "extend", "history": kind, "itemids": item_ids, "sortfield": "clock",
            "sortorder": "ASC", "time_from": int(start.timestamp()) + 1,
            "time_till": int(end.timestamp()), "limit": self.limit}) or []

    # -- the contract ------------------------------------------------------
    def fetch(self, device: dict[str, Any], start: datetime, end: datetime) -> list[Sample]:
        fields = self.maps.get(str(device.get("zabbixDeviceId") or ""))
        if fields is None:
            return []
        item_ids = [str(f.source) for f in fields]
        rows = self._history(item_ids, start, end, 0)
        if not rows:
            rows = self._history(item_ids, start, end, 3)      # unsigned items
        by_item = {str(f.source): f for f in fields}
        out: list[Sample] = []
        for row in rows:
            field = by_item.get(str(row.get("itemid")))
            if field is None:
                continue
            value = field.convert(row.get("value"))
            if value is None:
                continue
            out.append(Sample(device["urn"], field.property, value,
                              iso_z(datetime.fromtimestamp(int(row["clock"]), UTC))))
        return out
