"""OAuth for the MCP endpoint, driven over real HTTP the way ChatGPT drives it:
discover, register, send the owner to the approval page, trade the code with
PKCE, call /mcp, refresh, revoke."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import stat
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from loom import oauth
from loom.openclaw import OpenClawClient
from loom.web import AgentActivityWatcher, ClaudeRegistry, make_handler
from loom.web_projects import WebProjectRegistry

OWNER = "owner-" + "x" * 42
CHATGPT_REDIRECT = "https://chatgpt.com/connector_platform_oauth_redirect"


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LOOM_HOME", str(tmp_path / "loom-home"))
    (tmp_path / "home").mkdir()
    return tmp_path


def _serve(tmp_path: Path, *, token: str = OWNER, public_url: str = ""):
    launch = tmp_path / "launch"
    launch.mkdir(exist_ok=True)
    registry = WebProjectRegistry(tmp_path / "registry.json")
    handler = make_handler(
        registry,
        launch,
        tmp_path / "no-skills.md",
        ClaudeRegistry(),
        OpenClawClient(),
        token,
        activity_watcher=AgentActivityWatcher(registry),
        public_url=public_url,
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


@pytest.fixture()
def loom(tmp_path: Path):
    httpd, base = _serve(tmp_path)
    yield base
    httpd.shutdown()
    httpd.server_close()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D401 - urllib hook
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _http(url: str, method: str = "GET", body=None, headers=None, form: bool = False):
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        if form:
            data = urllib.parse.urlencode(body).encode()
            hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
        else:
            data = json.dumps(body).encode()
            hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with _OPENER.open(req, timeout=10) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _register(base: str, redirect: str = CHATGPT_REDIRECT) -> str:
    status, _, body = _http(f"{base}/oauth/register", "POST", {"client_name": "ChatGPT", "redirect_uris": [redirect]})
    assert status == 201, body
    data = json.loads(body)
    assert data["token_endpoint_auth_method"] == "none"
    return data["client_id"]


def _authorize_params(client_id: str, challenge: str, **extra) -> dict[str, str]:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": CHATGPT_REDIRECT,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "st-123",
        "scope": "mcp",
    }
    params.update(extra)
    return params


def _approval_page(base: str, params: dict[str, str]) -> tuple[int, str]:
    status, _, body = _http(f"{base}/oauth/authorize?{urllib.parse.urlencode(params)}")
    return status, body.decode()


def _request_id(page: str) -> str:
    marker = 'name="request_id" value="'
    start = page.index(marker) + len(marker)
    return page[start : page.index('"', start)]


def _approve(base: str, request_id: str, token: str = OWNER, decision: str = "allow"):
    return _http(
        f"{base}/oauth/authorize",
        "POST",
        {"request_id": request_id, "owner_token": token, "decision": decision},
        form=True,
    )


def _location_params(headers: dict) -> dict[str, str]:
    location = headers.get("Location", "")
    query = urllib.parse.urlsplit(location).query
    return {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}


def _grant(base: str, *, resource: str | None = None) -> tuple[str, dict]:
    client_id = _register(base)
    verifier, challenge = _pkce()
    extra = {"resource": resource} if resource else {}
    status, page = _approval_page(base, _authorize_params(client_id, challenge, **extra))
    assert status == 200
    status, headers, _ = _approve(base, _request_id(page))
    assert status == 302
    code = _location_params(headers)["code"]
    status, _, body = _http(
        f"{base}/oauth/token",
        "POST",
        {"grant_type": "authorization_code", "code": code, "redirect_uri": CHATGPT_REDIRECT,
         "client_id": client_id, "code_verifier": verifier, **({"resource": resource} if resource else {})},
        form=True,
    )
    assert status == 200, body
    return client_id, json.loads(body)


def _mcp(base: str, token: str | None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}
    return _http(f"{base}/mcp", "POST", init, headers)


# --- discovery -----------------------------------------------------------------------


def test_discovery_says_what_chatgpt_checks(loom: str) -> None:
    for path in ("/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"):
        status, _, body = _http(f"{loom}{path}")
        prm = json.loads(body)
        assert status == 200
        assert prm["resource"] == f"{loom}/mcp"
        assert prm["authorization_servers"] == [loom]
    status, _, body = _http(f"{loom}/.well-known/oauth-authorization-server")
    meta = json.loads(body)
    assert status == 200
    assert meta["issuer"] == loom
    assert meta["code_challenge_methods_supported"] == ["S256"]
    assert meta["authorization_response_iss_parameter_supported"] is True
    assert meta["token_endpoint_auth_methods_supported"] == ["none"]
    for key in ("authorization_endpoint", "token_endpoint", "registration_endpoint", "revocation_endpoint"):
        assert meta[key].startswith(f"{loom}/oauth/")


def test_mcp_401_points_at_discovery(loom: str) -> None:
    status, headers, _ = _mcp(loom, None)
    assert status == 401
    challenge = headers["WWW-Authenticate"]
    assert challenge.startswith("Bearer ")
    assert f'resource_metadata="{loom}/.well-known/oauth-protected-resource/mcp"' in challenge
    status, headers, _ = _mcp(loom, "loom_at_forged")
    assert status == 401 and 'error="invalid_token"' in headers["WWW-Authenticate"]
    # The browser console keeps its Basic prompt everywhere else.
    status, headers, _ = _http(f"{loom}/api/tasks")
    assert status == 401 and headers["WWW-Authenticate"].startswith("Basic ")


def test_public_url_is_the_issuer_everywhere(tmp_path: Path) -> None:
    httpd, base = _serve(tmp_path, public_url="https://loom.example.com/")
    try:
        meta = json.loads(_http(f"{base}/.well-known/oauth-authorization-server")[2])
        assert meta["issuer"] == "https://loom.example.com"
        assert meta["token_endpoint"] == "https://loom.example.com/oauth/token"
        client_id = _register(base)
        _, challenge = _pkce()
        _, page = _approval_page(base, _authorize_params(client_id, challenge))
        _, headers, _ = _approve(base, _request_id(page))
        assert _location_params(headers)["iss"] == "https://loom.example.com"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_oauth_is_off_without_an_owner_token(tmp_path: Path) -> None:
    httpd, base = _serve(tmp_path, token="")
    try:
        assert _http(f"{base}/.well-known/oauth-authorization-server")[0] == 404
        assert _http(f"{base}/oauth/register", "POST", {"redirect_uris": [CHATGPT_REDIRECT]})[0] == 404
    finally:
        httpd.shutdown()
        httpd.server_close()


# --- the whole flow --------------------------------------------------------------------


def test_chatgpt_flow_end_to_end(loom: str) -> None:
    client_id = _register(loom)
    verifier, challenge = _pkce()
    status, page = _approval_page(loom, _authorize_params(client_id, challenge, resource=f"{loom}/mcp"))
    assert status == 200
    assert "ChatGPT" in page and "chatgpt.com" in page
    assert OWNER not in page
    request_id = _request_id(page)

    # A wrong token re-asks on the same page; nothing goes to the client.
    status, headers, body = _approve(loom, request_id, token="not-it")
    assert status == 401 and "Location" not in headers
    assert b"not this Loom" in body

    status, headers, _ = _approve(loom, request_id)
    assert status == 302
    back = _location_params(headers)
    assert headers["Location"].startswith(CHATGPT_REDIRECT + "?")
    assert back["state"] == "st-123" and back["iss"] == loom and back["code"]

    status, headers, body = _http(
        f"{loom}/oauth/token",
        "POST",
        {"grant_type": "authorization_code", "code": back["code"], "redirect_uri": CHATGPT_REDIRECT,
         "client_id": client_id, "code_verifier": verifier, "resource": f"{loom}/mcp"},
        form=True,
    )
    assert status == 200 and headers.get("Cache-Control") == "no-store"
    tokens = json.loads(body)
    assert tokens["token_type"] == "Bearer" and tokens["expires_in"] == oauth.ACCESS_TTL
    assert tokens["access_token"].startswith("loom_at_") and tokens["refresh_token"].startswith("loom_rt_")

    status, _, body = _mcp(loom, tokens["access_token"])
    assert status == 200 and json.loads(body)["result"]["serverInfo"]["name"] == "loom"
    # The token opens the gateway and nothing behind it.
    for path in ("/api/tasks", "/api/projects", "/"):
        assert _http(f"{loom}{path}", headers={"Authorization": f"Bearer {tokens['access_token']}"})[0] == 401
    # The approval page is a one-shot.
    assert _approve(loom, request_id)[0] == 400


def test_codes_are_single_use_and_pkce_bound(loom: str) -> None:
    client_id = _register(loom)
    verifier, challenge = _pkce()
    _, page = _approval_page(loom, _authorize_params(client_id, challenge))
    code = _location_params(_approve(loom, _request_id(page))[1])["code"]
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": CHATGPT_REDIRECT, "client_id": client_id}
    status, _, body = _http(f"{loom}/oauth/token", "POST", {**form, "code_verifier": "x" * 43}, form=True)
    assert status == 400 and json.loads(body)["error"] == "invalid_grant"
    # The failed attempt spent the code: the right verifier is too late now.
    status, _, body = _http(f"{loom}/oauth/token", "POST", {**form, "code_verifier": verifier}, form=True)
    assert status == 400 and json.loads(body)["error"] == "invalid_grant"


def test_refresh_rotates_and_a_replayed_refresh_kills_the_grant(loom: str) -> None:
    client_id, first = _grant(loom)
    form = {"grant_type": "refresh_token", "client_id": client_id}
    status, _, body = _http(f"{loom}/oauth/token", "POST", {**form, "refresh_token": first["refresh_token"]}, form=True)
    assert status == 200
    second = json.loads(body)
    assert second["refresh_token"] != first["refresh_token"]
    assert _mcp(loom, second["access_token"])[0] == 200
    # The old refresh token again: someone copied it. Everything in the grant dies.
    status, _, body = _http(f"{loom}/oauth/token", "POST", {**form, "refresh_token": first["refresh_token"]}, form=True)
    assert status == 400 and json.loads(body)["error"] == "invalid_grant"
    assert _mcp(loom, second["access_token"])[0] == 401
    status, _, _ = _http(f"{loom}/oauth/token", "POST", {**form, "refresh_token": second["refresh_token"]}, form=True)
    assert status == 400


def test_revocation_endpoint_ends_the_grant(loom: str) -> None:
    _, tokens = _grant(loom)
    assert _http(f"{loom}/oauth/revoke", "POST", {"token": tokens["refresh_token"]}, form=True)[0] == 200
    assert _mcp(loom, tokens["access_token"])[0] == 401
    # Unknown tokens get the same answer: revocation reveals nothing.
    assert _http(f"{loom}/oauth/revoke", "POST", {"token": "nope"}, form=True)[0] == 200


def test_cli_revocation_reaches_a_running_server(loom: str) -> None:
    _, tokens = _grant(loom)
    assert _mcp(loom, tokens["access_token"])[0] == 200
    shell = oauth.OAuthStore()  # what `loom oauth revoke` opens
    grants = shell.grants()
    assert len(grants) == 1 and grants[0]["client_name"] == "ChatGPT"
    assert grants[0]["redirect_host"] == "chatgpt.com"
    assert shell.revoke_grant(grants[0]["id"])
    assert _mcp(loom, tokens["access_token"])[0] == 401


def test_connected_apps_api_lists_and_revokes(loom: str) -> None:
    _, tokens = _grant(loom)
    owner = {"Authorization": f"Bearer {OWNER}"}
    status, _, body = _http(f"{loom}/api/oauth/grants", headers=owner)
    data = json.loads(body)
    assert status == 200 and data["enabled"] is True and data["mcp_url"] == f"{loom}/mcp"
    assert [g["client_name"] for g in data["grants"]] == ["ChatGPT"]
    # An OAuth token cannot manage grants, its own included.
    assert _http(f"{loom}/api/oauth/grants", headers={"Authorization": f"Bearer {tokens['access_token']}"})[0] == 401
    gid = data["grants"][0]["id"]
    assert _http(f"{loom}/api/oauth/grants/{gid}", "DELETE", headers=owner)[0] == 200
    assert _mcp(loom, tokens["access_token"])[0] == 401
    assert _http(f"{loom}/api/oauth/grants/{gid}", "DELETE", headers=owner)[0] == 404


# --- what gets refused -------------------------------------------------------------


def test_authorize_never_redirects_to_an_unregistered_place(loom: str) -> None:
    _, challenge = _pkce()
    status, page = _approval_page(loom, _authorize_params("loomc_unknown", challenge))
    assert status == 400 and "not registered" in page
    client_id = _register(loom)
    status, headers, _ = _http(
        f"{loom}/oauth/authorize?"
        + urllib.parse.urlencode(_authorize_params(client_id, challenge, redirect_uri="https://evil.example/cb"))
    )
    assert status == 400 and "Location" not in headers


def test_authorize_errors_go_back_with_iss(loom: str) -> None:
    client_id = _register(loom)
    _, challenge = _pkce()
    cases = [
        ({"code_challenge_method": "plain"}, "invalid_request"),
        ({"code_challenge": ""}, "invalid_request"),
        ({"response_type": "token"}, "unsupported_response_type"),
        ({"resource": "https://other.example/mcp"}, "invalid_target"),
    ]
    for extra, error in cases:
        status, headers, _ = _http(
            f"{loom}/oauth/authorize?" + urllib.parse.urlencode(_authorize_params(client_id, challenge, **extra))
        )
        back = _location_params(headers)
        assert status == 302 and back["error"] == error and back["iss"] == loom and back["state"] == "st-123"


def test_deny_goes_back_with_access_denied(loom: str) -> None:
    client_id = _register(loom)
    _, challenge = _pkce()
    _, page = _approval_page(loom, _authorize_params(client_id, challenge))
    status, headers, _ = _approve(loom, _request_id(page), token="", decision="deny")
    back = _location_params(headers)
    assert status == 302 and back["error"] == "access_denied" and back["iss"] == loom and "code" not in back


def test_guessing_the_owner_token_gets_shut_out(loom: str) -> None:
    client_id = _register(loom)
    _, challenge = _pkce()
    _, page = _approval_page(loom, _authorize_params(client_id, challenge))
    request_id = _request_id(page)
    for _ in range(oauth.FAILURES_PER_IP):
        assert _approve(loom, request_id, token="guess")[0] == 401
    # Even the right token waits out the pause.
    assert _approve(loom, request_id)[0] == 429


def test_registration_checks_redirect_uris(loom: str) -> None:
    def register(uris):
        return _http(f"{loom}/oauth/register", "POST", {"client_name": "x", "redirect_uris": uris})

    assert register(["http://evil.example/cb"])[0] == 400
    assert register(["https://ok.example/cb#frag"])[0] == 400
    assert register([])[0] == 400
    assert register(["http://127.0.0.1:33418/callback"])[0] == 201
    assert register(["https://claude.ai/api/mcp/auth_callback"])[0] == 201


def test_token_endpoint_refuses_other_grants(loom: str) -> None:
    status, _, body = _http(f"{loom}/oauth/token", "POST", {"grant_type": "password"}, form=True)
    assert status == 400 and json.loads(body)["error"] == "unsupported_grant_type"


def test_tokens_rest_on_disk_hashed_and_private(loom: str) -> None:
    _, tokens = _grant(loom)
    path = oauth.oauth_state_path()
    text = path.read_text()
    assert tokens["access_token"] not in text and tokens["refresh_token"] not in text
    assert OWNER not in text
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_expired_access_tokens_stop_working(tmp_path: Path) -> None:
    now = [1_000_000.0]
    store = oauth.OAuthStore(tmp_path / "state.json", clock=lambda: now[0])
    base = "https://loom.example.com"
    registration, _ = store.register({"client_name": "c", "redirect_uris": [CHATGPT_REDIRECT]})
    verifier, challenge = _pkce()
    outcome = store.check_authorization(
        _authorize_params(registration["client_id"], challenge), base
    )
    code = store.issue_code(outcome["ok"])
    tokens, error = store.exchange_code(
        {"code": code, "client_id": registration["client_id"], "redirect_uri": CHATGPT_REDIRECT,
         "code_verifier": verifier}, base
    )
    assert error is None and store.access_allows(tokens["access_token"], base)
    assert not store.access_allows(tokens["access_token"], "https://elsewhere.example")
    now[0] += oauth.ACCESS_TTL + 1
    assert not store.access_allows(tokens["access_token"], base)
