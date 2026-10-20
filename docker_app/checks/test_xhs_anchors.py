"""Run on Linux: PYTHONPATH=docker_app python -m unittest discover -s docker_app/checks."""
from contextlib import nullcontext
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.db import Database
from app.services.utils import TIMEZONE, now_iso
from app.services.xhs.manager import XhsManager
from app.services.xhs.models import XhsNote
from app.services.xhs.sync import ScanPage, XhsSyncService


def note(key):
    return XhsNote(key, f'https://www.xiaohongshu.com/explore/{key}',
                   f'Title {key}', 'author1', 'image', None, now_iso(), ())


class PageWorker:
    configured = True

    def __init__(self):
        self.reads = 0

    def session(self, *args):
        return nullcontext('test-session')

    def pages(self, *args):
        self.reads = 0
        for keys, end in [(('a', 'b', 'c'), False), (('d',), True)]:
            self.reads += 1
            yield ScanPage(tuple(note(key) for key in keys), end)


class AnchorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / 'test.db')
        self.db.init()
        self.sync = XhsSyncService(self.db)
        source = self.sync.create_source({'name': 'Test', 'owner_key': 'test-owner',
                                         'initial_fetch_limit': 3, 'anchor_count': 2})
        self.source_id = source['source_id']
        self.update(ordering_stable=True)

    def update(self, **values):
        source = self.sync.get_source(self.source_id)
        return self.sync.update_source(self.source_id, {'config_version': source['config_version'], **values})

    def scan(self, pages, **kwargs):
        return self.sync.scan_pages(self.source_id, 'test-owner', pages, emit_task_history=False, **kwargs)

    def anchors(self):
        with self.db.connect() as conn:
            return self.sync._anchors(conn, self.sync.get_source(self.source_id, conn))

    def test_limited_first_scan_saves_leading_anchors_and_stays_partial(self):
        result = self.scan([ScanPage(tuple(note(key) for key in ('a', 'b', 'c', 'd')))])
        self.assertEqual(result['stop_reason'], 'first_sync_limit')
        self.assertEqual(result['result'], 'PARTIAL')
        self.assertEqual(result['complete_snapshot'], 0)
        self.assertEqual(result['seen_count'], 3)
        self.assertEqual(result['checkpoint_committed'], 1)
        self.assertEqual(self.anchors(), ['a', 'b'])
        source = self.sync.get_source(self.source_id)
        self.assertEqual(source['initial_fetch_completed'], 1)
        self.assertEqual(source['last_checkpoint_scan_id'], result['scan_id'])
        with self.db.connect() as conn:
            profile = conn.execute('select title_prefix, author_id from xhs_anchor_candidates '
                                   'where scan_id=? and rank=0', (result['scan_id'],)).fetchone()
        self.assertEqual(tuple(profile), ('Title a', 'author1'))

    def test_exact_page_boundary_saves_anchors(self):
        result = self.scan([ScanPage(tuple(note(key) for key in ('a', 'b', 'c')))])
        self.assertEqual(result['stop_reason'], 'first_sync_limit')
        self.assertEqual(self.anchors(), ['a', 'b'])

    def test_limit_smaller_than_anchor_count_saves_available_notes(self):
        self.update(initial_fetch_limit=1, anchor_count=3)
        result = self.scan([ScanPage((note('a'), note('b')))])
        self.assertEqual(result['seen_count'], 1)
        self.assertEqual(result['checkpoint_committed'], 1)
        self.assertEqual(self.anchors(), ['a'])

    def test_unreliable_limited_scan_does_not_complete_initialization(self):
        result = self.scan([ScanPage(tuple(note(key) for key in ('a', 'b', 'c')), integrity_ok=False)])
        self.assertEqual(result['checkpoint_committed'], 0)
        self.assertEqual(self.anchors(), [])
        self.assertEqual(self.sync.get_source(self.source_id)['initial_fetch_completed'], 0)

    def test_cancelled_scan_does_not_save_anchors(self):
        result = self.scan([ScanPage((note('a'),))], cancelled=lambda: True)
        self.assertEqual(result['stop_reason'], 'cancelled')
        self.assertEqual(result['checkpoint_committed'], 0)
        self.assertEqual(self.sync.get_source(self.source_id)['initial_fetch_completed'], 0)

    def test_old_completed_source_without_checkpoint_rebuilds_limited_baseline(self):
        with self.db.connect() as conn:
            conn.execute('update xhs_source_accounts set initial_fetch_completed=1 where source_id=?',
                         (self.source_id,))
        result = self.scan([ScanPage(tuple(note(key) for key in ('a', 'b', 'c', 'd')))])
        self.assertEqual(result['stop_reason'], 'first_sync_limit')
        self.assertEqual(result['initial_fetch_limit_at_start'], 3)
        self.assertEqual(self.anchors(), ['a', 'b'])

    def test_limited_rescan_does_not_mark_unseen_likes_removed(self):
        self.scan([ScanPage((note('old'),), end_of_feed=True)])
        self.update(initial_fetch_limit=2)
        result = self.scan([ScanPage((note('new1'), note('new2'), note('old')))])
        self.assertEqual(result['stop_reason'], 'first_sync_limit')
        self.assertEqual(self.anchors(), ['new1', 'new2'])
        with self.db.connect() as conn:
            liked = conn.execute('select is_currently_liked from xhs_like_state '
                                 'where source_id=? and note_id=?', (self.source_id, 'old')).fetchone()[0]
        self.assertEqual(liked, 1)

    def test_natural_end_is_still_a_full_snapshot(self):
        result = self.scan([ScanPage((note('a'),), end_of_feed=True)])
        self.assertEqual(result['result'], 'SUCCESS_FULL')
        self.assertEqual(result['complete_snapshot'], 1)
        self.assertEqual(self.anchors(), ['a'])

    def execute(self, *, force_full=False):
        worker = PageWorker()
        manager = XhsManager(self.db, None, None, worker)
        with patch.object(manager, 'ready', return_value=True), patch.object(manager, '_download_pending'):
            job, _ = manager.reserve_job(self.source_id, force_full=force_full)
            result = manager.execute(job['job_id'])
        self.assertIsNone(result['error_category'])
        return result, worker.reads

    def test_next_job_uses_first_scan_anchors_without_a_full_snapshot(self):
        first, _ = self.execute()
        second, reads = self.execute()
        self.assertEqual(first['discovery']['stop_reason'], 'first_sync_limit')
        self.assertFalse(second['effective_force_full'])
        self.assertEqual(second['discovery']['stop_reason'], 'anchor_hit')
        self.assertEqual(second['state'], 'success')
        self.assertEqual(reads, 1)

    def test_explicit_full_scan_still_ignores_initial_anchors(self):
        self.execute()
        result, reads = self.execute(force_full=True)
        self.assertTrue(result['effective_force_full'])
        self.assertEqual(result['discovery']['stop_reason'], 'end_of_feed')
        self.assertEqual(reads, 2)

    def test_unstable_order_still_disables_early_stop(self):
        self.execute()
        self.update(ordering_stable=False)
        result, reads = self.execute()
        self.assertTrue(result['effective_force_full'])
        self.assertEqual(reads, 2)

    def test_incremental_hits_do_not_postpone_periodic_full_scan(self):
        first, _ = self.execute()
        self.execute()  # Recent incremental checkpoint, but no complete snapshot.
        expired = (datetime.now(TIMEZONE) - timedelta(days=8)).isoformat()
        with self.db.connect() as conn:
            conn.execute('update xhs_sync_runs set finished_at=? where scan_id=?',
                         (expired, first['discovery']['scan_id']))
        result, reads = self.execute()
        self.assertTrue(result['effective_force_full'])
        self.assertEqual(result['discovery']['stop_reason'], 'end_of_feed')
        self.assertEqual(reads, 2)


if __name__ == '__main__':
    unittest.main()
