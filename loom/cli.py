"""CLI entry point for Loom.

- ``loom doctor`` checks the external tools Loom drives (tmux, git, agent CLI).
- ``loom init`` writes the minimal PLAN.md / NOTES.md templates
  into the current directory.
- ``loom web`` runs the local web UI for browsing / editing tasks
  and launching the deep-interview pane.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import typer
from rich.console import Console

from loom import __version__
from loom.openclaw import build_openclaw_config, openclaw_status
from loom.paths import bundled_skills_path

HELP = (
    "Loom - a lightweight task console for Claude Code / Codex "
    "(deep interview + PLAN.md + worktrees + diffs + notes)."
)

app = typer.Typer(name="loom", help=HELP, add_completion=False)
console = Console()


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"loom {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Show the Loom version and exit",
    ),
) -> None:
    """Loom - a lightweight task console for Claude Code / Codex."""

_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"

# Fallback templates used when /templates is missing (e.g. installed wheel
# without source dir).  Kept small on purpose - the real content comes from
# the deep-interview pane and the user's own edits.
_INLINE_TEMPLATES = {
    "PLAN.md": """\
# Plan

<!-- One-paragraph goal; the deep-interview pane rewrites it.
     Run `/goal` in Claude Code to act on it. -->

## What we have done

## Results

| metric | target | value |
|---|---|---|
|        |        |       |

## Future to do
- [ ] TODO

## Progress Log
""",
    "NOTES.md": """\
# Notes

Free-form scratch space for future work, ideas, things to come back to.
""",
}


_STATUS_MARK = {"ok": "[green]OK  [/green]", "warn": "[yellow]WARN[/yellow]", "fail": "[red]FAIL[/red]"}


@app.command()
def doctor(
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address to test"),
    port: int = typer.Option(8765, "--port", help="HTTP port to test"),
) -> None:
    """Check that tmux, git, an agent CLI and the bundled assets are usable."""
    from loom.doctor import run_checks

    report = run_checks(host, port)
    console.print(f"[bold]loom {__version__}[/bold]  ({sys.executable})\n")
    for check in report.checks:
        console.print(f"{_STATUS_MARK[check.status]}  {check.name:<15} {check.detail}")
        if check.hint and check.status != "ok":
            console.print(f"      [dim]{check.hint}[/dim]")
    if report.ok:
        console.print("\n[green]Ready.[/green] Start with: loom web --project /path/to/repo")
        return
    console.print(f"\n[red]{len(report.failures)} check(s) failed.[/red]")
    raise typer.Exit(1)


@app.command()
def init() -> None:
    """Create template PLAN.md and NOTES.md in the current directory."""
    created = 0
    for name in ("PLAN.md", "NOTES.md"):
        dest = Path.cwd() / name
        if dest.exists():
            console.print(f"[yellow]Skipped:[/yellow] {name} already exists")
            continue
        src = _TEMPLATES_DIR / name
        if src.is_file():
            shutil.copy2(src, dest)
        else:
            dest.write_text(_INLINE_TEMPLATES[name], encoding="utf-8")
        console.print(f"[green]Created:[/green] {name}")
        created += 1
    if created:
        console.print("\nEdit PLAN.md to describe your goal, NOTES.md for future ideas.")
    else:
        console.print("\nAll template files already exist.")


@app.command("web")
def web_cmd(
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address"),
    port: int = typer.Option(8765, "--port", help="HTTP port"),
    project: Path | None = typer.Option(
        None,
        "--project",
        "-p",
        help="Project root (git checkout); defaults to current directory",
    ),
    skills: Path = typer.Option(
        bundled_skills_path(),
        "--skills",
        help="Default skills markdown for new tasks (package default: loom/skills/charlie_skills.md)",
    ),
    daemon: bool = typer.Option(
        False,
        "--daemon",
        "--nohup",
        help="Start the web server in the background and exit",
    ),
    log_file: Path | None = typer.Option(
        None,
        "--log-file",
        help="Daemon log file; defaults to <project>/.RUD/web.log",
    ),
    auth_token: str | None = typer.Option(
        None,
        "--auth-token",
        help="Require HTTP auth for the web UI/API; username can be anything, password is this token",
    ),
    openclaw: bool = typer.Option(
        False,
        "--openclaw",
        help="Enable direct Loom -> OpenClaw gateway events",
    ),
    openclaw_url: str | None = typer.Option(
        None,
        "--openclaw-url",
        help="OpenClaw gateway URL to POST Loom events to",
    ),
    openclaw_token: str | None = typer.Option(
        None,
        "--openclaw-token",
        help="OpenClaw hooks token; sent as Authorization: Bearer <token>",
    ),
    openclaw_header: list[str] | None = typer.Option(
        None,
        "--openclaw-header",
        help="Header for OpenClaw requests, repeatable. Use 'Name: value' or 'Name=value'",
    ),
    openclaw_config: Path | None = typer.Option(
        None,
        "--openclaw-config",
        help="Loom OpenClaw JSON config with url, headers, timeout, enabled",
    ),
    openclaw_timeout_ms: int = typer.Option(
        10000,
        "--openclaw-timeout-ms",
        help="OpenClaw request timeout in milliseconds",
    ),
    openclaw_hook: str | None = typer.Option(
        None,
        "--openclaw-hook",
        help="OpenClaw HTTP hook payload type: wake or agent; inferred from URL if omitted",
    ),
    openclaw_wake_mode: str = typer.Option(
        "now",
        "--openclaw-wake-mode",
        help="OpenClaw wake mode: now or next-heartbeat",
    ),
    openclaw_agent_name: str | None = typer.Option(
        None,
        "--openclaw-agent-name",
        help="Name field for /hooks/agent payloads",
    ),
    openclaw_agent_id: str | None = typer.Option(
        None,
        "--openclaw-agent-id",
        help="Optional agentId for /hooks/agent payloads",
    ),
    openclaw_channel: str | None = typer.Option(
        None,
        "--openclaw-channel",
        help="Optional channel for /hooks/agent delivery, such as slack",
    ),
    openclaw_to: str | None = typer.Option(
        None,
        "--openclaw-to",
        help="Optional delivery target for /hooks/agent, such as channel:C123",
    ),
    openclaw_debug: bool = typer.Option(
        False,
        "--openclaw-debug",
        help="Enable Loom OpenClaw debug logging",
    ),
    projects: bool = typer.Option(
        False,
        "--projects",
        help=(
            "Multi-project workspace: launch directory is a container for several git repos; "
            "drop a redundant registry row for the launch path when child repos are registered. "
            "Omit this if the launch directory itself is a normal single project root."
        ),
    ),
    public_url: str | None = typer.Option(
        None,
        "--public-url",
        envvar="LOOM_PUBLIC_URL",
        help=(
            "The https:// address clients outside this machine use (tunnel or proxy). "
            "It is the OAuth issuer ChatGPT checks; without it, the request's own host is used."
        ),
    ),
) -> None:
    """Start local web UI for `.RUD` tasks (interview, PLAN.md, NOTES.md)."""
    from loom.doctor import required_failures
    from loom.web import serve

    blocking = required_failures()
    if blocking:
        console.print("[red]Loom cannot start - missing prerequisites:[/red]")
        for check in blocking:
            console.print(f"  [red]x[/red] {check.name}: {check.detail}")
            if check.hint:
                console.print(f"    [dim]{check.hint}[/dim]")
        console.print("\nRun [bold]loom doctor[/bold] for the full report.")
        raise typer.Exit(1)

    root = (project or Path.cwd()).resolve()
    web_auth_token = (auth_token or os.environ.get("LOOM_WEB_AUTH_TOKEN", "")).strip()
    openclaw_cfg = build_openclaw_config(
        enabled=openclaw,
        url=openclaw_url,
        token=openclaw_token,
        headers=openclaw_header,
        config_path=openclaw_config,
        timeout_ms=openclaw_timeout_ms,
        hook=openclaw_hook,
        wake_mode=openclaw_wake_mode,
        agent_name=openclaw_agent_name,
        agent_id=openclaw_agent_id,
        channel=openclaw_channel,
        to=openclaw_to,
        debug=openclaw_debug,
    )
    if daemon:
        log_path = (log_file.expanduser().resolve() if log_file else root / ".RUD" / "web.log")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            sys.executable,
            "-m",
            "loom",
            "web",
            "--host",
            host,
            "--port",
            str(port),
            "--project",
            str(root),
            "--skills",
            str(skills.resolve()),
        ]
        child_env = os.environ.copy()
        if web_auth_token:
            child_env["LOOM_WEB_AUTH_TOKEN"] = web_auth_token
        if openclaw_cfg.enabled:
            cmd.append("--openclaw")
        if openclaw_url:
            cmd.extend(["--openclaw-url", openclaw_url])
        if openclaw_token:
            cmd.extend(["--openclaw-token", openclaw_token])
        if openclaw_config:
            cmd.extend(["--openclaw-config", str(openclaw_config.expanduser().resolve())])
        if openclaw_timeout_ms != 10000:
            cmd.extend(["--openclaw-timeout-ms", str(openclaw_timeout_ms)])
        if openclaw_hook:
            cmd.extend(["--openclaw-hook", openclaw_hook])
        if openclaw_wake_mode != "now":
            cmd.extend(["--openclaw-wake-mode", openclaw_wake_mode])
        if openclaw_agent_name:
            cmd.extend(["--openclaw-agent-name", openclaw_agent_name])
        if openclaw_agent_id:
            cmd.extend(["--openclaw-agent-id", openclaw_agent_id])
        if openclaw_channel:
            cmd.extend(["--openclaw-channel", openclaw_channel])
        if openclaw_to:
            cmd.extend(["--openclaw-to", openclaw_to])
        if openclaw_debug:
            cmd.append("--openclaw-debug")
        if projects:
            cmd.append("--projects")
        if public_url:
            cmd.extend(["--public-url", public_url])
        for h in openclaw_header or []:
            cmd.extend(["--openclaw-header", h])
        with open(log_path, "ab", buffering=0) as out:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                cwd=str(root),
                env=child_env,
                start_new_session=True,
                close_fds=True,
            )
        console.print(f"[green]Loom started in background[/green] pid={proc.pid}")
        console.print(f"[dim]URL:[/dim] http://{host}:{port}/")
        console.print(f"[dim]Log:[/dim] {log_path}")
        if openclaw_cfg.enabled:
            console.print(f"[dim]OpenClaw:[/dim] {openclaw_status(openclaw_cfg)}")
        if web_auth_token:
            console.print("[dim]Auth:[/dim] enabled")
        return
    serve(
        host,
        port,
        root,
        skills.resolve(),
        openclaw_config=openclaw_cfg,
        auth_token=web_auth_token,
        multi_project_workspace=projects,
        public_url=(public_url or "").strip(),
    )


# --- agents and bots ----------------------------------------------------------
# Loom's tool catalog (loom/agent_tools.py) over MCP: stdio here for local
# agents, Streamable HTTP at POST /mcp on the server for everyone else, and a
# config printer so connecting a new client is copy-paste.


@app.command("mcp")
def mcp_cmd(
    url: str | None = typer.Option(
        None, "--url", envvar="LOOM_URL", help="Loom server URL (default http://127.0.0.1:8765)"
    ),
    token: str | None = typer.Option(
        None, "--token", envvar="LOOM_WEB_AUTH_TOKEN", help="The server's --auth-token", show_default=False
    ),
) -> None:
    """Serve Loom's tools to a local agent over MCP stdio.

    For clients that launch MCP servers as a subprocess (Claude Desktop,
    Cursor, Codex). Needs a running `loom web`; remote agents connect to its
    POST /mcp endpoint instead.
    """
    from loom.mcp_server import serve_stdio

    serve_stdio(url, token)


@app.command("agent-config")
def agent_config_cmd(
    url: str | None = typer.Option(
        None,
        "--url",
        envvar="LOOM_URL",
        help="Base URL as the *client* sees Loom (e.g. the far end of an SSH tunnel)",
    ),
    show_token: bool = typer.Option(
        False, "--show-token", help="Embed this host's agent token instead of $LOOM_AGENT_TOKEN"
    ),
) -> None:
    """Print ready-to-paste configs that connect agents and bots to Loom.

    Remote agents and bots get the *agent token*: it opens /mcp and nothing
    else, so a bot can never reach deletes or raw keystrokes even if its
    config leaks. Only the local stdio server uses the full web token.
    """
    from loom.routes_agent import agent_token, agent_token_path

    base = (url or "http://127.0.0.1:8765").rstrip("/")
    real = agent_token()  # created on first use, so the `cat` hint below works
    tok = real if show_token else "$LOOM_AGENT_TOKEN"
    stdio = {
        "mcpServers": {
            "loom": {
                "command": "loom",
                "args": ["mcp"],
                "env": {"LOOM_URL": base, "LOOM_WEB_AUTH_TOKEN": "$LOOM_WEB_AUTH_TOKEN"},
            }
        }
    }
    cursor = {
        "mcpServers": {
            "loom": {"url": f"{base}/mcp", "headers": {"Authorization": "Bearer ${env:LOOM_AGENT_TOKEN}"}}
        }
    }
    typer.echo(
        f"""# Loom agent gateway - {base}
#   MCP (Streamable HTTP): {base}/mcp
#   Manifest:              {base}/api/agent/manifest
#   Agent token:           {agent_token_path()}  - opens /mcp only
#                          export LOOM_AGENT_TOKEN=$(cat {agent_token_path()})

## Claude Code
claude mcp add --scope user --transport http loom {base}/mcp --header "Authorization: Bearer {tok}"

## Codex CLI   (LOOM_AGENT_TOKEN must be exported where codex runs)
codex mcp add loom --url {base}/mcp --bearer-token-env-var LOOM_AGENT_TOKEN

## Cursor - ~/.cursor/mcp.json
{json.dumps(cursor, indent=2)}

## OpenClaw - run on the gateway host
openclaw mcp add loom --url {base}/mcp --transport streamable-http --header "Authorization=Bearer {tok}" --approval auto
openclaw mcp doctor loom --probe

## Local stdio clients on this host (Claude Desktop, ...) - full web token
{json.dumps(stdio, indent=2)}

## ChatGPT (and other clients that only speak OAuth) - no token to paste
# ChatGPT reaches Loom from the internet, so Loom needs a public https address
# (Cloudflare Tunnel, Tailscale Funnel, a proxy); start the server with
#   loom web ... --public-url https://loom.example.com
# Then in ChatGPT: Settings -> Apps & Connectors -> Advanced -> Developer mode,
# create a connector with URL https://loom.example.com/mcp and OAuth. ChatGPT
# registers itself and opens Loom's approval page: sign in there with this
# server's --auth-token. ChatGPT gets its own token for /mcp only; list or cut
# it off with `loom oauth list` / `loom oauth revoke`, or Connected apps.
"""
    )


# --- OAuth grants -----------------------------------------------------------------
# What ChatGPT and other OAuth clients were allowed in, and a way to cut them
# off. The state is the server's own file, which a running server re-reads.

oauth_app = typer.Typer(help="Apps connected over OAuth (ChatGPT, ...): list and revoke.")
app.add_typer(oauth_app, name="oauth")


@oauth_app.command("list")
def oauth_list_cmd() -> None:
    """List connected apps: one row per approval."""
    import time as _time

    from loom.oauth import OAuthStore, oauth_state_path

    rows = OAuthStore().grants()
    if not rows:
        typer.echo(f"No connected apps.  ({oauth_state_path()})")
        return

    def when(ts: float) -> str:
        return _time.strftime("%Y-%m-%d %H:%M", _time.localtime(ts)) if ts else "-"

    for row in rows:
        typer.echo(
            f"{row['id']}  {row['client_name']}  ->{row['redirect_host'] or '?'}"
            f"  approved {when(row['approved_at'])}  last used {when(row['last_used_at'])}"
        )


@oauth_app.command("revoke")
def oauth_revoke_cmd(
    grant: str = typer.Argument(None, help="Grant id from `loom oauth list`"),
    all_: bool = typer.Option(False, "--all", help="Revoke every grant and forget every registered app"),
) -> None:
    """Cut an app off. It must be approved again to reconnect."""
    from loom.oauth import OAuthStore

    store = OAuthStore()
    if all_:
        typer.echo(f"Revoked {store.revoke_all()} grant(s); every app must connect again.")
        return
    if not grant:
        typer.echo("Name a grant id (see `loom oauth list`) or pass --all.")
        raise typer.Exit(1)
    if not store.revoke_grant(grant):
        typer.echo(f"No grant {grant}.")
        raise typer.Exit(1)
    typer.echo(f"Revoked {grant}.")
