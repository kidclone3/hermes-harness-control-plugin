from __future__ import annotations

_COMMON_TARGET_PROPERTIES = {
    "harness": {
        "type": "string",
        "description": "Configured harness name, such as claude, codex, pi, or omp.",
    },
    "cwd": {
        "type": "string",
        "description": "Absolute path to the disposable Git worktree used by the harness.",
    },
    "session": {
        "type": "string",
        "description": "Named ACPX session within this harness and worktree.",
    },
}


def _target_schema(
    *, extra: dict | None = None, required: list[str] | None = None
) -> dict:
    properties = dict(_COMMON_TARGET_PROPERTIES)
    if extra:
        properties.update(extra)
    return {
        "type": "object",
        "properties": properties,
        "required": required or ["harness", "cwd", "session"],
        "additionalProperties": False,
    }


HARNESS_LIST = {
    "name": "harness_list",
    "description": "List named ACP harnesses configured for the Hermes harness-control plugin.",
    "parameters": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
}

HARNESS_START = {
    "name": "harness_start",
    "description": (
        "Create or ensure a named persistent ACPX session for one configured harness "
        "inside an absolute worktree path."
    ),
    "parameters": _target_schema(
        extra={
            "fresh": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Create a fresh session instead of reusing an existing "
                    "matching session."
                ),
            }
        }
    ),
}

HARNESS_PROMPT = {
    "name": "harness_prompt",
    "description": (
        "Start an asynchronous prompt on a named ACPX session and return a run_id. "
        "Poll harness_events for raw ACP JSON-RPC frames. Permission policy is "
        "operator-owned; unrestricted turns use Hermes's native approval gate."
    ),
    "parameters": _target_schema(
        extra={
            "prompt": {
                "type": "string",
                "description": "Prompt sent to the ACP harness.",
            },
        },
        required=["harness", "cwd", "session", "prompt"],
    ),
}

HARNESS_STATUS = {
    "name": "harness_status",
    "description": "Read ACPX local process/session status for one named harness session.",
    "parameters": _target_schema(),
}

HARNESS_EVENTS = {
    "name": "harness_events",
    "description": "Poll buffered raw ACP events for an asynchronous harness_prompt run.",
    "parameters": {
        "type": "object",
        "properties": {
            "run_id": {
                "type": "string",
                "description": "Run id returned by harness_prompt.",
            },
            "cursor": {"type": "integer", "minimum": 0, "default": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
        },
        "required": ["run_id"],
        "additionalProperties": False,
    },
}

HARNESS_CANCEL = {
    "name": "harness_cancel",
    "description": "Request cooperative ACP session cancellation through ACPX.",
    "parameters": _target_schema(),
}

HARNESS_CLOSE = {
    "name": "harness_close",
    "description": "Soft-close a named persistent ACPX session and its adapter process.",
    "parameters": _target_schema(),
}

ALL_SCHEMAS = (
    HARNESS_LIST,
    HARNESS_START,
    HARNESS_PROMPT,
    HARNESS_STATUS,
    HARNESS_EVENTS,
    HARNESS_CANCEL,
    HARNESS_CLOSE,
)
