"""Isolated data-layout regressions; run with PYTHONPATH=docker_app and unittest discovery in docker_app/scripts."""
from __future__ import annotations

from dataclasses import replace
from contextlib import closing, contextmanager
from pathlib import Path
import asyncio
from types import SimpleNamespace
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from app.config import load_config
from app.db import Database
from app.services.database_layout import split_modules
from app.services.data_upgrade import DataUpgrade, DataUpgradeMiddleware
from app.services.storage import StorageService
from app.services.utils import dumps_json


class LayoutCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        config = replace(load_config(), storage_root=root, config_dir=root/'config', data_dir=root/'data',
                         images_dir=root/'data/images', livephoto_dir=root/'data/livephoto', database_path=root/'config/app.db')
        self.storage = StorageService(config)
        self.storage.ensure()
        self.db = Database(config.database_path)
        self.db.init()
        self.storage.db = self.db
        lock = threading.Lock()
        self.manager = SimpleNamespace(_queue_lock=threading.Lock(), _queue=[],
                                       _acquire=lambda *a: lock.acquire(blocking=False),
                                       _release=lambda: lock.release() if lock.locked() else None,
                                       _run_next_queued_task=lambda: None)
        self.upgrade = DataUpgrade(self.db, self.storage, None, self.manager)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def seed(self, count=8, pairs=3):
        for n in range(count):
            folder = dict(folder_name=f'folder-{n}', title=f'条目 {n}', pub_ts=n//2, pub_time='2026-10-20',
                          top_dynamic_id=str(n), source_dynamic_id=str(n), subscription_uid='42',
                          has_images=True, created_at=f'2026-10-{n+1:02d}')
            self.db.upsert_folder(folder)
            self.db.replace_gallery_index(folder['folder_name'], {**folder, 'review_status': 'approved'},
                [dict(item_key=f'{n}-{i}', pair_index=i, pub_ts=n//2, pub_time=folder['pub_time'], has_image=True) for i in range(pairs)])
        self.db.mark_gallery_index_rebuilt()

    def test_task_inspector_tracks_progress_and_completion(self):
        events = []
        self.manager._status = {'mode': 'data-upgrade', 'running': True, 'progress': 1,
                                'message': '升级数据结构', 'next_step': '开始执行'}
        self.manager._append_event = lambda message, **values: events.append((message, values))
        self.upgrade.report('正在迁移媒体目录', status='running', action='upgrade', progress=40, current='测试目录')
        status = self.manager._status
        self.assertEqual(status['progress'], 40)
        self.assertEqual(status['current_step'], '正在迁移媒体目录')
        self.assertEqual(status['current_source'], '测试目录')
        self.assertEqual(status['task_detail']['doing'], status['current_step'])
        self.upgrade.report('数据结构升级与目录迁移完成', status='completed', progress=100, current=None)
        status = self.manager._status
        self.assertFalse(status['running'])
        self.assertEqual((status['processed'], status['total']), (100, 100))
        self.assertEqual(status['next_step'], '任务完成')
        self.assertEqual(status['current_source'], '')
        self.assertEqual(events[-1][0], '数据结构升级与目录迁移完成')

    def test_task_inspector_reports_failure_without_claiming_completion(self):
        self.upgrade.report('正在检查媒体引用', status='running', action='check', progress=95)
        self.upgrade.report('升级停止，可修正问题后继续', status='failed', error='test-only failure')
        status = self.manager._status
        self.assertFalse(status['running'])
        self.assertEqual(status['target'], '检查数据')
        self.assertEqual(status['error'], 'test-only failure')
        self.assertEqual(status['progress'], 95)
        self.assertEqual(status['next_step'], '修正问题后继续')
        self.assertTrue(self.upgrade.blocked)

    def test_idle_health_statistics_do_not_rescan_or_write(self):
        self.seed()
        split_modules(self.db, lambda _: None)
        self.db.keep_hot()
        expected_index = self.db.gallery_index_status()
        expected_sites = self.db.site_stats()
        statements = []
        original_connect = self.db.connect
        @contextmanager
        def traced_connect():
            with original_connect() as conn:
                conn.set_trace_callback(statements.append)
                yield conn
        with patch.object(self.db, 'connect', traced_connect):
            for _ in range(50):
                self.assertEqual(self.db.gallery_index_status(), expected_index)
                self.assertEqual(self.db.site_stats(), expected_sites)
        sql = '\n'.join(statements).lower()
        self.assertNotIn('count(', sql)
        self.assertNotIn('group by', sql)
        for statement in statements:
            self.assertFalse(statement.lstrip().lower().startswith(('insert ', 'update ', 'delete ', 'replace ')), statement)

    def test_health_statistics_invalidate_on_content_and_setting_changes(self):
        self.seed(1, 1)
        split_modules(self.db, lambda _: None)
        before = self.db.gallery_index_status()
        self.assertEqual(before['asset_pair_rows'], 0)
        self.db.replace_folder_assets('folder-0', 'image', [
            {'filename':'one.jpg', 'rel_path':'data/images/folder-0/one.jpg', 'pair_index':1}])
        after = self.db.gallery_index_status()
        self.assertEqual(after['asset_pair_rows'], 1)
        self.assertFalse(after['stale'])
        self.db.save_settings({'gallery_index_rebuilding': True, 'gallery_index_version': 0})
        after = self.db.gallery_index_status()
        self.assertTrue(after['rebuilding'])
        self.assertTrue(after['stale'])
        self.assertEqual(self.db.site_stats()['source_count'], 0)
        source = self.db.create_site_source({'name':'fixture', 'entry_url':'https://example.invalid/feed'})
        self.assertEqual(self.db.site_stats()['source_count'], 1)
        self.db.upsert_site_post(source['id'], {'title':'fixture', 'url':'https://example.invalid/post'})
        self.assertEqual(self.db.site_stats()['post_count'], 1)

    def test_idle_anchors_keep_module_wals_open_without_read_poll_writes(self):
        split_modules(self.db, lambda _: None)
        self.db.keep_hot()
        paths = [self.db.db_path.with_name(f'{name}.db') for name in ('app', 'browse', 'control')]
        for n in range(10):
            self.db.save_settings({'app_title': str(n)})
            with self.db.connect() as conn:
                conn.execute('update content_revision set revision=revision+1 where id=1')
                conn.execute('update browse.content_revision set revision=revision+1 where id=1')
        files = [p for path in paths for p in (path, Path(str(path)+'-wal'), Path(str(path)+'-shm'))]
        self.assertTrue(all(path.is_file() for path in files))
        before = {path: (path.stat().st_size, path.stat().st_mtime_ns) for path in files}
        self.db.gallery_index_status()
        self.db.site_stats()
        for _ in range(20):
            self.db.gallery_index_status()
            self.db.site_stats()
        after = {path: (path.stat().st_size, path.stat().st_mtime_ns) for path in files}
        self.assertEqual(before, after)

    def test_split_preserves_control_and_restart(self):
        self.seed()
        self.db.save_settings({'app_title': '大型图库'})
        self.db.upsert_subscription('42', '测试订阅')
        self.db.update_auth_state(cookie='test-only', user_json='{}')
        split_modules(self.db, lambda _: None)
        self.db = Database(self.db.db_path)
        self.db.init()
        self.assertTrue(self.db.modules_enabled)
        self.assertEqual(self.db.get_settings()['app_title'], '大型图库')
        self.assertEqual(self.db.get_subscription('42')['uname'], '测试订阅')
        self.assertEqual(self.db.get_auth_state()['cookie'], 'test-only')
        self.db.set_folder_favorite('folder-0', True)
        self.assertEqual(self.db.query_folder_index(category='favorites')['total'], 1)
        self.db.delete_folder('folder-0')
        self.assertEqual(self.db.query_pair_index()['total'], 21)
        with closing(sqlite3.connect(self.db.db_path)) as conn:
            self.assertIsNone(conn.execute("select name from sqlite_master where name='settings'").fetchone())
        # Settings reads are physically independent of app.db and browse.db.
        with self.db.control_connect() as conn:
            self.assertEqual(len(conn.execute('pragma database_list').fetchall()), 1)

    def test_split_failure_rolls_back_and_retries(self):
        self.db.save_settings({'app_title': 'before'})
        def fail(_):
            raise RuntimeError('injected copy interruption')
        with self.assertRaisesRegex(RuntimeError, 'injected'):
            split_modules(self.db, fail)
        self.assertFalse(self.db.modules_enabled)
        self.assertEqual(self.db.get_settings()['app_title'], 'before')
        split_modules(self.db, lambda _: None)
        self.assertEqual(self.db.get_settings()['app_title'], 'before')

    def test_both_time_orders_and_seek_boundaries(self):
        self.seed()
        split_modules(self.db, lambda _: None)
        for table, query in (('folder_index', self.db.query_folder_index), ('pair_index', self.db.query_pair_index)):
            for mode in ('desc', 'asc', 'imported'):
                column = 'imported_at' if mode == 'imported' else 'pub_ts'
                direction = 'asc' if mode == 'asc' else 'desc'
                order = f'{column} {direction},folder_name {direction}' + (',pair_index asc' if table=='pair_index' else '')
                with self.db.connect() as conn:
                    expected = [tuple(row) for row in conn.execute(f'select folder_name' + (',pair_index' if table=='pair_index' else '') + f' from {table} order by {order}')]
                    plan = ' '.join(str(tuple(row)) for row in conn.execute(f'explain query plan select * from {table} order by {order} limit 5'))
                self.assertNotIn('TEMP B-TREE', plan)
                actual = []
                for page in range(1, (len(expected)+4)//5+1):
                    result = query(sort_order=mode, page=page, page_size=5)
                    actual.extend((row['folder_name'], row['pair_index']) if table=='pair_index' else (row['folder_name'],) for row in result['items'])
                self.assertEqual(actual, expected, (table, mode))
        first = self.db.gallery_page_cache_signature()
        self.db.set_folder_favorite('folder-0', True)
        self.assertNotEqual(first, self.db.gallery_page_cache_signature())
        self.assertEqual(self.db.query_folder_index(category='favorites')['total'], 1)

    def seed_files(self):
        self.seed(1, 1)
        image = self.storage.image_folder('folder-0')/'001__image.jpg'
        video = self.storage.livephoto_folder('folder-0')/'001__video.mov'
        image.parent.mkdir(parents=True)
        video.parent.mkdir(parents=True)
        image.write_bytes(b'original-image')
        video.write_bytes(b'original-video')
        self.db.replace_folder_assets('folder-0', 'image', [{'filename':image.name, 'rel_path':self.storage.relative_to_storage(image), 'pair_index':1}])
        with self.db.connect() as conn:
            conn.execute("insert into livephoto_pairs(folder_name,pair_index,content_id,image_path,video_path,updated_at,transaction_json) values(?,?,?,?,?,?,?)",
                         ('folder-0',1,'stable-content-id',self.storage.relative_to_storage(image), self.storage.relative_to_storage(video), 'today', dumps_json({'path':self.storage.relative_to_storage(video)})))
        return image, video

    def test_full_migration_reference_identity_backup_and_new_downloads(self):
        image, _ = self.seed_files()
        source = self.db.create_site_source({'name':'fixture', 'entry_url':'https://example.invalid/feed'})
        post = self.db.upsert_site_post(source['id'], {'title':'fixture', 'url':'https://example.invalid/post'})
        site_asset = self.db.upsert_site_asset(post['id'], {'url':'https://example.invalid/image','filename':image.name,'media_type':'image'})
        self.db.set_site_asset_result(site_asset['id'], 'ready', self.storage.relative_to_storage(image))
        healthy = self.storage.site_post_folder('healthy', '2026-10-20', 'post')/'kept.jpg'
        healthy.write_bytes(b'healthy-site-content')
        self.upgrade._run()
        self.assertEqual(self.upgrade.state['status'], 'completed', self.upgrade.state)
        image = self.storage.image_folder('folder-0')/'001__image.jpg'
        video = self.storage.livephoto_folder('folder-0')/'001__video.mov'
        self.assertEqual(image.read_bytes(), b'original-image')
        self.assertEqual(video.read_bytes(), b'original-video')
        self.assertIn('bilibili-42/2026-10-20', image.as_posix())
        asset = self.db.list_assets_for_folder('folder-0')[0]
        self.assertTrue(self.storage.resolve_storage_path(asset['rel_path']).is_file())
        self.assertTrue(self.storage.resolve_storage_path(self.db.list_site_assets(post['id'])[0]['rel_path']).is_file())
        self.assertEqual(healthy.read_bytes(), b'healthy-site-content')
        with self.db.connect() as conn:
            pair = dict(conn.execute('select * from livephoto_pairs').fetchone())
        self.assertEqual(pair['content_id'], 'stable-content-id')
        self.assertIn('bilibili-42', pair['transaction_json'])
        self.assertTrue((self.storage.config.config_dir/'before-data-layout-v2/app.db').is_file())
        self.storage.prepare_folder('new-content', 'xhs-4', '2026-10-21')
        self.assertIn('images/xhs-4/2026-10-21/new-content', self.storage.image_folder('new-content').as_posix())
        self.assertEqual(self.storage.media_folder_names(), {'folder-0'})
        restarted = DataUpgrade(Database(self.db.db_path), self.storage, None, self.manager)
        self.assertFalse(restarted.blocked)

    def test_resume_after_rename_before_reference_commit(self):
        image, _ = self.seed_files()
        target = self.storage.layout_folder('images', 'folder-0', 'bilibili-42', '2026-10-20')
        with self.db.connect() as conn:
            conn.execute('create table storage_moves(old_dir text primary key,new_dir text,folder_name text,media_type text,done integer default 0)')
            conn.execute('insert into storage_moves values(?,?,?,?,0)', (self.storage.relative_to_storage(image.parent), self.storage.relative_to_storage(target),'folder-0','images'))
        target.parent.mkdir(parents=True)
        image.parent.rename(target)
        self.upgrade._run()
        self.assertEqual(self.upgrade.state['status'], 'completed', self.upgrade.state)
        self.assertTrue(self.storage.resolve_storage_path(self.db.list_assets_for_folder('folder-0')[0]['rel_path']).is_file())

    def test_conflicting_destination_is_retained(self):
        image, _ = self.seed_files()
        target = self.storage.layout_folder('images', 'folder-0', 'bilibili-42', '2026-10-20')
        target.mkdir(parents=True)
        (target/'different.jpg').write_bytes(b'do-not-overwrite')
        self.upgrade._run()
        self.assertTrue(self.upgrade.blocked)
        self.assertIn('目标目录已存在', self.upgrade.state['error'])
        self.assertTrue(image.exists())
        self.assertEqual((target/'different.jpg').read_bytes(), b'do-not-overwrite')

    def test_tiny_cleanup_keeps_avatars_and_other_derivatives(self):
        paths = [self.storage.config.images_dir/'post/.thumbs/tiny/image.webp',
                 self.storage.config.images_dir/'post/.thumbs/small/image.webp',
                 self.storage.config.data_dir/'sites/source/date/post/.thumbs/tiny/old.webp',
                 self.storage.config.data_dir/'sites/source/date/post/.thumbs/tiny/avatar.webp',
                 self.storage.config.data_dir/'avatars/tiny/42.webp']
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'bytes')
        self.db.upsert_subscription('42', avatar_tiny_url=self.storage.storage_url(self.storage.relative_to_storage(paths[3])))
        stats = self.storage.storage_usage_stats()
        self.assertEqual(stats['trash_files'], 2)
        self.assertEqual(stats['thumbnail_files'], 2)
        result = self.storage.cleanup_trash_asset_files([])
        self.assertEqual(result['removed_files'], 2)
        self.assertFalse(paths[0].exists())
        self.assertFalse(paths[2].exists())
        self.assertTrue(all(paths[n].exists() for n in (1,3,4)))

    def test_takeover_drain_and_restart_recovery(self):
        self.assertTrue(self.upgrade.enter())
        self.upgrade.report('in progress', status='running', progress=30)
        self.assertFalse(self.upgrade.enter())
        self.upgrade.leave()
        restored = DataUpgrade(self.db, self.storage, None, self.manager)
        self.assertTrue(restored.blocked)
        self.assertEqual(restored.state['status'], 'interrupted')
        self.assertEqual(restored.status()['progress'], 30)

    def test_check_reports_missing_without_upgrading_data(self):
        image, _ = self.seed_files()
        image.unlink()
        self.upgrade.state['action'] = 'check'
        self.upgrade._run()
        self.assertEqual(self.upgrade.state['status'], 'completed')
        self.assertEqual(self.upgrade.state['result']['missing_assets'], 1)
        self.assertFalse(self.db.modules_enabled)
        self.assertFalse((self.storage.config.config_dir/'before-data-layout-v2').exists())

    def test_gate_tracks_the_entire_response_lifetime(self):
        async def handler(scope, receive, send):
            self.assertEqual(self.upgrade.requests, 1)
            await send({'type':'http.response.start','status':200,'headers':[]})
            await send({'type':'http.response.body','body':b'ok'})
            # A response background task still has exclusive request ownership.
            self.assertEqual(self.upgrade.requests, 1)
        sent = []
        async def send(message):
            sent.append(message)
        gate = DataUpgradeMiddleware(handler, self.upgrade, None, 'fixture')
        scope = {'type':'http','path':'/api/settings','method':'GET','headers':[]}
        asyncio.run(gate(scope, None, send))
        self.assertEqual(self.upgrade.requests, 0)
        self.upgrade.state['status'] = 'running'
        sent.clear()
        asyncio.run(gate(scope, None, send))
        self.assertEqual(sent[0]['status'], 503)


if __name__ == '__main__':
    unittest.main()
