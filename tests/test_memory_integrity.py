"""Phase 4 (production plan): memory integrity. No model: embeddings are a
deterministic fake; SQLite and Chroma are real, in temporary directories.

- B8  write_incident on an existing ref MERGES, it does not overwrite.
- History/undo: create/update/forget/revert rows; forget keeps history;
  revert restores (even a forgotten incident, under the same ref).
- B26 incremental reindex: after any sequence of operations the Chroma ids,
  documents, metadata and vectors equal what a full reindex produces.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from omen import store, tools, vectors
from omen.contracts import IncidentSeed

CURATED_FORMS = [
    "functools.lru_cache wrapping a has_access check",
    "a module-level dict of user permissions that is never cleared",
    "a CDN edge cache keyed on an auth header",
    "a memoized can_edit(user, doc) decorator",
]


def fake_embed(prompts):
    out = []
    for text in prompts:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        out.append([b / 255.0 + 0.01 for b in digest[:8]])
    return out


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(vectors, "CHROMA_PATH", tmp_path / "chroma")
    monkeypatch.setattr(vectors, "_embed", fake_embed)
    return store.connect(tmp_path / "test.db"), tmp_path


def seed_incident(conn, ref="OMEN-001", forms=None, **kw) -> None:
    fields = dict(
        ref=ref, title="Stale permission cache", failure_mechanism="A permission decision is cached with no invalidation.",
        what_happened="Revoked access kept working.", the_rule="Invalidate on the permission-changing event.",
        severity="sev1", occurred_on="2025-03-02", source="postmortem:2025-03-02", learned_by="seed",
        surface_forms=list(CURATED_FORMS if forms is None else forms),
    )
    fields.update(kw)
    store.upsert_incident(conn, IncidentSeed(**fields))


def write(conn, source="git:abc123", learned_by="archivist:git", **kw):
    """The Archivist's write_incident tool."""
    args = dict(
        title="Permission cache never cleared", failure_mechanism="Authz results are memoized; revocation does not clear them.",
        what_happened="A different phrasing of what happened.", the_rule="Clear caches when permissions change.",
        surface_forms=["a Redis-backed session store holding role grants", "A MEMOIZED can_edit(user,   doc) decorator"],
        severity="sev3", existing_ref="",
    )
    args.update(kw)
    return tools._make_write_incident(conn, learned_by=learned_by, source=source)(**args)


# ---------------------------------------------------------------------------
# B8: merge on dedup
# ---------------------------------------------------------------------------


def test_b8_update_merges_instead_of_overwriting(env):
    conn, _ = env
    seed_incident(conn)

    assert write(conn, existing_ref="OMEN-001") == "OMEN-001"

    inc = store.get_incident(conn, "OMEN-001")
    # surface forms: the 4 curated ones first and untouched, then only the genuinely new one
    assert inc.surface_forms == [*CURATED_FORMS, "a Redis-backed session store holding role grants"]
    # curated text and dates are kept, not replaced by the model's rephrase
    assert inc.title == "Stale permission cache"
    assert inc.failure_mechanism == "A permission decision is cached with no invalidation."
    assert inc.what_happened == "Revoked access kept working."
    assert inc.the_rule == "Invalidate on the permission-changing event."
    assert (inc.severity, inc.occurred_on) == ("sev1", "2025-03-02")
    # original provenance kept; the new source is appended
    assert (inc.learned_by, inc.source) == ("seed", "postmortem:2025-03-02")
    assert [(s["learned_by"], s["source"]) for s in inc.sources] == [
        ("seed", "postmortem:2025-03-02"), ("archivist:git", "git:abc123"),
    ]
    assert len(store.list_incidents(conn)) == 1


def test_b8_the_models_unapplied_text_is_saved_in_history(env):
    conn, _ = env
    seed_incident(conn)
    write(conn, existing_ref="OMEN-001")

    [_, update] = store.list_history(conn, "OMEN-001")
    assert (update["version"], update["action"], update["source"]) == (2, "update", "git:abc123")
    proposal = store.get_history_proposal(conn, "OMEN-001", 2)
    assert proposal["title"] == "Permission cache never cleared"
    assert proposal["failure_mechanism"].startswith("Authz results are memoized")
    assert proposal["what_happened"] == "A different phrasing of what happened."
    assert proposal["the_rule"] == "Clear caches when permissions change."
    assert proposal["severity"] == "sev3"
    assert "A MEMOIZED can_edit(user,   doc) decorator" in proposal["surface_forms"]


def test_b8_blank_severity_and_date_are_filled_but_set_ones_are_not(env):
    conn, _ = env
    seed_incident(conn, severity=None, occurred_on=None)
    write(conn, existing_ref="OMEN-001", severity="sev2")
    inc = store.get_incident(conn, "OMEN-001")
    assert (inc.severity, inc.occurred_on) == ("sev2", None)  # filled; the model never supplies a date

    write(conn, existing_ref="OMEN-001", severity="sev3", source="git:def456")
    assert store.get_incident(conn, "OMEN-001").severity == "sev2"  # now set, so kept


def test_merge_surface_forms_dedups_on_whitespace_and_case():
    merged = store.merge_surface_forms(
        ["Alpha  beta", "gamma"], ["alpha BETA", " GAMMA ", "delta", "delta", "", "  "]
    )
    assert merged == ["Alpha  beta", "gamma", "delta"]


def test_merge_surface_forms_caps_at_twelve_originals_first():
    originals = [f"original form {i}" for i in range(10)]
    merged = store.merge_surface_forms(originals, [f"new form {i}" for i in range(5)])
    assert len(merged) == 12
    assert merged[:10] == originals and merged[10:] == ["new form 0", "new form 1"]


def test_merge_never_drops_originals_even_over_the_cap():
    originals = [f"original form {i}" for i in range(14)]
    assert store.merge_surface_forms(originals, ["new"]) == originals


def test_merging_the_same_source_twice_adds_no_duplicate_source_or_form(env):
    conn, _ = env
    seed_incident(conn)
    write(conn, existing_ref="OMEN-001")
    write(conn, existing_ref="OMEN-001")  # same source, same forms
    inc = store.get_incident(conn, "OMEN-001")
    assert len(inc.sources) == 2 and len(inc.surface_forms) == 5


def test_merge_into_an_unknown_ref_is_an_error(env):
    conn, _ = env
    with pytest.raises(LookupError):
        store.merge_incident(conn, "OMEN-042", IncidentSeed(ref="OMEN-042", title="t", failure_mechanism="m", what_happened="h", the_rule="r"))


def test_seed_replace_still_replaces_but_keeps_provenance(env):
    """`omen seed` is the curated path: editing incidents.yaml and re-seeding
    replaces the text. Earlier provenance stays in `sources`."""
    conn, _ = env
    seed_incident(conn)
    seed_incident(conn, title="Edited title", forms=["only form"], source="postmortem:edited")
    inc = store.get_incident(conn, "OMEN-001")
    assert (inc.title, inc.surface_forms) == ("Edited title", ["only form"])
    assert [s["source"] for s in inc.sources] == ["postmortem:2025-03-02", "postmortem:edited"]


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


def test_every_create_update_forget_writes_a_history_row(env):
    conn, _ = env
    seed_incident(conn)
    write(conn, existing_ref="OMEN-001")
    store.forget_incident(conn, "OMEN-001")

    rows = store.list_history(conn, "OMEN-001")
    assert [(r["version"], r["action"]) for r in rows] == [(1, "create"), (2, "update"), (3, "forget")]
    assert store.get_history_snapshot(conn, "OMEN-001", 1)["surface_forms"] == CURATED_FORMS
    assert len(store.get_history_snapshot(conn, "OMEN-001", 3)["surface_forms"]) == 5  # state when forgotten


def test_reseeding_unchanged_data_adds_no_history_row(env):
    conn, _ = env
    seed_incident(conn)
    seed_incident(conn)
    assert len(store.list_history(conn, "OMEN-001")) == 1


def test_an_incident_without_history_gets_a_baseline_before_its_first_change(env):
    """Incidents written before schema v3 have no history rows."""
    conn, _ = env
    seed_incident(conn)
    conn.execute("DELETE FROM incident_history")
    conn.commit()

    write(conn, existing_ref="OMEN-001")

    rows = store.list_history(conn, "OMEN-001")
    assert [(r["version"], r["action"]) for r in rows] == [(1, "create"), (2, "update")]
    assert store.get_history_snapshot(conn, "OMEN-001", 1)["surface_forms"] == CURATED_FORMS


def test_forget_keeps_history(env):
    conn, _ = env
    seed_incident(conn)
    assert store.forget_incident(conn, "OMEN-001") is True
    assert store.get_incident(conn, "OMEN-001") is None
    assert [r["action"] for r in store.list_history(conn, "OMEN-001")] == ["create", "forget"]
    assert store.forget_incident(conn, "OMEN-001") is False  # nothing to forget, no extra row
    assert len(store.list_history(conn, "OMEN-001")) == 2


# ---------------------------------------------------------------------------
# Revert
# ---------------------------------------------------------------------------


def test_revert_undoes_the_last_update(env):
    conn, _ = env
    seed_incident(conn)
    write(conn, existing_ref="OMEN-001")

    incident, version = store.revert_incident(conn, "OMEN-001")

    assert version == 1
    assert incident.surface_forms == CURATED_FORMS
    assert [s["learned_by"] for s in incident.sources] == ["seed"]
    assert [r["action"] for r in store.list_history(conn, "OMEN-001")] == ["create", "update", "revert"]
    assert store.list_history(conn, "OMEN-001")[-1]["source"] == "revert to v1"


def test_revert_to_a_named_version(env):
    conn, _ = env
    seed_incident(conn)
    write(conn, existing_ref="OMEN-001", source="git:one", surface_forms=["form from one"])
    write(conn, existing_ref="OMEN-001", source="git:two", surface_forms=["form from two"])

    incident, version = store.revert_incident(conn, "OMEN-001", to=2)

    assert version == 2
    assert incident.surface_forms == [*CURATED_FORMS, "form from one"]


def test_revert_of_a_forgotten_incident_restores_the_same_ref_and_never_reuses_it(env):
    """B11 + history: refs are never reused, and revert brings back the
    very same ref (and its original creation time)."""
    conn, _ = env
    seed_incident(conn)
    write(conn, existing_ref="OMEN-001")
    before = store.get_incident(conn, "OMEN-001")
    store.forget_incident(conn, "OMEN-001")

    # while it is forgotten, a new incident gets a NEW ref
    assert write(conn, title="other", surface_forms=["x"]) == "OMEN-002"

    incident, version = store.revert_incident(conn, "OMEN-001")

    assert incident.ref == "OMEN-001" and version == 3  # the state when it was forgotten
    assert incident.surface_forms == before.surface_forms and incident.created_at == before.created_at
    assert [s["source"] for s in incident.sources] == [s["source"] for s in before.sources]
    assert store.get_incident(conn, "OMEN-001") is not None
    assert write(conn, title="third", surface_forms=["y"]) == "OMEN-003"


def test_revert_errors(env):
    conn, _ = env
    with pytest.raises(LookupError):
        store.revert_incident(conn, "OMEN-001")  # no history at all
    seed_incident(conn)
    with pytest.raises(ValueError):
        store.revert_incident(conn, "OMEN-001")  # only version 1: nothing earlier
    with pytest.raises(LookupError):
        store.revert_incident(conn, "OMEN-001", to=9)


# ---------------------------------------------------------------------------
# B26: the incremental index equals a full reindex after any sequence
# ---------------------------------------------------------------------------


def chroma_state():
    got = vectors.get_collection().get(include=["documents", "embeddings", "metadatas"])
    return {
        i: (doc, [round(float(x), 5) for x in emb], meta)
        for i, doc, emb, meta in zip(got["ids"], got["documents"], got["embeddings"], got["metadatas"])
    }


def assert_index_equals_full_reindex(conn, tmp_path, monkeypatch, label):
    incremental = chroma_state()
    full_path = tmp_path / f"full-{label}"
    monkeypatch.setattr(vectors, "CHROMA_PATH", full_path)
    try:
        vectors.reindex(store.list_incidents(conn))
        full = chroma_state()
    finally:
        monkeypatch.setattr(vectors, "CHROMA_PATH", tmp_path / "chroma")
    assert set(incremental) == set(full), f"after {label}: ids differ: {set(incremental) ^ set(full)}"
    for vid in full:
        assert incremental[vid] == full[vid], f"after {label}: {vid} differs"


def test_b26_incremental_index_equals_full_reindex_after_every_step(env, monkeypatch):
    conn, tmp_path = env

    def check(label):
        assert_index_equals_full_reindex(conn, tmp_path, monkeypatch, label.replace(" ", "-"))

    # seed + full reindex, as `omen seed` then `omen reindex`
    seed_incident(conn, "OMEN-001")
    seed_incident(conn, "OMEN-002", title="Retry storm", failure_mechanism="Retries without backoff.",
                  forms=["a retry loop with no sleep", "an HTTP client that retries immediately"])
    seed_incident(conn, "OMEN-003", title="Leaked credential", failure_mechanism="A secret is logged.", forms=["print(api_key)"])
    vectors.reindex(store.list_incidents(conn))
    check("seed")

    assert write(conn, title="New one", failure_mechanism="A brand new mechanism.", surface_forms=["new form a", "new form b"]) == "OMEN-004"
    check("write new")

    write(conn, existing_ref="OMEN-001")  # merge: adds a form
    check("merge")

    write(conn, existing_ref="OMEN-002", surface_forms=["a retry loop with no sleep", "exponential backoff missing"], source="git:xyz")
    check("merge 2")

    store.forget_incident(conn, "OMEN-003")  # as `omen memory forget`
    vectors.delete_incident_variants("OMEN-003")
    check("forget")

    incident, _ = store.revert_incident(conn, "OMEN-003")  # bring it back
    vectors.reindex_incident(incident)
    check("revert forgotten")

    incident, _ = store.revert_incident(conn, "OMEN-001")  # undo the merge: one form fewer
    vectors.reindex_incident(incident)
    check("revert merge")
    assert not any(v.startswith("OMEN-001::surface:4") for v in chroma_state())  # the stale variant is gone

    write(conn, existing_ref="OMEN-004", surface_forms=["new form c"])
    check("merge new")

    store.forget_incident(conn, "OMEN-004")
    vectors.delete_incident_variants("OMEN-004")
    check("forget again")


def test_b26_write_incident_embeds_only_the_changed_incident(env, monkeypatch):
    conn, _ = env
    for n in range(1, 6):
        seed_incident(conn, f"OMEN-00{n}", forms=[f"form {n}.{k}" for k in range(4)])
    vectors.reindex(store.list_incidents(conn))  # 5 incidents x 5 variants

    embedded: list[int] = []
    monkeypatch.setattr(vectors, "_embed", lambda prompts: (embedded.append(len(prompts)), fake_embed(prompts))[1])
    write(conn, existing_ref="OMEN-003", surface_forms=["one more form"])

    assert embedded == [6]  # OMEN-003's mechanism + 5 forms, not the ledger's 25+ variants
    assert vectors.count() == 26


def test_b26_a_failed_embed_leaves_the_old_variants_in_place(env, monkeypatch):
    conn, _ = env
    seed_incident(conn)
    vectors.reindex(store.list_incidents(conn))
    before = chroma_state()

    def boom(prompts):
        raise ConnectionError("Ollama went away")

    monkeypatch.setattr(vectors, "_embed", boom)
    with pytest.raises(ConnectionError):
        write(conn, existing_ref="OMEN-001")
    assert chroma_state() == before


def test_history_snapshot_is_valid_json_of_the_full_state(env):
    conn, _ = env
    seed_incident(conn)
    raw = conn.execute("SELECT snapshot FROM incident_history WHERE ref = 'OMEN-001' AND version = 1").fetchone()[0]
    snap = json.loads(raw)
    assert {"ref", "title", "failure_mechanism", "what_happened", "the_rule", "severity", "occurred_on",
            "source", "learned_by", "created_at", "surface_forms", "sources"} <= set(snap)
