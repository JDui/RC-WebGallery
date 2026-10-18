from contextlib import contextmanager
from datetime import datetime, timedelta
import hashlib
from io import BytesIO
import json
from pathlib import Path
from types import SimpleNamespace

from PIL import Image
import pytest

from app.db import Database
from app.services.puller import PullManager
from app.services.utils import TIMEZONE, now_iso
from app.services.xhs.manager import XhsManager
from app.services.xhs.models import XhsNote, XhsValidationError
from app.services.xhs.sync import ScanPage, XhsConflict, XhsSyncService
from app.services.xhs.worker_client import WorkerError
from xhs_worker.parser import normalize_note, response_page


OWNER = "acct_owner_0001"
PROFILE_MARKER = "/private/chromium-profile/profile-marker"
TOKEN_MARKER = "worker-token-marker-01234567890123456789"
URL_MARKER = "https://img.xhscdn.com/private.jpg?xsec_token=url-marker-secret"


def image_bytes():
    output = BytesIO()
    Image.new("RGB", (2, 2), color=(20, 40, 60)).save(output, format="PNG")
    return output.getvalue()


class FakeBinaryResponse:
    def __init__(self, content, mime, digest):
        self.content = content
        self.headers = {"Content-Type": mime, "X-Asset-Sha256": digest}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def iter_content(self, chunk_size):
        for offset in range(0, len(self.content), chunk_size):
            yield self.content[offset:offset + chunk_size]


class FakeWorker:
    configured = True

    def __init__(self, *, owner=OWNER, raw_note=None, assets=None, binaries=None):
        self.owner = owner
        self.raw_note = raw_note or {"id": "note-new", "note_card": {"display_title": "Synthetic note"}}
        self.assets = assets or [{"ordinal": 0, "kind": "image"}]
        self.binaries = binaries or {(0, "image"): image_bytes()}
        self.bad_digests = set()
        self.page_error = None
        self.has_more = False
        self.calls = []
        self.sessions = []
        self.allowed_notes = []
        self.page_candidates = []
        self.status_payload = {
            "state": "READY", "owner_key": owner, "profile_path": PROFILE_MARKER,
            "token": TOKEN_MARKER, "last_media_url": URL_MARKER,
        }

    def status(self, *, check=False):
        self.calls.append(("status", check))
        return dict(self.status_payload)

    def request(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if path.endswith("/resolve"):
            return {"assets": list(self.assets)}
        return {}

    @contextmanager
    def session(self, owner_key, allowed_notes=()):
        self.sessions.append(owner_key)
        self.allowed_notes = list(allowed_notes)
        yield "fake-session-id"

    def pages(self, session_id, cooperate=None):
        if cooperate:
            cooperate()
        if self.page_error:
            raise self.page_error
        payload = {"data": {"notes": [self.raw_note], "has_more": self.has_more}}
        raw_notes, end_of_feed, integrity_ok = response_page(payload)
        notes = []
        for raw in raw_notes:
            note, candidates = normalize_note(raw)
            self.page_candidates.extend(candidates)
            notes.append(XhsNote.parse(note))
        yield ScanPage(tuple(notes), end_of_feed=end_of_feed, integrity_ok=integrity_ok)

    @contextmanager
    def response(self, method, path, payload=None, *, stream=False):
        self.calls.append((method, path, payload, stream))
        assert method == "GET" and stream is True
        parts = path.split("/")
        asset_key = (int(parts[-2]), parts[-1])
        content = self.binaries[asset_key]
        mime = "image/png" if asset_key[1] == "image" else "video/mp4"
        digest = hashlib.sha256(content).hexdigest()
        if asset_key in self.bad_digests:
            digest = "0" * 64
        with FakeBinaryResponse(content, mime, digest) as response:
            yield response


class FakeStorage:
    def __init__(self, root):
        self.root = Path(root)
        self.config = SimpleNamespace(data_dir=self.root)

    def site_post_folder(self, source_slug, pub_date, post_slug):
        path = self.root / "sites" / source_slug / (pub_date or "unknown") / post_slug
        path.mkdir(parents=True, exist_ok=True)
        return path

    def image_folder(self, folder_name):
        return self.root / "images" / folder_name

    def livephoto_folder(self, folder_name):
        return self.root / "livephoto" / folder_name

    def resolve_storage_path(self, rel_path):
        return self.root / rel_path if rel_path else None

    def relative_to_storage(self, path):
        return path.resolve().relative_to(self.root.resolve()).as_posix()


class FakeIndexer:
    def __init__(self):
        self.calls = []
        self.video_covers = []
        self.thumbnailer = SimpleNamespace(ensure_video_cover=lambda source, target: self.video_covers.append((source, target)))

    def index_folder(self, *args, **kwargs):
        self.calls.append((args, kwargs))


@pytest.fixture
def rig(tmp_path, monkeypatch):
    db = Database(tmp_path / "app.db")
    db.init()
    service = XhsSyncService(db)
    worker = FakeWorker()
    storage = FakeStorage(tmp_path / "storage")
    indexer = FakeIndexer()
    manager = XhsManager(db, storage, indexer, worker=worker)
    monkeypatch.setattr("app.services.xhs.manager.shutil.disk_usage", lambda _: SimpleNamespace(free=1 << 40))
    return SimpleNamespace(db=db, service=service, worker=worker, storage=storage,
                           indexer=indexer, manager=manager)


def make_source(service, **changes):
    return service.create_source({"name": "本人点赞", "owner_key": OWNER, "initial_fetch_limit": None, **changes})


def confirm(manager, source_id, **changes):
    payload = {"owner_confirmed": True, "paging_confirmed": True, "rules_reviewed": True,
               "ordering_stable": True, **changes}
    return manager.verify(source_id, payload)


def parsed_note(note_id, *, note_type="image", assets=()):
    return XhsNote.parse({
        "note_id": note_id,
        "url": f"https://www.xiaohongshu.com/explore/{note_id}",
        "title": note_id,
        "author_id": "author_1",
        "type": note_type,
        "published_at": "2024-01-02T03:04:05+08:00",
        "observed_at": now_iso(),
        "assets": list(assets),
    })


def all_database_text(db):
    values = []
    with db.connect() as conn:
        tables = [row[0] for row in conn.execute(
            "select name from sqlite_master where type='table' and name not like 'sqlite_%'")]
        for table in tables:
            for row in conn.execute(f'select * from "{table}"'):
                values.extend(str(value) for value in row if value is not None)
    return "\n".join(values)


def test_incomplete_verification_cannot_reserve_a_sync_job(rig):
    source = make_source(rig.service)

    with pytest.raises(XhsValidationError):
        rig.manager.verify(source["id"], {
            "owner_confirmed": True, "paging_confirmed": False,
            "rules_reviewed": True, "ordering_stable": True,
        })
    with pytest.raises(XhsConflict):
        rig.manager.reserve_job(source["id"])

    assert rig.worker.calls == []
    assert rig.service.get_source(source["id"])["verification_passed"] == 0


def test_verification_rejects_a_worker_identity_that_does_not_match_source(rig):
    source = make_source(rig.service)
    rig.worker.status_payload.update({"owner_key": "acct_other_0001"})

    with pytest.raises(XhsConflict, match="一致"):
        confirm(rig.manager, source["id"])

    updated = rig.service.get_source(source["id"])
    assert updated["verification_passed"] == 0
    assert updated["ordering_stable"] == 0


def test_verification_saves_ordering_and_allows_sync(rig):
    source = make_source(rig.service)
    before_version = source["config_version"]

    result = confirm(rig.manager, source["id"], ordering_stable=False)

    assert result["ok"] is True
    updated = rig.service.get_source(source["id"])
    assert updated["verification_passed"] == 1
    assert updated["ordering_stable"] == 0
    assert updated["auth_status"] == "READY"
    assert updated["config_version"] == before_version + 1
    assert rig.manager.ready(source["id"]) is True


def test_reserve_job_is_idempotent_and_allows_only_one_active_job_per_source(rig):
    source = make_source(rig.service)
    confirm(rig.manager, source["id"])
    job_id = "8daeb2d8-44ef-4bc0-a018-611f373724b5"

    first, created = rig.manager.reserve_job(source["id"], job_id=job_id, force_full=True)
    replay, replay_created = rig.manager.reserve_job(source["id"], job_id=job_id, force_full=True)

    assert created is True and replay_created is False
    assert first["job_id"] == replay["job_id"] == job_id
    assert len(rig.db.list_task_runs()) == 1
    with pytest.raises(XhsConflict):
        rig.manager.reserve_job(source["id"], job_id="5c763b1d-6fa7-48a9-9dc3-6c68fda07a93")
    with pytest.raises(XhsConflict):
        rig.manager.reserve_job(source["id"], job_id=job_id, force_full=False)


def test_start_sync_requires_queue_and_forwards_validated_options(rig):
    source = make_source(rig.service)

    with pytest.raises(XhsConflict, match="队列未就绪"):
        rig.manager.start_sync(source["id"], {})
    with pytest.raises(XhsValidationError):
        rig.manager.start_sync(source["id"], {"force_full": 1})

    class Queue:
        def __init__(self):
            self.calls = []

        def start_xhs_sync(self, source_id, **options):
            self.calls.append((source_id, options))
            return {"job_id": "queued-job"}

    queue = Queue()
    rig.manager.queue = queue
    result = rig.manager.start_sync(source["id"], {"force_full": True, "media_only": False})

    assert result == {"job_id": "queued-job"}
    assert queue.calls == [(source["id"], {"force_full": True, "media_only": False})]


def test_recover_marks_queued_and_running_jobs_interrupted(rig):
    first = make_source(rig.service)
    second = make_source(rig.service)
    confirm(rig.manager, first["id"])
    confirm(rig.manager, second["id"])
    queued, _ = rig.manager.reserve_job(first["id"])
    running, _ = rig.manager.reserve_job(second["id"])
    with rig.db.connect() as conn:
        conn.execute("update xhs_jobs set state='running' where job_id=?", (running["job_id"],))

    rig.manager.recover()

    with rig.db.connect() as conn:
        jobs = {row["job_id"]: dict(row) for row in conn.execute("select * from xhs_jobs")}
    assert jobs[queued["job_id"]]["state"] == "interrupted"
    assert jobs[running["job_id"]]["state"] == "interrupted"
    assert jobs[queued["job_id"]]["error_category"] == "PROCESS_RESTARTED"
    assert rig.db.get_task_run(queued["task_id"])["status"] == "failed"
    assert rig.db.get_task_run(running["task_id"])["status"] == "failed"


def test_release_note_approves_pending_media_and_unblocks_the_post(rig):
    source = make_source(rig.service)
    rig.service.scan_pages(source["id"], OWNER, [ScanPage((parsed_note("release_note"),), end_of_feed=True)])
    with rig.db.connect() as conn:
        post_id = conn.execute("select site_post_id from xhs_like_state where note_id='release_note'").fetchone()[0]
        conn.execute("update site_posts set is_blocked=1,status='blocked' where id=?", (post_id,))
        conn.execute("update xhs_note_downloads set status='failed',error_category='FILTERED' where post_id=?", (post_id,))

    assert rig.manager.release_note(source["id"], "release_note") == {"ok": True}
    post = rig.db.get_site_post(post_id)
    with rig.db.connect() as conn:
        download = dict(conn.execute("select * from xhs_note_downloads where post_id=?", (post_id,)).fetchone())
    assert post["is_blocked"] == 0 and post["status"] == "discovered"
    assert download["status"] == "approved" and download["error_category"] is None
    with pytest.raises(XhsValidationError):
        rig.manager.release_note(source["id"], "missing_note")


@pytest.mark.parametrize(("mode", "expected_state"), [("cancel", "cancelled"), ("failure", "failed")])
def test_cancelled_or_failed_worker_scan_does_not_advance_anchors(rig, mode, expected_state):
    source = make_source(rig.service)
    existing = tuple(parsed_note(note_id) for note_id in ("anchor_a", "anchor_b", "anchor_c"))
    rig.service.scan_pages(source["id"], OWNER, [ScanPage(existing, end_of_feed=True)])
    confirm(rig.manager, source["id"])
    before = rig.service.get_source(source["id"])
    before_anchors = rig.service.status()["items"][0]["anchors"]
    job, _ = rig.manager.reserve_job(source["id"])
    cancelled = None
    if mode == "cancel":
        cancelled = lambda: True
    else:
        rig.worker.page_error = WorkerError("NEEDS_REAUTH")

    result = rig.manager.execute(job["job_id"], cancelled=cancelled)

    after = rig.service.get_source(source["id"])
    assert result["state"] == expected_state
    assert after["checkpoint_version"] == before["checkpoint_version"]
    assert after["last_checkpoint_scan_id"] == before["last_checkpoint_scan_id"]
    assert rig.service.status()["items"][0]["anchors"] == before_anchors
    with rig.db.connect() as conn:
        state = conn.execute("select state from xhs_jobs where job_id=?", (job["job_id"],)).fetchone()[0]
    assert state == expected_state


def test_manager_forces_periodic_full_scan_when_last_verified_snapshot_expires(rig):
    source = make_source(rig.service, initial_fetch_limit=None)
    seeded = rig.service.scan_pages(
        source["id"], OWNER, [ScanPage(tuple(parsed_note(key) for key in ("full_a", "full_b")), end_of_feed=True)]
    )
    rig.service.update_source(source["id"], {
        "config_version": source["config_version"], "ordering_stable": True, "full_scan_interval_days": 7,
    })
    confirm(rig.manager, source["id"], ordering_stable=True)
    expired_at = (datetime.now(TIMEZONE) - timedelta(days=8)).isoformat()
    with rig.db.connect() as conn:
        conn.execute("update xhs_sync_runs set finished_at=? where scan_id=?", (expired_at, seeded["scan_id"]))

    rig.worker.raw_note = {"id": "periodic_full_new", "note_card": {"display_title": "Periodic full scan"}}
    job, _ = rig.manager.reserve_job(source["id"])
    result = rig.manager.execute(job["job_id"])

    assert result["state"] == "success"
    assert result["effective_force_full"] is True
    assert result["discovery"]["complete_snapshot"] == 1


def test_manager_uses_stable_incremental_scan_before_full_scan_interval(rig):
    source = make_source(rig.service, initial_fetch_limit=None)
    seeded = rig.service.scan_pages(source["id"], OWNER, [ScanPage((parsed_note("recent_anchor"),), end_of_feed=True)])
    rig.service.update_source(source["id"], {
        "config_version": source["config_version"], "ordering_stable": True, "full_scan_interval_days": 7,
    })
    confirm(rig.manager, source["id"], ordering_stable=True)
    rig.worker.raw_note = {"id": "recent_incremental_new", "note_card": {"display_title": "Recent scan"}}
    job, _ = rig.manager.reserve_job(source["id"])

    result = rig.manager.execute(job["job_id"])

    assert seeded["complete_snapshot"] == 1
    assert result["effective_force_full"] is False
    assert result["discovery"]["complete_snapshot"] == 1


def test_execute_resolves_ordered_assets_verifies_hash_atomically_and_retries_todo(rig):
    source = make_source(rig.service)
    confirm(rig.manager, source["id"])
    content = image_bytes()
    rig.worker.raw_note = {
        "id": "note_resolved",
        "note_card": {"display_title": "Resolved after scan", "image_list": []},
    }
    rig.worker.assets = [{"ordinal": 0, "kind": "image"}, {"ordinal": 1, "kind": "image"}]
    rig.worker.binaries = {(0, "image"): content, (1, "image"): content}
    rig.worker.bad_digests.add((0, "image"))
    first_job, _ = rig.manager.reserve_job(source["id"])

    first_result = rig.manager.execute(first_job["job_id"])

    assert rig.worker.page_candidates == []
    assert first_result["state"] == "partial" and first_result["errors"] == 1
    note_row = rig.service.notes(source["id"])[0]
    assert note_row["download_status"] == "failed"
    with rig.db.connect() as conn:
        post_id = conn.execute("select site_post_id from xhs_like_state where note_id='note_resolved'").fetchone()[0]
        first_asset = dict(conn.execute("select * from site_assets where post_id=?", (post_id,)).fetchone())
    assert first_asset["status"] == "failed"
    work_folder = next((rig.storage.root / "sites").rglob("note_resolved"))
    assert list(work_folder.iterdir()) == []

    rig.worker.bad_digests.clear()
    retry_job, _ = rig.manager.reserve_job(source["id"], media_only=True)
    retry_result = rig.manager.execute(retry_job["job_id"])

    assert retry_result["state"] == "success" and retry_result["downloaded"] == 2
    assets = rig.db.list_site_assets(post_id)
    assert [asset["url"] for asset in assets] == [
        f"urn:xhs:{source['id']}:note_resolved:0:image",
        f"urn:xhs:{source['id']}:note_resolved:1:image",
    ]
    assert all(asset["status"] == "ready" for asset in assets)
    assert all(Path(rig.storage.resolve_storage_path(asset["rel_path"])).is_file() for asset in assets)
    assert not list(work_folder.glob("*.part"))
    assert any(len(call) == 3 and call[1].endswith("/resolve") for call in rig.worker.calls)

    persisted = all_database_text(rig.db)
    for marker in (PROFILE_MARKER, TOKEN_MARKER, URL_MARKER, "xsec_token"):
        assert marker not in persisted


def test_sensitive_worker_failure_details_are_not_persisted_or_logged(rig, caplog):
    source = make_source(rig.service)
    confirm(rig.manager, source["id"])
    rig.worker.page_error = RuntimeError(f"{PROFILE_MARKER} {TOKEN_MARKER} {URL_MARKER}")
    job, _ = rig.manager.reserve_job(source["id"])

    result = rig.manager.execute(job["job_id"])

    assert result["state"] == "failed"
    persisted = all_database_text(rig.db)
    for marker in (PROFILE_MARKER, TOKEN_MARKER, URL_MARKER, "xsec_token"):
        assert marker not in persisted
        assert marker not in caplog.text


def test_single_video_is_indexed_as_standalone_video_not_livephoto(rig):
    source = make_source(rig.service)
    confirm(rig.manager, source["id"])
    video = b"\x00\x00\x00\x18ftypisom" + b"synthetic-video"
    rig.worker.raw_note = {
        "id": "standalone_video",
        "note_card": {"type": "video", "display_title": "Standalone clip"},
    }
    rig.worker.assets = [{"ordinal": 0, "kind": "video"}]
    rig.worker.binaries = {(0, "video"): video}
    job, _ = rig.manager.reserve_job(source["id"])

    result = rig.manager.execute(job["job_id"])

    assert result["state"] == "success"
    with rig.db.connect() as conn:
        post_id = conn.execute("select site_post_id from xhs_like_state where note_id='standalone_video'").fetchone()[0]
        note_type = conn.execute("select note_type from xhs_like_state where note_id='standalone_video'").fetchone()[0]
    assets = rig.db.list_site_assets(post_id)
    assert note_type == "video"
    assert [asset["media_type"] for asset in assets] == ["video"]
    metadata = rig.indexer.calls[-1][1]["metadata"]
    assert metadata["standalone_video"] is True
    assert len(rig.indexer.video_covers) == 1


@pytest.mark.parametrize("damage", ["none", "cache", "gallery", "missing_cache", "missing_gallery", "metadata"])
def test_validate_archive_checks_cache_and_gallery_hashes_and_records_retryable_failure(rig, damage):
    source = make_source(rig.service)
    confirm(rig.manager, source["id"])
    job, _ = rig.manager.reserve_job(source["id"])
    assert rig.manager.execute(job["job_id"])["state"] == "success"

    with rig.db.connect() as conn:
        row = conn.execute("""select a.*, p.id as post_id, l.note_id from site_assets a
            join site_posts p on p.id=a.post_id join xhs_like_state l on l.site_post_id=p.id
            where l.source_id=?""", (source["id"],)).fetchone()
        asset = dict(row)
    cached = rig.storage.resolve_storage_path(asset["rel_path"])
    archived = rig.storage.image_folder(f"xhs_{source['id']}_{asset['note_id']}") / f"001__{asset['filename']}"
    if damage == "cache":
        cached.write_bytes(b"corrupt-cache")
    elif damage == "gallery":
        archived.write_bytes(b"corrupt-gallery")
    elif damage == "missing_cache":
        cached.unlink()
    elif damage == "missing_gallery":
        archived.unlink()
    elif damage == "metadata":
        with rig.db.connect() as conn:
            conn.execute("delete from xhs_asset_details where asset_id=?", (asset["id"],))

    result = rig.manager.validate_archive(source["id"])

    assert result == {"checked": 1, "failed": int(damage != "none"), "source_id": source["id"]}
    updated = rig.db.list_site_assets(asset["post_id"])[0]
    with rig.db.connect() as conn:
        note_status = conn.execute("select status from xhs_note_downloads where post_id=?", (asset["post_id"],)).fetchone()[0]
    if damage == "none":
        assert updated["status"] == "ready"
        assert note_status == "ready"
    else:
        assert updated["status"] == "failed"
        assert updated["error"] == "MEDIA_CHECKSUM_MISMATCH"
        assert note_status == "failed"


def test_pull_manager_queues_and_retries_local_xhs_archive_validation(rig, monkeypatch):
    puller = PullManager(rig.db, rig.storage, rig.indexer, None, None, None)
    puller.xhs_manager = rig.manager
    rig.manager.validate_archive = lambda source_id, cooperate=None: {
        "checked": 3, "failed": 1, "source_id": source_id,
    }
    source = make_source(rig.service)

    puller._lock.acquire()
    try:
        result = puller.start_xhs_validation(source["id"])
    finally:
        puller._lock.release()
    assert result["queued"] is True
    queued = puller._queue[0]
    assert queued.kind == "xhs-verify"
    assert queued.payload["args"] == [source["id"]]

    launched = []

    class CapturedThread:
        def __init__(self, *, target, args=(), daemon=None):
            self.target, self.args = target, args

        def start(self):
            launched.append((self.target, self.args))

    monkeypatch.setattr("app.services.puller.threading.Thread", CapturedThread)
    puller._run_next_queued_task()
    assert launched == [(puller._run_xhs_validation, (source["id"],))]
    launched[0][0](*launched[0][1])

    task = rig.db.last_task_run("xhs-verify")
    details = json.loads(task["details_json"])
    assert task["status"] == "partial"
    assert details["checked"] == 3 and details["failed"] == 1
    assert details["retry_action"] == {"kind": "xhs-verify", "source_id": source["id"]}

    retried = []
    puller.start_xhs_validation = lambda source_id: retried.append(source_id) or {"queued": False}
    assert puller.retry_task_run(task["id"]) == {"queued": False}
    assert retried == [source["id"]]
