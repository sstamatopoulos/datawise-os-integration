"""
Nord Pool gate — day-ahead electricity prices per delivery area.

    - key: prices
      type: nordpool
      options:
        areas: [LV, EE]
        currency: EUR

Public, unauthenticated: GET https://dataportal-api.nordpoolgroup.com/api/DayAheadPrices
?date=YYYY-MM-DD&deliveryArea=LV&currency=EUR. Tomorrow's prices appear
around 13:00 CET; before that the day answers with no entries. Market
time unit is 15 minutes since 2025-10; each sample is stamped at the
start of its delivery period.

One Device per area, type MarketPriceFeed, no refBuilding: a price
applies to a whole delivery area. Join to buildings by `deliveryArea`.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any

import requests

from datagates.gates.base import DeviceSpec, Gate, Sample

BASE_URL = "https://dataportal-api.nordpoolgroup.com"
UNIT = "EUR_MWH"     # not a UN/CEFACT code: there is none for currency per energy


class NordPoolGate(Gate):
    type_name = "nordpool"
    speaks = "Nord Pool day-ahead market"
    default_schedule = "0 12,14 * * *"        # after publication, with one retry slot
    default_history_start = "2024-01-01"
    default_max_window_days = 31
    rolling = True                             # today + tomorrow every run
    backfill_mode = "stateless"
    device_attrs = ("deliveryArea", "currency")

    def __init__(self, config):
        super().__init__(config)
        self.areas = [str(a).upper() for a in self.option("areas", ["LV"])]
        self.currency = str(self.option("currency", "EUR")).upper()
        self.base = str(self.option("base_url", BASE_URL)).rstrip("/")
        self.timeout = float(self.option("timeout", 30))
        self._last = 0.0

    def discover(self) -> list[DeviceSpec]:
        return [DeviceSpec(
            urn=self.urn(area, self.currency),
            name=f"Nord Pool day-ahead price {area} ({self.currency}/MWh)",
            properties={"dayAheadPrice": UNIT},
            category=["marketPriceFeed"],
            attrs={"deliveryArea": area, "currency": self.currency, "market": "DayAhead"},
        ) for area in self.areas]

    def _day(self, day: str, area: str) -> list[dict[str, Any]]:
        wait = 0.3 - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        for attempt in range(1, 4):
            r = requests.get(f"{self.base}/api/DayAheadPrices",
                             params={"date": day, "deliveryArea": area, "currency": self.currency},
                             headers={"Accept": "application/json"}, timeout=self.timeout)
            self._last = time.monotonic()
            if r.status_code in (204, 404):
                return []
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(3 * attempt)
                continue
            r.raise_for_status()
            body = r.json() if r.content else {}
            return (body or {}).get("multiAreaEntries", []) or []
        raise RuntimeError(f"Nord Pool {day}/{area}: gave up after 3 attempts")

    def fetch(self, device: dict[str, Any], start: datetime, end: datetime) -> list[Sample]:
        area = device["deliveryArea"]
        if start == end:                     # rolling run: today and tomorrow
            days = [start.date(), start.date() + timedelta(days=1)]
        else:
            days = []
            d = start.date()
            while d <= end.date():
                days.append(d)
                d += timedelta(days=1)
        out: list[Sample] = []
        for day in days:
            for e in self._day(day.isoformat(), area):
                price = (e.get("entryPerArea") or {}).get(area)
                t = e.get("deliveryStart")
                if price is None or not t:
                    continue
                iso = datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                out.append(Sample(device["urn"], "dayAheadPrice", float(price), iso))
        return out
