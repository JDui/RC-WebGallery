import asyncio
import hashlib
import hmac
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from xhs_worker.main import create_app


TOKEN = "x" * 48
OWNER = "acct_owner_0001"
SESSION_ID = "session_smoke_001"


class FakeRuntime:
    def __init__(self, asset_path):
        self.state = "READY"
        self.owner_key = OWNER
        self.asset_path = Path(asset_path)
        self.calls = []
        self.closed = False

    async def reap(self):
        await asyncio.Event().wait()

    async def _close(self):
        self.closed = True

    async def login_start(self):
        self.calls.append(("login_start",))
        return {"state": "LOGIN_IN_PROGRESS"}

    async def login_finish(self):
        self.calls.append(("login_finish",))
        return {"state": "READY", "owner_key": OWNER}

    async def check(self):
        self.calls.append(("check",))
        return {"state": "READY", "owner_key": OWNER}

    async def revoke(self):
        self.calls.append(("revoke",))
        return {"state": "LOGIN_REQUIRED"}

    async def session_start(self, owner_key, allowed_notes):
        self.calls.append(("session_start", owner_key, list(allowed_notes)))
        return {"session_id": SESSION_ID, "owner_key": owner_key}

    async def next_page(self, session_id):
        self.calls.append(("next_page", session_id))
        return {"notes": [], "end_of_feed": False, "integrity_ok": True}

    async def session_close(self, session_id):
        self.calls.append(("session_close", session_id))
        return {"state": "READY"}

    async def cancel(self, session_id):
        self.calls.append(("cancel", session_id))
        return {"state": "CANCELLED"}

    async def resolve(self, session_id, note_id):
        self.calls.append(("resolve", session_id, note_id))
        return {"note_id": note_id, "assets": []}

    async def asset(self, session_id, note_id, ordinal, kind):
        self.calls.append(("asset", session_id, note_id, ordinal, kind))
        return self.asset_path, {
            "mime": "image/png", "sha256": hashlib.sha256(self.asset_path.read_bytes()).hexdigest(),
            "width": 2, "height": 3, "variant": "visible_web_media",
        }


@pytest.fixture
def worker_client(tmp_path):
    payload = tmp_path / "asset.png"
    payload.write_bytes(b"synthetic-image-content")
    runtime = FakeRuntime(payload)
    app = create_app(runtime=runtime, token=TOKEN)
    with TestClient(app) as client:
        yield SimpleNamespace(app=app, client=client, runtime=runtime, asset=payload)


def request_headers(nonce="test-request-nonce"):
    return {"Authorization": f"Bearer {TOKEN}", "X-Request-Nonce": nonce}


def expected_proof(nonce):
    return hmac.new(TOKEN.encode(), nonce.encode(), hashlib.sha256).hexdigest()


def test_worker_api_requires_bearer_token_and_proves_authenticated_response(worker_client):
    client = worker_client.client

    missing = client.get("/status")
    invalid = client.get("/status", headers={"Authorization": "Bearer wrong"})
    nonce = "nonce-for-proof"
    valid = client.get("/status", headers=request_headers(nonce))

    assert missing.status_code == invalid.status_code == 401
    assert missing.json() == invalid.json() == {"error_category": "UNAUTHORIZED"}
    assert missing.headers.get("cache-control") == "no-store"
    assert invalid.headers.get("cache-control") == "no-store"
    assert valid.status_code == 200
    assert valid.json() == {"state": "READY", "owner_key": OWNER, "network_mode": "direct"}
    assert valid.headers["cache-control"] == "no-store"
    assert valid.headers["x-worker-proof"] == expected_proof(nonce)
    assert worker_client.runtime.calls == []


def test_worker_api_allows_only_explicit_private_routes(worker_client):
    client = worker_client.client
    registered = {route.path for route in worker_client.app.routes if hasattr(route, "methods")}

    assert registered == {
        "/status", "/auth/{action}", "/sessions",
        "/sessions/{session_id}/{action}",
        "/sessions/{session_id}/assets/{note_id}/{ordinal}/{kind}",
    }
    health = client.get("/health")
    assert health.status_code == 200
    assert health.headers["cache-control"] == "no-store"
    headers = request_headers()
    assert client.get("/docs", headers=headers).status_code == 404
    assert client.get("/redoc", headers=headers).status_code == 404
    assert client.get("/openapi.json", headers=headers).status_code == 404
    assert client.post("/auth/unknown", headers=headers).status_code == 404
    assert client.post(f"/sessions/{SESSION_ID}/delete", headers=headers).status_code == 404
    assert client.get("/debug", headers=headers).status_code == 404


def test_authenticated_worker_exception_is_proved_no_store_and_sanitized(worker_client):
    async def fail_login():
        raise RuntimeError("synthetic-private-profile and worker-token-secret")

    worker_client.runtime.login_start = fail_login
    nonce = "exception-proof-nonce"

    response = worker_client.client.post("/auth/start", headers=request_headers(nonce))

    assert response.status_code == 503
    assert response.json() == {"error_category": "WORKER_ERROR"}
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-worker-proof"] == expected_proof(nonce)
    assert "synthetic-private-profile" not in response.text
    assert "worker-token-secret" not in response.text


def test_worker_api_validates_session_allowlist_and_action_routes(worker_client):
    client = worker_client.client
    headers = request_headers("session-nonce")

    created = client.post("/sessions", headers=headers, json={"owner_key": OWNER, "allowed_notes": ["note_1"]})
    assert created.status_code == 200
    assert created.json() == {"session_id": SESSION_ID, "owner_key": OWNER}
    assert created.headers["cache-control"] == "no-store"
    assert created.headers["x-worker-proof"] == expected_proof("session-nonce")
    assert worker_client.runtime.calls[-1] == ("session_start", OWNER, ["note_1"])

    page = client.post(f"/sessions/{SESSION_ID}/page", headers=request_headers("page-nonce"))
    assert page.status_code == 200
    assert page.json() == {"notes": [], "end_of_feed": False, "integrity_ok": True}
    assert page.headers["cache-control"] == "no-store"
    assert page.headers["x-worker-proof"] == expected_proof("page-nonce")

    cancelled = client.post(f"/sessions/{SESSION_ID}/cancel", headers=request_headers("cancel-nonce"))
    assert cancelled.status_code == 200
    assert cancelled.json() == {"state": "CANCELLED"}
    assert cancelled.headers["cache-control"] == "no-store"
    assert cancelled.headers["x-worker-proof"] == expected_proof("cancel-nonce")
    assert worker_client.runtime.calls[-1] == ("cancel", SESSION_ID)

    oversized = client.post(
        "/sessions", headers=request_headers(), json={"owner_key": OWNER, "allowed_notes": ["note_1"] * 10001}
    )
    assert oversized.status_code == 400


def test_worker_asset_route_returns_verified_metadata_and_no_store(worker_client):
    client = worker_client.client
    nonce = "asset-route-nonce"

    response = client.get(
        f"/sessions/{SESSION_ID}/assets/note_1/0/image",
        headers=request_headers(nonce),
    )

    assert response.status_code == 200
    assert response.content == b"synthetic-image-content"
    assert response.headers["content-type"] == "image/png"
    assert response.headers["x-asset-sha256"] == hashlib.sha256(response.content).hexdigest()
    assert response.headers["x-asset-width"] == "2"
    assert response.headers["x-asset-height"] == "3"
    assert response.headers["x-asset-variant"] == "visible_web_media"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-worker-proof"] == expected_proof(nonce)
    assert worker_client.asset.exists() is False
    assert worker_client.runtime.calls[-1] == ("asset", SESSION_ID, "note_1", 0, "image")
