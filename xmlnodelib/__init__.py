"""xmlnodelib — offline XML node query library.

Import an XML document once into a persistent SQLite node library, then run
structured JSON path-AST queries against the library alone; the original XML is
never needed at query time.  See README.md for the AST format and the exact
query semantics.
"""

from .astcheck import validate_ast
from .engine import run_query
from .errors import AstError, ImportRejected, QueryError, ResourceLimit, XnlError
from .importer import import_xml

__version__ = "1.0.0"

__all__ = [
    "import_xml",
    "run_query",
    "validate_ast",
    "XnlError",
    "ImportRejected",
    "AstError",
    "QueryError",
    "ResourceLimit",
    "__version__",
]
