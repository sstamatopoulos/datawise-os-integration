#!/usr/bin/env bash
# Write a .env with random secrets for every service. Refuses to overwrite.
#
#   ./scripts/gen-secrets.sh                 # PUBLIC_HOST=localhost
#   PUBLIC_HOST=data.example.org ACME_EMAIL=ops@example.org ./scripts/gen-secrets.sh
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ -e .env ]]; then
  echo ".env exists; delete it first if you really want new secrets (they would orphan the volumes)." >&2
  exit 1
fi

rand() { openssl rand -base64 48 | tr -d '/+=\n' | cut -c1-"${1:-40}"; }
# A Fernet key is urlsafe-base64 of 32 random bytes, which openssl and tr
# produce exactly (44 characters, decoding to 32 bytes). This used to call
# python with the cryptography package and fall back to `docker run` on the
# Airflow image, so a machine without cryptography waited on a 3 GB pull to
# write one line of .env -- and a CI runner that has cryptography in a
# system-managed environment could not install it at all.
fernet() { openssl rand -base64 32 | tr '+/' '-_'; }

# On Linux the container must run as the invoking user or the bind-mounted
# logs/ and drop/ directories end up root-owned. On Docker Desktop (Windows,
# macOS) the mount translation layer handles ownership and `id -u` returns a
# synthetic id -- 1058386 on a Git Bash -- which is meaningless inside the
# image, so fall back to Airflow's own default.
uid=$(id -u 2>/dev/null || echo 50000)
if [[ ! "$uid" =~ ^[0-9]+$ ]] || (( uid > 60000 )); then uid=50000; fi
cat > .env <<EOF
# Generated $(date -u +%Y-%m-%dT%H:%M:%SZ) by scripts/gen-secrets.sh — keep private, never commit.

PUBLIC_HOST=${PUBLIC_HOST:-localhost}
ACME_EMAIL=${ACME_EMAIL:-}

AIRFLOW_UID=${uid}
AIRFLOW_ADMIN_USER=admin
AIRFLOW_ADMIN_PASSWORD=$(rand 24)
AIRFLOW_FERNET_KEY=$(fernet)
AIRFLOW_JWT_SECRET=$(rand 48)

POSTGRES_PASSWORD=$(rand 32)

MONGO_USER=orion
MONGO_PASSWORD=$(rand 32)

# Required in the X-API-Key header on every request to https://orion.\${PUBLIC_HOST}
ORION_API_KEY=$(rand 40)

INFLUX_ORG=datagates
INFLUX_DEFAULT_BUCKET=telemetry
INFLUX_ADMIN_USER=admin
INFLUX_ADMIN_PASSWORD=$(rand 24)
INFLUX_TOKEN=$(rand 64)

# Gate secrets referenced as \${VAR} from config/gates.yaml. Only the ones your
# enabled gates use need a value; see .env.example for what each is for.
SNMP_COMMUNITY=
OPCUA_USER=
OPCUA_PASSWORD=
OBIX_USER=
OBIX_PASSWORD=
ZABBIX_TOKEN=
TB_USERNAME=
TB_PASSWORD=
INDOOR_AIR_API_KEY=
WS_USER=
WS_PASSWORD=
PORTAL_USER=
PORTAL_PASSWORD=
DH_USER=
DH_PASSWORD=
MQTT_USER=
MQTT_PASSWORD=
DB_USER=
DB_PASSWORD=
LEGACY_INFLUX_USER=
LEGACY_INFLUX_PASSWORD=
LEGACY_INFLUX_TOKEN=
CITY_BROKER_KEY=
ENTSOE_TOKEN=
EOF
chmod 600 .env
echo "wrote .env (mode 600). Airflow login: admin / $(grep ^AIRFLOW_ADMIN_PASSWORD .env | cut -d= -f2)"
