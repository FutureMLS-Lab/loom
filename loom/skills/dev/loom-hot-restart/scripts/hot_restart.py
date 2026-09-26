#!/usr/bin/env python3
"""Controlled Loom restart that preserves auth, tasks, tmux, and Turbogate.

Linux only: process discovery and environment transfer use /proc.
Secrets are copied in memory and never printed or written to disk.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SECRET_NAMES = ("LOOM_WEB_AUTH_TOKEN", "TOGETHER_API_KEY")


@dataclass(frozen=True)
class Process:
    pid: int
    ppid: int
    argv: tuple[str, ...]
    cwd: Path
    env: dict[str, str]


def _proc_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def _read_process(pid: int) -> Process | None:
    root = Path("/proc") / str(pid)
    try:
        argv = tuple(
            part.decode("utf-8", "surrogateescape")
            for part in (root / "cmdline").read_bytes().split(b"\0")
            if part
        )
        env: dict[str, str] = {}
        for entry in (root / "environ").read_bytes().split(b"\0"):
            if b"=" not in entry:
                continue
            key, value = entry.split(b"=", 1)
            env[key.decode("utf-8", "surrogateescape")] = value.decode(
                "utf-8", "surrogateescape"
            )
        ppid = 0
        for line in (root / "status").read_text().splitlines():
            if line.startswith("PPid:"):
                ppid = int(line.split(":", 1)[1].strip())
                break
        return Process(
            pid=pid,
            ppid=ppid,
            argv=argv,
            cwd=(root / "cwd").resolve(),
            env=env,
        )
    except (FileNotFoundError, PermissionError, ProcessLookupError, ValueError):
        return None


def _processes() -> list[Process]:
    out: list[Process] = []
    for item in Path("/proc").iterdir():
        if item.name.isdigit():
            process = _read_process(int(item.name))
            if process is not None and process.argv:
                out.append(process)
    return out


def _option(argv: tuple[str, ...], name: str) -> str:
    for index, value in enumerate(argv):
        if value == name and index + 1 < len(argv):
            return argv[index + 1]
        if value.startswith(name + "="):
            return value.split("=", 1)[1]
    return ""


def _is_loom_web_argv(argv: tuple[str, ...]) -> bool:
    """Recognize module, console-script, and Python console-script launches."""
    if len(argv) >= 4 and argv[1:4] == ("-m", "loom", "web"):
        return True
    if len(argv) >= 2 and Path(argv[0]).name == "loom" and argv[1] == "web":
        return True
    return (
        len(argv) >= 3
        and Path(argv[0]).name.startswith("python")
        and Path(argv[1]).name == "loom"
        and argv[2] == "web"
    )


def find_loom(port: int) -> Process:
    matches = []
    for process in _processes():
        args = process.argv
        if _is_loom_web_argv(args) and _option(args, "--port") == str(port):
            matches.append(process)
    if len(matches) != 1:
        raise RuntimeError(
            f"expected exactly one Loom process on port {port}, found {len(matches)}"
        )
    return matches[0]


def find_turbogate(port: int) -> Process | None:
    matches = []
    for process in _processes():
        args = process.argv
        # Some installations invoke the packaged binary through ld-linux, so
        # Turbogate is argv[1] rather than argv[0].
        is_turbogate = any(
            Path(value).name == "turbogate" for value in args[:2]
        )
        if (
            is_turbogate
            and "http" in args
            and str(port) in args
            and "--public" in args
        ):
            matches.append(process)
    if len(matches) > 1:
        raise RuntimeError(f"multiple Turbogate processes target port {port}")
    return matches[0] if matches else None


def _api_json(url: str, token: str, *, timeout: float = 5.0) -> dict[str, Any]:
    request = urllib.request.Request(
        url, headers={"Authorization": "Bearer " + token}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.load(response)
    return payload if isinstance(payload, dict) else {"value": payload}


def _wait_api(
    url: str, token: str, *, timeout: float, interval: float = 0.25
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return _api_json(url, token, timeout=min(5.0, timeout))
        except Exception as exc:  # noqa: BLE001 - retry boundary
            last_error = exc
            time.sleep(interval)
    name = type(last_error).__name__ if last_error else "unknown error"
    raise RuntimeError(f"{url} did not become healthy ({name})")


def _active_one_shot_jobs(port: int, token: str) -> list[str]:
    """Reviewer/idea/mining jobs die with the server; tmux author loops survive."""
    base = f"http://127.0.0.1:{port}"
    try:
        payload = _api_json(base + "/api/projects", token)
    except Exception:
        return ["could not inspect active jobs"]
    projects = payload.get("projects", payload.get("value", []))
    if not isinstance(projects, list):
        return ["could not parse the project list"]
    active: list[str] = []
    for project in projects:
        if not isinstance(project, dict) or not project.get("id"):
            continue
        project_id = str(project["id"])
        try:
            tasks = _api_json(
                base
                + "/api/tasks?project="
                + urllib.parse.quote(project_id),
                token,
            ).get("tasks", [])
        except Exception:
            continue
        for task in tasks if isinstance(tasks, list) else []:
            if not isinstance(task, dict) or str(task.get("kind", "")).lower() not in (
                "ar",
                "aris",
            ):
                continue
            slug = str(task.get("slug") or "")
            try:
                ar_payload = _api_json(
                    base
                    + "/api/tasks/"
                    + urllib.parse.quote(slug)
                    + "/ar?project="
                    + urllib.parse.quote(project_id),
                    token,
                )
            except Exception:
                continue
            state = ar_payload.get("state") or {}
            for job in ("papers", "ideas", "review"):
                if str(state.get(f"{job}_status") or "") == "running":
                    active.append(f"{project_id}/{slug}: {job}")
    return active


def _secret_fingerprints(env: dict[str, str]) -> dict[str, str]:
    return {
        name: hashlib.sha256(env.get(name, "").encode()).hexdigest()
        for name in SECRET_NAMES
    }


def _wait_dead(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _proc_alive(pid):
            return True
        time.sleep(0.1)
    return not _proc_alive(pid)


def _port_open(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _stop_group(process: Process, *, graceful: bool) -> None:
    sig = signal.SIGTERM if graceful else signal.SIGKILL
    os.kill(process.pid, sig)
    if _wait_dead(process.pid, 12.0 if graceful else 5.0):
        return
    os.kill(process.pid, signal.SIGKILL)
    if not _wait_dead(process.pid, 5.0):
        raise RuntimeError(f"process {process.pid} did not exit")


def _stop_obsolete_port(port: int) -> None:
    process = find_loom(port)
    tunnel = find_turbogate(port)
    _stop_group(process, graceful=True)
    if tunnel is not None and _proc_alive(tunnel.pid):
        try:
            os.killpg(tunnel.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        if not _wait_dead(tunnel.pid, 5.0):
            try:
                os.killpg(tunnel.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    if _port_open(port):
        raise RuntimeError(f"obsolete Loom port {port} is still listening")


def _preserve_tunnel_pipe(parent: Process, tunnel: Process) -> int | None:
    """Keep the tunnel's stdout readable after killing its original parent."""
    if tunnel.ppid != parent.pid:
        return None  # It is already detached and has a surviving reader.
    try:
        target = os.readlink(f"/proc/{tunnel.pid}/fd/1")
    except OSError as exc:
        raise RuntimeError(f"cannot inspect Turbogate stdout: {exc}") from exc
    read_fd: int | None = None
    for item in (Path("/proc") / str(parent.pid) / "fd").iterdir():
        try:
            if os.readlink(item) == target:
                read_fd = os.open(item, os.O_RDONLY)
                break
        except OSError:
            continue
    if read_fd is None:
        raise RuntimeError("cannot preserve the Turbogate stdout pipe")
    child = os.fork()
    if child == 0:
        try:
            os.setsid()
            os.set_blocking(read_fd, True)
            while os.read(read_fd, 65536):
                pass
        except Exception:
            pass
        finally:
            try:
                os.close(read_fd)
            except OSError:
                pass
        os._exit(0)
    os.close(read_fd)
    return child


def _public_url(port: int, token: str, supplied: str) -> str:
    if supplied:
        return supplied.rstrip("/")
    try:
        status = _api_json(
            f"http://127.0.0.1:{port}/api/turbogate", token
        )
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return ""
        raise
    return str(status.get("url") or "").rstrip("/")


def _launch_command(old: Process, source: Path) -> tuple[list[str], Path]:
    python = source / ".venv" / "bin" / "python"
    if not python.is_file():
        raise RuntimeError(f"updated interpreter does not exist: {python}")
    argv = list(old.argv)
    if len(argv) >= 4 and argv[1:4] == ["-m", "loom", "web"]:
        argv[0] = str(python)
    elif (
        len(argv) >= 3
        and Path(argv[0]).name.startswith("python")
        and Path(argv[1]).name == "loom"
        and argv[2] == "web"
    ):
        argv = [str(python), "-m", "loom", *argv[2:]]
    elif len(argv) >= 2 and Path(argv[0]).name == "loom" and argv[1] == "web":
        argv = [str(python), "-m", "loom", *argv[1:]]
    else:
        raise RuntimeError("unsupported Loom command shape")
    return argv, source


def _output_stream(old: Process, source: Path, port: int):
    try:
        target = os.readlink(f"/proc/{old.pid}/fd/1")
    except OSError:
        target = ""
    if target.startswith("/dev/") and Path(target).exists():
        return open(target, "a", buffering=1)
    log = source / ".RUD" / f"loom-{port}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    return log.open("a", buffering=1)


def restart(args: argparse.Namespace) -> dict[str, Any]:
    source = args.source.expanduser().resolve()
    if not (source / "loom").is_dir():
        raise RuntimeError(f"not a Loom source checkout: {source}")
    old = find_loom(args.port)
    token = old.env.get("LOOM_WEB_AUTH_TOKEN", "")
    if not token:
        raise RuntimeError("running Loom has no LOOM_WEB_AUTH_TOKEN")
    proc_path = Path(f"/proc/{old.pid}")
    if proc_path.exists() and os.geteuid() != proc_path.stat().st_uid:
        raise RuntimeError("running Loom belongs to another user")

    active = _active_one_shot_jobs(args.port, token)
    if active and not args.allow_active_jobs:
        raise RuntimeError(
            "refusing to interrupt non-resumable jobs: "
            + ", ".join(active)
            + " (wait, or pass --allow-active-jobs)"
        )

    tunnel = find_turbogate(args.port) if args.preserve_tunnel else None
    public_url = _public_url(args.port, token, args.public_url)
    if args.preserve_tunnel:
        if tunnel is None:
            raise RuntimeError("no Turbogate process found for the target port")
        if not public_url:
            raise RuntimeError(
                "public URL is unknown; pass --public-url so identity can be verified"
            )
        for name in SECRET_NAMES:
            if not old.env.get(name):
                raise RuntimeError(f"running Loom is missing {name}")

    command, cwd = _launch_command(old, source)
    result: dict[str, Any] = {
        "old_pid": old.pid,
        "port": args.port,
        "source": str(source),
        "public_url": public_url,
        "active_one_shot_jobs": active,
        "dry_run": bool(args.dry_run),
    }
    if args.dry_run:
        result["tunnel_pid"] = tunnel.pid if tunnel else None
        result["command"] = command
        return result

    for port in args.stop_port:
        if port == args.port:
            raise RuntimeError("--stop-port cannot equal --port")
        _stop_obsolete_port(port)

    before = _secret_fingerprints(old.env)
    drainer_pid = (
        _preserve_tunnel_pipe(old, tunnel)
        if tunnel is not None
        else None
    )
    _stop_group(old, graceful=not bool(tunnel))
    if tunnel is not None and not _proc_alive(tunnel.pid):
        raise RuntimeError("Turbogate exited while replacing Loom")

    deadline = time.monotonic() + 8.0
    while _port_open(args.port) and time.monotonic() < deadline:
        time.sleep(0.1)
    if _port_open(args.port):
        raise RuntimeError(f"port {args.port} did not become free")

    output = _output_stream(old, source, args.port)
    try:
        new = subprocess.Popen(
            command,
            cwd=str(cwd),
            env=old.env,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        output.close()

    try:
        _wait_api(
            f"http://127.0.0.1:{args.port}/api/projects",
            token,
            timeout=args.startup_timeout,
        )
        if public_url:
            _wait_api(
                public_url + "/api/projects",
                token,
                timeout=args.startup_timeout,
            )
    except Exception:
        if _proc_alive(new.pid):
            _stop_group(_read_process(new.pid) or old, graceful=True)
        raise

    current = _read_process(new.pid)
    if current is None:
        raise RuntimeError("restarted Loom disappeared after health check")
    after = _secret_fingerprints(current.env)
    if before != after:
        raise RuntimeError("secret fingerprints changed during restart")
    if tunnel is not None and not _proc_alive(tunnel.pid):
        raise RuntimeError("Turbogate disappeared after health check")

    result.update(
        {
            "new_pid": new.pid,
            "tunnel_pid": tunnel.pid if tunnel else None,
            "drainer_pid": drainer_pid,
            "local_health": "ok",
            "public_health": "ok" if public_url else "not-requested",
            "secrets_unchanged": True,
        }
    )
    state_file = source / ".RUD" / f"hot-restart-{args.port}.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(
        json.dumps(
            {
                "port": args.port,
                "public_url": public_url,
                "last_pid": new.pid,
                "tunnel_pid": tunnel.pid if tunnel else None,
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Restart Loom from an updated checkout while preserving its "
            "environment, tmux tasks, and optional public tunnel."
        )
    )
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument(
        "--stop-port",
        type=int,
        action="append",
        default=[],
        help="obsolete Loom port to stop completely; repeat as needed",
    )
    parser.add_argument(
        "--public-url",
        default="",
        help="expected existing Turbogate URL when the old API cannot report it",
    )
    parser.add_argument(
        "--preserve-tunnel",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument("--allow-active-jobs", action="store_true")
    parser.add_argument("--startup-timeout", type=float, default=45.0)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    try:
        result = restart(parse_args())
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    print(json.dumps({"ok": True, **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
