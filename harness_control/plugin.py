from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, cast

from .acpx import PermissionMode
from .controller import HarnessController
from .schemas import ALL_SCHEMAS
from .workspace import WorkspacePolicy

DEFAULT_ACPX_ARGV = ["npx", "-y", "acpx@0.13.2"]
DEFAULT_HARNESSES: dict[str, dict[str, str]] = {
    "codex": {"agent": "codex", "permission_mode": "approve_reads"},
    "pi": {"agent": "pi", "permission_mode": "approve_reads"},
    "omp": {"command": "omp acp", "permission_mode": "approve_reads"},
}
_ALLOWED_PERMISSION_MODES = {"deny_all", "approve_reads", "approve_all"}


def register(ctx: Any) -> None:
    """Register the production harness-neutral ACPX tool surface."""

    acpx_argv = _load_acpx_argv(ctx.get_config("acpx_argv", DEFAULT_ACPX_ARGV))
    harnesses = _load_harnesses(ctx.get_config("harnesses", DEFAULT_HARNESSES))
    workspace_policy = WorkspacePolicy(
        _load_allowed_roots(ctx.get_config("allowed_roots", []))
    )
    controller = HarnessController(
        acpx_argv=acpx_argv,
        harnesses=harnesses,
        data_dir=ctx.state.data_dir,
    )

    async def harness_list(args: dict[str, Any], **kwargs: Any) -> str:
        del args, kwargs
        permission_modes = {
            name: _configured_permission_mode(harnesses, name)
            for name in sorted(harnesses)
        }
        return _json(
            {
                "success": True,
                "harnesses": controller.list_harnesses(),
                "policy": {
                    "allowed_roots": [
                        str(path) for path in workspace_policy.allowed_roots
                    ],
                    "permission_modes": permission_modes,
                    "durable_events": True,
                    "unrestricted_turns_use_hermes_approval_gate": True,
                },
            }
        )

    async def harness_start(args: dict[str, Any], **kwargs: Any) -> str:
        del kwargs
        return await _call(
            lambda: controller.start(
                harness=_required_string(args, "harness"),
                cwd=_absolute_directory(args, workspace_policy),
                session=_required_string(args, "session"),
                permission_mode="deny_all",
                fresh=bool(args.get("fresh", False)),
            )
        )

    async def harness_prompt(args: dict[str, Any], **kwargs: Any) -> str:
        del kwargs
        try:
            harness = _required_string(args, "harness")
            cwd = _absolute_directory(args, workspace_policy)
            session = _required_string(args, "session")
            prompt = _required_string(args, "prompt")
            permission_mode = _configured_permission_mode(harnesses, harness)
            if permission_mode == "approve_all":
                approval = _request_harness_approval(
                    harness=harness,
                    cwd=cwd,
                    session=session,
                )
                if not approval.get("approved"):
                    return _json(
                        {
                            "success": False,
                            "error": approval.get("message") or "approval denied",
                        }
                    )
            result = await controller.prompt(
                harness=harness,
                cwd=cwd,
                session=session,
                prompt=prompt,
                permission_mode=cast(PermissionMode, permission_mode),
            )
            return _json({"success": True, **result})
        except (OSError, TypeError, ValueError) as exc:
            return _json({"success": False, "error": f"{type(exc).__name__}: {exc}"})

    async def harness_status(args: dict[str, Any], **kwargs: Any) -> str:
        del kwargs
        return await _call(
            lambda: controller.status(
                harness=_required_string(args, "harness"),
                cwd=_absolute_directory(args, workspace_policy),
                session=_required_string(args, "session"),
                permission_mode="deny_all",
            )
        )

    async def harness_events(args: dict[str, Any], **kwargs: Any) -> str:
        del kwargs
        try:
            result = controller.events(
                _required_string(args, "run_id"),
                cursor=int(args.get("cursor", 0)),
                limit=int(args.get("limit", 100)),
            )
            return _json({"success": True, **result})
        except (TypeError, ValueError) as exc:
            return _json({"success": False, "error": str(exc)})

    async def harness_cancel(args: dict[str, Any], **kwargs: Any) -> str:
        del kwargs
        return await _call(
            lambda: controller.cancel(
                harness=_required_string(args, "harness"),
                cwd=_absolute_directory(args, workspace_policy),
                session=_required_string(args, "session"),
                permission_mode="deny_all",
            )
        )

    async def harness_close(args: dict[str, Any], **kwargs: Any) -> str:
        del kwargs
        return await _call(
            lambda: controller.close(
                harness=_required_string(args, "harness"),
                cwd=_absolute_directory(args, workspace_policy),
                session=_required_string(args, "session"),
                permission_mode="deny_all",
            )
        )

    handlers = {
        "harness_list": harness_list,
        "harness_start": harness_start,
        "harness_prompt": harness_prompt,
        "harness_status": harness_status,
        "harness_events": harness_events,
        "harness_cancel": harness_cancel,
        "harness_close": harness_close,
    }
    for schema in ALL_SCHEMAS:
        name = cast(str, schema["name"])
        ctx.register_tool(
            name=name,
            toolset="harness_control",
            schema=schema,
            handler=handlers[name],
            is_async=True,
        )

    ctx.on_unload(controller.close_now)


async def _call(
    make_awaitable: Callable[[], Awaitable[Any]], *, add_success: bool = False
) -> str:
    try:
        result = await make_awaitable()
        if add_success:
            result = {"success": True, **result}
        return _json(result)
    except (OSError, TypeError, ValueError) as exc:
        return _json({"success": False, "error": f"{type(exc).__name__}: {exc}"})


def _absolute_directory(
    args: Mapping[str, Any], workspace_policy: WorkspacePolicy
) -> Path:
    raw = _required_string(args, "cwd")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ValueError("cwd must be an absolute path")
    return workspace_policy.validate(path)


def _required_string(args: Mapping[str, Any], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


def _load_acpx_argv(raw: Any) -> tuple[str, ...]:
    if (
        not isinstance(raw, list)
        or not raw
        or not all(isinstance(item, str) and item for item in raw)
    ):
        raise ValueError("acpx_argv must be a non-empty list of strings")
    return tuple(raw)


def _load_allowed_roots(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, list) or not all(
        isinstance(item, str) and item.strip() for item in raw
    ):
        raise ValueError("allowed_roots must be a list of non-empty paths")
    return tuple(raw)


def _configured_permission_mode(
    harnesses: Mapping[str, Mapping[str, Any]], harness: str
) -> str:
    try:
        raw = harnesses[harness].get("permission_mode", "approve_reads")
    except KeyError as exc:
        known = ", ".join(sorted(harnesses)) or "none"
        raise ValueError(f"unknown harness {harness!r}; configured: {known}") from exc
    if not isinstance(raw, str) or raw not in _ALLOWED_PERMISSION_MODES:
        raise ValueError(
            f"harness {harness!r} permission_mode must be one of "
            f"{', '.join(sorted(_ALLOWED_PERMISSION_MODES))}"
        )
    return raw


def _request_harness_approval(
    *, harness: str, cwd: Path, session: str
) -> dict[str, Any]:
    from tools.approval import request_tool_approval

    cwd_hash = hashlib.sha256(str(cwd).encode("utf-8")).hexdigest()[:16]
    return request_tool_approval(
        "harness_prompt",
        (
            f"Allow {harness!r} session {session!r} to run one unrestricted "
            f"ACPX turn in {cwd}? The harness may edit files and execute commands."
        ),
        rule_key=f"harness-turn:{harness}:{cwd_hash}",
    )


def _load_harnesses(raw: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, Mapping) or not raw:
        raise ValueError("harnesses must be a non-empty mapping")
    result: dict[str, dict[str, Any]] = {}
    for name, value in raw.items():
        if (
            not isinstance(name, str)
            or not name.strip()
            or not isinstance(value, Mapping)
        ):
            raise ValueError("each harness must be a named mapping")
        result[name] = dict(value)
    for name in result:
        _configured_permission_mode(result, name)
    return result


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)
