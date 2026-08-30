from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import threading
import uuid
from collections import deque
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .acpx import Action, HarnessSpec, PermissionMode, build_acpx_argv
from .store import RunStore

logger = logging.getLogger(__name__)

_MAX_STDERR_CHARS = 16_384
_MAX_STDERR_BYTES = _MAX_STDERR_CHARS * 4
_DEFAULT_MAX_EVENTS = 2_000
_DEFAULT_MAX_EVENT_BYTES = 4 * 1024 * 1024
_DEFAULT_MAX_RUNS = 32
_MAX_EVENT_LINE_BYTES = 1024 * 1024
_DEFAULT_MAX_CONTROL_OUTPUT_BYTES = 1024 * 1024
_DEFAULT_CONTROL_TIMEOUT_SECONDS = 120.0


@dataclass(slots=True)
class RunRecord:
    run_id: str
    harness: str
    session: str
    cwd: str
    argv: list[str]
    state: str = "running"
    events: deque[dict[str, Any]] = field(default_factory=deque)
    event_sizes: deque[int] = field(default_factory=deque)
    event_bytes: int = 0
    event_base_cursor: int = 0
    total_events: int = 0
    stderr: str = ""
    returncode: int | None = None
    error: str | None = None
    process: subprocess.Popen[bytes] | None = None
    thread: threading.Thread | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


class HarnessController:
    """Small in-process supervisor around ACPX CLI invocations."""

    def __init__(
        self,
        *,
        acpx_argv: Sequence[str],
        harnesses: Mapping[str, Mapping[str, Any]],
        data_dir: Path | None = None,
        max_events: int = _DEFAULT_MAX_EVENTS,
        max_event_bytes: int = _DEFAULT_MAX_EVENT_BYTES,
        max_runs: int = _DEFAULT_MAX_RUNS,
        max_control_output_bytes: int = _DEFAULT_MAX_CONTROL_OUTPUT_BYTES,
        control_timeout_seconds: float = _DEFAULT_CONTROL_TIMEOUT_SECONDS,
    ) -> None:
        if (
            max_events < 1
            or max_event_bytes < 1
            or max_runs < 1
            or max_control_output_bytes < 1
            or control_timeout_seconds <= 0
        ):
            raise ValueError("event and run limits must be positive")
        self._acpx_argv = tuple(str(part) for part in acpx_argv)
        self._max_events = max_events
        self._max_event_bytes = max_event_bytes
        self._max_runs = max_runs
        self._max_event_line_bytes = min(max_event_bytes, _MAX_EVENT_LINE_BYTES)
        self._max_control_output_bytes = max_control_output_bytes
        self._control_timeout_seconds = control_timeout_seconds
        self._store = (
            RunStore(
                data_dir,
                max_events=max_events,
                max_event_bytes=max_event_bytes,
                max_runs=max_runs,
            )
            if data_dir is not None
            else None
        )
        self._harnesses = {
            name: HarnessSpec(
                name=name,
                agent=_optional_string(value.get("agent")),
                command=_optional_string(value.get("command")),
            )
            for name, value in harnesses.items()
        }
        self._runs: dict[str, RunRecord] = {}
        self._runs_lock = threading.Lock()
        self._control_processes: dict[int, subprocess.Popen[bytes]] = {}
        self._control_processes_lock = threading.Lock()

    async def prompt(
        self,
        *,
        harness: str,
        cwd: Path,
        session: str,
        prompt: str,
        permission_mode: PermissionMode,
    ) -> dict[str, Any]:
        spec = self._get_harness(harness)
        argv = build_acpx_argv(
            acpx_argv=self._acpx_argv,
            spec=spec,
            cwd=cwd,
            permission_mode=permission_mode,
            action="prompt",
            session=session,
            prompt=prompt,
        )
        run_id = uuid.uuid4().hex
        record = RunRecord(
            run_id=run_id,
            harness=harness,
            session=session,
            cwd=str(cwd.expanduser().resolve()),
            argv=argv,
        )
        record.process = await asyncio.to_thread(_spawn_process, argv, cwd=cwd)
        self._store_run(record)
        record.thread = threading.Thread(
            target=self._collect_prompt,
            args=(record,),
            name=f"harness-control:{run_id}",
            daemon=True,
        )
        record.thread.start()
        return self._run_summary(record)

    def list_harnesses(self) -> list[dict[str, str]]:
        return [
            {
                "name": spec.name,
                "kind": "builtin" if spec.agent else "custom",
                "selector": spec.agent or str(spec.command),
            }
            for spec in sorted(self._harnesses.values(), key=lambda item: item.name)
        ]

    async def list_sessions(
        self,
        *,
        harness: str,
        cwd: Path,
        permission_mode: PermissionMode,
    ) -> dict[str, Any]:
        return await self._control(
            harness=harness,
            cwd=cwd,
            permission_mode=permission_mode,
            action="list",
        )

    async def start(
        self,
        *,
        harness: str,
        cwd: Path,
        session: str,
        permission_mode: PermissionMode,
        fresh: bool,
    ) -> dict[str, Any]:
        return await self._control(
            harness=harness,
            cwd=cwd,
            permission_mode=permission_mode,
            action="start",
            session=session,
            fresh=fresh,
        )

    async def status(
        self,
        *,
        harness: str,
        cwd: Path,
        session: str,
        permission_mode: PermissionMode,
    ) -> dict[str, Any]:
        return await self._control(
            harness=harness,
            cwd=cwd,
            permission_mode=permission_mode,
            action="status",
            session=session,
        )

    async def cancel(
        self,
        *,
        harness: str,
        cwd: Path,
        session: str,
        permission_mode: PermissionMode,
    ) -> dict[str, Any]:
        return await self._control(
            harness=harness,
            cwd=cwd,
            permission_mode=permission_mode,
            action="cancel",
            session=session,
        )

    async def close(
        self,
        *,
        harness: str,
        cwd: Path,
        session: str,
        permission_mode: PermissionMode,
    ) -> dict[str, Any]:
        return await self._control(
            harness=harness,
            cwd=cwd,
            permission_mode=permission_mode,
            action="close",
            session=session,
        )

    def events(
        self, run_id: str, *, cursor: int = 0, limit: int = 100
    ) -> dict[str, Any]:
        with self._runs_lock:
            record = self._runs.get(run_id)
        if record is None:
            if self._store is not None:
                return self._store.events(run_id, cursor=cursor, limit=limit)
            raise ValueError(f"unknown run_id: {run_id}")
        if cursor < 0:
            raise ValueError("cursor must be >= 0")
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")

        with record.lock:
            oldest = record.event_base_cursor
            newest = oldest + len(record.events)
            if cursor < oldest:
                raise ValueError(
                    f"cursor {cursor} expired; oldest available is {oldest}"
                )
            if cursor > newest:
                raise ValueError(
                    f"cursor {cursor} is beyond newest available cursor {newest}"
                )
            local_cursor = cursor - oldest
            end = min(local_cursor + limit, len(record.events))
            buffered_events = list(record.events)
            result = self._run_summary(record)
            result.update(
                {
                    "cursor": cursor,
                    "oldest_cursor": oldest,
                    "next_cursor": oldest + end,
                    "has_more": end < len(record.events),
                    "events": buffered_events[local_cursor:end],
                }
            )
        return result

    async def shutdown(self) -> None:
        await asyncio.to_thread(self.close_now)

    def close_now(self) -> None:
        """Terminate process groups and join reader threads during unload."""
        with self._runs_lock:
            records = list(self._runs.values())
        with self._control_processes_lock:
            control_processes = list(self._control_processes.values())
        for record in records:
            _terminate_process(record)
            self._persist_run_state(record)
        for process in control_processes:
            _terminate_popen(process)
        for record in records:
            if record.thread is not None:
                record.thread.join(timeout=2.0)

    async def _control(
        self,
        *,
        harness: str,
        cwd: Path,
        permission_mode: PermissionMode,
        action: Action,
        session: str | None = None,
        fresh: bool = False,
    ) -> dict[str, Any]:
        spec = self._get_harness(harness)
        argv = build_acpx_argv(
            acpx_argv=self._acpx_argv,
            spec=spec,
            cwd=cwd,
            permission_mode=permission_mode,
            action=action,
            session=session,
            fresh=fresh,
        )
        stdout_bytes, stderr_bytes, returncode, control_error = await asyncio.to_thread(
            self._run_control_sync, argv, cwd
        )
        stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
        stderr = stderr_bytes.decode("utf-8", errors="replace").strip()
        parsed: Any = None
        if stdout and control_error is None:
            lines = [line for line in stdout.splitlines() if line.strip()]
            try:
                values = [json.loads(line) for line in lines]
                parsed = values[0] if len(values) == 1 else values
            except json.JSONDecodeError:
                parsed = {"raw": stdout}
        result = {
            "success": returncode == 0 and control_error is None,
            "harness": harness,
            "session": session,
            "returncode": returncode,
            "result": parsed,
            "stderr": stderr[-_MAX_STDERR_CHARS:],
        }
        if control_error is not None:
            result["error"] = control_error
        return result

    def _run_control_sync(
        self, argv: list[str], cwd: Path
    ) -> tuple[bytes, bytes, int | None, str | None]:
        process = _spawn_process(argv, cwd=cwd)
        with self._control_processes_lock:
            self._control_processes[process.pid] = process
        stdout_buffer = bytearray()
        stderr_buffer = bytearray()
        stdout_overflow = threading.Event()
        stderr_overflow = threading.Event()
        if process.stdout is None or process.stderr is None:
            raise RuntimeError("ACPX process pipes were not created")
        stdout_thread = threading.Thread(
            target=_drain_stream,
            args=(process.stdout, stdout_buffer),
            kwargs={
                "max_bytes": self._max_control_output_bytes,
                "keep_tail": False,
                "overflow_flag": stdout_overflow,
            },
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=_drain_stream,
            args=(process.stderr, stderr_buffer),
            kwargs={
                "max_bytes": self._max_control_output_bytes,
                "keep_tail": True,
                "overflow_flag": stderr_overflow,
            },
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()
        control_error: str | None = None
        try:
            process.wait(timeout=self._control_timeout_seconds)
        except subprocess.TimeoutExpired:
            control_error = (
                "ACPX control command timed out after "
                f"{self._control_timeout_seconds:g} seconds"
            )
            _terminate_popen(process)
        finally:
            stdout_thread.join(timeout=2.0)
            stderr_thread.join(timeout=2.0)
            with self._control_processes_lock:
                self._control_processes.pop(process.pid, None)
        if control_error is None and stdout_overflow.is_set():
            control_error = (
                f"ACPX stdout exceeded {self._max_control_output_bytes} bytes"
            )
        if control_error is None and stderr_overflow.is_set():
            control_error = (
                f"ACPX stderr exceeded {self._max_control_output_bytes} bytes"
            )
        return (
            bytes(stdout_buffer),
            bytes(stderr_buffer),
            process.returncode,
            control_error,
        )

    def _collect_prompt(self, record: RunRecord) -> None:
        try:
            process = record.process
            if process is None or process.stdout is None or process.stderr is None:
                raise RuntimeError("ACPX process pipes were not created")
            stderr_buffer = bytearray()
            stderr_thread = threading.Thread(
                target=_drain_stream,
                args=(process.stderr, stderr_buffer),
                name=f"harness-control-stderr:{record.run_id}",
                daemon=True,
            )
            stderr_thread.start()
            while line := process.stdout.readline(self._max_event_line_bytes + 1):
                truncated = len(line) > self._max_event_line_bytes
                if truncated:
                    retained = line[: self._max_event_line_bytes]
                    while line and not line.endswith(b"\n"):
                        line = process.stdout.readline(self._max_event_line_bytes + 1)
                    text = retained.decode("utf-8", errors="replace")
                    event = {
                        "_acpx_event_truncated": True,
                        "raw": text,
                    }
                    self._append_event(record, event, len(retained))
                    continue
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    event = json.loads(text)
                except json.JSONDecodeError as exc:
                    event = {
                        "_acpx_parse_error": str(exc),
                        "raw": text,
                    }
                if not isinstance(event, dict):
                    event = {"_acpx_value": event}
                self._append_event(record, event, len(line))
            returncode = process.wait()
            stderr_thread.join(timeout=2.0)
            with record.lock:
                record.returncode = returncode
                record.stderr = bytes(stderr_buffer).decode("utf-8", errors="replace")[
                    -_MAX_STDERR_CHARS:
                ]
                if record.state != "cancelled":
                    record.state = "completed" if returncode == 0 else "failed"
            self._persist_run_state(record)
        except Exception as exc:
            with record.lock:
                record.state = "failed"
                record.error = f"{type(exc).__name__}: {exc}"
            self._persist_run_state(record)

    def _get_harness(self, name: str) -> HarnessSpec:
        try:
            return self._harnesses[name]
        except KeyError as exc:
            known = ", ".join(sorted(self._harnesses)) or "none"
            raise ValueError(f"unknown harness {name!r}; configured: {known}") from exc

    def _store_run(self, record: RunRecord) -> None:
        with self._runs_lock:
            removable = [
                run_id
                for run_id, existing in self._runs.items()
                if existing.state != "running"
            ]
            while len(self._runs) >= self._max_runs and removable:
                self._runs.pop(removable.pop(0), None)
            if len(self._runs) >= self._max_runs:
                _terminate_process(record)
                raise ValueError(
                    f"maximum active harness runs reached ({self._max_runs})"
                )
            self._runs[record.run_id] = record
        if self._store is not None:
            try:
                self._store.create(self._run_summary(record))
            except Exception:
                with self._runs_lock:
                    self._runs.pop(record.run_id, None)
                _terminate_process(record)
                raise

    def _append_event(
        self, record: RunRecord, event: dict[str, Any], size_bytes: int
    ) -> None:
        with record.lock:
            record.events.append(event)
            record.event_sizes.append(size_bytes)
            record.event_bytes += size_bytes
            record.total_events += 1
            while record.events and (
                len(record.events) > self._max_events
                or record.event_bytes > self._max_event_bytes
            ):
                record.events.popleft()
                record.event_bytes -= record.event_sizes.popleft()
                record.event_base_cursor += 1
        if self._store is not None:
            try:
                self._store.append(record.run_id, event)
            except Exception:
                logger.warning(
                    "Could not persist harness event for run %s",
                    record.run_id,
                    exc_info=True,
                )

    def _persist_run_state(self, record: RunRecord) -> None:
        if self._store is None:
            return
        with record.lock:
            fields = {
                "state": record.state,
                "returncode": record.returncode,
                "stderr": record.stderr,
                "error": record.error,
            }
        try:
            self._store.update(record.run_id, **fields)
        except Exception:
            logger.warning(
                "Could not persist harness run state for %s",
                record.run_id,
                exc_info=True,
            )

    @staticmethod
    def _run_summary(record: RunRecord) -> dict[str, Any]:
        return {
            "run_id": record.run_id,
            "harness": record.harness,
            "session": record.session,
            "cwd": record.cwd,
            "state": record.state,
            "event_count": record.total_events,
            "buffered_event_count": len(record.events),
            "oldest_cursor": record.event_base_cursor,
            "returncode": record.returncode,
            "stderr": record.stderr,
            "error": record.error,
        }


def _optional_string(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _spawn_process(argv: list[str], *, cwd: Path) -> subprocess.Popen[bytes]:
    process_cwd = str(cwd.expanduser().resolve())
    if os.name == "nt":
        return subprocess.Popen(  # noqa: S603 - argv vector, shell disabled.
            argv,
            cwd=process_cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            creationflags=int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)),
        )
    return subprocess.Popen(  # noqa: S603 - argv vector, shell disabled.
        argv,
        cwd=process_cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        start_new_session=True,
    )


def _terminate_process(record: RunRecord) -> None:
    process = record.process
    if process is None or process.poll() is not None:
        return
    with record.lock:
        record.state = "cancelled"
    _terminate_popen(process)


def _terminate_popen(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            process.send_signal(getattr(signal, "CTRL_BREAK_EVENT", signal.SIGTERM))
        else:
            os.killpg(process.pid, signal.SIGTERM)
    except OSError:
        process.terminate()
    try:
        process.wait(timeout=1.0)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        if os.name == "nt":
            process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        process.kill()
    with suppress(subprocess.TimeoutExpired):
        process.wait(timeout=1.0)


def _drain_stream(
    stream: Any,
    buffer: bytearray,
    *,
    max_bytes: int = _MAX_STDERR_BYTES,
    keep_tail: bool = True,
    overflow_flag: threading.Event | None = None,
) -> None:
    while chunk := stream.read(8192):
        if len(buffer) + len(chunk) > max_bytes and overflow_flag is not None:
            overflow_flag.set()
        if keep_tail:
            buffer.extend(chunk)
            if len(buffer) > max_bytes:
                del buffer[:-max_bytes]
        else:
            remaining = max_bytes - len(buffer)
            if remaining > 0:
                buffer.extend(chunk[:remaining])
