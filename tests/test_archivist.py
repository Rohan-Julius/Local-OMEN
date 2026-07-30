"""Phase 8a acceptance (PLAN.md):
- "produces an incident whose surface forms include a technology absent
  from the source document — that's the abstraction working."
- "Running it twice does not create a duplicate."

Live Ollama required (skips gracefully without it) — same as the other
agentic-role tests, there's no meaningful way to test prompt quality
without actually running the model.
"""
import urllib.request

import pytest

from omen import archivist, config, runners, store, vectors

POSTMORTEM_TEXT = """# Postmortem: wrong user's data returned under load

Service: a Python Flask app. The authentication middleware resolved the
caller's identity once per request and stashed it in a module-level global
variable, `_current_user`, so a view function several calls deeper could
read it again. Under concurrent load, one thread's request overwrote
`_current_user` before another thread's view function read it, so one
user's profile data (name, email, billing address) was returned in a
different user's response.

Root cause: per-request state was stored in a module-level global instead
of something scoped to the individual request. Under any concurrent
execution model, a module-level variable is shared by every in-flight
request, so the value read back is whichever request wrote it most
recently, not necessarily the one that's asking.

Fix: replaced the global with Flask's request-scoped `g` object.

Rule: any value specific to one in-flight request must live in a
mechanism actually scoped to that request (a request-context object, a
function argument/return value, or a request-keyed contextvar) — never a
bare module-level or global variable.
"""


def _ollama_reachable() -> bool:
    try:
        urllib.request.urlopen(config.OLLAMA_HOST, timeout=1)
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _ollama_reachable(), reason="Ollama is not reachable on OLLAMA_HOST")


@pytest.fixture(autouse=True)
def isolated_chroma(tmp_path, monkeypatch):
    """`write_incident` calls `vectors.reindex()`, which deletes and rebuilds
    the *entire* Chroma collection at `vectors.CHROMA_PATH` from whatever
    incident list it's given. Without this, an Archivist test using a tmp
    SQLite db would still reindex the real on-disk collection down to just
    its own one test incident, wiping out the 6 seeded incidents that
    test_fixtures.py/test_triage.py depend on — discovered the hard way
    when those two started failing after this file's tests ran. Redirect
    Chroma to a tmp directory so every test in this module is fully
    isolated, not just its SQLite db."""
    monkeypatch.setattr(vectors, "CHROMA_PATH", tmp_path / "chroma")


@pytest.mark.asyncio
async def test_archivist_abstracts_and_spans_technologies(tmp_path):
    (tmp_path / "postmortem.md").write_text(POSTMORTEM_TEXT, encoding="utf-8")
    conn = store.connect(tmp_path / "test.db")
    runner = runners.ADKRoleRunner()

    transcript = await archivist.learn_from_postmortem(runner, conn, tmp_path, "postmortem.md", think=False)

    ref = archivist.written_ref(transcript)
    assert ref, f"Archivist never wrote an incident: {transcript.final_text}"

    incident = store.get_incident(conn, ref)
    assert incident is not None
    assert len(incident.surface_forms) >= 3

    # The abstraction check: the postmortem only ever mentions Python/Flask.
    # At least one surface form should describe the mechanism in a
    # technology the postmortem never used.
    other_tech_markers = ["javascript", "node", "express", "java ", "servlet", "golang", " go ", "c#", ".net", "ruby", "rails"]
    joined = " ".join(incident.surface_forms).lower()
    assert any(marker in joined for marker in other_tech_markers), (
        f"no surface form spans a technology absent from the postmortem: {incident.surface_forms}"
    )

    # The mechanism sentence itself should be abstracted, not naming the
    # postmortem's specific stack.
    mechanism_lower = incident.failure_mechanism.lower()
    assert "flask" not in mechanism_lower
    assert "python" not in mechanism_lower


@pytest.mark.asyncio
async def test_archivist_dedups_on_second_run(tmp_path):
    (tmp_path / "postmortem.md").write_text(POSTMORTEM_TEXT, encoding="utf-8")
    conn = store.connect(tmp_path / "test.db")
    runner = runners.ADKRoleRunner()

    first = await archivist.learn_from_postmortem(runner, conn, tmp_path, "postmortem.md", think=False)
    first_ref = archivist.written_ref(first)
    assert first_ref

    second = await archivist.learn_from_postmortem(runner, conn, tmp_path, "postmortem.md", think=False)
    second_ref = archivist.written_ref(second)
    assert second_ref, f"second run never wrote/updated an incident: {second.final_text}"

    incidents = store.list_incidents(conn)
    matching = [i for i in incidents if i.ref == first_ref or i.ref == second_ref]
    assert first_ref == second_ref, (
        f"second run created a new ref ({second_ref}) instead of updating the first ({first_ref})"
    )
    assert len(incidents) == 1, f"expected exactly one incident after dedup, got {[i.ref for i in incidents]}"
