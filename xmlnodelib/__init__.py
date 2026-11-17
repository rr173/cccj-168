"""xmlnodelib — offline XML node query library.

Import an XML document once into a persistent SQLite node library, then run
structured JSON path-AST queries against the library alone; the original XML is
never needed at query time.  See README.md for the AST format and the exact
query semantics.
"""

from .astcheck import validate_ast
from .engine import run_query
from .errors import (AstError, ImportRejected, QueryError, ReconError,
                     ResourceLimit, XnlError)
from .importer import import_xml
from .recon import (EVENT_KINDS, ReconcileResult, ResultReader, load_mapping,
                    open_result, reconcile_catalogs)

__version__ = "1.0.0"

__all__ = [
    "import_xml",
    "run_query",
    "validate_ast",
    "reconcile_catalogs",
    "load_mapping",
    "open_result",
    "ResultReader",
    "ReconcileResult",
    "EVENT_KINDS",
    "XnlError",
    "ImportRejected",
    "AstError",
    "QueryError",
    "ReconError",
    "ResourceLimit",
    "__version__",
]
