from __future__ import annotations

import hashlib
import json
import math
import threading
from collections import defaultdict, deque
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from functools import cmp_to_key
from typing import Any, Iterator

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
DUPLICATE_CACHE_VERSION = 8


@dataclass
class DuplicateDeletion:
    cache_key: str
    threshold: int
    expected_content: dict[str, list[dict[str, Any]]]


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
        self._operation_lock = threading.RLock()

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
        if force:
            # 检测与重复项删除串行，避免相互覆盖刚生成的缓存。
            with self._operation_lock:
                source_signature = self.db.duplicate_content_signature(include_fingerprints=False)
                rebuilt_groups = self._build_groups(threshold)
                rebuilt_key = self._cache_key(threshold)
                if source_signature != self.db.duplicate_content_signature(include_fingerprints=False):
                    rebuilt_key += ":stale"
                with self._cache_lock:
                    self._signature_aliases = {}
                    self._cached_groups = rebuilt_groups
                    self.db.set_duplicate_group_cache(rebuilt_key, self._cached_groups)
                    self._cache_signature = rebuilt_key
                    self._cache_updated_at = now_iso()
                    self._has_cache = True

        with self._cache_lock:
            # 等待检测完成后再读取当前标记，不能拿检测开始前的状态判定新结果过期。
            cache_key = self._cache_key(self._image_threshold())
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

    def remove_folders(self, folder_names: list[str], change: DuplicateDeletion | None = None) -> dict[str, Any]:
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
                remaining = {str(item["folder_name"]) for item in group["items"]} - removed
                rebuilt = self._build_groups(self._image_threshold(), remaining) if len(remaining) >= 2 else []
                groups.extend(rebuilt)
                self._signature_aliases[str(group["signature"])] = (
                    str(rebuilt[0]["signature"]) if len(rebuilt) == 1 else ""
                )
            self._cached_groups = groups
            self._persist_incremental_cache(change)
            return self.list_groups()

    def cleanup_plan(self, signature: str) -> dict[str, Any]:
        self._require_fresh_cache()
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

    def _require_fresh_cache(self) -> None:
        self._ensure_operation_cache()
        current_key = self._cache_key(self._image_threshold())
        with self._cache_lock:
            self._cache_stale = not self._has_cache or self._cache_signature != current_key
            if self._cache_stale:
                raise RuntimeError("图库或检测设置已变化，请重新检测重复内容后再处理")

    def resolve_plan(self, signature: str, keep_folder_name: str) -> list[str]:
        self._require_fresh_cache()
        group = self.get_group(signature)
        if not group:
            raise RuntimeError("重复内容项不存在或已经处理")
        keep = next((item for item in group["items"] if item["folder_name"] == keep_folder_name), None)
        if not keep or not keep.get("can_keep_only"):
            raise RuntimeError("所选贴文未覆盖其余贴文的全部图片，或其余贴文含实况、视频；请使用仅清理重复或逐条处理")
        with self._cache_lock:
            internal = next(item for item in self._cached_groups if item["signature"] == group["signature"])
            links = list(internal["_image_links"])
        rows = self.db.list_assets_for_folders([str(item["folder_name"]) for item in group["items"]])
        by_id = {int(row["id"]): row for row in rows}
        if not any(row["folder_name"] == keep_folder_name for row in rows):
            raise RuntimeError("所选保留贴文的图片已缺失，请重新检测后再处理")
        fingerprints = {}
        covered = set()
        for left_id, right_id in links:
            left, right = by_id.get(left_id), by_id.get(right_id)
            if not left or not right:
                continue
            if left["folder_name"] != keep_folder_name and right["folder_name"] != keep_folder_name:
                continue
            for row in (left, right):
                if row["id"] not in fingerprints:
                    path = self.storage.resolve_storage_path(row.get("rel_path"))
                    fingerprints[row["id"]] = parse_fingerprint(image_fingerprint(path) if path else None)
            left_fp, right_fp = fingerprints[left_id], fingerprints[right_id]
            if left_fp is None or right_fp is None:
                continue
            if hamming_distance(left_fp[0], right_fp[0]) > MAX_HASH_DISTANCE:
                continue
            if max(left_fp[2], right_fp[2]) <= LOW_CONTRAST_LIMIT and color_distance(left_fp[1], right_fp[1]) > LOW_CONTRAST_COLOR_DISTANCE:
                continue
            covered.add(right_id if left["folder_name"] == keep_folder_name else left_id)
        if any(row["id"] not in covered for row in rows if row["folder_name"] != keep_folder_name):
            raise RuntimeError("原图复核未能覆盖其余贴文的全部图片，请重新检测或使用仅清理重复")
        self._require_fresh_cache()
        return [str(item["folder_name"]) for item in group["items"]]

    def validate_cleanup_targets(self, signature: str, targets: list[dict[str, Any]]) -> None:
        plan = self.cleanup_plan(signature)
        if targets != plan["targets"]:
            raise RuntimeError("重复图片清理计划已变化，请重新检测后再处理")
        # 后台排队期间文件可能被替换；清理前直接回读文件，不能只信任数据库指纹。
        rows = self.db.list_assets_for_folders(plan["folder_names"])
        images = {int(row["id"]): row for row in rows if row.get("media_type") == "image"}
        checked: set[int] = set()
        for target in targets:
            for key in ("asset_id", "keep_asset_id"):
                asset_id = int(target[key])
                if asset_id in checked:
                    continue
                row = images.get(asset_id)
                path = self.storage.resolve_storage_path(row.get("rel_path")) if row else None
                if not path or not path.is_file():
                    raise RuntimeError("待清理图片或对应保留图片已缺失，请重新检测后再处理")
                # 计划生成时已验证两张原图相似，此处确认原图仍与计划一致即可。
                # 缩略图是可重建的派生文件，不能与旧索引指纹逐字比较来判断原图是否变化。
                source_key = "source_fingerprint" if key == "asset_id" else "keep_source_fingerprint"
                if image_fingerprint(path) != target[source_key]:
                    raise RuntimeError("待清理原图或对应保留原图已变化，请重新检测后再处理")
                checked.add(asset_id)
        self._require_fresh_cache()

    def complete_cleanup(self, signature: str, change: DuplicateDeletion | None = None) -> dict[str, Any]:
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
            self._persist_incremental_cache(change)
            return self.list_groups()

    @contextmanager
    def track_deletions(
        self, *, asset_ids: list[int] | None = None, folder_names: list[str] | None = None,
    ) -> Iterator[DuplicateDeletion | None]:
        with self._operation_lock:
            self._ensure_operation_cache()
            with self._cache_lock:
                threshold = self._image_threshold()
                content = self.db.duplicate_content_snapshot()
                key = self._cache_key(threshold, snapshot=content)
                change = None
                if self._has_cache and key == self._cache_signature:
                    removed_ids, removed_folders = set(asset_ids or []), set(folder_names or [])
                    expected = {
                        table: [row for row in rows if row["folder_name"] not in removed_folders
                                and (table != "assets" or int(row["id"]) not in removed_ids)]
                        for table, rows in content.items()
                    }
                    change = DuplicateDeletion(key, threshold, expected)
            yield change

    def _persist_incremental_cache(self, change: DuplicateDeletion | None = None) -> None:
        # 只认可本次已确认删除的记录；外部新增、替换或设置变化仍使缓存过期。
        if change and change.cache_key == self._cache_signature and change.threshold == self._image_threshold():
            content = self.db.duplicate_content_snapshot()
            if content == change.expected_content:
                self._cache_signature = self._cache_key(change.threshold, snapshot=content)
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
                folder_pair = tuple(sorted((other.folder_name, asset.folder_name)))
                left_index, right_index = (
                    (other_index, index) if other.folder_name == folder_pair[0] else (index, other_index)
                )
                distance = hamming_distance(asset.hash_bits, other.hash_bits)
                candidates[folder_pair].append((distance, left_index, right_index))
            if root is None:
                root = BKNode(value=asset.hash_bits, indexes=[index])
            else:
                root.add(asset.hash_bits, index)

        accepted_links: dict[tuple[str, str], list[tuple[int, int, int]]] = {}
        for folder_pair, pair_matches in candidates.items():
            self._background_checkpoint()
            selected = self._maximum_matches(pair_matches)
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

    def _cache_key(self, threshold: int, *, snapshot: dict[str, list[dict[str, Any]]] | None = None) -> str:
        return f"v{DUPLICATE_CACHE_VERSION}:{threshold}:{self.db.duplicate_content_signature(snapshot=snapshot)}"

    def _duplicate_assets(self, folders: dict[str, dict[str, Any]]) -> list[DuplicateAsset]:
        output = []
        fingerprint_updates: dict[int, str] = {}
        rows = (self.db.list_assets_for_folders(sorted(folders))
                if len(folders) < 100 else self.db.list_all_assets(images_only=True))
        for row in rows:
            self._background_checkpoint()
            if row.get("media_type") != "image" or row.get("folder_name") not in folders:
                continue
            # 主动检测必须读取当前文件；旧索引指纹可能来自原图或重建前的缩略图。
            # 从最小派生图开始尝试，路径失效或解码失败时回退，避免反复检测仍复用旧值。
            fingerprint = None
            candidates = dict.fromkeys((row.get("small_thumb_rel_path"), row.get("thumb_rel_path"), row.get("rel_path")))
            for relative in candidates:
                path = self.storage.resolve_storage_path(relative) if relative else None
                fingerprint = image_fingerprint(path) if path else None
                if fingerprint:
                    break
            parsed = parse_fingerprint(fingerprint)
            if not fingerprint or parsed is None:
                continue
            if fingerprint != row.get("duplicate_fingerprint") and row.get("id"):
                fingerprint_updates[int(row["id"])] = fingerprint
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

    def _maximum_matches(self, matches: list[tuple[int, int, int]]) -> list[tuple[int, int, int]]:
        adjacency: dict[int, list[int]] = defaultdict(list)
        distances = {}
        for distance, left, right in sorted(set(matches)):
            adjacency[left].append(right)
            distances[left, right] = distance
        left_match: dict[int, int] = {}
        right_match: dict[int, int] = {}
        # 用增广路径求最大数量一对一匹配，近距离优先遍历；不依赖递归深度。
        for start in sorted(adjacency):
            self._background_checkpoint()
            queue = deque([start])
            seen_left = {start}
            previous: dict[int, int] = {}
            free_right = None
            while queue and free_right is None:
                left = queue.popleft()
                for right in adjacency[left]:
                    if right in previous:
                        continue
                    previous[right] = left
                    if right not in right_match:
                        free_right = right
                        break
                    next_left = right_match[right]
                    if next_left not in seen_left:
                        seen_left.add(next_left)
                        queue.append(next_left)
            while free_right is not None:
                left = previous[free_right]
                old_right = left_match.get(left)
                left_match[left] = free_right
                right_match[free_right] = left
                free_right = old_right
        return sorted((distances[left, right], left, right) for left, right in left_match.items())

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
        media_by_folder: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in self.db.list_assets_for_folders(sorted(member_names)):
            media_by_folder[str(row["folder_name"])].append(row)
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
        cleanup_targets = self._safe_cleanup_targets(links, assets, folders, media_by_folder)
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
                    "can_keep_only": all(
                        all(row.get("media_type") == "image" for row in media_by_folder[other])
                        and any(
                            set(summary["folders"]) == {name, other}
                            and summary["match_count"] == len(media_by_folder[other])
                            for summary in link_summaries
                        )
                        for other in member_names if other != name
                    ),
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
            "_image_links": [(assets[left].id, assets[right].id) for matches in links.values() for _, left, right in matches],
            "_cleanup_targets": sorted(
                cleanup_targets,
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

    def _safe_cleanup_targets(
        self,
        links: dict[tuple[str, str], list[tuple[int, int, int]]],
        assets: list[DuplicateAsset],
        folders: dict[str, dict[str, Any]],
        media_by_folder: dict[str, list[dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        neighbors: dict[int, set[int]] = defaultdict(set)
        for matches in links.values():
            for _, left, right in matches:
                neighbors[left].add(right)
                neighbors[right].add(left)
        protected = {
            (name, int(row.get("pair_index") or 0))
            for name, rows in media_by_folder.items()
            for row in rows if row.get("media_type") != "image"
        }
        pair_counts: dict[tuple[str, int], int] = defaultdict(int)
        for name, rows in media_by_folder.items():
            for row in rows:
                pair_counts[name, int(row.get("pair_index") or 0)] += 1
        protected.update(pair for pair, count in pair_counts.items() if count != 1)

        def compare(left: int, right: int) -> int:
            discarded, _ = self._cleanup_asset(assets[left], assets[right], folders)
            return 1 if discarded is assets[left] else -1

        removed: set[int] = set()
        retained: set[int] = set()
        targets = []
        rows_by_id = {int(row["id"]): row for rows in media_by_folder.values() for row in rows}
        source_fingerprints: dict[int, str | None] = {}

        def original(asset: DuplicateAsset) -> DuplicateAsset | None:
            if asset.id not in source_fingerprints:
                row = rows_by_id.get(asset.id, {})
                path = self.storage.resolve_storage_path(row.get("rel_path"))
                source_fingerprints[asset.id] = image_fingerprint(path) if path else None
            fingerprint = source_fingerprints[asset.id]
            parsed = parse_fingerprint(fingerprint)
            if parsed is None:
                return None
            bits, color, contrast = parsed
            return replace(asset, fingerprint=fingerprint, hash_bits=bits, color=color, contrast=contrast)

        for keep_index in sorted(neighbors, key=cmp_to_key(compare)):
            self._background_checkpoint()
            if keep_index in removed:
                continue
            retained.add(keep_index)
            keep = assets[keep_index]
            for index in sorted(neighbors[keep_index]):
                asset = assets[index]
                if index in removed or index in retained or (asset.folder_name, asset.pair_index) in protected:
                    continue
                discarded, reason = self._cleanup_asset(asset, keep, folders)
                if discarded is not asset:
                    continue
                original_asset, original_keep = original(asset), original(keep)
                if not original_asset or not original_keep or not self._assets_similar(original_asset, original_keep):
                    continue
                removed.add(index)
                targets.append({
                    "folder_name": asset.folder_name,
                    "pair_index": asset.pair_index,
                    "asset_id": asset.id,
                    "fingerprint": asset.fingerprint,
                    "keep_asset_id": keep.id,
                    "keep_folder_name": keep.folder_name,
                    "keep_pair_index": keep.pair_index,
                    "keep_fingerprint": keep.fingerprint,
                    "source_fingerprint": original_asset.fingerprint,
                    "keep_source_fingerprint": original_keep.fingerprint,
                    "width": asset.width,
                    "height": asset.height,
                    "pixels": asset.width * asset.height,
                    "file_size_bytes": asset.file_size_bytes,
                    "reason": reason,
                })
        return targets

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
