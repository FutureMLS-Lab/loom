import loom.doctor as doctor


def _by_name(report, name):
    return next(c for c in report.checks if c.name == name)


def _which_only(*available):
    present = set(available)
    return lambda name: f"/usr/bin/{name}" if name in present else None


def test_missing_tmux_blocks_web_start(monkeypatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", _which_only("git", "claude"))

    blocking = {c.name for c in doctor.required_failures()}

    assert "tmux" in blocking
    assert "git" not in blocking


def test_missing_agent_cli_fails_doctor_without_blocking_web(monkeypatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", _which_only("tmux", "git"))

    check = doctor._check_agents()

    assert check.status == doctor.FAIL
    assert check.required is False
    assert "agent CLI" not in {c.name for c in doctor.required_failures()}


def test_agent_check_accepts_any_supported_cli(monkeypatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", _which_only("cursor-agent"))

    check = doctor._check_agents()

    assert check.status == doctor.OK
    assert "cursor" in check.detail


def test_port_in_use_is_a_warning_not_a_failure() -> None:
    import socket

    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]

        check = doctor._check_port("127.0.0.1", port)

    assert check.status == doctor.WARN
    assert doctor.Report([check]).blocking == []
