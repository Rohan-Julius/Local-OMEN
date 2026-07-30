"""Phase 4 acceptance, pinned as an ongoing regression gate: all 4 true
positives survive retrieval and at most 2 of the 4 hard negatives do, at
the currently configured SIMILARITY_THRESHOLD. PLAN.md: this gates every
prompt/config change — a change that lifts recall while wrecking
precision must fail here, not on stage.

Runs against the real project ledger (`omen seed` + `omen reindex` must
have already been run — a precondition, not something this test owns,
since the point is to calibrate against what's actually indexed).
"""
import urllib.request

import pytest

from omen import config, fixtures as fixtures_mod, librarian, store, vectors


def _ollama_reachable() -> bool:
    try:
        urllib.request.urlopen(config.OLLAMA_HOST, timeout=1)
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _ollama_reachable(), reason="Ollama is not reachable on OLLAMA_HOST")


@pytest.fixture(scope="module")
def calibration_results():
    conn = store.connect()
    if vectors.count() == 0:
        pytest.skip("Chroma index is empty — run `omen seed incidents.yaml` and `omen reindex` first")

    cases = fixtures_mod.load_fixtures()
    chunks = [fixtures_mod.fixture_to_chunk(c) for c in cases]
    embeddings, _ = librarian.embed_chunks(conn, chunks)

    results = []
    for case, chunk in zip(cases, chunks):
        hits = vectors.query_chunk(embeddings[chunk.content_hash], n_results=librarian.QUERY_N_RESULTS)
        candidates = librarian.collapse_to_incidents(hits)
        results.append((case, candidates))
    return results


def _survives(candidates, threshold, target_ref=None):
    if target_ref:
        return any(c.incident_ref == target_ref and c.similarity >= threshold for c in candidates)
    return any(c.similarity >= threshold for c in candidates)


def test_fixture_set_has_the_expected_shape(calibration_results):
    labels = [case.label for case, _ in calibration_results]
    assert labels.count("true_match") == 4
    assert labels.count("hard_negative") == 4
    assert labels.count("unrelated") == 4


def test_all_true_positives_survive_retrieval(calibration_results):
    true_matches = [(c, cands) for c, cands in calibration_results if c.label == "true_match"]
    survived = [c.id for c, cands in true_matches if _survives(cands, config.SIMILARITY_THRESHOLD, c.target_ref)]
    assert len(survived) == 4, f"only {survived} survived — PLAN.md: fix incidents.yaml surface forms, not the threshold"


def test_at_most_two_hard_negatives_survive_retrieval(calibration_results):
    hard_negatives = [(c, cands) for c, cands in calibration_results if c.label == "hard_negative"]
    survived = [c.id for c, cands in hard_negatives if _survives(cands, config.SIMILARITY_THRESHOLD)]
    assert len(survived) <= 2, f"{survived} all survived — retrieval is over-admitting, tighten incidents.yaml"
