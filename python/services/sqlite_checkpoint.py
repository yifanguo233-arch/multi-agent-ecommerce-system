from __future__ import annotations

import sqlite3
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver


@asynccontextmanager
async def sqlite_checkpointer(path: str) -> AsyncIterator[AsyncSqliteSaver]:
    """为一次 graph 生命周期打开官方 LangGraph SQLite checkpointer。"""
    db_path = _normalize_sqlite_path(path)
    _ensure_parent_dir(db_path)
    async with AsyncSqliteSaver.from_conn_string(db_path) as saver:
        yield saver


class GraphReplayIndexStore:
    """持久化 request_id -> thread_id 索引，用于按 request_id replay。"""

    def __init__(self, path: str) -> None:
        self.path = _normalize_sqlite_path(path)
        self._lock = threading.RLock()
        _ensure_parent_dir(self.path)
        self._ensure_schema()

    def remember(self, request_id: str, thread_id: str) -> None:
        if not request_id or not thread_id:
            return
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO graph_replay_index (request_id, thread_id)
                VALUES (?, ?)
                ON CONFLICT(request_id) DO UPDATE SET
                    thread_id = excluded.thread_id,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (request_id, thread_id),
            )

    def resolve(self, request_id: str) -> str:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT thread_id FROM graph_replay_index WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        return str(row[0]) if row else ""

    def _ensure_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS graph_replay_index (
                    request_id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn


def _normalize_sqlite_path(path: str) -> str:
    if path.startswith("sqlite:///"):
        return path.replace("sqlite:///", "", 1)
    return path


def _ensure_parent_dir(path: str) -> None:
    if path == ":memory:":
        return
    Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
