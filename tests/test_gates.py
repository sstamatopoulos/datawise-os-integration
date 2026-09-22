"""Each built-in gate: what it registers and how it turns upstream payloads into samples."""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from datagates.core.entities import as_list, device_doc, device_entity
from datagates.gates.base import GateConfig
from datagates.gates.csv_drop import CsvDropGate
from datagates.gates.http_json import HttpJsonGate
from datagates.gates.nordpool import NordPoolGate
from datagates.gates.open_meteo import OpenMeteoGate, hourly_samples
from datagates.gates.thingsboard import ThingsBoardGate

T0 = int(datetime(2026, 9, 16, 15, tzinfo=UTC).timestamp())


# ── open-meteo ──────────────────────────────────────────────────────

def _weather(mode):
    return OpenMeteoGate(GateConfig(key=f"w_{mode}", type="open_meteo", options={
        "mode": mode, "locations": [{"id": "riga", "name": "Riga", "lat": 56.95, "lon": 24.1}]}))


def test_open_meteo_forecast_is_rolling_and_observed_is_stateless():
    fc, ob = _weather("forecast"), _weather("observed")
    assert fc.rolling and fc.backfill_mode == "none"
    assert not ob.rolling and ob.backfill_mode == "stateless" and ob.rewrite_lookback.days == 7
    assert "visibility" in fc.discover()[0].properties
    assert "visibility" not in ob.discover()[0].properties, "the archive does not serve it"
    d = fc.discover()[0]
    assert d.entity_type == "WeatherForecastLocation" and d.location == (24.1, 56.95)


def test_open_meteo_samples_scale_percent_to_fraction():
    payload = {"hourly": {"time": [T0, T0 + 3600], "temperature_2m": [19.7, None],
                          "relative_humidity_2m": [77, 80], "wind_speed_10m": [5.6, 5.1]}}
    s = hourly_samples(payload, "urn:x", _weather("forecast").variables)
    by = {(x.controlled_property, x.observed_at): x.value for x in s}
    assert by[("temperature", "2026-09-16T15:00:00Z")] == 19.7
    assert ("temperature", "2026-09-16T16:00:00Z") not in by
    assert by[("relativeHumidity", "2026-09-16T15:00:00Z")] == pytest.approx(0.77)


# ── nord pool ───────────────────────────────────────────────────────

def test_nordpool_devices_and_parsing():
    g = NordPoolGate(GateConfig(key="prices", type="nordpool", options={"areas": ["LV"]}))
    (d,) = g.discover()
    assert d.properties == {"dayAheadPrice": "EUR_MWH"} and d.ref_building is None
    doc = device_doc(device_entity(d, g), g)
    assert doc["deliveryArea"] == "LV" and doc["properties"] == ["dayAheadPrice"]
    body = {"multiAreaEntries": [
        {"deliveryStart": "2026-09-17T00:00:00Z", "deliveryEnd": "2026-09-17T00:15:00Z", "entryPerArea": {"LV": 87.45}},
        {"deliveryStart": "2026-09-17T00:15:00Z", "deliveryEnd": "2026-09-17T00:30:00Z", "entryPerArea": {}},
    ]}
    with patch.object(g, "_day", return_value=body["multiAreaEntries"]) as day:
        now = datetime(2026, 9, 16, 13, tzinfo=UTC)
        s = g.fetch(doc, now, now)                       # rolling: today + tomorrow
        assert [c.args[0] for c in day.call_args_list] == ["2026-09-16", "2026-09-17"]
    assert len(s) == 2 and s[0].value == 87.45 and s[0].observed_at == "2026-09-17T00:00:00Z"


# ── csv drop ────────────────────────────────────────────────────────

def test_csv_drop_reads_rows_for_its_device_only(tmp_path):
    (tmp_path / "a.csv").write_text(
        "Timestamp;MeterId;Consumption;Index\n"
        "2026-09-16 10:00:00;M1;0,5;100,0\n"
        "2026-09-16 11:00:00;M1;0,7;100,7\n"
        "2026-09-16 11:00:00;M2;9,9;999,9\n", encoding="utf-8")
    g = CsvDropGate(GateConfig(key="gas", type="csv_drop", options={
        "directory": str(tmp_path), "delimiter": ";", "timestamp_column": "Timestamp",
        "timestamp_format": "%Y-%m-%d %H:%M:%S", "timezone": "Europe/Riga", "device_column": "MeterId",
        "columns": {"Consumption": {"property": "gasConsumption", "unit": "MTQ"},
                    "Index": {"property": "gasVolume", "unit": "MTQ", "cumulative": True}},
        "devices": [{"id": "M1", "name": "meter one"}]}))
    assert g.cumulative == frozenset({"gasVolume"})
    (d,) = g.discover()
    doc = device_doc(device_entity(d, g), g)
    s = g.fetch(doc, datetime(2026, 9, 16, 7, 30, tzinfo=UTC), datetime(2026, 9, 17, tzinfo=UTC))
    # 10:00 Riga = 07:00Z is before the window; 11:00 Riga = 08:00Z is in it; M2 is ignored
    assert [(x.controlled_property, x.value, x.observed_at) for x in s] == [
        ("gasConsumption", 0.7, "2026-09-16T08:00:00Z"), ("gasVolume", 100.7, "2026-09-16T08:00:00Z")]


# ── http json ───────────────────────────────────────────────────────

def _http(layout, **extra):
    return HttpJsonGate(GateConfig(key="air", type="http_json", options={
        "url": "https://x/device/{device_id}/data", "params": {"from": "{start_date}", "to": "{end_date}"},
        "headers": {"Authorization": "Bearer t"}, "layout": layout, "time_field": "timestamp",
        "fields": {"nco2": {"property": "co2", "unit": "59"}, "temperatureReal": {"property": "temperature", "unit": "CEL"}},
        "devices": [{"id": 25667, "name": "Room 120B", "building_id": 134}], **extra}))


def test_http_json_columns_layout_and_placeholders():
    g = _http("columns")
    doc = device_doc(device_entity(g.discover()[0], g), g)
    assert doc["httpDeviceId"] == "25667" and doc["building_id"] == 134
    body = {"timestamp": ["2026-09-16T15:01:35Z", "2026-09-16T15:03:35Z"], "nco2": [445, None], "temperatureReal": [21.4, 21.5]}
    with patch.object(g, "_request", return_value=body) as req:
        s = g.fetch(doc, datetime(2026, 9, 16, 15, tzinfo=UTC), datetime(2026, 9, 16, 16, tzinfo=UTC))
    assert req.call_args.args[:2] == ("https://x/device/25667/data", {"from": "2026-09-16", "to": "2026-09-16"})
    assert sorted((x.controlled_property, x.value) for x in s) == [("co2", 445.0), ("temperature", 21.4), ("temperature", 21.5)]


def test_http_json_records_layout_with_nested_path():
    g = _http("records", records_path="data.items", time_format="epoch_ms")
    doc = device_doc(device_entity(g.discover()[0], g), g)
    body = {"data": {"items": [{"timestamp": T0 * 1000, "nco2": "500", "temperatureReal": "nan"}]}}
    with patch.object(g, "_request", return_value=body):
        s = g.fetch(doc, datetime(2026, 9, 16, 14, tzinfo=UTC), datetime(2026, 9, 16, 16, tzinfo=UTC))
    assert [(x.controlled_property, x.value, x.observed_at) for x in s] == [("co2", 500.0, "2026-09-16T15:00:00Z")]


# ── thingsboard ─────────────────────────────────────────────────────

def test_thingsboard_keys_and_cumulative():
    g = ThingsBoardGate(GateConfig(key="meters", type="thingsboard", options={
        "base_url": "https://tb/api", "username": "u", "password": "p",
        "devices": [{"id": "school", "name": "School", "tb_device_id": "dev-1",
                     "keys": {"energy": {"key": "Qel1", "unit": "KWH", "cumulative": True}, "power": {"key": "Pel1", "unit": "KWT"}}}]}))
    assert g.cumulative == frozenset({"energy"})
    (d,) = g.discover()
    assert d.properties == {"energy": "KWH", "power": "KWT"}
    doc = device_doc(device_entity(d, g), g)
    assert doc["tbKeys"] == {"energy": "Qel1", "power": "Pel1"}
    session = MagicMock()
    session.get.return_value.json.return_value = {"Qel1": [{"ts": T0 * 1000, "value": "12.5"}], "Pel1": [{"ts": T0 * 1000, "value": 3}]}
    session.get.return_value.raise_for_status.return_value = None
    g._session = session
    s = g.fetch(doc, datetime(2026, 9, 16, 14, tzinfo=UTC), datetime(2026, 9, 16, 16, tzinfo=UTC))
    assert {(x.controlled_property, x.value) for x in s} == {("energy", 12.5), ("power", 3.0)}


# ── entities ────────────────────────────────────────────────────────

def test_device_entity_shape_and_scalar_compaction_guard():
    g = _weather("forecast")
    e = device_entity(g.discover()[0], g)
    assert e["dataGate"]["value"] == "w_forecast" and e["influxBucket"]["value"] == "telemetry"
    assert e["location"]["type"] == "GeoProperty" and e["location"]["value"]["coordinates"] == [24.1, 56.95]
    assert as_list("waterConsumption") == ["waterConsumption"] and as_list(["a", "b"]) == ["a", "b"]
    compacted = dict(e, controlledProperty={"type": "Property", "value": "temperature"})
    assert device_doc(compacted, g)["properties"] == ["temperature"]
