"""
ThingsBoard gate — telemetry keys of ThingsBoard devices.

    - key: meters_at
      type: thingsboard
      options:
        base_url: https://tb.example.org/api
        username: ${TB_USERNAME}
        password: ${TB_PASSWORD}
        devices:
          - id: school-main-elec
            name: School - main - electricity
            tb_device_id: 2b0c7e30-....             # ThingsBoard device UUID
            ref_building: urn:ngsi-ld:Building:...
            keys:
              energy: {key: Qel1__A, unit: KWH, cumulative: true}
              power:  {key: Pel1__A, unit: KWT}

Uses POST /auth/login then GET /plugins/telemetry/DEVICE/{id}/values/timeseries
paged by 10 000 rows. `energy`-like running totals are marked cumulative so
the counter guard rejects readings that belong to another meter.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import requests

from datagates.gates.base import DeviceSpec, Gate, Sample


class ThingsBoardGate(Gate):
    type_name = "thingsboard"
    speaks = "ThingsBoard IoT platform"
    default_schedule = "*/30 * * * *"
    default_max_window_days = 30
    device_attrs = ("tbDeviceId", "tbKeys")

    def __init__(self, config):
        super().__init__(config)
        self.base = str(self.option("base_url", "")).rstrip("/")
        self.username = str(self.option("username", ""))
        self.password = str(self.option("password", ""))
        if not (self.base and self.username and self.password):
            raise ValueError(f"{self.key}: base_url, username and password are required")
        self.timeout = float(self.option("timeout", 60))
        self.page = int(self.option("page_size", 10000))
        self.cumulative = frozenset(  # type: ignore[misc]
            prop for d in self.option("devices", []) for prop, k in (d.get("keys") or {}).items() if k.get("cumulative"))
        self._session: requests.Session | None = None

    def discover(self) -> list[DeviceSpec]:
        out = []
        for d in self.option("devices", []):
            keys = d.get("keys") or {}
            out.append(DeviceSpec(
                urn=self.urn(d["id"]), name=str(d.get("name") or d["id"]),
                properties={prop: k.get("unit", "C62") for prop, k in keys.items()},
                ref_building=d.get("ref_building"), category=list(d.get("category", ["meter"])),
                attrs={"tbDeviceId": d["tb_device_id"], "tbKeys": {prop: k["key"] for prop, k in keys.items()}},
            ))
        return out

    def _login(self) -> requests.Session:
        if self._session is None:
            s = requests.Session()
            r = s.post(f"{self.base}/auth/login", json={"username": self.username, "password": self.password},
                       timeout=self.timeout)
            r.raise_for_status()
            token = r.json().get("token")
            s.headers.update({"X-Authorization": f"Bearer {token}"})
            self._session = s
        return self._session

    def _series(self, tb_id: str, key: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
        s = self._login()
        out: list[dict[str, Any]] = []
        cursor = start_ms
        while cursor <= end_ms:
            r = s.get(f"{self.base}/plugins/telemetry/DEVICE/{tb_id}/values/timeseries",
                      params={"keys": key, "startTs": cursor, "endTs": end_ms, "limit": self.page,
                              "order": "ASC", "useStrictDataTypes": "true"}, timeout=self.timeout)
            r.raise_for_status()
            page = r.json().get(key, []) or []
            if not page:
                break
            out.extend(page)
            last = int(page[-1]["ts"])
            if last >= end_ms or len(page) < self.page:
                break
            cursor = last + 1
            time.sleep(0.1)
        return out

    def fetch(self, device: dict[str, Any], start: datetime, end: datetime) -> list[Sample]:
        tb_id = device["tbDeviceId"]
        keys: dict[str, str] = dict(device.get("tbKeys") or {})
        s_ms, e_ms = int(start.timestamp() * 1000) + 1, int(end.timestamp() * 1000)
        out: list[Sample] = []
        for prop, key in keys.items():
            for row in self._series(tb_id, key, s_ms, e_ms):
                try:
                    val = float(row["value"])
                    iso = datetime.fromtimestamp(int(row["ts"]) / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                except (KeyError, TypeError, ValueError):
                    continue
                out.append(Sample(device["urn"], prop, val, iso))
        return out
