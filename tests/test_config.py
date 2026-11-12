"""Phase 2 (production plan): one configured Ollama address (audit B16),
the store location (OMEN_HOME / --store), and prompts shipped as package
data. No model and no network: every Ollama-facing call is intercepted.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from omen import config, prompts, runners, vectors
from omen.contracts import TriageVerdict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FAKE_HOST = "http://ollama.test:5555"
PROMPT_NAMES = ["adjudicator.txt", "archivist_git.txt", "archivist_postmortem.txt", "investigator.txt", "triage.txt"]


def _python(code: str, cwd: Path, **env: str) -> str:
    """Run a fresh interpreter, so module-level config is read anew."""
    full_env = {k: v for k, v in os.environ.items() if k not in ("OLLAMA_HOST", "OMEN_HOME")}
    full_env.update(env)
    full_env["PYTHONPATH"] = str(PROJECT_ROOT)
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=cwd, env=full_env, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# OLLAMA_HOST: one source, read from the environment (B16)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, "http://localhost:11434"),
        ("", "http://localhost:11434"),
        ("http://localhost:11434", "http://localhost:11434"),
        ("http://localhost:11434/", "http://localhost:11434"),
        ("gpu-box:8080", "http://gpu-box:8080"),
        ("gpu-box", "http://gpu-box:11434"),
        ("0.0.0.0", "http://127.0.0.1:11434"),  # Ollama's server bind address, not a client target
        ("https://ollama.internal", "https://ollama.internal:11434"),
        ("[::1]:11434", "http://[::1]:11434"),
    ],
)
def test_normalize_ollama_host(raw, expected):
    assert config.normalize_ollama_host(raw) == expected


def test_ollama_host_is_read_from_the_environment(tmp_path):
    code = "from omen import config; print(config.OLLAMA_HOST)"
    assert _python(code, tmp_path) == "http://localhost:11434"
    assert _python(code, tmp_path, OLLAMA_HOST="gpu-box:8080") == "http://gpu-box:8080"


def test_embedding_call_uses_the_configured_host(monkeypatch):
    seen = []

    def fake_urlopen(req, *a, **k):
        seen.append(req.full_url)
        return io.BytesIO(json.dumps({"embeddings": [[0.1, 0.2]]}).encode())

    monkeypatch.setattr(config, "OLLAMA_HOST", FAKE_HOST)
    monkeypatch.setattr(vectors.urllib.request, "urlopen", fake_urlopen)
    assert vectors.embed_document(["x"]) == [[0.1, 0.2]]
    assert seen == [f"{FAKE_HOST}/api/embed"]


@pytest.mark.asyncio
async def test_direct_runner_client_uses_the_configured_host(monkeypatch):
    hosts = []
    verdict = TriageVerdict(code_mechanism="m", verdict="NO_MATCH", confidence="low", reasoning="r")

    class FakeClient:
        def __init__(self, host=None, **kwargs):
            hosts.append(host)

        def chat(self, **kwargs):
            return type("Resp", (), {"message": type("Msg", (), {"content": verdict.model_dump_json()})()})()

    monkeypatch.setattr(config, "OLLAMA_HOST", FAKE_HOST)
    monkeypatch.setattr(runners.ollama, "Client", FakeClient)
    result = await runners.DirectOllamaRunner().run_structured("instr", "prompt", TriageVerdict)
    assert result == verdict
    assert hosts == [FAKE_HOST]


def test_adk_model_api_base_is_the_configured_host(monkeypatch):
    from omen import agents

    monkeypatch.setattr(config, "OLLAMA_HOST", FAKE_HOST)
    model = agents._model(think=False)
    # LiteLlm forwards these kwargs to every litellm completion call.
    assert model._additional_args["api_base"] == FAKE_HOST


# ---------------------------------------------------------------------------
# Store location
# ---------------------------------------------------------------------------


def test_store_defaults_to_omen_store_and_honours_omen_home(tmp_path):
    code = "from omen import config; print(config.STORE_DIR, config.DB_PATH, config.CHROMA_PATH, sep='|')"
    default = _python(code, tmp_path).split("|")
    assert [Path(p) for p in default] == [Path("omen_store"), Path("omen_store/omen.db"), Path("omen_store/chroma")]

    home = tmp_path / "elsewhere"
    custom = _python(code, tmp_path, OMEN_HOME=str(home)).split("|")
    assert [Path(p) for p in custom] == [home, home / "omen.db", home / "chroma"]


def test_store_and_vectors_follow_set_store_dir(tmp_path, monkeypatch):
    from omen import store

    for name in ("STORE_DIR", "DB_PATH", "CHROMA_PATH"):
        monkeypatch.setattr(config, name, getattr(config, name))  # restored after the test
    monkeypatch.setattr(vectors, "CHROMA_PATH", None)

    config.set_store_dir(tmp_path / "s")
    store.connect().close()
    assert (tmp_path / "s" / "omen.db").is_file()
    assert vectors._chroma_path() == tmp_path / "s" / "chroma"


# ---------------------------------------------------------------------------
# Prompts as package data
# ---------------------------------------------------------------------------


def test_prompts_load_from_any_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    package_dir = Path(prompts.__file__).parent
    for name in PROMPT_NAMES:
        text = prompts.load_prompt(name)
        assert text.strip(), name
        assert text == (package_dir / name).read_text(encoding="utf-8")


def test_only_the_five_role_prompts_ship():
    package_dir = Path(prompts.__file__).parent
    assert sorted(p.name for p in package_dir.glob("*.txt")) == PROMPT_NAMES
    assert not (PROJECT_ROOT / "prompts").exists()
