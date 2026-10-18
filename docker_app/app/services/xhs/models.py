from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import re
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_NOTES = 10000
MAX_ASSETS = 50000
ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
SENSITIVE = re.compile(
    r"cookie|authorization|xsec[_-]?token|set-cookie|[?&](?:sign|token|signature)=|"
    r"\b(?:web_session|a1|access_token|refresh_token)\s*[=:]", re.IGNORECASE
)


class XhsValidationError(ValueError):
    pass


def fields(value: Any, allowed: set[str], required: set[str]) -> dict:
    if not isinstance(value, dict) or not required <= value.keys() or value.keys() - allowed:
        raise XhsValidationError("JSON 字段缺失或包含不支持的字段；请仅导入净化后的清单")
    return value


def text(value: Any, maximum: int = 128, *, empty: bool = False) -> str:
    if (not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip())
            or SENSITIVE.search(value) or any(ord(ch) < 32 for ch in value)):
        raise XhsValidationError("字段格式无效或包含认证信息")
    return value


def identifier(value: Any, minimum: int = 1) -> str:
    value = text(value)
    if len(value) < minimum or not ID_PATTERN.fullmatch(value):
        raise XhsValidationError("标识符格式无效")
    return value


def integer(value: Any, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise XhsValidationError(f"请输入 {low}–{high} 之间的整数")
    return value


def timestamp(value: Any) -> str:
    value = text(value, 64)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise XhsValidationError("时间必须是带时区的 ISO 8601 格式") from None
    if parsed.tzinfo is None:
        raise XhsValidationError("时间必须包含时区")
    return parsed.isoformat()


def note_url(value: Any, note_id: str) -> str:
    value = text(value, 512)
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme == "https" and parsed.netloc == "www.xiaohongshu.com"
                 and not parsed.query and not parsed.fragment
                 and parsed.path in {f"/explore/{note_id}", f"/discovery/item/{note_id}"})
    except ValueError:
        valid = False
    if not valid:
        raise XhsValidationError("仅允许与笔记 ID 一致、无查询参数的小红书 HTTPS 笔记链接")
    return f"https://www.xiaohongshu.com/explore/{note_id}"


@dataclass(frozen=True)
class XhsAsset:
    ordinal: int
    kind: str

    @classmethod
    def parse(cls, value: Any) -> XhsAsset:
        value = fields(value, {"ordinal", "kind", "candidate_ref"}, {"ordinal", "kind"})
        ordinal = integer(value["ordinal"], 0, 99)
        if not isinstance(value["kind"], str) or value["kind"] not in {"image", "video", "livephoto"}:
            raise XhsValidationError("不支持的媒体类型")
        if "candidate_ref" in value:
            identifier(value["candidate_ref"])
        # Ephemeral worker references are deliberately discarded before persistence.
        return cls(ordinal, value["kind"])


@dataclass(frozen=True)
class XhsNote:
    note_id: str
    url: str
    title: str
    author_id: str
    type: str
    published_at: str | None
    observed_at: str
    assets: tuple[XhsAsset, ...]

    @classmethod
    def parse(cls, value: Any) -> XhsNote:
        value = fields(value, {"note_id", "url", "title", "author_id", "type", "published_at", "observed_at", "assets"},
                       {"note_id", "url", "observed_at"})
        note_id = identifier(value["note_id"])
        kind = value.get("type", "image")
        if not isinstance(kind, str) or kind not in {"image", "video", "livephoto"}:
            raise XhsValidationError("不支持的笔记类型")
        raw_assets = value.get("assets", [])
        if not isinstance(raw_assets, list) or len(raw_assets) > 100:
            raise XhsValidationError("每条笔记最多 100 个媒体项")
        assets = tuple(sorted((XhsAsset.parse(item) for item in raw_assets), key=lambda a: (a.ordinal, a.kind)))
        if len({(item.ordinal, item.kind) for item in assets}) != len(assets):
            raise XhsValidationError("媒体序号与类型不可重复")
        return cls(note_id, note_url(value["url"], note_id), text(value.get("title", ""), 1000, empty=True),
                   identifier(value["author_id"]) if value.get("author_id") else "", kind,
                   timestamp(value["published_at"]) if value.get("published_at") is not None else None,
                   timestamp(value["observed_at"]), assets)


@dataclass(frozen=True)
class XhsManifest:
    owner_key: str
    scan_id: str
    started_at: str
    completed_at: str
    complete_snapshot: bool
    stop_reason: str
    notes: tuple[XhsNote, ...]
    anchor_hit_note_id: str | None = None
    anchor_count_at_start: int | None = None

    @classmethod
    def parse(cls, raw: bytes) -> XhsManifest:
        if len(raw) > MAX_MANIFEST_BYTES:
            raise XhsValidationError("清单不能超过 4 MiB")
        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise XhsValidationError("JSON 不可包含重复字段")
                result[key] = value
            return result
        try:
            value = json.loads(raw, object_pairs_hook=unique_object)
        except (ValueError, UnicodeError, RecursionError):
            raise XhsValidationError("清单 JSON 无效或嵌套过深") from None
        allowed = {"schema_version", "source_kind", "owner_key", "scan_id", "started_at", "completed_at",
                   "complete_snapshot", "stop_reason", "anchor_hit_note_id", "anchor_count_at_start", "notes"}
        value = fields(value, allowed, allowed - {"anchor_hit_note_id", "anchor_count_at_start"})
        if type(value["schema_version"]) is not int or value["schema_version"] != 1 or value["source_kind"] != "xhs_my_likes":
            raise XhsValidationError("仅支持 xhs_my_likes Manifest v1")
        try:
            scan_id = str(UUID(text(value["scan_id"], 36)))
        except ValueError:
            raise XhsValidationError("scan_id 必须是 UUID") from None
        started, completed = timestamp(value["started_at"]), timestamp(value["completed_at"])
        if datetime.fromisoformat(started) > datetime.fromisoformat(completed):
            raise XhsValidationError("扫描结束时间不能早于开始时间")
        complete = value["complete_snapshot"]
        reason = text(value["stop_reason"], 64)
        if type(complete) is not bool or reason not in {
            "anchor_hit", "end_of_feed", "manual_import", "page_limit", "time_limit", "first_sync_limit",
            "cancelled", "cursor_error", "structure_changed", "needs_reauth", "needs_manual_verification", "rate_limited", "network_error",
        } or complete != (reason == "end_of_feed"):
            raise XhsValidationError("完整快照只能使用 end_of_feed 停止原因")
        raw_notes = value["notes"]
        if not isinstance(raw_notes, list) or len(raw_notes) > MAX_NOTES:
            raise XhsValidationError("清单最多包含 10000 条笔记")
        notes = {}
        asset_count = 0
        for raw_note in raw_notes:
            note = XhsNote.parse(raw_note)
            if not datetime.fromisoformat(started) <= datetime.fromisoformat(note.observed_at) <= datetime.fromisoformat(completed):
                raise XhsValidationError("观察时间必须位于扫描时间范围内")
            if note.note_id in notes and notes[note.note_id] != note:
                raise XhsValidationError("同一清单中的重复笔记内容冲突")
            notes[note.note_id] = note
            asset_count += len(note.assets)
            if asset_count > MAX_ASSETS:
                raise XhsValidationError("清单媒体项过多")
        hit = value.get("anchor_hit_note_id")
        if hit is not None:
            hit = identifier(hit)
        count = value.get("anchor_count_at_start")
        if count is not None:
            count = integer(count, 1, 20)
        if reason == "anchor_hit" and (hit not in notes or count is None):
            raise XhsValidationError("锚点停止必须包含命中笔记和启动时锚点数")
        return cls(identifier(value["owner_key"], 8), scan_id, started, completed, complete, reason,
                   tuple(notes.values()), hit, count)
