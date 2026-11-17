"""Standalone reconciliation result libraries.

A published result library is a single SQLite file that is fully
self-contained: the two input node libraries are not needed afterwards.
It holds the ordered events, the old/new values of changed fields (chunked,
so large values are streamed back without a size cap), the two source
library identities and the mapping text.

A library is recognizable as a complete result only when its meta table
contains ``status='complete'``; the status row is written last, after every
event and value has been copied and validated.  Incomplete or foreign files
are refused by :func:`open_result`.
"""

import json
import os
import sqlite3
import urllib.parse
from dataclasses import dataclass

from ..errors import ReconError

RESULT_SCHEMA_VERSION = "1"

#: All event kinds, in the stable output ranking used for event ids.
EVENT_KINDS = (
    "product_added",
    "product_removed",
    "product_field_changed",
    "variant_added",
    "variant_removed",
    "variant_field_changed",
)
_EVENT_RANK = {kind: i for i, kind in enumerate(EVENT_KINDS)}

DDL = """
CREATE TABLE events(
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,
  sku TEXT NOT NULL,
  code TEXT,
  field TEXT,
  old_state TEXT,
  new_state TEXT,
  old_node_id INTEGER,
  new_node_id INTEGER
);
CREATE INDEX idx_events_sku ON events(sku);
CREATE INDEX idx_events_kind ON events(kind);
CREATE INDEX idx_events_field ON events(field);
CREATE TABLE event_values(
  event_id INTEGER NOT NULL,
  side TEXT NOT NULL CHECK(side IN ('old','new')),
  seq INTEGER NOT NULL,
  chunk TEXT NOT NULL,
  PRIMARY KEY(event_id, side, seq)
) WITHOUT ROWID;
CREATE TABLE lib_identity(
  side TEXT PRIMARY KEY CHECK(side IN ('old','new')),
  path TEXT,
  schema_version TEXT,
  node_count INTEGER,
  max_depth_seen INTEGER,
  created_utc TEXT,
  fingerprint_sha256 TEXT
);
CREATE TABLE meta(
  key TEXT PRIMARY KEY,
  value TEXT
);
"""


def result_schema_sql(schema):
    """The result DDL with every object created inside ``schema`` (used when
    the work database creates the result file through ATTACH)."""
    sql = DDL
    replacements = (
        ("CREATE TABLE events", "CREATE TABLE %s.events" % schema),
        ("CREATE INDEX idx_events_sku ON events(sku)",
         "CREATE INDEX %s.idx_events_sku ON events(sku)" % schema),
        ("CREATE INDEX idx_events_kind ON events(kind)",
         "CREATE INDEX %s.idx_events_kind ON events(kind)" % schema),
        ("CREATE INDEX idx_events_field ON events(field)",
         "CREATE INDEX %s.idx_events_field ON events(field)" % schema),
        ("CREATE TABLE event_values", "CREATE TABLE %s.event_values" % schema),
        ("CREATE TABLE lib_identity", "CREATE TABLE %s.lib_identity" % schema),
        ("CREATE TABLE meta", "CREATE TABLE %s.meta" % schema),
    )
    sql = DDL
    for old, new in replacements:
        sql = sql.replace(old, new)
    return sql


def connect_ro(path):
    if not os.path.exists(path):
        raise ReconError("result library not found: %s" % path)
    uri = "file:" + urllib.parse.quote(os.path.abspath(path)) + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


@dataclass(frozen=True)
class EventFilter:
    """Filter over already-generated events.  Every criterion is optional
    and repeatable criteria combine as OR within a category and AND across
    categories."""

    skus: tuple = ()
    kinds: tuple = ()
    fields: tuple = ()
    field_prefix: str = None
    limit: int = None


def _validate_filter(flt):
    if flt.kinds:
        bad = [k for k in flt.kinds if k not in EVENT_KINDS]
        if bad:
            raise ReconError("unknown event kind(s): %s" % ", ".join(bad))
    if flt.skus and not all(isinstance(s, str) and s for s in flt.skus):
        raise ReconError("sku filters must be non-empty strings")
    if flt.fields and not all(isinstance(f, str) and f for f in flt.fields):
        raise ReconError("field filters must be non-empty strings")
    if flt.field_prefix is not None and (
            not isinstance(flt.field_prefix, str) or not flt.field_prefix):
        raise ReconError("field prefix must be a non-empty string when given")
    if flt.limit is not None and (not isinstance(flt.limit, int)
                                  or isinstance(flt.limit, bool) or flt.limit < 0):
        raise ReconError("limit must be a non-negative integer or None")


def _where(flt):
    clauses, params = [], []
    if flt.skus:
        clauses.append("sku IN (%s)" % ",".join("?" * len(flt.skus)))
        params.extend(flt.skus)
    if flt.kinds:
        clauses.append("kind IN (%s)" % ",".join("?" * len(flt.kinds)))
        params.extend(flt.kinds)
    if flt.fields:
        clauses.append("field IN (%s)" % ",".join("?" * len(flt.fields)))
        params.extend(flt.fields)
    if flt.field_prefix is not None:
        clauses.append("field LIKE ? ESCAPE '\\'")
        escaped = flt.field_prefix.replace("\\", "\\\\").replace("%", "\\%") \
            .replace("_", "\\_")
        params.append(escaped + "%")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


class ResultReader:
    """Read/filter/export events from a published result library."""

    def __init__(self, path, conn, meta):
        self.path = path
        self.conn = conn
        self._meta = meta

    # ------------------------------------------------------------ lifecycle
    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------ metadata
    @property
    def mapping_text(self):
        return self._meta.get("mapping_json")

    @property
    def mapping(self):
        return json.loads(self._meta["mapping_json"])

    def identities(self):
        out = {}
        for row in self.conn.execute(
                "SELECT * FROM lib_identity ORDER BY side"):
            out[row["side"]] = dict(row)
        return out

    def event_counts(self):
        return json.loads(self._meta.get("event_counts", "{}"))

    # ------------------------------------------------------------- queries
    def count(self, flt=None):
        flt = flt or EventFilter()
        _validate_filter(flt)
        where, params = _where(flt)
        sql = "SELECT COUNT(*) FROM events" + where
        if flt.limit is not None:
            sql = "SELECT COUNT(*) FROM (%s LIMIT ?)" % sql
            params = params + [flt.limit]
        return self.conn.execute(sql, params).fetchone()[0]

    def _value(self, event_id, side):
        cur = self.conn.execute(
            "SELECT chunk FROM event_values WHERE event_id = ? AND side = ?"
            " ORDER BY seq", (event_id, side))
        try:
            return "".join(r[0] for r in cur)
        finally:
            cur.close()

    def iter_events(self, flt=None):
        """Yield event dicts in stable id order, streaming values chunk by
        chunk.  ``old_value``/``new_value`` are null for missing fields,
        ``""`` for present-but-empty fields, and the concatenated string
        value otherwise; entity add/remove events carry no field values."""
        flt = flt or EventFilter()
        _validate_filter(flt)
        where, params = _where(flt)
        sql = "SELECT * FROM events" + where + " ORDER BY id"
        if flt.limit is not None:
            sql += " LIMIT ?"
            params = params + [flt.limit]
        cur = self.conn.execute(sql, params)
        try:
            for row in cur:
                event = {
                    "event_id": row["id"],
                    "event_kind": row["kind"],
                    "sku": row["sku"],
                    "variant_code": row["code"],
                    "field_path": row["field"],
                    "old_state": row["old_state"],
                    "new_state": row["new_state"],
                    "old_value": None,
                    "new_value": None,
                }
                if row["old_state"] == "value":
                    event["old_value"] = self._value(row["id"], "old")
                elif row["old_state"] == "empty":
                    event["old_value"] = ""
                if row["new_state"] == "value":
                    event["new_value"] = self._value(row["id"], "new")
                elif row["new_state"] == "empty":
                    event["new_value"] = ""
                yield event
        finally:
            cur.close()

    def export_jsonl(self, out_path, flt=None):
        """Write filtered events plus a trailing summary line atomically.
        Refuses to overwrite an existing file."""
        flt = flt or EventFilter()
        _validate_filter(flt)
        if os.path.exists(out_path):
            raise ReconError("refusing to overwrite existing output file: %s"
                             % out_path)
        total = self.count(flt)
        tmp_path = "%s.tmp-%d" % (out_path, os.getpid())
        written = 0
        try:
            with open(tmp_path, "w", encoding="utf-8", newline="\n") as fh:
                for event in self.iter_events(flt):
                    fh.write(json.dumps(event, ensure_ascii=False))
                    fh.write("\n")
                    written += 1
                fh.write(json.dumps({
                    "summary": True,
                    "total": total,
                    "returned": written,
                    "filter": {
                        "skus": list(flt.skus),
                        "event_kinds": list(flt.kinds),
                        "fields": list(flt.fields),
                        "field_prefix": flt.field_prefix,
                        "limit": flt.limit,
                    },
                    "result_library": os.path.abspath(self.path),
                }, ensure_ascii=False))
                fh.write("\n")
            os.replace(tmp_path, out_path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        return {"total": total, "returned": written, "out": out_path}


def open_result(path):
    """Open an existing, complete result library read-only."""
    conn = connect_ro(path)
    try:
        rows = conn.execute("SELECT key, value FROM meta").fetchall()
    except sqlite3.Error as exc:
        conn.close()
        raise ReconError("not a reconciliation result library (no meta table): %s"
                         % exc)
    meta = {r[0]: r[1] for r in rows}
    if meta.get("schema_kind") != "xmlnodelib-recon-result":
        raise ReconError(
            "not a reconciliation result library (schema_kind=%r)"
            % meta.get("schema_kind"))
    if meta.get("schema_version") != RESULT_SCHEMA_VERSION:
        raise ReconError("unsupported result schema version: %r"
                         % meta.get("schema_version"))
    if meta.get("status") != "complete":
        raise ReconError(
            "result library is incomplete (status=%r); refusing to open it"
            % meta.get("status"))
    return ResultReader(path, conn, meta)
