from __future__ import annotations

from datetime import datetime
from contextlib import contextmanager
import hashlib
import re
import shutil
import time
import threading
from uuid import UUID, uuid4

from PIL import Image

from app.services.utils import TIMEZONE, dumps_json, loads_json, now_iso, parse_date
from app.services.livephoto import LivePhotoError
from app.services.xhs.models import XhsAsset, XhsNote, XhsValidationError, fields
from app.services.xhs.sync import XhsConflict, XhsSyncService
from app.services.xhs.worker_client import WorkerError, XhsWorkerClient


class XhsManager:
    # 这些失败状态可能只是一次性风控/页面抖动（explore 侧栏未及时渲染、验证码、限流），
    # profile 里的 cookie 往往仍然有效：允许节流式静默复检自动恢复，而不是一律逼用户重新扫码。
    AUTH_RECHECK_STATES = {'LOGIN_REQUIRED', 'NEEDS_REAUTH', 'NEEDS_REBIND', 'RATE_LIMITED',
                           'NEEDS_MANUAL_VERIFICATION', 'STRUCTURE_CHANGED', 'CURSOR_ERROR',
                           'LOGIN_START_FAILED', 'WORKER_ERROR', 'ERROR'}

    def __init__(self, db, storage, indexer, worker=None):
        self.db, self.storage, self.indexer = db, storage, indexer
        self.worker = worker or XhsWorkerClient()
        self.queue = None
        self._auth_recheck_last_at = 0.0
        self._auth_recheck_block_until = 0.0

    def ready(self, source_id):
        try:
            source = XhsSyncService(self.db).get_source(source_id)
            return bool(self.worker.configured and source['enabled'] and source['verification_passed']
                        and source['auth_status'] == 'READY')
        except (ValueError, OSError):
            return False

    def status(self):
        result = XhsSyncService(self.db).status()
        try:
            worker = self.worker.status()
        except WorkerError as exc:
            worker = {'state': exc.category, 'owner_key': None, 'network_mode': 'direct'}
        worker = self._maybe_auto_recheck(worker)
        result['worker'] = worker
        result['automatic_sync_available'] = self.worker.configured
        for source in result['items']:
            source['can_sync'] = self.ready(source['id'])
        return result

    def _maybe_auto_recheck(self, worker):
        if not self.worker.configured:
            return worker
        if worker.get('state') not in self.AUTH_RECHECK_STATES:
            return worker
        now = time.time()
        if now < self._auth_recheck_block_until or now - self._auth_recheck_last_at < 60:
            return worker
        with self.db.connect() as conn:
            had_login = conn.execute(
                "select 1 from xhs_source_accounts where auth_mode='browser' and verification_passed=1 "
                "and auth_status not in ('MANUAL_ONLY','LOGIN_IN_PROGRESS') limit 1").fetchone()
        if not had_login:
            return worker
        self._auth_recheck_last_at = now
        try:
            recheck = self.worker.status(check=True)
        except WorkerError as exc:
            # 复检确认失败：退避 10 分钟，避免状态轮询变成对小红书的频繁无头访问。
            self._auth_recheck_block_until = now + 600
            return {'state': exc.category, 'owner_key': worker.get('owner_key'), 'network_mode': 'direct'}
        self._save_auth(recheck)
        return recheck

    def refresh_auth(self):
        result = self.worker.status(check=True)
        self._save_auth(result)
        return result

    def _save_auth(self, result):
        owner, state = result.get('owner_key'), result.get('state', 'ERROR')
        if state == 'RUNNING':
            state = 'READY'
        avatar_url = result.get('avatar_url')
        checked_at = now_iso()
        with self.db.connect() as conn:
            conn.execute('''update xhs_source_accounts set auth_status = ?, last_auth_check_at = ?
                where owner_key = ?''', (state, checked_at, owner))
            if isinstance(avatar_url, str) and avatar_url.startswith('https://'):
                conn.execute('''update site_sources set icon_url = ?, icon_tiny_url = ?, updated_at = ?
                    where id in (select source_id from xhs_source_accounts where owner_key = ?)''',
                    (avatar_url, avatar_url, checked_at, owner))

    def login(self, action, payload):
        fields(payload, {'confirm_revoke'}, set())
        if action == 'start':
            with self.db.connect() as conn:
                if conn.execute("select 1 from xhs_jobs where state in ('queued','running') limit 1").fetchone():
                    raise XhsConflict('请先等待或取消正在排队的小红书任务')
        if action == 'revoke':
            if payload.get('confirm_revoke') is not True:
                raise XhsValidationError('注销登录态需要明确确认')
            owner = self.worker.status().get('owner_key')
            result = self.worker.request('POST', '/auth/revoke')
            with self.db.connect() as conn:
                conn.execute('''update site_sources set enabled = 0 where id in
                    (select source_id from xhs_source_accounts where owner_key = ?)''', (owner,))
                conn.execute('''update xhs_source_accounts set verification_passed = 0, auth_status = 'LOGIN_REQUIRED',
                    config_version = config_version + 1
                    where owner_key = ?''', (owner,))
            return result
        methods = {'start': '/auth/start', 'finish': '/auth/finish', 'check': '/auth/check', 'qr': '/auth/qr'}
        if action not in methods:
            raise XhsValidationError('登录操作无效')
        result = self.worker.request('POST', methods[action])
        if action == 'start':
            with self.db.connect() as conn:
                conn.execute("update xhs_source_accounts set auth_status='LOGIN_IN_PROGRESS' where auth_mode='browser'")
        if action not in {'start', 'qr'}:
            self._save_auth(result)
        return result

    def verify(self, source_id, payload):
        fields(payload, {'owner_confirmed', 'paging_confirmed', 'rules_reviewed', 'ordering_stable'},
               {'owner_confirmed', 'paging_confirmed', 'rules_reviewed', 'ordering_stable'})
        if any(payload[key] is not True for key in ('owner_confirmed', 'paging_confirmed', 'rules_reviewed')) or type(payload['ordering_stable']) is not bool:
            raise XhsValidationError('请确认本人点赞页、完整分页和访问规则；排序不稳定时使用完整扫描')
        source = XhsSyncService(self.db).get_source(source_id)
        result = self.refresh_auth()
        if result.get('state') != 'READY' or result.get('owner_key') != source['owner_key']:
            raise XhsConflict('请先登录与此来源一致的本人账号')
        with self.db.connect() as conn:
            conn.execute('''update xhs_source_accounts set verification_passed = 1, ordering_stable = ?,
                auth_mode = 'browser', auth_status = 'READY', config_version = config_version + 1 where source_id = ?''',
                         (int(payload['ordering_stable']), source_id))
        return {'ok': True, 'message': '本人点赞源已验证，可手动同步；自动刷新仍由独立开关控制'}

    def reserve_job(self, source_id, *, job_id=None, force_full=False, media_only=False):
        if not self.ready(source_id):
            # 排程同步遇到疑似"失效"时先做一次节流复检：误判态（风控抖动写坏的 auth_status）
            # 可以在这里静默恢复，而不是直接拒绝任务逼用户重新扫码。
            try:
                worker = self.worker.status()
            except WorkerError as exc:
                worker = {'state': exc.category}
            self._maybe_auto_recheck(worker)
        if not self.ready(source_id):
            raise XhsConflict('请先完成此来源的本人账号登录与验证')
        try:
            job_id = str(UUID(job_id)) if job_id else str(uuid4())
        except (TypeError, ValueError, AttributeError):
            raise XhsValidationError('任务 ID 必须是 UUID') from None
        with self.db.connect() as conn:
            conn.execute('begin immediate')
            existing = conn.execute('select * from xhs_jobs where job_id = ?', (job_id,)).fetchone()
            if existing:
                if existing['source_id'] != source_id or bool(existing['force_full']) != force_full or bool(existing['media_only']) != media_only:
                    raise XhsConflict('任务 ID 已被其他操作使用')
                return dict(existing), False
            if conn.execute("select 1 from xhs_jobs where source_id = ? and state in ('queued','running')", (source_id,)).fetchone():
                raise XhsConflict('此来源已有排队或执行中的任务')
            task = conn.execute('''insert into task_runs(task_type,status,message,details_json,created_at)
                values ('xhs-sync','queued','等待小红书同步',?,?)''',
                (dumps_json({'retry_action': {'kind': 'xhs-sync', 'source_id': source_id,
                                             'force_full': force_full, 'media_only': media_only}}), now_iso()))
            conn.execute('''insert into xhs_jobs(job_id,source_id,task_id,state,force_full,media_only,created_at)
                values (?,?,?,'queued',?,?,?)''', (job_id, source_id, task.lastrowid, int(force_full), int(media_only), now_iso()))
            return dict(conn.execute('select * from xhs_jobs where job_id = ?', (job_id,)).fetchone()), True

    def start_sync(self, source_id, payload=None):
        payload = payload or {}
        fields(payload, {'job_id', 'force_full', 'media_only'}, set())
        for key in ('force_full', 'media_only'):
            if type(payload.get(key, False)) is not bool:
                raise XhsValidationError('同步选项必须为布尔值')
        if self.queue is None:
            raise XhsConflict('任务队列未就绪')
        return self.queue.start_xhs_sync(source_id, **payload)

    def start_all(self):
        sources = [item for item in self.db.list_site_sources() if item['source_type'] == 'xhs_likes' and self.ready(item['id'])]
        if not sources:
            raise XhsConflict('请先在订阅管理中登录并验证小红书来源')
        jobs = []
        for source in sources:
            try:
                jobs.append(self.start_sync(source['id']))
            except XhsConflict:
                continue
        return {'ok': True, 'jobs': jobs, 'message': f'已提交 {len(jobs)} 个小红书同步任务，已有任务的来源自动跳过'}

    def reset_source(self, source_id, payload):
        fields(payload, {'confirmations', 'config_version'}, {'confirmations', 'config_version'})
        if type(payload['confirmations']) is not int or payload['confirmations'] != 3:
            raise XhsValidationError('完全重置需要三次明确确认')
        if type(payload['config_version']) is not int or payload['config_version'] < 0:
            raise XhsValidationError('来源版本无效，请刷新后重试')
        service = XhsSyncService(self.db)
        with service._source_lock(source_id), self.db.connect() as conn:
            conn.execute('begin immediate')
            source = service.get_source(source_id, conn)
            if source['config_version'] != payload['config_version']:
                raise XhsConflict('来源设置已更新，请刷新后重新确认')
            if conn.execute("select 1 from xhs_jobs where source_id=? and state in ('queued','running')", (source_id,)).fetchone():
                raise XhsConflict('请先停止或取消此来源正在执行和排队的任务')
            task_ids = []
            for task in conn.execute("select id,status,details_json from task_runs where task_type in ('xhs-sync','xhs-verify','xhs-import','xhs-unlike')"):
                detail = loads_json(task['details_json'], {})
                target = detail.get('source_id', (detail.get('retry_action') or {}).get('source_id'))
                if target == source_id:
                    if task['status'] in {'queued', 'running', 'paused'}:
                        raise XhsConflict('请先停止或取消此来源正在执行和排队的任务')
                    task_ids.append(task['id'])
            prefix = f'xhs_{source_id}_'
            folders = {row[0] for row in conn.execute('select folder_name from folders where subscription_uid=?', (f'site:{source_id}',))}
            for root in (self.storage.config.images_dir, self.storage.config.livephoto_dir):
                folders.update(path.name for path in root.iterdir() if path.name.startswith(prefix))
            for folder in folders:
                if not folder.startswith(prefix) or '/' in folder or '\\' in folder:
                    raise XhsConflict('来源目录异常，未执行重置')
            count = conn.execute('select count(*) from site_posts where source_id=?', (source_id,)).fetchone()[0]
            for folder in folders:
                self.storage.remove_folder_assets(folder)
                for table in ('pair_index', 'folder_index', 'folders'):
                    conn.execute(f'delete from {table} where folder_name=?', (folder,))
            self.storage.remove_site_source_assets(f'xhs-{source_id}')
            for table in ('trash_items', 'blacklist_items', 'deleted_pair_marks', 'filter_logs', 'review_items'):
                conn.execute(f'delete from {table} where source_dynamic_id like ?', (f'site:{source_id}:%',))
            conn.execute('delete from site_filter_logs where source_id=?', (source_id,))
            task_ids.extend(row[0] for row in conn.execute('select task_id from xhs_jobs where source_id=? and task_id is not null', (source_id,)))
            conn.execute('delete from xhs_jobs where source_id=?', (source_id,))
            for task_id in set(task_ids):
                conn.execute('delete from task_runs where id=?', (task_id,))
            conn.execute('delete from site_posts where source_id=?', (source_id,))
            conn.execute('delete from xhs_anchor_candidates where source_id=?', (source_id,))
            conn.execute('delete from xhs_sync_runs where source_id=?', (source_id,))
            conn.execute('delete from xhs_schedule_state where source_id=?', (source_id,))
            conn.execute('''update xhs_source_accounts set initial_fetch_completed=0,last_checkpoint_scan_id=null,
                last_checkpoint_at=null,checkpoint_version=checkpoint_version+1,config_version=config_version+1 where source_id=?''', (source_id,))
            conn.execute('delete from gallery_page_cache')
            conn.execute('delete from duplicate_group_cache')
            for ignored in conn.execute('select signature,folder_names_json from duplicate_ignores').fetchall():
                if folders.intersection(loads_json(ignored['folder_names_json'], [])):
                    conn.execute('delete from duplicate_ignores where signature=?', (ignored['signature'],))
        self.db.refresh_sidebar_count_cache()
        restarted = False
        if self.queue is not None and self.ready(source_id):
            try:
                self.start_sync(source_id)
                restarted = True
            except XhsConflict:
                pass
        return {'ok': True, 'removed_posts': count, 'restarted': restarted,
                'message': '已完全重置，并按首次拉取规则重新开始。' if restarted else '已完全重置；登录验证后可按首次规则重新同步。'}

    def recover(self):
        with self.db.connect() as conn:
            conn.execute("""update task_runs set status='failed',message='本地已删除，原帖取消点赞因重启中断',finished_at=?
                where task_type='xhs-unlike' and status in ('queued','running')""", (now_iso(),))
            interrupted = conn.execute("select * from xhs_jobs where state in ('queued','running')").fetchall()
            for job in interrupted:
                conn.execute("update xhs_jobs set state='interrupted', finished_at=?, error_category='PROCESS_RESTARTED' where job_id=?", (now_iso(), job['job_id']))
                if job['task_id']:
                    conn.execute("update task_runs set status='failed', message='应用重启，小红书任务可重试', finished_at=? where id=?", (now_iso(), job['task_id']))
        for path in (self.storage.config.data_dir / 'sites').glob('xhs-*/*/*/*.part'):
            if path.is_file() and not path.is_symlink():
                path.unlink(missing_ok=True)

    def fail_job(self, job_id, category):
        with self.db.connect() as conn:
            job = conn.execute('select task_id from xhs_jobs where job_id=?', (job_id,)).fetchone()
            conn.execute("update xhs_jobs set state='failed',finished_at=?,error_category=? where job_id=?", (now_iso(), category, job_id))
            if job:
                conn.execute("update task_runs set status='failed',finished_at=?,message='小红书任务失败，可重试' where id=?", (now_iso(), job[0]))

    def scheduled_sync(self, source_id):
        result = self.start_sync(source_id)
        deadline = time.monotonic() + 7200
        while time.monotonic() < deadline:
            with self.db.connect() as conn:
                row = conn.execute('select state from xhs_jobs where job_id=?', (result['job_id'],)).fetchone()
            if not row or row[0] not in {'queued', 'running'}:
                return
            time.sleep(1)

    def cancel_queued(self, job_id):
        with self.db.connect() as conn:
            job = conn.execute('select * from xhs_jobs where job_id = ?', (job_id,)).fetchone()
            if job:
                conn.execute("update xhs_jobs set state='cancelled', finished_at=? where job_id=?", (now_iso(), job_id))
                conn.execute("update task_runs set status='cancelled', finished_at=? where id=?", (now_iso(), job['task_id']))

    def execute(self, job_id, cooperate=None, cancelled=None, progress=None):
        with self.db.connect() as conn:
            job = conn.execute('select * from xhs_jobs where job_id=?', (job_id,)).fetchone()
            if not job:
                raise XhsConflict('任务不存在')
            job = dict(job)
            conn.execute("update xhs_jobs set state='running' where job_id=?", (job_id,))
            conn.execute("update task_runs set status='running', message='正在读取本人小红书点赞' where id=?", (job['task_id'],))
        source = XhsSyncService(self.db).get_source(job['source_id'])
        stats = {'source_id': source['source_id'], 'downloaded': 0, 'errors': 0, 'blocked': 0, 'added_items': [],
                 'retry_action': {'kind': 'xhs-sync', 'source_id': source['source_id'],
                                  'force_full': bool(job['force_full']), 'media_only': bool(job['media_only'])}}
        state = 'success'
        category = None
        try:
            if not self.ready(source['source_id']):
                raise WorkerError('NEEDS_REAUTH')
            with self.db.connect() as conn:
                allowed = [row[0] for row in conn.execute('select note_id from xhs_like_state where source_id=? limit 10000', (source['source_id'],))]
            with self.worker.session(source['owner_key'], allowed) as session_id, self._cancel_monitor(session_id, cancelled):
                if not job['media_only']:
                    with self.db.connect() as conn:
                        # A limited first scan establishes an incremental baseline;
                        # ordinary anchor hits must not postpone periodic full scans.
                        baseline = conn.execute('''select finished_at from xhs_sync_runs where source_id=?
                            and (complete_snapshot=1 or stop_reason='first_sync_limit')
                            and checkpoint_committed=1 order by rowid desc limit 1''', (source['source_id'],)).fetchone()
                    periodic_full = not baseline or (datetime.now(TIMEZONE) - datetime.fromisoformat(baseline[0])).days >= source['full_scan_interval_days']
                    stats['effective_force_full'] = bool(job['force_full']) or not source['ordering_stable'] or periodic_full
                    stats['discovery'] = XhsSyncService(self.db).scan_pages(
                        source['source_id'], source['owner_key'], self.worker.pages(session_id, cooperate),
                        ignore_anchors=stats['effective_force_full'],
                        max_pages=100, max_seconds=900, cancelled=cancelled, emit_task_history=False)
                    stop = stats['discovery']['stop_reason']
                    if stop in {'needs_reauth', 'needs_manual_verification', 'rate_limited', 'cancelled', 'network_error', 'structure_changed', 'cursor_error'}:
                        raise WorkerError({'needs_reauth': 'NEEDS_REAUTH', 'rate_limited': 'RATE_LIMITED',
                                           'needs_manual_verification': 'NEEDS_MANUAL_VERIFICATION',
                                           'cancelled': 'WORKER_ERROR', 'structure_changed': 'STRUCTURE_CHANGED',
                                           'cursor_error': 'CURSOR_ERROR'}.get(stop, 'WORKER_ERROR'))
                    if stats['discovery']['result'] == 'PARTIAL':
                        state = 'partial'
                else:
                    with self.db.connect() as conn:
                        wanted = {row[0] for row in conn.execute('''select l.note_id from xhs_like_state l
                            join xhs_note_downloads d on d.post_id=l.site_post_id where l.source_id=? and d.status!='ready' ''', (source['source_id'],))}
                    for index, page in enumerate(self.worker.pages(session_id, cooperate)):
                        wanted.difference_update(note.note_id for note in page.notes)
                        if not wanted or page.end_of_feed or index >= 99:
                            break
                self._download_pending(source, session_id, stats, cooperate, progress)
            if stats['errors']:
                state = 'partial'
        except Exception as exc:
            category = exc.category if isinstance(exc, WorkerError) else 'SYNC_FAILED'
            state = 'cancelled' if cancelled and cancelled() else 'failed'
            if category in {'NEEDS_REAUTH', 'NEEDS_REBIND', 'RATE_LIMITED', 'NEEDS_MANUAL_VERIFICATION', 'STRUCTURE_CHANGED', 'CURSOR_ERROR'}:
                with self.db.connect() as conn:
                    conn.execute('update xhs_source_accounts set auth_status=? where source_id=?', (category, source['source_id']))
        stats['error_category'] = category
        with self.db.connect() as conn:
            conn.execute('update xhs_jobs set state=?, finished_at=?, error_category=? where job_id=?', (state, now_iso(), category, job_id))
        self.db.finish_task_run(job['task_id'], state, {'success': '小红书同步完成', 'partial': '小红书同步部分完成，可重试媒体',
                                                     'cancelled': '小红书同步已取消', 'failed': '小红书同步失败，请检查登录或重试'}[state], stats)
        self.db.refresh_sidebar_count_cache(['all', 'livephoto', 'tasks', 'subscriptions', 'sites'])
        return {'state': state, **stats}

    @contextmanager
    def _cancel_monitor(self, session_id, cancelled):
        stopped = threading.Event()
        def monitor():
            while not stopped.wait(0.25):
                if cancelled and cancelled():
                    try:
                        self.worker.request('POST', f'/sessions/{session_id}/cancel')
                    except Exception:
                        pass
                    return
        thread = threading.Thread(target=monitor, daemon=True) if cancelled else None
        if thread:
            thread.start()
        try:
            yield
        finally:
            stopped.set()
            if thread:
                thread.join(timeout=2)

    def _download_pending(self, source, session_id, stats, cooperate, progress):
        with self.db.connect() as conn:
            posts = [dict(row) for row in conn.execute('''select p.*,l.note_id,l.note_type,d.status as download_status,d.media_revision
                from xhs_note_downloads d join site_posts p on p.id=d.post_id join xhs_like_state l on l.site_post_id=p.id
                where p.source_id=? and d.status != 'ready' order by p.id''', (source['source_id'],))]
        quota = source['media_quota_mb'] * 1024 * 1024
        for index, post in enumerate(posts):
            if cooperate:
                cooperate()
            if progress:
                progress(index, len(posts))
            dynamic_id = f"site:{source['source_id']}:{post['id']}"
            if (post['is_blocked'] or self.db.is_blacklisted(dynamic_id, dynamic_id)
                    or self.db.is_site_post_in_active_trash(source['source_id'], post['url'])):
                stats['blocked'] += 1
                self._note_result(post['id'], 'blocked', 'MANUAL_EXCLUDED')
                continue
            try:
                resolved = self.worker.request('POST', f'/sessions/{session_id}/resolve', {'note_id': post['note_id']})
                raw_assets = resolved.get('assets')
                # 一张图最多「静态图 + 实况」两条，上限按配对后的条数放宽。
                if not isinstance(raw_assets, list) or not raw_assets or len(raw_assets) > 200:
                    raise WorkerError('MEDIA_NEEDS_RESOLUTION')
                assets = [XhsAsset.parse(asset) for asset in raw_assets]
                missing_motion = resolved.get('missing_livephoto_ordinals', [])
                if (not isinstance(missing_motion, list)
                        or any(type(ordinal) is not int or not 0 <= ordinal < 100 for ordinal in missing_motion)
                        or len(set(missing_motion)) != len(missing_motion)
                        or not set(missing_motion) <= {asset.ordinal for asset in assets if asset.kind == 'livephoto'}
                        or resolved.get('media_complete') is not (not missing_motion)):
                    raise WorkerError('MEDIA_NEEDS_RESOLUTION')
                missing_motion = list(missing_motion)
                note = XhsNote.parse(resolved.get('note'))
                if note.note_id != post['note_id'] or tuple(sorted(assets, key=lambda asset: (asset.ordinal, asset.kind))) != note.assets:
                    raise WorkerError('MEDIA_NEEDS_RESOLUTION')
                previous_assets = {item['url']: item for item in self.db.list_site_assets(post['id'])}
                # Detail snapshots can omit every live marker. Keep motion
                # already observed for these image ordinals pending (or reuse
                # its cached file), rather than deleting it as obsolete.
                if note.type != 'video':
                    image_ordinals = {asset.ordinal for asset in assets if asset.kind == 'image'}
                    motion_ordinals = {asset.ordinal for asset in assets if asset.kind == 'livephoto'}
                    for item in previous_assets.values():
                        if item['media_type'] != 'livephoto':
                            continue
                        ordinal = int(item['url'].split(':')[-2])
                        if ordinal in image_ordinals and ordinal not in motion_ordinals:
                            assets.append(XhsAsset(ordinal, 'livephoto'))
                            missing_motion.append(ordinal)
                            motion_ordinals.add(ordinal)
                resolved_title = note.title
                if resolved_title == note.note_id and post.get('title') and post['title'] != note.note_id:
                    resolved_title = post['title']
                post['title'] = resolved_title
                post['note_type'] = 'livephoto' if note.type != 'video' and any(asset.kind == 'livephoto' for asset in assets) else note.type
                if note.published_at:
                    post['pub_date'] = datetime.fromisoformat(note.published_at).astimezone(TIMEZONE).date().isoformat()
                with self.db.connect() as conn:
                    conn.execute('update site_posts set title=?,pub_date=?,updated_at=? where id=?',
                                 (post['title'], post['pub_date'], now_iso(), post['id']))
                    conn.execute('update xhs_like_state set note_type=?,author_id=? where site_post_id=?',
                                 (post['note_type'], note.author_id, post['id']))
                expected_keys = set()
                live_errors = []
                for asset in assets:
                    if cooperate:
                        cooperate()
                    stable_key = f"urn:xhs:{source['source_id']}:{post['note_id']}:{asset.ordinal}:{asset.kind}"
                    expected_keys.add(stable_key)
                    row = self.db.upsert_site_asset(post['id'], {'url': stable_key, 'media_type': asset.kind,
                        'filename': previous_assets.get(stable_key, {}).get('filename') or f'{asset.ordinal:03d}-{asset.kind}'})
                    old_path = self.storage.resolve_storage_path(row.get('rel_path'))
                    if post['media_revision'] >= 1 and row['status'] == 'ready' and old_path and old_path.is_file() and old_path.stat().st_size:
                        continue
                    try:
                        if asset.kind == 'livephoto' and asset.ordinal in missing_motion:
                            raise WorkerError('LIVEPHOTO_STREAM_UNAVAILABLE', {'stage': 'resolve', 'ordinal': asset.ordinal})
                        downloaded = self._download_asset(session_id, post, row, asset, source, quota, cooperate)
                    except Exception as exc:
                        # A video's cover image is optional: keep going so the video
                        # itself can still be archived. _archive() generates a cover
                        # from a video frame when no image asset was stored. 实况同理：
                        # 取不到实况就保留静态图，别让整篇笔记连带失败。
                        category = exc.category if isinstance(exc, WorkerError) else 'MEDIA_DOWNLOAD_FAILED'
                        if category in {'CANCELLED', 'MEDIA_CANCELLED', 'MEDIA_QUOTA_EXCEEDED',
                                        'NEEDS_REAUTH', 'RATE_LIMITED', 'NEEDS_MANUAL_VERIFICATION'}:
                            raise
                        optional = asset.kind == 'livephoto' or (
                            post['note_type'] == 'video' and asset.kind == 'image')
                        if optional:
                            scope = 'livephoto' if asset.kind == 'livephoto' else 'cover'
                            diagnostic = exc.diagnostic if isinstance(exc, WorkerError) else {
                                'stage': scope, 'error': type(exc).__name__}
                            diagnostics = stats.setdefault('resolution_diagnostics', [])
                            if len(diagnostics) < 20:
                                diagnostics.append({'note_id': post['note_id'], 'category': category,
                                                    'scope': scope, **diagnostic})
                            key = 'livephoto_errors' if scope == 'livephoto' else 'cover_errors'
                            stats[key] = stats.get(key, 0) + 1
                            if scope == 'livephoto':
                                self.db.set_site_asset_result(row['id'], 'failed', error=category)
                                live_errors.append(category)
                            continue
                        raise
                    quota -= downloaded
                    stats['downloaded'] += 1
                with self.db.connect() as conn:
                    for old in previous_assets.values():
                        if old['url'] not in expected_keys:
                            conn.execute('delete from site_assets where id=?', (old['id'],))
                self.db.update_site_post_counts(post['id'])
                folder = self._archive(source, post)
                self._note_result(post['id'], 'partial' if live_errors else 'ready',
                                  live_errors[0] if live_errors else None)
                if live_errors:
                    stats['errors'] += 1
                stored_assets = [item for item in self.db.list_site_assets(post['id']) if item['status'] == 'ready']
                stats['added_items'].append({'folder_name': folder, 'top_dynamic_id': dynamic_id, 'source_dynamic_id': dynamic_id,
                                             'title': post['title'], 'new_post': True, 'saved_files': len(stored_assets),
                                             'image_count': sum(1 for item in stored_assets if item['media_type'] == 'image'),
                                             'livephoto_count': sum(1 for item in stored_assets if item['media_type'] == 'livephoto')})
            except Exception as exc:
                stats['errors'] += 1
                if isinstance(exc, WorkerError):
                    category, diagnostic = exc.category, exc.diagnostic
                elif isinstance(exc, LivePhotoError):
                    category, diagnostic = 'MEDIA_DOWNLOAD_FAILED', {'stage': 'livephoto-pairing', 'error': str(exc)}
                else:
                    category, diagnostic = 'MEDIA_DOWNLOAD_FAILED', {'stage': 'note', 'error': type(exc).__name__}
                self._note_result(post['id'], 'failed', category)
                if diagnostic:
                    diagnostics = stats.setdefault('resolution_diagnostics', [])
                    if len(diagnostics) < 20:
                        diagnostics.append({'note_id': post['note_id'], 'category': category, **diagnostic})
                if category in {'NEEDS_REAUTH', 'RATE_LIMITED', 'NEEDS_MANUAL_VERIFICATION', 'MEDIA_QUOTA_EXCEEDED',
                                'CANCELLED', 'MEDIA_CANCELLED'}:
                    raise

    def _note_result(self, post_id, status, error=None):
        with self.db.connect() as conn:
            conn.execute('''update xhs_note_downloads set status=?, error_category=?, updated_at=?,
                media_revision=case when ? in ('ready','partial') then 2 else media_revision end where post_id=?''',
                         (status, error, now_iso(), status, post_id))

    def _download_asset(self, session_id, post, row, asset, source, quota, cooperate):
        folder = self.storage.site_post_folder(f"xhs-{source['source_id']}", post['pub_date'], post['note_id'])
        part = folder / f'{asset.ordinal:03d}-{asset.kind}.part'
        target = None
        try:
            with self.worker.response('GET', f"/sessions/{session_id}/assets/{post['note_id']}/{asset.ordinal}/{asset.kind}", stream=True) as response:
                mime = response.headers.get('Content-Type', '').split(';')[0]
                extensions = {'image/jpeg': '.jpg', 'image/png': '.png', 'image/webp': '.webp',
                              'image/gif': '.gif', 'image/avif': '.avif', 'video/mp4': '.mp4'}
                if mime not in extensions or (asset.kind == 'image') != mime.startswith('image/'):
                    raise WorkerError('MEDIA_DOWNLOAD_FAILED',
                                      {'stage': 'mime', 'mime': mime[:64], 'kind': asset.kind})
                target = folder / f'{asset.ordinal:03d}-{asset.kind}{extensions[mime]}'
                digest, count = hashlib.sha256(), 0
                with part.open('wb') as output:
                    for chunk in response.iter_content(64 * 1024):
                        if cooperate:
                            cooperate()
                        count += len(chunk)
                        if count > min(quota, 512 * 1024 * 1024) or shutil.disk_usage(folder).free < 128 * 1024 * 1024:
                            raise WorkerError('MEDIA_QUOTA_EXCEEDED')
                        digest.update(chunk)
                        output.write(chunk)
                if not count or digest.hexdigest() != response.headers.get('X-Asset-Sha256'):
                    raise WorkerError('MEDIA_DOWNLOAD_FAILED', {
                        'stage': 'verify', 'bytes': count, 'kind': asset.kind,
                        'sha_match': digest.hexdigest() == response.headers.get('X-Asset-Sha256')})
                width, height = 0, 0
                if asset.kind == 'image':
                    with Image.open(part) as image:
                        width, height = image.size
                        image.verify()
                part.replace(target)
                with self.db.connect() as conn:
                    conn.execute('''update site_assets set status='ready',filename=?,rel_path=?,error=null,updated_at=? where id=?''',
                                 (target.name, self.storage.relative_to_storage(target), now_iso(), row['id']))
                    conn.execute('''insert into xhs_asset_details(asset_id,sha256,bytes,width,height,variant,duration)
                        values (?,?,?,?,?,?,?) on conflict(asset_id) do update set
                        sha256=excluded.sha256,bytes=excluded.bytes,width=excluded.width,height=excluded.height,
                        variant=excluded.variant,duration=excluded.duration''',
                                 (row['id'], digest.hexdigest(), count, width or int(response.headers.get('X-Asset-Width', 0)),
                                  height or int(response.headers.get('X-Asset-Height', 0)), 'visible_web_media',
                                  float(response.headers.get('X-Asset-Duration', 0))))
                return count
        except Exception as exc:
            part.unlink(missing_ok=True)
            if isinstance(exc, WorkerError):
                self.db.set_site_asset_result(row['id'], 'failed', error=exc.category)
                raise
            diagnostic = {'stage': 'asset', 'error': type(exc).__name__}
            errno = getattr(exc, 'errno', None)
            if errno is not None:
                diagnostic['errno'] = errno
            self.db.set_site_asset_result(row['id'], 'failed', error='MEDIA_DOWNLOAD_FAILED')
            raise WorkerError('MEDIA_DOWNLOAD_FAILED', diagnostic) from exc

    def _archive(self, source, post):
        folder_name = f"xhs_{source['source_id']}_{post['note_id']}"
        image_folder, video_folder = self.storage.image_folder(folder_name), self.storage.livephoto_folder(folder_name)
        image_folder.mkdir(parents=True, exist_ok=True)
        video_folder.mkdir(parents=True, exist_ok=True)
        expected_paths = set()
        changed_paths = []
        stored = self.db.list_site_assets(post['id'])
        image_ordinals = {int(asset['url'].split(':')[-2]) for asset in stored
                          if asset['media_type'] == 'image' and asset['status'] == 'ready' and asset['rel_path']}
        for asset in stored:
            if asset['status'] != 'ready' or not asset['rel_path']:
                continue
            path = self.storage.resolve_storage_path(asset['rel_path'])
            if not path or not path.is_file():
                continue
            ordinal = int(asset['url'].split(':')[-2]) + 1
            target = (image_folder if asset['media_type'] == 'image' else video_folder) / f'{ordinal:03d}__{asset["filename"]}'
            expected_paths.add(target)
            if not target.is_file() or target.stat().st_mtime < path.stat().st_mtime:
                if shutil.disk_usage(target.parent).free < path.stat().st_size + 128 * 1024 * 1024:
                    raise WorkerError('MEDIA_QUOTA_EXCEEDED')
                shutil.copy2(path, target)
                changed_paths.append(target)
            if asset['media_type'] == 'video' and ordinal - 1 not in image_ordinals:
                cover = image_folder / f'{ordinal:03d}__video-cover.jpg'
                expected_paths.add(cover)
                self.indexer.thumbnailer.ensure_video_cover(target, cover)
        for directory in (image_folder, video_folder):
            for old in directory.iterdir():
                if old.is_file() and len(old.name) > 5 and old.name[:3].isdigit() and old.name[3:5] == '__' and old not in expected_paths:
                    old.unlink()
        published = parse_date(post['pub_date'])
        pub_ts = int(datetime.combine(published, datetime.min.time(), TIMEZONE).timestamp()) if published else 0
        dynamic_id = f"site:{source['source_id']}:{post['id']}"
        self.indexer.index_folder(folder_name, pub_ts, post['title'], '', dynamic_id, dynamic_id,
                                  subscription_uid=f"site:{source['source_id']}", subscription_name=source['name'],
                                  metadata={'source': 'xhs', 'site_post_id': post['id'], 'site_post_url': post['url'],
                                            'standalone_video': post['note_type'] == 'video',
                                            'media_variant': 'visible_web_media'})
        self.indexer.livephotos.normalize_download(folder_name, changed_paths)
        return folder_name

    def unlike_target(self, folder_name):
        match = re.fullmatch(r'xhs_([0-9]+)_([A-Za-z0-9_-]+)', folder_name)
        if not match:
            return None
        source_id, note_id = int(match[1]), match[2]
        with self.db.connect() as conn:
            row = conn.execute('select 1 from xhs_like_state where source_id=? and note_id=?',
                               (source_id, note_id)).fetchone()
        return (source_id, note_id) if row else None

    def unlike_note(self, source_id, note_id, task_id):
        details = {'source_id': source_id, 'note_id': note_id, 'local_deleted': True}
        try:
            with XhsSyncService(self.db)._source_lock(source_id):
                source = XhsSyncService(self.db).get_source(source_id)
                if not self.ready(source_id):
                    raise WorkerError('LOGIN_REQUIRED')
                with self.worker.session(source['owner_key'], [note_id]) as session_id:
                    found = False
                    for index, page in enumerate(self.worker.pages(session_id)):
                        if any(note.note_id == note_id for note in page.notes):
                            found = True
                            break
                        if page.end_of_feed or index >= 99:
                            break
                    if not found:
                        raise WorkerError('NOTE_NOT_OBSERVED')
                    result = self.worker.request('POST', f'/sessions/{session_id}/unlike', {'note_id': note_id})
                    if result.get('note_id') != note_id or result.get('unliked') is not True:
                        raise WorkerError('UNLIKE_UNCONFIRMED')
                with self.db.connect() as conn:
                    conn.execute('update xhs_like_state set is_currently_liked=0 where source_id=? and note_id=?',
                                 (source_id, note_id))
            self.db.finish_task_run(task_id, 'success', '本地已删除，原帖已取消点赞', details)
        except Exception as exc:
            details['error_category'] = exc.category if isinstance(exc, WorkerError) else 'UNLIKE_UNCONFIRMED'
            self.db.finish_task_run(task_id, 'failed', '本地已删除，原帖取消点赞未完成', details)

    def release_note(self, source_id, note_id):
        with self.db.connect() as conn:
            row = conn.execute('select site_post_id from xhs_like_state where source_id=? and note_id=?', (source_id, note_id)).fetchone()
            if not row:
                raise XhsValidationError('笔记不存在')
            conn.execute("update site_posts set is_blocked=0,status='discovered' where id=?", (row[0],))
            conn.execute("update xhs_note_downloads set status='approved',error_category=null where post_id=?", (row[0],))
        return {'ok': True}

    def validate_archive(self, source_id, cooperate=None):
        XhsSyncService(self.db).get_source(source_id)
        with self.db.connect() as conn:
            assets = [dict(row) for row in conn.execute('''select a.*,d.sha256,l.note_id from site_assets a
                join site_posts p on p.id=a.post_id left join xhs_asset_details d on d.asset_id=a.id
                join xhs_like_state l on l.site_post_id=p.id
                where p.source_id=? and a.status='ready' ''', (source_id,))]
        failed = 0
        for asset in assets:
            if cooperate:
                cooperate()
            cached = self.storage.resolve_storage_path(asset['rel_path'])
            folder_name = f"xhs_{source_id}_{asset['note_id']}"
            parent = (self.storage.image_folder(folder_name) if asset['media_type'] == 'image'
                      else self.storage.livephoto_folder(folder_name))
            ordinal = int(asset['url'].split(':')[-2]) + 1
            archived = parent / f'{ordinal:03d}__{asset["filename"]}'
            valid = bool(asset['sha256'])
            for path in (cached, archived):
                if not valid or not path or not path.is_file():
                    valid = False
                    break
                digest = hashlib.sha256()
                with path.open('rb') as content:
                    while chunk := content.read(1024 * 1024):
                        if cooperate:
                            cooperate()
                        digest.update(chunk)
                expected = asset['sha256']
                if path == archived:
                    expected = self.indexer.livephotos.archive_checksum(folder_name, ordinal, archived) or expected
                valid = digest.hexdigest() == expected
            if not valid:
                failed += 1
                self.db.set_site_asset_result(asset['id'], 'failed', error='MEDIA_CHECKSUM_MISMATCH')
                self._note_result(asset['post_id'], 'failed', 'MEDIA_CHECKSUM_MISMATCH')
        return {'checked': len(assets), 'failed': failed, 'source_id': source_id}
