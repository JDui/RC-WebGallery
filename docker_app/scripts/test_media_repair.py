"""Isolated stress regressions for explicit missing-media repair.

Run with ``PYTHONPATH=docker_app python -m unittest docker_app/scripts/test_media_repair.py``
or from docker_app with ``python -m unittest scripts.test_media_repair``.
All databases and media are generated inside TemporaryDirectory instances;
HTTP tests bind only to loopback.
"""
from __future__ import annotations

from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import io
import json
import os
import random
import subprocess
import sys
import threading
import time
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from app.config import load_config
from app.db import Database
from app.services.data_upgrade import DataUpgrade
from app.services.media_repair import MediaRepair
from app.services.storage import StorageService
from app.services.utils import now_iso


def png_bytes(color=(40, 120, 220)):
    stream = io.BytesIO()
    Image.new('RGB', (3, 2), color).save(stream, format='PNG')
    return stream.getvalue()


def truncated_jpeg_bytes():
    randomizer = random.Random(17)
    pixels = bytes(randomizer.randrange(256) for _ in range(512 * 512 * 3))
    stream = io.BytesIO()
    Image.frombytes('RGB', (512, 512), pixels).save(stream, format='JPEG', quality=90)
    encoded = stream.getvalue()
    return encoded[:len(encoded) * 9 // 10]


class LocalHTTP:
    def __init__(self, routes):
        self.routes = routes
        self.counts = {}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.counts[self.path] = outer.counts.get(self.path, 0) + 1
                status, body, content_type = outer.routes.get(
                    self.path, (404, b'not found', 'text/plain'))
                self.send_response(status)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


class MediaRepairCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='media-repair-test-')
        root = Path(self.temp.name)
        config = replace(load_config(), storage_root=root, config_dir=root/'config', data_dir=root/'data',
                         images_dir=root/'data/images', livephoto_dir=root/'data/livephoto',
                         database_path=root/'config/app.db')
        self.storage = StorageService(config)
        self.storage.ensure()
        self.db = Database(config.database_path)
        self.db.init()
        self.storage.db = self.db
        self.repair = MediaRepair(self.db, self.storage)
        self.http = None
        self.site_sources = {}
        self.seed_folder('gallery-folder')

    def tearDown(self):
        if self.http:
            self.http.close()
        self.db.close()
        self.temp.cleanup()

    def seed_folder(self, name):
        self.db.upsert_folder({'folder_name': name, 'title': name, 'pub_ts': 1,
                               'pub_time': '2026-10-20', 'top_dynamic_id': f'top-{name}',
                               'source_dynamic_id': f'src-{name}', 'subscription_uid': '42',
                               'has_images': True})

    def image_path(self, filename='image.png', source='bilibili-42', day='2026-10-20', folder='gallery-folder'):
        return f'data/images/{source}/{day}/{folder}/{filename}'

    def site_path(self, filename='image.png', source='site-a', day='2026-10-20', post='same-slug'):
        return f'data/sites/{source}/{day}/{post}/{filename}'

    def add_gallery_asset(self, rel_path, filename=None, folder='gallery-folder', media_type='image', status='ready'):
        filename = filename or Path(rel_path).name
        if not any(row['folder_name'] == folder for row in self.db.list_folders()):
            self.seed_folder(folder)
        now = now_iso()
        with self.db.connect() as conn:
            cursor = conn.execute('''insert into assets(folder_name,media_type,pair_index,filename,rel_path,status,
                created_at,updated_at) values(?,?,?,?,?,?,?,?)''',
                (folder, media_type, int(time.time_ns() % 2_000_000_000), filename, rel_path, status, now, now))
            return int(cursor.lastrowid)

    def add_site_asset(self, rel_path, url='', source_type='html', filename=None, status='error',
                       source_slug='repair-source', post_slug='repair-post', media_type='image'):
        source = self.site_sources.get(source_slug)
        if source is None:
            source = self.db.create_site_source({'name': source_slug, 'slug': source_slug,
                'source_type': 'html' if source_type == 'xhs_likes' else source_type,
                'entry_url': 'http://127.0.0.1/feed', 'use_proxy': False})
            self.site_sources[source_slug] = source
        post = self.db.upsert_site_post(source['id'], {'title': post_slug, 'slug': post_slug,
            'url': f'http://127.0.0.1/{source_slug}/{post_slug}'})
        asset = self.db.upsert_site_asset(post['id'], {'url': url, 'media_type': media_type,
            'filename': filename or Path(rel_path).name})
        with self.db.connect() as conn:
            if source_type == 'xhs_likes':
                conn.execute('update site_sources set source_type=? where id=?', (source_type, source['id']))
            conn.execute('update site_assets set rel_path=?,status=? where id=?', (rel_path, status, asset['id']))
        return source, post, asset['id']

    def make_http(self, routes):
        self.http = LocalHTTP(routes)
        return self.http

    def add_legacy_local_candidate(self, filename, payload=None, folder='gallery-folder', kind='images'):
        candidate = self.storage.config.data_dir / kind / folder / filename
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_bytes(payload if payload is not None else png_bytes())
        return candidate

    def make_test_video(self):
        try:
            from imageio_ffmpeg import get_ffmpeg_exe
            ffmpeg = get_ffmpeg_exe()
        except (ImportError, RuntimeError):
            self.skipTest('imageio-ffmpeg unavailable for generating the isolated MP4 fixture')
        frame = Path(self.temp.name) / 'video-frame.png'
        video = Path(self.temp.name) / 'valid-fixture.mp4'
        Image.new('RGB', (32, 32), (10, 80, 150)).save(frame, format='PNG')
        result = subprocess.run([ffmpeg, '-v', 'error', '-loop', '1', '-i', str(frame),
                                 '-frames:v', '1', '-c:v', 'mpeg4', '-q:v', '5', '-y', str(video)],
                                capture_output=True, text=True, timeout=30)
        if result.returncode or not video.is_file() or not video.stat().st_size:
            self.skipTest(f'could not generate a small MP4 fixture: {result.stderr[-300:]}')
        return video.read_bytes()

    def test_duplicate_rows_are_excluded_from_649_missing_count_and_preserved(self):
        self.add_gallery_asset(self.image_path('truly-missing.png'))
        source = self.db.create_site_source({'name': 'dups', 'slug': 'dups', 'source_type': 'html',
            'entry_url': 'http://127.0.0.1/feed', 'use_proxy': False})
        post = self.db.upsert_site_post(source['id'], {'title': 'duplicates', 'slug': 'duplicates',
            'url': 'http://127.0.0.1/duplicates'})
        now = now_iso()
        with self.db.connect() as conn:
            conn.executemany('''insert into site_assets(post_id,url,media_type,filename,rel_path,status,created_at,updated_at)
                values(?,?,?,?,?,'duplicate',?,?)''', [(post['id'], f'http://invalid/{i}', 'image', f'{i}.png',
                self.site_path(f'{i}.png'), now, now) for i in range(649)])
        result = self.repair.scan()
        self.assertEqual(result['ignored_duplicates'], 649)
        self.assertEqual(result['checked_assets'], 1)
        self.assertEqual(result['missing_assets'], 1)
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("select count(*) from site_assets where status='duplicate'").fetchone()[0], 649)

    def test_shared_rel_path_in_gallery_and_site_is_downloaded_once(self):
        target = self.site_path('shared.png')
        self.add_gallery_asset(target, 'shared.png')
        http = self.make_http({'/shared.png': (200, png_bytes(), 'image/png')})
        _, _, site_id = self.add_site_asset(target, f'{http.base}/shared.png', filename='shared.png')
        result = self.repair.repair()
        self.assertEqual(result['recovered_files'], 1, self.repair.load())
        self.assertEqual(http.counts.get('/shared.png'), 1)
        self.assertEqual(self.storage.config.storage_root.joinpath(*Path(target).parts).read_bytes(), png_bytes())
        with self.db.connect() as conn:
            self.assertEqual(conn.execute('select count(*) from assets where rel_path=?', (target,)).fetchone()[0], 1)
            self.assertEqual(conn.execute('select status from site_assets where id=?', (site_id,)).fetchone()[0], 'ready')

    def test_local_recovery_matches_same_gallery_content_and_filename(self):
        target = self.image_path('same.png')
        self.add_gallery_asset(target, 'same.png')
        candidate = self.add_legacy_local_candidate('same.png', png_bytes((2, 3, 4)))
        result = self.repair.repair()
        restored = self.storage.config.storage_root.joinpath(*Path(target).parts)
        self.assertEqual(result['recovered_files'], 1)
        self.assertEqual(restored.read_bytes(), png_bytes((2, 3, 4)))

    def test_multiple_local_candidates_are_rejected_without_overwrite(self):
        target = self.image_path('same.png')
        self.add_gallery_asset(target, 'same.png')
        for source, day in (('bilibili-42', '2026-10-21'), ('bilibili-99', '2026-10-22')):
            candidate = self.storage.config.storage_root / f'data/images/{source}/{day}/gallery-folder/same.png'
            candidate.parent.mkdir(parents=True, exist_ok=True)
            candidate.write_bytes(png_bytes())
        result = self.repair.repair()
        self.assertEqual(result['recovered_files'], 0)
        self.assertIn('多个同名', self.repair.load()['items'][0]['error'])
        self.assertFalse(self.storage.config.storage_root.joinpath(*Path(target).parts).exists())

    def test_livephoto_never_uses_same_named_image_candidate(self):
        target = self.image_path('motion.mp4', folder='gallery-folder').replace('/images/', '/livephoto/')
        self.add_gallery_asset(target, 'motion.mp4', media_type='livephoto')
        candidate = self.add_legacy_local_candidate('motion.mp4', b'not-a-video')
        result = self.repair.repair()
        self.assertEqual(result['recovered_files'], 0)
        self.assertFalse(self.storage.config.storage_root.joinpath(*Path(target).parts).exists())

    def test_same_folder_slug_on_different_date_is_not_a_local_match(self):
        target = self.image_path('same.png', day='2026-10-20')
        self.add_gallery_asset(target, 'same.png')
        wrong_date = self.storage.config.storage_root / 'data/images/bilibili-42/2026-09-01/gallery-folder/same.png'
        wrong_date.parent.mkdir(parents=True, exist_ok=True)
        wrong_date.write_bytes(png_bytes((90, 2, 3)))
        result = self.repair.repair()
        self.assertEqual(result['recovered_files'], 0, 'same slug from another date must not be guessed as the original')
        self.assertFalse(self.storage.config.storage_root.joinpath(*Path(target).parts).exists())

    def test_same_post_slug_from_another_site_is_not_a_local_match(self):
        target = self.site_path('same.png', source='site-a')
        self.add_site_asset(target, '', source_slug='source-a', post_slug='same-slug')
        wrong_site = self.storage.config.storage_root / 'data/sites/site-b/2026-10-20/same-slug/same.png'
        wrong_site.parent.mkdir(parents=True, exist_ok=True)
        wrong_site.write_bytes(png_bytes())
        result = self.repair.repair()
        self.assertEqual(result['recovered_files'], 0)
        self.assertFalse(self.storage.config.storage_root.joinpath(*Path(target).parts).exists())

    def test_http_valid_image_recovers_and_html_404_empty_and_missing_url_do_not(self):
        http = self.make_http({
            '/good.png': (200, png_bytes(), 'image/png'),
            '/html.png': (200, b'<html>not an image</html>', 'text/html'),
            '/404.png': (404, b'not found', 'text/plain'),
            '/empty.png': (200, b'', 'image/png'),
        })
        ids = {}
        for name, url in [('good.png', f'{http.base}/good.png'), ('html.png', f'{http.base}/html.png'),
                          ('404.png', f'{http.base}/404.png'), ('empty.png', f'{http.base}/empty.png'),
                          ('no-url.png', '')]:
            path = self.site_path(name, post=f'post-{name}')
            _, _, ids[name] = self.add_site_asset(path, url, filename=name, post_slug=f'post-{name}')
        result = self.repair.repair()
        self.assertEqual(result['recovered_files'], 1)
        self.assertEqual(http.counts, {'/good.png': 1, '/html.png': 1, '/404.png': 1, '/empty.png': 1})
        self.assertEqual(self.storage.config.storage_root.joinpath(*Path(self.site_path('good.png', post='post-good.png')).parts).read_bytes(), png_bytes())
        items = {row['filename']: row for row in self.repair.load()['items']}
        for name in ('html.png', '404.png', 'empty.png', 'no-url.png'):
            self.assertEqual(items[name]['status'], 'unresolved')

    def test_truncated_jpeg_that_passes_header_verify_is_rejected_after_full_decode(self):
        payload = truncated_jpeg_bytes()
        with Image.open(io.BytesIO(payload)) as image:
            image.verify()
        with Image.open(io.BytesIO(payload)) as image:
            with self.assertRaises(OSError):
                image.load()

        http = self.make_http({'/truncated.jpg': (200, payload, 'image/jpeg')})
        target = self.site_path('truncated.jpg', post='truncated-jpeg')
        self.add_site_asset(target, f'{http.base}/truncated.jpg', filename='truncated.jpg',
                            post_slug='truncated-jpeg')
        result = self.repair.repair()
        restored = self.storage.config.storage_root.joinpath(*Path(target).parts)
        self.assertEqual(result['recovered_files'], 0)
        self.assertFalse(restored.exists())
        self.assertEqual(http.counts.get('/truncated.jpg'), 1)
        self.assertEqual(self.repair.load()['items'][0]['status'], 'unresolved')

    def test_valid_mp4_is_recovered_and_fake_video_is_rejected(self):
        video_bytes = self.make_test_video()
        http = self.make_http({
            '/valid.mp4': (200, video_bytes, 'video/mp4'),
            '/fake.mp4': (200, b'<html>not a movie</html>', 'video/mp4'),
        })
        valid_path = self.site_path('valid.mp4', post='valid-video')
        fake_path = self.site_path('fake.mp4', post='fake-video')
        self.add_site_asset(valid_path, f'{http.base}/valid.mp4', filename='valid.mp4',
                            post_slug='valid-video', media_type='video')
        self.add_site_asset(fake_path, f'{http.base}/fake.mp4', filename='fake.mp4',
                            post_slug='fake-video', media_type='video')
        result = self.repair.repair()
        self.assertEqual(result['recovered_files'], 1)
        self.assertEqual(self.storage.config.storage_root.joinpath(*Path(valid_path).parts).read_bytes(), video_bytes)
        self.assertFalse(self.storage.config.storage_root.joinpath(*Path(fake_path).parts).exists())
        items = {row['filename']: row for row in self.repair.load()['items']}
        self.assertEqual(items['valid.mp4']['status'], 'recovered')
        self.assertEqual(items['fake.mp4']['status'], 'unresolved')

    def test_expired_xhs_history_url_is_never_requested(self):
        http = self.make_http({'/expired.png': (200, png_bytes(), 'image/png')})
        target = self.site_path('expired.png')
        self.add_site_asset(target, f'{http.base}/expired.png', source_type='xhs_likes')
        result = self.repair.repair()
        self.assertEqual(result['recovered_files'], 0)
        self.assertEqual(http.counts, {})
        self.assertIn('小红书来源', self.repair.load()['items'][0]['error'])

    def test_existing_and_zero_byte_targets_are_never_replaced(self):
        existing = self.site_path('existing.png', post='existing')
        zero = self.site_path('zero.png', post='zero')
        http = self.make_http({'/existing.png': (200, png_bytes((255, 0, 0)), 'image/png'),
                               '/zero.png': (200, png_bytes(), 'image/png')})
        self.add_site_asset(existing, f'{http.base}/existing.png', filename='existing.png')
        self.add_site_asset(zero, f'{http.base}/zero.png', filename='zero.png')
        existing_path = self.storage.config.storage_root.joinpath(*Path(existing).parts)
        zero_path = self.storage.config.storage_root.joinpath(*Path(zero).parts)
        existing_path.parent.mkdir(parents=True, exist_ok=True)
        existing_path.write_bytes(b'keep-existing')
        zero_path.parent.mkdir(parents=True, exist_ok=True)
        zero_path.write_bytes(b'')
        result = self.repair.repair()
        self.assertEqual(existing_path.read_bytes(), b'keep-existing')
        self.assertEqual(zero_path.read_bytes(), b'')
        self.assertEqual(http.counts.get('/existing.png', 0), 0)
        self.assertEqual(http.counts.get('/zero.png', 0), 0)
        self.assertEqual(result['recovered_files'], 0)

    def test_invalid_traversal_and_symlink_paths_do_not_write_outside_storage(self):
        outside = Path(self.temp.name).parent / f'{Path(self.temp.name).name}-outside.png'
        outside.unlink(missing_ok=True)
        traversal = 'data/sites/site-a/2026-10-20/post/../../../../outside.png'
        self.add_site_asset(traversal, '', post_slug='traversal')
        cases = [traversal]
        outside.write_bytes(b'outside')
        if hasattr(Path, 'is_junction'):
            final_rel = self.site_path('final-link.png', post='links')
            final = self.storage.config.storage_root.joinpath(*Path(final_rel).parts)
            final.parent.mkdir(parents=True, exist_ok=True)
            parent_rel = self.site_path('parent-link.png', post='parent-link')
            parent = self.storage.config.storage_root.joinpath(*Path(parent_rel).parts).parent
            parent.parent.mkdir(parents=True, exist_ok=True)
            try:
                final.symlink_to(outside)
                parent.symlink_to(outside.parent, target_is_directory=True)
            except (OSError, NotImplementedError):
                final.unlink(missing_ok=True)
                parent.unlink(missing_ok=True)
            else:
                self.add_site_asset(final_rel, '', post_slug='final-link')
                self.add_site_asset(parent_rel, '', post_slug='parent-link')
                cases.extend((final_rel, parent_rel))
        try:
            self.repair.repair()
            self.assertEqual(outside.read_bytes(), b'outside')
            self.assertFalse((Path(self.temp.name).parent / 'outside.png').exists())
        finally:
            outside.unlink(missing_ok=True)
        loaded = self.repair.load()
        self.assertEqual(len(loaded['items']), len(cases), loaded)
        self.assertTrue(all(item['status'] == 'unresolved' for item in loaded['items']))

    def test_network_failure_is_single_attempt_and_report_keeps_full_detail(self):
        http = self.make_http({'/fail.png': (500, b'failure', 'text/plain')})
        for n in range(25):
            path = self.site_path(f'{n}.png', post=f'item-{n}')
            self.add_site_asset(path, f'{http.base}/fail.png', filename=f'{n}.png', post_slug=f'item-{n}')
        report = self.repair.scan()
        self.assertEqual(report['missing_assets'], 25)
        self.assertEqual(len(self.repair.load()['items']), 25)
        self.assertEqual(len(report['missing_samples']), 20)
        self.repair.repair()
        self.assertEqual(http.counts.get('/fail.png'), 25)
        self.assertEqual(len(self.repair.load()['items']), 25)

    def test_retry_after_precommit_failure_recovers_only_on_explicit_retry(self):
        target = self.site_path('retry.png')
        http = self.make_http({'/retry.png': (200, png_bytes((1, 5, 9)), 'image/png')})
        self.add_site_asset(target, f'{http.base}/retry.png')
        real_link = __import__('os').link
        with patch('app.services.media_repair.os.link', side_effect=OSError('injected before commit')):
            first = self.repair.repair()
        self.assertEqual(first['recovered_files'], 0)
        path = self.storage.config.storage_root.joinpath(*Path(target).parts)
        self.assertFalse(path.exists())
        second = self.repair.repair()
        self.assertEqual(second['recovered_files'], 1)
        self.assertEqual(path.read_bytes(), png_bytes((1, 5, 9)))
        self.assertEqual(http.counts.get('/retry.png'), 2)
        self.assertTrue(callable(real_link))

    def test_retry_after_link_before_reference_update_reconciles_database(self):
        target = self.site_path('commit-gap.png')
        http = self.make_http({'/commit-gap.png': (200, png_bytes((8, 4, 1)), 'image/png')})
        _, _, asset_id = self.add_site_asset(target, f'{http.base}/commit-gap.png', status='error')
        real_update = self.db.set_site_asset_result

        def fail_reference_update(*_args, **_kwargs):
            raise RuntimeError('injected after filesystem commit, before reference update')

        # Isolate the post-link crash window even on Windows, where the separate
        # read-only-handle fsync compatibility check is reported by other tests.
        with patch('app.services.media_repair.os.fsync', return_value=None), \
             patch.object(self.db, 'set_site_asset_result', fail_reference_update):
            first = self.repair.repair()
        path = self.storage.config.storage_root.joinpath(*Path(target).parts)
        self.assertEqual(first['recovered_files'], 0)
        self.assertEqual(path.read_bytes(), png_bytes((8, 4, 1)))
        second = self.repair.repair()
        with self.db.connect() as conn:
            status = conn.execute('select status from site_assets where id=?', (asset_id,)).fetchone()[0]
        self.assertEqual(second['missing_files'], 0)
        self.assertEqual(status, 'ready', 'retry should reconcile the committed file with its database reference')
        self.assertEqual(http.counts.get('/commit-gap.png'), 1)
        self.assertTrue(callable(real_update))

    @unittest.skipUnless(os.name == 'posix', 'process-exit durability case runs in Linux only')
    def test_process_exit_after_atomic_link_reconciles_without_redownload(self):
        target = self.site_path('process-exit.png', post='process-exit')
        expected = png_bytes((14, 28, 42))
        http = self.make_http({'/process-exit.png': (200, expected, 'image/png')})
        _, _, asset_id = self.add_site_asset(target, f'{http.base}/process-exit.png',
                                             filename='process-exit.png', post_slug='process-exit',
                                             status='error')
        marker = Path(self.temp.name) / 'after-link.marker'
        child = r'''
import os
import sys
from dataclasses import replace
from pathlib import Path
from app.config import load_config
from app.db import Database
from app.services.storage import StorageService
from app.services.media_repair import MediaRepair

root = Path(sys.argv[1])
config = replace(load_config(), storage_root=root, config_dir=root/'config', data_dir=root/'data',
                 images_dir=root/'data/images', livephoto_dir=root/'data/livephoto',
                 database_path=root/'config/app.db')
db = Database(config.database_path)
storage = StorageService(config)
storage.ensure()
storage.db = db
repair = MediaRepair(db, storage)
real_link = os.link
def exit_after_link(source, destination):
    real_link(source, destination)
    Path(sys.argv[2]).write_text('linked', encoding='utf-8')
    os._exit(86)
os.link = exit_after_link
repair.repair()
os._exit(0)
'''
        child_result = subprocess.run([sys.executable, '-c', child, self.temp.name, str(marker)],
                                       capture_output=True, text=True, timeout=60)
        self.assertEqual(child_result.returncode, 86,
                         f'child should terminate immediately after os.link: {child_result.stderr}')
        self.assertEqual(marker.read_text(encoding='utf-8'), 'linked')

        restored = self.storage.config.storage_root.joinpath(*Path(target).parts)
        self.assertEqual(restored.read_bytes(), expected)
        self.db.close()
        self.db = Database(self.storage.config.database_path)
        self.db.init()
        self.storage.db = self.db
        self.repair = MediaRepair(self.db, self.storage)
        result = self.repair.repair()

        with self.db.connect() as conn:
            status = conn.execute('select status from site_assets where id=?', (asset_id,)).fetchone()[0]
        self.assertEqual(result['recovered_files'], 1, self.repair.load())
        self.assertEqual(result['missing_files'], 0, self.repair.load())
        self.assertEqual(status, 'ready')
        self.assertEqual(restored.read_bytes(), expected)
        self.assertEqual(http.counts.get('/process-exit.png'), 1,
                         'restart reconciliation must not fetch the already-linked file again')

    def test_data_upgrade_repair_action_preserves_layout_version_and_publishes_completion(self):
        target = self.image_path('managed.png')
        self.add_gallery_asset(target, 'managed.png')
        self.add_legacy_local_candidate('managed.png')
        manager = SimpleNamespace(_queue_lock=threading.Lock(), _queue=[], _status={},
            _acquire=lambda *_args: True, _release=lambda: None, _run_next_queued_task=lambda: None)
        upgrade = DataUpgrade(self.db, self.storage, None, manager)
        upgrade.version = 2
        with self.db.connect() as conn:
            before_version = conn.execute('select version from data_layout where id=1').fetchone()[0]
        upgrade.state.update(status='running', action='repair', progress=0)
        upgrade._run()
        with self.db.connect() as conn:
            after_version = conn.execute('select version from data_layout where id=1').fetchone()[0]
        self.assertEqual(upgrade.state['status'], 'completed', upgrade.state)
        self.assertFalse(manager._status['running'])
        self.assertEqual(before_version, after_version)
        self.assertEqual(upgrade.state['result']['recovered_files'], 1)

    def test_data_upgrade_repair_error_preserves_report_and_can_start_again(self):
        target = self.image_path('retry-managed.png')
        self.add_gallery_asset(target, 'retry-managed.png')
        self.add_legacy_local_candidate('retry-managed.png')
        manager_lock = threading.Lock()
        acquired = threading.Lock()
        manager = SimpleNamespace(_queue_lock=manager_lock, _queue=[], _status={},
            _acquire=lambda *_args: acquired.acquire(blocking=False),
            _release=lambda: acquired.release() if acquired.locked() else None,
            _run_next_queued_task=lambda: None)
        upgrade = DataUpgrade(self.db, self.storage, None, manager)
        original_repair = upgrade.media_repair.repair
        upgrade.media_repair.scan()
        with patch.object(upgrade.media_repair, 'repair', side_effect=RuntimeError('injected repair failure')):
            upgrade.state.update(status='running', action='repair', progress=5)
            upgrade._run()
        self.assertEqual(upgrade.state['status'], 'failed')
        self.assertEqual(len(upgrade.media_repair.load()['items']), 1)

        class InlineThread:
            def __init__(self, target, daemon=None):
                self.target = target
            def start(self):
                self.target()

        with patch('app.services.data_upgrade.threading.Thread', InlineThread):
            with patch.object(upgrade.media_repair, 'repair', original_repair):
                result = upgrade.start('repair')
        self.assertEqual(result['action'], 'repair')
        self.assertEqual(upgrade.state['status'], 'completed', upgrade.state)
        self.assertFalse(manager._status['running'])
        self.assertEqual(upgrade.state['result']['recovered_files'], 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
