#!/usr/bin/env python3
"""Generate a large benchmark XML document and its query ASTs at runtime.

Nothing large is committed to the repository: this script streams the
document to disk in bounded writes.  Layout:

  <cat>
    <item code="x">v0</item><item code="y">v1</item>...   (--items siblings)
    <blob>...--blob-chars characters...</blob>
  </cat>

Query ASTs written next to it:
  q_reverse.json       nearest preceding sibling of the last item
  q_filtered_last.json last item with code="x" (filter, then last())
  q_blob_string.json   the blob element whose string-value equals the full
                       big text (chunked comparison against a huge target)
"""

import argparse
import json
import os

PIECE = "abcdefghij" * 8 + "\n"  # 81 chars, no XML-special characters


def make_blob(blob_chars):
    reps = blob_chars // len(PIECE) + 1
    return (PIECE * reps)[:blob_chars]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--items", type=int, default=200000)
    ap.add_argument("--blob-chars", type=int, default=8 * 1024 * 1024)
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)

    blob = make_blob(args.blob_chars)
    xml_path = os.path.join(args.outdir, "big.xml")
    with open(xml_path, "w", encoding="utf-8", newline="") as fh:
        fh.write("<cat>")
        buf = []
        for i in range(args.items):
            buf.append('<item code="%s">v%d</item>' % ("xy"[i % 2], i))
            if len(buf) >= 4096:
                fh.write("".join(buf))
                buf.clear()
        if buf:
            fh.write("".join(buf))
        fh.write("<blob>")
        fh.write(blob)
        fh.write("</blob></cat>\n")

    def step(axis, qname, preds=None):
        s = {"axis": axis, "test": {"kind": "name", "qname": qname}}
        if preds:
            s["predicates"] = preds
        return s

    pos1 = {"kind": "pos", "n": 1}
    plast = {"kind": "pos", "last": True}
    asts = {
        "q_reverse.json": {"paths": [{"steps": [
            step("child", "cat"),
            step("child", "item", [plast]),
            step("preceding-sibling", "item", [pos1])]}]},
        "q_filtered_last.json": {"paths": [{"steps": [
            step("child", "cat"),
            step("child", "item",
                 [{"kind": "attr", "qname": "code", "value": "x"}, plast])]}]},
        "q_blob_string.json": {"paths": [{"steps": [
            step("child", "cat"),
            step("child", "blob",
                 [{"kind": "str", "value": blob}])]}]},
    }
    for name, ast in asts.items():
        with open(os.path.join(args.outdir, name), "w", encoding="utf-8") as fh:
            json.dump(ast, fh)

    # Facts the benchmark uses to check the query results.
    n = args.items
    with open(os.path.join(args.outdir, "facts.json"), "w", encoding="utf-8") as fh:
        json.dump({
            "items": n,
            "blob_chars": len(blob),
            # items alternate x/y starting with x at index 0
            "last_item_value": "v%d" % (n - 1),
            "prev_item_value": "v%d" % (n - 2),
            "last_x_item_value": "v%d" % (n - 2 if n % 2 == 0 else n - 1),
        }, fh)
    print("generated %s (%d items, blob %d chars)"
          % (xml_path, args.items, len(blob)))


if __name__ == "__main__":
    main()
