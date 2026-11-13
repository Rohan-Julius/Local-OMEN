"""Loader for the labeled fixture files: fixtures/calibration.yaml (the
Phase 4 tuning set and retrieval regression gate) and fixtures/heldout.yaml
(the evaluation set, never used for tuning; production plan Phase 6). Shared by `omen calibrate` (the threshold sweep)
and tests/test_fixtures.py (the pinned acceptance check), so the two
never drift apart on how a fixture becomes a Chunk.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import yaml

from omen.contracts import Chunk, FixtureCase

# Tuning set: `omen calibrate`, test_fixtures.py, test_triage.py.
DEFAULT_FIXTURES_PATH = Path("fixtures") / "calibration.yaml"
# Evaluation set: `omen eval`. Never used to choose a threshold or edit a prompt.
DEFAULT_HELDOUT_PATH = Path("fixtures") / "heldout.yaml"


def load_fixtures(path: Path = DEFAULT_FIXTURES_PATH) -> list[FixtureCase]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return [FixtureCase(**entry) for entry in data.get("fixtures", [])]


def fixture_to_chunk(fixture: FixtureCase) -> Chunk:
    """Content hash is namespaced by fixture id so two fixtures that
    happen to share code text (none do today) still cache independently."""
    content_hash = hashlib.sha256(f"{fixture.id}:{fixture.code}".encode("utf-8")).hexdigest()
    lines = fixture.code.count("\n") + 1
    return Chunk(
        file_path=fixture.file_path,
        start_line=1,
        end_line=lines,
        symbol=fixture.symbol,
        content=fixture.code,
        content_hash=content_hash,
    )
