# plugins/datagates/core/orion_registry.py
from __future__ import annotations

import logging
from typing import Any

import requests
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from datagates.core.settings import DEFAULT_CONTEXT, ORION_API_KEY, ORION_URL, REQUEST_TIMEOUT


def _as_utc(iso):
    """ISO 8601 (any offset, 'Z', or naive) -> aware UTC datetime, else None."""
    from datetime import UTC, datetime

    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _raise_with_body(resp, what: str) -> None:
    """raise_for_status(), but with Orion's error body in the message.

    Orion-LD says exactly what it disliked — "location must be a
    GeoProperty", "Invalid value for URI parameter /limit/" — in a JSON
    body. requests' own HTTPError drops it, which turned a one-line fix
    into a guessing game in the Airflow log. `response` is attached so
    the tenacity predicate still sees the status code.
    """
    if resp.ok:
        return
    detail = (resp.text or "").strip().replace("\n", " ")[:400]
    raise requests.HTTPError(
        f"{resp.status_code} {resp.reason} on {what}: {detail}", response=resp)


def _is_transient(exc: BaseException) -> bool:
    """Retry on connection errors and 5xx responses."""
    if isinstance(exc, requests.exceptions.ConnectionError):
        return True
    if isinstance(exc, requests.exceptions.Timeout):
        return True
    if isinstance(exc, requests.exceptions.HTTPError):
        return exc.response is not None and exc.response.status_code >= 500
    return False

log = logging.getLogger(__name__)

_HEADERS = {"Content-Type": "application/ld+json"}
if ORION_API_KEY:
    _HEADERS["X-API-Key"] = ORION_API_KEY


class OrionRegistry:
    """
    Thin wrapper around the Orion Context Broker NGSIv2 API for
    upserting and querying Building and Device entities.

    All entities are stored in NGSI-LD normalized format
    (attribute dicts with "type"/"value" keys).
    """

    def __init__(self, base_url: str = ORION_URL):
        self.base_url = base_url.rstrip("/")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    @retry(
        retry=retry_if_exception(_is_transient),
        wait=wait_exponential(multiplier=2, min=5, max=120),
        stop=stop_after_attempt(8),
        reraise=True,
    )
    def upsert_entity(self, entity: dict[str, Any]) -> str:
        """
        Create entity if it doesn't exist, otherwise update its attributes.
        Returns 'created' | 'updated'.
        """
        entity_id = entity["id"]

        resp = requests.post(
            self._url("/ngsi-ld/v1/entities"),
            headers=_HEADERS,
            json=entity,
            timeout=REQUEST_TIMEOUT,
        )

        if resp.status_code == 201:
            log.debug("Orion: created entity %s", entity_id)
            return "created"

        if resp.status_code == 409:
            # Entity already exists — append/overwrite every attribute.
            # POST /attrs ("Append Entity Attributes") both creates
            # attributes the entity lacks and overwrites the ones it has.
            # PATCH /attrs ("Update Entity Attributes") only touches
            # attributes that already exist and silently reports the rest
            # as notUpdated — which is how re-registering 100 IEQ sensors
            # in 2026-09 renamed nothing and added nothing on them.
            attrs = {
                k: v
                for k, v in entity.items()
                if k not in ("id", "type")
            }
            if "@context" not in attrs:
                attrs["@context"] = DEFAULT_CONTEXT
            post = requests.post(
                self._url(f"/ngsi-ld/v1/entities/{entity_id}/attrs"),
                headers=_HEADERS,
                json=attrs,
                timeout=REQUEST_TIMEOUT,
            )
            _raise_with_body(post, f"POST /entities/{entity_id}/attrs")
            log.debug("Orion: updated entity %s", entity_id)
            return "updated"

        _raise_with_body(resp, f"POST /entities ({entity_id})")
        return "error"

    # ------------------------------------------------------------------
    # Buildings
    # ------------------------------------------------------------------

    def upsert_building(self, entity: dict[str, Any]) -> str:
        return self.upsert_entity(entity)

    def get_building(self, ref_building: str) -> dict[str, Any] | None:
        resp = requests.get(
            self._url(f"/ngsi-ld/v1/entities/{ref_building}"),
            headers=_HEADERS,
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Devices / Meters
    # ------------------------------------------------------------------

    def upsert_device(self, entity: dict[str, Any]) -> str:
        return self.upsert_entity(entity)

    def get_device(self, ref_device: str) -> dict[str, Any] | None:
        resp = requests.get(
            self._url(f"/ngsi-ld/v1/entities/{ref_device}"),
            headers=_HEADERS,
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    def get_last_reading_values(
        self, ref_device: str, props: list[str],
    ) -> dict[str, float]:
        """Newest stored value per controlledProperty, from the summaries.

        Used by the counter guard: a batch whose very first sample is the
        bad one has nothing within itself to judge that sample against, so
        the guard needs the last value already on record. Missing or
        non-numeric summaries are simply left out of the result, which the
        guard treats as "no floor known".
        """
        from datagates.core.measurement_summary import (
            summary_measurement_urn,
        )

        out: dict[str, float] = {}
        for prop in props:
            try:
                resp = requests.get(
                    self._url("/ngsi-ld/v1/entities/"
                              + summary_measurement_urn(ref_device, prop)),
                    headers=_HEADERS,
                    params={"attrs": "lastReadingValue"},
                    timeout=REQUEST_TIMEOUT,
                )
                if resp.status_code != 200:
                    continue
                val = (resp.json().get("lastReadingValue") or {}).get("value")
                if val is not None:
                    out[prop] = float(val)
            except (requests.RequestException, ValueError, TypeError):
                continue
        return out

    def get_all_devices(self, limit: int = 1000, q: str | None = None) -> list[dict[str, Any]]:
        """
        Return Device entities (up to limit).
        Pass q to filter server-side, e.g. q='dataGate=="weather"'
        """
        params: dict = {"type": "Device", "limit": limit}
        if q:
            params["q"] = q
        resp = requests.get(
            self._url("/ngsi-ld/v1/entities"),
            headers=_HEADERS,
            params=params,
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()

    def batch_upsert_entities(self, entities: list[dict[str, Any]]) -> dict:
        """
        Batch upsert via POST /ngsi-ld/v1/entityOperations/upsert.
        Returns {"success": n, "errors": [...]} summary.
        Much faster than individual upserts for large payloads.
        """
        resp = requests.post(
            self._url("/ngsi-ld/v1/entityOperations/upsert"),
            headers=_HEADERS,
            json=entities,
            timeout=REQUEST_TIMEOUT,
            params={"options": "update"},
        )
        if resp.status_code in (201, 204):
            return {"success": len(entities), "errors": []}
        if resp.status_code == 207:
            body = resp.json()
            errors = body.get("errors", [])
            success = len(entities) - len(errors)
            if errors:
                log.warning("Batch upsert partial errors: %s", errors)
            return {"success": success, "errors": errors}
        resp.raise_for_status()
        return {"success": 0, "errors": []}

    def get_weather_locations(
        self, entity_type: str = "WeatherForecastLocation",
    ) -> list[dict[str, Any]]:
        """Return every entity of one weather-location type
        (WeatherForecastLocation or WeatherObservedLocation)."""
        params = {"type": entity_type, "limit": 1000}
        resp = requests.get(
            self._url("/ngsi-ld/v1/entities"),
            headers=_HEADERS,
            params=params,
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()

    def get_devices_for_ingestion(self) -> list[dict[str, Any]]:
        """
        Return flattened dicts for devices that have a non-empty tbDeviceId,
        shaped to match what aee_orion_run expects.
        lastIngestedAt (ms epoch) is stored on the Device entity itself —
        no MongoDB watermarks collection required.
        """
        entities = self.get_all_devices(q="tbDeviceId")
        result = []
        for e in entities:
            tb_id = e.get("tbDeviceId", {}).get("value", "")
            if not tb_id:
                continue
            result.append({
                "_id":               e["id"],
                "tb_device_id":      tb_id,
                "energy_key":        e.get("energyKey",       {}).get("value", ""),
                "energy_unit_code":  e.get("energyUnitCode",  {}).get("value", "KWH"),
                "power_key":         e.get("powerKey",        {}).get("value", ""),
                "power_unit_code":   e.get("powerUnitCode",   {}).get("value", "KWT"),
                "power_scale_to_kw": e.get("powerScaleToKw",  {}).get("value", 1.0),
                # Watermark: 0 means never ingested (first run will fetch full history)
                "last_ingested_at":  int(e.get("lastIngestedAt", {}).get("value", 0) or 0),
            })
        return result

    def get_devices_for_backfill(self) -> list[dict[str, Any]]:
        """
        Return flattened dicts for devices with tbDeviceId, including both
        the live `lastIngestedAt` and the backfill cursor state.
        Used by `aee_orion_backfill`.
        """
        entities = self.get_all_devices(q="tbDeviceId")
        result = []
        for e in entities:
            tb_id = e.get("tbDeviceId", {}).get("value", "")
            if not tb_id:
                continue
            bf = self.get_backfill_state(e)
            result.append({
                "_id":               e["id"],
                "tb_device_id":      tb_id,
                "energy_key":        e.get("energyKey",       {}).get("value", ""),
                "energy_unit_code":  e.get("energyUnitCode",  {}).get("value", "KWH"),
                "power_key":         e.get("powerKey",        {}).get("value", ""),
                "power_unit_code":   e.get("powerUnitCode",   {}).get("value", "KWT"),
                "power_scale_to_kw": e.get("powerScaleToKw",  {}).get("value", 1.0),
                "last_ingested_at":  int(e.get("lastIngestedAt", {}).get("value", 0) or 0),
                "backfill_cursor":   bf["cursor"],
                "backfill_empty_streak": bf["empty_streak"],
                "backfill_done_at":  bf["done_at"],
            })
        return result

    def patch_attrs(self, entity_id: str, attrs: dict[str, Any]) -> None:
        """
        Generic "Append Attributes" via POST /entities/<id>/attrs.

        POST (not PATCH) is used because POST both CREATES the attribute
        on first write and UPDATES it on subsequent writes. PATCH would
        silently no-op the first time (returns 207 Multi-Status). The
        `attrs` dict is the plain attribute map — @context is added
        automatically if the caller hasn't supplied one.

        Used by the run DAGs to refresh the summary DeviceMeasurement
        entities' lastReadingAt / lastReadingValue / rolling24h*
        attributes in place.
        """
        body = dict(attrs)
        body.setdefault("@context", DEFAULT_CONTEXT)
        resp = requests.post(
            self._url(f"/ngsi-ld/v1/entities/{entity_id}/attrs"),
            headers=_HEADERS,
            json=body,
            timeout=REQUEST_TIMEOUT,
        )
        _raise_with_body(resp, f"POST /entities/{entity_id}/attrs")

    def patch_summary_if_newer(
        self, summary_urn: str, last_reading_at: str, attrs: dict[str, Any],
    ) -> bool:
        """patch_attrs(), unless the summary already holds a newer reading.

        Summary refreshes race whenever one run processes several batches
        in parallel — a backlog of SFTP drops, say. Each batch PATCHes the
        summary with ITS newest reading, and whichever task finishes last
        wins, even when it carried the oldest data. One batch-fed gas meter's snapshot sat
        three months behind its own InfluxDB series for exactly this
        reason. Comparing against what is stored makes the refresh
        order-independent.

        Returns True when the PATCH was sent, False when it was skipped
        because the stored lastReadingAt is already at or past
        `last_reading_at`. A summary with no lastReadingAt, or one that
        cannot be read, is always patched.
        """
        current = None
        try:
            resp = requests.get(
                self._url(f"/ngsi-ld/v1/entities/{summary_urn}"),
                headers=_HEADERS,
                params={"attrs": "lastReadingAt"},
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code == 200:
                current = (resp.json().get("lastReadingAt") or {}).get("value")
        except (requests.RequestException, ValueError):
            current = None

        have, want = _as_utc(current), _as_utc(last_reading_at)
        if have is not None and want is not None and have >= want:
            return False
        self.patch_attrs(summary_urn, attrs)
        return True

    def patch_last_ingested(self, ref_device: str, ts_ms: int) -> None:
        """
        Persist the watermark for a device directly on its Orion entity.
        Uses POST /attrs (NGSI-LD "Append Attributes") which both creates the
        attribute on first write and updates it on subsequent writes. PATCH
        would only update an existing attribute and silently no-op (207) the
        first time, which is why we use POST here.
        """
        resp = requests.post(
            self._url(f"/ngsi-ld/v1/entities/{ref_device}/attrs"),
            headers=_HEADERS,
            json={
                "@context": DEFAULT_CONTEXT,
                "lastIngestedAt": {"type": "Property", "value": ts_ms},
            },
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        log.debug("Orion: updated lastIngestedAt for %s → %d", ref_device, ts_ms)

    # ── Backfill cursor state ─────────────────────────────────────────
    # The backfill DAG walks the historical window backwards, one slice
    # at a time, before the live `lastIngestedAt`. State is stored on
    # the same Device entity to avoid an extra collection.

    def get_backfill_state(self, entity: dict[str, Any]) -> dict[str, int | None]:
        """
        Extract backfill state from an already-fetched Device entity.
        Returns {"cursor": ms|None, "empty_streak": int, "done_at": ms|None}.
        """
        cursor = int(entity.get("backfillCursorAt", {}).get("value", 0) or 0)
        empty_streak = int(entity.get("backfillEmptyStreak", {}).get("value", 0) or 0)
        done_at = int(entity.get("backfillDoneAt", {}).get("value", 0) or 0)
        return {
            "cursor": cursor or None,
            "empty_streak": empty_streak,
            "done_at": done_at or None,
        }

    # NOTE: query_measurements_temporal() was removed with chunk C6.
    # Time-series history no longer lives in Orion — it's in InfluxDB
    # under one _measurement per (device, controlledProperty). Consumers
    # that need raw history call InfluxWriter.query_range() instead.

    def patch_backfill_state(
        self,
        ref_device: str,
        cursor_ms: int | None = None,
        empty_streak: int | None = None,
        done_at_ms: int | None = None,
    ) -> None:
        """Append or update one or more backfill-state properties on a Device.
        Uses POST /attrs so that first-time writes create the attribute rather
        than silently no-op'ing the way PATCH does for missing attributes."""
        body: dict[str, Any] = {"@context": DEFAULT_CONTEXT}
        if cursor_ms is not None:
            body["backfillCursorAt"] = {"type": "Property", "value": cursor_ms}
        if empty_streak is not None:
            body["backfillEmptyStreak"] = {"type": "Property", "value": empty_streak}
        if done_at_ms is not None:
            body["backfillDoneAt"] = {"type": "Property", "value": done_at_ms}
        if len(body) == 1:
            return  # only @context — nothing to write
        resp = requests.post(
            self._url(f"/ngsi-ld/v1/entities/{ref_device}/attrs"),
            headers=_HEADERS,
            json=body,
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
