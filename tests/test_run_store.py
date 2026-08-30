from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from harness_control.store import RunStore


def _metadata(run_id: str, *, state: str = "running") -> dict[str, object]:
    return {
        "run_id": run_id,
        "harness": "codex",
        "session": "backend",
        "cwd": "/tmp/repo",
        "state": state,
        "returncode": None,
        "stderr": "",
        "error": None,
    }


def test_run_store_uses_delete_journal_for_sqlite_wal_reset_safety(tmp_path: Path) -> None:
    store = RunStore(tmp_path, max_events=10, max_event_bytes=10_000, max_runs=4)

    with sqlite3.connect(store.path) as connection:
        mode = connection.execute("PRAGMA journal_mode").fetchone()[0]

    assert mode == "delete"


def test_run_store_persists_events_and_terminal_state_across_instances(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path, max_events=10, max_event_bytes=10_000, max_runs=4)
    store.create(_metadata("run-1"))
    store.append("run-1", {"jsonrpc": "2.0", "method": "session/update"})
    store.update("run-1", state="completed", returncode=0)

    reopened = RunStore(tmp_path, max_events=10, max_event_bytes=10_000, max_runs=4)
    result = reopened.events("run-1", cursor=0, limit=10)

    assert result["state"] == "completed"
    assert result["returncode"] == 0
    assert result["event_count"] == 1
    assert result["events"] == [{"jsonrpc": "2.0", "method": "session/update"}]
    assert result["next_cursor"] == 1


def test_run_store_bounds_events_and_preserves_absolute_cursor(tmp_path: Path) -> None:
    store = RunStore(tmp_path, max_events=2, max_event_bytes=10_000, max_runs=4)
    store.create(_metadata("run-1"))
    store.append("run-1", {"n": 0})
    store.append("run-1", {"n": 1})
    store.append("run-1", {"n": 2})

    with pytest.raises(ValueError, match="cursor 0 expired"):
        store.events("run-1", cursor=0, limit=10)

    result = store.events("run-1", cursor=1, limit=10)
    assert result["oldest_cursor"] == 1
    assert result["event_count"] == 3
    assert result["events"] == [{"n": 1}, {"n": 2}]
    assert result["next_cursor"] == 3


def test_run_store_marks_abandoned_running_records_interrupted(tmp_path: Path) -> None:
    first = RunStore(tmp_path, max_events=10, max_event_bytes=10_000, max_runs=4)
    first.create(_metadata("run-1"))

    reopened = RunStore(tmp_path, max_events=10, max_event_bytes=10_000, max_runs=4)
    result = reopened.events("run-1", cursor=0, limit=10)

    assert result["state"] == "interrupted"
    assert result["error"] == "Hermes stopped before this harness run completed"


def test_run_store_prunes_oldest_terminal_run_before_capacity(tmp_path: Path) -> None:
    store = RunStore(tmp_path, max_events=10, max_event_bytes=10_000, max_runs=2)
    store.create(_metadata("run-1", state="completed"))
    store.create(_metadata("run-2", state="running"))
    store.create(_metadata("run-3", state="running"))

    with pytest.raises(ValueError, match="unknown run_id"):
        store.events("run-1", cursor=0, limit=10)
    assert store.events("run-2", cursor=0, limit=10)["state"] == "running"
    assert store.events("run-3", cursor=0, limit=10)["state"] == "running"


def test_run_store_refuses_capacity_eviction_when_all_runs_are_active(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path, max_events=10, max_event_bytes=10_000, max_runs=1)
    store.create(_metadata("run-1", state="running"))

    with pytest.raises(ValueError, match="maximum active harness runs"):
        store.create(_metadata("run-2", state="running"))
