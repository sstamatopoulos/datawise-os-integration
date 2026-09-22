"""
Open-Meteo gate — hourly weather forecast or ERA5 reanalysis per location.

    - key: weather_forecast
      type: open_meteo
      options:
        mode: forecast            # or "observed" (archive / reanalysis)
        forecast_hours: 48
        locations:
          - id: site-a
            name: Site A
            lat: 56.9184
            lon: 24.0352
            ref_building: urn:ngsi-ld:Building:...   # optional

No API key. Free for non-commercial use, 10 000 calls/day.
https://open-meteo.com/en/docs and /en/docs/historical-weather-api

Two things checked live that the docs do not say: the archive serves no
visibility / precipitation probability / UV index, and the default wind
unit is km/h (we ask for m/s so the MTS unit code is true).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import requests
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from datagates.gates.base import DeviceSpec, Gate, Sample

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"


@dataclass(frozen=True)
class Variable:
    api_name: str
    controlled_property: str
    unit_code: str
    scale: float = 1.0
    forecast: bool = True
    archive: bool = True


VARIABLES: tuple[Variable, ...] = (
    Variable("temperature_2m",            "temperature",              "CEL"),
    Variable("apparent_temperature",      "feelsLikeTemperature",     "CEL"),
    Variable("pressure_msl",              "atmosphericPressure",      "A97"),
    Variable("relative_humidity_2m",      "relativeHumidity",         "P1",  scale=0.01),
    Variable("visibility",                "visibility",               "MTR", archive=False),
    Variable("wind_speed_10m",            "windSpeed",                "MTS"),
    Variable("wind_direction_10m",        "windDirection",            "DD"),
    Variable("wind_gusts_10m",            "gustSpeed",                "MTS"),
    Variable("precipitation",             "precipitation",            "MMT"),
    Variable("precipitation_probability", "precipitationProbability", "C62", scale=0.01, archive=False),
    Variable("uv_index",                  "uVIndexMax",               "C62", archive=False),
)
_COMMON = {"timezone": "UTC", "timeformat": "unixtime", "wind_speed_unit": "ms"}


def _transient(exc: BaseException) -> bool:
    if isinstance(exc, requests.ConnectionError | requests.Timeout):
        return True
    r = getattr(exc, "response", None)
    return r is not None and (r.status_code == 429 or r.status_code >= 500)


@retry(retry=retry_if_exception(_transient), stop=stop_after_attempt(4),
       wait=wait_exponential(min=2, max=20), reraise=True)
def _get(url: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
    r = requests.get(url, params=params, timeout=timeout)
    if r.status_code == 400:
        raise RuntimeError(f"open-meteo: {r.json().get('reason', r.text)}")
    r.raise_for_status()
    return r.json()


def hourly_samples(payload: dict[str, Any], urn: str, variables) -> list[Sample]:
    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []
    out: list[Sample] = []
    for v in variables:
        col = hourly.get(v.api_name)
        if not col:
            continue
        for ts, raw in zip(times, col, strict=False):
            if raw is None:
                continue
            try:
                val = float(raw)
            except (TypeError, ValueError):
                continue
            if math.isnan(val) or math.isinf(val):
                continue
            iso = datetime.fromtimestamp(int(ts), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            out.append(Sample(urn, v.controlled_property, val * v.scale, iso))
    return out


class OpenMeteoGate(Gate):
    type_name = "open_meteo"
    speaks = "Open-Meteo forecast and ERA5 reanalysis"
    default_schedule = "0 */6 * * *"
    default_history_start = "2024-01-01"
    default_max_window_days = 92
    device_attrs = ("latitude", "longitude", "weatherMode")

    def __init__(self, config):
        super().__init__(config)
        self.mode = self.option("mode", "forecast")
        if self.mode not in ("forecast", "observed"):
            raise ValueError(f"{self.key}: mode must be 'forecast' or 'observed'")
        self.hours = int(self.option("forecast_hours", 48))
        self.timeout = float(self.option("timeout", 30))
        self.variables = tuple(v for v in VARIABLES if (v.forecast if self.mode == "forecast" else v.archive))
        if self.mode == "forecast":
            self.schedule = config.schedule or "0 */6 * * *"
        else:
            self.schedule = config.schedule or "0 3 * * *"

    # forecast: rolling window, no backfill. observed: daily, re-fetch a week.
    @property
    def rolling(self) -> bool:  # type: ignore[override]
        return self.mode == "forecast"

    @property
    def backfill_mode(self) -> str:  # type: ignore[override]
        return "none" if self.mode == "forecast" else "stateless"

    @property
    def rewrite_lookback(self) -> timedelta:  # type: ignore[override]
        return timedelta(0) if self.mode == "forecast" else timedelta(days=7)

    def discover(self) -> list[DeviceSpec]:
        kind = "WeatherForecastLocation" if self.mode == "forecast" else "WeatherObservedLocation"
        out = []
        for loc in self.option("locations", []) or []:
            out.append(DeviceSpec(
                urn=f"urn:ngsi-ld:{kind}:{self.key}-{loc['id']}",
                entity_type=kind,
                name=str(loc.get("name") or loc["id"]),
                properties={v.controlled_property: v.unit_code for v in self.variables},
                ref_building=loc.get("ref_building"),
                category=["weather"],
                location=(float(loc["lon"]), float(loc["lat"])),
                attrs={"latitude": float(loc["lat"]), "longitude": float(loc["lon"]),
                       "weatherMode": self.mode, "locationId": loc["id"]},
            ))
        return out

    def fetch(self, device: dict[str, Any], start: datetime, end: datetime) -> list[Sample]:
        lat, lon = float(device["latitude"]), float(device["longitude"])
        if self.mode == "forecast":
            payload = _get(FORECAST_URL, {"latitude": lat, "longitude": lon, "forecast_hours": self.hours,
                                          "hourly": ",".join(v.api_name for v in self.variables), **_COMMON},
                           self.timeout)
            return hourly_samples(payload, device["urn"], self.variables)
        today = datetime.now(UTC).date()
        a: date = start.date()
        b: date = min(end.date(), today - timedelta(days=1))   # the archive is complete only to yesterday
        if a > b:
            return []
        payload = _get(ARCHIVE_URL, {"latitude": lat, "longitude": lon,
                                     "start_date": a.isoformat(), "end_date": b.isoformat(),
                                     "hourly": ",".join(v.api_name for v in self.variables), **_COMMON},
                       self.timeout)
        return hourly_samples(payload, device["urn"], self.variables)
