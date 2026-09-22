## What and why

<!-- The diff shows the what; say why. If it fixes an issue, link it. -->

## Checklist

- [ ] `ruff check .` and `pytest` pass
- [ ] Tests added or updated, on a recorded payload, no network access
- [ ] Docs updated (`docs/gates.md` for an option, `docs/legacy-systems.md` for a product, `CHANGELOG.md`)
- [ ] No secrets, no real site, building, meter or person names anywhere in the diff

### For a new gate type

- [ ] Class with a docstring showing its YAML and what the upstream really does
- [ ] Any driver imported inside the method that uses it, not at module level
- [ ] `BUILTIN_TYPES` entry, and `requirements-gates.txt` + `pyproject.toml` extra if it needs a driver
- [ ] A working, disabled example in `config/gates.yaml` (the catalogue test checks it)
- [ ] New `${VAR}` names added to `.env.example` and `docker-compose.yml`
- [ ] Section in `docs/gates.md`

### For a change under `plugins/datagates/core/`

- [ ] I read the decision log in `ROADMAP.md` §3 and this change does not quietly reverse one
- [ ] The summary URN namespace and key format are unchanged
- [ ] Verified against a running stack, or it is stated below what could not be verified
