from contextlib import contextmanager
import hashlib
import hmac
import json

import pytest

from app.services.xhs import worker_client
from app.services.xhs.worker_client import WorkerError, XhsWorkerClient


TOKEN = "unit-test-worker-token-012345678901234567890123456789"


class FakeHTTPResponse:
    def __init__(self, *, status_code=200, payload=None, proof="", raw=None):
        self.status_code = status_code
        self.headers = {"X-Worker-Proof": proof}
        self.content = raw if raw is not None else json.dumps(payload or {}).encode()
        self.json_calls = 0
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.closed = True
        return False

    def json(self):
        self.json_calls += 1
        return json.loads(self.content)


class FakeHTTPSession:
    def __init__(self, token, *, proof_mode="valid", status_code=200, payload=None):
        self.token = token
        self.proof_mode = proof_mode
        self.status_code = status_code
        self.payload = payload or {"state": "READY", "owner_key": "acct_owner_0001"}
        self.calls = []
        self.responses = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        nonce = kwargs["headers"]["X-Request-Nonce"]
        proof = hmac.new(self.token.encode(), nonce.encode(), hashlib.sha256).hexdigest()
        if self.proof_mode == "invalid":
            proof = "0" * 64
        if self.proof_mode == "missing":
            proof = ""
        response = FakeHTTPResponse(
            status_code=self.status_code,
            payload=self.payload,
            proof=proof,
        )
        self.responses.append(response)
        return response


def make_client(tmp_path, monkeypatch, *, proof_mode="valid", status_code=200, payload=None):
    token_file = tmp_path / "worker-token"
    token_file.write_text(TOKEN)
    session = FakeHTTPSession(TOKEN, proof_mode=proof_mode, status_code=status_code, payload=payload)

    @contextmanager
    def direct_session():
        yield session

    monkeypatch.setattr(worker_client, "direct_session", direct_session)
    return XhsWorkerClient("http://xhs-worker:8091", token_file), session


def test_worker_client_accepts_matching_hmac_response_proof(tmp_path, monkeypatch):
    client, session = make_client(tmp_path, monkeypatch)

    result = client.request("POST", "/sessions/fake-session/page", {"cursor": "synthetic"})

    assert result == {"state": "READY", "owner_key": "acct_owner_0001"}
    method, url, kwargs = session.calls[0]
    assert method == "POST" and url == "http://xhs-worker:8091/sessions/fake-session/page"
    assert kwargs["allow_redirects"] is False
    assert kwargs["timeout"] == (5, 90)
    assert kwargs["stream"] is False
    assert kwargs["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert session.responses[0].closed is True


@pytest.mark.parametrize("proof_mode", ["invalid", "missing"])
def test_worker_client_rejects_missing_or_invalid_response_proof(tmp_path, monkeypatch, proof_mode):
    client, session = make_client(tmp_path, monkeypatch, proof_mode=proof_mode)

    with pytest.raises(WorkerError) as error:
        client.request("GET", "/status")

    assert error.value.category == "UNAUTHORIZED"
    assert str(error.value) == "UNAUTHORIZED"
    assert session.responses[0].json_calls == 0
    assert session.responses[0].closed is True


def test_worker_client_checks_proof_before_returning_worker_error_category(tmp_path, monkeypatch):
    client, session = make_client(
        tmp_path,
        monkeypatch,
        status_code=429,
        payload={"error_category": "RATE_LIMITED", "message": "private-response-body"},
    )

    with pytest.raises(WorkerError) as error:
        client.request("POST", "/sessions/fake/page")

    assert error.value.category == "RATE_LIMITED"
    assert "private-response-body" not in str(error.value)
    assert session.responses[0].closed is True


def test_worker_client_session_marks_trusted_failed_notes_for_local_resolution(tmp_path, monkeypatch):
    client, session = make_client(tmp_path, monkeypatch)
    session.payload = {"session_id": "trusted-session-001", "owner_key": "acct_owner_0001"}

    with client.session("acct_owner_0001", allowed_notes=("failed_note_a", "failed_note_b")) as session_id:
        assert session_id == "trusted-session-001"

    create_method, create_url, create_kwargs = session.calls[0]
    close_method, close_url, close_kwargs = session.calls[1]
    assert create_method == "POST" and create_url.endswith("/sessions")
    assert create_kwargs["json"] == {
        "owner_key": "acct_owner_0001", "allowed_notes": ["failed_note_a", "failed_note_b"]
    }
    assert close_method == "POST" and close_url.endswith("/sessions/trusted-session-001/close")
    assert close_kwargs["json"] is None
    assert all(response.closed for response in session.responses)


def test_worker_client_rejects_untrusted_worker_addresses_without_network(tmp_path):
    token_file = tmp_path / "worker-token"
    token_file.write_text(TOKEN)

    for base_url in (
        "https://xhs-worker:8091",
        "http://attacker.test:8091",
        "http://user@xhs-worker:8091",
        "http://xhs-worker:8091/private",
        "http://xhs-worker:8091/?token=secret",
    ):
        with pytest.raises(ValueError, match="Invalid internal XHS worker address"):
            XhsWorkerClient(base_url, token_file)
