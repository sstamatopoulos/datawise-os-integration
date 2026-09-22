"""
CSV drop gate — files a partner uploads to a directory (SFTP, a mounted
share, rsync), one row per timestamp.

    - key: gas
      type: csv_drop
      options:
        directory: /opt/airflow/drop/gas          # default /opt/airflow/drop/<key>
        pattern: "*.csv"
        delimiter: ";"
        timestamp_column: Timestamp
        timestamp_format: "%Y-%m-%d %H:%M:%S"     # or iso (default) / epoch_s / epoch_ms
        timezone: Europe/Riga                     # naive stamps are in this zone; default UTC
        device_column: MeterId                    # which row belongs to which device
        columns:                                  # csv column -> property
          Consumption: {property: gasConsumption, unit: MTQ}
          Index:       {property: gasVolume,      unit: MTQ, cumulative: true}
        devices:
          - id: J00025571
            name: Gymnasium gas meter
            ref_building: urn:ngsi-ld:Building:...

Files are never moved or deleted; every run re-reads them and the
watermark drops rows already stored (see datagates.gates.file_drop).

Three details that come up with real exports: `encoding: cp1257` (or
latin-1) for files written by a Windows-era meter reading system,
`header: [ts, meter, value]` for files that have no header row at all,
and `skip_rows: 3` for the ones that start with a title block. Decimal
commas are handled by the field map.
"""
from __future__ import annotations

import csv
from collections.abc import Iterator
from typing import Any

from datagates.gates.file_drop import FileDropGate


class CsvDropGate(FileDropGate):
    type_name = "csv_drop"
    speaks = "CSV / TSV exports on a shared directory"

    def __init__(self, config):
        super().__init__(config)
        self.delimiter = str(self.option("delimiter", ","))
        self.quotechar = str(self.option("quotechar", '"'))
        self.encoding = str(self.option("encoding", "utf-8-sig"))
        self.skip_rows = int(self.option("skip_rows", 0))
        self.header: list[str] | None = self.option("header")

    def rows(self, path: str) -> Iterator[dict[str, Any]]:
        with open(path, encoding=self.encoding, errors="replace", newline="") as fh:
            for _ in range(self.skip_rows):
                fh.readline()
            reader = csv.DictReader(fh, fieldnames=self.header, delimiter=self.delimiter,
                                    quotechar=self.quotechar)
            for row in reader:
                # A short line gives None values, a long one collects the rest
                # under the None key; neither should reach the field map.
                yield {k: v for k, v in row.items() if isinstance(k, str)}
