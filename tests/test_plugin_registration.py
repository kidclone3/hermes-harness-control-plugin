import asyncio
import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from harness_control.plugin import register
from harness_control.schemas import HARNESS_PROMPT


class FakeContext:
    def __init__(self, settings: dict[str, Any] | None = None) -> None:
        self.tools: dict[str, dict[str, Any]] = {}
        self.unload_callbacks: list[Callable[[], None]] = []
        self.settings = settings or {}
        self._tempdir = TemporaryDirectory()
        self.state = SimpleNamespace(data_dir=Path(self._tempdir.name))

    def get_config(self, key: str, default: Any = None) -> Any:
        return self.settings.get(key, default)

    def register_tool(self, **kwargs: Any) -> None:
        self.tools[kwargs["name"]] = kwargs

    def spawn_task(self, coro: Any, *, name: str | None = None) -> asyncio.Task[Any]:
        return asyncio.create_task(coro, name=name)

    def on_unload(self, callback: Callable[[], None]) -> None:
        self.unload_callbacks.append(callback)


def test_registers_harness_neutral_async_tool_surface() -> None:
    ctx = FakeContext()

    register(ctx)

    assert set(ctx.tools) == {
        "harness_list",
        "harness_start",
        "harness_prompt",
        "harness_status",
        "harness_events",
        "harness_cancel",
        "harness_close",
    }
    assert all(tool["toolset"] == "harness_control" for tool in ctx.tools.values())
    assert all(tool["is_async"] is True for tool in ctx.tools.values())
    assert len(ctx.unload_callbacks) == 1


def test_registers_profile_scoped_durable_run_store() -> None:
    ctx = FakeContext()

    with patch("harness_control.plugin.HarnessController") as controller_type:
        register(ctx)

    assert controller_type.call_args.kwargs["data_dir"] == ctx.state.data_dir


def test_registration_rejects_invalid_operator_permission_mode() -> None:
    ctx = FakeContext(
        {
            "harnesses": {
                "codex": {"agent": "codex", "permission_mode": "model_decides"}
            }
        }
    )

    with pytest.raises(ValueError, match="permission_mode"):
        register(ctx)


def test_harness_list_returns_default_claude_codex_pi_and_omp_profiles() -> None:
    async def exercise() -> None:
        ctx = FakeContext()
        register(ctx)

        raw = await ctx.tools["harness_list"]["handler"]({}, task_id="test")
        payload = json.loads(raw)

        assert payload["success"] is True
        assert [item["name"] for item in payload["harnesses"]] == [
            "claude",
            "codex",
            "omp",
            "pi",
        ]
        assert next(
            item for item in payload["harnesses"] if item["name"] == "claude"
        ) == {
            "name": "claude",
            "kind": "builtin",
            "selector": "claude",
        }
        assert next(item for item in payload["harnesses"] if item["name"] == "omp") == {
            "name": "omp",
            "kind": "custom",
            "selector": "omp acp",
        }
        assert payload["policy"] == {
            "allowed_roots": [],
            "permission_modes": {
                "claude": "approve_reads",
                "codex": "approve_reads",
                "omp": "approve_reads",
                "pi": "approve_reads",
            },
            "durable_events": True,
            "unrestricted_turns_use_hermes_approval_gate": True,
        }

    asyncio.run(exercise())


def test_handler_returns_json_error_for_invalid_worktree_path() -> None:
    async def exercise() -> None:
        ctx = FakeContext()
        register(ctx)

        raw = await ctx.tools["harness_start"]["handler"](
            {"harness": "codex", "cwd": "relative/path", "session": "spike"},
            task_id="test",
        )
        payload = json.loads(raw)

        assert payload["success"] is False
        assert payload["error"] == "ValueError: cwd must be an absolute path"

    asyncio.run(exercise())


def test_handler_rejects_git_worktree_outside_operator_allowed_roots(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        outside = tmp_path / "outside"
        subprocess.run(["git", "init", "-q", str(outside)], check=True)
        ctx = FakeContext({"allowed_roots": [str(allowed)]})
        register(ctx)

        raw = await ctx.tools["harness_start"]["handler"](
            {"harness": "codex", "cwd": str(outside), "session": "prod"},
            task_id="test",
        )
        payload = json.loads(raw)

        assert payload["success"] is False
        assert "outside configured allowed_roots" in payload["error"]

    asyncio.run(exercise())


def test_prompt_schema_does_not_allow_model_to_select_permission_mode() -> None:
    assert "permission_mode" not in HARNESS_PROMPT["parameters"]["properties"]


def test_approve_all_profile_requires_human_approval_before_launch(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        repo = tmp_path / "repo"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        ctx = FakeContext(
            {
                "allowed_roots": [str(tmp_path)],
                "harnesses": {
                    "codex": {"agent": "codex", "permission_mode": "approve_all"}
                },
            }
        )
        with (
            patch(
                "harness_control.plugin._request_harness_approval",
                return_value={"approved": False, "message": "denied"},
            ) as approval,
            patch(
                "harness_control.plugin.HarnessController.prompt",
                new_callable=AsyncMock,
            ) as prompt,
        ):
            register(ctx)
            raw = await ctx.tools["harness_prompt"]["handler"](
                {
                    "harness": "codex",
                    "cwd": str(repo),
                    "session": "prod",
                    "prompt": "edit the code",
                },
                task_id="test",
            )

        payload = json.loads(raw)
        assert payload == {"success": False, "error": "denied"}
        approval.assert_called_once()
        prompt.assert_not_awaited()

    asyncio.run(exercise())


def test_approved_profile_launches_with_operator_permission_mode(tmp_path: Path) -> None:
    async def exercise() -> None:
        repo = tmp_path / "repo"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        ctx = FakeContext(
            {
                "allowed_roots": [str(tmp_path)],
                "harnesses": {
                    "codex": {"agent": "codex", "permission_mode": "approve_all"}
                },
            }
        )
        with (
            patch(
                "harness_control.plugin._request_harness_approval",
                return_value={"approved": True, "message": None},
            ),
            patch(
                "harness_control.plugin.HarnessController.prompt",
                new_callable=AsyncMock,
                return_value={"run_id": "run-1", "state": "running"},
            ) as prompt,
        ):
            register(ctx)
            raw = await ctx.tools["harness_prompt"]["handler"](
                {
                    "harness": "codex",
                    "cwd": str(repo),
                    "session": "prod",
                    "prompt": "edit the code",
                    "permission_mode": "deny_all",
                },
                task_id="test",
            )

        assert json.loads(raw)["success"] is True
        assert prompt.await_args.kwargs["permission_mode"] == "approve_all"

    asyncio.run(exercise())
