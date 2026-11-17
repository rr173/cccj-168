"""Reconciliation driver: extract, diff on disk, publish atomically.

Pipeline (every step either completes fully or leaves no published result):

1. Open both node libraries read-only (reusing the streaming engine; no XML
   is touched) and record their content identities.
2. Stream every product/variant/field into a temporary *work* SQLite
   database, validating key cardinality (exactly one non-empty key), field
   cardinality (zero or one node) and key uniqueness as we go.  All
   validation problems are collected and the run fails wholesale if any
   exist.
3. Compute the six event kinds with set-oriented SQL entirely inside the
   work database (no Python dictionaries of the catalogs), order them by a
   total key independent of document order, and give them stable ids.
4. Copy the ordered events plus only the old/new values they reference into
   a fresh, standalone result database, validate the copy, and only then
   write ``status='complete'`` and atomically rename it into place.
"""

import hashlib
import os
import sqlite3
import tempfile
from dataclasses import dataclass, field as dc_field
from datetime import datetime, timezone

from ..errors import QueryError, ReconError
from .extract import LibraryReader
from .mapping import Mapping, load_mapping
from .result import EVENT_KINDS, RESULT_SCHEMA_VERSION

_TOOL = "xmlnodelib"
_TOOL_VERSION = "1.0.0"

# Field cardinality states.  'missing' (no node matched) and 'empty' (a node
# matched whose string-value is the empty string) are distinct first-class
# states, so the difference between absent and present-but-empty survives.
STATE_MISSING = "missing"
STATE_EMPTY = "empty"
STATE_VALUE = "value"

_ERROR_CAP = 20  # detailed messages kept per category

WORK_DDL = """
CREATE TABLE products(
  side TEXT NOT NULL, sku TEXT NOT NULL, node_id INTEGER NOT NULL
);
CREATE TABLE variants(
  side TEXT NOT NULL, sku TEXT NOT NULL, code TEXT NOT NULL,
  node_id INTEGER NOT NULL
);
CREATE TABLE p_fields(
  side TEXT NOT NULL, sku TEXT NOT NULL, field TEXT NOT NULL,
  state TEXT NOT NULL, node_id INTEGER, hash TEXT
);
CREATE TABLE v_fields(
  side TEXT NOT NULL, sku TEXT NOT NULL, code TEXT NOT NULL,
  field TEXT NOT NULL, state TEXT NOT NULL, node_id INTEGER, hash TEXT
);
CREATE TABLE raw_chunks(
  side TEXT NOT NULL, node_id INTEGER NOT NULL, seq INTEGER NOT NULL,
  chunk TEXT NOT NULL,
  PRIMARY KEY(side, node_id, seq)
) WITHOUT ROWID;
CREATE TABLE events(
  kind TEXT NOT NULL, sku TEXT NOT NULL, code TEXT, field TEXT,
  old_state TEXT, new_state TEXT,
  old_node_id INTEGER, new_node_id INTEGER
);
"""

#: Built once extraction finishes: they back the set-based cardinality
#: validation (GROUP BY) and every cross-side diff join.
STAGING_INDEX_DDL = """
CREATE INDEX idx_products_side_sku ON products(side, sku);
CREATE INDEX idx_variants_side_sku_code ON variants(side, sku, code);
CREATE INDEX idx_pf_side_sku ON p_fields(side, sku);
CREATE INDEX idx_vf_side_sku_code ON v_fields(side, sku, code);
CREATE INDEX idx_chunks_side_node ON raw_chunks(side, node_id);
"""

_EVENT_CASE = " ".join(
    "WHEN '%s' THEN %d" % (kind, i) for i, kind in enumerate(EVENT_KINDS))


class _ErrorSink:
    """Collects validation failures so one run can report every problem
    instead of failing at the first bad entity."""

    def __init__(self):
        self.messages = []
        self.counts = {}

    def add(self, category, message):
        self.counts[category] = self.counts.get(category, 0) + 1
        if self.counts[category] <= _ERROR_CAP:
            self.messages.append("[%s] %s" % (category, message))

    def __bool__(self):
        return bool(self.counts)

    def render(self):
        lines = ["reconciliation rejected: %d validation problem(s):"
                 % sum(self.counts.values())]
        lines.extend("  " + m for m in self.messages)
        hidden = sum(self.counts.values()) - len(self.messages)
        if hidden > 0:
            lines.append("  ...and %d more (first %d shown)"
                         % (hidden, len(self.messages)))
        return "\n".join(lines)


class _Extractor:
    # Prepared statements (text is constant; parameters bind per row).
    _SQL_INS_PRODUCT = (
        "INSERT INTO products(side, sku, node_id) VALUES (?,?,?)")
    _SQL_INS_VARIANT = (
        "INSERT INTO variants(side, sku, code, node_id) VALUES (?,?,?,?)")
    _SQL_INS_PFIELD = (
        "INSERT INTO p_fields(side, sku, field, state, node_id, hash)"
        " VALUES (?,?,?,?,?,?)")
    _SQL_INS_VFIELD = (
        "INSERT INTO v_fields(side, sku, code, field, state, node_id, hash)"
        " VALUES (?,?,?,?,?,?,?)")
    _SQL_INS_CHUNK = (
        "INSERT OR IGNORE INTO raw_chunks(side, node_id, seq, chunk)"
        " VALUES (?,?,?,?)")
    _SQL_DEL_CHUNK = (
        "DELETE FROM raw_chunks WHERE side=? AND node_id=?")

    def __init__(self, conn, reader, side, mapping, batch):
        self.conn = conn
        self.reader = reader
        self.side = side
        self.mapping = mapping
        self.pmap = mapping.product
        self.vmap = mapping.product.variant
        self.batch = batch
        self.errors = _ErrorSink()
        # All row inserts are buffered into per-table batches; validation
        # SELECTs still run per entity, but the write path never does.
        self.pending = {
            "products": [], "variants": [], "p_fields": [],
            "v_fields": [], "raw_chunks": [],
        }

    # --------------------------------------------------------------- flush
    def _flush_table(self, key, sql):
        rows = self.pending[key]
        if rows:
            self.conn.executemany(sql, rows)
            rows.clear()

    def _flush_all(self):
        self._flush_table("products", self._SQL_INS_PRODUCT)
        self._flush_table("variants", self._SQL_INS_VARIANT)
        self._flush_table("p_fields", self._SQL_INS_PFIELD)
        self._flush_table("v_fields", self._SQL_INS_VFIELD)
        self._flush_table("raw_chunks", self._SQL_INS_CHUNK)

    def _flush_pending_as_needed(self):
        """Keep every staging list bounded: once any table reaches the batch
        size, flush all of them.  This is what keeps peak Python memory
        independent of the catalog size (the whole catalog must never sit in
        a Python list)."""
        if any(len(rows) >= self.batch for rows in self.pending.values()):
            self._flush_all()

    # ----------------------------------------------------------- key fields
    def _key_value(self, owner_row, descriptor, label):
        """Validate a key field: exactly one node with a non-empty value."""
        nodes = self.reader.take_two(owner_row, descriptor)
        if not nodes:
            self.errors.add(
                label + "_key_missing",
                "%s node id=%d: key field %r matched no node"
                % (label, owner_row["id"], descriptor.name))
            return None
        if len(nodes) > 1:
            self.errors.add(
                label + "_key_multi",
                "%s node id=%d: key field %r matched %d nodes (exactly 1 required)"
                % (label, owner_row["id"], descriptor.name, len(nodes)))
            return None
        value = self.reader.read_value(nodes[0])
        if value == "":
            self.errors.add(
                label + "_key_empty",
                "%s node id=%d: key field %r is present but empty"
                % (label, owner_row["id"], descriptor.name))
            return None
        return value

    # ------------------------------------------------------ ordinary fields
    def _store_value_chunks(self, node_id, kind, last_desc):
        """Stream a node's string-value into raw_chunks; return (state, hash).

        State is 'empty' when no non-empty chunk exists, otherwise 'value'.
        Chunks go to disk in batches, so an arbitrarily large field value
        never becomes one Python string.
        """
        digest = hashlib.sha256()
        saw_content = False
        seq = 0
        chunks = self.pending["raw_chunks"]
        for chunk in self.reader.value_chunks_for_id(
                node_id, kind=kind, last_desc=last_desc):
            if chunk:
                saw_content = True
            digest.update(chunk.encode("utf-8"))
            chunks.append((self.side, node_id, seq, chunk))
            seq += 1
            self._flush_pending_as_needed()
        if not saw_content:
            self.conn.execute(self._SQL_DEL_CHUNK, (self.side, node_id))
            return STATE_EMPTY, node_id, None
        return STATE_VALUE, node_id, digest.hexdigest()

    def _extract_field(self, owner_row, descriptor):
        """Return (state, node_id, hash) for one ordinary field, or None when
        the field is invalid (a multi-value match has been recorded)."""
        nodes = self.reader.take_two(owner_row, descriptor)
        if not nodes:
            return STATE_MISSING, None, None
        if len(nodes) > 1:
            self.errors.add(
                "field_multi",
                "node id=%d: field %r matched %d nodes (at most 1 allowed)"
                % (owner_row["id"], descriptor.name, len(nodes)))
            return None
        node = nodes[0]
        return self._store_value_chunks(
            node["id"], node["kind"], node["last_desc"])

    # ------------------------------------------------------------- products
    def run(self):
        # Stream products in bounded batches: every field/variant lookup for
        # a whole batch is resolved through set-based SQL, so the number of
        # cursor round-trips is proportional to the number of batches, not to
        # the number of entities.  Mappings with non-simple steps fall back to
        # the per-entity streaming path (still fully disk-backed).
        batch_owners = []
        for prow in self.reader.products(self.pmap):
            if prow["kind"] != "element":
                self.errors.add(
                    "product_kind",
                    "product selection matched non-element node id=%d (kind=%s)"
                    % (prow["id"], prow["kind"]))
                continue
            batch_owners.append(prow)
            if len(batch_owners) >= self.batch:
                self._process_product_batch(batch_owners)
                batch_owners = []
        if batch_owners:
            self._process_product_batch(batch_owners)

        self._flush_all()
        self._validate_cardinality()
        return self.errors

    def _process_product_batch(self, owners):
        # Resolve the product key for the whole batch up front.
        key_spec = self.reader.simple_field_spec(self.pmap.key)
        if key_spec is None:
            for prow in owners:
                self._process_product_slow(prow)
            return
        key_matches = self.reader.batch_child_matches(owners, *key_spec)
        valid = []
        for prow in owners:
            nodes = key_matches.get(prow["id"], [])
            sku = self._check_key(prow, nodes, self.pmap.key, "product")
            if sku is None:
                continue
            valid.append((prow, sku))
            self.pending["products"].append((self.side, sku, prow["id"]))

        # Ordinary product fields, set-based when each is a simple step.
        for descriptor in self.pmap.fields:
            self._resolve_fields_batch(valid, descriptor, is_variant=False)

        # Variants: resolve the variant sub-elements for the whole batch.
        if self.vmap is None or not valid:
            return
        vpath = self.vmap.path_steps
        if len(vpath) == 1 and not vpath[0].predicates \
                and vpath[0].test.kind == "name" and vpath[0].axis == "child":
            step = vpath[0]
            vrows_by_owner = self.reader.batch_child_matches(
                [p for p, _ in valid], step.axis, step.test.ns, step.test.local,
                limit_per_owner=None)
            all_variants = []
            for prow, sku in valid:
                for vrow in vrows_by_owner.get(prow["id"], []):
                    if vrow["kind"] != "element":
                        self.errors.add(
                            "variant_kind",
                            "sku=%r: variant selection matched non-element node "
                            "id=%d (kind=%s)" % (sku, vrow["id"], vrow["kind"]))
                        continue
                    all_variants.append((vrow, sku))
            self._process_variants_fast(all_variants)
        else:
            for prow, sku in valid:
                for vrow in self.reader.iter_relative(prow, vpath):
                    if vrow["kind"] != "element":
                        self.errors.add(
                            "variant_kind",
                            "sku=%r: variant selection matched non-element node "
                            "id=%d (kind=%s)" % (sku, vrow["id"], vrow["kind"]))
                        continue
                    self._process_variant_slow(vrow, sku)
        # Bound Python memory: hand the batch's staged rows to SQLite now.
        self._flush_pending_as_needed()

    def _process_product_slow(self, prow):
        sku = self._key_value(prow, self.pmap.key, "product")
        if sku is None:
            return
        self.pending["products"].append((self.side, sku, prow["id"]))
        for descriptor in self.pmap.fields:
            got = self._extract_field(prow, descriptor)
            if got is None:
                continue
            state, node_id, digest = got
            self.pending["p_fields"].append(
                (self.side, sku, descriptor.name, state, node_id, digest))
        if self.vmap is not None:
            for vrow in self.reader.iter_relative(prow, self.vmap.path_steps):
                if vrow["kind"] != "element":
                    self.errors.add(
                        "variant_kind",
                        "sku=%r: variant selection matched non-element node "
                        "id=%d (kind=%s)" % (sku, vrow["id"], vrow["kind"]))
                    continue
                self._process_variant_slow(vrow, sku)

    def _check_key(self, owner_row, nodes, descriptor, label):
        if not nodes:
            self.errors.add(
                label + "_key_missing",
                "%s node id=%d: key field %r matched no node"
                % (label, owner_row["id"], descriptor.name))
            return None
        if len(nodes) > 1:
            self.errors.add(
                label + "_key_multi",
                "%s node id=%d: key field %r matched %d nodes (exactly 1 required)"
                % (label, owner_row["id"], descriptor.name, len(nodes)))
            return None
        value = self.reader.read_value(nodes[0])
        if value == "":
            self.errors.add(
                label + "_key_empty",
                "%s node id=%d: key field %r is present but empty"
                % (label, owner_row["id"], descriptor.name))
            return None
        return value

    def _resolve_fields_batch(self, owners, descriptor, *, is_variant):
        """Resolve one ordinary field for many (owner, key) pairs at once.

        ``owners`` is a list of (row, sku) for products or (row, sku, code)
        for variants.  Missing fields are staged as 'missing'; multi-value
        matches are recorded as errors; exactly-one matches stream their
        value to disk.
        """
        spec = self.reader.simple_field_spec(descriptor)
        if spec is None:  # complex path: per-entity fallback
            for item in owners:
                got = self._extract_field(item[0], descriptor)
                if got is None:
                    continue
                self._stage_field(item, descriptor, got, is_variant)
            return
        owner_rows = [item[0] for item in owners]
        matches = self.reader.batch_child_matches(owner_rows, *spec)
        # First pass: classify each owner (missing / multi / single) and
        # collect the single-match nodes for one bulk value resolution.
        singles = []          # (item, node_row)
        for item in owners:
            row = item[0]
            nodes = matches.get(row["id"], [])
            if not nodes:
                self._stage_field(
                    item, descriptor, (STATE_MISSING, None, None), is_variant)
            elif len(nodes) > 1:
                self.errors.add(
                    "field_multi",
                    "node id=%d: field %r matched %d nodes (at most 1 allowed)"
                    % (row["id"], descriptor.name, len(nodes)))
            else:
                singles.append((item, nodes[0]))
        self._resolve_singles_bulk(singles, descriptor, is_variant)

    def _resolve_singles_bulk(self, singles, descriptor, is_variant):
        """Stage values for exactly-one field matches in bulk.

        Leaf nodes (the common case: one text child, or an attribute's own
        value) are hashed and written through set-based reads; anything with
        complex content falls back to subtree streaming.  Either way the
        value lands in raw_chunks without building a Python dict of catalog.
        """
        if not singles:
            return
        by_id = {node["id"]: (item, node) for item, node in singles}
        leaf_rows = self.reader.bulk_leaf_values(list(by_id))
        for lr in leaf_rows:
            item, node = by_id[lr["id"]]
            kind = lr["kind"]
            leaf_fast = (
                kind == "attribute" and lr["own_value"] is not None) or (
                kind in ("element", "document")
                and lr["text_child_count"] == 1 and lr["leaf_text"] is not None)
            if leaf_fast:
                value = lr["own_value"] if kind == "attribute" else lr["leaf_text"]
                got = self._store_leaf_value(node["id"], value)
            else:
                got = self._store_value_chunks(
                    node["id"], node["kind"], node["last_desc"])
            self._stage_field(item, descriptor, got, is_variant)

    def _store_leaf_value(self, node_id, value):
        """Hash and stage a value already read as one string (leaf fast
        path); keeps the same raw_chunks layout as the streaming path."""
        if value == "":
            return STATE_EMPTY, node_id, None
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
        self.pending["raw_chunks"].append((self.side, node_id, 0, value))
        self._flush_pending_as_needed()
        return STATE_VALUE, node_id, digest

    def _stage_field(self, item, descriptor, got, is_variant):
        state, node_id, digest = got
        if is_variant:
            _row, sku, code = item
            self.pending["v_fields"].append(
                (self.side, sku, code, descriptor.name, state, node_id, digest))
        else:
            _row, sku = item
            self.pending["p_fields"].append(
                (self.side, sku, descriptor.name, state, node_id, digest))

    def _process_variants_fast(self, variants):
        """Batched variant key + field resolution.

        ``variants`` is a list of (variant_row, owning_sku).  The variant
        code field is resolved for all of them in set-based batches, then
        each ordinary variant field is likewise resolved per batch.
        """
        key_spec = self.reader.simple_field_spec(self.vmap.key)
        if key_spec is None:
            for vrow, sku in variants:
                self._process_variant_slow(vrow, sku)
            return
        keyed = []
        for start in range(0, len(variants), self.batch):
            chunk = variants[start:start + self.batch]
            rows = [v for v, _ in chunk]
            matches = self.reader.batch_child_matches(rows, *key_spec)
            for vrow, sku in chunk:
                code = self._check_key(
                    vrow, matches.get(vrow["id"], []), self.vmap.key, "variant")
                if code is None:
                    continue
                self.pending["variants"].append(
                    (self.side, sku, code, vrow["id"]))
                keyed.append((vrow, sku, code))
        for descriptor in self.vmap.fields:
            self._resolve_fields_batch(keyed, descriptor, is_variant=True)

    def _process_variant_slow(self, vrow, sku):
        code = self._key_value(vrow, self.vmap.key, "variant")
        if code is None:
            return
        self.pending["variants"].append((self.side, sku, code, vrow["id"]))
        for descriptor in self.vmap.fields:
            got = self._extract_field(vrow, descriptor)
            if got is None:
                continue
            state, node_id, digest = got
            self.pending["v_fields"].append(
                (self.side, sku, code, descriptor.name, state, node_id, digest))

    def _validate_cardinality(self):
        """Set-based key-uniqueness validation over the staged rows.

        Missing/empty/multi-value keys were already caught while streaming;
        this catches duplicate keys (a product SKU, or a variantCode within
        one SKU) using GROUP BY instead of per-row lookups.
        """
        for row in self.conn.execute(
                "SELECT side, sku, COUNT(*) FROM products"
                " GROUP BY side, sku HAVING COUNT(*) > 1 LIMIT ?",
                (_ERROR_CAP,)):
            self.errors.add(
                "product_duplicate",
                "side %r: duplicate SKU %r appears %d times"
                % (row[0], row[1], row[2]))
        for row in self.conn.execute(
                "SELECT side, sku, code, COUNT(*) FROM variants"
                " GROUP BY side, sku, code HAVING COUNT(*) > 1 LIMIT ?",
                (_ERROR_CAP,)):
            self.errors.add(
                "variant_duplicate",
                "side %r sku=%r: duplicate variantCode %r appears %d times"
                % (row[0], row[1], row[2], row[3]))


# ---------------------------------------------------------------- SQL diff
def _generate_events(conn):
    """Compute the six event kinds with set-oriented SQL (disk-backed)."""
    conn.executescript(
        """
        INSERT INTO events(kind, sku, code, field, old_state, new_state,
                           old_node_id, new_node_id)
        SELECT 'product_added', n.sku, NULL, NULL, NULL, 'present',
               NULL, n.node_id
        FROM products n
        WHERE n.side='new'
          AND NOT EXISTS (SELECT 1 FROM products o
                          WHERE o.side='old' AND o.sku=n.sku);

        INSERT INTO events(kind, sku, code, field, old_state, new_state,
                           old_node_id, new_node_id)
        SELECT 'product_removed', o.sku, NULL, NULL, 'present', NULL,
               o.node_id, NULL
        FROM products o
        WHERE o.side='old'
          AND NOT EXISTS (SELECT 1 FROM products n
                          WHERE n.side='new' AND n.sku=o.sku);

        INSERT INTO events(kind, sku, code, field, old_state, new_state,
                           old_node_id, new_node_id)
        SELECT 'product_field_changed', n.sku, NULL, n.field,
               o.state, n.state, o.node_id, n.node_id
        FROM p_fields n
        JOIN p_fields o
          ON o.side='old' AND o.sku=n.sku AND o.field=n.field
        WHERE n.side='new'
          AND EXISTS (SELECT 1 FROM products p WHERE p.side='old' AND p.sku=n.sku)
          AND EXISTS (SELECT 1 FROM products p WHERE p.side='new' AND p.sku=n.sku)
          AND (o.state <> n.state
               OR (o.state='value' AND n.state='value' AND o.hash <> n.hash));

        INSERT INTO events(kind, sku, code, field, old_state, new_state,
                           old_node_id, new_node_id)
        SELECT 'variant_added', n.sku, n.code, NULL, NULL, 'present',
               NULL, n.node_id
        FROM variants n
        WHERE n.side='new'
          AND EXISTS (SELECT 1 FROM products p WHERE p.side='new' AND p.sku=n.sku)
          AND NOT EXISTS (SELECT 1 FROM variants o
                          WHERE o.side='old' AND o.sku=n.sku
                            AND o.code IS n.code);

        INSERT INTO events(kind, sku, code, field, old_state, new_state,
                           old_node_id, new_node_id)
        SELECT 'variant_removed', o.sku, o.code, NULL, 'present', NULL,
               o.node_id, NULL
        FROM variants o
        WHERE o.side='old'
          AND EXISTS (SELECT 1 FROM products p WHERE p.side='old' AND p.sku=o.sku)
          AND NOT EXISTS (SELECT 1 FROM variants n
                          WHERE n.side='new' AND n.sku=o.sku
                            AND n.code IS o.code);

        INSERT INTO events(kind, sku, code, field, old_state, new_state,
                           old_node_id, new_node_id)
        SELECT 'variant_field_changed', n.sku, n.code, n.field,
               o.state, n.state, o.node_id, n.node_id
        FROM v_fields n
        JOIN v_fields o
          ON o.side='old' AND o.sku=n.sku AND o.code IS n.code
         AND o.field=n.field
        WHERE n.side='new'
          AND EXISTS (SELECT 1 FROM variants v
                      WHERE v.side='old' AND v.sku=n.sku AND v.code IS n.code)
          AND EXISTS (SELECT 1 FROM variants v
                      WHERE v.side='new' AND v.sku=n.sku AND v.code IS n.code)
          AND (o.state <> n.state
               OR (o.state='value' AND n.state='value' AND o.hash <> n.hash));

        -- Total, deterministic order independent of input document order.
        CREATE TABLE sorted_events AS
        SELECT kind, sku, code, field, old_state, new_state,
               old_node_id, new_node_id
        FROM events
        ORDER BY CASE kind %s END,
                 sku,
                 code IS NOT NULL, code,
                 field IS NOT NULL, field;
        """ % (" ".join("WHEN '%s' THEN %d" % (k, i)
                        for i, k in enumerate(EVENT_KINDS))))


def _open_work_db(path, cache_kb=8192):
    conn = sqlite3.connect(path)
    # A larger page size costs nothing at small scale and markedly cuts the
    # number of pages touched for the multi-hundred-MB work tables; the page
    # cache stays bounded so peak RSS does not grow with the catalog.
    # mmap is deliberately left off: a memory-mapped window counts toward
    # process RSS and would scale with the work database size.
    conn.execute("PRAGMA page_size=8192")
    conn.execute("PRAGMA journal_mode=MEMORY")
    conn.execute("PRAGMA synchronous=OFF")
    conn.execute("PRAGMA temp_store=1")  # big sorts spill to disk, not RAM
    conn.execute("PRAGMA cache_size=%d" % (-max(2048, cache_kb),))
    conn.execute("PRAGMA mmap_size=0")
    conn.executescript(WORK_DDL)
    return conn


@dataclass
class ReconcileResult:
    result_path: str
    old_identity: dict
    new_identity: dict
    event_counts: dict
    total: int
    mapping: Mapping = dc_field(repr=False)


def reconcile_catalogs(old_db, new_db, mapping_source, result_path, *,
                       batch=2000, cache_kb=8192, scratch_dir=None):
    """Reconcile two complete node libraries and publish a result library.

    ``old_db`` is yesterday's catalog and ``new_db`` today's; both must be
    complete xmlnodelib node libraries (the source XML files are not
    accessed).  ``mapping_source`` is a mapping dict or a mapping JSON path.
    The result is written to a new standalone SQLite file at ``result_path``;
    an existing path is never overwritten.  On any failure (bad mapping,
    incomplete library, missing/duplicate/empty keys, multi-value fields,
    I/O error) no complete result library is left behind.
    """
    if not isinstance(batch, int) or batch < 1:
        raise ReconError("batch must be a positive integer")
    if os.path.exists(result_path):
        raise ReconError(
            "refusing to overwrite existing result library: %s" % result_path)

    mapping = load_mapping(mapping_source)
    result_dir = os.path.dirname(os.path.abspath(result_path)) or "."
    scratch_dir = scratch_dir or result_dir

    work_fd, work_path = tempfile.mkstemp(
        prefix="xmlnodelib-recon-work-", suffix=".db", dir=scratch_dir)
    os.close(work_fd)
    res_fd, res_tmp_path = tempfile.mkstemp(
        prefix="xmlnodelib-recon-result-", suffix=".db", dir=result_dir)
    os.close(res_fd)
    work = None
    try:
        work = _open_work_db(work_path, cache_kb=cache_kb)
        work.execute("BEGIN")
        identities = {}
        all_errors = _ErrorSink()
        for side, db_path in (("old", old_db), ("new", new_db)):
            try:
                reader = LibraryReader(
                    db_path, batch=batch, cache_kb=cache_kb)
            except QueryError as exc:
                raise ReconError("cannot open %s node library: %s"
                                 % (side, exc))
            with reader:
                identities[side] = reader.identity()
                errors = _Extractor(work, reader, side, mapping, batch).run()
                for cat, n in errors.counts.items():
                    all_errors.counts[cat] = all_errors.counts.get(cat, 0) + n
                all_errors.messages.extend(errors.messages)
        if all_errors:
            raise ReconError(all_errors.render())
        work.commit()  # staging complete; indexes/diff run in fresh transactions

        # Build the diff/cardinality indexes only after both sides are fully
        # staged (bulk loading into a heap, then indexing, is far cheaper
        # than maintaining indexes row by row), then gather statistics so
        # the planner picks the cross-side joins well.
        work.executescript(STAGING_INDEX_DDL)
        work.execute("ANALYZE")
        _generate_events(work)
        counts = dict(work.execute(
            "SELECT kind, COUNT(*) FROM sorted_events GROUP BY kind").fetchall())
        event_counts = {kind: counts.get(kind, 0) for kind in EVENT_KINDS}
        total = sum(event_counts.values())
        _validate_work(work, total)
        _publish(work, res_tmp_path, mapping, identities, event_counts, total)
        os.replace(res_tmp_path, result_path)
        res_tmp_path = None
        return ReconcileResult(
            result_path=os.path.abspath(result_path),
            old_identity=identities["old"],
            new_identity=identities["new"],
            event_counts=event_counts, total=total, mapping=mapping)
    finally:
        if work is not None:
            work.close()
        for p in (work_path, res_tmp_path):
            try:
                if p:
                    os.unlink(p)
            except OSError:
                pass


def _validate_work(conn, total):
    """Invariants that must hold before anything is copied to a result."""
    n_sorted = conn.execute("SELECT COUNT(*) FROM sorted_events").fetchone()[0]
    n_events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    if n_sorted != n_events or n_sorted != total:
        raise ReconError("internal error: event staging counts disagree "
                         "(%d vs %d vs %d)" % (n_sorted, n_events, total))
    # Ids assigned by the ordered CREATE TABLE must be contiguous 1..N and
    # still satisfy the total ordering.
    bounds = conn.execute(
        "SELECT COALESCE(MIN(rowid),0), COALESCE(MAX(rowid),0) FROM sorted_events"
    ).fetchone()
    if bounds != (1 if total else 0, total):
        raise ReconError("internal error: event ids are not contiguous")
    bad_order = conn.execute(
        """SELECT COUNT(*) FROM sorted_events a JOIN sorted_events b
           ON a.rowid < b.rowid
           WHERE (CASE a.kind """ + _EVENT_CASE + """ END,
                  a.sku, a.code IS NOT NULL, a.code,
                  a.field IS NOT NULL, a.field)
                 >
                 (CASE b.kind """ + _EVENT_CASE + """ END,
                  b.sku, b.code IS NOT NULL, b.code,
                  b.field IS NOT NULL, b.field)""").fetchone()[0]
    if bad_order:
        raise ReconError("internal error: event ordering is not total")
    # Every referenced value must exist in the chunk table.
    referenced = {
        "old": ("old_node_id", "old_state"),
        "new": ("new_node_id", "new_state"),
    }
    for side, (node_col, state_col) in referenced.items():
        missing = conn.execute(
            "SELECT COUNT(*) FROM sorted_events se"
            " WHERE se." + state_col + " = ? AND se." + node_col + " IS NOT NULL"
            " AND NOT EXISTS (SELECT 1 FROM raw_chunks rc"
            " WHERE rc.side = ? AND rc.node_id = se." + node_col + ")",
            (STATE_VALUE, side)).fetchone()[0]
        if missing:
            raise ReconError(
                "internal error: %d %s-side event value(s) missing from storage"
                % (missing, side))


def _publish(work, res_path, mapping, identities, event_counts, total):
    import json
    from .result import result_schema_sql

    # The work connection owns the whole transaction: attach the empty result
    # file, create its schema through the attached name, copy every row, run
    # the validation queries against the attached tables, and only then write
    # the status='complete' meta row.
    work.execute("ATTACH DATABASE ? AS result", (res_path,))
    try:
        work.execute("PRAGMA result.journal_mode=MEMORY")
        work.execute("PRAGMA result.synchronous=OFF")
        work.executescript(result_schema_sql("result"))

        work.execute(
            "INSERT INTO result.events(id, kind, sku, code, field, old_state,"
            " new_state, old_node_id, new_node_id)"
            " SELECT rowid, kind, sku, code, field, old_state, new_state,"
            " old_node_id, new_node_id FROM sorted_events ORDER BY rowid")

        # Copy only the chunks referenced by actual change events.
        work.execute(
            "INSERT INTO result.event_values(event_id, side, seq, chunk)"
            " SELECT se.rowid, 'old', rc.seq, rc.chunk"
            " FROM sorted_events se"
            " JOIN raw_chunks rc ON rc.side='old' AND rc.node_id=se.old_node_id"
            " WHERE se.old_state='value'")
        work.execute(
            "INSERT INTO result.event_values(event_id, side, seq, chunk)"
            " SELECT se.rowid, 'new', rc.seq, rc.chunk"
            " FROM sorted_events se"
            " JOIN raw_chunks rc ON rc.side='new' AND rc.node_id=se.new_node_id"
            " WHERE se.new_state='value'")

        work.executemany(
            "INSERT INTO result.lib_identity(side, path, schema_version,"
            " node_count, max_depth_seen, created_utc, fingerprint_sha256)"
            " VALUES (?,?,?,?,?,?,?)",
            [(side, ident["path"], ident["schema_version"],
              ident["node_count"], ident["max_depth_seen"],
              ident["created_utc"], ident["fingerprint_sha256"])
             for side, ident in sorted(identities.items())])

        # Validate the copy before declaring the library complete.
        copied = work.execute(
            "SELECT COUNT(*) FROM result.events").fetchone()[0]
        if copied != total:
            raise ReconError("internal error: published event count %d != %d"
                             % (copied, total))
        for side, node_col, state_col in (
                ("old", "old_node_id", "old_state"),
                ("new", "new_node_id", "new_state")):
            # Expected (event, chunk) rows: every value-state event references
            # a node whose chunks are copied once per referencing event, so a
            # node used by several events contributes its chunks several times.
            ref_chunks = work.execute(
                "SELECT COUNT(*) FROM raw_chunks rc"
                " JOIN sorted_events se"
                " ON rc.side=? AND rc.node_id=se." + node_col +
                " WHERE se." + state_col + "='value'",
                (side,)).fetchone()[0]
            got = work.execute(
                "SELECT COUNT(*) FROM result.event_values WHERE side=?",
                (side,)).fetchone()[0]
            if got != ref_chunks:
                raise ReconError(
                    "internal error: lost %s-side value chunks (%d != %d)"
                    % (side, got, ref_chunks))
        if work.execute(
                "PRAGMA result.integrity_check").fetchone()[0] != "ok":
            raise ReconError(
                "internal error: result library integrity check failed")

        meta_rows = [
            ("schema_kind", "xmlnodelib-recon-result"),
            ("schema_version", RESULT_SCHEMA_VERSION),
            ("tool", _TOOL),
            ("tool_version", _TOOL_VERSION),
            ("status", "complete"),  # written last among meta rows, in one txn
            ("created_utc", datetime.now(timezone.utc).isoformat(timespec="seconds")),
            ("event_total", str(total)),
            ("event_counts", json.dumps(event_counts, sort_keys=True)),
            ("mapping_json", mapping.canonical),
            ("old_fingerprint_sha256", identities["old"]["fingerprint_sha256"]),
            ("new_fingerprint_sha256", identities["new"]["fingerprint_sha256"]),
            ("old_source_path", identities["old"]["path"]),
            ("new_source_path", identities["new"]["path"]),
        ]
        work.executemany(
            "INSERT INTO result.meta(key, value) VALUES (?,?)", meta_rows)
        work.commit()
    finally:
        try:
            work.execute("DETACH DATABASE result")
        except sqlite3.Error:
            pass
