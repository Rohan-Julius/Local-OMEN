"""Phase 4 acceptance, pinned as an ongoing regression gate over the TUNING set
(fixtures/calibration.yaml; the held-out set is for `omen eval`, never for
gates that guide tuning): all 4 true
positives survive retrieval and at most 2 of the 4 hard negatives do, at
the currently configured SIMILARITY_THRESHOLD. PLAN.md: this gates every
prompt/config change — a change that lifts recall while wrecking
precision must fail here, not on stage.

Runs against a temporary ledger seeded from the real `incidents.yaml` and
indexed with the real embedding model (conftest.py `seeded_store`), so the
result depends only on files in the repo, never on the state of the local
`omen_store/` (audit B20).
"""

import pytest

from omen import config, fixtures as fixtures_mod, librarian, vectors


@pytest.fixture(scope="module")
def calibration_results(seeded_store):
    conn = seeded_store

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


@pytest.mark.live
def test_fixture_set_has_the_expected_shape(calibration_results):
    labels = [case.label for case, _ in calibration_results]
    assert labels.count("true_match") == 4
    assert labels.count("hard_negative") == 4
    assert labels.count("unrelated") == 4


@pytest.mark.live
def test_all_true_positives_survive_retrieval(calibration_results):
    true_matches = [(c, cands) for c, cands in calibration_results if c.label == "true_match"]
    survived = [c.id for c, cands in true_matches if _survives(cands, config.SIMILARITY_THRESHOLD, c.target_ref)]
    assert len(survived) == 4, f"only {survived} survived — PLAN.md: fix incidents.yaml surface forms, not the threshold"


@pytest.mark.live
def test_at_most_two_hard_negatives_survive_retrieval(calibration_results):
    hard_negatives = [(c, cands) for c, cands in calibration_results if c.label == "hard_negative"]
    survived = [c.id for c, cands in hard_negatives if _survives(cands, config.SIMILARITY_THRESHOLD)]
    assert len(survived) <= 2, f"{survived} all survived — retrieval is over-admitting, tighten incidents.yaml"
