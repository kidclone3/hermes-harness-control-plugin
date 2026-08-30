# Hermes Harness Control Plugin

A standalone Hermes Agent plugin that lets Hermes control multiple ACP-compatible coding harnesses through [ACPX](https://github.com/openclaw/acpx).

Hermes remains the orchestrator. ACPX owns ACP framing, adapter launch, named sessions, queues, cancellation, reconnect/resume behavior, permission flags, and raw structured events.

## Status

Version **0.1.0** is the production transition from the validated spike. It targets stock Hermes native plugins and ACPX **0.13.2** (the current npm `latest` version when verified on 2026-08-30).

The plugin has no separately administered daemon. It launches ACPX directly with `subprocess.Popen(..., shell=False)`.

## Tools

| Tool | Purpose |
|---|---|
| `harness_list` | List configured named harnesses. |
| `harness_start` | Create or ensure a named persistent ACPX session. |
| `harness_prompt` | Start a prompt asynchronously and return a `run_id`. |
| `harness_status` | Read ACPX local session/process status. |
| `harness_events` | Poll bounded raw ACP JSON events by absolute cursor. |
| `harness_cancel` | Request cooperative ACP cancellation. |
| `harness_close` | Soft-close the named ACPX session. |

Defaults:

- `codex` -> ACPX built-in `codex`
- `pi` -> ACPX built-in `pi`
- `omp` -> custom ACP server command `omp acp`

Other ACP agents can be added through operator configuration.

## Security boundary

- `cwd` must be an absolute **Git worktree root** beneath an operator-configured `allowed_roots` entry.
- The ACPX subprocess receives the validated path both as `--cwd` and as its operating-system `cwd`.
- ACPX is invoked with an argv vector and no shell.
- Machine output uses `--format json --json-strict`.
- Permission mode is operator-owned per harness. It is not exposed in the model-facing tool schema.
- Default `approve_reads` permits ACPX-classified read/search operations and fails when non-interactive write approval is needed.
- `approve_all` routes each unrestricted harness turn through Hermes's native approval gate. Approval is coarse-grained for the whole turn, not each internal command. Hermes's normal yolo, cron, single-query, session, and permanent approval policy still applies.
- Event, stderr, control-output, line-size, and run-count limits are enforced. Control commands time out after 120 seconds by default; asynchronous prompt runs have no plugin wall-clock timeout and must be monitored or cancelled explicitly.
- Prompt and control processes use isolated process groups on POSIX and are terminated on plugin unload.
- Raw events are sensitive project data. They are stored locally with restricted permissions in Hermes's profile-scoped plugin data directory: `$HERMES_HOME/plugin-data/<plugin-namespace>/runs.sqlite3`.

With no `allowed_roots` configured, all workspace operations fail closed.

## Prerequisites

- Hermes Agent with native `plugin.yaml` support
- Python 3.11+
- Node.js 22.13+
- `npx`
- Installed/authenticated target harnesses
- `git`

The default runtime is pinned:

```text
npx -y acpx@0.13.2
```

## Install

### Published Git repository

Once the repository has a remote:

```bash
hermes plugins install <owner>/hermes-harness-control-plugin --enable
```

The wheel intentionally relies on the `hermes_agent.plugins` entry point; `plugin.yaml` is used by directory/Git installations and is included in the source distribution, not the wheel.

### Local checkout

For local verification, link the checkout into the active profile and enable it:

```bash
ln -s /absolute/path/to/hermes-harness-control-plugin \
  "${HERMES_HOME:-$HOME/.hermes}/plugins/harness-control"
hermes plugins enable harness-control
```

A running Hermes process must be restarted to discover a newly installed plugin.

## Configure

Set at least one allowed parent directory. Do not hand-edit `config.yaml`:

```bash
hermes config set --force \
  plugins.entries.harness-control.settings.allowed_roots \
  '["/absolute/path/to/worktrees"]'
```

Configure named harnesses and their operator-owned permission modes:

```bash
hermes config set --force \
  plugins.entries.harness-control.settings.harnesses \
  '{"codex":{"agent":"codex","permission_mode":"approve_reads"},"pi":{"agent":"pi","permission_mode":"approve_reads"},"omp":{"command":"omp acp","permission_mode":"approve_reads"}}'
```

Supported permission modes:

- `deny_all`
- `approve_reads`
- `approve_all` — routes the turn through Hermes's native approval policy before launch

To use a globally installed ACPX binary instead of the pinned `npx` command:

```bash
hermes config set --force \
  plugins.entries.harness-control.settings.acpx_argv \
  '["acpx"]'
```

## Example flow

```json
{"harness":"codex","cwd":"/absolute/worktree","session":"backend","fresh":false}
```

Then prompt:

```json
{"harness":"codex","cwd":"/absolute/worktree","session":"backend","prompt":"Inspect the failing tests and report the root cause."}
```

Poll the returned run id:

```json
{"run_id":"<run_id>","cursor":0,"limit":100}
```

Continue from `next_cursor` until `state` is terminal and `has_more` is false.

## Durable events and restart behavior

Each event gets a monotonically increasing absolute cursor. Per-run retention defaults to 2,000 events and 4 MiB; at most 32 runs are retained. Expired cursors return an explicit error with the oldest available cursor.

Completed events survive Hermes restart. A run left `running` when a new plugin instance starts is marked `interrupted`; the plugin does not pretend the old collector process is still attached. The underlying named ACPX session may still be resumable through ACPX.

## Development and verification

```bash
UV_CACHE_DIR="$HOME/.cache/uv" uv run --with pytest python -m pytest -q
UV_CACHE_DIR="$HOME/.cache/uv" uvx ruff check .
UV_CACHE_DIR="$HOME/.cache/uv" uvx mypy harness_control --ignore-missing-imports
hermes plugins doctor . --ci
HERMES_SOURCE=/home/delus/.hermes/hermes-agent \
  /home/delus/.hermes/hermes-agent/.venv/bin/python \
  scripts/verify_real_hermes_dispatch.py
UV_CACHE_DIR="$HOME/.cache/uv" uv build
```

The integration harness uses a temporary `HERMES_HOME`, normal plugin discovery, the real scoped Hermes tool registry, a real Git worktree path, and a deterministic ACPX boundary fake. It dispatches while an event loop is already running and verifies durable event recovery after plugin reload.

See [docs/prior-art.md](docs/prior-art.md) for the existing-plugin search and design comparison.

## Current limits

- Hermes's approval gate authorizes one unrestricted ACPX turn as a unit; it does not bridge each inner ACP permission request back into Hermes.
- Live Codex was exercised by the spike. Pi and OMP adapter lifecycle was exercised, but their full edit/command/cancel/resume matrix still requires live conformance runs.
- POSIX/WSL process-group cleanup is covered. Native Windows process-tree cleanup is not yet live-verified.
- Events remain raw ACPX JSON. Stable cross-harness event normalization is deferred.
- This repository has no public remote yet, so immutable Git-install verification must wait until it is published.

## License

MIT
