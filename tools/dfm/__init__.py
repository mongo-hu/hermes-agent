"""Built-in DFM domain package.

The public Hermes surface lives in :mod:`tools.dfm_tool`; this package keeps
domain state and execution details out of the core agent loop.
"""

from .contracts import MANIFEST_SCHEMA_VERSION

__all__ = ["MANIFEST_SCHEMA_VERSION"]
