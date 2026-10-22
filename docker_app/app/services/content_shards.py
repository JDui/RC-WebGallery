"""Version 3 content partitions. Only explicit, exclusive maintenance creates them.

The catalog and all partitions use rollback journals: an ordinary transaction
therefore has one SQLite super-journal, including its derived browse updates.
Logical UNION views are read-only; writes are routed to physical tables here.
No SQL is retried after a failed commit and no missing database is recreated.
"""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import uuid4

VERSION = 3
SHARDS = ('bilibili', 'sites', 'xiaohongshu')
SHARED = {
    'folders': SHARDS, 'assets': SHARDS, 'livephoto_pairs': SHARDS,
    'site_sources': SHARDS[1:], 'site_posts': SHARDS[1:],
    'site_assets': SHARDS[1:], 'site_filter_logs': SHARDS[1:],
}
AUTO_IDS = {'assets', 'site_sources', 'site_posts', 'site_assets', 'site_filter_logs'}


def checked_path(root: Path, name: str, *, create=False) -> Path:
    path = root / f'{name}.db'
    if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
        raise RuntimeError(f'数据库不能是符号链接：{path.name}')
    if not create and (not path.is_file() or path.stat().st_size == 0):
        raise RuntimeError(f'数据库模块缺失或为空：{path.name}；请恢复同一份完整备份')
    return path


def verify_identity(conn, schema, dataset_id, role):
    identity = sqlite3.Connection.execute(conn, f'select dataset_id,role,version from {schema}.database_identity').fetchall()
    if len(identity) != 1 or tuple(identity[0]) != (dataset_id, role, VERSION):
        raise RuntimeError(f'{role}.db 不属于当前数据集，已阻止写入')


def attach(conn, root, name, *, create=False, dataset_id=None):
    path = checked_path(root, name, create=create)
    uri = path.resolve().as_uri() + ('?mode=rwc' if create else '?mode=rw')
    sqlite3.Connection.execute(conn, f'attach database ? as {name}', (uri,))
    if dataset_id is not None:
        verify_identity(conn, name, dataset_id, name)
    sqlite3.Connection.execute(conn, f'pragma {name}.journal_mode=delete')
    sqlite3.Connection.execute(conn, f'pragma {name}.synchronous=extra')


def classify_folder(row, sources):
    name = str(row['folder_name'])
    uid = str(row.get('subscription_uid') or '')
    if uid.startswith('site:'):
        try:
            return sources[int(uid.split(':', 1)[1])]
        except (KeyError, ValueError):
            # A removed subscription must not make its already archived media
            # disappear. The durable folder name still identifies XHS archives.
            return 'xiaohongshu' if name.startswith('xhs_') else 'sites'
    if name.startswith('xhs_'):
        return 'xiaohongshu'
    if name.startswith('site_') or str(row.get('source_dynamic_id') or '').startswith('site:'):
        return 'sites'
    return 'bilibili'


class ShardConnection(sqlite3.Connection):
    """Preserve native cursors/lastrowid/UPSERT while routing logical DML.

    Only the application's single-row VALUES inserts are accepted. Unsupported
    bulk syntax fails before writing rather than guessing a destination.
    """
    shard_columns: dict

    def _raw(self, sql, params=()):
        return sqlite3.Connection.execute(self, sql, params)

    def _route(self, table, values):
        if table in ('folders', 'assets', 'livephoto_pairs'):
            key = str(values.get('folder_name') or '')
            if not key:
                raise ValueError('分库写入缺少 folder_name')
            found = self._raw('select shard from content_catalog where folder_name=?', (key,)).fetchone()
            if found:
                return found[0]
            if table == 'assets':
                raise sqlite3.IntegrityError('媒体所属贴文不存在')
            uid = str(values.get('subscription_uid') or '')
            sources = {}
            if uid.startswith('site:'):
                try:
                    source_id = int(uid.split(':', 1)[1])
                    route = self._raw("select shard from entity_routes where entity='site_sources' and id=?", (source_id,)).fetchone()
                    if route:
                        sources[source_id] = route[0]
                except ValueError:
                    pass
            shard = classify_folder(values, sources)
            self._raw('insert into content_catalog values(?,?)', (key, shard))
            return shard
        if table == 'site_sources':
            return 'xiaohongshu' if values.get('source_type') == 'xhs_likes' else 'sites'
        parent, key = ('site_sources', 'source_id') if table in ('site_posts', 'site_filter_logs') else ('site_posts', 'post_id')
        identity = values.get(key)
        if identity is None and table == 'site_filter_logs':
            return 'sites'
        row = self._raw('select shard from entity_routes where entity=? and id=?', (parent, identity)).fetchone()
        if not row:
            if table == 'site_filter_logs':
                return 'sites'
            raise sqlite3.IntegrityError(f'分库写入缺少父记录：{parent}/{identity}')
        return row[0]

    def execute(self, sql, parameters=()):
        head = re.match(r'\s*(insert(?:\s+or\s+\w+)?\s+into|update|delete\s+from)\s+([a-z_]\w*)\b', sql, re.I)
        routed = head and (head[2].lower() in SHARED or
                          (head[1].lower() == 'delete from' and head[2].lower() == 'task_runs'))
        if not routed:
            return self._raw(sql, parameters)
        if not self.in_transaction:
            self._raw('begin immediate')
        self._raw('savepoint shard_statement')
        try:
            result = self._execute_routed(sql, parameters)
            self._raw('release shard_statement')
            return result
        except BaseException:
            if self.in_transaction:
                self._raw('rollback to shard_statement')
                self._raw('release shard_statement')
            raise

    def _execute_routed(self, sql, parameters=()):
        head = re.match(r'\s*(insert(?:\s+or\s+\w+)?\s+into|update|delete\s+from)\s+([a-z_]\w*)\b', sql, re.I)
        if head and head[1].lower() == 'delete from' and head[2].lower() == 'task_runs':
            # SQLite cannot declare an FK from a content file to app.db.
            # Keep its original ON DELETE SET NULL semantics in this transaction.
            if not self.in_transaction:
                self._raw('begin immediate')
            suffix = sql[head.end(2):]
            self._raw('update xiaohongshu.xhs_jobs set task_id=null where task_id in (select id from main.task_runs' + suffix + ')', parameters)
        if not head or head[2].lower() not in SHARED:
            return self._raw(sql, parameters)
        table = head[2].lower()
        if head[1].lower().startswith('insert'):
            match = re.match(r'\s*insert(?:\s+or\s+\w+)?\s+into\s+\w+\s*\(([^)]+)\)\s*values\s*\(', sql, re.I)
            if not match:
                raise ValueError('分库只接受显式列名单行 VALUES 写入')
            start = match.end()
            depth, quote, end = 1, None, start
            while end < len(sql):
                char = sql[end]
                if quote:
                    if char == quote:
                        if end + 1 < len(sql) and sql[end + 1] == quote:
                            end += 1
                        else:
                            quote = None
                elif char in "'\"":
                    quote = char
                elif char == '(':
                    depth += 1
                elif char == ')':
                    depth -= 1
                    if not depth:
                        break
                end += 1
            if depth or sql[end + 1:].lstrip().startswith(','):
                raise ValueError('分库拒绝无法识别的多行写入')
            expressions = sql[start:end]
            bound = parameters if isinstance(parameters, dict) else parameters[:len(re.findall(r'\?(?!\w)', re.sub(r"'(?:''|[^'])*'", "''", expressions)))]
            row = self._raw('select ' + expressions, bound).fetchone()
            columns = [name.strip().strip('"') for name in match[1].split(',')]
            values = dict(zip(columns, row))
            # Route/sequence reservations must roll back with the physical DML.
            if not self.in_transaction:
                self._raw('begin immediate')
            shard = self._route(table, values)
            if table in AUTO_IDS:
                identity = values.get('id')
                if identity is None:
                    identity = self._raw('update shard_sequences set value=value+1 where name=? returning value', (table,)).fetchone()[0]
                    # Inject a bound id, never interpolate user values into SQL.
                    col_start = sql.index('(', head.end()) + 1
                    if isinstance(parameters, dict):
                        if '__shard_id' in parameters:
                            raise ValueError('保留的分库参数名')
                        parameters = {**parameters, '__shard_id': identity}
                        sql = sql[:start] + ':__shard_id,' + sql[start:]
                    else:
                        parameters = (identity, *parameters)
                        sql = sql[:start] + '?,' + sql[start:]
                    sql = sql[:col_start] + 'id,' + sql[col_start:]
                else:
                    self._raw('update shard_sequences set value=max(value,?) where name=?', (identity, table))
                existing = self._raw('select shard from entity_routes where entity=? and id=?', (table, identity)).fetchone()
                if existing and existing[0] != shard:
                    raise sqlite3.IntegrityError('全局 ID 已属于另一分库')
                self._raw('insert or ignore into entity_routes values(?,?,?)', (table, identity, shard))
            rewritten = sql[:head.start(2)] + f'{shard}.{table}' + sql[head.end(2):]
            result = self._raw(rewritten, parameters)
        else:
            if not self.in_transaction:
                self._raw('begin immediate')
            affected = 0
            result = None
            for shard in SHARED[table]:
                rewritten = sql[:head.start(2)] + f'{shard}.{table}' + sql[head.end(2):]
                result = self._raw(rewritten, parameters)
                affected += max(0, result.rowcount)
            result = CountCursor(result, affected)
        if table == 'folders':
            self._raw('update browse.gallery_state set expected_folders=' + '+'.join(
                f"(select rows from {shard}.table_counts where name='folders')" for shard in SHARDS) + ' where id=1')
        return result

    def executemany(self, sql, seq_of_parameters):
        if re.match(r'\s*(insert|update|delete)', sql, re.I):
            total, result = 0, None
            for parameters in seq_of_parameters:
                result = self.execute(sql, parameters)
                total += max(0, result.rowcount)
            return CountCursor(result, total)
        return sqlite3.Connection.executemany(self, sql, seq_of_parameters)


class CountCursor:
    def __init__(self, cursor, count):
        self._cursor, self.rowcount = cursor, count

    def __getattr__(self, key):
        return getattr(self._cursor, key)

    def __iter__(self):
        return iter(self._cursor)


def install_views(conn, columns):
    for table, shards in SHARED.items():
        names = ','.join(f'"{name}"' for name in columns[table])
        conn._raw(f'create temp view "{table}" as ' + ' union all '.join(
            f'select {names} from {shard}."{table}"' for shard in shards))
    for table, keys in (('folders', ('top_dynamic_id', 'source_dynamic_id')),
                        ('livephoto_pairs', ('content_id',)), ('site_sources', ('slug',))):
        for shard in SHARED[table]:
            conflicts = ' or '.join('exists(select 1 from ' + other + '.' + table + ' t where ' +
                ' and '.join(f't.{key}=new.{key}' for key in keys) + ')' for other in SHARED[table] if other != shard)
            for event in ('insert', 'update'):
                conn._raw(f'''create temp trigger unique_{shard}_{table}_{event} before {event} on {shard}.{table}
                    when {conflicts} begin select raise(abort,'跨分库唯一键冲突'); end''')
    for table, shards in SHARED.items():
        keys = (('id',) if table in AUTO_IDS else ('folder_name',))
        if table == 'assets':
            keys += ('folder_name',)
        elif table in ('site_posts', 'site_assets'):
            keys += ('source_id' if table == 'site_posts' else 'post_id',)
        for shard in shards:
            condition = ' or '.join(f'new.{key} is not old.{key}' for key in keys)
            conn._raw(f'''create temp trigger immutable_{shard}_{table} before update of {','.join(keys)} on {shard}.{table}
                when {condition} begin select raise(abort,'不能直接更改分库实体标识'); end''')
    for shard in ('sites', 'xiaohongshu'):
        comparison = '!=' if shard == 'xiaohongshu' else '='
        conn._raw(f'''create temp trigger source_partition_{shard} before update of source_type on {shard}.site_sources
            when new.source_type {comparison} 'xhs_likes' begin select raise(abort,'不能直接更改来源分库'); end''')
    # Derived author data changes in the same cross-file transaction as the
    # source. TEMP triggers may address browse tables from a content file.
    from app.services.creators import site_creator_url
    from urllib.parse import quote
    conn.create_function('creator_home', 2, site_creator_url, deterministic=True)
    conn.create_function('creator_xhs_home', 1, lambda value:
                         'https://www.xiaohongshu.com/user/profile/' + quote(str(value).strip(), safe='') if value else '', deterministic=True)
    for action in ('insert', 'update of author_id,author_name'):
        event = action.split()[0]
        changes = ';'.join(f'''update {table} set author_id=coalesce(new.author_id,''),author_name=coalesce(new.author_name,''),
            creator_name=coalesce(nullif(trim(new.author_name),''),case when trim(coalesce(new.author_id,''))!='' then '作者 '||trim(new.author_id) else '未知作者' end),
            creator_url=creator_xhs_home(new.author_id) where folder_name='xhs_'||new.source_id||'_'||new.note_id''' for table in ('folder_index', 'pair_index'))
        conn._raw(f'create temp trigger author_{event} after {action} on xiaohongshu.xhs_like_state begin {changes}; end')
    conn._raw('''create temp trigger source_author_update after update of name,entry_url,source_type on sites.site_sources begin
        update folder_index set creator_name=new.name,creator_url=creator_home(new.entry_url,new.source_type) where subscription_uid='site:'||new.id;
        update pair_index set creator_name=new.name,creator_url=creator_home(new.entry_url,new.source_type) where subscription_uid='site:'||new.id;
        end''')
    for event in ('insert', 'update of task_id'):
        conn._raw(f'''create temp trigger job_task_{event.split()[0]} before {event} on xiaohongshu.xhs_jobs
            when new.task_id is not null and not exists(select 1 from main.task_runs where id=new.task_id)
            begin select raise(abort,'小红书任务引用不存在'); end''')


def check_database(conn, schemas):
    for schema in schemas:
        errors = [row[0] for row in conn.execute(f'pragma {schema}.quick_check')]
        if errors != ['ok']:
            raise RuntimeError(f'{schema} 数据库检查失败：{errors[:3]}')
        if conn.execute(f'pragma {schema}.foreign_key_check').fetchone():
            raise RuntimeError(f'{schema} 外键检查失败')


def check_catalog(conn):
    """Full relational validation at startup/maintenance, never per card read.

    Routes may retain deleted IDs as tombstones; every existing row must have
    its correct route and sequence. This also detects a valid but stale catalog.
    """
    if conn.execute('''select 1 from xiaohongshu.xhs_jobs j left join main.task_runs t on t.id=j.task_id
        where j.task_id is not null and t.id is null limit 1''').fetchone():
        raise RuntimeError('小红书任务引用不一致')
    for table, shards in SHARED.items():
        if table in ('folders', 'assets', 'livephoto_pairs'):
            for shard in shards:
                if conn.execute(f'''select 1 from {shard}.{table} t left join main.content_catalog c
                    on c.folder_name=t.folder_name where c.shard is null or c.shard!=? limit 1''', (shard,)).fetchone():
                    raise RuntimeError(f'{shard}.{table} 路由表不一致')
        if table in AUTO_IDS:
            for shard in shards:
                if conn.execute(f'''select 1 from {shard}.{table} t left join main.entity_routes r
                    on r.entity=? and r.id=t.id where r.shard is null or r.shard!=? limit 1''', (table, shard)).fetchone():
                    raise RuntimeError(f'{shard}.{table} 全局 ID 路由不一致')
            maximum = max(conn.execute(f'select coalesce(max(id),0) from {s}.{table}').fetchone()[0] for s in shards)
            sequence = conn.execute('select value from main.shard_sequences where name=?', (table,)).fetchone()
            if not sequence or sequence[0] < maximum:
                raise RuntimeError(f'{table} 全局 ID 序列不一致')
    expected = conn.execute('select expected_folders from browse.gallery_state where id=1').fetchone()
    total = 0
    for shard in SHARDS:
        actual = conn.execute(f'select count(*) from {shard}.folders').fetchone()[0]
        counter = conn.execute(f"select rows from {shard}.table_counts where name='folders'").fetchone()
        if not counter or counter[0] != actual:
            raise RuntimeError(f'{shard} 贴文计数不一致')
        total += actual
    if not expected or expected[0] != total:
        raise RuntimeError('浏览目录与内容库计数不一致')
    for table in ('folder_index', 'pair_index'):
        counter = conn.execute('select rows from browse.table_counts where name=?', (table,)).fetchone()
        if not counter or counter[0] != conn.execute(f'select count(*) from browse.{table}').fetchone()[0]:
            raise RuntimeError(f'{table} 索引计数不一致')


def split_content(db, progress=lambda _: None):
    if db.shards_enabled:
        return
    if not db.modules_enabled or not db.exclusive_maintenance:
        raise RuntimeError('内容分库必须通过独占升级入口执行')
    db.close()
    root = db.db_path.parent
    with closing(sqlite3.connect(db.db_path, timeout=60, uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('pragma journal_mode=delete')
        conn.execute('pragma synchronous=extra')
        conn.execute('pragma foreign_keys=off')
        for name in ('control', 'browse'):
            attach(conn, root, name)
        check_database(conn, ('main', 'control', 'browse'))
        sources = {row['id']: ('xiaohongshu' if row['source_type'] == 'xhs_likes' else 'sites')
                   for row in conn.execute('select id,source_type from site_sources')}
        extra_tables = [row[0] for row in conn.execute("select name from sqlite_master where type='table' and name like 'xhs_%'")]
        for shard in SHARDS:
            attach(conn, root, shard, create=True)
            if conn.execute(f"select name from {shard}.sqlite_master where name not like 'sqlite_%'").fetchone():
                raise RuntimeError(f'目标分库非空，保留现场：{shard}.db')
        conn.execute('begin immediate')
        try:
            conn.execute('create table content_catalog(folder_name text primary key, shard text not null check(shard in (\'bilibili\',\'sites\',\'xiaohongshu\')))')
            conn.execute('create table entity_routes(entity text not null,id integer not null,shard text not null,primary key(entity,id))')
            conn.execute('create table shard_sequences(name text primary key,value integer not null)')
            conn.execute('create table shard_manifest(id integer primary key check(id=1),dataset_id text not null,columns_json text not null)')
            routes = {row['folder_name']: classify_folder(dict(row), sources) for row in conn.execute('select * from folders')}
            for row in conn.execute('select distinct folder_name from livephoto_pairs'):
                routes.setdefault(row[0], classify_folder({'folder_name': row[0]}, sources))
            conn.executemany('insert into content_catalog values(?,?)', routes.items())
            columns = {}
            token = uuid4().hex
            for schema, role in (('main', 'app'), ('control', 'control'), ('browse', 'browse')):
                conn.execute(f'create table {schema}.database_identity(dataset_id text not null,role text not null,version integer not null)')
                conn.execute(f'insert into {schema}.database_identity values(?,?,?)', (token, role, VERSION))
            for shard in SHARDS:
                conn.execute(f'create table {shard}.database_identity(dataset_id text not null,role text not null,version integer not null)')
                conn.execute(f'insert into {shard}.database_identity values(?,?,?)', (token, shard, VERSION))
                conn.execute(f'create table {shard}.content_revision(id integer primary key,revision integer not null)')
                conn.execute(f'insert into {shard}.content_revision values(1,0)')
                conn.execute(f'create table {shard}.table_counts(name text primary key,rows integer not null)')
            tables = list(SHARED) + extra_tables
            for table in tables:
                ddl = conn.execute("select sql from main.sqlite_master where type='table' and name=?", (table,)).fetchone()[0]
                columns[table] = [r['name'] for r in conn.execute(f'pragma main.table_info("{table}")')]
                targets = SHARED.get(table, ('xiaohongshu',))
                for shard in targets:
                    changed = re.sub(r'(?i)^(create table\s+)(?:"' + table + '"|' + table + ')', rf'\1{shard}."{table}"', ddl, count=1)
                    changed = changed.replace(' references task_runs(id) on delete set null', '')
                    conn.execute(changed)
                    if table in ('folders', 'assets', 'livephoto_pairs'):
                        condition = f"folder_name in (select folder_name from content_catalog where shard='{shard}')"
                    elif table == 'site_sources':
                        condition = "source_type='xhs_likes'" if shard == 'xiaohongshu' else "source_type!='xhs_likes'"
                    elif table == 'site_posts':
                        condition = f'source_id in (select id from {shard}.site_sources)'
                    elif table == 'site_assets':
                        condition = f'post_id in (select id from {shard}.site_posts)'
                    elif table == 'site_filter_logs':
                        condition = ('source_id in (select id from xiaohongshu.site_sources)' if shard == 'xiaohongshu'
                                     else 'source_id is null or source_id not in (select id from xiaohongshu.site_sources)')
                    else:
                        condition = '1=1'
                    conn.execute(f'insert into {shard}."{table}" select * from main."{table}" where {condition}')
                    for index in conn.execute("select sql from main.sqlite_master where type='index' and tbl_name=? and sql is not null", (table,)).fetchall():
                        conn.execute(re.sub(r'(?i)^(create (?:unique )?index\s+)(\S+)', rf'\1{shard}.\2', index[0], count=1))
                    if conn.execute(f'select * from main."{table}" where {condition} except select * from {shard}."{table}"').fetchone():
                        raise RuntimeError(f'{table}: 分库内容校验不一致')
                    if table in AUTO_IDS:
                        conn.execute(f'insert into entity_routes select ?,id,? from {shard}."{table}"', (table, shard))
                    if table == 'folders':
                        conn.execute(f"insert into {shard}.table_counts select 'folders',count(*) from {shard}.folders")
                        for action, delta in (('insert', 1), ('delete', -1)):
                            conn.execute(f'''create trigger {shard}.count_folders_{action} after {action} on folders begin
                                update table_counts set rows=rows+({delta}) where name='folders'; end''')
                    if table in ('folders', 'assets', 'site_sources', 'site_posts', 'site_assets'):
                        for action in ('insert', 'update', 'delete'):
                            conn.execute(f'''create trigger {shard}.rev_{table}_{action} after {action} on "{table}" begin
                                update content_revision set revision=revision+1 where id=1; end''')
                total = sum(conn.execute(f'select count(*) from {s}."{table}"').fetchone()[0] for s in targets)
                if total != conn.execute(f'select count(*) from main."{table}"').fetchone()[0]:
                    raise RuntimeError(f'{table}: 分库数量校验不一致')
                progress(f'已校验内容分库：{table}')
            for table in AUTO_IDS:
                conn.execute(f'''insert into shard_sequences select ?,max(coalesce(max(id),0),
                    coalesce((select seq from main.sqlite_sequence where name=?),0)) from main."{table}"''', (table, table))
            conn.execute('insert into shard_manifest values(1,?,?)', (token, json.dumps(columns)))
            conn.execute('create table browse.gallery_state(id integer primary key check(id=1),expected_folders integer not null)')
            conn.execute('insert into browse.gallery_state select 1,count(*) from main.folders')
            for table in ('folder_index', 'pair_index'):
                conn.execute(f"update browse.{table} set source_kind=coalesce((select case shard when 'xiaohongshu' then 'xhs' when 'sites' then 'site' else 'up' end from main.content_catalog c where c.folder_name={table}.folder_name),'up')")
                conn.execute(f"update browse.{table} set video_count=(select count(*) from main.assets a where a.folder_name={table}.folder_name and a.media_type='video')")
            # Enrich in bounded batches, without decoding media or rebuilding
            # thumbnails. Preserve titles, original timestamps and file bytes.
            from app.services.creators import attach_creators
            cursor = conn.execute('select i.*,f.metadata_json from browse.folder_index i join main.folders f using(folder_name)')
            while batch := cursor.fetchmany(256):
                items = [dict(row) for row in batch]
                attach_creators(db, items, conn=conn)
                for item in items:
                    metadata = json.loads(item['metadata_json'] or '{}')
                    original = metadata.get('site_post_url') or metadata.get('original_url') or ''
                    if not original and str(item['source_dynamic_id']).startswith('site:'):
                        parts = str(item['source_dynamic_id']).split(':')
                        if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
                            found = conn.execute('select url from main.site_posts where source_id=? and id=?', (int(parts[1]), int(parts[2]))).fetchone()
                            original = found[0] if found else ''
                    values = (item.get('author_id', ''), item.get('author_name', ''), original,
                              item.get('creator_name', ''), item.get('creator_url', ''), item['folder_name'])
                    for table in ('folder_index', 'pair_index'):
                        conn.execute(f'update browse.{table} set author_id=?,author_name=?,original_url=?,creator_name=?,creator_url=? where folder_name=?', values)
            # Child tables must disappear before their parents. Foreign keys are
            # disabled only on this isolated maintenance connection.
            for table in reversed(tables):
                conn.execute(f'drop table main."{table}"')
            check_database(conn, (*SHARDS, 'main', 'control', 'browse'))
            check_catalog(conn)
            conn.execute('update data_layout set version=3 where id=1')
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    db.shards_enabled = True
    db.layout_version = VERSION
    db._shard_columns = columns
    db._dataset_id = token
