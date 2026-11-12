"""Phase 4 (production plan): `omen doctor` and the memory show / history /
revert commands. No model: Ollama is a fake probe, embeddings a fake
function; `diagnose` is also tested against a plain dict standing in for
Chroma.
"""
from __future__ import annotations

import pytest

from omen import cli, config, doctor, store, vectors
from omen.contracts import IncidentSeed
from tests.test_memory_integrity import CURATED_FORMS, fake_embed, seed_incident, write

BOTH_MODELS = [f"{config.LLM_MODEL}", f"{config.EMBED_MODEL}:latest", "qwen3:4b"]


@pytest.fixture
def conn(tmp_path):
    c = store.connect(tmp_path / "t.db")
    seed_incident(c, "OMEN-001")
    seed_incident(c, "OMEN-002", title="Retry storm", failure_mechanism="Retries without backoff.", forms=["a retry loop", "no sleep"])
    return c


def expected_index(conn):
    return doctor._expected_variants(store.list_incidents(conn))


# ---------------------------------------------------------------------------
# diagnose (pure)
# ---------------------------------------------------------------------------


def test_clean_store_is_ok(conn):
    report = doctor.diagnose(conn, expected_index(conn), BOTH_MODELS)
    assert report.ok and report.problems == []
    assert (report.n_incidents, report.n_variants, report.n_indexed) == (2, 8, 8)
    assert report.schema_version == report.latest_schema_version == store.SCHEMA_VERSION


def test_missing_stale_and_extra_variants_are_reported_separately(conn):
    indexed = expected_index(conn)
    del indexed["OMEN-001::surface:2"]  # missing
    indexed["OMEN-002::mechanism"] = "an old version of the mechanism text"  # stale
    indexed["OMEN-009::mechanism"] = "an incident that was forgotten"  # extra
    indexed["OMEN-001::surface:9"] = "a form that was removed"  # extra

    report = doctor.diagnose(conn, indexed, BOTH_MODELS)

    assert report.missing == ["OMEN-001::surface:2"]
    assert report.stale == ["OMEN-002::mechanism"]
    assert report.extra == ["OMEN-001::surface:9", "OMEN-009::mechanism"]
    assert report.drift and not report.ok
    assert any("index drift: 1 missing, 1 stale, 2 extra" in p for p in report.problems)


def test_an_empty_index_over_a_populated_ledger_is_all_missing(conn):
    report = doctor.diagnose(conn, {}, BOTH_MODELS)
    assert len(report.missing) == 8 and not report.stale and not report.extra


def test_ollama_unreachable_is_a_problem(conn):
    report = doctor.diagnose(conn, expected_index(conn), None, "not reachable at http://x:1 (URLError)")
    assert not report.ok and report.problems == ["Ollama: not reachable at http://x:1 (URLError)"]


def test_each_missing_model_is_named(conn):
    report = doctor.diagnose(conn, expected_index(conn), [config.LLM_MODEL])
    assert report.models_missing == [config.EMBED_MODEL]
    assert report.problems == [f"model not pulled: {config.EMBED_MODEL} (run `ollama pull {config.EMBED_MODEL}`)"]
    both = doctor.diagnose(conn, expected_index(conn), [])
    assert both.models_missing == [config.LLM_MODEL, config.EMBED_MODEL]


@pytest.mark.parametrize("wanted,available,present", [
    ("embeddinggemma", ["embeddinggemma:latest"], True),
    ("embeddinggemma", ["embeddinggemma:v2"], False),
    ("gemma4:12b-it-q4_K_M", ["gemma4:12b-it-q4_K_M"], True),
    ("gemma4:12b-it-q4_K_M", ["gemma4:12b-it-q4_K_M:latest"], False),
])
def test_model_name_matching(wanted, available, present):
    assert doctor._has_model(wanted, available) is present


def test_an_old_schema_version_is_a_problem(conn):
    conn.execute("UPDATE meta SET value = '2' WHERE key = 'schema_version'")
    report = doctor.diagnose(conn, expected_index(conn), BOTH_MODELS)
    assert report.problems == [f"schema version 2, expected {store.SCHEMA_VERSION}"]


def test_probe_ollama_reads_the_tags_endpoint(monkeypatch):
    import io
    import json

    seen = []

    def fake_urlopen(url, timeout=None):
        seen.append(url)
        return io.BytesIO(json.dumps({"models": [{"name": "a:1"}, {"name": "b:2"}]}).encode())

    monkeypatch.setattr(doctor.urllib.request, "urlopen", fake_urlopen)
    assert doctor.probe_ollama("http://ollama.test:5555") == (["a:1", "b:2"], None)
    assert seen == ["http://ollama.test:5555/api/tags"]


def test_probe_ollama_failure_is_a_reason_not_an_exception(monkeypatch):
    def refuse(url, timeout=None):
        raise ConnectionRefusedError("refused")

    monkeypatch.setattr(doctor.urllib.request, "urlopen", refuse)
    models, error = doctor.probe_ollama("http://localhost:1")
    assert models is None and "not reachable at http://localhost:1" in error


# ---------------------------------------------------------------------------
# the commands, through cli.main with real (temporary) SQLite and Chroma
# ---------------------------------------------------------------------------


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    for name in ("STORE_DIR", "DB_PATH", "CHROMA_PATH"):
        monkeypatch.setattr(config, name, getattr(config, name))
    store_dir = tmp_path / "store"
    # populate() runs before cli.main applies --store, so point the index
    # there explicitly (cli.main's --store sets the same directory).
    monkeypatch.setattr(vectors, "CHROMA_PATH", store_dir / "chroma")
    monkeypatch.setattr(vectors, "_embed", fake_embed)
    monkeypatch.setattr(doctor, "probe_ollama", lambda *a, **k: (BOTH_MODELS, None))

    async def run(*argv):
        return await cli.main([*argv, "--store", str(store_dir)])

    return run, store_dir


def populate(store_dir):
    conn = store.connect(store_dir / "omen.db")
    seed_incident(conn, "OMEN-001")
    vectors.reindex(store.list_incidents(conn))
    return conn


@pytest.mark.asyncio
async def test_doctor_clean_exit_zero(cli_env, capsys):
    run, store_dir = cli_env
    populate(store_dir)
    assert await run("doctor") == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "ledger:  1 incident(s), 5 variant(s)" in out and "status:  OK" in out


@pytest.mark.asyncio
async def test_doctor_reports_drift_exit_one_and_fix_repairs_it(cli_env, capsys):
    run, store_dir = cli_env
    populate(store_dir)
    vectors.get_collection().delete(ids=["OMEN-001::surface:1"])  # drift: a variant vanished
    vectors.get_collection().upsert(ids=["OMEN-009::mechanism"], embeddings=[[0.1] * 8], documents=["orphan"])

    assert await run("doctor") == cli.EXIT_USER_ERROR
    out = capsys.readouterr().out
    assert "missing: OMEN-001::surface:1" in out and "extra: OMEN-009::mechanism" in out and "PROBLEMS" in out

    assert await run("doctor", "--fix") == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "rebuilding the index" in out and "status:  OK" in out
    assert await run("doctor") == cli.EXIT_OK  # and it stays fixed


@pytest.mark.asyncio
async def test_doctor_fix_does_nothing_when_clean(cli_env, capsys, monkeypatch):
    run, store_dir = cli_env
    populate(store_dir)
    monkeypatch.setattr(vectors, "reindex", lambda incidents: pytest.fail("reindexed a clean store"))
    assert await run("doctor", "--fix") == cli.EXIT_OK


@pytest.mark.asyncio
async def test_doctor_ollama_down_exit_one(cli_env, monkeypatch, capsys):
    run, store_dir = cli_env
    populate(store_dir)
    monkeypatch.setattr(doctor, "probe_ollama", lambda *a, **k: (None, "not reachable at http://localhost:11434 (URLError)"))
    assert await run("doctor") == cli.EXIT_USER_ERROR
    assert "ollama:  not reachable" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_doctor_does_not_create_a_missing_store(cli_env, capsys):
    run, store_dir = cli_env
    assert await run("doctor") == cli.EXIT_USER_ERROR
    assert "no ledger at" in capsys.readouterr().out
    assert not store_dir.exists()


# ---------------------------------------------------------------------------
# memory show / history / revert
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_show_prints_the_whole_record(cli_env, capsys):
    run, store_dir = cli_env
    conn = populate(store_dir)
    write(conn, existing_ref="OMEN-001")
    capsys.readouterr()

    assert await run("memory", "show", "OMEN-001") == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "OMEN-001" in out and "Stale permission cache" in out and "2025-03-02" in out
    assert f"surface forms ({len(CURATED_FORMS) + 1})" in out
    assert "sources (2, original first)" in out and "git:abc123" in out


@pytest.mark.asyncio
async def test_memory_show_unknown_and_forgotten(cli_env, capsys):
    run, store_dir = cli_env
    conn = populate(store_dir)
    assert await run("memory", "show", "OMEN-042") == cli.EXIT_USER_ERROR
    store.forget_incident(conn, "OMEN-001")
    capsys.readouterr()
    assert await run("memory", "show", "OMEN-001") == cli.EXIT_USER_ERROR
    assert "memory revert OMEN-001" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_memory_history_lists_versions(cli_env, capsys):
    run, store_dir = cli_env
    conn = populate(store_dir)
    write(conn, existing_ref="OMEN-001")
    capsys.readouterr()

    assert await run("memory", "history", "OMEN-001") == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "OMEN-001 (present)" in out and "create" in out and "update" in out and "[+ unapplied proposal]" in out
    assert await run("memory", "history", "OMEN-042") == cli.EXIT_USER_ERROR


@pytest.mark.asyncio
async def test_memory_forget_then_revert_round_trip_through_the_cli(cli_env, capsys):
    run, store_dir = cli_env
    populate(store_dir)

    assert await run("memory", "forget", "OMEN-001") == cli.EXIT_OK
    assert vectors.count() == 0
    assert "memory revert OMEN-001" in capsys.readouterr().out

    assert await run("memory", "revert", "OMEN-001") == cli.EXIT_OK
    assert "Restored OMEN-001 from version 2 (5 variant(s) re-indexed)" in capsys.readouterr().out
    assert vectors.count() == 5
    assert await run("doctor") == cli.EXIT_OK  # ledger and index agree again


@pytest.mark.asyncio
async def test_memory_revert_to_a_version_and_errors(cli_env, capsys):
    run, store_dir = cli_env
    conn = populate(store_dir)
    write(conn, existing_ref="OMEN-001")
    capsys.readouterr()

    assert await run("memory", "revert", "OMEN-001", "--to", "1") == cli.EXIT_OK
    assert vectors.count() == 5  # the merged-in form's variant is gone from the index
    assert await run("memory", "revert", "OMEN-001", "--to", "9") == cli.EXIT_USER_ERROR
    assert await run("memory", "revert", "OMEN-042") == cli.EXIT_USER_ERROR


@pytest.mark.asyncio
async def test_revert_with_ollama_down_keeps_the_ledger_and_points_at_doctor(cli_env, monkeypatch, capsys):
    run, store_dir = cli_env
    conn = populate(store_dir)
    store.forget_incident(conn, "OMEN-001")
    vectors.delete_incident_variants("OMEN-001")

    def down(prompts):
        raise ConnectionError("Ollama went away")

    monkeypatch.setattr(vectors, "_embed", down)
    capsys.readouterr()
    assert await run("memory", "revert", "OMEN-001") == cli.EXIT_RUNTIME_ERROR
    captured = capsys.readouterr()
    assert "omen doctor --fix" in captured.out
    # the raw ConnectionError becomes the one-line "Ollama is not reachable" message
    assert "Ollama is not reachable at" in captured.err and "ollama serve" in captured.err
    assert store.get_incident(store.connect(store_dir / "omen.db"), "OMEN-001") is not None


def test_seed_incident_helper_is_importable_for_other_tests():
    assert IncidentSeed  # keeps the shared helpers' imports honest
