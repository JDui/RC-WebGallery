from __future__ import annotations

import re
from collections import defaultdict
from urllib.parse import quote

from app.services.utils import dumps_json, loads_json
from app.services.xhs.worker_client import WorkerError


def missing_author_name(name, author_id):
    name, author_id = str(name or '').strip(), str(author_id or '').strip()
    return (not name or name in {author_id, f'作者 {author_id}', '未知作者'}
            or bool(re.fullmatch(r'[0-9a-fA-F]{24}', name)))


class XhsAuthorMaintenance:
    """Repair legacy author labels without importing a scan or touching media."""

    def __init__(self, db, worker):
        self.db, self.worker = db, worker

    def run(self, cooperate=None, progress=None, stats=None):
        stats = stats if stats is not None else {}
        with self.db.connect() as conn:
            rows = [dict(row) for row in conn.execute('''select l.*, a.owner_key,
                f.metadata_json from xhs_like_state l
                join xhs_source_accounts a on a.source_id=l.source_id
                left join folders f on f.folder_name='xhs_'||l.source_id||'_'||l.note_id
                order by l.last_seen_at desc, l.source_id, l.note_id''')]
        pending = {(row['source_id'], row['note_id']): row for row in rows
                   if missing_author_name(row['author_name'], row['author_id'])}
        stats.update(scan_scope='all', checked=len(rows), repaired=0,
                     unchanged=len(rows) - len(pending), unresolved=len(pending), pages=0,
                     issues=[], completed=False)

        def checkpoint():
            if cooperate:
                cooperate()

        def report():
            stats['unresolved'] = len(pending)
            if progress:
                done = stats['checked'] if stats['completed'] else stats['checked'] - len(pending)
                progress(done, stats['checked'], stats)

        def repair(key, author_id, name):
            checkpoint()
            row = pending[key]
            folder = f"xhs_{row['source_id']}_{row['note_id']}"
            with self.db.connect() as conn:
                current = conn.execute('select author_id,author_name from xhs_like_state '
                                       'where source_id=? and note_id=?', key).fetchone()
                if not current:
                    return
                if not missing_author_name(current['author_name'], current['author_id']):
                    stats['unchanged'] += 1
                else:
                    conn.execute('update xhs_like_state set author_id=?,author_name=? '
                                 'where source_id=? and note_id=?', (author_id, name, *key))
                    metadata = conn.execute('select metadata_json from folders where folder_name=?', (folder,)).fetchone()
                    if metadata:
                        value = loads_json(metadata[0], {})
                        value = value if isinstance(value, dict) else {}
                        value.update(author_id=author_id, author_name=name)
                        conn.execute('update folders set metadata_json=? where folder_name=?', (dumps_json(value), folder))
                    # Both old single databases and partitioned browse indexes must
                    # immediately expose the repaired name, including cached pages.
                    url = 'https://www.xiaohongshu.com/user/profile/' + quote(author_id, safe='') if author_id else ''
                    for table in ('folder_index', 'pair_index'):
                        conn.execute(f'update {table} set author_id=?,author_name=?,creator_name=?,creator_url=? '
                                     'where folder_name=?', (author_id, name, name, url, folder))
                    conn.execute('delete from gallery_page_cache')
                    stats['repaired'] += 1
            pending.pop(key, None)
            report()

        # A previously stored nickname can repair other notes by the same author,
        # even when that note is no longer visible in the current likes feed.
        names = {}
        for row in rows:
            checkpoint()
            name = str(row['author_name'] or '').strip()
            if row['author_id'] and not missing_author_name(name, row['author_id']):
                names.setdefault(row['author_id'], name)
            metadata = loads_json(row['metadata_json'], {})
            if isinstance(metadata, dict):
                name = str(metadata.get('author_name') or '').strip()
                author = str(metadata.get('author_id') or row['author_id']).strip()
                if author and not missing_author_name(name, author):
                    names.setdefault(author, name)
                    key = (row['source_id'], row['note_id'])
                    if key in pending and (not row['author_id'] or row['author_id'] == author):
                        repair(key, author, name)
        for key, row in list(pending.items()):
            if row['author_id'] in names:
                repair(key, row['author_id'], names[row['author_id']])
        report()

        by_note, by_author = defaultdict(set), defaultdict(set)
        for key, row in pending.items():
            by_note[row['owner_key'], row['note_id']].add(key)
            if row['author_id']:
                by_author[row['owner_key'], row['author_id']].add(key)

        if pending and self.worker.configured:
            # One browser session per owner, independent of source enablement,
            # first-fetch limits and incremental anchors. Never import this scan.
            owners = sorted({row['owner_key'] for row in pending.values()})
            for owner in owners:
                checkpoint()
                if not any(row['owner_key'] == owner for row in pending.values()):
                    continue
                try:
                    with self.worker.session(owner) as session_id:
                        for page in self.worker.pages(session_id, cooperate):
                            checkpoint()
                            stats['pages'] += 1
                            if not page.integrity_ok:
                                raise WorkerError('STRUCTURE_CHANGED')
                            for note in page.notes:
                                if missing_author_name(note.author_name, note.author_id):
                                    continue
                                keys = by_note[owner, note.note_id] | by_author[owner, note.author_id]
                                for key in sorted(keys):
                                    if key in pending:
                                        repair(key, note.author_id or pending[key]['author_id'], note.author_name.strip())
                            report()
                            if page.end_of_feed or not any(row['owner_key'] == owner for row in pending.values()):
                                break
                except WorkerError as exc:
                    stats['issues'].append({'owner_key': owner, 'reason': exc.category})
        elif pending:
            stats['issues'].append({'reason': 'NOT_CONFIGURED'})

        stats['unresolved'] = len(pending)
        stats['unresolved_notes'] = [{'source_id': row['source_id'], 'note_id': row['note_id'],
                                      'reason': 'AUTHOR_NOT_OBSERVED'} for row in pending.values()]
        stats['completed'] = True
        report()
        return stats
