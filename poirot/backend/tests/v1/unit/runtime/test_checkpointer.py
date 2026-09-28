from poirot.backend.agents.runtime.checkpointer import SessionCheckpointer

from poirot.backend.agents.runtime.checkpointer import (
    get_checkpointer,
    reset_checkpointer,
)


def test_get_checkpointer_returns_singleton(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("POIROT_STORAGE_ROOT", str(tmp_path))
    reset_checkpointer()
    cp1 = get_checkpointer()
    cp2 = get_checkpointer()
    assert cp1 is cp2


def test_get_checkpointer_returns_sqlite_saver(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("POIROT_STORAGE_ROOT", str(tmp_path))
    reset_checkpointer()
    cp = get_checkpointer()
    assert isinstance(cp, SessionCheckpointer)
    cp.get_tuple({"configurable": {"thread_id": "test"}})
    assert (cp.store.session_dir("test") / "checkpoints.db").is_file()
    assert not (tmp_path / "checkpoints.db").exists()


def test_reset_checkpointer_creates_new_instance(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("POIROT_STORAGE_ROOT", str(tmp_path))
    reset_checkpointer()
    cp1 = get_checkpointer()
    reset_checkpointer()
    cp2 = get_checkpointer()
    assert cp1 is not cp2
