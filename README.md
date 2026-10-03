<p align="center">
  <img src="loom/web_static/loom-logo.png" alt="Loom" width="200" />
</p>

<h1 align="center">Loom</h1>

<p align="center"><em>You drive Claude Code / Codex / Cursor — Loom keeps the worktrees, plans, diffs, and notes tidy.</em></p>

**Loom** is a small console for driving coding agents. Give a task a goal and
it gets a real terminal, a `PLAN.md` the deep-interview writes, its own git
worktree, a live diff, and a ping when the agent needs you. One server, three
clients, and a Research Factory that turns directions into reviewed papers.

> **Shortest path:** open a coding agent on the target machine and say
> *"follow [AGENT.md](AGENT.md) and deploy Loom"* — install, auth token,
> and a reachable URL, end to end.

## Install

Needs **Python 3.10+**, **git**, **tmux**, and one agent CLI (`claude`,
`codex`, or Cursor's `agent`) you have logged into.

```bash
pipx install git+https://github.com/FutureMLS-Lab/Loom.git
loom doctor        # names anything missing (LaTeX only matters for the Factory)
```

Hacking on Loom itself: clone, `pip install -e '.[dev]'`, `pytest tests`.

## Quickstart

```bash
loom web --project /path/to/your/project    # → http://127.0.0.1:8765
```

```
Create task ─▶ Start agent ─▶ Deep Interview ─▶ Run /goal ─▶ Write result ─▶ (repeat)
```

The interview fills `PLAN.md` (goal, an empty results table, a to-do list);
`/goal` executes it; the Changes tab shows the diff; **Push** or **Merge ↩**
when you like what you see. Loom never commits or pushes on its own.

## Three clients, one server

| Client | What it is |
|---|---|
| **Web** (built-in) | The full console at `:8765` — terminal, plans, diffs, Factory. |
| **[loom-desktop](https://github.com/FutureMLS-Lab/loom-desktop)** | macOS dock & console: live task pills, inline chat, terminal and diffs without a browser tab. |
| **[loom-app](https://github.com/FutureMLS-Lab/loom-app)** | Mobile & web client for checking the fleet and replying to agents from a phone. |

All three speak to the same server and auth token; [AGENT.md](AGENT.md) step 5
covers making it reachable (SSH tunnel / Tailscale / Cloudflare Tunnel / your
own domain).

Agents and bots get their own door: Loom's tools over **MCP** (`POST /mcp`,
or `loom mcp` over stdio) for Claude Code / Codex / Cursor / OpenClaw, and a
**concierge** you can just talk to — the `/agent` page, `loom ask "what is
waiting on me?"`, or `POST /api/agent/chat` from any bot. `loom agent-config`
prints the setup for each client; see [docs/AGENT-GATEWAY.md](docs/AGENT-GATEWAY.md).

## Research Factory

`/factory` — the front door to three production lines that share one panel
of reviewers and call each other's APIs:

```
Paper Factory    /paper-factory     brief ─▶ ideas (citations verified vs OpenAlex) ─▶ papers
                                    draft ─▶ your gate ─▶ rounds (panel review each round) ─▶ your gate
Review Factory   /review-factory    any compiled PDF ─▶ three independent reports ─▶ lowest-rating verdict
Rebuttal Factory /rebuttal-factory  venue policy ─▶ point-by-point responses ─▶ strict delivery bundle
```

**Paper Factory** — a studio mines what the field just published and proposes
grounded ideas; each idea you keep becomes a paper that drafts itself in the
venue's LaTeX, runs its own experiments, and argues with the reviewer panel
round after round behind a hard readiness gate (no placeholders, real
numbers, a page-one figure, clean build). Stalled authors are nudged back to
work; you are only interrupted at the two gates. On final approval the
manuscript is handed to the Rebuttal Factory automatically, so the reviews
have somewhere to land. Needs `latexmk` + TeX Live; methodology lives in
`loom/skills/ar/`.

The Paper Factory can also ground ideation in a generated **researcher
profile**. Select a local PDF export of a Google Scholar profile, add an
optional note, and click Generate; Loom uploads the PDF, extracts the structured
background, and activates it automatically. Strict, Balanced, and Exploratory
fit remain Studio-level choices. Reusable profiles stay host-local under
`~/.loom/researcher-profiles/`; each Studio stores a bounded snapshot so later
profile edits do not silently change an existing run. Extraction runs through
the configured Cursor model in an isolated workspace.

**Review Factory** — the same three-model panel as a service: paste an arXiv
/ OpenReview / PDF link (or a local directory), and the panel reviews to the
venue's own form — thirty CORE-A* venues carry their real sections and score
scales. Download `review.md` per run, or auto-fill the paper's OpenReview
Official_Review form behind a preview-then-confirm. The Paper Factory calls
this exact panel on every round.

## Rebuttal Factory

`/rebuttal-factory` — the same discipline pointed at the reviews you received:

```
Studio: CFP URL ─▶ policy draft ─▶ your gate
Paper:  import package ─▶ concern matrix ─▶ responses ─▶ validation ─▶ your gate
        ─▶ delivery agent ─▶ strict preflight ─▶ three-model figure check ─▶ your gate ─▶ bundle
```

One OpenReview forum link is the whole intake: the venue is read off the
submission itself, the **conference studio** is found or created, and the
venue's own author guide becomes a frozen, human-approved policy every paper
under it inherits. A live agent atomizes each review into a concern matrix
and drafts evidence-bounded point-by-point responses; deterministic
validation, a strict rebuild-and-preflight harness (full body pages,
anonymity, no placeholder values), and a unanimous three-model figure check
stand between the drafts and a byte-exact `submission-bundle.zip`. Approvals
bind to content hashes — change a file and the approval dies. Posting back
to OpenReview is built in but never automatic: sign in, preview the exact
plan, and an explicit confirm fires it.

## OpenClaw notifications

Flip **Notify** on a task and Loom pings you (e.g. in Slack, via an OpenClaw
gateway) when its agent stops and waits for input — your reply is typed
straight back into the pane. The
Factory also reports drafts ready, rounds reviewed, and stalled authors.

```bash
loom web --project /path --openclaw \
  --openclaw-url http://127.0.0.1:18789/hooks/agent \
  --openclaw-token <gateway-token>
```

## Run options

| Flag | Purpose |
|------|---------|
| `--project PATH` | Project root: a git repo, or with `--projects` a directory of repos. |
| `--skills PATH` | Default skills markdown for new tasks (the default prompt is always injected regardless). |
| `--auth-token TOKEN` | Require auth (bearer / basic, password = token). Also reads `LOOM_WEB_AUTH_TOKEN`. |
| `--daemon` | Run in the background; logs in `<project>/.RUD/web.log`. |
| `--openclaw …` | Notifications + reply-back (full flags in `loom/cli.py`). |

## Going deeper

- **[AGENT.md](AGENT.md)** — one-file deploy runbook an agent can execute.
- **[docs/API.md](docs/API.md)** — the HTTP API contract every client speaks
  (console, desktop, mobile, OpenClaw agents, your scripts), plus where
  everything lives on disk.
- **[docs/PAPER-PIPELINE.md](docs/PAPER-PIPELINE.md)** — how a paper actually
  gets written: studio steps, gates, the readiness gate, the reviewer panel.
- **[docs/OPENCLAW.md](docs/OPENCLAW.md)** — notifications in Slack and
  replying to agents from there.
- **[docs/AGENT-GATEWAY.md](docs/AGENT-GATEWAY.md)** — MCP tools and the
  concierge: how agents and bots query and drive Loom, and the safety model.
- **[docs/research-factory/arch.md](docs/research-factory/arch.md)** and
  **[docs/rebuttal-factory/arch.md](docs/rebuttal-factory/arch.md)** — each
  factory's full pipeline flowchart and module map, with its debt ledger
  in the sibling `todos.md`.
- **[docs/LOOM_CODEBASE_ARCHITECTURE.md](docs/LOOM_CODEBASE_ARCHITECTURE.md)**
  and **[docs/FIGURE_SKILL_FLOW.md](docs/FIGURE_SKILL_FLOW.md)** — how the
  codebase and the figure-skill pipeline are put together.

## Licence

Noncommercial use only, under the
[PolyForm Noncommercial 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0)
terms in [`LICENSE`](LICENSE). Commercial use needs a separate licence from
FutureMLS-Lab.
