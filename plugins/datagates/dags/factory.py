"""
datagates.dags.factory — three Airflow DAGs per configured gate.

    build_dags(gate) -> {"<key>_init": DAG, "<key>_run": DAG, "<key>_backfill": DAG}

The DAGs are the same for every gate; only discover() and fetch() differ.
That is the point: a new upstream system is a small class plus a YAML
entry, and it inherits the watermarking, idempotent writes, counter
guard, summary refresh and backfill state machine unchanged.

Device entities carry `dataGate: <key>` so each gate's run DAG finds
exactly its own devices whatever `source` they share with another gate.

One Airflow 3 trap, for whoever writes a test or a health check against
this module: the `@dag` decorator here produces an `airflow.sdk` DAG, and
`isinstance(x, airflow.models.DAG)` is False for it. Counting DAGs that
way reports an empty bag while every DAG has in fact been built. Parse the
folder with `airflow.models.dagbag.DagBag` instead, which is what the
scheduler does and which also reports import errors with their file.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any

from airflow.decorators import dag, task
from airflow.models.param import Param
from airflow.operators.python import get_current_context

from datagates.core.counter_guard import guard_samples_by_property
from datagates.core.dag_params import parse_end_datetime, parse_start_date
from datagates.core.entities import building_entity, device_entity, iso_z
from datagates.core.entities import device_doc as _device_doc
from datagates.core.entities import ms_of as _ms
from datagates.core.entities import prop as _prop
from datagates.core.influx_writer import InfluxWriter
from datagates.core.measurement_summary import refresh_summaries, summary_entity
from datagates.core.orion_registry import OrionRegistry
from datagates.core.settings import INFLUX_BATCH_SIZE
from datagates.gates.base import Gate, Sample

log = logging.getLogger(__name__)

_START = datetime(2025, 1, 1)


def _iso(ms: int) -> str:
    return iso_z(datetime.fromtimestamp(ms / 1000, tz=UTC))


# ── shared write path ───────────────────────────────────────────────

def _write(gate: Gate, w: InfluxWriter, orion: OrionRegistry, samples: list[Sample],
           *, last_known: dict[str, float] | None = None,
           only_if_newer: bool = True) -> tuple[int, int, str]:
    """Guard counters, write to InfluxDB, refresh summaries.
    Returns (accepted, written, summary_status)."""
    points = [s.as_point() for s in samples]
    if gate.cumulative:
        points, guard = guard_samples_by_property(points, last_known, cumulative=set(gate.cumulative))
        if guard.rejected:
            log.warning("%s: counter guard rejected %d reading(s): %s",
                        gate.key, len(guard.rejected), guard.summary)
    written = sum(w.write_many(points[i:i + INFLUX_BATCH_SIZE])
                  for i in range(0, len(points), INFLUX_BATCH_SIZE))
    status = "nothing"
    if written:
        p, s, f = refresh_summaries(w, orion, points, only_if_newer=only_if_newer)
        status = f"patched {p}, skipped {s}, failed {f}"
    return len(points), written, status


def _windows(start: datetime, end: datetime, step: timedelta):
    a = start
    while a < end:
        b = min(a + step, end)
        yield a, b
        a = b


# ── the three DAGs ───────────────────────────────────────────────────

def build_init_dag(gate: Gate):
    @dag(dag_id=f"{gate.key}_init", start_date=_START, schedule=None, catchup=False,
         tags=[*gate.tags, "init"], description=f"Register {gate.key} ({gate.type_name}) in Orion-LD and InfluxDB")
    def _init():
        @task
        def register() -> dict:
            orion = OrionRegistry()
            stats = {"buildings": 0, "devices": 0, "summaries": 0, "influx_seeded": 0}
            for b in gate.buildings():
                orion.upsert_entity(building_entity(b, gate))
                stats["buildings"] += 1
            devices = gate.discover()
            for d in devices:
                orion.upsert_entity(device_entity(d, gate))
                stats["devices"] += 1
            with InfluxWriter(bucket=gate.bucket) as w:
                stats["bucket_ok"] = w.ensure_bucket(gate.bucket)
                for d in devices:
                    stats["influx_seeded"] += w.ensure_measurement(d.urn, list(d.properties))
            for d in devices:
                for prop, unit in d.properties.items():
                    orion.upsert_entity(summary_entity(d.urn, prop, unit, gate.bucket,
                                                       gate.data_provider, gate.source))
                    stats["summaries"] += 1
            log.info("%s init: %s", gate.key, json.dumps(stats))
            return stats
        register()
    return _init()


def build_run_dag(gate: Gate):
    @dag(dag_id=f"{gate.key}_run", start_date=_START, schedule=gate.schedule, catchup=False,
         max_active_runs=1, default_args={"retries": 2, "retry_delay": timedelta(minutes=5)},
         tags=[*gate.tags, "run"], description=f"Ingest {gate.key} ({gate.type_name})")
    def _run():
        @task
        def load_devices() -> list[dict]:
            docs = [_device_doc(e, gate)
                    for e in OrionRegistry().get_all_devices(q=f'dataGate=="{gate.key}"')]
            log.info("%s: %d device(s)", gate.key, len(docs))
            return docs

        @task(execution_timeout=timedelta(minutes=30))
        def ingest(doc: dict) -> str:
            now = datetime.now(UTC)
            last_ms = int(doc["last_ingested_ms"])
            samples: list[Sample] = []
            if gate.rolling:
                # Forecast-like feeds: the gate chooses its own window around
                # now and every returned hour overwrites what was stored.
                samples = gate.fetch(doc, now, now)
                start = now
            else:
                start = datetime.fromtimestamp(last_ms / 1000, tz=UTC) if last_ms else now - gate.live_lookback
                start -= gate.rewrite_lookback
                if gate.history_start:
                    start = max(start, datetime.combine(gate.history_start, datetime.min.time(), tzinfo=UTC))
                floor_ms = last_ms if not gate.rewrite_lookback else 0
                for a, b in _windows(start, now, gate.max_window):
                    samples += [s for s in gate.fetch(doc, a, b) if _ms(s.observed_at) > floor_ms]
            if not samples:
                return f"{doc['urn']}: no new data since {start:%Y-%m-%d %H:%M}"
            orion = OrionRegistry()
            last_known = _last_known(orion, doc) if gate.cumulative else None
            with InfluxWriter(bucket=gate.bucket) as w:
                n, written, status = _write(gate, w, orion, samples, last_known=last_known)
            newest = max(_ms(s.observed_at) for s in samples)
            if written and newest > last_ms:
                orion.patch_last_ingested(doc["urn"], newest)
            return f"{doc['urn']}: {n} samples, wrote {written}; summaries {status}"

        @task
        def refresh_status(docs: list[dict]) -> str:
            updates = gate.status(docs)
            if not updates:
                return "no status attributes for this gate"
            orion = OrionRegistry()
            ok = 0
            for urn, attrs in updates.items():
                try:
                    orion.patch_attrs(urn, {k: _prop(v) for k, v in attrs.items()})
                    ok += 1
                except Exception as exc:                          # noqa: BLE001
                    log.warning("%s: status patch failed for %s: %s", gate.key, urn, exc)
            return f"status patched on {ok}/{len(updates)} devices"

        docs = load_devices()
        ingest.expand(doc=docs)
        refresh_status(docs)
    return _run()


def _last_known(orion: OrionRegistry, doc: dict) -> dict[str, float]:
    """Latest stored value per cumulative property, for the counter guard."""
    try:
        return orion.get_last_reading_values(doc["urn"], list(doc["properties"]))
    except Exception:                                             # noqa: BLE001
        return {}


def build_backfill_dag(gate: Gate):
    stateless = gate.backfill_mode == "stateless"
    hs = gate.history_start.isoformat() if gate.history_start else "2024-01-01"
    params = {
        "window_days": Param(min(gate.max_window.days, 30), type="integer", minimum=1, maximum=366),
        "dry_run": Param(False, type="boolean"),
    }
    if stateless:
        params["start_date"] = Param(hs, type="string", format="date")
        params["end_date"] = Param("", type="string", description="Leave empty for now. Or YYYY-MM-DD.")
    else:
        params["windows_per_run"] = Param(4, type="integer", minimum=1, maximum=100)
        params["empty_streak_threshold"] = Param(3, type="integer", minimum=1, maximum=50)
        params["floor_iso"] = Param(hs, type="string", format="date")

    @dag(dag_id=f"{gate.key}_backfill", start_date=_START,
         schedule=None if stateless else "*/30 * * * *", catchup=False, max_active_runs=1,
         default_args={"retries": 1, "retry_delay": timedelta(minutes=5)},
         tags=[*gate.tags, "backfill"], params=params,
         description=f"Load {gate.key} history ({'stateless' if stateless else 'cursor walk'})")
    def _backfill():
        @task
        def load_devices() -> list[dict]:
            orion = OrionRegistry()
            docs = []
            for e in orion.get_all_devices(q=f'dataGate=="{gate.key}"'):
                d = _device_doc(e, gate)
                d.update(orion.get_backfill_state(e))
                docs.append(d)
            return docs

        @task(execution_timeout=timedelta(minutes=60))
        def backfill(doc: dict) -> str:
            p = get_current_context()["params"]
            window = timedelta(days=int(p["window_days"]))
            dry_run = bool(p["dry_run"])
            orion = OrionRegistry()
            urn = doc["urn"]

            if stateless:
                start = datetime.combine(parse_start_date(str(p["start_date"])), datetime.min.time(), tzinfo=UTC)
                end = parse_end_datetime(str(p.get("end_date") or ""))
                fetched = written = 0
                with InfluxWriter(bucket=gate.bucket) as w:
                    for a, b in _windows(start, end, window):
                        samples = gate.fetch(doc, a, b)
                        fetched += len(samples)
                        if samples and not dry_run:
                            _, n, _ = _write(gate, w, orion, samples)
                            written += n
                return f"{urn}: [{start:%Y-%m-%d}..{end:%Y-%m-%d}) fetched {fetched}, wrote {written}{' (dry run)' if dry_run else ''}"

            # cursor walk backwards from the live watermark
            floor = datetime.combine(date.fromisoformat(str(p["floor_iso"])), datetime.min.time(), tzinfo=UTC)
            floor_ms = int(floor.timestamp() * 1000)
            if doc.get("done_at"):
                return f"{urn}: already done"
            if not doc["last_ingested_ms"]:
                return f"{urn}: no live watermark yet, skipping"
            cursor_ms = int(doc.get("cursor") or doc["last_ingested_ms"])
            streak = int(doc.get("empty_streak") or 0)
            fetched = written = walked = 0
            done = None
            with InfluxWriter(bucket=gate.bucket) as w:
                for _ in range(int(p["windows_per_run"])):
                    if cursor_ms <= floor_ms:
                        done = "reached floor"
                        break
                    cursor = datetime.fromtimestamp(cursor_ms / 1000, tz=UTC)
                    a = max(cursor - window, floor)
                    samples = [s for s in gate.fetch(doc, a, cursor) if _ms(s.observed_at) < cursor_ms]
                    walked += 1
                    fetched += len(samples)
                    if samples:
                        streak = 0
                        if not dry_run:
                            _, n, _ = _write(gate, w, orion, samples)
                            if n == 0:
                                log.warning("%s: window %s..%s wrote nothing, stopping", urn, a.date(), cursor.date())
                                break
                            written += n
                    else:
                        streak += 1
                        if streak >= int(p["empty_streak_threshold"]):
                            done = f"{streak} empty windows"
                    cursor_ms = int(a.timestamp() * 1000)
                    if done:
                        break
            if not dry_run:
                orion.patch_backfill_state(urn, cursor_ms=cursor_ms, empty_streak=streak,
                                           done_at_ms=cursor_ms if done else None)
            return (f"{urn}: {walked} window(s), fetched {fetched}, wrote {written}, cursor={_iso(cursor_ms)}"
                    f"{'; done (' + done + ')' if done else ''}{' (dry run)' if dry_run else ''}")

        backfill.expand(doc=load_devices())
    return _backfill()


def build_dags(gate: Gate) -> dict[str, Any]:
    dags = {
        f"{gate.key}_init": build_init_dag(gate),
        f"{gate.key}_run": build_run_dag(gate),
    }
    if gate.backfill_mode != "none":
        dags[f"{gate.key}_backfill"] = build_backfill_dag(gate)
    return dags
