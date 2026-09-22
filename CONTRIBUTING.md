# Contributing

Thanks for helping. The bar for a change is simple: it works against the
compose stack, `ruff check .` and `pytest` pass, and CI still builds every
configured gate's DAGs under Airflow.

```bash
pip install -r requirements-dev.txt
ruff check . && pytest
```

## New gate types

A new gate type is the most useful contribution there is, and the most useful
gate types are the unglamorous ones: the protocol or export format that a
whole class of buildings is stuck with. Follow
[docs/adding-a-gate.md](docs/adding-a-gate.md) and use the shared machinery
(`core.fieldmap`, `core.http`, `core.timeparse`, `PollingGate`,
`FileDropGate`) rather than reimplementing it — a built-in gate is usually
80 to 150 lines because of them.

A built-in gate ships with:

- the class in `plugins/datagates/gates/<type>.py`, with a docstring that
  shows its YAML, names the optional dependency if it has one, and **says
  what the upstream really does where it differs from its documentation**;
- any third-party driver imported *inside* the method that uses it, never at
  module level, so a worker without that package still loads every other DAG;
- an entry in `BUILTIN_TYPES` (`gates/registry.py`), a line in
  `requirements-gates.txt` and an extra in `pyproject.toml` if it needs a
  driver, and any new `${VAR}` in `.env.example` and `docker-compose.yml`;
- a **working, disabled example** in `config/gates.yaml` — the catalogue is
  tested, so the example must construct and discover devices offline;
- a section in `docs/gates.md`, and a row in
  `docs/legacy-systems.md` if it maps to identifiable products;
- tests in the matching `tests/test_gates_*.py` that exercise `discover()`
  and `fetch()` on a recorded payload, with the transport mocked at one
  method. No test may touch the network.

The docstring findings are the part that cannot be reconstructed later. "The
archive serves no visibility", "an empty answer is a 400, not a 204", "this
meter reverses its words", "the history query silently caps at 1000 rows" —
write those down even when the code that handles them looks obvious.

## Changes to `core/` and the DAG factory

`plugins/datagates/core/` is shared with a live system and every gate; the
conventions in [docs/data-model.md](docs/data-model.md) and the decision log
in `ROADMAP.md` are load-bearing. In particular: the summary URN namespace and
key format must never change (every existing series hangs off them), existing
entities are updated with POST /attrs and never PATCH, `location` holds
coordinates only, and anything read back from Orion goes through
`core.entities.as_list` first. If you think one of those decisions is wrong,
read the reason recorded next to it in `ROADMAP.md` before opening the pull
request, and address that reason.

## Never commit

Credentials, tokens, private keys, partner exports, a `.env`, or anything
naming a real site, building, meter serial or person. Secrets reach gates only
through `${VAR}` references resolved from the environment. Recorded payloads
in tests must be anonymised: invent the device ids and the addresses.

## Reporting a problem

Open an issue with the gate type, the relevant `gates.yaml` entry with secrets
removed, and the task log of the failing DAG run. For anything that looks like
a vulnerability, follow [SECURITY.md](SECURITY.md) instead of opening an issue.

## Commit messages and pull requests

Commit messages explain the **why**; the diff shows the what. One logical
change per pull request. If you fixed something the upstream does not document,
put that sentence in the gate's docstring as well as in the commit message —
the commit gets buried, the docstring gets read.
