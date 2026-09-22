import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins"))
os.environ.setdefault("INFLUX_TOKEN", "")          # writers no-op in tests
os.environ.setdefault("DATAGATES_CONFIG", str(ROOT / "config" / "gates.yaml"))


@pytest.fixture
def doc_of():
    """The flat device dict a gate's fetch() receives, built exactly the
    way the run DAG builds it: discover() -> Orion entity -> device doc.
    Going through the entity is the point; it is where Orion's scalar
    compaction and the device_attrs contract would break."""
    from datagates.core.entities import device_doc, device_entity

    def build(gate, index: int = 0):
        return device_doc(device_entity(gate.discover()[index], gate), gate)

    return build
