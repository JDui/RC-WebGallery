from __future__ import annotations

import pytest
from PIL import Image

from app.config import load_config
from app.db import Database
from app.services.gallery import GalleryService
from app.services.media_indexer import MediaIndexer
from app.services.storage import StorageService
from app.services.thumbnailer import ThumbnailService
from app.services.xhs.sync import XhsSyncService


@pytest.mark.parametrize("indexed", [False, True])
@pytest.mark.parametrize("view_mode", ["folder", "pair"])
def test_import_order_preserves_source_date_and_works_with_index_or_fallback(tmp_path, monkeypatch, indexed, view_mode):
    monkeypatch.setenv("APP_STORAGE_ROOT", str(tmp_path))
    config = load_config()
    storage = StorageService(config)
    storage.ensure()
    db = Database(config.database_path)
    db.init()
    indexer = MediaIndexer(db, storage, ThumbnailService())
    for name, pub_ts, created_at in [("future-source", 4102444800, "2026-01-01T12:00:00+08:00"), ("recent-import", 1767225600, "2026-10-18T12:00:00+08:00")]:
        directory = storage.image_folder(name)
        directory.mkdir(parents=True)
        Image.new("RGB", (32, 32), "blue").save(directory / "01.jpg")
        indexer.index_folder(name, pub_ts, name, name, "top-" + name, "src-" + name)
        with db.connect() as conn:
            conn.execute("update folders set created_at = ? where folder_name = ?", (created_at, name))
    if indexed:
        indexer.rebuild_gallery_indexes()
    else:
        db.save_settings({"gallery_index_version": 0})
    gallery = GalleryService(db, storage)
    published = gallery.get_gallery_items(view_mode=view_mode, sort_order="desc")
    imported = gallery.get_gallery_items(view_mode=view_mode, sort_order="imported")
    assert published["items"][0]["folder_name"] == "future-source"
    assert imported["items"][0]["folder_name"] == "recent-import"
    assert any(item["pub_time"].startswith("2100-") for item in imported["items"])
    assert gallery.get_gallery_items(view_mode=view_mode, sort_order="imported")["items"] == imported["items"]


@pytest.mark.parametrize("indexed", [False, True])
@pytest.mark.parametrize("view_mode", ["folder", "pair"])
def test_xhs_gallery_filter_is_separate_from_sites_for_index_and_fallback(tmp_path, monkeypatch, indexed, view_mode):
    monkeypatch.setenv("APP_STORAGE_ROOT", str(tmp_path))
    config = load_config()
    storage = StorageService(config)
    storage.ensure()
    db = Database(config.database_path)
    db.init()
    xhs = XhsSyncService(db).create_source({"name": "点赞", "owner_key": "test-account"})
    site = db.create_site_source({"name": "普通站点", "entry_url": "https://example.test", "source_type": "html"})
    indexer = MediaIndexer(db, storage, ThumbnailService())
    for name, uid in [("bili", "12345"), ("site", f"site:{site['id']}"), ("xhs", f"site:{xhs['id']}")]:
        directory = storage.image_folder(name)
        directory.mkdir(parents=True)
        Image.new("RGB", (32, 32), "blue").save(directory / "01.jpg")
        indexer.index_folder(name, 1767225600, name, name, "top-" + name, "src-" + name,
                             subscription_uid=uid, subscription_name=name)
    if indexed:
        indexer.rebuild_gallery_indexes()
    else:
        db.save_settings({"gallery_index_version": 0})
    gallery = GalleryService(db, storage)
    for kind, expected in [("all", {"bili", "site", "xhs"}), ("up", {"bili"}), ("site", {"site"}), ("xhs", {"xhs"})]:
        payload = gallery.get_gallery_items(source_kind=kind, view_mode=view_mode)
        assert payload["total"] == len(expected)
        assert {item["folder_name"] for item in payload["items"]} == expected
        assert gallery.get_gallery_items(source_kind=kind, view_mode=view_mode)["cache_hit"] is True
