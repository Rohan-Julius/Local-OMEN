"""Phase 5 (production plan): `scan --max-gated` / `--max-positives`, the
warm-up's call conditions at the CLI, and batch retrieval. Scripted fake
runner and fake embeddings: no model, no Ollama.
"""
from __future__ import annotations

import hashlib
import re

import pytest

from omen import cli, librarian, profiles, runners, store, vectors, warmup
from tests.test_scan_audit import ScriptedRunner, candidate_refs, scan_env, verdict  # noqa: F401  (scan_env is a fixture)


def graded_embed(prompts):
    """Incident texts embed to [1,0,0]; a chunk `def funcK()` to [1, 0.2*K, 0],
    so cosine similarity to every incident falls as K grows (K=0 -> 1.0,
    K=9 -> 0.49), all above the 0.35 gate."""
    out = []
    for text in prompts:
        m = re.search(r"def func(\d+)\(", text)
        out.append([1.0, 0.2 * int(m.group(1)), 0.0] if m else [1.0, 0.0, 0.0])
    return out


def write_chunks(repo, ks):
    """One file per chunk, written so file order is the REVERSE of similarity order."""
    (repo / "permissions.py").unlink(missing_ok=True)
    for k in ks:
        (repo / f"mod{len(ks) - k:02d}.py").write_text(f"def func{k}():\n    return {k}\n")


def investigate(tools, budget):
    tools["get_incident"](incident_ref="OMEN-001")
    return "looked"


def reject(prompt):
    return verdict(verdict="rejected", evidence=[], ref=candidate_refs(prompt)[0])


class Recording(ScriptedRunner):
    """ScriptedRunner that remembers which chunk Triage and the Investigator saw."""

    def __init__(self):
        super().__init__(investigate, reject)

    def seen(self, role):
        return [int(re.search(r"def func(\d+)\(", p).group(1)) for r, _i, p in self.calls if r == role]


@pytest.fixture
def many(scan_env, monkeypatch):  # noqa: F811
    repo, conn, run = scan_env
    monkeypatch.setattr(vectors, "_embed", graded_embed)
    return repo, conn, run


# ---------------------------------------------------------------------------
# caps
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gated_chunks_are_triaged_in_descending_similarity_and_the_rest_are_reported(many, capsys):
    repo, conn, run = many
    write_chunks(repo, range(5))
    runner = Recording()

    await run(runner, max_gated=3, max_positives=3)

    assert runner.seen("triage") == [0, 1, 2]  # most similar first, though file order is 4,3,2,1,0
    out = capsys.readouterr().out
    assert "skipped 2 chunk(s) because of caps" in out and "not triaged: 2" in out
    assert "func3" in out and "func4" in out and "--max-gated 3" in out
    rows = conn.execute("SELECT * FROM skipped_chunks ORDER BY similarity DESC").fetchall()
    assert [(r["symbol"], r["stage"], r["triage_verdict"]) for r in rows] == [("func3", "triage", None), ("func4", "triage", None)]
    assert all(r["reason"] == "not triaged: --max-gated 3 reached" and r["incident_ref"] and 0.35 < r["similarity"] < 1 for r in rows)
    run_row = conn.execute("SELECT n_gated, n_triaged, n_skipped FROM runs").fetchone()
    assert tuple(run_row) == (5, 3, 2)  # 5 cleared the gate, 3 went to Triage, 2 skipped


@pytest.mark.asyncio
async def test_positives_beyond_the_cap_stay_triage_positive_and_are_stored(many, capsys):
    repo, conn, run = many
    write_chunks(repo, range(4))
    runner = Recording()

    await run(runner, max_gated=4, max_positives=1)  # Triage says MATCH for every chunk

    assert runner.seen("triage") == [0, 1, 2, 3]  # all triaged
    assert runner.seen("investigator") == [0]  # only the most similar positive is investigated
    out = capsys.readouterr().out
    assert "Triage-positive, not investigated: 3" in out and "investigation skipped: cap (--max-positives 1)" in out
    rows = conn.execute("SELECT * FROM skipped_chunks ORDER BY similarity DESC").fetchall()
    assert [(r["symbol"], r["stage"], r["triage_verdict"], r["reason"]) for r in rows] == [
        (f"func{k}", "investigation", "MATCH", "investigation skipped: cap (--max-positives 1)") for k in (1, 2, 3)
    ]
    assert conn.execute("SELECT n_triaged, n_investigated, n_skipped FROM runs").fetchone()[:] == (4, 1, 3)
    assert conn.execute("SELECT COUNT(*) FROM findings").fetchone()[0] == 1  # one finding; the others are in skipped_chunks


@pytest.mark.asyncio
async def test_both_caps_together_and_nothing_is_lost(many):
    repo, conn, run = many
    write_chunks(repo, range(6))
    runner = Recording()

    await run(runner, max_gated=4, max_positives=2)

    stages = [r["stage"] for r in conn.execute("SELECT stage FROM skipped_chunks")]
    assert sorted(stages) == ["investigation", "investigation", "triage", "triage"]
    seen = set(runner.seen("triage"))
    investigated = set(runner.seen("investigator"))
    skipped = {r["symbol"] for r in conn.execute("SELECT symbol FROM skipped_chunks")}
    # every gated chunk is exactly one of: investigated, or in skipped_chunks
    assert len(investigated) + len(skipped) == 6 and not investigated & {int(s[4:]) for s in skipped}
    assert seen == {0, 1, 2, 3}


@pytest.mark.asyncio
async def test_default_caps_are_eight_and_two(many):
    repo, conn, run = many
    write_chunks(repo, range(10))
    runner = Recording()

    await run(runner, max_gated=8, max_positives=2)  # the CLI defaults

    assert len(runner.seen("triage")) == 8 and len(runner.seen("investigator")) == 2
    assert conn.execute("SELECT COUNT(*) FROM skipped_chunks").fetchone()[0] == 2 + 6
    ns = cli.build_parser().parse_args(["scan", "."])
    assert (ns.max_gated, ns.max_positives) == (8, 2)  # the real CLI defaults


@pytest.mark.asyncio
async def test_caps_not_reached_skip_nothing(many, capsys):
    repo, conn, run = many
    write_chunks(repo, range(3))
    await run(Recording(), max_gated=8, max_positives=8)
    assert conn.execute("SELECT COUNT(*) FROM skipped_chunks").fetchone()[0] == 0
    assert conn.execute("SELECT n_skipped FROM runs").fetchone()[0] == 0
    assert "skipped" not in capsys.readouterr().out.split("scan complete")[-1]


@pytest.mark.asyncio
async def test_max_positives_zero_triages_everything_and_investigates_nothing(many):
    repo, conn, run = many
    write_chunks(repo, range(3))
    runner = Recording()
    await run(runner, max_gated=8, max_positives=0)
    assert runner.seen("investigator") == [] and len(runner.seen("triage")) == 3
    assert conn.execute("SELECT COUNT(*) FROM skipped_chunks WHERE stage = 'investigation'").fetchone()[0] == 3


@pytest.mark.asyncio
async def test_negative_caps_are_a_user_error(many, capsys):
    repo, conn, run = many
    args_ns = dict(max_gated=-1, max_positives=2)
    import argparse

    ns = argparse.Namespace(path=str(repo), since=None, all=True, dry_run=False, retrieval_only=False, runner="adk",
                            no_warmup=True, profile="gpu", **args_ns)
    assert await cli.cmd_scan(ns) == cli.EXIT_USER_ERROR
    assert "must be 0 or more" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# warm-up: when it runs, and when it must not
# ---------------------------------------------------------------------------


@pytest.fixture
def warm_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(warmup, "warm_up", lambda *a, **k: calls.append("warm") or 0.1)
    return calls


async def scan_via_main(repo, *flags):
    return await cli.main(["scan", str(repo), "--all", "--profile", "gpu", *flags])


@pytest.mark.asyncio
async def test_warmup_runs_once_before_the_first_llm_call(scan_env, monkeypatch, warm_calls):  # noqa: F811
    repo, conn, run = scan_env
    order = []
    runner = Recording()
    original = runner.run_structured

    async def spy(*a, **k):
        order.append("llm")
        return await original(*a, **k)

    runner.run_structured = spy
    monkeypatch.setattr(warmup, "warm_up", lambda *a, **k: order.append("warm") or 0.1)
    monkeypatch.setattr(runners, "ADKRoleRunner", lambda: runner)

    assert await scan_via_main(repo) == cli.EXIT_OK

    assert order[0] == "warm" and order.count("warm") == 1 and "llm" in order


@pytest.mark.asyncio
@pytest.mark.parametrize("flags", [["--dry-run"], ["--retrieval-only"], ["--no-warmup"]], ids=["dry-run", "retrieval-only", "no-warmup"])
async def test_warmup_is_skipped_for_dry_run_retrieval_only_and_no_warmup(scan_env, monkeypatch, warm_calls, flags):  # noqa: F811
    repo, conn, run = scan_env
    monkeypatch.setattr(runners, "ADKRoleRunner", lambda: Recording())
    assert await scan_via_main(repo, *flags) == cli.EXIT_OK
    assert warm_calls == []


@pytest.mark.asyncio
async def test_warmup_is_skipped_when_nothing_cleared_retrieval(scan_env, monkeypatch, warm_calls):  # noqa: F811
    repo, conn, run = scan_env
    monkeypatch.setattr(librarian, "SIMILARITY_THRESHOLD", 1.1)  # nothing can clear it
    monkeypatch.setattr(runners, "ADKRoleRunner", lambda: Recording())
    monkeypatch.setattr(librarian.run, "__defaults__", (1.1,))
    assert await scan_via_main(repo) == cli.EXIT_OK
    assert warm_calls == []


@pytest.mark.asyncio
async def test_commands_without_an_llm_never_warm_up(tmp_path, warm_calls):
    for argv in (["memory", "list"], ["reindex"], ["doctor"]):
        await cli.main([*argv, "--store", str(tmp_path / "s")])
    assert warm_calls == []


@pytest.mark.asyncio
async def test_learn_warms_up_unless_dry_run(tmp_path, monkeypatch, warm_calls):
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "T"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True)
    (repo / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fix: a thing"], check=True)

    class Idle:
        async def run_tooled(self, *a, **k):
            from omen.contracts import Transcript

            return Transcript()

    monkeypatch.setattr(runners, "ADKRoleRunner", lambda: Idle())
    base = ["learn", "--from-git", "HEAD", "--repo", str(repo), "--profile", "gpu", "--store", str(tmp_path / "s")]
    await cli.main([*base, "--dry-run"])
    assert warm_calls == []
    await cli.main(base)
    assert warm_calls == ["warm"]
    await cli.main([*base, "--no-warmup"])
    assert warm_calls == ["warm"]  # unchanged


@pytest.mark.asyncio
async def test_the_chosen_profile_is_logged_at_verbose_and_becomes_active(scan_env, monkeypatch, warm_calls, capsys):  # noqa: F811
    repo, conn, run = scan_env
    monkeypatch.setattr(runners, "ADKRoleRunner", lambda: Recording())
    seen = {}
    original = Recording.run_tooled

    async def spy(self, *a, **k):
        seen["active"] = profiles.active().name
        return await original(self, *a, **k)

    monkeypatch.setattr(Recording, "run_tooled", spy)

    assert await cli.main(["scan", str(repo), "--all", "--profile", "cpu", "--verbose"]) == cli.EXIT_OK

    assert "profile cpu: investigator think=False" in capsys.readouterr().err
    assert seen["active"] == "cpu"


# ---------------------------------------------------------------------------
# batch retrieval
# ---------------------------------------------------------------------------


class FakeCollection:
    """Stands in for a Chroma collection. Hits depend only on the embedding."""

    def __init__(self):
        self.query_calls = []

    def query(self, query_embeddings, n_results):
        self.query_calls.append(len(query_embeddings))
        ids, dists, metas, docs = [], [], [], []
        for emb in query_embeddings:
            seed = int(hashlib.sha256(repr([round(x, 4) for x in emb]).encode()).hexdigest(), 16)  # float32 cache round-trip safe
            n = 3
            ids.append([f"OMEN-00{1 + (seed + i) % 4}::surface:{i}" for i in range(n)])
            dists.append([((seed >> (3 * i)) % 60) / 100 for i in range(n)])
            metas.append([{"incident_ref": f"OMEN-00{1 + (seed + i) % 4}", "variant": f"surface:{i}"} for i in range(n)])
            docs.append([f"doc {i}" for i in range(n)])
        return {"ids": ids, "distances": dists, "metadatas": metas, "documents": docs}


@pytest.fixture
def fake_collection(monkeypatch):
    col = FakeCollection()
    monkeypatch.setattr(vectors, "get_collection", lambda client=None: col)
    return col


def make_chunks(n):
    from omen.contracts import Chunk

    return [
        Chunk(file_path=f"f{i}.py", start_line=1, end_line=2, symbol=f"s{i}", content=f"def func{i}(): pass", content_hash=f"h{i}")
        for i in range(n)
    ]


def test_batch_query_makes_one_call_and_matches_the_per_chunk_queries(fake_collection, tmp_path, monkeypatch):
    monkeypatch.setattr(vectors, "_embed", graded_embed)
    conn = store.connect(tmp_path / "b.db")
    chunks = make_chunks(7)

    gated, _ = librarian.run(conn, chunks, threshold=0.0)

    assert fake_collection.query_calls == [7]  # ONE query for all seven chunks

    # the same answer the old per-chunk path gives
    embeddings, _ = librarian.embed_chunks(conn, chunks)
    fake_collection.query_calls.clear()
    expected = []
    for chunk in chunks:
        hits = vectors.query_chunk(embeddings[chunk.content_hash], n_results=librarian.QUERY_N_RESULTS)
        gc = librarian.gate(chunk, librarian.collapse_to_incidents(hits), threshold=0.0)
        if gc:
            expected.append(gc)
    assert fake_collection.query_calls == [1] * 7  # (the old way: seven calls)
    assert [g.model_dump() for g in gated] == [g.model_dump() for g in expected]


def test_query_chunks_results_equal_query_chunk_per_embedding(fake_collection):
    embeddings = [[1.0, 0.1 * i, 0.0] for i in range(5)]
    batch = vectors.query_chunks(embeddings, n_results=6)
    single = [vectors.query_chunk(e, n_results=6) for e in embeddings]
    assert batch == single
    assert all("distance" not in hit for hits in batch for hit in hits)  # callers never see raw distances


def test_query_chunks_with_nothing_makes_no_call(fake_collection):
    assert vectors.query_chunks([]) == []
    assert fake_collection.query_calls == []


def test_batch_query_against_real_chroma_equals_per_chunk(tmp_path, monkeypatch):
    """Same property on the real vector store (fake embedder)."""
    monkeypatch.setattr(vectors, "CHROMA_PATH", tmp_path / "chroma")
    monkeypatch.setattr(vectors, "_embed", graded_embed)
    from omen.contracts import IncidentSeed

    conn = store.connect(tmp_path / "r.db")
    for n in (1, 2, 3):
        store.upsert_incident(conn, IncidentSeed(ref=f"OMEN-00{n}", title="t", failure_mechanism=f"mechanism {n}",
                                                 what_happened="h", the_rule="r", surface_forms=[f"form {n}.{k}" for k in range(3)]))
    vectors.reindex(store.list_incidents(conn))
    embeddings = [[1.0, 0.3 * i, 0.1 * (i % 3)] for i in range(6)]

    assert vectors.query_chunks(embeddings, n_results=6) == [vectors.query_chunk(e, n_results=6) for e in embeddings]
