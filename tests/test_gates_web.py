"""The web gates: CSV exports, SOAP/XML services, oBIX, Zabbix, MQTT.

Every payload here is the shape a real upstream answers with, recorded
once and then relied on. The HTTP layer itself is tested in test_core.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from datagates.gates.base import GateConfig
from datagates.gates.http_csv import HttpCsvGate
from datagates.gates.http_xml import HttpXmlGate
from datagates.gates.mqtt import MqttGate, spool_prefix, topic_matches
from datagates.gates.obix import ObixGate
from datagates.gates.zabbix import ZabbixGate

START, END = datetime(2026, 9, 16, tzinfo=UTC), datetime(2026, 9, 17, tzinfo=UTC)


# ── http_csv ────────────────────────────────────────────────────────

def _http_csv(**options):
    base = {"url": "https://portal.example.org/export",
            "params": {"meter": "{device_id}", "from": "{start_date}", "to": "{end_date}"},
            "delimiter": ";", "skip_rows": 2, "timestamp_column": "Laiks",
            "timestamp_format": "%d.%m.%Y %H:%M", "timezone": "Europe/Riga",
            "columns": {"kWh": {"property": "energyConsumption", "unit": "KWH"}},
            "devices": [{"id": "5432", "name": "Heat meter 5432"}]}
    return HttpCsvGate(GateConfig(key="heat_portal", type="http_csv", options={**base, **options}))


def test_http_csv_skips_a_title_block_and_fills_the_window_placeholders(doc_of):
    gate = _http_csv()
    doc = doc_of(gate)
    body = ("Meter export 5432\nGenerated 16.09.2026\n"
            "Laiks;kWh\n16.09.2026 10:00;1 234,5\n16.09.2026 11:00;1 240,0\n")
    with patch.object(gate, "_download", return_value=body) as download:
        samples = gate.fetch(doc, START, END)
    assert download.call_args.args[1] == {"meter": "5432", "from": "2026-09-16", "to": "2026-09-17"}
    assert [(s.value, s.observed_at) for s in samples] == [
        (1234.5, "2026-09-16T07:00:00Z"), (1240.0, "2026-09-16T08:00:00Z")]


def test_http_csv_refuses_a_login_page_pretending_to_be_an_export(doc_of):
    gate = _http_csv()
    response = MagicMock()
    response.text = "<!DOCTYPE html><html><body>Please sign in</body></html>"
    with patch.object(gate.client, "request", return_value=response), \
         pytest.raises(RuntimeError, match="answered markup, not CSV"):
        gate.fetch(doc_of(gate), START, END)


def test_http_csv_can_split_one_file_between_devices(doc_of):
    gate = _http_csv(device_column="Meter",
                     devices=[{"id": "5432", "name": "A"}, {"id": "5433", "name": "B"}])
    body = "x\nx\nLaiks;Meter;kWh\n16.09.2026 10:00;5432;10\n16.09.2026 10:00;5433;20\n"
    with patch.object(gate, "_download", return_value=body):
        first = gate.fetch(doc_of(gate, 0), START, END)
        second = gate.fetch(doc_of(gate, 1), START, END)
    assert [s.value for s in first] == [10.0] and [s.value for s in second] == [20.0]


# ── http_xml / SOAP ─────────────────────────────────────────────────

SOAP_ANSWER = """<?xml version="1.0"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">
  <soapenv:Body>
    <GetMeterReadingsResponse xmlns="urn:example:billing">
      <MeterReading time="2026-09-16T10:00:00Z"><Value>12345.5</Value><Flow>0.8</Flow></MeterReading>
      <MeterReading time="2026-09-16T11:00:00Z"><Value>12347.0</Value><Flow>0.9</Flow></MeterReading>
      <MeterReading time="2026-09-15T11:00:00Z"><Value>1.0</Value><Flow>0.1</Flow></MeterReading>
    </GetMeterReadingsResponse>
  </soapenv:Body>
</soapenv:Envelope>"""


def _http_xml(**options):
    base = {"url": "https://ws.example.org/readings", "method": "POST",
            "soap_action": "urn:GetMeterReadings",
            "body": "<Envelope><meterId>{device_id}</meterId><from>{start_iso}</from></Envelope>",
            "row_path": ".//MeterReading", "timestamp_selector": "@time",
            "fields": {"Value": {"property": "energyConsumption", "unit": "KWH", "cumulative": True},
                       "Flow": {"property": "flow", "unit": "MQH"}},
            "devices": [{"id": "5432", "name": "Heat meter 5432"}]}
    return HttpXmlGate(GateConfig(key="billing_ws", type="http_xml", options={**base, **options}))


def test_http_xml_sends_the_envelope_and_reads_the_rows_in_the_window(doc_of):
    gate = _http_xml()
    assert gate.cumulative == frozenset({"energyConsumption"})
    assert gate.client.session.headers["SOAPAction"] == '"urn:GetMeterReadings"'
    with patch.object(gate, "_request", return_value=SOAP_ANSWER) as request:
        samples = gate.fetch(doc_of(gate), START, END)
    assert request.call_args.args[2] == \
        "<Envelope><meterId>5432</meterId><from>2026-09-16T00:00:00Z</from></Envelope>"
    assert sorted((s.controlled_property, s.value) for s in samples) == [
        ("energyConsumption", 12345.5), ("energyConsumption", 12347.0), ("flow", 0.8), ("flow", 0.9)]


def test_http_xml_reads_plain_xml_over_get_too():
    gate = _http_xml(method="GET", body=None, soap_action="")
    samples = gate.samples(SOAP_ANSWER, "urn:x", START, END)
    assert len(samples) == 4 and "SOAPAction" not in gate.client.session.headers


# ── oBIX / Niagara ──────────────────────────────────────────────────

OBIX_HISTORY = """<?xml version="1.0"?>
<obj xmlns="http://obix.org/ns/schema/1.1" is="obix:HistoryQueryOut">
  <int name="count" val="3"/>
  <list name="data" of="obix:HistoryRecord">
    <obj is="obix:HistoryRecord">
      <abstime name="timestamp" val="2026-09-16T13:00:00+03:00"/><real name="value" val="21.4"/>
    </obj>
    <obj is="obix:HistoryRecord">
      <abstime name="timestamp" val="2026-09-16T13:15:00+03:00"/><real name="value" val="21.6"/>
    </obj>
    <obj is="obix:HistoryRecord">
      <abstime name="timestamp" val="2026-09-16T13:30:00+03:00"/><real name="value" val="null"/>
    </obj>
  </list>
</obj>"""


def _obix(**options):
    base = {"base_url": "https://jace.example.org/obix", "history": True,
            "auth": {"type": "basic", "username": "obix", "password": "s3cret"},
            "devices": [{"id": "ahu1", "name": "AHU 1", "fields": {
                "S1/AHU1_SupplyTemp": {"property": "temperature", "unit": "CEL"}}}]}
    return ObixGate(GateConfig(key="jace", type="obix", options={**base, **options}))


def test_obix_history_records_keep_the_stations_own_offset(doc_of):
    gate = _obix()
    assert gate.rolling is False and gate.backfill_mode == "cursor"
    records = gate.history_records(OBIX_HISTORY)
    assert len(records) == 3 and records[0][1] == "21.4"
    with patch.object(gate, "_history_query", return_value=OBIX_HISTORY) as query:
        samples = gate.fetch(doc_of(gate), START, END)
    assert query.call_args.args[0] == "S1/AHU1_SupplyTemp"
    assert [(s.value, s.observed_at) for s in samples] == [
        (21.4, "2026-09-16T10:00:00Z"), (21.6, "2026-09-16T10:15:00Z")], "'null' is not a reading"


def test_obix_present_value_mode_polls_instead(doc_of):
    gate = _obix(history=False)
    assert gate.rolling is True and gate.backfill_mode == "none"
    assert gate.present_value('<real val="21.4" name="out"/>') == "21.4"
    with patch.object(gate, "_read_point", return_value='<real val="21.4"/>'):
        samples = gate.fetch(doc_of(gate), START, START)
    assert [s.value for s in samples] == [21.4]


def test_obix_a_point_that_fails_does_not_fail_the_station(doc_of, caplog):
    gate = _obix(devices=[{"id": "ahu1", "fields": {
        "S1/Gone": {"property": "temperature", "unit": "CEL"},
        "S1/Power": {"property": "power", "unit": "KWT"}}}])
    def answer(point, *_a):
        if point == "S1/Gone":
            raise RuntimeError("404 Not Found")
        return OBIX_HISTORY
    with patch.object(gate, "_history_query", side_effect=answer):
        samples = gate.fetch(doc_of(gate), START, END)
    assert {s.controlled_property for s in samples} == {"power"} and "unreadable" in caplog.text


# ── zabbix ──────────────────────────────────────────────────────────

def _zabbix(**options):
    base = {"url": "https://zabbix.example.org/api_jsonrpc.php", "token": "tok",
            "devices": [{"id": "crac1", "name": "CRAC 1", "fields": {
                "23451": {"property": "temperature", "unit": "CEL"},
                "23452": {"property": "relativeHumidity", "unit": "P1", "scale": 0.01}}}]}
    return ZabbixGate(GateConfig(key="facility", type="zabbix", options={**base, **options}))


def test_zabbix_maps_item_ids_and_clock_seconds(doc_of):
    gate = _zabbix()
    doc = doc_of(gate)
    assert doc["zabbixDeviceId"] == "crac1"
    rows = [{"itemid": "23451", "clock": "1789570800", "value": "21.4"},
            {"itemid": "23452", "clock": "1789570800", "value": "43"},
            {"itemid": "99999", "clock": "1789570800", "value": "7"}]     # an item of another host
    with patch.object(gate, "_history", return_value=rows):
        samples = {s.controlled_property: (s.value, s.observed_at) for s in gate.fetch(doc, START, END)}
    assert samples == {"temperature": (21.4, "2026-09-16T15:00:00Z"),
                       "relativeHumidity": (pytest.approx(0.43), "2026-09-16T15:00:00Z")}


def test_zabbix_asks_again_as_unsigned_when_the_float_history_is_empty(doc_of):
    gate = _zabbix()
    with patch.object(gate, "_call", side_effect=[[], [{"itemid": "23451", "clock": "1789570800",
                                                       "value": "22"}]]) as call:
        samples = gate.fetch(doc_of(gate), START, END)
    assert [c.args[1]["history"] for c in call.call_args_list] == [0, 3]
    assert [s.value for s in samples] == [22.0]


def test_zabbix_sends_a_token_as_a_header_and_a_password_as_a_session(doc_of):
    # Zabbix 7.0 removed the request body's `auth` property, so a token there
    # fails with "Invalid params" on new servers; a session id is still a body
    # property on every version.
    with_token = _zabbix()
    assert with_token.client.session.headers["Authorization"] == "Bearer tok"
    with patch.object(with_token.client, "post_json", return_value={"result": []}) as post:
        with_token.fetch(doc_of(with_token), START, END)
    assert "auth" not in post.call_args.kwargs["json"]

    with_password = _zabbix(token="", user="u", password="p")
    assert "Authorization" not in with_password.client.session.headers
    with patch.object(with_password.client, "post_json",
                      side_effect=[{"result": "session-id"}, {"result": []}, {"result": []}]) as post:
        with_password.fetch(doc_of(with_password), START, END)
    calls = [c.kwargs["json"] for c in post.call_args_list]
    assert calls[0]["method"] == "user.login" and "auth" not in calls[0]
    assert calls[1]["auth"] == "session-id"


def test_zabbix_raises_the_api_error_text(doc_of):
    gate = _zabbix()
    body = {"jsonrpc": "2.0", "error": {"message": "Not authorised.", "data": "Session terminated."}}
    with patch.object(gate.client, "post_json", return_value=body), \
         pytest.raises(RuntimeError, match="Not authorised"):
        gate.fetch(doc_of(gate), START, END)


# ── mqtt ────────────────────────────────────────────────────────────

def test_topic_filters():
    assert topic_matches("a/b/c", "a/b/c")
    assert topic_matches("a/+/c", "a/x/c") and not topic_matches("a/+/c", "a/x/y/c")
    assert topic_matches("a/#", "a/x/y") and not topic_matches("a/#", "b/x")
    assert not topic_matches("a/b", "a/b/c")


def test_spool_prefix_is_filename_safe_and_shared_with_the_subscriber():
    assert spool_prefix("room120") == "room120"
    assert spool_prefix("app/1/dev:2") == "app_1_dev_2"
    assert spool_prefix("") == "device"


def _mqtt(**options):
    base = {"host": "mqtt.example.org", "client_id": "datagates-test",
            "devices": [{"id": "room120", "name": "Room 120B", "topic": "app/1/device/room120/up",
                         "time_field": "time",
                         "fields": {"object.co2": {"property": "co2", "unit": "59"},
                                    "object.temperature": {"property": "temperature", "unit": "CEL"}}}]}
    return MqttGate(GateConfig(key="lora", type="mqtt", options={**base, **options}))


def test_mqtt_reads_nested_payloads_and_prefers_the_payloads_own_time(doc_of):
    gate = _mqtt()
    assert gate.rolling is True and gate.backfill_mode == "none"
    messages = [
        {"topic": "app/1/device/room120/up", "received": "2026-09-16T15:10:00Z",
         "payload": json.dumps({"time": "2026-09-16T15:00:00Z",
                                "object": {"co2": 445, "temperature": 21.4}})},
        {"topic": "app/1/device/other/up", "received": "2026-09-16T15:10:00Z",
         "payload": json.dumps({"object": {"co2": 999}})},
    ]
    with patch.object(gate, "collect", return_value=messages) as collect:
        samples = gate.fetch(doc_of(gate), START, START)
    # One subscription and one session per device: the run DAG fans out over
    # devices, and a shared client id would have them disconnect each other.
    assert collect.call_args.args == ("app/1/device/room120/up", "datagates-test-room120")
    assert sorted((s.controlled_property, s.value, s.observed_at) for s in samples) == [
        ("co2", 445.0, "2026-09-16T15:00:00Z"), ("temperature", 21.4, "2026-09-16T15:00:00Z")]


def test_mqtt_falls_back_to_arrival_time_and_tolerates_junk(doc_of, caplog):
    gate = _mqtt(devices=[{"id": "room120", "topic": "app/#",
                           "fields": {"co2": {"property": "co2", "unit": "59"}}}])
    messages = [{"topic": "app/x", "received": "2026-09-16T15:10:00Z", "payload": "not json"},
                {"topic": "app/x", "received": "2026-09-16T15:10:00Z", "payload": '{"co2": 500}'}]
    with patch.object(gate, "collect", return_value=messages):
        samples = gate.fetch(doc_of(gate), START, START)
    assert [(s.value, s.observed_at) for s in samples] == [(500.0, "2026-09-16T15:10:00Z")]
    assert "not JSON" in caplog.text


def test_mqtt_bare_value_payloads(doc_of):
    gate = _mqtt(devices=[{"id": "meter", "topic": "meters/1/kwh", "payload": "value",
                           "fields": {"value": {"property": "energy", "unit": "KWH",
                                                "cumulative": True}}}])
    assert gate.cumulative == frozenset({"energy"})
    with patch.object(gate, "collect", return_value=[
            {"topic": "meters/1/kwh", "received": "2026-09-16T15:00:00Z", "payload": "1234.5"}]):
        samples = gate.fetch(doc_of(gate), START, START)
    assert [(s.controlled_property, s.value) for s in samples] == [("energy", 1234.5)]


def test_mqtt_spool_mode_drains_only_its_own_device_files_and_only_finished_ones(tmp_path, doc_of):
    gate = _mqtt(mode="spool", spool_dir=str(tmp_path),
                 devices=[{"id": "room120", "topic": "app/#",
                           "fields": {"co2": {"property": "co2", "unit": "59"}}},
                          {"id": "room121", "topic": "app/#",
                           "fields": {"co2": {"property": "co2", "unit": "59"}}}])

    def line(value):
        return json.dumps({"topic": "app/x", "received": "2026-09-16T15:00:00Z",
                           "payload": json.dumps({"co2": value})}) + "\n"

    mine = tmp_path / "room120-20260916T150000.jsonl"
    mine.write_text(line(480) + "rubbish\n", encoding="utf-8")
    other_device = tmp_path / "room121-20260916T150000.jsonl"
    other_device.write_text(line(999), encoding="utf-8")
    still_open = tmp_path / "room120-20260916T150100.jsonl.part"
    still_open.write_text(line(555), encoding="utf-8")

    samples = gate.fetch(doc_of(gate, 0), START, START)
    assert [s.value for s in samples] == [480.0], "a junk line is skipped, the .part file is not read"
    assert not mine.exists(), "a drained file is removed, so a message is stored exactly once"
    assert other_device.exists(), "the other device's task drains its own spool"
    assert still_open.exists()


def test_mqtt_routes_a_message_to_every_matching_device():
    gate = _mqtt(devices=[{"id": "a", "topic": "app/+/up", "fields": {"v": {"property": "x"}}},
                          {"id": "b", "topic": "app/1/up", "fields": {"v": {"property": "x"}}},
                          {"id": "c", "topic": "other/#", "fields": {"v": {"property": "x"}}}])
    assert gate.devices_for("app/1/up") == ["a", "b"]
    assert gate.devices_for("other/1") == ["c"]
    assert gate.devices_for("nothing/here") == []
    assert gate.topics() == ["app/+/up", "app/1/up", "other/#"]


def test_mqtt_collect_mode_needs_a_host():
    with pytest.raises(ValueError, match="host is required"):
        MqttGate(GateConfig(key="lora", type="mqtt", options={
            "devices": [{"id": "a", "topic": "t", "fields": {"v": {"property": "x"}}}]}))
