#!/usr/bin/env python3
"""Scalability benchmark for snapshot reconciliation.

At scales 1x (default 200k products, 2 variants each) and 2x (everything
doubled), this tool:

  1. generates both catalog snapshots at runtime (nothing large is committed);
  2. imports each snapshot through the normal xmlnodelib importer;
  3. reconciles the two node libraries into a standalone result library;
  4. runs filtered event queries (by SKU, by event kind, by field prefix)
     against the result library and asserts exact counts against the
     generator's hand-written facts;
  5. reports peak RSS and wall time of each phase and compares 2x/1x ratios.

Every measured phase runs in a fresh wrapper process so
resource.getrusage(RUSAGE_CHILDREN).ru_maxrss reflects exactly one run.
A ratio close to 1 is expected: extraction streams, the diff is set-oriented
SQL, and only the published change events (a small slice of the catalog) are
copied into the result library.

Usage:
  python3 tools/recon_bench.py --workdir /tmp/xnlreconbench
      [--products 200000] [--variants 2] [--rss-threshold 3.0] [--keep]
"""

import argparse
import json
import os
import resource
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GEN = Path(__file__).resolve().with_name("gen_recon_bench.py")


def measure(argv):
    """Run one phase in a fresh wrapper process; return (peak_rss_kb, sec)."""
    wrapper = [sys.executable, os.path.abspath(__file__), "--measure-one"] + argv
    r = subprocess.run(wrapper, capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(r.stdout)
        sys.stderr.write(r.stderr)
        raise SystemExit("measured command failed: " + " ".join(argv))
    return json.loads(r.stdout.strip().splitlines()[-1])


def measure_one(argv):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    t0 = time.monotonic()
    proc = subprocess.run(argv, capture_output=True, text=True, env=env)
    elapsed = time.monotonic() - t0
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        return proc.returncode or 1
    peak = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    print(json.dumps({"rss_kb": peak, "sec": round(elapsed, 3)}))
    return 0


def cli(*args):
    return [sys.executable, "-m", "xmlnodelib", *map(str, args)]


def count_events(result_path):
    """Read counts with bare sqlite3 (no product code in the assertion path)."""
    conn = sqlite3.connect(str(result_path))
    try:
        counts = dict(conn.execute(
            "SELECT kind, COUNT(*) FROM events GROUP BY kind").fetchall())
        total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    finally:
        conn.close()
    return counts, total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--products", type=int, default=200000)
    ap.add_argument("--variants", type=int, default=2)
    ap.add_argument("--rss-threshold", type=float, default=3.0)
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    rows = []
    failures = []

    for scale in (1, 2):
        sdir = workdir / ("scale%d" % scale)
        sdir.mkdir(parents=True, exist_ok=True)
        n_products = args.products * scale
        n_variants = args.variants * scale

        gen = subprocess.run(
            [sys.executable, str(GEN), "--outdir", str(sdir),
             "--products", str(n_products), "--variants", str(n_variants)],
            capture_output=True, text=True)
        if gen.returncode != 0:
            raise SystemExit("generation failed: " + gen.stderr)
        facts = json.loads((sdir / "facts.json").read_text())
        sizes = {name: os.path.getsize(sdir / name)
                 for name in ("catalog-old.xml", "catalog-new.xml")}

        old_db, new_db = sdir / "old.nldb", sdir / "new.nldb"
        for xml_name, db in (("catalog-old.xml", old_db),
                             ("catalog-new.xml", new_db)):
            m = measure(cli("import", "--xml", sdir / xml_name, "--db", db))
            rows.append((scale, "import:" + xml_name, sizes[xml_name],
                         m["rss_kb"], m["sec"]))

        result = sdir / "diff.rrdb"
        m = measure(cli("reconcile", "--old-db", old_db, "--new-db", new_db,
                        "--mapping", sdir / "mapping.json", "--result", result))
        rows.append((scale, "reconcile",
                     sizes["catalog-old.xml"] + sizes["catalog-new.xml"],
                     m["rss_kb"], m["sec"]))

        counts, total = count_events(result)
        for kind, want in facts.items():
            if kind in ("probes", "products", "variants_per_product"):
                continue
            got = counts.get(kind, 0)
            if got != want:
                failures.append("scale%d %s: got %d events, facts say %d"
                                % (scale, kind, got, want))
        if total != sum(v for k, v in facts.items()
                        if k not in ("probes", "products",
                                     "variants_per_product")):
            failures.append("scale%d total %d disagrees with facts"
                            % (scale, total))

        # Filtered query phases, each in a fresh process.  Expected counts
        # are computed from the hand-written facts plus a direct bare-sqlite
        # read of the result library (never through product query code).
        probe = facts["probes"]

        def events_matching(pred):
            conn = sqlite3.connect(str(result))
            try:
                n = conn.execute(
                    "SELECT COUNT(*) FROM events WHERE " + pred[0],
                    pred[1]).fetchone()[0]
            finally:
                conn.close()
            return n

        filters = [
            ("query_sku",
             ["events", "--result", result,
              "--sku", probe["first_added_sku"],
              "--sku", probe["unrelated_stable_sku"],
              "--out", sdir / "q_sku.jsonl"],
             events_matching(
                 ("sku IN (?,?)",
                  [probe["first_added_sku"], probe["unrelated_stable_sku"]]))),
            ("query_kind",
             ["events", "--result", result, "--event", "variant_removed",
              "--out", sdir / "q_kind.jsonl"],
             facts["variant_removed"]),
            ("query_field",
             ["events", "--result", result,
              "--field", "product/price",
              "--out", sdir / "q_field.jsonl"],
             facts["product_field_changed"]),
            ("query_prefix_limit",
             ["events", "--result", result, "--field-prefix", "variant/",
              "--limit", "1000", "--out", sdir / "q_prefix.jsonl"],
             min(1000, facts["variant_field_changed"])),
        ]
        for phase, argv, want_returned in filters:
            m = measure(cli(*argv))
            rows.append((scale, phase,
                         sizes["catalog-old.xml"] + sizes["catalog-new.xml"],
                         m["rss_kb"], m["sec"]))
            out_path = argv[argv.index("--out") + 1]
            lines = Path(out_path).read_text().splitlines()
            body = [json.loads(x) for x in lines[:-1]]
            summary = json.loads(lines[-1])
            if summary["returned"] != want_returned \
                    or len(body) != want_returned:
                failures.append(
                    "scale%d %s: returned %d, expected %d (summary=%r)"
                    % (scale, phase, summary["returned"], want_returned,
                       summary))

        # Provenance/identity and detach behavior on the big result.
        conn = sqlite3.connect(str(result))
        status = conn.execute(
            "SELECT value FROM meta WHERE key='status'").fetchone()[0]
        n_ident = conn.execute("SELECT COUNT(*) FROM lib_identity").fetchone()[0]
        conn.close()
        if status != "complete" or n_ident != 2:
            failures.append("scale%d result meta incomplete" % scale)

    print("\n%-6s %-24s %14s %12s %10s"
          % ("scale", "phase", "input bytes", "peak RSS MiB", "seconds"))
    by_phase = {}
    for scale, phase, in_bytes, rss_kb, sec in rows:
        print("%-6d %-24s %14d %12.1f %10.2f"
              % (scale, phase, in_bytes, rss_kb / 1024.0, sec))
        by_phase.setdefault(phase, {})[scale] = rss_kb

    print("\nRSS ratios (2x / 1x):")
    worst = 0.0
    for phase in sorted(by_phase):
        r = by_phase[phase]
        if 1 in r and 2 in r and r[1] > 0:
            ratio = r[2] / r[1]
            worst = max(worst, ratio)
            print("  %-24s %.2f" % (phase, ratio))

    if worst > args.rss_threshold:
        failures.append("worst RSS ratio %.2f exceeds threshold %.2f"
                        % (worst, args.rss_threshold))
    if not args.keep:
        shutil.rmtree(workdir, ignore_errors=True)
    if failures:
        print("\nBENCH FAILURES:")
        for f in failures:
            print("  " + f)
        return 1
    print("\nRECON BENCH OK (all event counts and filtered queries correct; "
          "worst RSS ratio %.2f <= %.2f)" % (worst, args.rss_threshold))
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--measure-one":
        sys.exit(measure_one(sys.argv[2:]))
    sys.exit(main())
