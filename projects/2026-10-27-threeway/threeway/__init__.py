"""threeway: Myers diff, unified patches and diff3 three-way merge (stdlib only)."""

from .merge3 import MergeResult, merge3
from .myers import difflib_blocks, myers_blocks
from .unified import PatchError, apply_patch, parse_patch, split_lines, unified_diff

__all__ = [
    "MergeResult",
    "PatchError",
    "apply_patch",
    "difflib_blocks",
    "merge3",
    "myers_blocks",
    "parse_patch",
    "split_lines",
    "unified_diff",
]
__version__ = "0.1.0"
