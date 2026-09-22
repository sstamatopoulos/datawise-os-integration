# Working in this repository

Read `ROADMAP.md` first: it holds the state of the project, the decisions that
must not be reopened without reading their reasons, and the ordered list of
what to do next. Pick the first unchecked item of the lowest milestone unless
the user says otherwise, and tick it when it is done end to end.

## Layout

- `plugins/datagates/core/` — ported production code (Orion-LD, InfluxDB, summaries, counter guard) plus the shared machinery every gate uses: `fieldmap`, `http`, `timeparse`, `binary`, `xmlrows`, `cadence`. Change with care; it is shared with a live system and with 24 gate types.
- `plugins/datagates/gates/` — the Gate contract (`base.py`), the YAML loader (`registry.py`), the `PollingGate` and `FileDropGate` bases, and the built-in gates (one module each).
- `plugins/datagates/dags/factory.py` — the three DAG templates every gate gets.
- `dags/datagates_dags.py` — the only file Airflow loads; it builds DAGs from `config/gates.yaml`.
- `config/gates.yaml` — which gates run, **and a working disabled example of every built-in type**. It is the catalogue and a test checks it.
- `scripts/` — `gen-secrets.sh`, `verify_platform.py` (checks a live deployment), `mqtt_spool.py`.
- `docs/` — data model, gate reference, legacy-system playbook, how to add a gate, publishing checklist.
- `tests/` — unit tests, no network. `PYTHONPATH` and the `doc_of` fixture come from `tests/conftest.py`.

## Commands

```bash
pip install -r requirements-dev.txt
pip install -r requirements-gates.txt   # only to work on a gate that needs a driver
ruff check .            # lint, must be clean
pytest                  # must be green
docker compose up -d --build     # the stack; Airflow at :8080, Orion-LD at :1026, InfluxDB at :8086
python scripts/verify_platform.py       # against a running stack
```

The DAG factory needs Airflow to import; locally that means the compose
stack or `pip install apache-airflow==3.0.6` under its constraints file (see
`.github/workflows/ci.yml`).

## Rules

- No secrets in the tree: `.env` is gitignored; gates read `${VAR}` from the environment.
- No pilot-specific names, ids or credentials from the private DATAWiSE repository. Example addresses are RFC 1918, example hosts end in `.example`.
- Every gate change comes with a test on a recorded payload and a docs update.
- A new gate type ships with all of: class + docstring, `BUILTIN_TYPES` entry, a working disabled example in `config/gates.yaml`, a driver line in `requirements-gates.txt` and an extra in `pyproject.toml` if it needs one, new `${VAR}`s in `.env.example` **and** `docker-compose.yml`, a section in `docs/gates.md`, a row in `docs/legacy-systems.md`, tests. `CONTRIBUTING.md` is the checklist.
- Import third-party drivers **inside the method that uses them**, never at module level: a worker without `pymodbus` must still load every other DAG, and a test enforces it.
- Use the shared machinery instead of reimplementing it (`FieldMap` for a `fields:` block, `client_for` for HTTP, `parse_stamp` for timestamps, `PollingGate` for an upstream with no history, `FileDropGate` for files).
- Every `controlledProperty` a gate emits is registered in `core/vocab.py` and `docs/properties.md`, with its unit, kind (instant / delta / register / state) and aggregation rule. Renaming one mints a new summary id and orphans the old series.
- Semantic models (SAREF, the full Smart Data Models) are **export projections**, never contexts attached to stored entities: attaching one expands attribute names in Orion-LD and breaks every consumer's `q=` filter.
- Use `core.entities.as_list` on anything read back from Orion-LD; never index raw values.
- `location` is the GeoProperty; never put text there.
- Existing entities are updated with POST /attrs (append), never PATCH.
- Never change the summary URN namespace or key format: every existing series hangs off it.

## Style

Python 3.12, ruff (E, F, W, I, UP), 120 columns. Docstrings say what the
upstream really does where it differs from its documentation — those findings
are the most valuable part of a gate, so write them down even when the code
that handles them looks obvious. Commit messages explain the why.
