"""Exclusive, resumable maintenance. Never run against a live writer."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import closing, contextmanager
from pathlib import Path

from app.services.database_layout import split_modules
from app.services.content_shards import VERSION, SHARDS, split_content, check_database, check_catalog
from app.services.media_repair import MediaRepair
from app.services.database_safety import atomic_json, durable_replace
from uuid import uuid4
from app.services.utils import now_iso
from starlette.requests import Request
from starlette.responses import JSONResponse


def sync_directory(path):
    if os.name != 'nt':
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


@contextmanager
def storage_instance_lock(config_dir):
    """Refuse multiple web processes writing the same storage during migration."""
    with (config_dir / '.app-instance.lock').open('a+b') as stream:
        if os.name == 'nt':
            import msvcrt
            if stream.seek(0, os.SEEK_END) == 0:
                stream.write(b'0')
                stream.flush()
            stream.seek(0)
            acquire = lambda: msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            release = lambda: msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            acquire = lambda: fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            release = lambda: fcntl.flock(stream, fcntl.LOCK_UN)
        try:
            acquire()
        except OSError as exc:
            raise RuntimeError('此 storage 已被其他应用进程使用；请保持单容器、单 Uvicorn worker') from exc
        try:
            yield
        finally:
            stream.seek(0)
            release()


class DataUpgradeMiddleware:
    def __init__(self, app, upgrade, templates, app_title):
        self.app, self.upgrade, self.templates, self.app_title = app, upgrade, templates, app_title

    async def __call__(self, scope, receive, send):
        if scope.get('type') == 'websocket' and self.upgrade.blocked:
            await send({'type': 'websocket.close', 'code': 1013})
            return
        if scope.get('type') != 'http':
            await self.app(scope, receive, send)
            return
        path = scope.get('path', '')
        if path.startswith('/static/') or path == '/intranet/health' or path in ('/api/settings/data-upgrade', '/api/settings/data-upgrade/start'):
            await self.app(scope, receive, send)
            return
        if not self.upgrade.enter():
            request = Request(scope)
            if path == '/' or 'text/html' in request.headers.get('accept', ''):
                response = self.templates.TemplateResponse(request=request, name='data_upgrade.html',
                    context={'app_title': self.app_title}, headers={'Cache-Control': 'no-store'})
            else:
                response = JSONResponse({'detail': '数据升级正在接管页面', 'maintenance': True},
                                        status_code=503, headers={'Retry-After': '2', 'Cache-Control': 'no-store'})
            await response(scope, receive, send)
            return
        try:
            # Includes streamed bodies and response background tasks, not just
            # response headers: unlike-note workers must finish before migrating.
            await self.app(scope, receive, send)
        finally:
            self.upgrade.leave()


class DataUpgrade:
    def __init__(self, db, storage, indexer, manager):
        self.db, self.storage, self.indexer, self.manager = db, storage, indexer, manager
        self.path = storage.config.config_dir / 'data-upgrade.json'
        self.media_repair = MediaRepair(db, storage)
        self.condition = threading.Condition(threading.RLock())
        self.requests = 0
        self.version = db.layout_version
        self.before_run = lambda: None
        self.after_run = lambda: None
        self.external_busy = lambda: False
        self.state = {'status': 'idle', 'progress': 0, 'message': ''}
        if self.path.exists():
            try:
                saved = json.loads(self.path.read_text(encoding='utf-8'))
                if not isinstance(saved, dict) or saved.get('status') not in ('idle', 'running', 'failed', 'interrupted', 'completed'):
                    raise ValueError('无效的维护状态')
                self.state = saved
            except (OSError, ValueError):
                # Unknown maintenance state must never silently reopen writers.
                self.state.update(status='interrupted', error='维护状态文件无法读取，请检查存储后继续')
            if self.state['status'] in ('running', 'failed', 'interrupted'):
                self.state.update(status='interrupted', message='数据维护中断，请继续；已恢复的文件和已完成的目录会保留')
        manager.data_upgrade = self

    @property
    def blocked(self):
        return self.state['status'] in ('running', 'failed', 'interrupted')

    def enter(self):
        with self.condition:
            if self.blocked:
                return False
            self.requests += 1
            return True

    def leave(self):
        with self.condition:
            self.requests -= 1
            self.condition.notify_all()

    def report(self, message, **values):
        with self.condition:
            state = {**self.state, 'message': message, 'updated_at': now_iso(), **values}
            temp = self.path.with_suffix('.tmp')
            with temp.open('w', encoding='utf-8') as stream:
                json.dump(state, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            temp.replace(self.path)
            self.state = state
            sync_directory(self.path.parent)
            self._publish_task_status()

    def _publish_task_status(self):
        """Keep the shared task inspector consistent with the durable upgrade state."""
        state = self.state
        previous = getattr(self.manager, '_status', {})
        message = state.get('message', '')
        running = state.get('status') == 'running'
        progress = state.get('progress', 0)
        completed = [message] if state.get('status') == 'completed' else []
        target = {'check': '检查数据', 'repair': '修复缺失原文件', 'backup': '备份数据库'}.get(state.get('action'), '升级数据结构')
        next_step = '等待当前步骤完成' if running else ('任务完成' if state.get('status') == 'completed' else '修正问题后继续')
        self.manager._status = {
            **previous, 'mode': 'data-upgrade', 'running': running,
            'status': state.get('status'), 'completed_steps': completed,
            'stage': state.get('status'), 'message': message,
            'progress': progress, 'processed': progress, 'total': 100,
            'current_step': message, 'current_source': state.get('current') or '',
            'current_post': '', 'target': target, 'next_step': next_step,
            'error': state.get('error'),
            'stats': state.get('result') or {},
            'task_detail': {'completed': completed, 'doing': message, 'target': target, 'next': next_step},
        }
        append_event = getattr(self.manager, '_append_event', None)
        if append_event and (previous.get('message') != message or previous.get('running') != running):
            append_event(message, mode='data-upgrade', progress=progress, error=state.get('error'))

    def status(self):
        with self.condition:
            result = {**self.state, 'blocked': self.blocked}
        return {**result, 'version': self.version, 'current_version': VERSION, 'dirty': self.version < VERSION,
                'modules': self.db.modules_enabled, 'content_shards': self.db.shards_enabled,
                'backup': self.db.backups.status()}

    def start(self, action='upgrade'):
        with self.condition, self.manager._queue_lock:
            if self.state['status'] == 'running':
                return self.status()
            if action not in ('check', 'repair', 'upgrade', 'backup'):
                raise ValueError('无效的数据维护操作')
            label = {'check': '检查数据', 'repair': '修复缺失原文件', 'backup': '备份数据库'}.get(action, '升级数据结构')
            if self.manager._queue or not self.manager._acquire('data-upgrade', label):
                raise RuntimeError('请先等待后台任务完成，再开始数据升级')
            try:
                self.report('正在等待当前请求结束', status='running', action=action, progress=0, error=None, current=None, result=None)
                threading.Thread(target=self._run, daemon=True).start()
            except BaseException as exc:
                if self.state['status'] == 'running':
                    try:
                        self.report('维护启动失败，可以继续', status='failed', error=str(exc))
                    except OSError:
                        self.state.update(status='failed', message='维护启动失败，请检查存储后继续', error=str(exc))
                        self._publish_task_status()
                self.manager._release()
                raise
        return self.status()

    def _run(self):
        try:
            with self.condition:
                self.condition.wait_for(lambda: self.requests == 0)
            self.before_run()
            while self.external_busy():
                self.report('正在等待图标任务结束')
                threading.Event().wait(1)
            self.db.close()
            # SQLite super-journals make each filesystem-reference transaction
            # crash-atomic across app.db and browse.db during maintenance.
            self.db.exclusive_maintenance = True
            upgrading = self.state.get('action', 'upgrade') == 'upgrade'
            repairing = self.state.get('action') == 'repair'
            backing_up = self.state.get('action') == 'backup'
            if backing_up:
                self.report('正在校验并保存完整数据库备份', progress=5)
                result = self.db.backups.create('manual', force=True)
            if upgrading:
                self.report('正在校验并保存一致的升级前备份', progress=1)
                self.db.backups.create('before-v3-upgrade', force=True)
            if upgrading and not self.db.shards_enabled:
                self.report('正在备份数据库', progress=2)
                backup = self.path.parent / 'before-data-layout-v2'
                if backup.is_symlink() or (hasattr(backup, 'is_junction') and backup.is_junction()):
                    raise RuntimeError('旧版快照目录不能是符号链接')
                backup.mkdir(exist_ok=True)
                manifest = backup / 'snapshot.json'
                if manifest.is_symlink():
                    raise RuntimeError('旧版快照清单不能是符号链接')
                if not manifest.exists():
                    if any((backup / f'{name}.db').exists() for name in ('app', 'control', 'browse')):
                        raise RuntimeError('已有数据库备份但缺少快照清单，请保留备份并检查，避免混合不同时间的数据库')
                    atomic_json(manifest, {'modules': self.db.modules_enabled})
                    sync_directory(backup)
                    sync_directory(backup.parent)
                snapshot_modules = json.loads(manifest.read_text(encoding='utf-8'))['modules']
                # A complete pre-upgrade snapshot, retained across retries.
                for name in ('app', 'control', 'browse') if snapshot_modules else ('app',):
                    source = self.db.db_path if name == 'app' else self.path.parent / f'{name}.db'
                    target = backup / f'{name}.db'
                    if target.is_symlink():
                        raise RuntimeError('旧版快照数据库不能是符号链接')
                    if source.exists() and not target.exists():
                        temp = target.with_suffix(f'.{uuid4().hex}.tmp')
                        with closing(sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True)) as src, closing(sqlite3.connect(temp)) as dst:
                            src.backup(dst)
                            if dst.execute('pragma quick_check').fetchone()[0] != 'ok':
                                raise RuntimeError(f'备份校验失败：{name}.db')
                        durable_replace(temp, target)
                        sync_directory(backup)
                    if not target.is_file():
                        raise RuntimeError(f'数据库备份缺失：{name}.db')
                    with closing(sqlite3.connect(target.resolve().as_uri() + '?mode=ro', uri=True)) as saved:
                        if saved.execute('pragma quick_check').fetchone()[0] != 'ok':
                            raise RuntimeError(f'数据库备份损坏：{name}.db')
                self.report('正在拆分设置、订阅与浏览数据库', progress=8)
                split_modules(self.db, lambda message: self.report(message))
                self.version = 1
            if upgrading:
                # Already mapped v2 directories are skipped. Explicit retries
                # still discover legacy directories imported after that upgrade.
                self.report('正在核对媒体目录映射', progress=20)
                self._migrate_directories()
            if upgrading and not self.db.shards_enabled:
                self.report('正在分块并逐表校验 B站、站点、小红书数据库', progress=85)
                split_content(self.db, lambda message: self.report(message))
            if repairing:
                self.report('正在重新检查缺失原文件，排除已去重记录', progress=5)
                result = self.media_repair.repair(lambda done, total, current: self.report(
                    f'正在修复缺失原文件 {done}/{total}', progress=10 + int(75 * done / max(1, total)), current=current))
            self.report('正在验证数据库与媒体引用', progress=90)
            with self.db.connect() as conn:
                check_database(conn, ('main', *self.db.database_names()[1:]))
                if self.db.shards_enabled:
                    check_catalog(conn)
                if upgrading:
                    conn.execute('delete from gallery_page_cache')
                    conn.execute('delete from browse_queries')
                    conn.execute('delete from browse_boundaries')
                    conn.execute('delete from duplicate_group_cache')
                    conn.execute("update sidebar_count_cache set count=0 where key='duplicates'")
                elif repairing:
                    conn.execute('delete from gallery_page_cache')
            if upgrading and getattr(self.manager, 'duplicate_service', None):
                self.manager.duplicate_service.invalidate_cache()
            if not repairing and not backing_up:
                result = self._check_media()
            if upgrading:
                with self.db.connect() as conn:
                    conn.execute('update data_layout set version=3 where id=1')
                self.db.set_storage_stats_cache({**self.db.get_storage_stats_cache(), 'updated_at': None})
                self.version = VERSION
                self.db.layout_version = VERSION
                self.report('正在回收主库空闲页并优化索引', progress=96)
                with self.db.connect() as conn:
                    conn.execute('vacuum main')
                    for schema in ('main', 'control', 'browse', *SHARDS):
                        conn.execute(f'analyze {schema}')
                self.db.backups.create('after-v3-upgrade', force=True)
            elif repairing:
                self.db.set_storage_stats_cache({**self.db.get_storage_stats_cache(), 'updated_at': None})
            self.db.exclusive_maintenance = False
            self.db.keep_hot()
            self.after_run()
            message = ('完整数据库备份已校验并保存' if backing_up else '数据结构升级与目录迁移完成' if upgrading else
                       (f'修复完成，仍有 {result["missing_assets"]} 条引用需要处理' if repairing else '数据检查完成'))
            self.report(message, status='completed', progress=100, processed=self.state.get('total', 0), current=None, result=result)
        except Exception as exc:
            try:
                self.report('数据维护停止，可修正问题后继续', status='failed', error=str(exc))
            except OSError as state_error:
                # Disk-full/read-only errors can also prevent writing failure
                # status. Stay blocked in memory; the last durable running state
                # keeps the application blocked after restart as well.
                with self.condition:
                    self.state.update(status='failed', message='维护状态无法写入，修复存储后继续',
                                      error=f'{exc}; 状态保存失败：{state_error}')
                    self._publish_task_status()
        finally:
            self.manager._release()
            if not self.blocked:
                self.manager._run_next_queued_task()

    def _check_media(self):
        return self.media_repair.scan(lambda checked, missing: self.report(
            f'正在检查媒体引用：已检查 {checked} 个，缺失 {missing} 条', progress=95, current=None))

    def _migrate_directories(self):
        with self.db.connect() as conn:
            conn.execute('''create table if not exists storage_moves (
                old_dir text primary key, new_dir text not null, folder_name text not null,
                media_type text not null, done integer not null default 0)''')
            for schema in ('sites', 'xiaohongshu') if self.db.shards_enabled else ('main',):
                conn.execute(f'create index if not exists {schema}.idx_site_assets_rel_path on site_assets(rel_path)')
        # Recover a rename that succeeded before its reference transaction committed.
        with self.db.connect() as conn:
            pending = [dict(row) for row in conn.execute('select * from storage_moves where done=0')]
        for move in pending:
            self._move(move)
        folders = {item['folder_name']: item for item in self.db.list_folders()}
        with self.db.connect() as conn:
            known_sources = {kind: set() for kind in ('images', 'livephoto')}
            for row in conn.execute('select * from folder_locations'):
                parts = Path(row['rel_dir']).parts
                if len(parts) > 3:
                    known_sources[row['media_type']].add(parts[2])
        candidates = []
        for kind in known_sources:
            root = self.storage.config.data_dir / kind
            for directory in sorted(root.iterdir()):
                if not directory.is_dir() or directory.name in known_sources[kind] or directory.name.startswith(('bilibili-', 'xhs-')):
                    continue
                if directory.is_symlink():
                    raise RuntimeError(f'不迁移符号链接目录：{directory.name}')
                name = directory.name
                folder = folders.get(name, {})
                uid = str(folder.get('subscription_uid') or 'unknown')
                source = f'xhs-{name.split("_", 2)[1]}' if name.startswith('xhs_') else f'bilibili-{uid}'
                target = self.storage.layout_folder(kind, name, source, folder.get('pub_time') or name)
                candidates.append({'old_dir': self.storage.relative_to_storage(directory),
                                   'new_dir': self.storage.relative_to_storage(target),
                                   'folder_name': name, 'media_type': kind})
        total = len(candidates)
        for index, move in enumerate(candidates):
            self.report(f'正在迁移目录 {index + 1}/{total}', current=move['folder_name'],
                        processed=index, total=total, progress=20 + int(65 * index / max(1, total)))
            with self.db.connect() as conn:
                conn.execute('insert or ignore into storage_moves(old_dir,new_dir,folder_name,media_type) values(:old_dir,:new_dir,:folder_name,:media_type)', move)
            self._move(move)

    def _move(self, move):
        old, new = (self.storage.resolve_storage_path(move[key]) for key in ('old_dir', 'new_dir'))
        root = self.storage.config.storage_root.resolve()
        for key in ('old_dir', 'new_dir'):
            candidate = root
            for part in Path(move[key]).parts:
                candidate /= part
                if candidate.is_symlink() or candidate.is_junction():
                    raise RuntimeError(f'不迁移符号链接或目录联接：{move[key]}')
        if old.exists() and new.exists():
            raise RuntimeError(f'目标目录已存在，保留两端文件：{move["new_dir"]}')
        if old.is_symlink() or new.is_symlink():
            raise RuntimeError(f'不迁移符号链接目录：{move["old_dir"]}')
        if old.exists():
            new.parent.mkdir(parents=True, exist_ok=True)
            old.rename(new)
            sync_directory(new.parent)
            sync_directory(old.parent)
        if not new.is_dir():
            raise RuntimeError(f'迁移目录丢失：{move["old_dir"]}')
        before, after = move['old_dir'] + '/', move['new_dir'] + '/'
        folder = move['folder_name']
        columns = {
            'assets': ('rel_path', 'thumb_rel_path', 'small_thumb_rel_path', 'tiny_thumb_rel_path', 'cover_rel_path', 'reverse_rel_path', 'metadata_json'),
            'livephoto_pairs': ('image_path', 'video_path', 'transaction_json'),
            'folders': ('metadata_json',),
            'folder_index': ('preview_assets_json',),
            'pair_index': ('preview_url', 'thumb_url', 'image_json', 'livephoto_json'),
            'trash_items': ('folder_json', 'assets_json'),
        }
        # The rename journal and all live path references commit together.
        with self.db.connect() as conn:
            for table, fields in columns.items():
                assignments = ','.join(f'{field}=replace({field},?,?)' for field in fields)
                conn.execute(f'update {table} set {assignments} where folder_name=?', [v for _ in fields for v in (before, after)] + [folder])
            conn.execute('update site_assets set rel_path=replace(rel_path,?,?) where rel_path>=? and rel_path<?',
                         (before, after, before, move['old_dir'] + '0'))
            for table, fields in (('subscriptions', ('avatar_url', 'avatar_tiny_url')), ('site_sources', ('icon_url', 'icon_tiny_url'))):
                for field in fields:
                    conn.execute(f'update {table} set {field}=replace({field},?,?) where instr({field},?)>0', (before, after, before))
            conn.execute('insert or replace into folder_locations values(?,?,?)',
                         (folder, move['media_type'], move['new_dir']))
            conn.execute('update storage_moves set done=1 where old_dir=?', (move['old_dir'],))
