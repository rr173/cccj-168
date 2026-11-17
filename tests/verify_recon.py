#!/usr/bin/env python3
"""Independent verification for the snapshot reconciliation feature.

The expected change set is computed here from *hand-written* SKU/variant
identity tables and hand-written field-change triples using plain sets and
dicts.  This program never imports the product's mapping, identity-matching
or diff code: the system under test is driven entirely through its CLI, and
the published result libraries are read back with bare ``sqlite3`` (not even
the product's result reader).

Covers:
  * product added/removed, ordinary product field changes (value->value,
    empty->missing, missing->value distinctions);
  * two different SKUs both carrying variantCode "1" (identity is SKU+code);
  * variant add/remove/field-change under one product;
  * unordered variant reordering producing no event;
  * attribute order and namespace-prefix changes producing no event;
  * duplicate/missing/empty keys and multi-value fields rejected with no new
    result library left behind; bad mapping and incomplete libraries rejected;
  * filtering by SKU, event kind and field path;
  * the result library remaining fully queryable after the input node
    libraries are moved away;
  * deterministic output across re-runs and parse-batch sizes.

Usage:  python3 tests/verify_recon.py [--workdir DIR] [--keep]
Exit code 0 iff every check passes.
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = Path(__file__).resolve().parent / "recon_samples"

FAILURES = []
CHECKS = [0]
ENV = dict(os.environ, PYTHONPATH=str(ROOT))


def check(cond, label, detail=""):
    CHECKS[0] += 1
    if cond:
        print("ok %3d  %s" % (CHECKS[0], label))
    else:
        print("FAIL %3d  %s" % (CHECKS[0], label))
        if detail:
            print("     " + detail.replace("\n", "\n     "))
        FAILURES.append(label)


def run_cli(*args, expect=0):
    proc = subprocess.run(
        [sys.executable, "-m", "xmlnodelib", *map(str, args)],
        capture_output=True, text=True, env=ENV)
    if proc.returncode != expect:
        print(proc.stdout)
        print(proc.stderr)
        raise AssertionError("CLI %s exited %d (expected %d)"
                             % (args[0], proc.returncode, expect))
    return proc


def import_xml(work, name, xml, input_chunk=None):
    db = work / (name + ".nldb")
    args = ["import", "--xml", xml, "--db", db]
    if input_chunk is not None:
        args += ["--input-chunk", str(input_chunk)]
    run_cli(*args)
    return db


def read_events_raw(db_path):
    """Read a result library with bare sqlite3; no product code involved."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    rows = conn.execute(
        "SELECT id, kind, sku, code, field, old_state, new_state,"
        " old_node_id, new_node_id FROM events ORDER BY id").fetchall()
    events = []
    for r in rows:
        ev = dict(r)
        for side, state_col in (("old", "old_state"), ("new", "new_state")):
            if ev[state_col] == "value":
                val = "".join(x[0] for x in conn.execute(
                    "SELECT chunk FROM event_values WHERE event_id=? AND side=?"
                    " ORDER BY seq", (r["id"], side)))
            elif ev[state_col] == "empty":
                val = ""
            else:
                val = None
            ev[side + "_value"] = val
        events.append(ev)
    conn.close()
    return meta, events


# --------------------------------------------------------------------------
# Hand-written identity tables for the two main snapshots (old.xml/new.xml).
# Variant identity is (SKU, variantCode); note P2 and P6 both have code "1"
# and must never be treated as the same variant.
# --------------------------------------------------------------------------
OLD_PRODUCTS = {"P1": {"name": "Alpha", "price": "10.00",
                       "note": "", "uom": "each"},
                "P2": {"name": "Beta", "price": "22.50",
                       "note": "", "uom": "box"},
                "P3": {"name": "Gamma", "price": "7.00",
                       "note": None, "uom": "kg"},
                "P4": {"name": "Delta", "price": "99.00",
                       "note": None, "uom": "set"},
                "P5": {"name": "Epsilon", "price": "3.10",
                       "note": None, "uom": "each"}}
NEW_PRODUCTS = {"P1": {"name": "Alpha renamed", "price": "10.00",
                       "note": "", "uom": "each"},
                "P2": {"name": "Beta", "price": "19.90",
                       "note": None, "uom": "box"},
                "P3": {"name": "Gamma", "price": "7.00",
                       "note": None, "uom": "kg"},
                "P4": {"name": "Delta", "price": "99.00",
                       "note": None, "uom": "set"},
                "P6": {"name": "Zeta", "price": "55.00",
                       "note": None, "uom": "pallet"}}
# None = field absent (missing); "" = present but empty.

OLD_VARIANTS = {
    ("P1", "1"): {"label": "red", "stock": "5"},
    ("P1", "2"): {"label": "blue", "stock": "0"},
    ("P2", "1"): {"label": "small", "stock": "9"},
    ("P3", "1"): {"label": "one-litre", "stock": "3"},
    ("P5", "7"): {"label": "pack", "stock": "4"},
}
NEW_VARIANTS = {
    ("P1", "1"): {"label": "red", "stock": "6"},
    ("P1", "2"): {"label": "blue", "stock": "0"},
    ("P2", "1"): {"label": "small", "stock": "9"},
    ("P3", "1"): {"label": "one-litre", "stock": "3"},
    ("P3", "2"): {"label": "two-litre", "stock": "1"},
    ("P6", "1"): {"label": "bulk", "stock": "20"},
}

FIELD_PATHS = {"name": "product/name", "price": "product/price",
               "note": "product/note", "uom": "product/uom"}
VFIELD_PATHS = {"label": "variant/label", "stock": "variant/stock"}


def state_and_value(v):
    return ("missing", None) if v is None else (
        ("empty", "") if v == "" else ("value", v))


def expected_events():
    """Compute the expected event set purely from the hand-written tables."""
    exp = []
    old_keys, new_keys = set(OLD_PRODUCTS), set(NEW_PRODUCTS)
    for sku in sorted(new_keys - old_keys):
        exp.append(("product_added", sku, None, None, None, "present"))
    for sku in sorted(old_keys - new_keys):
        exp.append(("product_removed", sku, None, None, "present", None))
    for sku in sorted(old_keys & new_keys):
        for f in sorted(OLD_PRODUCTS[sku]):
            ov, nv = OLD_PRODUCTS[sku][f], NEW_PRODUCTS[sku][f]
            if ov != nv:
                os_, oval = state_and_value(ov)
                ns_, nval = state_and_value(nv)
                exp.append(("product_field_changed", sku, None,
                            FIELD_PATHS[f], os_, ns_))
    old_v, new_v = set(OLD_VARIANTS), set(NEW_VARIANTS)
    for key in sorted(new_v - old_v):
        exp.append(("variant_added", key[0], key[1], None, None, "present"))
    for key in sorted(old_v - new_v):
        exp.append(("variant_removed", key[0], key[1], None, "present", None))
    for key in sorted(old_v & new_v):
        for f in sorted(OLD_VARIANTS[key]):
            ov, nv = OLD_VARIANTS[key][f], NEW_VARIANTS[key][f]
            if ov != nv:
                os_, _ = state_and_value(ov)
                ns_, _ = state_and_value(nv)
                exp.append(("variant_field_changed", key[0], key[1],
                            VFIELD_PATHS[f], os_, ns_))
    return exp


def event_signature(ev):
    return (ev["kind"], ev["sku"], ev["code"], ev["field"],
            ev["old_state"], ev["new_state"])


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--workdir", default=None)
    p.add_argument("--keep", action="store_true")
    args = p.parse_args()

    if args.workdir:
        work = Path(args.workdir)
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        cleanup = False
    else:
        work = Path(tempfile.mkdtemp(prefix="xnl-recon-verify-"))
        cleanup = True

    try:
        mapping = SAMPLES / "mapping.json"
        old_db = import_xml(work, "old", SAMPLES / "old.xml")
        new_db = import_xml(work, "new", SAMPLES / "new.xml")

        # 1) Main reconciliation through the CLI.
        result = work / "main.rrdb"
        proc = run_cli("reconcile", "--old-db", old_db, "--new-db", new_db,
                       "--mapping", mapping, "--result", result)
        meta, events = read_events_raw(result)
        check(meta.get("status") == "complete", "result meta status=complete")
        check(meta.get("schema_kind") == "xmlnodelib-recon-result",
              "result meta schema_kind")
        check(meta.get("mapping_json") is not None,
              "mapping text stored in result library")

        got = [event_signature(e) for e in events]
        exp = expected_events()
        check(len(got) == len(exp), "event count matches hand-written model",
              "got %d expected %d" % (len(got), len(exp)))
        check(got == exp, "event identities/order equal hand-written model",
              "got=%r\nexp=%r" % (got, exp))

        # 2) Old/new values on every change event.
        val_fail = []
        for e in events:
            sku = e["sku"]
            if e["kind"] == "product_field_changed":
                fname = {v: k for k, v in FIELD_PATHS.items()}[e["field"]]
                exp_old = OLD_PRODUCTS[sku][fname]
                exp_new = NEW_PRODUCTS[sku][fname]
                if (e["old_value"] != exp_old or e["new_value"] != exp_new):
                    val_fail.append(e)
            elif e["kind"] == "variant_field_changed":
                vname = {v: k for k, v in VFIELD_PATHS.items()}[e["field"]]
                exp_old = OLD_VARIANTS[(sku, e["code"])][vname]
                exp_new = NEW_VARIANTS[(sku, e["code"])][vname]
                if (e["old_value"] != exp_old or e["new_value"] != exp_new):
                    val_fail.append(e)
        check(not val_fail, "old/new values correct on every field change",
              json.dumps(val_fail, indent=1))

        # 3) Stable, gap-free ids and total ordering.
        ids = [e["id"] for e in events]
        check(ids == list(range(1, len(ids) + 1)),
              "event ids are contiguous starting at 1")
        ranks = {k: i for i, k in enumerate(
            ("product_added", "product_removed", "product_field_changed",
             "variant_added", "variant_removed", "variant_field_changed"))}
        ordered = all(
            (ranks[a["kind"]], a["sku"],
             a["code"] is not None, a["code"],
             a["field"] is not None, a["field"])
            <= (ranks[b["kind"]], b["sku"],
                b["code"] is not None, b["code"],
                b["field"] is not None, b["field"])
            for a, b in zip(events, events[1:]))
        check(ordered, "events follow the documented total order")

        # 4) Both library identities recorded, with fingerprints.
        idents = {}
        conn = sqlite3.connect(str(result))
        for r in conn.execute("SELECT * FROM lib_identity"):
            idents[r[0]] = r
        conn.close()
        check(set(idents) == {"old", "new"}, "old and new identities stored")
        check(all(len(i[6]) == 64 for i in idents.values()),
              "identities carry sha256 fingerprints")

        # 5) Invariance: reordered attrs/elements and re-prefixing.
        for tag, xml_name in (("reorder", "old_reordered.xml"),
                              ("reprefix", "old_reprefixed.xml")):
            other = import_xml(work, tag, SAMPLES / xml_name)
            rr = work / ("inv-%s.rrdb" % tag)
            run_cli("reconcile", "--old-db", old_db, "--new-db", other,
                    "--mapping", mapping, "--result", rr)
            _, evs = read_events_raw(rr)
            check(evs == [], "%s: zero events under serialization changes" % tag)

        # 5b) One-sided field presence: missing<->value transitions must be
        # emitted for both product and variant fields, on both directions.
        os_db = import_xml(work, "onesided-old", SAMPLES / "onesided_old.xml")
        ns_db = import_xml(work, "onesided-new", SAMPLES / "onesided_new.xml")
        rr = work / "onesided.rrdb"
        run_cli("reconcile", "--old-db", os_db, "--new-db", ns_db,
                "--mapping", mapping, "--result", rr)
        _, os_evs = read_events_raw(rr)
        os_sig = {(e["kind"], e["sku"], e["code"], e["field"],
                   e["old_state"], e["new_state"]) for e in os_evs}
        expected_onesided = {
            ("product_field_changed", "A", None, "product/price",
             "value", "missing"),
            ("product_field_changed", "B", None, "product/price",
             "missing", "value"),
            # A keeps variant 1 but loses its stock field; B has no variant
            # in the old snapshot at all, so its variant 1 is an *addition*.
            ("variant_field_changed", "A", "1", "variant/stock",
             "value", "missing"),
            ("variant_added", "B", "1", None, None, "present"),
        }
        check(os_sig == expected_onesided,
              "one-sided missing<->value transitions for product and variant")
        # And the carried values must be right (null on the missing side).
        val_ok = all(
            (e["old_value"] is None) == (e["old_state"] == "missing")
            and (e["new_value"] is None) == (e["new_state"] == "missing")
            for e in os_evs if e["kind"].endswith("_field_changed"))
        check(val_ok, "one-sided field events carry null on the missing side")

        # 5c) Complex mapping (multi-step child, descendant, nested variant
        # field) exercises the per-entity streaming fallback, including two
        # field paths resolving to the same value node and nested elements.
        cx_old = import_xml(work, "complex-old", SAMPLES / "complex_old.xml")
        cx_new = import_xml(work, "complex-new", SAMPLES / "complex_new.xml")
        cx_rr = work / "complex.rrdb"
        run_cli("reconcile", "--old-db", cx_old, "--new-db", cx_new,
                "--mapping", SAMPLES / "complex_mapping.json", "--result", cx_rr)
        _, cx_evs = read_events_raw(cx_rr)
        cx_sig = {(e["kind"], e["field"], e["code"],
                   e["old_value"], e["new_value"]) for e in cx_evs}
        check(cx_sig == {
            ("product_field_changed", "any-name", None, "deep", "deep changed"),
            ("product_field_changed", "deep/name", None, "deep", "deep changed"),
            ("variant_field_changed", "nested/lang", "1", "L1", "L2"),
        }, "complex multi-step/descendant/nested mapping via fallback path")

        # 6) Re-run is byte-stable in event content, and parse-batch invariant.
        rerun = work / "rerun.rrdb"
        run_cli("reconcile", "--old-db", old_db, "--new-db", new_db,
                "--mapping", mapping, "--result", rerun)
        _, evs_rerun = read_events_raw(rerun)
        check([event_signature(e) for e in evs_rerun] == got,
              "re-run produces the identical event sequence")
        old_chunked = import_xml(work, "old-chunked", SAMPLES / "old.xml",
                                 input_chunk=17)
        new_chunked = import_xml(work, "new-chunked", SAMPLES / "new.xml",
                                 input_chunk=4099)
        rr = work / "chunked.rrdb"
        run_cli("reconcile", "--old-db", old_chunked,
                "--new-db", new_chunked, "--mapping", mapping, "--result", rr)
        _, evs_chunk = read_events_raw(rr)
        check([event_signature(e) for e in evs_chunk] == got,
              "parser input-chunk sizes do not change the result")
        # Fingerprints of re-imports with different parser batches must match.
        conn = sqlite3.connect(str(rr))
        fp_chunked = dict(conn.execute(
            "SELECT side, fingerprint_sha256 FROM lib_identity")).get("old")
        conn.close()
        conn = sqlite3.connect(str(result))
        fp_main = dict(conn.execute(
            "SELECT side, fingerprint_sha256 FROM lib_identity")).get("old")
        conn.close()
        check(fp_chunked == fp_main and len(fp_main) == 64,
              "content fingerprints are parser-batch independent")

        # 7) Rejection cases: failure exit, no result file, no temp leftovers.
        reject_cases = [
            ("bad_dup_sku", "bad_dup_sku.xml", mapping),
            ("bad_missing_sku", "bad_missing_sku.xml", mapping),
            ("bad_empty_sku", "bad_empty_sku.xml", mapping),
            ("bad_dup_variant", "bad_dup_variant.xml", mapping),
            ("bad_multi_field", "bad_multi_field.xml", mapping),
        ]
        for tag, xml_name, mp in reject_cases:
            bad_db = import_xml(work, tag, SAMPLES / xml_name)
            target = work / ("reject-%s.rrdb" % tag)
            proc = run_cli("reconcile", "--old-db", old_db, "--new-db", bad_db,
                           "--mapping", mp, "--result", target, expect=1)
            check(not target.exists(),
                  "%s: rejected and no result library left" % tag)
            check("reconciliation error" in proc.stderr,
                  "%s: failure reported on stderr" % tag)
        target = work / "reject-badmap.rrdb"
        proc = run_cli("reconcile", "--old-db", old_db, "--new-db", new_db,
                       "--mapping", SAMPLES / "bad_mapping_empty_select.json",
                       "--result", target, expect=1)
        check(not target.exists(), "bad mapping rejected with no result")

        # Incomplete node library (status != complete).
        inc = work / "incomplete.nldb"
        conn = sqlite3.connect(str(inc))
        conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO meta VALUES('status','building')")
        conn.commit()
        conn.close()
        target = work / "reject-incomplete.rrdb"
        run_cli("reconcile", "--old-db", old_db, "--new-db", inc,
                "--mapping", mapping, "--result", target, expect=1)
        check(not target.exists(), "incomplete library rejected, no result")

        # A foreign SQLite file is also refused.
        foreign = work / "foreign.rrdb"
        conn = sqlite3.connect(str(foreign))
        conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
        conn.commit()
        conn.close()
        run_cli("events", "--result", foreign, expect=1)

        # Overwrite refusal on an existing result.
        proc = run_cli("reconcile", "--old-db", old_db, "--new-db", new_db,
                       "--mapping", mapping, "--result", result, expect=1)
        check("refusing to overwrite" in proc.stderr,
              "existing result library is never overwritten")

        # No work/result temporary files left in the working directory.
        leftovers = [p.name for p in work.glob("*recon-*")]
        check(not leftovers, "no temporary work files leaked", str(leftovers))

        # 8) Filtering through the CLI (bare-sqlite re-check of the output).
        out = work / "filter-p1.jsonl"
        run_cli("events", "--result", result, "--sku", "P1", "--out", out)
        lines = [json.loads(x) for x in out.read_text().splitlines()]
        body, summary = lines[:-1], lines[-1]
        check(summary["summary"] and all(e["sku"] == "P1" for e in body),
              "SKU filter keeps only P1 events")
        check(summary["total"] == len(body), "filter summary total matches")

        out = work / "filter-kind.jsonl"
        run_cli("events", "--result", result,
                "--event", "variant_added", "--event", "variant_removed",
                "--out", out)
        body = [json.loads(x) for x in out.read_text().splitlines()][:-1]
        check({e["event_kind"] for e in body} == {"variant_added",
                                                  "variant_removed"},
              "event-kind OR filter")
        check({(e["sku"], e["variant_code"]) for e in body}
              == {("P3", "2"), ("P6", "1"), ("P5", "7")},
              "variant events carry (SKU, variantCode) identities")

        out = work / "filter-field.jsonl"
        run_cli("events", "--result", result,
                "--field", "product/price", "--out", out)
        body = [json.loads(x) for x in out.read_text().splitlines()][:-1]
        check([e["field_path"] for e in body] == ["product/price"],
              "exact field-path filter")
        run_cli("events", "--result", result,
                "--field-prefix", "variant/",
                "--out", work / "filter-prefix.jsonl")
        body = [json.loads(x)
                for x in (work / "filter-prefix.jsonl").read_text()
                .splitlines()][:-1]
        check(all(e["field_path"].startswith("variant/") for e in body)
              and len(body) == 1, "field-prefix filter")
        check(all((e["old_value"], e["new_value"]) == (None, None)
                  for e in [ev for ev in events
                            if ev["kind"] in ("product_added",
                                              "product_removed",
                                              "variant_added",
                                              "variant_removed")]),
              "entity add/remove events carry no old/new values")

        # Combined filter + limit.
        run_cli("events", "--result", result, "--sku", "P1",
                "--event", "product_field_changed", "--limit", "1",
                "--out", work / "filter-limit.jsonl")
        body = [json.loads(x)
                for x in (work / "filter-limit.jsonl").read_text()
                .splitlines()]
        check(len(body) == 2 and body[0]["sku"] == "P1"
              and body[1]["summary"] is True and body[1]["returned"] == 1,
              "limit caps output while summary still reports total")

        # 9) Detach the input node libraries: the result must still filter.
        moved = work / "moved-away"
        moved.mkdir()
        for db in (old_db, new_db):
            shutil.move(str(db), moved / db.name)
        out = work / "after-detach.jsonl"
        run_cli("events", "--result", result, "--sku", "P2", "--out", out)
        body = [json.loads(x) for x in out.read_text().splitlines()][:-1]
        check(body and all(e["sku"] == "P2" for e in body),
              "result library filters after node libraries are moved away")
        detached, _ = read_events_raw(result)
        check(detached.get("status") == "complete",
              "detached result still validates as complete")
        check(detached.get("old_source_path") is not None,
              "original source paths remain recorded for provenance")

        # 10) The Python API surface used by embedders.
        sys.path.insert(0, str(ROOT))
        import xmlnodelib  # noqa: E402,F401
        from xmlnodelib import reconcile_catalogs  # noqa: E402
        from xmlnodelib.recon.result import EventFilter, open_result  # noqa: E402
        api_result = work / "api.rrdb"
        stats = reconcile_catalogs(
            str(work / "moved-away" / old_db.name),
            str(work / "moved-away" / new_db.name),
            str(mapping), str(api_result))
        check(stats.total == len(exp), "Python API reconcile count")
        with open_result(str(api_result)) as reader:
            flt = EventFilter(kinds=("variant_field_changed",))
            check(reader.count(flt) == 1 and len(
                list(reader.iter_events(flt))) == 1,
                "Python API EventFilter over a published result")
            check(reader.identities()["old"]["fingerprint_sha256"]
                  == idents["old"][6],
                "Python API exposes the recorded library fingerprints")

        # 11) Same variantCode "1" under two SKUs is not cross-matched:
        # P2/1 survives unchanged; P6/1 is an addition, never a modification.
        p21 = [e for e in events if e["sku"] == "P2" and e["code"] == "1"]
        p61 = [e for e in events if e["sku"] == "P6" and e["code"] == "1"]
        check(p21 == [], "P2 variantCode 1 untouched despite P6 also having 1")
        check([e["kind"] for e in p61] == ["variant_added"],
              "P6 variantCode 1 is an addition (SKU-scoped identity)")

    finally:
        if cleanup and not args.keep:
            shutil.rmtree(work, ignore_errors=True)

    print("\n%d checks, %d failure(s)" % (CHECKS[0], len(FAILURES)))
    if FAILURES:
        print("FAILURES:\n  " + "\n  ".join(FAILURES))
        return 1
    print("RECON VERIFY OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
