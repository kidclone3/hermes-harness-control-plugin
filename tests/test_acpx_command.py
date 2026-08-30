from pathlib import Path

import pytest

from harness_control.acpx import HarnessSpec, build_acpx_argv


def test_builds_fail_closed_prompt_command_for_builtin_harness(tmp_path: Path) -> None:
    spec = HarnessSpec(name="codex", agent="codex")

    argv = build_acpx_argv(
        acpx_argv=("npx", "-y", "acpx@0.13.2"),
        spec=spec,
        cwd=tmp_path,
        permission_mode="approve_reads",
        action="prompt",
        session="backend",
        prompt="inspect the repository",
    )

    assert argv == [
        "npx",
        "-y",
        "acpx@0.13.2",
        "--cwd",
        str(tmp_path.resolve()),
        "--format",
        "json",
        "--json-strict",
        "--approve-reads",
        "--non-interactive-permissions",
        "fail",
        "codex",
        "-s",
        "backend",
        "prompt",
        "inspect the repository",
    ]


def test_builds_custom_agent_command_without_shell(tmp_path: Path) -> None:
    spec = HarnessSpec(name="omp", command="omp acp")

    argv = build_acpx_argv(
        acpx_argv=("acpx",),
        spec=spec,
        cwd=tmp_path,
        permission_mode="deny_all",
        action="start",
        session="review",
        fresh=True,
    )

    assert argv == [
        "acpx",
        "--cwd",
        str(tmp_path.resolve()),
        "--format",
        "json",
        "--json-strict",
        "--deny-all",
        "--non-interactive-permissions",
        "fail",
        "--agent",
        "omp acp",
        "sessions",
        "new",
        "--name",
        "review",
    ]


def test_places_custom_agent_prompt_options_after_subcommand(tmp_path: Path) -> None:
    spec = HarnessSpec(name="omp", command="omp acp")

    argv = build_acpx_argv(
        acpx_argv=("acpx",),
        spec=spec,
        cwd=tmp_path,
        permission_mode="approve_reads",
        action="prompt",
        session="review",
        prompt="inspect the repository",
    )

    assert argv[-4:] == [
        "prompt",
        "-s",
        "review",
        "inspect the repository",
    ]


@pytest.mark.parametrize("permission_mode", ["unknown", "", "approve-everything"])
def test_rejects_unknown_permission_mode(tmp_path: Path, permission_mode: str) -> None:
    with pytest.raises(ValueError, match="permission_mode"):
        build_acpx_argv(
            acpx_argv=("acpx",),
            spec=HarnessSpec(name="codex", agent="codex"),
            cwd=tmp_path,
            permission_mode=permission_mode,
            action="status",
            session="review",
        )
