from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

PermissionMode = Literal["deny_all", "approve_reads", "approve_all"]
Action = Literal["list", "start", "prompt", "status", "cancel", "close"]

_PERMISSION_FLAGS: dict[str, str] = {
    "deny_all": "--deny-all",
    "approve_reads": "--approve-reads",
    "approve_all": "--approve-all",
}


@dataclass(frozen=True, slots=True)
class HarnessSpec:
    """One named ACPX launch profile.

    Built-in ACPX profiles use ``agent``. Other ACP-compatible harnesses use
    ``command`` and are passed as one ``--agent`` value; no shell is involved.
    """

    name: str
    agent: str | None = None
    command: str | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("harness name must not be empty")
        if bool(self.agent) == bool(self.command):
            raise ValueError("exactly one of agent or command is required")

    def selector_argv(self) -> list[str]:
        if self.agent:
            return [self.agent]
        return ["--agent", str(self.command)]


def build_acpx_argv(
    *,
    acpx_argv: Sequence[str],
    spec: HarnessSpec,
    cwd: Path,
    permission_mode: PermissionMode,
    action: Action,
    session: str | None = None,
    prompt: str | None = None,
    fresh: bool = False,
) -> list[str]:
    """Build an ACPX argv vector without invoking a shell."""

    executable = [str(part) for part in acpx_argv if str(part)]
    if not executable:
        raise ValueError("acpx_argv must not be empty")

    try:
        permission_flag = _PERMISSION_FLAGS[permission_mode]
    except KeyError as exc:
        raise ValueError(f"unknown permission_mode: {permission_mode!r}") from exc

    resolved_cwd = cwd.expanduser().resolve()
    if not resolved_cwd.is_dir():
        raise ValueError(f"cwd is not an existing directory: {resolved_cwd}")

    if action != "list" and not (session or "").strip():
        raise ValueError(f"session is required for action {action!r}")

    argv = [
        *executable,
        "--cwd",
        str(resolved_cwd),
        "--format",
        "json",
        "--json-strict",
        permission_flag,
        "--non-interactive-permissions",
        "fail",
        *spec.selector_argv(),
    ]

    if action == "list":
        return [*argv, "sessions", "list", "--local"]
    if action == "start":
        verb = "new" if fresh else "ensure"
        return [*argv, "sessions", verb, "--name", str(session)]
    if action == "prompt":
        if not (prompt or "").strip():
            raise ValueError("prompt must not be empty")
        if spec.command:
            return [*argv, "prompt", "-s", str(session), str(prompt)]
        return [*argv, "-s", str(session), "prompt", str(prompt)]
    if action == "status":
        return [*argv, "status", "-s", str(session)]
    if action == "cancel":
        return [*argv, "cancel", "-s", str(session)]
    if action == "close":
        return [*argv, "sessions", "close", str(session)]
    raise ValueError(f"unknown action: {action!r}")
