from pathlib import Path
from unittest.mock import patch

import pytest

from poirot.backend.agents.config.model_router import ModelRouter
from poirot.backend.app.bootstrap import bootstrap_runtime
from poirot.backend.tests.v1._fake_model import FakeChatModelWithTools


def _runtime(root: Path, directory: Path):
    with (
        patch.object(
            ModelRouter,
            "build_model",
            lambda self, role: FakeChatModelWithTools(responses=["answer"]),
        ),
        patch.object(ModelRouter, "chain_names", lambda self, role: ["fake"]),
        patch("poirot.backend.app.bootstrap._check_node_available", return_value=False),
    ):
        return bootstrap_runtime(
            cli_overrides={"storage_root": str(root), "logs_root": str(root / "logs")},
            project_dir=directory,
        )


def test_cd_creates_isolated_thread_and_reuses_projects(tmp_path: Path):
    root = tmp_path / "storage"
    a = tmp_path / "a"
    b = tmp_path / "b"
    shared = tmp_path / "shared"
    a.mkdir()
    b.mkdir()
    shared.mkdir()
    runtime = _runtime(root, a)
    try:
        old_id = runtime.thread_id
        runtime.run_question("in A")
        runtime.add_reference_dir(shared)
        changed = runtime.cd("../b")
        assert changed.thread_id != old_id
        assert changed.project.project_name == "b"
        assert changed.thread_store.require(changed.thread_id).cwd == str(b.resolve())
        assert changed.thread_store.require(changed.thread_id).extra_dirs == ()
        assert changed.thread_store.require(old_id).cwd == str(a.resolve())
        assert changed.thread_store.require(old_id).extra_dirs == (str(shared.resolve()),)
        assert changed.thread_store.list_project("a")[0].thread_id == old_id
        assert changed.thread_store.list_project("b") == []
        changed.run_question("in B")
        assert changed.thread_store.list_project("b")[0].thread_id == changed.thread_id

        same = changed.cd(b)
        assert same.thread_id != changed.thread_id
        assert same.thread_store.require(same.thread_id).cwd == str(b.resolve())
        assert same.thread_store.require(changed.thread_id).cwd == str(b.resolve())
    finally:
        runtime.close()


def test_cd_failure_keeps_current_runtime_and_does_not_create_thread(tmp_path: Path):
    a = tmp_path / "a"
    a.mkdir()
    runtime = _runtime(tmp_path / "storage", a)
    try:
        old_id = runtime.thread_id
        before = set(item.thread_id for item in runtime.thread_store.list())
        with pytest.raises((ValueError, FileNotFoundError)):
            runtime.cd("missing")
        assert runtime.thread_id == old_id
        assert set(item.thread_id for item in runtime.thread_store.list()) == before

        conflicting = tmp_path / "other"
        conflicting.mkdir()
        runtime.project_store.ensure(conflicting, "b")
        target = tmp_path / "b"
        target.mkdir()
        with pytest.raises(ValueError, match="already bound"):
            runtime.cd(target)
        assert runtime.thread_id == old_id
    finally:
        runtime.close()


def test_cd_runtime_initialization_failure_cleans_pending_target(tmp_path: Path, monkeypatch):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    runtime = _runtime(tmp_path / "storage", a)
    try:
        old_id = runtime.thread_id
        monkeypatch.setattr(
            runtime.checkpointer,
            "get_tuple",
            lambda config: (_ for _ in ()).throw(ValueError("checkpoint unavailable")),
        )
        with pytest.raises(ValueError, match="checkpoint unavailable"):
            runtime.cd(b)
        assert runtime.thread_id == old_id
        assert runtime.thread_store.list_project("b") == []
        assert all(item.thread_id == old_id for item in runtime.thread_store.list())
    finally:
        runtime.close()


def test_cd_on_unbound_legacy_thread_requires_absolute_path(tmp_path: Path):
    directory = tmp_path / "a"
    directory.mkdir()
    runtime = _runtime(tmp_path / "storage", directory)
    try:
        legacy = runtime.thread_store.create("legacy")
        runtime = runtime.switch_thread(legacy.thread_id)
        with pytest.raises(ValueError, match="absolute path"):
            runtime.cd("relative")
    finally:
        runtime.close()
