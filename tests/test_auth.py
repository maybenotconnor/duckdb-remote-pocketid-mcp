"""OIDC wiring, against a fake PocketID discovery endpoint on localhost."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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
