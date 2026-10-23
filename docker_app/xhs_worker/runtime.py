from __future__ import annotations

import asyncio
import base64
from collections import deque
import fcntl
import hashlib
import hmac
import math
import os
from pathlib import Path
import random
import re
import secrets
import shutil
import time
import threading
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit
from uuid import uuid4

from app.services.utils import human_interval
from app.services.xhs.models import XhsNote, identifier, note_url
from app.services.xhs.browser_errors import launch_diagnostic
from xhs_worker.media import MediaRejected, download_public_media
from xhs_worker.parser import extract_detail_note, normalize_note, response_page, media_url, extract_video_candidates, extract_live_candidates, live_photo_seen, video_diagnostic


# Media downloads are the slowest worker operation. Keeping a single asset inside
# a bounded budget prevents one large or stalled candidate from occupying the
# worker for minutes and from exceeding the main application's read timeout.
# Original/screencast candidates only get a short probe so the playable stream
# fallback still has most of the budget.
ASSET_TOTAL_TIMEOUT = 480.0
ASSET_ORIGIN_TIMEOUT = 45.0
ASSET_ORIGIN_LIMIT = 3
ASSET_CANDIDATE_LIMIT = 6

# 登录二维码单独展示；安全验证通过同一登录会话的截图和操作回传完成。
# 选择器按官方登录弹窗从精确到宽泛排列，命中第一个“可见 + 接近正方形”的元素即截图。
LOGIN_QR_SELECTORS = (
    '.qrcode-img img',
    '.qrcode-img canvas',
    '.qrcode-img',
    'img[class*="qrcode"]',
    'canvas[class*="qrcode"]',
    'div[class*="qrcode"] img',
    'div[class*="qrcode"] canvas',
)
LOGIN_MODAL_SELECTOR = '.login-container, #login-container, .login-modal, div[class*="login-container"]'
LOGIN_CHALLENGE_SELECTOR = (
    '.captcha-container, .security-verify, #captcha, #captcha-modal, #red-captcha, #red-captcha-container, '
    '[class*="captcha-modal"], [class*="captcha-wrapper"], '
    '.verify-slider, .verify-dialog, iframe[src*="captcha"], iframe[src*="verify"]'
)
LOGIN_QR_MAX_IMAGE_BYTES = 1_600_000
# 连续抓不到二维码达到该次数后，提示重新启动扫码登录（约 60 秒）。
LOGIN_QR_MISS_LIMIT = 24


class BrowserFault(RuntimeError):
    def __init__(self, category, diagnostic=None):
        super().__init__(category)
        self.diagnostic = diagnostic



class BrowserRuntime:
    def __init__(self, root: Path, token: str) -> None:
        self.root, self.token = root, token
        root.mkdir(parents=True, exist_ok=True)
        root.chmod(0o700)
        salt_path = root / '.account-salt'
        if not salt_path.exists():
            descriptor = os.open(salt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'w') as output:
                output.write(secrets.token_hex(32))
        self.owner_salt = salt_path.read_bytes()
        self.work = root / 'work'
        self.work.mkdir(exist_ok=True)
        for pattern in ('*.part', '*.media', 'vnc-*'):
            for stale in self.work.glob(pattern):
                if stale.is_file() and not stale.is_symlink():
                    stale.unlink(missing_ok=True)
        self.profile = root / 'chromium-profile'
        self.lock = asyncio.Lock()
        self.profile_lock = None
        self.playwright = self.context = self.page = None
        self.state, self.owner_key = 'LOGIN_REQUIRED', None
        self.profile_url = None
        self.login_deadline = 0.0
        self.session_id = None
        self.session_deadline = 0.0
        self.queue = deque(maxlen=12)
        self.assets = {}
        self.asset_candidates = {}
        self.avatar_url = None
        self.note_links = {}
        self.seen = set()
        self.cursors = set()
        self.detail_payload = {}
        self.capture_tasks = set()
        self.cancel_event = threading.Event()
        self.last_launch_error = None
        # 二维码登录轮询的节流计数（仅在 LOGIN_IN_PROGRESS 期间使用）。
        self.qr_misses = 0
        self.qr_reloads = 0
        self.qr_login_clicks = 0
        self.login_frames = deque(maxlen=24)

    async def _launch(self, headed=False):
        from playwright.async_api import async_playwright
        if self.context:
            return
        handle = (self.root / '.profile.lock').open('a')
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise BrowserFault('PROFILE_BUSY') from None
        self.profile_lock = handle
        try:
            self.playwright = await async_playwright().start()
            environment = {key: value for key, value in os.environ.items() if 'proxy' not in key.lower()}
            self.context = await self.playwright.chromium.launch_persistent_context(
                str(self.profile), headless=not headed, chromium_sandbox=True, channel='chromium',
                env=environment, args=['--no-proxy-server'], accept_downloads=False,
                viewport={'width': 1280, 'height': 900}, timeout=45000,
            )
            self.context.set_default_timeout(20000)
            self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
            self.context.on('response', self._on_response)
            self.last_launch_error = None
        except Exception as exc:
            self.last_launch_error = launch_diagnostic(exc)
            await self._close()
            self.state = self.last_launch_error['category']
            raise BrowserFault(self.state) from None

    async def _close(self):
        for task in tuple(self.capture_tasks):
            task.cancel()
        if self.context:
            try:
                await self.context.close()
            except Exception:
                pass
        if self.playwright:
            try:
                await self.playwright.stop()
            except Exception:
                pass
        self.context = self.playwright = self.page = None
        if self.profile_lock:
            fcntl.flock(self.profile_lock, fcntl.LOCK_UN)
            self.profile_lock.close()
            self.profile_lock = None
        for path in self.work.glob('vnc-*'):
            path.unlink(missing_ok=True)
        self.login_deadline = 0
        self.login_frames.clear()

    def _on_response(self, response):
        task = asyncio.create_task(self._capture(response))
        self.capture_tasks.add(task)
        task.add_done_callback(self.capture_tasks.discard)

    async def _capture(self, response):
        try:
            parsed = urlsplit(response.url)
            if parsed.scheme != 'https' or not (parsed.hostname == 'xiaohongshu.com' or (parsed.hostname or '').endswith('.xiaohongshu.com')):
                return
            if '/note/like/page' not in parsed.path and '/v1/feed' not in parsed.path:
                return
            if response.status in (401, 403, 429):
                # 扫码期间的匿名推荐请求不能覆盖交互登录状态。
                if self.login_deadline and not self.session_id:
                    return
                self.state = {401: 'NEEDS_REAUTH', 403: 'NEEDS_MANUAL_VERIFICATION', 429: 'RATE_LIMITED'}[response.status]
                return
            headers = await response.all_headers()
            if int(headers.get('content-length', '0')) > 5 * 1024 * 1024:
                self.state = 'STRUCTURE_CHANGED'
                return
            body = await response.body()
            if len(body) > 5 * 1024 * 1024:
                return
            import json
            payload = json.loads(body)
            if not isinstance(payload, dict):
                return
            if payload.get('success') is False:
                self.state = 'NEEDS_MANUAL_VERIFICATION'
                return
            if '/v1/feed' in parsed.path:
                for raw in (payload.get('data') or {}).get('items', [])[:100]:
                    card = raw.get('note_card') or raw.get('noteCard') or raw
                    key = raw.get('id') or card.get('note_id') or card.get('noteId')
                    if key:
                        self.detail_payload[str(key)] = raw
                return
            page = response_page(payload)
            if page:
                cursor = (payload.get('data') or {}).get('cursor')
                if isinstance(cursor, str) and cursor and not page[1]:
                    cursor_hash = hashlib.sha256(cursor.encode()).digest()
                    if cursor_hash in self.cursors:
                        self.queue.clear()
                        self.state = 'CURSOR_ERROR'
                        return
                    self.cursors.add(cursor_hash)
                if len(self.queue) >= self.queue.maxlen:
                    self.queue.clear()
                    self.state = 'CURSOR_ERROR'
                    return
                self.queue.append(page)
        except Exception:
            return

    @staticmethod
    def _safe_avatar_url(value):
        if not isinstance(value, str) or len(value) > 2048:
            return None
        value = value.replace("\\u002F", "/").replace("\\/", "/")
        if value.startswith("//"):
            value = "https:" + value
        elif value.startswith("http://"):
            value = "https://" + value[7:]
        try:
            parsed = urlsplit(value)
            host = (parsed.hostname or "").lower()
            trusted_host = (
                host == "xiaohongshu.com" or host.endswith(".xiaohongshu.com")
                or host == "xhscdn.com" or host.endswith(".xhscdn.com")
            )
            if parsed.scheme != "https" or not trusted_host or parsed.port not in (None, 443):
                return None
        except ValueError:
            return None
        return value

    async def _identity(self) -> str:
        if await self._challenge_box():
            self.state = 'NEEDS_MANUAL_VERIFICATION'
            raise BrowserFault(self.state)
        links = self.page.locator('a.channel[href*="/user/profile/"], .side-bar a[href*="/user/profile/"], a.user[href*="/user/profile/"], [data-testid="self-profile"][href]')
        href = None
        profile_link = None
        for index in range(await links.count()):
            link = links.nth(index)
            if await link.is_visible() and ('我' in await link.inner_text() or await link.get_attribute('data-testid') == 'self-profile'):
                href = await link.get_attribute('href')
                profile_link = link
                break
        match = re.search(r'/user/profile/([A-Za-z0-9_-]+)', href or '')
        target = urlsplit(urljoin('https://www.xiaohongshu.com/', href or ''))
        if not match or target.scheme != 'https' or target.netloc != 'www.xiaohongshu.com':
            self.state = 'NEEDS_REAUTH'
            raise BrowserFault('NEEDS_REAUTH')
        avatar = None
        if profile_link is not None:
            try:
                avatar = await profile_link.locator('img').first.evaluate(
                    "(node) => node.currentSrc || node.src || node.getAttribute('data-src') || node.getAttribute('data-lazy-src') || ''"
                )
            except Exception:
                avatar = None
        self.avatar_url = self._safe_avatar_url(avatar)
        self.profile_url = urljoin('https://www.xiaohongshu.com/', href)
        return 'acct_' + hmac.new(self.owner_salt, match[1].encode(), hashlib.sha256).hexdigest()[:32]
    async def login_start(self):
        async with self.lock:
            if self.session_id:
                raise BrowserFault('WORKER_BUSY')
            await self._close()
            await self._launch(headed=True)
            try:
                # HTTP screenshots and input relay work without noVNC/WebSocket.
                password = secrets.token_urlsafe(24)
                await self.page.goto('https://www.xiaohongshu.com/explore', wait_until='domcontentloaded')
                self.state = 'LOGIN_IN_PROGRESS'
                self.login_deadline = time.monotonic() + 600
                self.qr_misses = self.qr_reloads = self.qr_login_clicks = 0
                return {'state': self.state, 'viewer_password': password, 'expires_in': 600}
            except BrowserFault as exc:
                await self._close()
                self.state = str(exc)
                raise
            except Exception:
                await self._close()
                self.state = 'ERROR'
                raise BrowserFault('LOGIN_START_FAILED') from None

    async def login_qr(self):
        """登录期间的一次抓取：返回登录二维码截图或当前登录阶段，供页面直接展示。

        二维码元素截成 PNG 回传；验证码通过 HTTP 验证画面处理，始终使用
        同一浏览器会话，不复制或重新加载第三方验证模块。
        """
        async with self.lock:
            if not self.context or not self.page or not self.login_deadline:
                raise BrowserFault('LOGIN_REQUIRED')
            if self.state in {'NEEDS_REAUTH', 'NEEDS_MANUAL_VERIFICATION', 'RATE_LIMITED'}:
                self.state = 'LOGIN_IN_PROGRESS'
            if self.state != 'LOGIN_IN_PROGRESS':
                raise BrowserFault(self.state)
            try:
                return await self._login_qr_probe()
            except BrowserFault:
                raise
            except Exception:
                # 页面导航中的瞬时抖动不应终结登录会话，下一轮轮询会重新探测。
                return {'state': self.state, 'phase': 'preparing', 'message': '登录页面正在加载…'}

    async def _login_qr_probe(self):
        page = self.page
        if urlsplit(page.url).path.rstrip('/') == '/website-login/error':
            code = parse_qs(urlsplit(page.url).query).get('error_code', [''])[0]
            message = ('小红书拒绝了当前网络（300012：IP 存在风险），请切换可靠网络后重新登录。'
                       if code == '300012' else '小红书显示安全限制，请查看下方页面提示。')
            return {'state': self.state, 'phase': 'blocked', 'message': message}
        if await self._challenge_box():
            return {'state': self.state, 'phase': 'captcha',
                    'message': '需要安全验证，请在下方验证画面中完成点击或拖动。'}
        try:
            cookies = {item.get('name'): item.get('value')
                       for item in await self.context.cookies('https://www.xiaohongshu.com')}
        except Exception:
            cookies = {}
        signed_in = bool(cookies.get('web_session'))
        modal_text = ''
        modal_visible = False
        modal = page.locator(LOGIN_MODAL_SELECTOR)
        for index in range(min(await modal.count(), 4)):
            node = modal.nth(index)
            if await node.is_visible():
                modal_visible = True
                try:
                    modal_text = (await node.inner_text() or '')[:800]
                except Exception:
                    modal_text = ''
                break
        if signed_in and not modal_visible:
            try:
                profile_ready = await page.evaluate('''() => {
                    const nodes = document.querySelectorAll('a.channel[href*="/user/profile/"], .side-bar a[href*="/user/profile/"], a.user[href*="/user/profile/"], [data-testid="self-profile"][href]');
                    for (const node of nodes) {
                        if (!node.getClientRects().length) continue;
                        if ((node.textContent || '').includes('我') || node.getAttribute('data-testid') === 'self-profile') return true;
                    }
                    return false;
                }''')
            except Exception:
                profile_ready = False
            if profile_ready:
                return {'state': self.state, 'phase': 'logged_in', 'message': '登录成功，正在完成…'}
            if self.qr_reloads < 3:
                self.qr_reloads += 1
                try:
                    await page.reload(wait_until='domcontentloaded', timeout=15000)
                except Exception:
                    pass
                return {'state': self.state, 'phase': 'finalizing', 'message': '已检测到登录，正在刷新页面确认…'}
        if modal_visible and any(token in modal_text for token in ('扫描成功', '已扫码', '请在手机上确认', '已在手机上确认')):
            return {'state': self.state, 'phase': 'scanned', 'message': '已扫码，请在手机上确认登录。'}
        for selector in LOGIN_QR_SELECTORS:
            try:
                nodes = page.locator(selector)
                total = await nodes.count()
            except Exception:
                continue
            for index in range(min(total, 4)):
                node = nodes.nth(index)
                try:
                    if not await node.is_visible():
                        continue
                    box = await node.bounding_box()
                except Exception:
                    continue
                if not box or box['width'] <= 0 or box['height'] <= 0:
                    continue
                if not (80 <= box['width'] <= 640 and 80 <= box['height'] <= 640):
                    continue
                if not (0.7 <= box['width'] / box['height'] <= 1.4):
                    continue
                try:
                    png = await node.screenshot(type='png', timeout=4000)
                except Exception:
                    continue
                if not png or len(png) < 100 or len(png) > LOGIN_QR_MAX_IMAGE_BYTES:
                    continue
                self.qr_misses = 0
                return {'state': self.state, 'phase': 'await_scan',
                        'image_data_url': 'data:image/png;base64,' + base64.b64encode(png).decode('ascii'),
                        'message': '请使用小红书 App 扫码并在手机上确认登录。'}
        if modal_visible and any(token in modal_text for token in ('已失效', '已过期', '点击刷新')):
            refreshed = False
            try:
                refresh = page.get_by_text('刷新', exact=False)
                for index in range(min(await refresh.count(), 4)):
                    node = refresh.nth(index)
                    if await node.is_visible():
                        try:
                            await node.click(timeout=3000)
                            refreshed = True
                        except Exception:
                            refreshed = False
                        break
            except Exception:
                refreshed = False
            if not refreshed:
                try:
                    await page.reload(wait_until='domcontentloaded', timeout=15000)
                except Exception:
                    pass
            return {'state': self.state, 'phase': 'refreshing', 'message': '二维码已刷新，请重新扫码。'}
        self.qr_misses += 1
        if not modal_visible and self.qr_misses >= 4 and self.qr_login_clicks < 2:
            # 有的会话不会自动弹出登录框，主动点一次页面上的“登录”按钮。
            self.qr_login_clicks += 1
            try:
                buttons = page.get_by_text('登录', exact=True)
                for index in range(min(await buttons.count(), 6)):
                    node = buttons.nth(index)
                    if await node.is_visible():
                        await node.click(timeout=3000)
                        break
            except Exception:
                pass
        if self.qr_misses >= LOGIN_QR_MISS_LIMIT:
            return {'state': self.state, 'phase': 'missing',
                    'message': '未能取得登录二维码，请打开验证画面检查当前页面。'}
        return {'state': self.state, 'phase': 'preparing', 'message': '正在准备登录二维码…'}

    async def _challenge_box(self):
        # Outer iframe boxes use main-viewport coordinates, including cross-origin frames.
        nodes = self.page.locator(LOGIN_CHALLENGE_SELECTOR)
        viewport = self.page.viewport_size or {'width': 1280, 'height': 900}
        for index in range(min(await nodes.count(), 20)):
            node = nodes.nth(index)
            if not await node.is_visible():
                continue
            box = await node.bounding_box()
            if not box:
                continue
            left, top = max(0, math.floor(box['x']) - 16), max(0, math.floor(box['y']) - 16)
            right = min(viewport['width'], math.ceil(box['x'] + box['width']) + 16)
            bottom = min(viewport['height'], math.ceil(box['y'] + box['height']) + 16)
            if right - left >= 60 and bottom - top >= 40:
                return {'x': left, 'y': top, 'width': right - left, 'height': bottom - top}
        return None

    def _require_login_view(self):
        if (not self.page or not self.context or self.session_id
                or time.monotonic() >= self.login_deadline):
            raise BrowserFault('LOGIN_REQUIRED')

    async def login_frame(self, payload):
        async with self.lock:
            self._require_login_view()
            viewport = self.page.viewport_size or {'width': 1280, 'height': 900}
            challenge = await self._challenge_box()
            clip = challenge if payload.get('mode') != 'page' and challenge else {
                'x': 0, 'y': 0, **viewport}
            # Never scroll an element into view: that would move a pending click/drag target.
            image = await self.page.screenshot(type='jpeg', quality=85, clip=clip, timeout=5000)
            if len(image) > LOGIN_QR_MAX_IMAGE_BYTES:
                raise BrowserFault('STRUCTURE_CHANGED')
            frame_id = secrets.token_urlsafe(18)
            self.login_frames.append((frame_id, time.monotonic(), dict(clip), self.page.url, dict(viewport), clip is challenge))
            return {'frame_id': frame_id, 'image_data_url': 'data:image/jpeg;base64,' + base64.b64encode(image).decode('ascii'),
                    'width': clip['width'], 'height': clip['height'],
                    'mode': 'captcha' if clip is challenge else 'page', 'challenge': bool(challenge)}

    async def login_input(self, payload):
        async with self.lock:
            self._require_login_view()
            frame = next((item for item in self.login_frames if item[0] == payload.get('frame_id')), None)
            if (not frame or time.monotonic() - frame[1] > 30 or frame[3] != self.page.url
                    or frame[4] != self.page.viewport_size):
                raise BrowserFault('LOGIN_FRAME_EXPIRED')
            clip = frame[2]
            if frame[5] and await self._challenge_box() != clip:
                raise BrowserFault('LOGIN_FRAME_EXPIRED')
            kind = payload.get('kind')
            if kind == 'pointer':
                points = payload.get('points')
                if not isinstance(points, list) or not 1 <= len(points) <= 160:
                    raise BrowserFault('LOGIN_INPUT_INVALID')
                previous = 0
                for point in points:
                    if not isinstance(point, dict):
                        raise BrowserFault('LOGIN_INPUT_INVALID')
                    x, y, elapsed = point.get('x'), point.get('y'), point.get('t')
                    if (any(type(v) not in (int, float) or not math.isfinite(v) for v in (x, y, elapsed))
                            or not 0 <= x < clip['width'] or not 0 <= y < clip['height']
                            or not previous <= elapsed <= 8000):
                        raise BrowserFault('LOGIN_INPUT_INVALID')
                    previous = elapsed
                await self.page.mouse.move(clip['x'] + points[0]['x'], clip['y'] + points[0]['y'])
                try:
                    await self.page.mouse.down()
                    previous = 0
                    for point in points:
                        await asyncio.sleep((point['t'] - previous) / 1000)
                        await self.page.mouse.move(clip['x'] + point['x'], clip['y'] + point['y'])
                        previous = point['t']
                finally:
                    await self.page.mouse.up()
            elif kind == 'text':
                value = payload.get('text')
                if not isinstance(value, str) or not 1 <= len(value) <= 128 or any(ord(c) < 32 for c in value):
                    raise BrowserFault('LOGIN_INPUT_INVALID')
                await self.page.keyboard.insert_text(value)
            elif kind == 'key':
                key = payload.get('key')
                if key not in {'Backspace', 'Delete', 'Tab', 'Enter', 'Escape', 'ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'}:
                    raise BrowserFault('LOGIN_INPUT_INVALID')
                await self.page.keyboard.press(key)
            elif kind == 'wheel':
                delta = payload.get('delta')
                if type(delta) not in (int, float) or not math.isfinite(delta) or abs(delta) > 900:
                    raise BrowserFault('LOGIN_INPUT_INVALID')
                await self.page.mouse.move(clip['x'] + clip['width'] / 2, clip['y'] + clip['height'] / 2)
                await self.page.mouse.wheel(0, delta)
            else:
                raise BrowserFault('LOGIN_INPUT_INVALID')
            return {'ok': True}

    async def login_finish(self):
        async with self.lock:
            if not self.context or not self.login_deadline:
                raise BrowserFault('LOGIN_REQUIRED')
            # 手机确认后，侧栏账号信息可能比 Cookie 晚渲染，允许短暂等待。
            for attempt in range(12):
                try:
                    owner = await self._identity()
                    break
                except BrowserFault as exc:
                    self.state = 'LOGIN_IN_PROGRESS'
                    if str(exc) != 'NEEDS_REAUTH':
                        raise
                    if attempt == 11:
                        raise BrowserFault('LOGIN_IN_PROGRESS') from None
                    await asyncio.sleep(0.5)
            if self.owner_key and owner != self.owner_key:
                await self._close()
                self.state = 'NEEDS_REBIND'
                raise BrowserFault('NEEDS_REBIND')
            self.owner_key = owner
            await self._close()
            self.state = 'READY'
            return {'state': self.state, 'owner_key': owner, 'avatar_url': self.avatar_url}

    async def check(self):
        async with self.lock:
            if self.session_id or self.login_deadline:
                return {'state': self.state, 'owner_key': self.owner_key, 'avatar_url': self.avatar_url}
            try:
                await self._launch()
                await self.page.goto('https://www.xiaohongshu.com/explore', wait_until='domcontentloaded')
                await asyncio.sleep(human_interval())
                owner = await self._identity()
                if self.owner_key and owner != self.owner_key:
                    raise BrowserFault('NEEDS_REBIND')
                self.owner_key, self.state = owner, 'READY'
            finally:
                await self._close()
            return {'state': self.state, 'owner_key': self.owner_key, 'avatar_url': self.avatar_url}

    async def session_start(self, owner_key, allowed_notes=()):
        async with self.lock:
            if self.session_id or self.login_deadline:
                raise BrowserFault('WORKER_BUSY')
            await self._launch()
            try:
                self.queue.clear()
                self.cancel_event.clear()
                self.assets.clear()
                self.asset_candidates.clear()
                self.note_links.clear()
                self.seen.clear()
                self.cursors.clear()
                self.seen.update(allowed_notes)
                self.detail_payload.clear()
                await self.page.goto('https://www.xiaohongshu.com/explore', wait_until='domcontentloaded')
                await asyncio.sleep(human_interval())
                actual = await self._identity()
                if actual != owner_key:
                    raise BrowserFault('NEEDS_REBIND')
                self.owner_key = actual
                await self.page.goto(self.profile_url, wait_until='domcontentloaded')
                likes = self.page.get_by_text('点赞', exact=True)
                visible_like = None
                for index in range(await likes.count()):
                    if await likes.nth(index).is_visible():
                        visible_like = likes.nth(index)
                        break
                if visible_like is None:
                    raise BrowserFault('LIKES_NOT_VISIBLE')
                await asyncio.sleep(human_interval())
                await visible_like.click()
                await asyncio.sleep(human_interval())
                self.session_id = uuid4().hex
                self.session_deadline = time.monotonic() + 1800
                self.state = 'RUNNING'
                return {'session_id': self.session_id, 'owner_key': actual}
            except Exception as exc:
                await self._close()
                self.state = str(exc) if isinstance(exc, BrowserFault) else 'STRUCTURE_CHANGED'
                raise BrowserFault(self.state) from None

    async def next_page(self, session_id):
        async with self.lock:
            self._require_session(session_id)
            for _ in range(40):
                if self.state != 'RUNNING':
                    raise BrowserFault(self.state)
                if self.queue:
                    raws, end, reliable = self.queue.popleft()
                    visible_links = await self.page.evaluate('''() => Array.from(document.querySelectorAll('a[href]'))
                        .slice(0, 2000).map(node => node.href)''')
                    observed_links = {}
                    for link in visible_links or []:
                        parsed = urlsplit(link)
                        match = re.fullmatch(r'/(?:explore|discovery/item)/([A-Za-z0-9_-]+)', parsed.path)
                        query = parse_qs(parsed.query)
                        token = query.get('xsec_token', [''])[0]
                        if (parsed.scheme == 'https' and parsed.netloc == 'www.xiaohongshu.com' and match
                                and re.fullmatch(r'[A-Za-z0-9_+/=-]{1,1024}', token)):
                            source = query.get('xsec_source', ['pc_user'])[0]
                            source = source if re.fullmatch(r'[A-Za-z0-9_-]{1,64}', source) else 'pc_user'
                            observed_links[match[1]] = urlunsplit(('https', parsed.netloc, parsed.path, urlencode({'xsec_token': token, 'xsec_source': source}), ''))
                    result = []
                    for raw in raws:
                        note, candidates = normalize_note(raw)
                        self._register_assets(note['note_id'], raw.get('note_card') or raw.get('noteCard') or raw, candidates)
                        safe_note = XhsNote.parse(note)
                        from dataclasses import asdict
                        self.seen.add(safe_note.note_id)
                        card = raw.get('note_card') or raw.get('noteCard') or raw
                        token = raw.get('xsec_token') or raw.get('xsecToken') or card.get('xsec_token') or card.get('xsecToken')
                        if isinstance(token, str) and re.fullmatch(r'[A-Za-z0-9_+/=-]{1,1024}', token):
                            source = raw.get('xsec_source') or raw.get('xsecSource') or card.get('xsec_source') or card.get('xsecSource') or 'pc_user'
                            source = source if isinstance(source, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', source) else 'pc_user'
                            self.note_links[safe_note.note_id] = safe_note.url + '?' + urlencode({'xsec_token': token, 'xsec_source': source})
                        if safe_note.note_id in observed_links:
                            self.note_links[safe_note.note_id] = observed_links[safe_note.note_id]
                        public_note = asdict(safe_note)
                        public_note['url'] = note_url(self.note_links.get(safe_note.note_id, safe_note.url), safe_note.note_id)
                        public_note['assets'] = [asdict(asset) for asset in safe_note.assets]
                        result.append(public_note)
                    return {'notes': result, 'end_of_feed': end, 'integrity_ok': reliable}
                # 拟人化滚动：随机步幅 + 250~880ms 随机间隔，避免固定 1100px/0.5s 的机器特征。
                await self.page.mouse.wheel(0, random.randint(650, 1300))
                await asyncio.sleep(human_interval())
            # No response is never treated as a natural end of the list.
            raise BrowserFault('CURSOR_ERROR')

    def _require_session(self, session_id):
        if not self.session_id or not secrets.compare_digest(session_id, self.session_id):
            raise BrowserFault('SESSION_EXPIRED')
        self.session_deadline = time.monotonic() + 1800

    def _register_assets(self, note_id, card, candidates):
        """记住每个媒体项的全部可选地址，让实况与视频共用同一套回退预算。"""
        images = card.get('image_list') if isinstance(card, dict) else None
        images = images or (card.get('imageList') if isinstance(card, dict) else None) or []
        if card.get('type') != 'video' and not card.get('video'):
            for ordinal, image in enumerate(images[:100]):
                if live_photo_seen(image):
                    self.assets.setdefault((note_id, ordinal, 'livephoto'), None)
        for ordinal, kind, url in candidates:
            key = (note_id, ordinal, kind)
            self.assets[key] = url
            if kind == 'video':
                self.asset_candidates[key] = extract_video_candidates(card.get('video') if isinstance(card, dict) else None)
            elif kind == 'livephoto' and isinstance(images, list) and 0 <= ordinal < len(images):
                self.asset_candidates[key] = extract_live_candidates(images[ordinal])

    async def resolve(self, session_id, note_id):
        async with self.lock:
            self._require_session(session_id)
            note_id = identifier(note_id)
            if note_id not in self.seen:
                raise BrowserFault('NOTE_NOT_OBSERVED')
            if note_id not in self.note_links:
                raise BrowserFault('NOTE_LINK_UNAVAILABLE')
            detail = await self.context.new_page()
            player_responses = []
            def capture_player(response):
                if (response.status in {200, 206} and response.request.resource_type == 'media'
                        and response.headers.get('content-type', '').split(';')[0].lower() == 'video/mp4'):
                    url = media_url(response.url)
                    if url and len(player_responses) < 20:
                        player_responses.append(url)
            detail.on('response', capture_player)
            try:
                self.detail_payload.pop(note_id, None)
                await detail.goto(self.note_links[note_id], wait_until='domcontentloaded')
                await asyncio.sleep(human_interval())
                if urlsplit(getattr(detail, 'url', '')).path.rstrip('/') == '/login':
                    raise BrowserFault('NOTE_DETAIL_LOGIN_REQUIRED')
                raw = None
                state_fallback = None
                state_waits = 0
                expected_motion = {key[1] for key in self.assets if key[0] == note_id and key[2] == 'livephoto'}
                best_image_raw, best_image_score = None, (-1, -1)
                last_video = False
                last_video_fields = []
                for _ in range(20):
                    raw = self.detail_payload.pop(note_id, None)
                    if not raw:
                        state = await detail.evaluate('''(id) => {
                            const source = window.__INITIAL_STATE__?.note;
                            const map = source?.noteDetailMap || source?.note_detail_map || {};
                            for (const entry of Object.values(map)) {
                                const note = entry?.note || entry?.note_card || entry?.noteCard;
                                if (note && String(note.noteId || note.note_id || note.id) === id)
                                    return {note: {noteDetailMap: {[id]: {note}}}};
                            }
                            return null;
                        }''', note_id)
                        raw = extract_detail_note(state, note_id)
                        state_fallback = raw
                    if raw:
                        candidate_note, candidate_urls = normalize_note(raw, detail=True)
                        is_video_note = candidate_note['type'] == 'video'
                        motion_ordinals = {ordinal for ordinal, kind, _ in candidate_urls if kind == 'livephoto'}
                        if not is_video_note:
                            expected_motion.update(asset['ordinal'] for asset in candidate_note['assets'] if asset['kind'] == 'livephoto')
                            score = (len(motion_ordinals), len(candidate_note['assets']))
                            if score > best_image_score:
                                best_image_raw, best_image_score = raw, score
                        missing_motion = bool(expected_motion - motion_ordinals)
                        ready = (not is_video_note
                                 or any(asset['kind'] == 'video' for asset in candidate_note['assets']))
                        if ready:
                            # __INITIAL_STATE__ 是 SSR 快照，实况视频的 stream 常常只在
                            # /v1/feed 响应里给全；只拿到快照时再多等几次，别让实况静默丢掉。
                            if (not is_video_note and (missing_motion or (raw is state_fallback and not motion_ordinals))
                                    and state_waits < 12):
                                state_waits += 1
                            else:
                                break
                        else:
                            last_video = True
                            card = raw.get('note_card') or raw.get('noteCard') or raw
                            last_video_fields = video_diagnostic(card.get('video'))
                            player = await detail.evaluate('''() => {
                                const root = document.querySelector('#noteContainer, .note-detail-mask, .note-container');
                                if (!root) return null;
                                const videos = Array.from(root.querySelectorAll('video')).filter(node => node.getClientRects().length);
                                if (videos.length !== 1) return null;
                                const video = videos[0];
                                return {urls: [video.currentSrc, video.src, ...Array.from(video.querySelectorAll('source')).map(node => node.src)]};
                            }''')
                            if isinstance(player, dict) and isinstance(player.get('urls'), list):
                                urls = player['urls'] + player_responses
                                url = next((media_url(value) for value in urls if media_url(value)), None)
                                if url:
                                    card = dict(card)
                                    card['video'] = {'media': {'stream': {'h264': [{'master_url': url}]}}}
                                    raw = {'id': note_id, 'note_card': card}
                                    break
                            raw = None
                    await asyncio.sleep(0.25)
                if self.state in {'NEEDS_REAUTH', 'NEEDS_MANUAL_VERIFICATION', 'RATE_LIMITED'}:
                    raise BrowserFault(self.state)
                if not last_video and best_image_raw is not None:
                    raw = best_image_raw
                if not raw:
                    raise BrowserFault('VIDEO_STREAM_UNAVAILABLE' if last_video else 'NOTE_DETAIL_UNAVAILABLE',
                                       {'video_fields': last_video_fields, 'media_responses': len(player_responses)})
                note, candidates = normalize_note(raw, detail=True)
                card = raw.get('note_card') or raw.get('noteCard') or raw
                images = card.get('image_list') or card.get('imageList') or []
                if note['type'] != 'video':
                    present_motion = {asset['ordinal'] for asset in note['assets'] if asset['kind'] == 'livephoto'}
                    for ordinal in sorted(expected_motion - present_motion):
                        if ordinal < len(images):
                            note['assets'].append({'ordinal': ordinal, 'kind': 'livephoto'})
                    if any(asset['kind'] == 'livephoto' for asset in note['assets']):
                        note['type'] = 'livephoto'
                image_count = sum(asset['kind'] == 'image' for asset in note['assets'])
                motion_count = sum(asset['kind'] == 'livephoto' for asset in note['assets'])
                declared = card.get('image_count', card.get('imageCount', len(images)))
                if (note['note_id'] != note_id or not candidates or len(note['assets']) > 200
                        or (note['type'] == 'video' and not any(asset['kind'] == 'video' for asset in note['assets']))
                        or (note['type'] != 'video' and (not images or image_count != len(images)
                            or (isinstance(declared, int) and declared > image_count)))):
                    # livephoto_seen 说明载荷里确实有实况线索，却没能解析出可下载地址，
                    # 与「这篇本来就没有实况」区分开，便于在解析诊断里判断结构变更。
                    raise BrowserFault('MEDIA_NEEDS_RESOLUTION', {
                        'video_fields': last_video_fields,
                        'livephoto_seen': sum(1 for image in images if live_photo_seen(image)),
                        'livephoto_assets': motion_count})
                from dataclasses import asdict
                note['url'] = self.note_links[note_id]
                parsed_note = XhsNote.parse(note)
                safe_note = asdict(parsed_note)
                safe_note['assets'] = [asdict(asset) for asset in parsed_note.assets]
                # Never reuse an expired URL left over from list-page parsing or
                # an earlier resolve when the refreshed detail omitted it.
                for mapping in (self.assets, self.asset_candidates):
                    for key in list(mapping):
                        if key[0] == note_id:
                            del mapping[key]
                self._register_assets(note_id, card, candidates)
                motion_ordinals = {ordinal for ordinal, kind, _ in candidates if kind == 'livephoto'}
                missing_motion = [asset['ordinal'] for asset in note['assets']
                                  if asset['kind'] == 'livephoto' and asset['ordinal'] not in motion_ordinals]
                return {'note_id': note_id, 'assets': note['assets'], 'note': safe_note,
                        'media_complete': not missing_motion, 'missing_livephoto_ordinals': missing_motion}
            finally:
                await detail.close()

    async def unlike(self, session_id, note_id):
        async with self.lock:
            self._require_session(session_id)
            note_id = identifier(note_id)
            if note_id not in self.seen or note_id not in self.note_links:
                raise BrowserFault('NOTE_NOT_OBSERVED')
            page = await self.context.new_page()
            try:
                await page.goto(self.note_links[note_id], wait_until='domcontentloaded')
                await asyncio.sleep(human_interval())
                selector = '.interact-container .like-wrapper, .engage-bar .like-wrapper, button[aria-label="取消点赞"], button[aria-label="点赞"]'
                async def state():
                    return await page.evaluate('''({id, selector}) => {
                        const map = window.__INITIAL_STATE__?.note?.noteDetailMap || window.__INITIAL_STATE__?.note?.note_detail_map || {};
                        const note = Object.values(map).map(entry => entry?.note || entry?.note_card || entry?.noteCard)
                            .find(note => note && String(note.noteId || note.note_id || note.id) === id);
                        if (!note) return null;
                        const buttons = Array.from(document.querySelectorAll(selector)).filter(node => node.getClientRects().length);
                        if (buttons.length !== 1) return null;
                        const node = buttons[0], label = node.getAttribute('aria-label');
                        if (label === '取消点赞' || node.getAttribute('aria-pressed') === 'true') return true;
                        if (label === '点赞' || node.getAttribute('aria-pressed') === 'false') return false;
                        const icon = node.querySelector('use');
                        const href = icon?.getAttribute('href') || icon?.getAttribute('xlink:href') || '';
                        if (href.endsWith('#liked')) return true;
                        if (href.endsWith('#like')) return false;
                        const info = note.interactInfo || note.interact_info;
                        return typeof info?.liked === 'boolean' ? info.liked : null;
                    }''', {'id': note_id, 'selector': selector})
                before = None
                for _ in range(20):
                    before = await state()
                    if before is not None:
                        break
                    await asyncio.sleep(0.25)
                if before is None:
                    raise BrowserFault('UNLIKE_UNCONFIRMED')
                if before:
                    buttons = page.locator(selector)
                    visible = [buttons.nth(i) for i in range(await buttons.count()) if await buttons.nth(i).is_visible()]
                    if len(visible) != 1:
                        raise BrowserFault('UNLIKE_UNCONFIRMED')
                    await asyncio.sleep(human_interval())
                    await visible[0].click()
                    for _ in range(20):
                        if await state() is False:
                            break
                        await asyncio.sleep(0.25)
                    else:
                        raise BrowserFault('UNLIKE_UNCONFIRMED')
                return {'note_id': note_id, 'unliked': True}
            finally:
                await page.close()

    async def asset(self, session_id, note_id, ordinal, kind):
        async with self.lock:
            self._require_session(session_id)
            key = (identifier(note_id), ordinal, kind)
            url = self.assets.get(key)
            if not url:
                raise BrowserFault('LIVEPHOTO_STREAM_UNAVAILABLE' if kind == 'livephoto' else 'MEDIA_NEEDS_RESOLUTION')
            # Snapshot the candidates, then release the runtime lock before any
            # network I/O. The main application waits only 90 seconds for
            # resolve/page requests, so a slow media download must never hold the
            # lock that those requests need.
            candidates = list(self.asset_candidates.get(key) or [('stream', url)])
        origin_like = [item for item in candidates if item[0] != 'stream'][:ASSET_ORIGIN_LIMIT]
        streams = [item for item in candidates if item[0] == 'stream'][:max(1, ASSET_CANDIDATE_LIMIT - len(origin_like))]
        # When no playable stream was advertised, the preferred original is the
        # only option and may use the whole budget; otherwise it gets a short
        # probe so the stream fallback keeps most of the budget.
        origin_cap = None if not streams else ASSET_ORIGIN_TIMEOUT
        plan = [(candidate, origin_cap) for candidate in origin_like]
        plan += [(candidate, None) for candidate in streams]
        target = self.work / (uuid4().hex + '.media')
        deadline = time.monotonic() + ASSET_TOTAL_TIMEOUT
        def cancelled():
            return self.cancel_event.is_set() or time.monotonic() > deadline
        last_error = None
        try:
            for index, ((kind, candidate), cap) in enumerate(plan):
                if self.cancel_event.is_set():
                    raise BrowserFault('CANCELLED')
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    metadata = await asyncio.to_thread(
                        download_public_media, candidate, target, cancelled=cancelled,
                        timeout=remaining if cap is None else min(cap, remaining),
                    )
                    return target, metadata
                except Exception as exc:
                    last_error = exc
                    if isinstance(exc, MediaRejected):
                        exc.diagnostic.setdefault('candidate', f'{index}:{kind}')
                    target.unlink(missing_ok=True)
                    if self.cancel_event.is_set():
                        raise BrowserFault('CANCELLED') from None
            if time.monotonic() > deadline:
                raise MediaRejected('MEDIA_TIME_LIMIT', {'stage': 'budget', 'candidates': len(plan)})
            if last_error:
                if isinstance(last_error, MediaRejected):
                    raise last_error
                raise MediaRejected('MEDIA_DOWNLOAD_FAILED',
                                    {'stage': 'worker', 'error': type(last_error).__name__})
            raise BrowserFault('MEDIA_NEEDS_RESOLUTION')
        except Exception:
            target.unlink(missing_ok=True)
            raise

    async def cancel(self, session_id):
        self._require_session(session_id)
        self.cancel_event.set()
        self.state = 'CANCELLED'
        if self.page:
            try:
                await self.page.close()
            except Exception:
                pass
        return {'state': 'CANCELLED'}

    async def session_close(self, session_id):
        async with self.lock:
            self._require_session(session_id)
            await self._close()
            self.session_id = None
            self.assets.clear()
            self.asset_candidates.clear()
            self.note_links.clear()
            self.detail_payload.clear()
            self.queue.clear()
            self.seen.clear()
            if self.state == 'RUNNING':
                self.state = 'READY'
            for path in self.work.glob('*.media'):
                path.unlink(missing_ok=True)
            return {'state': self.state}

    async def revoke(self):
        async with self.lock:
            if self.session_id:
                raise BrowserFault('WORKER_BUSY')
            await self._close()
            shutil.rmtree(self.profile, ignore_errors=True)
            self.owner_key, self.state = None, 'LOGIN_REQUIRED'
            return {'state': self.state}

    async def reap(self):
        while True:
            await asyncio.sleep(5)
            if self.login_deadline and time.monotonic() > self.login_deadline:
                async with self.lock:
                    await self._close()
                    self.state = 'LOGIN_REQUIRED'
            if self.session_id and time.monotonic() > self.session_deadline:
                await self.session_close(self.session_id)
