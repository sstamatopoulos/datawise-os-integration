# Publishing checklist

What to do before this repository is public, and in what order. Written to be
worked through top to bottom by whoever presses the button; tick the items in
`ROADMAP.md` as you go.

## 1. Decide the identity

- [x] The account or organisation that will own it, and the final repository
      name. Everything below depends on it. **Done 2026-09-22**:
      `github.com/sstamatopoulos/datawise-os-integration`, created **private**
      until section 2 is finished. If the consortium later wants it under an
      organisation, transfer it rather than re-creating it, and redo this
      section's URL edits.
- [x] Replace the placeholder URL in `pyproject.toml` (`[project.urls]`),
      `CITATION.cff` (`repository-code`) and
      `.github/ISSUE_TEMPLATE/config.yml`.
- [ ] Confirm the copyright line and the licence with the consortium.
      Apache-2.0 is the usual choice for Horizon Europe software outputs; the
      decision and its reason are in `ROADMAP.md` §3.
- [x] `CITATION.cff`: individual author added beside ICCS. **Done 2026-09-23.**
      Still open: ORCIDs, the funding programme and grant number, and further
      authors as the consortium decides attribution.
- [x] Name the security contact in `SECURITY.md`, with a reporting process and
      a scope section. **Done 2026-09-23**: GitHub private advisories or
      sstamatopoulos@epu.ntua.gr.

## 2. Prove it works

Do not publish a stack nobody has started. §2 of `ROADMAP.md` lists what is
still unverified; these are the same items.

- [x] `ruff check .` clean and `pytest` green on 3.12 and 3.13. **Done**:
      CI is green on `main`, including the DAG factory under the Airflow the
      Dockerfile names (3.3.2 since 2026-09-24).
- [x] `docker compose up -d --build` on a clean machine. **Done 2026-09-22**;
      the seven fixes it cost are in `ROADMAP.md` §3.
- [x] One full cycle, for all three credential-free gates. **Done 2026-09-22**:
      48 forecast points, 1249 backfilled ERA5 points per property, 193 Nord
      Pool points, and the proxy checked from outside.
- [x] `python scripts/verify_platform.py` with nothing failing, and its output
      in `README.md` under "Verified output". **Done 2026-09-22.**
- [x] Automate the above so it cannot rot: `scripts/integration_test.sh` and the
      `integration` CI job. **Done 2026-09-22**, and it has already caught a
      dependency bump that every other job passed.
- [ ] At least one credentialed gate against a real upstream, ideally a legacy
      one, so the field-protocol path is not published purely on unit tests.
      The `sql` gate has run against SQLite — which found a data-loss bug in the
      cursor backfill (§3) — but nothing needing a credential or a device has.

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

- [x] No credentials, tokens or keys. **Checked 2026-09-23**, including that the
      live `ORION_API_KEY` from the running stack appears nowhere in the tree.
- [x] No pilot-specific names, building ids, meter serials or partner
      hostnames. **Checked 2026-09-23**, and it found two: a supplier's real
      hostname in a core docstring and its product name in an example
      environment variable, both now neutral. Example addresses are RFC 1918;
      example hosts end in `.example`.
- [x] `.env` is absent and ignored; `logs/` and `drop/` are ignored.
- [x] The git history is the one intended: this repository starts fresh and
      nothing has been rewritten.

## 4. Repository settings

- [ ] Description: one sentence, and the topics `fiware`, `ngsi-ld`,
      `orion-ld`, `influxdb`, `apache-airflow`, `modbus`, `bacnet`, `opc-ua`,
      `smart-buildings`, `energy`, `iot`, `data-integration`.
- [x] Description and the 12 topics. **Done 2026-09-22.**
- [x] Issues and Discussions on; squash and merge allowed, rebase off, branch
      deleted on merge. **Done 2026-09-22.**
- [x] Dependabot alerts and automated security fixes on. **Done 2026-09-22.**
- [x] Actions: workflow token read-only, and no secrets configured. **Done.**
- [ ] **After going public, not before**: branch protection on `main` requiring
      all six CI jobs (`lint-and-test (3.12)`, `lint-and-test (3.13)`,
      `packaging`, `gate-extras`, `dags-load`, `integration`), and private
      vulnerability reporting. Both are refused on a free private repository —
      branch protection answers *"Upgrade to GitHub Pro or make this repository
      public"* and vulnerability reporting answers 404 — so they are the first
      two things to do once the switch is flipped, not part of the preparation.
- [ ] Issue templates visible and sensible once the repository is public.
- [ ] Check the first Dependabot run after publication: the schedule is in
      `.github/dependabot.yml`, and five bumps arrived within a day of the
      repository being created.

## 5. Release

- [x] `CHANGELOG.md` finished and dated, with the unverified items named.
- [x] Same version in `pyproject.toml` and `CITATION.cff` (0.2.0).
- [x] Tag `v0.2.0` and push. **Done 2026-09-23.**
- [x] GitHub release with notes covering the catalogue, what was verified, what
      was not, and the defects a running stack found. **Done 2026-09-23.**
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
