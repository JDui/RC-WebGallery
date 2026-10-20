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

    @router.get('/pending')
    def pending():
        return HTMLResponse('''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>正在启动 Chromium</title>
<body style="font:16px sans-serif;background:#edf3ff;padding:32px"><h2>正在启动 Chromium 登录窗口…</h2>
<p>浏览器准备好后将自动显示。若启动失败，请回到应用查看提示并重试。</p></body></html>''',
            headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
                     'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'self'"})

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
<style>html,body{{margin:0;width:100%;height:100%;overflow:hidden;background:#edf3ff;font:14px sans-serif}}
header{{min-height:48px;box-sizing:border-box;display:flex;align-items:center;gap:12px;padding:8px 12px;background:#fff;flex-wrap:wrap}}
body{{display:flex;flex-direction:column}}#screen{{flex:1;min-height:0;overflow:hidden}}
#message{{flex:1;min-width:180px}}button{{padding:6px 12px;cursor:pointer}}</style>
</head><body><header><strong>Chromium · 小红书登录</strong><span id="message" role="status">正在加载登录组件…</span>
<button id="retry" type="button">重新连接</button></header><div id="screen"></div>
<script nonce="{nonce}">
let rfb = null, connected = false, stopped = false;
const message = document.getElementById('message');
const scriptNonce = document.currentScript.nonce;
document.getElementById('retry').addEventListener('click', () => location.reload());
function fail(text) {{
  if (stopped) return;
  stopped = true; clearTimeout(timer); message.textContent = text;
  if (rfb) rfb.disconnect();
}}
const timer = setTimeout(() => fail('连接超时。请重新连接；仍失败时检查反向代理的 WebSocket 转发，或回到应用重新启动登录。'), 20000);
(async () => {{
  try {{
    // Load one classic script instead of dozens of CORS-mode module requests.
    // Some NAS remote-access proxies fail or rewrite those module responses.
    await new Promise((resolve, reject) => {{
      const script = document.createElement('script');
      script.src = './client.js'; script.nonce = scriptNonce;
      script.onload = resolve; script.onerror = () => reject(new Error('CLIENT_LOAD_FAILED'));
      document.head.appendChild(script);
    }});
    if (stopped) return;
    const RFB = window.NoVNC && window.NoVNC.default;
    if (typeof RFB !== 'function') throw new Error('CLIENT_INVALID');
    message.textContent = '正在连接容器中的 Chromium…';
    const url = new URL('./socket', location.href); url.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    rfb = new RFB(document.getElementById('screen'), url.href, {{credentials:{{password:{password}}}}});
    rfb.scaleViewport = true; rfb.resizeSession = false;
    rfb.addEventListener('connect', () => {{
      if (stopped) return;
      connected = true; clearTimeout(timer);
      message.textContent = '扫码后请回到应用点击“完成扫码”。';
    }});
    rfb.addEventListener('disconnect', () => fail(connected
      ? '登录连接已断开。可重新连接，或回到应用完成扫码。'
      : '无法连接登录画面。请检查 WebSocket 转发，或回到应用重新启动登录。'));
    rfb.addEventListener('securityfailure', () => fail('登录窗口凭证已失效，请回到应用重新启动登录。'));
  }} catch (error) {{
    if (stopped) return;
    if (error.message === 'CLIENT_LOAD_FAILED') {{
      let detail = '请求被浏览器或代理拦截';
      const controller = new AbortController();
      const probeTimer = setTimeout(() => controller.abort(), 5000);
      try {{
        const response = await fetch('./client.js', {{cache:'no-store', signal:controller.signal}});
        detail = response.status === 200
          ? '脚本响应类型异常或脚本执行被拦截'
          : `HTTP ${{response.status}}`;
        if (response.body) await response.body.cancel();
      }} catch (_) {{}} finally {{clearTimeout(probeTimer);}}
      fail(`登录组件加载失败（${{detail}}）。请重新连接；若提示 HTTP 503，请更新完整镜像。`);
    }} else {{
      fail('登录画面初始化失败。请重新连接，并检查浏览器控制台中的具体错误。');
    }}
    console.error('XHS login viewer failed', error);
  }}
}})();
window.addEventListener('pagehide', () => {{stopped = true; clearTimeout(timer); if (rfb) rfb.disconnect();}}, {{once:true}});
</script></body></html>'''
        return HTMLResponse(html, headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
            'Content-Security-Policy': f"default-src 'none'; script-src 'self' 'nonce-{nonce}'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data: blob:; frame-ancestors 'self'"})

    @router.get('/client.js')
    async def client(request: Request):
        gateway.authorize(request)
        target = Path('/usr/share/novnc/rfb.bundle.js')
        if not target.is_file():
            raise HTTPException(503, '登录组件不完整，请更新完整镜像')
        return FileResponse(target, media_type='application/javascript', headers={
            'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})

    @router.get('/assets/{path:path}')
    async def assets(path: str, request: Request):
        gateway.authorize(request)
        base = Path('/usr/share/novnc').resolve()
        target = (base / path).resolve()
        if not target.is_relative_to(base) or path.split('/')[0] not in {'core', 'vendor'} or not target.is_file():
            raise HTTPException(404)
        return FileResponse(target, media_type='application/javascript' if target.suffix == '.js' else None,
                            headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})

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
