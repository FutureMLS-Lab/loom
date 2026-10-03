"""Terminal and tmux routes: PTY streams, capture, scroll, input delivery."""

from __future__ import annotations

import os
import socket

from urllib.parse import parse_qs

from loom.tmux_util import (
    capture_pane,
    list_tmux_panes,
    list_tmux_sessions,
    open_pane_attach,
    scroll_pane,
    send_pane_key,
    send_pane_literal,
    send_pane_text,
    tmux_available,
    validate_tmux_target,
)
from loom.web_util import (
    _TERMINAL_STREAM_SELECT_SECONDS,
    _filter_tmux_sessions_for_project,
    _json_bytes,
)


def handle_get(self, path, parsed) -> bool:  # noqa: C901
    if path == "/api/tmux/sessions":
        qs = parse_qs(parsed.query or "")
        proj = (qs.get("project") or [""])[0].strip()
        all_sessions = list_tmux_sessions()
        if proj:
            p_root = self.pr.get_path(proj)
            sessions = _filter_tmux_sessions_for_project(all_sessions, proj, p_root)
        else:
            sessions = all_sessions
        st, b, h = _json_bytes({"tmux": tmux_available(), "sessions": sessions})
        self._send(st, b, h)
        return True


    if path == "/api/tmux/panes":
        qs = parse_qs(parsed.query or "")
        sess = (qs.get("session") or [""])[0].strip()
        if not sess:
            st, b, h = _json_bytes({"error": "session required"}, 400)
            self._send(st, b, h)
            return True
        st, b, h = _json_bytes({"panes": list_tmux_panes(sess)})
        self._send(st, b, h)
        return True


    if path == "/api/tmux/capture":
        qs = parse_qs(parsed.query or "")
        target = (qs.get("target") or [""])[0].strip()
        lines = int((qs.get("lines") or ["80"])[0] or 80)
        if not validate_tmux_target(target):
            st, b, h = _json_bytes({"ok": False, "error": "invalid target", "text": ""}, 400)
            self._send(st, b, h)
            return True
        ok, text = capture_pane(target, lines)
        st, b, h = _json_bytes({"ok": ok, "text": text if ok else "", "error": "" if ok else text})
        self._send(st, b, h)
        return True


    if path == "/api/tmux/stream":
        import select as _select

        qs = parse_qs(parsed.query or "")
        target = (qs.get("target") or [""])[0].strip()
        try:
            cols = int((qs.get("cols") or ["80"])[0] or 80)
            rows = int((qs.get("rows") or ["24"])[0] or 24)
        except ValueError:
            cols, rows = 80, 24
        if not validate_tmux_target(target):
            st, b, h = _json_bytes({"ok": False, "error": "invalid target"}, 400)
            self._send(st, b, h)
            return True
        proc, master = open_pane_attach(target, cols, rows)
        if proc is None or master is None:
            st, b, h = _json_bytes(
                {"ok": False, "error": "could not attach to pane"}, 502
            )
            self._send(st, b, h)
            return True
        stream_id = self.terminal_streams.register(master, proc)
        self.close_connection = True
        # A dropped SSH tunnel or a lid-closed laptop never sends FIN:
        # without keepalive the half-open socket keeps this attach (a
        # real tmux client, pinning the window size) alive for the
        # kernel's full retransmission timeout - tens of minutes.
        # Aggressive keepalive turns that into ~a minute, and a send
        # timeout stops a flooding pane from blocking on a dead peer.
        try:
            self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 30)
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
            self.connection.settimeout(60)
        except OSError:
            pass
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("X-Loom-Terminal-Stream", stream_id)
            self.send_header("Connection", "close")
            self.end_headers()
        except OSError:
            self.terminal_streams.unregister(stream_id, master)
            self._kill_pty(proc, master)
            return True
        conn = self.connection
        try:
            while True:
                if not self.terminal_streams.lease_active(stream_id):
                    break
                if proc.poll() is not None:
                    try:
                        while True:
                            data = os.read(master, 65536)
                            if not data:
                                break
                            self.wfile.write(data)
                    except OSError:
                        pass
                    break
                r, _, _ = _select.select(
                    [master, conn],
                    [],
                    [],
                    _TERMINAL_STREAM_SELECT_SECONDS,
                )
                if conn in r:
                    try:
                        probe = conn.recv(4096)
                    except OSError:
                        probe = b""
                    if not probe:
                        break  # client closed the stream
                if master in r:
                    try:
                        data = os.read(master, 65536)
                    except OSError:
                        break
                    if not data:
                        break
                    self.wfile.write(data)
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.terminal_streams.unregister(stream_id, master)
            self._kill_pty(proc, master)
        return True


    return False

def handle_post(self, path, parsed, body) -> bool:  # noqa: C901
    if path == "/api/tmux/stream-input":
        stream_id = str(body.get("stream_id", "")).strip()
        text = body.get("text", "")
        if not isinstance(text, str):
            st, b, h = _json_bytes(
                {"ok": False, "error": "text must be string"}, 400
            )
            self._send(st, b, h)
            return True
        ok, msg = self.terminal_streams.write(stream_id, text)
        st, b, h = (
            _json_bytes({"ok": True})
            if ok
            else _json_bytes({"ok": False, "error": msg}, 409)
        )
        self._send(st, b, h)
        return True


    if path == "/api/tmux/stream-close":
        stream_id = str(body.get("stream_id", "")).strip()
        ok, msg = self.terminal_streams.close(stream_id)
        st, b, h = (
            _json_bytes({"ok": True})
            if ok
            else _json_bytes({"ok": False, "error": msg}, 400)
        )
        self._send(st, b, h)
        return True


    if path == "/api/tmux/stream-heartbeat":
        stream_id = str(body.get("stream_id", "")).strip()
        ok, msg = self.terminal_streams.touch(stream_id)
        st, b, h = (
            _json_bytes({"ok": True})
            if ok
            else _json_bytes({"ok": False, "error": msg}, 409)
        )
        self._send(st, b, h)
        return True


    if path == "/api/tmux/send-text":
        target = str(body.get("target", "")).strip()
        text = body.get("text", "")
        submit = bool(body.get("submit", False))
        if not isinstance(text, str):
            st, b, h = _json_bytes({"ok": False, "error": "text must be string"}, 400)
            self._send(st, b, h)
            return True
        ok, msg = send_pane_text(target, text, submit=submit)
        st, b, h = (
            _json_bytes({"ok": True})
            if ok
            else _json_bytes({"ok": False, "error": msg}, 400)
        )
        self._send(st, b, h)
        return True


    if path == "/api/tmux/send-key":
        target = str(body.get("target", "")).strip()
        key = str(body.get("key", "")).strip()
        ok, msg = send_pane_key(target, key)
        st, b, h = (
            _json_bytes({"ok": True})
            if ok
            else _json_bytes({"ok": False, "error": msg}, 400)
        )
        self._send(st, b, h)
        return True


    if path == "/api/tmux/send-literal":
        target = str(body.get("target", "")).strip()
        text = body.get("text", "")
        if not isinstance(text, str):
            st, b, h = _json_bytes({"ok": False, "error": "text must be string"}, 400)
            self._send(st, b, h)
            return True
        ok, msg = send_pane_literal(target, text)
        st, b, h = (
            _json_bytes({"ok": True})
            if ok
            else _json_bytes({"ok": False, "error": msg}, 400)
        )
        self._send(st, b, h)
        return True


    if path == "/api/tmux/scroll":
        target = str(body.get("target", "")).strip()
        direction = str(body.get("dir", "up")).strip()
        try:
            lines = int(body.get("lines", 3))
        except (TypeError, ValueError):
            lines = 3
        if not validate_tmux_target(target):
            st, b, h = _json_bytes({"ok": False, "error": "invalid target"}, 400)
            self._send(st, b, h)
            return True
        ok, msg = scroll_pane(target, direction, lines)
        st, b, h = (
            _json_bytes({"ok": True})
            if ok
            else _json_bytes({"ok": False, "error": msg}, 400)
        )
        self._send(st, b, h)
        return True


    return False
