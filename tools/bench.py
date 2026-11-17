#!/usr/bin/env python3
"""Scalability benchmark: import + query peak RSS at 1x and 2x input size.

Generates the large document at runtime (see gen_big.py), imports it and
runs the three required query shapes (reverse-axis nearest, filtered last,
parent-element string-value equality over a huge text) at both scales, and
compares peak RSS of each phase.  Every measurement runs the CLI in a fresh
wrapper process so resource.getrusage(RUSAGE_CHILDREN) reflects exactly one
run.

Usage:
  python3 tools/bench.py --workdir /tmp/xnlbench [--items 200000]
      [--blob-mb 8] [--rss-threshold 3.0]

Exit code 0 iff all query results are correct and no RSS ratio exceeds the
threshold (a ratio near 1 is expected: both phases stream).
"""

import argparse
import json
import os
import resource
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GEN_BIG = Path(__file__).resolve().with_name("gen_big.py")


def measure(cmd):
    """Run cmd in a fresh wrapper process; return (peak_rss_kb, seconds)."""
    wrapper = [sys.executable, os.path.abspath(__file__), "--measure-one"] + cmd
    r = subprocess.run(wrapper, capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(r.stdout)
        sys.stderr.write(r.stderr)
        raise SystemExit("measured command failed: %s" % " ".join(cmd))
    return json.loads(r.stdout.strip().splitlines()[-1])


def measure_one(argv):
    t0 = time.monotonic()
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    proc = subprocess.run(argv, capture_output=True, text=True, env=env)
    elapsed = time.monotonic() - t0
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        return proc.returncode or 1
    peak = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    print(json.dumps({"rss_kb": peak, "sec": round(elapsed, 3)}))
    return 0


def cli(*args):
    return [sys.executable, "-m", "xmlnodelib"] + list(args)


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--items", type=int, default=200000,
                    help="sibling <item> count at scale 1 (doubled at scale 2)")
    ap.add_argument("--blob-mb", type=float, default=8.0,
                    help="big-text container size in MiB at scale 1")
    ap.add_argument("--rss-threshold", type=float, default=3.0)
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    blob_chars = int(args.blob_mb * 1024 * 1024)

    rows = []
    failures = []
    for scale in (1, 2):
        sdir = workdir / ("scale%d" % scale)
        sdir.mkdir(parents=True, exist_ok=True)
        items = args.items * scale
        bchars = blob_chars * scale

        gen = subprocess.run(
            [sys.executable, str(GEN_BIG), "--outdir", str(sdir),
             "--items", str(items), "--blob-chars", str(bchars)],
            capture_output=True, text=True)
        if gen.returncode != 0:
            raise SystemExit("generation failed: " + gen.stderr)
        facts = json.loads((sdir / "facts.json").read_text())
        xml_bytes = os.path.getsize(sdir / "big.xml")

        db = sdir / "big.nldb"
        if db.exists():
            db.unlink()
        m = measure(cli("import", "--xml", str(sdir / "big.xml"), "--db", str(db)))
        rows.append((scale, "import", xml_bytes, m["rss_kb"], m["sec"]))

        queries = [
            ("q_reverse", "prev_item_value"),
            ("q_filtered_last", "last_x_item_value"),
            ("q_blob_string", None),
        ]
        for qname, fact_key in queries:
            out = sdir / (qname + ".jsonl")
            if out.exists():
                out.unlink()
            m = measure(cli("query", "--db", str(db),
                            "--ast", str(sdir / (qname + ".json")),
                            "--out", str(out)))
            rows.append((scale, qname, xml_bytes, m["rss_kb"], m["sec"]))
            records, summary = read_jsonl(out)
            if summary["total"] != 1 or len(records) != 1:
                failures.append("scale%d %s: expected exactly 1 node, got %s"
                                % (scale, qname, summary))
                continue
            if fact_key is not None:
                if records[0]["value"] != facts[fact_key]:
                    failures.append("scale%d %s: value %r != expected %r"
                                    % (scale, qname, records[0]["value"],
                                       facts[fact_key]))
            else:
                # string-value equality already ran inside the engine; also
                # verify the exported big value is intact end-to-end.
                if len(records[0]["value"]) != facts["blob_chars"]:
                    failures.append("scale%d %s: exported blob length %d != %d"
                                    % (scale, qname, len(records[0]["value"]),
                                       facts["blob_chars"]))
                if records[0]["kind"] != "element" \
                        or records[0]["name"] != {"uri": None, "local": "blob"}:
                    failures.append("scale%d %s: unexpected node identity"
                                    % (scale, qname))

    print("\n%-6s %-16s %12s %12s %10s" % ("scale", "phase", "input MiB",
                                           "peak RSS MiB", "seconds"))
    by_phase = {}
    for scale, phase, xml_bytes, rss_kb, sec in rows:
        print("%-6d %-16s %12.1f %12.1f %10.2f"
              % (scale, phase, xml_bytes / 2**20, rss_kb / 1024.0, sec))
        by_phase.setdefault(phase, {})[scale] = rss_kb

    print("\nRSS ratios (2x / 1x):")
    worst = 0.0
    for phase in sorted(by_phase):
        r = by_phase[phase]
        if 1 in r and 2 in r and r[1] > 0:
            ratio = r[2] / r[1]
            worst = max(worst, ratio)
            print("  %-16s %.2f" % (phase, ratio))
    ok = not failures and worst <= args.rss_threshold
    if worst > args.rss_threshold:
        failures.append("worst RSS ratio %.2f exceeds threshold %.2f"
                        % (worst, args.rss_threshold))
    if not args.keep:
        import shutil
        shutil.rmtree(workdir, ignore_errors=True)
    if failures:
        print("\nBENCH FAILURES:")
        for f in failures:
            print("  " + f)
        return 1
    print("\nBENCH OK (all query results correct; worst RSS ratio %.2f <= %.2f)"
          % (worst, args.rss_threshold))
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--measure-one":
        sys.exit(measure_one(sys.argv[2:]))
    sys.exit(main())
