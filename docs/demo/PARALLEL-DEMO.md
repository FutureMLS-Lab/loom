# Parallel tasks demo: recording script

A 30–45 second screen recording (or GIF) of the web console. It shows one
idea: three agents work on one repository at the same time, your checkout is
not touched, merges go in one at a time, and a merge that conflicts is
aborted cleanly.

The agents need a few minutes to finish, so the clip is recorded in two takes
and cut together. Timings below are for the finished cut.

## Setup

1. **Build the demo repository.** From a clone of Loom:

   ```bash
   scripts/make-parallel-demo.sh              # creates ~/loom-parallel-demo
   ```

   It prints the four titles and goals; they are also listed at the end of
   this page. `--force` rebuilds a demo the script made earlier.

2. **Use a Loom that holds only the demo.** The sidebar lists every
   registered project, so do not record your everyday Loom. Use a separate
   user account or a fresh machine where `claude`, `codex` and Cursor's
   `agent` are logged in (`loom doctor` checks), then:

   ```bash
   loom web --projects --project ~
   ```

   Add `~/loom-parallel-demo` as a project (existing folder).

3. **Pre-flight.**
   - Loom starts Claude Code with `--dangerously-skip-permissions` and
     Cursor's agent with `-f`, so they do not stop to ask. Codex follows
     `~/.codex/config.toml`; if it asks before running the tests or
     committing, approve it off camera.
   - git needs a `user.name` and `user.email` for the agents' commits and for
     Merge ↩. The script says so if they are missing.
   - Notifications off (system Do Not Disturb). Close other windows.

4. **Screen.**
   - Browser window exactly 1440×900 at 100% zoom. An app window hides the
     address bar, tabs and bookmarks, for example
     `chromium --app=http://127.0.0.1:8765 --window-size=1440,900`.
   - Keep tokens and URLs off screen: sign in to Loom before recording (the
     sign-in prompt shows the address), never show a shell that holds the
     token, and check the server name at the top of the sidebar.
   - Agent CLIs print the signed-in account in their start banner. Stay on
     the Chat tab, or scroll the Terminal tab past the banner before a take.
   - A separate terminal window for your own checkout, about 90×12, 16 px
     font, a neutral prompt: `PS1='$ '; cd ~/loom-parallel-demo; clear`.
   - Record at 30 fps.

## Takes

- **Take A** (beats 1–4): create the three tasks live and show them working.
- **Between takes, off camera:** create the fourth task, Count numbers (any
  agent), start it and send its goal. Wait until all four agents have
  committed: each Changes tab lists only committed files and Merge ↩ is
  enabled.
- **Take B** (beats 5–7): one clean merge, one aborted merge, the clean
  checkout.

## Beats

| # | Cut time | Take | On screen | Action | Caption |
|---|---|---|---|---|---|
| 1 | 0:00–0:03 | A | Workspace overview; `loom-parallel-demo` in the sidebar, no tasks yet. | Hold. | One repository. Three agents. |
| 2 | 0:03–0:13 | A | Create Task dialog, three times. | Keep apostrophes: type the title, pick Claude, paste the goal. Hold 1.5 s on the line "branch loom/keep-apostrophes from loom-parallel-demo@main (…), checked out at .RUD/keep-apostrophes/work/loom-parallel-demo — your checkout is not touched", then create. Share column (Codex) and Read stdin (Cursor): show only the agent pick and the create click, at 2×. | Each task gets its own branch. |
| 3 | 0:13–0:19 | A | Sidebar and dock. | Start agent and send the goal in Chat on each task (cut down to about 2 s). Then hold on the three rows working, each with its `loom/` branch and +/− counts rising, and the dock open with three active tasks. | Three agents, three worktrees. |
| 4 | 0:19–0:23 | A | Your terminal window. | Run `git status` (nothing to commit, working tree clean), then `git branch --list 'loom/*'` (three branches, each marked `+`). | Your checkout: untouched. |
| 5 | 0:23–0:30 | B | Keep apostrophes, Changes tab. | Scroll the diff: the `WORD_RE` line in `tally/words.py` and the new test. Press Merge ↩; hold on the result. | Review the diff. Merge ↩. |
| 6 | 0:30–0:38 | B | Count numbers, Changes tab, then your terminal. | The diff changes the same `WORD_RE` line. Press Merge ↩: Loom reports that the merge hit conflicts and was aborted, and lists `tally/words.py` (usually `tests/test_words.py` too). Hold 2 s. Cut to the terminal: `git status` is still clean. | Same line twice: Loom stops, lists the file. |
| 7 | 0:38–0:42 | B | Closing card, or hold on the console. | None. | Closing caption (below). |

**Closing caption:** Every task in its own worktree. You merge one at a time.

## Goals to paste

The same text the script prints, for the default `~/loom-parallel-demo`.

**Keep apostrophes** (Claude)

```text
Work in ./loom-parallel-demo (your git worktree for this task). Make tally count a word with an apostrophe as one word, so "it's" and "doesn't" stop splitting into "it" + "s" and "doesn" + "t". Change WORD_RE in tally/words.py and add a test to tests/test_words.py; touch no other file. Run python3 -m unittest until it passes, then commit both files on the current branch.
```

**Share column** (Codex)

```text
Work in ./loom-parallel-demo (your git worktree for this task). Add a third column, share, to the table in tally/report.py: each word's share of all counted words as a percentage with one decimal, for example 12.5%. Change only tally/report.py and tests/test_report.py. Run python3 -m unittest until it passes, then commit both files on the current branch.
```

**Read stdin** (Cursor)

```text
Work in ./loom-parallel-demo (your git worktree for this task). Let tally read standard input when no file is given or the file is -, so that cat sample.txt | python3 -m tally works. Change only tally/cli.py and add tests/test_cli.py. Run python3 -m unittest until it passes, then commit both files on the current branch.
```

**Count numbers** (any agent; created between takes)

```text
Work in ./loom-parallel-demo (your git worktree for this task). Make tally count numbers as words, so the "4" and "2" in sample.txt are counted. Change WORD_RE in tally/words.py and add a test to tests/test_words.py; touch no other file. Run python3 -m unittest until it passes, then commit both files on the current branch.
```

The demo skips Deep Interview to keep the clip short: after Start agent, the
goal goes straight into Chat. Deep Interview and Run /goal work on these goals
too; they take longer.

## Export

- Cut, and speed up typing and waiting to at most 2×, so text stays readable.
- Keep the MP4 for places that play video. For a GIF, 15 fps at 1200 px wide:

  ```bash
  ffmpeg -i parallel-demo.mp4 -vf "fps=15,scale=1200:-1:flags=lanczos,split[a][b];[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4" parallel-demo.gif
  ```

- The end of beat 3 (three tasks working, dock open) makes a good still for
  `docs/images/parallel-tasks.png`, the README image. Export it 1800 px wide
  so it stays sharp at the README's 900.

## Reset for another attempt

Stop the agents and delete the four tasks in Loom, then rebuild:

```bash
scripts/make-parallel-demo.sh --force
```

The project stays registered, since the path does not change.
