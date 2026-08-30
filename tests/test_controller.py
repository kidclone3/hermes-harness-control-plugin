import asyncio
import sys
from pathlib import Path

import pytest

from harness_control.controller import HarnessController


def test_prompt_returns_immediately_and_events_are_cursor_polled(
    tmp_path: Path,
) -> None:
    fake_acpx = tmp_path / "fake_acpx.py"
    fake_acpx.write_text(
        """
import json
import time

time.sleep(0.05)
print(json.dumps({"jsonrpc": "2.0", "method": "session/update", "params": {"n": 1}}), flush=True)
time.sleep(0.05)
print(json.dumps({"jsonrpc": "2.0", "id": "req-1", "result": {"stopReason": "end_turn"}}), flush=True)
""".strip(),
        encoding="utf-8",
    )

    async def exercise() -> None:
        controller = HarnessController(
            acpx_argv=(sys.executable, str(fake_acpx)),
            harnesses={"fake": {"agent": "fake"}},
        )
        started = await controller.prompt(
            harness="fake",
            cwd=tmp_path,
            session="spike",
            prompt="do the thing",
            permission_mode="deny_all",
        )
        assert started["state"] == "running"
        assert started["run_id"]

        for _ in range(50):
            page = controller.events(started["run_id"], cursor=0, limit=1)
            if page["state"] != "running":
                break
            await asyncio.sleep(0.02)
        else:
            raise AssertionError("fake ACPX process did not finish")

        assert page["state"] == "completed"
        assert page["returncode"] == 0
        assert page["next_cursor"] == 1
        assert page["has_more"] is True
        assert page["events"] == [
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {"n": 1},
            }
        ]

        second = controller.events(started["run_id"], cursor=1, limit=10)
        assert second["has_more"] is False
        assert second["next_cursor"] == 2
        assert second["events"][0]["result"]["stopReason"] == "end_turn"
        with pytest.raises(ValueError, match="beyond"):
            controller.events(started["run_id"], cursor=999, limit=10)
        await controller.shutdown()

    asyncio.run(exercise())


def test_prompt_drains_large_stderr_without_deadlocking(tmp_path: Path) -> None:
    fake_acpx = tmp_path / "fake_large_stderr.py"
    fake_acpx.write_text(
        """
import json
import sys

sys.stderr.write("x" * (1024 * 1024))
sys.stderr.flush()
print(json.dumps({"jsonrpc": "2.0", "id": "done", "result": {"stopReason": "end_turn"}}), flush=True)
""".strip(),
        encoding="utf-8",
    )

    async def exercise() -> None:
        controller = HarnessController(
            acpx_argv=(sys.executable, str(fake_acpx)),
            harnesses={"fake": {"agent": "fake"}},
        )
        started = await controller.prompt(
            harness="fake",
            cwd=tmp_path,
            session="stderr",
            prompt="emit",
            permission_mode="deny_all",
        )
        try:
            page = controller.events(started["run_id"], cursor=0, limit=10)
            for _ in range(100):
                page = controller.events(started["run_id"], cursor=0, limit=10)
                if page["state"] != "running":
                    break
                await asyncio.sleep(0.01)
            assert page["state"] == "completed"
            assert page["event_count"] == 1
            assert len(page["stderr"]) == 16_384
        finally:
            await controller.shutdown()

    asyncio.run(exercise())


def test_event_buffer_evicts_oldest_events_with_absolute_cursors(
    tmp_path: Path,
) -> None:
    fake_acpx = tmp_path / "fake_many_events.py"
    fake_acpx.write_text(
        """
import json

for index in range(5):
    print(json.dumps({"jsonrpc": "2.0", "method": "session/update", "params": {"index": index}}), flush=True)
""".strip(),
        encoding="utf-8",
    )

    async def exercise() -> None:
        controller = HarnessController(
            acpx_argv=(sys.executable, str(fake_acpx)),
            harnesses={"fake": {"agent": "fake"}},
            max_events=3,
            max_event_bytes=4096,
        )
        started = await controller.prompt(
            harness="fake",
            cwd=tmp_path,
            session="bounded",
            prompt="emit",
            permission_mode="deny_all",
        )
        await asyncio.sleep(0.1)

        with pytest.raises(ValueError, match="expired.*oldest available is 2"):
            controller.events(started["run_id"], cursor=0, limit=10)

        page = controller.events(started["run_id"], cursor=2, limit=10)
        assert page["state"] == "completed"
        assert page["oldest_cursor"] == 2
        assert page["next_cursor"] == 5
        assert page["event_count"] == 5
        assert [event["params"]["index"] for event in page["events"]] == [2, 3, 4]
        await controller.shutdown()

    asyncio.run(exercise())


def test_unload_cleanup_terminates_adapter_process_group(tmp_path: Path) -> None:
    heartbeat = tmp_path / "heartbeat.txt"
    fake_acpx = tmp_path / "fake_process_tree.py"
    fake_acpx.write_text(
        """
import json
import subprocess
import sys
import time

heartbeat = sys.argv[1]
child = subprocess.Popen([
    sys.executable,
    "-c",
    "import pathlib,sys,time; p=pathlib.Path(sys.argv[1]); "
    "[(p.open('a').write('x'), time.sleep(0.02)) for _ in iter(int, 1)]",
    heartbeat,
])
print(json.dumps({"jsonrpc": "2.0", "method": "session/update", "params": {"child_pid": child.pid}}), flush=True)
time.sleep(60)
""".strip(),
        encoding="utf-8",
    )

    async def exercise() -> None:
        controller = HarnessController(
            acpx_argv=(sys.executable, str(fake_acpx), str(heartbeat)),
            harnesses={"fake": {"agent": "fake"}},
        )
        started = await controller.prompt(
            harness="fake",
            cwd=tmp_path,
            session="cleanup",
            prompt="emit",
            permission_mode="deny_all",
        )
        for _ in range(100):
            page = controller.events(started["run_id"], cursor=0, limit=10)
            if page["events"] and heartbeat.exists():
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("fixture process tree did not start")

        controller.close_now()
        first_size = heartbeat.stat().st_size
        await asyncio.sleep(0.1)
        second_size = heartbeat.stat().st_size

        assert second_size == first_size
        final = controller.events(started["run_id"], cursor=1, limit=10)
        assert final["state"] == "cancelled"

    asyncio.run(exercise())


def test_completed_run_events_survive_controller_restart(tmp_path: Path) -> None:
    fake_acpx = tmp_path / "fake_persistent.py"
    fake_acpx.write_text(
        """
import json
print(json.dumps({"jsonrpc": "2.0", "method": "session/update", "params": {"text": "done"}}), flush=True)
""".strip(),
        encoding="utf-8",
    )

    async def exercise() -> None:
        data_dir = tmp_path / "plugin-data"
        first = HarnessController(
            acpx_argv=(sys.executable, str(fake_acpx)),
            harnesses={"fake": {"agent": "fake"}},
            data_dir=data_dir,
        )
        started = await first.prompt(
            harness="fake",
            cwd=tmp_path,
            session="durable",
            prompt="emit",
            permission_mode="deny_all",
        )
        for _ in range(100):
            page = first.events(started["run_id"], cursor=0, limit=10)
            if page["state"] != "running":
                break
            await asyncio.sleep(0.01)
        assert page["state"] == "completed"
        await first.shutdown()

        reopened = HarnessController(
            acpx_argv=(sys.executable, str(fake_acpx)),
            harnesses={"fake": {"agent": "fake"}},
            data_dir=data_dir,
        )
        recovered = reopened.events(started["run_id"], cursor=0, limit=10)
        assert recovered["state"] == "completed"
        assert recovered["events"][0]["params"]["text"] == "done"
        await reopened.shutdown()

    asyncio.run(exercise())
