"""Streaming XML importer.

Parses the input document with pyexpat in ``input_chunk`` byte blocks and
writes nodes/values to a temporary SQLite file in bounded batches.  Peak
memory is independent of document size: character data is chunked straight
into the ``node_values`` table, so a multi-hundred-MB text node never lives
in Python memory.

Only when the whole document parsed and every row is committed is the
temporary file atomically renamed to the target path, so a failed import
never leaves anything that the normal entry points would accept as a valid
library.
"""

import os
import sqlite3
from datetime import datetime, timezone
from xml.parsers import expat

from . import store
from .errors import ImportRejected, ResourceLimit

#: Separator between namespace URI and local name in expat-reported names.
#: \x01 can never appear in a well-formed XML 1.0 document, so it is safe.
_NS_SEP = "\x01"


def _split_expanded(name):
    """Split an expat expanded name into (namespace URI or None, local)."""
    if _NS_SEP in name:
        ns, local = name.split(_NS_SEP, 1)
        return ns, local
    return None, name


class _Importer:
    def __init__(self, conn, max_depth, batch, value_chunk):
        self.conn = conn
        self.max_depth = max_depth
        self.batch = batch
        self.vchunk = value_chunk
        self.next_id = 0
        self.document_id = None
        self.stack = []                # ids of open elements
        self.nodes_pending = []        # (id, kind, ns, local, parent, depth, last_desc)
        self.values_pending = []       # (node_id, seq, chunk)
        self.updates_pending = []      # (last_desc, id)
        self.open_text = None          # id of the currently open text node
        self.text_seq = 0              # next value-chunk seq of the open text node
        self.text_carry = ""           # incomplete trailing chunk of the open text node
        self.max_depth_seen = 0

    # ------------------------------------------------------------------ rows
    def _new_node(self, kind, ns, local, parent, depth):
        self.next_id += 1
        nid = self.next_id
        self.nodes_pending.append((nid, kind, ns, local, parent, depth, None))
        if len(self.nodes_pending) >= self.batch:
            self._flush_nodes()
        return nid

    def _store_value(self, node_id, value):
        """Store a complete value (attribute/comment/pi), chunked."""
        if not value:
            return
        vc = self.vchunk
        for off in range(0, len(value), vc):
            self.values_pending.append((node_id, off // vc, value[off:off + vc]))
        if len(self.values_pending) >= self.batch:
            self._flush_values()

    def _flush_nodes(self):
        if self.nodes_pending:
            self.conn.executemany(
                "INSERT INTO nodes(id, kind, ns, local, parent, depth, last_desc)"
                " VALUES (?,?,?,?,?,?,?)", self.nodes_pending)
            self.nodes_pending.clear()

    def _flush_values(self):
        if self.values_pending:
            self.conn.executemany(
                "INSERT INTO node_values(node_id, seq, chunk) VALUES (?,?,?)",
                self.values_pending)
            self.values_pending.clear()

    def _flush_updates(self):
        if self.updates_pending:
            self.conn.executemany(
                "UPDATE nodes SET last_desc = ? WHERE id = ?", self.updates_pending)
            self.updates_pending.clear()

    def flush_all(self):
        self._flush_nodes()
        self._flush_values()
        self._flush_updates()

    # ----------------------------------------------------------- text nodes
    def char_data(self, data):
        # Character data outside the root element (prolog/epilog whitespace)
        # is not part of the data model; ignore it.
        if not self.stack:
            return
        if self.open_text is None:
            self.open_text = self._new_node(
                "text", None, None, self.stack[-1], len(self.stack) + 1)
            self.text_seq = 0
            self.text_carry = ""
        buf = self.text_carry + data
        vc = self.vchunk
        nfull = len(buf) // vc
        if nfull:
            for i in range(nfull):
                self.values_pending.append(
                    (self.open_text, self.text_seq, buf[i * vc:(i + 1) * vc]))
                self.text_seq += 1
            buf = buf[nfull * vc:]
            if len(self.values_pending) >= self.batch:
                self._flush_values()
        self.text_carry = buf

    def close_text(self):
        """Close the open logical text node, if any.

        Called on every structural event, so consecutive character-data
        callbacks and adjacent CDATA/plain text merge into one text node,
        while comments, PIs and element boundaries split text nodes.
        """
        if self.open_text is not None:
            if self.text_carry:
                self.values_pending.append(
                    (self.open_text, self.text_seq, self.text_carry))
                self.text_seq += 1
                self.text_carry = ""
            self.open_text = None

    # ------------------------------------------------------------- elements
    def start_element(self, name, attrs):
        self.close_text()
        ns, local = _split_expanded(name)
        depth = len(self.stack) + 1
        if depth > self.max_depth:
            raise ResourceLimit(
                "element depth %d exceeds --max-depth %d" % (depth, self.max_depth))
        if depth > self.max_depth_seen:
            self.max_depth_seen = depth
        parent = self.stack[-1] if self.stack else self.document_id
        eid = self._new_node("element", ns, local, parent, depth)
        # Attributes are assigned ids right after their element, sorted by
        # expanded name (no-namespace first, then URI, then local name).
        expanded = []
        for aname, aval in attrs.items():
            ans, alocal = _split_expanded(aname)
            expanded.append(((ans or "", alocal), ans, alocal, aval))
        expanded.sort(key=lambda item: item[0])
        adepth = depth + 1
        for _key, ans, alocal, aval in expanded:
            aid = self._new_node("attribute", ans, alocal, eid, adepth)
            self._store_value(aid, aval)
        self.stack.append(eid)

    def end_element(self, _name):
        self.close_text()
        eid = self.stack.pop()
        # Ids are sequential, so the subtree's last descendant is the most
        # recently assigned id.
        self.updates_pending.append((self.next_id, eid))
        if len(self.updates_pending) >= self.batch:
            self._flush_updates()

    # ------------------------------------------------------- other events
    def comment(self, data):
        self.close_text()
        parent = self.stack[-1] if self.stack else self.document_id
        cid = self._new_node("comment", None, None, parent, len(self.stack) + 1)
        self._store_value(cid, data)

    def processing_instruction(self, target, data):
        self.close_text()
        parent = self.stack[-1] if self.stack else self.document_id
        pid = self._new_node("pi", None, target, parent, len(self.stack) + 1)
        self._store_value(pid, data)

    def start_doctype(self, _name, _sysid, _pubid, _has_internal):
        raise ImportRejected("DOCTYPE is not supported (no DTD, no entities)")

    def external_entity(self, *args):
        raise ImportRejected("external entities are not supported")


def import_xml(xml_path, db_path, *, max_depth=256, input_chunk=65536,
               batch=5000, value_chunk=262144, cache_kb=8192):
    """Import ``xml_path`` into a new node library at ``db_path``.

    Refuses to overwrite an existing file.  Returns a stats dict.
    Raises XnlError subclasses on any failure; no library file is left behind.
    """
    for name, val in (("max_depth", max_depth), ("input_chunk", input_chunk),
                      ("batch", batch), ("value_chunk", value_chunk),
                      ("cache_kb", cache_kb)):
        if not isinstance(val, int) or val < 1:
            raise ImportRejected("parameter %s must be a positive integer" % name)
    if not os.path.isfile(xml_path):
        raise ImportRejected("input XML not found: %s" % xml_path)
    if os.path.exists(db_path):
        raise ImportRejected("refusing to overwrite existing node library: %s" % db_path)

    tmp_path = "%s.tmp-%d" % (db_path, os.getpid())
    conn = sqlite3.connect(tmp_path)
    try:
        conn.execute("PRAGMA journal_mode=MEMORY")
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute("PRAGMA temp_store=2")  # memory; import has no big sorts
        conn.execute("PRAGMA cache_size=%d" % (-cache_kb,))
        conn.executescript(store.DDL)

        imp = _Importer(conn, max_depth, batch, value_chunk)
        imp.document_id = imp._new_node("document", None, None, None, 0)

        parser = expat.ParserCreate(None, _NS_SEP)
        parser.StartElementHandler = imp.start_element
        parser.EndElementHandler = imp.end_element
        parser.CharacterDataHandler = imp.char_data
        parser.CommentHandler = imp.comment
        parser.ProcessingInstructionHandler = imp.processing_instruction
        parser.StartCdataSectionHandler = lambda: None
        parser.EndCdataSectionHandler = lambda: None
        parser.StartDoctypeDeclHandler = imp.start_doctype
        parser.ExternalEntityRefHandler = imp.external_entity

        try:
            with open(xml_path, "rb") as fh:
                while True:
                    block = fh.read(input_chunk)
                    if not block:
                        break
                    parser.Parse(block, False)
                parser.Parse(b"", True)
        except expat.ExpatError as exc:
            raise ImportRejected(
                "XML parse error at line %d, column %d: %s"
                % (parser.ErrorLineNumber, parser.ErrorColumnNumber, exc)) from exc

        imp.close_text()
        imp.updates_pending.append((imp.next_id, imp.document_id))
        imp.flush_all()

        meta_rows = [
            ("schema_version", store.SCHEMA_VERSION),
            ("status", "complete"),
            ("tool", "xmlnodelib"),
            ("node_count", str(imp.next_id)),
            ("max_depth_seen", str(imp.max_depth_seen)),
            ("created_utc", datetime.now(timezone.utc).isoformat(timespec="seconds")),
        ]
        conn.executemany("INSERT INTO meta(key, value) VALUES (?,?)", meta_rows)
        conn.execute("ANALYZE")
        conn.commit()
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.close()
        os.replace(tmp_path, db_path)
    except BaseException:
        try:
            conn.close()
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise

    return {"nodes": imp.next_id, "max_depth": imp.max_depth_seen, "db": db_path}
