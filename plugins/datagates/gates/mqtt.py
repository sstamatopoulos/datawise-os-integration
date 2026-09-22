"""
MQTT gate — a broker's topics, drained on a schedule.

Airflow is a batch scheduler and MQTT is a stream, so the interesting part of
this gate is not the protocol but how the two are reconciled without losing
messages between runs.

The answer is the broker's own persistent session. The gate connects with a
fixed client id and `clean_session: false`, and subscribes at QoS 1. The
broker then queues matching messages *while the gate is not connected* and
delivers them at the next run, which is exactly the semantics a scheduled
drain needs. Nothing runs between DAG runs, and nothing is lost as long as the
broker's queue holds (mosquitto's `max_queued_messages` is 1000 by default —
raise it, or shorten the schedule, for busy topics).

    - key: lora
      type: mqtt
      schedule: "*/10 * * * *"
      options:
        host: mqtt.example.org
        port: 8883
        tls: true
        username: ${MQTT_USER}
        password: ${MQTT_PASSWORD}
        client_id: datagates-lora         # must be stable: it identifies the sessions
        collect_seconds: 30               # how long each run stays connected
        devices:
          - id: room120
            name: Room 120B sensor
            topic: "application/1/device/room120/event/up"
            payload: json                 # json | value
            time_field: time              # optional: use the payload's own timestamp
            fields:
              "object.co2":         {property: co2, unit: "59"}
              "object.temperature": {property: temperature, unit: CEL}

Field keys are dotted paths into the JSON payload, so a LoRaWAN network
server's nested `object` or a vendor's `d.values` needs no transformation
upstream. With `payload: value` the message body is a bare number and the
single field's key is ignored.

**One session per device, not per gate.** The run DAG fans out over devices,
so the tasks of one gate run in parallel; if they shared a client id they
would disconnect each other, and whichever connected first would receive —
and then drop — the messages belonging to the others. Each device therefore
gets its own subscription and its own session, `<client_id>-<device id>`, and
the broker queues per device. This is the whole reason the option is called
`client_id` and has to stay stable: it is the name the broker remembers the
queue by.

The rest of what experience teaches:

- **Timestamps.** Use the payload's own `time_field` when there is one:
  arrival time is the time the broker delivered a queued message, which after
  a missed run is minutes or hours later than the measurement. Without a
  `time_field` the arrival time is used, and the series then quietly encodes
  the schedule rather than the phenomenon.
- **Wildcards work** (`+`, `#`), and a message matching two devices is stored
  for both, which is what a wildcard means.
- **`mode: spool`** reads JSON lines written by an always-on subscriber
  (scripts/mqtt_spool.py, run as a compose service) instead of connecting from
  the task. That is the right shape for busy topics or for a broker that will
  not grant persistent sessions. The subscriber routes each message to the
  spool file of the device whose topic filter it matches, for the same
  fan-out reason.

Needs the `paho-mqtt` extra (pip install -r requirements-gates.txt).
"""
from __future__ import annotations

import glob
import json
import logging
import os
import re
import time
from datetime import datetime
from typing import Any

from datagates.core.fieldmap import FieldMap
from datagates.core.timeparse import iso_z, parse_stamp, stamp_now
from datagates.gates.base import DeviceSpec, Gate, Sample
from datagates.gates.http_json import dig

log = logging.getLogger(__name__)

_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def topic_matches(pattern: str, topic: str) -> bool:
    """MQTT topic filter matching, including + and #."""
    if pattern == topic:
        return True
    parts, levels = pattern.split("/"), topic.split("/")
    for index, part in enumerate(parts):
        if part == "#":
            return True
        if index >= len(levels):
            return False
        if part != "+" and part != levels[index]:
            return False
    return len(parts) == len(levels)


def spool_prefix(device_id: str) -> str:
    """The spool file prefix of one device. Shared by the gate and
    scripts/mqtt_spool.py: if the two disagreed, the subscriber would write
    files the gate never reads, and the drop directory would fill up in
    silence."""
    return _UNSAFE.sub("_", str(device_id)) or "device"


class MqttGate(Gate):
    type_name = "mqtt"
    speaks = "MQTT brokers (LoRaWAN network servers, IoT gateways, SCADA bridges)"
    default_schedule = "*/10 * * * *"
    rolling = True                  # a drain has no window: it stores what arrived
    backfill_mode = "none"          # a broker keeps no history
    device_attrs = ("mqttDeviceId", "mqttTopic")

    def __init__(self, config):
        super().__init__(config)
        self.mode = str(self.option("mode", "collect")).lower()
        if self.mode not in ("collect", "spool"):
            raise ValueError(f"{self.key}: mode must be 'collect' or 'spool'")
        self.host = str(self.option("host", ""))
        if self.mode == "collect" and not self.host:
            raise ValueError(f"{self.key}: options.host is required in collect mode")
        self.port = int(self.option("port", 1883))
        self.tls = bool(self.option("tls", False))
        self.username = str(self.option("username", ""))
        self.password = str(self.option("password", ""))
        self.client_id = str(self.option("client_id", f"datagates-{self.key}"))
        self.qos = int(self.option("qos", 1))
        self.collect_seconds = float(self.option("collect_seconds", 30))
        self.spool_dir = str(self.option("spool_dir", f"/opt/airflow/drop/{self.key}-mqtt"))
        self.entries = self.devices_option()
        self.maps = {str(e["id"]): FieldMap(e.get("fields") or self.option("fields"),
                                            gate_key=self.key, block="fields") for e in self.entries}
        if not self.maps:
            raise ValueError(f"{self.key}: options.devices must list at least one device")
        self.cumulative = frozenset().union(*(m.cumulative for m in self.maps.values()))  # type: ignore[misc]

    def discover(self) -> list[DeviceSpec]:
        return [DeviceSpec(
            urn=self.urn(entry["id"]), name=str(entry.get("name") or entry["id"]),
            properties=self.maps[str(entry["id"])].properties,
            ref_building=entry.get("ref_building"),
            category=list(entry.get("category", ["sensor"])),
            attrs={"mqttDeviceId": str(entry["id"]), "mqttTopic": str(entry.get("topic", ""))},
        ) for entry in self.entries]

    def topics(self) -> list[str]:
        """Every device's topic filter, for the spool subscriber."""
        return [str(e["topic"]) for e in self.entries if e.get("topic")]

    def devices_for(self, topic: str) -> list[str]:
        """Which device ids a message on this topic belongs to."""
        return [str(e["id"]) for e in self.entries
                if e.get("topic") and topic_matches(str(e["topic"]), topic)]

    # -- collecting ---------------------------------------------------------
    def collect(self, topic: str, client_id: str) -> list[dict[str, Any]]:
        """Connect as `client_id`, drain that session's queue for
        collect_seconds, return the messages as {topic, payload, received}."""
        import paho.mqtt.client as mqtt  # imported here: optional dependency

        messages: list[dict[str, Any]] = []

        def on_connect(client, _userdata, _flags, _reason, _properties=None):
            client.subscribe(topic, qos=self.qos)

        def on_message(_client, _userdata, message):
            messages.append({"topic": message.topic,
                             "payload": message.payload.decode("utf-8", errors="replace"),
                             "received": stamp_now()})

        try:                                                 # paho 2.x
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id,
                                 clean_session=False)
        except AttributeError:                               # pragma: no cover - paho 1.x
            client = mqtt.Client(client_id=client_id, clean_session=False)
        client.on_connect, client.on_message = on_connect, on_message
        if self.username:
            client.username_pw_set(self.username, self.password)
        if self.tls:
            client.tls_set()
        client.connect(self.host, self.port, keepalive=60)
        deadline = time.monotonic() + self.collect_seconds
        while time.monotonic() < deadline:
            client.loop(timeout=1.0)
        client.disconnect()
        log.info("%s: %s collected %d message(s) in %.0fs", self.key, client_id,
                 len(messages), self.collect_seconds)
        return messages

    def spooled(self, device_id: str) -> list[dict[str, Any]]:
        """Messages the always-on subscriber wrote for this device
        (scripts/mqtt_spool.py).

        Only finished files are read: the subscriber appends to
        `<name>.jsonl.part` and renames it to `<name>.jsonl` when it rotates,
        so a file this gate can see is one nobody is still writing to. It is
        deleted once read, which makes the handover exactly-once without a
        lock. Truncating a file still open for appending elsewhere would lose
        whatever arrived in between, and nobody would notice for a month."""
        messages: list[dict[str, Any]] = []
        pattern = os.path.join(self.spool_dir, f"{spool_prefix(device_id)}-*.jsonl")
        for path in sorted(glob.glob(pattern)):
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        messages.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
            os.unlink(path)
        return messages

    # -- the contract -------------------------------------------------------
    def fetch(self, device: dict[str, Any], start: datetime, end: datetime) -> list[Sample]:
        device_id = str(device.get("mqttDeviceId") or "")
        entry = next((e for e in self.entries if str(e["id"]) == device_id), None)
        fields = self.maps.get(device_id)
        if entry is None or fields is None:
            return []
        if self.mode == "collect":
            topic = str(entry.get("topic") or device.get("mqttTopic") or "")
            if not topic:
                log.warning("%s: device %s has no topic", self.key, device_id)
                return []
            messages = self.collect(topic, f"{self.client_id}-{spool_prefix(device_id)}")
        else:
            messages = self.spooled(device_id)
        return self.samples(messages, device["urn"], entry, fields)

    def samples(self, messages: list[dict[str, Any]], urn: str, entry: dict[str, Any],
                fields: FieldMap) -> list[Sample]:
        pattern = str(entry.get("topic", ""))
        time_field = entry.get("time_field")
        as_value = str(entry.get("payload", "json")).lower() == "value"
        out: list[Sample] = []
        for message in messages:
            if pattern and not topic_matches(pattern, str(message.get("topic", ""))):
                continue
            body: Any = message.get("payload")
            if not as_value:
                try:
                    body = json.loads(body) if isinstance(body, str) else body
                except json.JSONDecodeError:
                    log.warning("%s: %s carried a payload that is not JSON", self.key, message.get("topic"))
                    continue
            observed_at = message.get("received") or stamp_now()
            if time_field and isinstance(body, dict):
                stamp = parse_stamp(dig(body, str(time_field)))
                if stamp is not None:
                    observed_at = iso_z(stamp)
            if as_value:
                field = fields.fields[0]
                value = field.convert(body)
                if value is not None:
                    out.append(Sample(urn, field.property, value, observed_at))
                continue
            row = {f.source: dig(body, f.source) for f in fields}
            out.extend(fields.samples(urn, row, observed_at))
        return out
