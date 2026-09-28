from pathlib import Path

import pytest

from poirot.backend.agents.runtime.file_access import (
    FileOutsideThreadError,
    FileReferenceAmbiguous,
    FileReferenceNotFound,
    ThreadFileAccess,
    UnboundThreadError,
    prepare_question,
    extract_references,
)
from poirot.backend.agents.sandbox.local.local_sandbox_provider import LocalSandboxProvider
from poirot.backend.agents.sandbox.types import PathMapping


def test_file_reference_keeps_question_and_reads_utf8(tmp_path: Path):
    (tmp_path / "main.py").write_text("print('ok')", encoding="utf-8")
    prepared = prepare_question("@main.py 帮我看看", ThreadFileAccess(tmp_path))
    assert prepared.original == "@main.py 帮我看看"
    assert "print('ok')" in prepared.enriched
    assert prepared.references[0].relative_path == "main.py"


def test_at_in_email_and_decorator_is_not_a_file_reference():
    assert extract_references("mail foo@example.com and @router.get('/x')") == []


def test_explicit_path_and_traversal_are_thread_scoped(tmp_path: Path):
    project = tmp_path / "project"
    outside = tmp_path / "outside.py"
    project.mkdir()
    outside.write_text("secret", encoding="utf-8")
    access = ThreadFileAccess(project)
    with pytest.raises(FileOutsideThreadError):
        access.resolve("../outside.py")
    with pytest.raises(FileOutsideThreadError):
        access.resolve(outside)


def test_duplicate_basename_is_not_selected_implicitly(tmp_path: Path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "main.py").write_text("a", encoding="utf-8")
    (tmp_path / "b" / "main.py").write_text("b", encoding="utf-8")
    with pytest.raises(FileReferenceAmbiguous):
        prepare_question("@main.py", ThreadFileAccess(tmp_path))


def test_binary_and_size_limits(tmp_path: Path):
    (tmp_path / "binary.dat").write_bytes(b"\x00abc")
    (tmp_path / "large.txt").write_bytes(b"x" * (100 * 1024 + 1))
    access = ThreadFileAccess(tmp_path)
    with pytest.raises(Exception, match="Binary"):
        prepare_question("@binary.dat", access)
    with pytest.raises(Exception, match="100 KiB"):
        prepare_question("@large.txt", access)


def test_unbound_thread_fails_closed(tmp_path: Path):
    with pytest.raises(UnboundThreadError):
        prepare_question("@main.py", ThreadFileAccess(None))


def test_suggestions_skip_dependency_trees_and_reuse_index(tmp_path: Path, monkeypatch):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("ok", encoding="utf-8")
    (tmp_path / ".venv" / "lib").mkdir(parents=True)
    (tmp_path / ".venv" / "lib" / "main.py").write_text("dependency", encoding="utf-8")

    access = ThreadFileAccess(tmp_path)
    assert access.suggest_paths("main") == [tmp_path / "src" / "main.py"]

    def fail_if_rebuilt():
        raise AssertionError("suggestion index was rebuilt for a later keystroke")

    monkeypatch.setattr(access, "_build_suggestion_index", fail_if_rebuilt)
    assert access.suggest_paths("mai") == [tmp_path / "src" / "main.py"]
    assert access.search_filename("main.py") == [tmp_path / "src" / "main.py"]


def test_directory_listing_skips_dependency_trees(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("ok", encoding="utf-8")
    (tmp_path / ".venv" / "lib").mkdir(parents=True)
    (tmp_path / ".venv" / "lib" / "dependency.py").write_text("hidden", encoding="utf-8")

    _, rows, truncated = ThreadFileAccess(tmp_path).list_text_files(".")
    assert not truncated
    assert [relative for relative, _ in rows] == ["src/main.py"]


def test_directory_reference_lists_without_injecting_body(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("secret body", encoding="utf-8")
    prepared = prepare_question("@src", ThreadFileAccess(tmp_path))
    assert "main.py" in prepared.enriched
    assert "secret body" not in prepared.enriched


def test_extra_roots_use_cwd_priority_then_append_order(tmp_path: Path):
    cwd = tmp_path / "project"
    first = tmp_path / "first"
    second = tmp_path / "second"
    cwd.mkdir()
    first.mkdir()
    second.mkdir()
    (first / "shared.py").write_text("first", encoding="utf-8")
    (second / "shared.py").write_text("second", encoding="utf-8")
    access = ThreadFileAccess(cwd, [first, second])
    assert [path.parent for path in access.search_filename("shared.py")] == [first, second]
    prepared = prepare_question("@shared.py", access, choose=lambda _name, matches: matches[0])
    assert "first" in prepared.enriched
    (cwd / "shared.py").write_text("cwd", encoding="utf-8")
    access.invalidate_suggestion_index()
    assert access.search_filename("shared.py") == [cwd / "shared.py"]
    assert "cwd" in prepare_question("@shared.py", access).enriched


def test_extra_root_explicit_relative_path_and_symlink_escape(tmp_path: Path):
    cwd = tmp_path / "project"
    extra = tmp_path / "extra"
    outside = tmp_path / "outside.py"
    cwd.mkdir()
    extra.mkdir()
    outside.write_text("secret", encoding="utf-8")
    (extra / "src").mkdir()
    (extra / "src" / "foo.py").write_text("extra", encoding="utf-8")
    (extra / "escape.py").symlink_to(outside)
    access = ThreadFileAccess(cwd, [extra])
    assert "extra" in prepare_question("@src/foo.py", access).enriched
    with pytest.raises(FileOutsideThreadError):
        access.resolve("escape.py")


def test_local_sandbox_uses_thread_cwd_and_rejects_unbound(tmp_path: Path):
    bound = tmp_path / "bound"
    outside = tmp_path / "outside"
    bound.mkdir()
    outside.mkdir()
    (bound / "ok.txt").write_text("ok", encoding="utf-8")
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    provider = LocalSandboxProvider(
        [PathMapping("/mnt/poirot/user-data/workspace", str(tmp_path / "fixed"))],
        thread_cwd_resolver=lambda thread_id: str(bound) if thread_id == "bound" else None,
    )
    sandbox_id = provider.acquire("bound")
    sandbox = provider.get(sandbox_id)
    assert sandbox is not None
    assert sandbox.read_file("/mnt/poirot/user-data/workspace/ok.txt") == "ok"
    with pytest.raises(Exception):
        sandbox.read_file("/mnt/poirot/user-data/workspace/../outside/secret.txt")
    unbound_id = provider.acquire("unbound")
    unbound = provider.get(unbound_id)
    assert unbound is not None
    with pytest.raises(Exception):
        unbound.read_file("/mnt/poirot/user-data/workspace/ok.txt")
