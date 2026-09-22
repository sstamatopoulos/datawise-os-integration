"""
SQL gate — the historian, the billing database, the legacy application's
own tables.

Ask an operator where the data is and the honest answer is usually "in
the database". A Wonderware or PI historian with a SQL interface, an
Oracle table a nightly job fills, the MySQL behind a twenty-year-old
metering application, a Microsoft SQL Server nobody dares upgrade: one
query and they are all sources. This gate runs a query you write, per
device, per window.

    - key: historian
      type: sql
      options:
        dsn: "mssql+pyodbc://${DB_USER}:${DB_PASSWORD}@sqlsrv.example.org/Historian?driver=ODBC+Driver+18+for+SQL+Server"
        layout: long                      # long (one row per reading) | wide
        query: |
          SELECT TagName, DateTime AS ts, Value
          FROM   History
          WHERE  TagName = :device_id AND DateTime > :start AND DateTime <= :end
          ORDER  BY DateTime
        timestamp_column: ts
        property_column: TagName          # long layout only
        value_column: Value               # long layout only
        timezone: Europe/Riga             # the zone naive columns are in
        columns:
          "AHU1.SupplyTemp": {property: temperature, unit: CEL}
          "AHU1.Power":      {property: power, unit: KWT}
        devices:
          - {id: "AHU1.SupplyTemp", name: AHU 1 supply temperature}

With `layout: wide` the query returns one row per timestamp and one
column per property, and `columns:` maps column names instead:

        query: "SELECT ts, t_supply, t_return, flow FROM heat WHERE meter = :device_id AND ts > :start AND ts <= :end"
        columns:
          t_supply: {property: temperature, unit: CEL}
          t_return: {property: returnTemperature, unit: CEL}

Rules this gate keeps to:

- **Bind parameters, never string formatting.** `:device_id`, `:start`,
  `:end`, `:start_ms`, `:end_ms`, `:start_s`, `:end_s` and `:limit` are
  bound by the driver. A gate that formatted a device id into SQL would
  be an injection hole pointed at a production database by a YAML file.
- **Read-only, and say so.** Give the gate an account with SELECT on the
  tables it needs and nothing else; the framework never writes upstream,
  but the database administrator should not have to take that on trust.
- **Windows are bounded and ordered.** Every query must filter on the
  window and order by time, or a backfill over a large table will read
  it whole, repeatedly, and be noticed by the database administrator
  before it finishes.
- **Naive timestamps are the norm.** Most historian columns are local
  time with no offset; `timezone:` is what turns them into instants, and
  getting it wrong shifts a year of data by an hour twice.

Needs the `SQLAlchemy` extra plus the driver of the database in question
(pyodbc, psycopg, oracledb, mysqlclient ... — see requirements-gates.txt).
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from datagates.core.fieldmap import FieldMap
from datagates.core.timeparse import iso_z, parse_stamp, zone_of
from datagates.gates.base import DeviceSpec, Gate, Sample

log = logging.getLogger(__name__)

RESERVED = {"id", "name", "ref_building", "category"}


class SqlGate(Gate):
    type_name = "sql"
    speaks = "any SQL database or historian (SQL Server, Oracle, MySQL, Postgres, ODBC)"
    default_schedule = "0 * * * *"
    default_max_window_days = 31
    default_history_start = "2020-01-01"
    device_attrs = ("sqlDeviceId",)

    def __init__(self, config):
        super().__init__(config)
        self.dsn = str(self.required("dsn"))
        self.query = str(self.required("query"))
        self.layout = str(self.option("layout", "wide")).lower()
        if self.layout not in ("wide", "long"):
            raise ValueError(f"{self.key}: layout must be 'wide' or 'long'")
        self.ts_col = str(self.option("timestamp_column", "ts"))
        self.ts_fmt = str(self.option("timestamp_format", "iso"))
        self.tz = zone_of(self.option("timezone"))
        self.property_column = str(self.option("property_column", "tag"))
        self.value_column = str(self.option("value_column", "value"))
        self.limit = int(self.option("limit", 100000))
        self.columns = FieldMap(self.option("columns"), gate_key=self.key, block="columns")
        self.cumulative = self.columns.cumulative           # type: ignore[misc]
        self.entries = self.devices_option()
        self._extra_keys = sorted({k for d in self.entries for k in d} - RESERVED)
        self.device_attrs = ("sqlDeviceId", *self._extra_keys)   # type: ignore[misc]
        self._engine = None

    def discover(self) -> list[DeviceSpec]:
        return [DeviceSpec(
            urn=self.urn(entry["id"]), name=str(entry.get("name") or entry["id"]),
            properties=self.columns.properties, ref_building=entry.get("ref_building"),
            category=list(entry.get("category", ["meter"])),
            attrs={"sqlDeviceId": str(entry["id"]), **{k: entry[k] for k in self._extra_keys if k in entry}},
        ) for entry in self.entries]

    # -- upstream ----------------------------------------------------------
    @property
    def engine(self):
        """One pooled engine per gate instance; a task that opens a
        connection per window exhausts a legacy server's connection limit
        long before it exhausts its patience."""
        if self._engine is None:
            from sqlalchemy import create_engine  # imported here: optional dependency

            self._engine = create_engine(self.dsn, pool_pre_ping=True,
                                         connect_args=dict(self.option("connect_args") or {}))
        return self._engine

    def _rows(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        from sqlalchemy import text

        with self.engine.connect() as connection:
            result = connection.execute(text(self.query), params)
            return [dict(row) for row in result.mappings()]

    # -- the contract ------------------------------------------------------
    def fetch(self, device: dict[str, Any], start: datetime, end: datetime) -> list[Sample]:
        params = {"device_id": device.get("sqlDeviceId"), "start": start, "end": end,
                  "start_s": int(start.timestamp()), "end_s": int(end.timestamp()),
                  "start_ms": int(start.timestamp() * 1000), "end_ms": int(end.timestamp() * 1000),
                  "limit": self.limit,
                  **{k: device.get(k) for k in self._extra_keys}}
        rows = self._rows(params)
        return self.samples(rows, device["urn"])

    def samples(self, rows: list[dict[str, Any]], urn: str) -> list[Sample]:
        out: list[Sample] = []
        skipped = 0
        for row in rows:
            stamp = parse_stamp(row.get(self.ts_col), self.ts_fmt, self.tz)
            if stamp is None:
                skipped += 1
                continue
            observed_at = iso_z(stamp)
            if self.layout == "wide":
                out.extend(self.columns.samples(urn, row, observed_at))
                continue
            field = self.columns.by_source(str(row.get(self.property_column)))
            if field is None:
                continue
            value = field.convert(row.get(self.value_column))
            if value is not None:
                out.append(Sample(urn, field.property, value, observed_at))
        if skipped:
            log.info("%s: %d row(s) without a usable %s", self.key, skipped, self.ts_col)
        return out
