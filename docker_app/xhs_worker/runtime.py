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
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit
from uuid import uuid4

from app.services.xhs.models import XhsNote, identifier
from app.services.xhs.browser_errors import launch_diagnostic
from xhs_worker.media import download_public_media
from xhs_worker.parser import extract_detail_note, normalize_note, response_page, media_url, extract_video_urls, video_diagnostic


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
        self.viewer_processes = []
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
        challenges = self.page.locator('.captcha-container, .security-verify, iframe[src*="captcha"]')
        for index in range(await challenges.count()):
            if await challenges.nth(index).is_visible():
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
    def _viewer(self):
        password = secrets.token_urlsafe(6)[:8]
        password_file = self.work / ('vnc-' + uuid4().hex)
        subprocess.run(['x11vnc', '-storepasswd', password, str(password_file)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        password_file.chmod(0o600)
        commands = [
            ['x11vnc', '-display', os.environ.get('DISPLAY', ':99'), '-localhost', '-rfbport', '5900',
             '-rfbauth', str(password_file), '-forever', '-shared'],
        ]
        if os.environ.get('XHS_EMBEDDED') != '1':
            commands.append(['websockify', '--web', '/usr/share/novnc', '0.0.0.0:6080', '127.0.0.1:5900'])
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
            return {'state': self.state, 'owner_key': owner, 'avatar_url': self.avatar_url}

    async def check(self):
        async with self.lock:
            if self.session_id or self.login_deadline:
                return {'state': self.state, 'owner_key': self.owner_key, 'avatar_url': self.avatar_url}
            try:
                await self._launch()
                await self.page.goto('https://www.xiaohongshu.com/explore', wait_until='domcontentloaded')
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
                        for ordinal, kind, url in candidates:
                            key = (note['note_id'], ordinal, kind)
                            self.assets[key] = url
                            if kind == 'video':
                                card = raw.get('note_card') or raw.get('noteCard') or raw
                                self.asset_candidates[key] = extract_video_urls(card.get('video'))
                        safe_note = XhsNote.parse(note)
                        from dataclasses import asdict
                        result.append(asdict(safe_note))
                        self.seen.add(safe_note.note_id)
                        card = raw.get('note_card') or raw.get('noteCard') or raw
                        token = raw.get('xsec_token') or raw.get('xsecToken') or card.get('xsec_token') or card.get('xsecToken')
                        if isinstance(token, str) and re.fullmatch(r'[A-Za-z0-9_+/=-]{1,1024}', token):
                            source = raw.get('xsec_source') or raw.get('xsecSource') or card.get('xsec_source') or card.get('xsecSource') or 'pc_user'
                            source = source if isinstance(source, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', source) else 'pc_user'
                            self.note_links[safe_note.note_id] = safe_note.url + '?' + urlencode({'xsec_token': token, 'xsec_source': source})
                        if safe_note.note_id in observed_links:
                            self.note_links[safe_note.note_id] = observed_links[safe_note.note_id]
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
                if urlsplit(getattr(detail, 'url', '')).path.rstrip('/') == '/login':
                    raise BrowserFault('NOTE_DETAIL_LOGIN_REQUIRED')
                raw = None
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
                    if raw:
                        candidate_note, _ = normalize_note(raw, detail=True)
                        if candidate_note['type'] != 'video' or any(asset['kind'] == 'video' for asset in candidate_note['assets']):
                            break
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
                if not raw:
                    raise BrowserFault('VIDEO_STREAM_UNAVAILABLE' if last_video else 'NOTE_DETAIL_UNAVAILABLE',
                                       {'video_fields': last_video_fields, 'media_responses': len(player_responses)})
                note, candidates = normalize_note(raw, detail=True)
                card = raw.get('note_card') or raw.get('noteCard') or raw
                images = card.get('image_list') or card.get('imageList') or []
                image_count = sum(asset['kind'] == 'image' for asset in note['assets'])
                declared = card.get('image_count', card.get('imageCount', len(images)))
                if (note['note_id'] != note_id or not candidates or len(note['assets']) > 100
                        or (note['type'] == 'video' and not any(asset['kind'] == 'video' for asset in note['assets']))
                        or (note['type'] != 'video' and (not images or image_count != len(images)
                            or (isinstance(declared, int) and declared > image_count)))):
                    raise BrowserFault('MEDIA_NEEDS_RESOLUTION')
                from dataclasses import asdict
                parsed_note = XhsNote.parse(note)
                safe_note = asdict(parsed_note)
                safe_note['assets'] = [asdict(asset) for asset in parsed_note.assets]
                for ordinal, kind, url in candidates:
                    key = (note_id, ordinal, kind)
                    self.assets[key] = url
                    if kind == 'video':
                        self.asset_candidates[key] = extract_video_urls(card.get('video'))
                return {'note_id': note_id, 'assets': note['assets'], 'note': safe_note, 'media_complete': True}
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
                raise BrowserFault('MEDIA_NEEDS_RESOLUTION')
            target = self.work / (uuid4().hex + '.media')
            candidate_urls = self.asset_candidates.get(key) or [url]
            last_error = None
            for candidate in candidate_urls[:16]:
                if self.cancel_event.is_set():
                    raise BrowserFault('CANCELLED')
                try:
                    metadata = await asyncio.to_thread(
                        download_public_media, candidate, target, cancelled=self.cancel_event.is_set
                    )
                    return target, metadata
                except Exception as exc:
                    last_error = exc
                    target.unlink(missing_ok=True)
                    if self.cancel_event.is_set():
                        raise BrowserFault('CANCELLED') from None
            if last_error:
                raise last_error
            raise BrowserFault('MEDIA_NEEDS_RESOLUTION')

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
