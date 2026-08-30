from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any


class RunStore:
    """Profile-scoped durable run metadata and bounded ACP event storage."""

    def __init__(
        self,
        data_dir: Path,
        *,
        max_events: int,
        max_event_bytes: int,
        max_runs: int,
    ) -> None:
        if max_events < 1 or max_event_bytes < 1 or max_runs < 1:
            raise ValueError("run store limits must be positive")
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            os.chmod(self.data_dir, 0o700)
        self.path = self.data_dir / "runs.sqlite3"
        self._max_events = max_events
        self._max_event_bytes = max_event_bytes
        self._max_runs = max_runs
        self._initialize()
        self._recover_interrupted()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = DELETE")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    harness TEXT NOT NULL,
                    session TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    state TEXT NOT NULL,
                    total_events INTEGER NOT NULL DEFAULT 0,
                    oldest_cursor INTEGER NOT NULL DEFAULT 0,
                    event_bytes INTEGER NOT NULL DEFAULT 0,
                    returncode INTEGER,
                    stderr TEXT NOT NULL DEFAULT '',
                    error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                    cursor INTEGER NOT NULL,
                    event_json TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    PRIMARY KEY (run_id, cursor)
                );
                CREATE INDEX IF NOT EXISTS idx_runs_updated_at
                    ON runs(updated_at);
                """
            )
        if os.name != "nt":
            os.chmod(self.path, 0o600)

    def _recover_interrupted(self) -> None:
        now = time.time()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE runs
                   SET state = 'interrupted',
                       error = 'Hermes stopped before this harness run completed',
                       updated_at = ?
                 WHERE state = 'running'
                """,
                (now,),
            )

    def create(self, metadata: Mapping[str, Any]) -> None:
        run_id = self._run_id(metadata.get("run_id"))
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._prune_runs_for_capacity(connection)
            connection.execute(
                """
                INSERT INTO runs (
                    run_id, harness, session, cwd, state, returncode,
                    stderr, error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    str(metadata.get("harness", "")),
                    str(metadata.get("session", "")),
                    str(metadata.get("cwd", "")),
                    str(metadata.get("state", "running")),
                    metadata.get("returncode"),
                    str(metadata.get("stderr", "")),
                    metadata.get("error"),
                    now,
                    now,
                ),
            )

    def _prune_runs_for_capacity(self, connection: sqlite3.Connection) -> None:
        count = int(connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0])
        while count >= self._max_runs:
            row = connection.execute(
                """
                SELECT run_id FROM runs
                 WHERE state != 'running'
                 ORDER BY updated_at ASC, run_id ASC
                 LIMIT 1
                """
            ).fetchone()
            if row is None:
                raise ValueError(
                    f"maximum active harness runs reached ({self._max_runs})"
                )
            connection.execute("DELETE FROM runs WHERE run_id = ?", (row[0],))
            count -= 1

    def append(self, run_id: str, event: Mapping[str, Any]) -> None:
        run_id = self._run_id(run_id)
        encoded = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        size_bytes = len(encoded.encode("utf-8"))
        now = time.time()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT total_events, event_bytes FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown run_id: {run_id}")
            cursor = int(row["total_events"])
            event_bytes = int(row["event_bytes"]) + size_bytes
            connection.execute(
                "INSERT INTO events(run_id, cursor, event_json, size_bytes) VALUES (?, ?, ?, ?)",
                (run_id, cursor, encoded, size_bytes),
            )
            total_events = cursor + 1
            connection.execute(
                """
                UPDATE runs
                   SET total_events = ?, event_bytes = ?, updated_at = ?
                 WHERE run_id = ?
                """,
                (total_events, event_bytes, now, run_id),
            )
            self._prune_events(connection, run_id, total_events, event_bytes)

    def _prune_events(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        total_events: int,
        event_bytes: int,
    ) -> None:
        count = int(
            connection.execute(
                "SELECT COUNT(*) FROM events WHERE run_id = ?", (run_id,)
            ).fetchone()[0]
        )
        while count > self._max_events or event_bytes > self._max_event_bytes:
            oldest = connection.execute(
                """
                SELECT cursor, size_bytes FROM events
                 WHERE run_id = ? ORDER BY cursor ASC LIMIT 1
                """,
                (run_id,),
            ).fetchone()
            if oldest is None:
                break
            connection.execute(
                "DELETE FROM events WHERE run_id = ? AND cursor = ?",
                (run_id, oldest["cursor"]),
            )
            event_bytes -= int(oldest["size_bytes"])
            count -= 1
        first = connection.execute(
            "SELECT MIN(cursor) FROM events WHERE run_id = ?", (run_id,)
        ).fetchone()[0]
        oldest_cursor = total_events if first is None else int(first)
        connection.execute(
            "UPDATE runs SET oldest_cursor = ?, event_bytes = ? WHERE run_id = ?",
            (oldest_cursor, event_bytes, run_id),
        )

    def update(self, run_id: str, **fields: Any) -> None:
        run_id = self._run_id(run_id)
        allowed = {"state", "returncode", "stderr", "error"}
        unexpected = set(fields) - allowed
        if unexpected:
            raise ValueError(f"unsupported run fields: {', '.join(sorted(unexpected))}")
        if not fields:
            return
        assignments = ", ".join(f"{key} = ?" for key in fields)
        values = [fields[key] for key in fields]
        values.extend((time.time(), run_id))
        with self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE runs SET {assignments}, updated_at = ? WHERE run_id = ?",  # noqa: S608
                values,
            )
            if cursor.rowcount != 1:
                raise ValueError(f"unknown run_id: {run_id}")

    def events(self, run_id: str, *, cursor: int, limit: int) -> dict[str, Any]:
        run_id = self._run_id(run_id)
        if cursor < 0:
            raise ValueError("cursor must be >= 0")
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        with self._connect() as connection:
            run = connection.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise ValueError(f"unknown run_id: {run_id}")
            oldest = int(run["oldest_cursor"])
            newest = int(run["total_events"])
            if cursor < oldest:
                raise ValueError(
                    f"cursor {cursor} expired; oldest available is {oldest}"
                )
            if cursor > newest:
                raise ValueError(
                    f"cursor {cursor} is beyond newest available cursor {newest}"
                )
            rows = connection.execute(
                """
                SELECT cursor, event_json FROM events
                 WHERE run_id = ? AND cursor >= ?
                 ORDER BY cursor ASC LIMIT ?
                """,
                (run_id, cursor, limit),
            ).fetchall()
        events = [json.loads(row["event_json"]) for row in rows]
        next_cursor = cursor if not rows else int(rows[-1]["cursor"]) + 1
        return {
            "run_id": run["run_id"],
            "harness": run["harness"],
            "session": run["session"],
            "cwd": run["cwd"],
            "state": run["state"],
            "event_count": newest,
            "buffered_event_count": newest - oldest,
            "oldest_cursor": oldest,
            "returncode": run["returncode"],
            "stderr": run["stderr"],
            "error": run["error"],
            "cursor": cursor,
            "next_cursor": next_cursor,
            "has_more": next_cursor < newest,
            "events": events,
        }

    @staticmethod
    def _run_id(value: Any) -> str:
        if not isinstance(value, str) or not value.strip() or len(value) > 128:
            raise ValueError("run_id must be a non-empty string of at most 128 characters")
        return value.strip()
