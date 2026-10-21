from __future__ import annotations

import os
import sqlite3
import random
import threading
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, Iterator

from app.services.utils import dumps_json, loads_json, now_iso, safe_slug
from app.services.xhs.schema import migrate as migrate_xhs
from app.theme import DEFAULT_THEME as DEFAULT_UI_THEME
from app.services.database_layout import module_ddl, install_hot_indexes


DEFAULT_SETTINGS = {
    "host_mid": 31968078,
    "app_title": os.environ.get("APP_TITLE", "RC-WebGallery"),
    "pull_images": True,
    "image_min_count": 6,
    "pull_livephoto": True,
    "include_forwarded": True,
    "subscription_policy_migrated": False,
    "image_threshold_default_migrated": False,
    "reload_all_once": False,
    "auto_load_enabled": True,
    "wide_single_cover": False,
    "random_cover_enabled": True,
    "single_thumbnail_migrated": True,
    "download_sleep": 0.2,
    "scheduler_enabled": False,
    "scheduler_interval_hours": 12,
    "site_scheduler_enabled": False,
    "site_scheduler_interval_hours": 12,
    "ad_filter_enabled": True,
    "ad_filter_keywords": [
        "推广",
        "广告",
        "合作",
        "恰饭",
        "课程",
        "优惠",
        "抽奖",
        "预约",
        "链接",
    ],
    "long_image_ratio": 3.0,
    "duplicate_image_threshold": 2,
    "gallery_index_version": 0,
    "gallery_index_rebuilt_at": None,
    "gallery_index_rebuilding": False,
    "auto_gallery_index_check": True,
    "auto_storage_cleanup": False,
    "site_default_start_date": "2026-04-01",
    "site_request_sleep": 0.4,
    "site_request_timeout": 300,
    "site_max_media_per_post": 100,
    "site_user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
    "site_proxy_enabled": False,
    "site_proxy_host": "127.0.0.1",
    "site_proxy_port": 7890,
    "review_source_open_mode": "browser",
    "small_thumb_edge": 368,
    "small_thumb_quality": 50,
    "video_default_loop": True,
    "video_default_volume": 0,
    # 界面配色：色相 / 饱和度 / 明度三个参数，整套色板由 app/theme.py 派生。
    "ui_theme": dict(DEFAULT_UI_THEME),
    "storage_stats_cache": {
        "image_bytes": 0,
        "thumbnail_bytes": 0,
        "trash_bytes": 0,
        "image_files": 0,
        "thumbnail_files": 0,
        "trash_files": 0,
        "updated_at": None,
    },
}
LEGACY_SITE_USER_AGENTS = {
    "PostArchiver/0.1 (+authorized personal archive)",
    "PostArchiver/0.1",
}
LEGACY_SITE_REQUEST_TIMEOUTS = {20, 120}
GALLERY_INDEX_VERSION = 4

DEFAULT_SITE_RULES = {
    "mode": "blacklist",
    "keywords": [],
    "allow_keywords": [],
    "block_keywords": [],
    "title_allow": [],
    "title_block": [],
    "tag_allow": [],
    "tag_block": [],
    "use_regex": False,
}


class Database:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.modules_enabled = False
        self.exclusive_maintenance = False
        self._anchors: list[sqlite3.Connection] = []
        self._stats_lock = threading.RLock()
        self._stats_cache: dict[str, tuple[tuple, dict]] = {}
        if db_path.exists():
            with closing(sqlite3.connect(db_path)) as conn:
                if conn.execute("select 1 from sqlite_master where name='data_layout'").fetchone():
                    layout = conn.execute('select version from data_layout where id=1').fetchone()
                    self.modules_enabled = bool(layout and layout[0] >= 1)

    def keep_hot(self) -> None:
        """Keep WALs open between requests instead of checkpointing on every close.

        These idle connections never own transactions; request connections remain
        thread-local. Exclusive maintenance closes them before changing schemas.
        """
        self.close()
        for name in ('app', 'control', 'browse') if self.modules_enabled else ('app',):
            path = self.db_path if name == 'app' else self.db_path.with_name(f'{name}.db')
            conn = sqlite3.connect(path, check_same_thread=False)
            conn.execute('pragma journal_mode=wal')
            conn.execute('pragma query_only=on')
            self._anchors.append(conn)

    def close(self) -> None:
        with self._stats_lock:
            self._stats_cache.clear()
        for conn in self._anchors:
            conn.close()
        self._anchors.clear()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("pragma foreign_keys = on")
            mode = 'delete' if self.exclusive_maintenance else 'wal'
            sync = 'full' if self.exclusive_maintenance else 'normal'
            conn.execute(f'pragma journal_mode={mode}')
            conn.execute(f'pragma synchronous={sync}')
            conn.execute("pragma temp_store = memory")
            conn.execute("pragma cache_size = -12000")
            if self.modules_enabled:
                for module in ('control', 'browse'):
                    path = self.db_path.with_name(f'{module}.db')
                    if not path.is_file():
                        raise RuntimeError(f'缺少数据库模块：{path.name}，请恢复完整 config 备份')
                    conn.execute(f'attach database ? as {module}', (str(path),))
                    conn.execute(f'pragma {module}.journal_mode={mode}')
                    conn.execute(f'pragma {module}.synchronous={sync}')
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init(self) -> None:
        with self.connect() as conn:
            conn.executescript(module_ddl(
                """
                create table if not exists settings (
                    key text primary key,
                    value text not null
                );

                create table if not exists auth_state (
                    id integer primary key check (id = 1),
                    cookie text,
                    cookie_json text,
                    user_json text,
                    qr_key text,
                    qr_url text,
                    qr_status text,
                    qr_created_at text
                );

                create table if not exists folders (
                    folder_name text primary key,
                    title text,
                    text_prefix text,
                    pub_ts integer not null default 0,
                    pub_time text not null,
                    top_dynamic_id text not null,
                    source_dynamic_id text not null,
                    has_images integer not null default 0,
                    has_livephoto integer not null default 0,
                    status text not null default 'active',
                    review_status text not null default 'approved',
                    review_reason text,
                    metadata_json text,
                    created_at text not null,
                    updated_at text not null,
                    unique(top_dynamic_id, source_dynamic_id)
                );

                create table if not exists assets (
                    id integer primary key autoincrement,
                    folder_name text not null references folders(folder_name) on delete cascade,
                    media_type text not null,
                    pair_index integer not null default 0,
                    filename text not null,
                    rel_path text not null,
                    thumb_rel_path text,
                    small_thumb_rel_path text,
                    tiny_thumb_rel_path text,
                    cover_rel_path text,
                    reverse_rel_path text,
                    width integer,
                    height integer,
                    status text not null default 'ready',
                    metadata_json text,
                    created_at text not null,
                    updated_at text not null,
                    unique(folder_name, media_type, filename)
                );

                -- Durable identity, independent of rebuilt assets/pair_index caches.
                create table if not exists livephoto_pairs (
                    folder_name text not null,
                    pair_index integer not null,
                    content_id text not null unique,
                    image_path text,
                    video_path text,
                    image_checksum text,
                    video_checksum text,
                    status text not null default 'pending',
                    auto_pair integer not null default 0,
                    version integer not null default 0,
                    error text,
                    transaction_json text,
                    updated_at text not null,
                    primary key(folder_name, pair_index)
                );

                create table if not exists task_runs (
                    id integer primary key autoincrement,
                    task_type text not null,
                    status text not null,
                    message text,
                    details_json text,
                    created_at text not null,
                    finished_at text
                );

                create table if not exists review_items (
                    id integer primary key autoincrement,
                    top_dynamic_id text not null,
                    source_dynamic_id text not null,
                    folder_name_candidate text,
                    text_excerpt text,
                    reasons_json text not null,
                    payload_json text not null,
                    status text not null default 'pending',
                    created_at text not null,
                    updated_at text not null,
                    unique(top_dynamic_id, source_dynamic_id)
                );

                create table if not exists filter_logs (
                    id integer primary key autoincrement,
                    top_dynamic_id text not null,
                    source_dynamic_id text not null,
                    folder_name_candidate text,
                    decision text not null,
                    reasons_json text not null,
                    created_at text not null
                );

                create table if not exists blacklist_items (
                    id integer primary key autoincrement,
                    top_dynamic_id text not null,
                    source_dynamic_id text not null,
                    folder_name text,
                    title text,
                    reason text,
                    created_at text not null,
                    unique(top_dynamic_id, source_dynamic_id)
                );

                create table if not exists deleted_pair_marks (
                    id integer primary key autoincrement,
                    top_dynamic_id text not null,
                    source_dynamic_id text not null,
                    folder_name text,
                    pair_index integer not null,
                    reason text,
                    created_at text not null,
                    unique(top_dynamic_id, source_dynamic_id, pair_index)
                );

                create table if not exists trash_items (
                    id integer primary key autoincrement,
                    top_dynamic_id text not null,
                    source_dynamic_id text not null,
                    folder_name text not null,
                    title text,
                    folder_json text not null,
                    assets_json text not null,
                    deleted_at text not null,
                    restored_at text,
                    unique(top_dynamic_id, source_dynamic_id)
                );

                create table if not exists duplicate_ignores (
                    signature text primary key,
                    folder_names_json text not null,
                    ignored_at text not null
                );

                create table if not exists duplicate_group_cache (
                    id integer primary key check(id = 1),
                    cache_key text not null,
                    groups_json text not null,
                    updated_at text not null
                );

                create table if not exists subscriptions (
                    uid text primary key,
                    uname text,
                    avatar_url text,
                    avatar_tiny_url text,
                    status text not null default 'active',
                    pull_images integer not null default 1,
                    image_min_count integer not null default 1,
                    pull_livephoto integer not null default 1,
                    include_forwarded integer not null default 1,
                    created_at text not null,
                    updated_at text not null
                );

                create table if not exists site_rules (
                    key text primary key,
                    value text not null
                );

                create table if not exists site_sources (
                    id integer primary key autoincrement,
                    name text not null,
                    slug text not null unique,
                    source_type text not null,
                    entry_url text not null,
                    page_url_template text,
                    max_pages integer not null default 1,
                    list_item_selector text,
                    detail_link_selector text,
                    title_selector text,
                    date_selector text,
                    tag_selector text,
                    body_selector text,
                    media_selector text,
                    skip_head_images integer not null default 0,
                    skip_tail_images integer not null default 0,
                    use_proxy integer not null default 1,
                    icon_url text,
                    icon_tiny_url text,
                    enabled integer not null default 1,
                    start_date text,
                    created_at text not null,
                    updated_at text not null
                );

                create table if not exists site_posts (
                    id integer primary key autoincrement,
                    source_id integer not null references site_sources(id) on delete cascade,
                    url text not null,
                    title text not null,
                    slug text not null,
                    pub_date text,
                    tags_json text not null default '[]',
                    excerpt text,
                    status text not null default 'new',
                    filter_reason text,
                    is_favorite integer not null default 0,
                    is_blocked integer not null default 0,
                    asset_count integer not null default 0,
                    downloaded_count integer not null default 0,
                    created_at text not null,
                    updated_at text not null,
                    unique(source_id, url)
                );

                create table if not exists site_assets (
                    id integer primary key autoincrement,
                    post_id integer not null references site_posts(id) on delete cascade,
                    url text not null,
                    media_type text not null,
                    filename text not null,
                    rel_path text,
                    status text not null default 'pending',
                    error text,
                    created_at text not null,
                    updated_at text not null,
                    unique(post_id, url)
                );

                create table if not exists site_filter_logs (
                    id integer primary key autoincrement,
                    source_id integer,
                    post_url text not null,
                    title text,
                    decision text not null,
                    reason text,
                    created_at text not null
                );

                create table if not exists sidebar_count_cache (
                    key text primary key,
                    count integer not null default 0,
                    label text,
                    payload_json text,
                    updated_at text not null
                );

                create table if not exists folder_index (
                    folder_name text primary key references folders(folder_name) on delete cascade,
                    title text,
                    text_prefix text,
                    pub_ts integer not null default 0,
                    pub_time text not null,
                    top_dynamic_id text not null,
                    source_dynamic_id text not null,
                    subscription_uid text,
                    subscription_name text,
                    has_images integer not null default 0,
                    has_livephoto integer not null default 0,
                    is_favorite integer not null default 0,
                    review_status text not null default 'approved',
                    review_reason text,
                    image_count integer not null default 0,
                    livephoto_count integer not null default 0,
                    asset_count integer not null default 0,
                    preview_assets_json text not null default '[]',
                    year_key text,
                    month_key text,
                    updated_at text not null
                );

                create table if not exists pair_index (
                    item_key text primary key,
                    folder_name text not null references folders(folder_name) on delete cascade,
                    pair_index integer not null,
                    title text,
                    pub_ts integer not null default 0,
                    pub_time text not null,
                    subscription_uid text,
                    subscription_name text,
                    is_favorite integer not null default 0,
                    has_image integer not null default 0,
                    has_livephoto integer not null default 0,
                    preview_url text,
                    preview_kind text,
                    thumb_url text,
                    display_ratio text,
                    image_json text,
                    livephoto_json text,
                    year_key text,
                    month_key text,
                    updated_at text not null,
                    unique(folder_name, pair_index)
                );

                create table if not exists gallery_page_cache (
                    cache_key text primary key,
                    signature text not null,
                    payload_json text not null,
                    updated_at text not null
                );

                create index if not exists idx_folders_pub_ts on folders(pub_ts desc);
                create index if not exists idx_folders_review_pub_ts on folders(review_status, pub_ts desc);
                create index if not exists idx_folders_updated on folders(updated_at desc);
                create index if not exists idx_assets_folder on assets(folder_name, media_type, pair_index);
                create index if not exists idx_assets_folder_pair_media on assets(folder_name, pair_index, media_type);
                create index if not exists idx_assets_updated on assets(updated_at desc);
                create index if not exists idx_review_status on review_items(status, updated_at desc);
                create index if not exists idx_filter_logs_created on filter_logs(created_at desc);
                create index if not exists idx_blacklist_items_dynamic on blacklist_items(top_dynamic_id, source_dynamic_id);
                create index if not exists idx_deleted_pair_marks_dynamic on deleted_pair_marks(top_dynamic_id, source_dynamic_id, pair_index);
                create index if not exists idx_trash_items_deleted on trash_items(deleted_at desc);
                create index if not exists idx_duplicate_ignores_ignored on duplicate_ignores(ignored_at desc);
                create index if not exists idx_subscriptions_status on subscriptions(status, updated_at desc);
                create index if not exists idx_site_posts_source_date on site_posts(source_id, pub_date desc);
                create index if not exists idx_site_posts_flags on site_posts(is_favorite, is_blocked, status);
                create index if not exists idx_site_assets_post on site_assets(post_id, status);
                create index if not exists idx_site_filter_logs_created on site_filter_logs(created_at desc);
                create index if not exists idx_folder_index_pub_ts on folder_index(pub_ts desc, folder_name desc);
                create index if not exists idx_folder_index_subscription_pub_ts on folder_index(subscription_uid, pub_ts desc, folder_name desc);
                create index if not exists idx_folder_index_review_pub_ts on folder_index(review_status, pub_ts desc, folder_name desc);
                create index if not exists idx_folder_index_favorite_pub_ts on folder_index(is_favorite, pub_ts desc, folder_name desc);
                create index if not exists idx_folder_index_updated on folder_index(updated_at desc);
                create index if not exists idx_pair_index_pub_ts on pair_index(pub_ts desc, folder_name desc, pair_index asc);
                create index if not exists idx_pair_index_subscription_pub_ts on pair_index(subscription_uid, pub_ts desc, folder_name desc, pair_index asc);
                create index if not exists idx_pair_index_folder_pair on pair_index(folder_name, pair_index asc);
                create index if not exists idx_pair_index_updated on pair_index(updated_at desc);
                create index if not exists idx_gallery_page_cache_updated on gallery_page_cache(updated_at desc);
                """, self.modules_enabled
            ))
            self._ensure_column(conn, "folders", "subscription_uid", "text")
            self._ensure_column(conn, "folders", "subscription_name", "text")
            self._ensure_column(conn, "folders", "is_favorite", "integer not null default 0")
            self._ensure_column(conn, "assets", "small_thumb_rel_path", "text")
            self._ensure_column(conn, "assets", "tiny_thumb_rel_path", "text")
            self._ensure_column(conn, "assets", "duplicate_fingerprint", "text")
            conn.execute(
                "create index if not exists idx_folders_subscription_pub_ts on folders(subscription_uid, pub_ts desc)"
            )
            self._ensure_column(conn, "subscriptions", "pull_images", "integer not null default 1")
            self._ensure_column(conn, "subscriptions", "image_min_count", "integer not null default 1")
            self._ensure_column(conn, "subscriptions", "pull_livephoto", "integer not null default 1")
            self._ensure_column(conn, "subscriptions", "include_forwarded", "integer not null default 1")
            self._ensure_column(conn, "subscriptions", "avatar_url", "text")
            self._ensure_column(conn, "subscriptions", "avatar_tiny_url", "text")
            self._ensure_column(conn, "site_sources", "skip_head_images", "integer not null default 0")
            self._ensure_column(conn, "site_sources", "skip_tail_images", "integer not null default 0")
            self._ensure_column(conn, "site_sources", "use_proxy", "integer not null default 1")
            self._ensure_column(conn, "site_sources", "icon_url", "text")
            self._ensure_column(conn, "site_sources", "icon_tiny_url", "text")
            self._ensure_sidebar_count_cache(conn)
            self._ensure_default_settings(conn)
            self._ensure_default_site_rules(conn)
            conn.execute("insert or ignore into auth_state(id, qr_status) values (1, 'idle')")
            self._ensure_default_subscription(conn)
            self._migrate_subscription_policies(conn)
            self._migrate_image_threshold_defaults(conn)
            self._backfill_folder_subscriptions(conn)
            migrate_xhs(conn)
            self._ensure_column(conn, 'livephoto_pairs', 'auto_pair', 'integer not null default 0')
            self._ensure_column(conn, 'livephoto_pairs', 'confirmed_pair', 'integer not null default 0')
            install_hot_indexes(self, conn)

    @contextmanager
    def control_connect(self) -> Iterator[sqlite3.Connection]:
        if not self.modules_enabled:
            with self.connect() as conn:
                yield conn
            return
        path = self.db_path.with_name('control.db')
        if not path.is_file():
            raise RuntimeError('缺少 control.db，请恢复完整 config 备份')
        conn = sqlite3.connect(path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _ensure_default_settings(self, conn: sqlite3.Connection) -> None:
        existing_rows = conn.execute("select key, value from settings").fetchall()
        existing_settings = {row["key"]: row["value"] for row in existing_rows}
        missing_site_scheduler_enabled = "site_scheduler_enabled" not in existing_settings
        missing_site_scheduler_interval = "site_scheduler_interval_hours" not in existing_settings
        legacy_scheduler_enabled = bool(
            loads_json(
                existing_settings.get("scheduler_enabled"),
                DEFAULT_SETTINGS["scheduler_enabled"],
            )
        )
        legacy_scheduler_interval = loads_json(
            existing_settings.get("scheduler_interval_hours"),
            DEFAULT_SETTINGS["scheduler_interval_hours"],
        )
        if "single_thumbnail_migrated" not in existing_settings:
            for key, old_default in (("small_thumb_edge", 192), ("small_thumb_quality", 48)):
                previous = loads_json(existing_settings.get(key), old_default)
                if previous == old_default:
                    conn.execute("insert or replace into settings(key,value) values (?,?)",
                                 (key, dumps_json(DEFAULT_SETTINGS[key])))
            conn.execute("delete from settings where key in ('thumb_edge','thumb_quality')")
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute(
                "insert or ignore into settings(key, value) values (?, ?)",
                (key, dumps_json(value)),
            )
        if missing_site_scheduler_enabled and legacy_scheduler_enabled:
            conn.execute(
                "update settings set value = ? where key = 'site_scheduler_enabled'",
                (dumps_json(True),),
            )
        if missing_site_scheduler_interval:
            try:
                interval_hours = max(int(legacy_scheduler_interval or DEFAULT_SETTINGS["site_scheduler_interval_hours"]), 1)
            except (TypeError, ValueError):
                interval_hours = DEFAULT_SETTINGS["site_scheduler_interval_hours"]
            conn.execute(
                "update settings set value = ? where key = 'site_scheduler_interval_hours'",
                (dumps_json(interval_hours),),
            )
        row = conn.execute("select value from settings where key = 'site_user_agent'").fetchone()
        current = loads_json(row["value"], "") if row else ""
        if current in LEGACY_SITE_USER_AGENTS:
            conn.execute(
                "update settings set value = ? where key = 'site_user_agent'",
                (dumps_json(DEFAULT_SETTINGS["site_user_agent"]),),
            )
        row = conn.execute("select value from settings where key = 'site_request_timeout'").fetchone()
        current_timeout = loads_json(row["value"], 20) if row else 20
        try:
            normalized_timeout = int(current_timeout or 0)
        except (TypeError, ValueError):
            normalized_timeout = 0
        if normalized_timeout in LEGACY_SITE_REQUEST_TIMEOUTS:
            conn.execute(
                "update settings set value = ? where key = 'site_request_timeout'",
                (dumps_json(DEFAULT_SETTINGS["site_request_timeout"]),),
            )

    def _ensure_default_site_rules(self, conn: sqlite3.Connection) -> None:
        for key, value in DEFAULT_SITE_RULES.items():
            conn.execute(
                "insert or ignore into site_rules(key, value) values (?, ?)",
                (key, dumps_json(value)),
            )

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {row["name"] for row in conn.execute(f"pragma table_info({table})").fetchall()}
        if column in columns:
            return
        conn.execute(f"alter table {table} add column {column} {definition}")

    def _ensure_sidebar_count_cache(self, conn: sqlite3.Connection) -> None:
        defaults = {
            "all": "全部项目",
            "favorites": "收藏",
            "livephoto": "Live Photo",
            "video": "视频",
            "duplicates": "重复内容",
            "review": "待审核",
            "logs": "过滤日志",
            "tasks": "任务队列",
            "trash": "内容垃圾桶",
            "subscriptions": "订阅管理",
            "sites": "站点订阅",
        }
        now = now_iso()
        for key, label in defaults.items():
            conn.execute(
                """
                insert or ignore into sidebar_count_cache(key, count, label, payload_json, updated_at)
                values (?, 0, ?, null, ?)
                """,
                (key, label, now),
            )

    def _ensure_default_subscription(self, conn: sqlite3.Connection) -> None:
        row = conn.execute("select value from settings where key = 'host_mid'").fetchone()
        host_mid = loads_json(row["value"], DEFAULT_SETTINGS["host_mid"]) if row else DEFAULT_SETTINGS["host_mid"]
        uid = str(host_mid)
        now = now_iso()
        defaults = self._subscription_defaults(conn)
        conn.execute(
            """
            insert into subscriptions(
                uid, uname, status, pull_images, image_min_count, pull_livephoto, include_forwarded, created_at, updated_at
            )
            values (?, ?, 'active', ?, ?, ?, ?, ?, ?)
            on conflict(uid) do update set updated_at = excluded.updated_at
            """,
            (uid, None, defaults["pull_images"], defaults["image_min_count"], defaults["pull_livephoto"], defaults["include_forwarded"], now, now),
        )

    def _subscription_defaults(self, conn: sqlite3.Connection) -> dict[str, int]:
        rows = conn.execute(
            """
            select key, value from settings
            where key in ('pull_images', 'image_min_count', 'pull_livephoto', 'include_forwarded')
            """
        ).fetchall()
        values = {row["key"]: loads_json(row["value"], DEFAULT_SETTINGS.get(row["key"])) for row in rows}
        pull_images_enabled = bool(values.get("pull_images", DEFAULT_SETTINGS["pull_images"]))
        image_min_count = self._normalize_image_threshold(
            values.get("image_min_count", DEFAULT_SETTINGS["image_min_count"]),
            DEFAULT_SETTINGS["image_min_count"],
        )
        return {
            "pull_images": 1 if pull_images_enabled and image_min_count >= 0 else 0,
            "image_min_count": image_min_count if pull_images_enabled else -1,
            "pull_livephoto": 1 if values.get("pull_livephoto", DEFAULT_SETTINGS["pull_livephoto"]) else 0,
            "include_forwarded": 1 if values.get("include_forwarded", DEFAULT_SETTINGS["include_forwarded"]) else 0,
        }

    def _migrate_subscription_policies(self, conn: sqlite3.Connection) -> None:
        marker_row = conn.execute(
            "select value from settings where key = 'subscription_policy_migrated'"
        ).fetchone()
        migrated = loads_json(marker_row["value"], False) if marker_row else False
        if migrated:
            return
        defaults = self._subscription_defaults(conn)
        conn.execute(
            """
            update subscriptions
            set pull_images = ?,
                image_min_count = ?,
                pull_livephoto = ?,
                include_forwarded = ?,
                updated_at = ?
            """,
            (
                defaults["pull_images"],
                defaults["image_min_count"],
                defaults["pull_livephoto"],
                defaults["include_forwarded"],
                now_iso(),
            ),
        )
        conn.execute(
            """
            insert into settings(key, value) values (?, ?)
            on conflict(key) do update set value = excluded.value
            """,
            ("subscription_policy_migrated", dumps_json(True)),
        )

    def _migrate_image_threshold_defaults(self, conn: sqlite3.Connection) -> None:
        marker_row = conn.execute(
            "select value from settings where key = 'image_threshold_default_migrated'"
        ).fetchone()
        migrated = loads_json(marker_row["value"], False) if marker_row else False
        if migrated:
            return
        default_threshold = int(DEFAULT_SETTINGS["image_min_count"])
        current_row = conn.execute(
            "select value from settings where key = 'image_min_count'"
        ).fetchone()
        current_threshold = loads_json(current_row["value"], default_threshold) if current_row else default_threshold
        if int(current_threshold or 0) <= 1:
            conn.execute(
                """
                insert into settings(key, value) values (?, ?)
                on conflict(key) do update set value = excluded.value
                """,
                ("image_min_count", dumps_json(default_threshold)),
            )
        conn.execute(
            """
            update subscriptions
            set image_min_count = ?,
                updated_at = ?
            where image_min_count is null or image_min_count <= 1
            """,
            (default_threshold, now_iso()),
        )
        conn.execute(
            """
            insert into settings(key, value) values (?, ?)
            on conflict(key) do update set value = excluded.value
            """,
            ("image_threshold_default_migrated", dumps_json(True)),
        )

    def _backfill_folder_subscriptions(self, conn: sqlite3.Connection) -> None:
        default_row = conn.execute(
            "select uid, uname from subscriptions order by created_at asc limit 1"
        ).fetchone()
        if not default_row:
            return
        default_uid = str(default_row["uid"])
        default_name = default_row["uname"]
        conn.execute(
            """
            update folders
            set subscription_uid = coalesce(subscription_uid, ?),
                subscription_name = coalesce(subscription_name, ?)
            where subscription_uid is null or subscription_uid = ''
            """,
            (default_uid, default_name),
        )

    def get_settings(self) -> dict[str, Any]:
        with self.control_connect() as conn:
            rows = conn.execute("select key, value from settings").fetchall()
        output = dict(DEFAULT_SETTINGS)
        for row in rows:
            output[row["key"]] = loads_json(row["value"], DEFAULT_SETTINGS.get(row["key"]))
        return output

    def save_settings(self, updates: dict[str, Any]) -> dict[str, Any]:
        current = self.get_settings()
        updates = {key: value for key, value in updates.items() if key not in {"thumb_edge", "thumb_quality"}}
        current.update(updates)
        with self.control_connect() as conn:
            for key, value in updates.items():
                conn.execute(
                    """
                    insert into settings(key, value) values (?, ?)
                    on conflict(key) do update set value = excluded.value where settings.value != excluded.value
                    """,
                    (key, dumps_json(value)),
                )
        return current

    def get_storage_stats_cache(self) -> dict[str, Any]:
        return dict(self.get_settings().get("storage_stats_cache") or DEFAULT_SETTINGS["storage_stats_cache"])

    def set_storage_stats_cache(self, stats: dict[str, Any]) -> dict[str, Any]:
        payload = {**DEFAULT_SETTINGS["storage_stats_cache"], **(stats or {})}
        self.save_settings({"storage_stats_cache": payload})
        return payload

    def set_gallery_index_rebuilding(self, rebuilding: bool) -> None:
        self.save_settings({"gallery_index_rebuilding": bool(rebuilding)})

    def mark_gallery_index_rebuilt(self) -> None:
        self.save_settings(
            {
                "gallery_index_version": GALLERY_INDEX_VERSION,
                "gallery_index_rebuilt_at": now_iso(),
                "gallery_index_rebuilding": False,
            }
        )

    def get_site_rules(self) -> dict[str, Any]:
        with self.connect() as conn:
            rows = conn.execute("select key, value from site_rules").fetchall()
        output = dict(DEFAULT_SITE_RULES)
        for row in rows:
            output[row["key"]] = loads_json(row["value"], output.get(row["key"]))
        return output

    def save_site_rules(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.connect() as conn:
            for key in DEFAULT_SITE_RULES:
                if key not in payload:
                    continue
                value = payload[key]
                if key == "mode":
                    value = str(value or "blacklist").strip().lower()
                    if value not in {"whitelist", "blacklist", "both"}:
                        value = "blacklist"
                elif key == "use_regex":
                    value = bool(value)
                else:
                    value = [str(item).strip() for item in (value or []) if str(item).strip()]
                conn.execute(
                    """
                    insert into site_rules(key, value) values (?, ?)
                    on conflict(key) do update set value = excluded.value
                    """,
                    (key, dumps_json(value)),
                )
        return self.get_site_rules()

    def is_site_post_in_active_trash(self, source_id: int, url: str) -> bool:
        url = str(url or "").strip()
        if not url:
            return False
        source_prefix = f"site:{int(source_id)}:"
        with self.connect() as conn:
            rows = conn.execute(
                """
                select folder_json
                from trash_items
                where restored_at is null
                    and (top_dynamic_id like ? or source_dynamic_id like ?)
                """,
                (f"{source_prefix}%", f"{source_prefix}%"),
            ).fetchall()
        for row in rows:
            folder = loads_json(row["folder_json"], {})
            candidates = [
                str(folder.get("original_url") or "").strip(),
            ]
            metadata = loads_json(folder.get("metadata_json"), {})
            candidates.extend(
                [
                    str(metadata.get("site_post_url") or "").strip(),
                    str(metadata.get("original_url") or "").strip(),
                ]
            )
            if any(self._same_site_url(url, candidate) for candidate in candidates if candidate):
                return True
        return False

    def _same_site_url(self, left: str, right: str) -> bool:
        return left.strip().rstrip("/") == right.strip().rstrip("/")

    def list_site_sources(self, include_disabled: bool = True) -> list[dict[str, Any]]:
        sql = "select * from site_sources"
        params: tuple[Any, ...] = ()
        if not include_disabled:
            sql += " where enabled = 1"
        sql += " order by created_at desc, id desc"
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def get_site_source(self, source_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("select * from site_sources where id = ?", (int(source_id),)).fetchone()
        return dict(row) if row else None

    def xhs_subscription_uids(self) -> set[str]:
        with self.connect() as conn:
            return {f"site:{row[0]}" for row in conn.execute("select id from site_sources where source_type = 'xhs_likes'")}

    def get_site_post_by_source_url(self, source_id: int, url: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "select * from site_posts where source_id = ? and url = ?",
                (int(source_id), str(url)),
            ).fetchone()
        return dict(row) if row else None

    def create_site_source(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("source_type") == "xhs_likes":
            raise ValueError("小红书来源请通过专用入口创建")
        now = now_iso()
        name = str(payload.get("name") or "未命名来源").strip()
        slug = safe_slug(str(payload.get("slug") or name), "source")
        with self.connect() as conn:
            cur = conn.execute(
                """
                insert into site_sources(
                    name, slug, source_type, entry_url, page_url_template, max_pages,
                    list_item_selector, detail_link_selector, title_selector, date_selector,
                    tag_selector, body_selector, media_selector, skip_head_images, skip_tail_images,
                    use_proxy, icon_url, enabled, start_date, created_at, updated_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    slug,
                    str(payload.get("source_type") or "html"),
                    str(payload.get("entry_url") or "").strip(),
                    payload.get("page_url_template") or None,
                    max(int(payload.get("max_pages") or 1), 1),
                    payload.get("list_item_selector") or None,
                    payload.get("detail_link_selector") or None,
                    payload.get("title_selector") or None,
                    payload.get("date_selector") or None,
                    payload.get("tag_selector") or None,
                    payload.get("body_selector") or None,
                    payload.get("media_selector") or None,
                    max(int(payload.get("skip_head_images") or 0), 0),
                    max(int(payload.get("skip_tail_images") or 0), 0),
                    1 if payload.get("use_proxy", True) else 0,
                    payload.get("icon_url") or None,
                    1 if payload.get("enabled", True) else 0,
                    payload.get("start_date") or None,
                    now,
                    now,
                ),
            )
            source_id = int(cur.lastrowid)
        item = self.get_site_source(source_id)
        if not item:
            raise RuntimeError("站点来源创建失败")
        return item

    def update_site_source(self, source_id: int, payload: dict[str, Any]) -> dict[str, Any] | None:
        current = self.get_site_source(source_id)
        if not current:
            return None
        if current.get("source_type") == "xhs_likes" or payload.get("source_type") == "xhs_likes":
            raise ValueError("小红书来源请通过专用入口修改")
        merged = {**current, **payload}
        merged["slug"] = safe_slug(str(merged.get("slug") or merged.get("name")), "source")
        with self.connect() as conn:
            conn.execute(
                """
                update site_sources set
                    name = ?, slug = ?, source_type = ?, entry_url = ?, page_url_template = ?,
                    max_pages = ?, list_item_selector = ?, detail_link_selector = ?,
                    title_selector = ?, date_selector = ?, tag_selector = ?, body_selector = ?,
                    media_selector = ?, skip_head_images = ?, skip_tail_images = ?,
                    use_proxy = ?, icon_url = ?, enabled = ?, start_date = ?, updated_at = ?
                where id = ?
                """,
                (
                    merged["name"],
                    merged["slug"],
                    merged["source_type"],
                    merged["entry_url"],
                    merged.get("page_url_template") or None,
                    max(int(merged.get("max_pages") or 1), 1),
                    merged.get("list_item_selector") or None,
                    merged.get("detail_link_selector") or None,
                    merged.get("title_selector") or None,
                    merged.get("date_selector") or None,
                    merged.get("tag_selector") or None,
                    merged.get("body_selector") or None,
                    merged.get("media_selector") or None,
                    max(int(merged.get("skip_head_images") or 0), 0),
                    max(int(merged.get("skip_tail_images") or 0), 0),
                    1 if merged.get("use_proxy", True) else 0,
                    merged.get("icon_url") or None,
                    1 if merged.get("enabled") else 0,
                    merged.get("start_date") or None,
                    now_iso(),
                    int(source_id),
                ),
            )
        return self.get_site_source(source_id)

    def delete_site_source(self, source_id: int) -> bool:
        with self.connect() as conn:
            cur = conn.execute("delete from site_sources where id = ?", (int(source_id),))
            return cur.rowcount > 0

    def site_subscription_uid(self, source_id: int | str) -> str:
        return f"site:{source_id}"

    def list_site_gallery_folders(self, source_id: int) -> list[dict[str, Any]]:
        uid = self.site_subscription_uid(source_id)
        with self.connect() as conn:
            rows = conn.execute(
                "select * from folders where subscription_uid = ? order by folder_name asc",
                (uid,),
            ).fetchall()
        return [dict(row) for row in rows]

    def clear_site_source_and_gallery(self, source_id: int) -> dict[str, Any]:
        uid = self.site_subscription_uid(source_id)
        with self.connect() as conn:
            folder_rows = conn.execute(
                "select folder_name from folders where subscription_uid = ? order by folder_name asc",
                (uid,),
            ).fetchall()
            folder_names = [str(row["folder_name"]) for row in folder_rows]
            post_count = conn.execute(
                "select count(*) from site_posts where source_id = ?",
                (int(source_id),),
            ).fetchone()[0]
            asset_count = conn.execute(
                """
                select count(*)
                from site_assets
                join site_posts on site_posts.id = site_assets.post_id
                where site_posts.source_id = ?
                """,
                (int(source_id),),
            ).fetchone()[0]
            for folder_name in folder_names:
                conn.execute("delete from pair_index where folder_name = ?", (folder_name,))
                conn.execute("delete from folder_index where folder_name = ?", (folder_name,))
                conn.execute("delete from folders where folder_name = ?", (folder_name,))
            conn.execute("delete from site_filter_logs where source_id = ?", (int(source_id),))
            conn.execute("delete from site_sources where id = ?", (int(source_id),))
        return {
            "folder_names": folder_names,
            "folders": len(folder_names),
            "posts": int(post_count or 0),
            "assets": int(asset_count or 0),
        }

    def clear_site_source_content(self, source_id: int) -> dict[str, Any]:
        source = self.get_site_source(source_id)
        if source and source.get("source_type") == "xhs_likes":
            raise ValueError("小红书来源禁止使用破坏式站点全量校验")
        uid = self.site_subscription_uid(source_id)
        with self.connect() as conn:
            folder_rows = conn.execute(
                "select folder_name from folders where subscription_uid = ? order by folder_name asc",
                (uid,),
            ).fetchall()
            folder_names = [str(row["folder_name"]) for row in folder_rows]
            post_count = conn.execute(
                "select count(*) from site_posts where source_id = ?",
                (int(source_id),),
            ).fetchone()[0]
            asset_count = conn.execute(
                """
                select count(*)
                from site_assets
                join site_posts on site_posts.id = site_assets.post_id
                where site_posts.source_id = ?
                """,
                (int(source_id),),
            ).fetchone()[0]
            for folder_name in folder_names:
                conn.execute("delete from pair_index where folder_name = ?", (folder_name,))
                conn.execute("delete from folder_index where folder_name = ?", (folder_name,))
                conn.execute("delete from folders where folder_name = ?", (folder_name,))
            conn.execute("delete from site_filter_logs where source_id = ?", (int(source_id),))
            conn.execute("delete from site_posts where source_id = ?", (int(source_id),))
        return {
            "folder_names": folder_names,
            "folders": len(folder_names),
            "posts": int(post_count or 0),
            "assets": int(asset_count or 0),
        }

    def export_site_sources(self) -> dict[str, Any]:
        return {
            "version": 1,
            "sources": [self._site_source_export_item(item) for item in self.list_site_sources()
                        if item.get("source_type") != "xhs_likes"],
        }

    def import_site_sources(self, payload: dict[str, Any]) -> dict[str, Any]:
        sources = payload.get("sources")
        if not isinstance(sources, list):
            raise ValueError("导入文件缺少 sources 数组")
        created = 0
        updated = 0
        items = []
        existing = {item["slug"]: item for item in self.list_site_sources()}
        for raw in sources:
            if not isinstance(raw, dict):
                continue
            item = self._clean_site_source_import(raw)
            if not item.get("entry_url"):
                continue
            if item["slug"] in existing:
                saved = self.update_site_source(int(existing[item["slug"]]["id"]), item)
                updated += 1
            else:
                saved = self.create_site_source(item)
                created += 1
            if saved:
                items.append(saved)
        return {"created": created, "updated": updated, "items": items}

    def _site_source_export_item(self, source: dict[str, Any]) -> dict[str, Any]:
        keys = (
            "name",
            "slug",
            "source_type",
            "entry_url",
            "page_url_template",
            "max_pages",
            "list_item_selector",
            "detail_link_selector",
            "title_selector",
            "date_selector",
            "tag_selector",
            "body_selector",
            "media_selector",
            "skip_head_images",
            "skip_tail_images",
            "use_proxy",
            "icon_url",
            "enabled",
            "start_date",
        )
        item = {key: source.get(key) for key in keys}
        item["enabled"] = bool(item.get("enabled"))
        item["use_proxy"] = bool(item.get("use_proxy", True))
        item["max_pages"] = max(int(item.get("max_pages") or 1), 1)
        item["skip_head_images"] = max(int(item.get("skip_head_images") or 0), 0)
        item["skip_tail_images"] = max(int(item.get("skip_tail_images") or 0), 0)
        return item

    def _clean_site_source_import(self, source: dict[str, Any]) -> dict[str, Any]:
        name = str(source.get("name") or source.get("slug") or "未命名来源").strip()
        slug = safe_slug(str(source.get("slug") or name), "source")
        return {
            "name": name,
            "slug": slug,
            "source_type": str(source.get("source_type") or "html"),
            "entry_url": str(source.get("entry_url") or "").strip(),
            "page_url_template": source.get("page_url_template") or None,
            "max_pages": max(int(source.get("max_pages") or 1), 1),
            "list_item_selector": source.get("list_item_selector") or None,
            "detail_link_selector": source.get("detail_link_selector") or None,
            "title_selector": source.get("title_selector") or None,
            "date_selector": source.get("date_selector") or None,
            "tag_selector": source.get("tag_selector") or None,
            "body_selector": source.get("body_selector") or None,
            "media_selector": source.get("media_selector") or None,
            "skip_head_images": max(int(source.get("skip_head_images") or 0), 0),
            "skip_tail_images": max(int(source.get("skip_tail_images") or 0), 0),
            "use_proxy": bool(source.get("use_proxy", True)),
            "icon_url": source.get("icon_url") or None,
            "enabled": bool(source.get("enabled", True)),
            "start_date": source.get("start_date") or None,
        }

    def upsert_site_post(self, source_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        now = now_iso()
        title = str(payload.get("title") or "未命名贴文").strip()
        slug = safe_slug(payload.get("slug") or title or payload["url"], "post")
        tags = payload.get("tags") or []
        with self.connect() as conn:
            conn.execute(
                """
                insert into site_posts(
                    source_id, url, title, slug, pub_date, tags_json, excerpt, status,
                    filter_reason, created_at, updated_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(source_id, url) do update set
                    title = excluded.title,
                    slug = excluded.slug,
                    pub_date = excluded.pub_date,
                    tags_json = excluded.tags_json,
                    excerpt = excluded.excerpt,
                    updated_at = excluded.updated_at
                """,
                (
                    int(source_id),
                    payload["url"],
                    title,
                    slug,
                    payload.get("pub_date"),
                    dumps_json(tags),
                    payload.get("excerpt") or "",
                    payload.get("status") or "discovered",
                    payload.get("filter_reason"),
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "select * from site_posts where source_id = ? and url = ?",
                (int(source_id), payload["url"]),
            ).fetchone()
        return self._site_post_row(row)

    def get_site_post(self, post_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                select site_posts.*, site_sources.name as source_name, site_sources.slug as source_slug
                from site_posts join site_sources on site_sources.id = site_posts.source_id
                where site_posts.id = ?
                """,
                (int(post_id),),
            ).fetchone()
        return self._site_post_row(row) if row else None

    def list_site_posts(self, category: str = "all", source_id: int | None = None, q: str = "") -> list[dict[str, Any]]:
        where = []
        params: list[Any] = []
        if category == "favorites":
            where.append("site_posts.is_favorite = 1")
            where.append("site_posts.is_blocked = 0")
        elif category == "blocked":
            where.append("site_posts.is_blocked = 1")
        else:
            where.append("site_posts.is_blocked = 0")
        if source_id:
            where.append("site_posts.source_id = ?")
            params.append(int(source_id))
        if q:
            where.append("(site_posts.title like ? or site_posts.tags_json like ?)")
            params.extend([f"%{q}%", f"%{q}%"])
        sql = """
            select site_posts.*, site_sources.name as source_name, site_sources.slug as source_slug,
                (
                    select rel_path from site_assets
                    where site_assets.post_id = site_posts.id and site_assets.status = 'ready'
                    order by case media_type when 'image' then 0 else 1 end, id asc
                    limit 1
                ) as cover_rel_path
            from site_posts join site_sources on site_sources.id = site_posts.source_id
        """
        if where:
            sql += " where " + " and ".join(where)
        sql += " order by coalesce(site_posts.pub_date, '') desc, site_posts.updated_at desc limit 500"
        with self.connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [self._site_post_row(row) for row in rows]

    def site_source_content_stats(self) -> dict[int, dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                select
                    source_id,
                    count(*) as post_count,
                    coalesce(sum(asset_count), 0) as asset_count,
                    max(coalesce(pub_date, '')) as latest_content_at
                from site_posts
                group by source_id
                """
            ).fetchall()
        return {
            int(row["source_id"]): {
                "post_count": int(row["post_count"] or 0),
                "asset_count": int(row["asset_count"] or 0),
                "latest_content_at": row["latest_content_at"] or None,
            }
            for row in rows
        }

    def set_site_post_flag(self, post_id: int, key: str, value: bool) -> dict[str, Any] | None:
        if key not in {"is_favorite", "is_blocked"}:
            raise ValueError("不支持的标记")
        with self.connect() as conn:
            conn.execute(f"update site_posts set {key} = ?, updated_at = ? where id = ?", (1 if value else 0, now_iso(), int(post_id)))
        return self.get_site_post(post_id)

    def set_site_post_status(self, post_id: int, status: str, reason: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "update site_posts set status = ?, filter_reason = ?, updated_at = ? where id = ?",
                (status, reason, now_iso(), int(post_id)),
            )

    def upsert_site_asset(self, post_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        now = now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                insert into site_assets(post_id, url, media_type, filename, status, created_at, updated_at)
                values (?, ?, ?, ?, 'pending', ?, ?)
                on conflict(post_id, url) do update set
                    media_type = excluded.media_type,
                    filename = excluded.filename,
                    updated_at = excluded.updated_at
                """,
                (int(post_id), payload["url"], payload["media_type"], payload["filename"], now, now),
            )
            row = conn.execute("select * from site_assets where post_id = ? and url = ?", (int(post_id), payload["url"])).fetchone()
        return dict(row)

    def set_site_asset_result(self, asset_id: int, status: str, rel_path: str | None = None, error: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "update site_assets set status = ?, rel_path = coalesce(?, rel_path), error = ?, updated_at = ? where id = ?",
                (status, rel_path, error, now_iso(), int(asset_id)),
            )

    def list_site_assets(self, post_id: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("select * from site_assets where post_id = ? order by id asc", (int(post_id),)).fetchall()
        return [dict(row) for row in rows]

    def update_site_post_counts(self, post_id: int) -> None:
        with self.connect() as conn:
            counts = conn.execute(
                """
                select count(*) as asset_count,
                    sum(case when status = 'ready' then 1 else 0 end) as downloaded_count
                from site_assets where post_id = ? and status != 'duplicate'
                """,
                (int(post_id),),
            ).fetchone()
            asset_count = int(counts["asset_count"] or 0)
            downloaded_count = int(counts["downloaded_count"] or 0)
            conn.execute(
                """
                update site_posts set asset_count = ?, downloaded_count = ?,
                    status = case when ? > 0 and ? = ? then 'ready' else status end,
                    updated_at = ?
                where id = ?
                """,
                (asset_count, downloaded_count, asset_count, downloaded_count, asset_count, now_iso(), int(post_id)),
            )

    def list_synced_site_posts(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                select site_posts.*, site_sources.name as source_name, site_sources.slug as source_slug
                from site_posts join site_sources on site_sources.id = site_posts.source_id
                where exists (
                    select 1 from site_assets
                    where site_assets.post_id = site_posts.id
                        and site_assets.status = 'ready'
                        and site_assets.rel_path is not null
                )
                order by site_posts.updated_at desc
                """
            ).fetchall()
        return [self._site_post_row(row) for row in rows]

    def site_post_url_from_dynamic_id(self, source_dynamic_id: str | None) -> str:
        parts = str(source_dynamic_id or "").split(":")
        if len(parts) != 3 or parts[0] != "site":
            return ""
        try:
            source_id = int(parts[1])
            post_id = int(parts[2])
        except ValueError:
            return ""
        with self.connect() as conn:
            row = conn.execute(
                "select url from site_posts where id = ? and source_id = ?",
                (post_id, source_id),
            ).fetchone()
        return str(row["url"] or "") if row else ""

    def add_site_filter_log(self, source_id: int | None, post_url: str, title: str, decision: str, reason: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "insert into site_filter_logs(source_id, post_url, title, decision, reason, created_at) values (?, ?, ?, ?, ?, ?)",
                (source_id, post_url, title, decision, reason, now_iso()),
            )

    def list_site_filter_logs(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("select * from site_filter_logs order by id desc limit ?", (int(limit),)).fetchall()
        return [dict(row) for row in rows]

    def clear_site_filter_logs(self) -> int:
        with self.connect() as conn:
            cursor = conn.execute("delete from site_filter_logs")
            return int(cursor.rowcount or 0)

    def site_stats(self) -> dict[str, Any]:
        def compute(conn):
            source_count = conn.execute("select count(*) from site_sources where source_type != 'xhs_likes'").fetchone()[0]
            post_count = conn.execute("""select count(*) from site_posts p join site_sources s on s.id = p.source_id
                where p.is_blocked = 0 and s.source_type != 'xhs_likes'""").fetchone()[0]
            favorite_count = conn.execute("""select count(*) from site_posts p join site_sources s on s.id = p.source_id
                where p.is_favorite = 1 and p.is_blocked = 0 and s.source_type != 'xhs_likes'""").fetchone()[0]
            blocked_count = conn.execute("""select count(*) from site_posts p join site_sources s on s.id = p.source_id
                where p.is_blocked = 1 and s.source_type != 'xhs_likes'""").fetchone()[0]
            asset_count = conn.execute("""select count(*) from site_assets a
                join site_posts p on p.id = a.post_id join site_sources s on s.id = p.source_id
                where a.status = 'ready' and s.source_type != 'xhs_likes'""").fetchone()[0]
            return {
                "source_count": source_count,
                "post_count": post_count,
                "favorite_count": favorite_count,
                "blocked_count": blocked_count,
                "asset_count": asset_count,
            }
        return self._cached_content_stats('sites', compute)

    def _site_post_row(self, row: sqlite3.Row | None) -> dict[str, Any]:
        if row is None:
            return {}
        item = dict(row)
        item["tags"] = loads_json(item.pop("tags_json", "[]"), [])
        cover = item.get("cover_rel_path")
        item["cover_url"] = f"/storage/{cover}" if cover else None
        item["is_favorite"] = bool(item.get("is_favorite"))
        item["is_blocked"] = bool(item.get("is_blocked"))
        return item

    def get_auth_state(self) -> dict[str, Any]:
        with self.control_connect() as conn:
            row = conn.execute("select * from auth_state where id = 1").fetchone()
        if not row:
            return {"qr_status": "idle"}
        return dict(row)

    def update_auth_state(self, **kwargs: Any) -> None:
        if not kwargs:
            return
        assignments = ", ".join(f"{key} = ?" for key in kwargs)
        values = list(kwargs.values())
        with self.control_connect() as conn:
            conn.execute(f"update auth_state set {assignments} where id = 1", values)

    def get_folder(self, folder_name: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("select * from folders where folder_name = ?", (folder_name,)).fetchone()
        return dict(row) if row else None

    def list_subscriptions(self, include_paused: bool = True) -> list[dict[str, Any]]:
        sql = "select * from subscriptions"
        params: tuple[Any, ...] = ()
        if not include_paused:
            sql += " where status = ?"
            params = ("active",)
        sql += " order by updated_at desc, uid asc"
        with self.control_connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def get_subscription(self, uid: str) -> dict[str, Any] | None:
        with self.control_connect() as conn:
            row = conn.execute("select * from subscriptions where uid = ?", (str(uid),)).fetchone()
        return dict(row) if row else None

    def upsert_subscription(
        self,
        uid: str,
        uname: str | None = None,
        status: str = "active",
        pull_images: bool | None = None,
        image_min_count: int | None = None,
        pull_livephoto: bool | None = None,
        include_forwarded: bool | None = None,
        avatar_url: str | None = None,
        avatar_tiny_url: str | None = None,
    ) -> dict[str, Any]:
        now = now_iso()
        with self.connect() as conn:
            existing = conn.execute("select * from subscriptions where uid = ?", (str(uid),)).fetchone()
            defaults = self._subscription_defaults(conn)
            normalized_threshold = self._normalize_image_threshold(
                image_min_count
                if image_min_count is not None
                else (existing["image_min_count"] if existing else defaults["image_min_count"]),
                defaults["image_min_count"],
            )
            pull_images_flag = (
                1
                if pull_images
                else 0
                if pull_images is not None
                else int(existing["pull_images"])
                if existing
                else defaults["pull_images"]
            )
            if normalized_threshold < 0:
                pull_images_flag = 0
            payload = {
                "uid": str(uid),
                "uname": uname if uname is not None else (existing["uname"] if existing else None),
                "avatar_url": avatar_url if avatar_url is not None else (existing["avatar_url"] if existing else None),
                "avatar_tiny_url": avatar_tiny_url if avatar_tiny_url is not None else (existing["avatar_tiny_url"] if existing else None),
                "status": status if status is not None else (existing["status"] if existing else "active"),
                "pull_images": pull_images_flag,
                "image_min_count": normalized_threshold,
                "pull_livephoto": 1 if pull_livephoto else 0 if pull_livephoto is not None else int(existing["pull_livephoto"]) if existing else defaults["pull_livephoto"],
                "include_forwarded": 1 if include_forwarded else 0 if include_forwarded is not None else int(existing["include_forwarded"]) if existing else defaults["include_forwarded"],
                "created_at": existing["created_at"] if existing else now,
                "updated_at": now,
            }
            conn.execute(
                """
                insert into subscriptions(
                    uid, uname, avatar_url, avatar_tiny_url, status, pull_images, image_min_count, pull_livephoto, include_forwarded, created_at, updated_at
                )
                values (:uid, :uname, :avatar_url, :avatar_tiny_url, :status, :pull_images, :image_min_count, :pull_livephoto, :include_forwarded, :created_at, :updated_at)
                on conflict(uid) do update set
                    uname = excluded.uname,
                    avatar_url = excluded.avatar_url,
                    avatar_tiny_url = excluded.avatar_tiny_url,
                    status = excluded.status,
                    pull_images = excluded.pull_images,
                    image_min_count = excluded.image_min_count,
                    pull_livephoto = excluded.pull_livephoto,
                    include_forwarded = excluded.include_forwarded,
                    updated_at = excluded.updated_at
                """,
                payload,
            )
        return self.get_subscription(uid) or {
            "uid": str(uid),
            "uname": uname,
            "avatar_url": avatar_url,
            "avatar_tiny_url": avatar_tiny_url,
            "status": status,
            "pull_images": 1 if pull_images and self._normalize_image_threshold(image_min_count, DEFAULT_SETTINGS["image_min_count"]) >= 0 else 0,
            "image_min_count": self._normalize_image_threshold(image_min_count, DEFAULT_SETTINGS["image_min_count"]),
            "pull_livephoto": 1 if pull_livephoto else 0,
            "include_forwarded": 1 if include_forwarded else 0,
        }

    def update_subscription_status(self, uid: str, status: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "update subscriptions set status = ?, updated_at = ? where uid = ?",
                (status, now_iso(), str(uid)),
            )

    def update_subscription_settings(self, uid: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        current = self.get_subscription(uid)
        if not current:
            return None
        normalized_threshold = self._normalize_image_threshold(
            updates.get("image_min_count", current.get("image_min_count", DEFAULT_SETTINGS["image_min_count"])),
            current.get("image_min_count", DEFAULT_SETTINGS["image_min_count"]),
        )
        payload = {
            "uid": str(uid),
            "uname": current.get("uname"),
            "avatar_url": current.get("avatar_url"),
            "status": current.get("status", "active"),
            "pull_images": bool(updates.get("pull_images", current.get("pull_images"))) and normalized_threshold >= 0,
            "image_min_count": normalized_threshold,
            "pull_livephoto": bool(updates.get("pull_livephoto", current.get("pull_livephoto"))),
            "include_forwarded": bool(updates.get("include_forwarded", current.get("include_forwarded"))),
        }
        return self.upsert_subscription(**payload)

    def _normalize_image_threshold(self, value: Any, fallback: int) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            number = int(fallback)
        return max(-1, min(12, number))

    def set_subscription_icon(self, uid: str, avatar_url: str | None, avatar_tiny_url: str | None = None) -> dict[str, Any] | None:
        current = self.get_subscription(uid)
        if not current:
            return None
        with self.connect() as conn:
            conn.execute(
                "update subscriptions set avatar_url = ?, avatar_tiny_url = ?, updated_at = ? where uid = ?",
                (avatar_url or None, avatar_tiny_url or None, now_iso(), str(uid)),
            )
        return self.get_subscription(uid)

    def set_site_source_icon(self, source_id: int, icon_url: str | None, icon_tiny_url: str | None = None) -> dict[str, Any] | None:
        if not self.get_site_source(source_id):
            return None
        with self.connect() as conn:
            conn.execute(
                "update site_sources set icon_url = ?, icon_tiny_url = ?, updated_at = ? where id = ?",
                (icon_url or None, icon_tiny_url or None, now_iso(), int(source_id)),
            )
        return self.get_site_source(source_id)

    def delete_subscription(self, uid: str) -> None:
        with self.connect() as conn:
            conn.execute("delete from subscriptions where uid = ?", (str(uid),))

    def upsert_folder(self, folder: dict[str, Any]) -> None:
        payload = {
            "title": folder.get("title", ""),
            "text_prefix": folder.get("text_prefix", ""),
            "pub_ts": int(folder.get("pub_ts", 0)),
            "pub_time": folder.get("pub_time", ""),
            "top_dynamic_id": str(folder.get("top_dynamic_id", "")),
            "source_dynamic_id": str(folder.get("source_dynamic_id", "")),
            "subscription_uid": str(folder.get("subscription_uid") or ""),
            "subscription_name": folder.get("subscription_name"),
            "has_images": 1 if folder.get("has_images") else 0,
            "has_livephoto": 1 if folder.get("has_livephoto") else 0,
            "is_favorite": 1 if folder.get("is_favorite") else 0,
            "status": folder.get("status", "active"),
            "review_status": folder.get("review_status", "approved"),
            "review_reason": folder.get("review_reason"),
            "metadata_json": dumps_json(folder.get("metadata", {})),
            "updated_at": now_iso(),
        }
        created_at = folder.get("created_at", payload["updated_at"])
        with self.connect() as conn:
            conn.execute(
                """
                insert into folders(
                    folder_name, title, text_prefix, pub_ts, pub_time, top_dynamic_id, source_dynamic_id,
                    subscription_uid, subscription_name, has_images, has_livephoto, is_favorite, status, review_status, review_reason,
                    metadata_json, created_at, updated_at
                )
                values(
                    :folder_name, :title, :text_prefix, :pub_ts, :pub_time, :top_dynamic_id, :source_dynamic_id,
                    :subscription_uid, :subscription_name, :has_images, :has_livephoto, :is_favorite, :status, :review_status, :review_reason,
                    :metadata_json, :created_at, :updated_at
                )
                on conflict(folder_name) do update set
                    title = excluded.title,
                    text_prefix = excluded.text_prefix,
                    pub_ts = excluded.pub_ts,
                    pub_time = excluded.pub_time,
                    top_dynamic_id = excluded.top_dynamic_id,
                    source_dynamic_id = excluded.source_dynamic_id,
                    subscription_uid = excluded.subscription_uid,
                    subscription_name = coalesce(excluded.subscription_name, folders.subscription_name),
                    has_images = excluded.has_images,
                    has_livephoto = excluded.has_livephoto,
                    status = excluded.status,
                    review_status = excluded.review_status,
                    review_reason = excluded.review_reason,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                {"folder_name": folder["folder_name"], "created_at": created_at, **payload},
            )

    def replace_folder_assets(self, folder_name: str, media_type: str, assets: list[dict[str, Any]]) -> None:
        now = now_iso()
        with self.connect() as conn:
            conn.execute(
                "delete from assets where folder_name = ? and media_type = ?",
                (folder_name, media_type),
            )
            for asset in assets:
                conn.execute(
                    """
                    insert into assets(
                        folder_name, media_type, pair_index, filename, rel_path, thumb_rel_path, small_thumb_rel_path, tiny_thumb_rel_path,
                        cover_rel_path, reverse_rel_path, width, height, status, metadata_json, duplicate_fingerprint,
                        created_at, updated_at
                    )
                    values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        folder_name,
                        media_type,
                        int(asset.get("pair_index", 0)),
                        asset["filename"],
                        asset["rel_path"],
                        asset.get("thumb_rel_path"),
                        asset.get("small_thumb_rel_path"),
                        asset.get("tiny_thumb_rel_path"),
                        asset.get("cover_rel_path"),
                        asset.get("reverse_rel_path"),
                        asset.get("width"),
                        asset.get("height"),
                        asset.get("status", "ready"),
                        dumps_json(asset.get("metadata", {})),
                        asset.get("duplicate_fingerprint"),
                        now,
                        now,
                    ),
                )

    def get_folder_by_dynamic(self, top_dynamic_id: str, source_dynamic_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                select * from folders
                where top_dynamic_id = ? and source_dynamic_id = ?
                limit 1
                """,
                (str(top_dynamic_id), str(source_dynamic_id)),
            ).fetchone()
        return dict(row) if row else None

    def list_folders_by_dynamic_pairs(self, pairs: list[tuple[str, str]]) -> list[dict[str, Any]]:
        normalized = sorted(
            {
                (str(top_dynamic_id), str(source_dynamic_id))
                for top_dynamic_id, source_dynamic_id in (pairs or [])
                if str(top_dynamic_id).strip() and str(source_dynamic_id).strip()
            }
        )
        if not normalized:
            return []
        rows: list[sqlite3.Row] = []
        chunk_size = 300
        with self.connect() as conn:
            for index in range(0, len(normalized), chunk_size):
                chunk = normalized[index:index + chunk_size]
                clauses = ["(top_dynamic_id = ? and source_dynamic_id = ?)" for _ in chunk]
                params = [value for pair in chunk for value in pair]
                rows.extend(
                    conn.execute(
                        f"select * from folders where {' or '.join(clauses)}",
                        params,
                    ).fetchall()
                )
        return [dict(row) for row in rows]

    def list_folders(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("select * from folders order by pub_ts desc, folder_name desc").fetchall()
        return [dict(row) for row in rows]

    def list_all_assets(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "select * from assets order by folder_name asc, pair_index asc, media_type asc, filename asc"
            ).fetchall()
        return [dict(row) for row in rows]

    def list_assets_for_folder(self, folder_name: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "select * from assets where folder_name = ? order by media_type, pair_index, filename",
                (folder_name,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_assets_for_folders(self, folder_names: list[str]) -> list[dict[str, Any]]:
        normalized = sorted({str(folder_name) for folder_name in (folder_names or []) if str(folder_name).strip()})
        if not normalized:
            return []
        rows: list[sqlite3.Row] = []
        chunk_size = 500
        with self.connect() as conn:
            for index in range(0, len(normalized), chunk_size):
                chunk = normalized[index:index + chunk_size]
                placeholders = ", ".join("?" for _ in chunk)
                rows.extend(
                    conn.execute(
                        f"select * from assets where folder_name in ({placeholders}) order by folder_name, media_type, pair_index, filename",
                        chunk,
                    ).fetchall()
                )
        return [dict(row) for row in rows]

    def set_asset_duplicate_fingerprints(self, fingerprints: dict[int, str]) -> None:
        if not fingerprints:
            return
        with self.connect() as conn:
            conn.executemany(
                "update assets set duplicate_fingerprint = ? where id = ?",
                [(str(fingerprint), int(asset_id)) for asset_id, fingerprint in fingerprints.items()],
            )

    def list_ignored_duplicate_signatures(self) -> set[str]:
        with self.connect() as conn:
            rows = conn.execute("select signature from duplicate_ignores").fetchall()
        return {str(row["signature"]) for row in rows}

    def ignore_duplicate_group(self, signature: str, folder_names: list[str]) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                insert into duplicate_ignores(signature, folder_names_json, ignored_at)
                values (?, ?, ?)
                on conflict(signature) do update set
                    folder_names_json = excluded.folder_names_json,
                    ignored_at = excluded.ignored_at
                """,
                (str(signature), dumps_json(sorted(str(name) for name in folder_names)), now_iso()),
            )

    def get_duplicate_group_cache(self, cache_key: str) -> list[dict[str, Any]] | None:
        with self.connect() as conn:
            row = conn.execute(
                "select groups_json from duplicate_group_cache where id = 1 and cache_key = ?",
                (str(cache_key),),
            ).fetchone()
        if not row:
            return None
        payload = loads_json(row["groups_json"], None)
        if not isinstance(payload, list):
            return None
        return [dict(group) for group in payload if isinstance(group, dict)]

    def get_latest_duplicate_group_cache(self) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "select cache_key, groups_json, updated_at from duplicate_group_cache where id = 1",
            ).fetchone()
        if not row:
            return None
        groups = loads_json(row["groups_json"], None)
        if not isinstance(groups, list):
            return None
        return {
            "cache_key": str(row["cache_key"]),
            "groups": [dict(group) for group in groups if isinstance(group, dict)],
            "updated_at": row["updated_at"],
        }

    def set_duplicate_group_cache(self, cache_key: str, groups: list[dict[str, Any]]) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                insert into duplicate_group_cache(id, cache_key, groups_json, updated_at)
                values (1, ?, ?, ?)
                on conflict(id) do update set
                    cache_key = excluded.cache_key,
                    groups_json = excluded.groups_json,
                    updated_at = excluded.updated_at
                """,
                (str(cache_key), dumps_json(groups), now_iso()),
            )

    def clear_duplicate_group_cache(self) -> None:
        with self.connect() as conn:
            conn.execute("delete from duplicate_group_cache")

    def duplicate_content_signature(self) -> str:
        with self.connect() as conn:
            row = conn.execute(
                """
                select
                    count(*) as image_rows,
                    coalesce(max(id), 0) as max_image_id,
                    coalesce(sum(id), 0) as image_id_sum,
                    count(distinct folder_name) as folder_rows,
                    coalesce(sum(pair_index), 0) as pair_index_sum,
                    coalesce(sum(width), 0) as width_sum,
                    coalesce(sum(height), 0) as height_sum,
                    sum(case when coalesce(duplicate_fingerprint, '') != '' then 1 else 0 end) as fingerprint_rows,
                    coalesce(max(created_at), '') as newest_created_at
                from assets
                where media_type = 'image'
                """
            ).fetchone()
        return dumps_json(dict(row))

    def set_folder_favorite(self, folder_name: str, is_favorite: bool) -> None:
        with self.connect() as conn:
            conn.execute(
                "update folders set is_favorite = ?, updated_at = ? where folder_name = ?",
                (1 if is_favorite else 0, now_iso(), folder_name),
            )
            conn.execute(
                "update folder_index set is_favorite = ?, updated_at = ? where folder_name = ?",
                (1 if is_favorite else 0, now_iso(), folder_name),
            )
            conn.execute(
                "update pair_index set is_favorite = ?, updated_at = ? where folder_name = ?",
                (1 if is_favorite else 0, now_iso(), folder_name),
            )

    def delete_asset(self, asset_id: int) -> None:
        with self.connect() as conn:
            conn.execute("delete from assets where id = ?", (asset_id,))

    def delete_assets(self, asset_ids: list[int]) -> int:
        normalized = sorted({int(asset_id) for asset_id in asset_ids if int(asset_id) > 0})
        if not normalized:
            return 0
        with self.connect() as conn:
            conn.executemany("delete from assets where id = ?", [(asset_id,) for asset_id in normalized])
        return len(normalized)

    def update_folder_media_flags(self, folder_name: str, has_images: bool, has_livephoto: bool) -> None:
        with self.connect() as conn:
            conn.execute(
                "update folders set has_images = ?, has_livephoto = ?, updated_at = ? where folder_name = ?",
                (1 if has_images else 0, 1 if has_livephoto else 0, now_iso(), folder_name),
            )

    def update_asset_small_thumbnail(self, asset_id: int, small_thumb_rel_path: str | None) -> None:
        with self.connect() as conn:
            conn.execute(
                "update assets set small_thumb_rel_path = ?, updated_at = ? where id = ?",
                (small_thumb_rel_path, now_iso(), int(asset_id)),
            )

    def update_asset_thumbnails(
        self,
        asset_id: int,
        thumb_rel_path: str | None,
        small_thumb_rel_path: str | None,
        tiny_thumb_rel_path: str | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                "update assets set thumb_rel_path = ?, small_thumb_rel_path = ?, tiny_thumb_rel_path = ?, updated_at = ? where id = ?",
                (thumb_rel_path, small_thumb_rel_path, tiny_thumb_rel_path, now_iso(), int(asset_id)),
            )

    def add_deleted_pair_mark(
        self,
        top_dynamic_id: str,
        source_dynamic_id: str,
        folder_name: str,
        pair_index: int,
        reason: str = "手动删除",
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                insert into deleted_pair_marks(
                    top_dynamic_id, source_dynamic_id, folder_name, pair_index, reason, created_at
                )
                values (?, ?, ?, ?, ?, ?)
                on conflict(top_dynamic_id, source_dynamic_id, pair_index) do update set
                    folder_name = excluded.folder_name,
                    reason = excluded.reason
                """,
                (str(top_dynamic_id), str(source_dynamic_id), folder_name, int(pair_index), reason, now_iso()),
            )

    def add_deleted_pair_marks(self, marks: list[dict[str, Any]], reason: str = "手动删除") -> int:
        normalized: dict[tuple[str, str, int], tuple[str, str, str, int, str, str]] = {}
        created_at = now_iso()
        for mark in marks:
            top_dynamic_id = str(mark.get("top_dynamic_id") or "")
            source_dynamic_id = str(mark.get("source_dynamic_id") or "")
            folder_name = str(mark.get("folder_name") or "")
            pair_index = int(mark.get("pair_index") or 0)
            if not top_dynamic_id or not source_dynamic_id or not folder_name or pair_index <= 0:
                continue
            normalized[(top_dynamic_id, source_dynamic_id, pair_index)] = (
                top_dynamic_id,
                source_dynamic_id,
                folder_name,
                pair_index,
                str(mark.get("reason") or reason),
                created_at,
            )
        if not normalized:
            return 0
        with self.connect() as conn:
            conn.executemany(
                """
                insert into deleted_pair_marks(
                    top_dynamic_id, source_dynamic_id, folder_name, pair_index, reason, created_at
                )
                values (?, ?, ?, ?, ?, ?)
                on conflict(top_dynamic_id, source_dynamic_id, pair_index) do update set
                    folder_name = excluded.folder_name,
                    reason = excluded.reason
                """,
                list(normalized.values()),
            )
        return len(normalized)

    def list_deleted_pair_indices(self, top_dynamic_id: str, source_dynamic_id: str) -> set[int]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                select pair_index from deleted_pair_marks
                where top_dynamic_id = ? and source_dynamic_id = ?
                order by pair_index asc
                """,
                (str(top_dynamic_id), str(source_dynamic_id)),
            ).fetchall()
        return {int(row["pair_index"]) for row in rows}

    def has_deleted_pair_marks(self, top_dynamic_id: str, source_dynamic_id: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                """
                select 1 from deleted_pair_marks
                where top_dynamic_id = ? and source_dynamic_id = ?
                limit 1
                """,
                (str(top_dynamic_id), str(source_dynamic_id)),
            ).fetchone()
        return bool(row)

    def clear_deleted_pair_marks(self, top_dynamic_id: str, source_dynamic_id: str) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                "delete from deleted_pair_marks where top_dynamic_id = ? and source_dynamic_id = ?",
                (str(top_dynamic_id), str(source_dynamic_id)),
            )
            return int(cursor.rowcount or 0)

    def delete_folder_if_empty(self, folder_name: str) -> None:
        with self.connect() as conn:
            count = conn.execute(
                "select count(*) from assets where folder_name = ?",
                (folder_name,),
            ).fetchone()[0]
            if count == 0:
                conn.execute("delete from folders where folder_name = ?", (folder_name,))
                conn.execute("delete from folder_index where folder_name = ?", (folder_name,))
                conn.execute("delete from pair_index where folder_name = ?", (folder_name,))

    def create_task_run(self, task_type: str, status: str, message: str = "", details: dict[str, Any] | None = None) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """
                insert into task_runs(task_type, status, message, details_json, created_at)
                values (?, ?, ?, ?, ?)
                """,
                (task_type, status, message, dumps_json(details or {}), now_iso()),
            )
            return int(cur.lastrowid)

    def finish_task_run(self, task_id: int, status: str, message: str = "", details: dict[str, Any] | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                update task_runs
                set status = ?, message = ?, details_json = ?, finished_at = ?
                where id = ?
                """,
                (status, message, dumps_json(details or {}), now_iso(), task_id),
            )

    def last_task_run(self, task_type: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "select * from task_runs where task_type = ? order by id desc limit 1",
                (task_type,),
            ).fetchone()
        return dict(row) if row else None

    def list_task_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "select * from task_runs order by id desc limit ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_task_run(self, task_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("select * from task_runs where id = ?", (task_id,)).fetchone()
        return dict(row) if row else None

    def clear_finished_task_runs(self) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                "delete from task_runs where status != 'running'"
            )
            return int(cursor.rowcount or 0)

    def get_sidebar_count_cache(self) -> dict[str, Any]:
        with self.control_connect() as conn:
            rows = conn.execute("select * from sidebar_count_cache order by key asc").fetchall()
        counts = {str(row["key"]): int(row["count"] or 0) for row in rows}
        updated_at = {str(row["key"]): row["updated_at"] for row in rows}
        payloads = {
            str(row["key"]): loads_json(row["payload_json"], None)
            for row in rows
            if row["payload_json"]
        }
        return {"counts": counts, "updated_at": updated_at, "payloads": payloads}

    def set_sidebar_count(self, key: str, count: int, label: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                insert into sidebar_count_cache(key, count, label, payload_json, updated_at)
                values (?, ?, ?, null, ?)
                on conflict(key) do update set
                    count = excluded.count,
                    label = excluded.label,
                    updated_at = excluded.updated_at
                """,
                (str(key), max(0, int(count)), str(label), now_iso()),
            )

    def refresh_sidebar_count_cache(self, keys: list[str] | None = None) -> dict[str, Any]:
        requested = {str(key) for key in keys or [] if str(key)}
        if not requested:
            requested = {"all", "favorites", "livephoto", "video", "review", "logs", "tasks", "trash", "subscriptions", "sites"}
        if requested & {"all", "livephoto"}:
            requested.add("video")
        computed = self._compute_sidebar_counts(requested)
        now = now_iso()
        with self.connect() as conn:
            for key, value in computed.items():
                conn.execute(
                    """
                    insert into sidebar_count_cache(key, count, label, payload_json, updated_at)
                    values (?, ?, ?, ?, ?)
                    on conflict(key) do update set
                        count = excluded.count,
                        label = excluded.label,
                        payload_json = excluded.payload_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        key,
                        int(value.get("count") or 0),
                        value.get("label"),
                        dumps_json(value.get("payload")) if value.get("payload") is not None else None,
                        now,
                    ),
                )
        return self.get_sidebar_count_cache()

    def _compute_sidebar_counts(self, keys: set[str]) -> dict[str, dict[str, Any]]:
        labels = {
            "all": "全部项目",
            "favorites": "收藏",
            "livephoto": "Live Photo",
            "video": "视频",
            "duplicates": "重复内容",
            "review": "待审核",
            "logs": "过滤日志",
            "tasks": "任务队列",
            "trash": "内容垃圾桶",
            "subscriptions": "订阅管理",
            "sites": "站点订阅",
        }
        output: dict[str, dict[str, Any]] = {}
        with self.connect() as conn:
            if keys & {"all", "favorites", "livephoto"}:
                source_table = "folder_index" if self._gallery_index_ready_conn(conn) else "folders"
                row = conn.execute(
                    f"""
                    select
                        count(*) as all_count,
                        sum(case when has_livephoto = 1 then 1 else 0 end) as livephoto_count,
                        sum(case when is_favorite = 1 then 1 else 0 end) as favorites_count
                    from {source_table}
                    """
                ).fetchone()
                output["all"] = {"count": int(row["all_count"] or 0), "label": labels["all"]}
                output["livephoto"] = {"count": int(row["livephoto_count"] or 0), "label": labels["livephoto"]}
                output["favorites"] = {"count": int(row["favorites_count"] or 0), "label": labels["favorites"]}
            if "video" in keys:
                count = conn.execute("""
                    select count(*) from folders f where exists (
                        select 1 from assets a where a.folder_name = f.folder_name and a.media_type = 'video'
                    )
                """).fetchone()[0]
                output["video"] = {"count": int(count or 0), "label": labels["video"]}
            if "review" in keys:
                count = conn.execute("select count(*) from review_items where status = 'pending'").fetchone()[0]
                output["review"] = {"count": int(count or 0), "label": labels["review"]}
            if "logs" in keys:
                count = conn.execute("select count(*) from filter_logs").fetchone()[0]
                output["logs"] = {"count": int(count or 0), "label": labels["logs"]}
            if "tasks" in keys:
                count = conn.execute("select count(*) from task_runs").fetchone()[0]
                queued = conn.execute("select count(*) from task_runs where status = 'running'").fetchone()[0]
                output["tasks"] = {
                    "count": int(count or 0),
                    "label": labels["tasks"],
                    "payload": {"running": int(queued or 0)},
                }
            if "trash" in keys:
                count = conn.execute("select count(*) from trash_items where restored_at is null").fetchone()[0]
                output["trash"] = {"count": int(count or 0), "label": labels["trash"]}
            if "subscriptions" in keys:
                count = conn.execute(
                    "select (select count(*) from subscriptions) + (select count(*) from site_sources)"
                ).fetchone()[0]
                output["subscriptions"] = {"count": int(count or 0), "label": labels["subscriptions"]}
            if "sites" in keys:
                count = conn.execute("select count(*) from site_sources where source_type != 'xhs_likes'").fetchone()[0]
                output["sites"] = {"count": int(count or 0), "label": labels["sites"]}
        return {key: value for key, value in output.items() if key in keys}

    def _gallery_index_ready_conn(self, conn: sqlite3.Connection) -> bool:
        counts = conn.execute(
            """
            select
                (select count(*) from folders) as folder_rows,
                (select count(*) from folder_index) as folder_index_rows
            """
        ).fetchone()
        return int(counts["folder_rows"] or 0) == int(counts["folder_index_rows"] or 0)

    def upsert_review_item(
        self,
        top_dynamic_id: str,
        source_dynamic_id: str,
        folder_name_candidate: str,
        text_excerpt: str,
        reasons: list[str],
        payload: dict[str, Any],
        status: str = "pending",
    ) -> None:
        now = now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                insert into review_items(
                    top_dynamic_id, source_dynamic_id, folder_name_candidate, text_excerpt,
                    reasons_json, payload_json, status, created_at, updated_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(top_dynamic_id, source_dynamic_id) do update set
                    folder_name_candidate = excluded.folder_name_candidate,
                    text_excerpt = excluded.text_excerpt,
                    reasons_json = excluded.reasons_json,
                    payload_json = excluded.payload_json,
                    status = excluded.status,
                    updated_at = excluded.updated_at
                """,
                (
                    str(top_dynamic_id),
                    str(source_dynamic_id),
                    folder_name_candidate,
                    text_excerpt,
                    dumps_json(reasons),
                    dumps_json(payload),
                    status,
                    now,
                    now,
                ),
            )

    def list_review_items(self, status: str = "pending") -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "select * from review_items where status = ? order by updated_at desc",
                (status,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_review_item(self, item_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("select * from review_items where id = ?", (item_id,)).fetchone()
        return dict(row) if row else None

    def set_review_status(self, item_id: int, status: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "update review_items set status = ?, updated_at = ? where id = ?",
                (status, now_iso(), item_id),
            )

    def get_review_status(self, top_dynamic_id: str, source_dynamic_id: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                select status from review_items
                where top_dynamic_id = ? and source_dynamic_id = ?
                limit 1
                """,
                (str(top_dynamic_id), str(source_dynamic_id)),
            ).fetchone()
        return row["status"] if row else None

    def add_blacklist_item(
        self,
        top_dynamic_id: str,
        source_dynamic_id: str,
        folder_name: str,
        title: str,
        reason: str,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                insert into blacklist_items(
                    top_dynamic_id, source_dynamic_id, folder_name, title, reason, created_at
                )
                values (?, ?, ?, ?, ?, ?)
                on conflict(top_dynamic_id, source_dynamic_id) do update set
                    folder_name = excluded.folder_name,
                    title = excluded.title,
                    reason = excluded.reason
                """,
                (str(top_dynamic_id), str(source_dynamic_id), folder_name, title, reason, now_iso()),
            )

    def delete_blacklist_item(self, top_dynamic_id: str, source_dynamic_id: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "delete from blacklist_items where top_dynamic_id = ? and source_dynamic_id = ?",
                (str(top_dynamic_id), str(source_dynamic_id)),
            )

    def is_blacklisted(self, top_dynamic_id: str, source_dynamic_id: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                """
                select 1 from blacklist_items
                where top_dynamic_id = ? and source_dynamic_id = ?
                limit 1
                """,
                (str(top_dynamic_id), str(source_dynamic_id)),
            ).fetchone()
        return bool(row)

    def list_blacklist_items(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("select * from blacklist_items order by created_at desc").fetchall()
        return [dict(row) for row in rows]

    def upsert_trash_item(self, folder: dict[str, Any], assets: list[dict[str, Any]]) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                insert into trash_items(
                    top_dynamic_id, source_dynamic_id, folder_name, title, folder_json, assets_json, deleted_at
                )
                values (?, ?, ?, ?, ?, ?, ?)
                on conflict(top_dynamic_id, source_dynamic_id) do update set
                    folder_name = excluded.folder_name,
                    title = excluded.title,
                    folder_json = excluded.folder_json,
                    assets_json = excluded.assets_json,
                    deleted_at = excluded.deleted_at,
                    restored_at = null
                """,
                (
                    str(folder["top_dynamic_id"]),
                    str(folder["source_dynamic_id"]),
                    folder["folder_name"],
                    folder.get("title") or folder["folder_name"],
                    dumps_json(folder),
                    dumps_json(assets),
                    now_iso(),
                ),
            )

    def list_trash_items(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "select * from trash_items where restored_at is null order by deleted_at desc"
            ).fetchall()
        return [dict(row) for row in rows]

    def get_trash_item(self, item_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("select * from trash_items where id = ?", (item_id,)).fetchone()
        return dict(row) if row else None

    def mark_trash_restored(self, item_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "update trash_items set restored_at = ? where id = ?",
                (now_iso(), item_id),
            )

    def delete_folder(self, folder_name: str) -> None:
        with self.connect() as conn:
            conn.execute("delete from folders where folder_name = ?", (folder_name,))
            conn.execute("delete from folder_index where folder_name = ?", (folder_name,))
            conn.execute("delete from pair_index where folder_name = ?", (folder_name,))

    def delete_site_gallery_post(self, source_id: int, post_id: int, fallback_folder_name: str | None = None) -> list[str]:
        dynamic_id = f"site:{int(source_id)}:{int(post_id)}"
        folder_names: set[str] = set()
        if fallback_folder_name:
            folder_names.add(str(fallback_folder_name))
        with self.connect() as conn:
            rows = conn.execute(
                """
                select folder_name from folders
                where top_dynamic_id = ? or source_dynamic_id = ?
                union
                select folder_name from folder_index
                where top_dynamic_id = ? or source_dynamic_id = ?
                """,
                (dynamic_id, dynamic_id, dynamic_id, dynamic_id),
            ).fetchall()
            folder_names.update(str(row["folder_name"]) for row in rows)
            for folder_name in folder_names:
                conn.execute("delete from folders where folder_name = ?", (folder_name,))
                conn.execute("delete from folder_index where folder_name = ?", (folder_name,))
                conn.execute("delete from pair_index where folder_name = ?", (folder_name,))
        return sorted(folder_names)

    def add_filter_log(
        self,
        top_dynamic_id: str,
        source_dynamic_id: str,
        folder_name_candidate: str,
        decision: str,
        reasons: list[str],
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                insert into filter_logs(
                    top_dynamic_id, source_dynamic_id, folder_name_candidate, decision, reasons_json, created_at
                )
                values (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(top_dynamic_id),
                    str(source_dynamic_id),
                    folder_name_candidate,
                    decision,
                    dumps_json(reasons),
                    now_iso(),
                ),
            )

    def list_filter_logs(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "select * from filter_logs order by id desc limit ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def clear_filter_logs(self) -> int:
        with self.connect() as conn:
            cursor = conn.execute("delete from filter_logs")
            return int(cursor.rowcount or 0)

    def clear_gallery_indexes(self) -> None:
        with self.connect() as conn:
            conn.execute("delete from pair_index")
            conn.execute("delete from folder_index")

    def gallery_page_cache_signature(self) -> str:
        with self.connect() as conn:
            return self._content_revision(conn)

    def _content_revision(self, conn) -> str:
        schemas = ('main', 'browse', 'control') if self.modules_enabled else ('main',)
        return ':'.join(str(conn.execute(f'select revision from {schema}.content_revision where id=1').fetchone()[0]) for schema in schemas)

    def _cached_content_stats(self, key, compute, extra=()) -> dict:
        # Health polling must not repeatedly scan media tables. Revisions and
        # counts share a read snapshot, so concurrent writes cannot poison a
        # cached revision with counts from another transaction. Cache hits only
        # read three one-row counters and never write a persistent cache.
        with self._stats_lock, self.connect() as conn:
            conn.execute('begin')
            signature = (self.modules_enabled, self._content_revision(conn), *extra)
            cached = self._stats_cache.get(key)
            if cached and cached[0] == signature:
                return dict(cached[1])
            result = compute(conn)
            self._stats_cache[key] = (signature, dict(result))
            return result

    def get_gallery_page_cache(self, cache_key: str, signature: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "select * from gallery_page_cache where cache_key = ? limit 1",
                (str(cache_key),),
            ).fetchone()
            if not row:
                return None
            if str(row["signature"]) != str(signature):
                conn.execute("delete from gallery_page_cache where cache_key = ?", (str(cache_key),))
                return None
        return dict(row)

    def set_gallery_page_cache(self, cache_key: str, signature: str, payload: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                insert into gallery_page_cache(cache_key, signature, payload_json, updated_at)
                values (?, ?, ?, ?)
                on conflict(cache_key) do update set
                    signature = excluded.signature,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (str(cache_key), str(signature), dumps_json(payload), now_iso()),
            )
            conn.execute('delete from gallery_page_cache where cache_key in (select cache_key from gallery_page_cache order by updated_at desc, rowid desc limit -1 offset 256)')

    def replace_gallery_index(
        self,
        folder_name: str,
        folder_index: dict[str, Any],
        pair_indexes: list[dict[str, Any]],
    ) -> None:
        now = now_iso()
        folder_payload = {
            "folder_name": folder_name,
            "title": folder_index.get("title", ""),
            "text_prefix": folder_index.get("text_prefix", ""),
            "pub_ts": int(folder_index.get("pub_ts") or 0),
            "pub_time": folder_index.get("pub_time", ""),
            "top_dynamic_id": str(folder_index.get("top_dynamic_id") or ""),
            "source_dynamic_id": str(folder_index.get("source_dynamic_id") or ""),
            "subscription_uid": str(folder_index.get("subscription_uid") or ""),
            "subscription_name": folder_index.get("subscription_name"),
            "has_images": 1 if folder_index.get("has_images") else 0,
            "has_livephoto": 1 if folder_index.get("has_livephoto") else 0,
            "is_favorite": 1 if folder_index.get("is_favorite") else 0,
            "review_status": folder_index.get("review_status", "approved"),
            "review_reason": folder_index.get("review_reason"),
            "image_count": int(folder_index.get("image_count") or 0),
            "livephoto_count": int(folder_index.get("livephoto_count") or 0),
            "asset_count": int(folder_index.get("asset_count") or 0),
            "preview_assets_json": folder_index.get("preview_assets_json", "[]"),
            "year_key": folder_index.get("year_key"),
            "month_key": folder_index.get("month_key"),
            "updated_at": now,
        }
        with self.connect() as conn:
            conn.execute(
                """
                insert into folder_index(
                    folder_name, title, text_prefix, pub_ts, pub_time, top_dynamic_id, source_dynamic_id,
                    subscription_uid, subscription_name, has_images, has_livephoto, is_favorite, review_status,
                    review_reason, image_count, livephoto_count, asset_count, preview_assets_json, year_key,
                    month_key, updated_at
                )
                values(
                    :folder_name, :title, :text_prefix, :pub_ts, :pub_time, :top_dynamic_id, :source_dynamic_id,
                    :subscription_uid, :subscription_name, :has_images, :has_livephoto, :is_favorite, :review_status,
                    :review_reason, :image_count, :livephoto_count, :asset_count, :preview_assets_json, :year_key,
                    :month_key, :updated_at
                )
                on conflict(folder_name) do update set
                    title = excluded.title,
                    text_prefix = excluded.text_prefix,
                    pub_ts = excluded.pub_ts,
                    pub_time = excluded.pub_time,
                    top_dynamic_id = excluded.top_dynamic_id,
                    source_dynamic_id = excluded.source_dynamic_id,
                    subscription_uid = excluded.subscription_uid,
                    subscription_name = excluded.subscription_name,
                    has_images = excluded.has_images,
                    has_livephoto = excluded.has_livephoto,
                    is_favorite = excluded.is_favorite,
                    review_status = excluded.review_status,
                    review_reason = excluded.review_reason,
                    image_count = excluded.image_count,
                    livephoto_count = excluded.livephoto_count,
                    asset_count = excluded.asset_count,
                    preview_assets_json = excluded.preview_assets_json,
                    year_key = excluded.year_key,
                    month_key = excluded.month_key,
                    updated_at = excluded.updated_at
                """,
                folder_payload,
            )
            conn.execute("update folder_index set imported_at=coalesce((select created_at from folders where folder_name=?),'') where folder_name=?", (folder_name, folder_name))
            conn.execute("delete from pair_index where folder_name = ?", (folder_name,))
            for row in pair_indexes:
                conn.execute(
                    """
                    insert into pair_index(
                        item_key, folder_name, pair_index, title, pub_ts, pub_time, subscription_uid,
                        subscription_name, is_favorite, has_image, has_livephoto, preview_url, preview_kind,
                        thumb_url, display_ratio, image_json, livephoto_json, year_key, month_key, updated_at
                    )
                    values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["item_key"],
                        folder_name,
                        int(row.get("pair_index") or 0),
                        row.get("title", ""),
                        int(row.get("pub_ts") or 0),
                        row.get("pub_time", ""),
                        str(row.get("subscription_uid") or ""),
                        row.get("subscription_name"),
                        1 if row.get("is_favorite") else 0,
                        1 if row.get("has_image") else 0,
                        1 if row.get("has_livephoto") else 0,
                        row.get("preview_url"),
                        row.get("preview_kind"),
                        row.get("thumb_url"),
                        row.get("display_ratio"),
                        row.get("image_json"),
                        row.get("livephoto_json"),
                        row.get("year_key"),
                        row.get("month_key"),
                        now,
                    ),
                )

            conn.execute("update pair_index set imported_at=coalesce((select created_at from folders where folder_name=?),'') where folder_name=?", (folder_name, folder_name))

    def gallery_index_status(self) -> dict[str, Any]:
        settings = self.get_settings()
        def compute(conn):
            counts = conn.execute(
                """
                select
                    (select count(*) from folders) as folder_rows,
                    (select count(*) from folder_index) as folder_index_rows,
                    (select count(*) from pair_index) as pair_index_rows,
                    (
                        select count(*)
                        from (
                            select folder_name, pair_index
                            from assets
                            group by folder_name, pair_index
                        )
                    ) as asset_pair_rows
                """
            ).fetchone()
            version = int(settings.get("gallery_index_version") or 0)
            stale = (
                version != GALLERY_INDEX_VERSION
                or (counts["folder_rows"] and not counts["folder_index_rows"])
                or int(counts["folder_index_rows"] or 0) != int(counts["folder_rows"] or 0)
                or int(counts["pair_index_rows"] or 0) != int(counts["asset_pair_rows"] or 0)
            )
            return {
                "version": version,
                "current_version": GALLERY_INDEX_VERSION,
                "rebuilt_at": settings.get("gallery_index_rebuilt_at"),
                "rebuilding": bool(settings.get("gallery_index_rebuilding")),
                "folder_rows": int(counts["folder_rows"] or 0),
                "folder_index_rows": int(counts["folder_index_rows"] or 0),
                "pair_index_rows": int(counts["pair_index_rows"] or 0),
                "asset_pair_rows": int(counts["asset_pair_rows"] or 0),
                "stale": stale,
            }
        return self._cached_content_stats('gallery-index', compute, (
            settings.get('gallery_index_version'), settings.get('gallery_index_rebuilt_at'),
            bool(settings.get('gallery_index_rebuilding')),
        ))

    def gallery_index_needs_rebuild(self) -> bool:
        return bool(self.gallery_index_status().get("stale"))

    def gallery_index_ready(self) -> bool:
        with self.connect() as conn:
            counts = conn.execute(
                """
                select
                    (select count(*) from folders) as folder_rows,
                    (select count(*) from folder_index) as folder_index_rows
                """
            ).fetchone()
        folder_rows = int(counts["folder_rows"] or 0)
        folder_index_rows = int(counts["folder_index_rows"] or 0)
        if folder_rows != folder_index_rows:
            return False
        settings = self.get_settings()
        return folder_rows == 0 or int(settings.get("gallery_index_version") or 0) == GALLERY_INDEX_VERSION

    def _query_browse_page(self, table, where_sql, params, page, page_size, column, direction, random_order):
        # Each boundary is a small lookup entry for the next waterfall page.
        # Deep sequential browsing seeks into the B tree instead of walking OFFSET.
        revision = self.gallery_page_cache_signature()
        key = dumps_json([table, where_sql, params, page_size, column, direction])
        pair = table == 'pair_index'
        order = f"{column} {direction}, folder_name {direction}" + (", pair_index asc" if pair else "")
        with self.connect() as conn:
            cached = conn.execute('select * from browse_queries where query_key=?', (key,)).fetchone()
            if not cached or cached['revision'] != revision:
                total = conn.execute(f'select count(*) from {table} where {where_sql}', params).fetchone()[0]
                conn.execute('delete from browse_boundaries where query_key=?', (key,))
                conn.execute('insert or replace into browse_queries values(?,?,?)', (key, revision, total))
            else:
                total = cached['total']
            boundary = conn.execute('select * from browse_boundaries where query_key=? and page=?', (key, page)).fetchone()
            if random_order:
                offsets = random.sample(range(total), min(page_size, total))
                rows = [conn.execute(f'select * from {table} where {where_sql} order by {order} limit 1 offset ?', [*params, offset]).fetchone() for offset in offsets]
            else:
                predicate, values = '', []
                offset = max(page - 1, 0) * page_size
                if boundary:
                    op = '>' if direction == 'asc' else '<'
                    # The inclusive tuple bound seeks directly into the index;
                    # the residual handles pairs ascending within each folder.
                    if pair:
                        predicate = f' and ({column},folder_name) {op}= (?,?) and (({column},folder_name) {op} (?,?) or pair_index>?)'
                        values = [boundary['sort_value'], boundary['folder_name']] * 2 + [boundary['pair_index']]
                    else:
                        predicate = f' and ({column},folder_name) {op} (?,?)'
                        values = [boundary['sort_value'], boundary['folder_name']]
                    offset = 0
                rows = conn.execute(f'select * from {table} where ({where_sql}){predicate} order by {order} limit ? offset ?', [*params, *values, page_size, offset]).fetchall()
                if rows:
                    last = rows[-1]
                    conn.execute('insert or ignore into browse_boundaries values(?,?,?,?,?)', (key, page+1, last[column], last['folder_name'], last['pair_index'] if pair else 0))
            # Bound lookup storage even when filters/search combinations proliferate.
            conn.execute('delete from browse_boundaries where query_key in (select query_key from browse_queries order by rowid desc limit -1 offset 128)')
            conn.execute('delete from browse_queries where rowid in (select rowid from browse_queries order by rowid desc limit -1 offset 128)')
        return {'total': total, 'items': [dict(row) for row in rows if row is not None]}

    def query_folder_index(
        self,
        category: str = "all",
        year: str | None = None,
        month: str | None = None,
        start_month: str | None = None,
        end_month: str | None = None,
        subscription_uids: list[str] | None = None,
        source_kind: str = "all",
        search_query: str | None = None,
        page: int = 1,
        page_size: int = 24,
        sort_order: str = "desc",
    ) -> dict[str, Any]:
        where_sql, params = self._gallery_index_where(
            category=category,
            year=year,
            month=month,
            start_month=start_month,
            end_month=end_month,
            subscription_uids=subscription_uids,
            source_kind=source_kind,
            search_query=search_query,
            table_alias="folder_index",
            pair_mode=False,
        )
        random_order = sort_order == "random"
        order_direction = "asc" if sort_order == "asc" else "desc"
        order_column = "pub_ts"
        if sort_order == "imported":
            order_column = "imported_at"
        return self._query_browse_page("folder_index", where_sql, params, page, page_size, order_column, order_direction, random_order)

    def query_pair_index(
        self,
        category: str = "all",
        year: str | None = None,
        month: str | None = None,
        start_month: str | None = None,
        end_month: str | None = None,
        subscription_uids: list[str] | None = None,
        source_kind: str = "all",
        search_query: str | None = None,
        page: int = 1,
        page_size: int = 24,
        sort_order: str = "desc",
    ) -> dict[str, Any]:
        where_sql, params = self._gallery_index_where(
            category=category,
            year=year,
            month=month,
            start_month=start_month,
            end_month=end_month,
            subscription_uids=subscription_uids,
            source_kind=source_kind,
            search_query=search_query,
            table_alias="pair_index",
            pair_mode=True,
        )
        random_order = sort_order == "random"
        order_direction = "asc" if sort_order == "asc" else "desc"
        order_column = "pub_ts"
        if sort_order == "imported":
            order_column = "imported_at"
        return self._query_browse_page("pair_index", where_sql, params, page, page_size, order_column, order_direction, random_order)

    def gallery_meta_from_index(self) -> dict[str, Any]:
        with self.connect() as conn:
            counts_row = conn.execute(
                """
                select
                    count(*) as all_count,
                    sum(case when exists (select 1 from assets a where a.folder_name = folder_index.folder_name and a.media_type = 'video') then 1 else 0 end) as video_count,
                    sum(case when has_images = 1 then 1 else 0 end) as images_count,
                    sum(case when has_livephoto = 1 then 1 else 0 end) as livephoto_count,
                    sum(case when has_images = 1 and has_livephoto = 1 then 1 else 0 end) as paired_count,
                    sum(case when not (has_images = 1 and has_livephoto = 1) then 1 else 0 end) as unpaired_count,
                    sum(case when is_favorite = 1 then 1 else 0 end) as favorites_count
                from folder_index
                """
            ).fetchone()
            months = conn.execute(
                """
                select distinct year_key, month_key
                from folder_index
                where coalesce(month_key, '') != ''
                order by year_key desc, month_key desc
                """
            ).fetchall()
            subscriptions = conn.execute(
                """
                select subscription_uid as uid,
                       max(subscription_name) as name,
                       count(*) as count
                from folder_index
                where coalesce(subscription_uid, '') != ''
                group by subscription_uid
                order by lower(coalesce(max(subscription_name), subscription_uid)) asc
                """
            ).fetchall()
        years: dict[str, list[str]] = {}
        for row in months:
            year_key = str(row["year_key"] or "未知")
            month_key = str(row["month_key"] or "")
            years.setdefault(year_key, [])
            if month_key and month_key not in years[year_key]:
                years[year_key].append(month_key)
        return {
            "counts": {
                "all": int(counts_row["all_count"] or 0),
                "images": int(counts_row["images_count"] or 0),
                "video": int(counts_row["video_count"] or 0),
                "livephoto": int(counts_row["livephoto_count"] or 0),
                "paired": int(counts_row["paired_count"] or 0),
                "unpaired": int(counts_row["unpaired_count"] or 0),
                "favorites": int(counts_row["favorites_count"] or 0),
            },
            "years": {year: months for year, months in years.items()},
            "subscriptions": [
                {
                    "uid": str(row["uid"]),
                    "name": row["name"] or f"UID {row['uid']}",
                    "count": int(row["count"] or 0),
                }
                for row in subscriptions
            ],
        }

    def subscription_stats_from_index(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                select
                    folder_index.subscription_uid as uid,
                    max(folder_index.subscription_name) as uname,
                    count(*) as folder_count,
                    sum(folder_index.image_count) as image_count,
                    sum(folder_index.livephoto_count) as livephoto_count,
                    sum(folder_index.asset_count) as asset_count,
                    max(folder_index.pub_ts) as latest_content_ts,
                    (
                        select latest.pub_time
                        from folder_index as latest
                        where latest.subscription_uid = folder_index.subscription_uid
                        order by latest.pub_ts desc, latest.folder_name desc
                        limit 1
                    ) as latest_content_at
                from folder_index
                where coalesce(folder_index.subscription_uid, '') != ''
                group by folder_index.subscription_uid
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def _gallery_index_where(
        self,
        *,
        category: str,
        year: str | None,
        month: str | None,
        start_month: str | None,
        end_month: str | None,
        subscription_uids: list[str] | None,
        source_kind: str,
        search_query: str | None,
        table_alias: str,
        pair_mode: bool,
    ) -> tuple[str, list[Any]]:
        clauses = ["1 = 1"]
        params: list[Any] = []
        column = lambda name: f"{table_alias}.{name}"
        if subscription_uids:
            normalized = [str(uid) for uid in subscription_uids if str(uid).strip()]
            if normalized:
                placeholders = ", ".join("?" for _ in normalized)
                clauses.append(f"{column('subscription_uid')} in ({placeholders})")
                params.extend(normalized)
        elif source_kind in {"site", "xhs"}:
            clauses.append(f"{column('subscription_uid')} like 'site:%'")
            operator = "in" if source_kind == "xhs" else "not in"
            clauses.append(f"""{column('subscription_uid')} {operator}
                (select 'site:' || id from site_sources where source_type = 'xhs_likes')""")
        elif source_kind == "up":
            clauses.append(f"coalesce({column('subscription_uid')}, '') not like 'site:%'")
        if year:
            clauses.append(f"{column('year_key')} = ?")
            params.append(year)
        if month:
            clauses.append(f"{column('month_key')} = ?")
            params.append(month)
        elif start_month or end_month:
            range_start = min(item for item in [start_month, end_month] if item)
            range_end = max(item for item in [start_month, end_month] if item)
            clauses.append(f"{column('month_key')} >= ? and {column('month_key')} <= ?")
            params.extend([range_start, range_end])
        if category == "images":
            clauses.append(f"{column('has_images' if not pair_mode else 'has_image')} = 1")
        elif category == "video":
            clauses.append(f"exists (select 1 from assets a where a.folder_name = {column('folder_name')} and a.media_type = 'video')")
        elif category == "livephoto":
            clauses.append(f"{column('has_livephoto')} = 1")
        elif category == "paired":
            clauses.append(f"{column('has_livephoto')} = 1")
            clauses.append(f"{column('has_images' if not pair_mode else 'has_image')} = 1")
        elif category == "unpaired":
            clauses.append(
                f"not ({column('has_livephoto')} = 1 and {column('has_images' if not pair_mode else 'has_image')} = 1)"
            )
        elif category == "favorites":
            clauses.append(f"{column('is_favorite')} = 1")
        terms = [term.strip().lower() for term in str(search_query or "").split() if term.strip()]
        for term in terms:
            like_value = f"%{term}%"
            if pair_mode:
                clauses.append(
                    f"(lower({column('title')}) like ? or lower({column('folder_name')}) like ? or lower(coalesce({column('subscription_name')}, '')) like ?)"
                )
                params.extend([like_value, like_value, like_value])
            else:
                clauses.append(
                    f"(lower({column('title')}) like ? or lower({column('text_prefix')}) like ? or lower({column('folder_name')}) like ? or lower(coalesce({column('subscription_name')}, '')) like ?)"
                )
                params.extend([like_value, like_value, like_value, like_value])
        return " and ".join(clauses), params

    def clear_content_data(self) -> None:
        with self.connect() as conn:
            conn.execute("delete from pair_index")
            conn.execute("delete from folder_index")
            conn.execute("delete from assets")
            conn.execute("delete from folders")
            conn.execute("delete from review_items")
            conn.execute("delete from filter_logs")
            conn.execute("delete from blacklist_items")
            conn.execute("delete from deleted_pair_marks")
            conn.execute("delete from trash_items")
            conn.execute("delete from duplicate_ignores")
            conn.execute("delete from duplicate_group_cache")
            conn.execute("delete from task_runs")
