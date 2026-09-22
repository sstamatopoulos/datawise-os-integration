"""The controlledProperty vocabulary, and the gates' obligation to it.

A registry nothing enforces is an appendix. These tests are what make it a
contract: every property any built-in gate can emit is registered, with a unit
that exists and an aggregation rule a consumer can act on.
"""
from __future__ import annotations

import pytest

from datagates.core import vocab
from datagates.gates.registry import BUILTIN_TYPES, load_gates

EXAMPLE_SECRETS = {"TB_USERNAME": "u", "TB_PASSWORD": "p", "MESH_API_KEY": "k", "ENTSOE_TOKEN": "t"}


@pytest.fixture
def shipped_gates(monkeypatch):
    for name, value in EXAMPLE_SECRETS.items():
        monkeypatch.setenv(name, value)
    return load_gates(include_disabled=True)


def emitted(gates) -> dict[str, set[str]]:
    """property -> the unit codes the gates declare for it."""
    out: dict[str, set[str]] = {}
    for gate in gates:
        for spec in gate.discover():
            for name, unit in spec.properties.items():
                out.setdefault(name, set()).add(unit)
    return out


def test_every_property_the_catalogue_emits_is_registered(shipped_gates):
    unknown = sorted(set(emitted(shipped_gates)) - set(vocab.PROPERTIES))
    assert not unknown, (
        f"unregistered properties: {unknown}. Add them to core/vocab.py and docs/properties.md, "
        "or use a registered name — a consumer cannot tell energy from energyConsumption by guessing")


def test_no_gate_uses_a_name_that_was_replaced(shipped_gates):
    used = set(emitted(shipped_gates))
    stale = sorted(used & set(vocab.DEPRECATED))
    assert not stale, f"deprecated names still in use: {[(n, vocab.DEPRECATED[n]) for n in stale]}"


def test_the_units_the_gates_declare_all_exist(shipped_gates):
    for name, units in sorted(emitted(shipped_gates).items()):
        for unit in units:
            assert unit in vocab.UNITS, f"{name}: unit code {unit!r} is not in core/vocab.py UNITS"


def test_a_property_measured_in_two_units_is_deliberate(shipped_gates):
    """Two gates may report the same property in different units — power in kW
    and in W is normal. It is only safe because unitCode lives on each summary,
    so this test documents the cases rather than forbidding them."""
    multi = {n: sorted(u) for n, u in emitted(shipped_gates).items() if len(u) > 1}
    assert multi == {"power": ["KWT", "WTT"]}, (
        f"new multi-unit properties: {multi}. Fine, but check the consumer guide mentions reading "
        "unitCode, and that the registry's default unit is still the common one")


def test_every_registered_property_is_usable_by_a_consumer():
    for name, prop in vocab.PROPERTIES.items():
        assert prop.name == name
        assert prop.unit in vocab.UNITS, f"{name}: default unit {prop.unit!r} is not registered"
        assert prop.kind in (vocab.INSTANT, vocab.DELTA, vocab.CUMULATIVE, vocab.STATE), name
        assert prop.aggregate in (vocab.MEAN, vocab.SUM, vocab.DIFFERENCE, vocab.LAST, vocab.NONE), name
        assert prop.meaning and not prop.meaning[0].isupper(),             f"{name}: the meaning is a fragment completing \"this property is ...\", not a sentence"


def test_the_aggregation_rule_follows_from_the_kind():
    """The rules that make a downsampled series wrong if broken: a register is
    differenced, a period total is summed, and neither is averaged."""
    for name, prop in vocab.PROPERTIES.items():
        if prop.kind == vocab.CUMULATIVE:
            assert prop.aggregate == vocab.DIFFERENCE, f"{name}: a meter register is never summed or averaged"
        if prop.kind == vocab.DELTA:
            assert prop.aggregate == vocab.SUM, f"{name}: a period total is summed, not averaged"
        if prop.kind == vocab.STATE:
            assert prop.aggregate in (vocab.LAST, vocab.NONE), f"{name}: a state cannot be averaged"


def test_cumulative_properties_are_the_ones_gates_guard(shipped_gates):
    """`cumulative: true` in YAML and CUMULATIVE here are the same claim: the
    counter guard protects exactly the properties that only go up."""
    guarded = {p for gate in shipped_gates for p in gate.cumulative}
    for name in guarded:
        prop = vocab.lookup(name)
        assert prop is not None and prop.kind == vocab.CUMULATIVE, (
            f"{name} is guarded as a counter but registered as {prop.kind if prop else 'unknown'}")


def test_semantic_mappings_are_marked_as_verified_or_not():
    for name, prop in vocab.PROPERTIES.items():
        if prop.status == vocab.CONFIRMED:
            assert prop.saref, f"{name}: confirmed mapping with no IRI"
            assert prop.saref.startswith("https://saref.etsi.org/"), name
        if prop.status == vocab.UNMAPPED:
            assert prop.note, f"{name}: unmapped properties need a note saying why"
    # The honest state of the mapping, so that an export cannot quietly ship
    # guesses: this list shrinks only by checking the published ontologies.
    assert len(vocab.unmapped()) == 28, (
        f"the unverified set changed: {vocab.unmapped()}. Update this count when a mapping is "
        "confirmed against saref.etsi.org, and say so in docs/properties.md")


def test_units_carry_a_name_and_a_candidate_iri():
    for code, (name, iri) in vocab.UNITS.items():
        assert name and not name.startswith(code), f"{code}: needs a human-readable name"
        assert iri is None or iri.startswith("http://qudt.org/vocab/unit/"), code
    assert vocab.unit_name("CEL") == "degree Celsius"
    assert vocab.unit_name("XYZ") == "XYZ", "an unknown code reads back as itself"


def test_the_helpers_behave_for_names_nobody_registered():
    assert vocab.is_registered("temperature") and not vocab.is_registered("temprature")
    assert vocab.canonical("humidity") == "relativeHumidity"
    assert vocab.canonical("temperature") == "temperature"
    # LAST is the safe default: it cannot invent a value that was never read.
    assert vocab.aggregation_of("somebodys_custom_property") == vocab.LAST
    assert vocab.aggregation_of("energy") == vocab.DIFFERENCE


def test_the_registry_covers_the_examples_in_the_gate_docstrings():
    """The docstrings are what people copy, so a property that appears only
    there still has to be registered."""
    import importlib
    import re

    # Match the inline-mapping form the YAML examples use, `{property: name`,
    # and nothing else: a docstring that merely discusses `property:` in prose
    # is not declaring one.
    pattern = re.compile(r"\{property:\s*([A-Za-z][A-Za-z0-9]*)")
    seen: set[str] = set()
    for target in BUILTIN_TYPES.values():
        module = importlib.import_module(target.split(":")[0])
        seen.update(pattern.findall(module.__doc__ or ""))
    unknown = sorted(n for n in seen if not vocab.is_registered(n))
    assert not unknown, f"properties shown in docstrings but not registered: {unknown}"


def test_the_consumer_client_agrees_about_which_properties_are_counters():
    """clients/python/datagates_client.py carries its own copy of the
    cumulative set, because a consumer copies that one file into their project
    and it must not import the platform. A deliberate duplicate still needs
    something holding it to the registry, or it drifts — which is exactly what
    happened to the copy in core/counter_guard.py."""
    import ast
    import pathlib

    source = pathlib.Path("clients/python/datagates_client.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    found = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", None) == "CUMULATIVE_PROPERTIES" for t in node.targets):
            value = node.value
            # frozenset({...}) is a Call; literal_eval only takes the set inside it.
            if isinstance(value, ast.Call) and value.args:
                value = value.args[0]
            found = ast.literal_eval(value)
    assert found is not None, "the client no longer declares CUMULATIVE_PROPERTIES"
    assert set(found) == set(vocab.CUMULATIVE_PROPERTIES), (
        "the client and core/vocab.py disagree about which properties are running totals; "
        f"client has {sorted(set(found) - set(vocab.CUMULATIVE_PROPERTIES))} extra and "
        f"{sorted(set(vocab.CUMULATIVE_PROPERTIES) - set(found))} missing")


def test_the_documentation_lists_every_registered_property():
    """docs/properties.md is the registry's public face: a property that is not
    in it is a property a consumer will not find."""
    import pathlib

    doc = pathlib.Path("docs/properties.md").read_text(encoding="utf-8")
    missing = sorted(name for name in vocab.PROPERTIES if f"`{name}`" not in doc)
    assert not missing, f"not documented in docs/properties.md: {missing}"
    for code in vocab.UNITS:
        assert f"`{code}`" in doc, f"unit code {code} is not documented"
