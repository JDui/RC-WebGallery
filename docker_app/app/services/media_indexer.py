from __future__ import annotations

import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

from PIL import Image

from app.db import Database
from app.services.image_similarity import image_fingerprint
from app.services.sidecar import SidecarRunner
from app.services.storage import StorageService
from app.services.thumb_chain import INDEX_PREVIEW_LEVELS, INDEX_THUMB_LEVELS, pick_thumb_url
from app.services.livephoto import LivePhotoService
from app.services.thumbnailer import ThumbnailService
from app.services.task_priority import TaskPriorityCoordinator
from app.services.utils import dumps_json, format_pub_time, loads_json, now_iso


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".avif"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".webm"}
PAIR_PREFIX_RE = re.compile(r"^(?P<index>\d{3})__")
THUMBNAIL_WORKERS_ENV = "APP_THUMBNAIL_WORKERS"
DEFAULT_THUMBNAIL_WORKERS = 3
ProgressCallback = Callable[[dict[str, Any]], None]
CooperateCallback = Callable[[], None]


class MediaIndexer:
    def __init__(
        self,
        db: Database,
        storage: StorageService,
        thumbnailer: ThumbnailService,
        priority: TaskPriorityCoordinator | None = None,
    ) -> None:
        self.db = db
        self.storage = storage
        self.thumbnailer = thumbnailer
        self.priority = priority
        self.sidecar = SidecarRunner()
        self.livephotos = LivePhotoService(db, storage)

    def index_folder(
        self,
        folder_name: str,
        pub_ts: int,
        title: str,
        text_prefix: str,
        top_dynamic_id: str,
        source_dynamic_id: str,
        subscription_uid: str = "",
        subscription_name: str | None = None,
        review_status: str = "approved",
        review_reason: str | None = None,
        generate_derivatives: bool = True,
        metadata: dict | None = None,
    ) -> None:
        image_folder = self.storage.image_folder(folder_name)
        livephoto_folder = self.storage.livephoto_folder(folder_name)
        image_assets = self._index_images(folder_name, image_folder, generate_derivatives=generate_derivatives)
        standalone_video = bool((metadata or {}).get('standalone_video'))
        livephoto_assets = self._index_livephotos(
            folder_name,
            livephoto_folder,
            image_assets=image_assets,
            generate_derivatives=generate_derivatives,
            generate_reverse=not standalone_video,
        )

        self.db.upsert_folder(
            {
                "folder_name": folder_name,
                "title": title,
                "text_prefix": text_prefix,
                "pub_ts": pub_ts,
                "pub_time": "" if (metadata or {}).get('source') == 'xhs' and pub_ts <= 0 else format_pub_time(pub_ts),
                "top_dynamic_id": top_dynamic_id,
                "source_dynamic_id": source_dynamic_id,
                "subscription_uid": subscription_uid,
                "subscription_name": subscription_name,
                "has_images": bool(image_assets),
                "has_livephoto": bool(livephoto_assets) and not standalone_video,
                "review_status": review_status,
                "review_reason": review_reason,
                "metadata": {**(metadata or {}), "indexed_at": now_iso()},
            }
        )
        self.db.replace_folder_assets(folder_name, "image", image_assets)
        self.db.replace_folder_assets(folder_name, "livephoto", [] if standalone_video else livephoto_assets)
        if standalone_video:
            self.db.replace_folder_assets(folder_name, "video", livephoto_assets)
        else:
            self.db.replace_folder_assets(folder_name, "video", [])
        self.refresh_gallery_index(folder_name)

    def reindex_library(
        self,
        progress: ProgressCallback | None = None,
        cooperate: CooperateCallback | None = None,
    ) -> None:
        folders = self.storage.media_folder_names()
        existing = {
            folder["folder_name"]: folder
            for folder in self.db.list_folders()
        }
        folder_names = sorted(folders)
        total = len(folder_names)
        for index, folder_name in enumerate(folder_names, start=1):
            if cooperate:
                cooperate()
            if progress:
                progress(
                    {
                        "stage": "library-index",
                        "message": f"正在索引图库 {index}/{total}",
                        "processed": index - 1,
                        "total": total,
                        "current": folder_name,
                    }
                )
            folder = existing.get(folder_name, {})
            self.index_folder(
                folder_name=folder_name,
                pub_ts=int(folder.get("pub_ts", 0)),
                title=folder.get("title", folder_name),
                text_prefix=folder.get("text_prefix", ""),
                top_dynamic_id=str(folder.get("top_dynamic_id", folder_name)),
                source_dynamic_id=str(folder.get("source_dynamic_id", folder_name)),
                subscription_uid=str(folder.get("subscription_uid") or ""),
                subscription_name=folder.get("subscription_name"),
                review_status=folder.get("review_status", "approved"),
                review_reason=folder.get("review_reason"),
                generate_derivatives=False,
                metadata=loads_json(folder.get('metadata_json'), {}),
            )
            if progress:
                progress(
                    {
                        "stage": "library-index",
                        "message": f"已索引图库 {index}/{total}",
                        "processed": index,
                        "total": total,
                        "current": folder_name,
                    }
                )
        self.db.mark_gallery_index_rebuilt()

    def rebuild_gallery_indexes(
        self,
        clear_derivatives: bool = True,
        progress: ProgressCallback | None = None,
        cooperate: CooperateCallback | None = None,
        thumbnail_workers: int | None = None,
    ) -> None:
        folders = {folder["folder_name"]: folder for folder in self.db.list_folders()}
        assets_by_folder: dict[str, list[dict]] = {}
        for asset in self.db.list_all_assets():
            assets_by_folder.setdefault(asset["folder_name"], []).append(asset)
        self.db.clear_gallery_indexes()
        folder_items = list(folders.items())
        total_folders = len(folder_items)
        total_assets = sum(len(assets_by_folder.get(folder_name, [])) for folder_name in folders)
        processed_assets = 0

        def thumbnail_progress(payload: dict[str, Any]) -> None:
            nonlocal processed_assets
            processed_assets += int(payload.get("step") or 1)
            if progress:
                progress(
                    {
                        "stage": "thumbnails",
                        "message": f"正在生成缩略图 {min(processed_assets, total_assets)}/{max(total_assets, 1)}",
                        "processed": min(processed_assets, total_assets),
                        "total": total_assets,
                        "current": payload.get("current"),
                    }
                )

        for folder_index, (folder_name, folder) in enumerate(folder_items, start=1):
            if cooperate:
                cooperate()
            if progress:
                progress(
                    {
                        "stage": "gallery-index",
                        "message": f"正在重建页面索引 {folder_index}/{total_folders}",
                        "processed": folder_index - 1,
                        "total": total_folders,
                        "current": folder_name,
                    }
                )
            if clear_derivatives:
                self.storage.clear_folder_thumbnail_derivatives(folder_name)
            assets = self._backfill_thumbnails(
                assets_by_folder.get(folder_name, []),
                progress=thumbnail_progress,
                cooperate=cooperate,
                thumbnail_workers=thumbnail_workers,
            )
            self._replace_gallery_index(folder, assets)
            if progress:
                progress(
                    {
                        "stage": "gallery-index",
                        "message": f"已重建页面索引 {folder_index}/{total_folders}",
                        "processed": folder_index,
                        "total": total_folders,
                        "current": folder_name,
                    }
                )
        self.db.mark_gallery_index_rebuilt()

    def refresh_gallery_index(self, folder_name: str) -> None:
        folder = self.db.get_folder(folder_name)
        if not folder:
            return
        assets = self.db.list_assets_for_folder(folder_name)
        assets = self._backfill_thumbnails(assets)
        self._replace_gallery_index(folder, assets)

    def _thumbnail_worker_count(self, requested: int | None = None) -> int:
        raw = requested if requested is not None else os.environ.get(THUMBNAIL_WORKERS_ENV)
        try:
            count = int(raw if raw is not None else DEFAULT_THUMBNAIL_WORKERS)
        except (TypeError, ValueError):
            count = DEFAULT_THUMBNAIL_WORKERS
        return max(1, min(16, count))

    def _backfill_thumbnails(
        self,
        assets: list[dict],
        progress: ProgressCallback | None = None,
        cooperate: CooperateCallback | None = None,
        thumbnail_workers: int | None = None,
    ) -> list[dict]:
        output: list[dict] = []
        if not assets:
            return output
        workers = min(self._thumbnail_worker_count(thumbnail_workers), len(assets))

        if workers <= 1:
            results = []
            for asset in assets:
                if cooperate:
                    cooperate()
                results.append((asset, self._ensure_asset_thumbnails(asset)))
                if progress:
                    progress({"current": asset.get("filename") or asset.get("folder_name"), "step": 1})
        else:
            results_by_key: dict[Any, tuple[str | None, str | None]] = {}
            assets_by_key: dict[Any, dict] = {}
            executor = ThreadPoolExecutor(max_workers=workers)
            try:
                future_map = {}
                for index, asset in enumerate(assets):
                    if cooperate:
                        cooperate()
                    key = asset.get("id") or index
                    assets_by_key[key] = asset
                    future_map[executor.submit(self._ensure_asset_thumbnails, dict(asset))] = key
                for future in as_completed(future_map):
                    if cooperate:
                        cooperate()
                    key = future_map[future]
                    results_by_key[key] = future.result()
                    asset = assets_by_key.get(key, {})
                    if progress:
                        progress({"current": asset.get("filename") or asset.get("folder_name"), "step": 1})
            except Exception:
                executor.shutdown(wait=False, cancel_futures=True)
                raise
            else:
                executor.shutdown(wait=True)
            results = [
                (asset, results_by_key.get(asset.get("id") or index, (None, None)))
                for index, asset in enumerate(assets)
            ]

        for asset, (thumb_rel_path, small_thumb_rel_path) in results:
            if (thumb_rel_path or small_thumb_rel_path) and asset.get("id"):
                self.db.update_asset_thumbnails(int(asset["id"]), thumb_rel_path, small_thumb_rel_path)
                asset = {
                    **asset,
                    "thumb_rel_path": thumb_rel_path,
                    "small_thumb_rel_path": small_thumb_rel_path,
                    "tiny_thumb_rel_path": None,
                }
            output.append(asset)
        return output

    def _ensure_asset_thumbnails(self, asset: dict) -> tuple[str | None, str | None]:
        self._background_checkpoint()
        media_type = asset.get("media_type")
        media_path = self.storage.resolve_storage_path(asset.get("rel_path"))
        if media_type == "image":
            source = media_path
        elif media_type in {"livephoto", "video"}:
            source = self.storage.resolve_storage_path(asset.get("cover_rel_path") or asset.get("thumb_rel_path"))
        else:
            return None, None
        if not source or not source.exists():
            return None, None
        target_source = media_path or source
        thumb_root = target_source.parent / ".thumbs"
        thumb_target = thumb_root / f"{target_source.stem}.webp"
        small_thumb_target = thumb_root / "small" / f"{target_source.stem}.webp"
        if media_type != "image" or not self._derive_image_with_sidecar(source, thumb_target, small_thumb_target):
            self.thumbnailer.ensure_image_thumbnail(source, thumb_target)
            self.thumbnailer.ensure_small_image_thumbnail(source, small_thumb_target)
        return (
            self.storage.relative_to_storage(thumb_target) if thumb_target.exists() else None,
            self.storage.relative_to_storage(small_thumb_target) if small_thumb_target.exists() else None,
        )

    def _replace_gallery_index(self, folder: dict, assets: list[dict]) -> None:
        image_assets = [asset for asset in assets if asset["media_type"] == "image"]
        livephoto_assets = [asset for asset in assets if asset["media_type"] == "livephoto"]
        preview_assets = image_assets or assets
        folder_index = {
            "title": folder.get("title") or folder["folder_name"],
            "text_prefix": folder.get("text_prefix") or "",
            "pub_ts": int(folder.get("pub_ts") or 0),
            "pub_time": folder.get("pub_time") or "",
            "top_dynamic_id": str(folder.get("top_dynamic_id") or ""),
            "source_dynamic_id": str(folder.get("source_dynamic_id") or ""),
            "subscription_uid": str(folder.get("subscription_uid") or ""),
            "subscription_name": folder.get("subscription_name") or "",
            "has_images": bool(folder.get("has_images")),
            "has_livephoto": bool(folder.get("has_livephoto")),
            "is_favorite": bool(folder.get("is_favorite")),
            "review_status": folder.get("review_status", "approved"),
            "review_reason": folder.get("review_reason"),
            "image_count": len(image_assets),
            "livephoto_count": len(livephoto_assets),
            "asset_count": len(assets),
            "preview_assets_json": dumps_json([self._asset_json(asset) for asset in preview_assets[:4]]),
            "year_key": (folder.get("pub_time") or "")[:4],
            "month_key": (folder.get("pub_time") or "")[:7],
        }
        pair_map: dict[int, dict[str, dict]] = {}
        for asset in image_assets:
            pair_map.setdefault(int(asset.get("pair_index") or 0), {})["image"] = asset
        for asset in livephoto_assets:
            pair_map.setdefault(int(asset.get("pair_index") or 0), {})["livephoto"] = asset
        pair_rows: list[dict] = []
        for pair_index in sorted(pair_map):
            image = pair_map[pair_index].get("image")
            livephoto = pair_map[pair_index].get("livephoto")
            image_json = self._asset_json(image)
            livephoto_json = self._asset_json(livephoto)
            preview_json = image_json or livephoto_json or {}
            preview = image or livephoto or {}
            pair_rows.append(
                {
                    "item_key": f"{folder['folder_name']}::{pair_index}",
                    "pair_index": int(pair_index),
                    "title": folder_index["title"],
                    "pub_ts": folder_index["pub_ts"],
                    "pub_time": folder_index["pub_time"],
                    "subscription_uid": folder_index["subscription_uid"],
                    "subscription_name": folder_index["subscription_name"],
                    "is_favorite": folder_index["is_favorite"],
                    "has_image": bool(image),
                    "has_livephoto": bool(livephoto),
                    "preview_url": pick_thumb_url(
                        [image_json, livephoto_json], levels=INDEX_PREVIEW_LEVELS, original_last=True
                    ),
                    "preview_kind": "paired" if image and livephoto else ("livephoto" if livephoto and not image else "image"),
                    "thumb_url": pick_thumb_url([preview_json], levels=INDEX_THUMB_LEVELS, original_last=True),
                    "display_ratio": self._display_ratio(preview),
                    "image_json": dumps_json(image_json),
                    "livephoto_json": dumps_json(livephoto_json),
                    "year_key": folder_index["year_key"],
                    "month_key": folder_index["month_key"],
                }
            )
        self.db.replace_gallery_index(folder["folder_name"], folder_index, pair_rows)

    def _index_images(self, folder_name: str, folder_path: Path, generate_derivatives: bool = True) -> list[dict]:
        if not folder_path.exists():
            return []
        assets: list[dict] = []
        files = sorted(
            path for path in folder_path.iterdir()
            if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES and not path.name.startswith(".")
        )
        confirmed = self.livephotos.confirmed_indices(folder_name)
        next_pair_index = 1
        for file_path in files:
            self._background_checkpoint()
            explicit_pair_index = confirmed.get(self.storage.relative_to_storage(file_path)) or self._extract_pair_index(file_path.name)
            pair_index = explicit_pair_index or next_pair_index
            next_pair_index = max(next_pair_index, pair_index + 1)
            thumb_path = folder_path / ".thumbs" / f"{file_path.stem}.webp"
            small_thumb_path = folder_path / ".thumbs" / "small" / f"{file_path.stem}.webp"
            width = height = None
            sidecar_result = None
            if generate_derivatives:
                sidecar_result = self.sidecar.derive_image(file_path, thumb_path, small_thumb_path, self.thumbnailer.sidecar_options())
                if sidecar_result:
                    width = sidecar_result.get("width")
                    height = sidecar_result.get("height")
                else:
                    self.thumbnailer.ensure_image_thumbnail(file_path, thumb_path)
                    self.thumbnailer.ensure_small_image_thumbnail(file_path, small_thumb_path)
            if width is None or height is None:
                probe = self.sidecar.probe_image(file_path)
                if probe:
                    width = probe.get("width")
                    height = probe.get("height")
            if width is None or height is None:
                try:
                    with Image.open(file_path) as image:
                        width, height = image.size
                except Exception:
                    width = height = None
            assets.append(
                {
                    "pair_index": pair_index,
                    "filename": file_path.name,
                    "rel_path": self.storage.relative_to_storage(file_path),
                    "thumb_rel_path": self.storage.relative_to_storage(thumb_path) if thumb_path.exists() else None,
                    "small_thumb_rel_path": self.storage.relative_to_storage(small_thumb_path) if small_thumb_path.exists() else None,
                    "width": width,
                    "height": height,
                    "duplicate_fingerprint": image_fingerprint(small_thumb_path if small_thumb_path.exists() else file_path),
                    "metadata": {
                        "kind": "image",
                        "pair_hash": self._image_hash(file_path),
                    },
                }
            )
        return assets

    def _derive_image_with_sidecar(self, source: Path, thumb: Path, small: Path) -> bool:
        result = self.sidecar.derive_image(source, thumb, small, self.thumbnailer.sidecar_options())
        return bool(result and thumb.exists() and small.exists())

    def _index_livephotos(
        self,
        folder_name: str,
        folder_path: Path,
        image_assets: list[dict] | None = None,
        generate_derivatives: bool = True,
        generate_reverse: bool = True,
    ) -> list[dict]:
        if not folder_path.exists():
            return []
        video_files = sorted(
            path for path in folder_path.iterdir()
            if path.is_file() and path.suffix.lower() in VIDEO_SUFFIXES and not path.name.startswith(".")
        )
        image_pairs = self._image_pair_lookup(image_assets or [])
        assets: list[dict] = []
        confirmed = self.livephotos.confirmed_indices(folder_name)
        used_pairs: set[int] = set()
        next_pair_index = max([asset["pair_index"] for asset in image_assets], default=0) + 1
        for fallback_index, file_path in enumerate(video_files, start=1):
            self._background_checkpoint()
            cover_path = folder_path / ".covers" / f"{file_path.stem}.jpg"
            provided_cover = None
            if not generate_reverse:
                provided_cover = next((asset for asset in image_assets or []
                    if asset['pair_index'] == self._extract_pair_index(file_path.name)), None)
                candidate = self.storage.resolve_storage_path((provided_cover or {}).get('rel_path'))
                if candidate and candidate.is_file():
                    cover_path = candidate
                else:
                    provided_cover = None
            cover_webp = folder_path / ".thumbs" / f"{file_path.stem}.webp"
            small_cover_webp = folder_path / ".thumbs" / "small" / f"{file_path.stem}.webp"
            reverse_path = folder_path / ".reverse" / file_path.name
            if generate_derivatives:
                if not provided_cover:
                    self.thumbnailer.ensure_video_cover(file_path, cover_path)
                if cover_path.exists():
                    self.thumbnailer.ensure_image_thumbnail(cover_path, cover_webp)
                    self.thumbnailer.ensure_small_image_thumbnail(cover_path, small_cover_webp)
                if generate_reverse:
                    self.thumbnailer.ensure_reverse_video(file_path, reverse_path)
            pair_index = confirmed.get(self.storage.relative_to_storage(file_path)) or self._extract_pair_index(file_path.name) or self._match_live_pair_index(
                folder_path,
                file_path,
                image_pairs,
                used_pairs,
            )
            if pair_index is None:
                pair_index = fallback_index if fallback_index not in used_pairs else next_pair_index
            used_pairs.add(pair_index)
            next_pair_index = max(next_pair_index, pair_index + 1)
            assets.append(
                {
                    "pair_index": pair_index,
                    "filename": file_path.name,
                    "rel_path": self.storage.relative_to_storage(file_path),
                    "thumb_rel_path": self.storage.relative_to_storage(cover_webp) if cover_webp.exists() else None,
                    "small_thumb_rel_path": self.storage.relative_to_storage(small_cover_webp) if small_cover_webp.exists() else None,
                    "cover_rel_path": self.storage.relative_to_storage(cover_path) if cover_path.exists() else None,
                    "reverse_rel_path": self.storage.relative_to_storage(reverse_path) if reverse_path.exists() else None,
                    "metadata": {
                        "kind": "livephoto",
                        "pair_hash": self._image_hash(folder_path / ".source_covers" / f"{file_path.stem}.jpg"),
                    },
                }
            )
        return assets

    def _extract_pair_index(self, filename: str) -> int | None:
        match = PAIR_PREFIX_RE.match(filename)
        if not match:
            return None
        return int(match.group("index"))

    def _image_hash(self, path: Path) -> str | None:
        if not path.exists():
            return None
        try:
            with Image.open(path) as image:
                normalized = image.convert("L").resize((16, 16))
                pixels = list(normalized.getdata())
        except Exception:
            return None
        if not pixels:
            return None
        avg = sum(pixels) / len(pixels)
        bits = "".join("1" if pixel >= avg else "0" for pixel in pixels)
        return f"{int(bits, 2):064x}"

    def _image_pair_lookup(self, image_assets: list[dict]) -> dict[int, str]:
        lookup: dict[int, str] = {}
        for asset in image_assets:
            metadata = asset.get("metadata", {})
            image_hash = metadata.get("pair_hash")
            if image_hash:
                lookup[int(asset["pair_index"])] = image_hash
        return lookup

    def _match_live_pair_index(
        self,
        folder_path: Path,
        file_path: Path,
        image_pairs: dict[int, str],
        used_pairs: set[int],
    ) -> int | None:
        source_cover = folder_path / ".source_covers" / f"{file_path.stem}.jpg"
        cover_hash = self._image_hash(source_cover)
        if not cover_hash or not image_pairs:
            return None
        ranked: list[tuple[int, int]] = []
        cover_bits = int(cover_hash, 16)
        for pair_index, image_hash in image_pairs.items():
            if pair_index in used_pairs:
                continue
            ranked.append((self._hamming_distance(cover_bits, int(image_hash, 16)), pair_index))
        if not ranked:
            return None
        ranked.sort(key=lambda item: item[0])
        return ranked[0][1]

    def _hamming_distance(self, left: int, right: int) -> int:
        return (left ^ right).bit_count()

    def _background_checkpoint(self) -> None:
        if self.priority is not None:
            self.priority.background_checkpoint()

    def _asset_json(self, asset: dict | None) -> dict | None:
        if not asset:
            return None
        metadata = asset.get("metadata", {})
        if not metadata and asset.get("metadata_json"):
            metadata = loads_json(asset["metadata_json"], {})
        return {
            "id": asset.get("id"),
            "media_type": asset.get("media_type"),
            "pair_index": int(asset.get("pair_index") or 0),
            "filename": asset.get("filename"),
            "url": self.storage.storage_url(asset.get("rel_path")),
            "thumb_url": self.storage.storage_url(asset.get("thumb_rel_path")),
            "small_thumb_url": self.storage.storage_url(asset.get("small_thumb_rel_path")),
            "cover_url": self.storage.storage_url(asset.get("cover_rel_path")),
            "reverse_url": self.storage.storage_url(asset.get("reverse_rel_path")),
            "width": asset.get("width"),
            "height": asset.get("height"),
            "metadata": metadata,
        }

    def _display_ratio(self, asset: dict | None) -> str:
        width = float(asset.get("width") or 1) if asset else 1.0
        height = float(asset.get("height") or 1) if asset else 1.0
        if width <= 0 or height <= 0:
            return "1 / 1"
        ratio = max(1 / 3, min(3, width / height))
        return f"{ratio:.4f} / 1"
