# Agent gateway — letting agents and bots talk to Loom

Loom is driven from a browser, but everything it knows — running agents,
tasks, worktrees, the Paper / Review / Rebuttal factories — is equally useful
to other agents and to chat bots. The gateway gives them two doors onto one
tool catalog:

| Door | For | Endpoint |
|------|-----|----------|
| **MCP** — typed tools | agents (Claude Code, Codex, Cursor, OpenClaw, Claude Desktop) | `POST /mcp` (Streamable HTTP) or `loom mcp` (stdio) |
| **Concierge** — plain language | bots, scripts, phones, people | `POST /api/agent/chat`, the `/agent` page, `loom ask` |

```
 Slack ── OpenClaw ──┐                         ┌── loom_status, list_tasks, get_task,
 Claude Code ────────┼── MCP  /mcp ───────────┤   read_conversation, read_screen,
 Codex / Cursor ─────┘                         │   paper_factory, get_paper, read_review,
                                               │   get_review_report, send_to_agent,
 curl / bots / phone ─┐                        │   create_task, start/stop_agent,
 /agent page ─────────┼─ /api/agent/chat ─ concierge ─┘   paper_loop, paper_gate, review_paper
 loom ask ────────────┘     (headless Claude Code, fenced to the loom tools)
                                               │
                                       Loom REST API  (single source of truth)
```

Code: `loom/agent_tools.py` (catalog), `loom/mcp_server.py` (protocol),
`loom/concierge.py` (chat agent), `loom/routes_agent.py` (HTTP).

## Connect a client

Run `loom agent-config` on the Loom host: it prints ready-to-paste setups for
every client below. Pass `--url` with the base URL *as the client sees Loom*
(for OpenClaw on the control host that is the tunnel end, `http://127.0.0.1:18766`).
Auth is a Bearer header. Give agents and bots the **agent token**
(`~/.loom/agent/agent-token`, created on first use, `export
LOOM_AGENT_TOKEN=$(cat ~/.loom/agent/agent-token)`): it opens `/mcp` and
`/api/agent/*` and nothing else. The server's `--auth-token` still opens
everything, and is what the local stdio server uses.

```bash
# Claude Code
claude mcp add --scope user --transport http loom http://127.0.0.1:8765/mcp \
  --header "Authorization: Bearer $LOOM_AGENT_TOKEN"

# Codex
codex mcp add loom --url http://127.0.0.1:8765/mcp --bearer-token-env-var LOOM_AGENT_TOKEN

# OpenClaw (on the gateway host, through the SSH tunnel)
openclaw mcp add loom --url http://127.0.0.1:18766/mcp --transport streamable-http \
  --header "Authorization=Bearer <agent token>" --approval auto
openclaw mcp doctor loom --probe

# Anything that launches stdio MCP servers (Claude Desktop, ...)
LOOM_URL=http://127.0.0.1:8765 LOOM_WEB_AUTH_TOKEN=... loom mcp

# Just talk to it
loom ask "what is waiting on me?"          # continues your last conversation; --new to start over
curl -s -H "Authorization: Bearer $LOOM_AGENT_TOKEN" -H "Content-Type: application/json" \
  -d '{"message": "how is the KV-cache paper doing?"}' http://127.0.0.1:8765/api/agent/chat
```

`GET /api/agent/manifest` describes the same endpoints machine-readably, with
URLs rewritten to the `Host` the caller used.

## The tools

Ten read-only tools and seven that change state. Each wraps a REST call and
**summarizes**: a raw task payload is ~500 KB and a paper's AR state ~80 KB;
tools return what an agent can act on (a paper is ~2.5 KB).

| Tool | What it returns / does |
|------|------------------------|
| `loom_status` | Start here: agents working now, finished-and-unseen, every gate waiting on you |
| `list_projects` / `list_tasks` | Projects; tasks across projects with live status |
| `get_task` | Goal, agent, worktrees (clean/dirty), PLAN.md |
| `read_conversation` | The agent's latest messages, tool calls collapsed to counts |
| `read_screen` | The live agent pane, including prompts it is blocked on |
| `paper_factory` / `get_paper` | Studios and papers; one paper's rating trajectory, panel, gates, available actions |
| `read_review` | A round's panel report — optionally one reviewer (`kimi`, `gpt`, `claude`) |
| `get_review_report` | Review Factory results |
| `send_to_agent` | Type a message into a task's agent pane and submit |
| `create_task` / `start_agent` / `stop_agent` | Task lifecycle (`stop_agent` is marked destructive) |
| `paper_loop` | Start / stop a paper's author–reviewer loop |
| `paper_gate` | Record the owner's decision at a human gate |
| `review_paper` | Review any arXiv / OpenReview / PDF link with the three-vendor panel |

Tasks are named by slug or any unique fragment of a slug or title
(`"selection-error"` finds the KV-cache paper in whichever project owns it);
an ambiguous fragment returns the candidates instead of guessing.

## Safety model

- **A narrower key for bots.** The agent token opens `/mcp` and
  `/api/agent/*` only — not the REST API behind them. OpenClaw's own docs
  warn that config literals are readable by its agent; with this token, an
  agent that reads its config still holds only the curated tools. (Tools
  reach the REST API over loopback with the server's own token.)
- **No token, no tools.** Browser POSTs to `/mcp` and `/api/agent/chat` must
  also be same-origin, so another site cannot drive them with a browser's
  cached Basic credentials.
- **Annotated tools.** Every tool carries MCP `readOnlyHint` /
  `destructiveHint`. Codex app-server — and therefore OpenClaw — auto-approves
  the read-only ones and asks before the rest.
- **What is absent on purpose:** deleting tasks / studios / projects,
  worktree merge and push, raw keystrokes, file writes, and OpenReview
  submission. Those stay human-only in the UI.
- **Gates are the owner's call.** `paper_gate` exists so a bot can relay a
  decision you made ("approve the KV paper"); the concierge is instructed
  never to decide one itself.

## The concierge

`concierge.py` runs one headless Claude Code turn per message — riding the
host's existing login, no API key — fenced to Loom:

- the Loom MCP server is its only toolset (`--strict-mcp-config`), built-in
  shell/file tools are off (`--tools ""`), only `mcp__loom` is pre-approved;
- it runs in `/tmp/loom-concierge` with user settings skipped, so it inherits
  neither the host runbook (`~/CLAUDE.md`) nor the global agent-stop hook;
- the token reaches the CLI through a 0600 MCP config file, never argv;
- each conversation is one CLI session (`--session-id`, then `--resume`), so
  follow-ups ("and which reviewer was lowest?") keep their context.

Conversations live in `~/.loom/agent/sessions/`. `/agent?session=<id>` opens
one in the browser. Tunables: `LOOM_CONCIERGE_MODEL` (default `sonnet`),
`LOOM_CONCIERGE_CLI` (default `claude`), `LOOM_CONCIERGE_WORKDIR`,
`LOOM_AGENT_HOME`. A typical turn takes 5–20 s and a few cents.
