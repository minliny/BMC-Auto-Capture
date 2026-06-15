"""Public package facade for BMC Auto-Capture.

The current codebase still keeps the legacy ``src`` package as the runtime
module tree.  This facade establishes the stable public package name while the
internal modules are migrated incrementally.
"""

from ._version import __version__

__all__ = ["__version__"]
