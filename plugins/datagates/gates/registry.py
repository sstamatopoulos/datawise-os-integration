"""
datagates.gates.registry — gates.yaml -> Gate instances.

    gates:
      - key: weather
        type: open_meteo
        schedule: "0 */6 * * *"
        options:
          locations: [...]
      - key: prices_lv
        type: nordpool
        options: {areas: [LV]}
      - key: my_scada
        type: "my_package.gates:ScadaGate"      # any importable class

Option values may reference environment variables as ${VAR} or
${VAR:-default}; that is how secrets stay out of the file.
"""
from __future__ import annotations

import importlib
import os
import re
from pathlib import Path
from typing import Any

import yaml

from datagates.core.settings import GATES_CONFIG
from datagates.gates.base import Gate, GateConfig

# Built-in gate types. Import lazily so one broken gate does not take the
# whole DAG bag down.
BUILTIN_TYPES: dict[str, str] = {
    # -- field protocols: what the plant room speaks ----------------------
    "modbus":      "datagates.gates.modbus:ModbusGate",
    "bacnet":      "datagates.gates.bacnet:BacnetGate",
    "opcua":       "datagates.gates.opcua:OpcUaGate",
    "s7":          "datagates.gates.s7:S7Gate",
    "snmp":        "datagates.gates.snmp:SnmpGate",
    # -- files somebody exports every night --------------------------------
    "csv_drop":    "datagates.gates.csv_drop:CsvDropGate",
    "excel_drop":  "datagates.gates.excel_drop:ExcelDropGate",
    "xml_drop":    "datagates.gates.xml_drop:XmlDropGate",
    "remote_drop": "datagates.gates.remote_drop:RemoteDropGate",
    # -- web APIs, old and new ---------------------------------------------
    "http_json":   "datagates.gates.http_json:HttpJsonGate",
    "http_xml":    "datagates.gates.http_xml:HttpXmlGate",
    "http_csv":    "datagates.gates.http_csv:HttpCsvGate",
    "obix":        "datagates.gates.obix:ObixGate",
    "zabbix":      "datagates.gates.zabbix:ZabbixGate",
    "thingsboard": "datagates.gates.thingsboard:ThingsBoardGate",
    "mqtt":        "datagates.gates.mqtt:MqttGate",
    # -- other stores to read from -----------------------------------------
    "sql":           "datagates.gates.sql:SqlGate",
    "influx_source": "datagates.gates.influx_source:InfluxSourceGate",
    "prometheus":    "datagates.gates.prometheus:PrometheusGate",
    "ngsi_ld":       "datagates.gates.ngsi_ld:NgsiLdGate",
    "ngsi_v2":       "datagates.gates.ngsi_v2:NgsiV2Gate",
    # -- weather and markets ------------------------------------------------
    "open_meteo":  "datagates.gates.open_meteo:OpenMeteoGate",
    "nordpool":    "datagates.gates.nordpool:NordPoolGate",
    "entsoe":      "datagates.gates.entsoe:EntsoeGate",
}

_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), value)
    if isinstance(value, list):
        return [_expand(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    return value


def resolve_type(type_name: str) -> type[Gate]:
    target = BUILTIN_TYPES.get(type_name, type_name)
    if ":" not in target:
        raise ValueError(f"unknown gate type {type_name!r}; built-ins: {sorted(BUILTIN_TYPES)} "
                         "or give 'module.path:ClassName'")
    module_name, class_name = target.split(":", 1)
    cls = getattr(importlib.import_module(module_name), class_name)
    if not (isinstance(cls, type) and issubclass(cls, Gate)):
        raise TypeError(f"{target} is not a Gate subclass")
    return cls


def load_config(path: str | Path | None = None) -> list[GateConfig]:
    p = Path(path or GATES_CONFIG)
    if not p.exists():
        return []
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    configs: list[GateConfig] = []
    seen: set[str] = set()
    for item in raw.get("gates", []) or []:
        item = _expand(dict(item))
        cfg = GateConfig(**{k: v for k, v in item.items() if k in GateConfig.__dataclass_fields__})
        if not re.fullmatch(r"[a-z][a-z0-9_]*", cfg.key):
            raise ValueError(f"gate key {cfg.key!r} must be lowercase letters, digits, underscores")
        if cfg.key in seen:
            raise ValueError(f"duplicate gate key {cfg.key!r}")
        seen.add(cfg.key)
        configs.append(cfg)
    return configs


def load_gates(path: str | Path | None = None, *, include_disabled: bool = False) -> list[Gate]:
    gates: list[Gate] = []
    for cfg in load_config(path):
        if not cfg.enabled and not include_disabled:
            continue
        gates.append(resolve_type(cfg.type)(cfg))
    return gates
