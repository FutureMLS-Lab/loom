"""The route split relocated handler methods into module functions.

A call site left as ``self._helper(...)`` while ``_helper`` became a module
function crashes only when that route is hit in production (the Download PDF
button died this way). This test catches the whole class statically.
"""

import re
from pathlib import Path

import pytest

ROUTE_FILES = sorted(
    (Path(__file__).resolve().parents[1] / "loom").glob("routes_*.py")
)


@pytest.mark.parametrize("path", ROUTE_FILES, ids=lambda p: p.name)
def test_no_self_calls_to_module_functions(path):
    text = path.read_text(encoding="utf-8")
    module_fns = set(re.findall(r"^def (_\w+)\(self", text, re.M))
    stale = [
        f"{path.name}:{text[: m.start()].count(chr(10)) + 1}: self.{m.group(1)}("
        for m in re.finditer(r"self\.(_\w+)\(", text)
        if m.group(1) in module_fns
    ]
    assert not stale, (
        "these call module functions as if they were still handler methods: "
        + ", ".join(stale)
    )


def test_web_does_not_self_call_relocated_route_helpers():
    # The other half of the same bug class: web.py still calling a helper
    # that moved into a routes_* module. The approvals inbox wrapped one in
    # `except Exception: pass`, so every Paper Factory gate silently vanished
    # from "Waiting on you" instead of crashing loudly.
    loom_dir = Path(__file__).resolve().parents[1] / "loom"
    web = (loom_dir / "web.py").read_text(encoding="utf-8")
    defined = set(re.findall(r"^\s*def (_\w+)\(", web, re.M))
    relocated: dict[str, str] = {}
    for path in ROUTE_FILES:
        for name in re.findall(r"^def (_\w+)\(self", path.read_text(encoding="utf-8"), re.M):
            relocated[name] = path.name
    stale = [
        f"web.py:{web[: m.start()].count(chr(10)) + 1}: self.{m.group(1)}( "
        f"-> {relocated[m.group(1)]}"
        for m in re.finditer(r"self\.(_\w+)\(", web)
        if m.group(1) in relocated and m.group(1) not in defined
    ]
    assert not stale, "web.py calls relocated route helpers as methods: " + ", ".join(stale)
