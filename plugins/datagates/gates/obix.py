"""
oBIX gate — Tridium Niagara AX/N4 and other oBIX building servers.

Niagara is the integration layer of a very large share of the world's
commercial buildings: the JACE controller in the plant room that already
speaks BACnet, LonWorks, Modbus and half a dozen proprietary field buses
and has been logging every point to its own history database for years.
Its oBIX driver exposes all of that over HTTP, which makes it the single
most productive gate to configure in an existing building — one
credential, and the histories of hundreds of points come with it.

    - key: jace
      type: obix
      options:
        base_url: "https://jace.example.org/obix"
        auth: {type: basic, username: "${OBIX_USER}", password: "${OBIX_PASSWORD}"}
        history: true                       # read the station's history database
        devices:
          - id: ahu1
            name: AHU 1
            ref_building: urn:ngsi-ld:Building:...
            fields:
              "S1/AHU1_SupplyTemp": {property: temperature, unit: CEL}
              "S1/AHU1_Power":      {property: power, unit: KWT}

With `history: true` the field key is the history id under
`/obix/histories/` (`<station>/<history name>`), and the gate asks for
`~historyQuery` over the run's window, so it gets the station's own
timestamps and can backfill years. With `history: false` the field key
is a point path under `/obix/config/` and the gate reads present values
on the schedule instead.

What the oBIX specification does not prepare you for:

- **Niagara requires a digest or basic login against its own user
  service**, and an "obix" user usually has to be created with HTTP
  Basic explicitly enabled; the default station setup refuses basic auth
  silently with a 401 that looks like a wrong password.
- **History timestamps carry the station's local offset**, not UTC, and
  the station is often set to a site's local time with daylight saving.
  The offsets in the response are honoured; a history whose records have
  no offset at all is read in `timezone:` (default UTC).
- **`~historyQuery` caps its answer** (`limit`, default 1000 on many
  stations). Keep `max_window_days` at 1 for minute-interval points.
- **The XML is namespaced and the namespace changed** between oBIX 1.0
  and 1.1; matching is on local names for that reason.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from datagates.core.fieldmap import FieldMap
from datagates.core.httpclient import client_for
from datagates.core.timeparse import iso_z, parse_stamp, zone_of
from datagates.core.xmlrows import parse_xml, select
from datagates.gates.base import DeviceSpec, Gate, Sample

log = logging.getLogger(__name__)

QUERY = ('<obj is="obix:HistoryFilter">'
         '<abstime name="start" val="{start}"/>'
         '<abstime name="end" val="{end}"/>'
         '<int name="limit" val="{limit}"/>'
         '</obj>')


class ObixGate(Gate):
    type_name = "obix"
    speaks = "Tridium Niagara AX/N4 (JACE) and other oBIX servers"
    default_schedule = "*/15 * * * *"
    default_max_window_days = 1
    default_history_start = "2020-01-01"
    device_attrs = ("obixDeviceId",)

    def __init__(self, config):
        super().__init__(config)
        self.base_url = str(self.required("base_url")).rstrip("/")
        self.history = bool(self.option("history", True))
        self.limit = int(self.option("limit", 1000))
        self.tz = zone_of(self.option("timezone"))
        self.entries = self.devices_option()
        self.maps: dict[str, FieldMap] = {}
        shared = self.option("fields") or {}
        for entry in self.entries:
            spec = {**shared, **(entry.get("fields") or {})}
            self.maps[str(entry["id"])] = FieldMap(spec, gate_key=self.key, block="fields")
        if not self.maps:
            raise ValueError(f"{self.key}: options.devices must list at least one device")
        self.cumulative = frozenset().union(*(m.cumulative for m in self.maps.values()))  # type: ignore[misc]
        self._client = None

    @property
    def rolling(self) -> bool:                               # type: ignore[override]
        return not self.history

    @property
    def backfill_mode(self) -> str:                          # type: ignore[override]
        return "cursor" if self.history else "none"

    def discover(self) -> list[DeviceSpec]:
        return [DeviceSpec(
            urn=self.urn(entry["id"]), name=str(entry.get("name") or entry["id"]),
            properties=self.maps[str(entry["id"])].properties,
            ref_building=entry.get("ref_building"),
            category=list(entry.get("category", ["sensor"])),
            attrs={"obixDeviceId": str(entry["id"])},
        ) for entry in self.entries]

    # -- upstream ----------------------------------------------------------
    @property
    def client(self):
        if self._client is None:
            self._client = client_for(self, headers={"Content-Type": "text/xml"}, base_url=self.base_url)
        return self._client

    def _history_query(self, point: str, start: datetime, end: datetime) -> str:
        body = QUERY.format(start=iso_z(start), end=iso_z(end), limit=self.limit)
        response = self.client.request("POST", f"/histories/{point.strip('/')}/~historyQuery",
                                       data=body.encode("utf-8"))
        return response.text

    def _read_point(self, point: str) -> str:
        return self.client.request("GET", f"/config/{point.strip('/')}/out/").text

    # -- parsing (tested without a station) --------------------------------
    def history_records(self, document: str) -> list[tuple[datetime, Any]]:
        """(timestamp, value) of every HistoryRecord in a query answer."""
        root = parse_xml(document)
        out: list[tuple[datetime, Any]] = []
        for record in select(root, ".//obj"):
            stamp = value = None
            for child in record:
                name, val = child.get("name"), child.get("val")
                if name == "timestamp":
                    stamp = parse_stamp(val, "iso", self.tz)
                elif name == "value":
                    value = val
            if stamp is not None and value is not None:
                out.append((stamp, value))
        return out

    @staticmethod
    def present_value(document: str) -> Any:
        """The `val` of a point read, whatever oBIX type it came back as."""
        root = parse_xml(document)
        return root.get("val")

    # -- the contract ------------------------------------------------------
    def fetch(self, device: dict[str, Any], start: datetime, end: datetime) -> list[Sample]:
        fields = self.maps.get(str(device.get("obixDeviceId") or ""))
        if fields is None:
            return []
        out: list[Sample] = []
        for field in fields:
            point = str(field.source)
            try:
                if self.history:
                    records = self.history_records(self._history_query(point, start, end))
                else:
                    records = [(datetime.now(tz=start.tzinfo), self.present_value(self._read_point(point)))]
            except Exception as exc:                        # noqa: BLE001 - one point, not the station
                log.warning("%s: %s unreadable: %s", self.key, point, exc)
                continue
            for stamp, raw in records:
                converted = field.convert(raw)
                if converted is None:
                    continue
                if self.history and not (start < stamp <= end):
                    continue
                out.append(Sample(device["urn"], field.property, converted, iso_z(stamp)))
        return out
