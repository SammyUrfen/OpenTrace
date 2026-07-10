"""Local-only request guard + LLM key/base_url binding + the per-launch API
bearer token.

The backend is a localhost service with no auth by default, so three
invariants matter: requests carrying a real web origin or a non-local Host are
rejected; a stored LLM API key is never sent to a base_url set after the key
was stored; and once Electron configures a bearer token (OPENTRACE_API_TOKEN),
every request but /health and CORS preflight must carry it.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import config, secrets
from app import main
from app.main import app


@pytest.fixture()
def client(ot_home):
    # base_url picks the Host header; the guard only answers for local hosts.
    return TestClient(app, base_url="http://localhost")


# --- origin / host guard ------------------------------------------------------


def test_no_origin_passes(client):
    assert client.get("/health").status_code == 200


def test_null_origin_passes(client):
    r = client.get("/health", headers={"Origin": "null"})
    assert r.status_code == 200


def test_dev_server_origin_passes_with_cors(client):
    r = client.get("/health", headers={"Origin": "http://localhost:5173"})
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == "http://localhost:5173"


def test_web_origin_rejected(client):
    r = client.get("/health", headers={"Origin": "https://evil.example.com"})
    assert r.status_code == 403


def test_web_origin_rejected_on_writes(client):
    r = client.put(
        "/config/llm",
        json={"base_url": "https://evil.example.com/v1"},
        headers={"Origin": "https://evil.example.com"},
    )
    assert r.status_code == 403


def test_dns_rebinding_host_rejected(client):
    r = client.get("/health", headers={"Host": "evil.example.com"})
    assert r.status_code == 403


def test_local_host_with_port_passes(client):
    r = client.get("/health", headers={"Host": "127.0.0.1:8000"})
    assert r.status_code == 200


# --- LLM key is bound to the base_url it was stored for ------------------------


def _secret_name() -> str:
    return config.load().llm.api_key_secret_name


def test_base_url_change_without_key_drops_secret(client):
    r = client.put("/config/llm", json={
        "base_url": "https://api.example.com/v1", "model": "m", "api_key": "sek",
    })
    assert r.status_code == 200 and r.json()["has_key"] is True

    r = client.put("/config/llm", json={"base_url": "https://other.example.com/v1"})
    assert r.status_code == 200
    assert r.json()["has_key"] is False
    assert secrets.get_secret(_secret_name()) is None


def test_base_url_change_with_new_key_stores_it(client):
    client.put("/config/llm", json={
        "base_url": "https://api.example.com/v1", "model": "m", "api_key": "old",
    })
    r = client.put("/config/llm", json={
        "base_url": "https://new.example.com/v1", "api_key": "new",
    })
    assert r.json()["has_key"] is True
    assert secrets.get_secret(_secret_name()) == "new"


def test_unchanged_base_url_keeps_key(client):
    client.put("/config/llm", json={
        "base_url": "https://api.example.com/v1", "model": "m", "api_key": "sek",
    })
    r = client.put("/config/llm", json={
        "base_url": "https://api.example.com/v1", "model": "m2",
    })
    assert r.json()["has_key"] is True
    r = client.put("/config/llm", json={"continuous_summaries": True})
    assert r.json()["has_key"] is True


# --- per-launch API bearer token ---------------------------------------------
# API_TOKEN is a module global read once at import (from OPENTRACE_API_TOKEN);
# monkeypatching it here is equivalent to Electron having set the env var
# before the backend started, without needing a subprocess per test.


def test_no_token_configured_is_open(client, monkeypatch):
    monkeypatch.setattr(main, "API_TOKEN", "")
    assert client.get("/info").status_code == 200


def test_token_configured_rejects_missing_auth(client, monkeypatch):
    monkeypatch.setattr(main, "API_TOKEN", "secret123")
    assert client.get("/info").status_code == 401


def test_token_configured_rejects_wrong_token(client, monkeypatch):
    monkeypatch.setattr(main, "API_TOKEN", "secret123")
    r = client.get("/info", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_token_configured_accepts_correct_bearer_header(client, monkeypatch):
    monkeypatch.setattr(main, "API_TOKEN", "secret123")
    r = client.get("/info", headers={"Authorization": "Bearer secret123"})
    assert r.status_code == 200


def test_token_configured_accepts_query_param_for_sse(client, monkeypatch):
    # EventSource can't set headers, so /stream-style endpoints also accept
    # ?token= — verified against a plain endpoint here (behavior is identical;
    # the SSE route itself is exercised in test_streaming.py-style live tests).
    monkeypatch.setattr(main, "API_TOKEN", "secret123")
    r = client.get("/info?token=secret123")
    assert r.status_code == 200


def test_token_configured_health_is_exempt(client, monkeypatch):
    monkeypatch.setattr(main, "API_TOKEN", "secret123")
    assert client.get("/health").status_code == 200


def test_token_configured_options_preflight_passes_through(client, monkeypatch):
    monkeypatch.setattr(main, "API_TOKEN", "secret123")
    r = client.options(
        "/info",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert r.status_code in (200, 204)
