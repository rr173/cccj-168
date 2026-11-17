"""Validation of the structured JSON path AST.

The whole AST is validated and normalized *before* any query execution:
unknown operations, malformed steps, unbound namespace prefixes, illegal
positions or a union inside a relative existence path are all reported here,
so a query never runs half-way and silently drops its remaining conditions.

Normalized nodes are frozen dataclasses consumed by the engine.
"""

from dataclasses import dataclass
from typing import Optional, Tuple

from .errors import AstError

AXES = (
    "child", "descendant", "parent", "ancestor",
    "following-sibling", "preceding-sibling", "attribute",
)

XML_NS = "http://www.w3.org/XML/1998/namespace"

MAX_AST_NODES = 10000
MAX_PATH_DEPTH = 64  # nesting of exists() relative paths


# ---------------------------------------------------------------- normalized
@dataclass(frozen=True)
class NameTest:
    kind: str                      # 'name' | 'star' | 'text' | 'node'
    ns: Optional[str] = None
    local: Optional[str] = None


@dataclass(frozen=True)
class PosPred:
    n: Optional[int] = None        # position()=n
    is_last: bool = False          # position()=last()


@dataclass(frozen=True)
class AttrEq:
    ns: Optional[str]
    local: str
    value: str


@dataclass(frozen=True)
class StrEq:
    value: str


@dataclass(frozen=True)
class Exists:
    steps: Tuple["Step", ...]


@dataclass(frozen=True)
class And:
    args: tuple


@dataclass(frozen=True)
class Or:
    args: tuple


@dataclass(frozen=True)
class Not:
    arg: object


@dataclass(frozen=True)
class Step:
    axis: str
    test: NameTest
    predicates: tuple


@dataclass(frozen=True)
class Path:
    steps: Tuple[Step, ...]


@dataclass(frozen=True)
class Ast:
    context_id: Optional[int]
    paths: Tuple[Path, ...]


class _Counter:
    def __init__(self):
        self.nodes = 0

    def tick(self, where):
        self.nodes += 1
        if self.nodes > MAX_AST_NODES:
            raise AstError("AST too large (>%d nodes) at %s" % (MAX_AST_NODES, where))


def _err(where, msg):
    raise AstError("%s: %s" % (where, msg))


def resolve_qname(qname, nsmap, where):
    """Resolve a QName to (ns, local).  Unprefixed names are in no namespace
    (matching XPath 1.0 name tests).  Unbound prefixes are an error."""
    if not isinstance(qname, str) or not qname:
        _err(where, "name must be a non-empty string")
    if qname.count(":") > 1:
        _err(where, "malformed name %r (more than one ':')" % qname)
    if ":" in qname:
        prefix, local = qname.split(":", 1)
        if not prefix or not local:
            _err(where, "malformed name %r" % qname)
        if prefix == "xmlns":
            _err(where, "the 'xmlns' prefix is not usable in queries")
        if prefix == "xml":
            return XML_NS, local
        if prefix not in nsmap:
            _err(where, "unbound namespace prefix %r" % prefix)
        return nsmap[prefix], local
    return None, qname


def _parse_name_fields(obj, nsmap, where):
    """Parse either {'qname': ...} or {'uri': ..., 'local': ...} fields."""
    has_qname = "qname" in obj
    has_expanded = "local" in obj or "uri" in obj
    if has_qname and has_expanded:
        _err(where, "use either 'qname' or 'uri'/'local', not both")
    if has_qname:
        return resolve_qname(obj["qname"], nsmap, where)
    if "local" not in obj:
        _err(where, "missing 'local' (or 'qname')")
    local = obj["local"]
    uri = obj.get("uri")
    if not isinstance(local, str) or not local:
        _err(where, "'local' must be a non-empty string")
    if uri is not None and (not isinstance(uri, str) or not uri):
        _err(where, "'uri' must be null or a non-empty string")
    return uri, local


def _check_keys(obj, allowed, where):
    if not isinstance(obj, dict):
        _err(where, "expected an object")
    extra = set(obj) - allowed
    if extra:
        _err(where, "unknown key(s): %s" % ", ".join(sorted(extra)))


def _parse_test(obj, nsmap, where, counter):
    counter.tick(where)
    _check_keys(obj, {"kind", "qname", "uri", "local"}, where)
    kind = obj.get("kind")
    if kind == "name":
        ns, local = _parse_name_fields(obj, nsmap, where)
        return NameTest("name", ns, local)
    if kind in ("star", "text", "node"):
        if set(obj) != {"kind"}:
            _err(where, "test %r takes no further fields" % kind)
        return NameTest(kind)
    _err(where, "unknown node test kind %r (expected 'name', 'star', 'text' or 'node')"
         % (kind,))


def _parse_step(obj, nsmap, where, counter, depth):
    counter.tick(where)
    _check_keys(obj, {"axis", "test", "predicates"}, where)
    axis = obj.get("axis")
    if axis not in AXES:
        _err(where, "unknown axis %r (supported: %s)" % (axis, ", ".join(AXES)))
    if "test" not in obj:
        _err(where, "missing 'test'")
    test = _parse_test(obj["test"], nsmap, where + ".test", counter)
    preds_raw = obj.get("predicates", [])
    if not isinstance(preds_raw, list):
        _err(where + ".predicates", "expected a list")
    preds = tuple(
        _parse_pred(p, nsmap, "%s.predicates[%d]" % (where, i), counter, depth,
                    allow_pos=True)
        for i, p in enumerate(preds_raw))
    return Step(axis, test, preds)


def _parse_path(obj, nsmap, where, counter, depth):
    counter.tick(where)
    _check_keys(obj, {"steps"}, where)
    steps_raw = obj.get("steps")
    if not isinstance(steps_raw, list):
        _err(where, "'steps' must be a list")
    steps = tuple(
        _parse_step(s, nsmap, "%s.steps[%d]" % (where, i), counter, depth)
        for i, s in enumerate(steps_raw))
    return Path(steps)


def _parse_pred(obj, nsmap, where, counter, depth, allow_pos):
    counter.tick(where)
    _check_keys(obj, {"kind", "n", "last", "qname", "uri", "local", "value",
                      "path", "args", "arg"}, where)
    kind = obj.get("kind")
    if kind == "pos":
        if not allow_pos:
            _err(where, "position predicates are only allowed directly in a "
                        "step's predicate list, not inside boolean expressions")
        has_n = "n" in obj
        has_last = "last" in obj
        if has_n == has_last:
            _err(where, "position predicate needs exactly one of 'n' or 'last'")
        if has_n:
            n = obj["n"]
            if not isinstance(n, int) or isinstance(n, bool) or n < 1:
                _err(where, "position 'n' must be an integer >= 1")
            return PosPred(n=n)
        if obj["last"] is not True:
            _err(where, "'last' must be true")
        return PosPred(is_last=True)
    if kind == "attr":
        ns, local = _parse_name_fields(obj, nsmap, where)
        value = obj.get("value")
        if not isinstance(value, str):
            _err(where, "attribute comparison 'value' must be a string")
        return AttrEq(ns, local, value)
    if kind == "str":
        value = obj.get("value")
        if not isinstance(value, str):
            _err(where, "string-value comparison 'value' must be a string")
        return StrEq(value)
    if kind == "exists":
        if depth + 1 > MAX_PATH_DEPTH:
            _err(where, "relative paths nested deeper than %d" % MAX_PATH_DEPTH)
        raw = obj.get("path")
        if not isinstance(raw, dict):
            _err(where, "'exists' needs a 'path' object")
        if "paths" in raw:
            _err(where + ".path", "relative existence paths do not support "
                                   "path union ('paths'); use a single 'steps' list")
        return Exists(_parse_path(raw, nsmap, where + ".path", counter,
                                  depth + 1).steps)
    if kind in ("and", "or"):
        args = obj.get("args")
        if not isinstance(args, list) or not args:
            _err(where, "'%s' needs a non-empty 'args' list" % kind)
        parsed = tuple(
            _parse_pred(a, nsmap, "%s.args[%d]" % (where, i), counter, depth,
                        allow_pos=False)
            for i, a in enumerate(args))
        return And(parsed) if kind == "and" else Or(parsed)
    if kind == "not":
        if "arg" not in obj:
            _err(where, "'not' needs an 'arg'")
        return Not(_parse_pred(obj["arg"], nsmap, where + ".arg", counter, depth,
                               allow_pos=False))
    _err(where, "unknown predicate kind %r (expected 'pos', 'attr', 'str', "
                "'exists', 'and', 'or' or 'not')" % (kind,))


def validate_ast(obj):
    """Validate a decoded JSON AST object and return the normalized Ast."""
    counter = _Counter()
    where = "ast"
    counter.tick(where)
    _check_keys(obj, {"version", "namespaces", "context", "paths", "comment"},
                where)
    if "comment" in obj and not isinstance(obj["comment"], str):
        _err(where + ".comment", "optional 'comment' must be a string")
    version = obj.get("version", 1)
    if version != 1:
        _err(where, "unsupported AST version %r (expected 1)" % (version,))
    nsmap = obj.get("namespaces", {})
    if not isinstance(nsmap, dict):
        _err(where + ".namespaces", "expected an object mapping prefixes to URIs")
    for prefix, uri in nsmap.items():
        if not isinstance(prefix, str) or not prefix or ":" in prefix:
            _err(where + ".namespaces", "invalid prefix %r" % (prefix,))
        if prefix in ("xml", "xmlns"):
            _err(where + ".namespaces", "prefix %r is reserved" % prefix)
        if not isinstance(uri, str) or not uri:
            _err(where + ".namespaces",
                 "prefix %r must map to a non-empty URI string" % prefix)
    context_id = None
    context = obj.get("context")
    if context is not None:
        _check_keys(context, {"id"}, where + ".context")
        context_id = context.get("id")
        if not isinstance(context_id, int) or isinstance(context_id, bool) \
                or context_id < 1:
            _err(where + ".context.id", "context id must be a positive integer")
    paths_raw = obj.get("paths")
    if not isinstance(paths_raw, list) or not paths_raw:
        _err(where + ".paths", "expected a non-empty list of paths")
    paths = tuple(
        _parse_path(p, nsmap, "%s.paths[%d]" % (where, i), counter, 0)
        for i, p in enumerate(paths_raw))
    return Ast(context_id, paths)
