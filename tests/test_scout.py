"""Phase 2 acceptance: non-git fallback, unparseable file, over-long
function. Plus the chunking corner cases (Class.method symbols, a
method-less class, the module-level synthetic chunk) since they're cheap
to pin down now and expensive to debug later via a live scan.
"""
import subprocess
from pathlib import Path

from omen import scout


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@test.com")
    _git(repo, "config", "user.name", "Test")


# ---------------------------------------------------------------------------
# Scope resolution
# ---------------------------------------------------------------------------


def test_non_git_dir_falls_back_to_full_tree(tmp_path):
    (tmp_path / "a.py").write_text("def f():\n    pass\n")
    result = scout.resolve_scope(tmp_path)
    assert result.scope == "full-tree (non-git fallback)"
    assert result.files == [tmp_path / "a.py"]


def test_git_diff_scope_shows_only_uncommitted_changes(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "a.py").write_text("def f():\n    return 1\n")
    (tmp_path / "b.py").write_text("def g():\n    return 2\n")
    _git(tmp_path, "add", "a.py", "b.py")
    _git(tmp_path, "commit", "-qm", "initial")

    (tmp_path / "a.py").write_text("def f():\n    return 1\n\ndef h():\n    return 3\n")

    result = scout.resolve_scope(tmp_path)
    assert result.scope == "diff"
    assert result.files == [tmp_path / "a.py"]


def test_git_all_scope_returns_every_tracked_file(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "a.py").write_text("def f():\n    pass\n")
    (tmp_path / "b.py").write_text("def g():\n    pass\n")
    _git(tmp_path, "add", "a.py", "b.py")
    _git(tmp_path, "commit", "-qm", "initial")

    result = scout.resolve_scope(tmp_path, all_files=True)
    assert result.scope == "all"
    assert sorted(result.files) == sorted([tmp_path / "a.py", tmp_path / "b.py"])


def test_git_since_scope_diffs_against_given_ref(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "a.py").write_text("def f():\n    return 1\n")
    _git(tmp_path, "add", "a.py")
    _git(tmp_path, "commit", "-qm", "initial")
    first_sha = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()

    (tmp_path / "b.py").write_text("def g():\n    return 2\n")
    _git(tmp_path, "add", "b.py")
    _git(tmp_path, "commit", "-qm", "second")

    result = scout.resolve_scope(tmp_path, since=first_sha)
    assert result.scope == f"since:{first_sha}"
    assert result.files == [tmp_path / "b.py"]


def test_git_diff_skips_deleted_files(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "a.py").write_text("def f():\n    pass\n")
    _git(tmp_path, "add", "a.py")
    _git(tmp_path, "commit", "-qm", "initial")
    (tmp_path / "a.py").unlink()

    result = scout.resolve_scope(tmp_path)
    assert result.files == []


# ---------------------------------------------------------------------------
# ast chunking
# ---------------------------------------------------------------------------


def test_unparseable_file_is_skipped_not_raised(tmp_path):
    bad = tmp_path / "broken.py"
    bad.write_text("def f(:\n    this is not python\n")
    assert scout.chunk_file(bad, repo_root=tmp_path) == []


def test_over_long_function_splits_with_overlap(tmp_path):
    body = "\n".join(f"    x = {i}" for i in range(150))
    path = tmp_path / "big.py"
    path.write_text(f"def big():\n{body}\n    return x\n")

    chunks = scout.chunk_file(path, repo_root=tmp_path)

    assert len(chunks) > 1
    assert [c.symbol for c in chunks] == [f"big#{i}" for i in range(1, len(chunks) + 1)]
    for c in chunks:
        assert c.end_line - c.start_line + 1 <= scout.CHUNK_LINE_CAP
    # consecutive windows overlap by exactly CHUNK_OVERLAP lines
    for prev, nxt in zip(chunks, chunks[1:]):
        assert prev.end_line - nxt.start_line + 1 == scout.CHUNK_OVERLAP
    # the windows jointly cover the whole function, no gap
    assert chunks[0].start_line == 1
    assert chunks[-1].end_line == len(body.splitlines()) + 2


def test_short_function_is_a_single_chunk(tmp_path):
    path = tmp_path / "small.py"
    path.write_text("def small():\n    return 1\n")
    chunks = scout.chunk_file(path, repo_root=tmp_path)
    assert len(chunks) == 1
    assert chunks[0].symbol == "small"
    assert chunks[0].start_line == 1
    assert chunks[0].end_line == 2


def test_class_methods_get_dotted_symbols(tmp_path):
    path = tmp_path / "cls.py"
    path.write_text(
        "class Foo:\n"
        "    def method_a(self):\n"
        "        return 1\n"
        "\n"
        "    def method_b(self):\n"
        "        return 2\n"
    )
    chunks = scout.chunk_file(path, repo_root=tmp_path)
    assert {c.symbol for c in chunks} == {"Foo.method_a", "Foo.method_b"}


def test_class_with_no_methods_is_one_whole_class_chunk(tmp_path):
    path = tmp_path / "exc.py"
    path.write_text("class MyError(Exception):\n    pass\n")
    chunks = scout.chunk_file(path, repo_root=tmp_path)
    assert len(chunks) == 1
    assert chunks[0].symbol == "MyError"


def test_decorators_are_included_in_the_chunk(tmp_path):
    path = tmp_path / "deco.py"
    path.write_text("@app.route('/x')\ndef view():\n    return 1\n")
    chunks = scout.chunk_file(path, repo_root=tmp_path)
    assert len(chunks) == 1
    assert chunks[0].start_line == 1
    assert "@app.route" in chunks[0].content


def test_module_level_statements_become_one_synthetic_chunk(tmp_path):
    path = tmp_path / "mod.py"
    path.write_text(
        "import os\n"
        "\n"
        "CONST = 1\n"
        "\n"
        "def f():\n"
        "    return 1\n"
        "\n"
        "print('side effect')\n"
    )
    chunks = scout.chunk_file(path, repo_root=tmp_path)
    module_chunks = [c for c in chunks if c.symbol == "<module>"]
    assert len(module_chunks) == 1
    assert "CONST = 1" in module_chunks[0].content
    assert "print(" in module_chunks[0].content
    assert "import os" not in module_chunks[0].content


def test_file_with_only_imports_has_no_module_chunk(tmp_path):
    path = tmp_path / "imports_only.py"
    path.write_text("import os\nimport sys\n\ndef f():\n    return 1\n")
    chunks = scout.chunk_file(path, repo_root=tmp_path)
    assert all(c.symbol != "<module>" for c in chunks)


def test_content_hash_is_deterministic_and_content_addressed(tmp_path):
    path = tmp_path / "a.py"
    path.write_text("def f():\n    return 1\n")
    chunks1 = scout.chunk_file(path, repo_root=tmp_path)
    chunks2 = scout.chunk_file(path, repo_root=tmp_path)
    assert chunks1[0].content_hash == chunks2[0].content_hash

    path.write_text("def f():\n    return 2\n")
    chunks3 = scout.chunk_file(path, repo_root=tmp_path)
    assert chunks3[0].content_hash != chunks1[0].content_hash
