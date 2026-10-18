from __future__ import annotations

import asyncio
from collections import deque
import fcntl
import hashlib
import hmac
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import time
import threading
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

from app.services.xhs.models import XhsNote, identifier
from xhs_worker.media import download_public_media
from xhs_worker.parser import normalize_note, response_page


class BrowserFault(RuntimeError):
    pass


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
        self.viewer_processes = []
        self.session_id = None
        self.session_deadline = 0.0
        self.queue = deque(maxlen=12)
        self.assets = {}
        self.note_links = {}
        self.seen = set()
        self.cursors = set()
        self.detail_payload = {}
        self.capture_tasks = set()
        self.cancel_event = threading.Event()

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
        except Exception:
            await self._close()
            raise BrowserFault('BROWSER_UNAVAILABLE') from None

    async def _close(self):
        for process in self.viewer_processes:
            if process.poll() is None:
                process.terminate()
        self.viewer_processes = []
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

    async def _identity(self) -> str:
        challenges = self.page.locator('.captcha-container, .security-verify, iframe[src*="captcha"]')
        for index in range(await challenges.count()):
            if await challenges.nth(index).is_visible():
                self.state = 'NEEDS_MANUAL_VERIFICATION'
                raise BrowserFault(self.state)
        links = self.page.locator('a.channel[href*="/user/profile/"], .side-bar a[href*="/user/profile/"], a.user[href*="/user/profile/"], [data-testid="self-profile"][href]')
        href = None
        for index in range(await links.count()):
            link = links.nth(index)
            if await link.is_visible() and ('我' in await link.inner_text() or await link.get_attribute('data-testid') == 'self-profile'):
                href = await link.get_attribute('href')
                break
        match = re.search(r'/user/profile/([A-Za-z0-9_-]+)', href or '')
        target = urlsplit(urljoin('https://www.xiaohongshu.com/', href or ''))
        if not match or target.scheme != 'https' or target.netloc != 'www.xiaohongshu.com':
            self.state = 'NEEDS_REAUTH'
            raise BrowserFault('NEEDS_REAUTH')
        self.profile_url = urljoin('https://www.xiaohongshu.com/', href)
        return 'acct_' + hmac.new(self.owner_salt, match[1].encode(), hashlib.sha256).hexdigest()[:32]

    def _viewer(self):
        password = secrets.token_urlsafe(6)[:8]
        password_file = self.work / ('vnc-' + uuid4().hex)
        subprocess.run(['x11vnc', '-storepasswd', password, str(password_file)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        password_file.chmod(0o600)
        commands = [
            ['x11vnc', '-display', os.environ.get('DISPLAY', ':99'), '-localhost', '-rfbport', '5900',
             '-rfbauth', str(password_file), '-forever', '-shared'],
            ['websockify', '--web', '/usr/share/novnc', '0.0.0.0:6080', '127.0.0.1:5900'],
        ]
        self.viewer_processes = [subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for command in commands]
        return password

    async def login_start(self):
        async with self.lock:
            if self.session_id:
                raise BrowserFault('WORKER_BUSY')
            await self._close()
            await self._launch(headed=True)
            try:
                password = self._viewer()
                await self.page.goto('https://www.xiaohongshu.com/explore', wait_until='domcontentloaded')
                self.state = 'LOGIN_IN_PROGRESS'
                self.login_deadline = time.monotonic() + 600
                return {'state': self.state, 'viewer_password': password, 'expires_in': 600}
            except Exception:
                await self._close()
                self.state = 'ERROR'
                raise BrowserFault('LOGIN_START_FAILED') from None

    async def login_finish(self):
        async with self.lock:
            if not self.context or not self.login_deadline:
                raise BrowserFault('LOGIN_REQUIRED')
            owner = await self._identity()
            if self.owner_key and owner != self.owner_key:
                await self._close()
                self.state = 'NEEDS_REBIND'
                raise BrowserFault('NEEDS_REBIND')
            self.owner_key = owner
            await self._close()
            self.state = 'READY'
            return {'state': self.state, 'owner_key': owner}

    async def check(self):
        async with self.lock:
            if self.session_id or self.login_deadline:
                return {'state': self.state, 'owner_key': self.owner_key}
            try:
                await self._launch()
                await self.page.goto('https://www.xiaohongshu.com/explore', wait_until='domcontentloaded')
                owner = await self._identity()
                if self.owner_key and owner != self.owner_key:
                    raise BrowserFault('NEEDS_REBIND')
                self.owner_key, self.state = owner, 'READY'
            finally:
                await self._close()
            return {'state': self.state, 'owner_key': self.owner_key}

    async def session_start(self, owner_key, allowed_notes=()):
        async with self.lock:
            if self.session_id or self.login_deadline:
                raise BrowserFault('WORKER_BUSY')
            await self._launch()
            try:
                self.queue.clear()
                self.cancel_event.clear()
                self.assets.clear()
                self.note_links.clear()
                self.seen.clear()
                self.cursors.clear()
                self.seen.update(allowed_notes)
                self.detail_payload.clear()
                await self.page.goto('https://www.xiaohongshu.com/explore', wait_until='domcontentloaded')
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
                await visible_like.click()
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
                    result = []
                    for raw in raws:
                        note, candidates = normalize_note(raw)
                        for ordinal, kind, url in candidates:
                            self.assets[(note['note_id'], ordinal, kind)] = url
                        safe_note = XhsNote.parse(note)
                        from dataclasses import asdict
                        result.append(asdict(safe_note))
                        self.seen.add(safe_note.note_id)
                        token = raw.get('xsec_token') or raw.get('xsecToken')
                        if isinstance(token, str) and re.fullmatch(r'[A-Za-z0-9_=-]{1,1024}', token):
                            from urllib.parse import urlencode
                            self.note_links[safe_note.note_id] = safe_note.url + '?' + urlencode({'xsec_token': token, 'xsec_source': 'pc_user'})
                    return {'notes': result, 'end_of_feed': end, 'integrity_ok': reliable}
                await self.page.mouse.wheel(0, 1100)
                await asyncio.sleep(0.5)
            # No response is never treated as a natural end of the list.
            raise BrowserFault('CURSOR_ERROR')

    def _require_session(self, session_id):
        if not self.session_id or not secrets.compare_digest(session_id, self.session_id):
            raise BrowserFault('SESSION_EXPIRED')
        self.session_deadline = time.monotonic() + 1800

    async def resolve(self, session_id, note_id):
        async with self.lock:
            self._require_session(session_id)
            note_id = identifier(note_id)
            if note_id not in self.seen:
                raise BrowserFault('NOTE_NOT_OBSERVED')
            detail = await self.context.new_page()
            try:
                await detail.goto(self.note_links.get(note_id, f'https://www.xiaohongshu.com/explore/{note_id}'), wait_until='domcontentloaded')
                for _ in range(20):
                    if note_id in self.detail_payload:
                        break
                    await asyncio.sleep(0.25)
                if self.state in {'NEEDS_REAUTH', 'NEEDS_MANUAL_VERIFICATION', 'RATE_LIMITED'}:
                    raise BrowserFault(self.state)
                raw = self.detail_payload.pop(note_id, None)
                if raw:
                    note, candidates = normalize_note(raw)
                else:
                    images = await detail.locator('.note-detail-mask img.note-slider-img, .note-content img').evaluate_all('(nodes) => nodes.map(n => n.currentSrc || n.src)')
                    videos = await detail.locator('.note-detail-mask video').evaluate_all('(nodes) => nodes.map(n => n.currentSrc || n.src)')
                    note = {'note_id': note_id, 'assets': []}
                    candidates = []
                    for ordinal, url in enumerate(dict.fromkeys(images)):
                        if url.startswith('https://'):
                            note['assets'].append({'ordinal': ordinal, 'kind': 'image'})
                            candidates.append((ordinal, 'image', url))
                    for ordinal, url in enumerate(videos[:1]):
                        if url.startswith('https://'):
                            note['assets'].append({'ordinal': ordinal, 'kind': 'video'})
                            candidates.append((ordinal, 'video', url))
                for ordinal, kind, url in candidates:
                    self.assets[(note_id, ordinal, kind)] = url
                return {'note_id': note_id, 'assets': note['assets']}
            finally:
                await detail.close()

    async def asset(self, session_id, note_id, ordinal, kind):
        async with self.lock:
            self._require_session(session_id)
            key = (identifier(note_id), ordinal, kind)
            url = self.assets.get(key)
            if not url:
                raise BrowserFault('MEDIA_NEEDS_RESOLUTION')
            target = self.work / (uuid4().hex + '.media')
            metadata = await asyncio.to_thread(download_public_media, url, target, cancelled=self.cancel_event.is_set)
            return target, metadata

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
