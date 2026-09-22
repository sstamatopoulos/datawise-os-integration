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
fernet() { python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" 2>/dev/null \
           || docker run --rm apache/airflow:3.0.6 python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"; }

uid=$(id -u 2>/dev/null || echo 50000)
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

# Gate secrets referenced as \${VAR} from config/gates.yaml
TB_USERNAME=
TB_PASSWORD=
MESH_API_KEY=
EOF
chmod 600 .env
echo "wrote .env (mode 600). Airflow login: admin / $(grep ^AIRFLOW_ADMIN_PASSWORD .env | cut -d= -f2)"
