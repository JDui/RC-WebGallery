"""Isolated Live Photo layout and migration regressions.

Run in the Linux app runtime with:
    PYTHONPATH=/review/docker_app:/review python -m unittest checks.test_livephoto_layout

Every database and media file is synthesized under TemporaryDirectory. The MP4
fixtures are real, locally generated videos; no real gallery or network service
is accessed.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from types import SimpleNamespace

from PIL import Image
import imageio_ffmpeg

from app.config import load_config
from app.db import Database
from app.services.data_upgrade import DataUpgrade
from app.services.livephoto import LivePhotoService
from app.services.storage import StorageService


class LivePhotoLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='livephoto-layout-')
        self.root = Path(self.temp.name)
        self.config = replace(load_config(), storage_root=self.root, config_dir=self.root/'config',
                              data_dir=self.root/'data', images_dir=self.root/'data/images',
                              livephoto_dir=self.root/'data/livephoto', database_path=self.root/'config/app.db')
        self.storage = StorageService(self.config)
        self.storage.ensure()
        self.db = Database(self.config.database_path)
        self.db.init()
        self.storage.db = self.db
        self.livephotos = LivePhotoService(self.db, self.storage)
        self.asset_rows = defaultdict(lambda: {'image': [], 'livephoto': []})
        self.media_entries = []
        self._make_video_fixture()
        self._seed_source_folders()

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def _make_video_fixture(self):
        if not shutil.which('exiftool'):
            self.fail('ExifTool is required by this Linux integration test')
        frame = self.root/'fixture-frame.png'
        self.video_fixture = self.root/'fixture-video.mp4'
        Image.new('RGB', (48, 32), (21, 85, 144)).save(frame, format='PNG')
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        result = subprocess.run([ffmpeg, '-v', 'error', '-loop', '1', '-i', str(frame),
                                 '-frames:v', '1', '-c:v', 'mpeg4', '-q:v', '5', '-y', str(self.video_fixture)],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertGreater(self.video_fixture.stat().st_size, 0)

    def _seed_folder(self, name, uid, pub_time):
        self.db.upsert_folder({'folder_name': name, 'title': name, 'pub_ts': 1_700_000_000,
                               'pub_time': pub_time, 'top_dynamic_id': f'top-{name}',
                               'source_dynamic_id': f'source-{name}', 'subscription_uid': uid,
                               'subscription_name': uid, 'has_images': True, 'has_livephoto': True})

    def _add_pair(self, folder, index, stem, prefixed):
        visible_stem = f'{index:03d}__{stem}' if prefixed else stem
        image_name = visible_stem + '.png'
        video_name = visible_stem + '.mp4'
        image = self.storage.image_folder(folder)/image_name
        video = self.storage.livephoto_folder(folder)/video_name
        image.parent.mkdir(parents=True, exist_ok=True)
        video.parent.mkdir(parents=True, exist_ok=True)
        Image.new('RGB', (48, 32), (index * 41 % 255, index * 73 % 255, 52)).save(image, format='PNG')
        shutil.copyfile(self.video_fixture, video)
        self.media_entries.extend(((folder, 'image', image_name, image),
                                   (folder, 'livephoto', video_name, video)))
        if not prefixed:
            self.asset_rows[folder]['image'].append({'filename': image_name, 'pair_index': index,
                'rel_path': self.storage.relative_to_storage(image), 'status': 'ready'})
            self.asset_rows[folder]['livephoto'].append({'filename': video_name, 'pair_index': index,
                'rel_path': self.storage.relative_to_storage(video), 'status': 'ready'})

    def _seed_source_folders(self):
        self.bilibili = 'layout-bilibili-42'
        self.xhs = 'xhs_creator_layout-note'
        self._seed_folder(self.bilibili, '42', '2025-03-04')
        self._seed_folder(self.xhs, 'xhs:creator', '2024-08-09')
        # One current prefixed pair plus a same-stem legacy pair in Bilibili;
        # Xiaohongshu has its own prefixed pair.
        self._add_pair(self.bilibili, 1, 'bili-current', prefixed=True)
        self._add_pair(self.bilibili, 2, 'archived-same-stem', prefixed=False)
        self._add_pair(self.xhs, 1, 'xhs-current', prefixed=True)
        for folder, kinds in self.asset_rows.items():
            for media_type, rows in kinds.items():
                self.db.replace_folder_assets(folder, media_type, rows)

    def _bytes_by_folder(self):
        output = {}
        for folder, media_type, filename, _legacy in self.media_entries:
            root = self.storage.image_folder(folder) if media_type == 'image' else self.storage.livephoto_folder(folder)
            output[(folder, media_type, filename)] = (root/filename).read_bytes()
        return output

    def _groups(self):
        return {folder: self.livephotos.pairs(folder) for folder in (self.bilibili, self.xhs)}

    def test_upgrade_migrates_bilibili_and_xhs_livephoto_groups_without_read_scan_mutation(self):
        original_bytes = self._bytes_by_folder()
        before = self.livephotos.maintain(repair=False)
        self.assertEqual(before['scan_scope'], 'all')
        self.assertEqual(before['checked'], 3)
        self.assertEqual(before['repairable'], 3)
        self.assertEqual(self._bytes_by_folder(), original_bytes,
                         'read-only inspection must not normalize historical identifiers')
        with self.db.connect() as conn:
            self.assertEqual(conn.execute('select count(*) from livephoto_pairs').fetchone()[0], 0)

        # Historical normalization is only performed after an explicit repair request.
        repaired = self.livephotos.maintain(repair=True)
        self.assertEqual(repaired['repaired'], 3)
        self.assertEqual(repaired['scan_scope'], 'all')
        before_upgrade_bytes = self._bytes_by_folder()
        expected_ids = {}
        for folder, groups in self._groups().items():
            for group in groups:
                image, video = group['images'][0], group['videos'][0]
                identifiers = self.livephotos.read_identifiers([image, video])
                record = self.livephotos.record(folder, group['index'])
                self.assertTrue(identifiers[0])
                self.assertEqual(identifiers, [record['content_id'], record['content_id']])
                expected_ids[(folder, group['index'])] = record['content_id']

        manager = SimpleNamespace(_queue_lock=threading.Lock(), _queue=[], _status={},
                                  _release=lambda: None, _run_next_queued_task=lambda: None)
        upgrade = DataUpgrade(self.db, self.storage, None, manager)
        upgrade.state.update(status='running', action='upgrade', progress=0)
        upgrade._run()
        self.assertEqual(upgrade.state['status'], 'completed', upgrade.state)
        self.assertEqual(upgrade.version, 2)

        self.db.close()
        self.db = Database(self.config.database_path)
        self.db.init()
        self.storage = StorageService(self.config)
        self.storage.ensure()
        self.storage.db = self.db
        self.livephotos = LivePhotoService(self.db, self.storage)
        after = self.livephotos.maintain(repair=False)
        self.assertEqual(after['scan_scope'], 'all')
        self.assertEqual(after['scanned_folders'], 2)
        self.assertEqual(after['checked'], 3)
        self.assertEqual(after['paired'], 3)
        self.assertEqual(after['repairable'], 0)
        self.assertEqual(after['errors'], 0, after['issues'])

        groups = self._groups()
        self.assertEqual({name: len(value) for name, value in groups.items()},
                         {self.bilibili: 2, self.xhs: 1})
        for folder, folder_groups in groups.items():
            for group in folder_groups:
                key = (folder, group['index'])
                self.assertEqual(self.livephotos.read_identifiers(group['images'] + group['videos']),
                                 [expected_ids[key], expected_ids[key]])
                self.assertEqual(self.livephotos.record(*key)['content_id'], expected_ids[key])
        self.assertEqual(self._bytes_by_folder(), before_upgrade_bytes,
                         'directory migration must preserve every image and video byte')
        with self.db.connect() as conn:
            locations = {row['folder_name']: row['rel_dir'] for row in conn.execute(
                'select folder_name,rel_dir from folder_locations where media_type="images"')}
        self.assertIn('/bilibili-42/2025-03-04/', '/' + locations[self.bilibili] + '/')
        self.assertIn('/xhs-creator/2024-08-09/', '/' + locations[self.xhs] + '/')

    def test_subset_folder_and_target_scope_then_all_scope_includes_both_sources(self):
        target = next(group for group in self.livephotos.pairs(self.bilibili) if group['index'] == 2)
        result = self.livephotos.maintain(folders=[self.bilibili], targets=[
            self.livephotos.target(self.bilibili, target)])
        self.assertEqual(result['scan_scope'], 'subset')
        self.assertEqual(result['scanned_folders'], 1)
        self.assertEqual(result['checked'], 1)
        self.assertEqual([(item['folder_name'], item['pair_index']) for item in result['repairable_targets']],
                         [(self.bilibili, 2)])

        whole = self.livephotos.maintain()
        self.assertEqual(whole['scan_scope'], 'all')
        self.assertEqual(whole['scanned_folders'], 2)
        self.assertEqual(whole['checked'], 3)
        self.assertEqual({item['folder_name'] for item in whole['repairable_targets']},
                         {self.bilibili, self.xhs})


if __name__ == '__main__':
    unittest.main(verbosity=2)
