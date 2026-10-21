from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import fcntl
import hashlib
import json
import time
from typing import Callable, Iterable
from uuid import uuid4

from app.db import Database
from app.services.utils import dumps_json, now_iso
from app.services.xhs.adapter import XhsLikesAdapter
from app.services.xhs.models import MAX_ASSETS, MAX_NOTES, XhsManifest, XhsNote, XhsValidationError, identifier, integer, text


class XhsConflict(RuntimeError):
    pass


# 锚点扑空止损：增量扫描最多翻到 2 倍基数仍未命中锚点即止损；
# 基数复用“首次拉取条数”，该值为“不限”时回退到此条数。
ANCHOR_MISS_BASE_FALLBACK = 1000
# 锚点标题指纹：提交锚点时顺带存归一化标题的前这么多字；扑空轮超过基数后，
# 若本轮已读到“同作者且标题吻合”的笔记（疑似作者删旧发新）则视同触底提前结束。
# 作者缺失时要求更长的标题以降低模板标题撞车的误停概率。
ANCHOR_TITLE_PREFIX_CHARS = 64
ANCHOR_TITLE_MIN = 4
ANCHOR_TITLE_MIN_WITHOUT_AUTHOR = 8


def _anchor_title_matches(profiles, note: XhsNote) -> bool:
    title = note.title
    if not title or title == note.note_id or len(title) < ANCHOR_TITLE_MIN:
        return False
    for profile in profiles:
        prefix = profile["title_prefix"]
        if not prefix or not title.startswith(prefix):
            continue
        if profile["author_id"] and profile["author_id"] == note.author_id:
            return True
        if not profile["author_id"] and len(title) >= ANCHOR_TITLE_MIN_WITHOUT_AUTHOR:
            return True
    return False


@dataclass(frozen=True)
class ScanPage:
    """Only a future verified worker transport may construct these pages."""

    notes: tuple[XhsNote, ...]
    end_of_feed: bool = False
    integrity_ok: bool = True


class XhsSyncService:
    def __init__(self, db: Database) -> None:
        self.db = db

    @contextmanager
    def _source_lock(self, source_id: int):
        lock_dir = self.db.db_path.parent / ".xhs-locks"
        lock_dir.mkdir(exist_ok=True)
        with (lock_dir / f"{int(source_id)}.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise XhsConflict("此小红书来源已有导入或扫描任务") from None
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def create_source(self, payload: dict) -> dict:
        from app.services.xhs.models import fields
        fields(payload, {"name", "owner_key", "anchor_count", "initial_fetch_limit", "refresh_enabled", "refresh_interval_minutes"}, {"name", "owner_key"})
        name = text(payload["name"], 100)
        owner = identifier(payload["owner_key"], 8)
        count = integer(payload.get("anchor_count", 3), 1, 20)
        limit = self._limit(payload.get("initial_fetch_limit", 100))
        refresh_interval = integer(payload.get("refresh_interval_minutes", 720), 23, 525600)
        refresh_enabled = payload.get("refresh_enabled", False)
        if type(refresh_enabled) is not bool:
            raise XhsValidationError("自动刷新状态必须为布尔值")
        now = now_iso()
        with self.db.connect() as conn:
            conn.execute("begin immediate")
            cursor = conn.execute(
                """insert into site_sources(name, slug, source_type, entry_url, use_proxy, enabled, created_at, updated_at)
                   values (?, ?, 'xhs_likes', 'https://www.xiaohongshu.com/', 0, 1, ?, ?)""",
                (name, f"xhs-{uuid4().hex}", now, now),
            )
            source_id = int(cursor.lastrowid)
            conn.execute(
                """insert into xhs_source_accounts(source_id, owner_key, anchor_count, initial_fetch_limit,
                   refresh_interval_minutes, refresh_enabled) values (?, ?, ?, ?, ?, ?)""",
                (source_id, owner, count, limit, refresh_interval, int(refresh_enabled)),
            )
        self.db.refresh_sidebar_count_cache(["sites", "subscriptions"])
        return self.get_source(source_id)

    @staticmethod
    def _limit(value):
        return None if value is None else integer(value, 1, MAX_NOTES)

    def get_source(self, source_id: int, conn=None) -> dict:
        if conn is None:
            with self.db.connect() as connection:
                return self.get_source(source_id, connection)
        row = conn.execute(
            """select s.id, s.name, s.source_type, s.enabled, s.icon_url, s.icon_tiny_url, a.*
               from site_sources s join xhs_source_accounts a on a.source_id = s.id
               where s.id = ? and s.source_type = 'xhs_likes'""",
            (source_id,),
        ).fetchone()
        if not row:
            raise XhsValidationError("小红书来源不存在")
        return dict(row)

    def update_source(self, source_id: int, payload: dict) -> dict:
        from app.services.xhs.models import fields
        fields(payload, {"anchor_count", "initial_fetch_limit", "enabled", "config_version", "refresh_enabled", "refresh_interval_minutes", 'ordering_stable', 'full_scan_interval_days'}, {"config_version"})
        integer(payload["config_version"], 0, 2**31 - 1)
        with self.db.connect() as conn:
            conn.execute("begin immediate")
            current = self.get_source(source_id, conn)
            if current["config_version"] != payload["config_version"]:
                raise XhsConflict("来源设置已更新，请刷新后重试")
            count = integer(payload.get("anchor_count", current["anchor_count"]), 1, 20)
            limit = self._limit(payload.get("initial_fetch_limit", current["initial_fetch_limit"]))
            enabled = payload.get("enabled", bool(current["enabled"]))
            refresh_enabled = payload.get("refresh_enabled", bool(current["refresh_enabled"]))
            refresh_interval = integer(payload.get("refresh_interval_minutes", current["refresh_interval_minutes"]), 23, 525600)
            full_interval = integer(payload.get('full_scan_interval_days', current['full_scan_interval_days']), 1, 3650)
            ordering = payload.get('ordering_stable', bool(current['ordering_stable']))
            if type(ordering) is not bool:
                raise XhsValidationError('锚点早停状态必须为布尔值')
            if type(enabled) is not bool or type(refresh_enabled) is not bool:
                raise XhsValidationError("启用状态必须为布尔值")
            conn.execute(
                """update xhs_source_accounts set anchor_count = ?, initial_fetch_limit = ?,
                   refresh_interval_minutes = ?, refresh_enabled = ?, ordering_stable = ?, full_scan_interval_days = ?,
                   initial_fetch_completed = case when initial_fetch_limit is not ? then 0 else initial_fetch_completed end,
                   config_version = config_version + 1
                   where source_id = ?""", (count, limit, refresh_interval, int(refresh_enabled), int(ordering), full_interval, limit, source_id),
            )
            conn.execute("update site_sources set enabled = ?, updated_at = ? where id = ?", (int(enabled), now_iso(), source_id))
        return self.get_source(source_id)

    def status(self) -> dict:
        with self.db.connect() as conn:
            ids = [row[0] for row in conn.execute("select source_id from xhs_source_accounts order by source_id desc")]
            items = []
            for source_id in ids:
                item = self.get_source(source_id, conn)
                item["anchors"] = self._anchors(conn, item)
                item["last_run"] = self._last_run(conn, source_id)
                item["network_mode"] = "direct"
                schedule = conn.execute("select next_run_at, jitter_minutes from xhs_schedule_state where source_id = ?", (source_id,)).fetchone()
                item["next_refresh_at"] = schedule["next_run_at"] if schedule else None
                item["refresh_jitter_minutes"] = schedule["jitter_minutes"] if schedule else None
                item["note_count"] = conn.execute("select count(*) from xhs_like_state where source_id = ?", (source_id,)).fetchone()[0]
                item["pending_media"] = conn.execute(
                    """select count(*) from site_assets a join xhs_like_state l on a.post_id = l.site_post_id
                       where l.source_id = ? and a.status != 'ready'""", (source_id,),
                ).fetchone()[0]
                item['pending_notes'] = conn.execute('''select count(*) from xhs_note_downloads d join site_posts p on p.id=d.post_id
                    where p.source_id=? and d.status!='ready' ''', (source_id,)).fetchone()[0]
                item['last_full_at'] = conn.execute("select max(finished_at) from xhs_sync_runs where source_id=? and complete_snapshot=1", (source_id,)).fetchone()[0]
                item['last_incremental_at'] = conn.execute("select max(finished_at) from xhs_sync_runs where source_id=? and result='SUCCESS_INCREMENTAL'", (source_id,)).fetchone()[0]
                items.append(item)
        return {"mode": "manual_manifest", "automatic_sync_available": False, "items": items}

    @staticmethod
    def _anchors(conn, source):
        return [row[0] for row in conn.execute(
            "select note_id from xhs_anchor_candidates where source_id = ? and scan_id = ? order by rank",
            (source["source_id"], source["last_checkpoint_scan_id"]),
        )]

    @staticmethod
    def _last_run(conn, source_id):
        row = conn.execute("select * from xhs_sync_runs where source_id = ? order by rowid desc limit 1", (source_id,)).fetchone()
        return dict(row) if row else None

    def runs(self, source_id: int | None = None, limit: int = 50) -> list[dict]:
        with self.db.connect() as conn:
            return [dict(row) for row in conn.execute(
                "select * from xhs_sync_runs where (? is null or source_id = ?) order by rowid desc limit ?",
                (source_id, source_id, integer(limit, 1, 200)),
            )]

    def notes(self, source_id: int) -> list[dict]:
        with self.db.connect() as conn:
            self.get_source(source_id, conn)
            return [dict(row) for row in conn.execute(
                """select l.note_id, l.note_type, l.author_name, l.author_id, l.first_seen_at, l.last_seen_at, l.is_currently_liked,
                       p.title, p.url, p.pub_date, p.asset_count, d.status as download_status, d.error_category,
                       (select f.folder_name from folders f where f.folder_name =
                           'xhs_' || l.source_id || '_' || l.note_id limit 1) as folder_name
                   from xhs_like_state l join site_posts p on p.id = l.site_post_id
                   left join xhs_note_downloads d on d.post_id = p.id where l.source_id = ?
                   order by l.first_seen_at desc, p.id desc limit 50""", (source_id,),
            )]

    def import_manifest(self, source_id: int, raw: bytes) -> dict:
        manifest = XhsManifest.parse(raw)
        with self._source_lock(source_id):
            source = self.get_source(source_id)
            if manifest.owner_key != source["owner_key"]:
                raise XhsConflict("清单账号与此来源不一致，需显式创建独立来源")
            limited = not source["initial_fetch_completed"] and source["initial_fetch_limit"] is not None
            notes = manifest.notes[:source["initial_fetch_limit"]] if limited else manifest.notes
            reason = "first_sync_limit" if len(notes) < len(manifest.notes) else "manual_import"
            return self._commit(source, manifest, notes, reason, False, False,
                                hashlib.sha256(raw).hexdigest(), first_fetch_done=bool(notes))

    def scan_pages(self, source_id: int, owner_key: str, pages: Iterable[ScanPage], *,
                   ignore_anchors: bool = False, max_pages: int = 100, max_seconds: float = 300,
                   cancelled: Callable[[], bool] | None = None, emit_task_history: bool = True) -> dict:
        """Internal discovery engine; deliberately unavailable through HTTP until P0/P2 are verified."""
        integer(max_pages, 1, 1000)
        if not 0 < max_seconds <= 3600:
            raise XhsValidationError("扫描时长须在 0–3600 秒之间")
        with self._source_lock(source_id):
            source = self.get_source(source_id)
            if not source["enabled"]:
                raise XhsConflict("来源已停用")
            if source["owner_key"] != owner_key:
                raise XhsConflict("NEEDS_REBIND：账号与来源不一致")
            # Older limited scans marked initialization complete without saving
            # a checkpoint. Repeat initialization once to establish usable anchors.
            initial_scan = not source["initial_fetch_completed"] or not source["last_checkpoint_scan_id"]
            anchors, profiles = set(), []
            with self.db.connect() as conn:
                if not ignore_anchors and not initial_scan:
                    rows = conn.execute(
                        """select note_id, title_prefix, author_id from xhs_anchor_candidates
                           where source_id = ? and scan_id = ? order by rank""",
                        (source["source_id"], source["last_checkpoint_scan_id"]),
                    ).fetchall()
                    anchors = {row["note_id"] for row in rows}
                    profiles = [row for row in rows if row["title_prefix"]]
            notes: dict[str, XhsNote] = {}
            started, clock = now_iso(), time.monotonic()
            hit, title_hit, complete, reliable, reason, asset_count = None, False, False, True, "cursor_error", 0
            initial_limit = source["initial_fetch_limit"] if initial_scan else None
            stop_limit = None
            check_base = None
            if not ignore_anchors and not initial_scan:
                base = source["initial_fetch_limit"] if source["initial_fetch_limit"] is not None else ANCHOR_MISS_BASE_FALLBACK
                check_base = min(base, MAX_NOTES)
                stop_limit = min(2 * base, MAX_NOTES)
            iterator = iter(pages)
            try:
                for page_index in range(max_pages):
                    if cancelled and cancelled():
                        reason = "cancelled"
                        break
                    if time.monotonic() - clock >= max_seconds:
                        reason = "time_limit"
                        break
                    try:
                        page = next(iterator)
                    except StopIteration:
                        break
                    if cancelled and cancelled():
                        reason = "cancelled"
                        break
                    if time.monotonic() - clock >= max_seconds:
                        reason = "time_limit"
                        break
                    if not isinstance(page, ScanPage) or len(page.notes) > MAX_NOTES:
                        reason = "structure_changed"
                        break
                    reliable = reliable and page.integrity_ok
                    limit_hit = False
                    for note in page.notes:
                        if not isinstance(note, XhsNote):
                            raise XhsValidationError("笔记尚未规范化")
                        if note.note_id in notes:
                            reliable = False
                            continue
                        if len(notes) >= MAX_NOTES or (initial_limit is not None and len(notes) >= initial_limit):
                            limit_hit = True
                            break
                        if asset_count + len(note.assets) > MAX_ASSETS:
                            reliable = False
                            limit_hit = True
                            break
                        asset_count += len(note.assets)
                        notes[note.note_id] = note
                        if note.note_id in anchors:
                            hit = hit or note.note_id
                        elif profiles and hit is None and _anchor_title_matches(profiles, note):
                            title_hit = True
                    if limit_hit:
                        reason = "first_sync_limit" if initial_limit is not None else "page_limit"
                        break
                    if page.end_of_feed:
                        complete = reliable
                        reason = "end_of_feed" if reliable else "cursor_error"
                        break
                    if not page.notes:
                        reason = "cursor_error"
                        break
                    if initial_limit is not None and len(notes) >= initial_limit:
                        reason = "first_sync_limit"
                        break
                    if hit and reliable and len(notes) >= source["anchor_count"]:
                        reason = "anchor_hit"
                        break
                    if profiles and reliable and hit is None and title_hit and check_base is not None and len(notes) >= check_base:
                        reason = "anchor_title_hit"
                        break
                    if stop_limit is not None and hit is None and len(notes) >= stop_limit:
                        reason = "anchor_miss_limit"
                        break
                    if page_index == max_pages - 1:
                        reason = "page_limit"
            except Exception as exc:
                # Transport exceptions must never leak headers, signed URLs or response bodies.
                category = getattr(exc, 'category', '')
                reason = {'NEEDS_REAUTH': 'needs_reauth', 'LOGIN_REQUIRED': 'needs_reauth',
                          'NEEDS_MANUAL_VERIFICATION': 'needs_manual_verification',
                          'RATE_LIMITED': 'rate_limited', 'STRUCTURE_CHANGED': 'structure_changed',
                          'CURSOR_ERROR': 'cursor_error'}.get(category, 'network_error')
                if cancelled and cancelled():
                    reason = 'cancelled'
                complete = False
            finally:
                close = getattr(iterator, "close", None)
                if close:
                    try:
                        close()
                    except Exception:
                        reason, complete = "network_error", False
            finished = now_iso()
            manifest = XhsManifest(owner_key, str(uuid4()), started, finished, complete, reason,
                                   tuple(notes.values()), hit, source["anchor_count"])
            fingerprint = hashlib.sha256(json.dumps(asdict(manifest), sort_keys=True).encode()).hexdigest()
            # Reliable limited initialization also saves the leading anchors.
            # It remains a partial scan and must never reconcile removed likes.
            checkpoint = reliable and reason in {
                "end_of_feed", "anchor_hit", "first_sync_limit", "anchor_miss_limit", "anchor_title_hit",
            }
            return self._commit(source, manifest, manifest.notes, reason, complete, checkpoint, fingerprint,
                                first_fetch_done=checkpoint, emit_task_history=emit_task_history)

    def _commit(self, source: dict, manifest: XhsManifest, notes: tuple[XhsNote, ...], reason: str,
                complete: bool, checkpoint: bool, fingerprint: str, *, first_fetch_done: bool, emit_task_history: bool = True) -> dict:
        source_id = source["source_id"]
        now = now_iso()
        with self.db.connect() as conn:
            conn.execute("begin immediate")
            current = self.get_source(source_id, conn)
            existing = conn.execute("select * from xhs_sync_runs where scan_id = ?", (manifest.scan_id,)).fetchone()
            if existing:
                if existing["source_id"] != source_id or existing["manifest_hash"] != fingerprint:
                    raise XhsConflict("scan_id 已被另一份清单使用")
                return {**dict(existing), "replayed": True}
            if not current["enabled"] or current["owner_key"] != manifest.owner_key:
                raise XhsConflict("来源已停用或账号不一致")
            if current["checkpoint_version"] != source["checkpoint_version"]:
                raise XhsConflict("扫描检查点已更新，旧任务不可覆盖")
            latest_row = conn.execute(
                """select finished_at from xhs_sync_runs where source_id = ? and checkpoint_committed = 1
                   order by rowid desc limit 1""", (source_id,),
            ).fetchone()
            latest = latest_row[0] if latest_row else None
            # Untrusted manifest timestamps cannot become authoritative scan boundaries.
            if latest:
                from datetime import datetime
                if datetime.fromisoformat(manifest.completed_at) < datetime.fromisoformat(latest):
                    raise XhsConflict("清单早于已导入扫描，请重新导出")
            new_count = 0
            for note in notes:
                row = conn.execute("select * from xhs_like_state where source_id = ? and note_id = ?", (source_id, note.note_id)).fetchone()
                parsed = XhsLikesAdapter.to_post(note)
                if row:
                    post_id = row["site_post_id"]
                    existing_post = conn.execute("select title from site_posts where id = ?", (post_id,)).fetchone()
                    title = parsed.title
                    if title == note.note_id and existing_post and existing_post["title"] and existing_post["title"] != note.note_id:
                        title = existing_post["title"]
                    conn.execute("update site_posts set title = ?, pub_date = coalesce(?, pub_date), updated_at = ? where id = ?",
                                 (title, parsed.pub_date, now, post_id))
                else:
                    new_count += 1
                    cursor = conn.execute(
                        """insert into site_posts(source_id, url, title, slug, pub_date, status, created_at, updated_at)
                           values (?, ?, ?, ?, ?, 'discovered', ?, ?)""",
                        (source_id, parsed.url, parsed.title, note.note_id, parsed.pub_date, now, now),
                    )
                    post_id = int(cursor.lastrowid)
                conn.execute('''insert into xhs_note_downloads(post_id, status, last_scan_id, updated_at)
                    values (?, 'pending', ?, ?) on conflict(post_id) do update set last_scan_id = excluded.last_scan_id''',
                             (post_id, manifest.scan_id, now))
                conn.execute(
                    """insert into xhs_like_state(source_id, note_id, site_post_id, author_id, author_name, note_type,
                           first_seen_at, last_seen_at, is_currently_liked, last_full_snapshot_id)
                       values (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                       on conflict(source_id, note_id) do update set last_seen_at = excluded.last_seen_at,
                           author_id = case when excluded.author_id != '' then excluded.author_id else xhs_like_state.author_id end,
                           author_name = case when excluded.author_name != '' then excluded.author_name else xhs_like_state.author_name end,
                           note_type = excluded.note_type, is_currently_liked = 1,
                           last_full_snapshot_id = coalesce(excluded.last_full_snapshot_id, xhs_like_state.last_full_snapshot_id)""",
                    (source_id, note.note_id, post_id, note.author_id, note.author_name, note.type, now, now, manifest.scan_id if complete else None),
                )
                for asset in note.assets:
                    stable_key = f"urn:xhs:{source_id}:{note.note_id}:{asset.ordinal}:{asset.kind}"
                    conn.execute(
                        """insert into site_assets(post_id, url, media_type, filename, status, created_at, updated_at)
                           values (?, ?, ?, ?, 'needs_resolution', ?, ?) on conflict(post_id, url) do nothing""",
                        (post_id, stable_key, asset.kind, f"{asset.ordinal:03d}-{asset.kind}", now, now),
                    )
                if note.type == 'video':
                    conn.execute("""update xhs_note_downloads set status='pending',error_category=null
                        where post_id=? and status='ready' and not exists
                        (select 1 from site_assets where post_id=? and media_type='video' and status='ready')""",
                                 (post_id, post_id))
                if any(asset.kind == 'livephoto' for asset in note.assets):
                    conn.execute("""update xhs_note_downloads set status='pending',error_category=null
                        where post_id=? and status='ready' and exists
                        (select 1 from site_assets where post_id=? and media_type='livephoto' and status!='ready')""",
                                 (post_id, post_id))
                conn.execute("update site_posts set asset_count = (select count(*) from site_assets where post_id = ?) where id = ?", (post_id, post_id))
            if complete:
                conn.execute(
                    """update xhs_like_state set is_currently_liked = 0 where source_id = ?
                       and (last_full_snapshot_id is null or last_full_snapshot_id != ?)""", (source_id, manifest.scan_id),
                )
            result = ("SUCCESS_FULL" if complete else "PARTIAL" if reason == "first_sync_limit"
                      else "SUCCESS_INCREMENTAL" if checkpoint else "IMPORTED" if reason == "manual_import" else "PARTIAL")
            conn.execute(
                """insert into xhs_sync_runs(scan_id, source_id, manifest_hash, started_at, finished_at, result,
                       complete_snapshot, seen_count, new_count, stop_reason, anchor_hit_note_id,
                       anchor_count_at_start, initial_fetch_limit_at_start, checkpoint_committed, checkpoint_version, error_category)
                   values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (manifest.scan_id, source_id, fingerprint, manifest.started_at, manifest.completed_at, result, int(complete),
                 len(notes), new_count, reason, manifest.anchor_hit_note_id if checkpoint else None, source["anchor_count"],
                 source["initial_fetch_limit"] if not source["initial_fetch_completed"] or not source["last_checkpoint_scan_id"] else None,
                 int(checkpoint), source["checkpoint_version"] + int(checkpoint),
                 reason if reason in {"network_error", "cursor_error", "structure_changed", "cancelled", "time_limit", 'needs_reauth', 'needs_manual_verification', 'rate_limited'} else None),
            )
            if checkpoint:
                for rank, note in enumerate(notes[:source["anchor_count"]]):
                    conn.execute(
                        """insert into xhs_anchor_candidates(source_id, scan_id, rank, note_id,
                               title_prefix, author_id, note_type, asset_count)
                           values (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (source_id, manifest.scan_id, rank, note.note_id,
                         note.title[:ANCHOR_TITLE_PREFIX_CHARS], note.author_id, note.type, len(note.assets)),
                    )
                conn.execute(
                    """update xhs_source_accounts set last_checkpoint_scan_id = ?, last_checkpoint_at = ?,
                       checkpoint_version = checkpoint_version + 1 where source_id = ? and checkpoint_version = ?""",
                    (manifest.scan_id, now, source_id, source["checkpoint_version"]),
                )
            if first_fetch_done:
                conn.execute("update xhs_source_accounts set initial_fetch_completed = 1 where source_id = ?", (source_id,))
            summary = dict(conn.execute("select * from xhs_sync_runs where scan_id = ?", (manifest.scan_id,)).fetchone())
            if emit_task_history:
                conn.execute(
                    """insert into task_runs(task_type, status, message, details_json, created_at, finished_at)
                       values ('xhs-import', ?, ?, ?, ?, ?)""",
                    ("success" if result != "PARTIAL" else "partial", f"小红书清单：读取 {len(notes)} 条，新增 {new_count} 条",
                     dumps_json(summary), now, now),
                )
        self.db.refresh_sidebar_count_cache(["tasks"])
        return summary
