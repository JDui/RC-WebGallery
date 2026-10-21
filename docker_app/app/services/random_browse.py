"""Persistent shuffled snapshots for duplicate-free, continuous gallery browsing."""
from __future__ import annotations

from hashlib import blake2b
import re
import time
from uuid import uuid4

from app.services.utils import dumps_json


class RandomBrowseError(ValueError):
    pass


class RandomFeedExpired(RandomBrowseError):
    pass


def normalize_seed(seed: str | None, page: int) -> str:
    if seed is None:
        if page > 1:
            raise RandomBrowseError('连续随机浏览需要沿用第一页返回的 random_seed')
        return uuid4().hex
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', seed):
        raise RandomBrowseError('随机浏览标识无效')
    return seed


def page_keys(db, table, where_sql, params, seed, page, page_size, fallback_keys=None):
    seed = normalize_seed(seed, page)
    session_key = blake2b(dumps_json([table, where_sql, params, seed, page_size]).encode(), digest_size=20).hexdigest()
    key_column = 'item_key' if table == 'pair_index' else 'folder_name'
    clock = time.time()
    seed_key = blake2b(seed.encode(), digest_size=32).digest()
    def rank(key):
        return blake2b(str(key).encode(), key=seed_key, digest_size=16).hexdigest()
    with db.connect() as conn:
        conn.execute('begin immediate')
        session = conn.execute('select id,total from random_browse_sessions where session_key=?', (session_key,)).fetchone()
        if not session:
            if page > 1:
                raise RandomFeedExpired('本轮随机浏览已过期，请重新开始浏览')
            cursor = conn.execute('insert into random_browse_sessions(session_key,total,last_used) values (?,0,?)', (session_key, clock))
            session_id = cursor.lastrowid
            if fallback_keys is None:
                conn.create_function('gallery_shuffle_rank', 1, rank, deterministic=True)
                conn.execute(f'''insert into random_browse_entries(session_id,position,item_key)
                    select ?,row_number() over (order by gallery_shuffle_rank({key_column}),{key_column})-1,{key_column}
                    from {table} where {where_sql}''', [session_id, *params])
            else:
                ordered = sorted(set(fallback_keys), key=lambda key: (rank(key), key))
                conn.executemany('insert into random_browse_entries values (?,?,?)',
                                 ((session_id, index, key) for index, key in enumerate(ordered)))
            total = conn.execute('select count(*) from random_browse_entries where session_id=?', (session_id,)).fetchone()[0]
            conn.execute('update random_browse_sessions set total=? where id=?', (total, session_id))
        else:
            session_id, total = session['id'], session['total']
        conn.execute('update random_browse_sessions set last_used=? where id=?', (clock, session_id))
        start = max(0, page - 1) * page_size
        keys = [row[0] for row in conn.execute('''select item_key from random_browse_entries
            where session_id=? and position>=? and position<? order by position''', (session_id, start, start + page_size))]
        # Eviction expires a feed explicitly; it never silently changes its order mid-scroll.
        old = [row[0] for row in conn.execute('''select id from random_browse_sessions
            where id!=? and (last_used<? or id in
                (select id from random_browse_sessions order by last_used desc,id desc limit -1 offset 16))''',
            (session_id, clock - 86400))]
        for old_id in old:
            conn.execute('delete from random_browse_entries where session_id=?', (old_id,))
            conn.execute('delete from random_browse_sessions where id=?', (old_id,))
    return {'total': total, 'keys': keys}
