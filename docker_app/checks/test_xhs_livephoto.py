"""Run on the Linux app runtime: python -m unittest discover -s docker_app/checks."""
import asyncio
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
import hashlib
import io
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from PIL import Image
import imageio_ffmpeg

from app.config import load_config
from app.db import Database
from app.services.gallery import GalleryService
from app.services.media_indexer import MediaIndexer
from app.services.storage import StorageService
from app.services.thumbnailer import ThumbnailService
from app.services.xhs.manager import XhsManager
from app.services.xhs.models import XhsNote
from app.services.xhs.schema import migrate
from app.services.xhs.sync import ScanPage, XhsSyncService
from app.services.xhs.worker_client import WorkerError
from xhs_worker.media import MediaRejected
from xhs_worker.main import create_app
from xhs_worker.parser import extract_live_candidates, normalize_note
from xhs_worker.runtime import BrowserFault, BrowserRuntime


def card():
    # Sanitized shape of the supplied five-image share; no expiring URLs/tokens.
    return {'noteId': 'live-note', 'type': 'normal', 'title': 'Live Photo sample',
            'imageList': [{'urlDefault': f'https://sns-webpic.xhscdn.com/{i}.jpg',
                           'livePhoto': True,
                           'stream': {'h264': [{'masterUrl': f'https://sns-video-v2.xhscdn.com/{i}.mp4',
                                               'backupUrls': [f'https://sns-bak-v1.xhscdn.com/{i}.mp4']}],
                                      'h265': [], 'h266': [], 'av1': []}}
                          for i in range(5)]}


def resolved(raw, missing=()):
    note, _ = normalize_note(raw, detail=True)
    return {'note_id': note['note_id'], 'note': note, 'assets': note['assets'],
            'media_complete': not missing, 'missing_livephoto_ordinals': list(missing)}


class ParserTests(unittest.TestCase):
    def test_five_pairs_and_backups(self):
        raw = card()
        note, urls = normalize_note(raw, detail=True)
        self.assertEqual(note['type'], 'livephoto')
        self.assertEqual(len(note['assets']), 10)
        self.assertEqual(len(urls), 10)
        self.assertEqual(len(XhsNote.parse(note).assets), 10)
        self.assertEqual(len(extract_live_candidates(raw['imageList'][0])), 2)

    def test_mixed_stills_and_motion(self):
        raw = card()
        raw['imageList'][1].pop('stream')
        raw['imageList'][1]['livePhoto'] = False
        note, _ = normalize_note(raw)
        self.assertEqual(sum(a['kind'] == 'livephoto' for a in note['assets']), 4)

    def test_missing_stream_retains_expected_motion(self):
        raw = card()
        raw['imageList'][0]['stream'] = {}
        note, urls = normalize_note(raw)
        self.assertIn({'ordinal': 0, 'kind': 'livephoto'}, note['assets'])
        self.assertFalse(any(i == 0 and kind == 'livephoto' for i, kind, _ in urls))

    def test_nested_legacy_stream_and_direct_url(self):
        for key in ('live_photo', 'livePhoto', 'video'):
            image = {'urlDefault': 'https://sns-webpic.xhscdn.com/still.jpg',
                     key: {'videoUrl': 'https://sns-video-v2.xhscdn.com/direct.mp4'}}
            self.assertEqual(extract_live_candidates(image)[0][1], 'https://sns-video-v2.xhscdn.com/direct.mp4')
            image[key] = {'stream': card()['imageList'][0]['stream']}
            self.assertEqual(len(extract_live_candidates(image)), 2)

    def test_ordinary_video_cover_is_not_livephoto(self):
        raw = card()
        raw['type'] = 'video'
        raw['video'] = {'media': {'stream': raw['imageList'][0]['stream']}}
        note, _ = normalize_note(raw)
        self.assertEqual(note['type'], 'video')
        self.assertFalse(any(a['kind'] == 'livephoto' for a in note['assets']))

    def test_plain_image_note(self):
        raw = card()
        for image in raw['imageList']:
            image.pop('stream')
            image.pop('livePhoto')
        note, _ = normalize_note(raw)
        self.assertEqual(note['type'], 'image')
        self.assertEqual(len(note['assets']), 5)


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runtime = BrowserRuntime(Path(self.tmp.name), 'test-token-' * 4)
        self.runtime.session_id = 'session'
        self.runtime.seen.add('live-note')
        self.runtime.note_links['live-note'] = 'https://www.xiaohongshu.com/explore/live-note'
        self.page = SimpleNamespace(on=lambda *args: None, goto=AsyncMock(), close=AsyncMock(),
                                    url=self.runtime.note_links['live-note'])
        self.runtime.context = SimpleNamespace(new_page=AsyncMock(return_value=self.page))
        self.sleep = patch('xhs_worker.runtime.asyncio.sleep', new_callable=AsyncMock)
        self.sleep.start()
        self.addCleanup(self.sleep.stop)

    def state(self, raw):
        return {'note': {'noteDetailMap': {'live-note': {'note': raw}}}}

    async def test_complete_detail_registers_all_candidates(self):
        self.page.evaluate = AsyncMock(return_value=self.state(card()))
        result = await self.runtime.resolve('session', 'live-note')
        self.assertTrue(result['media_complete'])
        self.assertEqual(result['missing_livephoto_ordinals'], [])
        self.assertEqual(len(self.runtime.asset_candidates), 5)
        self.page.close.assert_awaited_once()

    async def test_missing_detail_is_partial_and_clears_stale_url(self):
        raw = card()
        self.runtime._register_assets('live-note', raw, normalize_note(raw)[1])
        raw['imageList'][0]['stream'] = {}
        self.page.evaluate = AsyncMock(return_value=self.state(raw))
        result = await self.runtime.resolve('session', 'live-note')
        self.assertFalse(result['media_complete'])
        self.assertEqual(result['missing_livephoto_ordinals'], [0])
        with self.assertRaises(BrowserFault) as error:
            await self.runtime.asset('session', 'live-note', 0, 'livephoto')
        self.assertEqual(str(error.exception), 'LIVEPHOTO_STREAM_UNAVAILABLE')

    async def test_list_hints_survive_static_only_detail(self):
        raw = card()
        self.runtime._register_assets('live-note', raw, normalize_note(raw)[1])
        for image in raw['imageList']:
            image.pop('livePhoto')
            image.pop('stream')
        self.page.evaluate = AsyncMock(return_value=self.state(raw))
        result = await self.runtime.resolve('session', 'live-note')
        self.assertEqual(result['missing_livephoto_ordinals'], list(range(5)))

    async def test_delayed_feed_fills_ssr_motion(self):
        raw = card()
        stills = deepcopy(raw)
        for image in stills['imageList']:
            image.pop('stream')
        async def evaluate(*args):
            self.runtime.detail_payload['live-note'] = raw
            return self.state(stills)
        self.page.evaluate = AsyncMock(side_effect=evaluate)
        result = await self.runtime.resolve('session', 'live-note')
        self.assertTrue(result['media_complete'])
        self.assertEqual(len(result['assets']), 10)

    async def test_failed_master_uses_backup(self):
        raw = card()
        self.runtime._register_assets('live-note', raw, normalize_note(raw)[1])
        def download(url, target, **kwargs):
            if 'sns-video-v2' in url:
                raise MediaRejected('MEDIA_UNAVAILABLE')
            target.write_bytes(b'backup')
            return {'size': 6}
        with patch('xhs_worker.runtime.download_public_media', side_effect=download) as downloader:
            target, _ = await self.runtime.asset('session', 'live-note', 0, 'livephoto')
        self.assertEqual(target.read_bytes(), b'backup')
        self.assertEqual(downloader.call_count, 2)

    async def test_missing_stream_category_survives_media_endpoint(self):
        app = create_app(runtime=self.runtime, token='test-token-' * 4)
        endpoint = next(route.endpoint for route in app.routes
                        if route.path == '/sessions/{session_id}/assets/{note_id}/{ordinal}/{kind}')
        response = await endpoint('session', 'live-note', 0, 'livephoto')
        self.assertEqual(response.status_code, 409)
        self.assertIn(b'LIVEPHOTO_STREAM_UNAVAILABLE', response.body)
        self.assertEqual(WorkerError('LIVEPHOTO_STREAM_UNAVAILABLE').category, 'LIVEPHOTO_STREAM_UNAVAILABLE')


class LocalMediaWorker:
    def __init__(self, raw, image, video):
        self.payload = resolved(raw)
        self.blobs = {'image': image, 'livephoto': video}
        self.fail_motion = False
        self.failure_category = 'MEDIA_UNAVAILABLE'
        self.requests = []

    def request(self, *args):
        return self.payload

    @contextmanager
    def response(self, method, path, **kwargs):
        kind = path.rsplit('/', 1)[1]
        ordinal = int(path.rsplit('/', 2)[1])
        self.requests.append((ordinal, kind))
        if kind == 'livephoto' and self.fail_motion:
            raise WorkerError(self.failure_category)
        blob = self.blobs[kind]
        # Required transport integrity headers, matching the production RPC.
        response = SimpleNamespace(headers={'Content-Type': 'image/jpeg' if kind == 'image' else 'video/mp4',
                                           'X-Asset-Sha256': hashlib.sha256(blob).hexdigest(),
                                           'X-Asset-Width': '32', 'X-Asset-Height': '48', 'X-Asset-Duration': '0.2'},
                                   iter_content=lambda size: iter([blob]))
        yield response


class ArchiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.media_tmp = tempfile.TemporaryDirectory()
        video = Path(cls.media_tmp.name) / 'clip.mp4'
        subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), '-v', 'error', '-f', 'lavfi', '-i',
                        'color=c=red:s=32x48:d=0.2', '-pix_fmt', 'yuv420p', str(video)], check=True)
        cls.video = video.read_bytes()
        image = io.BytesIO()
        Image.new('RGB', (32, 48), 'blue').save(image, format='JPEG')
        cls.image = image.getvalue()

    @classmethod
    def tearDownClass(cls):
        cls.media_tmp.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.config = replace(load_config(), storage_root=root, config_dir=root / 'config', data_dir=root / 'data',
                              images_dir=root / 'data/images', livephoto_dir=root / 'data/livephoto',
                              database_path=root / 'config/app.db')
        self.storage = StorageService(self.config)
        self.storage.ensure()
        self.db = Database(self.config.database_path)
        self.db.init()
        self.sync = XhsSyncService(self.db)
        self.source = self.sync.create_source({'name': 'Test', 'owner_key': 'test-owner'})
        self.raw = card()
        self.raw['imageList'] = self.raw['imageList'][:1]
        self.seed(self.raw)
        self.worker = LocalMediaWorker(self.raw, self.image, self.video)
        self.indexer = MediaIndexer(self.db, self.storage, ThumbnailService())
        self.manager = XhsManager(self.db, self.storage, self.indexer, self.worker)

    def seed(self, raw):
        note = XhsNote.parse(normalize_note(raw)[0])
        self.sync.scan_pages(self.source['source_id'], 'test-owner', [ScanPage((note,), True)],
                             ignore_anchors=True, emit_task_history=False)

    def download(self):
        stats = {'downloaded': 0, 'errors': 0, 'blocked': 0, 'added_items': []}
        self.manager._download_pending(self.source, 'session', stats, None, None)
        return stats

    def status(self):
        with self.db.connect() as conn:
            return dict(conn.execute('select * from xhs_note_downloads').fetchone())

    def detail(self):
        return GalleryService(self.db, self.storage).get_folder_detail(f"xhs_{self.source['source_id']}_live-note")

    def test_download_and_gallery_pair(self):
        stats = self.download()
        self.assertEqual(stats['downloaded'], 2)
        self.assertEqual(self.status()['status'], 'ready')
        detail = self.detail()
        self.assertEqual(len(detail['pairs']), 1)
        self.assertTrue(detail['pairs'][0]['complete'])
        self.assertEqual(len(detail['videos']), 0)
        self.assertTrue(detail['folder']['has_livephoto'])

    def test_partial_retry_reuses_still(self):
        self.worker.fail_motion = True
        first = self.download()
        self.assertEqual(first['errors'], 1)
        self.assertEqual(first['added_items'][0]['livephoto_count'], 0)
        self.assertEqual(self.status()['status'], 'partial')
        self.assertEqual(self.status()['media_revision'], 2)
        self.assertEqual(len(self.detail()['images']), 1)
        self.worker.requests.clear()
        self.worker.fail_motion = False
        second = self.download()
        self.assertEqual(self.worker.requests, [(0, 'livephoto')])
        self.assertEqual(second['downloaded'], 1)
        self.assertEqual(self.status()['status'], 'ready')
        self.assertTrue(self.detail()['pairs'][0]['complete'])

    def test_missing_motion_keeps_still_and_pending_asset(self):
        self.worker.payload = resolved(self.raw, missing=[0])
        stats = self.download()
        self.assertEqual(self.worker.requests, [(0, 'image')])
        self.assertEqual(stats['errors'], 1)
        self.assertEqual(self.status()['error_category'], 'LIVEPHOTO_STREAM_UNAVAILABLE')
        self.assertEqual(self.status()['status'], 'partial')
        self.worker.payload = resolved(self.raw)
        self.download()
        self.assertEqual(self.status()['status'], 'ready')

    def test_quota_failure_stops_instead_of_becoming_optional(self):
        self.worker.fail_motion = True
        self.worker.failure_category = 'MEDIA_QUOTA_EXCEEDED'
        with self.assertRaises(WorkerError):
            self.download()
        self.assertEqual(self.status()['status'], 'failed')

    def test_markerless_retry_does_not_forget_known_motion(self):
        self.worker.fail_motion = True
        self.download()
        still = deepcopy(self.raw)
        still['imageList'][0].pop('stream')
        still['imageList'][0].pop('livePhoto')
        self.worker.payload = resolved(still)
        stats = self.download()
        self.assertEqual(self.status()['status'], 'partial')
        self.assertEqual(stats['errors'], 1)
        self.assertEqual(len(self.db.list_site_assets(self.status()['post_id'])), 2)

    def test_markerless_detail_preserves_cached_motion(self):
        self.download()
        with self.db.connect() as conn:
            conn.execute("update xhs_note_downloads set status='pending'")
        still = deepcopy(self.raw)
        still['imageList'][0].pop('stream')
        still['imageList'][0].pop('livePhoto')
        self.worker.payload = resolved(still)
        self.worker.requests.clear()
        self.download()
        self.assertEqual(self.worker.requests, [])
        self.assertEqual(self.status()['status'], 'ready')
        self.assertTrue(self.detail()['pairs'][0]['complete'])

    def test_new_motion_discovery_reopens_old_ready_note(self):
        still = deepcopy(self.raw)
        still['imageList'][0].pop('stream')
        still['imageList'][0].pop('livePhoto')
        with self.db.connect() as conn:
            conn.execute("delete from site_assets where media_type='livephoto'")
        self.worker.payload = resolved(still)
        self.download()
        self.assertEqual(self.status()['status'], 'ready')
        self.seed(self.raw)
        self.assertEqual(self.status()['status'], 'pending')

    def test_upgrade_requeues_legacy_ready_once(self):
        self.download()
        with self.db.connect() as conn:
            conn.execute("update xhs_source_accounts set auth_mode='browser'")
            conn.execute("update xhs_note_downloads set media_revision=1")
            conn.execute('delete from xhs_schema_migrations where version=7')
            migrate(conn)
        self.assertEqual(self.status()['status'], 'pending')
        with self.db.connect() as conn:
            conn.execute("update xhs_note_downloads set status='ready'")
            migrate(conn)
        self.assertEqual(self.status()['status'], 'ready')

    def test_incomplete_non_motion_resolution_is_rejected(self):
        self.worker.payload['media_complete'] = False
        stats = self.download()
        self.assertEqual(stats['errors'], 1)
        self.assertEqual(self.status()['status'], 'failed')
        self.assertEqual(self.worker.requests, [])


if __name__ == '__main__':
    unittest.main()
