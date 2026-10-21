"""Explicit, bounded recovery of missing originals; never deletes library rows."""
from __future__ import annotations

from collections import defaultdict
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from urllib.parse import urlparse

from PIL import Image

from app.services.site_downloader import MediaDownloader
from app.services.thumbnailer import ThumbnailService
from app.services.utils import now_iso


class MediaRepair:
    def __init__(self, db, storage):
        self.db, self.storage = db, storage
        self.path = storage.config.config_dir / 'data-check-report.json'

    def save(self, report):
        from app.services.data_upgrade import sync_directory
        temp = self.path.with_suffix('.tmp')
        with temp.open('w', encoding='utf-8') as stream:
            json.dump(report, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(self.path)
        sync_directory(self.path.parent)

    def load(self):
        if not self.path.exists():
            return {'check_version': 2, 'items': [], 'needs_check': True}
        report = json.loads(self.path.read_text(encoding='utf-8'))
        if not isinstance(report, dict) or not isinstance(report.get('items'), list):
            raise ValueError('检查明细格式损坏，请重新检查数据')
        return report

    def target(self, rel_path):
        path = Path(rel_path or '')
        if (not rel_path or path.is_absolute() or '\\' in rel_path or '..' in path.parts
                or len(path.parts) < 4 or path.parts[0] != 'data'
                or path.parts[1] not in ('images', 'livephoto', 'sites')
                or any(part.startswith('.') for part in path.parts)):
            raise ValueError('无效的原文件路径')
        candidate = self.storage.config.storage_root.resolve()
        for part in path.parts:
            candidate /= part
            if candidate.is_symlink() or candidate.is_junction():
                raise ValueError('原文件路径含符号链接或目录联接')
        return candidate

    def scan(self, progress=lambda *_: None):
        items, checked = [], 0
        with self.db.connect() as conn:
            ignored = conn.execute("select count(*) from site_assets where status='duplicate'").fetchone()[0]
        for table in ('assets', 'site_assets'):
            last_id = 0
            while True:
                with self.db.connect() as conn:
                    if table == 'assets':
                        rows = conn.execute('''select a.id,a.rel_path,a.filename,a.media_type,a.folder_name,
                            null as url,null as source_type,null as source_id,null as post_id,
                            null as post_url,null as post_slug
                            from assets a where a.id>? order by a.id limit 512''', (last_id,)).fetchall()
                    else:
                        rows = conn.execute('''select a.id,a.rel_path,a.filename,a.media_type,null as folder_name,
                            a.url,s.source_type,s.id as source_id,p.id as post_id,p.url as post_url,p.slug as post_slug
                            from site_assets a join site_posts p on p.id=a.post_id join site_sources s on s.id=p.source_id
                            where a.id>? and a.status!='duplicate' and (a.rel_path is not null or a.status='ready')
                            order by a.id limit 512''', (last_id,)).fetchall()
                if not rows:
                    break
                for row in rows:
                    checked += 1
                    reason = '原文件不存在'
                    try:
                        target = self.target(row['rel_path'])
                        if target.is_file() and target.stat().st_size > 0:
                            continue
                        if target.is_file():
                            reason = '原文件为空，保留现场，需要手动检查'
                        elif target.exists():
                            reason = '原文件路径被目录占用'
                    except (OSError, ValueError) as exc:
                        reason = str(exc)
                    items.append({**dict(row), 'table': table, 'status': 'missing', 'reason': reason, 'error': None})
                last_id = rows[-1]['id']
                progress(checked, len(items))
        result = {'check_version': 2, 'checked_assets': checked, 'missing_assets': len(items),
                  'missing_files': len({row['rel_path'] for row in items}), 'ignored_duplicates': ignored,
                  'missing_samples': [row['rel_path'] for row in items[:20]], 'checked_at': now_iso()}
        self.save({**result, 'items': items})
        return result

    def _local_candidates(self, items):
        # Folder names identify gallery content. Site titles/slugs are not
        # unique identities across dates, so never match site originals by name.
        wanted = {self._local_key(row) for row in items} - {None}
        found = defaultdict(list)
        if not wanted:
            return found
        for kind in ('images', 'livephoto'):
            root = self.storage.config.data_dir / kind
            try:
                self.target(f'data/{kind}/probe/probe')
            except ValueError:
                continue
            for directory, names, filenames in os.walk(root, followlinks=False):
                parent = Path(directory)
                names[:] = [name for name in names if not name.startswith('.') and name not in ('trash', 'avatars')
                            and not (parent/name).is_symlink() and not (parent/name).is_junction()]
                for name in filenames:
                    key = (kind, parent.name, name)
                    if key not in wanted:
                        continue
                    path = parent/name
                    if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
                        continue
                    found[key].append(path)
        return found

    def _local_key(self, row):
        parts = Path(row.get('rel_path') or '').parts
        if row.get('folder_name') and len(parts) > 2 and parts[1] in ('images', 'livephoto'):
            return parts[1], row['folder_name'], row['filename']
        return None

    def _allowed_local(self, row, candidate):
        kind, name, filename = self._local_key(row)
        allowed = {self.storage.config.data_dir/kind/name/filename}
        with self.db.connect() as conn:
            mapped = conn.execute('select rel_dir from folder_locations where folder_name=? and media_type=?',
                                  (name, kind)).fetchone()
        if mapped:
            allowed.add(self.target(mapped['rel_dir']+'/'+filename))
        folder = self.db.get_folder(name)
        if folder:
            source = (f'xhs-{name.split("_", 2)[1]}' if name.startswith('xhs_') else
                      f'bilibili-{folder.get("subscription_uid") or "unknown"}')
            allowed.add(self.storage.layout_folder(kind, name, source, folder.get('pub_time') or name)/filename)
        return candidate.resolve() in {path.resolve() for path in allowed}

    def _download(self, row, target):
        if row.get('source_type') == 'xhs_likes':
            raise ValueError('需在小红书来源中重新同步，不能使用可能过期的历史下载地址')
        url = row.get('url') or ''
        if urlparse(url).scheme not in ('http', 'https'):
            raise ValueError('没有可用的原文件下载地址，请重新同步对应来源')
        settings = self.db.get_settings()
        proxies = None
        source = self.db.get_site_source(row['source_id']) if row.get('source_id') else None
        if settings.get('site_proxy_enabled') and (source is None or source.get('use_proxy', True)):
            proxy = f"http://{settings.get('site_proxy_host', '127.0.0.1')}:{int(settings.get('site_proxy_port', 7890))}"
            proxies = {'http': proxy, 'https': proxy}
        downloader = MediaDownloader(timeout=30, user_agent=str(settings.get('site_user_agent')), proxies=proxies)
        if row.get('post_url'):
            downloader.session.headers['Referer'] = row['post_url']
        try:
            downloader.download(url, target)
        finally:
            downloader.session.close()

    def _verify(self, path, media_type):
        if path.stat().st_size == 0:
            raise ValueError('下载返回空文件')
        if media_type == 'image':
            with Image.open(path) as image:
                image.verify()
            with Image.open(path) as image:
                image.load()
        elif media_type in ('video', 'livephoto'):
            ffmpeg = ThumbnailService()._resolve_ffmpeg()
            if not ffmpeg:
                raise ValueError('缺少视频校验工具，保留记录等待恢复')
            result = subprocess.run([ffmpeg, '-v', 'error', '-i', str(path), '-map', '0:v:0',
                                     '-frames:v', '1', '-an', '-f', 'null', '-'],
                                    capture_output=True, text=True, timeout=30)
            if result.returncode:
                raise ValueError('下载内容不是有效视频')
        else:
            raise ValueError('不支持此原文件类型，保留记录等待手动恢复')

    def _mark_ready(self, reference):
        if reference['table'] == 'site_assets':
            self.db.set_site_asset_result(reference['id'], 'ready', reference['rel_path'])
        else:
            with self.db.connect() as conn:
                conn.execute("update assets set status='ready' where id=?", (reference['id'],))

    def _reconcile_previous(self):
        # The complete missing report is durable before any file is restored.
        # A crash after atomic creation but before the DB update must reconcile
        # that file on explicit retry rather than download/overwrite it again.
        try:
            previous = self.load().get('items', [])
        except (OSError, ValueError):
            return []
        reconciled = []
        for row in previous:
            if not isinstance(row, dict):
                continue
            if row.get('status') == 'recovered' or row.get('source_type') == 'xhs_likes':
                continue
            if row.get('table') not in ('assets', 'site_assets'):
                continue
            try:
                target = self.target(row.get('rel_path'))
                if not target.is_file() or target.stat().st_size == 0:
                    continue
                with self.db.connect() as conn:
                    current = conn.execute(f'select * from {row["table"]} where id=?', (row['id'],)).fetchone()
                if (not current or current['status'] == 'duplicate'
                        or any(current[key] != row.get(key) for key in ('rel_path', 'filename', 'media_type'))
                        or (row['table'] == 'site_assets' and current['url'] != row.get('url'))):
                    continue
                self._verify(target, row['media_type'])
                self._mark_ready(row)
                row.update(status='recovered', method='reconciled', error=None)
            except Exception as exc:
                row.update(status='unresolved', error=str(exc))
            reconciled.append(row)
        return reconciled

    def repair(self, progress=lambda *_: None):
        previous = self._reconcile_previous()
        self.scan(lambda checked, missing: progress(0, 1, f'检查原文件：{checked} 条引用'))
        report = self.load()
        items = report['items']
        candidates = self._local_candidates(items) if items else {}
        by_path = defaultdict(list)
        for row in items:
            by_path[row['rel_path']].append(row)
        identities = {(row['table'], row['id']) for row in items}
        items.extend(row for row in previous if (row['table'], row['id']) not in identities)
        recovered = len({row['rel_path'] for row in previous if row['status'] == 'recovered'})
        for index, (rel_path, references) in enumerate(by_path.items(), 1):
            # Prefer a site record's original URL over its mirrored gallery row.
            row = next((item for item in references if item.get('url')), references[0])
            progress(index-1, len(by_path), rel_path or '无效路径')
            temp = None
            try:
                target = self.target(rel_path)
                if target.exists():
                    raise ValueError('目标已经存在，保留现场并重新检查')
                local = candidates.get(self._local_key(row), [])
                if len(local) > 1:
                    raise ValueError('发现多个同名原文件，需要手动选择，未覆盖目标')
                if local and not self._allowed_local(row, local[0]):
                    raise ValueError('同名原文件位于不同来源或日期，需要手动确认，未覆盖目标')
                target.parent.mkdir(parents=True, exist_ok=True)
                descriptor, name = tempfile.mkstemp(prefix='.recover-', suffix=target.suffix, dir=target.parent)
                os.close(descriptor)
                temp = Path(name)
                if local:
                    shutil.copyfile(local[0], temp)
                    method = 'local'
                else:
                    self._download(row, temp)
                    method = 'download'
                self._verify(temp, row['media_type'])
                with temp.open('r+b') as stream:
                    os.fsync(stream.fileno())
                # Atomic create without replacing another original. A filesystem
                # lacking hard links must fail safely instead of overwriting.
                os.link(temp, target)
                from app.services.data_upgrade import sync_directory
                sync_directory(target.parent)
                for reference in references:
                    self._mark_ready(reference)
                    reference.update(status='recovered', method=method, error=None)
                recovered += 1
            except Exception as exc:
                for reference in references:
                    reference.update(status='unresolved', error=str(exc))
            finally:
                if temp is not None:
                    temp.unlink(missing_ok=True)
                    temp.with_suffix(temp.suffix+'.part').unlink(missing_ok=True)
                self.save(report)
        remaining = [row for row in items if row['status'] != 'recovered']
        result = {key: value for key, value in report.items() if key != 'items'}
        result.update(missing_assets=len(remaining), missing_files=len({row['rel_path'] for row in remaining}),
                      missing_samples=[row['rel_path'] for row in remaining[:20]], recovered_files=recovered,
                      unresolved_files=len({row['rel_path'] for row in remaining}), checked_at=now_iso())
        self.save({**result, 'items': items})
        return result
