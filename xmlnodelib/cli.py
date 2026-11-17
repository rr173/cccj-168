"""Command line interface: ``python -m xmlnodelib import|query ...``.

Exit codes: 0 success; 1 any expected failure (rejected import, invalid AST,
query error, resource limit, overwrite refusal); 2 CLI usage error.
"""

import argparse
import json
import sqlite3
import sys

from . import __version__
from .astcheck import validate_ast
from .engine import run_query
from .errors import AstError, XnlError
from .importer import import_xml


def _build_parser():
    ap = argparse.ArgumentParser(
        prog="xmlnodelib",
        description="Offline XML node query library: import an XML document "
                    "into a persistent node library, then run structured "
                    "JSON path queries against it without the original XML.")
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
    return ap


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

        # query
        try:
            with open(args.ast, "r", encoding="utf-8") as fh:
                ast_obj = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            raise AstError("cannot read AST JSON %s: %s" % (args.ast, exc))
        stats = run_query(
            args.db, ast_obj, args.out, page_size=args.page_size,
            batch=args.batch, cache_kb=args.cache_kb, scratch_dir=args.scratch_dir)
        print("query matched %d node(s), wrote %d record(s) -> %s"
              % (stats["total"], stats["returned"], args.out), file=sys.stderr)
        return 0
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
