"""Persistent A2A task store (SQLite).

``InMemoryTaskStore`` drops every task when the process stops, so after each
``cf push`` every existing Joule conversation thread gets
``-32001: Task ... does not exist`` on its next message — which surfaces in
Joule as an undiagnosable "Something went wrong". This store persists tasks to
a SQLite file so threads survive a restart / redeploy.

Enabled by ``TASK_STORE_PATH`` (e.g. ``/home/vcap/app/data/tasks.db``). Unset
→ in-memory, same as the a2a-sdk default.

SQLite is fine for this low-QPS single-instance chatbot. For multi-instance
scale-out, point ``TASK_STORE_PATH`` at a shared volume or replace this with a
Postgres/Redis store implementing the same ``TaskStore`` interface.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from pathlib import Path

from a2a.server.tasks.task_store import TaskStore
from a2a.types import Task

logger = logging.getLogger(__name__)


class SQLiteTaskStore(TaskStore):
    def __init__(self, path: str) -> None:
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._init_db()
        logger.info("SQLiteTaskStore active: path=%s", path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS tasks ("
                "  id TEXT PRIMARY KEY,"
                "  context_id TEXT,"
                "  data TEXT NOT NULL,"
                "  updated_at REAL DEFAULT (strftime('%s','now'))"
                ")"
            )

    async def save(self, task: Task, context=None) -> None:
        payload = task.model_dump_json()
        ctx = getattr(task, "context_id", None)
        async with self._lock:
            await asyncio.to_thread(self._save_sync, task.id, ctx, payload)
        logger.debug("Task %s saved (context_id=%s)", task.id, ctx)

    def _save_sync(self, task_id: str, ctx: str | None, payload: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO tasks (id, context_id, data, updated_at) "
                "VALUES (?, ?, ?, strftime('%s','now')) "
                "ON CONFLICT(id) DO UPDATE SET data=excluded.data, "
                "context_id=excluded.context_id, updated_at=excluded.updated_at",
                (task_id, ctx, payload),
            )

    async def get(self, task_id: str, context=None) -> Task | None:
        async with self._lock:
            row = await asyncio.to_thread(self._get_sync, task_id)
        if row is None:
            return None
        try:
            return Task.model_validate_json(row)
        except Exception:
            logger.exception("Corrupt task row for %s; treating as missing", task_id)
            return None

    def _get_sync(self, task_id: str) -> str | None:
        with self._connect() as conn:
            cur = conn.execute("SELECT data FROM tasks WHERE id = ?", (task_id,))
            r = cur.fetchone()
            return r[0] if r else None

    async def delete(self, task_id: str, context=None) -> None:
        async with self._lock:
            await asyncio.to_thread(self._delete_sync, task_id)

    def _delete_sync(self, task_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))

    async def get_by_context(self, context_id: str, limit: int = 20) -> list[Task]:
        """All tasks sharing a context_id, oldest first. Used to reconstruct a
        conversation when the client threads only contextId (a fresh taskId per
        turn) instead of continuing one task."""
        if not context_id:
            return []
        async with self._lock:
            rows = await asyncio.to_thread(self._get_by_context_sync, context_id, limit)
        tasks: list[Task] = []
        for row in rows:
            try:
                tasks.append(Task.model_validate_json(row))
            except Exception:  # pragma: no cover - skip a corrupt row
                logger.warning("Skipping corrupt task row for context %s", context_id)
        return tasks

    def _get_by_context_sync(self, context_id: str, limit: int) -> list[str]:
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT data FROM tasks WHERE context_id = ? ORDER BY updated_at ASC LIMIT ?",
                (context_id, max(1, limit)),
            )
            return [r[0] for r in cur.fetchall()]


def build_task_store() -> TaskStore:
    """Return a persistent store if ``TASK_STORE_PATH`` is set, else in-memory."""
    path = os.getenv("TASK_STORE_PATH")
    if path:
        try:
            return SQLiteTaskStore(path)
        except Exception:
            logger.exception("Could not open SQLiteTaskStore at %s; using in-memory", path)

    from a2a.server.tasks.inmemory_task_store import InMemoryTaskStore

    logger.info("Using InMemoryTaskStore (set TASK_STORE_PATH to persist tasks across restarts)")
    return InMemoryTaskStore()
