# Loom HTTP API

One server, plain JSON. This page is the contract every client speaks —
the web console, loom-desktop, loom-app, OpenClaw agents, and your scripts.

## Conventions

- **Auth** — when the server runs with `--auth-token`, send
  `Authorization: Bearer <token>` (HTTP basic also works; password = token).
- **Project scoping** — multi-project servers take `?project=<id>` (or the
  `X-Loom-Project` header) on task-scoped calls. Get ids from `GET /api/projects`.
- **Errors** — non-2xx responses carry `{"ok": false, "error": "…"}`.
- **Long jobs** return `202` and run in the background; poll the resource.

## Pages

| URL | What it serves |
|-----|----------------|
| `/` | The Loom console |
| `/factory` | Research Factory portal (three lines + approvals inbox) |
| `/paper-factory` | Paper Factory (studios → ideas → papers → rounds) |
| `/review-factory` | Review Factory (panel-as-a-service) |
| `/rebuttal-factory` | Auto Rebuttal Factory |
| `/terminal?target=<pane>` | The Agent Terminal alone — factories iframe this |

## Projects and notes

| Method | URL | Purpose |
|--------|-----|---------|
| `GET` | `/api/project` | Active project root, skills path, skills options |
| `GET` | `/api/projects` | Registered projects, default, launch root |
| `POST` | `/api/projects` `{path}` | Register a project root |
| `POST` | `/api/projects/<id>/activate` | Set the default project |
| `POST` | `/api/projects/<id>/code-root` `{pattern}` | Where task worktrees are based |
| `POST` | `/api/projects/reorder` `{ids}` | Persist chip order |
| `DELETE` | `/api/projects/<id>` | Drop from registry (files untouched) |
| `GET` / `PUT` | `/api/notes` | Read / save the project's `NOTES.md` |

## Tasks

| Method | URL | Purpose |
|--------|-----|---------|
| `GET` | `/api/tasks` | All tasks for the active project |
| `POST` | `/api/tasks` `{title, general_goal, agent?, kind?}` | Create a task (auto-worktree when the root is a git repo) |
| `GET` | `/api/tasks/<slug>` | Meta + PLAN.md + markdown files + agent summary + worktree statuses |
| `PUT` | `/api/tasks/<slug>/meta` `{title?, general_goal?, agent?, skills_path?}` | Rename / re-goal / switch agent |
| `PUT` | `/api/tasks/<slug>/template` `{name, content}` | Write PLAN.md (or another task markdown) |
| `GET` | `/api/tasks/<slug>/files?path=` | Browse the task tree — one directory, or one file's text |
| `POST` | `/api/tasks/reorder` `{slugs}` | Persist sidebar order |
| `DELETE` | `/api/tasks/<slug>` | Delete the task tree (unregisters its worktrees) |
| `GET` | `/api/tasks/<slug>/diff` | Changes tab: uncommitted + committed diff per worktree |
| `POST` | `/api/tasks/<slug>/review` `{path, rules?}` | AI review of the diff vs rules / skills |
| `GET`/`POST`/`DELETE` | `/api/tasks/<slug>/monitor` | Run-monitor status / enable / disable |

For an existing-paper Author task, set `kind: "author"` and include
`author_manuscript_repo`, optional `author_experiment_repo`, `author_venue`,
and `author_main_tex` (relative to the manuscript repository). Both repository
paths must name git roots on the Loom host. The response is the normal task
metadata; `GET /api/tasks/<slug>` also includes the persisted `author` config.

### Agent pane

| Method | URL | Purpose |
|--------|-----|---------|
| `POST` | `/api/tasks/<slug>/claude/start` | Launch the agent CLI in a tmux pane |
| `POST` | `/api/tasks/<slug>/claude/stop` | Kill the pane (sessions stay resumable) |
| `POST` | `/api/tasks/<slug>/claude/paste-prompt` | Re-paste the deep-interview prompt |
| `POST` | `/api/tasks/<slug>/claude/send` `{text, submit?, rotate_context?}` | Type a message into the pane — the OpenClaw reply path. Returns `409 context_budget_exceeded` after a transcript, live-context, or per-turn budget trip unless `rotate_context` requests a fresh-session handoff. |
| `POST` | `/api/tasks/<slug>/claude/rotate` `{paste_prompt?}` | Stop the current pane, start a clean non-resumed session, and by default reload the task prompt/`PLAN.md`. |
| `POST` | `/api/tasks/<slug>/claude/resume` `{session_id, allow_oversize?}` | Fresh tmux, `--resume <id>`; oversized history requires explicit opt-in. |
| `GET` | `/api/tasks/<slug>/claude-sessions` | Tracked session ids, logical transcript footprint, estimated tokens, soft-warning threshold, hard budget and status. |
| `GET` | `/api/tasks/<slug>/conversation` | Parsed transcript of the newest session |

`/api/tasks/<slug>/interview/{start,stop,paste-prompt}` are aliases kept for
old clients.

### Worktrees

| Method | URL | Purpose |
|--------|-----|---------|
| `GET` | `/api/tasks/<slug>/worktree-candidates` | Repos a worktree could be based on |
| `POST` / `DELETE` | `/api/tasks/<slug>/worktree` | Create / remove a worktree |
| `POST` | `/api/tasks/<slug>/worktree/push` `{path}` | `git push -u origin <branch>` |
| `POST` | `/api/tasks/<slug>/worktree/merge` `{path}` | Merge into the base branch (never pushes) |
| `POST` | `/api/tasks/<slug>/worktrees/push-all` | Push every task worktree |

## Terminal and tmux

The interactive terminal is a PTY attach: the stream carries output, and
input goes back to **that same PTY** via `stream-input` (so xterm's automatic
capability replies are consumed by tmux instead of leaking into the agent).

| Method | URL | Purpose |
|--------|-----|---------|
| `GET` | `/api/tmux/stream?target=…&cols=N&rows=N` | Chunked live PTY bytes; response header `X-Loom-Terminal-Stream` is the stream id |
| `POST` | `/api/tmux/stream-input` `{stream_id, text}` | Keystrokes into the attached PTY |
| `POST` | `/api/tmux/stream-close` `{stream_id}` | Client-initiated close (proxies can swallow socket closes) |
| `GET` | `/api/tmux/capture?target=…&lines=N` | Scrollback snapshot (read-only) |
| `POST` | `/api/tmux/scroll` `{target, dir: up\|down\|bottom, lines?}` | Wheel scrolling: copy-mode for normal screens, PgUp/PgDn or real wheel events for full-screen TUIs; `bottom` returns to live before typing |
| `POST` | `/api/tmux/send-literal` `{target, text}` | Raw keystrokes (no paste buffer) |
| `POST` | `/api/tmux/send-text` `{target, text, submit?}` | Paste-buffer text delivery — the IME-safe path |
| `POST` | `/api/tmux/send-key` `{target, key}` | One named key (`Enter`, `Escape`, …) |
| `GET` | `/api/tmux/sessions` / `/api/tmux/panes?session=…` | What is running |

## Activity (the rings)

| Method | URL | Purpose |
|--------|-----|---------|
| `GET` | `/api/activity` | Which tasks are working / finished-unseen, per project |
| `POST` | `/api/activity/ack` `{slug}` | Clear a finish once the user has looked |
| `POST` | `/api/activity/finished` `{cwd, task_id?}` | Agent stop-hooks report a finish |

## Paper Factory (AR)

AR state lives in the task: `GET /api/tasks/<slug>/ar` returns the full
studio/paper state; actions POST to `/api/tasks/<slug>/ar/<action>`.

| Method | URL | Purpose |
|--------|-----|---------|
| `GET` | `/api/ar/catalog` | Venues, directions, defaults, the AR project id |
| `GET` | `/api/ar/overview` | Every studio and paper, for the fleet page |
| `GET` | `/api/ar/skills` | The injected skill catalog (AR-AUTHOR, figure skills, …) |
| `GET` | `/api/tasks/<slug>/ar` | Full AR payload (state, loop, actions, logs) |
| `POST` | `…/ar/search/suggest` | Draft arXiv search settings from the brief |
| `POST` | `…/ar/mine` | Mine recent arXiv work |
| `POST` | `…/ar/ideas` / `…/ar/venue` | Idea cards from the survey / from last cycle |
| `POST` | `…/ar/link` | Ground claims as citations, verify against OpenAlex |
| `POST` | `…/ar/spawn` `{idea_ids}` | Turn idea cards into paper tasks |
| `POST` | `…/ar/draft` | Start the first-draft author |
| `POST` | `…/ar/gate` `{gate, decision, note?}` | Approve / reject the draft or final gate |
| `POST` | `…/ar/loop` `{action: start\|stop}` | Run / pause the author↔reviewer loop |
| `POST` | `…/ar/review` | Review now (outside the loop) |
| `POST` | `…/ar/build` / `…/ar/submission` | Rebuild the PDF / prepare submission files |
| `GET` | `…/ar/pdf` | The compiled PDF |
| `GET` | `…/ar/files?path=` | Browse what the author wrote |
| `GET` | `…/ar/review/<n>` | Round n's panel reports |
| `GET` | `…/ar/skills-report` | What THIS paper was told and demonstrably used |

## Review Factory

| Method | URL | Purpose |
|--------|-----|---------|
| `GET` | `/api/review/projects` | All review projects with latest verdicts |
| `POST` | `/api/review/projects` `{path}` or `{url, venue?}` | Register a directory holding a PDF, or fetch a paper off arXiv / OpenReview / a PDF link |
| `GET` | `/api/review/projects/<id>` | State + latest report + all runs |
| `POST` | `/api/review/projects/<id>/run` | Start the three-model panel (`202`) |
| `GET` | `/api/review/projects/<id>/runs/<run>/review.md` | The assembled report (`?dl=1` downloads) |
| `GET` | `/api/review/projects/<id>/runs/<run>/panel.json` | Scores, models, deciding reviewer (`?dl=1` downloads) |
| `POST` | `/api/review/projects/<id>/submit-openreview` `{confirm?}` | Fill the venue's Official_Review form from the report — dry run without `confirm`; requires OpenReview sign-in and the reviewer role |
| `DELETE` | `/api/review/projects/<id>` | Unregister (reports stay on disk) |

The panel reviews to the venue's own form: a static family shape for all 30
catalog venues, overridden by the paper's live OpenReview form schema when the
project came off a forum link and a sign-in is cached.

## Rebuttal Factory

| Method | URL | Purpose |
|--------|-----|---------|
| `POST` | `/api/rebuttal/quick-import` `{url}` | One OpenReview forum link → venue read off the submission, studio found/created, policy discovery kicked, package fetched. Active studio: registers and starts the agent. Pending studio: stages the package; it auto-joins on policy approval |
| `GET` | `/api/rebuttal/catalog` | Stages and the default policy |
| `GET` / `POST` | `/api/rebuttal/studios` | List studios / create one (`{conference, year, cfp_url, policy_url?}`) |
| `GET` / `DELETE` | `/api/rebuttal/studios/<id>` | One studio / forget it |
| `POST` | `/api/rebuttal/studios/<id>/discover-policy` | Agent extracts the official rebuttal policy (`202`) |
| `POST` | `/api/rebuttal/studios/<id>/approve-policy` | Human gate; staged quick-import papers join and start automatically |
| `POST` | `/api/rebuttal/studios/<id>/add-paper` `{path\|url, title?}` | Add a paper (directory or OpenReview link) under the approved policy |
| `POST` | `/api/rebuttal/studios/<id>/policy` | Save a hand-edited policy draft |
| `GET` / `DELETE` | `/api/rebuttal/projects` , `…/<id>` | List / read / forget paper projects |
| `POST` | `…/<id>/start-agent` / `stop-agent` | The live rebuttal agent |
| `POST` | `…/<id>/rescan` / `validate` / `approve` | Re-read the package / deterministic checks / content approval (binds to hashes) |
| `POST` | `…/<id>/save-response` `{reviewer_id, body}` | Edit one response (invalidates approvals) |
| `POST` | `…/<id>/start-delivery` / `stop-delivery` / `verify-figures` / `approve-delivery` | The strict delivery pipeline to `submission-bundle.zip` |
| `GET` | `…/<id>/delivery/(revised-paper\|rebuttal\|supplement\|bundle\|preflight\|handoff)` | Delivery artifacts |
| `POST` | `…/<id>/submit-openreview` `{confirm?}` | Post each approved response as a forum reply — dry-run plan first, explicit confirm posts, author signature required |

## OpenReview session

Shared by both factories. The password is exchanged for a token cached at
`~/.loom/openreview-auth.json` (0600) and never stored; every openreview.net
fetch rides the token, which also skips OpenReview's datacenter-IP challenge.

| Method | URL | Purpose |
|--------|-----|---------|
| `GET` | `/api/openreview/auth` | `{logged_in, user}` |
| `POST` | `/api/openreview/login` `{username, password}` | Sign in |
| `POST` | `/api/openreview/logout` | Drop the cached token |

## Portal

| Method | URL | Purpose |
|--------|-----|---------|
| `GET` | `/api/factories/approvals` | Every human gate currently waiting, across all three factories — the "Waiting on you" inbox |

## Kernel Lab (removed)

Kernel Lab is gone: the bundle, the `/api/kernel/*` routes, the task kind
and the panel were all removed. To revisit it, check out a pre-removal
revision (before the burial commit on 2026-08-25).

## Where things live on disk

```
<project>/.RUD/
├── NOTES.md              # project-scoped scratch (📓 Notes button)
├── MEMORY.md             # lessons agents append when tasks finish
├── task-order.json
└── <slug>/
    ├── task.json         # title, goal, agent, skills, worktrees, sessions
    ├── PLAN.md           # done / results / to-do
    ├── monitor.json      # run-monitor state (only if used)
    ├── author.json       # existing-paper repositories / venue (Author tasks)
    ├── ar.json           # only for Factory (AR) tasks
    ├── rounds/round-NN/  # author notes, readiness reports, panel reviews
    └── work/<repo>/…     # git worktree, branch loom/<slug>

~/.loom/
├── web-projects.json     # registered project paths
├── openreview-auth.json  # cached OpenReview token (0600; never the password)
├── review-projects.json  # Review Factory registry
└── factories/
    ├── review/<venue>/<paper>/       # fetched papers + review-output/reviews/<run>/
    └── rebuttal/<studio>/<paper>/    # fetched forum packages (+ staged quick imports)

~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl   # agent transcripts
```

Factory (AR) tasks live in their own always-registered project (`~/ar` by
default, `LOOM_AR_ROOT` moves it) rather than inside a code repo.
