from __future__ import annotations

import json
import sqlite3
from typing import Callable

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from app.services.xhs.models import MAX_MANIFEST_BYTES, XhsValidationError
from app.services.xhs.sync import XhsConflict, XhsSyncService
from app.services.xhs.worker_client import WorkerError
from app.services.xhs.browser_errors import browser_error_text


async def read_body(request: Request, maximum: int = MAX_MANIFEST_BYTES) -> bytes:
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(415, "请使用 application/json 上传清单")
    # 单人自部署场景不做跨站拦截：反代后容器收到的 Host 与浏览器 Origin 常不一致，会误判成跨站。
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > maximum:
            raise HTTPException(413, "请求体过大")
        raw.extend(chunk)
    return bytes(raw)


async def read_object(request: Request, maximum: int = 4096) -> dict:
    try:
        value = json.loads(await read_body(request, maximum))
    except (ValueError, UnicodeError, RecursionError):
        raise HTTPException(400, "JSON 无效") from None
    if not isinstance(value, dict):
        raise HTTPException(400, "JSON 必须是对象")
    return value


def create_router(get_service: Callable[[], XhsSyncService], on_schedule_changed: Callable[[], None] | None = None,
                  get_manager=None, browser_gateway=None) -> APIRouter:
    router = APIRouter(prefix="/api/xhs")

    async def execute(action, *args):
        try:
            result = await run_in_threadpool(action, *args)
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        except XhsValidationError as exc:
            raise HTTPException(400, str(exc)) from None
        except XhsConflict as exc:
            raise HTTPException(409, str(exc)) from None
        except WorkerError as exc:
            raise HTTPException(409, browser_error_text(exc.category)) from None
        except (sqlite3.Error, OSError):
            raise HTTPException(503, "小红书数据暂时无法写入，请稍后重试") from None

    @router.get("/status")
    async def status():
        return await execute(get_manager().status if get_manager else get_service().status)

    @router.post('/login/{action}')
    async def login(action: str, request: Request):
        if not get_manager:
            raise HTTPException(409, 'Worker 未配置')
        result = await execute(get_manager().login, action, await read_object(request))
        if browser_gateway:
            if action == 'start':
                payload = json.loads(result.body)
                browser_gateway.begin(payload.pop('viewer_password'), int(payload.get('expires_in', 600)))
                payload['viewer_url'] = browser_gateway.PATH + '/'
                result = JSONResponse(payload, headers={'Cache-Control': 'no-store'})
                result.set_cookie(browser_gateway.COOKIE, browser_gateway.token, max_age=600,
                                  httponly=True, samesite='strict', secure=request.url.scheme == 'https',
                                  path=browser_gateway.PATH)
            elif action in {'finish', 'revoke'}:
                browser_gateway.revoke()
                result.delete_cookie(browser_gateway.COOKIE, path=browser_gateway.PATH)
        if on_schedule_changed:
            await run_in_threadpool(on_schedule_changed)
        return result

    @router.post('/sources/{source_id}/verify')
    async def verify(source_id: int, request: Request):
        if not get_manager:
            raise HTTPException(409, 'Worker 未配置')
        result = await execute(get_manager().verify, source_id, await read_object(request))
        if on_schedule_changed:
            await run_in_threadpool(on_schedule_changed)
        return result

    @router.post('/sources/{source_id}/sync')
    async def sync(source_id: int, request: Request):
        if not get_manager:
            raise HTTPException(409, 'Worker 未配置')
        return await execute(get_manager().start_sync, source_id, await read_object(request))

    @router.post('/sources/{source_id}/reset')
    async def reset(source_id: int, request: Request):
        if not get_manager:
            raise HTTPException(409, 'Worker 未配置')
        result = await execute(get_manager().reset_source, source_id, await read_object(request))
        if on_schedule_changed:
            await run_in_threadpool(on_schedule_changed)
        return result

    @router.post('/sync')
    async def sync_all(request: Request):
        await read_object(request)
        if not get_manager:
            raise HTTPException(409, 'Worker 未配置')
        return await execute(get_manager().start_all)

    @router.post('/sources/{source_id}/check-files')
    async def check_files(source_id: int, request: Request):
        await read_object(request)
        if not get_manager or get_manager().queue is None:
            raise HTTPException(409, '任务队列未就绪')
        return await execute(get_manager().queue.start_xhs_validation, source_id)

    @router.post('/sources/{source_id}/notes/{note_id}/release')
    async def release(source_id: int, note_id: str, request: Request):
        await read_object(request)
        if not get_manager:
            raise HTTPException(409, 'Worker 未配置')
        return await execute(get_manager().release_note, source_id, note_id)

    @router.post("/sources")
    async def create_source(request: Request):
        result = await execute(get_service().create_source, await read_object(request))
        if on_schedule_changed:
            await run_in_threadpool(on_schedule_changed)
        return result

    @router.patch("/sources/{source_id}/anchor")
    async def update_source(source_id: int, request: Request):
        result = await execute(get_service().update_source, source_id, await read_object(request))
        if on_schedule_changed:
            await run_in_threadpool(on_schedule_changed)
        return result

    @router.post("/manifest/import")
    async def import_manifest(request: Request, source_id: int):
        if source_id <= 0:
            raise HTTPException(400, "来源 ID 无效")
        return await execute(get_service().import_manifest, source_id, await read_body(request))

    @router.get("/runs")
    async def runs(source_id: int | None = None, limit: int = 50):
        return await execute(lambda: {"items": get_service().runs(source_id, limit)})

    @router.get("/sources/{source_id}/notes")
    async def notes(source_id: int):
        return await execute(lambda: {"items": get_service().notes(source_id)})

    return router
