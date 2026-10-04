#!/usr/bin/env bash
# Build a small git repository for trying Loom's parallel tasks.
#
#   scripts/make-parallel-demo.sh [--force] [DIR]
#
# DIR (default ~/loom-parallel-demo) gets "tally", a tiny Python word counter
# with tests and three commits. The script then prints four tasks to paste
# into Loom: three change different files and merge cleanly; the fourth edits
# the same line as the first, so its Merge stops on a conflict.
set -euo pipefail

usage() {
  cat <<'EOF'
usage: make-parallel-demo.sh [--force] [DIR]

Create a demo git repository in DIR (default: ~/loom-parallel-demo) and print
the Loom tasks to run on it.

  --force     replace DIR if an earlier run of this script made it
  -h, --help  show this help
EOF
}

force=0
target=""
while [ $# -gt 0 ]; do
  case "$1" in
    --force) force=1 ;;
    -h | --help) usage; exit 0 ;;
    --) shift; break ;;
    -*) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    *)
      if [ -n "$target" ]; then echo "give one DIR at most" >&2; exit 2; fi
      target="$1"
      ;;
  esac
  shift
done
if [ $# -gt 0 ]; then
  if [ -n "$target" ] || [ $# -gt 1 ]; then echo "give one DIR at most" >&2; exit 2; fi
  target="$1"
fi
target="${target:-$HOME/loom-parallel-demo}"

command -v git >/dev/null 2>&1 || { echo "git is required" >&2; exit 1; }

# Written inside .git so it never shows up in git status. --force deletes a
# non-empty DIR only when it carries this marker.
marker=".git/loom-parallel-demo"

if [ -e "$target" ] && [ ! -d "$target" ]; then
  echo "$target exists and is not a directory" >&2
  exit 1
fi
if [ -d "$target" ] && [ -n "$(ls -A "$target")" ]; then
  if [ "$force" -ne 1 ]; then
    echo "$target is not empty. Pick another DIR, or pass --force to replace an earlier demo." >&2
    exit 1
  fi
  if [ ! -f "$target/$marker" ]; then
    echo "$target was not made by this script, so --force will not delete it. Pick another DIR." >&2
    exit 1
  fi
  if [ -d "$target/.RUD" ]; then
    echo "Replacing the earlier demo and its Loom tasks. Stop their agents in Loom if any are still running."
  fi
  rm -rf "$target"
fi

mkdir -p "$target"
target="$(cd "$target" && pwd -P)"
name="$(basename "$target")"

# Every git call carries its own identity and settings: nothing is written to
# your global or repository config, and no hooks or signing get in the way.
g() {
  git -C "$target" \
    -c user.name="Loom Demo" \
    -c user.email="demo@loom.invalid" \
    -c commit.gpgsign=false \
    -c core.hooksPath=/dev/null \
    -c core.autocrlf=false \
    "$@"
}

commit() {
  local message="$1"
  shift
  g add -- "$@"
  g commit -q -m "$message"
}

git init -q "$target"
g symbolic-ref HEAD refs/heads/main
: >"$target/$marker"
mkdir -p "$target/tally" "$target/tests"

# --- commit 1: count words ---------------------------------------------------

cat >"$target/.gitignore" <<'EOF'
__pycache__/
*.pyc
.venv/
# Loom keeps its tasks here: PLAN.md, task.json and each task's worktree.
.RUD/
EOF

cat >"$target/README.md" <<'EOF'
# tally

Count the words in a text file.

    python3 -m tally FILE
EOF

cat >"$target/tally/__init__.py" <<'EOF'
"""tally - count the words in a text file."""

__version__ = "0.1.0"
EOF

cat >"$target/tally/__main__.py" <<'EOF'
from tally.cli import main

raise SystemExit(main())
EOF

cat >"$target/tally/words.py" <<'EOF'
"""Split text into words and count them."""

import re
from collections import Counter

# A word is a run of letters; anything else ends it.
WORD_RE = re.compile(r"[a-z]+")


def split_words(text):
    """Return the words in text, lower-cased, in order."""
    return WORD_RE.findall(text.lower())


def count_words(text):
    """Return a Counter of how often each word in text appears."""
    return Counter(split_words(text))
EOF

cat >"$target/tally/cli.py" <<'EOF'
"""Command line: python3 -m tally FILE [--top N]"""

import argparse
from pathlib import Path

from tally.words import count_words


def main(argv=None):
    parser = argparse.ArgumentParser(prog="tally", description="Count the words in a text file.")
    parser.add_argument("file", help="the text file to read")
    parser.add_argument("--top", type=int, default=10, metavar="N", help="show the N most common words (default 10)")
    args = parser.parse_args(argv)
    text = Path(args.file).read_text(encoding="utf-8")
    for word, n in count_words(text).most_common(args.top):
        print(f"{n:5}  {word}")
    return 0
EOF

: >"$target/tests/__init__.py"

cat >"$target/tests/test_words.py" <<'EOF'
import unittest

from tally.words import count_words, split_words


class WordsTest(unittest.TestCase):
    def test_lower_cases_and_drops_punctuation(self):
        self.assertEqual(split_words("The loom, the LOOM."), ["the", "loom", "the", "loom"])

    def test_counts_repeated_words(self):
        self.assertEqual(count_words("warp weft warp")["warp"], 2)


if __name__ == "__main__":
    unittest.main()
EOF

commit "Count the words in a text file" \
  .gitignore README.md tally/__init__.py tally/__main__.py tally/words.py tally/cli.py \
  tests/__init__.py tests/test_words.py

# --- commit 2: print a table -------------------------------------------------

cat >"$target/tally/report.py" <<'EOF'
"""Format word counts as a plain-text table."""


def render(counts, top=10):
    """Return the top most common words in counts as an aligned table."""
    rows = counts.most_common(top)
    if not rows:
        return "(no words)"
    width = max(len("word"), *(len(word) for word, _ in rows))
    lines = [f"{'word':<{width}}  count"]
    for word, n in rows:
        lines.append(f"{word:<{width}}  {n:>5}")
    return "\n".join(lines)
EOF

cat >"$target/tally/cli.py" <<'EOF'
"""Command line: python3 -m tally FILE [--top N]"""

import argparse
from pathlib import Path

from tally.report import render
from tally.words import count_words


def main(argv=None):
    parser = argparse.ArgumentParser(prog="tally", description="Count the words in a text file.")
    parser.add_argument("file", help="the text file to read")
    parser.add_argument("--top", type=int, default=10, metavar="N", help="show the N most common words (default 10)")
    args = parser.parse_args(argv)
    text = Path(args.file).read_text(encoding="utf-8")
    print(render(count_words(text), top=args.top))
    return 0
EOF

cat >"$target/tests/test_report.py" <<'EOF'
import unittest
from collections import Counter

from tally.report import render


class RenderTest(unittest.TestCase):
    def test_most_common_first(self):
        lines = render(Counter({"weft": 1, "warp": 3})).splitlines()
        self.assertEqual(lines[0].split(), ["word", "count"])
        self.assertEqual(lines[1].split(), ["warp", "3"])
        self.assertEqual(lines[2].split(), ["weft", "1"])

    def test_top_limits_rows(self):
        table = render(Counter({"warp": 3, "weft": 2, "heddle": 1}), top=2)
        self.assertEqual(len(table.splitlines()), 3)

    def test_no_words(self):
        self.assertEqual(render(Counter()), "(no words)")


if __name__ == "__main__":
    unittest.main()
EOF

commit "Print the counts as an aligned table" tally/report.py tally/cli.py tests/test_report.py

# --- commit 3: sample text and usage -----------------------------------------

cat >"$target/sample.txt" <<'EOF'
The weaver's loom has 4 shafts and 2 treadles.
It's an old loom, but it's quick, and the weaver doesn't stop.
Warp threads run along the loom; weft threads run across it.
Each pass of the shuttle adds one more row to the cloth.
EOF

cat >"$target/README.md" <<'EOF'
# tally

Count the words in a text file.

    python3 -m tally sample.txt
    python3 -m tally sample.txt --top 5

Run the tests:

    python3 -m unittest

A demo project for running several Loom tasks on one repository, made by
Loom's `scripts/make-parallel-demo.sh`.
EOF

commit "Add a sample text and usage notes" sample.txt README.md

# --- check it works ----------------------------------------------------------

tests_note="tests not run (no python3)"
if command -v python3 >/dev/null 2>&1; then
  if ! out="$(cd "$target" && PYTHONDONTWRITEBYTECODE=1 python3 -m unittest 2>&1)"; then
    printf '%s\n' "$out" >&2
    echo "The demo's own tests failed; see above." >&2
    exit 1
  fi
  tests_note="tests pass"
fi

head="$(g rev-parse --short HEAD)"

identity_note=""
if [ -z "$(git -C "$target" config user.email || true)" ]; then
  identity_note="
Note: git has no user.email here. The agents' commits and Merge need one;
set it for this repository only with
  git -C $target config user.email you@example.com
  git -C $target config user.name \"Your Name\"
"
fi

cat <<EOF

Demo repository ready: $target
  main @ $head, 3 commits, $tests_note
$identity_note
Next, in Loom:

1. Start Loom if it is not running:

     loom web --projects --project ~

2. Add $target as a project (existing folder).

3. Create these four tasks on it. For each: Create task, enter the title,
   pick the agent, paste the goal, create. Then Start agent and send the
   same goal in Chat (or run Deep Interview and Run /goal as usual).

     Title              Agent    Changes
     Keep apostrophes   Claude   tally/words.py
     Share column       Codex    tally/report.py
     Read stdin         Cursor   tally/cli.py
     Count numbers      any      tally/words.py, the same line as Keep apostrophes

   Keep apostrophes:
   Work in ./$name (your git worktree for this task). Make tally count a word with an apostrophe as one word, so "it's" and "doesn't" stop splitting into "it" + "s" and "doesn" + "t". Change WORD_RE in tally/words.py and add a test to tests/test_words.py; touch no other file. Run python3 -m unittest until it passes, then commit both files on the current branch.

   Share column:
   Work in ./$name (your git worktree for this task). Add a third column, share, to the table in tally/report.py: each word's share of all counted words as a percentage with one decimal, for example 12.5%. Change only tally/report.py and tests/test_report.py. Run python3 -m unittest until it passes, then commit both files on the current branch.

   Read stdin:
   Work in ./$name (your git worktree for this task). Let tally read standard input when no file is given or the file is -, so that cat sample.txt | python3 -m tally works. Change only tally/cli.py and add tests/test_cli.py. Run python3 -m unittest until it passes, then commit both files on the current branch.

   Count numbers:
   Work in ./$name (your git worktree for this task). Make tally count numbers as words, so the "4" and "2" in sample.txt are counted. Change WORD_RE in tally/words.py and add a test to tests/test_words.py; touch no other file. Run python3 -m unittest until it passes, then commit both files on the current branch.

4. When an agent has committed, open its task's Changes tab and press
   Merge ↩. Keep apostrophes, Share column and Read stdin merge cleanly in
   any order. Merge Count numbers after Keep apostrophes: it edits the same
   line of tally/words.py, so Loom aborts that merge, lists the conflicting
   files and leaves your checkout clean.

Check your own checkout at any point:

  git -C $target status
  git -C $target branch --list 'loom/*'

EOF
