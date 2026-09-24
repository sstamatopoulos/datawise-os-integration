#!/usr/bin/env python3
"""
verify_platform.py — does this deployment actually work?

Run it after `docker compose up`, after a configuration change, and from
a cron job on a live installation. It checks the platform the way a
consumer would: it asks the broker and InfluxDB the same questions, in
the same order, and says which of the conventions the data model promises
are true right now.

    python scripts/verify_platform.py                  # everything
    python scripts/verify_platform.py --section model
    python scripts/verify_platform.py --gate weather_forecast --json
    python scripts/verify_platform.py --tolerance 6     # slower upstreams

Sections:

  connectivity  the broker answers, InfluxDB is healthy, the buckets exist
  model         entities exist per gate, types and units are as documented,
                `location` holds coordinates and nothing else
  bridge        every summary's influxMeasurement is the id the convention
                requires, and that measurement has points
  freshness     every summary's lastReadingAt is within the gate's cadence
  queries       the `q=` filters consumers rely on still match, which is
                what the core-context-only decision protects

What it needs: `config/gates.yaml`, and the same environment variables the
platform uses (ORION_URL, ORION_API_KEY, INFLUX_URL, INFLUX_TOKEN,
INFLUX_ORG). It talks plain HTTP to both stores and so depends only on
requests and PyYAML — it has to run on a laptop that has not installed
the Airflow image's dependencies.

Exit status is 0 when nothing failed, 1 when something did, so it can be
a deployment gate. Warnings (a gate that has not run yet) do not fail.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins"))

from datagates.core.cadence import describe, expected_interval  # noqa: E402
from datagates.core.measurement_summary import summary_measurement_urn  # noqa: E402
from datagates.core.settings import (  # noqa: E402
    INFLUX_DEFAULT_BUCKET,
    INFLUX_ORG,
    INFLUX_TOKEN,
    INFLUX_URL,
    ORION_API_KEY,
    ORION_URL,
)
from datagates.gates.registry import load_gates  # noqa: E402

SECTIONS = ("connectivity", "model", "bridge", "freshness", "queries")
OK, WARN, FAIL = "ok", "warn", "fail"


@dataclass
class Report:
    checks: list[dict[str, Any]] = field(default_factory=list)

    def add(self, section: str, name: str, status: str, detail: str = "") -> None:
        self.checks.append({"section": section, "check": name, "status": status, "detail": detail})
        if not ARGS.json:
            mark = {OK: "  ok  ", WARN: " warn ", FAIL: " FAIL "}[status]
            print(f"[{mark}] {name}{': ' + detail if detail else ''}")

    def counts(self) -> dict[str, int]:
        return {s: sum(1 for c in self.checks if c["status"] == s) for s in (OK, WARN, FAIL)}


def section(title: str) -> None:
    # ASCII only in what this prints: it runs on whatever console an operator
    # has, and a code page that cannot encode a box-drawing character would
    # crash it before it reported anything.
    if not ARGS.json:
        print(f"\n-- {title} {'-' * max(0, 58 - len(title))}")


# ── the two stores, over plain HTTP ──────────────────────────────────

def orion(path: str, **params: Any) -> Any:
    headers = {"Accept": "application/ld+json"}
    if ORION_API_KEY:
        headers["X-API-Key"] = ORION_API_KEY
    response = requests.get(f"{ORION_URL}{path}", params=params or None, headers=headers, timeout=30)
    response.raise_for_status()
    return response.json() if response.content else None


def influx_flux(query: str) -> list[dict[str, str]]:
    response = requests.post(f"{INFLUX_URL}/api/v2/query", params={"org": INFLUX_ORG},
                             headers={"Authorization": f"Token {INFLUX_TOKEN}",
                                      "Content-Type": "application/vnd.flux",
                                      "Accept": "application/csv"},
                             data=query.encode("utf-8"), timeout=60)
    response.raise_for_status()
    lines = [x for x in response.text.splitlines() if x.strip() and not x.lstrip().startswith("#")]
    if not lines:
        return []
    header = lines[0].split(",")
    return [dict(zip(header, row.split(","), strict=False)) for row in lines[1:]]


def as_list(value: Any) -> list:
    """Orion-LD compacts one-element arrays to scalars; never index before
    this (see the decision log in ROADMAP.md)."""
    if value is None:
        return []
    return list(value) if isinstance(value, list | tuple) else [value]


def plain(entity: dict[str, Any], name: str, default: Any = None) -> Any:
    node = entity.get(name)
    if isinstance(node, dict):
        return node.get("value", node.get("object", default))
    return default if node is None else node


# ── sections ─────────────────────────────────────────────────────────

def check_connectivity(report: Report, gates: list) -> None:
    section("connectivity")
    try:
        version = orion("/version")
        report.add("connectivity", "Orion-LD answers", OK,
                   f"version {(version or {}).get('orionld version', 'unknown')}")
    except Exception as exc:                                        # noqa: BLE001
        report.add("connectivity", "Orion-LD answers", FAIL, f"{ORION_URL}: {exc}")
        return
    try:
        health = requests.get(f"{INFLUX_URL}/health", timeout=15).json()
        report.add("connectivity", "InfluxDB is healthy",
                   OK if health.get("status") == "pass" else FAIL, str(health.get("message", "")))
    except Exception as exc:                                        # noqa: BLE001
        report.add("connectivity", "InfluxDB is healthy", FAIL, f"{INFLUX_URL}: {exc}")
        return
    if not INFLUX_TOKEN:
        report.add("connectivity", "InfluxDB token is set", FAIL,
                   "INFLUX_TOKEN is empty: the writers silently do nothing")
        return
    try:
        buckets = requests.get(f"{INFLUX_URL}/api/v2/buckets", params={"limit": 100},
                               headers={"Authorization": f"Token {INFLUX_TOKEN}"}, timeout=15).json()
        names = {b.get("name") for b in buckets.get("buckets", [])}
        for bucket in sorted({g.bucket for g in gates} | {INFLUX_DEFAULT_BUCKET}):
            report.add("connectivity", f"bucket {bucket!r} exists", OK if bucket in names else FAIL,
                       "" if bucket in names else "run the gate's _init DAG")
    except Exception as exc:                                        # noqa: BLE001
        report.add("connectivity", "InfluxDB buckets are listable", FAIL, str(exc))


def devices_of(gate) -> list[dict[str, Any]]:
    return as_list(orion("/ngsi-ld/v1/entities", q=f'dataGate=="{gate.key}"', limit=1000,
                         type=",".join(sorted({s.entity_type for s in gate.discover()}) or ["Device"])))


def summaries_of(devices: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """The summaries of these devices, and how many they should have but do not.

    Found by id -- summary_measurement_urn(device, property) -- which is the
    only thing that ties a summary to one gate. This used to match on
    dataProvider or source, and two gates reading the same upstream share
    both: with weather_observed paused, `--gate weather_forecast` reported
    its eight idle summaries as weather_forecast's and failed a healthy gate.
    """
    expected = {summary_measurement_urn(d["id"], p) for d in devices for p in as_list(plain(d, "controlledProperty"))}
    found = [e for e in as_list(orion("/ngsi-ld/v1/entities", type="DeviceMeasurement",
                                      q='entityKind=="summary"', limit=1000))
             if e.get("id") in expected]
    return found, len(expected) - len(found)


def check_model(report: Report, gates: list) -> dict[str, list[dict[str, Any]]]:
    section("model")
    found: dict[str, list[dict[str, Any]]] = {}
    for gate in gates:
        expected = gate.discover()
        try:
            devices = devices_of(gate)
        except Exception as exc:                                     # noqa: BLE001
            report.add("model", f"{gate.key}: devices are queryable", FAIL, str(exc))
            continue
        found[gate.key] = devices
        status = OK if len(devices) >= len(expected) else (WARN if devices else FAIL)
        report.add("model", f"{gate.key}: {len(expected)} configured device(s) registered", status,
                   f"{len(devices)} in the broker" + ("" if devices else f"; run {gate.key}_init"))
        for device in devices:
            name = plain(device, "name", device.get("id"))
            properties = as_list(plain(device, "controlledProperty"))
            if not properties:
                report.add("model", f"{gate.key}: {name} declares its properties", FAIL,
                           "controlledProperty is missing")
            location = device.get("location")
            if location is not None:
                is_geo = isinstance(location, dict) and location.get("type") == "GeoProperty"
                report.add("model", f"{gate.key}: {name} location is a GeoProperty", OK if is_geo else FAIL,
                           "" if is_geo else "text in `location` breaks every geo query")
    return found


# The summary id — and therefore the InfluxDB measurement name — is minted
# by core.measurement_summary. Importing it rather than restating the uuid5
# recipe is the point: if this script computed it separately, the check
# would pass while consumers using the documented convention broke.


def check_bridge(report: Report, gates: list, devices: dict[str, list[dict[str, Any]]]) -> None:
    section("bridge")
    for gate in gates:
        for device in devices.get(gate.key, []):
            for prop in as_list(plain(device, "controlledProperty")):
                expected = summary_measurement_urn(device["id"], prop)
                try:
                    summary = orion(f"/ngsi-ld/v1/entities/{expected}")
                except requests.HTTPError as exc:
                    report.add("bridge", f"{gate.key}: summary for {prop}", FAIL,
                               f"{expected} is not in the broker ({exc.response.status_code})")
                    continue
                measurement = plain(summary, "influxMeasurement")
                matches = measurement == expected
                report.add("bridge", f"{gate.key}: {prop} points at its own measurement",
                           OK if matches else FAIL,
                           "" if matches else f"influxMeasurement is {measurement!r}, expected {expected!r}")
                bucket = plain(summary, "influxBucket", gate.bucket)
                # The window has to reach into the future. A forecast gate's
                # points are all ahead of now, so `range(start: -90d)` alone
                # counted 2 of the 48 hours actually stored and called that
                # healthy -- the same trap docs/data-model.md warns consumers
                # about, in the script meant to check it.
                rows = influx_flux(f'from(bucket: "{bucket}") |> range(start: -90d, stop: 90d) '
                                   f'|> filter(fn: (r) => r._measurement == "{expected}") |> count()')
                counted = sum(int(r.get("_value") or 0) for r in rows)
                report.add("bridge", f"{gate.key}: {prop} has points in InfluxDB",
                           OK if counted else WARN, f"{counted} point(s) in the last 90 days")


def check_freshness(report: Report, gates: list, devices: dict[str, list[dict[str, Any]]]) -> None:
    section("freshness")
    now = datetime.now(UTC)
    for gate in gates:
        interval = expected_interval(gate.schedule)
        allowed = (interval or timedelta(days=1)) * ARGS.tolerance
        stale = fresh = 0
        summaries, missing = summaries_of(devices.get(gate.key, []))
        for summary in summaries:
            last = plain(summary, "lastReadingAt")
            if not last:
                missing += 1
                continue
            try:
                when = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
            except ValueError:
                missing += 1
                continue
            if now - when > allowed:
                stale += 1
            else:
                fresh += 1
        detail = f"{fresh} fresh, {stale} stale, {missing} never written ({describe(gate.schedule)})"
        status = FAIL if stale else (WARN if missing and not fresh else OK)
        report.add("freshness", f"{gate.key}: summaries are within {allowed}", status, detail)


def check_queries(report: Report, gates: list) -> None:
    section("queries")
    # Probe the entity types the configured gates actually register, not
    # "Device": open_meteo registers WeatherForecastLocation and entsoe
    # MarketPriceFeed, so a hard-coded Device probe reported zero entities on a
    # perfectly healthy deployment and taught the reader to ignore warnings.
    types = sorted({spec.entity_type for gate in gates for spec in gate.discover()})
    probes = [(f"type filter ({name})", {"type": name, "limit": 1}) for name in types]
    probes += [
        ("summary filter", {"type": "DeviceMeasurement", "q": 'entityKind=="summary"', "limit": 1}),
        ("gate filter", {"q": f'dataGate=="{gates[0].key}"', "limit": 1} if gates else {"limit": 1}),
    ]
    for name, params in probes:
        try:
            result = as_list(orion("/ngsi-ld/v1/entities", **params))
            report.add("queries", f"{name} returns entities", OK if result else WARN,
                       f"{len(result)} entity/entities")
        except Exception as exc:                                     # noqa: BLE001
            report.add("queries", f"{name} returns entities", FAIL, str(exc))
    try:
        expanded = as_list(orion("/ngsi-ld/v1/entities", type="https://smartdatamodels.org/dataModel.Device/Device",
                                 limit=1))
        if expanded:
            report.add("queries", "attribute names are not expanded", FAIL,
                       "entities answer to expanded type names: a Smart Data Model context has been added, "
                       "which breaks every consumer's q= filter (see ROADMAP.md)")
        else:
            report.add("queries", "attribute names are not expanded", OK, "short names only, as documented")
    except requests.HTTPError:
        report.add("queries", "attribute names are not expanded", OK, "short names only, as documented")


# ── entry point ──────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    global ARGS
    ARGS = parse_args(argv)
    gates = [g for g in load_gates() if not ARGS.gate or g.key in ARGS.gate]
    if not gates:
        print("no enabled gates in config/gates.yaml match the request", file=sys.stderr)
        return 1
    report = Report()
    wanted = ARGS.section or list(SECTIONS)
    devices: dict[str, list[dict[str, Any]]] = {}
    if "connectivity" in wanted:
        check_connectivity(report, gates)
        if any(c["status"] == FAIL for c in report.checks):
            # Nothing below can mean anything if a store is unreachable.
            wanted = ["connectivity"]
    if "model" in wanted:
        devices = check_model(report, gates)
    if "bridge" in wanted:
        devices = devices or check_model(report, gates)
        check_bridge(report, gates, devices)
    if "freshness" in wanted:
        if not devices:
            devices = {g.key: devices_of(g) for g in gates}
        check_freshness(report, gates, devices)
    if "queries" in wanted:
        check_queries(report, gates)

    counts = report.counts()
    if ARGS.json:
        print(json.dumps({"checks": report.checks, "counts": counts,
                          "gates": [g.key for g in gates]}, indent=2))
    else:
        print(f"\n{counts[OK]} ok, {counts[WARN]} warning(s), {counts[FAIL]} failure(s)")
    return 1 if counts[FAIL] else 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[1],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--section", action="append", choices=SECTIONS,
                        help="run only this section (repeatable)")
    parser.add_argument("--gate", action="append", help="check only this gate key (repeatable)")
    parser.add_argument("--tolerance", type=float, default=3.0,
                        help="how many cadences a summary may lag before it counts as stale (default 3)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    return parser.parse_args(argv)


# Defaults, so the checks can be imported and called from a DAG or a test;
# main() replaces them with what the command line asked for.
ARGS = parse_args([])

if __name__ == "__main__":
    os.environ.setdefault("DATAGATES_CONFIG", str(ROOT / "config" / "gates.yaml"))
    sys.exit(main())
