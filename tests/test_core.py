"""The shared machinery: field maps, timestamps, binary decoding, HTTP policy, XML rows.

These are the parts every gate leans on, so a bug here is a bug in
twenty integrations at once.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
import requests

from datagates.core.binary import decode, decode_bits, decode_registers, register_count, size_of
from datagates.core.cadence import describe, expected_interval
from datagates.core.fieldmap import FieldMap, to_float
from datagates.core.httpclient import HttpClient, auth_from_options, query_auth
from datagates.core.timeparse import iso_z, parse_stamp, zone_of
from datagates.core.xmlrows import parse_xml, pick, row_of, select, self_and_attributes

# ── to_float: what legacy exports actually contain ──────────────────

@pytest.mark.parametrize(("raw", "expected"), [
    ("21.4", 21.4), ("0,7", 0.7), ("1.234,56", 1234.56), ("1,234.56", 1234.56),
    ("1 234", 1234.0), (" 12 ", 12.0), (True, 1.0), (False, 0.0), (7, 7.0),
    ("", None), ("n/a", None), ("N/A", None), ("-", None), ("nan", None), (None, None),
    ("abc", None), (float("inf"), None), (float("nan"), None),
])
def test_to_float_handles_the_shapes_upstreams_send(raw, expected):
    assert to_float(raw) == expected


# ── FieldMap ────────────────────────────────────────────────────────

def test_fieldmap_exposes_properties_units_and_cumulative_set():
    fm = FieldMap({
        "T_ROOM": {"property": "temperature", "unit": "CEL", "scale": 0.1},
        "E_TOT": {"property": "energy", "unit": "KWH", "cumulative": True},
        "CO2": "co2",                                    # shorthand: just the property
    })
    assert fm.properties == {"temperature": "CEL", "energy": "KWH", "co2": "C62"}
    assert fm.cumulative == frozenset({"energy"})
    assert len(fm) == 3 and fm.by_source("CO2").property == "co2"


def test_fieldmap_applies_scale_offset_and_rejects_sentinels_and_out_of_range():
    fm = FieldMap({
        "t": {"property": "temperature", "unit": "CEL", "scale": 0.1, "offset": -273.15},
        "co2": {"property": "co2", "unit": "59", "invalid": [-9999, 32767], "min": 0, "max": 5000},
    })
    samples = fm.samples("urn:x", {"t": "3000", "co2": -9999}, "2026-09-16T15:00:00Z")
    assert [(s.controlled_property, round(s.value, 2)) for s in samples] == [("temperature", 26.85)]
    assert fm.samples("urn:x", {"co2": 9000}, "2026-09-16T15:00:00Z") == []
    assert fm.samples("urn:x", {"co2": 445}, "2026-09-16T15:00:00Z")[0].value == 445.0


def test_fieldmap_keeps_extra_keys_for_the_gate_that_needs_them():
    fm = FieldMap({"40001": {"property": "energy", "unit": "KWH", "type": "uint32", "table": "input"}})
    field = fm.by_source("40001")
    assert field.get("type") == "uint32" and field.get("table") == "input"
    assert field.get("word_order", "big") == "big"


def test_fieldmap_required_block_fails_at_construction():
    with pytest.raises(ValueError, match="options.fields"):
        FieldMap({}, gate_key="k", block="fields")
    assert len(FieldMap(None, required=False)) == 0


# ── timestamps ──────────────────────────────────────────────────────

def test_parse_stamp_formats():
    assert iso_z(parse_stamp("2026-09-16T15:00:00Z")) == "2026-09-16T15:00:00Z"
    assert iso_z(parse_stamp("2026-09-16 15:00:00 UTC")) == "2026-09-16T15:00:00Z"
    assert iso_z(parse_stamp("1789570800", "epoch_s")) == "2026-09-16T15:00:00Z"
    assert iso_z(parse_stamp(1789570800000, "epoch_ms")) == "2026-09-16T15:00:00Z"
    assert iso_z(parse_stamp("16.09.2026 18:00", "%d.%m.%Y %H:%M", zone_of("Europe/Riga"))) \
        == "2026-09-16T15:00:00Z"
    assert iso_z(parse_stamp(datetime(2026, 9, 16, 15, tzinfo=UTC))) == "2026-09-16T15:00:00Z"


def test_parse_stamp_returns_none_instead_of_raising_on_a_bad_row():
    assert parse_stamp("") is None and parse_stamp(None) is None
    assert parse_stamp("Total") is None
    assert parse_stamp("not a date", "%Y-%m-%d") is None


def test_unknown_timezone_fails_loudly():
    with pytest.raises(ValueError, match="unknown timezone"):
        zone_of("Europe/Atlantis")


# ── binary decoding ─────────────────────────────────────────────────

def test_decode_registers_word_order_is_the_classic_meter_trap():
    # 1 000 000 as uint32 is 0x000F4240: high word 0x000F, low word 0x4240
    assert decode_registers([0x000F, 0x4240], "uint32") == 1_000_000
    assert decode_registers([0x4240, 0x000F], "uint32", word_order="little") == 1_000_000
    # read with the wrong order it is not slightly wrong, it is nonsense
    assert decode_registers([0x000F, 0x4240], "uint32", word_order="little") == 1_111_490_575
    assert decode_registers([0xFFFF], "int16") == -1
    assert decode_registers([0xFFFF], "uint16") == 65535
    assert decode_registers([0x41A8, 0x0000], "float32") == pytest.approx(21.0)
    assert decode_registers([], "uint16") is None


def test_sizes_and_bits():
    assert register_count("uint16") == 1 and register_count("float32") == 2 and register_count("float64") == 4
    assert size_of("string:8") == 8
    assert decode(b"\x00", "float32") is None, "a short answer is not a zero reading"
    assert decode_bits([0b1000], 3) == 1 and decode_bits([0b1000], 2) == 0
    with pytest.raises(ValueError, match="unknown type"):
        size_of("word")


# ── HTTP policy ─────────────────────────────────────────────────────

def _response(status: int, headers=None):
    response = MagicMock(spec=requests.Response)
    response.status_code = status
    response.headers = headers or {}
    response.raise_for_status.side_effect = (
        None if status < 400 else requests.HTTPError(response=response))
    return response


def test_http_client_retries_transient_status_and_honours_retry_after():
    client = HttpClient(max_attempts=3)
    client.session = MagicMock()
    client.session.request.side_effect = [_response(429, {"Retry-After": "2"}), _response(200)]
    with patch("datagates.core.httpclient.time.sleep") as sleep:
        assert client.request("GET", "https://x/").status_code == 200
    assert sleep.call_args_list[-1].args[0] == 2.0, "Retry-After beats exponential backoff"
    assert client.session.request.call_count == 2


def test_http_client_does_not_retry_a_configuration_error():
    client = HttpClient(max_attempts=4)
    client.session = MagicMock()
    client.session.request.return_value = _response(401)
    with pytest.raises(requests.HTTPError):
        client.request("GET", "https://x/")
    assert client.session.request.call_count == 1, "a 401 is not transient"


def test_http_client_gives_up_after_max_attempts():
    client = HttpClient(max_attempts=2)
    client.session = MagicMock()
    client.session.request.return_value = _response(503)
    with patch("datagates.core.httpclient.time.sleep"), pytest.raises(requests.HTTPError):
        client.request("GET", "https://x/")
    assert client.session.request.call_count == 2


def test_http_client_throttles_to_min_interval():
    client = HttpClient(min_interval_s=0.5)
    client.session = MagicMock()
    client.session.request.return_value = _response(200)
    with patch("datagates.core.httpclient.time.sleep") as sleep:
        client.request("GET", "https://x/")
        client.request("GET", "https://x/")
    assert sleep.called and 0 < sleep.call_args.args[0] <= 0.5


def test_http_client_reports_html_where_json_was_expected():
    client = HttpClient()
    response = MagicMock(spec=requests.Response)
    response.content, response.text, response.url = b"<html>", "<html>login</html>", "https://x/"
    response.headers = {"Content-Type": "text/html"}
    response.json.side_effect = ValueError("no json")
    with pytest.raises(RuntimeError, match="expected JSON"):
        client._json(response)


def test_auth_options():
    assert auth_from_options({"auth": {"type": "basic", "username": "u", "password": "p"}}) == (("u", "p"), {})
    assert auth_from_options({"auth": {"type": "bearer", "token": "t"}}) == (None, {"Authorization": "Bearer t"})
    assert auth_from_options({"auth": {"type": "header", "name": "X-API-Key", "value": "k"}}) \
        == (None, {"X-API-Key": "k"})
    assert query_auth({"auth": {"type": "query", "name": "apikey", "value": "k"}}) == {"apikey": "k"}
    assert auth_from_options({}) == (None, {})
    with pytest.raises(ValueError, match="unknown auth type"):
        auth_from_options({"auth": {"type": "kerberos"}})


# ── XML rows ────────────────────────────────────────────────────────

DOC = """<?xml version="1.0"?>
<obj xmlns="http://obix.org/ns/schema/1.1" is="obix:HistoryQueryOut">
  <list name="data">
    <obj><abstime name="timestamp" val="2026-09-16T18:00:00+03:00"/><real name="value" val="21.4"/></obj>
    <obj><abstime name="timestamp" val="2026-09-16T18:15:00+03:00"/><real name="value" val="21.6"/></obj>
  </list>
</obj>"""


def test_xml_namespaces_are_stripped_so_a_mapping_survives_an_upstream_upgrade():
    root = parse_xml(DOC)
    rows = select(root, ".//list/obj")
    assert len(rows) == 2
    assert pick(rows[0], "abstime/@val") == "2026-09-16T18:00:00+03:00"
    assert pick(rows[0], "real/@val") == "21.4"
    assert row_of(rows[1], {"v": "real/@val"}) == {"v": "21.6"}
    assert parse_xml(DOC, keep_namespaces=True).tag.startswith("{http://obix.org")


def test_bare_tag_name_selects_anywhere_and_missing_selectors_are_none():
    root = parse_xml("<Doc><Reading time='t1'><Value>1.5</Value></Reading></Doc>")
    (row,) = select(root, "Reading")
    assert pick(row, "@time") == "t1" and pick(row, "Value") == "1.5"
    assert pick(row, "Absent") is None and pick(row, "Value/@unit") is None
    assert self_and_attributes(row) == {"time": "t1", "Value": "1.5"}


# ── cadence ─────────────────────────────────────────────────────────

@pytest.mark.parametrize(("schedule", "expected"), [
    ("*/5 * * * *", timedelta(minutes=5)),
    ("*/30 * * * *", timedelta(minutes=30)),
    ("0 * * * *", timedelta(hours=1)),
    ("0 */6 * * *", timedelta(hours=6)),
    ("0 3 * * *", timedelta(hours=24)),
    ("0 12,14 * * *", timedelta(hours=12)),
    ("0 6 * * 1", timedelta(days=7)),
    ("@hourly", timedelta(hours=1)),
    ("@daily", timedelta(days=1)),
    (None, None),
])
def test_expected_interval_of_the_schedules_the_gates_use(schedule, expected):
    assert expected_interval(schedule) == expected


def test_describe_reads_as_words():
    assert describe("*/5 * * * *") == "every 5 min"
    assert describe("0 */6 * * *") == "every 6 h"
    assert describe("0 3 * * *") == "every 1 d"
    assert describe(None) == "on demand"


def test_every_configured_gate_has_a_cadence_a_health_check_can_use(monkeypatch):
    """A gate whose schedule nothing can interpret cannot be monitored, so
    the shipped examples must all be interpretable."""
    for name, value in {"TB_USERNAME": "u", "TB_PASSWORD": "p", "INDOOR_AIR_API_KEY": "k",
                        "ENTSOE_TOKEN": "t"}.items():
        monkeypatch.setenv(name, value)
    from datagates.gates.registry import load_gates
    for gate in load_gates(include_disabled=True):
        assert expected_interval(gate.schedule), f"{gate.key}: schedule {gate.schedule!r} is not interpretable"


# ── the device query ────────────────────────────────────────────────

def test_a_gates_devices_are_found_whatever_entity_type_they_use():
    """A type filter of "Device" hid open_meteo's WeatherForecastLocation and
    entsoe's MarketPriceFeed devices, so their run DAGs mapped over nothing
    and reported success. The query must key on dataGate alone."""
    from datagates.core.orion_registry import OrionRegistry

    registry = OrionRegistry()
    with patch("datagates.core.orion_registry.requests.get") as get:
        get.return_value.json.return_value = []
        get.return_value.raise_for_status.return_value = None

        registry.get_all_devices(q='dataGate=="weather_forecast"')
        assert "type" not in get.call_args.kwargs["params"], \
            "filtering on type=Device silently drops gates with their own entity type"
        assert get.call_args.kwargs["params"]["q"] == 'dataGate=="weather_forecast"'

        registry.get_all_devices()
        assert get.call_args.kwargs["params"]["type"] == "Device", "unfiltered listing keeps its default"

        registry.get_all_devices(q='dataGate=="x"', entity_type="MarketPriceFeed")
        assert get.call_args.kwargs["params"]["type"] == "MarketPriceFeed", "narrowing stays possible"
