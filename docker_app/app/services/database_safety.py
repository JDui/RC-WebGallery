"""Durable files and consistent, verified database backup generations."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def sync_directory(path):
    if os.name != 'nt':
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def sync_file(path):
    with Path(path).open('r+b') as stream:
        os.fsync(stream.fileno())


def durable_replace(source, target):
    source, target = Path(source), Path(target)
    sync_file(source)
    os.replace(source, target)
    sync_directory(target.parent)
    if source.parent != target.parent:
        sync_directory(source.parent)


def atomic_json(path, value):
    path = Path(path)
    temp = path.with_name(path.name + f'.{uuid4().hex}.tmp')
    try:
        with temp.open('x', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        sync_directory(path.parent)
    finally:
        temp.unlink(missing_ok=True)


class DatabaseBackups:
    def __init__(self, db):
        self.db = db
        self.lock = threading.Lock()
        self.last_result = None

    def create(self, reason='scheduled', *, force=False):
        try:
            result = self._create(reason, force=force)
            self.last_result = {**result, 'ok': True, 'checked_at': datetime.now(timezone.utc).isoformat()}
            return result
        except Exception as exc:
            self.last_result = {'ok': False, 'error': str(exc), 'checked_at': datetime.now(timezone.utc).isoformat()}
            raise

    @staticmethod
    def validate(directory):
        """Read back a complete generation without altering its files."""
        from app.services.content_shards import SHARDS, check_database, check_catalog, checked_path, verify_identity
        directory = Path(directory)
        if directory.is_symlink() or (hasattr(directory, 'is_junction') and directory.is_junction()):
            raise RuntimeError('备份不能是符号链接')
        manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
        layout = manifest.get('layout_version')
        if manifest.get('version') != 1 or type(layout) is not int or layout not in (0, 1, 2, 3):
            raise RuntimeError('无效的备份清单')
        created = datetime.fromisoformat(str(manifest.get('created_at', '')))
        if created.tzinfo is None:
            raise RuntimeError('备份时间缺少时区')
        names = ('app', *(('control', 'browse') if layout >= 1 else ()), *(SHARDS if layout >= 3 else ()))
        if manifest.get('databases') != [f'{name}.db' for name in names]:
            raise RuntimeError('备份模块清单不完整')
        with closing(sqlite3.connect(checked_path(directory, 'app').resolve().as_uri() + '?mode=ro', uri=True)) as conn:
            for name in names[1:]:
                conn.execute(f'attach database ? as {name}', (checked_path(directory, name).resolve().as_uri() + '?mode=ro',))
            if layout >= 3:
                for name in names:
                    verify_identity(conn, 'main' if name == 'app' else name, manifest.get('dataset_id'), name)
                check_catalog(conn)
            row = conn.execute('select version from main.data_layout where id=1').fetchone()
            if not row or row[0] != layout:
                raise RuntimeError('备份版本与清单不一致')
            check_database(conn, ('main', *names[1:]))
        return manifest

    def status(self):
        parent = self.db.db_path.parent / 'backups'
        latest = None
        if parent.is_dir() and not parent.is_symlink():
            for path in sorted(parent.iterdir(), reverse=True):
                if path.name.startswith('.') or not path.is_dir() or path.is_symlink():
                    continue
                try:
                    manifest = json.loads((path / 'manifest.json').read_text(encoding='utf-8'))
                    if manifest.get('version') == 1:
                        latest = {'path': str(path), 'created_at': manifest.get('created_at'), 'reason': manifest.get('reason')}
                        break
                except (OSError, ValueError):
                    continue
        return {'last_attempt': self.last_result, 'latest': latest, 'interval_hours': 24, 'media_included': False}

    def _create(self, reason='scheduled', *, force=False):
        """Keep complete generations; a failed attempt never prunes good ones.

        BEGIN IMMEDIATE reserves every attached database against other writers.
        Separate read connections run the SQLite backup API while those locks
        are held, so all files represent the same application write boundary.
        """
        from app.services.content_shards import attach, check_database, check_catalog, checked_path, verify_identity
        root = self.db.db_path.parent
        parent = root / 'backups'
        with self.lock, self.db._write_lock:
            if parent.is_symlink() or (hasattr(parent, 'is_junction') and parent.is_junction()):
                raise RuntimeError('备份目录不能是符号链接')
            parent.mkdir(exist_ok=True)
            complete = sorted(p for p in parent.iterdir() if p.is_dir() and not p.name.startswith('.') and (p / 'manifest.json').is_file())
            valid = []
            for path in complete:
                try:
                    manifest = self.validate(path)
                    valid.append(path)
                except (OSError, ValueError, sqlite3.Error, RuntimeError):
                    # Keep damaged evidence; never count it toward redundancy.
                    continue
                age = datetime.now(timezone.utc).timestamp() - datetime.fromisoformat(manifest['created_at']).timestamp()
                if not force and 0 <= age < 86400 and manifest['layout_version'] == self.db.layout_version and manifest.get('dataset_id') == self.db._dataset_id:
                    return {'skipped': True, 'path': str(path)}
            name = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '-' + uuid4().hex[:8]
            stage, target = parent / ('.pending-' + name), parent / name
            stage.mkdir()
            sync_directory(parent)
            names = self.db.database_names()
            # Never open a missing source using sqlite's create-if-missing mode.
            with closing(sqlite3.connect(checked_path(root, 'app').resolve().as_uri() + '?mode=rw', uri=True, timeout=60)) as guard:
                if self.db.shards_enabled:
                    verify_identity(guard, 'main', self.db._dataset_id, 'app')
                guard.execute('pragma journal_mode=delete')
                guard.execute('pragma synchronous=extra')
                for module in names[1:]:
                    attach(guard, root, module, dataset_id=self.db._dataset_id)
                guard.execute('begin immediate')
                try:
                    check_database(guard, ('main', *names[1:]))
                    if self.db.shards_enabled:
                        check_catalog(guard)
                    for module in names:
                        source = checked_path(root, module)
                        destination = stage / source.name
                        with closing(sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True)) as src, closing(sqlite3.connect(destination)) as dst:
                            src.backup(dst)
                            if [r[0] for r in dst.execute('pragma quick_check')] != ['ok']:
                                raise RuntimeError(f'备份校验失败：{source.name}')
                        sync_file(destination)
                    atomic_json(stage / 'manifest.json', {
                        'version': 1, 'layout_version': self.db.layout_version,
                        'dataset_id': self.db._dataset_id, 'reason': reason,
                        'created_at': datetime.now(timezone.utc).isoformat(),
                        'databases': [f'{module}.db' for module in names],
                        'media_included': False,
                    })
                    self.validate(stage)
                    sync_directory(stage)
                    stage.rename(target)
                    sync_directory(parent)
                finally:
                    guard.rollback()
            # Keep at least three completed snapshots AND the pre-upgrade one.
            # Only delete directories whose manifest belongs to this feature.
            import shutil
            for old in (valid + [target])[:-3]:
                manifest = json.loads((old / 'manifest.json').read_text(encoding='utf-8'))
                if manifest.get('version') == 1 and manifest.get('reason') == 'scheduled' and not old.is_symlink():
                    shutil.rmtree(old)
            return {'skipped': False, 'path': str(target), 'databases': len(names)}
