from __future__ import annotations

import asyncio
import json
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse
from app.services.xhs.worker_client import WorkerError


class BrowserGateway:
    COOKIE = 'xhs_browser_session'
    PATH = '/api/xhs/browser'

    def __init__(self):
        self.token = ''
        self.password = ''
        self.deadline = 0.0

    def begin(self, password: str, expires_in: int = 600):
        if not isinstance(password, str) or not password or len(password) > 128:
            raise ValueError('LOGIN_START_FAILED')
        self.token, self.password = secrets.token_urlsafe(32), password
        self.deadline = time.monotonic() + min(600, max(1, expires_in))

    def revoke(self):
        self.token = self.password = ''
        self.deadline = 0

    def valid(self, token):
        return bool(self.token and isinstance(token, str) and token.isascii() and token and time.monotonic() < self.deadline
                    and secrets.compare_digest(self.token, token))

    @staticmethod
    def same_origin(scope):
        origin = scope.headers.get('origin')
        if not origin:
            return False
        try:
            parsed = urlsplit(origin)
            scheme = 'https' if scope.url.scheme in {'https', 'wss'} else 'http'
            return parsed.scheme == scheme and parsed.netloc == scope.headers.get('host') and parsed.path in {'', '/'}
        except ValueError:
            return False

    def authorize(self, request):
        if (request.headers.get('sec-fetch-site') == 'cross-site'
                or (request.headers.get('origin') and not self.same_origin(request))
                or not self.valid(request.cookies.get(self.COOKIE))):
            raise HTTPException(403, '登录窗口已关闭，请重新启动手动登录')


def create_browser_router(gateway: BrowserGateway, get_worker):
    router = APIRouter(prefix=BrowserGateway.PATH)

    @router.get('/')
    async def viewer(request: Request):
        gateway.authorize(request)
        try:
            state = await run_in_threadpool(get_worker().status)
        except WorkerError:
            raise HTTPException(503, '浏览器正在准备，请稍后重试') from None
        if state.get('state') != 'LOGIN_IN_PROGRESS':
            raise HTTPException(410, '登录窗口已关闭')
        nonce = secrets.token_urlsafe(24)
        password = json.dumps(gateway.password).replace('<', '\\u003c')
        html = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>小红书扫码登录</title>
<style>html,body,#screen{{margin:0;width:100%;height:100%;overflow:hidden;background:#edf3ff}}
#message{{position:fixed;top:8px;left:8px;padding:6px 12px;background:#fff;border-radius:8px;font:14px sans-serif;z-index:2}}</style>
</head><body><div id="message">正在打开登录窗口…</div><div id="screen"></div>
<script type="module" nonce="{nonce}">
import RFB from './assets/core/rfb.js';
const url = new URL('./socket', location.href); url.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
const rfb = new RFB(document.getElementById('screen'), url.href, {{credentials:{{password:{password}}}}});
rfb.scaleViewport = true; rfb.resizeSession = false;
let closed = false;
const message = document.getElementById('message');
rfb.addEventListener('connect', () => {{message.hidden = true;}});
rfb.addEventListener('disconnect', () => {{closed = true; message.hidden = false; message.textContent = '登录窗口已关闭，可返回应用完成扫码或重新启动登录。';}});
rfb.addEventListener('securityfailure', () => {{message.hidden = false; message.textContent = '登录窗口已过期，请重新启动登录。';}});
window.addEventListener('pagehide', () => {{if (!closed) rfb.disconnect();}}, {{once:true}});
</script></body></html>'''
        return HTMLResponse(html, headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
            'Content-Security-Policy': f"default-src 'none'; script-src 'self' 'nonce-{nonce}'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data: blob:; frame-ancestors 'self'"})

    @router.get('/assets/{path:path}')
    async def assets(path: str, request: Request):
        gateway.authorize(request)
        base = Path('/usr/share/novnc').resolve()
        target = (base / path).resolve()
        if not target.is_relative_to(base) or path.split('/')[0] not in {'core', 'vendor'} or not target.is_file():
            raise HTTPException(404)
        return FileResponse(target, headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})

    @router.websocket('/socket')
    async def socket(websocket: WebSocket):
        token = websocket.cookies.get(gateway.COOKIE)
        if not gateway.same_origin(websocket) or not gateway.valid(token):
            await websocket.close(code=1008)
            return
        try:
            state = await run_in_threadpool(get_worker().status)
            if state.get('state') != 'LOGIN_IN_PROGRESS':
                await websocket.close(code=1008)
                return
            reader, writer = await asyncio.wait_for(asyncio.open_connection('127.0.0.1', 5900), timeout=5)
        except Exception:
            await websocket.close(code=1013)
            return
        protocols = websocket.headers.get('sec-websocket-protocol', '').split(',')
        await websocket.accept(subprotocol='binary' if 'binary' in [p.strip() for p in protocols] else None)

        async def from_browser():
            while gateway.valid(token):
                message = await websocket.receive()
                if message['type'] == 'websocket.disconnect':
                    return
                data = message.get('bytes')
                if data is None or len(data) > 1024 * 1024:
                    return
                writer.write(data)
                await writer.drain()

        async def to_browser():
            while gateway.valid(token):
                data = await reader.read(64 * 1024)
                if not data:
                    return
                await websocket.send_bytes(data)

        async def expires():
            while gateway.valid(token):
                await asyncio.sleep(0.5)

        tasks = [asyncio.create_task(call()) for call in (from_browser, to_browser, expires)]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
            try:
                await websocket.close()
            except (RuntimeError, WebSocketDisconnect):
                pass

    return router
