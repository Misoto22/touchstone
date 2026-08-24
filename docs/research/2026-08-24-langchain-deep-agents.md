# LangChain Deep Agents — Touchstone fit (2026-08-24)

## Executive finding

[Deep Agents](https://www.langchain.com/deep-agents) is an open-source,
opinionated agent harness for long-running, complex tasks. The official
documentation places it above the [LangGraph runtime](https://docs.langchain.com/oss/python/concepts/products):
Deep Agents builds on LangGraph and adds predefined planning, filesystem/context
management, subagents, skills, and token-management behavior. It is therefore a
potential inner-loop capability for Touchstone, not a substitute for
Touchstone's outer audit lifecycle.

## What the official sources establish

- `create_deep_agent` provides a batteries-included tool-calling agent that can
  be extended or have pieces replaced; it is model-agnostic for models that
  support tool calling. The official repository lists subagents, pluggable
  filesystem backends, context summarization/offloading, persistent memory,
  human approval, skills, and custom/MCP tools as bundled capabilities.
  ([official repository README](https://github.com/langchain-ai/deepagents#the-batteries-included-agent-harness))
- The filesystem is virtual and backend-driven. Available choices include
  thread-scoped state, local disk, a LangGraph store, composite routing, and
  sandboxes; permissions can restrict built-in filesystem operations.
  ([official backends documentation](https://docs.langchain.com/oss/python/deepagents/backends),
  [official overview](https://docs.langchain.com/oss/python/deepagents/overview#virtual-filesystem-access))
- Subagents receive fresh/isolated context and return one final report to the
  parent. Deep Agents also supports streaming delegated-task events.
  ([official subagents documentation](https://docs.langchain.com/oss/python/deepagents/subagents),
  [official overview](https://docs.langchain.com/oss/python/deepagents/overview#subagents))
- Human approval is implemented through LangGraph interrupts via
  `interrupt_on`; approval can pause before selected tool calls and resume from
  checkpointed state.
  ([official human-in-the-loop documentation](https://docs.langchain.com/oss/python/deepagents/human-in-the-loop),
  [official production documentation](https://docs.langchain.com/oss/python/deepagents/going-to-production#durability))
- The security model is explicit: the official project says it “trusts the
  LLM,” so boundaries must be enforced by tools and sandboxes rather than by
  expecting model self-policing. Its local-shell backend runs directly on the
  host with no isolation.
  ([official security statement](https://github.com/langchain-ai/deepagents#security),
  [official backend security notes](https://docs.langchain.com/oss/python/deepagents/backends#localshellbackend-local-shell))

## Mapping to the current Touchstone architecture

| Deep Agents capability | Touchstone today | Fit / boundary |
| --- | --- | --- |
| LangGraph runtime and durable interrupts | `graph.py` uses `StateGraph`, `interrupt`, and `runner.py` uses `SqliteSaver` | Already present. Replacing the graph would discard explicit risk routing and the parked-PR resume contract. |
| Model/tool loop | `engines/base.py` exposes separate author/review contracts; Codex and Claude run through `execution` | Possible integration seam: use a Deep Agent as an implementation of an engine session, while preserving structured review output and timeout/cost metadata. |
| Filesystem and shell | Worktree creation/teardown in `runner.py`; executors support local or SSH commands | Do not grant a Deep Agent unrestricted host access. A sandbox or tightly scoped backend would be required, and its path permissions would complement—not replace—Touchstone's post-run diff gates. |
| Planning, context offload, skills | Briefs under `briefs/`; flat checkpointed `LoopState`; no general subagent layer | Potentially useful inside audit/review sessions for long investigations. It does not replace the ledger, one-finding policy, protected paths, or forge transitions. |
| Human-in-the-loop | `await_person` interrupts after a draft PR is opened; `resume` continues the same thread | Deep Agents' tool-level approval is lower-level. It could add approvals inside an agent session, but the PR-level merge/close decision remains Touchstone-owned. |
| Persistence/memory | SQLite checkpoints plus an external JSONL ledger | Deep Agents' default filesystem state is thread-scoped; cross-thread memory requires a configured store. It is not a drop-in replacement for the auditable ledger or forge state. |

## Limitations and adoption judgment

1. Deep Agents does not provide Touchstone's domain policy: risk
   classification, independent review, diff confinement, production-health
   gates, branch/worktree lifecycle, ledger semantics, or GitHub/forge actions
   remain application code.
2. Its defaults increase agent capability and therefore the security surface.
   In particular, local-shell execution has no isolation; use a sandbox and
   explicit filesystem permissions for any unattended authoring path.
3. A Deep Agent's subagent isolation and context handling are useful for
   investigation, but subagent reports are not the same as Touchstone's
   independently approved, schema-validated review verdict.
4. The repository currently declares `langgraph` and
   `langgraph-checkpoint-sqlite`, but not `deepagents`; this note makes no
   dependency or runtime change.

**Recommendation:** keep Touchstone's outer LangGraph graph and safety gates.
Evaluate Deep Agents only behind the existing `Engine` protocol (initially for
read-only audit research or a bounded author session), with a sandboxed
filesystem, explicit time/cost accounting, structured output validation, and
the existing diff/forge checks as final authorities. A full migration is not
justified by the current architecture or the official feature description.

## Official sources

- <https://www.langchain.com/deep-agents>
- <https://docs.langchain.com/oss/python/deepagents/overview>
- <https://docs.langchain.com/oss/python/deepagents/backends>
- <https://docs.langchain.com/oss/python/deepagents/subagents>
- <https://docs.langchain.com/oss/python/deepagents/human-in-the-loop>
- <https://docs.langchain.com/oss/python/deepagents/going-to-production>
- <https://docs.langchain.com/oss/python/concepts/products>
- <https://github.com/langchain-ai/deepagents>
