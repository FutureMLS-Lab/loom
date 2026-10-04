# Agent gateway — Loom over MCP

Loom is driven from a browser, but everything it knows — running agents,
tmux sessions, tasks, worktrees, the Paper / Review / Rebuttal factories — is
equally useful to other agents and to bots. The gateway exposes it as an MCP
server, so any MCP client can read and drive Loom with typed tools:

| Transport | For | How |
|-----------|-----|-----|
| Streamable HTTP | remote agents and bots (OpenClaw, Claude Code, Codex, Cursor) | `POST /mcp` on the running `loom web` |
| stdio | local clients that launch servers as a subprocess (Claude Desktop, ...) | `loom mcp` |

```
 Slack ── OpenClaw ──┐
 Claude Code ────────┼── POST /mcp ──┐
 Codex / Cursor ─────┘               ├── 20 tools (loom/agent_tools.py) ── Loom REST API
 Claude Desktop ──── loom mcp ───────┘        (summarized, annotated)       (single source of truth)
```

Code: `loom/agent_tools.py` (the catalog), `loom/mcp_server.py` (JSON-RPC,
both transports, stdlib-only, stateless), `loom/routes_agent.py` (HTTP
routes and the agent token).

## Connect a client

Run `loom agent-config` on the Loom host: it prints ready-to-paste setups for
every client below. Pass `--url` with the base URL *as the client sees Loom*
(for OpenClaw on the control host that is the tunnel end, `http://127.0.0.1:18766`).

Auth is a Bearer header. Give agents and bots the **agent token**
(`~/.loom/agent/agent-token`, minted when the server starts; `export
LOOM_AGENT_TOKEN=$(cat ~/.loom/agent/agent-token)`): it opens `/mcp` and the
manifest and nothing else. The server's `--auth-token` still opens everything,
and is what the local stdio server uses.

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
```

### ChatGPT: OAuth, so no token is pasted anywhere

ChatGPT connects to a remote MCP server only through OAuth, so Loom runs a
small authorization server for `/mcp` (on whenever the server has an
`--auth-token`):

1. **Make Loom reachable over https.** ChatGPT connects from OpenAI's side,
   not from your machine: put Loom behind a tunnel or proxy (Cloudflare
   Tunnel, Tailscale Funnel, Caddy) and start it with that address,
   `loom web ... --public-url https://loom.example.com`. The public URL is the
   OAuth issuer, which ChatGPT compares character for character.
2. **Add the connector.** ChatGPT → Settings → Apps & Connectors → Advanced →
   Developer mode → create a connector with URL `https://loom.example.com/mcp`
   and OAuth authentication.
3. **Approve it on Loom's page.** ChatGPT registers itself and opens Loom's
   approval page, which names the app and where it sends you back. Enter this
   Loom's `--auth-token` there and Allow.

ChatGPT ends up with an access token (one hour) and a refresh token
(30 days, rotated on use) of its own. They open `/mcp` and nothing else, and
your Loom token stays on the server. **Connected apps** in the console's
sidebar, or `loom oauth list` / `loom oauth revoke <id>`, cuts an app off at
once. The same flow works for any client that speaks MCP OAuth with PKCE and
dynamic registration. Endpoints: [API.md](API.md#oauth-for-mcp-chatgpt-and-other-oauth-only-clients).

`GET /api/agent/manifest` describes the endpoint and tools machine-readably,
with URLs rewritten to the `Host` the caller used.

## The tools

Twelve read-only tools and eight that change state. Each wraps REST calls and
**summarizes**: a raw task payload is ~500 KB and a paper's AR state ~80 KB;
tools return what an agent can act on (a paper is ~2.5 KB).

| Tool | What it returns / does |
|------|------------------------|
| `loom_status` | Start here: agents working now, finished-and-unseen, every gate waiting on the owner |
| `list_projects` / `list_tasks` | Projects; tasks across projects with live status |
| `get_task` | Goal, agent, worktrees (clean/dirty), PLAN.md |
| `read_conversation` | A task agent's latest messages, tool calls collapsed to counts |
| `list_sessions` | Every tmux session, attached or not, and which task owns it; one session's pane targets |
| `read_screen` | Snapshot of a pane — a task's agent or any `session:window.pane` |
| `watch_pane` | Attach for up to N seconds and watch live; returns early when the screen matches a regex |
| `paper_factory` / `get_paper` | Studios and papers; one paper's rating trajectory, panel, gates, available actions |
| `read_review` | A round's panel report — optionally one reviewer (`kimi`, `gpt`, `claude`) |
| `get_review_report` | Review Factory results |
| `send_to_agent` | Type text into a task's agent pane or any pane, then Enter |
| `send_keys` | Press named keys: `Escape` (interrupt an agent), `C-c`, arrows, `Enter`, `Tab`, `PageUp`, F-keys, ... |
| `create_task` / `start_agent` / `stop_agent` | Task lifecycle |
| `paper_loop` | Start / stop a paper's author–reviewer loop |
| `paper_gate` | Record the owner's decision at a human gate |
| `review_paper` | Review any arXiv / OpenReview / PDF link with the three-vendor panel |

Tasks are named by slug or any unique fragment of a slug or title
(`"selection-error"` finds the KV-cache paper in whichever project owns it);
an ambiguous fragment returns the candidates instead of guessing. The pane
tools take either `task` or a raw `target` from `list_sessions`.

`watch_pane` is the agent-shaped version of attaching to a terminal: the raw
PTY stream (`/api/tmux/stream`) is xterm redraw bytes meant for a browser, so
the tool polls the rendered screen once a second instead. Keep `seconds`
under the client's tool timeout (Codex defaults to 60).

## What agents do to your tmux windows

No tool attaches a tmux client, so nothing an agent does can switch the window
you are looking at or resize one.

| Tools | Effect on the windows you are looking at |
|-------|------------------------------------------|
| `list_sessions`, `read_screen`, `watch_pane` | None: `tmux list-*` and `capture-pane` only. |
| `send_to_agent`, `send_keys` | They type into that one pane — that is the point. If you had scrolled up in it, tmux leaves copy-mode first so the keys reach the program, so your view returns to the bottom; scrollback is kept. Text goes through a private, self-deleting paste buffer, never your own. |
| `start_agent`, `stop_agent` | Create or kill that task's agent pane. |

## Safety model

- **A narrower key for bots.** The agent token opens `/mcp` and the manifest
  only — not the REST API behind them. OpenClaw's own docs warn that config
  literals are readable by its agent; with this token, an agent that reads its
  config still holds only the curated tools. Tool calls reach the REST API
  over loopback with the server's own token.
- **No token, no tools.** Browser POSTs to `/mcp` must also be same-origin,
  so another site cannot drive it with a browser's cached Basic credentials.
- **OAuth clients never see a Loom token.** The owner approves on Loom's own
  page with the `--auth-token`; the client gets short-lived tokens bound to
  this server's `/mcp` (stored hashed, revocable, PKCE S256 only, codes
  single-use, a replayed refresh token revokes the grant). The approval page
  cannot be framed and posts only to Loom and the redirect the app
  registered; repeated wrong tokens pause approvals.
- **Annotated tools.** Every tool carries MCP `readOnlyHint` /
  `destructiveHint`; `send_keys` and `stop_agent` are marked destructive (a
  key can approve a prompt or kill a process). Codex app-server — and so
  OpenClaw — auto-approves the read-only tools and asks before the rest.
- **What is absent on purpose:** deleting tasks / studios / projects,
  worktree merge and push, file writes, and OpenReview submission. Those
  stay human-only in the UI.
- **Gates are the owner's call.** `paper_gate` exists so a bot can relay a
  decision the owner made ("approve the KV paper"), never to decide one.
