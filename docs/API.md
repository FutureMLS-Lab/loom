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
| `POST` | `/api/projects` `{path, mode?, repo_url?, code_root_pattern?, git_init?}` | Register a project root (see below) |
| `POST` | `/api/projects/<id>/activate` | Set the default project |
| `POST` | `/api/projects/<id>/code-root` `{pattern}` | Where task worktrees are based |
| `POST` | `/api/projects/reorder` `{ids}` | Persist chip order |
| `DELETE` | `/api/projects/<id>` | Drop from registry (files untouched) |
| `GET` / `PUT` | `/api/notes` | Read / save the project's `NOTES.md` |

`mode` is `existing` (the default: register a folder as it is), `empty`
(create the folder) or `clone` (`git clone` `repo_url` into it); the last two
must stay inside the launch directory. An `empty` folder also becomes a git
repository with one empty commit, "Initial commit", so its tasks have a HEAD
to branch their worktrees from — unless `git_init: false`, or the folder is
already inside a git work tree. With no git identity configured, that commit
is signed `<os user> <<os user>@<hostname>>` for that one command; no git
config is written. `.RUD/` goes into the new repo's `.git/info/exclude`.
`201` → `{id, defaultProjectId, projects}`, and for `empty` also
`git_initialized` (bool) plus `git_error` when git failed. A git failure never
blocks registration: the project is registered either way.

## Tasks

| Method | URL | Purpose |
|--------|-----|---------|
| `GET` | `/api/tasks` | All tasks for the active project |
| `POST` | `/api/tasks` `{title, general_goal, agent?, kind?, source_repo?}` | Create a task in its own worktree (see [Worktrees](#worktrees)) |
| `GET` | `/api/task-preview?title=&source_repo=` | What creating that task would do — read-only |
| `GET` | `/api/tasks/<slug>` | Meta + PLAN.md + markdown files + agent summary + worktree statuses |
| `PUT` | `/api/tasks/<slug>/meta` `{title?, general_goal?, agent?, skills_path?}` | Rename / re-goal / switch agent |
| `PUT` | `/api/tasks/<slug>/template` `{name, content}` | Write PLAN.md (or another task markdown) |
| `GET` | `/api/tasks/<slug>/files?path=` | Browse the task tree — one directory, or one file's text |
| `POST` | `/api/tasks/reorder` `{slugs}` | Persist sidebar order |
| `DELETE` | `/api/tasks/<slug>` | Delete the task tree (unregisters its worktrees) |
| `GET` | `/api/tasks/<slug>/diff` | Changes tab: uncommitted + committed diff per worktree |
| `GET` | `/api/task-changes` | Change counts for every task's sidebar row, in one poll |
| `POST` | `/api/tasks/<slug>/review` `{path, rules?}` | AI review of the diff vs rules / skills |
| `GET`/`POST`/`DELETE` | `/api/tasks/<slug>/monitor` | Run-monitor status / enable / disable |

### Agent pane

| Method | URL | Purpose |
|--------|-----|---------|
| `POST` | `/api/tasks/<slug>/claude/start` | Launch the agent CLI in a tmux pane |
| `POST` | `/api/tasks/<slug>/claude/stop` | Kill the pane (sessions stay resumable) |
| `POST` | `/api/tasks/<slug>/claude/paste-prompt` | Re-paste the deep-interview prompt |
| `POST` | `/api/tasks/<slug>/claude/send` `{text, submit?}` | Type a message into the pane — the OpenClaw reply path |
| `POST` | `/api/tasks/<slug>/claude/resume` `{session_id}` | Fresh tmux, `--resume <id>` |
| `GET` | `/api/tasks/<slug>/claude-sessions` | Tracked session ids + transcripts |
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

**Create.** Every agent task gets `work/<repo>` on branch `loom/<slug>`,
branched from the HEAD of the project's code root — or of `source_repo`, which
must be one of the project's worktree candidates (else `400` with `allowed`,
before anything is created). Paper Factory (AR) tasks get no worktree and
ignore `source_repo`. The fork commit is recorded in `meta.worktree_bases`.
When the project's `.RUD/` lies inside the source repo (project root == repo
root, typically), an anchored entry for it goes into that repo's
`.git/info/exclude`, so the user's own `git status` stays clean;
`.gitignore` is never edited.
`201` → `{meta, worktree_created, worktree_warning?}`: `meta.worktrees`
already lists the new worktree, and `worktree_warning` — one plain sentence —
appears only when an agent task got none (the source is not a git repository,
has no commits yet, or git refused), because its agent would then edit the
folder itself.

**Preview.** `GET /api/task-preview?project=<id>&title=<title>[&source_repo=<path>]`
→

```json
{
  "ok": true,
  "slug": "fix-the-bug",
  "branch": "loom/fix-the-bug",
  "task_dir": "<project>/.RUD/fix-the-bug",
  "worktree_dest": "<project>/.RUD/fix-the-bug/work/<repo>",
  "code_root": "<code root>",
  "source": {"path": "<repo>", "name": "<repo name>", "is_git": true,
             "branch": "main", "head": "<sha>", "head_short": "<short sha>",
             "dirty": 1, "untracked": 0},
  "candidates": [{"path": "<repo>", "name": "<repo name>", "kind": "self",
                  "branch": "main", "head_short": "<short sha>"}]
}
```

- Creates, reserves and records nothing. `slug` is what creation would give
  right now; a task created in between takes it, and creation then gets `-2`.
- A blank `title` leaves `slug`, `branch`, `task_dir` and `worktree_dest` as
  `""` (creating would `400`); everything else is filled in.
- `source` is the git toplevel the worktree would come from (`source_repo`,
  else the code root). `branch` is `""` on a detached HEAD; `head` /
  `head_short` are `""` before the first commit. `dirty` counts tracked files
  with uncommitted changes and `untracked` untracked entries (not `.RUD/`) —
  work that will not be in the new worktree. Both are `null` when `git status`
  fails or takes longer than 5 s.
- Outside git: `is_git: false`, `branch` / `head` / `head_short` `""`,
  `dirty` / `untracked` `null`.
- When no worktree would be made, `worktree_dest` is `""` and
  `worktree_warning` carries the sentence creation would return.
- `source_repo` not among the candidates → `400` with `allowed`.

**Change counts.** `GET /api/task-changes?project=<id>` → one entry per task:

```json
{"ok": true, "tasks": {
  "fix-the-bug": {"branch": "loom/fix-the-bug", "worktrees": 1,
                  "files": 3, "insertions": 6, "deletions": 1,
                  "uncommitted": 2, "unknown_base": 0, "pending": false},
  "notes-only": {"worktrees": 0}
}}
```

- `files` / `insertions` / `deletions`: the net change from each worktree's
  recorded fork commit to its working tree — committed, staged, unstaged, and
  untracked files as additions (lines counted for text files up to 1 MiB,
  16 MiB per worktree) — summed over the task's worktrees.
- `uncommitted`: changed plus untracked files not yet committed.
- `unknown_base`: worktrees left out of the totals because no fork commit was
  recorded (tasks from before it was) or it no longer exists. A worktree whose
  git fails or times out is left out too; `null` totals mean none could be
  counted.
- `branch`: the primary worktree's current branch (`""` when detached).
- Cached per worktree: a commit, stage or checkout shows on the next poll;
  an unstaged edit changes neither HEAD nor the index, so it shows once the
  45 s cache entry expires (within about a minute at a 30 s poll). A request
  waits at most ~5 s; a worktree still being counted reports its previous
  numbers, or marks the task `pending: true` (totals partial or `null`) until
  a later poll. Meant to be polled every ~30 s.

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

## Agent gateway (MCP)

Loom's tools over MCP for agents and bots. Design, tool list, and client
setup: [AGENT-GATEWAY.md](AGENT-GATEWAY.md).

| Method | URL | Purpose |
|--------|-----|---------|
| `POST` | `/mcp` | MCP over Streamable HTTP (JSON-RPC: `initialize`, `tools/list`, `tools/call`, ...); `GET`/`DELETE` answer 405 |
| `GET` | `/api/agent/manifest` | Machine-readable connection info, URLs as the caller sees them |

Both accept the scoped **agent token** (`~/.loom/agent/agent-token`) as well
as the web token; nothing else accepts the agent token. Browser-originated
POSTs to `/mcp` must be same-origin.

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

Factory (AR) tasks live in their own always-registered project, `~/loom`
(the visible research folder, not the hidden `~/.loom` state above), rather
than inside a code repo. Installs that already have `~/ar` keep it, as do
machines whose `~/loom` is a checkout of Loom's own source; `LOOM_AR_ROOT`
moves it anywhere.
