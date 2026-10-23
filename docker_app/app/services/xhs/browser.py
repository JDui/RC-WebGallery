from __future__ import annotations

import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse

from app.services.xhs.browser_errors import browser_error_text
from app.services.xhs.routes import read_object
from app.services.xhs.worker_client import WorkerError


class BrowserGateway:
    COOKIE = 'xhs_browser_session'
    PATH = '/api/xhs/browser'

    def __init__(self):
        self.token = ''
        self.deadline = 0.0

    def begin(self, password: str, expires_in: int = 600):
        # Keep the start RPC compatible; only the HttpOnly cookie grants access.
        if not isinstance(password, str) or not password or len(password) > 128:
            raise ValueError('LOGIN_START_FAILED')
        self.token = secrets.token_urlsafe(32)
        self.deadline = time.monotonic() + min(600, max(1, expires_in))

    def revoke(self):
        self.token = ''
        self.deadline = 0

    def valid(self, token):
        return bool(self.token and isinstance(token, str) and token.isascii()
                    and time.monotonic() < self.deadline and secrets.compare_digest(self.token, token))

    @staticmethod
    def same_origin(scope):
        try:
            parsed = urlsplit(scope.headers.get('origin', ''))
            scheme = 'https' if scope.url.scheme in {'https', 'wss'} else 'http'
            return parsed.scheme == scheme and parsed.netloc == scope.headers.get('host') and parsed.path in {'', '/'}
        except ValueError:
            return False

    def authorize(self, request):
        if (request.headers.get('sec-fetch-site') == 'cross-site'
                or (request.headers.get('origin') and not self.same_origin(request)
                    and request.headers.get('sec-fetch-site') != 'same-origin')
                or not self.valid(request.cookies.get(self.COOKIE))):
            raise HTTPException(403, '登录窗口已关闭，请重新启动扫码登录')


def create_browser_router(gateway: BrowserGateway, get_worker):
    router = APIRouter(prefix=BrowserGateway.PATH)

    @router.get('/')
    async def viewer(request: Request):
        gateway.authorize(request)
        nonce = secrets.token_urlsafe(24)
        html = Path(__file__).with_name('login_viewer.html').read_text(encoding='utf-8').replace('__NONCE__', nonce)
        return HTMLResponse(html, headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
            'Content-Security-Policy': f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'unsafe-inline'; connect-src 'self'; img-src data:; frame-ancestors 'self'"})

    @router.post('/{action}')
    async def relay(action: str, request: Request):
        gateway.authorize(request)
        if action not in {'frame', 'input'}:
            raise HTTPException(404)
        payload = await read_object(request, 16384)
        try:
            # Use worker RPC for both embedded and external workers.
            result = await run_in_threadpool(get_worker().request, 'POST', '/auth/' + action, payload)
        except WorkerError as exc:
            status = 410 if exc.category == 'LOGIN_REQUIRED' else 409
            raise HTTPException(status, browser_error_text(exc.category)) from None
        gateway.authorize(request)
        return JSONResponse(result, headers={'Cache-Control': 'no-store'})

    return router
