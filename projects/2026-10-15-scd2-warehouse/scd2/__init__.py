"""SQL-driven Type-2 slowly changing dimension builder over daily snapshots."""
from .store import Warehouse, SnapshotError

__all__ = ["Warehouse", "SnapshotError"]
