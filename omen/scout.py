"""Pure Python, no models: scope resolution via local git, then `ast`
chunking with exact line spans. Fully unit-testable without a running
Ollama (PLAN.md Phase 2).
"""
from __future__ import annotations

import ast
import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path

from omen.contracts import Chunk

CHUNK_LINE_CAP = 120
CHUNK_OVERLAP = 10

SKIP_DIR_NAMES = {".git", ".venv", "venv", "__pycache__", "node_modules", ".pytest_cache"}


# ---------------------------------------------------------------------------
# Scope resolution
# ---------------------------------------------------------------------------


@dataclass
class ScopeResult:
    files: list[Path]
    scope: str  # human-readable description, stored in runs.scope


def _run_git(repo_path: Path, args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo_path), *args],
        capture_output=True,
        text=True,
    )


def _is_git_repo(repo_path: Path) -> bool:
    result = _run_git(repo_path, ["rev-parse", "--is-inside-work-tree"])
    return result.returncode == 0 and result.stdout.strip() == "true"


def _walk_py_files(repo_path: Path) -> list[Path]:
    files = []
    for path in repo_path.rglob("*.py"):
        if any(part in SKIP_DIR_NAMES or part.startswith(".") for part in path.relative_to(repo_path).parts[:-1]):
            continue
        files.append(path)
    return sorted(files)


def _git_tracked_py_files(repo_path: Path) -> list[Path]:
    result = _run_git(repo_path, ["ls-files"])
    files = []
    for line in result.stdout.splitlines():
        if line.endswith(".py"):
            files.append(repo_path / line)
    return files


def _git_diff_py_files(repo_path: Path, since: str | None) -> list[Path]:
    args = ["diff", "--name-only"]
    if since:
        args.append(since)
    result = _run_git(repo_path, args)
    files = []
    for line in result.stdout.splitlines():
        if line.endswith(".py"):
            candidate = repo_path / line
            if candidate.exists():  # diff can list deleted files
                files.append(candidate)
    return files


def resolve_scope(repo_path: Path, since: str | None = None, all_files: bool = False) -> ScopeResult:
    """Resolve which files to scan.

    Default: `git diff --name-only` (uncommitted changes). `--since <rev>`
    diffs against a specific ref. `--all` walks the whole tracked tree.
    A path that isn't a git repo falls back to a full filesystem walk, with
    a printed notice — platform-agnostic, per PLAN.md's decision to keep
    scope resolution local-git-only rather than depend on a host API.
    """
    repo_path = Path(repo_path).resolve()

    if not _is_git_repo(repo_path):
        print(f"warning: {repo_path} is not a git repository - falling back to a full tree scan")
        return ScopeResult(files=_walk_py_files(repo_path), scope="full-tree (non-git fallback)")

    if all_files:
        return ScopeResult(files=_git_tracked_py_files(repo_path), scope="all")

    if since:
        return ScopeResult(files=_git_diff_py_files(repo_path, since), scope=f"since:{since}")

    return ScopeResult(files=_git_diff_py_files(repo_path, None), scope="diff")


# ---------------------------------------------------------------------------
# ast chunking
# ---------------------------------------------------------------------------


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _slice_lines(lines: list[str], start_line: int, end_line: int) -> str:
    """1-indexed, inclusive on both ends."""
    return "\n".join(lines[start_line - 1 : end_line])


def _def_start_line(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> int:
    """Include decorators in the chunk — an auth-path chunk without its
    @app.route/@requires_permission decorator is missing the evidence a
    reviewer (human or model) would need."""
    if node.decorator_list:
        return node.decorator_list[0].lineno
    return node.lineno


def _split_oversized(file_path: str, symbol: str, lines: list[str], start_line: int, end_line: int) -> list[Chunk]:
    total = end_line - start_line + 1
    if total <= CHUNK_LINE_CAP:
        content = _slice_lines(lines, start_line, end_line)
        return [
            Chunk(
                file_path=file_path,
                start_line=start_line,
                end_line=end_line,
                symbol=symbol,
                content=content,
                content_hash=_content_hash(content),
            )
        ]

    step = CHUNK_LINE_CAP - CHUNK_OVERLAP
    windows: list[tuple[int, int]] = []
    window_start = start_line
    while window_start <= end_line:
        window_end = min(window_start + CHUNK_LINE_CAP - 1, end_line)
        windows.append((window_start, window_end))
        if window_end == end_line:
            break
        window_start += step

    chunks = []
    for i, (w_start, w_end) in enumerate(windows, start=1):
        content = _slice_lines(lines, w_start, w_end)
        chunks.append(
            Chunk(
                file_path=file_path,
                start_line=w_start,
                end_line=w_end,
                symbol=f"{symbol}#{i}",
                content=content,
                content_hash=_content_hash(content),
            )
        )
    return chunks


def _is_docstring(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def chunk_file(path: Path, repo_root: Path | None = None) -> list[Chunk]:
    """Chunk one file: each top-level function/async function, each method
    of a top-level class (symbol `Class.method`), a whole-class chunk for
    classes with no methods, and one synthetic chunk bundling module-level
    statements. Syntax errors are warned and skipped, never raised."""
    path = Path(path).resolve()
    repo_root = Path(repo_root).resolve() if repo_root else None

    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        print(f"warning: could not read {path}: {e}")
        return []

    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as e:
        print(f"warning: skipping {path} - syntax error: {e}")
        return []

    lines = source.splitlines()
    display_path = str(path.relative_to(repo_root)) if repo_root else str(path)

    chunks: list[Chunk] = []
    module_level_nodes: list[ast.stmt] = []

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start = _def_start_line(node)
            end = node.end_lineno
            chunks.extend(_split_oversized(display_path, node.name, lines, start, end))

        elif isinstance(node, ast.ClassDef):
            methods = [n for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
            if not methods:
                start = _def_start_line(node)
                end = node.end_lineno
                chunks.extend(_split_oversized(display_path, node.name, lines, start, end))
            else:
                for method in methods:
                    start = _def_start_line(method)
                    end = method.end_lineno
                    symbol = f"{node.name}.{method.name}"
                    chunks.extend(_split_oversized(display_path, symbol, lines, start, end))

        elif isinstance(node, (ast.Import, ast.ImportFrom)) or _is_docstring(node):
            continue  # not interesting on their own; not worth a synthetic chunk

        else:
            module_level_nodes.append(node)

    if module_level_nodes:
        start = min(_def_start_line(n) if hasattr(n, "decorator_list") else n.lineno for n in module_level_nodes)
        end = max(n.end_lineno for n in module_level_nodes)
        content = "\n".join(_slice_lines(lines, n.lineno, n.end_lineno) for n in module_level_nodes)
        chunks.append(
            Chunk(
                file_path=display_path,
                start_line=start,
                end_line=end,
                symbol="<module>",
                content=content,
                content_hash=_content_hash(content),
            )
        )

    return chunks


def chunk_files(files: list[Path], repo_root: Path | None = None) -> list[Chunk]:
    chunks: list[Chunk] = []
    for path in files:
        chunks.extend(chunk_file(path, repo_root=repo_root))
    return chunks
