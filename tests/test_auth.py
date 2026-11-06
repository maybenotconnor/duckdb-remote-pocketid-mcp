"""OIDC wiring, against a fake PocketID discovery endpoint on localhost."""

from __future__ import annotations

import base64
import hashlib
import html
import json
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from cryptography.fernet import Fernet
from starlette.testclient import TestClient

import server
from conftest import gateway_env, patched_env


@pytest.fixture(scope="module")
def fake_pocketid():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    issuer = f"http://127.0.0.1:{httpd.server_port}"
    discovery = {
        "issuer": issuer,
        "authorization_endpoint": f"{issuer}/authorize",
        "token_endpoint": f"{issuer}/api/oidc/token",
        "userinfo_endpoint": f"{issuer}/api/oidc/userinfo",
        "jwks_uri": f"{issuer}/.well-known/jwks.json",
        "response_types_supported": ["code"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["RS256"],
        "scopes_supported": ["openid", "profile", "email", "groups"],
        "code_challenge_methods_supported": ["S256"],
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = {
                "/.well-known/openid-configuration": discovery,
                "/.well-known/jwks.json": {"keys": []},
            }.get(self.path)
            self.send_response(200 if body else 404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body or {}).encode())

        def log_message(self, *args):
            pass

    httpd.RequestHandlerClass = Handler
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield issuer
    httpd.shutdown()


@pytest.fixture(scope="module")
def http(duckdb_assets, sandbox, fake_pocketid, tmp_path_factory):
    env = gateway_env(
        duckdb_assets,
        sandbox,
        MCP_AUTH="oidc",
        POCKETID_ISSUER_URL=fake_pocketid + "/",  # trailing slash is tolerated
        POCKETID_CLIENT_ID="client-id",
        POCKETID_CLIENT_SECRET="client-secret",
        MCP_BASE_URL="https://duckdb-mcp.example.com",
        JWT_SIGNING_KEY="test-signing-key-" + "x" * 32,
        STORAGE_ENCRYPTION_KEY=Fernet.generate_key().decode(),
        OAUTH_STORAGE_DIR=str(tmp_path_factory.mktemp("oauth")),
    )
    with patched_env(env):
        mcp = server.build_server()
    with TestClient(mcp.http_app(path="/mcp"), base_url="https://duckdb-mcp.example.com") as c:
        yield c


def test_health_needs_no_auth(http):
    r = http.get("/health")
    assert r.status_code == 200 and r.text == "ok"


def test_mcp_needs_a_token(http):
    r = http.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Accept": "application/json, text/event-stream"},
    )
    assert r.status_code == 401
    assert "resource_metadata" in r.headers.get("www-authenticate", "")


def test_bogus_token_is_rejected(http):
    r = http.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Accept": "application/json, text/event-stream", "Authorization": "Bearer nope"},
    )
    assert r.status_code == 401


def test_oauth_metadata_points_at_the_gateway(http):
    r = http.get("/.well-known/oauth-authorization-server")
    assert r.status_code == 200
    meta = r.json()
    assert meta["issuer"].rstrip("/") == "https://duckdb-mcp.example.com"
    assert meta["authorization_endpoint"].startswith("https://duckdb-mcp.example.com/")


@pytest.mark.parametrize("missing", ["POCKETID_ISSUER_URL", "POCKETID_CLIENT_ID", "POCKETID_CLIENT_SECRET", "MCP_BASE_URL", "JWT_SIGNING_KEY", "STORAGE_ENCRYPTION_KEY"])
def test_missing_oidc_setting_stops_startup(duckdb_assets, sandbox, missing, monkeypatch):
    env = gateway_env(
        duckdb_assets, sandbox, MCP_AUTH="oidc",
        POCKETID_ISSUER_URL="https://id.example.com", POCKETID_CLIENT_ID="c", POCKETID_CLIENT_SECRET="s",
        MCP_BASE_URL="https://x.example.com", JWT_SIGNING_KEY="k" * 40,
        STORAGE_ENCRYPTION_KEY=Fernet.generate_key().decode(),
    )
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv(missing)
    with pytest.raises(SystemExit, match=missing):
        server.build_auth()


# ---------------------------------------------------------------- OAuth hardening

def browser(http):
    """A separate cookie jar on the same app, like a second browser."""
    return TestClient(http.app, base_url="https://duckdb-mcp.example.com", follow_redirects=False)


def register(http, redirect_uri):
    return http.post("/register", json={
        "redirect_uris": [redirect_uri], "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
    })


def start_sign_in(b, client_id, redirect_uri):
    """/authorize then the consent page. Returns (txn_id, csrf_token)."""
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    r = b.get("/authorize?" + urlencode({
        "response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": "s",
        "resource": "https://duckdb-mcp.example.com/mcp",
    }))
    assert r.status_code == 302, r.text
    consent = urlsplit(r.headers["location"])
    page = b.get(f"{consent.path}?{consent.query}")
    csrf = html.unescape(re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1))
    return parse_qs(consent.query)["txn_id"][0], csrf


@pytest.mark.parametrize("uri", [
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
    "http://localhost:53682/callback",
    "http://127.0.0.1:8080/cb",
    "http://[::1]:9000/callback",
])
def test_claude_and_loopback_redirects_can_register(http, uri):
    assert register(http, uri).status_code == 201


@pytest.mark.parametrize("uri", [
    "https://evil.example/steal",
    "https://claude.ai/somewhere-else",
    "http://localhost.evil.example/cb",
    "http://localhost@evil.example/cb",
])
def test_other_redirects_cannot_register(http, uri):
    r = register(http, uri)
    assert r.status_code == 400 and r.json()["error"] == "invalid_redirect_uri"


def test_resource_is_not_sent_to_pocketid(http):
    """PocketID v2 rejects a `resource` that is not a registered API."""
    uri = "https://claude.ai/api/mcp/auth_callback"
    b = browser(http)
    txn_id, csrf = start_sign_in(b, register(http, uri).json()["client_id"], uri)
    r = b.post("/consent", data={"txn_id": txn_id, "csrf_token": csrf, "action": "approve"})
    assert r.status_code == 302
    upstream = parse_qs(urlsplit(r.headers["location"]).query)
    assert "resource" not in upstream and upstream["state"] == [txn_id]


def test_consent_forged_from_another_browser_is_refused(http):
    """GHSA-rww4-4w9c-7733: the attacker's consent token submitted from the owner's browser."""
    uri = "http://localhost:1234/cb"
    txn_id, csrf = start_sign_in(browser(http), register(http, uri).json()["client_id"], uri)
    r = browser(http).post("/consent", data={"txn_id": txn_id, "csrf_token": csrf, "action": "approve"})
    assert r.status_code == 403


def test_sign_in_link_forwarded_to_another_browser_is_refused(http):
    """The PocketID callback must reach the browser that approved consent."""
    uri = "http://localhost:1234/cb"
    attacker = browser(http)
    txn_id, csrf = start_sign_in(attacker, register(http, uri).json()["client_id"], uri)
    assert attacker.post("/consent", data={"txn_id": txn_id, "csrf_token": csrf, "action": "approve"}).status_code == 302
    r = browser(http).get(f"/auth/callback?code=pocketid-code&state={txn_id}")
    assert r.status_code == 403
