"""Exception hierarchy for xmlnodelib.

Every expected, user-facing failure (rejected input, invalid AST, resource
limit, overwrite refusal, ...) derives from XnlError so the CLI can report it
cleanly and exit with a non-zero status.
"""


class XnlError(Exception):
    """Base class for all expected xmlnodelib failures."""


class ImportRejected(XnlError):
    """The input XML is not acceptable (not well-formed, DOCTYPE, entities,
    depth limit, ...) or the target library already exists."""


class AstError(XnlError):
    """The query AST is structurally invalid, uses unknown operations, or
    references unbound names. Raised before any execution starts."""


class QueryError(XnlError):
    """The query cannot be executed (bad library, bad context node, output
    already exists, ...)."""


class ResourceLimit(XnlError):
    """A configured resource limit (e.g. max depth) was exceeded."""
