"""The gates that read another store: SQL, InfluxDB, Prometheus, NGSI-LD, NGSI-v2, ENTSO-E.

The SQL gate runs against a real SQLite database, so the bind parameters
and the row mapping are exercised for real rather than mocked. The rest
parse recorded answers.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
import requests

from datagates.gates.base import GateConfig
from datagates.gates.entsoe import EntsoeGate, stamp
from datagates.gates.influx_source import InfluxSourceGate
from datagates.gates.ngsi_ld import NgsiLdGate
from datagates.gates.ngsi_v2 import NgsiV2Gate
from datagates.gates.prometheus import PrometheusGate
from datagates.gates.sql import SqlGate

START, END = datetime(2026, 9, 16, tzinfo=UTC), datetime(2026, 9, 17, tzinfo=UTC)


# ── sql, against a real database ────────────────────────────────────

def _sqlite(tmp_path):
    sqlalchemy = pytest.importorskip("sqlalchemy")
    path = tmp_path / "historian.db"
    engine = sqlalchemy.create_engine(f"sqlite+pysqlite:///{path}")
    with engine.begin() as connection:
        connection.execute(sqlalchemy.text(
            "CREATE TABLE history (tag TEXT, ts TEXT, epoch INTEGER, value TEXT)"))
        for tag, when, value in [
            ("AHU1.SupplyTemp", "2026-09-16 10:00:00", "21,4"),          # a decimal comma in a TEXT column
            ("AHU1.SupplyTemp", "2026-09-16 11:00:00", "21.8"),
            ("AHU1.SupplyTemp", "2026-09-15 10:00:00", "9.9"),           # before the window
            ("AHU1.Power", "2026-09-16 10:00:00", "3.2"),
        ]:
            epoch = int(datetime.fromisoformat(when + "+00:00").timestamp())
            connection.execute(sqlalchemy.text(
                "INSERT INTO history VALUES (:t, :w, :e, :v)"), {"t": tag, "w": when, "e": epoch, "v": value})
    return f"sqlite+pysqlite:///{path}"


def _sql(dsn, **options):
    base = {"dsn": dsn, "layout": "long",
            "query": ("SELECT tag, ts, value FROM history "
                      "WHERE tag = :device_id AND epoch > :start_s AND epoch <= :end_s ORDER BY ts"),
            "timestamp_column": "ts", "property_column": "tag", "value_column": "value",
            "columns": {"AHU1.SupplyTemp": {"property": "temperature", "unit": "CEL"}},
            "devices": [{"id": "AHU1.SupplyTemp", "name": "AHU 1 supply"}]}
    return SqlGate(GateConfig(key="historian", type="sql", options={**base, **options}))


def test_sql_long_layout_binds_the_device_and_the_window(tmp_path, doc_of):
    gate = _sql(_sqlite(tmp_path))
    samples = gate.fetch(doc_of(gate), START, END)
    assert [(s.controlled_property, s.value, s.observed_at) for s in samples] == [
        ("temperature", 21.4, "2026-09-16T10:00:00Z"),
        ("temperature", 21.8, "2026-09-16T11:00:00Z")]


def test_sql_wide_layout_maps_one_row_to_several_properties(tmp_path, doc_of):
    gate = _sql(_sqlite(tmp_path), layout="wide",
                query=("SELECT ts, MAX(CASE WHEN tag='AHU1.SupplyTemp' THEN value END) AS t, "
                       "MAX(CASE WHEN tag='AHU1.Power' THEN value END) AS p FROM history "
                       "WHERE epoch > :start_s AND epoch <= :end_s AND :device_id = :device_id "
                       "GROUP BY ts ORDER BY ts"),
                columns={"t": {"property": "temperature", "unit": "CEL"},
                         "p": {"property": "power", "unit": "KWT"}})
    samples = gate.fetch(doc_of(gate), START, END)
    assert sorted((s.controlled_property, s.value) for s in samples) == [
        ("power", 3.2), ("temperature", 21.4), ("temperature", 21.8)]


def test_sql_naive_columns_are_read_in_the_configured_zone(tmp_path, doc_of):
    gate = _sql(_sqlite(tmp_path), timezone="Europe/Riga")
    samples = gate.fetch(doc_of(gate), START, END)
    assert samples[0].observed_at == "2026-09-16T07:00:00Z"


def test_sql_rejects_an_unknown_layout(tmp_path):
    with pytest.raises(ValueError, match="layout must be"):
        _sql("sqlite://", layout="narrow")


# ── influxdb source ─────────────────────────────────────────────────

def _influx(**options):
    base = {"url": "http://10.0.60.4:8086", "version": 1, "database": "building",
            "devices": [{"id": "ahu1", "name": "AHU 1", "measurement": "hvac", "tags": {"device": "ahu1"},
                         "fields": {"temp_supply": {"property": "temperature", "unit": "CEL"},
                                    "kwh_total": {"property": "energy", "unit": "KWH",
                                                  "cumulative": True}}}]}
    return InfluxSourceGate(GateConfig(key="legacy_influx", type="influx_source",
                                       options={**base, **options}))


def test_influxql_quotes_identifiers_and_keeps_the_half_open_window():
    gate = _influx()
    entry = gate.entries[0]
    query = gate._influxql(entry, gate.maps["ahu1"], START, END)
    assert query == ('SELECT "temp_supply", "kwh_total" FROM "hvac" '
                     'WHERE time > 1789516800000ms AND time <= 1789603200000ms '
                     'AND "device" = \'ahu1\'')


def test_influx_v1_rows_become_samples(doc_of):
    gate = _influx()
    rows = [{"time": 1789570800000, "temp_supply": 21.4, "kwh_total": 1234.5},
            {"time": 1789574400000, "temp_supply": None, "kwh_total": 1235.0}]
    with patch.object(gate, "_query_v1", return_value=rows):
        samples = gate.fetch(doc_of(gate), START, END)
    assert sorted((s.controlled_property, s.value, s.observed_at) for s in samples) == [
        ("energy", 1234.5, "2026-09-16T15:00:00Z"), ("energy", 1235.0, "2026-09-16T16:00:00Z"),
        ("temperature", 21.4, "2026-09-16T15:00:00Z")]


def test_influx_v2_builds_flux_and_reads_annotated_csv(doc_of):
    gate = _influx(version=2, bucket="telemetry", org="acme", token="tok", database="")
    flux = gate._flux(gate.entries[0], gate.maps["ahu1"], START, END)
    assert 'from(bucket: "telemetry")' in flux and 'r["device"] == "ahu1"' in flux
    assert 'r._field == "temp_supply" or r._field == "kwh_total"' in flux
    csv_body = ("#datatype,string,long,dateTime:RFC3339,double,string\n"
                ",result,table,_time,_value,_field\n"
                ",_result,0,2026-09-16T15:00:00Z,21.4,temp_supply\n")
    response = MagicMock(text=csv_body)
    with patch.object(gate.client, "request", return_value=response):
        samples = gate.fetch(doc_of(gate), START, END)
    assert [(s.controlled_property, s.value) for s in samples] == [("temperature", 21.4)]


def test_influx_source_requires_the_right_store_option():
    with pytest.raises(ValueError, match="needs options.database"):
        _influx(database="")
    with pytest.raises(ValueError, match="needs options.bucket"):
        _influx(version=2, database="x")


# ── prometheus ──────────────────────────────────────────────────────

def _prom(**options):
    base = {"url": "http://prometheus.example.org:9090", "step": "60s",
            "devices": [{"id": "A", "name": "Rack A", "fields": {
                'sum(pdu_power_watts{{rack="{device_id}"}})': {"property": "power", "unit": "WTT"}}}]}
    return PrometheusGate(GateConfig(key="dc_power", type="prometheus", options={**base, **options}))


def test_prometheus_substitutes_the_device_into_promql_and_filters_the_window(doc_of):
    gate = _prom()
    result = [{"metric": {}, "values": [[1789570800, "1450.5"],
                                        [1789516800 - 60, "1.0"]]}]         # before the window
    with patch.object(gate, "_query_range", return_value=result) as query:
        samples = gate.fetch(doc_of(gate), START, END)
    assert query.call_args.args[0] == 'sum(pdu_power_watts{rack="A"})'
    assert [(s.value, s.observed_at) for s in samples] == [(1450.5, "2026-09-16T15:00:00Z")]


def test_prometheus_warns_when_an_expression_returns_several_series(doc_of, caplog):
    gate = _prom()
    result = [{"metric": {"rack": "A"}, "values": [[1789570800, "1"]]},
              {"metric": {"rack": "B"}, "values": [[1789570800, "2"]]}]
    with patch.object(gate, "_query_range", return_value=result):
        samples = gate.fetch(doc_of(gate), START, END)
    assert len(samples) == 2 and "returned 2 series" in caplog.text


def test_prometheus_reports_a_query_error(doc_of):
    gate = _prom()
    with patch.object(gate.client, "get_json", return_value={"status": "error", "error": "bad_data"}), \
         pytest.raises(RuntimeError, match="bad_data"):
        gate.fetch(doc_of(gate), START, END)


# ── ngsi-ld mirror ──────────────────────────────────────────────────

TEMPORAL = {
    "id": "urn:ngsi-ld:AirQualityObserved:Station-7", "type": "AirQualityObserved",
    "co2": [{"type": "Property", "value": 445, "observedAt": "2026-09-16T15:00:00Z"},
            {"type": "Property", "value": 460, "observedAt": "2026-09-16T16:00:00Z"},
            {"type": "Property", "value": 1, "observedAt": "2026-09-15T16:00:00Z"}],
    "temperature": {"type": "Property", "value": 21.4, "observedAt": "2026-09-16T15:00:00Z"},
}


def _ngsi_ld(**options):
    base = {"url": "https://broker.city.example.org",
            "devices": [{"id": "urn:ngsi-ld:AirQualityObserved:Station-7", "name": "Station 7",
                         "fields": {"co2": {"property": "co2", "unit": "59"},
                                    "temperature": {"property": "temperature", "unit": "CEL"}}}]}
    return NgsiLdGate(GateConfig(key="city_broker", type="ngsi_ld", options={**base, **options}))


def test_ngsi_ld_reads_the_temporal_representation_including_a_compacted_single_instance(doc_of):
    gate = _ngsi_ld()
    doc = doc_of(gate)
    assert doc["remoteEntityId"] == "urn:ngsi-ld:AirQualityObserved:Station-7"
    with patch.object(gate, "_temporal", return_value=TEMPORAL) as temporal:
        samples = gate.fetch(doc, START, END)
    assert temporal.call_args.args[1] == ["co2", "temperature"]
    assert sorted((s.controlled_property, s.value) for s in samples) == [
        ("co2", 445.0), ("co2", 460.0), ("temperature", 21.4)]


def test_ngsi_ld_sends_the_context_as_a_link_header():
    gate = _ngsi_ld(tenant="smartcity")
    headers = gate.client.session.headers
    assert "ngsi-ld-core-context.jsonld" in headers["Link"] and headers["NGSILD-Tenant"] == "smartcity"


def test_ngsi_ld_discovers_by_type_when_no_devices_are_listed():
    gate = NgsiLdGate(GateConfig(key="city", type="ngsi_ld", options={
        "url": "https://broker.example.org", "entity_type": "AirQualityObserved",
        "fields": {"co2": {"property": "co2", "unit": "59"}}}))
    entities = [{"id": "urn:ngsi-ld:AirQualityObserved:1", "name": {"type": "Property", "value": "Station 1"}},
                {"id": "urn:ngsi-ld:AirQualityObserved:2"}]
    with patch.object(gate, "_remote_entities", return_value=entities):
        specs = gate.discover()
    assert [s.name for s in specs] == ["Station 1", "urn:ngsi-ld:AirQualityObserved:2"]
    assert specs[0].properties == {"co2": "59"}


def test_ngsi_ld_needs_devices_or_a_type():
    with pytest.raises(ValueError, match="options.devices or options.entity_type"):
        NgsiLdGate(GateConfig(key="c", type="ngsi_ld", options={"url": "https://b"}))


# ── ngsi-v2 (legacy fiware) ─────────────────────────────────────────

def _ngsi_v2(**options):
    base = {"url": "http://orion-v2.example.org:1026", "history": "quantumleap",
            "service": "smartcity", "service_path": "/buildings",
            "devices": [{"id": "Sensor:001", "name": "Room 120B", "entity_type": "AirQualityObserved",
                         "fields": {"co2": {"property": "co2", "unit": "59"},
                                    "temperature": {"property": "temperature", "unit": "CEL"}}}]}
    return NgsiV2Gate(GateConfig(key="legacy_fiware", type="ngsi_v2", options={**base, **options}))


def test_ngsi_v2_quantumleap_index_and_attribute_arrays(doc_of):
    gate = _ngsi_v2()
    assert gate.client.session.headers["Fiware-ServicePath"] == "/buildings"
    body = {"index": ["2026-09-16T15:00:00.000", "2026-09-16T16:00:00.000"],
            "attributes": [{"attrName": "co2", "values": [445, 460]},
                           {"attrName": "temperature", "values": [21.4, None]}]}
    with patch.object(gate.client, "get_json", return_value=body):
        samples = gate.fetch(doc_of(gate), START, END)
    assert sorted((s.controlled_property, s.value, s.observed_at) for s in samples) == [
        ("co2", 445.0, "2026-09-16T15:00:00Z"), ("co2", 460.0, "2026-09-16T16:00:00Z"),
        ("temperature", 21.4, "2026-09-16T15:00:00Z")]


def test_ngsi_v2_sth_shape_uses_recv_time(doc_of):
    gate = _ngsi_v2(history="sth")

    def body(name, value):
        return {"contextResponses": [{"contextElement": {"attributes": [
            {"name": name, "values": [{"recvTime": "2026-09-16T15:00:00.000Z", "attrValue": value}]}]}}]}

    # STH is queried one attribute at a time, so each call answers for one.
    with patch.object(gate.client, "get_json",
                      side_effect=[body("co2", 445), body("temperature", 21.4)]) as get:
        samples = gate.fetch(doc_of(gate), START, END)
    assert "/STH/v1/contextEntities/type/AirQualityObserved/id/Sensor:001" in get.call_args.args[0]
    assert sorted((s.controlled_property, s.value, s.observed_at) for s in samples) == [
        ("co2", 445.0, "2026-09-16T15:00:00Z"), ("temperature", 21.4, "2026-09-16T15:00:00Z")]


def test_ngsi_v2_without_a_history_component_polls_current_values(doc_of):
    gate = _ngsi_v2(history="none")
    assert gate.rolling is True and gate.backfill_mode == "none"
    with patch.object(gate.client, "get_json", return_value={"co2": 445, "temperature": "21.4"}):
        samples = gate.fetch(doc_of(gate), START, START)
    assert sorted((s.controlled_property, s.value) for s in samples) == [("co2", 445.0), ("temperature", 21.4)]


# ── entso-e ─────────────────────────────────────────────────────────

MARKET_DOCUMENT = """<?xml version="1.0" encoding="UTF-8"?>
<Publication_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:0">
  <mRID>abc</mRID>
  <TimeSeries>
    <mRID>1</mRID>
    <Period>
      <timeInterval><start>2026-09-16T22:00Z</start><end>2026-09-17T22:00Z</end></timeInterval>
      <resolution>PT60M</resolution>
      <Point><position>1</position><price.amount>87.45</price.amount></Point>
      <Point><position>2</position><price.amount>85.10</price.amount></Point>
      <Point><position>4</position><price.amount>90.00</price.amount></Point>
    </Period>
  </TimeSeries>
</Publication_MarketDocument>"""


def _entsoe(**options):
    base = {"token": "tok", "document_type": "A44",
            "areas": [{"id": "LV", "name": "Latvia", "eic": "10YLV-1001A00074"}]}
    return EntsoeGate(GateConfig(key="market", type="entsoe", options={**base, **options}))


def test_entsoe_expands_sparse_positions_into_a_full_series():
    gate = _entsoe()
    samples = gate.samples(MARKET_DOCUMENT, "urn:x")
    assert [(s.observed_at, s.value) for s in samples] == [
        ("2026-09-16T22:00:00Z", 87.45),
        ("2026-09-16T23:00:00Z", 85.10),
        ("2026-09-17T00:00:00Z", 85.10),      # position 3 is absent: unchanged
        ("2026-09-17T01:00:00Z", 90.00)]
    assert all(s.controlled_property == "dayAheadPrice" for s in samples)


def test_entsoe_timestamp_format_and_document_types():
    assert stamp(datetime(2026, 9, 16, 22, tzinfo=UTC)) == "202609162200"
    prices, load = _entsoe(), _entsoe(document_type="A65")
    assert prices.rolling is True and load.rolling is False
    assert load.discover()[0].properties == {"electricityLoad": "MAW"}
    assert prices.discover()[0].entity_type == "MarketPriceFeed"
    with pytest.raises(ValueError, match="document_type must be"):
        _entsoe(document_type="A99")


def test_entsoe_asks_for_today_and_tomorrow_on_a_rolling_run(doc_of):
    gate = _entsoe()
    with patch.object(gate, "_query", return_value="") as query:
        gate.fetch(doc_of(gate), datetime(2026, 9, 16, 13, tzinfo=UTC), datetime(2026, 9, 16, 13, tzinfo=UTC))
    start, end = query.call_args.args[1:]
    assert (stamp(start), stamp(end)) == ("202609160000", "202609180000")


def test_entsoe_no_data_yet_is_not_a_failure(doc_of, caplog):
    caplog.set_level(logging.INFO)
    gate = _entsoe()
    response = MagicMock(spec=requests.Response, status_code=400,
                         text="<Reason><text>No matching data found</text></Reason>")
    with patch.object(gate.client, "request", side_effect=requests.HTTPError(response=response)):
        assert gate.fetch(doc_of(gate), START, END) == []
    assert "no data published yet" in caplog.text


def test_entsoe_a_real_error_still_fails(doc_of):
    gate = _entsoe()
    response = MagicMock(spec=requests.Response, status_code=401, text="Unauthorized")
    with patch.object(gate.client, "request", side_effect=requests.HTTPError(response=response)), \
         pytest.raises(requests.HTTPError):
        gate.fetch(doc_of(gate), START, END)
