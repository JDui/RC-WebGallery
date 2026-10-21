"""Versioned database modules and small, indexed browse records.

SQLite's B trees already divide tables into disk pages. Keep large media metadata
in app.db and move the hot control/browse tables to separate page caches instead
of splitting the same logical table into arbitrary files.
"""
from __future__ import annotations

import re
import sqlite3

MODULES = {
    'control': ('settings', 'auth_state', 'subscriptions', 'sidebar_count_cache'),
    'browse': ('folder_index', 'pair_index', 'gallery_page_cache', 'browse_queries', 'browse_boundaries'),
}


def schema_for(table: str, enabled: bool) -> str:
    return next((name for name, tables in MODULES.items() if table in tables), 'main') if enabled else 'main'


def module_ddl(sql: str, enabled: bool) -> str:
    if not enabled:
        return sql
    for module, tables in MODULES.items():
        for table in tables:
            sql = re.sub(rf'(create table if not exists )({table})\b', rf'\1{module}.\2', sql)
            sql = re.sub(rf'(create index if not exists )(\w+)( on {table}\b)', rf'\1{module}.\2\3', sql)
    # A derived index has no cross-database FK; deletion is explicit in Database.
    for table in ('folder_index', 'pair_index'):
        sql = re.sub(rf'(create table if not exists browse\.{table} \(.*?\n                \);)',
                     lambda m: m[0].replace(' references folders(folder_name) on delete cascade', ''), sql, flags=re.S)
    return sql


def install_hot_indexes(db, conn: sqlite3.Connection) -> None:
    for table in ('folder_index', 'pair_index'):
        columns = {row['name'] for row in conn.execute(f'pragma table_info({table})')}
        if 'imported_at' not in columns:
            db._ensure_column(conn, table, 'imported_at', "text not null default ''")
            conn.execute(f"update {table} set imported_at=coalesce((select created_at from folders f where f.folder_name={table}.folder_name),'') where imported_at=''")
        schema = schema_for(table, db.modules_enabled)
        tail = ', pair_index asc' if table == 'pair_index' else ''
        for prefix, columns in (('imported', 'imported_at desc, folder_name desc'),
                                ('subscription_imported', 'subscription_uid, imported_at desc, folder_name desc')):
            conn.execute(f'create index if not exists {schema}.idx_{table}_{prefix} on {table}({columns}{tail})')
        if table == 'pair_index':
            for prefix, columns in (('pub_asc', 'pub_ts asc, folder_name asc'),
                                    ('subscription_pub_asc', 'subscription_uid, pub_ts asc, folder_name asc')):
                conn.execute(f'create index if not exists {schema}.idx_{table}_{prefix} on {table}({columns},pair_index asc)')
    conn.execute('create index if not exists idx_task_runs_type_id on task_runs(task_type, id desc)')
    conn.execute('''create table if not exists folder_locations (
        folder_name text not null, media_type text not null, rel_dir text not null unique,
        primary key(folder_name, media_type))''')
    conn.execute('''create table if not exists data_layout (id integer primary key check(id=1), version integer not null)''')
    conn.execute('insert or ignore into data_layout values(1,0)')
    schema = schema_for('folder_index', db.modules_enabled)
    conn.execute(f'''create table if not exists {schema}.browse_queries (
        query_key text primary key, revision text not null, total integer not null)''')
    conn.execute(f'''create table if not exists {schema}.browse_boundaries (
        query_key text not null, page integer not null, sort_value, folder_name text not null,
        pair_index integer not null, primary key(query_key,page))''')
    # Transactional revision counters replace COUNT/MAX/SUM on every cache read.
    watched = ('folders', 'assets', 'folder_index', 'pair_index', 'site_sources', 'subscriptions', 'site_posts', 'site_assets')
    for schema in ('main', 'control', 'browse') if db.modules_enabled else ('main',):
        conn.execute(f'create table if not exists {schema}.content_revision(id integer primary key, revision integer not null)')
        conn.execute(f'insert or ignore into {schema}.content_revision values(1,0)')
    for table in watched:
        schema = schema_for(table, db.modules_enabled)
        for action in ('insert', 'update', 'delete'):
            conn.execute(f'''create trigger if not exists {schema}.rev_{table}_{action}
                after {action} on {table} begin
                update content_revision set revision=revision+1 where id=1; end''')


def split_modules(db, progress) -> None:
    if db.modules_enabled:
        return
    # DELETE journals supply SQLite's super-journal atomic commit across files.
    # No application connection may be open during this exclusive maintenance.
    conn = sqlite3.connect(db.db_path, timeout=60)
    try:
        conn.execute('pragma foreign_keys=off')
        conn.execute('pragma journal_mode=delete')
        for module in MODULES:
            conn.execute(f'attach database ? as {module}', (str(db.db_path.with_name(f'{module}.db')),))
            conn.execute(f'pragma {module}.journal_mode=delete')
            conn.execute(f'pragma {module}.synchronous=full')
        conn.execute('pragma synchronous=full')
        conn.execute('begin immediate')
        for table in ('gallery_page_cache', 'browse_queries', 'browse_boundaries'):
            conn.execute(f'delete from main.{table}')
        for module, tables in MODULES.items():
            for table in tables:
                ddl = conn.execute("select sql from main.sqlite_master where type='table' and name=?", (table,)).fetchone()[0]
                ddl = re.sub(rf'CREATE TABLE (?:"{table}"|{table})', f'CREATE TABLE {module}.{table}', ddl, count=1, flags=re.I)
                ddl = ddl.replace('primary key references folders(folder_name) on delete cascade', 'primary key')
                ddl = ddl.replace('text not null references folders(folder_name) on delete cascade', 'text not null')
                conn.execute(ddl)
                conn.execute(f'insert into {module}.{table} select * from main.{table}')
                before = conn.execute(f'select count(*) from main.{table}').fetchone()[0]
                after = conn.execute(f'select count(*) from {module}.{table}').fetchone()[0]
                if before != after:
                    raise RuntimeError(f'{table}: 模块复制数量不一致')
                conn.execute(f'drop table main.{table}')
                progress(f'已拆分 {table}')
        conn.execute('update data_layout set version=1 where id=1')
        conn.commit()
        db.modules_enabled = True
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    # Recreate normal secondary indexes in their new modules.
    db.init()
    with db.connect() as conn:
        for schema in ('main', 'control', 'browse'):
            conn.execute(f'analyze {schema}')
