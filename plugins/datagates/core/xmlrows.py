"""
datagates.core.xmlrows — reading rows out of XML, for the gates that must.

XML is where the legacy is: oBIX from a Niagara station, a SOAP envelope
from a utility's billing system, ENTSO-E market documents, MSCONS-style
meter exports, a PLC gateway that only speaks XML over HTTP. Those gates
share this module rather than each growing their own XPath handling.

Two conventions, both learned the hard way:

- **Namespaces are stripped on parse.** Real-world documents change their
  namespace URI between versions of the same product (oBIX 1.0 vs 1.1,
  ENTSO-E document revisions) and a configuration written against one URI
  then silently matches nothing. Matching on local names keeps a YAML
  mapping working across upstream upgrades; pass `keep_namespaces=True`
  when a document genuinely needs them.
- **A field selector is a tiny path, not full XPath.** `value` (child
  text), `@val` (attribute), `obj/real/@val` (nested attribute), `.`
  (this element's text). ElementTree implements a subset of XPath only,
  and pretending otherwise produces configurations that fail at 3 a.m.
"""
from __future__ import annotations

import re
from typing import Any
from xml.etree import ElementTree as ET

_NS = re.compile(r"\{[^}]*\}")


def strip_ns(tag: str) -> str:
    return _NS.sub("", tag)


def parse_xml(text: str | bytes, *, keep_namespaces: bool = False) -> ET.Element:
    """Parse a document and, by default, drop namespaces from every tag
    and attribute name.

    ElementTree does not resolve external entities and refuses undefined
    ones, so a hostile document cannot make it fetch a URL or read a file;
    it is still worth remembering that the input is a partner's file.
    """
    if isinstance(text, str):
        text = text.encode("utf-8", errors="replace")
    root = ET.fromstring(text.lstrip())        # noqa: S314 - see the docstring
    if keep_namespaces:
        return root
    for el in root.iter():
        if isinstance(el.tag, str):
            el.tag = strip_ns(el.tag)
        for name in [a for a in el.attrib if "}" in a]:
            el.attrib[strip_ns(name)] = el.attrib.pop(name)
    return root


def select(root: ET.Element, path: str) -> list[ET.Element]:
    """Elements matching `path`, which may be an ElementTree path
    (`.//Point`, `obj/list/obj`) or a bare local tag name found anywhere."""
    path = (path or "").strip()
    if not path or path == ".":
        return [root]
    if re.fullmatch(r"[A-Za-z_][\w.-]*", path):
        return root.findall(f".//{path}") or ([root] if strip_ns(root.tag) == path else [])
    return root.findall(path)


def pick(element: ET.Element, selector: str) -> Any:
    """Value of a field selector relative to `element`. None when absent."""
    selector = (selector or ".").strip()
    if selector in (".", "text()"):
        return (element.text or "").strip() or None
    if selector.startswith("@"):
        return element.get(selector[1:])
    head, _, attr = selector.partition("/@")
    target = element if head in ("", ".") else element.find(head)
    if target is None:
        return None
    if attr:
        return target.get(attr)
    return (target.text or "").strip() or None


def row_of(element: ET.Element, selectors: dict[str, str]) -> dict[str, Any]:
    """`{name: selector}` -> `{name: value}`, for the field maps."""
    return {name: pick(element, sel) for name, sel in selectors.items()}


def self_and_attributes(element: ET.Element) -> dict[str, Any]:
    """A flat row from an element: its attributes plus every child's text
    and `val`/`value` attribute, keyed by local name. Enough for the
    common "one element per reading" document without writing selectors."""
    row: dict[str, Any] = dict(element.attrib)
    for child in element:
        name = strip_ns(child.tag) if isinstance(child.tag, str) else ""
        if not name:
            continue
        value = (child.text or "").strip()
        row.setdefault(name, value or child.get("val") or child.get("value"))
    return row
