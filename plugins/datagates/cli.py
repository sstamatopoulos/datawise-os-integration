"""
datagates.cli — try a gate without Airflow.

The loop this replaces: edit gates.yaml, rebuild the image, wait for the
scheduler to parse the bag, trigger `<key>_init` in the web interface, open
the task log, read the traceback, edit gates.yaml again. Twenty minutes to
learn that a register address was off by one.

    datagates types                     what gate types exist, and what they speak to
    datagates list                      what this gates.yaml configures
    datagates check indoor_air          construct the gate and register nothing
    datagates fetch indoor_air --hours 6    pull a window and show what came back
    datagates fetch indoor_air --json | jq  the samples themselves

Nothing here writes: not to Orion-LD, not to InfluxDB, not to the upstream.
`fetch` does talk to the upstream, because that is the point — it is the
fastest way to find out that a timestamp is in local time, that a meter
reverses its words, or that the portal answers HTML when a session expires.

Everything is built the way the run DAG builds it — `discover()` to a
DeviceSpec, through the Orion entity, back to the flat device dict — so
what `fetch` sees is what the DAG will see, including Orion's habit of
compacting one-element arrays.

Exit status: 0 when every device answered, 1 when any did not, 2 for a
usage or configuration error. That makes it usable in a deployment check:

    datagates check weather_forecast || echo "the weather gate is unhappy"
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

from datagates.core.cadence import describe
from datagates.core.entities import device_doc, device_entity
from datagates.core.timeparse import iso_z, parse_stamp
from datagates.gates.base import Gate, Sample
from datagates.gates.registry import BUILTIN_TYPES, load_gates, resolve_type

OK, FAILED, USAGE = 0, 1, 2


def _out(text: str = "") -> None:
    """Print without assuming the console can encode anything but ASCII: an
    operator's terminal is whatever code page it is, and a crash in a
    diagnostic tool is worse than a plain-looking one."""
    sys.stdout.write(text.encode("ascii", "replace").decode("ascii") + "\n")


def _gates(args) -> list[Gate]:
    return load_gates(args.config, include_disabled=True)


def _gate(args) -> Gate:
    wanted = [g for g in _gates(args) if g.key == args.key]
    if not wanted:
        keys = sorted(g.key for g in _gates(args))
        raise SystemExit(f"no gate named {args.key!r} in the config; configured: {', '.join(keys)}")
    return wanted[0]


def _docs(gate: Gate) -> list[dict[str, Any]]:
    """The device dicts fetch() will receive, built through the entity so
    that the device_attrs contract is exercised, not bypassed."""
    return [device_doc(device_entity(spec, gate), gate) for spec in gate.discover()]


def _window(gate: Gate, args) -> tuple[datetime, datetime]:
    now = datetime.now(UTC)
    if gate.rolling:
        # A rolling gate chooses its own window; the factory calls it with
        # (now, now) and stores everything it returns.
        return now, now
    end = parse_stamp(args.end) if getattr(args, "end", None) else now
    if getattr(args, "start", None):
        start = parse_stamp(args.start)
    else:
        start = end - timedelta(hours=float(args.hours))
    if start is None or end is None:
        raise SystemExit("could not parse --start/--end; use ISO 8601, e.g. 2026-09-16T00:00:00Z")
    return start, end


# ── commands ─────────────────────────────────────────────────────────

def _kind(cls: type[Gate]) -> str:
    """poll / history / either, read off the class without instantiating it.

    `opcua`, `obix`, `ngsi_v2` and `open_meteo` decide from their options and
    so declare `backfill_mode` as a property; reading it off the class gives
    the property object, which is not "none" and quietly mislabels them.
    """
    for klass in cls.__mro__:
        if "backfill_mode" in klass.__dict__:
            value = klass.__dict__["backfill_mode"]
            if isinstance(value, property):
                return "either"
            return "poll" if value == "none" else "history"
    return "history"                                            # pragma: no cover


def cmd_types(args) -> int:
    for name in sorted(BUILTIN_TYPES):
        try:
            cls = resolve_type(name)
        except Exception as exc:                                # noqa: BLE001
            _out(f"{name:<14} !! {exc}")
            continue
        _out(f"{name:<14} {_kind(cls):<8} {cls.speaks or '-'}")
    _out()
    _out(f"{len(BUILTIN_TYPES)} built-in types. A gate type can also be 'module.path:ClassName'.")
    return OK


def cmd_list(args) -> int:
    gates = _gates(args)
    if not gates:
        _out("no gates configured; see config/gates.yaml")
        return OK
    _out(f"{'key':<18} {'type':<14} {'state':<9} {'schedule':<15} {'cadence':<14} dags")
    for gate in gates:
        dags = 2 if gate.backfill_mode == "none" else 3
        state = "enabled" if gate.config.enabled else "disabled"
        _out(f"{gate.key:<18} {gate.type_name:<14} {state:<9} {str(gate.schedule):<15} "
             f"{describe(gate.schedule):<14} {dags}")
    enabled = sum(1 for g in gates if g.config.enabled)
    _out()
    _out(f"{len(gates)} gates configured, {enabled} enabled")
    return OK


def cmd_check(args) -> int:
    """Construct the gate and ask it what it serves. Touches no upstream
    unless the gate discovers from one."""
    gate = _gate(args)
    _out(f"{gate.key} ({gate.type_name}) -> {gate.speaks or 'upstream'}")
    _out(f"  schedule {gate.schedule} ({describe(gate.schedule)}), "
         f"{'rolling' if gate.rolling else 'windowed'}, backfill {gate.backfill_mode}")
    _out(f"  bucket {gate.bucket}, source {gate.source}, provider {gate.data_provider}")
    if gate.cumulative:
        _out(f"  counters guarded: {', '.join(sorted(gate.cumulative))}")
    try:
        devices = gate.discover()
    except Exception as exc:                                    # noqa: BLE001
        _out(f"  FAILED discover(): {type(exc).__name__}: {exc}")
        return FAILED
    if not devices:
        _out("  FAILED: discover() returned no devices")
        return FAILED
    for spec in devices:
        properties = ", ".join(f"{p} [{u}]" for p, u in spec.properties.items())
        _out(f"  device {spec.name}")
        _out(f"    {spec.urn}")
        _out(f"    {properties}")
    _out(f"  {len(devices)} device(s), "
         f"{sum(len(d.properties) for d in devices)} summary entities would be created")
    return OK


def _describe_samples(samples: list[Sample]) -> list[str]:
    by_property: dict[str, list[Sample]] = {}
    for sample in samples:
        by_property.setdefault(sample.controlled_property, []).append(sample)
    lines = []
    for prop in sorted(by_property):
        values = [s.value for s in by_property[prop]]
        stamps = sorted(s.observed_at for s in by_property[prop])
        lines.append(f"    {prop:<24} {len(values):>5} samples  "
                     f"min {min(values):>12.3f}  max {max(values):>12.3f}  "
                     f"{stamps[0]} .. {stamps[-1]}")
    return lines


def cmd_fetch(args) -> int:
    gate = _gate(args)
    start, end = _window(gate, args)
    try:
        docs = _docs(gate)
    except Exception as exc:                                    # noqa: BLE001
        _out(f"FAILED discover(): {type(exc).__name__}: {exc}")
        return FAILED
    if args.device:
        docs = [d for d in docs if args.device in (d["urn"], d["name"])]
        if not docs:
            raise SystemExit(f"no device matching {args.device!r}; run 'datagates check {gate.key}'")
    if not args.json:
        window = "the gate's own window" if gate.rolling else f"{iso_z(start)} .. {iso_z(end)}"
        _out(f"{gate.key}: fetching {window} for {len(docs)} device(s); nothing is written")
    status = OK
    collected: list[Sample] = []
    for doc in docs:
        try:
            samples = gate.fetch(doc, start, end)
        except Exception as exc:                                # noqa: BLE001
            _out(f"  FAILED {doc['name']}: {type(exc).__name__}: {exc}")
            status = FAILED
            continue
        collected.extend(samples)
        if args.json:
            continue
        _out(f"  {doc['name']}: {len(samples)} sample(s)")
        if not samples:
            _out("    nothing in this window. Not necessarily broken: a meter read daily has "
                 "nothing to say about the last hour.")
        for line in _describe_samples(samples):
            _out(line)
    if args.json:
        limit = int(args.limit) if args.limit else None
        print(json.dumps([s.as_point() for s in collected][:limit], indent=2))
    return status


# ── entry point ──────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="datagates", description="Try a gate without Airflow. Nothing here writes.")
    parser.add_argument("--config", help="gates.yaml (default: $DATAGATES_CONFIG)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("types", help="the built-in gate types").set_defaults(func=cmd_types)
    sub.add_parser("list", help="the gates this config declares").set_defaults(func=cmd_list)

    check = sub.add_parser("check", help="construct one gate and show what it serves")
    check.add_argument("key")
    check.set_defaults(func=cmd_check)

    fetch = sub.add_parser("fetch", help="pull a window from the upstream and show it")
    fetch.add_argument("key")
    fetch.add_argument("--hours", default=24, help="how far back to ask (default 24)")
    fetch.add_argument("--start", help="ISO 8601; overrides --hours")
    fetch.add_argument("--end", help="ISO 8601; default now")
    fetch.add_argument("--device", help="one device by urn or name")
    fetch.add_argument("--json", action="store_true", help="the samples as JSON on stdout")
    fetch.add_argument("--limit", help="with --json, at most this many samples")
    fetch.set_defaults(func=cmd_fetch)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except SystemExit as exc:                     # our own messages, not argparse's
        if isinstance(exc.code, str):
            _out(exc.code)
            return USAGE
        raise
    except KeyboardInterrupt:                     # pragma: no cover
        return FAILED


if __name__ == "__main__":                        # pragma: no cover
    sys.exit(main())
