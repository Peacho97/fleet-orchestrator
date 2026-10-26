import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

import server


class _Resp:
    def __init__(self, data, status=200):
        self._d, self.status_code, self.text = data, status, ""
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self._d


def _patch_broker(monkeypatch, routes):
    """routes: path -> JSON payload; anything else raises a connection error."""
    async def fake_get(self, url, params=None, timeout=None):
        for path, data in routes.items():
            if url.endswith(path):
                return _Resp(data)
        raise httpx.ConnectError("down")
    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)


@pytest.fixture
def client(monkeypatch):
    server._rich_cache.update(ts=0.0, data=None)
    monkeypatch.setattr(server.gpu_snapshot, "snapshot", lambda: {"gpus": [], "ollama": []})
    return TestClient(server.app)


def test_sessions_broker_down(client, monkeypatch):
    _patch_broker(monkeypatch, {})
    r = client.get("/api/sessions")
    assert r.status_code == 200
    assert r.json() == {"up": False, "runs": [], "budget": None, "paused": None}


def test_sessions_rich_dict_payload_and_failure_not_cached(client, monkeypatch):
    _patch_broker(monkeypatch, {})
    assert client.get("/api/sessions/rich").json() == {"up": False, "runs": []}
    assert server._rich_cache["data"] is None
    _patch_broker(monkeypatch, {
        "/api/runs": {"runs": [{"id": "r1", "status": "running", "goal": "g"}]},
        "/api/runs/r1/tree": {"summary": {"target": "a.py", "worker_tier": "small", "spend": 0.5}}})
    body = client.get("/api/sessions/rich").json()
    assert body["up"] is True
    assert body["runs"] == [{"id": "r1", "status": "running", "goal": "g", "target": "a.py",
                             "tier": "small", "spend": 0.5, "recommendation": None,
                             "tests": None, "branch": None}]


def test_archive_501_when_runs_dir_unset(client, monkeypatch):
    monkeypatch.setattr(server, "SESSION_RUNS_DIR", None)
    r = client.post("/api/sessions/abc/archive")
    assert r.status_code == 501 and r.json()["ok"] is False


def test_overview_shape(client, monkeypatch):
    _patch_broker(monkeypatch, {"/api/runs": []})
    body = client.get("/api/overview").json()
    assert set(body) == {"gpu", "sessions", "config"}
    assert set(body["gpu"]) == {"broker_up", "cards", "host_ram", "ollama"}
    assert body["sessions"]["up"] is True
    assert set(body["config"]) == {"gpu_broker_url", "session_broker_url"}
