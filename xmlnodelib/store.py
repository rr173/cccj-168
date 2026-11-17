"""Persistent node-store layout shared by the importer and the query engine.

The node library is a single SQLite file with three tables:

  nodes(id, kind, ns, local, parent, depth, last_desc)
      One row per node. ``id`` is assigned in document order following this
      tool's ordering convention: an element's attributes immediately follow
      the element (sorted by expanded name) and precede its children.
      ``last_desc`` is set on document/element nodes and is the largest id in
      the subtree (inclusive), so subtree ranges are ``(id, last_desc]``.
      ``parent`` of an attribute is its owning element.

  node_values(node_id, seq, chunk)
      Chunked character values for attribute/text/comment/pi nodes.  Element
      and document nodes have no rows here: their string-value is computed by
      streaming their descendant text nodes in document order.

  meta(key, value)
      schema_version / status / statistics.  ``status`` is 'complete' only
      when the import finished successfully; queries refuse anything else.
"""

import os
import sqlite3
import urllib.parse

from .errors import QueryError

SCHEMA_VERSION = "1"

#: Node kinds that appear on the child/descendant/sibling axes.
CONTENT_KINDS = ("element", "text", "comment", "pi")

DDL = """
CREATE TABLE nodes(
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL CHECK(kind IN ('document','element','attribute','text','comment','pi')),
  ns TEXT,
  local TEXT,
  parent INTEGER,
  depth INTEGER NOT NULL,
  last_desc INTEGER
);
CREATE INDEX idx_nodes_parent ON nodes(parent, id);
CREATE INDEX idx_nodes_kind_name ON nodes(kind, ns, local, id);
CREATE TABLE node_values(
  node_id INTEGER NOT NULL,
  seq INTEGER NOT NULL,
  chunk TEXT NOT NULL,
  PRIMARY KEY(node_id, seq)
) WITHOUT ROWID;
CREATE TABLE meta(
  key TEXT PRIMARY KEY,
  value TEXT
);
"""

NODE_COLUMNS = "id, kind, ns, local, parent, depth, last_desc"


def connect_ro(path):
    """Open an existing node library read-only."""
    if not os.path.exists(path):
        raise QueryError("node library not found: %s" % path)
    uri = "file:" + urllib.parse.quote(os.path.abspath(path)) + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def read_meta(conn):
    """Return the meta table as a dict, validating this is a complete library."""
    try:
        rows = conn.execute("SELECT key, value FROM meta").fetchall()
    except sqlite3.Error as exc:
        raise QueryError("not an xmlnodelib node library (no meta table): %s" % exc)
    meta = {}
    for row in rows:
        meta[row[0]] = row[1]
    if meta.get("schema_version") != SCHEMA_VERSION:
        raise QueryError(
            "unsupported node library schema version: %r" % meta.get("schema_version"))
    if meta.get("status") != "complete":
        raise QueryError("node library is incomplete (status=%r); refusing to query it"
                         % meta.get("status"))
    return meta
