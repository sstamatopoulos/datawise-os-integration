"""
ENTSO-E gate — the European electricity market's own data, in XML.

The Transparency Platform is the reference source for day-ahead prices,
load and generation across all of Europe, free with a registered token.
It is also a museum piece of an API: EIC area codes, document type codes,
`yyyyMMddHHmm` timestamps, positions instead of timestamps, and a
different document shape per query. Once mapped, it is the most reliable
market source there is.

    - key: market
      type: entsoe
      options:
        token: "${ENTSOE_TOKEN}"
        document_type: A44                  # A44 price, A65 load, A75 generation
        areas:
          - {id: LV, name: Latvia, eic: "10YLV-1001A00074"}
          - {id: EE, name: Estonia, eic: "10Y1001A1001A39I"}

Document types this gate maps, with the property each becomes:

| `document_type` | meaning | property | unit |
|---|---|---|---|
| A44 | day-ahead prices | `dayAheadPrice` | EUR_MWH |
| A65 | total load, actual | `electricityLoad` | MAW |
| A75 | actual generation per production type | `electricityGeneration` | MAW |

The things that make a first integration fail:

- **Time is `yyyyMMddHHmm` in UTC**, with no separators and no zone
  marker, and the window is interpreted as market time; the gate formats
  it and asks in UTC, which the platform accepts for every document type.
- **There are no timestamps in the answer.** A `Period` has a
  `timeInterval`, a `resolution` (`PT15M`, `PT30M`, `PT60M`, `P1D`) and
  `Point`s carrying a `position` starting at 1. The instant of a point is
  `start + (position - 1) * resolution` — and positions may be sparse:
  a missing position means "same as the previous one", which is how the
  platform compresses repeated values. Filling that gap is the difference
  between 96 points a day and 40.
- **A44 covers tomorrow.** Prices for the next day publish around 13:00
  CET, so the gate is rolling like the Nord Pool one: it asks for today
  and tomorrow every run rather than following a watermark into a future
  it does not have yet.
- **An empty answer is an HTTP 400** with `<Reason><text>No matching data
  found</text>` in the body, not a 204 and not an empty document. Treating
  it as an error makes every night before publication a failed task.
- **The token is rate limited** to 400 requests per minute per token, and
  the platform bans for an hour on abuse; `min_interval_s` defaults to
  0.5 here for that reason.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import requests

from datagates.core.httpclient import client_for
from datagates.core.timeparse import iso_z, parse_stamp
from datagates.core.xmlrows import parse_xml, select
from datagates.gates.base import DeviceSpec, Gate, Sample

log = logging.getLogger(__name__)

API_URL = "https://web-api.tp.entsoe.eu/api"

# document type -> (controlledProperty, unit code, is it about the future?)
DOCUMENTS: dict[str, tuple[str, str, bool]] = {
    "A44": ("dayAheadPrice", "EUR_MWH", True),
    "A65": ("electricityLoad", "MAW", False),
    "A75": ("electricityGeneration", "MAW", False),
}

RESOLUTIONS = {"PT15M": timedelta(minutes=15), "PT30M": timedelta(minutes=30),
               "PT60M": timedelta(hours=1), "PT1H": timedelta(hours=1),
               "P1D": timedelta(days=1), "P7D": timedelta(days=7)}


def stamp(dt: datetime) -> str:
    """The platform's own timestamp format: yyyyMMddHHmm, UTC, no zone."""
    return dt.astimezone(UTC).strftime("%Y%m%d%H%M")


class EntsoeGate(Gate):
    type_name = "entsoe"
    speaks = "ENTSO-E Transparency Platform (prices, load, generation)"
    default_schedule = "0 13,15 * * *"
    default_history_start = "2023-01-01"
    default_max_window_days = 7
    default_min_interval_s = 0.5
    backfill_mode = "stateless"
    device_attrs = ("marketArea", "areaCode", "documentType")

    def __init__(self, config):
        super().__init__(config)
        self.token = str(self.required("token"))
        self.document_type = str(self.option("document_type", "A44")).upper()
        if self.document_type not in DOCUMENTS:
            raise ValueError(f"{self.key}: document_type must be one of {', '.join(DOCUMENTS)}")
        self.property_name, self.unit, self._future = DOCUMENTS[self.document_type]
        self.areas = [dict(a) for a in (self.option("areas") or [])]
        if not self.areas:
            raise ValueError(f"{self.key}: options.areas must list at least one area with its EIC code")
        self.url = str(self.option("url", API_URL))
        self._client = None

    @property
    def rolling(self) -> bool:                               # type: ignore[override]
        return self._future

    def discover(self) -> list[DeviceSpec]:
        return [DeviceSpec(
            urn=self.urn(area["id"], self.document_type),
            name=str(area.get("name") or area["id"]) + f" {self.property_name}",
            properties={self.property_name: self.unit},
            category=["marketPriceFeed" if self.document_type == "A44" else "gridFeed"],
            entity_type="MarketPriceFeed" if self.document_type == "A44" else "Device",
            attrs={"marketArea": str(area["id"]), "areaCode": str(area["eic"]),
                   "documentType": self.document_type},
        ) for area in self.areas]

    @property
    def client(self):
        if self._client is None:
            self._client = client_for(self, headers={"Accept": "application/xml"})
        return self._client

    def _query(self, eic: str, start: datetime, end: datetime) -> str:
        params = {"securityToken": self.token, "documentType": self.document_type,
                  "periodStart": stamp(start), "periodEnd": stamp(end)}
        if self.document_type == "A44":
            params.update({"in_Domain": eic, "out_Domain": eic})
        else:
            params["outBiddingZone_Domain"] = eic
        try:
            return self.client.request("GET", self.url, params=params).text
        except requests.HTTPError as exc:
            body = getattr(exc.response, "text", "") or ""
            if exc.response is not None and exc.response.status_code == 400 and "No matching data" in body:
                log.info("%s: no data published yet for %s..%s", self.key, iso_z(start), iso_z(end))
                return ""
            raise

    # -- parsing (tested on a recorded document) ---------------------------
    def samples(self, document: str, urn: str) -> list[Sample]:
        if not document.strip():
            return []
        root = parse_xml(document)
        out: list[Sample] = []
        for period in select(root, ".//Period"):
            interval = period.find("timeInterval")
            begins = parse_stamp(interval.findtext("start") if interval is not None else None)
            step = RESOLUTIONS.get((period.findtext("resolution") or "PT60M").strip())
            if begins is None or step is None:
                log.warning("%s: skipping a Period with resolution %r", self.key, period.findtext("resolution"))
                continue
            points: dict[int, float] = {}
            highest = 0
            for point in period.findall("Point"):
                try:
                    position = int(point.findtext("position") or 0)
                    raw = point.findtext("price.amount") or point.findtext("quantity")
                    points[position] = float(raw)
                except (TypeError, ValueError):
                    continue
                highest = max(highest, position)
            # Sparse positions mean "unchanged since the last one".
            carried: float | None = None
            for position in range(1, highest + 1):
                carried = points.get(position, carried)
                if carried is None:
                    continue
                out.append(Sample(urn, self.property_name, carried, iso_z(begins + (position - 1) * step)))
        return out

    def fetch(self, device: dict[str, Any], start: datetime, end: datetime) -> list[Sample]:
        eic = str(device["areaCode"])
        if self.rolling and start == end:
            day = start.replace(hour=0, minute=0, second=0, microsecond=0)
            start, end = day, day + timedelta(days=2)
        return self.samples(self._query(eic, start, end), device["urn"])
