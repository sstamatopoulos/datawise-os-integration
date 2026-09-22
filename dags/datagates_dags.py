"""
Every gate declared in config/gates.yaml becomes three DAGs here:
<key>_init, <key>_run and <key>_backfill. Nothing else to write.

A gate whose class fails to import is logged and skipped so the rest of
the bag still loads; fix the gate and the scheduler picks it up.
"""
from __future__ import annotations

import logging

from datagates.dags.factory import build_dags
from datagates.gates.registry import load_config, resolve_type

log = logging.getLogger(__name__)

for _cfg in load_config():
    if not _cfg.enabled:
        continue
    try:
        _gate = resolve_type(_cfg.type)(_cfg)
        globals().update(build_dags(_gate))
    except Exception as exc:                                       # noqa: BLE001
        log.error("gate %r (%s) could not be loaded: %s", _cfg.key, _cfg.type, exc)
