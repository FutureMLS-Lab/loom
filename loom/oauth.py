"""OAuth 2.1 for Loom's MCP endpoint.

ChatGPT connects to a remote MCP server only through OAuth: it will not carry
a static bearer token the way Claude Code or OpenClaw do. So Loom runs a small
authorization server of its own, for /mcp alone:

- discovery: protected-resource metadata (RFC 9728) and authorization-server
  metadata (RFC 8414), advertising PKCE S256 and issuer identification
  (RFC 9207), which is what lets ChatGPT use its stable redirect URI;
- dynamic client registration (RFC 7591) for public clients;
- the authorization-code grant with PKCE, approved by the owner on Loom's
  own page with the server's --auth-token. That token is typed into Loom and
  checked by Loom; it never travels to the client;
- opaque access tokens (one hour) and rotating refresh tokens (30 days),
  stored only as hashes and bound to this server's MCP resource (RFC 8707).
  They open what the agent token opens - /mcp and the manifest - and no more;
- revocation (RFC 7009), plus listing and cutting off connected apps.

This module is the logic and the state; routes_oauth.py speaks HTTP.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from loom.agent_hooks import loom_home

SCOPE = "mcp"
ACCESS_TTL = 3600
REFRESH_TTL = 30 * 86400
CODE_TTL = 120
PENDING_TTL = 600
MAX_CLIENTS = 200
MAX_PENDING = 100
# The owner's token is 192 bits, so guessing it is hopeless anyway; these
# only keep a scripted guesser from turning the approval page into a load.
FAILURES_PER_IP = 5
FAILURES_GLOBAL = 20
FAILURE_WINDOW = 15 * 60
# How often a used token's "last used" time is written back to disk.
TOUCH_INTERVAL = 300

_VERIFIER_RE = re.compile(r"^[A-Za-z0-9\-._~]{43,128}$")
_CHALLENGE_RE = re.compile(r"^[A-Za-z0-9\-_]{43}$")
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}


def oauth_state_path() -> Path:
    return loom_home() / "oauth" / "state.json"


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def mcp_resource(base: str) -> str:
    """The canonical identifier tokens are bound to: this server's /mcp."""
    return f"{base.rstrip('/')}/mcp"


def resource_matches(value: str, base: str) -> bool:
    """Whether a client's ``resource`` names this server's MCP endpoint.

    Clients differ in what they send - the MCP URL, the bare origin, with or
    without a trailing slash - and all of those mean this server.
    """
    wanted = value.strip().rstrip("/")
    root = base.rstrip("/")
    return wanted in (root, mcp_resource(root))


def pkce_matches(verifier: str, challenge: str) -> bool:
    if not _VERIFIER_RE.match(verifier or "") or not _CHALLENGE_RE.match(challenge or ""):
        return False
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return hmac.compare_digest(computed, challenge)


def redirect_uri_ok(uri: str) -> bool:
    """HTTPS anywhere, or plain HTTP back to this machine for native clients."""
    if not isinstance(uri, str) or not uri or len(uri) > 2000:
        return False
    try:
        parts = urlsplit(uri)
    except ValueError:
        return False
    if parts.fragment or parts.username or parts.password or not parts.hostname:
        return False
    if parts.scheme == "https":
        return True
    return parts.scheme == "http" and parts.hostname in _LOOPBACK


def redirect_host(uri: str) -> str:
    try:
        return urlsplit(uri).netloc
    except ValueError:
        return ""


def authorization_server_metadata(base: str) -> dict[str, Any]:
    root = base.rstrip("/")
    return {
        "issuer": root,
        "authorization_endpoint": f"{root}/oauth/authorize",
        "token_endpoint": f"{root}/oauth/token",
        "registration_endpoint": f"{root}/oauth/register",
        "revocation_endpoint": f"{root}/oauth/revoke",
        "response_types_supported": ["code"],
        "response_modes_supported": ["query"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "revocation_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": [SCOPE],
        "authorization_response_iss_parameter_supported": True,
        "service_documentation": "https://github.com/FutureMLS-Lab/loom/blob/main/docs/AGENT-GATEWAY.md",
    }


def protected_resource_metadata(base: str) -> dict[str, Any]:
    root = base.rstrip("/")
    return {
        "resource": mcp_resource(root),
        "authorization_servers": [root],
        "scopes_supported": [SCOPE],
        "bearer_methods_supported": ["header"],
        "resource_name": "Loom",
        "resource_documentation": "https://github.com/FutureMLS-Lab/loom/blob/main/docs/AGENT-GATEWAY.md",
    }


def _oauth_error(code: str, description: str) -> dict[str, str]:
    return {"error": code, "error_description": description}


class OAuthStore:
    """Clients, grants and tokens, persisted under ~/.loom/oauth/ (0600).

    Pending approvals and authorization codes live only in memory: both are
    minutes long, and a restart in between just means approving again. What
    is on disk is reloaded when the file changes, so ``loom oauth revoke`` from
    a shell takes effect on a running server.
    """

    def __init__(self, path: Path | None = None, *, clock: Callable[[], float] = time.time) -> None:
        self.path = path or oauth_state_path()
        self._clock = clock
        self._lock = threading.RLock()
        self._state: dict[str, Any] = self._empty()
        # Which file the state was read from. Every save replaces the file, so
        # the inode alone changes each time; mtime can repeat within a tick.
        self._sig: tuple[int, int, int] | None = None
        self._pending: dict[str, dict[str, Any]] = {}
        self._codes: dict[str, dict[str, Any]] = {}
        self._failures: dict[str, list[float]] = {}
        self._touched: dict[str, float] = {}
        self._load()

    # --- persistence ---------------------------------------------------------

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {"version": 1, "clients": {}, "grants": {}, "access": {}, "refresh": {}, "retired": {}}

    def _load(self) -> None:
        try:
            st = self.path.stat()
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        state = self._empty()
        if isinstance(raw, dict):
            for key in state:
                if key != "version" and isinstance(raw.get(key), dict):
                    state[key] = raw[key]
        self._state = state
        self._sig = (st.st_ino, st.st_mtime_ns, st.st_size)

    def _reload_if_changed(self) -> None:
        try:
            st = self.path.stat()
        except OSError:
            return
        if (st.st_ino, st.st_mtime_ns, st.st_size) != self._sig:
            self._load()

    def _save(self) -> None:
        self._prune()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(self._state, fh, indent=1, sort_keys=True)
        os.replace(tmp, self.path)
        try:
            st = self.path.stat()
            self._sig = (st.st_ino, st.st_mtime_ns, st.st_size)
        except OSError:
            self._sig = None

    def _prune(self) -> None:
        now = self._clock()
        for table in ("access", "refresh", "retired"):
            rows = self._state[table]
            for key in [k for k, v in rows.items() if float(v.get("expires_at", 0)) <= now]:
                del rows[key]
        live = {v.get("grant") for t in ("access", "refresh") for v in self._state[t].values()}
        grants = self._state["grants"]
        for gid in [g for g in grants if g not in live]:
            del grants[gid]

    # --- clients (RFC 7591) ----------------------------------------------------

    def register(self, meta: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
        """Register a public client; ``(registration, None)`` or ``(None, error)``."""
        if not isinstance(meta, dict):
            return None, _oauth_error("invalid_client_metadata", "expected a JSON object")
        uris = meta.get("redirect_uris")
        if not isinstance(uris, list) or not uris or len(uris) > 10:
            return None, _oauth_error("invalid_redirect_uri", "redirect_uris must list 1 to 10 URIs")
        for uri in uris:
            if not redirect_uri_ok(uri):
                return None, _oauth_error(
                    "invalid_redirect_uri",
                    f"not allowed: {str(uri)[:200]} (use https, or http://localhost for a native app)",
                )
        grant_types = meta.get("grant_types") or ["authorization_code", "refresh_token"]
        if not isinstance(grant_types, list) or "authorization_code" not in grant_types:
            return None, _oauth_error("invalid_client_metadata", "the authorization_code grant is required")
        response_types = meta.get("response_types") or ["code"]
        if response_types != ["code"]:
            return None, _oauth_error("invalid_client_metadata", "only response_type code is supported")
        name = " ".join(str(meta.get("client_name") or "").split())[:100] or "An unnamed app"
        now = int(self._clock())
        with self._lock:
            self._reload_if_changed()
            clients = self._state["clients"]
            if len(clients) >= MAX_CLIENTS:
                # Registration is open by design (approving is what is guarded),
                # so it is also bounded: the oldest clients holding no grant go.
                used = {g.get("client_id") for g in self._state["grants"].values()}
                idle = sorted(
                    (c for c in clients if c not in used),
                    key=lambda c: clients[c].get("created_at", 0),
                )
                for cid in idle[: len(clients) - MAX_CLIENTS + 1]:
                    del clients[cid]
                if len(clients) >= MAX_CLIENTS:
                    return None, _oauth_error("invalid_client_metadata", "too many registered clients")
            client_id = f"loomc_{secrets.token_urlsafe(16)}"
            grant_types = [g for g in grant_types if g in ("authorization_code", "refresh_token")]
            clients[client_id] = {
                "client_name": name,
                "redirect_uris": list(uris),
                "grant_types": grant_types,
                "created_at": now,
            }
            self._save()
        return {
            "client_id": client_id,
            "client_id_issued_at": now,
            "client_name": name,
            "redirect_uris": list(uris),
            "grant_types": grant_types,
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "scope": SCOPE,
        }, None

    def client(self, client_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._reload_if_changed()
            found = self._state["clients"].get(client_id or "")
            return dict(found) if found else None

    # --- authorization requests ----------------------------------------------

    def check_authorization(self, params: dict[str, str], base: str) -> dict[str, Any]:
        """Classify an authorization request.

        ``{"page": message}`` - the client or redirect URI cannot be trusted, so
        say so on the page and redirect nowhere;
        ``{"redirect": uri, "error": code, "description": ..., "state": ...}``;
        ``{"ok": request}``.
        """
        client_id = params.get("client_id", "")
        client = self.client(client_id)
        if client is None:
            return {"page": "This app is not registered with Loom. Connect it again from the app."}
        redirect = params.get("redirect_uri", "")
        registered = client.get("redirect_uris") or []
        if not redirect and len(registered) == 1:
            redirect = registered[0]
        if redirect not in registered:
            return {"page": "The app asked to send you somewhere it did not register. Nothing was approved."}
        state = params.get("state", "")

        def fail(code: str, description: str) -> dict[str, Any]:
            return {"redirect": redirect, "error": code, "description": description, "state": state}

        if params.get("response_type") != "code":
            return fail("unsupported_response_type", "only response_type=code is supported")
        challenge = params.get("code_challenge", "")
        if params.get("code_challenge_method") != "S256" or not _CHALLENGE_RE.match(challenge):
            return fail("invalid_request", "PKCE with code_challenge_method=S256 is required")
        resource = params.get("resource", "")
        if resource and not resource_matches(resource, base):
            return fail("invalid_target", "resource must be this server's MCP endpoint")
        return {
            "ok": {
                "client_id": client_id,
                "client_name": client.get("client_name") or "An unnamed app",
                "redirect_uri": redirect,
                "code_challenge": challenge,
                "state": state,
                "scope": SCOPE,
                "resource": mcp_resource(base),
            }
        }

    def remember(self, request: dict[str, Any]) -> str:
        """Hold an approved-to-show request server-side; the page posts its id."""
        now = self._clock()
        with self._lock:
            for key in [k for k, v in self._pending.items() if v["expires_at"] <= now]:
                del self._pending[key]
            while len(self._pending) >= MAX_PENDING:
                del self._pending[next(iter(self._pending))]
            request_id = secrets.token_urlsafe(24)
            self._pending[request_id] = {**request, "expires_at": now + PENDING_TTL}
            return request_id

    def pending(self, request_id: str) -> dict[str, Any] | None:
        with self._lock:
            found = self._pending.get(request_id or "")
            if found is None or found["expires_at"] <= self._clock():
                self._pending.pop(request_id or "", None)
                return None
            return dict(found)

    def forget(self, request_id: str) -> None:
        with self._lock:
            self._pending.pop(request_id or "", None)

    # --- the owner's approval -------------------------------------------------

    def approval_blocked(self, client_ip: str) -> bool:
        now = self._clock()
        with self._lock:
            for key in list(self._failures):
                self._failures[key] = [t for t in self._failures[key] if now - t < FAILURE_WINDOW]
                if not self._failures[key]:
                    del self._failures[key]
            total = sum(len(v) for v in self._failures.values())
            return total >= FAILURES_GLOBAL or len(self._failures.get(client_ip, [])) >= FAILURES_PER_IP

    def approval_failed(self, client_ip: str) -> None:
        with self._lock:
            self._failures.setdefault(client_ip, []).append(self._clock())

    def approval_succeeded(self, client_ip: str) -> None:
        with self._lock:
            self._failures.pop(client_ip, None)

    def issue_code(self, request: dict[str, Any]) -> str:
        now = self._clock()
        code = secrets.token_urlsafe(32)
        with self._lock:
            for key in [k for k, v in self._codes.items() if v["expires_at"] <= now]:
                del self._codes[key]
            self._codes[_hash(code)] = {
                "client_id": request["client_id"],
                "redirect_uri": request["redirect_uri"],
                "code_challenge": request["code_challenge"],
                "scope": request["scope"],
                "resource": request["resource"],
                "expires_at": now + CODE_TTL,
            }
        return code

    # --- tokens -----------------------------------------------------------------

    def _mint(self, grant_id: str, client_id: str, resource: str) -> dict[str, Any]:
        now = self._clock()
        access = f"loom_at_{secrets.token_urlsafe(32)}"
        refresh = f"loom_rt_{secrets.token_urlsafe(32)}"
        row = {"grant": grant_id, "client_id": client_id, "scope": SCOPE, "resource": resource}
        self._state["access"][_hash(access)] = {**row, "expires_at": now + ACCESS_TTL}
        self._state["refresh"][_hash(refresh)] = {**row, "expires_at": now + REFRESH_TTL}
        return {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": ACCESS_TTL,
            "refresh_token": refresh,
            "scope": SCOPE,
        }

    def exchange_code(
        self, form: dict[str, str], base: str
    ) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
        code = form.get("code", "")
        with self._lock:
            # Single use whatever happens next: a code that fails one check
            # must not survive to be tried again.
            entry = self._codes.pop(_hash(code), None) if code else None
            if entry is None or entry["expires_at"] <= self._clock():
                return None, _oauth_error("invalid_grant", "the authorization code is invalid or has expired")
            if form.get("client_id", "") != entry["client_id"]:
                return None, _oauth_error("invalid_grant", "the code was issued to another client")
            if form.get("redirect_uri", entry["redirect_uri"]) != entry["redirect_uri"]:
                return None, _oauth_error("invalid_grant", "redirect_uri does not match the authorization request")
            if not pkce_matches(form.get("code_verifier", ""), entry["code_challenge"]):
                return None, _oauth_error("invalid_grant", "code_verifier does not match the code_challenge")
            resource = form.get("resource", "")
            if resource and not resource_matches(resource, base):
                return None, _oauth_error("invalid_target", "resource must be this server's MCP endpoint")
            self._reload_if_changed()
            if entry["client_id"] not in self._state["clients"]:
                return None, _oauth_error("invalid_client", "the client was removed")
            grant_id = secrets.token_urlsafe(12)
            now = int(self._clock())
            self._state["grants"][grant_id] = {
                "client_id": entry["client_id"],
                "resource": entry["resource"],
                "approved_at": now,
                "last_used_at": now,
            }
            tokens = self._mint(grant_id, entry["client_id"], entry["resource"])
            self._save()
            return tokens, None

    def exchange_refresh(
        self, form: dict[str, str], base: str
    ) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
        token = form.get("refresh_token", "")
        key = _hash(token) if token else ""
        with self._lock:
            self._reload_if_changed()
            entry = self._state["refresh"].pop(key, None) if key else None
            if entry is None:
                retired = self._state["retired"].get(key) if key else None
                if retired:
                    # A refresh token used twice was copied: cut the whole
                    # grant off, the thief's copy and the real client's alike.
                    self._drop_grant(retired.get("grant", ""))
                    self._save()
                return None, _oauth_error("invalid_grant", "the refresh token is invalid or was revoked")
            self._state["retired"][key] = {"grant": entry["grant"], "expires_at": entry["expires_at"]}
            if entry["expires_at"] <= self._clock():
                self._save()
                return None, _oauth_error("invalid_grant", "the refresh token has expired")
            if form.get("client_id", "") != entry["client_id"]:
                self._save()
                return None, _oauth_error("invalid_grant", "the refresh token belongs to another client")
            resource = form.get("resource", "")
            if resource and not resource_matches(resource, base):
                self._save()
                return None, _oauth_error("invalid_target", "resource must be this server's MCP endpoint")
            if entry["grant"] not in self._state["grants"]:
                self._save()
                return None, _oauth_error("invalid_grant", "access was revoked")
            self._state["grants"][entry["grant"]]["last_used_at"] = int(self._clock())
            tokens = self._mint(entry["grant"], entry["client_id"], entry["resource"])
            self._save()
            return tokens, None

    def access_allows(self, token: str, base: str) -> bool:
        """Whether *token* is a live access token for this server's MCP resource."""
        if not token or not token.startswith("loom_at_"):
            return False
        key = _hash(token)
        now = self._clock()
        with self._lock:
            self._reload_if_changed()
            entry = self._state["access"].get(key)
            if entry is None or float(entry.get("expires_at", 0)) <= now:
                return False
            if entry.get("grant") not in self._state["grants"]:
                return False
            if not hmac.compare_digest(str(entry.get("resource", "")), mcp_resource(base)):
                return False
            grant = self._state["grants"][entry["grant"]]
            if now - self._touched.get(entry["grant"], 0) >= TOUCH_INTERVAL:
                self._touched[entry["grant"]] = now
                grant["last_used_at"] = int(now)
                self._save()
            return True

    def revoke(self, token: str) -> None:
        """RFC 7009: revoking either token of a grant ends the whole grant."""
        if not token:
            return
        key = _hash(token)
        with self._lock:
            self._reload_if_changed()
            entry = self._state["access"].get(key) or self._state["refresh"].get(key)
            if entry:
                self._drop_grant(entry.get("grant", ""))
                self._save()

    def _drop_grant(self, grant_id: str) -> bool:
        found = self._state["grants"].pop(grant_id, None) is not None
        for table in ("access", "refresh", "retired"):
            rows = self._state[table]
            for key in [k for k, v in rows.items() if v.get("grant") == grant_id]:
                del rows[key]
                found = True
        return found

    # --- the owner's view ----------------------------------------------------------

    def grants(self) -> list[dict[str, Any]]:
        """Connected apps: one row per approval, newest first."""
        with self._lock:
            self._reload_if_changed()
            self._prune()
            rows = []
            for gid, grant in self._state["grants"].items():
                client = self._state["clients"].get(grant.get("client_id", ""), {})
                uris = client.get("redirect_uris") or []
                rows.append(
                    {
                        "id": gid,
                        "client_id": grant.get("client_id", ""),
                        "client_name": client.get("client_name") or "A removed app",
                        "redirect_host": redirect_host(uris[0]) if uris else "",
                        "approved_at": grant.get("approved_at", 0),
                        "last_used_at": grant.get("last_used_at", 0),
                    }
                )
            rows.sort(key=lambda r: r["approved_at"], reverse=True)
            return rows

    def revoke_grant(self, grant_id: str) -> bool:
        with self._lock:
            self._reload_if_changed()
            found = self._drop_grant(grant_id)
            if found:
                self._save()
            return found

    def revoke_all(self) -> int:
        """Every grant and every registered client; apps must connect again."""
        with self._lock:
            self._reload_if_changed()
            count = len(self._state["grants"])
            self._state = self._empty()
            self._codes.clear()
            self._pending.clear()
            self._save()
            return count
