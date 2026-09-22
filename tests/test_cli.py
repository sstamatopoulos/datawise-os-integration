"""The CLI: what an integrator runs before trusting a gate to Airflow.

Every test here goes through main() with real arguments, because the thing
being tested is the command line, not the functions behind it. No network:
the gates used read files from tmp_path or have their fetch mocked.
"""
from __future__ import annotations

import json
import textwrap
from unittest.mock import patch

import pytest

from datagates.cli import _kind, main
from datagates.gates.registry import BUILTIN_TYPES, resolve_type

OK, FAILED, USAGE = 0, 1, 2


@pytest.fixture
def config(tmp_path):
    """A gates.yaml with one file gate over a directory holding one export."""
    (tmp_path / "drop").mkdir()
    (tmp_path / "drop" / "readings.csv").write_text(
        "Timestamp;MeterId;Energy\n"
        "2026-09-16 10:00;M1;1234,5\n"
        "2026-09-16 11:00;M1;1240,0\n"
        "2026-09-16 11:00;M2;9,9\n", encoding="utf-8")
    path = tmp_path / "gates.yaml"
    path.write_text(textwrap.dedent(f"""
        gates:
          - key: heat
            type: csv_drop
            options:
              directory: "{(tmp_path / 'drop').as_posix()}"
              delimiter: ";"
              timestamp_column: Timestamp
              timestamp_format: "%Y-%m-%d %H:%M"
              timezone: UTC
              device_column: MeterId
              columns:
                Energy: {{property: energy, unit: KWH, cumulative: true}}
              devices:
                - {{id: M1, name: Heat meter M1}}
          - key: prices
            type: nordpool
            enabled: false
            options: {{areas: [LV]}}
    """), encoding="utf-8")
    return str(path)


# ── types ───────────────────────────────────────────────────────────

def test_types_lists_the_catalogue_with_what_each_speaks_to(capsys):
    assert main(["types"]) == OK
    out = capsys.readouterr().out
    assert f"{len(BUILTIN_TYPES)} built-in types" in out
    for name in BUILTIN_TYPES:
        assert name in out
    assert "Modbus/TCP" in out and "Tridium Niagara" in out


def test_kind_reads_gates_that_decide_from_their_options():
    # A property on the class is not a string: reading backfill_mode off the
    # class mislabels every gate that chooses its own mode.
    assert _kind(resolve_type("modbus")) == "poll"
    assert _kind(resolve_type("csv_drop")) == "history"
    for name in ("opcua", "obix", "ngsi_v2", "open_meteo"):
        assert _kind(resolve_type(name)) == "either", name


# ── list ────────────────────────────────────────────────────────────

def test_list_shows_state_cadence_and_dag_count(capsys, config):
    assert main(["--config", config, "list"]) == OK
    out = capsys.readouterr().out
    assert "heat" in out and "csv_drop" in out and "enabled" in out
    assert "disabled" in out, "a disabled gate is still part of the configuration"
    assert "every 30 min" in out
    assert "2 gates configured, 1 enabled" in out


# ── check ───────────────────────────────────────────────────────────

def test_check_reports_devices_properties_and_guarded_counters(capsys, config):
    assert main(["--config", config, "check", "heat"]) == OK
    out = capsys.readouterr().out
    assert "heat (csv_drop)" in out
    assert "counters guarded: energy" in out
    assert "Heat meter M1" in out and "energy [KWH]" in out
    assert "urn:ngsi-ld:Device:" in out
    assert "1 device(s), 1 summary entities would be created" in out


def test_check_fails_loudly_when_discover_cannot_work(capsys, config):
    with patch("datagates.gates.csv_drop.CsvDropGate.discover",
               side_effect=PermissionError("the drop directory is not readable")):
        assert main(["--config", config, "check", "heat"]) == FAILED
    assert "FAILED discover(): PermissionError" in capsys.readouterr().out


def test_an_unknown_gate_key_is_a_usage_error_that_names_the_real_ones(capsys, config):
    assert main(["--config", config, "check", "heta"]) == USAGE
    out = capsys.readouterr().out
    assert "no gate named 'heta'" in out and "heat, prices" in out


# ── fetch ───────────────────────────────────────────────────────────

def test_fetch_pulls_a_window_and_summarises_per_property(capsys, config):
    assert main(["--config", config, "fetch", "heat",
                 "--start", "2026-09-16T00:00:00Z", "--end", "2026-09-17T00:00:00Z"]) == OK
    out = capsys.readouterr().out
    assert "nothing is written" in out
    assert "Heat meter M1: 2 sample(s)" in out
    assert "energy" in out and "1234.500" in out and "1240.000" in out
    assert "2026-09-16T10:00:00Z .. 2026-09-16T11:00:00Z" in out


def test_fetch_json_prints_the_samples_themselves(capsys, config):
    assert main(["--config", config, "fetch", "heat", "--json",
                 "--start", "2026-09-16T00:00:00Z", "--end", "2026-09-17T00:00:00Z"]) == OK
    points = json.loads(capsys.readouterr().out)
    assert [p["value"] for p in points] == [1234.5, 1240.0]
    assert points[0]["controlled_property"] == "energy"
    assert points[0]["observed_at"] == "2026-09-16T10:00:00Z"


def test_fetch_limit_and_device_selection(capsys, config):
    assert main(["--config", config, "fetch", "heat", "--json", "--limit", "1",
                 "--device", "Heat meter M1",
                 "--start", "2026-09-16T00:00:00Z", "--end", "2026-09-17T00:00:00Z"]) == OK
    assert len(json.loads(capsys.readouterr().out)) == 1


def test_fetch_says_an_empty_window_is_not_necessarily_a_fault(capsys, config):
    assert main(["--config", config, "fetch", "heat",
                 "--start", "2026-09-15T00:00:00Z", "--end", "2026-09-15T12:00:00Z"]) == OK
    assert "nothing in this window" in capsys.readouterr().out


def test_fetch_reports_the_upstream_failure_and_exits_non_zero(capsys, config):
    with patch("datagates.gates.csv_drop.CsvDropGate.fetch",
               side_effect=ConnectionError("no route to host")):
        assert main(["--config", config, "fetch", "heat"]) == FAILED
    assert "FAILED Heat meter M1: ConnectionError: no route to host" in capsys.readouterr().out


def test_a_rolling_gate_is_asked_for_its_own_window(capsys, config):
    with patch("datagates.gates.nordpool.NordPoolGate.fetch", return_value=[]) as fetch:
        assert main(["--config", config, "fetch", "prices"]) == OK
    _doc, start, end = fetch.call_args.args
    assert start == end, "a rolling gate chooses its window; the factory calls it with (now, now)"
    assert "the gate's own window" in capsys.readouterr().out


def test_a_bad_timestamp_is_a_usage_error(capsys, config):
    assert main(["--config", config, "fetch", "heat", "--start", "last tuesday"]) == USAGE
    assert "could not parse" in capsys.readouterr().out
