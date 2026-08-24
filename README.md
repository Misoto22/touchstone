<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/brand/touchstone-readme-hero-dark.png">
  <img src="assets/brand/touchstone-readme-hero.png" alt="touchstone: scheduled repository audit loops" width="900">
</picture>

<br />

**A scheduled audit harness.**

Repository audit loops on LangGraph.

<br />

[Loop graph](docs/graph.md) · [Example configuration](touchstone.example.toml) · [Report an issue](https://github.com/Misoto22/touchstone/issues)

<br />

[![Python 3.12+](https://img.shields.io/badge/Python_3.12%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![LangGraph 1.0+](https://img.shields.io/badge/LangGraph_1.0%2B-1C3C3C)](https://langchain-ai.github.io/langgraph/)

</div>

---

### Features

- **Finding queue** — takes the first open ledger entry before searching for a new defect.
- **Risk gates** — only an independently approved low-risk change can be armed for auto-merge.
- **Park and resume** — a medium-, high-, or rejected change becomes a checkpointed draft instead of a fresh run.
- **Diff boundaries** — protected paths, scope confinement, and translated-document twins can only raise risk.
- **Configurable execution** — runs Codex or Claude sessions locally or over SSH without changing the graph.

---

### Tech Stack

<table>
<tr><td><b>Runtime</b></td><td>Python 3.12+ · LangGraph 1.0+ · SQLite checkpointer 2.0+</td></tr>
<tr><td><b>Agent engines</b></td><td>Codex CLI · Claude CLI</td></tr>
<tr><td><b>Quality</b></td><td>pytest 8.0+ · Ruff 0.9+</td></tr>
<tr><td><b>Build</b></td><td>Hatchling</td></tr>
</table>

---

### Project Structure

```
src/touchstone/
├── nodes/                  Audit, classify, review, and publish graph steps
├── engines/                Codex and Claude execution contracts
├── execution/              Local and SSH command runners
├── graph.py                Resumable LangGraph definition
├── runner.py               Gates, worktrees, checkpoints, and teardown
└── cli.py                  run, resume, and graph commands
briefs/                     Auditing and independent-review instructions
docs/graph.md               Generated Mermaid graph checked against the code
tests/test_acceptance.py    Regression cases from unattended-run failures
touchstone.example.toml     Loop, forge, engine, and execution decisions
```

---

### Getting Started

```bash
git clone git@github.com:Misoto22/touchstone.git
cd touchstone
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
touchstone graph --check
```

**Prerequisites** — Python 3.12+ · Git

**Common tasks**

```
touchstone graph            Print the generated Mermaid graph
touchstone graph --check    Fail when docs/graph.md is stale
touchstone graph --write    Regenerate docs/graph.md from the graph definition
```

<details>
<summary><b>Development checks</b></summary>

```bash
python -m pip install -e . pytest ruff "langgraph-cli[inmem]" grandalf
python -m pytest
ruff check .
langgraph dev
```

</details>

> [!IMPORTANT]
> A dry run stops before forge publication, but it still runs the audit,
> classification, and review sessions. Point its configuration at a repository
> and credentials you are authorised to use.

---

### Configuration

Copy [`touchstone.example.toml`](touchstone.example.toml) to `touchstone.toml`,
then set the repository, forge labels, model, and execution target deliberately.
After configuring a repository you are authorised to audit:

```bash
touchstone --config touchstone.toml run code --dry-run
```

The example also documents `resume`: a parked pull request is resumed using the
thread id the run printed, with either `merge` or `close` as the answer.

---

### Documentation

[`docs/graph.md`](docs/graph.md) is generated from the compiled LangGraph.
`touchstone graph --check` verifies that the committed diagram still matches
the graph edges.

---

### Operational Boundary

Touchstone owns the graph, its checkpoints, worktrees, risk gates, and forge
transitions. The audited repository owns its own rules, ledger, credentials,
CI, deployment, and scheduler. Those project-specific decisions enter through
the configuration and briefs rather than being hardcoded here.

---

<div align="center">
<sub>Built for bounded, reviewable repository maintenance.</sub>
</div>
