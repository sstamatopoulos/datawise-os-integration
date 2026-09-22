"""
Excel drop gate — the workbook a partner exports every month.

Energy managers, ESCOs and municipal utilities do not have an API; they
have a spreadsheet, and the spreadsheet is authoritative. This gate reads
it directly rather than asking somebody to convert it to CSV first,
because the conversion step is where the decimal commas, the dates and
the sheet nobody mentioned get lost.

    - key: invoices
      type: excel_drop
      options:
        directory: /opt/airflow/drop/invoices
        pattern: "*.xlsx"
        sheet: "Consumption"             # name, or 0 for the first sheet
        header_row: 3                    # 1-based row holding the column names
        timestamp_column: Month
        timestamp_format: "%Y-%m"        # or iso, or leave to Excel's own dates
        timezone: Europe/Riga
        device_column: "Meter"
        columns:
          "kWh":  {property: energyConsumption, unit: KWH}
          "m3":   {property: waterConsumption, unit: MTQ}
        devices:
          - {id: "J0025571", name: Gymnasium water meter}

What the format does to data, and what this gate does about it:

- **Dates are not text.** openpyxl returns real `datetime` objects for
  date-formatted cells, so `timestamp_format` is ignored for those and
  the cell's own value is used. A column formatted as text ("01.02.2026")
  still needs a `timestamp_format`.
- **Merged cells read as None in every cell but the first.** A device
  column that is merged down a block of rows therefore empties after the
  first row; `forward_fill: [Meter]` repeats the last non-empty value
  down the column, which is what the human reading the sheet sees.
- **Formulas are values, not formulas.** The workbook is opened with
  `data_only=True`, which returns the cached result Excel last computed.
  A file written by a tool that never opened Excel has no cached results
  and every formula cell reads None — the gate logs it rather than
  writing zeros.
- `.xls` (the pre-2007 binary format) is not supported by openpyxl. Ask
  for `.xlsx`, or convert with LibreOffice in the drop directory.

Needs the `openpyxl` extra (pip install -r requirements-gates.txt).
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

from datagates.gates.file_drop import FileDropGate

log = logging.getLogger(__name__)


class ExcelDropGate(FileDropGate):
    type_name = "excel_drop"
    speaks = "Excel workbooks exported by utilities, ESCOs and energy managers"

    def __init__(self, config):
        super().__init__(config)
        self.pattern = str(self.option("pattern", "*.xlsx"))
        self.sheet: Any = self.option("sheet", 0)
        self.header_row = int(self.option("header_row", 1))
        self.forward_fill = [str(c) for c in (self.option("forward_fill") or [])]

    def _sheet(self, workbook):
        if isinstance(self.sheet, int):
            return workbook.worksheets[self.sheet]
        if self.sheet not in workbook.sheetnames:
            raise ValueError(f"{self.key}: no sheet named {self.sheet!r}; the file has {workbook.sheetnames}")
        return workbook[self.sheet]

    def rows(self, path: str) -> Iterator[dict[str, Any]]:
        from openpyxl import load_workbook  # imported here: optional dependency

        workbook = load_workbook(path, read_only=True, data_only=True)
        try:
            sheet = self._sheet(workbook)
            header: list[str] = []
            carried: dict[str, Any] = {}
            for index, cells in enumerate(sheet.iter_rows(values_only=True), start=1):
                if index < self.header_row:
                    continue
                if index == self.header_row:
                    header = [str(c).strip() if c is not None else f"col{n}" for n, c in enumerate(cells)]
                    continue
                row = dict(zip(header, cells, strict=False))
                for column in self.forward_fill:
                    if row.get(column) in (None, ""):
                        row[column] = carried.get(column)
                    else:
                        carried[column] = row[column]
                if any(v is not None for v in row.values()):
                    yield row
        finally:
            workbook.close()
