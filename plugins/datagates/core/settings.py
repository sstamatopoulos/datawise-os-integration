"""
datagates.core.settings — everything comes from the environment.

Nothing in this file is site-specific and nothing secret is defaulted.
docker-compose.yml forwards these from the host .env into the Airflow
containers; see .env.example for the full list.
"""
from __future__ import annotations

import os

# ── FIWARE Orion-LD context broker ─────────────────────────────────────
ORION_URL = os.environ.get("ORION_URL", "http://orion-ld:1026").rstrip("/")
REQUEST_TIMEOUT = int(os.environ.get("ORION_TIMEOUT", "60"))
# Sent as X-API-Key on every broker request. Empty inside the compose network,
# where the broker is reachable only by the platform; set it when ORION_URL
# points at the reverse proxy (see SECURITY.md).
ORION_API_KEY = os.environ.get("ORION_API_KEY", "")

# NGSI-LD @context attached to every entity written.
#
# Only the core context, deliberately. Adding the FIWARE Smart Data Model
# JSON-LD contexts makes Orion-LD store every term those contexts define in
# its expanded form ("https://smartdatamodels.org/source"), while custom
# terms stay short. `?type=Device` and `?q=source=="x"` then stop matching
# unless every consumer sends a matching Link header on every request.
# The platform still follows the Smart Data Models where it matters —
# entity types, the "attribute named after controlledProperty" pattern,
# normalised form, UN/CEFACT unit codes — without the term expansion.
DEFAULT_CONTEXT = ["https://uri.etsi.org/ngsi-ld/v1/ngsi-ld-core-context.jsonld"]

# ── InfluxDB v2 ─────────────────────────────────────────────────────────
INFLUX_URL = os.environ.get("INFLUX_URL", "http://influxdb:8086").rstrip("/")
INFLUX_ORG = os.environ.get("INFLUX_ORG", "datagates")
INFLUX_TOKEN = os.environ.get("INFLUX_TOKEN", "")
INFLUX_DEFAULT_BUCKET = os.environ.get("INFLUX_DEFAULT_BUCKET", "telemetry")
INFLUX_RETENTION_HOURS = int(os.environ.get("INFLUX_RETENTION_HOURS", "43800"))   # 5 years
INFLUX_BATCH_SIZE = int(os.environ.get("INFLUX_BATCH_SIZE", "1000"))

# ── Gates ───────────────────────────────────────────────────────────────
# The YAML that declares which gates run, with what options and schedule.
GATES_CONFIG = os.environ.get("DATAGATES_CONFIG", "/opt/airflow/config/gates.yaml")
