# Running many tasks on one repository

Every Loom task gets its own git worktree, its own branch and its own
terminal. That is what lets several agents work on the same repository at
the same time: a Claude task fixing a bug, a Codex task adding an export, a
Cursor task updating the docs. None of them sees the others' half-finished
edits, and the files in your own checkout stay as they are until you merge.

This page covers what a task is on disk, its life from create to merge, how
merging and conflicts behave, what is and is not isolated, recipes, cleanup
and common questions.

To try it on a throwaway repository, run `scripts/make-parallel-demo.sh` from
a clone of Loom. It creates `~/loom-parallel-demo`, a small Python project,
and prints four tasks to paste: three change different files and merge
cleanly, and one edits the same line as another, so you can watch a conflict
being handled.

## What a task is on disk

A task called "Fix login" in the project `~/app`:

```
~/app/                         your checkout; untouched until you merge
├── src/ …
└── .RUD/                      Loom's folder for this project's tasks
    ├── NOTES.md               project notes
    ├── MEMORY.md              lessons agents append as tasks finish
    ├── task-order.json        sidebar order
    └── fix-login/             the task, named by its slug
        ├── task.json          title, goal, agent, worktrees, fork commits, sessions
        ├── PLAN.md            goal, results table, to-do list, progress log
        └── work/              the agent's terminal starts here
            └── app/           git worktree on branch loom/fix-login
```

- **Worktree.** `work/app/` is a full checkout of the commit your checkout
  was on when you created the task. More exactly, it is the HEAD of the
  project's code root, which is the project folder unless you set the code
  root to a subfolder that holds the repository. Git knows about it:
  `git worktree list` shows it, and `loom/fix-login` is an ordinary branch in
  your repository.
- **Agent config.** A worktree only gets tracked files. Loom symlinks your
  untracked agent config into it (`.claude/skills`, `.claude/commands`,
  `.claude/agents`, `.cursor/rules`, `AGENTS.md`, `CLAUDE.md`), so every task
  uses the project's skills and an edit to one is seen by all. Their
  top-level names (`/.claude`, `/.cursor`, `/AGENTS.md`, `/CLAUDE.md`) go
  into the repository's `info/exclude`, which keeps them out of `git status`,
  in your checkout too; use `git add -f` if you later want to commit one of
  them. Per-checkout state such as `.claude/settings.local.json` is not
  linked.
- **Terminal.** Each task has its own tmux session (`tmux ls` lists them as
  `loom-<agent>-…-<slug>`). The agent pane starts in `work/`, so it can reach
  every worktree the task holds, and it keeps running when you close the
  browser. The deep-interview prompt tells the agent which worktree and
  branch are its own.
- **Branch prefix.** Set `LOOM_BRANCH_PREFIX` before starting Loom to use
  something other than `loom/`.

### `.RUD/` stays out of your `git status`

When the project folder is the repository itself, `.RUD/` sits inside your
checkout. Loom adds `/.RUD/` to the repository's `.git/info/exclude` the first
time it creates a task worktree there (and when it turns a **New empty
folder** into a repository), so `git status` in your checkout stays clean and
`git add -A` never picks up task files. A project that shows `?? .RUD/` from
before this gets the entry with its next task.

`info/exclude` is local to your clone. To keep `.RUD/` out for everyone who
clones the repository, ignore it there too:

```bash
echo '.RUD/' >> .gitignore
```

Merge ↩ does not care either way; it only checks tracked files.

## From create to merge

1. **Create task.** Pick the project and the agent, and write the goal. The
   dialog shows where the task will start, for example:

   > branch loom/fix-login from app@main (3f2a9c1), checked out at
   > .RUD/fix-login/work/app — your checkout is not touched

   It warns you if your checkout has uncommitted changes: they are not
   carried into the task, which starts from the last commit. When the
   project folder holds several repositories, pick the one the task is about.
2. **Start agent.** Loom opens the task's tmux session and starts the agent
   CLI in `.RUD/<slug>/work/`.
3. **Deep Interview.** The agent asks a few questions, one at a time, then
   writes `PLAN.md`: what success looks like, an empty results table with a
   row for each number the task must produce, and a to-do list.
4. **Run /goal.** The agent works through `PLAN.md` in its worktree. This is
   a good moment to create the next task. The sidebar shows each task's
   branch and its +/− line counts, and the dock keeps the active ones in
   view.
5. **Write result.** The agent records what it did in `PLAN.md` and fills in
   the results table.
6. **Review.** The Changes tab shows everything the task has changed since
   it started: the commits on its branch and any uncommitted edits, for each
   of its worktrees.
7. **Commit.** Loom never commits on its own. Ask the agent to commit on its
   branch, or commit yourself in the Terminal tab. Merge ↩ and Push work on
   commits; uncommitted edits stay behind in the worktree.
8. **Merge ↩ or Push.** Merge ↩ brings the branch into your checkout (next
   section). Push runs `git push -u origin loom/<slug>` from the worktree,
   for a pull request or a review elsewhere.
9. **Delete** the task when you no longer need it (see [Cleanup](#cleanup)).

## Merge ↩

Merge ↩ runs one command in the checkout the worktree came from, which is
your checkout:

```bash
git merge --no-ff loom/<slug>
```

- It merges into whatever branch your checkout has checked out at that
  moment. That is usually the branch the task started from, but Loom does
  not switch branches for you.
- It refuses while your checkout has uncommitted changes to tracked files.
  Commit or stash them, then press Merge ↩ again. Untracked files do not
  block it (git itself still stops if the merge would overwrite one).
- It refuses on a detached HEAD. Check out a branch first.
- It merges commits only. If the agent has not committed, there is nothing
  to merge yet.
- On a conflict it runs `git merge --abort`, which leaves your checkout
  exactly as it was, and lists the files that conflict.
- It never pushes. The merge is a local commit you can look at before you
  push, and undo with `git reset --merge ORIG_HEAD` right after if you
  change your mind.

Merge one task at a time. Each merge moves your branch forward, and the next
one lands on top of it. Tasks that changed different files go in cleanly, in
any order.

### Conflicts

Two tasks that edit the same lines both finish without trouble, because
neither can see the other. The clash shows up when you merge the second one:
Loom aborts that merge, your checkout stays clean, and you get the list of
conflicting files. Then choose:

- **Hand it back to the agent.** In the second task's Chat: "Merge main into
  this branch, resolve the conflicts, run the tests and commit." Worktrees
  share your repository's branches, so `main` is right there. Press Merge ↩
  again when it is done.
- **Resolve it yourself.** In your checkout run `git merge loom/<slug>`, fix
  the files, `git add` them and `git commit`.
- **Keep one.** If the tasks were alternatives, keep the one you merged and
  delete the other.

## What is isolated and what is not

Each task has its own files (the worktree), its own branch, its own terminal
and agent session, its own `PLAN.md` and results table, and its own diff.
Everything else belongs to the machine or to your checkout:

| Not isolated | What goes wrong | What to do |
|---|---|---|
| Ports | Two dev servers both want port 3000. | Give each task its own port in its goal ("serve on port 3101"), or stop one server before the next starts. |
| GPUs | One task takes every GPU and the others run out of memory. | Pin GPUs per task in the goal: "use `CUDA_VISIBLE_DEVICES=2,3` only". |
| Memory and CPU | Five full builds at once slow everything down. | Cap parallelism inside each task (`make -j4`, fewer test workers) and run fewer tasks on small machines. |
| Global caches and installs | One task upgrades a global tool, and it changes for every task. | Install into the worktree: a `.venv` per worktree, `npm install` inside it. Keep `pip install --user` and `npm -g` out of goals. |
| Databases and outside services | Two tasks migrate the same database. | One database or schema per task, or a local throwaway one for tests. |
| Untracked and ignored files | `.env`, local data and build output are not in the worktree. | Copy in what the task needs (below). |
| Lines two tasks both edit | The second merge stops on a conflict. | Split work by file; resolve at merge time. |

A worktree is a separate checkout, not a sandbox. Loom starts Claude Code
with `--dangerously-skip-permissions` and Cursor's agent with `-f`, so both
run commands without asking; Codex follows your `~/.codex/config.toml`. Loom
does not stop an agent from working outside its worktree, so name the folder
and files in the goal and read what the agent ran.

To give a task your `.env` or local data:

```bash
cd ~/app
cp .env .RUD/fix-login/work/app/                # a copy, private to the task
ln -s ~/app/data .RUD/fix-login/work/app/data   # a link, shared with your checkout
git -C .RUD/fix-login/work/app status --short   # neither should be listed
```

A copy belongs to the task. A link points at your checkout's data, so every
task that links it reads and writes the same files; use links for data the
tasks only read. If `git status` lists either one, add it to
`.git/info/exclude` so the agent does not commit it.

## Recipes

### Same goal for Claude, Codex and Cursor; keep the best

1. Create three tasks with the same goal and a different agent each, for
   example titled `csv export claude`, `csv export codex` and
   `csv export cursor`. Identical titles work too: Loom makes the slugs
   unique (`csv-export`, `csv-export-2`, `csv-export-3`).
2. Start all three. They branch from the same commit, so their diffs compare
   directly.
3. Compare the Changes tabs, and the results tables in each `PLAN.md` if the
   goal asked for numbers. Run the tests in a worktree when you need a
   tie-break: `cd ~/app/.RUD/csv-export-codex/work/app && pytest`.
4. Merge ↩ the one you want. Delete the other two tasks, and their branches
   if you will not need them:
   `git branch -D loom/csv-export-claude loom/csv-export-cursor`.

### One task, two repositories

When a change has to land in a library and in the code that uses it, keep
it in one task:

1. Keep the repositories side by side in one folder, for example
   `~/shop/api` and `~/shop/web`, and add `~/shop` as the project.
2. Create the task and pick `api` as its repository.
3. In the Changes tab, use **+ Add worktree** and pick `web`; it offers the
   repositories inside the project folder. The task now holds
   `.RUD/<slug>/work/api` and `.RUD/<slug>/work/web`, each on a
   `loom/<slug>` branch in its own repository. The agent starts in `work/`
   and sees both.
4. The Changes tab shows both diffs. **Push all** pushes both branches;
   Merge ↩ works per worktree.

### Split one feature into parallel tasks

Parallel tasks merge cleanly when they touch different files, so split by
file rather than by step:

- Write each goal around the files it owns, and name what it must leave
  alone: "Change only `src/report.py` and `tests/test_report.py`."
- Land shared groundwork first. If three pieces need a new interface, make
  that a short task, merge it, then create the three; they branch from the
  commit that has it.
- Files everyone wants to touch (a route table, a changelog, a list of
  exports) are where conflicts come from. Give those edits to one task, or
  expect to resolve them at merge time.
- A task does not see work merged after it was created. If it needs that
  work, ask its agent to merge `main` into its branch.

The demo from `scripts/make-parallel-demo.sh` is split this way: three tasks
own `words.py`, `report.py` and `cli.py` and merge in any order, and a fourth
edits the same line of `words.py` as the first.

## Cleanup

Delete a task when you are done with it. Loom removes `.RUD/<slug>/`
(`PLAN.md`, `task.json` and the worktree checkout, with any uncommitted
changes in it) and unregisters the worktree from your repository. The branch
`loom/<slug>` and its commits stay. Stop the agent before you delete the
task.

Task branches pile up in your repository:

```bash
git branch --list 'loom/*'                  # all of them; + marks one a task has checked out
git branch --merged main --list 'loom/*'    # the ones already in main
git branch -d loom/fix-login                # delete a merged branch
git branch -D loom/csv-export-claude        # delete an unmerged one, and its commits with it
git push origin --delete loom/fix-login     # if you pushed it
```

Git refuses to delete a branch that a task's worktree still has checked out.

Two more things:

- If a branch named `loom/<slug>` already exists when you create a task with
  that slug (say you deleted "Fix login" and made a new one), the new task
  checks out the old branch, commits and all, instead of starting fresh from
  HEAD. Delete the old branch first if you want a clean start.
- If you delete a `.RUD/<slug>/` folder by hand instead of through Loom, run
  `git worktree prune` in your checkout to drop the stale entry.

## FAQ

**My `.env` is missing in the worktree.**
That is expected: a worktree holds tracked files only, plus the agent config
Loom links in. Copy the file in (`cp ~/app/.env ~/app/.RUD/<slug>/work/app/`)
or ask the agent to, and check `git status` there so it is not committed.

**Two tasks need the same port.**
Ports belong to the machine, not to a task, so the second server fails to
start, or a test talks to the other task's server. Give each task its own
port in its goal, or run them one after the other.

**I edited my checkout while a task was running.**
The task does not see your edits. It started from the commit you were on and
works in its own folder; uncommitted changes were never part of it. When you
merge, uncommitted changes to tracked files make Merge ↩ refuse until you
commit or stash them. Committed ones are merged together with the task's
work, and overlapping lines stop the merge as a conflict. If the task needs
your new commits, ask its agent to merge your branch into its own.

**How do I see all branches Loom made?**
Run `git branch --list 'loom/*'` in your checkout. A `+` marks a branch that
a task still has checked out, and `git worktree list` shows where.
`git log --oneline main..loom/<slug>` shows what one branch adds. If you set
`LOOM_BRANCH_PREFIX`, list that prefix instead.

**What does delete remove?**
The task folder `.RUD/<slug>/`: its `PLAN.md`, `task.json` and worktree
checkout, including anything uncommitted there. The branch and its commits
stay in your repository, so commit first if the work matters.

**A task has no worktree.**
Usually the project's code root is not a git repository. Point the code root
at the repository, or make the folder one (`git init`, then a first commit);
a **New empty folder** is set up that way already. An existing task can then
get its worktree from **+ Add worktree**.

**My editor or test runner finds every file twice.**
Tools that walk your whole checkout can see the task copies under `.RUD/`.
Many skip dot-folders or git-ignored paths; for one that does not, exclude
`.RUD` in its settings.

**Can I work in a task's worktree myself?**
Yes. It is an ordinary checkout at `.RUD/<slug>/work/<repo>`: open it in
your editor, run the tests, commit. The Changes tab shows your edits along
with the agent's.

**How many tasks can run at once?**
Loom sets no limit; the machine, the shared resources above and your agent
plans do. In daily use the maintainers keep dozens of tasks across more than
a dozen projects on one machine.

## How this compares

Several tools give each coding agent its own worktree. What Loom adds: it
runs on your own server and you reach it from a browser, the macOS app or a
phone; one fleet mixes Claude, Codex and Cursor, so the same goal can go to
all three and you keep the best diff; a task can span several repositories;
every task keeps its own `PLAN.md` with a results table; and bots and Slack
can drive it over MCP (see [AGENT-GATEWAY.md](AGENT-GATEWAY.md) and
[OPENCLAW.md](OPENCLAW.md)).
