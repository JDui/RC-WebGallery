"""Crash, scale, and path-safety stress checks for the data-layout upgrade.

Every database and file tree created here lives below TemporaryDirectory. The
suite has no network access requirements and never opens the user's real store.
Run in Linux with:
    PYTHONPATH=docker_app python -m unittest docker_app.scripts.test_data_upgrade_stress
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import replace
import asyncio
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.config import load_config
from app.db import Database
from app.services.data_upgrade import DataUpgrade, DataUpgradeMiddleware, storage_instance_lock
from app.services.database_layout import MODULES, split_modules
from app.services.duplicates import DuplicateService
from app.services.livephoto import LivePhotoService
from app.services.livephoto_recovery import LivePhotoError, LivePhotoRecovery
from app.services.storage import StorageService
from app.services.utils import dumps_json


class UpgradeStressCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rc-layout-stress-")
        root = Path(self.temp.name)
        config = replace(
            load_config(), storage_root=root, config_dir=root / "config",
            data_dir=root / "data", images_dir=root / "data/images",
            livephoto_dir=root / "data/livephoto", database_path=root / "config/app.db",
        )
        self.storage = StorageService(config)
        self.storage.ensure()
        self.db = Database(config.database_path)
        self.db.init()
        self.storage.db = self.db
        self._make_manager()
        self.upgrade = DataUpgrade(self.db, self.storage, None, self.manager)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def _make_manager(self):
        self.lock = threading.Lock()
        self.manager = SimpleNamespace(
            _queue_lock=threading.Lock(), _queue=[],
            _acquire=lambda *args: self.lock.acquire(blocking=False),
            _release=lambda: self.lock.release() if self.lock.locked() else None,
            _run_next_queued_task=lambda: None,
        )

    def _seed_folder(self, name, *, pub_ts=0, pub_time="", uid="u-42", favorite=False, pairs=2, created_at="2026-10-01T00:00:00"):
        folder = {
            "folder_name": name, "title": f"needle 标题 {name}", "text_prefix": "needle text",
            "pub_ts": pub_ts, "pub_time": pub_time, "top_dynamic_id": f"top:{name}",
            "source_dynamic_id": f"src:{name}", "subscription_uid": uid,
            "subscription_name": f"订阅 {uid}", "has_images": True,
            "has_livephoto": pairs > 0, "is_favorite": favorite,
            "created_at": created_at, "metadata": {"unicode": "日本語🙂"},
        }
        self.db.upsert_folder(folder)
        self.db.replace_gallery_index(
            name, {**folder, "review_status": "approved", "image_count": pairs,
                   "livephoto_count": pairs, "asset_count": pairs * 2,
                   "year_key": "2026" if pub_time else "未知",
                   "month_key": pub_time[5:7] if len(pub_time) >= 7 else "",
                   "preview_assets_json": dumps_json([{"path": f"data/images/{name}/p.jpg"}])},
            [dict(item_key=f"{name}:{i}", pair_index=i, pub_ts=pub_ts,
                  pub_time=pub_time, subscription_uid=uid,
                  subscription_name=f"订阅 {uid}", is_favorite=favorite,
                  has_image=True, has_livephoto=True,
                  image_json=dumps_json({"path": f"data/images/{name}/p.jpg"}),
                  livephoto_json=dumps_json({"path": f"data/livephoto/{name}/p.mov"}),
                  year_key="2026" if pub_time else "未知",
                  month_key=pub_time[5:7] if len(pub_time) >= 7 else "") for i in range(pairs)],
        )
        return folder

    def _reopen(self):
        self.db.close()
        self.db = Database(self.db.db_path)
        self.db.init()
        self.storage.db = self.db
        self.upgrade = DataUpgrade(self.db, self.storage, None, self.manager)

    def _seed_move_fixture(self, name="含中文_日本語🙂 [相同名]", *, legacy_date=""):
        folder = self._seed_folder(name, pub_ts=0, pub_time=legacy_date, uid="", favorite=True, pairs=1)
        old_image_dir = self.storage.config.images_dir / name
        old_live_dir = self.storage.config.livephoto_dir / name
        image = old_image_dir / "同名 文件.jpg"
        emoji_site_file = old_image_dir / "😀site-asset.bin"
        video = old_live_dir / "同名 文件.mov"
        image.parent.mkdir(parents=True)
        video.parent.mkdir(parents=True)
        image.write_bytes(b"original image bytes\x00\xff")
        emoji_site_file.write_bytes(b"emoji-prefixed site asset bytes\x00\x01")
        video.write_bytes(b"live photo video bytes\x00\xfe")
        image_thumb = old_image_dir / ".thumbs" / "standard" / "同名 文件.webp"
        image_small = old_image_dir / ".thumbs" / "small" / "同名 文件.webp"
        image_tiny = old_image_dir / ".thumbs" / "tiny" / "同名 文件.webp"
        cover = old_live_dir / ".source_covers" / "同名 文件.jpg"
        reverse = old_live_dir / "reverse" / "同名 文件.mp4"
        for path, data in ((image_thumb, b"thumb"), (image_small, b"small"), (image_tiny, b"tiny"),
                           (cover, b"source cover"), (reverse, b"reverse video")):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        old_image_rel = self.storage.relative_to_storage(image)
        old_video_rel = self.storage.relative_to_storage(video)
        path_fields = {
            "filename": image.name, "rel_path": old_image_rel,
            "thumb_rel_path": self.storage.relative_to_storage(image_thumb),
            "small_thumb_rel_path": self.storage.relative_to_storage(image_small),
            "tiny_thumb_rel_path": self.storage.relative_to_storage(image_tiny),
            "cover_rel_path": self.storage.relative_to_storage(cover),
            "reverse_rel_path": self.storage.relative_to_storage(reverse),
            "pair_index": 1,
            "metadata": {"cached_path": old_image_rel, "emoji": "🙂"},
        }
        self.db.replace_folder_assets(name, "image", [path_fields])
        self.db.replace_folder_assets(name, "livephoto", [{
            "filename": video.name, "rel_path": old_video_rel, "pair_index": 1,
            "metadata": {"motion_path": old_video_rel},
        }])
        with self.db.connect() as conn:
            conn.execute(
                "insert into livephoto_pairs(folder_name,pair_index,content_id,image_path,video_path,status,auto_pair,transaction_json,updated_at) values(?,?,?,?,?,?,?,?,?)",
                (name, 1, "stable-content-id🙂", old_image_rel, old_video_rel,
                 "confirmed", 1, dumps_json({"image": old_image_rel, "video": old_video_rel}), "today"),
            )
        self.db.replace_gallery_index(name, {**folder, "review_status": "approved",
            "preview_assets_json": dumps_json([{"path": old_image_rel}])}, [{
                "item_key": f"{name}:1", "pair_index": 1, "pub_ts": 0,
                "pub_time": legacy_date, "subscription_uid": "", "has_image": True,
                "has_livephoto": True, "image_json": dumps_json({"path": old_image_rel}),
                "livephoto_json": dumps_json({"path": old_video_rel}),
            }])
        self.db.upsert_trash_item({**folder, "metadata": {"old": old_image_rel}}, [{
            "folder_name": name, "media_type": "image", "filename": image.name,
            "rel_path": old_image_rel, "tiny_thumb_rel_path": path_fields["tiny_thumb_rel_path"],
        }])
        source = self.db.create_site_source({
            "name": "符号源🙂", "slug": "符号源", "entry_url": "https://example.invalid/feed",
            "icon_url": self.storage.storage_url(old_image_rel),
            "icon_tiny_url": self.storage.storage_url(path_fields["tiny_thumb_rel_path"]),
        })
        with self.db.connect() as conn:
            conn.execute("update site_sources set icon_tiny_url=? where id=?",
                         (self.storage.storage_url(path_fields["tiny_thumb_rel_path"]), source["id"]))
        post = self.db.upsert_site_post(source["id"], {"title": "site pointer", "url": "https://example.invalid/post"})
        site_asset = self.db.upsert_site_asset(post["id"], {"url": "https://example.invalid/image", "filename": image.name, "media_type": "image"})
        self.db.set_site_asset_result(site_asset["id"], "ready", old_image_rel)
        emoji_site_asset = self.db.upsert_site_asset(post["id"], {
            "url": "https://example.invalid/emoji-site-asset", "filename": emoji_site_file.name,
            "media_type": "image",
        })
        self.db.set_site_asset_result(emoji_site_asset["id"], "ready", self.storage.relative_to_storage(emoji_site_file))
        self.db.upsert_subscription("uid-42", "avatar ref", avatar_url=self.storage.storage_url(old_image_rel),
                                    avatar_tiny_url=self.storage.storage_url(path_fields["tiny_thumb_rel_path"]))
        self.db.upsert_subscription("external-avatar", "external", avatar_url="https://cdn.example.invalid/avatar.jpg",
                                    avatar_tiny_url="https://cdn.example.invalid/avatar-tiny.webp")
        site_file = self.storage.config.data_dir / "sites" / "保留站点" / "2020" / "原目录" / "留存🙂.bin"
        site_file.parent.mkdir(parents=True)
        site_file.write_bytes(b"site bytes must stay exactly here\x00\x01")
        split_modules(self.db, lambda _: None)
        self._reopen()
        with self.db.connect() as conn:
            conn.execute("create table if not exists storage_moves (old_dir text primary key, new_dir text not null, folder_name text not null, media_type text not null, done integer not null default 0)")
        target = self.storage.layout_folder("images", name, "bilibili-unknown", legacy_date or name)
        with self.db.connect() as conn:
            conn.execute("insert into storage_moves(old_dir,new_dir,folder_name,media_type,done) values(?,?,?,?,0)",
                         (self.storage.relative_to_storage(old_image_dir), self.storage.relative_to_storage(target), name, "images"))
        target_live = self.storage.layout_folder("livephoto", name, "bilibili-unknown", legacy_date or name)
        with self.db.connect() as conn:
            conn.execute("insert into storage_moves(old_dir,new_dir,folder_name,media_type,done) values(?,?,?,?,0)",
                         (self.storage.relative_to_storage(old_live_dir), self.storage.relative_to_storage(target_live), name, "livephoto"))
        return {
            "name": name, "image": image, "video": video, "image_rel": old_image_rel,
            "video_rel": old_video_rel, "image_target": target / image.name,
            "video_target": target_live / video.name, "site_file": site_file,
            "emoji_site_target": target / emoji_site_file.name,
            "source_id": source["id"], "post_id": post["id"],
        }

    def _assert_moved_references(self, fixture):
        name = fixture["name"]
        new_image = self.storage.relative_to_storage(fixture["image_target"])
        new_video = self.storage.relative_to_storage(fixture["video_target"])
        new_live_dir = self.storage.relative_to_storage(fixture["video_target"].parent)
        with self.db.connect() as conn:
            asset = dict(conn.execute("select * from assets where folder_name=? and media_type='image'", (name,)).fetchone())
            live = dict(conn.execute("select * from assets where folder_name=? and media_type='livephoto'", (name,)).fetchone())
            pair = dict(conn.execute("select * from livephoto_pairs where folder_name=?", (name,)).fetchone())
            folder_index = dict(conn.execute("select * from folder_index where folder_name=?", (name,)).fetchone())
            pair_index = dict(conn.execute("select * from pair_index where folder_name=?", (name,)).fetchone())
            trash = dict(conn.execute("select * from trash_items where folder_name=?", (name,)).fetchone())
            site_asset = dict(conn.execute("select * from site_assets where post_id=?", (fixture["post_id"],)).fetchone())
            emoji_site_asset = dict(conn.execute("select * from site_assets where post_id=? and filename like '😀%'", (fixture["post_id"],)).fetchone())
            move_count = conn.execute("select count(*) from storage_moves where folder_name=? and done=0", (name,)).fetchone()[0]
        for key in ("rel_path", "thumb_rel_path", "small_thumb_rel_path", "tiny_thumb_rel_path", "cover_rel_path", "reverse_rel_path", "metadata_json"):
            self.assertNotIn("data/images/" + name + "/", str(asset[key]))
        self.assertIn(new_image, asset["rel_path"])
        self.assertIn("data/images/", asset["thumb_rel_path"])
        self.assertIn("data/images/", asset["small_thumb_rel_path"])
        self.assertIn("data/images/", asset["tiny_thumb_rel_path"])
        self.assertIn(new_live_dir + "/.source_covers/", asset["cover_rel_path"])
        self.assertIn(new_live_dir + "/reverse/", asset["reverse_rel_path"])
        self.assertIn(new_video, live["rel_path"])
        self.assertEqual(pair["content_id"], "stable-content-id🙂")
        self.assertEqual((pair["status"], pair["auto_pair"]), ("confirmed", 1))
        self.assertIn(new_image, pair["transaction_json"])
        self.assertIn(new_video, pair["transaction_json"])
        self.assertIn(new_image, folder_index["preview_assets_json"])
        self.assertIn(new_image, pair_index["image_json"])
        self.assertIn(new_video, pair_index["livephoto_json"])
        self.assertIn(new_image, trash["assets_json"])
        self.assertEqual(site_asset["rel_path"], new_image)
        emoji_site_rel = self.storage.relative_to_storage(fixture["emoji_site_target"])
        self.assertEqual(emoji_site_asset["rel_path"], emoji_site_rel)
        self.assertEqual(self.db.get_subscription("external-avatar")["avatar_url"], "https://cdn.example.invalid/avatar.jpg")
        self.assertEqual(self.db.get_subscription("external-avatar")["avatar_tiny_url"], "https://cdn.example.invalid/avatar-tiny.webp")
        self.assertEqual(move_count, 0)
        self.assertTrue(fixture["image_target"].is_file())
        self.assertTrue(fixture["emoji_site_target"].is_file())
        self.assertEqual(fixture["emoji_site_target"].read_bytes(), b"emoji-prefixed site asset bytes\x00\x01")
        self.assertTrue(fixture["video_target"].is_file())
        self.assertEqual(fixture["image_target"].read_bytes(), b"original image bytes\x00\xff")
        self.assertEqual(fixture["video_target"].read_bytes(), b"live photo video bytes\x00\xfe")
        self.assertEqual(fixture["site_file"].read_bytes(), b"site bytes must stay exactly here\x00\x01")

    def test_split_failures_at_each_table_are_atomic_and_retryable(self):
        self._seed_folder("split-🙂", pub_ts=10, pub_time="2026-10-01", favorite=True, pairs=2)
        self.db.save_settings({"stress_marker": "保留設定"})
        self.db.upsert_subscription("split-uid", "保留訂閱")
        self.db.update_auth_state(cookie="synthetic-cookie", user_json='{"name":"synthetic"}')
        with self.db.connect() as conn:
            conn.execute("insert into livephoto_pairs(folder_name,pair_index,content_id,updated_at) values(?,?,?,?)",
                         ("split-🙂", 1, "split-content", "today"))
        self.db.set_gallery_page_cache("cache", "before", {"marker": "保留"})
        self.db.query_pair_index(page=1, page_size=1)
        self.db.close()
        base = self.db.db_path
        for failing_table in sum((list(tables) for tables in MODULES.values()), []):
            clone_dir = Path(self.temp.name) / ("clone-" + failing_table)
            clone_dir.mkdir()
            clone_path = clone_dir / "app.db"
            with closing(sqlite3.connect(base)) as src, closing(sqlite3.connect(clone_path)) as dst:
                src.backup(dst)
            clone = Database(clone_path)
            with self.assertRaisesRegex(RuntimeError, "injected split interruption"):
                split_modules(clone, lambda message, table=failing_table: (_ for _ in ()).throw(RuntimeError("injected split interruption")) if message.endswith(table) else None)
            self.assertFalse(clone.modules_enabled, failing_table)
            clone.init()
            self.assertEqual(clone.get_settings()["stress_marker"], "保留設定", failing_table)
            self.assertEqual(clone.get_auth_state()["cookie"], "synthetic-cookie", failing_table)
            self.assertEqual(clone.get_subscription("split-uid")["uname"], "保留訂閱", failing_table)
            with clone.connect() as conn:
                self.assertEqual(conn.execute("select content_id from livephoto_pairs where folder_name='split-🙂'").fetchone()[0], "split-content")
                self.assertEqual(conn.execute("select count(*) from folder_index where folder_name='split-🙂'").fetchone()[0], 1)
                self.assertEqual(conn.execute("select count(*) from pair_index where folder_name='split-🙂'").fetchone()[0], 2)
            split_modules(clone, lambda _: None)
            self.assertTrue(clone.modules_enabled, failing_table)
            self.assertEqual(clone.get_settings()["stress_marker"], "保留設定", failing_table)
            clone.close()

    def test_split_hard_process_exit_before_and_after_commit(self):
        self._seed_folder("crash-fold", pub_ts=42, pub_time="2026-10-02", favorite=True, pairs=2)
        self.db.save_settings({"crash_marker": "still here"})
        self.db.upsert_subscription("crash-uid", "keep me")
        with self.db.connect() as conn:
            conn.execute("insert into livephoto_pairs(folder_name,pair_index,content_id,updated_at) values(?,?,?,?)",
                         ("crash-fold", 1, "hard-exit-pair", "today"))
        self.db.close()
        child_code = textwrap.dedent("""
            import os, sys
            from pathlib import Path
            from app.db import Database
            from app.services.database_layout import split_modules
            db = Database(Path(sys.argv[1]))
            mode = sys.argv[2]
            def progress(message):
                if mode == 'before' and message.endswith('pair_index'):
                    os._exit(73)
            split_modules(db, progress)
            os._exit(74)
        """)
        for mode, code in (("before", 73), ("after", 74)):
            target_dir = Path(self.temp.name) / ("process-" + mode)
            target_dir.mkdir()
            target = target_dir / "app.db"
            with closing(sqlite3.connect(self.db.db_path)) as src, closing(sqlite3.connect(target)) as dst:
                src.backup(dst)
            proc = subprocess.run([sys.executable, "-c", child_code, str(target), mode], capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, code, (mode, proc.stdout, proc.stderr))
            recovered = Database(target)
            recovered.init()
            self.assertEqual(recovered.get_settings()["crash_marker"], "still here", mode)
            self.assertEqual(recovered.get_subscription("crash-uid")["uname"], "keep me", mode)
            with recovered.connect() as conn:
                self.assertEqual(conn.execute("select content_id from livephoto_pairs where folder_name='crash-fold'").fetchone()[0], "hard-exit-pair")
                self.assertEqual(conn.execute("select count(*) from pair_index where folder_name='crash-fold'").fetchone()[0], 2)
            self.assertEqual(recovered.modules_enabled, mode == "after")
            split_modules(recovered, lambda _: None)
            self.assertTrue(recovered.modules_enabled)
            recovered.close()

    def test_reference_transaction_aborts_after_rename_then_resumes_without_loss(self):
        fixture = self._seed_move_fixture()
        with self.db.connect() as conn:
            conn.execute("create trigger inject_reference_abort before update of image_path on livephoto_pairs begin select raise(abort,'injected reference failure'); end")
        move = {"old_dir": self.storage.relative_to_storage(self.storage.config.images_dir / fixture["name"]),
                "new_dir": self.storage.relative_to_storage(fixture["image_target"].parent),
                "folder_name": fixture["name"], "media_type": "images"}
        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected reference failure"):
            self.upgrade._move(move)
        self.assertFalse((self.storage.config.images_dir / fixture["name"]).exists())
        self.assertTrue(fixture["image_target"].parent.is_dir())
        with self.db.connect() as conn:
            current = conn.execute("select rel_path from assets where folder_name=? and media_type='image'", (fixture["name"],)).fetchone()[0]
            done = conn.execute("select done from storage_moves where old_dir=?", (move["old_dir"],)).fetchone()[0]
            conn.execute("drop trigger inject_reference_abort")
        self.assertEqual(current, fixture["image_rel"])
        self.assertEqual(done, 0)
        self.upgrade.state.update(action="upgrade", status="interrupted")
        self.upgrade._run()
        self.assertEqual(self.upgrade.state["status"], "completed", self.upgrade.state)
        self._assert_moved_references(fixture)

    def test_hard_kill_inside_cross_database_reference_transaction_recovers(self):
        fixture = self._seed_move_fixture("kill-inside-txn🙂")
        old_dir = self.storage.config.images_dir / fixture["name"]
        new_dir = fixture["image_target"].parent
        old_rel = self.storage.relative_to_storage(old_dir)
        new_rel = self.storage.relative_to_storage(new_dir)
        move = {"old_dir": old_rel, "new_dir": new_rel, "folder_name": fixture["name"], "media_type": "images"}
        marker = Path(self.temp.name) / "writer-entered-trigger"
        release = Path(self.temp.name) / "writer-release"
        trigger_name = "pause_subscription_update"
        with self.db.connect() as conn:
            conn.execute(f"create trigger control.{trigger_name} after update of avatar_url on subscriptions begin select pause_here(); end")
        child_code = textwrap.dedent("""
            import json, sys, time
            from contextlib import contextmanager
            from dataclasses import replace
            from pathlib import Path
            from types import SimpleNamespace
            from app.config import load_config
            from app.db import Database
            from app.services.data_upgrade import DataUpgrade
            from app.services.storage import StorageService
            class PausingDB:
                def __init__(self, db, marker, release): self.db, self.marker, self.release = db, marker, release
                def __getattr__(self, key): return getattr(self.db, key)
                @contextmanager
                def connect(self):
                    with self.db.connect() as conn:
                        observed = {}
                        for schema in ('main', 'control', 'browse'):
                            observed[schema] = {
                                'journal_mode': conn.execute(f'pragma {schema}.journal_mode').fetchone()[0],
                                'synchronous': conn.execute(f'pragma {schema}.synchronous').fetchone()[0],
                            }
                        expected = {schema: {'journal_mode': 'delete', 'synchronous': 2}
                                    for schema in ('main', 'control', 'browse')}
                        if observed != expected:
                            raise AssertionError(f'maintenance pragmas differ: {observed!r}')
                        def pause():
                            self.marker.write_text(json.dumps(observed, sort_keys=True))
                            while not self.release.exists(): time.sleep(.01)
                        conn.create_function('pause_here', 0, pause)
                        yield conn
            app_db = Database(Path(sys.argv[1]))
            app_db.close()
            app_db.exclusive_maintenance = True
            root = Path(sys.argv[2])
            config = replace(load_config(), storage_root=root, config_dir=root/'config', data_dir=root/'data',
                images_dir=root/'data/images', livephoto_dir=root/'data/livephoto', database_path=root/'config/app.db')
            storage = StorageService(config)
            manager = SimpleNamespace()
            proxy = PausingDB(app_db, Path(sys.argv[3]), Path(sys.argv[4]))
            upgrade = DataUpgrade(proxy, storage, None, manager)
            upgrade._move({'old_dir':sys.argv[5], 'new_dir':sys.argv[6], 'folder_name':sys.argv[7], 'media_type':'images'})
        """)
        proc = subprocess.Popen([sys.executable, "-c", child_code, str(self.db.db_path), self.temp.name,
                                 str(marker), str(release), old_rel, new_rel, fixture["name"]],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 25
            while not marker.exists() and proc.poll() is None and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(marker.exists(), "child did not enter the trigger-backed write transaction")
            self.assertTrue(new_dir.is_dir(), "directory rename must happen before reference transaction")
            self.assertEqual(json.loads(marker.read_text(encoding="utf-8")), {
                schema: {"journal_mode": "delete", "synchronous": 2}
                for schema in ("main", "control", "browse")
            })
            proc.kill()
            proc.communicate(timeout=10)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate(timeout=10)
        self.assertFalse(old_dir.exists())
        self.assertTrue(new_dir.is_dir())
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("select rel_path from assets where folder_name=? and media_type='image'", (fixture["name"],)).fetchone()[0], fixture["image_rel"])
            self.assertEqual(conn.execute("select image_path from livephoto_pairs where folder_name=?", (fixture["name"],)).fetchone()[0], fixture["image_rel"])
            self.assertEqual(conn.execute("select rel_path from site_assets where post_id=? and filename not like '😀%'", (fixture["post_id"],)).fetchone()[0], fixture["image_rel"])
            self.assertIn(fixture["image_rel"], conn.execute("select preview_assets_json from folder_index where folder_name=?", (fixture["name"],)).fetchone()[0])
            pair_index = conn.execute("select image_json,livephoto_json from pair_index where folder_name=?", (fixture["name"],)).fetchone()
            self.assertIn(fixture["image_rel"], pair_index["image_json"])
            self.assertIn(fixture["video_rel"], pair_index["livephoto_json"])
            self.assertEqual(self.db.get_subscription("uid-42")["avatar_url"], self.storage.storage_url(fixture["image_rel"]))
            self.assertEqual(self.db.get_subscription("uid-42")["avatar_tiny_url"], self.storage.storage_url(
                self.storage.relative_to_storage(self.storage.config.images_dir / fixture["name"] / ".thumbs" / "tiny" / "同名 文件.webp")))
            self.assertEqual(conn.execute("select done from storage_moves where old_dir=?", (old_rel,)).fetchone()[0], 0)
            conn.execute(f"drop trigger {trigger_name}")
        self.upgrade.state.update(action="upgrade", status="interrupted")
        self.upgrade._run()
        self.assertEqual(self.upgrade.state["status"], "completed", self.upgrade.state)
        self._assert_moved_references(fixture)

    def test_hard_exit_before_rename_leaves_legacy_tree_retryable(self):
        fixture = self._seed_move_fixture("pre-rename-exit🙂")
        old_dir = self.storage.config.images_dir / fixture["name"]
        marker = Path(self.temp.name) / "worker-ready-before-rename"
        release = Path(self.temp.name) / "worker-release-before-rename"
        old_rel = self.storage.relative_to_storage(old_dir)
        child_code = textwrap.dedent("""
            import os, sys, time
            from dataclasses import replace
            from pathlib import Path
            from types import SimpleNamespace
            from app.config import load_config
            from app.db import Database
            from app.services.data_upgrade import DataUpgrade
            from app.services.storage import StorageService
            marker, release = Path(sys.argv[3]), Path(sys.argv[4])
            marker.write_text('ready')
            while not release.exists(): time.sleep(.01)
            app_db = Database(Path(sys.argv[1]))
            root = Path(sys.argv[2])
            config = replace(load_config(), storage_root=root, config_dir=root/'config', data_dir=root/'data',
                images_dir=root/'data/images', livephoto_dir=root/'data/livephoto', database_path=root/'config/app.db')
            upgrade = DataUpgrade(app_db, StorageService(config), None, SimpleNamespace())
            upgrade._move({'old_dir':sys.argv[5], 'new_dir':sys.argv[6], 'folder_name':sys.argv[7], 'media_type':'images'})
        """)
        proc = subprocess.Popen([sys.executable, "-c", child_code, str(self.db.db_path), self.temp.name,
                                 str(marker), str(release), old_rel,
                                 self.storage.relative_to_storage(fixture["image_target"].parent), fixture["name"]],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 20
            while not marker.exists() and proc.poll() is None and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(marker.exists(), "child did not reach pre-rename pause")
            proc.kill()
            proc.communicate(timeout=10)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate(timeout=10)
        self.assertTrue(old_dir.is_dir())
        self.assertTrue(fixture["image"].is_file())
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("select done from storage_moves where old_dir=?", (old_rel,)).fetchone()[0], 0)
            self.assertEqual(conn.execute("select rel_path from assets where folder_name=? and media_type='image'", (fixture["name"],)).fetchone()[0], fixture["image_rel"])
        self.upgrade.state.update(action="upgrade", status="interrupted")
        self.upgrade._run()
        self.assertEqual(self.upgrade.state["status"], "completed", self.upgrade.state)
        self._assert_moved_references(fixture)

    def test_full_upgrade_unicode_orphans_missing_media_and_backup_are_stable(self):
        fixture = self._seed_move_fixture()
        # Existing sites files are outside migration candidates and retain their bytes.
        orphan_name = "孤立-日本語🙂 [无数据库行]"
        orphan_paths = []
        for media_type in ("images", "livephoto"):
            directory = self.storage.config.data_dir / media_type / orphan_name
            directory.mkdir(parents=True)
            path = directory / "同名-😀.bin"
            path.write_bytes((media_type + "\x00orphan").encode())
            orphan_paths.append((media_type, path, self.storage.layout_folder(media_type, orphan_name, "bilibili-unknown", orphan_name) / path.name))
        bulk_orphans = []
        for i in range(128):
            name = f"大量目录-{i:04}-🙂"
            for media_type in ("images", "livephoto"):
                directory = self.storage.config.data_dir / media_type / name
                directory.mkdir(parents=True)
                path = directory / "同名.bin"
                payload = f"{media_type}:{i}:synthetic".encode()
                path.write_bytes(payload)
                target = self.storage.layout_folder(media_type, name, "bilibili-unknown", name) / path.name
                bulk_orphans.append((path, target, payload))
        missing_name = "missing-media"
        missing = self._seed_folder(missing_name, pub_ts=0, pub_time="", uid="", pairs=0)
        with self.db.connect() as conn:
            conn.execute("insert into assets(folder_name,media_type,pair_index,filename,rel_path,status,created_at,updated_at) values(?,?,?,?,?,?,?,?)",
                         (missing_name, "image", 1, "gone.jpg", f"data/images/{missing_name}/gone.jpg", "ready", "today", "today"))
        self.db.save_settings({"pre_upgrade_setting": "original"})
        self.upgrade.state.update(action="upgrade")
        self.upgrade._run()
        self.assertEqual(self.upgrade.state["status"], "completed", self.upgrade.state)
        self.assertEqual(self.upgrade.state["result"]["missing_assets"], 1)
        self._assert_moved_references(fixture)
        for media_type, old, target in orphan_paths:
            self.assertFalse(old.exists())
            self.assertEqual(target.read_bytes(), (media_type + "\x00orphan").encode())
        for old, target, payload in bulk_orphans:
            self.assertFalse(old.exists())
            self.assertEqual(target.read_bytes(), payload)
        avatar = self.db.get_subscription("uid-42")
        self.assertIn("bilibili-unknown", avatar["avatar_url"])
        source = self.db.get_site_source(fixture["source_id"])
        self.assertIn("bilibili-unknown", source["icon_url"])
        self.assertTrue((self.storage.config.config_dir / "before-data-layout-v2" / "app.db").is_file())
        with closing(sqlite3.connect(self.storage.config.config_dir / "before-data-layout-v2" / "app.db")) as backup:
            self.assertEqual(backup.execute("select version from data_layout where id=1").fetchone()[0], 1)
        with closing(sqlite3.connect(self.storage.config.config_dir / "before-data-layout-v2" / "control.db")) as backup_control:
            self.assertEqual(backup_control.execute("select value from settings where key='pre_upgrade_setting'").fetchone()[0], '"original"')
        self.db.save_settings({"pre_upgrade_setting": "changed-after-upgrade"})
        self.upgrade._run()
        self.assertEqual(self.upgrade.state["status"], "completed", self.upgrade.state)
        with closing(sqlite3.connect(self.storage.config.config_dir / "before-data-layout-v2" / "app.db")) as backup:
            self.assertEqual(backup.execute("select version from data_layout where id=1").fetchone()[0], 1)
        with closing(sqlite3.connect(self.storage.config.config_dir / "before-data-layout-v2" / "control.db")) as backup_control:
            self.assertEqual(backup_control.execute("select value from settings where key='pre_upgrade_setting'").fetchone()[0], '"original"')
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("select count(*) from storage_moves where done=0").fetchone()[0], 0)
            self.assertEqual(conn.execute("select version from data_layout where id=1").fetchone()[0], 2)

    def test_existing_source_and_target_conflict_preserves_both_trees(self):
        fixture = self._seed_move_fixture("source-target-conflict")
        old = self.storage.config.images_dir / fixture["name"]
        target = fixture["image_target"].parent
        target.mkdir(parents=True)
        (target / "preexisting.bin").write_bytes(b"target must survive")
        move = {"old_dir": self.storage.relative_to_storage(old), "new_dir": self.storage.relative_to_storage(target),
                "folder_name": fixture["name"], "media_type": "images"}
        with self.assertRaisesRegex(RuntimeError, "目标目录已存在"):
            self.upgrade._move(move)
        self.assertTrue((old / "同名 文件.jpg").is_file())
        self.assertEqual((old / "同名 文件.jpg").read_bytes(), b"original image bytes\x00\xff")
        self.assertEqual((target / "preexisting.bin").read_bytes(), b"target must survive")
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("select done from storage_moves where old_dir=?", (move["old_dir"],)).fetchone()[0], 0)

    def test_storage_resolution_and_migration_reject_escape_and_symlink(self):
        outside = Path(self.temp.name).parent / (Path(self.temp.name).name + "-outside")
        outside.mkdir()
        outside_file = outside / "must-survive.bin"
        outside_file.write_bytes(b"outside bytes")
        with self.assertRaises(ValueError):
            self.storage.resolve_storage_path("../" + outside.name + "/must-survive.bin")
        link = self.storage.config.images_dir / "legacy-link"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink creation unavailable in this container: {exc}")
        self.upgrade.state.update(action="upgrade")
        self.upgrade._run()
        self.assertEqual(self.upgrade.state["status"], "failed", self.upgrade.state)
        self.assertEqual(outside_file.read_bytes(), b"outside bytes")
        self.assertTrue(link.is_symlink())
        shutil.rmtree(outside)

    def test_move_rejects_symlink_components_before_resolving_old_path(self):
        canonical = self.storage.config.images_dir / "canonical-target"
        canonical.mkdir(parents=True)
        payload = canonical / "keep.bin"
        payload.write_bytes(b"canonical bytes")
        link = self.storage.config.images_dir / "legacy-alias"
        try:
            link.symlink_to(canonical, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink creation unavailable in this container: {exc}")
        destination = self.storage.config.images_dir / "relocated"
        move = {"old_dir": "data/images/legacy-alias", "new_dir": "data/images/relocated",
                "folder_name": "legacy-alias", "media_type": "images"}
        try:
            self.upgrade._move(move)
        except (RuntimeError, ValueError):
            pass
        else:
            self.fail("_move followed a symlink component and moved its canonical target")
        self.assertTrue(link.is_symlink())
        self.assertEqual(payload.read_bytes(), b"canonical bytes")
        self.assertFalse(destination.exists())

    def test_symlinked_tiny_files_and_directories_cannot_delete_external_files(self):
        outside = Path(self.temp.name).parent / (Path(self.temp.name).name + "-tiny-outside")
        outside.mkdir()
        outside_file = outside / "keep.webp"
        outside_file.write_bytes(b"external derivative target")
        tiny_root = self.storage.config.images_dir / "legacy" / ".thumbs" / "tiny"
        tiny_root.mkdir(parents=True)
        file_link = tiny_root / "linked.webp"
        dir_link = self.storage.config.images_dir / "other" / ".thumbs" / "tiny"
        dir_link.parent.mkdir(parents=True)
        try:
            file_link.symlink_to(outside_file)
            dir_link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            shutil.rmtree(outside)
            self.skipTest(f"symlink creation unavailable in this container: {exc}")
        self.storage.cleanup_trash_asset_files([])
        outside_bytes = outside_file.read_bytes() if outside_file.is_file() else None
        # Deleting a symlink itself is safe; following either symlink is not.
        self.assertTrue(dir_link.is_symlink())
        self.assertFalse(file_link.exists())
        self.assertEqual(outside_bytes, b"external derivative target")
        shutil.rmtree(outside)

    def test_report_write_failure_and_corrupt_state_restart_remain_blocked(self):
        self.upgrade.report("prepared", status="running", action="upgrade", progress=12)
        original_replace = Path.replace
        def fail_upgrade_state_replace(path, target):
            if path.name == "data-upgrade.tmp":
                raise OSError("injected disk full")
            return original_replace(path, target)
        with patch.object(Path, "replace", fail_upgrade_state_replace):
            self.upgrade._run()
        self.assertEqual(self.upgrade.state["status"], "failed")
        self.assertIn("injected disk full", self.upgrade.state["error"])
        self.assertFalse(self.db.modules_enabled)
        self.assertFalse((self.storage.config.config_dir / "before-data-layout-v2").exists())
        restarted = DataUpgrade(Database(self.db.db_path), self.storage, None, self.manager)
        self.assertTrue(restarted.blocked)
        self.assertEqual(restarted.state["status"], "interrupted")
        self.assertEqual(restarted.status()["progress"], 12)
        restarted.path.write_text("{broken json", encoding="utf-8")
        corrupt = DataUpgrade(Database(self.db.db_path), self.storage, None, self.manager)
        self.assertTrue(corrupt.blocked)
        self.assertEqual(corrupt.state["status"], "interrupted")
        self.assertFalse(corrupt.enter())

    def test_start_can_retry_after_progress_or_thread_start_failure(self):
        original_path = self.upgrade.path
        original_path.unlink(missing_ok=True)
        for failure in ("initial-report", "thread-start"):
            with self.subTest(failure=failure):
                self.upgrade.path.unlink(missing_ok=True)
                self.upgrade = DataUpgrade(self.db, self.storage, None, self.manager)
                if failure == "initial-report":
                    with patch.object(self.upgrade, "report", side_effect=OSError("injected initial progress write failure")):
                        with self.assertRaisesRegex(OSError, "initial progress"):
                            self.upgrade.start()
                    self.assertEqual(self.upgrade.state["status"], "idle")
                else:
                    class FailedThread:
                        def __init__(self, *args, **kwargs): pass
                        def start(self): raise RuntimeError("injected thread creation failure")
                    with patch("app.services.data_upgrade.threading.Thread", FailedThread):
                        with self.assertRaisesRegex(RuntimeError, "thread creation"):
                            self.upgrade.start()
                    self.assertEqual(self.upgrade.state["status"], "failed")
                    self.assertIn("thread creation", self.upgrade.state["error"])
                self.assertFalse(self.lock.locked(), "failed start must release the background-task lock")

                starts = []
                class RecordingThread:
                    def __init__(self, target, daemon): self.target, self.daemon = target, daemon
                    def start(self): starts.append(self.target)
                with patch("app.services.data_upgrade.threading.Thread", RecordingThread):
                    result = self.upgrade.start()
                self.assertEqual(result["status"], "running")
                self.assertEqual(len(starts), 1, "retry must actually request worker start")
                self.assertTrue(self.lock.locked())
                self.manager._release()  # The stub intentionally does not execute the worker.

    def test_check_mode_reports_missing_assets_without_touching_media_or_layout(self):
        folder = self._seed_folder("check-mode", pub_ts=0, pub_time="", pairs=0)
        legacy = self.storage.config.images_dir / folder["folder_name"] / "keep.jpg"
        legacy.parent.mkdir(parents=True)
        legacy.write_bytes(b"do not move during check")
        site = self.storage.config.data_dir / "sites" / "source" / "date" / "post" / "keep.jpg"
        site.parent.mkdir(parents=True)
        site.write_bytes(b"site original")
        source = self.db.create_site_source({"name": "check", "slug": "check", "entry_url": "https://example.invalid"})
        post = self.db.upsert_site_post(source["id"], {"title": "site", "url": "https://example.invalid/post"})
        asset = self.db.upsert_site_asset(post["id"], {"url": "https://example.invalid/asset", "filename": site.name, "media_type": "image"})
        self.db.set_site_asset_result(asset["id"], "ready", self.storage.relative_to_storage(site))
        with self.db.connect() as conn:
            conn.execute("insert into assets(folder_name,media_type,pair_index,filename,rel_path,status,created_at,updated_at) values(?,?,?,?,?,?,?,?)",
                         (folder["folder_name"], "video", 1, "missing.mp4", "data/images/check-mode/missing.mp4", "ready", "today", "today"))
        self.upgrade.state.update(action="check")
        self.upgrade._run()
        self.assertEqual(self.upgrade.state["status"], "completed", self.upgrade.state)
        self.assertEqual(self.upgrade.state["result"]["missing_assets"], 1)
        self.assertEqual(self.upgrade.state["result"]["checked_assets"], 2)
        self.assertFalse(self.db.modules_enabled)
        self.assertEqual(legacy.read_bytes(), b"do not move during check")
        self.assertEqual(site.read_bytes(), b"site original")
        self.assertFalse((self.storage.config.config_dir / "before-data-layout-v2").exists())
        self.assertFalse((self.storage.config.images_dir / "bilibili-unknown").exists())

    def test_pending_move_replay_and_new_source_layout_are_classified_as_folders(self):
        fixture = self._seed_move_fixture("pending-replay🙂")
        self.upgrade.state.update(action="upgrade", status="interrupted")
        self.upgrade._run()
        self.assertEqual(self.upgrade.state["status"], "completed", self.upgrade.state)
        self._assert_moved_references(fixture)

    def test_hard_exit_after_reference_commit_replays_only_remaining_moves(self):
        fixture = self._seed_move_fixture("post-commit-exit🙂")
        old_dir = self.storage.config.images_dir / fixture["name"]
        new_dir = fixture["image_target"].parent
        old_rel = self.storage.relative_to_storage(old_dir)
        new_rel = self.storage.relative_to_storage(new_dir)
        child_code = textwrap.dedent("""
            import os, sys
            from dataclasses import replace
            from pathlib import Path
            from types import SimpleNamespace
            from app.config import load_config
            from app.db import Database
            from app.services.data_upgrade import DataUpgrade
            from app.services.storage import StorageService
            app_db = Database(Path(sys.argv[1]))
            root = Path(sys.argv[2])
            config = replace(load_config(), storage_root=root, config_dir=root/'config', data_dir=root/'data',
                images_dir=root/'data/images', livephoto_dir=root/'data/livephoto', database_path=root/'config/app.db')
            upgrade = DataUpgrade(app_db, StorageService(config), None, SimpleNamespace())
            upgrade._move({'old_dir':sys.argv[3], 'new_dir':sys.argv[4], 'folder_name':sys.argv[5], 'media_type':'images'})
            os._exit(88)
        """)
        proc = subprocess.run([sys.executable, "-c", child_code, str(self.db.db_path), self.temp.name,
                               old_rel, new_rel, fixture["name"]], capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 88, (proc.stdout, proc.stderr))
        self.assertFalse(old_dir.exists())
        self.assertTrue(new_dir.is_dir())
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("select done from storage_moves where old_dir=?", (old_rel,)).fetchone()[0], 1)
            self.assertEqual(conn.execute("select rel_path from assets where folder_name=? and media_type='image'", (fixture["name"],)).fetchone()[0], self.storage.relative_to_storage(fixture["image_target"]))
            self.assertEqual(conn.execute("select count(*) from storage_moves where folder_name=? and done=0", (fixture["name"],)).fetchone()[0], 1)
        self.upgrade.state.update(action="upgrade", status="interrupted")
        self.upgrade._run()
        self.assertEqual(self.upgrade.state["status"], "completed", self.upgrade.state)
        self._assert_moved_references(fixture)
        # New downloads should be allocated under source/date/name after v2.
        new_name = "新采集-xhs🙂"
        self.storage.prepare_folder(new_name, "xhs-12345", "2026-10-21")
        image_dir = self.storage.image_folder(new_name)
        live_dir = self.storage.livephoto_folder(new_name)
        self.assertIn("/images/xhs-12345/2026-10-21/", image_dir.as_posix())
        self.assertIn("/livephoto/xhs-12345/2026-10-21/", live_dir.as_posix())
        image_dir.mkdir(parents=True)
        (image_dir / "same.jpg").write_bytes(b"new bytes")
        self.assertIn(new_name, self.storage.media_folder_names())
        self.assertNotIn("xhs-12345", self.storage.media_folder_names())
        # A legacy XHS name is converted to a new source bucket and not mistaken
        # for one of the source-directory buckets on a subsequent migration pass.
        legacy_xhs = "xhs_777_abc_20261021_2"
        self._seed_folder(legacy_xhs, pub_ts=0, pub_time="", uid="777", pairs=0)
        legacy_xhs_dir = self.storage.config.images_dir / legacy_xhs
        legacy_xhs_dir.mkdir(parents=True)
        (legacy_xhs_dir / "xhs.jpg").write_bytes(b"xhs file")
        self.upgrade.state.update(action="upgrade")
        self.upgrade._run()
        xhs_target = self.storage.layout_folder("images", legacy_xhs, "xhs-777", legacy_xhs)
        self.assertTrue((xhs_target / "xhs.jpg").is_file())
        self.assertNotIn("xhs-777", self.storage.media_folder_names())

    def test_upgrade_invalidates_duplicate_caches_but_check_mode_and_ignores_are_preserved(self):
        fixture = self._seed_move_fixture("duplicate-cache-layout🙂")
        service = DuplicateService(self.db, self.storage)
        self.manager.duplicate_service = service
        signature = "duplicate-cache-old-media-url"
        old_image_url = self.storage.storage_url(fixture["image_rel"])
        cached_group = {
            "signature": signature,
            "items": [{"folder_name": fixture["name"], "preview_url": old_image_url,
                       "tile": {"preview_url": old_image_url}}],
        }
        self.db.set_duplicate_group_cache("v5:old-layout-signature", [cached_group])
        self.db.ignore_duplicate_group(signature, [fixture["name"]])
        self.db.set_sidebar_count("duplicates", 7, "重复内容")
        service._cache_signature = "v5:old-layout-signature"
        service._cached_groups = [cached_group]
        service._cache_updated_at = "before-upgrade"
        service._has_cache = True
        service._cache_stale = False
        service._signature_aliases = {"old-signature": signature}

        self.upgrade.state.update(action="check", status="interrupted")
        self.upgrade._run()
        self.assertEqual(self.upgrade.state["status"], "completed", self.upgrade.state)
        check_cache = self.db.get_latest_duplicate_group_cache()
        self.assertIsNotNone(check_cache)
        self.assertEqual(check_cache["groups"], [cached_group])
        self.assertEqual(service._cache_signature, "v5:old-layout-signature")
        self.assertEqual(service._cached_groups, [cached_group])
        self.assertTrue(service._has_cache)
        self.assertEqual(self.db.list_ignored_duplicate_signatures(), {signature})
        self.assertEqual(self.db.get_sidebar_count_cache()["counts"]["duplicates"], 7)

        self.upgrade.state.update(action="upgrade", status="interrupted")
        self.upgrade._run()
        self.assertEqual(self.upgrade.state["status"], "completed", self.upgrade.state)
        self.assertIsNone(self.db.get_latest_duplicate_group_cache())
        self.assertEqual(service._cache_signature, "")
        self.assertEqual(service._cached_groups, [])
        self.assertIsNone(service._cache_updated_at)
        self.assertFalse(service._has_cache)
        self.assertTrue(service._cache_stale)
        self.assertEqual(service._signature_aliases, {})
        self.assertEqual(self.db.list_ignored_duplicate_signatures(), {signature})
        self.assertEqual(self.db.get_sidebar_count_cache()["counts"]["duplicates"], 0)
        self._assert_moved_references(fixture)

    def test_livephoto_recovery_resolves_migrated_paths_and_rejects_other_or_linked_paths(self):
        fixture = self._seed_move_fixture("recovery-layout_日本語🙂")
        self.upgrade.state.update(action="upgrade", status="interrupted")
        self.upgrade._run()
        self.assertEqual(self.upgrade.state["status"], "completed", self.upgrade.state)
        self._assert_moved_references(fixture)

        recovery = LivePhotoRecovery(LivePhotoService(self.db, self.storage))
        image_root = recovery._root(fixture["name"], "image")
        video_root = recovery._root(fixture["name"], "video")
        self.assertEqual(image_root, fixture["image_target"].parent)
        self.assertEqual(video_root, fixture["video_target"].parent)
        image_rel = self.storage.relative_to_storage(fixture["image_target"])
        video_rel = self.storage.relative_to_storage(fixture["video_target"])
        self.assertEqual(recovery._path(fixture["name"], image_rel, "image"), fixture["image_target"])
        self.assertEqual(recovery._path(fixture["name"], video_rel, "video"), fixture["video_target"])

        image_link = image_root / "same-content-alias.jpg"
        try:
            image_link.symlink_to(fixture["image_target"])
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink creation unavailable in this container: {exc}")
        image_link_rel = image_link.relative_to(self.storage.config.storage_root).as_posix()
        with self.assertRaises(LivePhotoError):
            recovery._path(fixture["name"], image_link_rel, "image")

        other_name = "recovery-other-content"
        self.storage.prepare_folder(other_name, "xhs-54321", "2026-10-21")
        other_image = self.storage.image_folder(other_name) / "unrelated.jpg"
        other_image.parent.mkdir(parents=True, exist_ok=True)
        other_image.write_bytes(b"another content original")
        with self.assertRaises(LivePhotoError):
            recovery._path(fixture["name"], self.storage.relative_to_storage(other_image), "image")

        source_bucket = fixture["image_target"].parent.parent.parent
        link_bucket = self.storage.config.images_dir / "linked-source-for-recovery"
        try:
            link_bucket.symlink_to(source_bucket, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink creation unavailable in this container: {exc}")
        linked_rel_dir = (link_bucket.relative_to(self.storage.config.storage_root)
                          / fixture["image_target"].parent.parent.name / fixture["name"]).as_posix()
        with self.db.connect() as conn:
            conn.execute("update folder_locations set rel_dir=? where folder_name=? and media_type='images'",
                         (linked_rel_dir, fixture["name"]))
        with self.assertRaises(LivePhotoError):
            recovery._root(fixture["name"], "image")

    def test_tiny_cleanup_preserves_active_original_even_when_trash_references_same_file(self):
        shared = self.storage.config.images_dir / "active" / "original.jpg"
        shared.parent.mkdir(parents=True)
        shared.write_bytes(b"shared-original-must-survive")
        tiny = self.storage.config.images_dir / "trash" / ".thumbs" / "tiny" / "preview.webp"
        tiny.parent.mkdir(parents=True)
        tiny.write_bytes(b"derivative")
        active_small = self.storage.config.images_dir / "active" / ".thumbs" / "small" / "preview.webp"
        active_thumb = self.storage.config.images_dir / "active" / ".thumbs" / "standard" / "preview.webp"
        live_cover = self.storage.config.livephoto_dir / "active" / ".source_covers" / "cover.jpg"
        reverse = self.storage.config.livephoto_dir / "active" / "reverse.mp4"
        avatar = self.storage.config.data_dir / "avatars" / "tiny" / "42.webp"
        site_tiny = self.storage.config.data_dir / "sites" / "src" / "date" / "post" / ".thumbs" / "tiny" / "icon.webp"
        for path in (active_small, active_thumb, live_cover, reverse, avatar, site_tiny):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"keep")
        active_tiny = self.storage.config.images_dir / "active" / ".thumbs" / "tiny" / "preview.webp"
        active_tiny.parent.mkdir(parents=True)
        active_tiny.write_bytes(b"active derivative")
        self._seed_folder("active", pairs=0)
        shared_rel = self.storage.relative_to_storage(shared)
        active_tiny_rel = self.storage.relative_to_storage(active_tiny)
        self.db.replace_folder_assets("active", "image", [{"filename": shared.name, "rel_path": shared_rel,
            "tiny_thumb_rel_path": active_tiny_rel, "pair_index": 1}])
        self.db.upsert_subscription("42", "avatar", avatar_tiny_url=self.storage.storage_url(self.storage.relative_to_storage(avatar)))
        source = self.db.create_site_source({"name": "site", "slug": "site", "entry_url": "https://example.invalid",
            "icon_tiny_url": self.storage.storage_url(self.storage.relative_to_storage(site_tiny))})
        with self.db.connect() as conn:
            conn.execute("update site_sources set icon_tiny_url=? where id=?",
                         (self.storage.storage_url(self.storage.relative_to_storage(site_tiny)), source["id"]))
        trash = {"assets_json": dumps_json([{
            "rel_path": shared_rel, "rel_path_alias": shared_rel,
            "tiny_thumb_rel_path": self.storage.relative_to_storage(tiny),
        }])}
        result = self.storage.cleanup_trash_asset_files([trash])
        # A path shared with a live asset is not garbage just because one trash
        # record names it; deleting it would silently break the active gallery.
        self.assertFalse(tiny.exists())
        self.assertFalse(active_tiny.exists())  # global tiny cache cleanup is expected
        for path in (active_small, active_thumb, live_cover, reverse, avatar, site_tiny):
            self.assertTrue(path.exists(), str(path))
        self.assertTrue(shared.is_file(), result)
        self.assertEqual(shared.read_bytes(), b"shared-original-must-survive")
        self.assertIsNotNone(source)

    def test_large_browse_order_filters_ties_and_revision_invalidation(self):
        folder_count, pair_count = 3000, 30000
        self.db.close()
        with self.db.connect() as conn:
            folder_rows = []
            folder_index_rows = []
            pair_rows = []
            for i in range(folder_count):
                name = f"bulk-{i:05d}"
                pub_ts = (i // 4) % 180  # deliberately many timestamp ties
                imported = f"2026-10-{(i % 28) + 1:02d}T00:00:00"
                uid = "uid-A" if i % 2 == 0 else "uid-B"
                favorite = int(i % 7 == 0)
                folder_rows.append((name, f"title {'needle' if i % 101 == 0 else 'item'} {i}", "needle text" if i % 101 == 0 else "ordinary",
                    pub_ts, "2026-10-01", f"top-{i}", f"src-{i}", 1, 1, "active", "approved", favorite, "{}", imported, imported, uid, uid))
                folder_index_rows.append((name, folder_rows[-1][1], folder_rows[-1][2], pub_ts, "2026-10-01", f"top-{i}", f"src-{i}", uid, uid,
                    1, 1, favorite, "approved", None, 10, 10, 20, "[]", "2026", "10", imported, imported))
                for pair in range(10):
                    pair_rows.append((f"{name}:{pair}", name, pair, folder_rows[-1][1], pub_ts, "2026-10-01", uid, uid, favorite,
                        1, 1, f"/storage/{name}/{pair}.jpg", "image", None, None, "{}", "{}", "2026", "10", imported, imported))
            conn.executemany("insert into folders(folder_name,title,text_prefix,pub_ts,pub_time,top_dynamic_id,source_dynamic_id,has_images,has_livephoto,status,review_status,is_favorite,metadata_json,created_at,updated_at,subscription_uid,subscription_name) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", folder_rows)
            conn.executemany("insert into folder_index(folder_name,title,text_prefix,pub_ts,pub_time,top_dynamic_id,source_dynamic_id,subscription_uid,subscription_name,has_images,has_livephoto,is_favorite,review_status,review_reason,image_count,livephoto_count,asset_count,preview_assets_json,year_key,month_key,updated_at,imported_at) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", folder_index_rows)
            conn.executemany("insert into pair_index(item_key,folder_name,pair_index,title,pub_ts,pub_time,subscription_uid,subscription_name,is_favorite,has_image,has_livephoto,preview_url,preview_kind,thumb_url,display_ratio,image_json,livephoto_json,year_key,month_key,updated_at,imported_at) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", pair_rows)
        split_modules(self.db, lambda _: None)
        self._reopen()
        self.assertEqual(self.db.query_pair_index(page=1, page_size=1)["total"], pair_count)
        for table, query in (("folder_index", self.db.query_folder_index), ("pair_index", self.db.query_pair_index)):
            for sort in ("desc", "asc", "imported"):
                column = "imported_at" if sort == "imported" else "pub_ts"
                direction = "asc" if sort == "asc" else "desc"
                pair = table == "pair_index"
                order = f"{column} {direction},folder_name {direction}" + (",pair_index asc" if pair else "")
                with self.db.connect() as conn:
                    select = "folder_name" + (",pair_index" if pair else "")
                    expected = [tuple(row) for row in conn.execute(f"select {select} from {table} order by {order}")]
                    plan = " ".join(str(tuple(row)) for row in conn.execute(f"explain query plan select * from {table} order by {order} limit 8"))
                self.assertNotIn("TEMP B-TREE", plan, (table, sort, plan))
                page_size = 997
                actual = []
                for page in range(1, (len(expected) + page_size - 1) // page_size + 1):
                    result = query(page=page, page_size=page_size, sort_order=sort)
                    actual.extend((r["folder_name"], r["pair_index"]) if pair else (r["folder_name"],) for r in result["items"])
                self.assertEqual(actual, expected, (table, sort))
        filtered = self.db.query_pair_index(category="favorites", subscription_uids=["uid-A"], year="2026", month="10", search_query="needle", page=1, page_size=17)
        self.assertGreater(filtered["total"], 0)
        for item in filtered["items"]:
            self.assertEqual(item["subscription_uid"], "uid-A")
            self.assertTrue(item["is_favorite"])
            self.assertIn("needle", item["title"].lower())
        with self.db.connect() as conn:
            xhs_source_id = conn.execute("insert into site_sources(name,slug,source_type,entry_url,created_at,updated_at) values(?,?,?,?,?,?)",
                ("synthetic xhs", "synthetic-xhs", "xhs_likes", "https://example.invalid/xhs", "today", "today")).lastrowid
        xhs_uid = f"site:{xhs_source_id}"
        with self.db.connect() as conn:
            conn.execute("update folder_index set subscription_uid=? where cast(substr(folder_name,7) as integer) % 100 = 0", (xhs_uid,))
            conn.execute("update pair_index set subscription_uid=? where cast(substr(folder_name,7) as integer) % 100 = 0", (xhs_uid,))
        xhs_items = self.db.query_pair_index(source_kind="xhs", page=1, page_size=20)
        self.assertEqual(xhs_items["total"], 300)
        self.assertTrue(all(item["subscription_uid"] == xhs_uid for item in xhs_items["items"]))
        upstream_items = self.db.query_folder_index(source_kind="up", page=1, page_size=20)
        self.assertEqual(upstream_items["total"], folder_count - 30)
        before = self.db.query_folder_index(page=1, page_size=5)["total"]
        self._seed_folder("revision-new", pub_ts=999, pub_time="2026-10-21", favorite=True, pairs=1)
        after = self.db.query_folder_index(page=1, page_size=5)["total"]
        self.assertEqual((before, after), (folder_count, folder_count + 1))
        deep = self.db.query_pair_index(page=31, page_size=997, sort_order="desc")
        self.assertEqual(len(deep["items"]), pair_count + 1 - 30 * 997)

    def test_parallel_reads_writes_and_upgrade_request_drain(self):
        for i in range(160):
            self._seed_folder(f"parallel-{i:04d}", pub_ts=i // 2, pub_time="2026-10-01", pairs=2)
        errors = []
        barrier = threading.Barrier(7)
        def reader(offset):
            try:
                barrier.wait()
                for page in range(1, 9):
                    result = self.db.query_pair_index(page=page, page_size=11, sort_order="desc")
                    if result["total"] < 320:
                        raise AssertionError(f"unexpected partial total {result['total']} at reader {offset}")
            except BaseException as exc:
                errors.append(exc)
        threads = [threading.Thread(target=reader, args=(n,)) for n in range(6)]
        for thread in threads: thread.start()
        barrier.wait()
        for i in range(20):
            self.db.save_settings({"parallel_writer": i})
            self.db.set_folder_favorite(f"parallel-{i:04d}", bool(i % 2))
        for thread in threads: thread.join(timeout=30)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertFalse(errors, errors)

        entered = threading.Event()
        release = threading.Event()
        self.upgrade.before_run = lambda: (entered.set(), release.wait(10))
        self.assertTrue(self.upgrade.enter())  # in-flight request holds the drain barrier
        first = self.upgrade.start()
        self.assertEqual(first["status"], "running")
        self.assertEqual(self.upgrade.start()["status"], "running")  # duplicate start coalesces
        self.assertFalse(entered.wait(.2))
        self.upgrade.leave()
        self.assertTrue(entered.wait(5))
        release.set()
        deadline = time.monotonic() + 30
        while self.upgrade.state["status"] == "running" and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertEqual(self.upgrade.state["status"], "completed", self.upgrade.state)
        self.manager._queue.append("synthetic-background-job")
        with self.assertRaisesRegex(RuntimeError, "后台任务"):
            self.upgrade.start()
        self.assertFalse(self.lock.locked())

    def test_instance_lock_rejects_a_second_process_until_owner_exits(self):
        child_code = textwrap.dedent("""
            import sys
            from pathlib import Path
            from app.services.data_upgrade import storage_instance_lock
            try:
                with storage_instance_lock(Path(sys.argv[1])):
                    print('acquired')
            except RuntimeError as exc:
                print('blocked:' + str(exc))
        """)
        with storage_instance_lock(self.storage.config.config_dir):
            blocked = subprocess.run([sys.executable, "-c", child_code, str(self.storage.config.config_dir)],
                                     capture_output=True, text=True, timeout=30)
            self.assertEqual(blocked.returncode, 0, blocked.stderr)
            self.assertTrue(blocked.stdout.startswith("blocked:"), blocked.stdout)
        acquired = subprocess.run([sys.executable, "-c", child_code, str(self.storage.config.config_dir)],
                                  capture_output=True, text=True, timeout=30)
        self.assertEqual(acquired.returncode, 0, acquired.stderr)
        self.assertEqual(acquired.stdout.strip(), "acquired")

    def test_middleware_keeps_request_count_until_background_work_finishes(self):
        waiting = asyncio.Event()
        release = asyncio.Event()
        async def handler(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})
            waiting.set()
            await release.wait()
        async def scenario():
            sent = []
            async def send(message): sent.append(message)
            gate = DataUpgradeMiddleware(handler, self.upgrade, None, "fixture")
            scope = {"type": "http", "path": "/api/gallery", "method": "GET", "headers": []}
            task = asyncio.create_task(gate(scope, None, send))
            await waiting.wait()
            self.assertEqual(self.upgrade.requests, 1)
            self.upgrade.state["status"] = "running"
            sent.clear()
            await gate(scope, None, send)
            self.assertEqual(sent[0]["status"], 503)
            self.assertEqual(self.upgrade.requests, 1)
            release.set()
            await task
            self.assertEqual(self.upgrade.requests, 0)
        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main(verbosity=2)
