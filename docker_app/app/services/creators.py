from __future__ import annotations

from collections import defaultdict
from urllib.parse import quote, urlsplit, urlunsplit

from app.services.utils import loads_json


def _web_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        if parsed.scheme in {'http', 'https'} and parsed.hostname and not parsed.username and not parsed.password:
            return value
    except ValueError:
        pass
    return ''


def attach_creators(db, items: list[dict]) -> None:
    """Enrich visible cards in batches while preserving subscription names for filtering."""
    notes = defaultdict(set)
    site_ids = set()
    for item in items:
        parts = str(item.get('folder_name') or '').split('_', 2)
        if len(parts) == 3 and parts[0] == 'xhs' and parts[1].isdigit():
            notes[int(parts[1])].add(parts[2])
        elif str(item.get('subscription_uid') or '').startswith('site:'):
            value = str(item['subscription_uid']).split(':', 1)[1]
            if value.isdigit():
                site_ids.add(int(value))
    authors, sites = {}, {}
    if notes or site_ids:
        with db.connect() as conn:
            for source_id, note_ids in notes.items():
                keys = sorted(note_ids)
                for offset in range(0, len(keys), 400):
                    batch = keys[offset:offset + 400]
                    marks = ','.join('?' for _ in batch)
                    for row in conn.execute(f'''select note_id,author_id,author_name from xhs_like_state
                        where source_id=? and note_id in ({marks})''', [source_id, *batch]):
                        authors[f"xhs_{source_id}_{row['note_id']}"] = dict(row)
            keys = sorted(site_ids)
            for offset in range(0, len(keys), 400):
                batch = keys[offset:offset + 400]
                marks = ','.join('?' for _ in batch)
                for row in conn.execute(f'select id,name,entry_url,source_type from site_sources where id in ({marks})', batch):
                    sites[f"site:{row['id']}"] = dict(row)
    for item in items:
        metadata = loads_json(item.get('metadata_json'), {})
        folder_name = str(item.get('folder_name') or '')
        uid = str(item.get('subscription_uid') or '')
        if folder_name.startswith('xhs_') or metadata.get('source') == 'xhs':
            author = authors.get(folder_name, {})
            author_id = str(author.get('author_id') or item.get('author_id') or metadata.get('author_id') or '').strip()
            author_name = str(author.get('author_name') or item.get('author_name') or metadata.get('author_name') or '').strip()
            item['author_id'], item['author_name'] = author_id, author_name
            item['creator_name'] = author_name or (f'作者 {author_id}' if author_id else '未知作者')
            item['creator_url'] = f'https://www.xiaohongshu.com/user/profile/{quote(author_id, safe="")}' if author_id else ''
        else:
            source = sites.get(uid, {})
            item['creator_name'] = source.get('name') or item.get('subscription_name') or '默认订阅'
            if uid.isdigit():
                item['creator_url'] = f'https://space.bilibili.com/{uid}'
            else:
                url = _web_url(str(source.get('entry_url') or ''))
                if url and source.get('source_type') in {'rss', 'sitemap'}:
                    parsed = urlsplit(url)
                    url = urlunsplit((parsed.scheme, parsed.netloc, '/', '', ''))
                item['creator_url'] = url
