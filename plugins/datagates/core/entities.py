"""
datagates.core.entities — NGSI-LD entity builders and readers shared by the
DAG factory, the gates and the tests. Importable without Airflow.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from datagates.core.influx_writer import influx_pointer_attrs
from datagates.core.settings import DEFAULT_CONTEXT
from datagates.gates.base import BuildingSpec, DeviceSpec, Gate


def prop(value: Any) -> dict[str, Any]:
    """Wrap a plain value as an NGSI-LD Property; pass NGSI-LD through."""
    if isinstance(value, dict) and "type" in value and ("value" in value or "object" in value):
        return value
    return {"type": "Property", "value": value}


def building_entity(spec: BuildingSpec, gate: Gate) -> dict[str, Any]:
    return {
        "id": spec.urn, "type": "Building",
        "name": prop(spec.name),
        "dataProvider": prop(gate.data_provider),
        "source": prop(gate.source),
        **{k: prop(v) for k, v in spec.attrs.items() if v is not None},
        "@context": DEFAULT_CONTEXT,
    }


def device_entity(spec: DeviceSpec, gate: Gate) -> dict[str, Any]:
    """`location` is the reserved NGSI-LD GeoProperty: only coordinates go
    there. Anything descriptive about a place belongs in another attribute."""
    e = {
        "id": spec.urn, "type": spec.entity_type,
        "name": prop(spec.name),
        "category": prop(list(spec.category)),
        "controlledProperty": prop(list(spec.properties)),
        "dataProvider": prop(gate.data_provider),
        "source": prop(gate.source),
        "dataGate": prop(gate.key),
        **influx_pointer_attrs(gate.bucket),
        **{k: prop(v) for k, v in spec.attrs.items() if v is not None and k != "location"},
        "@context": DEFAULT_CONTEXT,
    }
    if spec.ref_building:
        e["refBuilding"] = {"type": "Relationship", "object": spec.ref_building}
    if spec.location:
        lon, lat = spec.location
        e["location"] = {"type": "GeoProperty",
                         "value": {"type": "Point", "coordinates": [float(lon), float(lat)]}}
    return e


def value(entity: dict[str, Any], name: str, default: Any = None) -> Any:
    v = entity.get(name)
    if isinstance(v, dict):
        return v.get("value", v.get("object", default))
    return default if v is None else v


def as_list(v: Any) -> list:
    """Orion-LD compacts a one-element array to a scalar on output, so a
    Device with one controlledProperty reads back as a string. Indexing
    that string gives its first letter. Always normalise first."""
    if v is None:
        return []
    return list(v) if isinstance(v, list | tuple) else [v]


def device_doc(entity: dict[str, Any], gate: Gate) -> dict[str, Any]:
    """The flat dict a gate's fetch() receives: urn, name, properties,
    watermark, plus the attributes the gate asked for in device_attrs."""
    doc = {
        "urn": entity["id"],
        "name": value(entity, "name", ""),
        "properties": as_list(value(entity, "controlledProperty")),
        "last_ingested_ms": int(value(entity, "lastIngestedAt", 0) or 0),
    }
    for a in gate.device_attrs:
        doc[a] = value(entity, a)
    return doc


def iso_z(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def ms_of(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)
