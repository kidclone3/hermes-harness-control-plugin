from pathlib import Path

import pytest

from harness_control.acpx import HarnessSpec, build_acpx_argv


def test_builds_fail_closed_prompt_command_for_builtin_harness(tmp_path: Path) -> None:
    spec = HarnessSpec(name="codex", agent="codex")

    argv = build_acpx_argv(
        acpx_argv=("npx", "-y", "acpx@0.21.1"),
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
        "acpx@0.21.1",
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


def test_claude_model_is_operator_owned_and_forwarded_to_acpx(tmp_path: Path) -> None:
    argv = build_acpx_argv(
        acpx_argv=("npx", "-y", "acpx@0.21.1"),
        spec=HarnessSpec(name="claude", agent="claude", model="claude-opus-5-5"),
        cwd=tmp_path,
        permission_mode="approve_reads",
        action="prompt",
        session="review",
        prompt="inspect",
    )
    assert argv[8:10] == ["--model", "claude-opus-5-5"]
    assert argv[-5:] == ["claude", "-s", "review", "prompt", "inspect"]


def test_claude_custom_adapter_keeps_session_option_after_prompt(tmp_path: Path) -> None:
    adapter = "npx -y @agentclientprotocol/claude-agent-acp@0.99.1"
    argv = build_acpx_argv(
        acpx_argv=("npx", "-y", "acpx@0.21.1"),
        spec=HarnessSpec(name="claude", command=adapter, model="claude-opus-5-5"),
        cwd=tmp_path,
        permission_mode="approve_reads",
        action="prompt",
        session="review",
        prompt="inspect",
    )
    assert argv[-6:] == ["--agent", adapter, "prompt", "-s", "review", "inspect"]


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
