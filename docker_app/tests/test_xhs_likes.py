from copy import deepcopy
from datetime import datetime, timedelta
import json
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from app.db import Database
from app.services.site_parser import SourceParser
from app.services.site_syncer import SiteSyncManager
from app.services.utils import TIMEZONE, now_iso
from app.services.xhs.models import MAX_MANIFEST_BYTES, XhsManifest, XhsNote, XhsValidationError
from app.services.xhs.sync import ScanPage, XhsConflict, XhsSyncService


OWNER = "local-account-1"


def note(note_id, **changes):
    return {"note_id": note_id, "url": f"https://www.xiaohongshu.com/explore/{note_id}",
            "title": f"笔记 {note_id}", "author_id": "author-1", "type": "image",
            "published_at": "2020-01-01T12:00:00+08:00", "observed_at": now_iso(),
            "assets": [{"ordinal": 0, "kind": "image", "candidate_ref": "temporary-ref"}], **changes}


def manifest(ids, **changes):
    notes = [note(key) for key in ids]
    return {"schema_version": 1, "source_kind": "xhs_my_likes", "owner_key": OWNER,
            "scan_id": str(uuid4()), "started_at": (datetime.now(TIMEZONE) - timedelta(minutes=1)).isoformat(),
            "completed_at": now_iso(), "complete_snapshot": False, "stop_reason": "manual_import",
            "notes": notes, **changes}


def encode(value):
    return json.dumps(value, ensure_ascii=False).encode()


def page(ids, end=False, **changes):
    return ScanPage(tuple(XhsNote.parse(note(key)) for key in ids), end_of_feed=end, **changes)


@pytest.fixture
def service(tmp_path):
    db = Database(tmp_path / "app.db")
    db.init()
    return XhsSyncService(db)


def source(service, **changes):
    return service.create_source({"name": "本人点赞", "owner_key": OWNER, "initial_fetch_limit": None, **changes})


def states(service, source_id):
    with service.db.connect() as conn:
        return {row["note_id"]: dict(row) for row in conn.execute("select * from xhs_like_state where source_id = ?", (source_id,))}


def scan(service, source_id, pages, **kwargs):
    return service.scan_pages(source_id, OWNER, pages, **kwargs)


def test_versioned_migration_is_additive_and_idempotent(service):
    with service.db.connect() as conn:
        conn.execute("update auth_state set cookie = 'bili-test-sentinel' where id = 1")
    item = source(service)
    service.db.init()
    with service.db.connect() as conn:
        assert conn.execute("select count(*) from xhs_schema_migrations").fetchone()[0] == 3
        assert conn.execute("select cookie from auth_state").fetchone()[0] == "bili-test-sentinel"
    assert service.get_source(item["id"])["anchor_count"] == 3


@pytest.mark.parametrize("changes", [
    {"schema_version": True}, {"schema_version": 2}, {"source_kind": "other"},
    {"cookie": "secret"}, {"notes": [{"note_id": "a", "cookie": "secret"}]},
    {"owner_key": "../profile"}, {"owner_key": "short"}, {"scan_id": "not-uuid"},
    {"started_at": "2026-10-19T00:00:00"}, {"notes": "not-array"},
    {"complete_snapshot": True}, {"trusted": True}, {"stop_reason": "unknown"},
    {"anchor_count_at_start": False},
])
def test_manifest_rejects_invalid_fields_without_echoing_secrets(changes):
    with pytest.raises(XhsValidationError) as error:
        XhsManifest.parse(encode(manifest(["a"], **changes)))
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("url", [
    "http://www.xiaohongshu.com/explore/a", "https://evil.test/explore/a",
    "https://www.xiaohongshu.com.evil.test/explore/a", "https://www.xiaohongshu.com:443/explore/a",
    "https://user@www.xiaohongshu.com/explore/a", "https://www.xiaohongshu.com/explore/a?xsec_token=secret",
    "https://www.xiaohongshu.com/explore/a?x=1", "https://www.xiaohongshu.com/explore/../a",
    "https://www.xiaohongshu.com/explore/b", "file:///etc/passwd", "http://127.0.0.1/a",
])
def test_manifest_rejects_unsafe_urls(url):
    with pytest.raises(XhsValidationError):
        XhsManifest.parse(encode(manifest([], notes=[note("a", url=url)])))


@pytest.mark.parametrize("asset", [
    {"ordinal": True, "kind": "image"}, {"ordinal": -1, "kind": "image"},
    {"ordinal": 0, "kind": []}, {"ordinal": 0, "kind": "script"},
    {"ordinal": 0, "kind": "image", "url": "https://evil.test"},
    {"ordinal": 0, "kind": "image", "candidate_ref": "../../private"},
])
def test_rejects_malicious_assets(asset):
    with pytest.raises(XhsValidationError):
        XhsManifest.parse(encode(manifest([], notes=[note("a", assets=[asset])])))


def test_limits_duplicates_and_canonical_links():
    with pytest.raises(XhsValidationError):
        XhsManifest.parse(b" " * (MAX_MANIFEST_BYTES + 1))
    with pytest.raises(XhsValidationError):
        XhsManifest.parse(b'{"schema_version":1,"schema_version":1}')
    with pytest.raises(XhsValidationError):
        XhsManifest.parse(encode(manifest([], notes=[note("a", title="x" * 1001)])))
    value = manifest(["a"])
    value["notes"].append(deepcopy(value["notes"][0]))
    assert len(XhsManifest.parse(encode(value)).notes) == 1
    value["notes"][1]["title"] = "conflict"
    with pytest.raises(XhsValidationError):
        XhsManifest.parse(encode(value))
    assert XhsNote.parse(note("a", url="https://www.xiaohongshu.com/discovery/item/a")).url.endswith("/explore/a")


@pytest.mark.parametrize("count", [0, 21, True, "3", 1.5])
def test_anchor_validation(service, count):
    with pytest.raises(XhsValidationError):
        source(service, anchor_count=count)


@pytest.mark.parametrize("count", [0, 10001, True, "100", 1.5])
def test_first_fetch_limit_validation(service, count):
    with pytest.raises(XhsValidationError):
        source(service, initial_fetch_limit=count)


def test_default_settings_and_optimistic_updates(service):
    item = service.create_source({"name": "点赞", "owner_key": OWNER})
    assert item["initial_fetch_limit"] == 100
    assert item["anchor_count"] == 3
    assert item["ordering_stable"] == 0
    assert item["full_scan_interval_days"] == 7
    item = service.update_source(item["id"], {
        "config_version": 0, "anchor_count": 20, "initial_fetch_limit": 25,
        "ordering_stable": True, "full_scan_interval_days": 30,
    })
    assert item["anchor_count"] == 20 and item["initial_fetch_limit"] == 25
    assert item["ordering_stable"] == 1 and item["full_scan_interval_days"] == 30
    assert item["config_version"] == 1
    with pytest.raises(XhsConflict):
        service.update_source(item["id"], {"config_version": 0, "anchor_count": 1})


@pytest.mark.parametrize("days", [0, 366, True, 1.5, "7"])
def test_full_scan_interval_requires_an_integer_between_one_and_365_days(service, days):
    item = service.create_source({"name": "点赞", "owner_key": OWNER})
    with pytest.raises(XhsValidationError):
        service.update_source(item["id"], {"config_version": 0, "full_scan_interval_days": days})


@pytest.mark.parametrize("limit,expected", [(1, 1), (3, 3), (100, 5), (None, 5)])
def test_first_manual_import_limit_is_exact_and_applies_only_once(service, limit, expected):
    item = source(service, initial_fetch_limit=limit)
    raw = encode(manifest(list("ABCDE")))
    result = service.import_manifest(item["id"], raw)
    assert result["seen_count"] == expected
    assert result["checkpoint_committed"] == result["complete_snapshot"] == 0
    assert result["stop_reason"] == ("first_sync_limit" if expected < 5 else "manual_import")
    assert service.get_source(item["id"])["initial_fetch_completed"] == 1
    assert service.import_manifest(item["id"], raw)["replayed"] is True
    assert len(service.db.list_task_runs()) == 1
    second = service.import_manifest(item["id"], encode(manifest(list("ABCDE"))))
    assert second["seen_count"] == 5
    assert second["new_count"] == 5 - expected


def test_empty_manual_import_does_not_consume_first_limit(service):
    item = source(service, initial_fetch_limit=2)
    service.import_manifest(item["id"], encode(manifest([])))
    assert service.get_source(item["id"])["initial_fetch_completed"] == 0


@pytest.mark.parametrize("limit,ids,end,reason,count", [
    (2, "ABCDE", True, "first_sync_limit", 2),
    (3, "ABC", False, "first_sync_limit", 3),
    (3, "ABC", True, "end_of_feed", 3),
    (100, "AB", True, "end_of_feed", 2),
    (None, "ABCDE", True, "end_of_feed", 5),
])
def test_initial_scan_limit_and_natural_end(service, limit, ids, end, reason, count):
    item = source(service, initial_fetch_limit=limit)
    result = scan(service, item["id"], [page(ids, end=end)])
    assert result["seen_count"] == count and result["stop_reason"] == reason
    assert bool(result["checkpoint_committed"]) == (reason == "end_of_feed")
    assert result["initial_fetch_limit_at_start"] == limit


@pytest.mark.parametrize("surviving", ["A", "B", "C"])
def test_any_single_anchor_stops_after_whole_page(service, surviving):
    item = source(service)
    scan(service, item["id"], [page("ABCDE", end=True)])
    def pages():
        yield page(["X", "Y", surviving, "Z"])
        raise AssertionError("must not fetch another page after an anchor hit")
    result = scan(service, item["id"], pages())
    assert result["result"] == "SUCCESS_INCREMENTAL"
    assert result["seen_count"] == 4
    assert result["anchor_hit_note_id"] == surviving
    assert result["complete_snapshot"] == 0
    assert service.status()["items"][0]["anchors"] == ["X", "Y", surviving]
    assert all(row["is_currently_liked"] for row in states(service, item["id"]).values())


def test_all_anchors_missing_full_snapshot_only_changes_liked_status(service, tmp_path):
    item = source(service)
    scan(service, item["id"], [page("ABCD", end=True)])
    archived_file = tmp_path / "archive.jpg"
    archived_file.write_bytes(b"archive-sentinel")
    old = states(service, item["id"])["A"]
    with service.db.connect() as conn:
        conn.execute("update site_posts set is_favorite = 1 where id = ?", (old["site_post_id"],))
        conn.execute("update site_assets set status = 'ready', rel_path = ? where post_id = ?", (str(archived_file), old["site_post_id"]))
    result = scan(service, item["id"], [page("XY"), page("Z", end=True)])
    assert result["result"] == "SUCCESS_FULL"
    assert states(service, item["id"])["A"]["is_currently_liked"] == 0
    assert service.db.get_site_post(old["site_post_id"])["is_favorite"] == 1
    assert service.db.list_site_assets(old["site_post_id"])[0]["status"] == "ready"
    assert archived_file.read_bytes() == b"archive-sentinel"


@pytest.mark.parametrize("mode", ["page_limit", "network_error", "cursor_error", "cancelled", "time_limit"])
def test_partial_scans_never_move_checkpoints_or_unlike(service, monkeypatch, mode):
    item = source(service)
    seed = scan(service, item["id"], [page("ABC", end=True)])
    kwargs = {}
    pages = [page("XYZ")]
    if mode == "page_limit":
        kwargs["max_pages"] = 1
    elif mode == "network_error":
        def fail():
            yield page("XYZ")
            raise RuntimeError("Cookie: secret; https://evil.test/?signature=secret")
        pages = fail()
    elif mode == "cancelled":
        kwargs["cancelled"] = lambda: True
    elif mode == "time_limit":
        ticks = iter([0, 301])
        monkeypatch.setattr("app.services.xhs.sync.time.monotonic", lambda: next(ticks))
    result = scan(service, item["id"], pages, **kwargs)
    assert result["stop_reason"] == mode
    assert result["checkpoint_committed"] == 0
    assert service.get_source(item["id"])["last_checkpoint_scan_id"] == seed["scan_id"]
    assert all(row["is_currently_liked"] for row in states(service, item["id"]).values())
    assert "secret" not in json.dumps(result)


def test_manual_complete_claim_cannot_advance_checkpoint_or_unlike(service):
    item = source(service)
    seed = scan(service, item["id"], [page("ABC", end=True)])
    result = service.import_manifest(item["id"], encode(manifest(["X"], complete_snapshot=True, stop_reason="end_of_feed")))
    assert result["result"] == "IMPORTED"
    assert result["complete_snapshot"] == result["checkpoint_committed"] == 0
    assert service.get_source(item["id"])["last_checkpoint_scan_id"] == seed["scan_id"]
    assert all(row["is_currently_liked"] for row in states(service, item["id"]).values())


def test_untrusted_manifest_time_cannot_block_verified_discovery(service):
    item = source(service)
    service.import_manifest(item["id"], encode(manifest(["A"], completed_at="2100-01-01T00:00:00+08:00")))
    result = scan(service, item["id"], [page("ABC", end=True)])
    assert result["result"] == "SUCCESS_FULL"


def test_historical_downloads_are_not_anchors_and_full_scan_ignores_anchors(service):
    item = source(service)
    scan(service, item["id"], [page("ABCDE", end=True)])
    result = scan(service, item["id"], [page("XDE")], max_pages=1)
    assert result["result"] == "PARTIAL"
    result = scan(service, item["id"], [page("XABC"), page("YZ", end=True)], ignore_anchors=True)
    assert result["result"] == "SUCCESS_FULL" and result["seen_count"] == 6


def test_anchor_count_snapshot_and_increasing_setting_preserve_old_candidates(service):
    item = source(service)
    scan(service, item["id"], [page("ABCD", end=True)])
    def pages():
        service.update_source(item["id"], {"config_version": 0, "anchor_count": 5})
        yield page("XAB")
    result = scan(service, item["id"], pages())
    assert result["anchor_count_at_start"] == 3
    assert service.status()["items"][0]["anchors"] == list("XAB")
    result = scan(service, item["id"], [page("YX"), page("ZAB")])
    assert result["seen_count"] == 5 and result["stop_reason"] == "anchor_hit"
    assert service.status()["items"][0]["anchors"] == list("YXZAB")


def test_duplicate_pages_and_structure_changes_disable_success(service):
    item = source(service)
    scan(service, item["id"], [page("ABC", end=True)])
    result = scan(service, item["id"], [page("X"), page("XAB", end=True)])
    assert result["result"] == "PARTIAL" and not result["checkpoint_committed"]
    result = scan(service, item["id"], [page("XAB", end=True, integrity_ok=False)])
    assert result["result"] == "PARTIAL" and not result["complete_snapshot"]


def test_old_publication_urls_media_order_and_idempotence(service):
    item = source(service)
    raw = encode(manifest(["old-note"]))
    first = service.import_manifest(item["id"], raw)
    before = states(service, item["id"])["old-note"]
    changed = note("old-note", url="https://www.xiaohongshu.com/discovery/item/old-note", assets=[
        {"ordinal": 1, "kind": "video"}, {"ordinal": 0, "kind": "image"},
    ])
    second = service.import_manifest(item["id"], encode(manifest([], notes=[changed])))
    assert first["new_count"] == 1 and second["new_count"] == 0
    after = states(service, item["id"])["old-note"]
    assert before["first_seen_at"] == after["first_seen_at"]
    assert before["site_post_id"] == after["site_post_id"]
    assert len(service.db.list_site_assets(after["site_post_id"])) == 2
    with service.db.connect() as conn:
        dump = "\n".join(conn.iterdump())
    assert "temporary-ref" not in dump
    assert "liked_at" not in after


def test_media_failure_does_not_undo_discovery_checkpoint(service):
    item = source(service)
    run = scan(service, item["id"], [page("ABC", end=True)])
    with service.db.connect() as conn:
        conn.execute("update site_assets set status = 'failed'")
    assert run["checkpoint_committed"] == 1
    assert service.get_source(item["id"])["last_checkpoint_scan_id"] == run["scan_id"]
    assert service.status()["items"][0]["pending_media"] == 3


def test_pending_write_failure_rolls_back_notes_run_and_checkpoint(service):
    item = source(service)
    with service.db.connect() as conn:
        conn.execute("create trigger fail_media before insert on site_assets begin select raise(abort, 'disk full'); end")
    with pytest.raises(sqlite3.IntegrityError):
        scan(service, item["id"], [page("ABC", end=True)])
    assert states(service, item["id"]) == {}
    assert service.runs() == [] and service.db.list_task_runs() == []
    assert service.get_source(item["id"])["checkpoint_version"] == 0


def test_stale_checkpoint_is_rejected_before_writes(service, monkeypatch):
    item = source(service)
    commit = service._commit
    def race(*args, **kwargs):
        with service.db.connect() as conn:
            conn.execute("update xhs_source_accounts set checkpoint_version = checkpoint_version + 1")
        return commit(*args, **kwargs)
    monkeypatch.setattr(service, "_commit", race)
    with pytest.raises(XhsConflict):
        scan(service, item["id"], [page("ABC", end=True)])
    assert states(service, item["id"]) == {} and service.runs() == []


def test_source_account_isolation_locks_and_disabled_sources(service):
    one = source(service)
    two = source(service, owner_key="another-owner")
    raw = encode(manifest(["A"]))
    with pytest.raises(XhsConflict):
        service.import_manifest(two["id"], raw)
    with service._source_lock(one["id"]):
        with pytest.raises(XhsConflict):
            XhsSyncService(service.db).import_manifest(one["id"], raw)
    service.import_manifest(one["id"], raw)
    assert states(service, two["id"]) == {}
    service.update_source(one["id"], {"config_version": 0, "enabled": False})
    with pytest.raises(XhsConflict):
        service.import_manifest(one["id"], encode(manifest(["B"])))
    with pytest.raises(XhsConflict):
        scan(service, two["id"], [])


def test_scan_id_reuse_cannot_change_content_or_sources(service):
    one, two = source(service), source(service)
    value = manifest(["A"])
    service.import_manifest(one["id"], encode(value))
    with pytest.raises(XhsConflict):
        service.import_manifest(two["id"], encode(value))
    value["notes"] = [note("B")]
    with pytest.raises(XhsConflict):
        service.import_manifest(one["id"], encode(value))


def test_generic_paths_never_parse_or_delete_xhs(service):
    item = source(service)
    service.import_manifest(item["id"], encode(manifest(["A"])))
    site = service.db.get_site_source(item["id"])
    syncer = SiteSyncManager(service.db, SimpleNamespace())
    with pytest.raises(RuntimeError):
        syncer.execute_full_validation(item["id"])
    with pytest.raises(ValueError):
        service.db.clear_site_source_content(item["id"])
    with pytest.raises(ValueError):
        SourceParser(SimpleNamespace()).discover(site)
    with pytest.raises(ValueError):
        service.db.update_site_source(item["id"], {"source_type": "html"})
    with pytest.raises(ValueError):
        service.db.create_site_source({"source_type": "xhs_likes"})
    assert syncer.execute_sync()["sources"] == 0
    assert len(states(service, item["id"])) == 1


def test_http_flow_size_csrf_sanitization_and_generic_guards(service, monkeypatch):
    from app import main
    from app.services.xhs.manager import XhsManager
    monkeypatch.setenv("XHS_WORKER_URL", "")
    monkeypatch.setattr(main, "db", service.db)
    monkeypatch.setattr(main, "xhs_manager", XhsManager(service.db, SimpleNamespace(), SimpleNamespace()))
    monkeypatch.setattr(main, "scheduler", SimpleNamespace(reload_xhs=lambda: None))
    client = TestClient(main.app)
    created = client.post("/api/xhs/sources", json={"name": "本人点赞", "owner_key": OWNER, "initial_fetch_limit": 2})
    assert created.status_code == 200
    item = created.json()
    endpoint = f'/api/xhs/manifest/import?source_id={item["id"]}'
    raw = encode(manifest(list("ABCD")))
    response = client.post(endpoint, content=raw, headers={"Content-Type": "application/json"})
    assert response.status_code == 200 and response.json()["seen_count"] == 2
    assert response.headers["cache-control"] == "no-store"
    assert client.get("/api/xhs/status").json()["automatic_sync_available"] is False
    assert len(client.get(f'/api/xhs/sources/{item["id"]}/notes').json()["items"]) == 2
    assert len(client.get("/api/xhs/runs").json()["items"]) == 1
    for path in [f'/api/site-sources/{item["id"]}/sync', f'/api/site-sources/{item["id"]}/validate']:
        assert client.post(path).status_code == 409
    assert client.post(f'/api/subscriptions/site:{item["id"]}/pull').status_code == 409
    assert client.post(endpoint, content=raw).status_code == 415
    assert client.post(endpoint, content=raw, headers={"Content-Type": "application/json", "Origin": "https://evil.test"}).status_code == 403
    assert client.post(endpoint, content=b"x" * (MAX_MANIFEST_BYTES + 1), headers={"Content-Type": "application/json"}).status_code == 413
    malformed = manifest(["A"], cookie="secret-sentinel")
    response = client.post(endpoint, json=malformed)
    assert response.status_code == 400 and "secret-sentinel" not in response.text
    assert client.get("/api/xhs/runs?limit=1000").status_code == 400


def test_global_sync_endpoint_queues_only_ready_xhs_sources(service, monkeypatch):
    from app import main
    from app.services.xhs.manager import XhsManager

    item = source(service)
    with service.db.connect() as conn:
        conn.execute(
            "update xhs_source_accounts set verification_passed=1, auth_status='READY' where source_id=?",
            (item["id"],),
        )

    class ReadyWorker:
        configured = True

    class Queue:
        def __init__(self):
            self.calls = []

        def start_xhs_sync(self, source_id, **options):
            self.calls.append((source_id, options))
            return {"job_id": "synthetic-global-job"}

    monkeypatch.setenv("XHS_WORKER_URL", "")
    monkeypatch.setattr(main, "db", service.db)
    manager = XhsManager(service.db, SimpleNamespace(), SimpleNamespace(), worker=ReadyWorker())
    queue = Queue()
    manager.queue = queue
    monkeypatch.setattr(main, "xhs_manager", manager)
    monkeypatch.setattr(main, "scheduler", SimpleNamespace(reload_xhs=lambda: None))

    response = TestClient(main.app).post("/api/xhs/sync", json={})

    assert response.status_code == 200
    assert response.json()["jobs"] == [{"job_id": "synthetic-global-job"}]
    assert response.headers["cache-control"] == "no-store"
    assert queue.calls == [(item["id"], {})]


def test_local_archive_check_route_queues_validation_without_worker_access(service, monkeypatch):
    from app import main
    from app.services.xhs.manager import XhsManager

    item = source(service)
    calls = []

    class Queue:
        def start_xhs_validation(self, source_id):
            calls.append(source_id)
            return {"ok": True, "queued": True, "message": "已提交本地归档检查，不会访问小红书"}

    monkeypatch.setenv("XHS_WORKER_URL", "")
    monkeypatch.setattr(main, "db", service.db)
    manager = XhsManager(service.db, SimpleNamespace(), SimpleNamespace())
    manager.queue = Queue()
    monkeypatch.setattr(main, "xhs_manager", manager)
    monkeypatch.setattr(main, "scheduler", SimpleNamespace(reload_xhs=lambda: None))

    response = TestClient(main.app).post(f"/api/xhs/sources/{item['id']}/check-files", json={})

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["queued"] is True
    assert calls == [item["id"]]
