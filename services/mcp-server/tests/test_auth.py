"""Tests for bearer token authentication middleware."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.responses import JSONResponse
from unittest.mock import patch

from tbc_mcp_server.auth import BearerTokenMiddleware


def _make_test_app(token: str) -> FastAPI:
    app = FastAPI()
    app.add_middleware(BearerTokenMiddleware)

    @app.get("/protected")
    async def protected():
        return {"ok": True}

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    return app


@pytest.fixture
def client():
    """Test client with a known bearer token."""
    with patch(
        "tbc_mcp_server.auth.settings"
    ) as mock_settings:
        from unittest.mock import MagicMock
        mock_token = MagicMock()
        mock_token.get_secret_value.return_value = "test-secret-token"
        mock_settings.mcp_bearer_token = mock_token

        app = _make_test_app("test-secret-token")
        with TestClient(app, raise_server_exceptions=True) as c:
            yield c


def test_missing_token_returns_401(client):
    """No Authorization header → 401."""
    resp = client.get("/protected")
    assert resp.status_code == 401


def test_wrong_token_returns_401(client):
    """Wrong token value → 401."""
    resp = client.get("/protected", headers={"Authorization": "Bearer wrong-token"})
    assert resp.status_code == 401


def test_correct_token_passes(client):
    """Correct bearer token → 200."""
    resp = client.get(
        "/protected", headers={"Authorization": "Bearer test-secret-token"}
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_health_no_auth_required(client):
    """/health endpoint is accessible without auth."""
    resp = client.get("/health")
    assert resp.status_code == 200
