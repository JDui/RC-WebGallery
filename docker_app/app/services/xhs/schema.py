from __future__ import annotations

import re
import sqlite3


FULL_SCAN_INTERVAL_MAX = 3650
# add column 时写死的 “between 1 and 365” 会永久留在 sqlite_master 里，
# SQLite 不支持修改已存在列的 CHECK，只能重建表。
_FULL_SCAN_CAP = re.compile(r'full_scan_interval_days[^,)]*between\s+1\s+and\s+365\b', re.I)
_FULL_SCAN_DECL = re.compile(
    r'(full_scan_interval_days\s+integer\s+not\s+null\s+(?:default\s+\d+\s+)?'
    r'check\(full_scan_interval_days\s+between\s+1\s+and\s+)365\b', re.I)


def _widen_full_scan_interval(conn: sqlite3.Connection) -> None:
    """把老库 xhs_source_accounts 上 full_scan_interval_days 的上限从 365 放宽到 3650。

    仅放宽 Python 校验是不够的：老库的 CHECK 仍会拒绝更大的值（IntegrityError）。
    新库由 add column 直接建出正确约束，这里只处理历史库，可重复执行。
    """
    row = conn.execute("select sql from sqlite_master where type='table' and name='xhs_source_accounts'").fetchone()
    ddl = row[0] if row else None
    if not ddl or not _FULL_SCAN_CAP.search(ddl):
        return
    if conn.execute("select count(*) from sqlite_master where type in ('trigger','view') "
                    "and sql like '%xhs_source_accounts%'").fetchone()[0]:
        raise sqlite3.OperationalError('xhs_source_accounts 上存在触发器或视图，请手工迁移 full_scan_interval_days 约束')
    staged = 'xhs_source_accounts__widened'
    new_ddl = _FULL_SCAN_DECL.sub(lambda match: match.group(1) + str(FULL_SCAN_INTERVAL_MAX), ddl, count=1)
    if new_ddl == ddl:
        raise sqlite3.OperationalError('无法改写 full_scan_interval_days 约束，请手工迁移')
    head = new_ddl.lower().index('xhs_source_accounts')
    if head > 40:  # 首个出现位置必须就是建表语句里的表名，否则宁可报错也不动表
        raise sqlite3.OperationalError('无法定位 xhs_source_accounts 建表语句，请手工迁移 full_scan_interval_days 约束')
    new_ddl = new_ddl[:head] + staged + new_ddl[head + len('xhs_source_accounts'):]

    conn.commit()  # foreign_keys 在事务内是 no-op，必须先结束 add column 留下的隐式事务
    conn.execute('pragma foreign_keys = off')
    if conn.execute('pragma foreign_keys').fetchone()[0]:
        raise sqlite3.OperationalError('无法关闭外键约束，放弃重建 xhs_source_accounts')
    try:
        conn.execute('pragma legacy_alter_table = on')  # 保证 rename 不去改写子表的 REFERENCES
    except sqlite3.OperationalError:
        pass  # 更老的 SQLite 没有这个开关，rename 不会改写子表定义
    try:
        conn.execute('begin immediate')
        try:
            conn.execute(new_ddl)
            conn.execute(f'insert into {staged} select * from xhs_source_accounts')
            conn.execute('drop table xhs_source_accounts')
            conn.execute(f'alter table {staged} rename to xhs_source_accounts')
            conn.execute('commit')
        except BaseException:
            conn.execute('rollback')
            raise
    finally:
        try:
            conn.execute('pragma legacy_alter_table = off')
        except sqlite3.OperationalError:
            pass
        conn.execute('pragma foreign_keys = on')
    if conn.execute('pragma foreign_key_check').fetchall():
        raise sqlite3.IntegrityError('放宽 full_scan_interval_days 约束后外键校验失败')


def migrate(conn: sqlite3.Connection) -> None:
    statements = [
        """create table if not exists xhs_schema_migrations (
            version integer primary key, applied_at text not null)""",
        """create table if not exists xhs_source_accounts (
            source_id integer primary key references site_sources(id) on delete cascade,
            owner_key text not null, auth_mode text not null default 'manual_manifest',
            auth_status text not null default 'MANUAL_ONLY', last_auth_check_at text,
            anchor_count integer not null default 3 check(anchor_count between 1 and 20),
            initial_fetch_limit integer default 100 check(initial_fetch_limit between 1 and 10000),
            initial_fetch_completed integer not null default 0,
            config_version integer not null default 0, checkpoint_version integer not null default 0,
            last_checkpoint_scan_id text, last_checkpoint_at text)""",
        """create table if not exists xhs_like_state (
            source_id integer not null references xhs_source_accounts(source_id) on delete cascade,
            note_id text not null, site_post_id integer not null references site_posts(id) on delete cascade,
            author_id text not null, note_type text not null,
            first_seen_at text not null, last_seen_at text not null,
            is_currently_liked integer not null default 1, last_full_snapshot_id text,
            primary key(source_id, note_id), unique(site_post_id))""",
        """create table if not exists xhs_sync_runs (
            scan_id text primary key, source_id integer not null references xhs_source_accounts(source_id) on delete cascade,
            manifest_hash text not null, started_at text not null, finished_at text not null,
            result text not null, complete_snapshot integer not null, seen_count integer not null,
            new_count integer not null, stop_reason text not null, anchor_hit_note_id text,
            anchor_count_at_start integer not null, initial_fetch_limit_at_start integer,
            checkpoint_committed integer not null, checkpoint_version integer not null,
            error_category text)""",
        """create table if not exists xhs_anchor_candidates (
            source_id integer not null references xhs_source_accounts(source_id) on delete cascade,
            scan_id text not null references xhs_sync_runs(scan_id) on delete cascade,
            rank integer not null, note_id text not null,
            primary key(source_id, scan_id, rank), unique(source_id, scan_id, note_id))""",
        "create index if not exists idx_xhs_runs_source on xhs_sync_runs(source_id, finished_at desc)",
    ]
    for statement in statements:
        conn.execute(statement)
    conn.execute("insert or ignore into xhs_schema_migrations values (1, datetime('now'))")
    columns = {row[1] for row in conn.execute("pragma table_info(xhs_source_accounts)")}
    additions = {
        "refresh_interval_minutes": "integer not null default 720 check(refresh_interval_minutes between 23 and 525600)",
        "refresh_enabled": "integer not null default 0 check(refresh_enabled in (0, 1))",
    }
    for name, declaration in additions.items():
        if name not in columns:
            conn.execute(f"alter table xhs_source_accounts add column {name} {declaration}")
    conn.execute("""create table if not exists xhs_schedule_state (
        source_id integer primary key references xhs_source_accounts(source_id) on delete cascade,
        config_version integer not null, next_run_at text not null,
        jitter_minutes integer not null check(abs(jitter_minutes) between 1 and 22))""")
    conn.execute("insert or ignore into xhs_schema_migrations values (2, datetime('now'))")
    for name, declaration in {
        'verification_passed': 'integer not null default 0',
        'ordering_stable': 'integer not null default 0',
        'full_scan_interval_days': f'integer not null default 7 check(full_scan_interval_days between 1 and {FULL_SCAN_INTERVAL_MAX})',
        'media_quota_mb': 'integer not null default 2048 check(media_quota_mb between 64 and 102400)',
    }.items():
        if name not in columns:
            conn.execute(f'alter table xhs_source_accounts add column {name} {declaration}')
    conn.execute('''create table if not exists xhs_note_downloads (
        post_id integer primary key references site_posts(id) on delete cascade,
        status text not null default 'pending', last_scan_id text not null,
        error_category text, updated_at text not null)''')
    conn.execute('''create table if not exists xhs_asset_details (
        asset_id integer primary key references site_assets(id) on delete cascade,
        sha256 text not null, bytes integer not null, width integer not null default 0,
        height integer not null default 0, variant text not null)''')
    if 'duration' not in {row[1] for row in conn.execute('pragma table_info(xhs_asset_details)')}:
        conn.execute('alter table xhs_asset_details add column duration real not null default 0')
    conn.execute('''create table if not exists xhs_jobs (
        job_id text primary key, source_id integer not null references xhs_source_accounts(source_id) on delete cascade,
        task_id integer references task_runs(id) on delete set null, state text not null,
        force_full integer not null, media_only integer not null, created_at text not null,
        finished_at text, error_category text)''')
    conn.execute('''insert or ignore into xhs_note_downloads(post_id,status,last_scan_id,updated_at)
        select l.site_post_id,'pending',coalesce(a.last_checkpoint_scan_id,'migration'),l.last_seen_at
        from xhs_like_state l join xhs_source_accounts a on a.source_id=l.source_id''')
    conn.execute("insert or ignore into xhs_schema_migrations values (3, datetime('now'))")
    if 'media_revision' not in {row[1] for row in conn.execute('pragma table_info(xhs_note_downloads)')}:
        conn.execute('alter table xhs_note_downloads add column media_revision integer not null default 0')
    if not conn.execute('select 1 from xhs_schema_migrations where version=4').fetchone():
        conn.execute("""update xhs_note_downloads set status='pending',error_category=null
            where status='ready' and post_id in (select l.site_post_id from xhs_like_state l
            join xhs_source_accounts a on a.source_id=l.source_id where a.auth_mode='browser')""")
        conn.execute("insert into xhs_schema_migrations values (4, datetime('now'))")
    if not conn.execute('select 1 from xhs_schema_migrations where version=5').fetchone():
        conn.execute("""update xhs_note_downloads set status='pending',error_category=null
            where status='blocked' and error_category='FILTERED' and post_id in
            (select id from site_posts where is_blocked=0)""")
        conn.execute("insert into xhs_schema_migrations values (5, datetime('now'))")
    anchor_columns = {row[1] for row in conn.execute('pragma table_info(xhs_anchor_candidates)')}
    for name, declaration in {
        'title_prefix': "text not null default ''",
        'author_id': "text not null default ''",
        'note_type': "text not null default ''",
        'asset_count': "integer not null default 0",
    }.items():
        if name not in anchor_columns:
            conn.execute(f'alter table xhs_anchor_candidates add column {name} {declaration}')
    _widen_full_scan_interval(conn)
    conn.execute("insert or ignore into xhs_schema_migrations values (6, datetime('now'))")
    # Re-resolve older image notes once: previous parsers could mark a Live Photo
    # note ready after saving only its stills. Keep all successfully cached files.
    if not conn.execute('select 1 from xhs_schema_migrations where version=7').fetchone():
        conn.execute("""update xhs_note_downloads set status='pending',error_category=null
            where status='ready' and media_revision<2 and post_id in
            (select l.site_post_id from xhs_like_state l join xhs_source_accounts a on a.source_id=l.source_id
             where a.auth_mode='browser' and l.note_type!='video')""")
        conn.execute("insert into xhs_schema_migrations values (7, datetime('now'))")
    if 'author_name' not in {row[1] for row in conn.execute('pragma table_info(xhs_like_state)')}:
        conn.execute("alter table xhs_like_state add column author_name text not null default ''")
    conn.execute("insert or ignore into xhs_schema_migrations values (8, datetime('now'))")
