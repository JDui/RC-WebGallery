from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import os
import hashlib
import hmac
from pathlib import Path
import secrets

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask

from app.services.xhs.models import identifier, integer
from xhs_worker.runtime import BrowserFault, BrowserRuntime
from xhs_worker.media import MediaRejected


def create_app(runtime=None, token: str | None = None):
    @asynccontextmanager
    async def lifespan(app):
        secret = token or Path(os.environ.get('XHS_WORKER_TOKEN_FILE', '/run/secrets/xhs_worker_token')).read_text().strip()
        if len(secret) < 32:
            raise RuntimeError('WORKER_SECRET_NOT_CONFIGURED')
        app.state.token = secret
        app.state.runtime = runtime or BrowserRuntime(Path(os.environ.get('XHS_PRIVATE_ROOT', '/var/lib/xhs')), secret)
        task = asyncio.create_task(app.state.runtime.reap())
        try:
            yield
        finally:
            task.cancel()
            await app.state.runtime._close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    if runtime is not None and token is not None:
        app.state.runtime, app.state.token = runtime, token

    @app.middleware('http')
    async def authenticate(request: Request, call_next):
        if request.url.path == '/health':
            return JSONResponse({'service': 'xhs-worker', 'ok': True}, headers={'Cache-Control': 'no-store'})
        supplied = request.headers.get('Authorization', '')
        secret = getattr(app.state, 'token', '')
        if not secret or not secrets.compare_digest(supplied, 'Bearer ' + secret):
            return JSONResponse({'error_category': 'UNAUTHORIZED'}, status_code=401, headers={'Cache-Control': 'no-store'})
        try:
            response = await call_next(request)
        except Exception:
            response = JSONResponse({'error_category': 'WORKER_ERROR'}, status_code=503)
        response.headers['Cache-Control'] = 'no-store'
        nonce = request.headers.get('X-Request-Nonce', '')
        response.headers['X-Worker-Proof'] = hmac.new(secret.encode(), nonce.encode(), hashlib.sha256).hexdigest()
        return response

    async def perform(method, *args):
        try:
            return await method(*args)
        except BrowserFault as exc:
            return JSONResponse({'error_category': str(exc)}, status_code=409)
        except Exception:
            return JSONResponse({'error_category': 'WORKER_ERROR'}, status_code=503)

    @app.get('/status')
    async def status():
        engine = app.state.runtime
        return {'state': engine.state, 'owner_key': engine.owner_key, 'network_mode': 'direct'}

    @app.post('/auth/{action}')
    async def auth(action: str):
        methods = {'start': 'login_start', 'finish': 'login_finish', 'check': 'check', 'revoke': 'revoke'}
        if action not in methods:
            raise HTTPException(404)
        return await perform(getattr(app.state.runtime, methods[action]))

    @app.post('/sessions')
    async def session(request: Request):
        payload = await request.json()
        allowed = payload.get('allowed_notes', [])
        if not isinstance(allowed, list) or len(allowed) > 10000:
            raise HTTPException(400)
        allowed = [identifier(note_id) for note_id in allowed]
        return await perform(app.state.runtime.session_start, identifier(payload.get('owner_key'), 8), allowed)

    @app.post('/sessions/{session_id}/{action}')
    async def session_action(session_id: str, action: str, request: Request):
        identifier(session_id)
        if action == 'page':
            return await perform(app.state.runtime.next_page, session_id)
        if action == 'close':
            return await perform(app.state.runtime.session_close, session_id)
        if action == 'cancel':
            return await perform(app.state.runtime.cancel, session_id)
        if action == 'resolve':
            payload = await request.json()
            return await perform(app.state.runtime.resolve, session_id, identifier(payload.get('note_id')))
        raise HTTPException(404)

    @app.get('/sessions/{session_id}/assets/{note_id}/{ordinal}/{kind}')
    async def asset(session_id: str, note_id: str, ordinal: int, kind: str):
        identifier(session_id)
        identifier(note_id)
        integer(ordinal, 0, 99)
        if kind not in {'image', 'video', 'livephoto'}:
            raise HTTPException(400)
        try:
            path, meta = await app.state.runtime.asset(session_id, note_id, ordinal, kind)
            return FileResponse(path, media_type=meta['mime'], headers={
                'X-Asset-Sha256': meta['sha256'], 'X-Asset-Width': str(meta['width']),
                'X-Asset-Height': str(meta['height']), 'X-Asset-Variant': meta['variant'],
                'X-Asset-Duration': str(meta.get('duration', 0)),
            }, background=BackgroundTask(path.unlink, missing_ok=True))
        except MediaRejected as exc:
            return JSONResponse({'error_category': str(exc)}, status_code=409)
        except Exception:
            return JSONResponse({'error_category': 'MEDIA_DOWNLOAD_FAILED'}, status_code=409)

    return app


app = create_app()
