"""Domain-level entry to the storage dry-run guard, so adapters never import storage."""

from repairlib.storage.dry_run import armed

__all__ = ["armed"]
