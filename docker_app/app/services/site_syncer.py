from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time as datetime_time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlparse
from xml.etree import ElementTree

from PIL import Image, ImageOps
import requests

from app.db import Database
from app.services.media_indexer import MediaIndexer
from app.services.site_downloader import MediaDownloader
from app.services.site_filtering import RuleEngine
from app.services.site_parser import PageFetcher, ParsedPost, SourceParser, browser_like_site_headers, parse_html
from app.services.storage import StorageService
from app.services.thumbnailer import ThumbnailService
from app.services.utils import TIMEZONE, clean_filename, parse_date, safe_slug


SITE_PAGE_REQUEST_TIMEOUT = 300
SITE_MEDIA_DOWNLOAD_TIMEOUT = 300
SITE_MEDIA_DOWNLOAD_CONCURRENCY = 3
SITE_MEDIA_DOWNLOAD_RETRIES = 5
SITE_ICON_CACHE_MAX_BYTES = 2 * 1024 * 1024
SITE_ICON_EXTENSIONS = (".ico", ".png", ".jpg", ".jpeg", ".webp", ".svg", ".gif", ".avif")


class SiteSyncManager:
    def __init__(self, db: Database, storage: StorageService, indexer: MediaIndexer | None = None) -> None:
        self.db = db
        self.storage = storage
        self.indexer = indexer
        self.thumbnailer = indexer.thumbnailer if indexer else ThumbnailService()
        self._lock = threading.Lock()
        self._task_queue: Any | None = None
        self._status: dict[str, Any] = {"running": False, "message": "空闲"}

    def bind_task_queue(self, task_queue: Any) -> None:
        self._task_queue = task_queue

    def status(self) -> dict[str, Any]:
        return {
            **self._status,
            "last_run": self.db.last_task_run("site-sync"),
        }

    def start_sync(self, source_id: int | None = None) -> dict[str, Any]:
        if self._task_queue is not None:
            return self._task_queue.start_site_sync(source_id)
        if not self._lock.acquire(blocking=False):
            return {"ok": True, "queued": False, "message": "已有站点同步任务正在运行"}
        thread = threading.Thread(target=self._run_sync_thread, args=(source_id,), daemon=True)
        thread.start()
        return {"ok": True, "queued": False, "message": "已开始站点同步", "sidebar_counts": self.db.refresh_sidebar_count_cache(["tasks"])}

    def start_full_validation(self, source_id: int, max_pages: int | None = None) -> dict[str, Any]:
        if self._task_queue is not None:
            return self._task_queue.start_site_validation(source_id, max_pages=max_pages)
        if not self._lock.acquire(blocking=False):
            return {"ok": True, "queued": False, "message": "已有站点同步任务正在运行"}
        thread = threading.Thread(target=self._run_full_validation_thread, args=(source_id, max_pages), daemon=True)
        thread.start()
        return {"ok": True, "queued": False, "message": "已开始站点全量校验", "sidebar_counts": self.db.refresh_sidebar_count_cache(["tasks"])}

    def test_source(self, source: dict[str, Any]) -> list[dict[str, Any]]:
        if source.get("source_type") == "xhs_likes":
            raise RuntimeError("小红书来源仅支持专用适配器")
        settings = self.db.get_settings()
        source_settings = self._settings_for_source(settings, source)
        fetcher = PageFetcher(
            timeout=self._page_request_timeout(source_settings),
            user_agent=str(source_settings.get("site_user_agent")),
            proxies=self._site_proxies(source_settings),
        )
        parser = SourceParser(fetcher)
        return [self._preview_dict(post) for post in parser.preview(source, limit=3)]

    def suggest_source(self, payload: dict[str, Any]) -> dict[str, Any]:
        settings = self.db.get_settings()
        fetcher = PageFetcher(
            timeout=self._page_request_timeout(settings),
            user_agent=str(settings.get("site_user_agent")),
            proxies=self._site_proxies(settings),
        )
        parser = SourceParser(fetcher)
        suggestion = parser.suggest(str(payload.get("entry_url") or ""))
        try:
            suggestion["preview"] = [self._preview_dict(post) for post in parser.preview(suggestion, limit=3)]
        except Exception:
            suggestion["preview"] = suggestion.get("preview") or []
        return suggestion

    def _run_sync_thread(self, source_id: int | None) -> None:
        task_id = self.db.create_task_run("site-sync", "running", "同步站点来源")
        try:
            details = self.execute_sync(source_id)
            self.db.finish_task_run(task_id, "success", "站点同步完成", details)
            details["sidebar_counts"] = self.db.refresh_sidebar_count_cache(["all", "favorites", "livephoto", "review", "logs", "tasks", "subscriptions", "sites"])
            self._status = {"running": False, "message": "站点同步完成"}
        except Exception as exc:
            details = {"sources": 0, "posts": 0, "downloaded": 0, "blocked": 0, "errors": 0, "added_items": []}
            details["errors"] += 1
            self.db.finish_task_run(task_id, "failed", str(exc), details)
            self.db.refresh_sidebar_count_cache(["tasks"])
            self._status = {"running": False, "message": f"站点同步失败: {exc}"}
        finally:
            self._lock.release()

    def _run_full_validation_thread(self, source_id: int, max_pages: int | None = None) -> None:
        task_id = self.db.create_task_run("site-sync", "running", "站点全量校验")
        try:
            details = self.execute_full_validation(source_id, max_pages=max_pages)
            self.db.finish_task_run(task_id, "success", "站点全量校验完成", details)
            details["sidebar_counts"] = self.db.refresh_sidebar_count_cache(["all", "favorites", "livephoto", "review", "logs", "tasks", "subscriptions", "sites"])
            self._status = {"running": False, "message": "站点全量校验完成"}
        except Exception as exc:
            details = {"sources": 0, "posts": 0, "downloaded": 0, "blocked": 0, "errors": 1, "added_items": []}
            self.db.finish_task_run(task_id, "failed", str(exc), details)
            self.db.refresh_sidebar_count_cache(["tasks"])
            self._status = {"running": False, "message": f"站点全量校验失败: {exc}"}
        finally:
            self._lock.release()

    def execute_sync(self, source_id: int | None = None, cooperate=None, max_pages: int | None = None) -> dict[str, Any]:
        settings = self.db.get_settings()
        details: dict[str, Any] = {
            "sources": 0,
            "discovered": 0,
            "posts": 0,
            "downloaded": 0,
            "blocked": 0,
            "skipped": 0,
            "no_media": 0,
            "errors": 0,
            "added_items": [],
            "proxy": self._site_proxy_summary(settings),
        }
        self._status = {"running": True, "message": "正在同步站点来源"}
        sources = [self.db.get_site_source(source_id)] if source_id else self.db.list_site_sources(include_disabled=False)
        for source in [item for item in sources if item]:
            if source.get("source_type") == "xhs_likes":
                continue
            if cooperate:
                cooperate()
            details["sources"] += 1
            self._status = {
                "running": True,
                "message": f"正在同步站点：{source.get('name') or source.get('slug') or source['id']}",
                "current_source": source.get("name") or source.get("slug") or source["id"],
                "source_index": details["sources"],
                "source_total": len([item for item in sources if item]),
            }
            self._log_site_event(source, "sync-start", "开始同步站点来源")
            try:
                if max_pages:
                    result = self._sync_source(source, cooperate=cooperate, max_pages=max_pages)
                else:
                    result = self._sync_source(source, cooperate=cooperate)
            except Exception as exc:
                details["errors"] += 1
                self.db.add_site_filter_log(
                    source.get("id"),
                    str(source.get("entry_url") or ""),
                    str(source.get("name") or source.get("slug") or source.get("id") or "站点来源"),
                    "error",
                    f"同步失败: {exc}",
                )
                continue
            for key, value in result.items():
                if key == "added_items":
                    if isinstance(value, list):
                        details["added_items"].extend(item for item in value if isinstance(item, dict))
                    continue
                details[key] = int(details.get(key, 0)) + int(value)
            if not result.get("discovered"):
                self._log_site_event(
                    source,
                    "empty",
                    "未发现可同步内容",
                    f"入口: {source.get('entry_url') or '-'}; 类型: {source.get('source_type') or '-'}; 起始日期: {source.get('start_date') or settings.get('site_default_start_date') or '-'}",
                )
            self._log_site_event(
                source,
                "sync-complete",
                "站点同步完成",
                f"发现 {result.get('discovered', 0)} 条，入库 {result.get('posts', 0)} 条，下载 {result.get('downloaded', 0)} 个，拦截 {result.get('blocked', 0)} 条，跳过 {result.get('skipped', 0)} 条，失败 {result.get('errors', 0)} 个",
            )
        self._status = {"running": False, "message": "站点同步完成"}
        return details

    def execute_full_validation(self, source_id: int, cooperate=None, max_pages: int | None = None) -> dict[str, Any]:
        source = self.db.get_site_source(source_id)
        if not source:
            raise RuntimeError("站点来源不存在")
        if source.get("source_type") == "xhs_likes":
            raise RuntimeError("小红书来源禁止使用破坏式站点全量校验")
        self._status = {"running": True, "message": f"正在全量校验站点：{source.get('name') or source_id}"}
        settings = self.db.get_settings()
        cleared = self.db.clear_site_source_content(source_id)
        removed_files = 0
        for folder_name in cleared.get("folder_names", []):
            removed_files += self.storage.remove_folder_assets(folder_name)
        removed_files += self.storage.remove_site_source_assets(source.get("slug") or source.get("name") or str(source_id))
        self._log_site_event(source, "validation-start", "开始站点全量校验")
        self._log_site_event(
            source,
            "validation-cleared",
            "已清理旧内容",
            f"旧帖子 {int(cleared.get('posts') or 0)} 条，旧素材 {int(cleared.get('assets') or 0)} 个，图库动态 {int(cleared.get('folders') or 0)} 个，删除文件 {removed_files} 个",
        )
        result = self.execute_sync(source_id, cooperate=cooperate, max_pages=max_pages)
        result["cleared_posts"] = int(cleared.get("posts") or 0)
        result["cleared_assets"] = int(cleared.get("assets") or 0)
        result["cleared_folders"] = int(cleared.get("folders") or 0)
        result["removed_files"] = removed_files
        result["validation_mode"] = True
        if max_pages:
            result["temporary_max_pages"] = int(max_pages)
        result["source"] = {
            "id": source.get("id"),
            "name": source.get("name"),
            "entry_url": source.get("entry_url"),
            "source_type": source.get("source_type"),
            "start_date": source.get("start_date") or settings.get("site_default_start_date"),
        }
        if not result.get("discovered"):
            self._log_site_event(source, "validation-empty", "全量校验未发现可同步内容")
        self._log_site_event(
            source,
            "validation-complete",
            "站点全量校验完成",
            f"发现 {result.get('discovered', 0)} 条，入库 {result.get('posts', 0)} 条，下载 {result.get('downloaded', 0)} 个，失败 {result.get('errors', 0)} 个",
        )
        self._status = {"running": False, "message": "站点全量校验完成"}
        return result

    def _sync_source(self, source: dict[str, Any], cooperate=None, max_pages: int | None = None) -> dict[str, Any]:
        if source.get("source_type") == "xhs_likes":
            raise RuntimeError("小红书来源仅支持专用适配器")
        settings = self.db.get_settings()
        source_settings = self._settings_for_source(settings, source)
        fetcher = PageFetcher(
            timeout=self._page_request_timeout(source_settings),
            user_agent=str(source_settings.get("site_user_agent")),
            proxies=self._site_proxies(source_settings),
        )
        parser = SourceParser(fetcher)
        engine = RuleEngine(self.db.get_site_rules())
        start_date = parse_date(source.get("start_date") or settings.get("site_default_start_date")) or date(2026, 4, 1)
        incremental_cutoff_date = self._latest_site_sync_date(source)
        request_sleep = max(float(settings.get("site_request_sleep") or 0), 0)
        max_media = max(int(settings.get("site_max_media_per_post") or 100), 1)
        counters: dict[str, Any] = {
            "discovered": 0,
            "posts": 0,
            "downloaded": 0,
            "blocked": 0,
            "skipped": 0,
            "no_media": 0,
            "errors": 0,
            "added_items": [],
        }

        parse_source = source
        if max_pages:
            parse_source = {**source, "max_pages": max(1, int(max_pages))}
        posts = parser.discover(parse_source, parse_assets=True)
        counters["discovered"] = len(posts)
        self._status = {
            **self._status,
            "running": True,
            "message": f"已发现 {len(posts)} 条，开始处理：{source.get('name') or source.get('slug') or source['id']}",
            "discovered": len(posts),
            "processed": 0,
            "progress": 0,
            "counters": dict(counters),
        }
        if not posts:
            return counters

        def mark_processed(processed: int) -> None:
            self._status = {
                **self._status,
                "processed": processed,
                "progress": int((processed / len(posts)) * 100) if posts else 100,
                "counters": dict(counters),
            }

        for index, parsed in enumerate(posts, start=1):
            if cooperate:
                cooperate()
            self._status = {
                **self._status,
                "running": True,
                "message": f"正在处理 {index}/{len(posts)}：{parsed.title or parsed.url}",
                "current_post": parsed.title or parsed.url,
                "processed": index - 1,
                "total": len(posts),
                "progress": int(((index - 1) / len(posts)) * 100) if posts else 0,
                "counters": dict(counters),
            }
            pub_date, date_fallback_reason = self._resolve_post_date(source, parsed, start_date)
            if date_fallback_reason:
                self.db.add_site_filter_log(source["id"], parsed.url, parsed.title, "date-fallback", date_fallback_reason)
            if pub_date and pub_date < start_date:
                counters["skipped"] += 1
                self.db.add_site_filter_log(source["id"], parsed.url, parsed.title, "skipped", "早于起始日期")
                mark_processed(index)
                continue
            if incremental_cutoff_date and pub_date and pub_date < incremental_cutoff_date:
                counters["skipped"] += 1
                self.db.add_site_filter_log(source["id"], parsed.url, parsed.title, "skipped", "早于本地最新时间")
                mark_processed(index)
                continue
            if self.db.is_site_post_in_active_trash(source["id"], parsed.url):
                counters["skipped"] += 1
                self.db.add_site_filter_log(source["id"], parsed.url, parsed.title, "skipped", "仍在内容垃圾桶")
                mark_processed(index)
                continue
            decision = engine.evaluate(parsed.title, parsed.tags)
            if not decision.allowed:
                existing_post = self.db.get_site_post_by_source_url(source["id"], parsed.url)
                if existing_post:
                    self.db.set_site_post_status(existing_post["id"], "blocked", decision.reason)
                    self.db.set_site_post_flag(existing_post["id"], "is_blocked", True)
                    self._remove_post_from_gallery(source, existing_post)
                counters["blocked"] += 1
                self.db.add_site_filter_log(source["id"], parsed.url, parsed.title, decision.decision, decision.reason)
                mark_processed(index)
                continue
            counters["posts"] += 1
            existing_post = self.db.get_site_post_by_source_url(source["id"], parsed.url)
            post = self.db.upsert_site_post(source["id"], self._post_payload(parsed))
            dynamic_id = f"site:{source['id']}:{post['id']}"
            if self.db.is_blacklisted(dynamic_id, dynamic_id):
                self.db.set_site_post_status(post["id"], "blocked", "仍在黑名单")
                self.db.set_site_post_flag(post["id"], "is_blocked", True)
                counters["blocked"] += 1
                self.db.add_site_filter_log(source["id"], parsed.url, parsed.title, "blocked", "仍在黑名单")
                mark_processed(index)
                continue
            self.db.add_site_filter_log(source["id"], parsed.url, parsed.title, decision.decision, decision.reason)
            if post.get("is_blocked"):
                if post.get("filter_reason") == "手动屏蔽":
                    self._remove_post_from_gallery(source, post)
                    mark_processed(index)
                    continue
                self.db.set_site_post_flag(post["id"], "is_blocked", False)
                self.db.set_site_post_status(post["id"], "discovered", None)
                post = self.db.get_site_post(post["id"]) or {**post, "is_blocked": False, "filter_reason": None}

            post_folder = self.storage.site_post_folder(source["slug"], parsed.pub_date, post["slug"])
            download_assets = self._apply_image_skip(parsed.assets, source)[:max_media]
            if not download_assets:
                counters["no_media"] += 1
                self.db.add_site_filter_log(source["id"], parsed.url, parsed.title, "no-media", "未发现可下载媒体")
            download_jobs = []
            saved_files = 0
            seen_urls: set[str] = set()
            for index, asset in enumerate(download_assets, start=1):
                if cooperate:
                    cooperate()
                asset_url = str(asset.url or "").strip()
                if not asset_url or asset_url in seen_urls:
                    continue
                seen_urls.add(asset_url)
                filename = clean_filename(asset.url, parsed.title, index, asset.media_type)
                db_asset = self.db.upsert_site_asset(post["id"], {"url": asset.url, "media_type": asset.media_type, "filename": filename})
                target = post_folder / filename
                if db_asset.get("status") == "duplicate":
                    continue
                if db_asset.get("status") == "ready" and target.exists() and target.stat().st_size > 0:
                    continue
                download_jobs.append({"asset_id": db_asset["id"], "url": asset.url, "target": target})
            for result in self._download_assets(download_jobs, source_settings, request_sleep):
                try:
                    if cooperate:
                        cooperate()
                    if result.get("error"):
                        raise result["error"]
                    target = result["target"]
                    self.db.set_site_asset_result(result["asset_id"], "ready", self.storage.relative_to_storage(target))
                    counters["downloaded"] += 1
                    saved_files += 1
                except Exception as exc:
                    self.db.set_site_asset_result(result["asset_id"], "failed", error=str(exc))
                    self.db.add_site_filter_log(source["id"], parsed.url, parsed.title, "download-error", f"下载失败: {exc}")
                    counters["errors"] += 1
            self.dedupe_post_images(post["id"])
            self.db.update_site_post_counts(post["id"])
            mirrored = self._mirror_post_to_gallery(source, self.db.get_site_post(post["id"]) or post, parsed)
            self._record_added_item(
                counters,
                source,
                self.db.get_site_post(post["id"]) or post,
                parsed,
                saved_files,
                existing_post is None,
                mirrored,
            )
            mark_processed(index)
        return counters

    def _record_added_item(
        self,
        counters: dict[str, Any],
        source: dict[str, Any],
        post: dict[str, Any],
        parsed: ParsedPost,
        saved_files: int,
        is_new_post: bool,
        mirrored: bool,
    ) -> None:
        if saved_files <= 0 or not mirrored:
            return
        folder_name = self._gallery_folder_name(source, post)
        folder = self.db.get_folder(folder_name)
        if not folder:
            return
        assets = self.db.list_assets_for_folder(folder_name)
        image_count = sum(1 for asset in assets if asset.get("media_type") == "image")
        if image_count <= 0:
            return
        top_dynamic_id = f"site:{source['id']}:{post['id']}"
        source_dynamic_id = top_dynamic_id
        item = {
            "top_dynamic_id": top_dynamic_id,
            "source_dynamic_id": source_dynamic_id,
            "title": str(folder.get("title") or post.get("title") or parsed.title or folder_name),
            "subscription_uid": self.site_subscription_uid(source["id"]),
            "subscription_name": str(folder.get("subscription_name") or source.get("name") or f"站点 {source['id']}"),
            "pub_time": str(folder.get("pub_time") or parsed.pub_date or ""),
            "image_count": image_count,
            "livephoto_count": 0,
            "saved_files": int(saved_files),
            "change_type": "new" if is_new_post else "updated",
        }
        items = counters.setdefault("added_items", [])
        key = (top_dynamic_id, source_dynamic_id)
        for existing in items:
            if (
                str(existing.get("top_dynamic_id") or ""),
                str(existing.get("source_dynamic_id") or ""),
            ) == key:
                existing["saved_files"] = int(existing.get("saved_files") or 0) + int(saved_files)
                existing["image_count"] = image_count
                existing["change_type"] = "new" if existing.get("change_type") == "new" or is_new_post else "updated"
                return
        items.append(item)

    def dedupe_post_images(self, post_id: int) -> int:
        ready_images = [
            asset
            for asset in self.db.list_site_assets(int(post_id))
            if asset.get("status") == "ready" and asset.get("rel_path") and asset.get("media_type") == "image"
        ]
        seen: list[tuple[int, int]] = []
        removed = 0
        for asset in ready_images:
            source_path = self.storage.resolve_storage_path(asset.get("rel_path"))
            if not source_path or not source_path.exists():
                continue
            image_hash = self._image_content_hash(source_path)
            if image_hash is None:
                continue
            if any(self._hamming_distance(image_hash, existing_hash) <= 8 for existing_hash, _asset_id in seen):
                source_path.unlink(missing_ok=True)
                self.db.set_site_asset_result(int(asset["id"]), "duplicate", error="重复图片")
                removed += 1
                continue
            seen.append((image_hash, int(asset["id"])))
        if removed:
            self.db.update_site_post_counts(int(post_id))
        return removed

    def _download_assets(self, jobs: list[dict[str, Any]], settings: dict[str, Any], request_sleep: float) -> list[dict[str, Any]]:
        if not jobs:
            return []
        worker_count = min(SITE_MEDIA_DOWNLOAD_CONCURRENCY, len(jobs))
        if worker_count <= 1:
            return [self._download_asset(job, settings, request_sleep) for job in jobs]
        results = []
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            future_map = {executor.submit(self._download_asset, job, settings, request_sleep): job for job in jobs}
            for future in as_completed(future_map):
                results.append(future.result())
        return results

    def _download_asset(self, job: dict[str, Any], settings: dict[str, Any], request_sleep: float) -> dict[str, Any]:
        target = job["target"]
        last_error: Exception | None = None
        for attempt in range(SITE_MEDIA_DOWNLOAD_RETRIES):
            try:
                downloader = self._new_media_downloader(settings)
                downloader.download(job["url"], target)
                if request_sleep:
                    time.sleep(request_sleep)
                return {**job, "target": target, "error": None}
            except Exception as exc:
                last_error = exc
                if attempt >= SITE_MEDIA_DOWNLOAD_RETRIES - 1:
                    break
                time.sleep(0.2 + attempt * 0.15)
        target.with_suffix(f"{target.suffix}.part").unlink(missing_ok=True)
        return {**job, "error": last_error}

    def _new_media_downloader(self, settings: dict[str, Any]) -> MediaDownloader:
        return MediaDownloader(
            timeout=SITE_MEDIA_DOWNLOAD_TIMEOUT,
            user_agent=str(settings.get("site_user_agent")),
            proxies=self._site_proxies(settings),
        )

    def refresh_site_icon(self, source_id: int) -> dict[str, Any]:
        source = self.db.get_site_source(source_id)
        if not source:
            raise RuntimeError("站点来源不存在")
        if source.get("source_type") == "xhs_likes":
            return source
        settings = self._settings_for_source(self.db.get_settings(), source)
        icon_url = self.discover_site_icon(source)
        cached_url, cached_tiny_url = self._cache_site_icon(source_id, icon_url, settings, str(source.get("entry_url") or ""))
        icon_url = cached_url or icon_url
        item = self.db.set_site_source_icon(source_id, icon_url, cached_tiny_url)
        if not item:
            raise RuntimeError("站点来源不存在")
        return item

    def _cache_site_icon(
        self,
        source_id: int,
        icon_url: str | None,
        settings: dict[str, Any],
        referer_url: str = "",
    ) -> tuple[str | None, str | None]:
        if not icon_url:
            return None, None
        parsed = urlparse(str(icon_url))
        if parsed.scheme == "file":
            source_path = Path(unquote(parsed.path))
            return self._cache_local_site_icon(source_id, source_path)
        if parsed.scheme not in {"http", "https"}:
            return icon_url, self._tiny_icon_url_from_local_value(source_id, str(icon_url))

        target_dir = self.storage.config.data_dir / "avatars" / "sites"
        target_dir.mkdir(parents=True, exist_ok=True)
        session = requests.Session()
        session.headers.update(browser_like_site_headers(str(settings.get("site_user_agent"))))
        session.headers["Accept"] = "image/avif,image/webp,image/png,image/svg+xml,image/*,*/*;q=0.7"
        if referer_url:
            session.headers["Referer"] = referer_url
        else:
            session.headers["Referer"] = f"{parsed.scheme}://{parsed.netloc}/"
        proxies = self._site_proxies(settings)
        if proxies:
            session.proxies.update(proxies)

        response = None
        tmp_target: Path | None = None
        try:
            response = session.get(str(icon_url), stream=True, timeout=(5, min(30, self._page_request_timeout(settings))))
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if content_type and not content_type.startswith("image/") and not self._looks_like_image_url(parsed.path):
                return None, None
            extension = self._site_icon_extension(parsed.path, content_type)
            tmp_target = target_dir / f".{int(source_id)}-{threading.get_ident()}-{time.time_ns()}{extension}.tmp"
            total = 0
            with tmp_target.open("wb") as file:
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > SITE_ICON_CACHE_MAX_BYTES:
                        tmp_target.unlink(missing_ok=True)
                        return None, None
                    file.write(chunk)
            if total <= 0:
                tmp_target.unlink(missing_ok=True)
                return None, None
            return self._commit_site_icon_file(source_id, tmp_target, extension)
        except Exception:
            if tmp_target is not None:
                tmp_target.unlink(missing_ok=True)
            return None, None
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass
            session.close()

    def _cache_local_site_icon(self, source_id: int, source_path: Path) -> tuple[str | None, str | None]:
        if not source_path.is_file() or source_path.stat().st_size <= 0 or source_path.stat().st_size > SITE_ICON_CACHE_MAX_BYTES:
            return None, None
        extension = self._site_icon_extension(source_path.name)
        target_dir = self.storage.config.data_dir / "avatars" / "sites"
        target_dir.mkdir(parents=True, exist_ok=True)
        tmp_target = target_dir / f".{int(source_id)}-{threading.get_ident()}-{time.time_ns()}{extension}.tmp"
        try:
            shutil.copyfile(source_path, tmp_target)
            return self._commit_site_icon_file(source_id, tmp_target, extension)
        except Exception:
            tmp_target.unlink(missing_ok=True)
            return None, None

    def _commit_site_icon_file(self, source_id: int, tmp_target: Path, extension: str) -> tuple[str | None, str | None]:
        revision = self._site_icon_file_revision(tmp_target)
        if not revision:
            tmp_target.unlink(missing_ok=True)
            return None, None
        target_dir = self.storage.config.data_dir / "avatars" / "sites"
        target = target_dir / f"{int(source_id)}-{revision}{extension}"
        if target.exists():
            tmp_target.unlink(missing_ok=True)
        else:
            tmp_target.replace(target)
        tiny_url = self._tiny_icon_url_from_path(source_id, target, revision=revision)
        for stale in target_dir.iterdir():
            if stale.is_file() and stale != target and re.match(rf"^{int(source_id)}(?:-|\.)", stale.name):
                stale.unlink(missing_ok=True)
        return self.storage.storage_url(self.storage.relative_to_storage(target)), tiny_url

    def _site_icon_file_revision(self, path: Path) -> str | None:
        try:
            digest = hashlib.sha256()
            with path.open("rb") as file:
                for chunk in iter(lambda: file.read(64 * 1024), b""):
                    digest.update(chunk)
            return digest.hexdigest()[:12]
        except Exception:
            return None

    def _tiny_icon_url_from_local_value(self, source_id: int, icon_url: str) -> str | None:
        parsed = urlparse(str(icon_url))
        if parsed.scheme == "file":
            return self._tiny_icon_url_from_path(source_id, Path(unquote(parsed.path)))
        if str(icon_url).startswith("/storage/"):
            rel_path = str(icon_url).removeprefix("/storage/").split("?", 1)[0]
            path = self.storage.resolve_storage_path(rel_path)
            return self._tiny_icon_url_from_path(source_id, path) if path else None
        return None

    def _tiny_icon_url_from_path(self, source_id: int, source_path: Path, revision: str | None = None) -> str | None:
        if not source_path.exists():
            return None
        resolved_revision = revision or self._site_icon_file_revision(source_path)
        if not resolved_revision:
            return None
        target_dir = self.storage.config.data_dir / "avatars" / "sites" / "tiny"
        target = target_dir / f"{int(source_id)}-{resolved_revision}.webp"
        try:
            self.thumbnailer.ensure_tiny_image_thumbnail(source_path, target)
        except Exception:
            return None
        if not target.exists():
            return None
        for stale in target_dir.glob(f"{int(source_id)}-*.webp"):
            if stale != target:
                stale.unlink(missing_ok=True)
        return self.storage.storage_url(self.storage.relative_to_storage(target))

    def discover_site_icon(self, source: dict[str, Any]) -> str | None:
        entry_url = str(source.get("entry_url") or "").strip()
        if not entry_url:
            return None
        settings = self._settings_for_source(self.db.get_settings(), source)
        candidates: list[str] = []
        homepage_candidates: list[str] = []
        try:
            fetcher = PageFetcher(
                timeout=self._page_request_timeout(settings),
                user_agent=str(settings.get("site_user_agent")),
                proxies=self._site_proxies(settings),
            )
            text = fetcher.get_text(entry_url)
            candidates.extend(self._icon_candidates_from_xml(entry_url, text))
            candidates.extend(self._icon_candidates_from_html(entry_url, text))
            candidates.extend(self._manifest_icon_candidates(fetcher, entry_url, text))
            homepage_candidates.extend(self._homepage_candidates_from_markup(entry_url, text))
            for homepage in homepage_candidates[:2]:
                if homepage == entry_url:
                    continue
                try:
                    homepage_text = fetcher.get_text(homepage)
                except Exception:
                    continue
                candidates.extend(self._icon_candidates_from_html(homepage, homepage_text))
                candidates.extend(self._manifest_icon_candidates(fetcher, homepage, homepage_text))
        except Exception:
            pass
        candidates.extend(self._fallback_icon_candidates(entry_url))
        for homepage in homepage_candidates:
            candidates.extend(self._fallback_icon_candidates(homepage))
        return self._first_reachable_icon(self._dedupe_urls(candidates), settings)

    def _icon_candidates_from_html(self, base_url: str, html: str) -> list[str]:
        soup = parse_html(html)
        link_candidates: list[str] = []
        structured_candidates: list[str] = []
        image_candidates: list[str] = []
        meta_candidates: list[str] = []
        raw_candidates: list[str] = []
        for node in self._iter_markup_nodes(soup, "link"):
            rel_value = self._node_attr(node, "rel") or ""
            if isinstance(rel_value, (list, tuple)):
                rel_text = " ".join(str(item) for item in rel_value)
            else:
                rel_text = str(rel_value)
            rel_text = rel_text.lower()
            href = self._node_attr(node, "href")
            if not href:
                continue
            href_url = urljoin(base_url, str(href))
            as_value = str(self._node_attr(node, "as") or "").lower()
            marker = f"{rel_text} {as_value} {href_url}".lower()
            if "icon" in rel_text or "image_src" in rel_text or "mask-icon" in rel_text:
                link_candidates.append(href_url)
            elif as_value == "image" and any(keyword in marker for keyword in ("logo", "icon", "favicon", "apple-touch", "cropped-")):
                image_candidates.append(href_url)
        for node in self._iter_markup_nodes(soup, "script"):
            script_type = str(self._node_attr(node, "type") or "").lower()
            if "ld+json" not in script_type:
                continue
            text = ""
            try:
                text = node.get_text(" ", strip=True)
            except Exception:
                text = ""
            structured_candidates.extend(self._icon_candidates_from_json_ld(base_url, text))
        for node in self._iter_markup_nodes(soup, "meta"):
            key = str(self._node_attr(node, "property") or self._node_attr(node, "name") or self._node_attr(node, "itemprop") or "").lower()
            if key not in {"og:image", "og:logo", "twitter:image", "twitter:image:src", "image", "thumbnail", "msapplication-tileimage"}:
                continue
            content = self._node_attr(node, "content")
            if content:
                meta_candidates.append(urljoin(base_url, str(content)))
        for node in self._iter_markup_nodes(soup, "img"):
            marker = " ".join(
                str(self._node_attr(node, key) or "")
                for key in ("class", "id", "alt", "aria-label", "title")
            ).lower()
            src = (
                self._node_attr(node, "src")
                or self._node_attr(node, "data-src")
                or self._node_attr(node, "data-original")
                or self._node_attr(node, "data-lazy-src")
            )
            if src and any(keyword in marker for keyword in ("logo", "icon", "brand", "site-logo", "avatar", "站点", "图标")):
                image_candidates.append(urljoin(base_url, str(src)))
            srcset = self._node_attr(node, "srcset") or self._node_attr(node, "data-srcset")
            if srcset and any(keyword in marker for keyword in ("logo", "icon", "brand", "site-logo")):
                if first_src := self._first_srcset_url(str(srcset)):
                    image_candidates.append(urljoin(base_url, first_src))
        for match in re.finditer(r"""["'(]([^"'()<> ]*(?:favicon|apple-touch|site-icon|logo|cropped-)[^"'()<> ]+\.(?:ico|png|jpe?g|webp|svg|gif|avif)(?:\?[^"'()<> ]*)?)["')]""", html, re.IGNORECASE):
            raw_candidates.append(urljoin(base_url, match.group(1)))
        return self._dedupe_urls(link_candidates + structured_candidates + image_candidates + meta_candidates + raw_candidates)

    def _manifest_icon_candidates(self, fetcher: PageFetcher, base_url: str, html: str) -> list[str]:
        soup = parse_html(html)
        manifest_urls: list[str] = []
        for node in self._iter_markup_nodes(soup, "link"):
            rel_value = self._node_attr(node, "rel") or ""
            rel_text = " ".join(str(item) for item in rel_value) if isinstance(rel_value, (list, tuple)) else str(rel_value)
            if "manifest" not in rel_text.lower():
                continue
            href = self._node_attr(node, "href")
            if href:
                manifest_urls.append(urljoin(base_url, str(href)))
        parsed = urlparse(base_url)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            root = f"{parsed.scheme}://{parsed.netloc}/"
            manifest_urls.extend([urljoin(root, "site.webmanifest"), urljoin(root, "manifest.json")])
        candidates: list[str] = []
        for manifest_url in self._dedupe_urls(manifest_urls):
            try:
                manifest = json.loads(fetcher.get_text(manifest_url))
            except Exception:
                continue
            for icon in manifest.get("icons") or []:
                if isinstance(icon, dict) and icon.get("src"):
                    candidates.append(urljoin(manifest_url, str(icon["src"])))
        return self._dedupe_urls(candidates)

    def _icon_candidates_from_json_ld(self, base_url: str, text: str) -> list[str]:
        if not text:
            return []
        try:
            payload = json.loads(text)
        except Exception:
            return []
        candidates: list[str] = []

        def visit(value: Any) -> None:
            if isinstance(value, dict):
                value_type = value.get("@type")
                types = value_type if isinstance(value_type, list) else [value_type]
                is_site_entity = any(str(item).lower() in {"organization", "website", "webpage"} for item in types if item)
                if is_site_entity:
                    for key in ("logo", "image"):
                        collect_image_value(value.get(key))
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        def collect_image_value(value: Any) -> None:
            if isinstance(value, str) and self._looks_like_image_url(value):
                candidates.append(urljoin(base_url, value))
            elif isinstance(value, dict):
                for key in ("url", "src", "contentUrl"):
                    raw = value.get(key)
                    if raw and self._looks_like_image_url(str(raw)):
                        candidates.append(urljoin(base_url, str(raw)))
                for child in value.values():
                    if isinstance(child, (dict, list)):
                        collect_image_value(child)
            elif isinstance(value, list):
                for child in value:
                    collect_image_value(child)

        visit(payload)
        return self._dedupe_urls(candidates)

    def _icon_candidates_from_xml(self, base_url: str, text: str) -> list[str]:
        stripped = text.lstrip()
        if not stripped.startswith("<"):
            return []
        try:
            root = ElementTree.fromstring(stripped.encode("utf-8"))
        except ElementTree.ParseError:
            return []
        candidates: list[str] = []
        for node in root.iter():
            name = self._xml_name(node.tag)
            if name in {"image", "logo", "icon"}:
                for attr in ("href", "url", "src"):
                    if node.get(attr):
                        candidates.append(urljoin(base_url, str(node.get(attr))))
                if node.text and node.text.strip():
                    candidates.append(urljoin(base_url, node.text.strip()))
            if name == "url" and node.text and self._looks_like_image_url(node.text):
                candidates.append(urljoin(base_url, node.text.strip()))
            for attr in ("href", "url", "src"):
                value = node.get(attr)
                if value and self._looks_like_image_url(value):
                    candidates.append(urljoin(base_url, str(value)))
        return self._dedupe_urls(candidates)

    def _homepage_candidates_from_markup(self, base_url: str, text: str) -> list[str]:
        candidates: list[str] = []
        soup = parse_html(text)
        for node in self._iter_markup_nodes(soup, "link"):
            rel_text = str(self._node_attr(node, "rel") or "").lower()
            href = self._node_attr(node, "href")
            if href and any(keyword in rel_text for keyword in ("home", "canonical", "alternate")):
                candidates.append(urljoin(base_url, str(href)))
        try:
            root = ElementTree.fromstring(text.encode("utf-8"))
            for node in root.iter():
                if self._xml_name(node.tag) == "link" and node.text and node.text.strip():
                    candidates.append(urljoin(base_url, node.text.strip()))
                    break
        except ElementTree.ParseError:
            pass
        return self._dedupe_urls([url for url in candidates if urlparse(url).scheme in {"http", "https", "file"}])

    def _fallback_icon_candidates(self, url: str) -> list[str]:
        parsed = urlparse(url)
        names = (
            "favicon.ico",
            "favicon.png",
            "favicon.svg",
            "favicon-32x32.png",
            "favicon-16x16.png",
            "apple-touch-icon.png",
            "apple-touch-icon-precomposed.png",
            "logo.png",
            "logo.svg",
        )
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            root = f"{parsed.scheme}://{parsed.netloc}/"
            domain = parsed.netloc.split("@")[-1]
            return [
                *[urljoin(root, name) for name in names],
                f"https://www.google.com/s2/favicons?domain={domain}&sz=128",
                f"https://icons.duckduckgo.com/ip3/{domain}.ico",
            ]
        if parsed.scheme == "file" and parsed.path:
            base = Path(parsed.path)
            directory = base if base.is_dir() else base.parent
            return [(directory / name).resolve().as_uri() for name in names]
        return []

    def _first_reachable_icon(self, candidates: list[str], settings: dict[str, Any]) -> str | None:
        session = requests.Session()
        session.headers.update(browser_like_site_headers(str(settings.get("site_user_agent"))))
        session.headers["Accept"] = "image/avif,image/webp,image/png,image/svg+xml,image/*,*/*;q=0.6"
        proxies = self._site_proxies(settings)
        if proxies:
            session.proxies.update(proxies)
        try:
            for url in candidates:
                parsed = urlparse(str(url))
                if parsed.scheme == "file":
                    if parsed.path and Path(parsed.path).exists() and self._looks_like_image_url(parsed.path):
                        return str(url)
                    continue
                if parsed.scheme not in {"http", "https"}:
                    continue
                response = None
                try:
                    response = session.get(str(url), stream=True, timeout=(5, min(20, self._page_request_timeout(settings))))
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "").lower()
                    if content_type.startswith("image/") or self._looks_like_image_url(parsed.path):
                        return str(url)
                except requests.RequestException:
                    continue
                finally:
                    if response is not None:
                        try:
                            response.close()
                        except Exception:
                            pass
        finally:
            session.close()
        return None

    def _iter_markup_nodes(self, root: Any, name: str | None = None) -> list[Any]:
        if hasattr(root, "find_all"):
            return list(root.find_all(name or True))
        output: list[Any] = []

        def visit(node: Any) -> None:
            for child in getattr(node, "children", []) or []:
                if name is None or getattr(child, "name", None) == name:
                    output.append(child)
                visit(child)

        visit(root)
        return output

    def _node_attr(self, node: Any, key: str) -> Any:
        try:
            return node.get(key)
        except Exception:
            return None

    def _first_srcset_url(self, srcset: str) -> str | None:
        for part in srcset.split(","):
            url = part.strip().split(" ", 1)[0].strip()
            if url:
                return url
        return None

    def _looks_like_image_url(self, value: str) -> bool:
        path = urlparse(str(value)).path.lower()
        return path.endswith(SITE_ICON_EXTENSIONS)

    def _site_icon_extension(self, path: str, content_type: str = "") -> str:
        suffix = Path(urlparse(path).path).suffix.lower()
        if suffix in SITE_ICON_EXTENSIONS:
            return ".jpg" if suffix == ".jpeg" else suffix
        content_type = content_type.split(";", 1)[0].strip().lower()
        return {
            "image/x-icon": ".ico",
            "image/vnd.microsoft.icon": ".ico",
            "image/icon": ".ico",
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/jpg": ".jpg",
            "image/webp": ".webp",
            "image/svg+xml": ".svg",
            "image/gif": ".gif",
            "image/avif": ".avif",
        }.get(content_type, ".ico")

    def _dedupe_urls(self, urls: list[str]) -> list[str]:
        seen: set[str] = set()
        output = []
        for url in urls:
            normalized = str(url or "").strip()
            if not normalized or normalized.startswith("data:") or normalized in seen:
                continue
            seen.add(normalized)
            output.append(normalized)
        return output

    def _xml_name(self, tag: str) -> str:
        return tag.rsplit("}", 1)[-1].lower()

    def _settings_for_source(self, settings: dict[str, Any], source: dict[str, Any] | None) -> dict[str, Any]:
        use_proxy = True if source is None else bool(source.get("use_proxy", True))
        return {**settings, "_site_proxy_allowed": use_proxy}

    def _site_proxies(self, settings: dict[str, Any]) -> dict[str, str] | None:
        if not settings.get("site_proxy_enabled") or not settings.get("_site_proxy_allowed", True):
            return None
        host = str(settings.get("site_proxy_host") or "127.0.0.1").strip() or "127.0.0.1"
        try:
            port = int(settings.get("site_proxy_port") or 7890)
        except (TypeError, ValueError):
            port = 7890
        port = max(1, min(port, 65535))
        proxy = f"http://{host}:{port}"
        return {"http": proxy, "https": proxy}

    def _site_proxy_summary(self, settings: dict[str, Any]) -> dict[str, Any]:
        proxies = self._site_proxies(settings)
        if not proxies:
            return {"enabled": False}
        return {"enabled": True, "http": proxies["http"], "https": proxies["https"]}

    def _log_site_event(self, source: dict[str, Any], decision: str, title: str, reason: str = "") -> None:
        self.db.add_site_filter_log(
            source.get("id"),
            str(source.get("entry_url") or ""),
            title,
            decision,
            reason or str(source.get("name") or source.get("slug") or source.get("id") or ""),
        )

    def _page_request_timeout(self, settings: dict[str, Any]) -> int:
        try:
            timeout = int(settings.get("site_request_timeout") or SITE_PAGE_REQUEST_TIMEOUT)
        except (TypeError, ValueError):
            timeout = SITE_PAGE_REQUEST_TIMEOUT
        return max(30, min(timeout, 900))

    def _resolve_post_date(self, source: dict[str, Any], parsed: ParsedPost, start_date: date) -> tuple[date, str]:
        pub_date = parse_date(parsed.pub_date)
        if pub_date:
            parsed.pub_date = pub_date.isoformat()
            return pub_date, ""
        existing = self.db.get_site_post_by_source_url(int(source["id"]), parsed.url)
        existing_date = parse_date(existing.get("pub_date") if existing else None)
        if existing_date:
            parsed.pub_date = existing_date.isoformat()
            return existing_date, ""
        for value in (parsed.url, parsed.title, parsed.excerpt):
            inferred = parse_date(value)
            if inferred:
                parsed.pub_date = inferred.isoformat()
                return inferred, "未发现发布日期，已从链接或标题推断"
        fallback = max(datetime.now(TIMEZONE).date(), start_date)
        parsed.pub_date = fallback.isoformat()
        return fallback, "未发现发布日期，已使用同步日期"

    def _latest_site_sync_date(self, source: dict[str, Any]) -> date | None:
        source_id = int(source["id"])
        dates: list[date] = []
        for folder in self.db.list_site_gallery_folders(source_id):
            pub_ts = int(folder.get("pub_ts") or 0)
            if pub_ts > 0:
                dates.append(datetime.fromtimestamp(pub_ts, TIMEZONE).date())
        for post in self.db.list_site_posts(source_id=source_id):
            pub_date = parse_date(post.get("pub_date"))
            if pub_date:
                dates.append(pub_date)
        return max(dates) if dates else None

    def _remove_post_from_gallery(self, source: dict[str, Any], post: dict[str, Any]) -> None:
        fallback_folder_name = self._gallery_folder_name(source, post)
        folder_names = self.db.delete_site_gallery_post(source["id"], post["id"], fallback_folder_name)
        for folder_name in folder_names:
            self.storage.remove_folder_assets(folder_name)

    def _mirror_post_to_gallery(self, source: dict[str, Any], post: dict[str, Any], parsed: ParsedPost) -> bool:
        ready_images = [
            asset
            for asset in self.db.list_site_assets(int(post["id"]))
            if asset.get("status") == "ready" and asset.get("rel_path") and asset.get("media_type") == "image"
        ]
        ready_videos = [
            asset
            for asset in self.db.list_site_assets(int(post["id"]))
            if asset.get("status") == "ready" and asset.get("rel_path") and asset.get("media_type") == "video"
        ]
        if not ready_images:
            return False

        folder_name = self._gallery_folder_name(source, post)
        image_folder = self.storage.image_folder(folder_name)
        image_folder.mkdir(parents=True, exist_ok=True)
        for existing in image_folder.iterdir():
            if existing.is_file() and not existing.name.startswith("."):
                existing.unlink(missing_ok=True)
        for pair_index, asset in enumerate(ready_images, start=1):
            source_path = self.storage.resolve_storage_path(asset.get("rel_path"))
            if not source_path or not source_path.exists():
                continue
            target = image_folder / f"{pair_index:03d}__{asset['filename']}"
            if not target.exists() or target.stat().st_mtime < source_path.stat().st_mtime:
                shutil.copy2(source_path, target)

        pub_ts = self._pub_ts(parsed.pub_date)
        top_dynamic_id = f"site:{source['id']}:{post['id']}"
        source_dynamic_id = f"site:{source['id']}:{post['id']}"
        subscription_uid = self.site_subscription_uid(source["id"])
        subscription_name = str(source.get("name") or post.get("source_name") or f"站点 {source['id']}")
        title = str(post.get("title") or parsed.title or folder_name)
        excerpt = str(post.get("excerpt") or parsed.excerpt or "")
        metadata = {
            "source": "site",
            "site_post_id": int(post["id"]),
            "site_post_url": str(post.get("url") or parsed.url or ""),
        }

        if self.indexer:
            self.indexer.index_folder(
                folder_name=folder_name,
                pub_ts=pub_ts,
                title=title,
                text_prefix=excerpt,
                top_dynamic_id=top_dynamic_id,
                source_dynamic_id=source_dynamic_id,
                subscription_uid=subscription_uid,
                subscription_name=subscription_name,
                metadata=metadata,
            )
            self._replace_gallery_videos(folder_name, ready_videos)
            return True

        image_assets = [
            {
                "pair_index": index,
                "filename": path.name,
                "rel_path": self.storage.relative_to_storage(path),
                "metadata": {"kind": "site-image"},
            }
            for index, path in enumerate(sorted(image_folder.iterdir()), start=1)
            if path.is_file() and not path.name.startswith(".")
        ]
        self.db.upsert_folder(
            {
                "folder_name": folder_name,
                "title": title,
                "text_prefix": excerpt,
                "pub_ts": pub_ts,
                "pub_time": datetime.fromtimestamp(pub_ts, TIMEZONE).strftime("%Y-%m-%d %H:%M:%S"),
                "top_dynamic_id": top_dynamic_id,
                "source_dynamic_id": source_dynamic_id,
                "subscription_uid": subscription_uid,
                "subscription_name": subscription_name,
                "has_images": bool(image_assets),
                "has_livephoto": False,
                "metadata": metadata,
            }
        )
        self.db.replace_folder_assets(folder_name, "image", image_assets)
        self._replace_gallery_videos(folder_name, ready_videos)
        return bool(image_assets)

    def _replace_gallery_videos(self, folder_name: str, ready_videos: list[dict[str, Any]]) -> None:
        video_assets = []
        for index, asset in enumerate(ready_videos, start=1):
            rel_path = asset.get("rel_path")
            if not rel_path:
                continue
            video_assets.append(
                {
                    "pair_index": index,
                    "filename": asset["filename"],
                    "rel_path": rel_path,
                    "metadata": {"kind": "site-video"},
                }
            )
        self.db.replace_folder_assets(folder_name, "video", video_assets)

    def _gallery_folder_name(self, source: dict[str, Any], post: dict[str, Any]) -> str:
        title = str(post.get("title") or "post")
        return f"site_{source['id']}_{post['id']}_{safe_slug(title, 'post', 48)}"

    def _pub_ts(self, value: str | None) -> int:
        parsed = parse_date(value)
        if not parsed:
            return 0
        return int(datetime.combine(parsed, datetime_time.min, tzinfo=TIMEZONE).timestamp())

    def mirror_existing_site_post(self, post: dict[str, Any]) -> None:
        source = {
            "id": post["source_id"],
            "slug": post.get("source_slug") or f"site-{post['source_id']}",
            "name": post.get("source_name") or f"站点 {post['source_id']}",
        }
        parsed = ParsedPost(
            url=str(post.get("url") or ""),
            title=str(post.get("title") or ""),
            pub_date=post.get("pub_date"),
            tags=[],
            excerpt=str(post.get("excerpt") or ""),
            assets=[],
        )
        self._mirror_post_to_gallery(source, post, parsed)

    @staticmethod
    def _image_content_hash(path) -> int | None:
        try:
            with Image.open(path) as image:
                normalized = ImageOps.exif_transpose(image).convert("L").resize((16, 16), Image.Resampling.LANCZOS)
                pixels = list(normalized.getdata())
        except Exception:
            return None
        average = sum(pixels) / len(pixels)
        value = 0
        for pixel in pixels:
            value = (value << 1) | (1 if pixel >= average else 0)
        return value

    @staticmethod
    def _hamming_distance(left: int, right: int) -> int:
        return (left ^ right).bit_count()

    @staticmethod
    def site_subscription_uid(source_id: int | str) -> str:
        return f"site:{source_id}"

    def _apply_image_skip(self, assets: list[Any], source: dict[str, Any]) -> list[Any]:
        skip_head = max(int(source.get("skip_head_images") or 0), 0)
        skip_tail = max(int(source.get("skip_tail_images") or 0), 0)
        if not skip_head and not skip_tail:
            return list(assets)

        image_positions = [index for index, asset in enumerate(assets) if asset.media_type == "image"]
        kept_image_positions = set(image_positions[skip_head:])
        if skip_tail:
            kept_image_positions -= set(image_positions[-skip_tail:])

        output = []
        for index, asset in enumerate(assets):
            if asset.media_type != "image" or index in kept_image_positions:
                output.append(asset)
        return output

    def _post_payload(self, parsed: ParsedPost) -> dict[str, Any]:
        return {
            "url": parsed.url,
            "title": parsed.title,
            "pub_date": parsed.pub_date,
            "tags": parsed.tags,
            "excerpt": parsed.excerpt[:1000],
            "status": "discovered",
        }

    def _preview_dict(self, post: ParsedPost) -> dict[str, Any]:
        return {
            "url": post.url,
            "title": post.title,
            "pub_date": post.pub_date,
            "tags": post.tags,
            "excerpt": post.excerpt[:240],
            "assets": [{"url": asset.url, "media_type": asset.media_type} for asset in post.assets],
        }
