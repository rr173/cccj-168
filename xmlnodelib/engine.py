"""Query engine: executes a validated AST against a node library.

Execution model (mirrors the XPath 1.0 semantics documented in the README):

* A path is evaluated step by step.  For every step, each input context node
  is processed independently: its axis candidates are produced in axis
  direction (document order for forward axes, reverse document order for
  reverse axes) and pushed through the step's predicate list as a lazy
  stream, so positions and last() always refer to the sequence left by the
  previous predicates *of that same context node*.  Candidate streams are
  SQLite cursors; position filters keep O(1) memory (``position()=n`` stops
  at the n-th survivor, ``position()=last()`` keeps only the most recent
  survivor), so no giant sibling list is ever materialized in Python.
* After a step, the survivors of all context nodes are merged into a
  disk-backed work table, deduplicated by node identity and re-read in
  document order for the next step.
* The union of all paths is deduplicated by identity and exported in
  document order.  ``--page-size`` only limits how many records are written;
  the total match count is computed from the fully evaluated result set.

Intermediate node sets live in a separate scratch SQLite database (the
library itself is opened read-only), and string comparisons stream value
chunks, so neither the candidate sets nor large string-values are held in
memory.
"""

import json
import os
import sys
import tempfile

from . import store
from .astcheck import (And, Ast, AttrEq, Exists, Not, Or, PosPred, StrEq,
                       validate_ast)
from .errors import QueryError

_CONTENT_KINDS_SQL = "('element','text','comment','pi')"
_NODE_COLS_N = ", ".join(
    "n." + c for c in ("id", "kind", "ns", "local", "parent", "depth",
                       "last_desc"))


class _Env:
    __slots__ = ("conn", "batch", "gen")

    def __init__(self, conn, batch):
        self.conn = conn
        self.batch = batch
        self.gen = 0

    def new_gen(self):
        self.gen += 1
        return self.gen


# --------------------------------------------------------------------- axes
def _node_by_id(env, nid):
    return env.conn.execute(
        "SELECT %s FROM nodes WHERE id = ?" % store.NODE_COLUMNS, (nid,)).fetchone()


def _test_matches(row, axis, test):
    """Node test for the fetch-based axes (parent/ancestor)."""
    kind = test.kind
    if kind == "node":
        return True
    if kind == "text":
        return row["kind"] == "text"
    principal = "attribute" if axis == "attribute" else "element"
    if kind == "star":
        return row["kind"] == principal
    return (row["kind"] == principal and row["ns"] == test.ns
            and row["local"] == test.local)


def _test_sql(axis, test):
    """Node test as a SQL WHERE fragment for the cursor-based axes.

    Returns (clause, params), or None for a combination that is always empty
    (text() on the attribute axis).
    """
    if test.kind == "node":
        return "", ()
    if test.kind == "text":
        if axis == "attribute":
            return None
        return " AND kind = 'text'", ()
    principal = "attribute" if axis == "attribute" else "element"
    if test.kind == "star":
        return " AND kind = ?", (principal,)
    if test.ns is None:
        return (" AND kind = ? AND ns IS NULL AND local = ?",
                (principal, test.local))
    return " AND kind = ? AND ns = ? AND local = ?", (principal, test.ns, test.local)


def _axis_sql(ctx, axis, test):
    """SQL for cursor-based axes; (None, ()) means the axis is empty."""
    if axis == "attribute":
        if ctx["kind"] != "element":
            return None, ()
        where = ["parent = ?", "kind = 'attribute'"]
        params = [ctx["id"]]
        order = " ORDER BY ns, local"
    elif axis == "child":
        if ctx["kind"] not in ("document", "element"):
            return None, ()
        where = ["parent = ?", "kind IN %s" % _CONTENT_KINDS_SQL]
        params = [ctx["id"]]
        order = " ORDER BY id"
    elif axis == "descendant":
        if ctx["last_desc"] is None:
            return None, ()
        where = ["id > ?", "id <= ?", "kind IN %s" % _CONTENT_KINDS_SQL]
        params = [ctx["id"], ctx["last_desc"]]
        order = " ORDER BY id"
    elif axis == "following-sibling":
        # Attribute nodes have no siblings; neither does the document node.
        if ctx["kind"] == "attribute" or ctx["parent"] is None:
            return None, ()
        where = ["parent = ?", "id > ?", "kind IN %s" % _CONTENT_KINDS_SQL]
        params = [ctx["parent"], ctx["id"]]
        order = " ORDER BY id"
    elif axis == "preceding-sibling":
        if ctx["kind"] == "attribute" or ctx["parent"] is None:
            return None, ()
        where = ["parent = ?", "id < ?", "kind IN %s" % _CONTENT_KINDS_SQL]
        params = [ctx["parent"], ctx["id"]]
        order = " ORDER BY id DESC"  # reverse axis: nearest first
    else:
        raise AssertionError("not a cursor-based axis: %s" % axis)
    tsql = _test_sql(axis, test)
    if tsql is None:
        return None, ()
    tclause, tparams = tsql
    sql = ("SELECT %s FROM nodes WHERE %s%s%s"
           % (store.NODE_COLUMNS, " AND ".join(where), tclause, order))
    return sql, tuple(params) + tparams


def _sql_stream(env, sql, params):
    cur = env.conn.execute(sql, params)
    try:
        for row in cur:
            yield row
    finally:
        cur.close()


def _empty_stream():
    return
    yield  # pragma: no cover - makes this a generator


def _parent_stream(env, ctx, test):
    try:
        pid = ctx["parent"]
        if pid is not None:
            row = _node_by_id(env, pid)
            if _test_matches(row, "parent", test):
                yield row
    finally:
        pass


def _ancestor_stream(env, ctx, test):
    # Reverse axis: walk the parent chain, nearest ancestor first.  The walk
    # is bounded by the (validated) document depth.
    pid = ctx["parent"]
    while pid is not None:
        row = _node_by_id(env, pid)
        if _test_matches(row, "ancestor", test):
            yield row
        pid = row["parent"]


def _axis_stream(env, ctx, axis, test):
    if axis == "parent":
        return _parent_stream(env, ctx, test)
    if axis == "ancestor":
        return _ancestor_stream(env, ctx, test)
    sql, params = _axis_sql(ctx, axis, test)
    if sql is None:
        return _empty_stream()
    return _sql_stream(env, sql, params)


# ---------------------------------------------------------------- predicates
def _pos_filter(stream, pred):
    try:
        if pred.is_last:
            last = None
            for row in stream:
                last = row
            if last is not None:
                yield last
        else:
            i = 0
            for row in stream:
                i += 1
                if i == pred.n:
                    yield row
                    return
    finally:
        stream.close()


def _bool_filter(env, stream, pred):
    try:
        for row in stream:
            if _eval_bool(env, row, pred):
                yield row
    finally:
        stream.close()


def _apply_predicates(env, stream, predicates):
    for pred in predicates:
        if isinstance(pred, PosPred):
            stream = _pos_filter(stream, pred)
        else:
            stream = _bool_filter(env, stream, pred)
    return stream


def _chunks_equal(chunks, target):
    """Streaming equality of a chunked character sequence with ``target``."""
    pos = 0
    n = len(target)
    for chunk in chunks:
        length = len(chunk)
        if pos + length > n or chunk != target[pos:pos + length]:
            return False
        pos += length
    return pos == n


def _string_value_chunks(env, row):
    """Yield the string-value of a node, chunk by chunk, in document order."""
    kind = row["kind"]
    if kind in ("element", "document"):
        cur = env.conn.execute(
            "SELECT v.chunk FROM nodes n JOIN node_values v ON v.node_id = n.id"
            " WHERE n.kind = 'text' AND n.id > ? AND n.id <= ?"
            " ORDER BY n.id, v.seq", (row["id"], row["last_desc"]))
    else:
        cur = env.conn.execute(
            "SELECT chunk FROM node_values WHERE node_id = ? ORDER BY seq",
            (row["id"],))
    try:
        for (chunk,) in cur:
            yield chunk
    finally:
        cur.close()


def _attr_eq(env, row, pred):
    if row["kind"] != "element":
        return False
    if pred.ns is None:
        attr = env.conn.execute(
            "SELECT id FROM nodes WHERE parent = ? AND kind = 'attribute'"
            " AND ns IS NULL AND local = ?", (row["id"], pred.local)).fetchone()
    else:
        attr = env.conn.execute(
            "SELECT id FROM nodes WHERE parent = ? AND kind = 'attribute'"
            " AND ns = ? AND local = ?", (row["id"], pred.ns, pred.local)).fetchone()
    if attr is None:
        return False  # missing attribute is false even against the empty string
    cur = env.conn.execute(
        "SELECT chunk FROM node_values WHERE node_id = ? ORDER BY seq",
        (attr["id"],))
    try:
        return _chunks_equal((r[0] for r in cur), pred.value)
    finally:
        cur.close()


def _exists(env, ctx_row, steps):
    """Existence of a relative path from ``ctx_row``; short-circuits."""
    if not steps:
        return True
    step = steps[0]
    stream = _apply_predicates(
        env, _axis_stream(env, ctx_row, step.axis, step.test), step.predicates)
    try:
        for row in stream:
            if _exists(env, row, steps[1:]):
                return True
        return False
    finally:
        stream.close()


def _eval_bool(env, row, pred):
    """Boolean predicates combine booleans only; there is no implicit
    number-to-position conversion anywhere in this engine."""
    if isinstance(pred, AttrEq):
        return _attr_eq(env, row, pred)
    if isinstance(pred, StrEq):
        return _chunks_equal(_string_value_chunks(env, row), pred.value)
    if isinstance(pred, Exists):
        return _exists(env, row, pred.steps)
    if isinstance(pred, And):
        for arg in pred.args:
            if not _eval_bool(env, row, arg):
                return False
        return True
    if isinstance(pred, Or):
        for arg in pred.args:
            if _eval_bool(env, row, arg):
                return True
        return False
    if isinstance(pred, Not):
        return not _eval_bool(env, row, pred.arg)
    raise AssertionError("unexpected predicate: %r" % (pred,))


# --------------------------------------------------------------------- steps
def _flush_work(env, pending):
    if pending:
        env.conn.executemany(
            "INSERT OR IGNORE INTO scratch.work(gen, id) VALUES (?,?)", pending)
        pending.clear()


def _eval_step(env, from_gen, step):
    """Evaluate one step for every context node in ``from_gen`` (per-context
    sequences), merge survivors into a fresh generation, return its id."""
    to_gen = env.new_gen()
    ctx_cur = env.conn.execute(
        "SELECT %s FROM scratch.work w JOIN nodes n ON n.id = w.id"
        " WHERE w.gen = ? ORDER BY w.id" % _NODE_COLS_N, (from_gen,))
    pending = []
    try:
        for ctx in ctx_cur:
            stream = _apply_predicates(
                env, _axis_stream(env, ctx, step.axis, step.test), step.predicates)
            try:
                for row in stream:
                    pending.append((to_gen, row["id"]))
                    if len(pending) >= env.batch:
                        _flush_work(env, pending)
            finally:
                stream.close()
    finally:
        ctx_cur.close()
    _flush_work(env, pending)
    return to_gen


# -------------------------------------------------------------------- export
def _write_record(env, fh, row):
    if row["local"] is not None:
        name = {"uri": row["ns"], "local": row["local"]}
    else:
        name = None
    head = {"id": row["id"], "kind": row["kind"], "name": name,
            "parent": row["parent"]}
    fh.write(json.dumps(head, ensure_ascii=False)[:-1])  # strip closing '}'
    fh.write(',"value":"')
    for chunk in _string_value_chunks(env, row):
        fh.write(json.dumps(chunk, ensure_ascii=False)[1:-1])
    fh.write('"}\n')


def _export(env, union_gen, out_path, page_size):
    total = env.conn.execute(
        "SELECT COUNT(*) FROM scratch.work WHERE gen = ?",
        (union_gen,)).fetchone()[0]
    written = 0
    tmp_path = "%s.tmp-%d" % (out_path, os.getpid())
    try:
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as fh:
            cur = env.conn.execute(
                "SELECT %s FROM scratch.work w JOIN nodes n ON n.id = w.id"
                " WHERE w.gen = ? ORDER BY w.id" % _NODE_COLS_N, (union_gen,))
            try:
                for row in cur:
                    if page_size is not None and written >= page_size:
                        break
                    _write_record(env, fh, row)
                    written += 1
            finally:
                cur.close()
            fh.write(json.dumps(
                {"summary": True, "total": total, "returned": written,
                 "page_size": page_size}, ensure_ascii=False))
            fh.write("\n")
        os.replace(tmp_path, out_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return total, written


# -------------------------------------------------------------------- driver
def run_query(db_path, ast_obj, out_path, *, page_size=None, batch=2000,
              cache_kb=8192, scratch_dir=None):
    """Validate ``ast_obj``, execute it against the node library at
    ``db_path`` and stream JSONL records to ``out_path``.

    The library is opened read-only; intermediate sets go to a scratch
    database that is deleted afterwards.  The output file appears atomically
    and only on success.  Returns {"total": ..., "returned": ...}.
    """
    ast = ast_obj if isinstance(ast_obj, Ast) else validate_ast(ast_obj)
    if page_size is not None and (not isinstance(page_size, int)
                                  or isinstance(page_size, bool) or page_size < 0):
        raise QueryError("page_size must be a non-negative integer or None")
    if not isinstance(batch, int) or batch < 1:
        raise QueryError("batch must be a positive integer")
    if os.path.exists(out_path):
        raise QueryError("refusing to overwrite existing output file: %s" % out_path)

    # Relative-path existence checks recurse per step; the AST size limit
    # keeps this bounded.  Raise the interpreter limit enough for any
    # validated AST (and restore it afterwards for embedding callers).
    old_limit = sys.getrecursionlimit()
    if old_limit < 10000:
        sys.setrecursionlimit(10000)

    conn = store.connect_ro(db_path)
    store.read_meta(conn)
    conn.execute("PRAGMA cache_size=%d" % (-cache_kb,))
    conn.execute("PRAGMA temp_store=1")  # file: big sorts must spill to disk

    scratch_fd, scratch_path = tempfile.mkstemp(
        prefix="xmlnodelib-scratch-", suffix=".db", dir=scratch_dir)
    os.close(scratch_fd)
    try:
        conn.execute("ATTACH DATABASE ? AS scratch", (scratch_path,))
        conn.execute("PRAGMA scratch.journal_mode=MEMORY")
        conn.execute("PRAGMA scratch.synchronous=OFF")
        conn.execute(
            "CREATE TABLE scratch.work("
            " gen INTEGER NOT NULL, id INTEGER NOT NULL,"
            " PRIMARY KEY(gen, id)) WITHOUT ROWID")
        env = _Env(conn, batch)

        # Base generation: the single context node (document by default).
        base_gen = env.new_gen()
        if ast.context_id is None:
            row = conn.execute(
                "SELECT id FROM nodes WHERE kind = 'document'").fetchone()
            if row is None:  # pragma: no cover - defensive
                raise QueryError("node library has no document node")
            context_id = row[0]
        else:
            context_id = ast.context_id
            row = conn.execute(
                "SELECT kind FROM nodes WHERE id = ?", (context_id,)).fetchone()
            if row is None:
                raise QueryError("context node %d does not exist" % context_id)
            if row[0] not in ("document", "element"):
                raise QueryError(
                    "context node %d is a %s; the context must be the document "
                    "node or an element node" % (context_id, row[0]))
        conn.execute("INSERT INTO scratch.work(gen, id) VALUES (?,?)",
                     (base_gen, context_id))

        union_gen = env.new_gen()
        for path in ast.paths:
            gen = base_gen
            for step in path.steps:
                gen = _eval_step(env, gen, step)
            conn.execute(
                "INSERT OR IGNORE INTO scratch.work(gen, id)"
                " SELECT ?, id FROM scratch.work WHERE gen = ?",
                (union_gen, gen))

        total, written = _export(env, union_gen, out_path, page_size)
        return {"total": total, "returned": written}
    finally:
        if sys.getrecursionlimit() != old_limit:
            sys.setrecursionlimit(old_limit)
        try:
            conn.close()
        finally:
            try:
                os.unlink(scratch_path)
            except OSError:
                pass
