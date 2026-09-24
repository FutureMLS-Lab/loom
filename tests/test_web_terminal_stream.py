import os
import socket
from pathlib import Path

import loom.routes_tmux as routes_tmux
import loom.web as web
from loom.web import _TerminalStreamRegistry


class _FakeConnection:
    def __init__(self) -> None:
        self.options: list[tuple[int, int, int]] = []
        self.timeout: int | None = None

    def setsockopt(self, level: int, option: int, value: int) -> None:
        self.options.append((level, option, value))

    def settimeout(self, value: int) -> None:
        self.timeout = value


def test_terminal_stream_socket_uses_darwin_keepalive_fallback(monkeypatch) -> None:
    monkeypatch.delattr(routes_tmux.socket, "TCP_KEEPIDLE", raising=False)
    monkeypatch.setattr(routes_tmux.socket, "TCP_KEEPALIVE", 0x10, raising=False)
    connection = _FakeConnection()

    routes_tmux._configure_stream_socket(connection)

    assert (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1) in connection.options
    assert (socket.IPPROTO_TCP, 0x10, 30) in connection.options
    assert connection.timeout == 60


def test_terminal_stream_routes_input_to_registered_pty() -> None:
    read_fd, write_fd = os.pipe()
    registry = _TerminalStreamRegistry()
    stream_id = registry.register(write_fd)
    try:
        ok, error = registry.write(stream_id, "\x1b[?1;2c")
        assert ok is True
        assert error == ""
        assert os.read(read_fd, 8) == b"\x1b[?1;2c"

        registry.unregister(stream_id, write_fd)
        ok, error = registry.write(stream_id, "x")
        assert ok is False
        assert error == "terminal stream is not active"
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_terminal_stream_rejects_invalid_id_and_large_input() -> None:
    read_fd, write_fd = os.pipe()
    registry = _TerminalStreamRegistry()
    stream_id = registry.register(write_fd)
    try:
        assert registry.write("not-a-stream", "x") == (
            False,
            "invalid terminal stream",
        )
        assert registry.write(stream_id, "x" * (64 * 1024 + 1)) == (
            False,
            "terminal input too large",
        )
    finally:
        registry.unregister(stream_id, write_fd)
        os.close(read_fd)
        os.close(write_fd)


def test_terminal_stream_close_terminates_registered_attach() -> None:
    class Proc:
        terminated = False

        def terminate(self) -> None:
            self.terminated = True

    read_fd, write_fd = os.pipe()
    proc = Proc()
    registry = _TerminalStreamRegistry()
    stream_id = registry.register(write_fd, proc)
    try:
        assert registry.close(stream_id) == (True, "")
        assert proc.terminated is True
        assert registry.lease_active(stream_id) is False
        assert registry.write(stream_id, "x") == (
            False,
            "terminal stream is not active",
        )
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_terminal_stream_lease_requires_browser_heartbeat(monkeypatch) -> None:
    clock = [100.0]
    monkeypatch.setattr(web.time, "monotonic", lambda: clock[0])
    read_fd, write_fd = os.pipe()
    registry = _TerminalStreamRegistry()
    stream_id = registry.register(write_fd)
    try:
        assert registry.lease_active(stream_id, lease_seconds=10) is True
        clock[0] = 111.0
        assert registry.lease_active(stream_id, lease_seconds=10) is False
        assert registry.touch(stream_id) == (True, "")
        assert registry.lease_active(stream_id, lease_seconds=10) is True
    finally:
        registry.unregister(stream_id, write_fd)
        os.close(read_fd)
        os.close(write_fd)


def test_terminal_clients_explicitly_close_and_heartbeat_streams() -> None:
    static_root = Path(__file__).resolve().parents[1] / "loom" / "web_static"
    for name in ("app.js", "terminal.html"):
        source = (static_root / name).read_text(encoding="utf-8")
        assert "/api/tmux/stream-close" in source
        assert "/api/tmux/stream-heartbeat" in source
        assert "pagehide" in source
