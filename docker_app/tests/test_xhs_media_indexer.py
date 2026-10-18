from pathlib import Path

import pytest

from app.config import load_config
from app.db import Database
from app.services.media_indexer import MediaIndexer
from app.services.storage import StorageService


class RecordingThumbnailer:
    def __init__(self):
        self.reverse_calls = []

    def ensure_video_cover(self, source, target):
        return False

    def ensure_reverse_video(self, source, target):
        self.reverse_calls.append((source, target))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"synthetic-reverse-video")
        return True


def build_indexer(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_STORAGE_ROOT", str(tmp_path / "storage"))
    monkeypatch.setenv("APP_REPO_ROOT", str(tmp_path / "repo"))
    config = load_config()
    storage = StorageService(config)
    storage.ensure()
    db = Database(config.database_path)
    db.init()
    thumbnailer = RecordingThumbnailer()
    return MediaIndexer(db, storage, thumbnailer), storage, db, thumbnailer


@pytest.mark.parametrize("standalone", [True, False])
def test_media_indexer_keeps_standalone_video_distinct_from_livephoto(tmp_path, monkeypatch, standalone):
    indexer, storage, db, thumbnailer = build_indexer(tmp_path, monkeypatch)
    folder_name = "xhs_synthetic_video"
    image_folder = storage.image_folder(folder_name)
    video_folder = storage.livephoto_folder(folder_name)
    image_folder.mkdir(parents=True)
    video_folder.mkdir(parents=True)
    still = image_folder / "001__still.jpg"
    still.write_bytes(b"synthetic-still")
    video = video_folder / "001__motion.mp4"
    video.write_bytes(b"synthetic-video")
    image_asset = {
        "pair_index": 1,
        "filename": still.name,
        "rel_path": storage.relative_to_storage(still),
        "metadata": {"kind": "image", "pair_hash": "0" * 64},
    }
    monkeypatch.setattr(
        indexer,
        "_index_images",
        lambda *_args, **_kwargs: [] if standalone else [image_asset],
    )
    monkeypatch.setattr(indexer, "_backfill_thumbnails", lambda assets: assets)

    indexer.index_folder(
        folder_name, 0, "Synthetic video", "", "top-synthetic", "source-synthetic",
        generate_derivatives=True, metadata={"standalone_video": standalone},
    )

    folder = db.get_folder(folder_name)
    assets = db.list_assets_for_folder(folder_name)
    video_assets = [asset for asset in assets if asset["media_type"] == "video"]
    livephoto_assets = [asset for asset in assets if asset["media_type"] == "livephoto"]
    assert len(video_assets) == int(standalone)
    assert len(livephoto_assets) == int(not standalone)
    assert bool(folder["has_livephoto"]) is (not standalone)
    if standalone:
        assert thumbnailer.reverse_calls == []
        assert video_assets[0]["reverse_rel_path"] is None
    else:
        assert len(thumbnailer.reverse_calls) == 1
        source, reverse_path = thumbnailer.reverse_calls[0]
        assert source == video and reverse_path.is_file()
        assert livephoto_assets[0]["reverse_rel_path"] == storage.relative_to_storage(reverse_path)
