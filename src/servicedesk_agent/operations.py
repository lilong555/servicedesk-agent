"""Small, session-scoped Redmine intent records in a native LangGraph Store."""

from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from threading import Lock

from langgraph.store.base import BaseStore
from langgraph.store.memory import InMemoryStore
from langgraph.store.sqlite import SqliteStore

from servicedesk_agent.domain import BusinessError

_LOCKS = tuple(Lock() for _ in range(64))


@contextmanager
def redmine_store(root: Path):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / "memory.sqlite"
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    path.chmod(0o600)
    with SqliteStore.from_conn_string(str(path)) as store:
        store.setup()
        yield store


class OperationRecords:
    def __init__(self, store: BaseStore):
        self.store = store
        self.lock_root = None
        if isinstance(store, SqliteStore):
            with store.lock:
                databases = store.conn.execute("PRAGMA database_list").fetchall()
            path = next((row[2] for row in databases if row[1] == "main"), "")
            if not path:
                raise BusinessError(
                    "operation_store_required", "Use a file-backed SqliteStore for Redmine writes."
                )
            self.lock_root = Path(path).resolve().with_suffix(".redmine-locks")
        elif not isinstance(store, InMemoryStore):
            raise BusinessError(
                "operation_store_required",
                "Use SqliteStore for production or explicitly inject InMemoryStore for tests.",
            )

    @contextmanager
    def locked(self, thread_id: str):
        # BaseStore has no compare-and-set. Serialize read/intent/HTTP/receipt as one unit.
        lock = _LOCKS[int(thread_id[:8], 16) % len(_LOCKS)]
        if not lock.acquire(blocking=False):
            raise BusinessError("operation_busy", "Another Redmine operation is in progress.")
        try:
            if self.lock_root is None:
                yield
            else:
                self.lock_root.mkdir(parents=True, exist_ok=True, mode=0o700)
                fd = os.open(self.lock_root / thread_id, os.O_CREAT | os.O_RDWR, 0o600)
                with os.fdopen(fd, "a") as handle:
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError as exc:
                        raise BusinessError(
                            "operation_busy", "Another process owns this Redmine operation."
                        ) from exc
                    try:
                        yield
                    finally:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            lock.release()

    def load(self, thread_id: str) -> dict:
        try:
            item = self.store.get(("redmine_operations", thread_id), "writes")
            return deepcopy(item.value) if item else {"pending": None, "receipts": {}}
        except Exception as exc:
            raise BusinessError(
                "operation_store_unavailable", "Restore the operation Store before writing."
            ) from exc

    def save(self, thread_id: str, records: dict) -> None:
        try:
            # One native Store value atomically owns the pending slot and compact receipts.
            self.store.put(
                ("redmine_operations", thread_id), "writes", records, index=False, ttl=None
            )
        except Exception as exc:
            raise BusinessError(
                "operation_store_unavailable", "Restore the operation Store and reconcile."
            ) from exc
