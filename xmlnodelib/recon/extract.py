"""Streaming reads of a node library for reconciliation.

All traversal reuses the query engine's axis machinery (SQLite cursors over
the read-only node library); nothing is materialized into a Python tree.
Two helpers sit on top of the engine primitives:

* :meth:`LibraryReader.iter_relative` evaluates a relative path from one
  context element, per-context, as a lazy generator;
* :meth:`LibraryReader.value_chunks` streams a node's string-value the same
  way the query engine does (descendant text for elements, stored chunks for
  the other kinds), so large field values never become one Python string
  unless a caller explicitly asks for :meth:`read_value`.
"""

import hashlib
import os

from .. import store
from ..engine import _Env, _apply_predicates, _axis_stream, _string_value_chunks
from ..errors import QueryError, ReconError


def _cursor_iter(cur):
    try:
        for row in cur:
            yield row
    finally:
        cur.close()


def _chunk_iter(cur):
    """Yield the single ``chunk`` column of each row as a string."""
    try:
        for (chunk,) in cur:
            yield chunk
    finally:
        cur.close()


def _empty_chunks():
    return
    yield  # pragma: no cover - generator producing no values


def _take_two(stream):
    out = []
    try:
        for row in stream:
            out.append(row)
            if len(out) == 2:
                break
    finally:
        stream.close()
    return out


class LibraryReader:
    """A read-only, streaming view of one complete node library."""

    def __init__(self, db_path, *, batch=2000, cache_kb=8192):
        self.db_path = db_path
        self.batch = batch
        try:
            self.conn = store.connect_ro(db_path)
            self.meta = store.read_meta(self.conn)
        except QueryError:
            raise
        self.conn.execute("PRAGMA cache_size=%d" % (-cache_kb,))
        self.env = _Env(self.conn, batch)
        row = self.conn.execute(
            "SELECT id FROM nodes WHERE kind = 'document'").fetchone()
        if row is None:  # pragma: no cover - defensive; complete libs have one
            raise QueryError("node library has no document node: %s" % db_path)
        self.document_id = row[0]

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------- traversal
    def iter_relative(self, ctx_row, steps, dedupe=False):
        """Yield node rows reached by ``steps`` from ``ctx_row``.

        Each step's per-context sequence is streamed straight from the
        engine's axis cursors.  Union-style duplicate nodes can only arise
        when a reverse axis feeds a later forward step; in that case (and only
        then) identity de-duplication is enabled, mirroring the engine's
        step-merge semantics while keeping forward-only paths fully
        allocation-free.
        """
        if not steps:
            yield ctx_row
            return

        def walk(ctx, depth):
            step = steps[depth]
            stream = _apply_predicates(
                self.env, _axis_stream(self.env, ctx, step.axis, step.test),
                step.predicates)
            last = depth == len(steps) - 1
            try:
                if last and not dedupe:
                    for row in stream:
                        yield row
                else:
                    seen = set() if last else None
                    for row in stream:
                        if last:
                            if row["id"] in seen:
                                continue
                            seen.add(row["id"])
                            yield row
                        else:
                            for sub in walk(row, depth + 1):
                                yield sub
            finally:
                stream.close()

        yield from walk(ctx_row, 0)

    def products(self, product_mapping):
        """Stream all product element rows selected by the mapping."""
        ctx = self.conn.execute(
            "SELECT %s FROM nodes WHERE id = ?" % store.NODE_COLUMNS,
            (self.document_id,)).fetchone()
        return self.iter_relative(ctx, product_mapping.select_steps)

    def field_nodes(self, owner_row, descriptor):
        """Stream the nodes matched by a field's relative path (deduplicated
        by node identity, as the engine would merge a step result)."""
        fast = self._simple_cursor(owner_row, descriptor)
        if fast is not None:
            cur, _sql = fast
            return _cursor_iter(cur)
        return self.iter_relative(owner_row, descriptor.steps, dedupe=True)

    # Fast path for the common mapping shape: a single predicate-free
    # child/attribute/descendant name step.  It resolves through the same
    # store indexes as the engine but without the generic step/generator
    # machinery, and its SQL text is built once per descriptor and cached.
    def _simple_cursor(self, owner_row, descriptor):
        steps = descriptor.steps
        if len(steps) != 1 or steps[0].predicates:
            return None
        step = steps[0]
        test = step.test
        if test.kind != "name":
            return None
        ns_clause = "ns IS NULL" if test.ns is None else "ns = ?"
        if step.axis == "attribute":
            if owner_row["kind"] != "element":
                return None
            sql = ("SELECT %s FROM nodes WHERE parent = ? AND kind='attribute'"
                   " AND %s AND local = ? ORDER BY id"
                   % (store.NODE_COLUMNS, ns_clause))
            params = ((owner_row["id"],) + (() if test.ns is None else (test.ns,))
                      + (test.local,))
        elif step.axis == "child":
            if owner_row["kind"] not in ("document", "element"):
                return None
            sql = ("SELECT %s FROM nodes WHERE parent = ? AND kind='element'"
                   " AND %s AND local = ? ORDER BY id"
                   % (store.NODE_COLUMNS, ns_clause))
            params = ((owner_row["id"],) + (() if test.ns is None else (test.ns,))
                      + (test.local,))
        elif step.axis == "descendant":
            if owner_row["last_desc"] is None:
                return None
            sql = ("SELECT %s FROM nodes WHERE id > ? AND id <= ?"
                   " AND kind='element' AND %s AND local = ? ORDER BY id"
                   % (store.NODE_COLUMNS, ns_clause))
            params = ((owner_row["id"], owner_row["last_desc"])
                      + (() if test.ns is None else (test.ns,))
                      + (test.local,))
        else:
            return None
        return self.conn.execute(sql, params), sql

    def take_two(self, owner_row, descriptor):
        """Return at most the first two matched node rows, without
        materializing the match set."""
        fast = self._simple_cursor(owner_row, descriptor)
        if fast is not None:
            cur, _sql = fast
            try:
                return cur.fetchmany(2)
            finally:
                cur.close()
        return _take_two(self.field_nodes(owner_row, descriptor))

    def simple_field_spec(self, descriptor):
        """If ``descriptor`` is one predicate-free child/attribute/descendant
        name step, return (axis, ns, local); otherwise None."""
        steps = descriptor.steps
        if len(steps) != 1 or steps[0].predicates:
            return None
        step = steps[0]
        if step.test.kind != "name" or step.axis not in (
                "child", "attribute", "descendant"):
            return None
        return step.axis, step.test.ns, step.test.local

    def batch_child_matches(self, owner_rows, axis, ns, local,
                            limit_per_owner=2):
        """Resolve one single-step name field for many owners in grouped,
        bounded SQL round-trips.

        Returns a dict ``owner_id -> list of matched node rows``.  When
        ``limit_per_owner`` is set, only that many matches per owner are
        retained (2 is enough to enforce the at-most-one field-cardinality
        rule); pass ``None`` for a collection path such as variant
        selection, where every match is required.
        """
        result = {}
        owners = list(owner_rows)
        if not owners:
            return result
        kind = "attribute" if axis == "attribute" else "element"
        ns_where = "n.ns IS NULL" if ns is None else "n.ns = ?"
        ns_params = [] if ns is None else [ns]
        cols = store.NODE_COLUMNS.split(", ")
        select_cols = ", ".join("n." + c for c in cols)
        for start in range(0, len(owners), self.batch):
            chunk = owners[start:start + self.batch]
            ids = [o["id"] for o in chunk]
            placeholders = ",".join("?" * len(ids))
            if axis == "descendant":
                # Owner subtree ranges: a node belongs to owner o when
                # o.id < n.id <= o.last_desc.
                sql = (
                    "SELECT %s, o.oid AS owner_id FROM nodes n JOIN ("
                    " SELECT id AS oid, last_desc AS olast FROM nodes"
                    " WHERE id IN (%s)) o ON n.id > o.oid AND n.id <= o.olast"
                    " WHERE n.kind=? AND %s AND n.local=?"
                    " ORDER BY o.oid, n.id" % (
                        select_cols, placeholders, ns_where))
            else:
                sql = (
                    "SELECT %s, n.parent AS owner_id FROM nodes n"
                    " WHERE n.parent IN (%s) AND n.kind=? AND %s AND n.local=?"
                    " ORDER BY n.parent, n.id" % (
                        select_cols, placeholders, ns_where))
            params = ids + [kind] + ns_params + [local]
            cur = self.conn.execute(sql, params)
            try:
                counts = {}
                for row in cur:
                    oid = row["owner_id"]
                    if limit_per_owner is not None:
                        seen = counts.get(oid, 0)
                        if seen >= limit_per_owner:
                            continue
                        counts[oid] = seen + 1
                    result.setdefault(oid, []).append(row)
            finally:
                cur.close()
        return result

    # -------------------------------------------------------------- values
    def value_chunks(self, row):
        """Yield the string-value chunks of ``row`` in document order."""
        return _string_value_chunks(self.env, row)

    def bulk_leaf_values(self, node_ids):
        """Stream ``(id, kind, last_desc, own_value, leaf_text,
        text_child_count)`` for the given element/attribute nodes in a
        bounded number of SQL round-trips.

        An attribute/comment/pi node stores its own value (``own_value``);
        an element is a leaf when it has exactly one text child, whose value
        comes back as ``leaf_text``.  Everything else falls back to general
        subtree streaming by the caller.
        """
        ids = list(node_ids)
        for start in range(0, len(ids), self.batch):
            chunk = ids[start:start + self.batch]
            placeholders = ",".join("?" * len(chunk))
            cur = self.conn.execute(
                "SELECT n.id, n.kind, n.last_desc,"
                " (SELECT v.chunk FROM node_values v WHERE v.node_id = n.id)"
                "  AS own_value,"
                " (SELECT v.chunk FROM node_values v JOIN nodes t"
                "   ON t.id = v.node_id WHERE t.parent = n.id AND t.kind='text'"
                "   ORDER BY t.id LIMIT 1) AS leaf_text,"
                " (SELECT COUNT(*) FROM nodes t WHERE t.parent = n.id"
                "   AND t.kind='text') AS text_child_count"
                " FROM nodes n WHERE n.id IN (%s)" % placeholders, chunk)
            try:
                for r in cur:
                    yield r
            finally:
                cur.close()

    def value_chunks_for_id(self, node_id, kind=None, last_desc=None):
        """Stream a node's string-value by id without first fetching its row.

        Non-element nodes keep their value directly in ``node_values``; an
        element/document node needs its subtree range, which the caller may
        supply to avoid an extra lookup.
        """
        if kind in (None, "element", "document"):
            if last_desc is None:
                row = self.conn.execute(
                    "SELECT kind, last_desc FROM nodes WHERE id=?",
                    (node_id,)).fetchone()
                if row is None:
                    return _empty_chunks()
                if row["kind"] not in ("element", "document"):
                    cur = self.conn.execute(
                        "SELECT chunk FROM node_values WHERE node_id=?"
                        " ORDER BY seq", (node_id,))
                    return _chunk_iter(cur)
                last_desc = row["last_desc"]
            cur = self.conn.execute(
                "SELECT v.chunk FROM nodes n JOIN node_values v"
                " ON v.node_id = n.id WHERE n.kind='text'"
                " AND n.id > ? AND n.id <= ? ORDER BY n.id, v.seq",
                (node_id, last_desc))
            return _chunk_iter(cur)
        cur = self.conn.execute(
            "SELECT chunk FROM node_values WHERE node_id=? ORDER BY seq",
            (node_id,))
        return _chunk_iter(cur)

    def read_value(self, row):
        """Read a complete (small) string-value.  Only used for key values,
        which are required to be non-empty and are expected to be short."""
        return "".join(self.value_chunks(row))

    def hash_value(self, row):
        """Streaming SHA-256 of a node's string-value."""
        digest = hashlib.sha256()
        for chunk in self.value_chunks(row):
            digest.update(chunk.encode("utf-8"))
        return digest.hexdigest()

    # ------------------------------------------------------------ identity
    def fingerprint(self):
        """Content fingerprint of the whole library, computed by streaming.

        Node identity/structure rows (in id order) and every stored value
        chunk (in node id, chunk seq order) are concatenated inside SQLite
        and read back as BLOBs in fixed batches, so the Python side only
        feeds byte strings into the hash.  File location, mtime and import
        timestamps are not part of the fingerprint, so two imports of the
        same XML content compare equal.
        """
        us, rs = chr(31), chr(30)  # unit / record separators
        digest = hashlib.sha256()

        def feed_blob(sql, params):
            cur = self.conn.execute(sql, params)
            try:
                while True:
                    rows = cur.fetchmany(20000)
                    if not rows:
                        break
                    for (blob,) in rows:
                        digest.update(blob)
            finally:
                cur.close()

        feed_blob(
            "SELECT CAST(id || ? || kind || ? || IFNULL(ns, '') || ?"
            " || IFNULL(local, '') || ? || IFNULL(parent, '') || ?"
            " || depth || ? || IFNULL(last_desc, '') || ? AS BLOB)"
            " FROM nodes ORDER BY id",
            (us, us, us, us, us, us, rs))
        feed_blob(
            "SELECT CAST(node_id || ? || seq || ? || chunk || ? AS BLOB)"
            " FROM node_values ORDER BY node_id, seq",
            (us, us, rs))
        return digest.hexdigest()

    def identity(self):
        """Return the provenance/identity record stored in result libraries."""
        return {
            "path": os.path.abspath(self.db_path),
            "schema_version": self.meta.get("schema_version"),
            "node_count": int(self.meta.get("node_count", "0") or 0),
            "max_depth_seen": int(self.meta.get("max_depth_seen", "0") or 0),
            "created_utc": self.meta.get("created_utc"),
            "fingerprint_sha256": self.fingerprint(),
        }


def open_reader(db_path, **kwargs):
    try:
        return LibraryReader(db_path, **kwargs)
    except QueryError as exc:
        raise ReconError(str(exc))
