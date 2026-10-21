from __future__ import annotations

import hashlib
import json
import math
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from app.db import Database
from app.services.image_similarity import color_distance, hamming_distance, image_fingerprint, parse_fingerprint
from app.services.storage import StorageService
from app.services.task_priority import TaskPriorityCoordinator
from app.services.thumb_chain import pick_thumb_url
from app.services.utils import loads_json, now_iso


MAX_HASH_DISTANCE = 32
MAX_ASPECT_LOG_DISTANCE = 0.24
LOW_CONTRAST_LIMIT = 12
LOW_CONTRAST_COLOR_DISTANCE = 36
DUPLICATE_CACHE_VERSION = 5


@dataclass
class DuplicateAsset:
    id: int
    folder_name: str
    pair_index: int
    fingerprint: str
    hash_bits: int
    color: tuple[int, int, int]
    contrast: int
    width: int
    height: int
    file_size_bytes: int
    preview_url: str | None
    tile: dict[str, Any]

    @property
    def aspect_ratio(self) -> float:
        return max(1.0 / 20.0, min(20.0, self.width / max(self.height, 1)))


@dataclass
class BKNode:
    value: int
    indexes: list[int] = field(default_factory=list)
    children: dict[int, "BKNode"] = field(default_factory=dict)

    def add(self, value: int, index: int) -> None:
        distance = hamming_distance(self.value, value)
        if distance == 0:
            self.indexes.append(index)
            return
        child = self.children.get(distance)
        if child is None:
            self.children[distance] = BKNode(value=value, indexes=[index])
            return
        child.add(value, index)

    def query(self, value: int, limit: int, output: list[int]) -> None:
        distance = hamming_distance(self.value, value)
        if distance <= limit:
            output.extend(self.indexes)
        minimum = max(1, distance - limit)
        maximum = distance + limit
        for edge, child in self.children.items():
            if minimum <= edge <= maximum:
                child.query(value, limit, output)


class DuplicateService:
    def __init__(
        self,
        db: Database,
        storage: StorageService,
        priority: TaskPriorityCoordinator | None = None,
    ) -> None:
        self.db = db
        self.storage = storage
        self.priority = priority
        self._cache_signature = ""
        self._cached_groups: list[dict[str, Any]] = []
        self._cache_updated_at: str | None = None
        self._has_cache = False
        self._cache_stale = True
        self._signature_aliases: dict[str, str] = {}
        self._cache_lock = threading.RLock()

    def invalidate_cache(self) -> None:
        """Drop derived URLs after a storage-layout change; keep ignore choices."""
        with self._cache_lock:
            self._cache_signature = ''
            self._cached_groups = []
            self._cache_updated_at = None
            self._has_cache = False
            self._cache_stale = True
            self._signature_aliases = {}

    def list_groups(self, include_ignored: bool = False, force: bool = False) -> dict[str, Any]:
        threshold = self._image_threshold()
        cache_key = self._cache_key(threshold)
        if force:
            rebuilt_groups = self._build_groups(threshold)
            with self._cache_lock:
                self._signature_aliases = {}
                self._cached_groups = rebuilt_groups
                self.db.set_duplicate_group_cache(cache_key, self._cached_groups)
                self._cache_signature = cache_key
                self._cache_updated_at = now_iso()
                self._has_cache = True
                self._cache_stale = False

        with self._cache_lock:
            if not self._cache_signature:
                cached = self.db.get_latest_duplicate_group_cache()
                if cached and str(cached["cache_key"]).startswith(f"v{DUPLICATE_CACHE_VERSION}:"):
                    self._cached_groups = list(cached["groups"])
                    self._cache_signature = str(cached["cache_key"])
                    self._cache_updated_at = cached.get("updated_at")
                    self._has_cache = True
                    self._cache_stale = self._cache_signature != cache_key
                else:
                    self._cached_groups = []
                    self._cache_signature = cache_key
                    self._cache_updated_at = None
                    self._has_cache = False
                    self._cache_stale = True
            else:
                self._cache_stale = not self._has_cache or self._cache_signature != cache_key
            ignored = self.db.list_ignored_duplicate_signatures()
            groups = [
                self._public_group(group, ignored=group["signature"] in ignored)
                for group in self._cached_groups
                if include_ignored or group["signature"] not in ignored
            ]
            active_total = sum(1 for group in self._cached_groups if group["signature"] not in ignored)
            self.db.set_sidebar_count("duplicates", active_total, "重复内容")
            return {
                "items": groups,
                "total": len(groups),
                "active_total": active_total,
                "image_threshold": threshold,
                "has_cache": self._has_cache,
                "cache_stale": self._cache_stale,
                "cache_updated_at": self._cache_updated_at,
            }

    def get_group(self, signature: str, include_ignored: bool = False) -> dict[str, Any] | None:
        self._ensure_operation_cache()
        with self._cache_lock:
            resolved = self._resolve_signature(signature)
            group = next(
                (item for item in self._cached_groups if str(item.get("signature")) == resolved),
                None,
            )
            if not group:
                return None
            ignored = resolved in self.db.list_ignored_duplicate_signatures()
            if ignored and not include_ignored:
                return None
            return self._public_group(group, ignored=ignored)

    def ignore_group(self, signature: str) -> dict[str, Any]:
        group = self.get_group(signature)
        if not group:
            raise RuntimeError("重复内容项不存在或已经处理")
        folder_names = [str(item["folder_name"]) for item in group["items"]]
        self.db.ignore_duplicate_group(signature, folder_names)
        payload = self.list_groups()
        return {
            "ok": True,
            "message": "已忽略重复项",
            "signature": signature,
            "duplicates": payload,
            "sidebar_counts": self.db.get_sidebar_count_cache(),
            "remaining": payload["active_total"],
        }

    def remove_folders(self, folder_names: list[str]) -> dict[str, Any]:
        self._ensure_operation_cache()
        removed = {str(name) for name in folder_names if str(name)}
        if not removed:
            return self.list_groups()
        with self._cache_lock:
            groups = []
            for group in self._cached_groups:
                if not any(str(item["folder_name"]) in removed for item in group["items"]):
                    groups.append(group)
                    continue
                updated = self._group_without_folders(group, removed)
                if updated is not None:
                    groups.append(updated)
                    self._signature_aliases[str(group["signature"])] = str(updated["signature"])
                else:
                    self._signature_aliases[str(group["signature"])] = ""
            self._cached_groups = groups
            self._persist_incremental_cache()
            return self.list_groups()

    def cleanup_plan(self, signature: str) -> dict[str, Any]:
        self._ensure_operation_cache()
        with self._cache_lock:
            resolved = self._resolve_signature(signature)
            group = next(
                (item for item in self._cached_groups if str(item.get("signature")) == resolved),
                None,
            )
            if not group:
                raise RuntimeError("重复内容项不存在或已经处理")
            return {
                "folder_names": [str(item["folder_name"]) for item in group.get("items", [])],
                "targets": [dict(target) for target in group.get("_cleanup_targets", [])],
            }

    def complete_cleanup(self, signature: str) -> dict[str, Any]:
        self._ensure_operation_cache()
        with self._cache_lock:
            resolved = self._resolve_signature(signature)
            self._cached_groups = [
                group for group in self._cached_groups
                if str(group.get("signature")) != resolved
            ]
            self._signature_aliases[str(signature)] = ""
            if resolved and resolved != str(signature):
                self._signature_aliases[resolved] = ""
            self._persist_incremental_cache()
            return self.list_groups()

    def _persist_incremental_cache(self) -> None:
        if not self._cache_stale:
            self._cache_signature = self._cache_key(self._image_threshold())
        self.db.set_duplicate_group_cache(self._cache_signature, self._cached_groups)
        self._cache_updated_at = now_iso()
        self._has_cache = True

    def _ensure_operation_cache(self) -> None:
        with self._cache_lock:
            loaded = bool(self._cache_signature)
        if not loaded:
            self.list_groups(include_ignored=True)

    def _resolve_signature(self, signature: str) -> str:
        current = str(signature)
        visited = set()
        while current and current not in visited and current in self._signature_aliases:
            visited.add(current)
            current = self._signature_aliases[current]
        return current

    def _build_groups(self, threshold: int, folder_names: set[str] | None = None) -> list[dict[str, Any]]:
        self._background_checkpoint()
        folders = {
            str(item["folder_name"]): item
            for item in self.db.list_folders()
            if folder_names is None or str(item["folder_name"]) in folder_names
        }
        assets = self._duplicate_assets(folders)
        if len(assets) < 2:
            return []
        candidates: dict[tuple[str, str], list[tuple[int, int, int]]] = defaultdict(list)
        root: BKNode | None = None
        for index, asset in enumerate(assets):
            self._background_checkpoint()
            matches: list[int] = []
            if root is not None:
                root.query(asset.hash_bits, MAX_HASH_DISTANCE, matches)
            for other_index in matches:
                other = assets[other_index]
                if other.folder_name == asset.folder_name or not self._assets_similar(asset, other):
                    continue
                left_index, right_index = sorted((other_index, index))
                folder_pair = tuple(sorted((other.folder_name, asset.folder_name)))
                distance = hamming_distance(asset.hash_bits, other.hash_bits)
                candidates[folder_pair].append((distance, left_index, right_index))
            if root is None:
                root = BKNode(value=asset.hash_bits, indexes=[index])
            else:
                root.add(asset.hash_bits, index)

        accepted_links: dict[tuple[str, str], list[tuple[int, int, int]]] = {}
        for folder_pair, pair_matches in candidates.items():
            self._background_checkpoint()
            selected = self._greedy_matches(pair_matches)
            if len(selected) >= threshold:
                accepted_links[folder_pair] = selected
        if not accepted_links:
            return []

        parent = {name: name for pair in accepted_links for name in pair}

        def find(name: str) -> str:
            while parent[name] != name:
                parent[name] = parent[parent[name]]
                name = parent[name]
            return name

        def union(left: str, right: str) -> None:
            left_root = find(left)
            right_root = find(right)
            if left_root != right_root:
                parent[right_root] = left_root

        for left, right in accepted_links:
            union(left, right)
        components: dict[str, set[str]] = defaultdict(set)
        for name in parent:
            components[find(name)].add(name)

        groups = []
        assets_by_folder: dict[str, list[DuplicateAsset]] = defaultdict(list)
        for asset in assets:
            assets_by_folder[asset.folder_name].append(asset)
        for member_names in components.values():
            self._background_checkpoint()
            if len(member_names) < 2:
                continue
            links = {
                pair: matches
                for pair, matches in accepted_links.items()
                if pair[0] in member_names and pair[1] in member_names
            }
            groups.append(self._group_payload(member_names, links, folders, assets, assets_by_folder))
        groups.sort(key=lambda group: (group["latest_pub_ts"], group["similarity"]), reverse=True)
        return groups

    def _image_threshold(self) -> int:
        value = self.db.get_settings().get("duplicate_image_threshold", 2)
        try:
            return max(1, min(100, int(value)))
        except (TypeError, ValueError):
            return 2

    def _cache_key(self, threshold: int) -> str:
        return f"v{DUPLICATE_CACHE_VERSION}:{threshold}"

    def _duplicate_assets(self, folders: dict[str, dict[str, Any]]) -> list[DuplicateAsset]:
        output = []
        fingerprint_updates: dict[int, str] = {}
        for row in self.db.list_all_assets():
            self._background_checkpoint()
            if row.get("media_type") != "image" or row.get("folder_name") not in folders:
                continue
            fingerprint = str(row.get("duplicate_fingerprint") or "")
            parsed = parse_fingerprint(fingerprint)
            if parsed is None:
                # 独立于展示链的文件优先级：感知哈希取“最小可分辨文件路径”，非 URL。
                fingerprint_rel_path = row.get("small_thumb_rel_path") or row.get("thumb_rel_path") or row.get("rel_path")
                path = self.storage.resolve_storage_path(fingerprint_rel_path)
                fingerprint = image_fingerprint(path) if path else None
                parsed = parse_fingerprint(fingerprint)
                if fingerprint and row.get("id"):
                    fingerprint_updates[int(row["id"])] = fingerprint
            if not fingerprint or parsed is None:
                continue
            hash_bits, color, contrast = parsed
            tile = self._asset_tile(row)
            source_path = self.storage.resolve_storage_path(row.get("rel_path"))
            file_size_bytes = 0
            if source_path:
                try:
                    file_size_bytes = max(0, int(source_path.stat().st_size))
                except (FileNotFoundError, OSError):
                    file_size_bytes = 0
            output.append(
                DuplicateAsset(
                    id=int(row.get("id") or 0),
                    folder_name=str(row["folder_name"]),
                    pair_index=int(row.get("pair_index") or 0),
                    fingerprint=fingerprint,
                    hash_bits=hash_bits,
                    color=color,
                    contrast=contrast,
                    width=max(1, int(row.get("width") or 1)),
                    height=max(1, int(row.get("height") or 1)),
                    file_size_bytes=file_size_bytes,
                    preview_url=pick_thumb_url([tile], original_last=True),
                    tile=tile,
                )
            )
        self.db.set_asset_duplicate_fingerprints(fingerprint_updates)
        return output

    def _assets_similar(self, left: DuplicateAsset, right: DuplicateAsset) -> bool:
        if abs(math.log(left.aspect_ratio / right.aspect_ratio)) > MAX_ASPECT_LOG_DISTANCE:
            return False
        distance = hamming_distance(left.hash_bits, right.hash_bits)
        if distance > MAX_HASH_DISTANCE:
            return False
        if max(left.contrast, right.contrast) <= LOW_CONTRAST_LIMIT:
            return color_distance(left.color, right.color) <= LOW_CONTRAST_COLOR_DISTANCE
        return True

    def _background_checkpoint(self) -> None:
        if self.priority is not None:
            self.priority.background_checkpoint()

    def _greedy_matches(self, matches: list[tuple[int, int, int]]) -> list[tuple[int, int, int]]:
        selected = []
        used_left: set[int] = set()
        used_right: set[int] = set()
        for distance, left_index, right_index in sorted(matches):
            if left_index in used_left or right_index in used_right:
                continue
            used_left.add(left_index)
            used_right.add(right_index)
            selected.append((distance, left_index, right_index))
        return selected

    def _group_payload(
        self,
        member_names: set[str],
        links: dict[tuple[str, str], list[tuple[int, int, int]]],
        folders: dict[str, dict[str, Any]],
        assets: list[DuplicateAsset],
        assets_by_folder: dict[str, list[DuplicateAsset]],
    ) -> dict[str, Any]:
        signature_source = [
            (name, sorted(asset.fingerprint for asset in assets_by_folder[name]))
            for name in sorted(member_names)
        ]
        signature = hashlib.sha256(
            json.dumps(signature_source, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        matched_by_folder: dict[str, set[int]] = defaultdict(set)
        cleanup_targets: dict[tuple[str, int], dict[str, Any]] = {}
        match_rows = []
        link_summaries = []
        total_distance = 0
        total_matches = 0
        for pair, pair_matches in links.items():
            link_similarity_total = 0
            link_pair_indexes: dict[str, set[int]] = defaultdict(set)
            for distance, left_index, right_index in pair_matches:
                left = assets[left_index]
                right = assets[right_index]
                matched_by_folder[left.folder_name].add(left.id)
                matched_by_folder[right.folder_name].add(right.id)
                link_pair_indexes[left.folder_name].add(left.pair_index)
                link_pair_indexes[right.folder_name].add(right.pair_index)
                total_distance += distance
                total_matches += 1
                similarity = round((1 - distance / 256) * 100)
                link_similarity_total += similarity
                cleanup_asset, cleanup_reason = self._cleanup_asset(left, right, folders)
                cleanup_targets[(cleanup_asset.folder_name, cleanup_asset.pair_index)] = {
                    "folder_name": cleanup_asset.folder_name,
                    "pair_index": cleanup_asset.pair_index,
                    "width": cleanup_asset.width,
                    "height": cleanup_asset.height,
                    "pixels": cleanup_asset.width * cleanup_asset.height,
                    "file_size_bytes": cleanup_asset.file_size_bytes,
                    "reason": cleanup_reason,
                }
                match_rows.append(
                    {
                        "left_folder_name": left.folder_name,
                        "right_folder_name": right.folder_name,
                        "left_pair_index": left.pair_index,
                        "right_pair_index": right.pair_index,
                        "left_url": left.preview_url,
                        "right_url": right.preview_url,
                        "similarity": similarity,
                    }
                )
            link_summaries.append(
                {
                    "folders": list(pair),
                    "match_count": len(pair_matches),
                    "similarity_total": link_similarity_total,
                    "pair_indexes": {
                        name: sorted(indexes)
                        for name, indexes in link_pair_indexes.items()
                    },
                }
            )
        items = []
        for name in sorted(member_names, key=lambda item: int(folders[item].get("pub_ts") or 0), reverse=True):
            folder = folders[name]
            folder_assets = sorted(assets_by_folder[name], key=lambda item: (item.pair_index, item.id))
            items.append(
                {
                    "folder_name": name,
                    "title": folder.get("title") or name,
                    "text_prefix": folder.get("text_prefix") or "",
                    "pub_time": folder.get("pub_time") or "",
                    "pub_ts": int(folder.get("pub_ts") or 0),
                    "subscription_uid": str(folder.get("subscription_uid") or ""),
                    "subscription_name": folder.get("subscription_name") or "",
                    "has_images": True,
                    "has_livephoto": bool(folder.get("has_livephoto")),
                    "is_favorite": bool(folder.get("is_favorite")),
                    "preview_tiles": [asset.tile for asset in folder_assets[:4]],
                    "image_count": len(folder_assets),
                    "asset_count": len(folder_assets),
                    "max_resolution_pixels": max(
                        (asset.width * asset.height for asset in folder_assets),
                        default=0,
                    ),
                    "duplicate_match_count": len(matched_by_folder[name]),
                    "duplicate_ratio": round(
                        len(matched_by_folder[name]) / max(len(folder_assets), 1) * 100
                    ),
                }
            )
        self._apply_feature_tags(items)
        similarity = round((1 - total_distance / max(total_matches * 256, 1)) * 100)
        match_rows.sort(key=lambda item: item["similarity"], reverse=True)
        return {
            "signature": signature,
            "group_key": signature,
            "_signature_source": signature_source,
            "_link_summaries": link_summaries,
            "_cleanup_targets": sorted(
                cleanup_targets.values(),
                key=lambda item: (item["folder_name"], item["pair_index"]),
            ),
            "cleanup_candidate_count": len(cleanup_targets),
            "items": items,
            "post_count": len(items),
            "matched_image_count": total_matches,
            "similarity": similarity,
            "latest_pub_ts": max((int(item["pub_ts"]) for item in items), default=0),
            "matches": match_rows[:64],
        }

    def _cleanup_asset(
        self,
        left: DuplicateAsset,
        right: DuplicateAsset,
        folders: dict[str, dict[str, Any]],
    ) -> tuple[DuplicateAsset, str]:
        left_pixels = left.width * left.height
        right_pixels = right.width * right.height
        if left_pixels != right_pixels:
            return (left if left_pixels < right_pixels else right), "lower_resolution"

        left_time = int(folders[left.folder_name].get("pub_ts") or 0)
        right_time = int(folders[right.folder_name].get("pub_ts") or 0)
        if left_time != right_time:
            return (left if left_time > right_time else right), "later_published"

        if left.file_size_bytes > 0 and right.file_size_bytes > 0 and left.file_size_bytes != right.file_size_bytes:
            return (left if left.file_size_bytes > right.file_size_bytes else right), "larger_file"

        left_key = (left.folder_name, left.pair_index, left.id)
        right_key = (right.folder_name, right.pair_index, right.id)
        return (left if left_key > right_key else right), "stable_tiebreak"

    def _group_without_folders(self, group: dict[str, Any], removed: set[str]) -> dict[str, Any] | None:
        items = [dict(item) for item in group["items"] if str(item["folder_name"]) not in removed]
        if len(items) < 2:
            return None
        matches = [
            dict(match)
            for match in group.get("matches", [])
            if str(match.get("left_folder_name")) not in removed
            and str(match.get("right_folder_name")) not in removed
        ]
        link_summaries = [
            dict(summary)
            for summary in group.get("_link_summaries", [])
            if not any(str(name) in removed for name in summary.get("folders", []))
        ]
        matched_by_folder: dict[str, set[int]] = defaultdict(set)
        for summary in link_summaries:
            for name, pair_indexes in summary.get("pair_indexes", {}).items():
                matched_by_folder[str(name)].update(int(index) for index in pair_indexes)
        for item in items:
            item["duplicate_match_count"] = len(matched_by_folder[str(item["folder_name"])])
            item["duplicate_ratio"] = round(
                item["duplicate_match_count"] / max(int(item.get("image_count") or 0), 1) * 100
            )
        self._apply_feature_tags(items)
        signature_source = [
            entry
            for entry in group.get("_signature_source", [])
            if str(entry[0]) not in removed
        ]
        signature = hashlib.sha256(
            json.dumps(signature_source, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        total_matches = sum(int(summary.get("match_count") or 0) for summary in link_summaries)
        similarity_total = sum(int(summary.get("similarity_total") or 0) for summary in link_summaries)
        return {
            **group,
            "signature": signature,
            "_signature_source": signature_source,
            "_link_summaries": link_summaries,
            "_cleanup_targets": [
                dict(target)
                for target in group.get("_cleanup_targets", [])
                if str(target.get("folder_name")) not in removed
            ],
            "cleanup_candidate_count": sum(
                1
                for target in group.get("_cleanup_targets", [])
                if str(target.get("folder_name")) not in removed
            ),
            "items": items,
            "post_count": len(items),
            "matched_image_count": total_matches,
            "similarity": round(similarity_total / total_matches) if total_matches else 0,
            "latest_pub_ts": max((int(item.get("pub_ts") or 0) for item in items), default=0),
            "matches": matches,
        }

    def _apply_feature_tags(self, items: list[dict[str, Any]]) -> None:
        if not items:
            return
        resolution_values = [int(item.get("max_resolution_pixels") or 0) for item in items]
        image_counts = [int(item.get("image_count") or 0) for item in items]
        publication_times = [int(item.get("pub_ts") or 0) for item in items]
        best_resolution = max(resolution_values, default=0)
        most_images = max(image_counts, default=0)
        earliest_publication = min(publication_times, default=0)
        resolution_distinct = len(set(resolution_values)) > 1
        image_count_distinct = len(set(image_counts)) > 1
        publication_distinct = len(set(publication_times)) > 1
        for item in items:
            tags = []
            if resolution_distinct and int(item.get("max_resolution_pixels") or 0) == best_resolution:
                tags.append("更高分辨率")
            if image_count_distinct and int(item.get("image_count") or 0) == most_images:
                tags.append("更多图片")
            if publication_distinct and int(item.get("pub_ts") or 0) == earliest_publication:
                tags.append("更早发布")
            item["feature_tags"] = tags

    def _public_group(self, group: dict[str, Any], ignored: bool) -> dict[str, Any]:
        return {
            key: (value[:12] if key == "matches" else value)
            for key, value in group.items()
            if not key.startswith("_")
        } | {"ignored": ignored}

    def _asset_tile(self, asset: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": int(asset.get("id") or 0),
            "media_type": asset.get("media_type"),
            "pair_index": int(asset.get("pair_index") or 0),
            "filename": asset.get("filename"),
            "url": self.storage.storage_url(asset.get("rel_path")),
            "thumb_url": self.storage.storage_url(asset.get("thumb_rel_path")),
            "small_thumb_url": self.storage.storage_url(asset.get("small_thumb_rel_path")),
            "width": asset.get("width"),
            "height": asset.get("height"),
            "metadata": loads_json(asset.get("metadata_json"), {}),
        }
