import pytest

from app.services.gallery import GalleryService
from app.services.random_browse import RandomBrowseError, RandomFeedExpired
from app.services.database_layout import split_modules
from test_thumbnail_policy import library


def populate(db, indexer, count=73):
    for index in range(count):
        name = f'random-{index:03d}'
        db.upsert_folder({'folder_name': name, 'title': name, 'pub_ts': index, 'pub_time': '2026-10-22',
                          'top_dynamic_id': name, 'source_dynamic_id': name, 'has_images': True,
                          'subscription_uid': '42' if index % 2 else '43', 'subscription_name': 'UP 主'})
        db.replace_folder_assets(name, 'image', [{'pair_index': pair, 'filename': f'{pair}.jpg',
            'rel_path': f'data/images/{name}/{pair}.jpg', 'width': 100, 'height': 100} for pair in (1, 2)])
        indexer._replace_gallery_index(db.get_folder(name), db.list_assets_for_folder(name))
    db.mark_gallery_index_rebuilt()


def collect(gallery, seed, mode='folder', **filters):
    output = []
    for page in range(1, 100):
        payload = gallery.get_gallery_items(sort_order='random', random_seed=seed, view_mode=mode,
                                            page=page, page_size=11, **filters)
        output.extend(item.get('item_key') or item['folder_name'] for item in payload['items'])
        if page >= payload['total_pages']:
            return output
    pytest.fail('random pagination failed to finish')


@pytest.mark.parametrize('mode', ['folder', 'pair'])
def test_random_stream_covers_the_filtered_library_once_and_repeats_pages_stably(tmp_path, mode):
    db, storage, indexer = library(tmp_path)
    populate(db, indexer)
    gallery = GalleryService(db, storage)
    keys = collect(gallery, 'round-one', mode)
    assert len(keys) == len(set(keys)) == (73 if mode == 'folder' else 146)
    assert keys == collect(gallery, 'round-one', mode)
    assert keys != collect(gallery, 'round-two', mode)
    filtered = collect(gallery, 'filtered', mode, subscription_uids=['42'])
    assert len(filtered) == len(set(filtered)) == (36 if mode == 'folder' else 72)


def test_library_changes_do_not_reorder_the_active_random_stream(tmp_path):
    db, storage, indexer = library(tmp_path)
    populate(db, indexer)
    gallery = GalleryService(db, storage)
    original = collect(gallery, 'stable')
    db.set_folder_favorite('random-001', True)
    with db.connect() as conn:
        conn.execute('delete from pair_index where folder_name=?', (original[20],))
        conn.execute('delete from folder_index where folder_name=?', (original[20],))
        conn.execute('delete from folders where folder_name=?', (original[20],))
    db.upsert_folder({'folder_name': 'new-arrival', 'title': '新内容', 'pub_ts': 999,
                      'top_dynamic_id': 'new', 'source_dynamic_id': 'new', 'has_images': True})
    db.replace_folder_assets('new-arrival', 'image', [{'pair_index': 1, 'filename': '1.jpg', 'rel_path': 'new/1.jpg'}])
    indexer._replace_gallery_index(db.get_folder('new-arrival'), db.list_assets_for_folder('new-arrival'))
    db.mark_gallery_index_rebuilt()
    seen = collect(gallery, 'stable')
    assert seen == [key for key in original if key != original[20]]
    assert len(seen) == len(set(seen))


def test_fallback_to_ready_index_transition_keeps_the_same_snapshot(tmp_path):
    db, storage, indexer = library(tmp_path)
    populate(db, indexer, count=25)
    db.save_settings({'gallery_index_version': 0})
    gallery = GalleryService(db, storage)
    first = gallery.get_gallery_items(sort_order='random', random_seed='upgrade-round', page_size=11)
    db.mark_gallery_index_rebuilt()
    second = gallery.get_gallery_items(sort_order='random', random_seed='upgrade-round', page_size=11, page=2)
    assert not {item['folder_name'] for item in first['items']} & {item['folder_name'] for item in second['items']}
    assert len(collect(gallery, 'upgrade-round')) == 25


def test_sessions_are_bounded_and_expired_pages_do_not_silently_reshuffle(tmp_path):
    db, storage, indexer = library(tmp_path)
    populate(db, indexer, count=6)
    gallery = GalleryService(db, storage)
    for index in range(20):
        gallery.get_gallery_items(sort_order='random', random_seed=f'round-{index}', page_size=2)
    with db.connect() as conn:
        assert conn.execute('select count(*) from random_browse_sessions').fetchone()[0] <= 16
        assert conn.execute('select count(*) from random_browse_entries').fetchone()[0] <= 16 * 6
    with pytest.raises(RandomFeedExpired):
        gallery.get_gallery_items(sort_order='random', random_seed='round-0', page=2, page_size=2)
    with pytest.raises(RandomBrowseError):
        gallery.get_gallery_items(sort_order='random', page=2)
    with pytest.raises(RandomFeedExpired):
        gallery.get_gallery_items(sort_order='random', random_seed='round-19', page=2, page_size=3)


def test_split_database_supports_new_and_existing_random_sessions(tmp_path):
    db, storage, indexer = library(tmp_path)
    populate(db, indexer, count=12)
    first = GalleryService(db, storage).get_gallery_items(sort_order='random', random_seed='split', page_size=6)
    split_modules(db, lambda message: None)
    second = GalleryService(db, storage).get_gallery_items(sort_order='random', random_seed='split-new', page_size=6)
    assert len(second['items']) == 6
    # Moving modules discards cache sessions; an old page must expire explicitly.
    with pytest.raises(RandomFeedExpired):
        GalleryService(db, storage).get_gallery_items(sort_order='random', random_seed='split', page=2, page_size=6)
