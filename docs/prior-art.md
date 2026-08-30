# Prior art: Hermes coding-harness plugins

Checked on **2026-08-30** using the Hermes community plugin index, direct GitHub repository/code/issue search, and DDGS web search.

## Conclusion

No existing plugin found provides the same generic surface:

```text
Hermes tools -> one harness-neutral controller -> ACPX -> Codex/Pi/OMP/other ACP agents
```

The closest projects validate parts of the design, but they are vendor-specific, replace Hermes's model provider, depend on unmerged Hermes core work, or use a different meaning of “ACP”. A standalone generic ACPX tool plugin still fills a real gap.

## Closest projects

| Project | What it does | Why it is not equivalent |
|---|---|---|
| [Cosmic-Construct/hermes-cursor-harness](https://github.com/Cosmic-Construct/hermes-cursor-harness) | Large Cursor-specific Hermes tool plugin with durable sessions/events, approval queues, SDK-first execution, ACP fallback, and stream-json fallback. | Closest architectural precedent, but it controls only Cursor and does not expose a generic ACPX-backed harness registry. |
| [s-a-s-k-i-a/hermes-kimi-code](https://github.com/s-a-s-k-i-a/hermes-kimi-code) | Production-shaped, fail-closed `kimi_code_delegate` tool speaking ACP directly. | One read-only Kimi tool, one fresh process per call, no generic multi-harness session/status/events/cancel surface. |
| [liizfq/claude-code-acp](https://github.com/liizfq/claude-code-acp) | Claude-specific coding tool with pooled ACP sessions and a Hermes approval bridge. | One vendor and depends on generic core ACP client work that is not on official `main`. |
| [Gitnapp/hermes-claude-acp](https://github.com/Gitnapp/hermes-claude-acp) | OpenAI-compatible local bridge that invokes `acpx claude exec`. | Archived, Claude-only, provider replacement, and requires a separately run bridge daemon; it registers no Hermes tools. |
| [mvdbastos/hermes-acp-agents](https://github.com/mvdbastos/hermes-acp-agents) | Claude/Codex/Copilot model-provider registrations for a generic Hermes ACP client. | Replaces Hermes's inference provider rather than letting Hermes orchestrate child harnesses. It depends on still-open [NousResearch/hermes-agent#68222](https://github.com/NousResearch/hermes-agent/pull/68222); the required generic client files are absent from official `main`. |
| [StrawCoding/hermes-cursor-agent](https://github.com/StrawCoding/hermes-cursor-agent) | Cursor model-provider shim over `cursor-agent --output-format stream-json`. | Provider replacement, Cursor-only, and not ACPX-backed. |
| [hjcenry/hermes-cursor-agent-plugin](https://github.com/hjcenry/hermes-cursor-agent-plugin) | Hermes tool that launches Cursor Agent with stream-json. | Tool-style precedent, but Cursor-only, direct CLI rather than ACPX, and no generic persistent session/event control plane. |
| [agentic-control-plane/hermes-acp-plugin](https://github.com/agentic-control-plane/hermes-acp-plugin) | Tool-call governance, approvals, audit, and cost metering. | “ACP” means Agentic Control Plane, not Agent Client Protocol. It does not control coding harnesses. |

## Upstream Hermes work

- [#5258](https://github.com/NousResearch/hermes-agent/pull/5258) implemented an ACPX-backed generic provider path in core. It was closed because third-party integrations must ship as standalone plugins.
- [#68222](https://github.com/NousResearch/hermes-agent/pull/68222) proposes a provider-neutral ACP client plus standalone named provider plugins. It remains open.
- [#71291](https://github.com/NousResearch/hermes-agent/pull/71291) proposes a deeper ACP client runtime with persistence and approval bridging. It remains open and changes core.
- [#15597](https://github.com/NousResearch/hermes-agent/issues/15597) and [#15628](https://github.com/NousResearch/hermes-agent/pull/15628) concern Hermes-native prompt/session operator controls inspired by ACPX, not child-harness tools.

These efforts support the direction but do not provide installable `harness_list`, `harness_start`, `harness_prompt`, `harness_status`, `harness_events`, `harness_cancel`, and `harness_close` tools on stock Hermes.

## Official index result

`hermes plugins search --refresh --json` returned five indexed plugins and no matches for `acp`, `acpx`, `codex`, or `harness`. The index is not exhaustive, so the GitHub searches above were also required.

## Design implications adopted here

1. Stay a **tool plugin**, not a Hermes model provider.
2. Remain **harness-neutral**; named agents are configuration.
3. Reuse ACPX for protocol/session/queue behavior instead of reimplementing ACP.
4. Keep raw event history durable and bounded under Hermes's profile-scoped plugin data directory.
5. Make workspaces operator-allowlisted Git worktree roots.
6. Keep write-capable mode operator-owned and route each unrestricted turn through Hermes's native approval policy.
7. Require no separately administered bridge daemon.
