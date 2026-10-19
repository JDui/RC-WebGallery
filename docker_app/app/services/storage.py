from __future__ import annotations

import shutil
from pathlib import Path

from app.config import AppConfig
from app.services.utils import date_key, loads_json, safe_slug
from app.services.task_priority import TaskPriorityCoordinator


class StorageService:
    def __init__(self, config: AppConfig, priority: TaskPriorityCoordinator | None = None) -> None:
        self.config = config
        self.priority = priority

    def ensure(self) -> None:
        self.config.config_dir.mkdir(parents=True, exist_ok=True)
        self.config.images_dir.mkdir(parents=True, exist_ok=True)
        self.config.livephoto_dir.mkdir(parents=True, exist_ok=True)

    def image_folder(self, folder_name: str) -> Path:
        return self.config.images_dir / folder_name

    def livephoto_folder(self, folder_name: str) -> Path:
        return self.config.livephoto_dir / folder_name

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
        return self.config.storage_root / rel_path

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
        trash_files = self.trash_asset_files(trash_items or [])
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
        return {"removed_files": removed_files, "removed_bytes": removed_bytes}

    def trash_asset_files(self, trash_items: list[dict]) -> list[Path]:
        files: list[Path] = []
        seen: set[Path] = set()
        for item in trash_items:
            self._background_checkpoint()
            for asset in loads_json(item.get("assets_json"), []):
                self._background_checkpoint()
                for key in ("rel_path", "thumb_rel_path", "small_thumb_rel_path", "tiny_thumb_rel_path", "cover_rel_path", "reverse_rel_path"):
                    path = self.resolve_storage_path(asset.get(key))
                    if path and path.exists() and path.is_file() and path not in seen:
                        seen.add(path)
                        files.append(path)
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
