#!/usr/bin/env bash
# integration_test.sh — prove that a running stack actually ingests.
#
#   docker compose up -d --build
#   ./scripts/integration_test.sh
#
# Runs the credential-free weather gate end to end against the compose stack
# and asserts what a consumer would care about: entities registered, points in
# InfluxDB under the summary URN, the summary refreshed, the conventions in
# docs/data-model.md still true, and the proxy enforcing its API key.
#
# This is the same sequence a person runs by hand after `docker compose up`,
# which is exactly why it is a script: the first run of this stack cost seven
# fixes that the unit suite could not see, and nothing keeps them fixed unless
# a machine repeats the run. CI calls it on every push (.github/workflows).
#
# Exit status 0 when every check passed, 1 otherwise. Every wait has a timeout,
# so a stuck stack fails the build instead of hanging it.
set -uo pipefail
export MSYS_NO_PATHCONV=1              # Git Bash would rewrite container paths

cd "$(dirname "$0")/.."
# A *relative* scratch directory, not mktemp's /tmp. On Git Bash, mktemp hands
# back an MSYS path (/tmp/tmp.XXXX) that the shell understands and the native
# Windows python.exe and curl.exe do not: the file is written and then cannot
# be opened, so every assertion reading it reports a healthy platform as
# broken. A path relative to the repository root resolves the same for both.
WORK="$(mktemp -d ./.integration.XXXXXX)"; trap 'rm -rf "$WORK"' EXIT
GATE="${GATE:-weather_forecast}"
COMPOSE="docker compose"
FAILURES=0

step()  { printf '\n== %s %s\n' "$1" "$(printf '%.0s-' $(seq 1 $((60 - ${#1}))))"; }
ok()    { printf '  [  ok  ] %s\n' "$1"; }
fail()  { printf '  [ FAIL ] %s\n' "$1"; FAILURES=$((FAILURES + 1)); }

# wait_for <seconds> <description> <command...>
wait_for() {
  local deadline=$(( SECONDS + $1 )); local what="$2"; shift 2
  while (( SECONDS < deadline )); do
    if "$@" >/dev/null 2>&1; then ok "$what"; return 0; fi
    sleep 3
  done
  fail "$what (gave up after $1s)"
  return 1
}

airflow_cli() { $COMPOSE exec -T airflow-scheduler airflow "$@" 2>/dev/null; }

dag_state() {
  airflow_cli dags list-runs "$1" -o json 2>/dev/null \
    | python3 -c "import json,sys; d=json.load(sys.stdin); print(d[0]['state'] if d else 'none')" 2>/dev/null
}

# run_dag <dag_id> [json conf]
run_dag() {
  local dag="$1"; local conf="${2:-}"
  airflow_cli dags unpause "$dag" >/dev/null
  if [ -n "$conf" ]; then
    airflow_cli dags trigger "$dag" --conf "$conf" >/dev/null
  else
    airflow_cli dags trigger "$dag" >/dev/null
  fi
  local deadline=$(( SECONDS + 420 ))
  while (( SECONDS < deadline )); do
    case "$(dag_state "$dag")" in
      success) ok "$dag succeeded"; return 0 ;;
      failed)  fail "$dag failed — see 'docker compose logs airflow-scheduler' and logs/"; return 1 ;;
    esac
    sleep 5
  done
  fail "$dag did not finish within 420s"
  return 1
}

# ── the stores answer ────────────────────────────────────────────────
step "services"
wait_for 180 "Orion-LD answers /version" \
  $COMPOSE exec -T orion-ld curl -fsS http://localhost:1026/version
wait_for 180 "InfluxDB is healthy" \
  $COMPOSE exec -T influxdb curl -fsS http://localhost:8086/health

# Airflow 3 runs tasks through the API server: the supervisor calls its
# /execution endpoint, so a DAG triggered before that server is accepting
# connections fails with "httpx.ConnectError: [Errno 111] Connection refused"
# and the scheduler then reports the DAG missing from serialized_dag, which
# reads like a parsing problem and is not one. Waiting on compose's own health
# status is the fix; on a machine where the stack has been up for a while the
# race never appears, which is why this only failed in CI.
compose_healthy() {
  local state
  state="$($COMPOSE ps "$1" --format '{{.Health}}' 2>/dev/null | tail -1)"
  [ "$state" = "healthy" ] && return 0
  # A service with no healthcheck reports nothing; running is all we can ask.
  [ -z "$state" ] && [ "$($COMPOSE ps "$1" --format '{{.State}}' 2>/dev/null | tail -1)" = "running" ]
}
wait_for 300 "the Airflow API server is healthy" compose_healthy airflow-apiserver
wait_for 300 "the Airflow scheduler is healthy" compose_healthy airflow-scheduler

# ── Airflow has parsed the DAG bag ───────────────────────────────────
step "dag bag"
bag_ready() { [ "$(airflow_cli dags list -o json | python3 -c \
  "import json,sys; print(len([d for d in json.load(sys.stdin) if d['dag_id'].startswith(('weather_','prices_'))]))" 2>/dev/null)" -ge 8 ]; }
wait_for 420 "the eight DAGs of the three enabled gates are registered" bag_ready
# Listed is not the same as serialized: the scheduler hands a task the
# serialized DAG, and `dags details` is the cheapest thing that fails until it
# exists.
serialized() { airflow_cli dags details "$1" >/dev/null 2>&1; }
wait_for 300 "${GATE}_init is serialized and ready to run" serialized "${GATE}_init"
if ! airflow_cli dags list-import-errors | grep -q "No data found"; then
  fail "the DAG folder has import errors"
  airflow_cli dags list-import-errors | head -20
else
  ok "no DAG import errors"
fi

# ── one full cycle ───────────────────────────────────────────────────
step "ingest"
run_dag "${GATE}_init"
run_dag "${GATE}_run"

# ── what a consumer would check ──────────────────────────────────────
step "the data model"
$COMPOSE exec -T orion-ld curl -sS -H 'Accept: application/json' \
  "http://localhost:1026/ngsi-ld/v1/entities?type=DeviceMeasurement&q=entityKind==%22summary%22;controlledProperty==%22temperature%22" \
  > "$WORK/summary.json" 2>/dev/null

# The document goes in a file, not down stdin: `python3 - <<PY` already uses
# stdin for the program itself, so a piped document arrives as nothing at all
# and this assertion reports a perfectly healthy platform as broken. It did.
MEASUREMENT="$(SUMMARY_FILE="$WORK/summary.json" python3 - <<'PY'
import json, os
try:
    entities = json.load(open(os.environ["SUMMARY_FILE"], encoding="utf-8"))
except (ValueError, OSError):
    entities = []
if not entities:
    print(""); raise SystemExit
entity = entities[0]
value = lambda name: (entity.get(name) or {}).get("value")
# The convention consumers depend on: the summary's id IS the InfluxDB
# measurement name, so nothing needs a lookup table (docs/data-model.md).
print(entity["id"] if value("influxMeasurement") == entity["id"] and value("lastReadingAt") else "")
PY
)"

if [ -n "$MEASUREMENT" ]; then
  ok "the temperature summary is refreshed and points at its own measurement"
else
  fail "no refreshed temperature summary: lastReadingAt unset, or influxMeasurement != id"
  head -c 600 "$WORK/summary.json"
fi

step "the series"
if [ -n "$MEASUREMENT" ]; then
  TOKEN="$(grep '^INFLUX_TOKEN=' .env | cut -d= -f2)"
  # stop: is not optional — a forecast is in the future, and a past-only range
  # counts two of the forty-eight points and calls that healthy.
  # Read the count by column name. Annotated CSV puts _value wherever it likes
  # (here between _stop and _field), the data row starts with two empty fields
  # because #default supplies the result name, and every line ends CRLF -- so
  # "the last comma-separated field" is the measurement name, not the count.
  COUNT="$($COMPOSE exec -T -e T="$TOKEN" -e M="$MEASUREMENT" influxdb sh -c \
    'influx query --host http://localhost:8086 --token "$T" --org "${DOCKER_INFLUXDB_INIT_ORG:-datagates}" --raw \
      "from(bucket:\"telemetry\") |> range(start:-7d, stop:7d) |> filter(fn:(r)=> r._measurement == \"$M\" and r._field == \"value\") |> count()"' \
    2>/dev/null | python3 -c '
import csv, sys
rows = [r for r in csv.reader(sys.stdin) if r and not r[0].lstrip().startswith("#")]
header = next((r for r in rows if "_value" in r), None)
data = rows[-1] if rows and rows[-1] is not header else None
column = header.index("_value") if header else -1
print(data[column].strip() if data and 0 <= column < len(data) else 0)
')"
  if [ "${COUNT:-0}" -ge 24 ] 2>/dev/null; then
    ok "InfluxDB holds $COUNT points under the summary URN"
  else
    fail "expected at least 24 points under $MEASUREMENT, counted ${COUNT:-0}"
  fi
fi

# ── the platform's own verification ──────────────────────────────────
step "verify_platform.py"
if $COMPOSE exec -T airflow-scheduler python /opt/airflow/scripts/verify_platform.py --gate "$GATE"; then
  ok "verify_platform reports no failures"
else
  fail "verify_platform reported a failure"
fi

# ── the proxy, from outside the compose network ──────────────────────
step "proxy"
HTTPS_PORT="$(grep '^HTTPS_PORT=' .env 2>/dev/null | cut -d= -f2)"; HTTPS_PORT="${HTTPS_PORT:-443}"
KEY="$(grep '^ORION_API_KEY=' .env | cut -d= -f2)"
# -o a real file rather than /dev/null: curl on Windows cannot write there and
# exits 23, which turns a passing probe into a confusing failure.
probe() { curl -k -sS --max-time 20 -o "$WORK/probe.out" -w '%{http_code}' \
          --resolve "orion.localhost:${HTTPS_PORT}:127.0.0.1" "$@"; }
URL="https://orion.localhost:${HTTPS_PORT}/ngsi-ld/v1/entities?type=DeviceMeasurement&limit=1"

if [ "$(probe "$URL")" = "401" ]; then
  ok "the broker refuses a request without X-API-Key"
else
  fail "the broker answered without an API key — the proxy is not enforcing it"
fi
if [ "$(probe -H "X-API-Key: ${KEY}" "$URL")" = "200" ]; then
  ok "the broker answers with a valid X-API-Key"
else
  fail "the broker rejected a valid X-API-Key"
fi

# ── verdict ──────────────────────────────────────────────────────────
printf '\n'
if [ "$FAILURES" -eq 0 ]; then
  echo "integration test passed"
else
  echo "integration test FAILED: $FAILURES check(s)"
fi
exit $(( FAILURES > 0 ))
