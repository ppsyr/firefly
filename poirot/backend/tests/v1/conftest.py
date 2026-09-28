"""Keep checkpoint tests away from the developer's user-level store."""
import pytest

from poirot.backend.agents.runtime.checkpointer import reset_checkpointer


@pytest.fixture(autouse=True)
def isolated_checkpoint_store(tmp_path, monkeypatch):
    reset_checkpointer()
    monkeypatch.setenv("POIROT_STORAGE_ROOT", str(tmp_path / "user-store"))
    yield
    reset_checkpointer()
