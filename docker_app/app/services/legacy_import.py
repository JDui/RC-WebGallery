from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from app.db import Database
from app.services.media_indexer import MediaIndexer
from app.services.storage import StorageService
from app.services.utils import build_folder_name, extract_chinese_prefix, parse_legacy_title, format_pub_time


STATE_BEGIN = "<!-- DOWNLOAD_STATE_BEGIN -->"
STATE_END = "<!-- DOWNLOAD_STATE_END -->"


@dataclass
class LegacyRecord:
    top_dynamic_id: str
    source_dynamic_id: str
    pub_ts: int
    folder_name: str
    title_hint: str


class LegacyImporter:
    def __init__(self, db: Database, storage: StorageService, indexer: MediaIndexer, repo_root: Path) -> None:
        self.db = db
        self.storage = storage
        self.indexer = indexer
        self.repo_root = repo_root

    def import_if_needed(self) -> dict[str, int]:
        if self.db.list_folders():
            return {"folders": 0, "moved": 0}

        image_records = self._load_records(self.repo_root / "downloads.md")
        live_records = self._load_records(self.repo_root / "LivePhoto" / "downloads_livephoto.md")
        legacy_image_root = self.repo_root / "downloaded"
        legacy_live_root = self.repo_root / "LivePhoto" / "downloaded"

        all_keys = set(image_records) | set(live_records)
        if not all_keys:
            return {"folders": 0, "moved": 0}

        used_names = set()
        moved = 0
        for key in sorted(all_keys):
            image_record = image_records.get(key)
            live_record = live_records.get(key)
            reference = image_record or live_record
            assert reference is not None
            title_hint = reference.title_hint or reference.folder_name
            folder_name = build_folder_name(reference.pub_ts, title_hint, used_names)
            self.storage.prepare_folder(folder_name, f"bilibili-{self.db.get_settings()['host_mid']}", format_pub_time(reference.pub_ts))

            if image_record:
                moved += self._move_folder(
                    legacy_image_root / image_record.folder_name,
                    self.storage.image_folder(folder_name),
                )
            if live_record:
                moved += self._move_folder(
                    legacy_live_root / live_record.folder_name,
                    self.storage.livephoto_folder(folder_name),
                )

            self.indexer.index_folder(
                folder_name=folder_name,
                pub_ts=reference.pub_ts,
                title=title_hint,
                text_prefix=extract_chinese_prefix(title_hint),
                top_dynamic_id=reference.top_dynamic_id,
                source_dynamic_id=reference.source_dynamic_id,
                generate_derivatives=False,
            )

        return {"folders": len(all_keys), "moved": moved}

    def _load_records(self, md_path: Path) -> dict[tuple[str, str], LegacyRecord]:
        if not md_path.exists():
            return {}
        text = md_path.read_text(encoding="utf-8")
        start = text.find(STATE_BEGIN)
        end = text.find(STATE_END)
        if start == -1 or end == -1 or end <= start:
            return {}
        payload = text[start + len(STATE_BEGIN):end].strip()
        if not payload:
            return {}
        items = json.loads(payload)
        output: dict[tuple[str, str], LegacyRecord] = {}
        for item in items:
            old_folder = str(item.get("folder_name") or "")
            title_hint = parse_legacy_title(old_folder)
            key = (str(item.get("top_dynamic_id")), str(item.get("source_dynamic_id")))
            output[key] = LegacyRecord(
                top_dynamic_id=key[0],
                source_dynamic_id=key[1],
                pub_ts=int(item.get("pub_ts") or self._folder_ts_guess(old_folder)),
                folder_name=old_folder,
                title_hint=title_hint,
            )
        return output

    def _folder_ts_guess(self, folder_name: str) -> int:
        match = re.match(r"^(\d{8})(?:_(\d{6}))?", folder_name)
        if not match:
            return 0
        date_part = match.group(1)
        time_part = match.group(2) or "000000"
        try:
            from datetime import datetime
            from zoneinfo import ZoneInfo

            dt = datetime.strptime(f"{date_part}{time_part}", "%Y%m%d%H%M%S")
            return int(dt.replace(tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())
        except ValueError:
            return 0

    def _move_folder(self, source: Path, target: Path) -> int:
        if not source.exists():
            return 0
        if target.exists():
            target.mkdir(parents=True, exist_ok=True)
            for child in source.iterdir():
                destination = target / child.name
                if destination.exists():
                    continue
                self._transfer_path(child, destination)
            self._remove_dir_if_empty(source)
            return 1
        target.parent.mkdir(parents=True, exist_ok=True)
        self._transfer_path(source, target)
        return 1

    def _transfer_path(self, source: Path, target: Path) -> None:
        try:
            shutil.move(str(source), str(target))
            return
        except OSError:
            pass

        if source.is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)

    def _remove_dir_if_empty(self, path: Path) -> None:
        try:
            path.rmdir()
        except OSError:
            return
