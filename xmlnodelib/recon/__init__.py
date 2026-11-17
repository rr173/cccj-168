"""Offline snapshot reconciliation for xmlnodelib.

Compare two already-imported, complete node libraries (yesterday's and
today's catalog) under a declarative mapping and publish a standalone
SQLite result library of reconciliation events.  Everything except small
control structures is streamed or disk-backed: neither catalog is ever
materialized as a Python tree or dict.

Public entry points (also re-exported from the top-level ``xmlnodelib``
package):

  load_mapping          validate/normalize a mapping dict or JSON file
  reconcile_catalogs    build a new result library from two node libraries
  ReconcileResult       summary of a successful reconciliation
  open_result           open a published result library for filtering/export
  ResultReader          streaming filter/export over a result library
"""

from .mapping import (FieldDescriptor, Mapping, ProductMapping,
                      VariantMapping, load_mapping)
from .reconcile import ReconcileResult, reconcile_catalogs
from .result import EVENT_KINDS, ResultReader, open_result

__all__ = [
    "load_mapping",
    "reconcile_catalogs",
    "ReconcileResult",
    "open_result",
    "ResultReader",
    "EVENT_KINDS",
    "Mapping",
    "ProductMapping",
    "VariantMapping",
    "FieldDescriptor",
]
