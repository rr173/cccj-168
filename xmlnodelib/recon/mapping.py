"""Mapping configuration for snapshot reconciliation.

A mapping declares, entirely in the existing JSON path-AST syntax (relative
step lists plus a prefix->URI table):

* how to select product elements (``product.select``);
* the product key field (``product.sku``), whose value must be present and
  non-empty on exactly one node per product;
* the ordinary product fields to compare (``product.fields``), each of which
  may match zero or one node;
* optionally, where variant sub-items live relative to a product
  (``product.variants.path``), the variant key field (``variants.code``) and
  the ordinary variant fields to compare.

Names in every path are matched by expanded name (namespace URI + local
name); the mapping's prefix table is independent of the prefixes used in the
source XML, so re-prefixing a catalog cannot produce spurious changes.

The whole mapping is validated up front (reusing the query engine's AST
validator) before any node library is opened.
"""

import json
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from ..astcheck import validate_ast
from ..errors import AstError, ReconError

MAPPING_VERSION = 1


@dataclass(frozen=True)
class FieldDescriptor:
    """One compared field.

    ``name`` is the stable field path used in output events; ``steps`` is the
    normalized relative path (an empty tuple means the context node itself).
    """

    name: str
    steps: Tuple[object, ...]


@dataclass(frozen=True)
class VariantMapping:
    path_steps: Tuple[object, ...]
    key: FieldDescriptor
    fields: Tuple[FieldDescriptor, ...]


@dataclass(frozen=True)
class ProductMapping:
    select_steps: Tuple[object, ...]
    key: FieldDescriptor
    fields: Tuple[FieldDescriptor, ...]
    variant: Optional[VariantMapping]


@dataclass(frozen=True)
class Mapping:
    version: int
    namespaces: Dict[str, str]
    product: ProductMapping
    canonical: str


def _err(where, msg):
    raise ReconError("mapping %s: %s" % (where, msg))


def _check_keys(obj, allowed, where):
    if not isinstance(obj, dict):
        _err(where, "expected an object")
    extra = set(obj) - allowed
    if extra:
        _err(where, "unknown key(s): %s" % ", ".join(sorted(extra)))


def _parse_steps(raw, namespaces, where):
    """Validate a relative step list against the engine's AST validator."""
    if not isinstance(raw, list):
        _err(where, "expected a list of path steps")
    synthetic = {"version": 1, "namespaces": namespaces,
                 "paths": [{"steps": raw}]}
    try:
        ast = validate_ast(synthetic)
    except AstError as exc:
        _err(where, str(exc))
    return ast.paths[0].steps


def _parse_field(obj, namespaces, where):
    _check_keys(obj, {"name", "path"}, where)
    name = obj.get("name")
    if not isinstance(name, str) or not name:
        _err(where, "'name' must be a non-empty string")
    steps = _parse_steps(obj.get("path", []), namespaces, where + ".path")
    return FieldDescriptor(name, steps)


def _parse_fields(raw, namespaces, where):
    if not isinstance(raw, list):
        _err(where, "expected a list of field descriptors")
    fields = tuple(
        _parse_field(f, namespaces, "%s[%d]" % (where, i))
        for i, f in enumerate(raw))
    seen = set()
    for f in fields:
        if f.name in seen:
            _err(where, "duplicate field name %r" % f.name)
        seen.add(f.name)
    return fields


def _parse_mapping_dict(obj):
    _check_keys(obj, {"version", "namespaces", "product"}, "mapping")
    version = obj.get("version", 1)
    if version != MAPPING_VERSION:
        _err("", "unsupported mapping version %r (expected %d)"
             % (version, MAPPING_VERSION))
    namespaces = obj.get("namespaces", {})
    if not isinstance(namespaces, dict):
        _err("mapping.namespaces", "expected an object mapping prefixes to URIs")

    pobj = obj.get("product")
    if not isinstance(pobj, dict):
        _err("mapping.product", "expected an object")
    _check_keys(pobj, {"select", "sku", "fields", "variants"}, "mapping.product")

    select_steps = _parse_steps(pobj.get("select"), namespaces,
                                "mapping.product.select")
    if not select_steps:
        _err("mapping.product.select", "the product selection path must not be empty")
    key = _parse_field(pobj.get("sku"), namespaces, "mapping.product.sku")
    fields = _parse_fields(pobj.get("fields", []), namespaces,
                           "mapping.product.fields")

    variant = None
    vobj = pobj.get("variants")
    if vobj is not None:
        if not isinstance(vobj, dict):
            _err("mapping.product.variants", "expected an object")
        _check_keys(vobj, {"path", "code", "fields"}, "mapping.product.variants")
        vpath = _parse_steps(vobj.get("path"), namespaces,
                             "mapping.product.variants.path")
        if not vpath:
            _err("mapping.product.variants.path",
                 "the variant selection path must not be empty")
        vkey = _parse_field(vobj.get("code"), namespaces,
                            "mapping.product.variants.code")
        vfields = _parse_fields(vobj.get("fields", []), namespaces,
                                "mapping.product.variants.fields")
        variant = VariantMapping(vpath, vkey, vfields)

    canonical = json.dumps(obj, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"))
    return Mapping(version, dict(namespaces),
                   ProductMapping(select_steps, key, fields, variant), canonical)


def load_mapping(source):
    """Validate ``source`` (a mapping dict or a path to a mapping JSON file)
    and return a normalized :class:`Mapping`.

    Raises :class:`ReconError` on any structural or semantic problem; nothing
    else is touched before validation succeeds.
    """
    if isinstance(source, Mapping):
        return source
    if isinstance(source, dict):
        return _parse_mapping_dict(source)
    if isinstance(source, str):
        try:
            with open(source, "r", encoding="utf-8") as fh:
                obj = json.load(fh)
        except OSError as exc:
            raise ReconError("cannot read mapping file %s: %s" % (source, exc))
        except json.JSONDecodeError as exc:
            raise ReconError("mapping file %s is not valid JSON: %s"
                             % (source, exc))
        if not isinstance(obj, dict):
            raise ReconError("mapping file %s must contain a JSON object" % source)
        return _parse_mapping_dict(obj)
    raise ReconError("mapping source must be a dict or a JSON file path")
