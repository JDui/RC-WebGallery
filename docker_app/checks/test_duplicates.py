"""重复检测的离线回归检查；所有数据库和图片均在临时目录生成。"""

import ast
import random
from pathlib import Path
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
    tree = ast.parse((Path(__file__).parents[1] / "app/services/puller.py").read_text(encoding="utf-8"))
    manager_class = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "PullManager")
    method_name = f"_run_duplicate_{operation}"
    method = next(node for node in manager_class.body if isinstance(node, ast.FunctionDef) and node.name == method_name)
    namespace = {"Any": Any}
    exec(compile(ast.Module(body=[method], type_ignores=[]), "puller-task", "exec"), namespace)
    manager = Mock()
    manager.db = Mock()
    manager.duplicate_service = Mock()
    manager.duplicate_service.validate_cleanup_targets.side_effect = RuntimeError("计划过期")
    manager.duplicate_service.resolve_plan.side_effect = RuntimeError("计划过期")
    manager.cleanup_duplicate_pairs = Mock()
    manager.move_to_trash = Mock()
    manager._release = Mock()
    manager._run_next_queued_task = Mock()
    if operation == "cleanup":
        namespace[method_name](manager, "signature", ["A", "B"], [])
    else:
        namespace[method_name](manager, "signature", "A", ["B"])
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
