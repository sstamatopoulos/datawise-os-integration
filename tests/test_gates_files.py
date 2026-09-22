"""The file gates: Excel workbooks, XML exports, and files pulled off SFTP/FTP.

Real files are written into tmp_path and read back through the gate, so
these tests exercise the whole path the way a partner's nightly upload
does.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from datagates.gates.base import GateConfig
from datagates.gates.excel_drop import ExcelDropGate
from datagates.gates.remote_drop import RemoteDropGate
from datagates.gates.xml_drop import XmlDropGate

WINDOW = (datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 12, 31, tzinfo=UTC))


# ── excel ───────────────────────────────────────────────────────────

def _workbook(path, rows, header_row=1):
    openpyxl = pytest.importorskip("openpyxl")
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Consumption"
    for _ in range(header_row - 1):
        sheet.append(["Monthly report"])
    for row in rows:
        sheet.append(row)
    workbook.save(path)


def _excel(tmp_path, **options):
    base = {"directory": str(tmp_path), "sheet": "Consumption", "header_row": 2,
            "timestamp_column": "Month", "timestamp_format": "%Y-%m", "timezone": "Europe/Riga",
            "device_column": "Meter", "forward_fill": ["Meter"],
            "columns": {"kWh": {"property": "energyConsumption", "unit": "KWH"},
                        "m3": {"property": "waterConsumption", "unit": "MTQ"}},
            "devices": [{"id": "J0025571", "name": "Gymnasium"}]}
    return ExcelDropGate(GateConfig(key="invoices", type="excel_drop", options={**base, **options}))


def test_excel_reads_a_sheet_by_name_with_a_title_block_above_the_header(tmp_path, doc_of):
    _workbook(tmp_path / "2026.xlsx",
              [["Month", "Meter", "kWh", "m3"],
               ["2026-02", "J0025571", "1 234,5", "12,5"],
               ["2026-03", None, "1300", "13"],            # merged device cell: forward filled
               ["2026-03", "OTHER", "9999", "99"],
               ["Total", "J0025571", "2534", "25"]],       # a totals line, skipped
              header_row=2)
    gate = _excel(tmp_path)
    assert gate.columns.properties == {"energyConsumption": "KWH", "waterConsumption": "MTQ"}
    samples = gate.fetch(doc_of(gate), *WINDOW)
    assert sorted((s.controlled_property, s.value, s.observed_at) for s in samples) == [
        ("energyConsumption", 1234.5, "2026-01-31T22:00:00Z"),
        ("energyConsumption", 1300.0, "2026-02-28T22:00:00Z"),
        ("waterConsumption", 12.5, "2026-01-31T22:00:00Z"),
        ("waterConsumption", 13.0, "2026-02-28T22:00:00Z"),
    ], "February in Riga starts at 22:00Z on 31 January"


def test_excel_uses_the_cells_own_datetime_when_the_column_is_date_formatted(tmp_path, doc_of):
    _workbook(tmp_path / "dates.xlsx",
              [["Month", "Meter", "kWh", "m3"],
               [datetime(2026, 5, 1, 10, 30), "J0025571", 500, 5]])
    gate = _excel(tmp_path, header_row=1, timezone="UTC")
    samples = gate.fetch(doc_of(gate), *WINDOW)
    assert {s.observed_at for s in samples} == {"2026-05-01T10:30:00Z"}


def test_excel_names_the_sheets_it_has_when_the_configured_one_is_absent(tmp_path, doc_of):
    _workbook(tmp_path / "x.xlsx", [["Month", "Meter", "kWh", "m3"]])
    gate = _excel(tmp_path, sheet="Readings")
    with pytest.raises(ValueError, match="no sheet named 'Readings'"):
        list(gate.rows(str(tmp_path / "x.xlsx")))


def test_a_file_that_cannot_be_read_is_logged_and_the_run_continues(tmp_path, doc_of, caplog):
    (tmp_path / "broken.xlsx").write_text("this is not a workbook", encoding="utf-8")
    _workbook(tmp_path / "good.xlsx", [["Month", "Meter", "kWh", "m3"],
                                       ["2026-06", "J0025571", 100, 1]])
    gate = _excel(tmp_path, header_row=1, timezone="UTC")
    samples = gate.fetch(doc_of(gate), *WINDOW)
    assert [s.value for s in samples if s.controlled_property == "energyConsumption"] == [100.0]


# ── xml ─────────────────────────────────────────────────────────────

EXPORT = """<?xml version="1.0" encoding="UTF-8"?>
<MeterReadings xmlns="urn:example:mscons:2011">
  <MeterReading meterId="J0025571">
    <ReadingTime>2026-09-16T12:00:00+03:00</ReadingTime>
    <Value kWh="12345.5"/>
    <Quality>1</Quality>
  </MeterReading>
  <MeterReading meterId="J0025571">
    <ReadingTime>2026-09-16T13:00:00+03:00</ReadingTime>
    <Value kWh="12347.0"/>
  </MeterReading>
  <MeterReading meterId="OTHER">
    <ReadingTime>2026-09-16T13:00:00+03:00</ReadingTime>
    <Value kWh="999.0"/>
  </MeterReading>
</MeterReadings>"""


def _xml(tmp_path, **options):
    base = {"directory": str(tmp_path), "row_path": ".//MeterReading",
            "device_selector": "@meterId", "timestamp_selector": "ReadingTime",
            "columns": {"Value/@kWh": {"property": "energy", "unit": "KWH", "cumulative": True},
                        "Quality": {"property": "readingQuality", "unit": "C62"}},
            "devices": [{"id": "J0025571", "name": "Gymnasium meter"}]}
    return XmlDropGate(GateConfig(key="meter_exports", type="xml_drop", options={**base, **options}))


def test_xml_drop_selects_rows_for_its_device_across_namespaces(tmp_path, doc_of):
    (tmp_path / "export.xml").write_text(EXPORT, encoding="utf-8")
    gate = _xml(tmp_path)
    assert gate.cumulative == frozenset({"energy"})
    samples = gate.fetch(doc_of(gate), *WINDOW)
    assert sorted((s.controlled_property, s.value, s.observed_at) for s in samples) == [
        ("energy", 12345.5, "2026-09-16T09:00:00Z"),
        ("energy", 12347.0, "2026-09-16T10:00:00Z"),
        ("readingQuality", 1.0, "2026-09-16T09:00:00Z"),
    ]


def test_xml_drop_is_stateless_for_backfill_because_the_files_do_not_change(tmp_path):
    assert _xml(tmp_path).backfill_mode == "stateless"


# ── remote drop ─────────────────────────────────────────────────────

def _remote(tmp_path, **options):
    base = {"protocol": "sftp", "host": "files.partner.example", "username": "u", "password": "p",
            "remote_dir": "/exports/heat", "directory": str(tmp_path), "format": "csv",
            "pattern": "*.csv", "delimiter": ";", "timestamp_column": "Timestamp",
            "timestamp_format": "%Y-%m-%d %H:%M", "timezone": "UTC", "device_column": "MeterId",
            "columns": {"Energy": {"property": "energy", "unit": "KWH", "cumulative": True}},
            "devices": [{"id": "5432", "name": "Heat meter 5432"}]}
    return RemoteDropGate(GateConfig(key="dh", type="remote_drop", options={**base, **options}))


def test_remote_drop_downloads_then_parses_with_the_configured_reader(tmp_path, doc_of, monkeypatch):
    gate = _remote(tmp_path)
    assert gate.cumulative == frozenset({"energy"})

    def fake_sync():
        (tmp_path / "heat-2026-09-16.csv").write_text(
            "Timestamp;MeterId;Energy\n2026-09-16 10:00;5432;1234,5\n2026-09-16 11:00;9999;7,0\n",
            encoding="utf-8")
        return 1

    monkeypatch.setattr(gate, "_sftp_sync", fake_sync)
    samples = gate.fetch(doc_of(gate), *WINDOW)
    assert [(s.controlled_property, s.value, s.observed_at) for s in samples] == [
        ("energy", 1234.5, "2026-09-16T10:00:00Z")]


def test_remote_drop_skips_a_file_already_mirrored_at_the_same_size(tmp_path):
    gate = _remote(tmp_path)
    (tmp_path / "a.csv").write_text("12345", encoding="utf-8")
    assert gate._wanted("a.csv", 99) is True, "a corrected re-upload has a different size"
    assert gate._wanted("a.csv", 5) is False
    assert gate._wanted("notes.txt", 10) is False, "the pattern decides what is downloaded"


def test_remote_drop_validates_protocol_and_format(tmp_path):
    with pytest.raises(ValueError, match="protocol must be"):
        _remote(tmp_path, protocol="scp")
    with pytest.raises(ValueError, match="format must be"):
        _remote(tmp_path, format="parquet")


def test_remote_drop_can_carry_an_xml_reader(tmp_path, doc_of, monkeypatch):
    gate = _remote(tmp_path, format="xml", pattern="*.xml", row_path=".//MeterReading",
                   device_selector="@meterId", timestamp_selector="ReadingTime", timestamp_format="iso",
                   columns={"Value/@kWh": {"property": "energy", "unit": "KWH"}},
                   devices=[{"id": "J0025571", "name": "Gymnasium meter"}])
    monkeypatch.setattr(gate, "_sftp_sync",
                        lambda: (tmp_path / "e.xml").write_text(EXPORT, encoding="utf-8") and 1)
    samples = gate.fetch(doc_of(gate), *WINDOW)
    assert {s.value for s in samples} == {12345.5, 12347.0}
