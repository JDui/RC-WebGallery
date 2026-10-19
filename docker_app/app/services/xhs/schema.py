from __future__ import annotations

import sqlite3


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
        'full_scan_interval_days': 'integer not null default 7 check(full_scan_interval_days between 1 and 365)',
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
