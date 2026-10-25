"""重复检测的离线回归检查；所有数据库和图片均在临时目录生成。"""

import ast
import random
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path
from types import MethodType, SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from PIL import Image

from app.db import Database
from app.services.duplicates import DuplicateService
from app.services.image_similarity import image_fingerprint


class LocalStorage:
    def __init__(self, root):
        self.root = root

    def resolve_storage_path(self, relative):
        return self.root / relative if relative else None

    def storage_url(self, relative):
        return f"/storage/{relative}" if relative else None


@pytest.fixture
def library(tmp_path):
    db = Database(tmp_path / "app.db")
    db.init()
    db.save_settings({"duplicate_image_threshold": 1})
    return db, LocalStorage(tmp_path), DuplicateService(db, LocalStorage(tmp_path))


def add_folder(library, name, seeds, *, published=1, live=False, fingerprint=True):
    db, storage, _ = library
    with db.connect() as conn:
        conn.execute(
            "insert into folders(folder_name,pub_time,pub_ts,top_dynamic_id,source_dynamic_id,created_at,updated_at) "
            "values(?,?,?,?,?,?,?)",
            (name, "2026-01-01", published, name, name, "now", "now"),
        )
        for index, seed in enumerate(seeds, 1):
            relative = f"{name}-{index}.png"
            path = storage.root / relative
            rng = random.Random(seed)
            image = Image.new("RGB", (68, 64))
            image.putdata([(rng.randrange(256), rng.randrange(256), rng.randrange(256)) for _ in range(68 * 64)])
            image.save(path)
            conn.execute(
                "insert into assets(folder_name,media_type,pair_index,filename,rel_path,width,height,"
                "duplicate_fingerprint,created_at,updated_at) values(?,?,?,?,?,?,?,?,?,?)",
                (name, "image", index, path.name, relative, 68, 64,
                 image_fingerprint(path) if fingerprint else None, "now", "now"),
            )
        if live:
            conn.execute(
                "insert into assets(folder_name,media_type,pair_index,filename,rel_path,created_at,updated_at) "
                "values(?,?,?,?,?,?,?)",
                (name, "livephoto", 1, "live.mov", "live.mov", "now", "now"),
            )


def puller_method(name):
    tree = ast.parse((Path(__file__).parents[1] / "app/services/puller.py").read_text(encoding="utf-8"))
    manager_class = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "PullManager")
    method = next(node for node in manager_class.body if isinstance(node, ast.FunctionDef) and node.name == name)
    namespace = {"Any": Any}
    exec(compile(ast.Module(body=[method], type_ignores=[]), "puller-task", "exec"), namespace)
    return namespace[name]


@pytest.fixture
def duplicate_manager(library):
    db, _, service = library
    manager = SimpleNamespace(
        db=db, duplicate_service=service, indexer=Mock(), _background_checkpoint=Mock(),
        _run_background_cleanup=Mock(), _remove_duplicate_pair_files=Mock(),
        _refresh_delete_sidebar_counts=db.get_sidebar_count_cache,
        _release=Mock(), _run_next_queued_task=Mock(),
    )
    for name in ("cleanup_duplicate_pairs", "_run_duplicate_cleanup", "_run_duplicate_trash", "_run_duplicate_resolve"):
        setattr(manager, name, MethodType(puller_method(name), manager))

    def trash(folder_name, **_):
        db.delete_folder(folder_name)
        return {"cleanup_queued": True}

    manager.move_to_trash = trash
    return manager


def two_duplicate_groups(library):
    for name, seed, published in (("A", 1, 1), ("B", 1, 2), ("C", 2, 3), ("D", 2, 4)):
        add_folder(library, name, [seed], published=published)
    groups = library[2].list_groups(force=True)["items"]
    assert len(groups) == 2
    return groups


@pytest.mark.parametrize("restart", [False, True])
def test_cleanups_can_run_back_to_back_without_rescan(library, duplicate_manager, restart):
    db, storage, service = library
    groups = two_duplicate_groups(library)
    plans = [(group["signature"], service.cleanup_plan(group["signature"])) for group in groups]
    for index, (signature, plan) in enumerate(plans):
        if restart and index:
            service = DuplicateService(db, storage)
            duplicate_manager.duplicate_service = service
        duplicate_manager._run_duplicate_cleanup(signature, plan["folder_names"], plan["targets"])
        assert db.list_task_runs(limit=1)[0]["status"] == "success"
        result = service.list_groups()
        assert not result["cache_stale"]
        assert result["total"] == 1 - index
        assert all(target["asset_id"] not in {row["id"] for row in db.list_all_assets()} for target in plan["targets"])


@pytest.mark.parametrize("operation", ["trash", "resolve"])
def test_deleting_duplicate_posts_keeps_other_groups_fresh(library, duplicate_manager, operation):
    groups = two_duplicate_groups(library)
    group, other = groups
    service = library[2]
    if operation == "trash":
        duplicate_manager._run_duplicate_trash(group["signature"], group["items"][0]["folder_name"])
    else:
        keep, removed = [item["folder_name"] for item in group["items"]]
        duplicate_manager._run_duplicate_resolve(group["signature"], keep, [removed])
    assert library[0].list_task_runs(limit=1)[0]["status"] == "success"
    assert not service.list_groups()["cache_stale"]
    plan = service.cleanup_plan(other["signature"])
    service.validate_cleanup_targets(other["signature"], plan["targets"])


@pytest.mark.parametrize("external_change", ["unrelated_asset", "new_asset_in_group", "threshold"])
def test_cleanup_does_not_certify_concurrent_library_changes(library, duplicate_manager, external_change):
    db, _, service = library
    groups = two_duplicate_groups(library)
    signature = groups[0]["signature"]
    plan = service.cleanup_plan(signature)
    cleanup = duplicate_manager.cleanup_duplicate_pairs

    def changing_cleanup(targets):
        result = cleanup(targets)
        if external_change == "threshold":
            db.save_settings({"duplicate_image_threshold": 2})
        else:
            with db.connect() as conn:
                if external_change == "unrelated_asset":
                    conn.execute("update assets set width = width + 1 where folder_name = ?", (groups[1]["items"][0]["folder_name"],))
                else:
                    conn.execute(
                        "insert into assets(folder_name,media_type,pair_index,filename,rel_path,created_at,updated_at) "
                        "values(?, 'image', 99, 'new.png', 'new.png', 'now', 'now')", (plan["folder_names"][0],),
                    )
        return result

    duplicate_manager.cleanup_duplicate_pairs = changing_cleanup
    duplicate_manager._run_duplicate_cleanup(signature, plan["folder_names"], plan["targets"])
    assert db.list_task_runs(limit=1)[0]["status"] == "success"
    assert service.list_groups()["cache_stale"]
    with pytest.raises(RuntimeError, match="重新检测"):
        service.cleanup_plan(groups[1]["signature"])


def test_revisiting_results_and_presentation_updates_do_not_expire_cache(library):
    db, storage, service = library
    groups = two_duplicate_groups(library)
    db.set_folder_favorite("A", True)
    db.save_settings({"app_title": "New title", "gallery_default_random": True})
    with db.connect() as conn:
        conn.execute("update folders set updated_at = 'later'")
        conn.execute("update assets set updated_at = 'later', tiny_thumb_rel_path = 'derived.webp'")
    # 模拟离开页面后读取其它信息，再重新进入与服务重启后的缓存读取。
    db.list_folders()
    db.get_settings()
    db.get_sidebar_count_cache()
    assert not service.list_groups()["cache_stale"]
    restarted = DuplicateService(db, storage)
    assert not restarted.list_groups()["cache_stale"]
    plan = restarted.cleanup_plan(groups[0]["signature"])
    restarted.validate_cleanup_targets(groups[0]["signature"], plan["targets"])


def test_waiting_reader_uses_key_after_scan_commits(library, monkeypatch):
    db, _, service = library
    two_duplicate_groups(library)
    started, sampled = threading.Event(), threading.Event()
    main_thread = threading.get_ident()
    cache_key = service._cache_key

    def read_key(*args, **kwargs):
        key = cache_key(*args, **kwargs)
        if threading.get_ident() != main_thread:
            sampled.set()
        return key

    def revisit():
        started.set()
        return service.list_groups()

    monkeypatch.setattr(service, "_cache_key", read_key)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with service._cache_lock:
            with db.connect() as conn:
                conn.execute("update assets set duplicate_fingerprint = null")
            pending = executor.submit(revisit)
            assert started.wait(2)
            # 旧实现此时已读到检测前的 key，随后等待同一个缓存锁。
            sampled.wait(0.1)
            assert not service.list_groups(force=True)["cache_stale"]
        assert not pending.result(timeout=5)["cache_stale"]


def test_rescan_waits_for_duplicate_deletion(library, duplicate_manager, monkeypatch):
    service = library[2]
    group = two_duplicate_groups(library)[0]
    plan = service.cleanup_plan(group["signature"])
    deleted, finish, scan_started, built = (threading.Event() for _ in range(4))
    cleanup, build = duplicate_manager.cleanup_duplicate_pairs, service._build_groups

    def waiting_cleanup(targets):
        result = cleanup(targets)
        deleted.set()
        assert finish.wait(5)
        return result

    def tracked_build(*args):
        built.set()
        return build(*args)

    def rescan():
        scan_started.set()
        return service.list_groups(force=True)

    duplicate_manager.cleanup_duplicate_pairs = waiting_cleanup
    monkeypatch.setattr(service, "_build_groups", tracked_build)
    with ThreadPoolExecutor(max_workers=2) as executor:
        task = executor.submit(duplicate_manager._run_duplicate_cleanup, group["signature"], plan["folder_names"], plan["targets"])
        try:
            assert deleted.wait(2)
            scan = executor.submit(rescan)
            assert scan_started.wait(2)
            assert not built.wait(0.1)
        finally:
            finish.set()
        task.result(timeout=5)
        result = scan.result(timeout=5)
    assert library[0].list_task_runs(limit=1)[0]["status"] == "success"
    assert result["total"] == 1 and not result["cache_stale"]


def test_maximum_matching_recovers_pairs_blocked_by_greedy(library):
    service = library[2]
    result = service._maximum_matches([(0, 1, 10), (1, 1, 11), (1, 2, 10)])
    assert {(left, right) for _, left, right in result} == {(1, 11), (2, 10)}


def test_matching_is_deterministic_and_one_to_one(library):
    edges = [(0, 1, 10), (0, 1, 11), (0, 2, 10), (1, 3, 11), (2, 3, 12)]
    service = library[2]
    result = service._maximum_matches(edges)
    assert result == service._maximum_matches(list(reversed(edges)))
    assert len(result) == len({left for _, left, _ in result}) == len({right for _, _, right in result}) == 3


def test_cache_detects_same_count_changes_and_survives_restart(library):
    db, storage, service = library
    add_folder(library, "A", [1])
    add_folder(library, "B", [1], published=2)
    result = service.list_groups(force=True)
    assert result["total"] == 1 and not result["cache_stale"]
    restarted = DuplicateService(db, storage)
    assert not restarted.list_groups()["cache_stale"]
    with db.connect() as conn:
        conn.execute("update assets set width = width + 1 where folder_name = 'A'")
    assert restarted.list_groups()["cache_stale"]
    with pytest.raises(RuntimeError, match="重新检测"):
        restarted.cleanup_plan(result["items"][0]["signature"])


def test_first_scan_fingerprints_do_not_expire_its_own_cache(library):
    add_folder(library, "A", [1], fingerprint=False)
    add_folder(library, "B", [1], fingerprint=False)
    assert not library[2].list_groups(force=True)["cache_stale"]


def test_rescan_refreshes_fingerprints_after_thumbnail_generation(library):
    add_folder(library, "A", [1], published=1)
    add_folder(library, "B", [1], published=2)
    db, storage, service = library
    service.list_groups(force=True)
    old_fingerprint = image_fingerprint(storage.root / "A-1.png")
    for name in ("A", "B"):
        thumb = storage.root / f"{name}-thumb.webp"
        with Image.open(storage.root / f"{name}-1.png") as image:
            image.resize((34, 32)).save(thumb, quality=50)
        assert image_fingerprint(thumb) != old_fingerprint
        with db.connect() as conn:
            conn.execute("update assets set small_thumb_rel_path = ? where folder_name = ?", (thumb.name, name))

    result = service.list_groups(force=True)
    assert not result["cache_stale"]
    group = result["items"][0]
    for row in db.list_assets_for_folders(["A", "B"]):
        assert row["duplicate_fingerprint"] == image_fingerprint(storage.root / row["small_thumb_rel_path"])
    targets = service.cleanup_plan(group["signature"])["targets"]
    assert targets
    service.validate_cleanup_targets(group["signature"], targets)


@pytest.mark.parametrize("remove_thumbnail", [False, True])
def test_queued_cleanup_accepts_rebuilt_or_removed_thumbnail(library, remove_thumbnail):
    add_folder(library, "A", [1], published=1)
    add_folder(library, "B", [1], published=2)
    db, storage, service = library
    thumb = storage.root / "thumb.png"
    thumb.write_bytes((storage.root / "B-1.png").read_bytes())
    with db.connect() as conn:
        conn.execute("update assets set small_thumb_rel_path = ? where folder_name = 'B'", (thumb.name,))
    group = service.list_groups(force=True)["items"][0]
    targets = service.cleanup_plan(group["signature"])["targets"]
    assert targets
    if remove_thumbnail:
        thumb.unlink()
    else:
        with Image.open(thumb) as image:
            image.resize((34, 32)).save(thumb)
    # 派生图变化不影响已经复核的原图；重启后载入的排队计划也应可执行。
    DuplicateService(db, storage).validate_cleanup_targets(group["signature"], targets)


def test_detection_falls_back_to_original_when_thumbnail_is_missing(library):
    add_folder(library, "A", [1], published=1, fingerprint=False)
    add_folder(library, "B", [1], published=2, fingerprint=False)
    db, _, service = library
    with db.connect() as conn:
        conn.execute("update assets set small_thumb_rel_path = 'missing.webp'")
    group = service.list_groups(force=True)["items"][0]
    targets = service.cleanup_plan(group["signature"])["targets"]
    assert targets
    service.validate_cleanup_targets(group["signature"], targets)


def test_chain_cleanup_keeps_a_direct_survivor_for_every_target(library):
    add_folder(library, "A", [1], published=1)
    add_folder(library, "B", [1, 2], published=2)
    add_folder(library, "C", [2], published=3)
    service = library[2]
    group = service.list_groups(force=True)["items"][0]
    plan = service.cleanup_plan(group["signature"])
    targets = plan["targets"]
    removed = {item["asset_id"] for item in targets}
    assert targets
    assert all(item["keep_asset_id"] not in removed for item in targets)
    service.validate_cleanup_targets(group["signature"], targets)
    eligibility = {item["folder_name"]: item["can_keep_only"] for item in group["items"]}
    assert eligibility == {"A": False, "B": True, "C": False}
    with pytest.raises(RuntimeError, match="未覆盖"):
        service.resolve_plan(group["signature"], "A")
    assert set(service.resolve_plan(group["signature"], "B")) == {"A", "B", "C"}


def test_livephoto_pair_is_never_a_cleanup_target(library):
    add_folder(library, "A", [1], published=1)
    add_folder(library, "B", [1], published=2, live=True)
    service = library[2]
    group = service.list_groups(force=True)["items"][0]
    assert all(target["folder_name"] != "B" for target in service.cleanup_plan(group["signature"])["targets"])
    assert not next(item for item in group["items"] if item["folder_name"] == "A")["can_keep_only"]


def test_queued_cleanup_rejects_missing_keeper(library):
    add_folder(library, "A", [1], published=1)
    add_folder(library, "B", [1], published=2)
    _, storage, service = library
    group = service.list_groups(force=True)["items"][0]
    targets = service.cleanup_plan(group["signature"])["targets"]
    assert targets
    keeper = targets[0]
    (storage.root / f"{keeper['keep_folder_name']}-{keeper['keep_pair_index']}.png").unlink()
    with pytest.raises(RuntimeError, match="缺失"):
        service.validate_cleanup_targets(group["signature"], targets)


def test_queued_cleanup_rejects_changed_original_even_with_old_thumbnail(library):
    add_folder(library, "A", [1], published=1)
    add_folder(library, "B", [1], published=2)
    db, storage, service = library
    thumb = storage.root / "thumb.png"
    thumb.write_bytes((storage.root / "B-1.png").read_bytes())
    with db.connect() as conn:
        conn.execute("update assets set small_thumb_rel_path = ? where folder_name = 'B'", (thumb.name,))
    group = service.list_groups(force=True)["items"][0]
    targets = service.cleanup_plan(group["signature"])["targets"]
    Image.new("RGB", (68, 64), "red").save(storage.root / "B-1.png")
    with pytest.raises(RuntimeError, match="原图.*变化"):
        service.validate_cleanup_targets(group["signature"], targets)
    with pytest.raises(RuntimeError, match="原图复核"):
        service.resolve_plan(group["signature"], "A")


def test_removing_bridge_folder_splits_group_and_removes_old_targets(library):
    add_folder(library, "A", [1], published=1)
    add_folder(library, "B", [1, 2], published=2)
    add_folder(library, "C", [2], published=3)
    service = library[2]
    assert service.list_groups(force=True)["total"] == 1
    with library[0].connect() as conn:
        conn.execute("delete from folders where folder_name = 'B'")
    result = service.remove_folders(["B"])
    assert result["total"] == 0
    assert result["cache_stale"]


def test_settings_change_expires_cache(library):
    add_folder(library, "A", [1])
    add_folder(library, "B", [1])
    service = library[2]
    group = service.list_groups(force=True)["items"][0]
    library[0].save_settings({"duplicate_image_threshold": 2})
    assert service.list_groups()["cache_stale"]
    with pytest.raises(RuntimeError, match="重新检测"):
        service.resolve_plan(group["signature"], "A")


def test_matching_cardinality_against_exhaustive_small_graphs(library):
    def maximum(edges, left=0, used=frozenset()):
        if left == 4:
            return 0
        return max(
            [maximum(edges, left + 1, used)]
            + [1 + maximum(edges, left + 1, used | {right})
               for candidate_left, right in edges if candidate_left == left and right not in used]
        )

    rng = random.Random(13)
    for _ in range(80):
        edges = [(left, right) for left in range(4) for right in range(4) if rng.random() < 0.4]
        result = library[2]._maximum_matches([(rng.randrange(33), left, right) for left, right in edges])
        assert len(result) == maximum(edges)


def test_multiple_images_in_same_pair_are_protected(library):
    add_folder(library, "A", [1], published=1)
    add_folder(library, "B", [1, 2], published=2)
    with library[0].connect() as conn:
        conn.execute("update assets set pair_index = 1 where folder_name = 'B'")
    service = library[2]
    group = service.list_groups(force=True)["items"][0]
    assert not service.cleanup_plan(group["signature"])["targets"]


def test_scan_changed_during_build_is_marked_stale(library, monkeypatch):
    add_folder(library, "A", [1])
    add_folder(library, "B", [1])
    db, _, service = library
    build = service._build_groups

    def changing_build(*args):
        groups = build(*args)
        with db.connect() as conn:
            conn.execute("update folders set title = 'changed' where folder_name = 'A'")
        return groups

    monkeypatch.setattr(service, "_build_groups", changing_build)
    assert service.list_groups(force=True)["cache_stale"]


@pytest.mark.parametrize("operation", ["cleanup", "resolve"])
def test_background_tasks_abort_before_deletion_on_failed_validation(operation):
    # 隔离执行真实任务方法，避免启动依赖已移除旧下载脚本的整套采集服务。
    method_name = f"_run_duplicate_{operation}"
    method = puller_method(method_name)
    manager = Mock()
    manager.db = Mock()
    manager.duplicate_service = Mock()
    manager.duplicate_service.track_deletions.return_value = nullcontext(None)
    manager.duplicate_service.validate_cleanup_targets.side_effect = RuntimeError("计划过期")
    manager.duplicate_service.resolve_plan.side_effect = RuntimeError("计划过期")
    manager.cleanup_duplicate_pairs = Mock()
    manager.move_to_trash = Mock()
    manager._release = Mock()
    manager._run_next_queued_task = Mock()
    if operation == "cleanup":
        method(manager, "signature", ["A", "B"], [])
        manager.duplicate_service.validate_cleanup_targets.assert_called_once()
    else:
        method(manager, "signature", "A", ["B"])
        manager.duplicate_service.resolve_plan.assert_called_once()
    manager.cleanup_duplicate_pairs.assert_not_called()
    manager.move_to_trash.assert_not_called()
    assert manager.db.finish_task_run.call_args.args[1] == "failed"


def test_thumbnail_match_without_matching_original_is_not_cleaned(library):
    add_folder(library, "A", [1], published=1)
    add_folder(library, "B", [1], published=2)
    db, storage, service = library
    thumb = storage.root / "thumb.png"
    thumb.write_bytes((storage.root / "B-1.png").read_bytes())
    with db.connect() as conn:
        conn.execute("update assets set small_thumb_rel_path = ? where folder_name = 'B'", (thumb.name,))
    Image.new("RGB", (68, 64), "red").save(storage.root / "B-1.png")
    group = service.list_groups(force=True)["items"][0]
    assert not service.cleanup_plan(group["signature"])["targets"]
