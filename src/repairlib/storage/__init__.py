"""Persistence boundary: sqlite3 connections and migrations live here.

No connection is opened at import time. Adapters that must report storage
failures use the re-exported error types instead of importing sqlite3.
"""

from sqlite3 import Error as StorageError, OperationalError as StorageOperationalError

__all__ = ["StorageError", "StorageOperationalError"]
