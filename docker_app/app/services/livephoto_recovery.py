"""Recover original stills and record explicitly confirmed historical pairs."""
from pathlib import Path
import os
import shutil
from uuid import uuid4

from PIL import Image

from app.services.livephoto import IMAGES, VIDEOS, PREFIX, LivePhotoError


class LivePhotoRecovery:
    def __init__(self, service, restore_source=None):
        self.service = service
        self.db, self.storage = service.db, service.storage
        self.restore_source = restore_source

    def _root(self, folder_name, kind):
        root = self.storage.image_folder(folder_name) if kind == 'image' else self.storage.livephoto_folder(folder_name)
        base = self.storage.config.images_dir if kind == 'image' else self.storage.config.livephoto_dir
        if root.resolve().parent != base.resolve() or root.is_symlink():
            raise LivePhotoError('媒体目录无效')
        return root

    def _path(self, folder_name, relative, kind):
        path = self.storage.resolve_storage_path(relative)
        root = self._root(folder_name, kind)
        if (not path or path.is_symlink() or path.resolve().parent != root.resolve()
                or not path.is_file() or path.suffix.lower() not in (IMAGES if kind == 'image' else VIDEOS)):
            raise LivePhotoError('所选文件不存在或不属于此贴文，请重新检查')
        return path

    def _images(self, name):
        root = self._root(name, 'image')
        return sorted(p for p in root.iterdir() if p.is_file() and not p.is_symlink()
                      and not p.name.startswith('.') and p.suffix.lower() in IMAGES) if root.exists() else []

    def _available_images(self, name, group):
        protected = {image for other in self.service.pairs(name)
                     if not self.service.issue_code(other) and other != group for image in other['images']}
        return [p for p in self._images(name) if p not in protected]

    def options(self, targets):
        rows = []
        for name in sorted({t['folder_name'] for t in targets}):
            for group in self.service.pairs(name):
                if not self.service.issue_code(group) or not self._matches(name, group, targets):
                    continue
                def item(path):
                    relative = self.storage.relative_to_storage(path)
                    return {'path': relative, 'name': path.name, 'url': self.storage.storage_url(relative)}
                rows.append({**self.service.target(name, group), 'code': self.service.issue_code(group),
                             'images': [item(p) for p in self._available_images(name, group)],
                             'videos': [item(p) for p in group['videos']]})
        return rows

    @staticmethod
    def _matches(name, group, targets):
        return any(t['folder_name'] == name and t.get('pair_index') == group['index']
                   and (not t.get('video_files') or t['video_files'] == [p.name for p in group['videos']]) for t in targets)

    def validate_choices(self, choices, targets):
        if not isinstance(choices, list) or not 1 <= len(choices) <= 100:
            raise LivePhotoError('请选择 1–100 组配对')
        used = set()
        for choice in choices:
            if not isinstance(choice, dict) or set(choice) != {'folder_name', 'image_path', 'video_path'}:
                raise LivePhotoError('配对选择格式无效')
            name = choice['folder_name']
            image = self._path(name, choice['image_path'], 'image')
            video = self._path(name, choice['video_path'], 'video')
            group = next((g for g in self.service.pairs(name) if video in g['videos']), None)
            if (not group or not self._matches(name, group, targets) or not self.service.issue_code(group)
                    or image not in self._available_images(name, group)):
                raise LivePhotoError('所选配对已变化或图片已有确定配对，请刷新异常列表')
            if image in used or video in used:
                raise LivePhotoError('同一文件不能用于多组配对')
            used.update((image, video))

    def confirm(self, name, image, video):
        # A separate logical index preserves original filenames and other pairs.
        # Keep the assignment even if metadata writing fails so retry can recover.
        mapping = self.service.confirmed_indices(name)
        existing = mapping.get(self.storage.relative_to_storage(video))
        if existing is None:
            indices = [g['index'] or 0 for g in self.service.pairs(name)]
            with self.db.connect() as conn:
                largest = conn.execute('select max(pair_index) from livephoto_pairs where folder_name=?', (name,)).fetchone()[0]
            existing = max([largest or 0, *indices], default=0) + 1
        self.service._save(name, existing, confirmed_pair=1,
                           image_path=self.storage.relative_to_storage(image), video_path=self.storage.relative_to_storage(video))
        self.service.normalize_pair(name, {'index': existing, 'images': [image], 'videos': [video]})

    def manual(self, choices, targets, cooperate=None):
        self.validate_choices(choices, targets)
        changed = set()
        errors = []
        for choice in choices:
            if cooperate:
                cooperate()
            try:
                name = choice['folder_name']
                self.confirm(name, self._path(name, choice['image_path'], 'image'), self._path(name, choice['video_path'], 'video'))
                changed.add(name)
            except LivePhotoError as exc:
                errors.append({'folder_name': choice['folder_name'], 'error': str(exc)[:500]})
        return changed, errors

    def _original(self, name, group, video):
        covers = self._root(name, 'video') / '.source_covers'
        if covers.is_dir() and not covers.is_symlink():
            found = [p for p in covers.iterdir() if p.is_file() and not p.is_symlink()
                     and p.stem == video.stem and p.suffix.lower() in IMAGES]
            if len(found) == 1:
                return found[0]
        # XHS keeps the untouched download cache separately from gallery copies.
        match = __import__('re').fullmatch(r'xhs_([0-9]+)_([A-Za-z0-9_-]+)', name)
        if match and group['index'] is not None:
            key = f'urn:xhs:{match[1]}:{match[2]}:{group["index"] - 1}:image'
            with self.db.connect() as conn:
                row = conn.execute("select a.rel_path from site_assets a join xhs_like_state l on l.site_post_id=a.post_id "
                                   "where l.source_id=? and l.note_id=? and a.url=? and a.status='ready'",
                                   (int(match[1]), match[2], key)).fetchone()
            if row and row[0]:
                path = self.storage.resolve_storage_path(row[0])
                cache = self.storage.config.data_dir / 'sites'
                if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(cache.resolve()):
                    return path
        return self.restore_source(name, video) if self.restore_source else None

    def _certain(self, video, images, original):
        same_name = [p for p in images if PREFIX.sub('', p.stem) == PREFIX.sub('', video.stem)]
        if len(same_name) == 1:
            return same_name[0]
        if original:
            with Image.open(original) as cover:
                pixels, size = cover.convert('RGB').tobytes(), cover.size
            exact = []
            for path in images:
                with Image.open(path) as image:
                    if image.size == size and image.convert('RGB').tobytes() == pixels:
                        exact.append(path)
            if len(exact) == 1:
                return exact[0]
        if images:
            ids = self.service.read_identifiers([video, *images])
            matches = [image for image, value in zip(images, ids[1:]) if ids[0] and value == ids[0]]
            if len(matches) == 1:
                return matches[0]
        return None

    def automatic(self, targets, cooperate=None, progress=None):
        changed, errors = set(), []
        count = 0
        for name in sorted({t['folder_name'] for t in targets}):
            for group in self.service.pairs(name):
                if not self._matches(name, group, targets) or not self.service.issue_code(group):
                    continue
                if cooperate:
                    cooperate()
                if progress:
                    progress(name, group['index'])
                try:
                    # Multiple videos sharing a number need explicit selection.
                    if len(group['videos']) != 1:
                        continue
                    video = self._path(name, self.storage.relative_to_storage(group['videos'][0]), 'video')
                    images = self._available_images(name, group)
                    original = self._original(name, group, video)
                    image = self._certain(video, images, original)
                    if image is None and not group['images'] and original:
                        with Image.open(original) as source:
                            source.verify()
                        root = self._root(name, 'image')
                        root.mkdir(parents=True, exist_ok=True)
                        image = root / f'{video.stem}-original{original.suffix.lower()}'
                        if image.exists():
                            raise LivePhotoError('原图恢复目标已存在，需人工确认')
                        staged = root / f'.livephoto-restore-{uuid4().hex}'
                        try:
                            shutil.copy2(original, staged)
                            os.replace(staged, image)
                        finally:
                            staged.unlink(missing_ok=True)
                    if image is not None:
                        self.confirm(name, image, video)
                        changed.add(name)
                        count += 1
                except Exception as exc:
                    errors.append({**self.service.target(name, group), 'error': str(exc)[:500]})
        return changed, count, errors
