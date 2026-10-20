from __future__ import annotations

import pytest

from app.config import load_config
from app.db import Database
from app.services.gallery import GalleryService
from app.services.media_indexer import MediaIndexer
from app.services.storage import StorageService
from app.services.thumbnailer import ThumbnailService


@pytest.mark.parametrize("indexed", [False, True])
def test_video_category_keeps_video_posts_separate_from_livephotos(tmp_path, monkeypatch, indexed):
    monkeypatch.setenv("APP_STORAGE_ROOT", str(tmp_path))
    config = load_config()
    storage = StorageService(config)
    storage.ensure()
    db = Database(config.database_path)
    db.init()
    indexer = MediaIndexer(db, storage, ThumbnailService())
    for name, media_type, timestamp in [("video-old", "video", 100), ("video-new", "video", 300), ("live-photo", "livephoto", 200), ("photo", "image", 400)]:
        db.upsert_folder({"folder_name": name, "title": name, "pub_ts": timestamp,
                          "top_dynamic_id": name, "source_dynamic_id": name,
                          "pub_time": f"2026-10-{timestamp // 100:02d}", "has_images": media_type == "image",
                          "has_livephoto": media_type == "livephoto", "subscription_uid": "123"})
        db.replace_folder_assets(name, media_type, [{"pair_index": 1, "filename": "01.mp4" if media_type != "image" else "01.jpg",
            "rel_path": f"data/{name}/01.mp4", "width": 720, "height": 1280}])
        indexer.refresh_gallery_index(name)
    if indexed:
        indexer.rebuild_gallery_indexes()
    else:
        db.save_settings({"gallery_index_version": 0})
    gallery = GalleryService(db, storage)
    result = gallery.get_gallery_items(category="video", view_mode="pair", page_size=1, sort_order="desc")
    assert result["view_mode"] == "folder"
    assert result["total"] == 2
    assert result["total_pages"] == 2
    assert result["items"][0]["folder_name"] == "video-new"
    assert gallery.get_gallery_items(category="video", page=2, page_size=1)["items"][0]["folder_name"] == "video-old"
    assert gallery.get_gallery_items(category="video", sort_order="asc")["items"][0]["folder_name"] == "video-old"
    assert gallery.get_gallery_items(category="video", subscription_uids=["other"])["total"] == 0
    assert gallery.get_gallery_meta()["counts"]["video"] == 2
    assert db.refresh_sidebar_count_cache(["all"])["counts"]["video"] == 2
    assert gallery.get_gallery_items(category="livephoto")["total"] == 1
