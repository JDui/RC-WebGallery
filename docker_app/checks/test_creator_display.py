from dataclasses import replace

from PIL import Image

from app.db import Database
from app.services.creators import attach_creators
from app.services.gallery import GalleryService
from app.services.xhs.models import XhsNote
from app.services.xhs.sync import ScanPage, XhsSyncService
from xhs_worker.parser import normalize_note
from test_thumbnail_policy import library


def note(name='笔记作者'):
    raw, _ = normalize_note({'noteId': 'author-note', 'title': '示例笔记',
                             'user': {'userId': 'author-id', 'nickname': name}})
    return XhsNote.parse(raw)


def test_parser_accepts_optional_author_name_and_older_notes():
    parsed = note('作者\n名字')
    assert parsed.author_name == '作者 名字'
    assert parsed.author_id == 'author-id'
    old = {'note_id': 'old-note', 'url': 'https://www.xiaohongshu.com/explore/old-note',
           'observed_at': parsed.observed_at}
    assert XhsNote.parse(old).author_name == ''


def test_scan_persists_author_name_and_empty_updates_do_not_erase_it(tmp_path):
    db, _, _ = library(tmp_path)
    sync = XhsSyncService(db)
    source = sync.create_source({'name': '我的小红书点赞', 'owner_key': 'test-owner'})
    for author_name in ('作者名字', ''):
        sync.scan_pages(source['source_id'], 'test-owner', [ScanPage((replace(note(author_name), author_id='author-id' if author_name else ''),), end_of_feed=True)],
                        ignore_anchors=True, emit_task_history=False)
    db.init()
    with Database(db.db_path).connect() as conn:
        saved = conn.execute('select author_name,author_id from xhs_like_state').fetchone()
    assert saved['author_name'] == '作者名字'
    assert saved['author_id'] == 'author-id'


def test_folder_pair_detail_and_cached_cards_show_current_note_author(tmp_path):
    db, storage, indexer = library(tmp_path)
    sync = XhsSyncService(db)
    source = sync.create_source({'name': '我的小红书点赞', 'owner_key': 'test-owner'})
    sync.scan_pages(source['source_id'], 'test-owner', [ScanPage((note(),), end_of_feed=True)], emit_task_history=False)
    folder_name = f"xhs_{source['source_id']}_author-note"
    folder = storage.image_folder(folder_name)
    folder.mkdir(parents=True)
    Image.new('RGB', (100, 100), 'blue').save(folder / '001.jpg')
    indexer.index_folder(folder_name, 1, '示例', '', 'top', 'source',
                         subscription_uid=f"site:{source['source_id']}", subscription_name=source['name'])
    indexer.rebuild_gallery_indexes(clear_derivatives=False)
    gallery = GalleryService(db, storage)
    for mode in ('folder', 'pair'):
        card = gallery.get_gallery_items(view_mode=mode)['items'][0]
        assert card['creator_name'] == '笔记作者'
        assert card['creator_url'] == 'https://www.xiaohongshu.com/user/profile/author-id'
        assert card['subscription_name'] == '我的小红书点赞'
    detail = gallery.get_folder_detail(folder_name)['folder']
    assert detail['creator_name'] == '笔记作者'
    with db.connect() as conn:
        conn.execute("update xhs_like_state set author_name='更新用户名'")
    card = gallery.get_gallery_items()['items'][0]
    assert card['creator_name'] == '更新用户名'


def test_creator_links_resolve_up_sites_and_missing_xhs_names(tmp_path):
    db, _, _ = library(tmp_path)
    sync = XhsSyncService(db)
    source = sync.create_source({'name': '旧订阅', 'owner_key': 'test-owner'})
    sync.scan_pages(source['source_id'], 'test-owner', [ScanPage((note(''),), end_of_feed=True)], emit_task_history=False)
    with db.connect() as conn:
        cursor = conn.execute("""insert into site_sources(name,slug,source_type,entry_url,created_at,updated_at)
            values ('站点名','creator-site','rss','https://example.test/feed.xml','now','now')""")
        site_id = cursor.lastrowid
    cards = [
        {'folder_name': 'bili', 'subscription_uid': '123', 'subscription_name': 'UP 主'},
        {'folder_name': 'site', 'subscription_uid': f'site:{site_id}', 'subscription_name': '旧站点名'},
        {'folder_name': f"xhs_{source['source_id']}_author-note", 'subscription_name': '我的小红书点赞'},
    ]
    attach_creators(db, cards)
    assert (cards[0]['creator_name'], cards[0]['creator_url']) == ('UP 主', 'https://space.bilibili.com/123')
    assert (cards[1]['creator_name'], cards[1]['creator_url']) == ('站点名', 'https://example.test/')
    assert cards[2]['creator_name'] == '作者 author-id'
    assert cards[2]['creator_url'].endswith('/user/profile/author-id')
