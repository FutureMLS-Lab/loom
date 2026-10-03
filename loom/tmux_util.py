"""Tmux helpers for the web UI (list sessions, capture panes, send input)."""

from __future__ import annotations

import re
import os
import signal
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

# Session:window.pane — conservative allowlist (no shell metacharacters)
_TARGET_RE = re.compile(r"^[A-Za-z0-9_.@-]+:\d+\.\d+$")
_KEYS = {
    "Enter",
    "Up",
    "Down",
    "Left",
    "Right",
    "Escape",
    "Tab",
    "BTab",
    "Backspace",
    "BSpace",
    "Space",
    "Home",
    "End",
    "DC",
    "IC",
    "PageUp",
    "PageDown",
}
# Also accept Ctrl-/Alt-<letter> combos and function keys. These are validated
# against a strict pattern and passed as a single argv (never via a shell), so
# they can't smuggle options or shell metacharacters.
_KEY_RE = re.compile(r"^(?:[CM]-[A-Za-z]|F[0-9]{1,2})$")


def tmux_subprocess_env() -> dict[str, str]:
    """Run tmux commands against the current user's default socket.

    ``loom web`` is often launched from inside tmux or through ``su``.
    Inheriting ``TMUX`` can point tmux clients at another user's socket
    (for example /tmp/tmux-0), which fails with Permission denied.
    """
    env = os.environ.copy()
    env.pop("TMUX", None)
    env.pop("TMUX_PANE", None)
    return env


def tmux_available() -> bool:
    import shutil

    return shutil.which("tmux") is not None


def list_tmux_sessions() -> list[dict[str, str]]:
    """Return ``[{name, attached}, ...]`` (best-effort; empty if tmux missing)."""
    import shutil

    if not shutil.which("tmux"):
        return []
    try:
        r = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}\t#{session_attached}"],
            capture_output=True,
            text=True,
            env=tmux_subprocess_env(),
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if r.returncode != 0:
        return []
    out: list[dict[str, str]] = []
    for line in (r.stdout or "").splitlines():
        parts = line.split("\t", 1)
        if not parts:
            continue
        name = parts[0].strip()
        if not name:
            continue
        attached = parts[1].strip() if len(parts) > 1 else ""
        out.append({"name": name, "attached": attached})
    return out


def list_tmux_panes(session: str) -> list[dict[str, str]]:
    """List panes in a session: ``[{id, title}, ...]`` where id is ``session:win.pane``."""
    import shutil

    if not shutil.which("tmux"):
        return []
    if not re.match(r"^[A-Za-z0-9_.@-]+$", session):
        return []
    try:
        r = subprocess.run(
            [
                "tmux",
                "list-panes",
                "-t",
                session,
                "-F",
                "#{session_name}:#{window_index}.#{pane_index}\t#{pane_title}",
            ],
            capture_output=True,
            text=True,
            env=tmux_subprocess_env(),
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if r.returncode != 0:
        return []
    rows: list[dict[str, str]] = []
    for line in (r.stdout or "").splitlines():
        parts = line.split("\t", 1)
        pid = parts[0].strip() if parts else ""
        title = parts[1].strip() if len(parts) > 1 else ""
        if pid:
            rows.append({"id": pid, "title": title})
    return rows


def validate_tmux_target(t: str) -> bool:
    s = t.strip()
    if not s:
        return True
    return bool(_TARGET_RE.match(s))


def capture_pane(target: str, lines: int = 80) -> tuple[bool, str]:
    """``tmux capture-pane`` without changing the live browser pane size."""
    import shutil

    if not shutil.which("tmux"):
        return False, "tmux not on PATH"
    t = target.strip()
    if not _TARGET_RE.match(t):
        return False, "invalid pane target (expected session:window.pane)"
    n = max(1, min(lines, 500))
    try:
        r = subprocess.run(
            ["tmux", "capture-pane", "-t", t, "-p", "-S", f"-{n}"],
            capture_output=True,
            text=True,
            env=tmux_subprocess_env(),
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "capture failed").strip()
    return True, r.stdout or ""


def send_pane_key(target: str, key: str) -> tuple[bool, str]:
    """Send a single safe tmux key to ``session:window.pane``."""
    import shutil

    if not shutil.which("tmux"):
        return False, "tmux not on PATH"
    t = target.strip()
    k = key.strip()
    if not _TARGET_RE.match(t):
        return False, "invalid pane target (expected session:window.pane)"
    if k not in _KEYS and not _KEY_RE.match(k):
        return False, f"unsupported key: {k}"
    env = tmux_subprocess_env()
    # Match send_pane_literal: if the user scrolled up (copy-mode), leave it so
    # the key reaches the running program instead of moving the copy cursor.
    _exit_copy_mode_if_active(t, env)
    try:
        r = subprocess.run(
            ["tmux", "send-keys", "-t", t, k],
            capture_output=True,
            text=True,
            env=env,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "send-key failed").strip()
    return True, ""


def _exit_copy_mode_if_active(t: str, env: dict) -> None:
    """Leave tmux copy-mode if the pane is in it, so subsequent input reaches
    the running program. While scrolled up (copy-mode), tmux otherwise consumes
    every keystroke (arrows move the copy cursor, symbols/typing do nothing)."""
    try:
        chk = subprocess.run(
            ["tmux", "display-message", "-p", "-t", t, "#{pane_in_mode}"],
            capture_output=True, text=True, env=env, timeout=5,
        )
        if chk.returncode == 0 and chk.stdout.strip() == "1":
            subprocess.run(
                ["tmux", "send-keys", "-t", t, "-X", "cancel"],
                capture_output=True, text=True, env=env, timeout=5,
            )
    except (OSError, subprocess.TimeoutExpired):
        pass


def send_pane_literal(target: str, text: str) -> tuple[bool, str]:
    """Send literal text to a pane fast via ``tmux send-keys -l`` (no Enter).

    Used by the native-terminal keystroke forwarding so each typed character (or
    a short burst of them) reaches the pane immediately, without the heavier
    load-buffer/paste path that ``send_pane_text`` uses for big blocks.
    """
    import shutil

    if not shutil.which("tmux"):
        return False, "tmux not on PATH"
    t = target.strip()
    if not _TARGET_RE.match(t):
        return False, "invalid pane target (expected session:window.pane)"
    if not isinstance(text, str):
        return False, "text must be a string"
    if text == "":
        return True, ""
    if len(text) > 10000:
        return False, "text too long"
    env = tmux_subprocess_env()
    # If the user scrolled up (copy-mode), leave it so typing reaches the program.
    _exit_copy_mode_if_active(t, env)
    try:
        r = subprocess.run(
            ["tmux", "send-keys", "-t", t, "-l", "--", text],
            capture_output=True,
            text=True,
            env=env,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "send-literal failed").strip()
    return True, ""


def _ensure_tmux_sync_output(env: dict[str, str]) -> None:
    """Enable atomic redraws for xterm clients without duplicating the option."""
    try:
        current = subprocess.run(
            ["tmux", "show-options", "-gv", "terminal-features"],
            capture_output=True,
            text=True,
            env=env,
            timeout=5,
        )
        if current.returncode != 0:
            return
        for entry in (current.stdout or "").splitlines():
            parts = entry.strip().split(":")
            if parts and parts[0].startswith("xterm") and "sync" in parts[1:]:
                return
        subprocess.run(
            ["tmux", "set-option", "-as", "terminal-features", ",xterm*:sync"],
            capture_output=True,
            text=True,
            env=env,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return


def open_pane_attach(target: str, cols: int = 80, rows: int = 24):
    """Open a PTY running ``tmux attach-session`` to *target*, sized cols x rows.

    Returns ``(proc, master_fd)`` on success or ``(None, None)`` on failure. The
    caller reads ``master_fd`` (the live terminal byte stream for xterm.js),
    writes browser terminal input back to that same fd, then must
    ``proc.terminate()`` and ``os.close(master_fd)`` when done.
    """
    import pty
    import struct
    import fcntl
    import termios
    import shutil

    if not shutil.which("tmux"):
        return None, None
    t = target.strip()
    if not _TARGET_RE.match(t):
        return None, None
    try:
        cols = max(20, min(500, int(cols)))
        rows = max(5, min(300, int(rows)))
    except (TypeError, ValueError):
        cols, rows = 80, 24
    env = tmux_subprocess_env()
    env["TERM"] = "xterm-256color"
    # Mark the attach as ours: if the web server dies without cleaning up,
    # these processes reparent to init and would hold the session (and its
    # window size) forever. The marker lets the next startup reap them.
    env["LOOM_TMUX_ATTACH"] = "1"
    _ensure_tmux_sync_output(env)
    # If the session doesn't exist, don't spawn a doomed `tmux attach` - it would
    # just print "can't find session: ..." into the stream. Signal "not alive" so
    # the caller can show a friendly message instead.
    try:
        chk = subprocess.run(
            ["tmux", "has-session", "-t", t.split(":", 1)[0]],
            capture_output=True, text=True, env=env, timeout=5,
        )
        if chk.returncode != 0:
            return None, None
    except (OSError, subprocess.TimeoutExpired):
        return None, None
    # Follow the most recently active client. "smallest" looked stable until
    # phones arrived: one loom-app viewer pinned every desktop to 49x22 for as
    # long as it stayed connected. With "latest" the client you are actually
    # typing in wins the size, an idle phone merely pans, and the window
    # springs back the moment the desktop acts.
    try:
        subprocess.run(
            ["tmux", "set-option", "-t", t.split(":")[0], "window-size", "latest"],
            capture_output=True, text=True, env=env, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        master, slave = pty.openpty()
    except OSError:
        return None, None
    try:
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except OSError:
        pass
    try:
        proc = subprocess.Popen(
            ["tmux", "attach-session", "-t", t],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            start_new_session=True,
            env=env,
        )
    except OSError:
        os.close(master)
        os.close(slave)
        return None, None
    os.close(slave)
    return proc, master


def scroll_pane(target: str, direction: str = "up", lines: int = 3) -> tuple[bool, str]:
    """Scroll a pane so the web terminal's wheel/touch-drag browses history.

    Two cases, decided from the pane's live state:

    * Normal screen (a shell, or Claude's classic inline renderer): browse tmux
      scrollback via ``copy-mode -e`` (what ``Ctrl-b [`` does). It's idempotent
      and auto-exits when you scroll back to the bottom, so live output resumes.
    * Alternate screen / full-screen app (Claude Code's fullscreen TUI since
      v2.1.172, plus vim/less/htop): the content lives in the alternate-screen
      buffer, which has *no* tmux scrollback — copy-mode would show nothing. So
      forward ``PgUp``/``PgDn`` to the app and let it scroll its own viewport.
      (Claude's fullscreen scrolls with PgUp/PgDn; the mouse wheel there is
      unreliable — some versions remap it to arrow keys / prompt history.)
    """
    import shutil

    if not shutil.which("tmux"):
        return False, "tmux not on PATH"
    t = target.strip()
    if not _TARGET_RE.match(t):
        return False, "invalid pane target (expected session:window.pane)"
    try:
        n = max(1, min(500, int(lines)))
    except (TypeError, ValueError):
        n = 3
    env = tmux_subprocess_env()

    if direction == "bottom":
        # "Take me back to live" - typing while scrolled must reach the
        # program, so leave copy-mode outright instead of scrolling there.
        _exit_copy_mode_if_active(t, env)
        return True, ""

    # Probe the pane: already browsing copy-mode? running a full-screen app?
    # has the app turned on mouse reporting (so it wants wheel events itself)?
    in_mode = False
    alt_screen = False
    mouse_on = False
    try:
        chk = subprocess.run(
            ["tmux", "display-message", "-p", "-t", t,
             "#{pane_in_mode},#{alternate_on},#{mouse_any_flag}"],
            capture_output=True, text=True, env=env, timeout=5,
        )
        parts = (chk.stdout or "").strip().split(",")
        if len(parts) == 3:
            in_mode = parts[0] == "1"
            alt_screen = parts[1] == "1"
            mouse_on = parts[2] == "1"
    except (OSError, subprocess.TimeoutExpired):
        pass

    # App captures the mouse (Claude's fullscreen TUI, vim/less with mouse, …)
    # and we're not browsing tmux copy-mode: forward REAL wheel events so the app
    # scrolls a few lines at a time, exactly like a hardware wheel. This is what
    # a normal terminal does (tmux's own WheelUpPane forwards when mouse is on),
    # and it keeps a small touchpad nudge to a small scroll instead of the
    # half-screen jumps that PgUp/PgDn caused. SGR mouse encoding (button 64 =
    # wheel up, 65 = wheel down) at a fixed in-pane coordinate.
    if mouse_on and not in_mode:
        button = 65 if direction == "down" else 64
        # The wheel's line count is already coalesced client-side; turn it into a
        # gentle, capped number of notches so a fast flick can't fling the view.
        notches = max(1, min(5, round(n / 8)))
        seq = f"\x1b[<{button};1;1M" * notches
        cmd = ["tmux", "send-keys", "-t", t, "-l", "--", seq]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=5)
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, str(e)
        return (r.returncode == 0), (r.stderr or "").strip()

    # Full-screen app WITHOUT mouse reporting: no wheel to forward, so fall back
    # to PgUp/PgDn (half a screen each). Collapse the line count into a small
    # number of presses instead of one per line.
    if alt_screen and not in_mode:
        key = "PageDown" if direction == "down" else "PageUp"
        presses = max(1, min(10, round(n / 8)))
        cmd = ["tmux", "send-keys", "-t", t] + [key] * presses
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=5)
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, str(e)
        return (r.returncode == 0), (r.stderr or "").strip()

    if direction == "down":
        # scroll-down (send-keys -X) only works while the pane is in copy-mode.
        # If it isn't, we're already at the live bottom, so treat it as a
        # successful no-op instead of erroring out with a 400.
        if not in_mode:
            return True, ""
        cmd = ["tmux", "send-keys", "-t", t, "-X", "-N", str(n), "scroll-down"]
    else:
        cmd = [
            "tmux", "copy-mode", "-e", "-t", t, ";",
            "send-keys", "-t", t, "-X", "-N", str(n), "scroll-up",
        ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=5)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    return (r.returncode == 0), (r.stderr or "").strip()


def send_pane_text(target: str, text: str, submit: bool = False) -> tuple[bool, str]:
    """Paste text into a tmux pane; optionally submit after paste mode settles."""
    import shutil

    if not shutil.which("tmux"):
        return False, "tmux not on PATH"
    t = target.strip()
    if not _TARGET_RE.match(t):
        return False, "invalid pane target (expected session:window.pane)"
    if not isinstance(text, str):
        return False, "text must be a string"
    # Leave copy-mode first so the paste lands in the program, not copy-mode.
    _exit_copy_mode_if_active(t, tmux_subprocess_env())
    buffer_name = f"loom-web-{uuid.uuid4().hex}"
    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as f:
            f.write(text)
            tmp_path = f.name
        load = subprocess.run(
            ["tmux", "load-buffer", "-b", buffer_name, tmp_path],
            capture_output=True,
            text=True,
            env=tmux_subprocess_env(),
            timeout=5,
        )
        if load.returncode != 0:
            return False, (load.stderr or load.stdout or "load-buffer failed").strip()
        paste = subprocess.run(
            ["tmux", "paste-buffer", "-b", buffer_name, "-p", "-d", "-t", t],
            capture_output=True,
            text=True,
            env=tmux_subprocess_env(),
            timeout=5,
        )
        if paste.returncode != 0:
            return False, (paste.stderr or paste.stdout or "paste-buffer failed").strip()
        if submit:
            # Cursor Agent processes bracketed-paste input asynchronously. If
            # Enter follows the paste-end bytes in the same PTY read, Cursor can
            # render the text but leave it sitting in the composer. Separate the
            # submit key into a later read so it is reliably treated as Send.
            time.sleep(0.1)
            return send_pane_key(t, "Enter")
        return True, ""
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    finally:
        if tmp_path:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except OSError:
                pass


def reap_orphaned_attaches() -> int:
    """Kill web-terminal ``tmux attach`` processes orphaned by a dead server.

    Every attach the web terminal spawns carries ``LOOM_TMUX_ATTACH=1`` in its
    environment. While the server lives they are its children and are killed
    when their stream closes; if the server is killed hard they reparent to
    init and sit on the session forever - keeping it "attached" and, under
    ``window-size smallest``, pinning every viewer to the dead client's size.
    Called once at startup, before the new server has spawned any attaches of
    its own, so anything marked AND reparented (ppid 1) is safely ours to kill.
    """
    reaped = 0
    proc_root = Path("/proc")
    if not proc_root.is_dir():  # non-Linux: nothing we can safely do
        return 0
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().split(b"\0")
            if not (cmdline and cmdline[0] == b"tmux" and b"attach-session" in cmdline):
                continue
            if b"LOOM_TMUX_ATTACH=1" not in (entry / "environ").read_bytes():
                continue
            stat_fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            ppid = int(stat_fields[1])  # field 4 of /proc/pid/stat
            if ppid != 1:
                continue  # still owned by a living server
            os.kill(int(entry.name), signal.SIGTERM)
            reaped += 1
        except (OSError, ValueError, IndexError):
            continue
    return reaped
