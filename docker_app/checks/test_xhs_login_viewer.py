"""Offline login transport tests; fixtures are not proof of a real XHS captcha."""
import asyncio
from collections import deque
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.services.xhs.browser import BrowserGateway, create_browser_router
from app.services.xhs.worker_client import WorkerError
from xhs_worker.main import create_app
from xhs_worker.runtime import BrowserFault, BrowserRuntime


def runtime():
    engine = BrowserRuntime.__new__(BrowserRuntime)
    engine.lock = asyncio.Lock()
    engine.login_deadline = time.monotonic() + 600
    engine.session_id = None
    engine.state = 'LOGIN_IN_PROGRESS'
    engine.login_frames = deque(maxlen=24)
    engine.context = Mock()
    engine.page = SimpleNamespace(
        viewport_size={'width': 1280, 'height': 900}, url='https://www.xiaohongshu.com/explore',
        screenshot=AsyncMock(return_value=b'jpeg'), mouse=SimpleNamespace(
            move=AsyncMock(), down=AsyncMock(), up=AsyncMock(), wheel=AsyncMock()),
        keyboard=SimpleNamespace(insert_text=AsyncMock(), press=AsyncMock()))
    engine._challenge_box = AsyncMock(return_value={'x': 420, 'y': 220, 'width': 400, 'height': 300})
    return engine


def run(coroutine):
    return asyncio.run(coroutine)


def test_crop_and_viewport_fallback():
    engine = runtime()
    crop = run(engine.login_frame({}))
    assert (crop['mode'], crop['width'], crop['height']) == ('captcha', 400, 300)
    assert engine.page.screenshot.call_args.kwargs['clip']['x'] == 420
    whole = run(engine.login_frame({'mode': 'page'}))
    assert (whole['mode'], whole['width'], whole['height']) == ('page', 1280, 900)
    engine._challenge_box.return_value = None
    assert run(engine.login_frame({}))['mode'] == 'page'


def test_crop_coordinates_and_drag_release():
    engine = runtime()
    frame = run(engine.login_frame({}))
    run(engine.login_input({'frame_id': frame['frame_id'], 'kind': 'pointer',
                           'points': [{'x': 20, 'y': 30, 't': 0}, {'x': 120, 'y': 30, 't': 20}]}))
    assert engine.page.mouse.move.call_args_list[0].args == (440, 250)
    assert engine.page.mouse.move.call_args_list[-1].args == (540, 250)
    engine.page.mouse.down.assert_awaited_once()
    engine.page.mouse.up.assert_awaited_once()


def test_release_after_pointer_failure():
    engine = runtime()
    frame = run(engine.login_frame({}))
    engine.page.mouse.move.side_effect = [None, RuntimeError('page navigated')]
    with pytest.raises(RuntimeError):
        run(engine.login_input({'frame_id': frame['frame_id'], 'kind': 'pointer',
                               'points': [{'x': 20, 'y': 30, 't': 0}]}))
    engine.page.mouse.up.assert_awaited_once()


@pytest.mark.parametrize('points', [[], [{'x': -1, 'y': 2, 't': 0}],
    [{'x': float('nan'), 'y': 0, 't': 0}], [{'x': 0, 'y': 300, 't': 0}],
    [{'x': 0, 'y': 0, 't': 8001}], [{'x': 0, 'y': 0, 't': 10}, {'x': 0, 'y': 0, 't': 5}],
    [{'x': True, 'y': 0, 't': 0}], [None]])
def test_invalid_gestures_never_press_mouse(points):
    engine = runtime()
    frame = run(engine.login_frame({}))
    with pytest.raises(BrowserFault, match='LOGIN_INPUT_INVALID'):
        run(engine.login_input({'frame_id': frame['frame_id'], 'kind': 'pointer', 'points': points}))
    engine.page.mouse.down.assert_not_awaited()


@pytest.mark.parametrize('change', ['url', 'viewport', 'challenge', 'expired', 'closed'])
def test_stale_frames_and_closed_sessions_reject_input(change):
    engine = runtime()
    frame = run(engine.login_frame({}))
    if change == 'url':
        engine.page.url += '/changed'
    elif change == 'viewport':
        engine.page.viewport_size = {'width': 640, 'height': 900}
    elif change == 'challenge':
        engine._challenge_box.return_value = None
    elif change == 'expired':
        old = engine.login_frames.pop()
        engine.login_frames.append((old[0], time.monotonic() - 40, *old[2:]))
    else:
        engine.login_deadline = 0
    with pytest.raises(BrowserFault, match='LOGIN_REQUIRED|LOGIN_FRAME_EXPIRED'):
        run(engine.login_input({'frame_id': frame['frame_id'], 'kind': 'key', 'key': 'Enter'}))
    engine.page.keyboard.press.assert_not_awaited()


def test_text_and_restricted_keys():
    engine = runtime()
    frame = run(engine.login_frame({}))
    for payload in ({'kind': 'text', 'text': '123中文'}, {'kind': 'key', 'key': 'Backspace'}):
        run(engine.login_input({'frame_id': frame['frame_id'], **payload}))
    engine.page.keyboard.insert_text.assert_awaited_once_with('123中文')
    with pytest.raises(BrowserFault, match='LOGIN_INPUT_INVALID'):
        run(engine.login_input({'frame_id': frame['frame_id'], 'kind': 'key', 'key': 'Control+L'}))


def test_risk_page_is_not_reloaded_or_reported_as_preparing():
    engine = runtime()
    engine.page.url = 'https://www.xiaohongshu.com/website-login/error?error_code=300012'
    result = run(engine._login_qr_probe())
    assert result['phase'] == 'blocked' and '300012' in result['message']
    engine._challenge_box.assert_not_awaited()


def gateway_client():
    gateway = BrowserGateway()
    gateway.begin('private-start-token')
    worker = Mock()
    worker.request.return_value = {'ok': True}
    app = FastAPI()
    app.include_router(create_browser_router(gateway, lambda: worker))
    client = TestClient(app)
    client.cookies.set(gateway.COOKIE, gateway.token)
    return client, gateway, worker


def test_viewer_has_no_external_asset_or_websocket_dependency():
    client, gateway, worker = gateway_client()
    response = client.get('/api/xhs/browser/')
    assert response.status_code == 200
    assert 'private-start-token' not in response.text
    assert '__NONCE__' not in response.text and 'nonce-' in response.headers['content-security-policy']
    assert 'WebSocket(' not in response.text and '<script src=' not in response.text
    assert response.headers['cache-control'] == 'no-store'
    worker.request.assert_not_called()


def test_relay_auth_proxy_origins_size_and_expiration():
    client, gateway, worker = gateway_client()
    endpoint = '/api/xhs/browser/input'
    assert client.post(endpoint, json={}, headers={'Origin': 'https://evil.example'}).status_code == 403
    assert client.post(endpoint, json={}, headers={'Sec-Fetch-Site': 'cross-site'}).status_code == 403
    # A same-origin browser request can have an internal Host after NAS proxying.
    response = client.post(endpoint, json={'points': [{'x': 12.123, 'y': 45.567, 't': i} for i in range(160)]},
                           headers={'Origin': 'https://gallery.example', 'Sec-Fetch-Site': 'same-origin'})
    assert response.status_code == 200
    assert worker.request.call_args.args[:2] == ('POST', '/auth/input')
    assert client.post(endpoint, json={'text': 'x' * 17000}).status_code == 413
    gateway.revoke()
    assert client.post(endpoint, json={}).status_code == 403


def test_worker_errors_are_actionable():
    client, gateway, worker = gateway_client()
    worker.request.side_effect = WorkerError('LOGIN_REQUIRED')
    assert client.post('/api/xhs/browser/frame', json={}).status_code == 410


def test_internal_worker_dispatches_frame_and_input():
    engine = runtime()
    engine.login_frame = AsyncMock(return_value={'mode': 'page'})
    client = TestClient(create_app(engine, token='s' * 32))
    response = client.post('/auth/frame', json={'mode': 'page'}, headers={'Authorization': 'Bearer ' + 's' * 32})
    assert response.status_code == 200
    engine.login_frame.assert_awaited_once_with({'mode': 'page'})


def test_challenge_box_clips_visible_cross_origin_iframe():
    engine = runtime()
    del engine._challenge_box
    hidden = SimpleNamespace(is_visible=AsyncMock(return_value=False))
    iframe = SimpleNamespace(is_visible=AsyncMock(return_value=True),
                             bounding_box=AsyncMock(return_value={'x': -10, 'y': 850, 'width': 400, 'height': 300}))
    nodes = Mock(count=AsyncMock(return_value=2))
    nodes.nth.side_effect = [hidden, iframe]
    engine.page.locator = Mock(return_value=nodes)
    assert run(engine._challenge_box()) == {'x': 0, 'y': 834, 'width': 406, 'height': 66}
