"""
datagates.core.fieldmap — the `fields:` / `columns:` block every gate shares.

Almost every gate maps something the upstream calls `T_ROOM_1` or column 7
or register 40003 onto a controlledProperty with a unit code:

    fields:
      T_ROOM_1: {property: temperature, unit: CEL}
      E_TOT:    {property: energy, unit: KWH, cumulative: true}
      CO2:      {property: co2, unit: "59", invalid: [-9999], min: 0, max: 5000}

Having one implementation means the same YAML behaves the same in the CSV
gate, the Modbus gate and the SQL gate, and that the traps found in
production are handled once:

- legacy exports write decimal commas (`0,7`) and thousands separators;
- they mark "no reading" with a sentinel (-9999, 32767, 9999.9) rather
  than an empty cell, and a sentinel stored as a value ruins every
  average a consumer computes afterwards;
- NaN and Inf reach InfluxDB as a write error for the whole batch;
- booleans from BACnet binary inputs and Modbus coils must become 0/1.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from dataclasses import field as _field
from typing import Any

from datagates.gates.base import Sample

DEFAULT_UNIT = "C62"          # UN/CEFACT "one", the unitless unit


def to_float(raw: Any) -> float | None:
    """Best-effort numeric conversion of whatever an upstream sent.
    None when the value is absent, textual, or not a finite number."""
    if raw is None or isinstance(raw, bool):
        return None if raw is None else float(raw)
    if isinstance(raw, int | float):
        val = float(raw)
    else:
        text = str(raw).strip().replace(" ", "").replace(" ", "")
        if not text or text.lower() in ("nan", "null", "none", "-", "na", "n/a", "#n/a"):
            return None
        try:
            val = float(text)
        except ValueError:
            # Mixed separators: the rightmost of "," and "." is the decimal
            # point and the other is a thousands separator, so "1.234,56"
            # and "1,234.56" both read as 1234.56. A lone comma is decimal
            # ("0,7" -> 0.7), which is how European exports write it; a
            # thousands separator therefore has to be a space or a dot.
            cleaned = (text.replace(".", "").replace(",", ".") if text.rfind(",") > text.rfind(".")
                       else text.replace(",", ""))
            try:
                val = float(cleaned)
            except ValueError:
                return None
    return None if math.isnan(val) or math.isinf(val) else val


@dataclass(frozen=True)
class Field:
    """One upstream field and what it becomes."""
    source: str                       # upstream key: column name, register, OID, node id
    property: str                     # controlledProperty on the summary entity
    unit: str = DEFAULT_UNIT          # UN/CEFACT code
    scale: float = 1.0
    offset: float = 0.0
    cumulative: bool = False
    invalid: tuple[float, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    extra: dict[str, Any] = _field(default_factory=dict)   # per-gate keys (register, oid, node_id, ...)

    def convert(self, raw: Any) -> float | None:
        """Raw upstream value -> stored value, or None to drop the reading."""
        val = to_float(raw)
        if val is None or any(val == bad for bad in self.invalid):
            return None
        val = val * self.scale + self.offset
        if (self.minimum is not None and val < self.minimum) or (self.maximum is not None and val > self.maximum):
            return None
        return val

    def get(self, name: str, default: Any = None) -> Any:
        return self.extra.get(name, default)


_KNOWN = {"property", "unit", "scale", "offset", "cumulative", "invalid", "min", "max"}


class FieldMap:
    """The parsed `fields:` block: ordered Fields, the properties dict a
    DeviceSpec needs, and the cumulative set the counter guard needs."""

    def __init__(self, spec: dict[str, Any] | None, *, gate_key: str = "", block: str = "fields",
                 default_unit: str = DEFAULT_UNIT, required: bool = True) -> None:
        spec = spec or {}
        if required and not spec:
            raise ValueError(f"{gate_key}: options.{block} must map upstream fields to properties")
        fields: list[Field] = []
        for source, raw in spec.items():
            cfg = dict(raw) if isinstance(raw, dict) else {"property": str(raw)}
            prop = str(cfg.get("property") or source)
            invalid = cfg.get("invalid")
            invalid = (invalid,) if isinstance(invalid, int | float) else tuple(invalid or ())
            fields.append(Field(
                source=str(source), property=prop,
                unit=str(cfg.get("unit", default_unit)),
                scale=float(cfg.get("scale", 1.0)), offset=float(cfg.get("offset", 0.0)),
                cumulative=bool(cfg.get("cumulative", False)),
                invalid=tuple(float(v) for v in invalid),
                minimum=None if cfg.get("min") is None else float(cfg["min"]),
                maximum=None if cfg.get("max") is None else float(cfg["max"]),
                extra={k: v for k, v in cfg.items() if k not in _KNOWN},
            ))
        self.fields: tuple[Field, ...] = tuple(fields)

    def __iter__(self):
        return iter(self.fields)

    def __len__(self) -> int:
        return len(self.fields)

    @property
    def properties(self) -> dict[str, str]:
        """controlledProperty -> unit code. One summary entity per key."""
        return {f.property: f.unit for f in self.fields}

    @property
    def cumulative(self) -> frozenset[str]:
        return frozenset(f.property for f in self.fields if f.cumulative)

    def by_source(self, source: str) -> Field | None:
        return next((f for f in self.fields if f.source == source), None)

    def samples(self, urn: str, row: dict[str, Any], observed_at: str) -> list[Sample]:
        """Every mapped field of one row that converts to a finite number."""
        out: list[Sample] = []
        for f in self.fields:
            if f.source not in row:
                continue
            val = f.convert(row[f.source])
            if val is not None:
                out.append(Sample(urn, f.property, val, observed_at))
        return out
