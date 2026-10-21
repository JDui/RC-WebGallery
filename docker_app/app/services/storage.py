from __future__ import annotations

import os
import shutil
from pathlib import Path

from app.config import AppConfig
from app.services.utils import date_key, loads_json, safe_slug
from app.services.task_priority import TaskPriorityCoordinator


class StorageService:
    def __init__(self, config: AppConfig, priority: TaskPriorityCoordinator | None = None) -> None:
        self.config = config
        self.priority = priority
        self.db = None

    def prepare_folder(self, folder_name: str, source: str, pub_date: str | None) -> None:
        """Allocate once; existing legacy paths stay valid until explicit migration."""
        if self.db is None:
            return
        with self.db.connect() as conn:
            enabled = conn.execute('select version from data_layout where id=1').fetchone()[0] >= 2
            if not enabled:
                return
            for media_type in ('images', 'livephoto'):
                legacy = self.config.data_dir / media_type / folder_name
                target = legacy if legacy.exists() else self.layout_folder(media_type, folder_name, source, pub_date)
                conn.execute('insert or ignore into folder_locations values(?,?,?)',
                             (folder_name, media_type, self.relative_to_storage(target)))

    def layout_folder(self, media_type: str, folder_name: str, source: str, pub_date: str | None) -> Path:
        if Path(folder_name).name != folder_name or folder_name in ('.', '..'):
            raise ValueError('非法内容目录')
        return self.config.data_dir / media_type / safe_slug(source, 'unknown') / date_key(pub_date) / folder_name

    def _media_folder(self, media_type: str, folder_name: str) -> Path:
        if Path(folder_name).name != folder_name or folder_name in ('.', '..'):
            raise ValueError('非法内容目录')
        if self.db is not None:
            with self.db.connect() as conn:
                row = conn.execute('select rel_dir from folder_locations where folder_name=? and media_type=?',
                                   (folder_name, media_type)).fetchone()
            if row:
                self.resolve_storage_path(row[0])  # validate containment before use
                return self.config.storage_root / row[0]
        return self.config.data_dir / media_type / folder_name

    def media_folder_names(self) -> set[str]:
        names = set()
        mapped_sources = {'images': set(), 'livephoto': set()}
        if self.db is not None:
            with self.db.connect() as conn:
                for row in conn.execute('select * from folder_locations'):
                    path = self.resolve_storage_path(row['rel_dir'])
                    if path.is_dir():
                        names.add(row['folder_name'])
                    parts = Path(row['rel_dir']).parts
                    if len(parts) > 3:
                        mapped_sources[row['media_type']].add(parts[2])
        for media_type in mapped_sources:
            root = self.config.data_dir / media_type
            if root.exists():
                names.update(p.name for p in root.iterdir() if p.is_dir() and p.name not in mapped_sources[media_type] and not p.name.startswith(('bilibili-', 'xhs-')))
        return names

    def ensure(self) -> None:
        self.config.config_dir.mkdir(parents=True, exist_ok=True)
        self.config.images_dir.mkdir(parents=True, exist_ok=True)
        self.config.livephoto_dir.mkdir(parents=True, exist_ok=True)

    def image_folder(self, folder_name: str) -> Path:
        return self._media_folder('images', folder_name)

    def livephoto_folder(self, folder_name: str) -> Path:
        return self._media_folder('livephoto', folder_name)

    def site_post_folder(self, source_slug: str, pub_date: str | None, post_slug: str) -> Path:
        folder = (
            self.config.data_dir
            / "sites"
            / safe_slug(source_slug, "source")
            / date_key(pub_date)
            / safe_slug(post_slug, "post")
        )
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def remove_site_source_assets(self, source_slug: str) -> int:
        self._background_checkpoint()
        folder = self.config.data_dir / "sites" / safe_slug(source_slug, "source")
        if not folder.exists():
            return 0
        removed = sum(1 for item in folder.rglob("*") if item.is_file())
        shutil.rmtree(folder)
        return removed

    def remove_folder_assets(self, folder_name: str) -> int:
        removed = 0
        for folder in (self.image_folder(folder_name), self.livephoto_folder(folder_name)):
            self._background_checkpoint()
            if folder.exists():
                shutil.rmtree(folder)
                removed += 1
        return removed

    def clear_folder_thumbnail_derivatives(self, folder_name: str) -> int:
        removed = 0
        for folder in (self.image_folder(folder_name), self.livephoto_folder(folder_name)):
            self._background_checkpoint()
            thumbs = folder / ".thumbs"
            if not thumbs.exists():
                continue
            for target in thumbs.rglob("*.webp"):
                if target.is_file():
                    target.unlink(missing_ok=True)
                    removed += 1
        return removed

    def clear_thumbnail_level(self, level: str) -> int:
        targets = {
            "thumb": lambda thumbs: [item for item in thumbs.glob("*.webp") if item.is_file()],
            "small": lambda thumbs: [item for item in (thumbs / "small").glob("*.webp") if item.is_file()],
        }
        if level not in targets:
            raise ValueError("unsupported thumbnail level")
        removed = 0
        for root in (self.config.images_dir, self.config.livephoto_dir, self.config.data_dir / "sites"):
            if not root.exists():
                continue
            for thumbs in root.rglob(".thumbs"):
                self._background_checkpoint()
                if not thumbs.is_dir():
                    continue
                for target in targets[level](thumbs):
                    self._background_checkpoint()
                    target.unlink(missing_ok=True)
                    removed += 1
        return removed

    def resolve_storage_path(self, rel_path: str | None) -> Path | None:
        if not rel_path:
            return None
        root = self.config.storage_root.resolve()
        path = (root / rel_path).resolve()
        if not path.is_relative_to(root):
            raise ValueError('媒体路径超出 storage 目录')
        return path

    def remove_asset_files(self, asset: dict) -> int:
        removed = 0
        for key in ("rel_path", "thumb_rel_path", "small_thumb_rel_path", "tiny_thumb_rel_path", "cover_rel_path", "reverse_rel_path"):
            self._background_checkpoint()
            path = self.resolve_storage_path(asset.get(key))
            if path and path.exists():
                path.unlink(missing_ok=True)
                removed += 1
        if asset.get("media_type") == "livephoto":
            live_folder = self.livephoto_folder(asset["folder_name"])
            source_cover = live_folder / ".source_covers" / f"{Path(asset['filename']).stem}.jpg"
            if source_cover.exists():
                source_cover.unlink(missing_ok=True)
                removed += 1
        return removed

    def clear_library_data(self) -> dict[str, int]:
        removed = {"images": 0, "livephoto": 0}
        for key, folder in (("images", self.config.images_dir), ("livephoto", self.config.livephoto_dir)):
            self._background_checkpoint()
            if folder.exists():
                children = list(folder.iterdir())
                removed[key] = len(children)
                shutil.rmtree(folder)
            folder.mkdir(parents=True, exist_ok=True)
        return removed

    def relative_to_storage(self, path: Path) -> str:
        return path.resolve().relative_to(self.config.storage_root.resolve()).as_posix()

    def storage_url(self, path: str | None) -> str | None:
        if not path:
            return None
        return f"/storage/{path}"

    def storage_usage_stats(self, trash_items: list[dict] | None = None) -> dict[str, int]:
        image_files = self._files_under(self.config.images_dir, include_thumbnails=False)
        image_files.extend(self._files_under(self.config.livephoto_dir, include_thumbnails=False))
        image_files.extend(self._files_under(self.config.data_dir / "sites", include_thumbnails=False))
        thumbnail_files = self._thumbnail_files_under(self.config.images_dir)
        thumbnail_files.extend(self._thumbnail_files_under(self.config.livephoto_dir))
        thumbnail_files.extend(self._thumbnail_files_under(self.config.data_dir / 'sites'))
        trash_files = self.trash_asset_files(trash_items or [])
        garbage = set(trash_files)
        image_files = [p for p in image_files if p not in garbage]
        thumbnail_files = [p for p in thumbnail_files if p not in garbage]
        return {
            "image_bytes": self._sum_file_sizes(image_files),
            "thumbnail_bytes": self._sum_file_sizes(thumbnail_files),
            "trash_bytes": self._sum_file_sizes(trash_files),
            "image_files": len(image_files),
            "thumbnail_files": len(thumbnail_files),
            "trash_files": len(trash_files),
        }

    def cleanup_trash_asset_files(self, trash_items: list[dict]) -> dict[str, int]:
        files = self.trash_asset_files(trash_items)
        removed_bytes = self._sum_file_sizes(files)
        removed_files = 0
        for path in files:
            self._background_checkpoint()
            if path.exists() and path.is_file():
                path.unlink(missing_ok=True)
                removed_files += 1
        if self.db is not None:
            with self.db.connect() as conn:
                conn.execute('update assets set tiny_thumb_rel_path=null where tiny_thumb_rel_path is not null')
                conn.execute("update assets set thumb_rel_path=small_thumb_rel_path where thumb_rel_path like '%/.thumbs/%.webp'")
        return {"removed_files": removed_files, "removed_bytes": removed_bytes}

    def trash_asset_files(self, trash_items: list[dict]) -> list[Path]:
        files: list[Path] = []
        seen: set[Path] = set()
        protected = set()
        if self.db is not None:
            # Deleted records may share originals and retained small thumbnails
            # with live records. Retired thumb/tiny paths are garbage unless
            # also referenced as an original, cover or avatar.
            with self.db.connect() as conn:
                for query in (
                    'select rel_path,small_thumb_rel_path,cover_rel_path,reverse_rel_path from assets',
                    'select image_path,video_path from livephoto_pairs',
                    'select rel_path from site_assets where rel_path is not null',
                ):
                    for row in conn.execute(query):
                        for value in row:
                            if value:
                                protected.add(self.resolve_storage_path(value))
            for item in [*self.db.list_subscriptions(), *self.db.list_site_sources()]:
                for key in ('avatar_url', 'avatar_tiny_url', 'icon_url', 'icon_tiny_url'):
                    url = item.get(key) or ''
                    if url.startswith('/storage/'):
                        protected.add(self.resolve_storage_path(url[len('/storage/'):].split('?')[0]))

        def collect(path):
            # Preserve the final symlink as a link (unlinking it is harmless),
            # and never traverse a directory link to enumerate/delete targets.
            root = self.config.storage_root.resolve()
            path = Path(os.path.abspath(root / path))
            for parent in path.parents:
                if parent == root:
                    break
                if parent.is_symlink() or parent.is_junction():
                    return
            # Expand Windows 8.3 parent aliases without resolving the final
            # symlink, which must remain the link we would unlink.
            canonical = path.parent.resolve() / path.name
            if not canonical.is_relative_to(root):
                return
            if (path.is_file() or path.is_symlink()) and path.resolve() not in protected and path not in seen:
                seen.add(path)
                files.append(path)

        for item in trash_items:
            self._background_checkpoint()
            for asset in loads_json(item.get("assets_json"), []):
                self._background_checkpoint()
                for key in ("rel_path", "thumb_rel_path", "small_thumb_rel_path", "tiny_thumb_rel_path", "cover_rel_path", "reverse_rel_path"):
                    if asset.get(key):
                        collect(asset[key])
        # Only media derivatives: avatar/icon tiny files live outside .thumbs and
        # remain protected even if a legacy installation stored one here.
        for root in (self.config.images_dir, self.config.livephoto_dir, self.config.data_dir / 'sites'):
            if not root.exists():
                continue
            for thumbs in root.rglob('.thumbs'):
                if thumbs.is_dir() and not thumbs.is_symlink() and not thumbs.is_junction():
                    for path in thumbs.glob('*.webp'):
                        self._background_checkpoint()
                        collect(path)
            for tiny in root.rglob('tiny'):
                if tiny.is_dir() and not tiny.is_symlink() and not tiny.is_junction() and tiny.parent.name == '.thumbs':
                    for path in tiny.rglob('*'):
                        self._background_checkpoint()
                        collect(path)
        return files

    def _files_under(self, root: Path, include_thumbnails: bool) -> list[Path]:
        if not root.exists():
            return []
        files = []
        for path in root.rglob("*"):
            self._background_checkpoint()
            if not path.is_file():
                continue
            parts = set(path.parts)
            is_thumb = ".thumbs" in parts or ".source_covers" in parts
            if include_thumbnails != is_thumb:
                continue
            files.append(path)
        return files

    def _thumbnail_files_under(self, root: Path) -> list[Path]:
        return self._files_under(root, include_thumbnails=True)

    def _sum_file_sizes(self, files: list[Path]) -> int:
        total = 0
        for path in files:
            self._background_checkpoint()
            try:
                total += path.stat().st_size
            except FileNotFoundError:
                continue
        return total

    def _background_checkpoint(self) -> None:
        if self.priority is not None:
            self.priority.background_checkpoint()
