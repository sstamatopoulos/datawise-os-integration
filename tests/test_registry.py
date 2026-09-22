"""gates.yaml -> Gate instances, with env expansion and validation."""
from __future__ import annotations

import importlib
import sys
import textwrap

import pytest

from datagates.gates.base import Gate
from datagates.gates.registry import BUILTIN_TYPES, load_config, load_gates, resolve_type

# Secrets the shipped examples reference. Unset variables expand to an empty
# string, which is fine everywhere except where a gate refuses to be built
# without the value.
EXAMPLE_SECRETS = {"TB_USERNAME": "u", "TB_PASSWORD": "p", "MESH_API_KEY": "k", "ENTSOE_TOKEN": "t"}

# Third-party packages a gate may import lazily. None of them is installed in
# CI, and none of them may be needed to *build* a gate or a DAG.
OPTIONAL_DEPENDENCIES = ("pymodbus", "BAC0", "asyncua", "snap7", "pysnmp", "paho", "paramiko")


@pytest.fixture
def example_env(monkeypatch):
    for name, value in EXAMPLE_SECRETS.items():
        monkeypatch.setenv(name, value)


def test_every_builtin_type_has_a_working_example_in_the_shipped_config(example_env):
    gates = load_gates(include_disabled=True)
    assert {g.type_name for g in gates} == set(BUILTIN_TYPES), \
        "config/gates.yaml is the catalogue: every built-in type needs an example entry there"
    assert len(BUILTIN_TYPES) >= 20, "the README promises more than twenty built-in gates"
    for gate in gates:
        assert gate.discover(), f"{gate.key}: discover() returned no devices from its options"
        for spec in gate.discover():
            assert spec.properties, f"{gate.key}: {spec.name} measures nothing"
            assert all(unit for unit in spec.properties.values()), f"{gate.key}: a property has no unit code"


def test_the_shipped_config_enables_only_the_credential_free_gates(example_env):
    assert {g.key for g in load_gates()} == {"weather_forecast", "weather_observed", "prices"}


def test_gate_keys_and_dag_ids_do_not_collide(example_env):
    configs = load_config()
    keys = [c.key for c in configs]
    assert len(keys) == len(set(keys))
    assert all(not a.startswith(f"{b}_") for a in keys for b in keys if a != b), \
        "one key must not prefix another, or their DAG ids overlap"


def test_no_gate_module_needs_an_optional_dependency_to_import(monkeypatch):
    """A worker without pymodbus must still load the DAG bag: the driver
    imports live inside the methods that use them. This is what keeps one
    unused gate type from taking down every DAG in the deployment."""
    for name in list(sys.modules):
        if name.split(".")[0] in OPTIONAL_DEPENDENCIES:
            monkeypatch.delitem(sys.modules, name)
    monkeypatch.setattr("builtins.__import__", _refuse_optional(__import__))
    for target in BUILTIN_TYPES.values():
        module_name, class_name = target.split(":")
        module = importlib.reload(importlib.import_module(module_name))
        assert issubclass(getattr(module, class_name), Gate)


def _refuse_optional(real_import):
    def guard(name, *args, **kwargs):
        if name.split(".")[0] in OPTIONAL_DEPENDENCIES:
            raise ImportError(f"{name} is not installed on this worker")
        return real_import(name, *args, **kwargs)
    return guard


def test_env_expansion_and_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRET", "s3cr3t")
    cfg = tmp_path / "g.yaml"
    cfg.write_text(textwrap.dedent("""
        gates:
          - key: prices
            type: nordpool
            options: {areas: [ee], token: "${SECRET}", other: "${MISSING:-dflt}"}
    """), encoding="utf-8")
    (g,) = load_gates(cfg)
    assert g.options["token"] == "s3cr3t" and g.options["other"] == "dflt"
    assert g.source == "nordpool" and g.data_provider == "prices" and g.bucket == "telemetry"
    assert g.areas == ["EE"]
    assert g.schedule == g.default_schedule


def test_request_policy_comes_from_the_framework_not_the_gate(tmp_path):
    cfg = tmp_path / "g.yaml"
    cfg.write_text(textwrap.dedent("""
        gates:
          - key: slow_portal
            type: http_csv
            max_attempts: 2
            min_interval_s: 1.5
            options:
              url: "https://portal.example.org/export"
              columns: {kWh: {property: energyConsumption, unit: KWH}}
              devices: [{id: "1"}]
          - key: default_portal
            type: http_csv
            options:
              url: "https://portal.example.org/export"
              columns: {kWh: {property: energyConsumption, unit: KWH}}
              devices: [{id: "1"}]
    """), encoding="utf-8")
    slow, default = load_gates(cfg)
    assert (slow.max_attempts, slow.min_interval_s) == (2, 1.5)
    assert slow.client.max_attempts == 2 and slow.client.min_interval_s == 1.5
    assert (default.max_attempts, default.min_interval_s) == (4, 0.0)


def test_bad_keys_are_rejected(tmp_path):
    cfg = tmp_path / "g.yaml"
    cfg.write_text("gates:\n  - {key: Bad-Key, type: nordpool}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="lowercase"):
        load_config(cfg)
    cfg.write_text("gates:\n  - {key: a, type: nordpool}\n  - {key: a, type: nordpool}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        load_config(cfg)


def test_custom_type_by_import_path():
    cls = resolve_type("datagates.gates.nordpool:NordPoolGate")
    assert issubclass(cls, Gate)
    with pytest.raises(ValueError, match="unknown gate type"):
        resolve_type("nope")


def test_missing_config_is_empty(tmp_path):
    assert load_config(tmp_path / "absent.yaml") == []
