import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

from harness_control.controller import HarnessController, _spawn_process


def test_start_invokes_acpx_and_parses_json_result(tmp_path: Path) -> None:
    fake_acpx = tmp_path / "fake_control.py"
    fake_acpx.write_text(
        """
import json
import sys

print(json.dumps({"argv": sys.argv[1:], "acpxRecordId": "record-1"}))
""".strip(),
        encoding="utf-8",
    )

    async def exercise() -> None:
        controller = HarnessController(
            acpx_argv=(sys.executable, str(fake_acpx)),
            harnesses={"omp": {"command": "omp acp"}},
        )
        result = await controller.start(
            harness="omp",
            cwd=tmp_path,
            session="review",
            permission_mode="deny_all",
            fresh=False,
        )

        assert result["success"] is True
        assert result["returncode"] == 0
        assert result["result"]["acpxRecordId"] == "record-1"
        assert result["result"]["argv"][-4:] == [
            "sessions",
            "ensure",
            "--name",
            "review",
        ]

    asyncio.run(exercise())


def test_control_failure_returns_sanitized_diagnostic(tmp_path: Path) -> None:
    fake_acpx = tmp_path / "fake_failure.py"
    fake_acpx.write_text(
        """
import sys

print("adapter unavailable", file=sys.stderr)
raise SystemExit(7)
""".strip(),
        encoding="utf-8",
    )

    async def exercise() -> None:
        controller = HarnessController(
            acpx_argv=(sys.executable, str(fake_acpx)),
            harnesses={"codex": {"agent": "codex"}},
        )
        result = await controller.status(
            harness="codex",
            cwd=tmp_path,
            session="review",
            permission_mode="deny_all",
        )

        assert result == {
            "success": False,
            "harness": "codex",
            "session": "review",
            "returncode": 7,
            "result": None,
            "stderr": "adapter unavailable",
        }

    asyncio.run(exercise())


def test_control_output_is_bounded(tmp_path: Path) -> None:
    fake_acpx = tmp_path / "fake_large_control.py"
    fake_acpx.write_text(
        """
import sys

sys.stdout.write("x" * (2 * 1024 * 1024))
sys.stdout.flush()
""".strip(),
        encoding="utf-8",
    )

    async def exercise() -> None:
        controller = HarnessController(
            acpx_argv=(sys.executable, str(fake_acpx)),
            harnesses={"codex": {"agent": "codex"}},
            max_control_output_bytes=64 * 1024,
        )
        result = await controller.status(
            harness="codex",
            cwd=tmp_path,
            session="bounded",
            permission_mode="deny_all",
        )

        assert result["success"] is False
        assert result["result"] is None
        assert result["error"] == "ACPX stdout exceeded 65536 bytes"

    asyncio.run(exercise())


def test_control_timeout_terminates_process_group(tmp_path: Path) -> None:
    fake_acpx = tmp_path / "fake_hung_control.py"
    fake_acpx.write_text(
        """
import subprocess
import sys
import time

subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
time.sleep(60)
""".strip(),
        encoding="utf-8",
    )

    async def exercise() -> None:
        controller = HarnessController(
            acpx_argv=(sys.executable, str(fake_acpx)),
            harnesses={"codex": {"agent": "codex"}},
            control_timeout_seconds=0.1,
        )
        result = await controller.status(
            harness="codex",
            cwd=tmp_path,
            session="timeout",
            permission_mode="deny_all",
        )

        assert result["success"] is False
        assert result["error"] == "ACPX control command timed out after 0.1 seconds"
        assert result["returncode"] is not None

    asyncio.run(exercise())


def test_spawn_process_sets_os_cwd_to_validated_worktree(tmp_path: Path) -> None:
    with patch("harness_control.controller.subprocess.Popen") as popen:
        _spawn_process(["acpx", "status"], cwd=tmp_path)

    assert popen.call_args.kwargs["cwd"] == str(tmp_path.resolve())
    assert popen.call_args.kwargs["shell"] is False
