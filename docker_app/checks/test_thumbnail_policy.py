import json
from dataclasses import replace

from PIL import Image

from app.config import load_config
from app.db import Database
from app.services.gallery import GalleryService
from app.services.media_indexer import MediaIndexer
from app.services.storage import StorageService
from app.services.thumbnailer import ThumbnailService


def library(tmp_path):
    config = replace(load_config(), storage_root=tmp_path, config_dir=tmp_path / 'config',
                     data_dir=tmp_path / 'data', images_dir=tmp_path / 'data/images',
                     livephoto_dir=tmp_path / 'data/livephoto', database_path=tmp_path / 'config/app.db')
    storage = StorageService(config)
    storage.ensure()
    db = Database(config.database_path)
    db.init()
    storage.db = db
    thumbnails = ThumbnailService()
    thumbnails.apply_settings(db.get_settings())
    indexer = MediaIndexer(db, storage, thumbnails)
    indexer.sidecar.media_worker = None
    return db, storage, indexer


def test_settings_migrate_once_and_cover_preferences_persist(tmp_path):
    db, _, _ = library(tmp_path)
    with db.control_connect() as conn:
        conn.execute("delete from settings where key='single_thumbnail_migrated'")
        conn.execute("update settings set value='192' where key='small_thumb_edge'")
        conn.execute("insert or replace into settings values('thumb_edge','576')")
    db.init()
    assert db.get_settings()['small_thumb_edge'] == 368
    assert db.get_settings()['small_thumb_quality'] == 50
    assert 'thumb_edge' not in db.get_settings()
    assert db.get_settings()['wide_single_cover'] is False
    assert db.get_settings()['random_cover_enabled'] is True
    db.save_settings({'small_thumb_edge': 400, 'small_thumb_quality': 60,
                      'wide_single_cover': True, 'random_cover_enabled': False})
    db.init()
    saved = Database(db.db_path).get_settings()
    assert saved['small_thumb_edge'] == 400
    assert saved['small_thumb_quality'] == 60
    assert saved['wide_single_cover'] is True
    assert saved['random_cover_enabled'] is False


def test_media_has_one_thumbnail_and_random_cover_samples_entire_folder(tmp_path, monkeypatch):
    db, storage, indexer = library(tmp_path)
    folder = storage.image_folder('sample')
    folder.mkdir(parents=True)
    for number in range(1, 7):
        Image.new('RGB', (800, 1200), (number * 30, 40, 90)).save(folder / f'{number:03}.jpg')
    monkeypatch.setattr('app.services.media_indexer.random.choice', lambda items: items[-1])
    indexer.index_folder('sample', 1, 'Sample', '', 'top', 'source')
    assets = db.list_assets_for_folder('sample')
    assert len(list((folder / '.thumbs').rglob('*.webp'))) == 6
    for asset in assets:
        assert asset['thumb_rel_path'] == asset['small_thumb_rel_path']
        with Image.open(storage.resolve_storage_path(asset['small_thumb_rel_path'])) as image:
            assert min(image.size) == 368
    payload = GalleryService(db, storage).get_gallery_items()
    card = next(item for item in payload['items'] if item['folder_name'] == 'sample')
    assert len(card['preview_tiles']) == 4
    assert card['random_cover']['pair_index'] == 6


def test_retired_tiers_are_garbage_but_current_media_and_covers_are_protected(tmp_path):
    db, storage, indexer = library(tmp_path)
    folder = storage.image_folder('sample')
    folder.mkdir(parents=True)
    original = folder / '001.jpg'
    Image.new('RGB', (900, 600), 'blue').save(original)
    indexer.index_folder('sample', 1, 'Sample', '', 'top', 'source')
    asset = db.list_assets_for_folder('sample')[0]
    current = storage.resolve_storage_path(asset['small_thumb_rel_path'])
    retired = folder / '.thumbs/001.webp'
    tiny = folder / '.thumbs/tiny/001.webp'
    tiny.parent.mkdir()
    retired.write_bytes(b'old-thumb')
    tiny.write_bytes(b'old-tiny')
    shared_cover = folder / '.thumbs/protected-cover.webp'
    shared_cover.write_bytes(b'cover')
    with db.connect() as conn:
        conn.execute('update assets set thumb_rel_path=?,cover_rel_path=? where id=?',
                     (storage.relative_to_storage(retired), storage.relative_to_storage(shared_cover), asset['id']))
    trash = [{'assets_json': json.dumps([asset])}]
    files = set(storage.trash_asset_files(trash))
    assert retired in files and tiny in files
    assert not {original, current, shared_cover} & files
    stats = storage.storage_usage_stats(trash)
    assert stats['trash_files'] == 2
    assert stats['trash_bytes'] == len(b'old-thumbold-tiny')
    result = storage.cleanup_trash_asset_files(trash)
    assert result['removed_files'] == 2
    assert original.exists() and current.exists() and shared_cover.exists()
    assert db.list_assets_for_folder('sample')[0]['thumb_rel_path'] == asset['small_thumb_rel_path']


def test_legacy_custom_thumbnail_settings_are_preserved(tmp_path):
    db, _, _ = library(tmp_path)
    with db.control_connect() as conn:
        conn.execute("delete from settings where key='single_thumbnail_migrated'")
        conn.execute("update settings set value='256' where key='small_thumb_edge'")
        conn.execute("update settings set value='70' where key='small_thumb_quality'")
    db.init()
    assert db.get_settings()['small_thumb_edge'] == 256
    assert db.get_settings()['small_thumb_quality'] == 70


def test_python_worker_generates_only_the_configured_single_tier(tmp_path, monkeypatch):
    import io
    import sys
    from PIL import ImageOps
    from app.services.sidecar import SidecarRunner

    _, storage, indexer = library(tmp_path)
    worker = tmp_path / 'media-worker'
    worker.write_text(f'#!{sys.executable}\nfrom app.sidecars.media_worker import main\nraise SystemExit(main())\n')
    worker.chmod(0o755)
    monkeypatch.setenv('APP_MEDIA_WORKER_BIN', str(worker))
    indexer.sidecar = SidecarRunner()
    source = storage.image_folder('worker') / '001.jpg'
    source.parent.mkdir(parents=True)
    Image.new('RGB', (800, 600), (50, 100, 180)).save(source)
    thumb, small = indexer._ensure_asset_thumbnails({'media_type': 'image', 'rel_path': storage.relative_to_storage(source)})
    assert thumb == small
    target = storage.resolve_storage_path(small)
    assert len(list(source.parent.rglob('*.webp'))) == 1
    with Image.open(source) as image:
        normalized = ImageOps.exif_transpose(image).convert('RGB')
        expected = indexer.thumbnailer._resize_to_short_edge(normalized, 368)
        output = io.BytesIO()
        expected.save(output, format='WEBP', quality=50, method=6)
    assert target.read_bytes() == output.getvalue()
