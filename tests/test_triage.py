"""Phase 6 acceptance: run the fixture set through Triage and record
precision/recall. This doubles as the full-pipeline fixture gate PLAN.md's
Verification section calls for: "≥3/4 true positives caught, ≤1/4 hard
negatives flagged. Gates every prompt change — a prompt edit that lifts
recall while wrecking precision must fail here, not on stage."

Live Ollama required (skips gracefully without it) — this is the one test
file in the suite that's genuinely slow (12 fixtures x one Triage call
each), because there is no meaningful way to test prompt quality without
actually running the model.
"""

import pytest

from omen import agents, config, fixtures as fixtures_mod, librarian, runners, store, vectors
from omen.contracts import TriageVerdict


def _format_prompt(chunk, candidates, conn) -> str:
    lines = [
        f"CODE CHUNK ({chunk.file_path}:{chunk.symbol}, lines {chunk.start_line}-{chunk.end_line}):",
        chunk.content,
        "",
        "CANDIDATE INCIDENTS:",
    ]
    for cand in candidates:
        incident = store.get_incident(conn, cand.incident_ref)
        if incident is None:
            continue
        lines.append(f"{incident.ref}: {incident.title}")
        lines.append(f"  failure_mechanism: {incident.failure_mechanism}")
        lines.append(f"  the_rule: {incident.the_rule}")
    return "\n".join(lines)


@pytest.fixture(scope="module")
def triage_results(seeded_store):
    # A temporary ledger seeded from incidents.yaml (conftest.py), not the
    # local omen_store/ (audit B20).
    conn = seeded_store

    cases = fixtures_mod.load_fixtures()
    chunks = [fixtures_mod.fixture_to_chunk(c) for c in cases]
    embeddings, _ = librarian.embed_chunks(conn, chunks)

    instruction = agents.load_prompt("triage.txt")
    runner = runners.ADKRoleRunner()

    import asyncio

    async def run_all():
        results = []
        for case, chunk in zip(cases, chunks):
            hits = vectors.query_chunk(embeddings[chunk.content_hash], n_results=librarian.QUERY_N_RESULTS)
            candidates = librarian.collapse_to_incidents(hits)
            gated_candidates = [c for c in candidates if c.similarity >= config.SIMILARITY_THRESHOLD]
            if not gated_candidates:
                # Never reaches Triage in the real pipeline — retrieval
                # already rejected it, which for a hard_negative/unrelated
                # fixture is a win, and for a true_match is a miss.
                results.append((case, None))
                continue
            prompt = _format_prompt(chunk, gated_candidates, conn)
            verdict = await runner.run_structured(instruction, prompt, TriageVerdict, think=config.THINK_DEFAULT)
            results.append((case, verdict))
        return results

    return asyncio.run(run_all())


@pytest.mark.live
@pytest.mark.slow
def test_triage_precision_recall(triage_results):
    true_matches = [(c, v) for c, v in triage_results if c.label == "true_match"]
    hard_negatives = [(c, v) for c, v in triage_results if c.label == "hard_negative"]
    unrelated = [(c, v) for c, v in triage_results if c.label == "unrelated"]

    caught = [c.id for c, v in true_matches if v is not None and v.verdict == "MATCH"]
    hn_flagged = [c.id for c, v in hard_negatives if v is not None and v.verdict == "MATCH"]
    unrelated_flagged = [c.id for c, v in unrelated if v is not None and v.verdict == "MATCH"]

    print(f"\nTriage results: {len(caught)}/4 true positives caught: {caught}")
    print(f"Triage results: {len(hn_flagged)}/4 hard negatives flagged: {hn_flagged}")
    print(f"Triage results: {len(unrelated_flagged)}/4 unrelated flagged: {unrelated_flagged}")

    # PLAN.md Verification: "≥3/4 true positives caught, ≤1/4 hard
    # negatives flagged."
    assert len(caught) >= 3, f"only {caught} caught — below the ≥3/4 recall bar"
    assert len(hn_flagged) <= 1, f"{hn_flagged} flagged — above the ≤1/4 hard-negative precision bar"


@pytest.mark.live
@pytest.mark.slow
def test_triage_never_flags_unrelated(triage_results):
    """Not a formal PLAN.md bar, but a chunk with zero relation to any
    incident mechanism flagging MATCH would be a much worse failure than
    a hard negative doing so — worth catching explicitly."""
    unrelated = [(c, v) for c, v in triage_results if c.label == "unrelated"]
    flagged = [c.id for c, v in unrelated if v is not None and v.verdict == "MATCH"]
    assert flagged == [], f"unrelated fixtures flagged as MATCH: {flagged}"
