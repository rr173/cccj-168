"""Command line interface: ``python -m xmlnodelib import|query|reconcile|events``.

Exit codes: 0 success; 1 any expected failure (rejected import, invalid AST,
query error, reconciliation failure, overwrite refusal); 2 CLI usage error.
"""

import argparse
import json
import sqlite3
import sys

from . import __version__
from .astcheck import validate_ast
from .engine import run_query
from .errors import AstError, ReconError, XnlError
from .importer import import_xml
from .recon import EVENT_KINDS, reconcile_catalogs
from .recon.result import EventFilter, open_result


def _build_parser():
    ap = argparse.ArgumentParser(
        prog="xmlnodelib",
        description="Offline XML node query library with snapshot "
                    "reconciliation: import XML into persistent node "
                    "libraries, query them without the original XML, and "
                    "reconcile two snapshot libraries into a result library.")
    ap.add_argument("--version", action="version",
                    version="xmlnodelib " + __version__)
    sub = ap.add_subparsers(dest="command", required=True)

    ip = sub.add_parser("import", help="import an XML document into a new node library")
    ip.add_argument("--xml", required=True, help="input XML file (read-only)")
    ip.add_argument("--db", required=True,
                    help="target node library (must not exist; never overwritten)")
    ip.add_argument("--max-depth", type=int, default=256,
                    help="maximum element nesting depth (default: 256)")
    ip.add_argument("--input-chunk", type=int, default=65536,
                    help="bytes fed to the parser per read (default: 65536)")
    ip.add_argument("--value-chunk", type=int, default=262144,
                    help="characters per stored value chunk (default: 262144)")
    ip.add_argument("--batch", type=int, default=5000,
                    help="rows per SQLite insert batch (default: 5000)")
    ip.add_argument("--cache-kb", type=int, default=8192,
                    help="SQLite page cache in KiB (default: 8192)")

    qp = sub.add_parser("query", help="run a JSON path AST against a node library")
    qp.add_argument("--db", required=True, help="existing node library (opened read-only)")
    qp.add_argument("--ast", required=True, help="JSON file with the path AST")
    qp.add_argument("--out", required=True,
                    help="output JSONL file (must not exist; written atomically)")
    qp.add_argument("--page-size", type=int, default=None,
                    help="maximum number of records to write (default: all); the "
                         "total match count is always computed after full evaluation")
    qp.add_argument("--batch", type=int, default=2000,
                    help="rows per work-table insert batch (default: 2000)")
    qp.add_argument("--cache-kb", type=int, default=8192,
                    help="SQLite page cache in KiB (default: 8192)")
    qp.add_argument("--scratch-dir", default=None,
                    help="directory for the disk-backed scratch database "
                         "(default: system temp dir)")

    rp = sub.add_parser(
        "reconcile",
        help="reconcile two complete node libraries into a standalone result library")
    rp.add_argument("--old-db", required=True,
                    help="older snapshot node library (e.g. yesterday)")
    rp.add_argument("--new-db", required=True,
                    help="newer snapshot node library (e.g. today)")
    rp.add_argument("--mapping", required=True,
                    help="mapping JSON file (product/variant selection and fields)")
    rp.add_argument("--result", required=True,
                    help="target result library (must not exist; written atomically)")
    rp.add_argument("--batch", type=int, default=2000,
                    help="rows per SQLite insert batch (default: 2000)")
    rp.add_argument("--cache-kb", type=int, default=8192,
                    help="SQLite page cache in KiB (default: 8192)")
    rp.add_argument("--scratch-dir", default=None,
                    help="directory for disk-backed work databases "
                         "(default: next to the result library)")

    ep = sub.add_parser(
        "events",
        help="filter/export events from a published result library "
             "(the input node libraries are not needed)")
    ep.add_argument("--result", required=True, help="published result library")
    ep.add_argument("--out", default=None,
                    help="output JSONL file (must not exist; default: stdout)")
    ep.add_argument("--sku", action="append", default=[], metavar="SKU",
                    help="keep events for this SKU (repeatable; OR within the filter)")
    ep.add_argument("--event", action="append", default=[], choices=EVENT_KINDS,
                    help="keep this event kind (repeatable; OR within the filter)")
    ep.add_argument("--field", action="append", default=[], metavar="FIELD_PATH",
                    help="keep events whose field path equals this (repeatable)")
    ep.add_argument("--field-prefix", default=None,
                    help="keep events whose field path starts with this prefix")
    ep.add_argument("--limit", type=int, default=None,
                    help="maximum number of events to write (default: all)")
    return ap


def _write_events_stdout(reader, flt):
    total = reader.count(flt)
    written = 0
    for event in reader.iter_events(flt):
        sys.stdout.write(json.dumps(event, ensure_ascii=False))
        sys.stdout.write("\n")
        written += 1
    sys.stdout.write(json.dumps({
        "summary": True, "total": total, "returned": written,
        "result_library": reader.path,
    }, ensure_ascii=False))
    sys.stdout.write("\n")
    return {"total": total, "returned": written}


def main(argv=None):
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "import":
            stats = import_xml(
                args.xml, args.db, max_depth=args.max_depth,
                input_chunk=args.input_chunk, batch=args.batch,
                value_chunk=args.value_chunk, cache_kb=args.cache_kb)
            print("imported %d nodes (max depth %d) -> %s"
                  % (stats["nodes"], stats["max_depth"], stats["db"]),
                  file=sys.stderr)
            return 0

        if args.command == "query":
            try:
                with open(args.ast, "r", encoding="utf-8") as fh:
                    ast_obj = json.load(fh)
            except (OSError, json.JSONDecodeError) as exc:
                raise AstError("cannot read AST JSON %s: %s" % (args.ast, exc))
            stats = run_query(
                args.db, ast_obj, args.out, page_size=args.page_size,
                batch=args.batch, cache_kb=args.cache_kb,
                scratch_dir=args.scratch_dir)
            print("query matched %d node(s), wrote %d record(s) -> %s"
                  % (stats["total"], stats["returned"], args.out), file=sys.stderr)
            return 0

        if args.command == "reconcile":
            stats = reconcile_catalogs(
                args.old_db, args.new_db, args.mapping, args.result,
                batch=args.batch, cache_kb=args.cache_kb,
                scratch_dir=args.scratch_dir)
            print("reconciled -> %s" % stats.result_path, file=sys.stderr)
            for kind in EVENT_KINDS:
                print("  %-22s %d" % (kind, stats.event_counts[kind]),
                      file=sys.stderr)
            print("  %-22s %d" % ("total", stats.total), file=sys.stderr)
            return 0

        # events
        flt = EventFilter(
            skus=tuple(args.sku), kinds=tuple(args.event),
            fields=tuple(args.field), field_prefix=args.field_prefix,
            limit=args.limit)
        with open_result(args.result) as reader:
            if args.out is None:
                stats = _write_events_stdout(reader, flt)
            else:
                stats = reader.export_jsonl(args.out, flt)
        print("events: %d matched, %d written%s"
              % (stats["total"], stats["returned"],
                 (" -> " + args.out) if args.out else " (stdout)"),
              file=sys.stderr)
        return 0
    except ReconError as exc:
        print("xmlnodelib: reconciliation error: %s" % exc, file=sys.stderr)
        return 1
    except XnlError as exc:
        print("xmlnodelib: error: %s" % exc, file=sys.stderr)
        return 1
    except OSError as exc:
        print("xmlnodelib: error: I/O failure: %s" % exc, file=sys.stderr)
        return 1
    except sqlite3.Error as exc:
        print("xmlnodelib: error: storage failure: %s" % exc, file=sys.stderr)
        return 1
    except RecursionError:
        print("xmlnodelib: error: resource limit: query AST too deep to execute",
              file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
