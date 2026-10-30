import asyncio
import contextvars
import json
import logging
import os
import subprocess
import threading
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch


def _progress():
    import harness_control.progress as progress

    return progress


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def wait(self, stop: threading.Event, timeout: float) -> bool:
        if stop.is_set():
            return True
        self.now += timeout
        return False


class ScriptedController:
    def __init__(self, scripts: dict[str, list[Any]]) -> None:
        self.scripts = {run_id: deque(script) for run_id, script in scripts.items()}
        self.calls: list[tuple[str, int, int]] = []
        self.lock = threading.Lock()

    def events(self, run_id: str, *, cursor: int, limit: int) -> dict[str, Any]:
        with self.lock:
            self.calls.append((run_id, cursor, limit))
            script = self.scripts[run_id]
            if not script:
                raise AssertionError(f"unexpected poll for {run_id}")
            expected_cursor, result = script.popleft()
        assert cursor == expected_cursor
        if isinstance(result, Exception):
            raise result
        return result


class FakeHandle:
    def __init__(self, *, raises: bool = False, ok: bool = True) -> None:
        self.raises = raises
        self.ok = ok
        self.edits: list[str] = []

    def edit(self, text: str) -> dict[str, Any]:
        self.edits.append(text)
        if self.raises:
            raise RuntimeError("adapter secret must not escape")
        return {"ok": self.ok, "edited": self.ok}


class FakeContext:
    def __init__(self, *, accepted: bool = True) -> None:
        self.accepted = accepted
        self.injections: list[tuple[str, str]] = []

    def inject_message(
        self, content: str, role: str = "user", *, session_key: str | None = None
    ) -> bool:
        del role
        self.injections.append((content, session_key or ""))
        return self.accepted


def _page(
    *,
    state: str,
    cursor: int,
    events: list[dict[str, Any]] | None = None,
    has_more: bool = False,
    returncode: int | None = None,
    error: str | None = None,
    oldest_cursor: int = 0,
) -> dict[str, Any]:
    batch = events or []
    return {
        "state": state,
        "events": batch,
        "next_cursor": cursor + len(batch),
        "has_more": has_more,
        "oldest_cursor": oldest_cursor,
        "returncode": returncode,
        "error": error,
    }


def _tool_event(kind: str, *, sensitive: str = "") -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "update": {
                "sessionUpdate": "tool_call_update",
                "kind": kind,
                "title": f"private title {sensitive}",
                "rawInput": {"token": sensitive},
                "rawOutput": f"private output {sensitive}",
            }
        },
    }


def test_two_concurrent_runs_keep_parent_routes_separate() -> None:
    progress = _progress()
    controller = ScriptedController(
        {
            "aaaaaaaa1111": [(0, _page(state="completed", cursor=0, returncode=0))],
            "bbbbbbbb2222": [(0, _page(state="failed", cursor=0, returncode=9))],
        }
    )
    ctx = FakeContext()
    manager = progress.ProgressManager(controller, ctx, poll_interval=0.1)

    assert manager.start(
        run_id="aaaaaaaa1111",
        harness="codex",
        session_key="session-a",
        surface="desktop",
        message=FakeHandle(),
    )
    assert manager.start(
        run_id="bbbbbbbb2222",
        harness="claude",
        session_key="session-b",
        surface="tui",
        message=FakeHandle(),
    )
    assert manager.join("aaaaaaaa1111", timeout=1.0)
    assert manager.join("bbbbbbbb2222", timeout=1.0)

    routed = {content.split(" run ")[1][:8]: key for content, key in ctx.injections}
    assert routed == {"aaaaaaaa": "session-a", "bbbbbbbb": "session-b"}
    assert manager.active_count == 0


def test_terminal_pagination_drains_before_one_completion_attempt() -> None:
    progress = _progress()
    sensitive = "TOP-SECRET-ARGUMENT"
    first = _tool_event("read", sensitive=sensitive)
    second = _tool_event("execute", sensitive=sensitive)
    controller = ScriptedController(
        {
            "run-page": [
                (0, _page(state="completed", cursor=0, events=[first], has_more=True, returncode=0)),
                (1, _page(state="completed", cursor=1, events=[second], returncode=0)),
            ]
        }
    )
    ctx = FakeContext()
    handle = FakeHandle()
    manager = progress.ProgressManager(controller, ctx, page_limit=1)

    assert manager.start(
        run_id="run-page",
        harness="codex",
        session_key="parent",
        surface="desktop",
        message=handle,
    )
    assert manager.join("run-page", timeout=1.0)

    assert [cursor for _, cursor, _ in controller.calls] == [0, 1]
    assert len(ctx.injections) == 1
    assert len(handle.edits) == 1
    assert "Run ended; review pending" in handle.edits[0]
    assert "returncode 0" in handle.edits[0]
    assert sensitive not in handle.edits[0]
    assert sensitive not in ctx.injections[0][0]
    assert "implementation success" not in handle.edits[0].lower()


def test_completion_prompt_includes_full_run_id_and_exact_events_instruction() -> None:
    progress = _progress()
    run_id = "0123456789abcdef0123456789abcdef"
    controller = ScriptedController(
        {run_id: [(0, _page(state="completed", cursor=0, returncode=0))]}
    )
    ctx = FakeContext()
    handle = FakeHandle()
    manager = progress.ProgressManager(controller, ctx)

    assert manager.start(
        run_id=run_id,
        harness="codex",
        session_key="parent",
        surface="desktop",
        message=handle,
    )
    assert manager.join(run_id, timeout=1.0)

    [wake] = ctx.injections
    assert run_id in wake[0]
    assert (
        f'Call harness_events with {{"run_id":"{run_id}","cursor":0,"limit":100}}; '
        "continue from next_cursor until state is terminal and has_more is false."
    ) in wake[0]
    assert run_id not in handle.edits[0]


def test_empty_session_key_never_guesses_or_injects() -> None:
    progress = _progress()
    controller = ScriptedController(
        {"run-empty": [(0, _page(state="completed", cursor=0, returncode=0))]}
    )
    ctx = FakeContext()
    handle = FakeHandle()
    manager = progress.ProgressManager(controller, ctx)

    assert manager.start(
        run_id="run-empty",
        harness="pi",
        session_key="",
        surface="desktop",
        message=handle,
    )
    assert manager.join("run-empty", timeout=1.0)

    assert ctx.injections == []
    assert len(handle.edits) == 1


def test_refused_injection_is_attempted_once_and_logged(caplog: Any) -> None:
    progress = _progress()
    controller = ScriptedController(
        {"run-refused": [(0, _page(state="failed", cursor=0, returncode=3))]}
    )
    ctx = FakeContext(accepted=False)
    manager = progress.ProgressManager(controller, ctx)

    with caplog.at_level(logging.WARNING, logger="harness_control.progress"):
        assert manager.start(
            run_id="run-refused",
            harness="omp",
            session_key="parent",
            surface="desktop",
            message=FakeHandle(),
        )
        assert manager.join("run-refused", timeout=1.0)

    assert len(ctx.injections) == 1
    assert "completion injection refused" in caplog.text


def test_ui_edit_failure_does_not_block_completion_injection() -> None:
    progress = _progress()
    controller = ScriptedController(
        {"run-ui": [(0, _page(state="failed", cursor=0, returncode=4))]}
    )
    ctx = FakeContext()
    handle = FakeHandle(raises=True)
    manager = progress.ProgressManager(controller, ctx)

    assert manager.start(
        run_id="run-ui",
        harness="codex",
        session_key="parent",
        surface="desktop",
        message=handle,
    )
    assert manager.join("run-ui", timeout=1.0)

    assert len(handle.edits) == 1
    assert len(ctx.injections) == 1


def test_classic_cli_receives_only_final_edit_after_start_notice() -> None:
    progress = _progress()
    clock = FakeClock()
    controller = ScriptedController(
        {
            "run-cli": [
                (0, _page(state="running", cursor=0, events=[_tool_event("read")])),
                (1, _page(state="running", cursor=1)),
                (1, _page(state="completed", cursor=1, returncode=0)),
            ]
        }
    )
    ctx = FakeContext()
    handle = FakeHandle()
    manager = progress.ProgressManager(
        controller,
        ctx,
        poll_interval=2.0,
        update_interval=0.0,
        monotonic=clock.monotonic,
        wait=clock.wait,
    )

    assert manager.start(
        run_id="run-cli",
        harness="codex",
        session_key="parent",
        surface="cli",
        message=handle,
    )
    assert manager.join("run-cli", timeout=1.0)

    assert len(handle.edits) == 1
    assert "Run ended; review pending" in handle.edits[0]
    assert len(ctx.injections) == 1


def test_running_edits_are_throttled_and_unknown_events_are_generic() -> None:
    progress = _progress()
    clock = FakeClock()
    sensitive = "PRIVATE-REASONING"
    unknown = {
        "method": "session/update",
        "params": {"update": {"sessionUpdate": "agent_thought_chunk", "text": sensitive}},
    }
    running = [(0, _page(state="running", cursor=0, events=[unknown]))]
    running.extend((1, _page(state="running", cursor=1)) for _ in range(5))
    controller = ScriptedController(
        {"run-throttle": [*running, (1, _page(state="completed", cursor=1, returncode=0))]}
    )
    ctx = FakeContext()
    handle = FakeHandle()
    manager = progress.ProgressManager(
        controller,
        ctx,
        poll_interval=2.0,
        update_interval=10.0,
        monotonic=clock.monotonic,
        wait=clock.wait,
    )

    assert manager.start(
        run_id="run-throttle",
        harness="claude",
        session_key="parent",
        surface="desktop",
        message=handle,
    )
    assert manager.join("run-throttle", timeout=1.0)

    assert len(handle.edits) == 2
    assert "Harness activity" in handle.edits[0]
    assert "last event 10s ago" in handle.edits[0]
    assert sensitive not in "".join(handle.edits)
    assert sensitive not in ctx.injections[0][0]


def test_expired_cursor_recovers_to_oldest_and_drains_terminal_pages() -> None:
    progress = _progress()
    controller = ScriptedController(
        {
            "run-expired": [
                (0, ValueError("cursor 0 expired; oldest available is 3")),
                (
                    3,
                    _page(
                        state="completed",
                        cursor=3,
                        events=[_tool_event("search")],
                        has_more=True,
                        returncode=6,
                        oldest_cursor=3,
                    ),
                ),
                (4, _page(state="completed", cursor=4, returncode=6, oldest_cursor=3)),
            ]
        }
    )
    ctx = FakeContext()
    manager = progress.ProgressManager(controller, ctx, page_limit=1)

    assert manager.start(
        run_id="run-expired",
        harness="codex",
        session_key="parent",
        surface="tui",
        message=FakeHandle(),
    )
    assert manager.join("run-expired", timeout=1.0)

    assert [cursor for _, cursor, _ in controller.calls] == [0, 3, 4]
    assert len(ctx.injections) == 1


def test_controller_error_notice_exposes_category_not_detail() -> None:
    progress = _progress()
    sensitive = "token=do-not-show"
    controller = ScriptedController(
        {
            "run-error": [
                (
                    0,
                    _page(
                        state="failed",
                        cursor=0,
                        returncode=None,
                        error=f"RuntimeError: {sensitive}",
                    ),
                )
            ]
        }
    )
    ctx = FakeContext()
    handle = FakeHandle()
    manager = progress.ProgressManager(controller, ctx)

    assert manager.start(
        run_id="run-error",
        harness="omp",
        session_key="parent",
        surface="desktop",
        message=handle,
    )
    assert manager.join("run-error", timeout=1.0)

    delivered = handle.edits[0] + ctx.injections[0][0]
    assert "controller error RuntimeError" in delivered
    assert sensitive not in delivered


def test_close_stops_watchers_without_later_edits_or_injection() -> None:
    progress = _progress()
    entered_wait = threading.Event()

    def blocking_wait(stop: threading.Event, timeout: float) -> bool:
        del timeout
        entered_wait.set()
        return stop.wait(1.0)

    class RunningController:
        def events(self, run_id: str, *, cursor: int, limit: int) -> dict[str, Any]:
            del run_id, limit
            return _page(state="running", cursor=cursor)

    ctx = FakeContext()
    handle = FakeHandle()
    manager = progress.ProgressManager(
        RunningController(), ctx, poll_interval=2.0, wait=blocking_wait
    )
    assert manager.start(
        run_id="run-stop",
        harness="codex",
        session_key="parent",
        surface="desktop",
        message=handle,
    )
    assert entered_wait.wait(1.0)

    manager.close(join_timeout=0.2)

    assert manager.active_count == 0
    assert handle.edits == []
    assert ctx.injections == []


def test_watcher_start_failure_is_fail_open_and_leaves_no_record() -> None:
    progress = _progress()

    class BrokenThread(threading.Thread):
        def start(self) -> None:
            raise RuntimeError("thread unavailable")

    def broken_thread_factory(**kwargs: Any) -> BrokenThread:
        del kwargs
        return BrokenThread()

    manager = progress.ProgressManager(
        ScriptedController({"run-start": []}),
        FakeContext(),
        thread_factory=broken_thread_factory,
    )

    assert not manager.start(
        run_id="run-start",
        harness="codex",
        session_key="parent",
        surface="desktop",
        message=FakeHandle(),
    )
    assert manager.active_count == 0


def test_watcher_outlives_disposable_async_dispatch_loop() -> None:
    progress = _progress()
    release = threading.Event()

    class GatedController:
        def events(self, run_id: str, *, cursor: int, limit: int) -> dict[str, Any]:
            del run_id, cursor, limit
            assert release.wait(1.0)
            return _page(state="completed", cursor=0, returncode=0)

    ctx = FakeContext()
    manager = progress.ProgressManager(GatedController(), ctx)

    async def dispatch() -> None:
        assert manager.start(
            run_id="run-loop",
            harness="codex",
            session_key="parent",
            surface="desktop",
            message=FakeHandle(),
        )

    asyncio.run(dispatch())
    release.set()

    assert manager.join("run-loop", timeout=1.0)
    assert len(ctx.injections) == 1


def test_activity_classification_uses_only_structured_safe_categories() -> None:
    progress = _progress()
    assert progress.activity_from_event(_tool_event("read", sensitive="private")) == "Reading"
    assert progress.activity_from_event(_tool_event("execute", sensitive="private")) == "Running a command"
    assert progress.activity_from_event(_tool_event("unknown", sensitive="private")) == "Using a tool"
    assert progress.activity_from_event({"title": "secret arbitrary title"}) == "Harness activity"


def test_refused_ui_edit_does_not_block_completion_injection() -> None:
    progress = _progress()
    controller = ScriptedController(
        {"run-edit-refused": [(0, _page(state="completed", cursor=0, returncode=0))]}
    )
    ctx = FakeContext()
    handle = FakeHandle(ok=False)
    manager = progress.ProgressManager(controller, ctx)

    assert manager.start(
        run_id="run-edit-refused",
        harness="codex",
        session_key="parent",
        surface="desktop",
        message=handle,
    )
    assert manager.join("run-edit-refused", timeout=1.0)

    assert len(handle.edits) == 1
    assert len(ctx.injections) == 1


def test_post_notice_absent_raising_or_refused_is_fail_open() -> None:
    from harness_control.plugin import _post_progress_notice

    class NoHost:
        pass

    class RaisingHost:
        def post_message(self, text: str) -> Any:
            del text
            raise RuntimeError("gateway unavailable")

    refused = SimpleNamespace(ok=False, error="no_conversation")

    class RefusingHost:
        def post_message(self, text: str) -> Any:
            del text
            return refused

    assert asyncio.run(_post_progress_notice(NoHost(), "start")) is None
    assert asyncio.run(_post_progress_notice(RaisingHost(), "start")) is None
    assert asyncio.run(_post_progress_notice(RefusingHost(), "start")) is refused


def test_prompt_result_stays_successful_when_watcher_start_raises(tmp_path: Path) -> None:
    from harness_control import plugin

    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)

    class RegistrationContext:
        def __init__(self) -> None:
            self.tools: dict[str, dict[str, Any]] = {}
            self.unload_callbacks: list[Any] = []
            data_dir = tmp_path / "data"
            data_dir.mkdir()
            self.state = SimpleNamespace(data_dir=data_dir)

        def get_config(self, key: str, default: Any = None) -> Any:
            if key == "allowed_roots":
                return [str(tmp_path)]
            return default

        def register_tool(self, **kwargs: Any) -> None:
            self.tools[kwargs["name"]] = kwargs

        def on_unload(self, callback: Any) -> None:
            self.unload_callbacks.append(callback)

        def session_surface(self) -> str:
            return "desktop"

        def post_message(self, text: str) -> FakeHandle:
            del text
            return FakeHandle()

        def inject_message(
            self, content: str, role: str = "user", *, session_key: str | None = None
        ) -> bool:
            del content, role, session_key
            return True

    ctx = RegistrationContext()
    with (
        patch.object(
            plugin.HarnessController,
            "prompt",
            new_callable=AsyncMock,
            return_value={"run_id": "run-fail-open", "state": "running"},
        ),
        patch.object(plugin, "_current_session_key", return_value="parent"),
        patch.object(plugin.ProgressManager, "start", side_effect=RuntimeError("thread failed")),
    ):
        plugin.register(ctx)
        raw = asyncio.run(
            ctx.tools["harness_prompt"]["handler"](
                {
                    "harness": "codex",
                    "cwd": str(repo),
                    "session": "review",
                    "prompt": "inspect",
                }
            )
        )

    assert json.loads(raw) == {
        "success": True,
        "run_id": "run-fail-open",
        "state": "running",
    }


def test_blocked_edit_does_not_delay_close_or_start_completion_injection() -> None:
    progress = _progress()
    edit_entered = threading.Event()
    release_edit = threading.Event()
    close_returned = threading.Event()
    close_errors: list[BaseException] = []

    class BlockingHandle:
        def edit(self, text: str) -> dict[str, Any]:
            del text
            edit_entered.set()
            assert release_edit.wait(1.0)
            return {"ok": True, "edited": True}

    controller = ScriptedController(
        {"run-blocked-edit": [(0, _page(state="completed", cursor=0, returncode=0))]}
    )
    ctx = FakeContext()
    manager = progress.ProgressManager(controller, ctx)
    assert manager.start(
        run_id="run-blocked-edit",
        harness="codex",
        session_key="parent",
        surface="desktop",
        message=BlockingHandle(),
    )
    assert edit_entered.wait(1.0)

    def close_manager() -> None:
        try:
            manager.close(join_timeout=0.02)
        except BaseException as exc:
            close_errors.append(exc)
        finally:
            close_returned.set()

    closer = threading.Thread(target=close_manager)
    closer.start()
    try:
        assert close_returned.wait(0.15)
        assert ctx.injections == []
    finally:
        release_edit.set()
        closer.join(timeout=1.0)
    assert not closer.is_alive()
    assert close_errors == []
    assert manager.join("run-blocked-edit", timeout=1.0)
    assert ctx.injections == []


def test_start_and_close_never_join_an_unstarted_watcher() -> None:
    progress = _progress()
    start_entered = threading.Event()
    release_start = threading.Event()
    close_called = threading.Event()
    errors: list[BaseException] = []
    starts: list[bool] = []

    class BarrierThread(threading.Thread):
        def start(self) -> None:
            start_entered.set()
            assert release_start.wait(1.0)
            super().start()

    def thread_factory(**kwargs: Any) -> threading.Thread:
        return BarrierThread(**kwargs)

    class RunningController:
        def events(self, run_id: str, *, cursor: int, limit: int) -> dict[str, Any]:
            del run_id, limit
            return _page(state="running", cursor=cursor)

    manager = progress.ProgressManager(
        RunningController(),
        FakeContext(),
        poll_interval=1.0,
        thread_factory=thread_factory,
    )

    starter = threading.Thread(
        target=lambda: starts.append(
            manager.start(
                run_id="run-start-close",
                harness="codex",
                session_key="parent",
                surface="desktop",
                message=FakeHandle(),
            )
        )
    )

    def close_manager() -> None:
        close_called.set()
        try:
            manager.close(join_timeout=0.2)
        except BaseException as exc:
            errors.append(exc)

    starter.start()
    assert start_entered.wait(1.0)
    closer = threading.Thread(target=close_manager)
    closer.start()
    assert close_called.wait(1.0)
    release_start.set()
    starter.join(timeout=1.0)
    closer.join(timeout=1.0)

    assert not starter.is_alive()
    assert not closer.is_alive()
    assert starts == [True]
    assert errors == []
    assert manager.active_count == 0


def test_post_notice_uses_sync_host_api_off_dispatch_loop_with_bound_context() -> None:
    from harness_control.plugin import _post_progress_notice

    route = contextvars.ContextVar("progress_test_route", default="")
    calls: list[tuple[str, bool, int]] = []
    handle = object()

    class RecordingHost:
        def post_message(self, text: str) -> object:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                on_loop = False
            else:
                on_loop = True
            calls.append((route.get(), on_loop, threading.get_ident()))
            assert text == "start"
            return handle

    caller_thread = threading.get_ident()
    token = route.set("sync-dispatch")
    try:
        assert asyncio.run(_post_progress_notice(RecordingHost(), "start")) is handle
    finally:
        route.reset(token)

    async def from_running_loop() -> object | None:
        token = route.set("running-loop-dispatch")
        try:
            return await _post_progress_notice(RecordingHost(), "start")
        finally:
            route.reset(token)

    assert asyncio.run(from_running_loop()) is handle
    assert [item[:2] for item in calls] == [
        ("sync-dispatch", False),
        ("running-loop-dispatch", False),
    ]
    assert all(thread_id != caller_thread for _, _, thread_id in calls)


def test_unbound_route_ignores_ambient_session_key(
    monkeypatch: Any,
) -> None:
    from harness_control import plugin

    monkeypatch.setenv("HERMES_SESSION_KEY", "ambient-stale-route")
    gateway_context = SimpleNamespace(bound_session_key=lambda: "")
    legacy_context = SimpleNamespace(
        session_key=lambda: os.environ["HERMES_SESSION_KEY"]
    )

    def import_module(name: str) -> Any:
        if name == "gateway.session_context":
            return gateway_context
        if name == "hermes_cli.plugin_session":
            return legacy_context
        raise ImportError(name)

    monkeypatch.setattr(plugin.importlib, "import_module", import_module)

    assert plugin._current_session_key() == ""


def test_malformed_activity_discriminators_do_not_lose_completion() -> None:
    progress = _progress()
    for invalid in [[], {}]:
        event = _tool_event("edit")
        event["params"]["update"]["sessionUpdate"] = invalid
        controller = ScriptedController(
            {"malformed-run": [(0, _page(state="completed", cursor=0, events=[event], returncode=0))]}
        )
        ctx = FakeContext()
        handle = FakeHandle()
        manager = progress.ProgressManager(controller, ctx)
        assert manager.start(
            run_id="malformed-run", harness="codex", session_key="parent",
            surface="desktop", message=handle,
        )
        assert manager.join("malformed-run", timeout=1.0)
        assert len(ctx.injections) == 1
        assert len(handle.edits) == 1
        assert "Harness activity" in handle.edits[0]
        assert "review pending" in handle.edits[0]
        assert manager.active_count == 0


def test_prompt_resuming_after_unload_does_not_post_start_notice(tmp_path: Path) -> None:
    from harness_control import plugin

    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    tools: dict[str, Any] = {}
    callbacks: list[Any] = []
    posts: list[str] = []
    injections: list[str] = []

    def register_tool(**kwargs: Any) -> None:
        tools[kwargs["name"]] = kwargs["handler"]

    def post_message(text: str) -> FakeHandle:
        posts.append(text)
        return FakeHandle()

    ctx = SimpleNamespace(
        state=SimpleNamespace(data_dir=tmp_path / "data"),
        get_config=lambda key, default=None: [str(tmp_path)] if key == "allowed_roots" else default,
        register_tool=register_tool,
        on_unload=callbacks.append,
        post_message=post_message,
        session_surface=lambda: "desktop",
        inject_message=lambda text, **kwargs: injections.append(text),
    )

    async def exercise() -> None:
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed_prompt(**kwargs: Any) -> dict[str, Any]:
            entered.set()
            await release.wait()
            return {"run_id": "late-run", "state": "running"}

        with (
            patch.object(plugin, "HarnessController") as controller_type,
            patch.object(plugin, "_current_session_key", return_value="parent"),
        ):
            controller = controller_type.return_value
            controller.prompt = AsyncMock(side_effect=delayed_prompt)
            plugin.register(ctx)
            task = asyncio.create_task(tools["harness_prompt"]({
                "harness": "codex", "cwd": str(repo), "session": "late", "prompt": "fixture",
            }))
            await entered.wait()
            callbacks[0]()
            release.set()
            assert json.loads(await task)["success"] is True
            controller.events.assert_not_called()
        assert posts == []
        assert injections == []

    asyncio.run(exercise())
