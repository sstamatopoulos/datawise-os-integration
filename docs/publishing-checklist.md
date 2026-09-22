# Publishing checklist

What to do before this repository is public, and in what order. Written to be
worked through top to bottom by whoever presses the button; tick the items in
`ROADMAP.md` as you go.

## 1. Decide the identity

- [ ] The account or organisation that will own it, and the final repository
      name. Everything below depends on it.
- [ ] Replace the placeholder URL `github.com/datawise-project/datawise-os-integration`
      in `pyproject.toml` (`[project.urls]`), `CITATION.cff`
      (`repository-code`) and `.github/ISSUE_TEMPLATE/config.yml`.
- [ ] Confirm the copyright line and the licence with the consortium.
      Apache-2.0 is the usual choice for Horizon Europe software outputs; the
      decision and its reason are in `ROADMAP.md` §3.
- [ ] `CITATION.cff`: list the individual authors with ORCIDs, and the funding
      programme and grant number the project reports under.
- [ ] Name the maintainers and the security contact in `SECURITY.md`.

## 2. Prove it works

Do not publish a stack nobody has started. §2 of `ROADMAP.md` lists what is
still unverified; these are the same items.

- [ ] `ruff check .` clean and `pytest` green on 3.12 and 3.13.
- [ ] `docker compose up -d --build` on a clean machine. Record every fix in
      `ROADMAP.md` — the next person hits the same ones.
- [ ] One full cycle: `weather_forecast_init` → `weather_forecast_run` →
      summaries in Orion carry `lastReadingAt` → the series is in InfluxDB
      under the summary URN → `weather_observed_backfill` loads 2024 onward.
- [ ] `python scripts/verify_platform.py` with nothing failing, and its output
      pasted into `README.md` as verified output.
- [ ] At least one credentialed gate against a real upstream, ideally a legacy
      one, so the field-protocol path is not published purely on unit tests.

## 3. Check what you are about to publish

The private repository this came from once had a private key in its history,
which is why this one starts from a fresh history. Keep it that way.

```bash
git ls-files | xargs grep -lInE '(BEGIN [A-Z ]*PRIVATE KEY|-----BEGIN)'          # keys
git ls-files | xargs grep -nIE '(password|passwd|secret|token|api[_-]?key)[[:space:]]*[:=][[:space:]]*["'"'"'][^"'"'"'$#{]' \
  | grep -vE '\.(md|example)$'                                                   # literal secrets
git ls-files | xargs grep -nIE '\b([0-9]{1,3}\.){3}[0-9]{1,3}\b' | grep -vE '10\.|192\.168\.|127\.0\.0\.1'   # real addresses
git ls-files | xargs grep -niE '(\.lv|\.gr|\.es)\b' | grep -vE 'docs/|README'     # real domains
git status --porcelain                                                            # nothing untracked that should be
git check-ignore -v .env logs drop                                                # all ignored
```

- [ ] No credentials, tokens or keys.
- [ ] No pilot-specific names, building ids, meter serials or partner
      hostnames — in code, configuration, tests or documentation. Example
      addresses are RFC 1918; example hosts end in `.example` or
      `.example.org`.
- [ ] `.env` is absent and ignored; `logs/` and `drop/` are ignored.
- [ ] The git history is the one you intend to publish (this repository starts
      fresh; if you rewrote anything, check with `git log --stat`).

## 4. Repository settings

- [ ] Description: one sentence, and the topics `fiware`, `ngsi-ld`,
      `orion-ld`, `influxdb`, `apache-airflow`, `modbus`, `bacnet`, `opc-ua`,
      `smart-buildings`, `energy`, `iot`, `data-integration`.
- [ ] Default branch `main`; branch protection with the CI jobs
      (`lint-and-test`, `packaging`, `gate-extras`, `dags-load`) required.
- [ ] Issues on, with the templates; Discussions on if someone will read them.
- [ ] Actions: workflow permissions read-only, and no secrets configured (CI
      needs none).
- [ ] Dependabot alerts and security updates on; the update schedule is
      already in `.github/dependabot.yml`.
- [ ] Private vulnerability reporting on, so `SECURITY.md` has a channel.

## 5. Release

- [ ] `CHANGELOG.md` finished for the version, with the unverified items named.
- [ ] Same version in `pyproject.toml` and `CITATION.cff`.
- [ ] Tag `v0.2.0`, push, confirm every CI job is green on the tag.
- [ ] GitHub release with the changelog section as its notes.
- [ ] Zenodo: enable it for the repository *before* tagging if you want a DOI,
      then add the DOI to `CITATION.cff` and the README.
- [ ] PyPI, when the gate contract has settled (ROADMAP M5). `pip install .`
      already works, so the remaining decision is the distribution name.

## 6. Tell the people who need it

- [ ] The project's own deliverable, website and data-management plan.
- [ ] The FIWARE catalogue, if the consortium wants it listed there.
- [ ] The partners whose systems the gates were written against: they are the
      most likely first users, and the most likely source of the next gate.
- [ ] Anyone who asked for "how do we get data out of X" during the project —
      `docs/legacy-systems.md` is the answer to that question.

## 7. After publishing

- [ ] Watch the first issues for the questions the documentation should have
      answered, and answer them in the documentation rather than the thread.
- [ ] Keep `config/gates.yaml` honest: it is the catalogue, it is tested, and
      a gate type without a working example there is a gate type nobody finds.
- [ ] Port improvements back to the private platform, and its findings
      forward. The gate docstrings are where that knowledge accumulates.
