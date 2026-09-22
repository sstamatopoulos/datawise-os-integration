#!/usr/bin/env python3
"""
mqtt_spool.py — the always-on subscriber behind `mqtt` gates in spool mode.

A gate in `mode: collect` connects from the Airflow task itself and relies on
the broker's persistent session to hold messages in between. That needs no
extra service and is enough for most feeds. It is not enough when the topic is
busy (the broker's queue overflows between runs) or when the broker refuses
persistent sessions, and then this process takes over: it stays connected,
writes every message as a JSON line, and the gate's run DAG drains the files.

    python scripts/mqtt_spool.py --gate lora
    python scripts/mqtt_spool.py --gate lora --rotate-seconds 30

In compose, as a service alongside the scheduler:

    mqtt-spool:
      build: .
      command: python /opt/airflow/scripts/mqtt_spool.py --gate lora
      volumes: ["./config:/opt/airflow/config:ro", "./drop:/opt/airflow/drop",
                "./scripts:/opt/airflow/scripts:ro", "./plugins:/opt/airflow/plugins:ro"]
      environment: {PYTHONPATH: /opt/airflow/plugins, DATAGATES_CONFIG: /opt/airflow/config/gates.yaml}
      env_file: [.env]
      restart: unless-stopped

Two things make the handover safe, and both matter:

**One file set per device.** The run DAG fans out over devices, so each
device's task drains its own files; if every message went to one shared file,
the first task to run would delete the messages belonging to the others. This
process routes each message to the spool of every device whose topic filter it
matches (a wildcard overlap writes it to both, which is what a wildcard
means), using the same `spool_prefix` the gate reads with.

**Handover by rename, not by truncation.** Messages go into
`<device>-<timestamp>.jsonl.part`; when the file reaches `--rotate-seconds` or
`--rotate-bytes` it is closed and renamed to `.jsonl`. The gate only reads
finished `.jsonl` files and deletes them afterwards, so nothing can be lost to
a reader truncating a file this process is still appending to — the kind of
loss nobody notices until a month of data is missing.

What *is* lost if this process is down: everything published while it is down,
because it subscribes with a clean session. A spool subscriber asking for a
persistent one would compete with the gate for the same client id. Run it
under `restart: unless-stopped` and monitor it like any other collector.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins"))

from datagates.gates.mqtt import spool_prefix  # noqa: E402
from datagates.gates.registry import load_gates  # noqa: E402

log = logging.getLogger("mqtt_spool")
RUNNING = True


class DeviceSpool:
    """Append JSON lines for one device and rename the file when it is full."""

    def __init__(self, directory: str, device_id: str, rotate_seconds: float, rotate_bytes: int) -> None:
        self.directory = directory
        self.prefix = spool_prefix(device_id)
        self.rotate_seconds, self.rotate_bytes = rotate_seconds, rotate_bytes
        self._handle = None
        self._path = ""
        self._opened_at = 0.0
        self._written = 0

    def _open(self) -> None:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
        self._path = os.path.join(self.directory, f"{self.prefix}-{stamp}.jsonl")
        self._handle = open(f"{self._path}.part", "a", encoding="utf-8")
        self._opened_at, self._written = time.monotonic(), 0

    def write(self, record: dict) -> None:
        if self._handle is None:
            self._open()
        line = json.dumps(record, separators=(",", ":"), default=str)
        self._handle.write(line + "\n")
        self._handle.flush()
        self._written += len(line)
        if (time.monotonic() - self._opened_at >= self.rotate_seconds
                or self._written >= self.rotate_bytes):
            self.rotate()

    def rotate(self) -> None:
        if self._handle is None:
            return
        self._handle.close()
        self._handle = None
        if self._written:
            os.replace(f"{self._path}.part", self._path)      # atomic: now visible to the gate
            log.info("rotated %s (%d bytes)", os.path.basename(self._path), self._written)
        else:
            os.unlink(f"{self._path}.part")


class Router:
    """Message -> the spool of every device whose topic filter matches."""

    def __init__(self, gate, directory: str, rotate_seconds: float, rotate_bytes: int) -> None:
        self.gate, self.directory = gate, directory
        self.rotate_seconds, self.rotate_bytes = rotate_seconds, rotate_bytes
        os.makedirs(directory, exist_ok=True)
        self.spools: dict[str, DeviceSpool] = {}
        self.unmatched = 0

    def spool(self, device_id: str) -> DeviceSpool:
        if device_id not in self.spools:
            self.spools[device_id] = DeviceSpool(self.directory, device_id,
                                                 self.rotate_seconds, self.rotate_bytes)
        return self.spools[device_id]

    def write(self, record: dict) -> None:
        targets = self.gate.devices_for(str(record.get("topic", "")))
        if not targets:
            self.unmatched += 1
            return
        for device_id in targets:
            self.spool(device_id).write(record)

    def rotate_all(self) -> None:
        for spool in self.spools.values():
            spool.rotate()


def run(gate, router: Router, qos: int, rotate_seconds: float) -> None:
    import paho.mqtt.client as mqtt

    topics = gate.topics()
    if not topics:
        raise SystemExit(f"gate {gate.key!r} has no device topics to subscribe to")

    def on_connect(client, _userdata, _flags, reason, _properties=None):
        log.info("connected to %s:%s (%s); subscribing to %s",
                 gate.host, gate.port, reason, ", ".join(topics))
        for topic in topics:
            client.subscribe(topic, qos=qos)

    def on_message(_client, _userdata, message):
        router.write({"topic": message.topic,
                      "payload": message.payload.decode("utf-8", errors="replace"),
                      "received": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")})

    def on_disconnect(_client, _userdata, reason, *_a):
        log.warning("disconnected (%s); paho will reconnect", reason)

    try:                                                      # paho 2.x
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"{gate.client_id}-spool")
    except AttributeError:                                    # pragma: no cover - paho 1.x
        client = mqtt.Client(client_id=f"{gate.client_id}-spool")
    client.on_connect, client.on_message, client.on_disconnect = on_connect, on_message, on_disconnect
    if gate.username:
        client.username_pw_set(gate.username, gate.password)
    if gate.tls:
        client.tls_set()
    client.reconnect_delay_set(min_delay=1, max_delay=60)
    client.connect(gate.host, gate.port, keepalive=60)
    # A quiet topic must still hand over what it has, so rotate on a timer as
    # well as on size: otherwise the last message of the evening waits for the
    # next one to arrive before the gate can see it.
    next_rotate = time.monotonic() + rotate_seconds
    while RUNNING:
        client.loop(timeout=1.0)
        if time.monotonic() >= next_rotate:
            router.rotate_all()
            next_rotate = time.monotonic() + rotate_seconds
    router.rotate_all()
    client.disconnect()
    log.info("stopped; %d message(s) matched no device", router.unmatched)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Spool an MQTT gate's topics to JSON lines")
    parser.add_argument("--gate", required=True, help="gate key from config/gates.yaml")
    parser.add_argument("--spool-dir", help="override the gate's spool_dir")
    parser.add_argument("--rotate-seconds", type=float, default=60.0)
    parser.add_argument("--rotate-bytes", type=int, default=4_000_000)
    parser.add_argument("--qos", type=int, default=1)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    os.environ.setdefault("DATAGATES_CONFIG", str(ROOT / "config" / "gates.yaml"))
    gates = {g.key: g for g in load_gates(include_disabled=True)}
    gate = gates.get(args.gate)
    if gate is None or gate.type_name != "mqtt":
        raise SystemExit(f"no mqtt gate named {args.gate!r}; configured: "
                         f"{sorted(k for k, g in gates.items() if g.type_name == 'mqtt')}")

    def stop(*_a):
        global RUNNING
        RUNNING = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    router = Router(gate, args.spool_dir or gate.spool_dir, args.rotate_seconds, args.rotate_bytes)
    run(gate, router, args.qos, args.rotate_seconds)
    return 0


if __name__ == "__main__":
    sys.exit(main())
