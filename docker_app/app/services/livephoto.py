"""In-place, lossless metadata pairing; historical media is only changed on request."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from uuid import uuid4

from PIL import Image

from app.services.utils import loads_json, now_iso
from app.services.database_safety import sync_file, sync_directory, durable_replace

IMAGES = {'.jpg', '.jpeg', '.png', '.webp', '.avif', '.heic', '.heif'}
VIDEOS = {'.mp4', '.mov', '.m4v'}
PREFIX = re.compile(r'^(\d{3,})__')


class LivePhotoError(RuntimeError):
    pass


class LivePhotoService:
    def __init__(self, db, storage):
        self.db, self.storage = db, storage

    def _run(self, args):
        executable = os.environ.get('APP_EXIFTOOL_BIN') or shutil.which('exiftool')
        if not executable:
            raise LivePhotoError('缺少 ExifTool，请更新包含 Live Photo 配对工具的容器镜像')
        try:
            result = subprocess.run([executable, *map(str, args)], capture_output=True,
                                    text=True, encoding='utf-8', errors='replace', timeout=180)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise LivePhotoError(f'Live Photo 元数据处理失败：{exc}') from exc
        if result.returncode:
            raise LivePhotoError((result.stderr or result.stdout or '元数据工具失败').strip()[:500])
        return result.stdout

    def read_identifiers(self, paths):
        # Deliberately read WITHOUT our config, just as Immich does.
        rows = json.loads(self._run(['-j', '-ContentIdentifier', '-MediaGroupUUID', *paths]))
        return [row.get('ContentIdentifier') or row.get('MediaGroupUUID') for row in rows]

    def record(self, folder_name, pair_index):
        with self.db.connect() as conn:
            row = conn.execute('select * from livephoto_pairs where folder_name=? and pair_index=?',
                               (folder_name, pair_index)).fetchone()
        return dict(row) if row else None

    def _save(self, folder_name, pair_index, **values):
        with self.db.connect() as conn:
            conn.execute('''insert or ignore into livephoto_pairs
                (folder_name,pair_index,content_id,status,updated_at) values (?,?,?,'pending',?)''',
                         (folder_name, pair_index, str(uuid4()), now_iso()))
            values['updated_at'] = now_iso()
            conn.execute('update livephoto_pairs set ' + ','.join(f'{key}=?' for key in values)
                         + ' where folder_name=? and pair_index=?',
                         (*values.values(), folder_name, pair_index))
        return self.record(folder_name, pair_index)

    def pairs(self, folder_name):
        folder = self.db.get_folder(folder_name) or {}
        metadata = loads_json(folder.get('metadata_json'), {})
        if metadata.get('standalone_video'):
            return []
        # Explicit filename prefixes take precedence; old unprefixed archives
        # retain the indexer's existing pairing instead of guessing by sorting.
        indexed = {(a['media_type'], a['filename']): int(a['pair_index'])
                   for a in self.db.list_assets_for_folder(folder_name)}
        confirmed = self.confirmed_indices(folder_name)
        groups = {}
        for kind, root, suffixes in (
            ('image', self.storage.image_folder(folder_name), IMAGES),
            ('video', self.storage.livephoto_folder(folder_name), VIDEOS),
        ):
            if not root.exists():
                continue
            for path in sorted(root.iterdir()):
                if not path.is_file() or path.name.startswith('.') or path.suffix.lower() not in suffixes:
                    continue
                match = PREFIX.match(path.name)
                index = confirmed.get(self.storage.relative_to_storage(path))
                if index is None:
                    index = int(match[1]) if match else indexed.get(('livephoto' if kind == 'video' else kind, path.name))
                key = index if index is not None else f'unindexed:{path.name}'
                groups.setdefault(key, {'index': index, 'images': [], 'videos': []})[kind + 's'].append(path)
        result = [group for group in groups.values() if group['videos']]
        for group in result:
            if len(group['images']) != 1 or len(group['videos']) != 1:
                continue
            image, video = group['images'][0], group['videos'][0]
            if (confirmed.get(self.storage.relative_to_storage(video)) == group['index']
                    or PREFIX.match(video.name) or image.stem == video.stem):
                continue
            # Legacy indexes may fall back to sort order or a nearest-image
            # match. That is sufficient for preview, but not for writing a
            # permanent relationship into historical media.
            cover = video.parent / '.source_covers' / f'{video.stem}.jpg'
            try:
                with Image.open(image) as still, Image.open(cover) as source:
                    certain = still.size == source.size and still.convert('RGB').tobytes() == source.convert('RGB').tobytes()
            except (OSError, ValueError):
                certain = False
            if not certain:
                group['ambiguous'] = True
        return result

    def confirmed_indices(self, folder_name):
        with self.db.connect() as conn:
            rows = conn.execute('select pair_index,image_path,video_path from livephoto_pairs '
                                'where folder_name=? and confirmed_pair=1', (folder_name,)).fetchall()
        return {row[key]: int(row['pair_index']) for row in rows for key in ('image_path', 'video_path') if row[key]}

    @staticmethod
    def issue_code(group):
        if not group['images']:
            return 'missing_image'
        if len(group['images']) != 1 or len(group['videos']) != 1:
            return 'multiple_candidates'
        if group['index'] is None or group.get('ambiguous'):
            return 'uncertain_pair'
        return None

    @staticmethod
    def target(folder_name, group):
        return {'folder_name': folder_name, 'pair_index': group['index'],
                'image_files': [p.name for p in group['images']], 'video_files': [p.name for p in group['videos']]}

    @staticmethod
    def checksum(path):
        # Used for the existing XHS integrity check: metadata edits necessarily
        # change the archive checksum, while the downloaded cache stays original.
        digest = hashlib.sha256()
        with Path(path).open('rb') as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    def archive_checksum(self, folder_name, pair_index, path):
        row = self.record(folder_name, pair_index)
        if not row or row['status'] != 'ready':
            return None
        relative = self.storage.relative_to_storage(path)
        if relative == row['image_path']:
            return row['image_checksum']
        if relative == row['video_path']:
            return row['video_checksum']
        return None

    def _recover(self, row):
        journal = loads_json(row.get('transaction_json'), [])
        expected = {self.storage.resolve_storage_path(row[key]).resolve() for key in ('image_path', 'video_path')}
        targets = {Path(entry['target']).resolve() for entry in journal}
        if len(journal) != 2 or targets != expected:
            raise LivePhotoError('恢复日志与当前配对路径不一致，已停止自动恢复')
        for entry in journal:
            backup, target = Path(entry['backup']), Path(entry['target'])
            if not backup.parent.name.startswith('.livephoto-') or backup.parent.parent.resolve() != target.parent.resolve():
                raise LivePhotoError('恢复备份路径无效，已停止自动恢复')
            if not backup.is_file():
                raise LivePhotoError(f'上次写入中断，恢复文件缺失：{backup}')
        for entry in journal:
            backup, target = Path(entry['backup']), Path(entry['target'])
            shutil.copy2(backup, target)
            sync_file(target)
            sync_directory(target.parent)
        self._save(row['folder_name'], row['pair_index'], status='pending', transaction_json=None)
        for entry in journal:
            shutil.rmtree(Path(entry['backup']).parent, ignore_errors=True)

    def normalize_pair(self, folder_name, group):
        if group.get('ambiguous') or group['index'] is None or len(group['images']) != 1 or len(group['videos']) != 1:
            raise LivePhotoError('图片缺失或配对存在歧义，已保留原文件')
        index = group['index']
        image, video = group['images'][0], group['videos'][0]
        row = self.record(folder_name, index)
        if row and row.get('transaction_json'):
            self._recover(row)
        identifiers = self.read_identifiers([image, video])
        # Preserve existing valid identifiers, but only for unmanaged archives;
        # managed pairs must keep their persisted ID even after file replacement.
        cid = row['content_id'] if row else (identifiers[0] if identifiers[0] and identifiers[0] == identifiers[1] else str(uuid4()))
        if not row:
            with self.db.connect() as conn:
                used = conn.execute('select 1 from livephoto_pairs where content_id=?', (cid,)).fetchone()
            if used:
                cid = str(uuid4())
        row = self._save(folder_name, index, content_id=cid,
                         image_path=self.storage.relative_to_storage(image),
                         video_path=self.storage.relative_to_storage(video))
        if identifiers == [cid, cid] and row['status'] == 'ready':
            return False
        work = []
        journal = []
        try:
            if identifiers != [cid, cid]:
                for path in (image, video):
                    directory = Path(tempfile.mkdtemp(prefix='.livephoto-', dir=path.parent))
                    work.append(directory)
                    staged, backup = directory / path.name, directory / 'original.bin'
                    shutil.copy2(path, staged)
                    shutil.copy2(path, backup)
                    journal.append({'target': str(path), 'backup': str(backup), 'staged': str(staged)})
                self._run(['-config', Path(__file__).with_name('livephoto.config'), '-overwrite_original',
                           '-P', f'-XMP-rcgallery:ContentIdentifier={cid}', journal[0]['staged']])
                # MP4/MOV native QuickTime key; no video transcoding.
                self._run(['-config', Path(__file__).with_name('livephoto.config'), '-overwrite_original',
                           '-P', f'-Keys:ContentIdentifier={cid}', f'-XMP-rcgallery:ContentIdentifier={cid}',
                           journal[1]['staged']])
                if self.read_identifiers([entry['staged'] for entry in journal]) != [cid, cid]:
                    raise LivePhotoError('配对标识回读失败，已保留原文件')
                # ExifTool -P may truncate subsecond filesystem timestamps.
                # Restore the original precision after metadata readback.
                for entry in journal:
                    original_stat = Path(entry['backup']).stat()
                    os.utime(entry['staged'], ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
                    sync_file(entry['backup'])
                    sync_file(entry['staged'])
                    sync_directory(Path(entry['backup']).parent)
                    sync_directory(Path(entry['target']).parent)
                self._save(folder_name, index, status='writing', transaction_json=json.dumps(journal))
                for entry in journal:
                    durable_replace(entry['staged'], entry['target'])
            # XHS downloads carry checksums; retain their archive integrity check.
            checksums = {'image_checksum': None, 'video_checksum': None}
            if folder_name.startswith('xhs_'):
                checksums = {'image_checksum': self.checksum(image), 'video_checksum': self.checksum(video)}
            self._save(folder_name, index, status='ready', version=1, error=None,
                       transaction_json=None, **checksums)
            return identifiers != [cid, cid]
        except Exception as exc:
            current = self.record(folder_name, index)
            if current.get('transaction_json'):
                self._recover(current)
            self._save(folder_name, index, status='failed', error=str(exc)[:500])
            raise
        finally:
            # A failed recovery must retain its journal and backups for retry.
            current = self.record(folder_name, index)
            if not current.get('transaction_json'):
                for directory in work:
                    shutil.rmtree(directory, ignore_errors=True)

    def normalize_download(self, folder_name, changed_paths):
        changed = {str(Path(path)) for path in changed_paths}
        errors = []
        for group in self.pairs(folder_name):
            row = self.record(folder_name, group['index']) if group['index'] is not None else None
            touched = any(str(path) in changed for path in group['images'] + group['videos'])
            if not touched and not (row and row['auto_pair'] and row['status'] != 'ready'):
                continue  # Never silently migrate historical pairs on rescan.
            try:
                self.normalize_pair(folder_name, group)
                if touched:
                    self._save(folder_name, group['index'], auto_pair=1)
            except Exception as exc:
                if group['index'] is not None:
                    self._save(folder_name, group['index'], status='failed', error=str(exc)[:500],
                               auto_pair=1 if touched else int(row['auto_pair']))
                errors.append(f"第 {group['index']} 组：{exc}")
        if errors:
            raise LivePhotoError('实况已归档，配对未完成，可在设置中修复：' + '；'.join(errors[:3]))

    def maintain(self, repair=False, cooperate=None, progress=None, stats=None, folders=None, targets=None):
        stats = stats if stats is not None else {}
        stats.update(checked=0, paired=0, repaired=0, needs_repair=0, repairable=0, unpaired=0, errors=0,
                     issues=[], repairable_targets=[], exception_targets=[], completed=False)
        names = {folder['folder_name'] for folder in self.db.list_folders()}
        names.update(name for name in self.storage.media_folder_names() if not name.startswith('.'))
        if folders is not None:
            names.intersection_update(folders)
        stats.update(scan_scope='all' if folders is None and targets is None else 'subset',
                     scanned_folders=len(names))
        groups = []
        for name in sorted(names):
            if cooperate:
                cooperate()
            if progress:
                progress(0, 0, f'扫描 {name}', stats)
            for group in self.pairs(name):
                if targets is None or any(t['folder_name'] == name and t.get('pair_index') == group['index']
                        and (not t.get('video_files') or t['video_files'] == [p.name for p in group['videos']]) for t in targets):
                    groups.append((name, group))
        for done, (name, group) in enumerate(groups, 1):
            if cooperate:
                cooperate()
            stats['checked'] += 1
            try:
                code = self.issue_code(group)
                if code:
                    stats['unpaired'] += 1
                    stats['needs_repair'] += 1
                    stats['exception_targets'].append(self.target(name, group))
                    raise LivePhotoError({'missing_image': '缺少对应原图', 'multiple_candidates': '同组有多张图片或多个视频',
                                          'uncertain_pair': '旧文件的图片与视频关系尚未确认'}[code])
                row = self.record(name, group['index'])
                ids = self.read_identifiers(group['images'] + group['videos'])
                valid = bool(ids[0] and ids[0] == ids[1] and not (row and row.get('transaction_json'))
                             and (not row or row['content_id'] == ids[0]))
                if valid:
                    stats['paired'] += 1
                else:
                    stats['needs_repair'] += 1
                    stats['repairable'] += 1
                    stats['repairable_targets'].append(self.target(name, group))
                if repair and (not valid or not row or row['status'] != 'ready'):
                    self.normalize_pair(name, group)
                    stats['repaired'] += 1
                    if not valid:
                        stats['needs_repair'] -= 1
                        stats['repairable'] -= 1
                        stats['repairable_targets'].pop()
                        stats['paired'] += 1
            except Exception as exc:
                stats['errors'] += 1
                if len(stats['issues']) < 50:
                    stats['issues'].append({**self.target(name, group), 'code': self.issue_code(group) or 'metadata_error',
                                            'error': str(exc)[:500]})
            if progress:
                progress(done, len(groups), f"{name} / {group['index'] or '?'}", stats)
        stats['completed'] = True
        return stats
