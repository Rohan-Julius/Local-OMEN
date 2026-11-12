"""Phase 2 (production plan): exit codes (audit B18), the global --store /
--verbose / --quiet options, the console-script entry point, and lazy ADK
import (audit B28). No model needed.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from omen import cli, config

PROJECT_ROOT = Path(__file__).resolve().parent.parent
INCIDENTS_YAML = PROJECT_ROOT / "incidents.yaml"


@pytest.fixture(autouse=True)
def restore_store_config(monkeypatch):
    """`--store` changes module-level config; put it back after each test."""
    for name in ("STORE_DIR", "DB_PATH", "CHROMA_PATH"):
        monkeypatch.setattr(config, name, getattr(config, name))


async def run(*argv: str) -> int:
    return await cli.main(list(argv))


# ---------------------------------------------------------------------------
# Exit codes: 0 ok, 1 user error, 3 runtime error (2 is reserved for --fail-on)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seed_then_list_exit_zero_with_store_flag(tmp_path, capsys):
    store_dir = tmp_path / "store"
    assert await run("seed", str(INCIDENTS_YAML), "--store", str(store_dir)) == cli.EXIT_OK
    assert (store_dir / "omen.db").is_file()
    assert await run("memory", "list", "--store", str(store_dir)) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "Seeded 6 incident(s)" in out
    assert "OMEN-001" in out and "OMEN-006" in out


@pytest.mark.asyncio
async def test_store_flag_works_before_the_subcommand(tmp_path, capsys):
    store_dir = tmp_path / "store"
    assert await run("--store", str(store_dir), "seed", str(INCIDENTS_YAML)) == cli.EXIT_OK
    assert (store_dir / "omen.db").is_file()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "argv",
    [
        ["memory", "list"],  # empty ledger
        ["memory", "forget", "OMEN-999"],  # unknown ref
        ["seed", "no-such-file.yaml"],
        ["scan", "no-such-dir"],
        ["learn"],  # neither source
        ["learn", "no-such-postmortem.md"],
        ["calibrate", "--fixtures-path", "no-such-fixtures.yaml"],
    ],
    ids=["empty-ledger", "unknown-ref", "missing-yaml", "missing-scan-path", "no-learn-source", "missing-postmortem", "missing-fixtures"],
)
async def test_user_errors_exit_one(tmp_path, argv):
    assert await run(*argv, "--store", str(tmp_path / "store")) == cli.EXIT_USER_ERROR


@pytest.mark.asyncio
@pytest.mark.parametrize("argv", [["bogus"], ["memory"], ["scan", "--no-such-flag"], ["-v", "memory", "list", "-q"]])
async def test_argument_errors_exit_one_not_two(argv, capsys):
    with pytest.raises(SystemExit) as exc:
        await run(*argv)
    assert exc.value.code == cli.EXIT_USER_ERROR
    assert "error:" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_runtime_error_exits_three_with_one_line(tmp_path, monkeypatch, capsys):
    def boom(args):
        raise RuntimeError("Ollama went away")

    monkeypatch.setattr(cli, "cmd_reindex", boom)
    assert await run("reindex", "--store", str(tmp_path)) == cli.EXIT_RUNTIME_ERROR
    err = capsys.readouterr().err
    assert "error: RuntimeError: Ollama went away" in err
    assert "Traceback" not in err


@pytest.mark.asyncio
async def test_runtime_error_traceback_only_with_verbose(tmp_path, monkeypatch, capsys):
    def boom(args):
        raise RuntimeError("Ollama went away")

    monkeypatch.setattr(cli, "cmd_reindex", boom)
    assert await run("reindex", "-v", "--store", str(tmp_path)) == cli.EXIT_RUNTIME_ERROR
    assert "Traceback" in capsys.readouterr().err


def test_entrypoint_exits_with_mains_code(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["omen", "memory", "list", "--store", str(tmp_path / "empty")])
    with pytest.raises(SystemExit) as exc:
        cli.entrypoint()
    assert exc.value.code == cli.EXIT_USER_ERROR


# ---------------------------------------------------------------------------
# --verbose / --quiet: diagnostics only, stdout unchanged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verbose_and_quiet_change_diagnostics_not_output(tmp_path, capsys):
    plain = tmp_path / "plain"  # not a git repo: Scout logs a warning
    plain.mkdir()
    (plain / "a.py").write_text("def f():\n    return 1\n")

    results = {}
    for flag in ([], ["-v"], ["-q"]):
        assert await run("scan", str(plain), "--dry-run", *flag) == cli.EXIT_OK
        results[tuple(flag)] = capsys.readouterr()

    assert results[()].out == results[("-v",)].out == results[("-q",)].out
    assert "scope: full-tree (non-git fallback)" in results[()].out
    assert "warning:" in results[()].err and "not a git repository" in results[()].err
    assert "debug: store:" in results[("-v",)].err
    assert "debug:" not in results[()].err
    assert results[("-q",)].err == ""


# ---------------------------------------------------------------------------
# B28: commands that run no model never import ADK
# ---------------------------------------------------------------------------


def test_memory_list_does_not_import_adk_or_litellm(tmp_path):
    code = (
        "import asyncio, sys\n"
        "from omen import cli\n"
        f"asyncio.run(cli.main(['memory', 'list', '--store', {str(tmp_path)!r}]))\n"
        "print(sorted({m.split('.')[0] + '.' + m.split('.')[1] if m.startswith('google.') else m.split('.')[0]"
        " for m in sys.modules if m.startswith(('google.adk', 'google.genai', 'litellm'))}))\n"
    )
    env = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT)}
    result = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env, capture_output=True, text=True, check=True)
    assert result.stdout.strip().splitlines()[-1] == "[]"


def test_scan_runner_import_is_lazy(tmp_path):
    """Importing the CLI must not import runners.py's ADK side either."""
    code = "import sys\nfrom omen import cli, runners\nprint(any(m.startswith('google.adk') for m in sys.modules))\n"
    env = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT)}
    result = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env, capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "False"
