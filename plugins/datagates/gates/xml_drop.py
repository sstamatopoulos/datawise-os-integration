"""
XML drop gate — meter exports, billing files and machine reports.

Every regulated data exchange in Europe is XML: MSCONS-style meter
readings, ENTSO-E market documents saved to disk, SML/DLMS exports from
a head-end system, the daily report an old building controller writes to
a share. One element per reading, timestamps and values as child elements
or attributes.

    - key: meter_exports
      type: xml_drop
      options:
        directory: /opt/airflow/drop/meters
        pattern: "*.xml"
        row_path: ".//MeterReading"        # elements, one per reading
        device_selector: "@meterId"        # which device the reading belongs to
        timestamp_selector: "ReadingTime"  # child element text
        timestamp_format: iso
        timezone: Europe/Riga
        columns:
          "Value/@kWh":     {property: energy, unit: KWH, cumulative: true}
          "Quality":        {property: readingQuality, unit: C62}
        devices:
          - {id: "J0025571", name: Gymnasium electricity meter}

Selectors are the small path language of core.xmlrows: `Child` (text of a
child element), `@attr` (attribute of the row element), `Child/@attr`,
`.` (text of the row element itself).

Namespaces are stripped before matching, deliberately: the same vendor's
export changes its namespace URI between software versions and a
configuration written against the old URI then matches nothing at all,
silently. Set `keep_namespaces: true` and write `{uri}Tag` selectors if a
document really needs them.
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from datagates.core.xmlrows import parse_xml, pick, select
from datagates.gates.file_drop import FileDropGate


class XmlDropGate(FileDropGate):
    type_name = "xml_drop"
    speaks = "XML meter exports, billing files and controller reports"

    def __init__(self, config):
        super().__init__(config)
        self.pattern = str(self.option("pattern", "*.xml"))
        self.row_path = str(self.option("row_path", ""))
        self.keep_namespaces = bool(self.option("keep_namespaces", False))
        self.encoding = str(self.option("encoding", "utf-8"))
        # The base class reads the timestamp and the device out of a flat
        # row dict; XML addresses them with selectors, so they get their
        # own keys in that dict.
        self.timestamp_selector = str(self.option("timestamp_selector", self.ts_col))
        self.device_selector = self.option("device_selector")
        self.ts_col = "__timestamp__"
        self.device_col = "__device__" if self.device_selector else None

    def rows(self, path: str) -> Iterator[dict[str, Any]]:
        with open(path, encoding=self.encoding, errors="replace") as fh:
            root = parse_xml(fh.read(), keep_namespaces=self.keep_namespaces)
        selectors = {f.source: f.source for f in self.columns}
        for element in select(root, self.row_path):
            row: dict[str, Any] = {name: pick(element, sel) for name, sel in selectors.items()}
            row[self.ts_col] = pick(element, self.timestamp_selector)
            if self.device_selector:
                row["__device__"] = pick(element, str(self.device_selector))
            yield row
