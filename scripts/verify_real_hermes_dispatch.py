"""Exercise discovery and dispatch through the installed Hermes runtime."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
HERMES_SOURCE = Path(os.environ.get("HERMES_SOURCE", "")).expanduser().resolve()
if not (HERMES_SOURCE / "hermes_cli" / "plugins.py").is_file():
    raise SystemExit("Set HERMES_SOURCE to a Hermes source checkout")
sys.path.insert(0, str(HERMES_SOURCE))


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="harness-control-e2e-") as raw_home:
        home = Path(raw_home)
        os.environ["HERMES_HOME"] = str(home)
        plugin_dir = home / "plugins" / "harness-control"
        shutil.copytree(
            PLUGIN_ROOT,
            plugin_dir,
            ignore=shutil.ignore_patterns(
                ".git",
                ".venv",
                ".pytest_cache",
                "__pycache__",
                "dist",
                "tests",
                "spikes",
            ),
        )
        fake_acpx = home / "fake_acpx.py"
        fake_acpx.write_text(
            """
import json
import sys
import time

args = sys.argv[1:]
if "prompt" in args:
    time.sleep(0.05)
    print(json.dumps({"jsonrpc": "2.0", "method": "session/update", "params": {"fixture": True}}), flush=True)
    print(json.dumps({"jsonrpc": "2.0", "id": "fixture", "result": {"stopReason": "end_turn"}}), flush=True)
elif "status" in args:
    print(json.dumps({"action": "status_snapshot", "status": "idle"}))
elif "sessions" in args:
    print(json.dumps({"acpxRecordId": "fixture-record", "acpxSessionId": "fixture-session"}))
""".strip(),
            encoding="utf-8",
        )
        config = {
            "plugins": {
                "enabled": ["harness-control"],
                "entries": {
                    "harness-control": {
                        "settings": {
                            "acpx_argv": [sys.executable, str(fake_acpx)],
                            "allowed_roots": [str(PLUGIN_ROOT)],
                            "harnesses": {
                                "fixture": {
                                    "agent": "fixture",
                                    "permission_mode": "approve_reads",
                                }
                            },
                        }
                    }
                },
            }
        }
        import yaml

        (home / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")

        from hermes_cli import plugins as plugins_mod
        from tools.registry import registry

        plugins_mod._plugin_manager = plugins_mod.PluginManager()
        plugins_mod.discover_plugins()
        manager = plugins_mod.get_plugin_manager()
        scope = manager.scope_key
        expected = {
            "harness_list",
            "harness_start",
            "harness_prompt",
            "harness_status",
            "harness_events",
            "harness_cancel",
            "harness_close",
        }
        registered = {
            name for name in expected if registry.get_entry(name, scope=scope)
        }
        assert registered == expected

        async def exercise_gateway_context() -> tuple[dict, dict, dict]:
            listed = json.loads(registry.dispatch("harness_list", {}, scope=scope))
            started = json.loads(
                registry.dispatch(
                    "harness_start",
                    {
                        "harness": "fixture",
                        "cwd": str(PLUGIN_ROOT),
                        "session": "production-check",
                    },
                    scope=scope,
                )
            )
            prompt = json.loads(
                registry.dispatch(
                    "harness_prompt",
                    {
                        "harness": "fixture",
                        "cwd": str(PLUGIN_ROOT),
                        "session": "production-check",
                        "prompt": "emit fixture events",
                    },
                    scope=scope,
                )
            )
            for _ in range(100):
                events = json.loads(
                    registry.dispatch(
                        "harness_events",
                        {"run_id": prompt["run_id"], "cursor": 0, "limit": 10},
                        scope=scope,
                    )
                )
                if events["state"] != "running":
                    break
                await asyncio.sleep(0.02)
            else:
                raise AssertionError("background prompt did not finish")
            return listed, started, events

        listed, started, events = asyncio.run(exercise_gateway_context())
        assert listed["harnesses"][0]["name"] == "fixture"
        assert started["success"] is True
        assert events["state"] == "completed", events
        assert events["event_count"] == 2
        assert events["events"][0]["method"] == "session/update"
        manager.unload()

        plugins_mod._plugin_manager = plugins_mod.PluginManager()
        plugins_mod.discover_plugins()
        reloaded_manager = plugins_mod.get_plugin_manager()
        recovered = json.loads(
            registry.dispatch(
                "harness_events",
                {"run_id": events["run_id"], "cursor": 0, "limit": 10},
                scope=reloaded_manager.scope_key,
            )
        )
        assert recovered["state"] == "completed"
        assert recovered["event_count"] == 2
        reloaded_manager.unload()

        print("REAL_HERMES_HARNESS_CONTROL_OK")
        print(f"registered_tools={len(registered)}")
        print(f"event_count={events['event_count']}")
        print(f"final_state={events['state']}")
        print(f"recovered_state={recovered['state']}")


if __name__ == "__main__":
    main()
