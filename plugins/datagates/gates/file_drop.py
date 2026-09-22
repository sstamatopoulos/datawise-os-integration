"""
datagates.gates.file_drop — the shared base of the file gates.

The oldest integration pattern there is, and still the most common one
with utilities, ESCOs and building operators: somebody exports a file
every night and drops it somewhere. The format differs (CSV, XLSX, XML),
the transport differs (a mounted share, SFTP, FTPS), the semantics do
not, so they share this base:

    directory + pattern -> files -> rows -> (device, timestamp, values)

Behaviour worth knowing before configuring one:

- **Files are never moved, renamed or deleted.** A gate that consumes its
  input cannot be re-run, and a partner who re-uploads a corrected file
  expects the correction to land. Every run re-reads what matches the
  pattern and the Device watermark drops the rows already stored, which
  makes the gate idempotent and the backfill `stateless`. Archive old
  files yourself, or set `max_file_age_days` once the directory grows.
- **One row may carry many devices.** `device_column` says which row
  belongs to which Device; without it every row belongs to the single
  Device of the gate.
- **Unparsable rows are skipped, not fatal.** Partner exports contain
  header repetitions, totals lines and "n/a". The count is logged.
"""
from __future__ import annotations

import glob
import logging
import os
import time
from collections.abc import Iterator
from datetime import datetime
from typing import Any

from datagates.core.fieldmap import FieldMap
from datagates.core.timeparse import iso_z, parse_stamp, zone_of
from datagates.gates.base import DeviceSpec, Gate, Sample

log = logging.getLogger(__name__)


class FileDropGate(Gate):
    """Base for gates that read rows out of files in a directory."""

    default_schedule = "*/30 * * * *"
    default_max_window_days = 3650
    backfill_mode = "stateless"
    device_attrs = ("dropDeviceId",)
    fields_block = "columns"
    default_category: tuple[str, ...] = ("meter",)

    def __init__(self, config) -> None:
        super().__init__(config)
        self.directory = str(self.option("directory", f"/opt/airflow/drop/{self.key}"))
        self.pattern = str(self.option("pattern", "*.csv"))
        self.recursive = bool(self.option("recursive", False))
        self.max_file_age_days = float(self.option("max_file_age_days", 0) or 0)
        self.ts_col = str(self.option("timestamp_column", "timestamp"))
        self.ts_fmt = str(self.option("timestamp_format", "iso"))
        self.tz = zone_of(self.option("timezone"))
        self.device_col = self.option("device_column")
        self.columns = FieldMap(self.option(self.fields_block), gate_key=self.key, block=self.fields_block)
        self.cumulative = self.columns.cumulative           # type: ignore[misc]

    # -- what a subclass provides ------------------------------------------
    def rows(self, path: str) -> Iterator[dict[str, Any]]:
        """Every row of one file as a flat mapping of column name to raw
        value, in file order."""
        raise NotImplementedError

    def prepare(self) -> None:
        """Called once before a fetch; where a remote gate downloads."""

    # -- the contract ------------------------------------------------------
    def discover(self) -> list[DeviceSpec]:
        entries = self.devices_option() or [{"id": self.key, "name": self.key}]
        reserved = {"id", "name", "ref_building", "category"}
        return [DeviceSpec(
            urn=self.urn(entry["id"]), name=str(entry.get("name") or entry["id"]),
            properties=self.columns.properties,
            ref_building=entry.get("ref_building"),
            category=list(entry.get("category", list(self.default_category))),
            attrs={"dropDeviceId": str(entry["id"]),
                   **{k: v for k, v in entry.items() if k not in reserved}},
        ) for entry in entries]

    def files(self) -> list[str]:
        pattern = os.path.join(self.directory, "**", self.pattern) if self.recursive \
            else os.path.join(self.directory, self.pattern)
        paths = sorted(glob.glob(pattern, recursive=self.recursive))
        if self.max_file_age_days:
            cutoff = time.time() - self.max_file_age_days * 86400
            paths = [p for p in paths if os.path.getmtime(p) >= cutoff]
        return [p for p in paths if os.path.isfile(p)]

    def fetch(self, device: dict[str, Any], start: datetime, end: datetime) -> list[Sample]:
        self.prepare()
        wanted = str(device.get("dropDeviceId") or "")
        out: list[Sample] = []
        skipped = 0
        for path in self.files():
            try:
                rows = list(self.rows(path))
            except Exception as exc:            # noqa: BLE001 - a partner's file breaks in every
                # imaginable way: not a zip, wrong encoding, malformed XML, a lock file left behind
                # by Excel. One bad file must not cost the run every other file in the directory.
                log.warning("%s: %s could not be read: %s", self.key, os.path.basename(path), exc)
                continue
            for row in rows:
                if self.device_col and str(row.get(self.device_col, "")).strip() != wanted:
                    continue
                dt = parse_stamp(row.get(self.ts_col), self.ts_fmt, self.tz)
                if dt is None:
                    skipped += 1
                    continue
                if not (start < dt <= end):
                    continue
                out.extend(self.columns.samples(device["urn"], row, iso_z(dt)))
        if skipped:
            log.info("%s: %d row(s) without a usable %s", self.key, skipped, self.ts_col)
        return out
