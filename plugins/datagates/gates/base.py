"""
datagates.gates.base — the contract a data gate implements.

A gate is one upstream system, described declaratively in gates.yaml and
implemented as a small Python class. The framework (datagates.dags.factory)
turns each configured gate into three Airflow DAGs:

    <key>_init      register Buildings and Devices in Orion-LD, seed the
                    InfluxDB measurements, create the summary entities
    <key>_run       scheduled: fetch what is new since each Device's
                    watermark, store it, refresh the summaries
    <key>_backfill  walk each Device's history backwards to history_start

A gate implements two methods and declares a few facts:

    discover()             -> the Devices this gate serves, with their
                              measured properties and unit codes
    fetch(device, a, b)    -> the samples of one Device between two instants

and, per gate type, sensible defaults for the schedule, the longest window
the upstream accepts per request, the earliest history available, which
properties are cumulative counters (guarded at ingestion) and which of a
Device's attributes fetch() needs read back from Orion.

The data model the framework writes is fixed and documented in
docs/data-model.md: Building / Device entities in Orion-LD, one summary
DeviceMeasurement per (Device, controlledProperty) holding the latest value
and rolling 24 h statistics, and the full series in InfluxDB under the
summary's URN as measurement name.
"""
from __future__ import annotations

import uuid as _uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, ClassVar

# One namespace for every deterministic URN the framework mints. Keyed on
# (gate key, upstream identity) so re-running init reuses the same ids.
DEVICE_NS = _uuid.UUID("a1b2c3d4-e5f6-7890-abcd-ef1234567890")


def device_urn(gate_key: str, *identity: Any) -> str:
    """Deterministic Device URN from the upstream identity of a device.
    The same (gate, identity) always yields the same URN, so the
    summaries and InfluxDB measurements hang off a stable name."""
    key = "|".join([gate_key, *[str(i) for i in identity]])
    return f"urn:ngsi-ld:Device:{_uuid.uuid5(DEVICE_NS, key)}"


def building_urn(*identity: Any) -> str:
    key = "|".join(["building", *[str(i) for i in identity]])
    return f"urn:ngsi-ld:Building:{_uuid.uuid5(DEVICE_NS, key)}"


@dataclass
class BuildingSpec:
    urn: str
    name: str
    attrs: dict[str, Any] = field(default_factory=dict)   # plain values -> Property


@dataclass
class DeviceSpec:
    """One device as the gate sees it. `properties` maps each
    controlledProperty to its UN/CEFACT unit code and is the contract:
    exactly one summary entity is created per name in it."""
    urn: str
    name: str
    properties: dict[str, str]
    ref_building: str | None = None
    category: list[str] = field(default_factory=lambda: ["sensor"])
    entity_type: str = "Device"
    attrs: dict[str, Any] = field(default_factory=dict)   # plain values -> Property
    location: tuple[float, float] | None = None           # (lon, lat) -> GeoProperty


@dataclass
class Sample:
    device_urn: str
    controlled_property: str
    value: float
    observed_at: str          # ISO 8601 UTC, e.g. 2026-09-16T15:00:00Z

    def as_point(self) -> dict[str, Any]:
        return {"device_urn": self.device_urn, "controlled_property": self.controlled_property,
                "value": float(self.value), "observed_at": self.observed_at}


@dataclass
class GateConfig:
    """What gates.yaml says about one gate instance."""
    key: str                       # DAG id prefix and the identity namespace
    type: str                      # gate type name or "module:Class"
    enabled: bool = True
    source: str = ""               # `source` attribute on entities; defaults to type
    data_provider: str = ""        # `dataProvider` attribute; defaults to key
    bucket: str = ""               # InfluxDB bucket; defaults to INFLUX_DEFAULT_BUCKET
    schedule: str | None = None    # cron or "@hourly"; None = gate type default
    history_start: str | None = None      # ISO date; None = gate type default
    live_lookback_days: int | None = None  # first run of a Device pulls this much
    max_window_days: int | None = None     # longest span per upstream request
    max_attempts: int | None = None        # retries on transient upstream failures
    min_interval_s: float | None = None    # minimum delay between upstream requests
    tags: list[str] = field(default_factory=list)
    options: dict[str, Any] = field(default_factory=dict)


class Gate(ABC):
    """Base class. Subclasses set the ClassVar defaults and implement
    discover() and fetch(); everything else is optional."""

    type_name: ClassVar[str] = "gate"
    default_schedule: ClassVar[str] = "0 * * * *"
    default_history_start: ClassVar[str | None] = None
    default_live_lookback_days: ClassVar[int] = 7
    default_max_window_days: ClassVar[int] = 30
    # controlledProperty names that are running totals: the counter guard
    # rejects readings a counter cannot have produced (see core.counter_guard)
    cumulative: ClassVar[frozenset[str]] = frozenset()
    # Device attributes (plain names) that fetch() needs read back from Orion
    device_attrs: ClassVar[tuple[str, ...]] = ()
    # "cursor": walk back window by window with state on the Device (default)
    # "stateless": one pass over [history_start, now), idempotent, no state
    # "none": no backfill DAG (pure forecasts)
    backfill_mode: ClassVar[str] = "cursor"
    # Forecast-like feeds: the run calls fetch(device, now, now) once and the
    # gate chooses its own window; nothing is filtered by the watermark, so
    # every returned hour overwrites what was stored for it.
    rolling: ClassVar[bool] = False
    # Re-fetch this much before the watermark on every run (0 = only new
    # data). For sources that revise recent values, e.g. a reanalysis.
    rewrite_lookback: ClassVar[timedelta] = timedelta(0)
    # Request policy defaults; gates.yaml overrides them per gate and
    # core.http applies them, so no gate implements retrying itself.
    default_max_attempts: ClassVar[int] = 4
    default_min_interval_s: ClassVar[float] = 0.0
    # Free-text: which upstream product family this gate speaks to, shown
    # in the catalogue in docs/gates.md.
    speaks: ClassVar[str] = ""

    def __init__(self, config: GateConfig) -> None:
        self.config = config
        self.key = config.key
        self.options = dict(config.options)
        self.source = config.source or self.type_name
        self.data_provider = config.data_provider or config.key
        from datagates.core.settings import INFLUX_DEFAULT_BUCKET
        self.bucket = config.bucket or INFLUX_DEFAULT_BUCKET
        self.schedule = config.schedule or self.default_schedule
        hs = config.history_start or self.default_history_start
        self.history_start: date | None = date.fromisoformat(hs) if hs else None
        self.live_lookback = timedelta(days=config.live_lookback_days or self.default_live_lookback_days)
        self.max_window = timedelta(days=config.max_window_days or self.default_max_window_days)
        self.max_attempts = int(config.max_attempts or self.default_max_attempts)
        self.min_interval_s = float(config.min_interval_s if config.min_interval_s is not None
                                    else self.default_min_interval_s)
        self.tags = ["datagates", self.type_name, *config.tags]

    # -- what every gate provides ------------------------------------------
    def buildings(self) -> list[BuildingSpec]:
        """Buildings this gate introduces (optional). Devices reference
        them by URN through DeviceSpec.ref_building."""
        return []

    @abstractmethod
    def discover(self) -> list[DeviceSpec]:
        """The Devices this gate serves, as read from the upstream or from
        the gate's options. Called by <key>_init."""

    @abstractmethod
    def fetch(self, device: dict[str, Any], start: datetime, end: datetime) -> list[Sample]:
        """Samples of one Device with observed_at in (start, end].
        `device` carries urn, name and the attributes named in
        device_attrs, as stored in Orion. Both bounds are aware UTC
        datetimes and their span never exceeds max_window."""

    def status(self, devices: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Optional: current status attributes per Device URN (battery,
        signal, last seen ...) patched onto the Device entities each run."""
        return {}

    # -- helpers ------------------------------------------------------------
    def urn(self, *identity: Any) -> str:
        return device_urn(self.key, *identity)

    def option(self, name: str, default: Any = None) -> Any:
        return self.options.get(name, default)

    def required(self, name: str) -> Any:
        """An option the gate cannot run without. Raised at construction,
        so a misconfigured gate is visible in the DAG list, not at 3 a.m."""
        value = self.options.get(name)
        if value in (None, "", [], {}):
            raise ValueError(f"{self.key}: options.{name} is required for a {self.type_name} gate")
        return value

    def devices_option(self, name: str = "devices") -> list[dict[str, Any]]:
        """The `devices:` list, normalised to dicts with an `id`."""
        out: list[dict[str, Any]] = []
        for entry in self.option(name, []) or []:
            item = dict(entry) if isinstance(entry, dict) else {"id": entry}
            if "id" not in item:
                raise ValueError(f"{self.key}: every entry of options.{name} needs an 'id'")
            out.append(item)
        return out
