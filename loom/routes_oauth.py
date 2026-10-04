"""OAuth routes: how ChatGPT (and any MCP client that only speaks OAuth)
gets into Loom's MCP endpoint without ever holding a Loom token.

Public, served before the server's own auth:

- ``GET  /.well-known/oauth-protected-resource[/mcp]``  RFC 9728
- ``GET  /.well-known/oauth-authorization-server``      RFC 8414
- ``POST /oauth/register``                              RFC 7591
- ``GET  /oauth/authorize``  the approval page; ``POST`` its form
- ``POST /oauth/token``      authorization_code (PKCE S256) and refresh_token
- ``POST /oauth/revoke``     RFC 7009

Behind the normal auth, for the console's Connected apps:

- ``GET    /api/oauth/grants``
- ``DELETE /api/oauth/grants/<id>``   (``all`` revokes everything)

None of this exists unless the server runs with ``--auth-token``: the owner
approves with that token, so without it there is nothing to approve with.
The logic and the state live in loom/oauth.py.
"""

from __future__ import annotations

import hmac
import json
import re
from html import escape
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from loom import oauth
from loom.routes_agent import AGENT_ROUTES
from loom.web_util import _json_bytes

MAX_FORM_BYTES = 64 * 1024
_PRM_PATHS = ("/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp")
_ASM_PATHS = ("/.well-known/oauth-authorization-server", "/.well-known/oauth-authorization-server/mcp")


def enabled(self) -> bool:
    return bool(getattr(self, "auth_token", "")) and getattr(self, "oauth_store", None) is not None


def public_base(self) -> str:
    """This server as the outside world reaches it - the OAuth issuer.

    ``--public-url`` (or ``LOOM_PUBLIC_URL``) decides when it is set; behind a
    tunnel or proxy that is what to use, since the issuer must compare equal,
    character for character, everywhere a client meets it. Otherwise the
    request's own forwarded proto and host stand in.
    """
    configured = str(getattr(self, "public_url", "") or "").strip().rstrip("/")
    if configured:
        return configured
    proto = (self.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
    if proto not in ("http", "https"):
        proto = "http"
    host = (self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or "").split(",")[0].strip()
    if not host:
        bound, port = self.server.server_address[:2]
        host = f"{bound}:{port}"
    return f"{proto}://{host}"


def challenge(self, presented: bool) -> str:
    """The WWW-Authenticate value a 401 from /mcp carries, pointing at discovery."""
    value = (
        f'Bearer resource_metadata="{public_base(self)}{_PRM_PATHS[1]}", scope="{oauth.SCOPE}"'
    )
    if presented:
        value += ', error="invalid_token", error_description="the access token is invalid or expired"'
    return value


def bearer_allows(self, token: str, path: str) -> bool:
    """Whether an OAuth access token opens *path*: the agent routes, nothing else."""
    if not enabled(self) or path not in AGENT_ROUTES:
        return False
    return self.oauth_store.access_allows(token, public_base(self))


# --- plumbing ----------------------------------------------------------------------


def _client_ip(self) -> str:
    peer = str((self.client_address or ("",))[0])
    # Behind the local proxy every request arrives from loopback, and the
    # proxy names the real client in X-Forwarded-For (Caddy replaces whatever
    # the client sent). The approval rate limit counts that address, so one
    # guesser cannot lock the owner out from everywhere.
    if peer in ("127.0.0.1", "::1"):
        forwarded = (self.headers.get("X-Forwarded-For") or "").split(",")[-1].strip()
        if forwarded:
            return forwarded
    return peer


def _read_body(self) -> bytes | None:
    try:
        size = int(self.headers.get("Content-Length", "0") or 0)
    except ValueError:
        return None
    if size < 0 or size > MAX_FORM_BYTES:
        return None
    return self.rfile.read(size) if size else b""


def _form(raw: bytes) -> dict[str, str]:
    parsed = parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True)
    return {k: v[0] for k, v in parsed.items() if v}


def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
    body = json.dumps(payload).encode("utf-8")
    self.send_response(status)
    self.send_header("Content-Type", "application/json")
    self.send_header("Content-Length", str(len(body)))
    self.send_header("Cache-Control", "no-store")
    self.send_header("Pragma", "no-cache")
    self.end_headers()
    self.wfile.write(body)


def _send_html(self, html: str, status: int = 200, form_target: str = "") -> None:
    body = html.encode("utf-8")
    # The approval page is the one public page that takes a secret, so it is
    # locked down: nothing loads from anywhere, it cannot be framed (no
    # clickjacking the Allow button), and its form may only post here and
    # follow the redirect to the app that asked.
    actions = "'self'" + (f" {form_target}" if form_target else "")
    csp = (
        "default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
        f"form-action {actions}; frame-ancestors 'none'; base-uri 'none'"
    )
    self.send_response(status)
    self.send_header("Content-Type", "text/html; charset=utf-8")
    self.send_header("Content-Length", str(len(body)))
    self.send_header("Cache-Control", "no-store")
    self.send_header("Content-Security-Policy", csp)
    self.send_header("X-Frame-Options", "DENY")
    self.send_header("Referrer-Policy", "no-referrer")
    self.end_headers()
    self.wfile.write(body)


def _redirect(self, uri: str, params: dict[str, str]) -> None:
    """Back to the client, with ``iss`` on every response (RFC 9207)."""
    parts = urlsplit(uri)
    extra = urlencode({k: v for k, v in params.items() if v})
    query = f"{parts.query}&{extra}" if parts.query else extra
    location = urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))
    self.send_response(302)
    self.send_header("Location", location)
    self.send_header("Content-Length", "0")
    self.send_header("Cache-Control", "no-store")
    self.send_header("Referrer-Policy", "no-referrer")
    self.end_headers()


def _origin(uri: str) -> str:
    parts = urlsplit(uri)
    return f"{parts.scheme}://{parts.netloc}"


# --- pages ---------------------------------------------------------------------------

_PAGE_STYLE = """
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#f7f5ef;
font:15px/1.55 Inter,"SF Pro Text","Segoe UI",system-ui,sans-serif;color:#1f2330}
.card{width:min(470px,92vw);box-sizing:border-box;background:#fff;border:1px solid #e8e4da;
border-radius:16px;box-shadow:0 20px 50px rgba(31,35,48,.12);padding:26px 28px}
.brand{display:flex;align-items:center;gap:9px;font-weight:700;font-size:1.05rem}
.mark{width:22px;height:22px;border-radius:6px;background:linear-gradient(135deg,#4f46e5,#22c55e)}
h1{font-size:1.22rem;line-height:1.3;margin:14px 0 8px;letter-spacing:-.02em}
p{margin:0 0 12px;color:#5a6072}ul{margin:0 0 14px;padding-left:20px;color:#5a6072}
.host{font-family:ui-monospace,Menlo,monospace;font-size:.88em;background:#f4f2ec;
border:1px solid #e8e4da;border-radius:6px;padding:1px 6px;color:#1f2330}
label{display:block;font-weight:600;font-size:.84rem;margin:18px 0 6px;color:#1f2330}
input[type=password]{width:100%;box-sizing:border-box;padding:10px 12px;border:1px solid #d6d1c3;
border-radius:10px;font:inherit}
input[type=password]:focus{outline:none;border-color:#6366f1;box-shadow:0 0 0 3px rgba(99,102,241,.14)}
.hint{font-size:.8rem;color:#8a8f9d;margin:6px 0 0}
.row{display:flex;gap:10px;justify-content:flex-end;margin-top:20px}
button{font:inherit;font-weight:600;border-radius:999px;padding:9px 18px;cursor:pointer;
border:1px solid #d6d1c3;background:#fff;color:#1f2330}
button.primary{background:#4f46e5;border-color:#4f46e5;color:#fff}
.error{color:#9f1239;background:#fff1f2;border:1px solid #fecdd3;border-radius:10px;padding:8px 12px;margin:14px 0 0}
"""


def _page(title: str, inner: str) -> str:
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>{escape(title)}</title><style>{_PAGE_STYLE}</style></head>"
        f"<body><main class=\"card\"><div class=\"brand\"><span class=\"mark\"></span>Loom</div>{inner}</main></body></html>"
    )


def _approval_page(request_id: str, request: dict[str, Any], error: str = "") -> str:
    name = escape(request.get("client_name") or "An app")
    host = escape(oauth.redirect_host(request.get("redirect_uri", "")) or "the app")
    message = f'<p class="error" role="alert">{escape(error)}</p>' if error else ""
    return _page(
        f"Allow {request.get('client_name') or 'this app'} to use Loom?",
        f"""<h1>Allow {name} to use this Loom?</h1>
<p>It will reach Loom's tools over MCP. It can:</p>
<ul><li>read projects, tasks, agent conversations and terminal screens</li>
<li>send messages and keys to agents, and create, start and stop tasks</li>
<li>run the paper loop and record the gate decisions you tell it to</li></ul>
<p>It gets a token of its own that opens only the MCP endpoint, never your Loom token,
and you can cut it off any time under <b>Connected apps</b> in Loom.</p>
<p>After you answer you go back to <span class="host">{host}</span>.</p>{message}
<form method="post" action="/oauth/authorize">
<input type="hidden" name="request_id" value="{escape(request_id)}">
<input type="text" name="username" value="loom" autocomplete="username" readonly hidden>
<label for="owner-token">Your Loom access token</label>
<input id="owner-token" name="owner_token" type="password" autocomplete="current-password" required autofocus>
<p class="hint">The <code>--auth-token</code> this Loom runs with. It is checked here and goes nowhere else.</p>
<div class="row"><button type="submit" name="decision" value="deny" formnovalidate>Deny</button>
<button type="submit" class="primary" name="decision" value="allow">Allow</button></div>
</form>""",
    )


def _message_page(title: str, text: str) -> str:
    return _page(title, f"<h1>{escape(title)}</h1><p>{escape(text)}</p>")


# --- public routes ------------------------------------------------------------------


def handle_public_get(self, path: str, parsed) -> bool:
    if path not in _PRM_PATHS and path not in _ASM_PATHS and path != "/oauth/authorize":
        return False
    if not enabled(self):
        _send_json(self, {"error": "OAuth is off: this Loom runs without --auth-token"}, 404)
        return True
    base = public_base(self)
    if path in _PRM_PATHS:
        _send_json(self, oauth.protected_resource_metadata(base))
        return True
    if path in _ASM_PATHS:
        _send_json(self, oauth.authorization_server_metadata(base))
        return True
    params = {k: v[0] for k, v in parse_qs(parsed.query or "", keep_blank_values=True).items() if v}
    outcome = self.oauth_store.check_authorization(params, base)
    if "page" in outcome:
        _send_html(self, _message_page("Loom can't approve this", outcome["page"]), 400)
        return True
    if "redirect" in outcome:
        _redirect(
            self,
            outcome["redirect"],
            {"error": outcome["error"], "error_description": outcome["description"],
             "state": outcome["state"], "iss": base},
        )
        return True
    request = outcome["ok"]
    request_id = self.oauth_store.remember(request)
    _send_html(self, _approval_page(request_id, request), form_target=_origin(request["redirect_uri"]))
    return True


def handle_public_post(self, path: str, parsed) -> bool:
    if path not in ("/oauth/register", "/oauth/authorize", "/oauth/token", "/oauth/revoke"):
        return False
    if not enabled(self):
        _send_json(self, {"error": "OAuth is off: this Loom runs without --auth-token"}, 404)
        return True
    raw = _read_body(self)
    if raw is None:
        _send_json(self, oauth._oauth_error("invalid_request", "bad or oversized body"), 400)
        return True
    base = public_base(self)
    store = self.oauth_store

    if path == "/oauth/register":
        try:
            meta = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            meta = None
        registration, error = store.register(meta)
        if error:
            _send_json(self, error, 400)
        else:
            _send_json(self, registration, 201)
        return True

    form = _form(raw)

    if path == "/oauth/authorize":
        request_id = form.get("request_id", "")
        request = store.pending(request_id)
        if request is None:
            _send_html(
                self,
                _message_page("This approval expired", "Start connecting again from the app."),
                400,
            )
            return True
        redirect = request["redirect_uri"]
        if form.get("decision") != "allow":
            store.forget(request_id)
            _redirect(self, redirect, {"error": "access_denied", "error_description": "the owner said no",
                                       "state": request["state"], "iss": base})
            return True
        ip = _client_ip(self)
        if store.approval_blocked(ip):
            _send_html(
                self,
                _message_page("Too many wrong tokens", "Approvals are paused for a few minutes. Try again later."),
                429,
            )
            return True
        presented = form.get("owner_token", "").strip()
        if not presented or not hmac.compare_digest(presented, self.auth_token):
            store.approval_failed(ip)
            _send_html(
                self,
                _approval_page(request_id, request, "That is not this Loom's access token."),
                401,
                form_target=_origin(redirect),
            )
            return True
        store.approval_succeeded(ip)
        store.forget(request_id)
        code = store.issue_code(request)
        _redirect(self, redirect, {"code": code, "state": request["state"], "iss": base})
        return True

    if path == "/oauth/token":
        grant_type = form.get("grant_type", "")
        if grant_type == "authorization_code":
            tokens, error = store.exchange_code(form, base)
        elif grant_type == "refresh_token":
            tokens, error = store.exchange_refresh(form, base)
        else:
            tokens, error = None, oauth._oauth_error("unsupported_grant_type", "use authorization_code or refresh_token")
        if error:
            _send_json(self, error, 400)
        else:
            _send_json(self, tokens)
        return True

    # /oauth/revoke: always 200, as RFC 7009 asks, so it says nothing about
    # whether a token ever existed.
    store.revoke(form.get("token", ""))
    _send_json(self, {})
    return True


# --- the owner's Connected apps (behind the normal auth) ---------------------------

_GRANT_RE = re.compile(r"^/api/oauth/grants/([A-Za-z0-9_\-]+)$")


def handle_get(self, path: str, parsed) -> bool:
    if path != "/api/oauth/grants":
        return False
    on = enabled(self)
    st, b, h = _json_bytes(
        {
            "ok": True,
            "enabled": on,
            "mcp_url": f"{public_base(self)}/mcp",
            "public_url": str(getattr(self, "public_url", "") or ""),
            "grants": self.oauth_store.grants() if on else [],
        }
    )
    self._send(st, b, h)
    return True


def handle_delete(self, path: str, parsed) -> bool:
    m = _GRANT_RE.match(path)
    if not m:
        return False
    if not enabled(self):
        st, b, h = _json_bytes({"ok": False, "error": "OAuth is off"}, 404)
        self._send(st, b, h)
        return True
    gid = m.group(1)
    if gid == "all":
        count = self.oauth_store.revoke_all()
        st, b, h = _json_bytes({"ok": True, "revoked": count})
    else:
        found = self.oauth_store.revoke_grant(gid)
        st, b, h = _json_bytes({"ok": found, "revoked": int(found)}, 200 if found else 404)
    self._send(st, b, h)
    return True
