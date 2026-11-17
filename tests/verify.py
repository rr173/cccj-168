#!/usr/bin/env python3
"""Independent verification program for xmlnodelib.

Cross-checks the shipped engine against an *independent* reference model
built on xml.dom.minidom (plain tree walking; it does not import or call any
of xmlnodelib's index or predicate-execution code), plus explicitly listed
expected node identities, orders and values for every case.

Covers: the three-group ordering example, reverse axes, nested same-name
containers, two prefixes bound to one URI, a rebound prefix, default
namespace vs unprefixed attributes, mixed content, CDATA adjacency,
comment/PI text splitting, union dedup of one node found by two paths, two
distinct nodes with identical values, attribute ordering convention, prolog
comment/PI, entity/character references, parser input-chunk and value-chunk
invariance, page-size semantics, and rejection of truncated input, DOCTYPE,
unknown prefixes and malformed ASTs (library must stay untouched).

Usage:  python3 tests/verify.py [--keep] [--workdir DIR]
Exit code 0 iff every check passes.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from xml.dom import minidom

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = Path(__file__).resolve().parent / "samples"

FAILURES = []
CHECKS = [0]


def check(cond, label, detail=""):
    CHECKS[0] += 1
    if cond:
        print("ok %3d  %s" % (CHECKS[0], label))
    else:
        print("FAIL %3d  %s" % (CHECKS[0], label))
        if detail:
            print("     " + detail.replace("\n", "\n     "))
        FAILURES.append(label)


# --------------------------------------------------------------------------
# Independent reference model (tree walk over minidom; no xmlnodelib code).
# --------------------------------------------------------------------------
CONTENT_KINDS = {"element", "text", "comment", "pi"}
XMLNS_URI = "http://www.w3.org/2000/xmlns/"
XML_NS = "http://www.w3.org/XML/1998/namespace"


class RNode:
    __slots__ = ("id", "kind", "ns", "local", "parent", "depth", "last_desc",
                 "value")


def build_ref(xml_bytes):
    """Parse bytes into the reference node list (ids start at 1, document
    order with the tool's attribute-ordering convention)."""
    dom = minidom.parseString(xml_bytes)
    nodes = []

    def add(kind, ns, local, parent, depth, value=None):
        n = RNode()
        n.id = len(nodes) + 1
        n.kind, n.ns, n.local = kind, ns, local
        n.parent, n.depth, n.last_desc, n.value = parent, depth, None, value
        nodes.append(n)
        return n

    doc = add("document", None, None, None, 0)

    def logical_children(domnode):
        out = []
        for ch in domnode.childNodes:
            t = ch.nodeType
            if t in (ch.TEXT_NODE, ch.CDATA_SECTION_NODE):
                if out and out[-1][0] == "text":
                    out[-1][1] += ch.data
                else:
                    out.append(["text", ch.data])
            elif t == ch.ELEMENT_NODE:
                out.append(["element", ch])
            elif t == ch.COMMENT_NODE:
                out.append(["comment", ch.data])
            elif t == ch.PROCESSING_INSTRUCTION_NODE:
                out.append(["pi", ch.target, ch.data])
        return out

    def walk(domnode, parent, child_depth):
        for item in logical_children(domnode):
            if item[0] == "text":
                add("text", None, None, parent.id, child_depth, item[1])
            elif item[0] == "comment":
                add("comment", None, None, parent.id, child_depth, item[1])
            elif item[0] == "pi":
                add("pi", None, item[1], parent.id, child_depth, item[2])
            else:
                el = item[1]
                e = add("element", el.namespaceURI or None, el.localName,
                        parent.id, child_depth)
                attrs = []
                for i in range(el.attributes.length):
                    a = el.attributes.item(i)
                    if a.namespaceURI == XMLNS_URI:
                        continue  # xmlns declarations are not attributes here
                    attrs.append((a.namespaceURI or None, a.localName, a.value))
                attrs.sort(key=lambda t: (t[0] or "", t[1]))
                for ans, alocal, aval in attrs:
                    add("attribute", ans, alocal, e.id, child_depth + 1, aval)
                walk(el, e, child_depth + 1)
                e.last_desc = nodes[-1].id

    walk(dom, doc, 1)
    doc.last_desc = nodes[-1].id
    children_of = {}
    for n in nodes:
        if n.parent is not None:
            children_of.setdefault(n.parent, []).append(n.id)
    return nodes, children_of


def ref_string_value(nodes, n):
    if n.kind in ("element", "document"):
        return "".join(t.value for t in nodes
                       if t.kind == "text" and n.id < t.id <= n.last_desc)
    return n.value or ""


def ref_dump(nodes):
    """Expected export records for every node, in id (=document) order."""
    out = []
    for n in nodes:
        name = None if n.local is None else {"uri": n.ns, "local": n.local}
        out.append({"id": n.id, "kind": n.kind, "name": name,
                    "parent": n.parent, "value": ref_string_value(nodes, n)})
    return out


def ref_resolve(qname, nsmap):
    if ":" in qname:
        prefix, local = qname.split(":", 1)
        if prefix == "xml":
            return XML_NS, local
        return nsmap[prefix], local
    return None, qname


def ref_name(obj, nsmap):
    if "qname" in obj:
        return ref_resolve(obj["qname"], nsmap)
    return obj.get("uri"), obj["local"]


def ref_axis(nodes, children_of, n, axis):
    if axis == "child":
        if n.kind not in ("document", "element"):
            return []
        return [nodes[i - 1] for i in children_of.get(n.id, [])
                if nodes[i - 1].kind in CONTENT_KINDS]
    if axis == "descendant":
        if n.last_desc is None:
            return []
        return [t for t in nodes
                if n.id < t.id <= n.last_desc and t.kind in CONTENT_KINDS]
    if axis == "parent":
        return [nodes[n.parent - 1]] if n.parent else []
    if axis == "ancestor":
        out, p = [], n.parent
        while p:
            out.append(nodes[p - 1])
            p = nodes[p - 1].parent
        return out
    if axis in ("following-sibling", "preceding-sibling"):
        if n.kind == "attribute" or not n.parent:
            return []
        sibs = [nodes[i - 1] for i in children_of.get(n.parent, [])]
        sibs = [t for t in sibs if t.kind in CONTENT_KINDS]
        if axis == "following-sibling":
            return [t for t in sibs if t.id > n.id]
        return [t for t in reversed(sibs) if t.id < n.id]
    if axis == "attribute":
        if n.kind != "element":
            return []
        return [nodes[i - 1] for i in children_of.get(n.id, [])
                if nodes[i - 1].kind == "attribute"]
    raise AssertionError(axis)


def ref_match(n, axis, test, nsmap):
    kind = test["kind"]
    if kind == "node":
        return True
    if kind == "text":
        return n.kind == "text"
    principal = "attribute" if axis == "attribute" else "element"
    if kind == "star":
        return n.kind == principal
    ns, local = ref_name(test, nsmap)
    return n.kind == principal and n.ns == ns and n.local == local


def ref_apply_preds(nodes, children_of, seq, preds, nsmap):
    for p in preds:
        k = p["kind"]
        if k == "pos":
            if p.get("last"):
                seq = seq[-1:]
            else:
                seq = seq[p["n"] - 1:p["n"]]
        else:
            seq = [x for x in seq if ref_bool(nodes, children_of, x, p, nsmap)]
    return seq


def ref_bool(nodes, children_of, node, p, nsmap):
    k = p["kind"]
    if k == "attr":
        ns, local = ref_name(p, nsmap)
        if node.kind != "element":
            return False
        for i in children_of.get(node.id, []):
            a = nodes[i - 1]
            if a.kind == "attribute" and a.ns == ns and a.local == local:
                return (a.value or "") == p["value"]
        return False
    if k == "str":
        return ref_string_value(nodes, node) == p["value"]
    if k == "exists":
        return ref_exists(nodes, children_of, node, p["path"]["steps"], nsmap)
    if k == "and":
        return all(ref_bool(nodes, children_of, node, a, nsmap)
                   for a in p["args"])
    if k == "or":
        return any(ref_bool(nodes, children_of, node, a, nsmap)
                   for a in p["args"])
    if k == "not":
        return not ref_bool(nodes, children_of, node, p["arg"], nsmap)
    raise AssertionError(k)


def ref_exists(nodes, children_of, ctx, steps, nsmap):
    if not steps:
        return True
    step = steps[0]
    seq = [x for x in ref_axis(nodes, children_of, ctx, step["axis"])
           if ref_match(x, step["axis"], step["test"], nsmap)]
    seq = ref_apply_preds(nodes, children_of, seq,
                          step.get("predicates", []), nsmap)
    return any(ref_exists(nodes, children_of, c, steps[1:], nsmap)
               for c in seq)


def ref_step(nodes, children_of, contexts, step, nsmap):
    merged = {}
    for ctx in contexts:
        seq = [x for x in ref_axis(nodes, children_of, ctx, step["axis"])
               if ref_match(x, step["axis"], step["test"], nsmap)]
        seq = ref_apply_preds(nodes, children_of, seq,
                              step.get("predicates", []), nsmap)
        for x in seq:
            merged[x.id] = x
    return [merged[i] for i in sorted(merged)]


def ref_eval(nodes, children_of, ast):
    nsmap = ast.get("namespaces", {})
    ctx_id = (ast.get("context") or {}).get("id") or 1
    result = {}
    for path in ast["paths"]:
        contexts = [nodes[ctx_id - 1]]
        for step in path["steps"]:
            contexts = ref_step(nodes, children_of, contexts, step, nsmap)
        for x in contexts:
            result[x.id] = x
    return [result[i] for i in sorted(result)]


# --------------------------------------------------------------------------
# CLI driving helpers
# --------------------------------------------------------------------------
def run_cli(args):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    # cwd is the data-only workdir: no chance of shadowing modules.
    return subprocess.run([sys.executable, "-m", "xmlnodelib"] + args,
                          capture_output=True, text=True, env=env, cwd=_CWD[0])


_CWD = [str(ROOT)]


def do_import(xml_path, db_path, extra=()):
    return run_cli(["import", "--xml", str(xml_path), "--db", str(db_path)]
                   + list(extra))


def do_query(db_path, ast_obj, out_path, ast_path, extra=()):
    with open(ast_path, "w", encoding="utf-8") as fh:
        json.dump(ast_obj, fh)
    return run_cli(["query", "--db", str(db_path), "--ast", str(ast_path),
                    "--out", str(out_path)] + list(extra))


def read_jsonl(path):
    records, summary = [], None
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            obj = json.loads(line)
            if obj.get("summary"):
                summary = obj
            else:
                records.append(obj)
    return records, summary


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(65536), b""):
            h.update(block)
    return h.hexdigest()


DUMP_AST = {"paths": [
    {"steps": []},
    {"steps": [{"axis": "descendant", "test": {"kind": "node"}}]},
    {"steps": [{"axis": "descendant", "test": {"kind": "star"}},
               {"axis": "attribute", "test": {"kind": "star"}}]},
]}


def st(axis, test, preds=None):
    s = {"axis": axis, "test": test}
    if preds:
        s["predicates"] = preds
    return s


def nm(qname):
    return {"kind": "name", "qname": qname}


STAR = {"kind": "star"}
TEXT = {"kind": "text"}
NODE = {"kind": "node"}


def pos(n):
    return {"kind": "pos", "n": n}


def plast():
    return {"kind": "pos", "last": True}


def attr(qname, value):
    return {"kind": "attr", "qname": qname, "value": value}


def seq_(value):
    return {"kind": "str", "value": value}


def exists(*steps):
    return {"kind": "exists", "path": {"steps": list(steps)}}


# --------------------------------------------------------------------------
# Query cases: (sample, ast, expected [(id, value), ...] in document order)
# Expected identities/orders/values are listed explicitly per the task.
# --------------------------------------------------------------------------
CASES = [
    # -- the three-group example: filter-then-last vs last-then-filter
    ("groups.xml",
     {"paths": [{"steps": [st("child", nm("r")), st("child", nm("g")),
                           st("child", nm("item"), [attr("code", "x"), plast()])]}]},
     [(10, "C"), (14, "D"), (18, "E")]),
    ("groups.xml",
     {"paths": [{"steps": [st("child", nm("r")), st("child", nm("g")),
                           st("child", nm("item"), [plast(), attr("code", "x")])]}]},
     [(10, "C"), (14, "D")]),  # g3's last item F is code=y -> dropped
    # -- from C: nearest preceding sibling, then nearest with code=x
    ("groups.xml",
     {"context": {"id": 10},
      "paths": [{"steps": [st("preceding-sibling", nm("item"), [pos(1)])]}]},
     [(7, "B")]),
    ("groups.xml",
     {"context": {"id": 10},
      "paths": [{"steps": [st("preceding-sibling", nm("item"),
                             [attr("code", "x"), pos(1)])]}]},
     [(4, "A")]),
    # -- per-context first child (must NOT merge all parents' children first)
    ("groups.xml",
     {"paths": [{"steps": [st("child", nm("r")), st("child", nm("g")),
                           st("child", nm("item"), [pos(1)])]}]},
     [(4, "A"), (14, "D"), (18, "E")]),
    # -- multi-context reverse axis, merged back to document order
    ("groups.xml",
     {"paths": [{"steps": [st("child", nm("r")), st("child", nm("g")),
                           st("child", nm("item"), [plast()]),
                           st("preceding-sibling", nm("item"), [pos(1)])]}]},
     [(7, "B"), (18, "E")]),
    # -- position()=2, following-sibling, attribute parent/ancestor rules
    ("groups.xml",
     {"paths": [{"steps": [st("child", nm("r")), st("child", nm("g"), [pos(2)])]}]},
     [(13, "D")]),
    ("groups.xml",
     {"context": {"id": 4},
      "paths": [{"steps": [st("following-sibling", nm("item"))]}]},
     [(7, "B"), (10, "C")]),
    ("groups.xml",
     {"context": {"id": 4},
      "paths": [{"steps": [st("following-sibling", nm("item"), [pos(1)])]}]},
     [(7, "B")]),
    ("groups.xml",  # attribute's parent is its element (reached mid-path)
     {"paths": [{"steps": [st("child", nm("r")), st("child", nm("g")),
                           st("child", nm("item"), [pos(1)]),
                           st("attribute", nm("code")),
                           st("parent", STAR)]}]},
     [(4, "A"), (14, "D"), (18, "E")]),
    ("groups.xml",  # attributes have no siblings
     {"paths": [{"steps": [st("child", nm("r")), st("child", nm("g")),
                           st("child", nm("item")), st("attribute", nm("code")),
                           st("following-sibling", NODE)]},
                {"steps": [st("child", nm("r")), st("child", nm("g")),
                           st("child", nm("item")), st("attribute", nm("code")),
                           st("preceding-sibling", NODE)]}]},
     []),
    ("groups.xml",  # ancestor axis merged back into document order
     {"paths": [{"steps": [st("child", nm("r")), st("child", nm("g"), [pos(1)]),
                           st("child", nm("item"), [pos(1)]),
                           st("child", TEXT),
                           st("ancestor", STAR)]}]},
     [(2, "ABCDEF"), (3, "ABC"), (4, "A")]),
    # -- boolean predicates
    ("groups.xml",
     {"paths": [{"steps": [st("child", nm("r")), st("child", nm("g")),
                           st("child", nm("item"),
                              [{"kind": "and", "args": [attr("code", "x"),
                                                        seq_("C")]}])]}]},
     [(10, "C")]),
    ("groups.xml",
     {"paths": [{"steps": [st("child", nm("r")), st("child", nm("g")),
                           st("child", nm("item"),
                              [{"kind": "or", "args": [attr("code", "x"),
                                                       attr("code", "y")]}])]}]},
     [(4, "A"), (7, "B"), (10, "C"), (14, "D"), (18, "E"), (21, "F")]),
    ("groups.xml",
     {"paths": [{"steps": [st("child", nm("r")), st("child", nm("g")),
                           st("child", nm("item"),
                              [{"kind": "not", "arg": attr("code", "x")}])]}]},
     [(7, "B"), (21, "F")]),
    # -- relative-path existence, incl. nested exists and reverse axes inside
    ("groups.xml",
     {"paths": [{"steps": [st("child", nm("r")),
                           st("child", nm("g"),
                              [exists(st("child", nm("item"),
                                         [attr("code", "y")]))])]}]},
     [(3, "ABC"), (17, "EF")]),
    ("groups.xml",
     {"paths": [{"steps": [
         st("child", nm("r")),
         st("child", nm("g"),
            [{"kind": "not",
              "arg": exists(st("child", nm("item"), [attr("code", "y")]))}]),
     ]}]},
     [(13, "D")]),
    ("groups.xml",  # existence is per-candidate: must not see neighbours
     {"paths": [{"steps": [st("child", nm("r")), st("child", nm("g")),
                           st("child", nm("item"),
                              [exists(st("child", TEXT),
                                      )])]}]},
     [(4, "A"), (7, "B"), (10, "C"), (14, "D"), (18, "E"), (21, "F")]),
    ("groups.xml",  # exists with a multi-step relative path
     {"paths": [{"steps": [st("child", nm("r")), st("child", nm("g")),
                           st("child", nm("item"),
                              [exists(st("parent", nm("g")),
                                      )])]}]},
     [(4, "A"), (7, "B"), (10, "C"), (14, "D"), (18, "E"), (21, "F")]),
    # -- union of two paths finding overlapping node sets, dedup by identity
    ("groups.xml",
     {"paths": [{"steps": [st("child", nm("r")), st("child", nm("g")),
                           st("child", nm("item"))]},
                {"steps": [st("descendant", nm("item"))]}]},
     [(4, "A"), (7, "B"), (10, "C"), (14, "D"), (18, "E"), (21, "F")]),
    # -- empty path yields the context node (document); value = string-value
    ("groups.xml", {"paths": [{"steps": []}]}, [(1, "ABCDEF")]),
    # -- document node participates via node(); parent of root is document
    ("groups.xml",
     {"paths": [{"steps": [st("child", nm("r")), st("child", nm("g"), [pos(1)]),
                           st("child", nm("item"), [pos(1)]), st("child", TEXT),
                           st("ancestor", NODE)]}]},
     [(1, "ABCDEF"), (2, "ABCDEF"), (3, "ABC"), (4, "A")]),
    ("groups.xml",
     {"paths": [{"steps": [st("child", nm("r")), st("parent", NODE)]}]},
     [(1, "ABCDEF")]),
    # -- reverse axis with several results: merged back to document order
    ("groups.xml",
     {"paths": [{"steps": [st("child", nm("r")), st("child", nm("g"), [pos(1)]),
                           st("child", nm("item"), [pos(3)]),
                           st("preceding-sibling", nm("item"))]}]},
     [(4, "A"), (7, "B")]),
    # -- descendant text nodes; position beyond the sequence length
    ("groups.xml",
     {"paths": [{"steps": [st("descendant", TEXT)]}]},
     [(6, "A"), (9, "B"), (12, "C"), (16, "D"), (20, "E"), (23, "F")]),
    ("groups.xml",
     {"paths": [{"steps": [st("child", nm("r")), st("child", nm("g"), [pos(9)])]}]},
     []),
    # -- exists with position+attribute inside the relative path
    ("groups.xml",
     {"paths": [{"steps": [
         st("child", nm("r")),
         st("child", nm("g"),
            [exists(st("child", nm("item"), [plast(), attr("code", "y")]))]),
     ]}]},
     [(17, "EF")]),
    # -- string-value equality on attribute nodes
    ("groups.xml",
     {"paths": [{"steps": [st("child", nm("r")), st("child", nm("g")),
                           st("child", nm("item")), st("attribute", nm("code")),
                           ]}]},
     [(5, "x"), (8, "y"), (11, "x"), (15, "x"), (19, "x"), (22, "y")]),
    ("groups.xml",
     {"paths": [{"steps": [st("child", nm("r")), st("child", nm("g")),
                           st("child", nm("item")), st("attribute", nm("code")),
                           ]},
                {"steps": [st("child", nm("r")), st("child", nm("g")),
                           st("child", nm("item")),
                           st("attribute", nm("code"), [seq_("x")])]}]},
     [(5, "x"), (8, "y"), (11, "x"), (15, "x"), (19, "x"), (22, "y")]),
    # -- nested same-name containers
    ("nested.xml",
     {"paths": [{"steps": [st("descendant", nm("box"))]}]},
     [(2, "deep"), (3, "deep"), (4, "deep")]),
    ("nested.xml",
     {"paths": [{"steps": [st("child", nm("box"))]}]},
     [(2, "deep")]),
    ("nested.xml",
     {"context": {"id": 4},
      "paths": [{"steps": [st("ancestor", nm("box"), [pos(1)])]}]},
     [(3, "deep")]),
    ("nested.xml",
     {"context": {"id": 4},
      "paths": [{"steps": [st("ancestor", nm("box"))]}]},
     [(2, "deep"), (3, "deep")]),
    ("nested.xml",
     {"context": {"id": 4},
      "paths": [{"steps": [st("parent", NODE)]}]},
     [(3, "deep")]),
    ("nested.xml",
     {"paths": [{"steps": [st("descendant", nm("box"), [plast()])]}]},
     [(4, "deep")]),
    # -- two prefixes, one URI
    ("ns_two_prefixes.xml",
     {"namespaces": {"m": "urn:u"},
      "paths": [{"steps": [st("child", nm("a")), st("child", nm("m:x"))]}]},
     [(3, ""), (4, "")]),
    ("ns_two_prefixes.xml",  # same query in explicit uri/local form
     {"paths": [{"steps": [st("child", nm("a")),
                           st("child", {"kind": "name", "uri": "urn:u",
                                        "local": "x"})]}]},
     [(3, ""), (4, "")]),
    # -- rebound prefix: expanded names are kept, no local-name fuzz
    ("ns_rebind.xml",
     {"namespaces": {"m": "urn:u1"},
      "paths": [{"steps": [st("descendant", nm("m:x"))]}]},
     [(3, "")]),
    ("ns_rebind.xml",
     {"namespaces": {"m": "urn:u2"},
      "paths": [{"steps": [st("descendant", nm("m:y"))]}]},
     [(5, "")]),
    ("ns_rebind.xml",
     {"namespaces": {"m": "urn:u1"},
      "paths": [{"steps": [st("descendant", nm("m:y"))]}]},
     []),
    # -- default namespace applies to elements, not to unprefixed attributes
    ("ns_default_attr.xml",
     {"paths": [{"steps": [st("child", nm("a"))]}]},  # unprefixed = no ns
     []),
    ("ns_default_attr.xml",
     {"namespaces": {"m": "urn:d"},
      "paths": [{"steps": [st("child", nm("m:a"))]}]},
     [(2, "")]),
    ("ns_default_attr.xml",
     {"context": {"id": 2},
      "paths": [{"steps": [st("attribute", nm("code"))]}]},
     [(3, "1")]),
    ("ns_default_attr.xml",
     {"namespaces": {"m": "urn:d"}, "context": {"id": 2},
      "paths": [{"steps": [st("attribute", nm("m:code"))]}]},
     []),
    ("ns_default_attr.xml",
     {"namespaces": {"m": "urn:d"}, "context": {"id": 2},
      "paths": [{"steps": [st("child", nm("m:b"))]}]},
     [(4, "")]),
    # -- mixed content: text merging, CDATA adjacency, comment/PI splitting
    ("mixed.xml",
     {"context": {"id": 2},
      "paths": [{"steps": [st("child", TEXT)]}]},
     [(3, "lead"), (6, "tailcdmore"), (8, "split"), (10, "end")]),
    ("mixed.xml",
     {"context": {"id": 2},
      "paths": [{"steps": [st("child", NODE)]}]},
     [(3, "lead"), (4, "bold"), (6, "tailcdmore"), (7, "com"),
      (8, "split"), (9, "pdata"), (10, "end")]),
    ("mixed.xml",
     {"paths": [{"steps": [st("child", nm("p"), [seq_("leadboldtailcdmoresplitend")])]}]},
     [(2, "leadboldtailcdmoresplitend")]),
    ("mixed.xml",  # string-value of a child element
     {"paths": [{"steps": [st("child", nm("p")), st("child", nm("b"),
                                                     [seq_("bold")])]}]},
     [(4, "bold")]),
    # -- two paths find the same node; two nodes share one value
    ("dupvalues.xml",
     {"paths": [{"steps": [st("child", nm("d")), st("child", nm("v"), [pos(1)])]},
                {"steps": [st("child", nm("d")), st("child", nm("v"))]}]},
     [(3, "same"), (5, "same")]),
    ("dupvalues.xml",
     {"paths": [{"steps": [st("child", nm("d")), st("child", nm("v")),
                           st("child", TEXT)]}]},
     [(4, "same"), (6, "same")]),
    ("dupvalues.xml",
     {"paths": [{"steps": [st("child", nm("d")), st("child", nm("v")),
                           st("child", TEXT, [seq_("same")])]}]},
     [(4, "same"), (6, "same")]),
    # -- attribute ordering convention: no-ns first (by local), then by URI
    ("attrs.xml",
     {"context": {"id": 2},
      "paths": [{"steps": [st("attribute", STAR)]}]},
     [(3, "2"), (4, "4"), (5, "1"), (6, "3")]),
    ("attrs.xml",  # node() on the attribute axis; text() is empty there
     {"context": {"id": 2},
      "paths": [{"steps": [st("attribute", NODE)]}]},
     [(3, "2"), (4, "4"), (5, "1"), (6, "3")]),
    ("attrs.xml",
     {"context": {"id": 2},
      "paths": [{"steps": [st("attribute", TEXT)]}]},
     []),
    ("attrs.xml",
     {"namespaces": {"m": "urn:p"},
      "paths": [{"steps": [st("child", nm("e"), [attr("m:m", "3")])]}]},
     [(2, "")]),
    ("attrs.xml",  # same predicate in explicit uri/local form
     {"paths": [{"steps": [st("child", nm("e"),
                              [{"kind": "attr", "uri": "urn:p", "local": "m",
                                "value": "3"}])]}]},
     [(2, "")]),
    ("attrs.xml",
     {"namespaces": {"m": "urn:p"},
      "paths": [{"steps": [st("child", nm("e"), [attr("m:m", "9")])]}]},
     []),
    # -- empty-valued attribute vs missing attribute vs non-matching value
    ("attrs.xml",
     {"paths": [{"steps": [st("child", nm("e")), st("child", nm("f"),
                                                     [attr("t", "")])]}]},
     [(8, "")]),
    ("attrs.xml",
     {"paths": [{"steps": [st("child", nm("e")), st("child", nm("f"),
                                                     [attr("nosuch", "")])]}]},
     []),
    ("attrs.xml",
     {"paths": [{"steps": [st("child", nm("e"), [attr("a", "")])]}]},
     []),
    ("attrs.xml",
     {"context": {"id": 8},
      "paths": [{"steps": [st("attribute", STAR)]}]},
     [(9, "")]),
    # -- prolog/epilog comment and PI are children of the document node
    ("prolog.xml",
     {"paths": [{"steps": [st("child", NODE)]}]},
     [(2, "top"), (3, ""), (4, "tdata")]),
    # -- entity and character references resolved by the parser
    ("entities.xml",
     {"context": {"id": 2},
      "paths": [{"steps": [st("child", TEXT)]}]},
     [(3, "a&bA"), (6, "tail")]),
    ("entities.xml",
     {"paths": [{"steps": [st("child", nm("e"), [seq_("a&bAytail")])]}]},
     [(2, "a&bAytail")]),
]


def main():
    keep = "--keep" in sys.argv
    workdir = None
    if "--workdir" in sys.argv:
        workdir = Path(sys.argv[sys.argv.index("--workdir") + 1])
        workdir.mkdir(parents=True, exist_ok=True)
        tmp = workdir
    else:
        tmp = Path(tempfile.mkdtemp(prefix="xmlnodelib-verify-"))
    _CWD[0] = str(tmp)
    print("verify workdir: %s" % tmp)

    refs = {}
    dbs = {}
    try:
        # -------------------------------------------------- import all samples
        for sample in sorted(SAMPLES.glob("*.xml")):
            xml_bytes = sample.read_bytes()
            refs[sample.name] = build_ref(xml_bytes)
            db = tmp / (sample.stem + ".nldb")
            r = do_import(sample, db)
            check(r.returncode == 0, "import %s" % sample.name, r.stderr)
            if r.returncode != 0:
                continue
            dbs[sample.name] = db
            # 注意：之后的所有查询都在新进程中只凭 db 完成，与 XML 文件无关
            #（README 的快速开始演示了导入后把原 XML 移走的场景）。

        # --------------------------------------- full dump vs reference model
        for name, db in sorted(dbs.items()):
            out = tmp / (Path(name).stem + ".dump.jsonl")
            astf = tmp / (Path(name).stem + ".dump.json")
            r = do_query(db, DUMP_AST, out, astf)
            ok = r.returncode == 0
            check(ok, "dump query %s" % name, r.stderr)
            if not ok:
                continue
            records, summary = read_jsonl(out)
            nodes, _children = refs[name]
            expected = ref_dump(nodes)
            check(records == expected,
                  "dump content %s (%d nodes)" % (name, len(expected)),
                  "first mismatch:\n got: %s\n exp: %s" % (
                      next((g for g, e in zip(records, expected) if g != e),
                           records[len(expected):] if len(records) > len(expected)
                           else "<missing>"),
                      next((e for g, e in zip(records, expected) if g != e),
                           expected[len(records):] if len(expected) > len(records)
                           else "<missing>")))
            check(summary == {"summary": True, "total": len(expected),
                              "returned": len(expected), "page_size": None},
                  "dump summary %s" % name, repr(summary))

        # ------------------------------------------------------- query cases
        for i, (sample, ast, expected) in enumerate(CASES, 1):
            db = dbs[sample]
            out = tmp / ("case%02d.jsonl" % i)
            astf = tmp / ("case%02d.json" % i)
            r = do_query(db, ast, out, astf)
            ok = r.returncode == 0
            check(ok, "case %02d runs (%s)" % (i, sample), r.stderr)
            if not ok:
                continue
            records, summary = read_jsonl(out)
            got = [(rec["id"], rec["value"]) for rec in records]
            check(got == expected,
                  "case %02d expected nodes (%s)" % (i, sample),
                  "got: %r\n exp: %r" % (got, expected))
            # cross-check against the independent reference evaluation
            nodes, children = refs[sample]
            ref_got = [(n.id, ref_string_value(nodes, n))
                       for n in ref_eval(nodes, children, ast)]
            check(got == ref_got,
                  "case %02d matches reference model (%s)" % (i, sample),
                  "got: %r\n ref: %r" % (got, ref_got))
            check(summary["total"] == len(expected)
                  and summary["returned"] == len(expected),
                  "case %02d summary counts (%s)" % (i, sample), repr(summary))

        # ------------------------------------ page size: selection unaffected
        out = tmp / "page.jsonl"
        r = do_query(dbs["groups.xml"],
                     {"paths": [{"steps": [st("child", nm("r")),
                                           st("child", nm("g")),
                                           st("child", nm("item"))]}]},
                     out, tmp / "page.json", extra=["--page-size", "2"])
        check(r.returncode == 0, "page-size query runs", r.stderr)
        records, summary = read_jsonl(out)
        check([(rec["id"], rec["value"]) for rec in records]
              == [(4, "A"), (7, "B")]
              and summary["total"] == 6 and summary["returned"] == 2,
              "page-size limits output only, total computed after filtering",
              "records=%r summary=%r" % (records, summary))

        # ----------------------- parser input/value chunk-size invariance
        for name in ("mixed.xml", "entities.xml", "groups.xml", "attrs.xml"):
            db_small = tmp / (Path(name).stem + ".smallchunk.nldb")
            r = do_import(SAMPLES / name, db_small,
                          extra=["--input-chunk", "3", "--value-chunk", "5",
                                 "--batch", "2"])
            check(r.returncode == 0, "small-chunk import %s" % name, r.stderr)
            if r.returncode != 0:
                continue
            out1, out2 = tmp / "c1.jsonl", tmp / "c2.jsonl"
            do_query(dbs[name], DUMP_AST, out1, tmp / "c1.json")
            do_query(db_small, DUMP_AST, out2, tmp / "c2.json")
            rec1, _ = read_jsonl(out1)
            rec2, _ = read_jsonl(out2)
            check(rec1 == rec2,
                  "chunk-size invariance %s (identity, order, values)" % name)

        # ------------------------------------------------------- rejections
        # truncated tail
        bad_xml = tmp / "truncated.xml"
        bad_xml.write_text("<r><a>x</a><b", encoding="utf-8")
        r = do_import(bad_xml, tmp / "truncated.nldb")
        check(r.returncode != 0 and not (tmp / "truncated.nldb").exists(),
              "truncated XML rejected, no library left", r.stderr.strip()[-200:])

        # DOCTYPE (internal subset with entity)
        doctype_xml = tmp / "doctype.xml"
        doctype_xml.write_text('<!DOCTYPE r [<!ENTITY x "y">]><r>&x;</r>',
                               encoding="utf-8")
        r = do_import(doctype_xml, tmp / "doctype.nldb")
        check(r.returncode != 0 and not (tmp / "doctype.nldb").exists(),
              "DOCTYPE rejected, no library left", r.stderr.strip()[-200:])

        # DOCTYPE with external DTD / network reference
        ext_xml = tmp / "extdoctype.xml"
        ext_xml.write_text('<!DOCTYPE r SYSTEM "http://example.com/x.dtd"><r/>',
                           encoding="utf-8")
        r = do_import(ext_xml, tmp / "extdoctype.nldb")
        check(r.returncode != 0 and not (tmp / "extdoctype.nldb").exists(),
              "external DOCTYPE rejected, no library left", r.stderr.strip()[-200:])

        # undefined entity without DTD
        ent_xml = tmp / "undefent.xml"
        ent_xml.write_text("<r>&nosuch;</r>", encoding="utf-8")
        r = do_import(ent_xml, tmp / "undefent.nldb")
        check(r.returncode != 0 and not (tmp / "undefent.nldb").exists(),
              "undefined entity rejected", r.stderr.strip()[-200:])

        # depth limit
        deep_xml = tmp / "deep.xml"
        deep_xml.write_text("<a><b><c/></b></a>", encoding="utf-8")
        r = do_import(deep_xml, tmp / "deep.nldb", extra=["--max-depth", "2"])
        check(r.returncode != 0 and not (tmp / "deep.nldb").exists(),
              "max-depth exceeded rejected", r.stderr.strip()[-200:])

        # refusing to overwrite an existing library
        groups_db = dbs["groups.xml"]
        before = sha256(groups_db)
        r = do_import(SAMPLES / "groups.xml", groups_db)
        check(r.returncode != 0 and sha256(groups_db) == before,
              "existing library not overwritten", r.stderr.strip()[-200:])

        # bad ASTs: each must fail before execution, leave no output, and
        # leave the library byte-identical
        bad_asts = {
            "unknown axis": {"paths": [{"steps": [
                {"axis": "sibling", "test": STAR}]}]},
            "unknown test": {"paths": [{"steps": [
                st("child", {"kind": "wildcard"})]}]},
            "unknown predicate": {"paths": [{"steps": [
                st("child", STAR, [{"kind": "regex", "value": "x"}])]}]},
            "position zero": {"paths": [{"steps": [st("child", STAR, [pos(0)])]}]},
            "position negative": {"paths": [{"steps": [st("child", STAR, [pos(-2)])]}]},
            "pos n and last": {"paths": [{"steps": [
                st("child", STAR, [{"kind": "pos", "n": 1, "last": True}])]}]},
            "union inside exists": {"paths": [{"steps": [
                st("child", STAR, [{"kind": "exists",
                                    "path": {"paths": [{"steps": []}]}}])]}]},
            "steps not a list": {"paths": [{"steps": {}}]},
            "empty paths": {"paths": []},
            "attr value not string": {"paths": [{"steps": [
                st("child", STAR, [{"kind": "attr", "qname": "a", "value": 3}])]}]},
            "empty and": {"paths": [{"steps": [
                st("child", STAR, [{"kind": "and", "args": []}])]}]},
            "pos inside not": {"paths": [{"steps": [
                st("child", STAR, [{"kind": "not", "arg": pos(1)}])]}]},
            "unknown prefix": {"paths": [{"steps": [st("child", nm("zz:item"))]}]},
            "unknown top key": {"paths": [{"steps": []}], "mode": "fast"},
            "bad version": {"version": 2, "paths": [{"steps": []}]},
            "context not found": {"context": {"id": 999999},
                                  "paths": [{"steps": []}]},
            "context not element": {"context": {"id": 6},  # a text node
                                    "paths": [{"steps": []}]},
        }
        for label, ast in sorted(bad_asts.items()):
            out = tmp / ("bad-%s.jsonl" % abs(hash(label)))
            astf = tmp / ("bad-%s.json" % abs(hash(label)))
            r = do_query(groups_db, ast, out, astf)
            check(r.returncode != 0 and not out.exists()
                  and sha256(groups_db) == before,
                  "reject: %s" % label, r.stderr.strip()[-200:])

        # existing output file is never overwritten
        out = tmp / "existing.jsonl"
        out.write_text("precious\n", encoding="utf-8")
        r = do_query(groups_db, {"paths": [{"steps": []}]}, out, tmp / "e.json")
        check(r.returncode != 0 and out.read_text() == "precious\n",
              "existing output not overwritten", r.stderr.strip()[-200:])

        # querying a non-library file fails cleanly
        r = do_query(SAMPLES / "groups.xml", {"paths": [{"steps": []}]},
                     tmp / "notadb.jsonl", tmp / "notadb.json")
        check(r.returncode != 0 and not (tmp / "notadb.jsonl").exists(),
              "non-library file rejected", r.stderr.strip()[-200:])

        # library still fully usable after all the failures
        out = tmp / "after.jsonl"
        r = do_query(groups_db,
                     {"paths": [{"steps": [st("descendant", nm("item"))]}]},
                     out, tmp / "after.json")
        records, summary = read_jsonl(out) if r.returncode == 0 else ([], None)
        check(r.returncode == 0 and len(records) == 6,
              "library intact and queryable after failures", r.stderr)
    finally:
        if keep:
            print("keeping workdir: %s" % tmp)
        elif workdir is None:
            shutil.rmtree(tmp, ignore_errors=True)

    print("\n%d checks, %d failure(s)" % (CHECKS[0], len(FAILURES)))
    if FAILURES:
        for f in FAILURES:
            print("FAILED: %s" % f)
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
