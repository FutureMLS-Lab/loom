# Agent usage budgets

Loom treats conversation history as a finite resource. Agent CLIs can issue
many model requests during one visible turn, and every tool call may carry the
same accumulated history again. Cached input is cheaper than uncached input,
but it is not free.

## Why these defaults exist

The 2026-09-24 team usage export contained 190 calls, 921.3 million tokens and
$1,444.57 of cost. `gpt-5.6-sol-max-fast` accounted for 101 calls, 671.8
million tokens and $1,034.49. About 96% of that model's tokens were cache
reads. The expensive pattern was therefore not simply “too many clicks”; it
was repeated tool turns inside very large conversations.

## Guardrails

- Cursor tasks keep the speed-first `gpt-5.6-sol-max-fast` default, available
  fast siblings are still preferred, and Claude panes keep `--effort max`.
  Loom's context controls do not silently trade away iteration quality.
- A session receives a 4 MiB soft warning and an 8 MiB hard transcript-history
  budget. Cursor uses its canonical agent JSONL transcript; `store.db` is not
  counted because it also contains file snapshots and tool artifacts and can
  overstate the conversation by orders of magnitude. The web UI marks
  **CONTEXT GROWING** at the warning and offers **New session**. After the hard
  limit, sending from Loom's compose box or workflow buttons uses a compact
  fresh-session handoff.
- A second guard watches the live terminal status while an agent is working.
  It pauses a turn at 1.5 million displayed tokens or 50% context occupancy,
  whichever comes first. This closes the expensive gap where one click can
  issue dozens of tool/model calls before the user gets another chance to
  press Send. Worktree changes are preserved, and the next message must rotate
  to a fresh `PLAN.md`-backed session.
- Resuming an over-budget historical session requires a second explicit
  confirmation. Normal handoff state belongs in `PLAN.md` and the worktree,
  not in an indefinitely resumed chat.
- Fresh handoffs pass paths to the default rules, `PLAN.md`, project memory,
  selected skills and worktree instead of pasting the whole original prompt.
  AR author prompts put their invariant methodology in an identical leading
  block, so the provider can reuse a stable prefix while round-specific review
  text stays at the end.
- Automated Research defaults to four rounds, permits at most twelve, starts
  each author/repair round in a fresh context window, and stops after two
  fruitless continuation nudges.
- Automated reviewer panels keep the existing strong three-model panel. If
  the compiled PDF is byte-for-byte unchanged, Loom skips the next panel
  rather than paying three reviewers to read it again.
- A failed reviewer panel is not blindly rerun. This avoids repeating the
  successful reviewers just because one model timed out.

## Configuration

Set `LOOM_SESSION_CONTEXT_BUDGET_MB` before starting Loom to tune the
per-session hard limit. Set `LOOM_SESSION_CONTEXT_WARNING_MB` to tune the soft
warning. `0` disables the corresponding threshold; positive values are
clamped to 0.25–64 MiB, and the warning never exceeds the hard limit.

Set `LOOM_CLAUDE_EFFORT` to `low`, `medium`, `high`, or `max` to override the
Claude pane reasoning effort. The default is `max`.

Set `LOOM_TURN_TOKEN_BUDGET` to the maximum token count displayed for one
active turn (default `1500000`) and `LOOM_CONTEXT_PERCENT_BUDGET` to the live
context occupancy ceiling (default `50`). `0` disables either guard. The
terminal reports rounded values and Loom polls every four seconds, so these are
safety cutoffs rather than exact billing limits.

The budget guard applies to messages sent through Loom's compose box and
workflow buttons. Raw keystrokes sent directly into the embedded terminal are
intentionally not intercepted; use the visible context indicator and **New
session** when operating the CLI directly.

## What “reuse” means here

The usage export already shows roughly 96% cache reads for the expensive
model. Raising that percentage alone cannot remove the bill: cached input is
still charged, and a multi-million-token history is still expensive when it
is replayed across many tool turns. The policy therefore optimizes for
**bounded reuse**: preserve a stable, cacheable instruction prefix; keep
changing state in repository files; and rotate to a compact handoff before
the accumulated conversation dominates every subsequent request.
