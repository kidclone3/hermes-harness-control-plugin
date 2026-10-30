from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

_TERMINAL_STATES = {"completed", "failed", "cancelled", "interrupted"}
_CURSOR_EXPIRED = re.compile(r"cursor \d+ expired; oldest available is (\d+)")
_ERROR_CATEGORY = re.compile(r"^([A-Za-z_][A-Za-z0-9_.]{0,63}):")
_TOOL_ACTIVITY = {
    "read": "Reading",
    "edit": "Editing",
    "delete": "Editing",
    "move": "Editing",
    "search": "Searching",
    "execute": "Running a command",
    "think": "Planning",
    "fetch": "Fetching",
    "switch_mode": "Changing mode",
    "other": "Using a tool",
}


@dataclass(slots=True)
class _Watcher:
    thread: threading.Thread
    stop: threading.Event


class ProgressManager:
    """Own one short-lived event watcher per prompt run."""

    def __init__(
        self,
        controller: Any,
        context: Any,
        *,
        poll_interval: float = 2.0,
        update_interval: float = 10.0,
        page_limit: int = 100,
        monotonic: Callable[[], float] = time.monotonic,
        wait: Callable[[threading.Event, float], bool] | None = None,
        thread_factory: Callable[..., threading.Thread] = threading.Thread,
    ) -> None:
        if poll_interval < 0 or update_interval < 0:
            raise ValueError("progress intervals must be non-negative")
        if not 1 <= page_limit <= 500:
            raise ValueError("progress page_limit must be between 1 and 500")
        self._controller = controller
        self._context = context
        self._poll_interval = poll_interval
        self._update_interval = update_interval
        self._page_limit = page_limit
        self._monotonic = monotonic
        self._wait = wait or _wait
        self._thread_factory = thread_factory
        self._watchers: dict[str, _Watcher] = {}
        self._watchers_lock = threading.Lock()
        self._delivery_gate = threading.Lock()
        self._unloading = threading.Event()

    def start(
        self,
        *,
        run_id: str,
        harness: str,
        session_key: str,
        surface: str,
        message: Any | None,
    ) -> bool:
        """Start a daemon watcher. Failure never changes the launched run result."""
        stop = threading.Event()
        started_at = self._monotonic()
        thread = self._thread_factory(
            target=self._watch,
            args=(run_id, harness, session_key, surface, message, stop, started_at),
            name=f"harness-progress:{run_id}",
            daemon=True,
        )
        watcher = _Watcher(thread=thread, stop=stop)
        try:
            with self._watchers_lock:
                if self._unloading.is_set() or run_id in self._watchers:
                    return False
                self._watchers[run_id] = watcher
                try:
                    thread.start()
                except Exception:
                    self._watchers.pop(run_id, None)
                    raise
        except Exception as exc:
            logger.warning(
                "progress watcher start failed run=%s category=%s",
                _short_id(run_id),
                type(exc).__name__,
            )
            return False
        return True

    def join(self, run_id: str, *, timeout: float) -> bool:
        """Wait for one watcher without stopping it."""
        with self._watchers_lock:
            watcher = self._watchers.get(run_id)
        if watcher is None:
            return True
        watcher.thread.join(timeout=max(0.0, timeout))
        return not watcher.thread.is_alive()

    @property
    def active_count(self) -> int:
        with self._watchers_lock:
            return len(self._watchers)

    def close(self, *, join_timeout: float = 2.0) -> None:
        """Stop all watchers and bound total join time during plugin unload."""
        with self._delivery_gate:
            self._unloading.set()
        with self._watchers_lock:
            watchers = list(self._watchers.values())
            for watcher in watchers:
                watcher.stop.set()
        deadline = time.monotonic() + max(0.0, join_timeout)
        for watcher in watchers:
            remaining = max(0.0, deadline - time.monotonic())
            watcher.thread.join(timeout=remaining)
        with self._watchers_lock:
            for run_id, watcher in list(self._watchers.items()):
                if not watcher.thread.is_alive():
                    self._watchers.pop(run_id, None)

    def _watch(
        self,
        run_id: str,
        harness: str,
        session_key: str,
        surface: str,
        message: Any | None,
        stop: threading.Event,
        started_at: float,
    ) -> None:
        cursor = 0
        activity = "Waiting for harness activity"
        last_event_at: float | None = None
        last_edit_at = started_at
        try:
            while not stop.is_set():
                try:
                    page = self._controller.events(
                        run_id, cursor=cursor, limit=self._page_limit
                    )
                except ValueError as exc:
                    recovered = _expired_cursor(str(exc))
                    if recovered is not None and recovered != cursor:
                        cursor = recovered
                        continue
                    self._log_poll_failure(run_id, exc)
                    if self._wait(stop, self._poll_interval):
                        return
                    continue
                except Exception as exc:
                    self._log_poll_failure(run_id, exc)
                    if self._wait(stop, self._poll_interval):
                        return
                    continue

                now = self._monotonic()
                events = page.get("events")
                if isinstance(events, list):
                    for event in events:
                        activity = activity_from_event(event)
                        last_event_at = now
                next_cursor = page.get("next_cursor")
                if isinstance(next_cursor, int) and next_cursor >= cursor:
                    cursor = next_cursor

                state = page.get("state")
                if page.get("has_more") is True:
                    continue
                if isinstance(state, str) and state in _TERMINAL_STATES:
                    self._finish(
                        run_id=run_id,
                        harness=harness,
                        session_key=session_key,
                        message=message,
                        stop=stop,
                        state=state,
                        returncode=page.get("returncode"),
                        controller_error=page.get("error"),
                        activity=activity,
                        started_at=started_at,
                        last_event_at=last_event_at,
                        now=now,
                    )
                    return

                if (
                    surface != "cli"
                    and now - last_edit_at >= self._update_interval
                ):
                    self._edit(
                        message,
                        running_notice(
                            harness=harness,
                            run_id=run_id,
                            activity=activity,
                            elapsed=now - started_at,
                            last_event_age=_age(now, last_event_at),
                        ),
                        run_id=run_id,
                        stop=stop,
                    )
                    last_edit_at = now
                if self._wait(stop, self._poll_interval):
                    return
        finally:
            current = threading.current_thread()
            with self._watchers_lock:
                watcher = self._watchers.get(run_id)
                if watcher is not None and watcher.thread is current:
                    self._watchers.pop(run_id, None)

    def _finish(
        self,
        *,
        run_id: str,
        harness: str,
        session_key: str,
        message: Any | None,
        stop: threading.Event,
        state: str,
        returncode: Any,
        controller_error: Any,
        activity: str,
        started_at: float,
        last_event_at: float | None,
        now: float,
    ) -> None:
        category = controller_error_category(controller_error)
        elapsed = now - started_at
        last_event_age = _age(now, last_event_at)
        self._edit(
            message,
            terminal_notice(
                harness=harness,
                run_id=run_id,
                state=state,
                returncode=returncode,
                controller_error=category,
                activity=activity,
                elapsed=elapsed,
                last_event_age=last_event_age,
            ),
            run_id=run_id,
            stop=stop,
        )
        if not session_key:
            return
        content = completion_prompt(
            harness=harness,
            run_id=run_id,
            state=state,
            returncode=returncode,
            controller_error=category,
            activity=activity,
            elapsed=elapsed,
            last_event_age=last_event_age,
        )
        if not self.admit_delivery(stop):
            return
        inject = getattr(self._context, "inject_message", None)
        if not callable(inject):
            return
        try:
            accepted = bool(inject(content, session_key=session_key))
        except Exception as exc:
            logger.warning(
                "completion injection failed run=%s category=%s",
                _short_id(run_id),
                type(exc).__name__,
            )
            return
        if not accepted:
            logger.warning("completion injection refused run=%s", _short_id(run_id))

    def _edit(
        self,
        message: Any | None,
        text: str,
        *,
        run_id: str,
        stop: threading.Event,
    ) -> None:
        edit = getattr(message, "edit", None)
        if not callable(edit):
            return
        if not self.admit_delivery(stop):
            return
        try:
            result = edit(text)
        except Exception as exc:
            logger.warning(
                "progress notice edit failed run=%s category=%s",
                _short_id(run_id),
                type(exc).__name__,
            )
            return
        if isinstance(result, Mapping) and result.get("ok") is False:
            logger.warning("progress notice edit refused run=%s", _short_id(run_id))

    def admit_delivery(self, stop: threading.Event | None = None) -> bool:
        """Linearize new host calls against unload without waiting for admitted I/O."""
        with self._delivery_gate:
            return not self._unloading.is_set() and (stop is None or not stop.is_set())


    @staticmethod
    def _log_poll_failure(run_id: str, exc: Exception) -> None:
        logger.warning(
            "progress poll failed run=%s category=%s",
            _short_id(run_id),
            type(exc).__name__,
        )


def activity_from_event(event: Any) -> str:
    """Return a fixed public category without copying event-controlled text."""
    if not isinstance(event, Mapping) or event.get("method") != "session/update":
        return "Harness activity"
    params = event.get("params")
    if not isinstance(params, Mapping):
        return "Harness activity"
    update = params.get("update")
    if not isinstance(update, Mapping):
        return "Harness activity"
    discriminator = update.get("sessionUpdate")
    if not isinstance(discriminator, str) or discriminator not in {"tool_call", "tool_call_update"}:
        return "Harness activity"
    kind = update.get("kind")
    if not isinstance(kind, str):
        return "Using a tool"
    return _TOOL_ACTIVITY.get(kind.lower(), "Using a tool")


def start_notice(*, harness: str, run_id: str) -> str:
    return f"[{_label(harness)} {_short_id(run_id)}] Run started | elapsed 0s | last event none"


def running_notice(
    *,
    harness: str,
    run_id: str,
    activity: str,
    elapsed: float,
    last_event_age: float | None,
) -> str:
    return (
        f"[{_label(harness)} {_short_id(run_id)}] {activity} | "
        f"elapsed {_seconds(elapsed)} | {_last_event(last_event_age)}"
    )


def terminal_notice(
    *,
    harness: str,
    run_id: str,
    state: str,
    returncode: Any,
    controller_error: str | None,
    activity: str,
    elapsed: float,
    last_event_age: float | None,
) -> str:
    parts = [
        f"[{_label(harness)} {_short_id(run_id)}] Run ended; review pending",
        activity,
        f"elapsed {_seconds(elapsed)}",
        _last_event(last_event_age),
        f"state {_terminal_state(state)}",
        _returncode(returncode),
    ]
    if controller_error is not None:
        parts.append(f"controller error {controller_error}")
    return " | ".join(parts)


def completion_prompt(
    *,
    harness: str,
    run_id: str,
    state: str,
    returncode: Any,
    controller_error: str | None,
    activity: str,
    elapsed: float,
    last_event_age: float | None,
) -> str:
    details = [
        f"Harness {_label(harness)} run {_short_id(run_id)} ended; review pending.",
        activity + ".",
        f"Elapsed {_seconds(elapsed)}; {_last_event(last_event_age)}.",
        f"State {_terminal_state(state)}; {_returncode(returncode)}.",
    ]
    if controller_error is not None:
        details.append(f"Controller error {controller_error}.")
    full_run_id = _full_run_id(run_id)
    details.append(
        f'Call harness_events with {{"run_id":"{full_run_id}","cursor":0,"limit":100}}; '
        "continue from next_cursor until state is terminal and has_more is false. "
        "Review those events before claiming implementation success."
    )
    return " ".join(details)


def controller_error_category(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    match = _ERROR_CATEGORY.match(value)
    if match is None:
        return "controller_error"
    return match.group(1).rsplit(".", 1)[-1]


def _wait(stop: threading.Event, timeout: float) -> bool:
    return stop.wait(timeout)


def _expired_cursor(message: str) -> int | None:
    match = _CURSOR_EXPIRED.search(message)
    return int(match.group(1)) if match is not None else None


def _age(now: float, event_at: float | None) -> float | None:
    return None if event_at is None else max(0.0, now - event_at)


def _seconds(value: float) -> str:
    return f"{int(max(0.0, value))}s"


def _last_event(age: float | None) -> str:
    return "last event none" if age is None else f"last event {_seconds(age)} ago"


def _returncode(value: Any) -> str:
    if isinstance(value, int):
        return f"returncode {value}"
    return "returncode unavailable"


def _terminal_state(value: str) -> str:
    return value if value in _TERMINAL_STATES else "ended"


def _label(value: str) -> str:
    cleaned = "".join(
        character
        for character in value[:32]
        if character.isalnum() or character in "._-"
    )
    return cleaned or "harness"


def _full_run_id(run_id: str) -> str:
    cleaned = "".join(
        character
        for character in run_id[:64]
        if character.isalnum() or character in "_-"
    )
    return cleaned or "unknown"


def _short_id(run_id: str) -> str:
    cleaned = "".join(character for character in run_id if character.isalnum())
    return (cleaned[:8] or "unknown").lower()
