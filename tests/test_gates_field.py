"""The field-protocol gates: Modbus, BACnet, OPC UA, S7, SNMP.

None of these can be reached from a test, and none of them need to be:
what is worth testing is the register/object/OID mapping, the decoding
and the polling contract. The transport is mocked at the one method that
touches it.
"""
from __future__ import annotations

import struct
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from datagates.gates.bacnet import BacnetGate, numeric
from datagates.gates.base import GateConfig
from datagates.gates.modbus import ModbusGate
from datagates.gates.opcua import OpcUaGate
from datagates.gates.polling import PollingGate
from datagates.gates.s7 import S7Gate
from datagates.gates.snmp import SnmpGate

NOW = datetime(2026, 9, 16, 15, 0, 43, tzinfo=UTC)


def _result(registers=None, bits=None, error=False):
    """What pymodbus hands back."""
    answer = MagicMock()
    answer.registers, answer.bits = registers or [], bits or []
    answer.isError.return_value = error
    return answer


# ── the polling contract, shared by all five ────────────────────────

def _modbus(**options):
    base = {"devices": [{"id": "ahu1", "name": "AHU 1", "host": "10.0.20.11", "unit": 3, "base": 40001,
                         "fields": {"40001": {"property": "temperature", "unit": "CEL",
                                              "type": "int16", "scale": 0.1}}}]}
    return ModbusGate(GateConfig(key="plant", type="modbus", options={**base, **options}))


def test_polling_gates_have_no_history_and_no_backfill():
    gate = _modbus()
    assert gate.rolling is True and gate.backfill_mode == "none"
    assert isinstance(gate, PollingGate)


def test_polling_stamp_is_aligned_so_a_retry_overwrites_its_own_point():
    gate = _modbus()
    with patch("datagates.gates.polling.datetime") as clock:
        clock.now.return_value = NOW
        clock.fromtimestamp.side_effect = datetime.fromtimestamp
        assert gate.stamp() == "2026-09-16T15:00:00Z", "the 43rd second is floored to the minute"
    gate.align_s = 0
    with patch("datagates.gates.polling.datetime") as clock:
        clock.now.return_value = NOW
        assert gate.stamp() == "2026-09-16T15:00:43Z"


def test_a_device_still_in_orion_but_dropped_from_the_config_is_skipped(doc_of):
    gate = _modbus()
    doc = doc_of(gate)
    gate.specs.clear()
    assert gate.fetch(doc, NOW, NOW) == []


def test_a_device_without_fields_is_rejected_at_construction():
    with pytest.raises(ValueError, match="no options.fields"):
        ModbusGate(GateConfig(key="plant", type="modbus",
                              options={"devices": [{"id": "x", "host": "h"}]}))


# ── modbus ──────────────────────────────────────────────────────────

def test_modbus_subtracts_the_manual_register_base_and_scales(doc_of):
    gate = _modbus()
    doc = doc_of(gate)
    assert doc["modbusHost"] == "10.0.20.11:502" and doc["modbusUnitId"] == 3
    with patch.object(gate, "_client") as client, patch.object(gate, "_call") as call:
        client.return_value.connect.return_value = True
        call.return_value = _result(registers=[214])
        samples = gate.fetch(doc, NOW, NOW)
    assert call.call_args.args[1:] == ("holding", 0, 1, 3), "40001 with base 40001 is register 0"
    assert [(s.controlled_property, s.value) for s in samples] == [("temperature", pytest.approx(21.4))]


def test_modbus_reads_32_bit_counters_bits_and_coils(doc_of):
    gate = _modbus(devices=[{"id": "m1", "host": "h", "fields": {
        "10": {"property": "energy", "unit": "KWH", "type": "uint32", "word_order": "little",
               "cumulative": True},
        "20.3": {"property": "alarmState", "unit": "C62"},
        "1": {"property": "pumpState", "unit": "C62", "table": "coil"},
    }}])
    assert gate.cumulative == frozenset({"energy"})
    doc = doc_of(gate)
    answers = {("holding", 10): _result(registers=[0x4240, 0x000F]),
               ("holding", 20): _result(registers=[0b1000]),
               ("coil", 1): _result(bits=[True])}
    with patch.object(gate, "_client") as client, \
         patch.object(gate, "_call", side_effect=lambda _c, table, addr, *a: answers[(table, addr)]):
        client.return_value.connect.return_value = True
        samples = {s.controlled_property: s.value for s in gate.fetch(doc, NOW, NOW)}
    assert samples == {"energy": 1_000_000.0, "alarmState": 1.0, "pumpState": 1.0}


def test_modbus_one_unreadable_register_does_not_lose_the_others(doc_of, caplog):
    gate = _modbus(devices=[{"id": "m1", "host": "h", "fields": {
        "1": {"property": "temperature", "unit": "CEL"},
        "2": {"property": "relativeHumidity", "unit": "P1"},
    }}])
    doc = doc_of(gate)
    with patch.object(gate, "_client") as client, \
         patch.object(gate, "_call", side_effect=[_result(error=True), _result(registers=[55])]):
        client.return_value.connect.return_value = True
        samples = gate.fetch(doc, NOW, NOW)
    assert [(s.controlled_property, s.value) for s in samples] == [("relativeHumidity", 55.0)]


def test_modbus_refuses_an_unknown_register_table(doc_of):
    gate = _modbus(devices=[{"id": "m1", "host": "h",
                             "fields": {"1": {"property": "t", "table": "holdings"}}}])
    doc = doc_of(gate)
    with patch.object(gate, "_client") as client, pytest.raises(ValueError, match="table 'holdings'"):
        client.return_value.connect.return_value = True
        gate.fetch(doc, NOW, NOW)


def test_modbus_needs_a_host():
    with pytest.raises(ValueError, match="needs a host"):
        ModbusGate(GateConfig(key="p", type="modbus", options={
            "devices": [{"id": "x", "fields": {"1": {"property": "t"}}}]}))


# ── bacnet ──────────────────────────────────────────────────────────

def _bacnet():
    return BacnetGate(GateConfig(key="bms", type="bacnet", options={
        "bind": "10.0.20.5/24",
        "devices": [{"id": "ahu1", "name": "AHU 1", "address": "10.0.20.41", "fields": {
            "analogInput:1": {"property": "temperature", "unit": "CEL"},
            "binaryInput:3": {"property": "occupancy", "unit": "C62"},
            "analogInput:7": {"property": "co2", "unit": "59", "invalid": [-9999]},
        }}]}))


def test_bacnet_enumerations_become_numbers():
    assert numeric("active") == 1.0 and numeric("inactive") == 0.0
    assert numeric("Occupied") == 1.0 and numeric(21.4) == 21.4
    assert numeric("faulty") == "faulty", "unknown text stays text and is dropped by the field map"


def test_bacnet_builds_the_request_string_and_drops_the_sentinel(doc_of):
    gate = _bacnet()
    doc = doc_of(gate)
    assert doc["bacnetAddress"] == "10.0.20.41"
    answers = {"analogInput:1": 21.4, "binaryInput:3": "active", "analogInput:7": -9999}
    with patch.object(gate, "_read_point", side_effect=lambda _a, point, _p: answers[point]) as read:
        samples = {s.controlled_property: s.value for s in gate.fetch(doc, NOW, NOW)}
    assert read.call_args.args == ("10.0.20.41", "analogInput:7", "presentValue")
    assert samples == {"temperature": 21.4, "occupancy": 1.0}, "the out-of-service sentinel is not a reading"


def test_bacnet_one_dead_point_is_logged_not_fatal(doc_of, caplog):
    gate = _bacnet()
    doc = doc_of(gate)
    def answer(_address, point, _property):
        if point == "analogInput:1":
            raise TimeoutError("no response")
        return 1
    with patch.object(gate, "_read_point", side_effect=answer):
        samples = {s.controlled_property for s in gate.fetch(doc, NOW, NOW)}
    assert samples == {"occupancy", "co2"} and "unreadable" in caplog.text


# ── opc ua ──────────────────────────────────────────────────────────

def _opcua(**options):
    base = {"endpoint": "opc.tcp://10.0.30.9:4840",
            "devices": [{"id": "chiller1", "name": "Chiller 1", "fields": {
                "ns=2;i=1002": {"property": "temperature", "unit": "CEL"},
                "ns=2;s=Chiller1.Power": {"property": "power", "unit": "KWT"}}}]}
    return OpcUaGate(GateConfig(key="scada", type="opcua", options={**base, **options}))


def test_opcua_snapshot_mode_polls(doc_of):
    gate = _opcua()
    assert gate.rolling and gate.backfill_mode == "none"
    doc = doc_of(gate)
    assert doc["opcuaEndpoint"] == "opc.tcp://10.0.30.9:4840"
    with patch.object(gate, "_read_values", return_value={"ns=2;i=1002": 6.5, "ns=2;s=Chiller1.Power": "31.2"}):
        samples = {s.controlled_property: s.value for s in gate.fetch(doc, NOW, NOW)}
    assert samples == {"temperature": 6.5, "power": 31.2}


def test_opcua_history_mode_becomes_a_windowed_gate_with_a_backfill(doc_of):
    gate = _opcua(history=True)
    assert gate.rolling is False and gate.backfill_mode == "cursor"
    doc = doc_of(gate)
    start, end = datetime(2026, 9, 16, 14, tzinfo=UTC), datetime(2026, 9, 16, 15, tzinfo=UTC)
    history = {
        "ns=2;i=1002": [(datetime(2026, 9, 16, 14, 30, tzinfo=UTC), 6.5),
                        (datetime(2026, 9, 16, 13, 30, tzinfo=UTC), 6.4)],   # before the window
        "ns=2;s=Chiller1.Power": [],
    }
    with patch.object(gate, "_read_history", side_effect=lambda _e, source, *a: history[source]):
        samples = gate.fetch(doc, start, end)
    assert [(s.controlled_property, s.value, s.observed_at) for s in samples] == [
        ("temperature", 6.5, "2026-09-16T14:30:00Z")]


def test_opcua_a_node_without_a_historian_is_logged_and_skipped(doc_of, caplog):
    gate = _opcua(history=True)
    doc = doc_of(gate)
    with patch.object(gate, "_read_history", side_effect=RuntimeError("BadHistoryOperationUnsupported")):
        assert gate.fetch(doc, NOW, NOW) == []
    assert "no history for" in caplog.text


# ── siemens s7 ──────────────────────────────────────────────────────

def _s7():
    return S7Gate(GateConfig(key="substation", type="s7", options={
        "devices": [{"id": "hs1", "name": "Heat substation 1", "host": "10.0.40.12", "slot": 2, "fields": {
            "supplyT": {"property": "temperature", "unit": "CEL", "db": 10, "start": 0, "type": "float32"},
            "heatTotal": {"property": "energy", "unit": "KWH", "db": 10, "start": 8, "type": "uint32",
                          "cumulative": True},
            "pumpOn": {"property": "pumpState", "unit": "C62", "db": 10, "start": 12, "type": "bool",
                       "bit": 2},
        }}]}))


def test_s7_decodes_reals_counters_and_bits_from_data_blocks(doc_of):
    gate = _s7()
    doc = doc_of(gate)
    assert (doc["s7Host"], doc["s7Slot"]) == ("10.0.40.12", 2)
    assert gate.cumulative == frozenset({"energy"})
    blocks = {0: struct.pack(">f", 63.5), 8: struct.pack(">I", 421_337), 12: bytes([0b0000_0100])}
    with patch.object(gate, "_connect"), \
         patch.object(gate, "_read_block", side_effect=lambda _c, _a, _db, start, _s: blocks[start]):
        samples = {s.controlled_property: s.value for s in gate.fetch(doc, NOW, NOW)}
    assert samples == {"temperature": pytest.approx(63.5), "energy": 421_337.0, "pumpState": 1.0}


def test_s7_refuses_an_unknown_area(doc_of):
    gate = S7Gate(GateConfig(key="s", type="s7", options={"devices": [
        {"id": "x", "host": "h", "fields": {"a": {"property": "t", "area": "flash"}}}]}))
    doc = doc_of(gate)
    with patch.object(gate, "_connect"), pytest.raises(ValueError, match="area 'flash'"):
        gate.fetch(doc, NOW, NOW)


# ── snmp ────────────────────────────────────────────────────────────

def _snmp(**options):
    base = {"version": "2c", "community": "public",
            "devices": [{"id": "ups-main", "name": "UPS", "host": "10.0.10.7", "fields": {
                "1.3.6.1.2.1.33.1.2.4.0": {"property": "batteryLevel", "unit": "P1", "scale": 0.01},
                "1.3.6.1.2.1.33.1.4.4.1.4.1": {"property": "power", "unit": "WTT"}}}]}
    return SnmpGate(GateConfig(key="ups", type="snmp", options={**base, **options}))


def test_snmp_applies_the_implied_scale_of_an_integer_mib_value(doc_of):
    gate = _snmp()
    doc = doc_of(gate)
    assert doc["snmpHost"] == "10.0.10.7:161"
    answered = {"1.3.6.1.2.1.33.1.2.4.0": 97, "1.3.6.1.2.1.33.1.4.4.1.4.1": 1450}
    with patch.object(gate, "_get", return_value=answered):
        samples = {s.controlled_property: s.value for s in gate.fetch(doc, NOW, NOW)}
    assert samples == {"batteryLevel": pytest.approx(0.97), "power": 1450.0}


def test_snmp_v3_needs_a_user():
    with pytest.raises(ValueError, match="SNMPv3 needs"):
        _snmp(version="3")


def test_snmp_reports_an_oid_the_agent_ignored(doc_of, caplog):
    gate = _snmp()
    doc = doc_of(gate)
    with patch.object(gate, "_get", return_value={"1.3.6.1.2.1.33.1.2.4.0": 97}):
        samples = gate.fetch(doc, NOW, NOW)
    assert len(samples) == 1 and "did not answer" in caplog.text
