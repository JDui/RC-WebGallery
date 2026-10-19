from __future__ import annotations

from contextlib import contextmanager
import hashlib
import hmac
import os
from pathlib import Path
import secrets
from urllib.parse import urlsplit

from app.services.xhs.models import XhsNote, identifier
from app.services.xhs.sync import ScanPage
from app.services.xhs.transport import direct_session
from app.services.xhs.browser_errors import BROWSER_ERRORS


ERRORS = {'UNAUTHORIZED', 'WORKER_ERROR', 'WORKER_BUSY', 'PROFILE_BUSY', 'BROWSER_UNAVAILABLE',
          'LOGIN_REQUIRED', 'LOGIN_START_FAILED', 'NEEDS_REAUTH', 'NEEDS_REBIND', 'LIKES_NOT_VISIBLE',
          'NEEDS_MANUAL_VERIFICATION', 'RATE_LIMITED', 'STRUCTURE_CHANGED', 'CURSOR_ERROR',
          'SESSION_EXPIRED', 'MEDIA_DOWNLOAD_FAILED', 'MEDIA_NEEDS_RESOLUTION', 'NOTE_NOT_OBSERVED', 'MEDIA_QUOTA_EXCEEDED'}
ERRORS.update({'UNLIKE_UNCONFIRMED', 'NOTE_LINK_UNAVAILABLE', 'NOTE_DETAIL_LOGIN_REQUIRED',
               'NOTE_DETAIL_UNAVAILABLE', 'VIDEO_STREAM_UNAVAILABLE'})
ERRORS.update(BROWSER_ERRORS)


class WorkerError(RuntimeError):
    def __init__(self, category, diagnostic=None):
        self.category = category if category in ERRORS else 'WORKER_ERROR'
        self.diagnostic = diagnostic if isinstance(diagnostic, dict) else {}
        super().__init__(self.category)


class XhsWorkerClient:
    def __init__(self, base_url: str | None = None, token_file: Path | None = None):
        self.base_url = base_url if base_url is not None else os.environ.get('XHS_WORKER_URL', '')
        self.token_file = token_file or Path(os.environ.get('XHS_WORKER_TOKEN_FILE', '/run/secrets/xhs_worker_token'))
        if self.base_url:
            parsed = urlsplit(self.base_url)
            if (parsed.scheme != 'http' or parsed.hostname not in {'xhs-worker', '127.0.0.1', 'localhost'}
                    or parsed.username or parsed.password or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
                raise ValueError('Invalid internal XHS worker address')
        self.base_url = self.base_url.rstrip('/')

    @property
    def configured(self):
        return bool(self.base_url and self.token_file.is_file())

    def _token(self):
        if not self.configured:
            raise WorkerError('WORKER_ERROR')
        try:
            token = self.token_file.read_text().strip()
        except OSError:
            raise WorkerError('UNAUTHORIZED') from None
        if len(token) < 32:
            raise WorkerError('UNAUTHORIZED')
        return token

    @contextmanager
    def response(self, method, path, payload=None, *, stream=False):
        token, nonce = self._token(), secrets.token_urlsafe(24)
        try:
            with direct_session() as session:
                with session.request(method, self.base_url + path, json=payload, stream=stream,
                                     headers={'Authorization': 'Bearer ' + token, 'X-Request-Nonce': nonce},
                                     timeout=(5, 360 if stream else 90), allow_redirects=False) as response:
                    proof = hmac.new(token.encode(), nonce.encode(), hashlib.sha256).hexdigest()
                    if not hmac.compare_digest(response.headers.get('X-Worker-Proof', ''), proof):
                        raise WorkerError('UNAUTHORIZED')
                    if response.status_code != 200:
                        try:
                            failure = response.json()
                            error = failure.get('error_category')
                        except Exception:
                            error = 'WORKER_ERROR'
                            failure = {}
                        raise WorkerError(error, failure.get('diagnostic'))
                    yield response
        except WorkerError:
            raise
        except Exception:
            raise WorkerError('WORKER_ERROR') from None

    def request(self, method, path, payload=None):
        with self.response(method, path, payload) as response:
            if len(response.content) > 4 * 1024 * 1024:
                raise WorkerError('STRUCTURE_CHANGED')
            result = response.json()
            if not isinstance(result, dict):
                raise WorkerError('STRUCTURE_CHANGED')
            return result

    def status(self, *, check=False):
        if not self.configured:
            return {'state': 'NOT_CONFIGURED', 'owner_key': None, 'network_mode': 'direct'}
        return self.request('POST' if check else 'GET', '/auth/check' if check else '/status')

    @contextmanager
    def session(self, owner_key, allowed_notes=()):
        result = self.request('POST', '/sessions', {'owner_key': identifier(owner_key, 8), 'allowed_notes': list(allowed_notes)})
        session_id = identifier(result.get('session_id'))
        if result.get('owner_key') != owner_key:
            raise WorkerError('NEEDS_REBIND')
        try:
            yield session_id
        finally:
            try:
                self.request('POST', f'/sessions/{session_id}/close')
            except WorkerError:
                pass

    def pages(self, session_id, cooperate=None):
        while True:
            if cooperate:
                cooperate()
            payload = self.request('POST', f'/sessions/{session_id}/page')
            notes = payload.get('notes')
            if not isinstance(notes, list) or len(notes) > 100:
                raise WorkerError('STRUCTURE_CHANGED')
            page = ScanPage(tuple(XhsNote.parse(note) for note in notes), payload.get('end_of_feed') is True,
                            payload.get('integrity_ok') is True)
            yield page
            if page.end_of_feed:
                return
