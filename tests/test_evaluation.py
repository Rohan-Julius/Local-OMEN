"""Phase 6 (production plan): honest evaluation, model-free part.

- the two fixture files: shape, honesty rules that can be checked by a
  machine, and no overlap between the tuning set and the held-out set;
- B21: no fixture code appears in any prompt (the old Triage prompt carried
  HN1 and HN2 as worked examples, so a "hard negative" test was partly a
  memory test);
- scoring: confusion counts, precision and recall per stage;
- `omen eval` end to end with a fake runner and a fake embedder: no model,
  no Ollama.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

import pytest
import yaml

from omen import cli, evaluation, fixtures as fixtures_mod, prompts, runners, store, vectors, warmup
from omen.contracts import FixtureCase, IncidentSeed, RetrievalCandidate, TriageVerdict

ROOT = Path(__file__).resolve().parent.parent
CALIBRATION = ROOT / "fixtures" / "calibration.yaml"
HELDOUT = ROOT / "fixtures" / "heldout.yaml"
INCIDENT_REFS = {i["ref"] for i in yaml.safe_load((ROOT / "incidents.yaml").read_text(encoding="utf-8"))["incidents"]}
PROMPT_NAMES = sorted(f.name for f in (ROOT / "omen" / "prompts").glob("*.txt"))


def labels(cases):
    return [c.label for c in cases]


# ---------------------------------------------------------------------------
# the two fixture sets
# ---------------------------------------------------------------------------


def test_default_paths_point_at_the_tuning_set_and_the_held_out_set():
    assert fixtures_mod.DEFAULT_FIXTURES_PATH == Path("fixtures") / "calibration.yaml"
    assert fixtures_mod.DEFAULT_HELDOUT_PATH == Path("fixtures") / "heldout.yaml"


def test_calibration_set_is_the_original_twelve():
    cases = fixtures_mod.load_fixtures(CALIBRATION)
    assert [c.id for c in cases] == ["TM1", "TM2", "TM3", "TM4", "HN1", "HN2", "HN3", "HN4", "U1", "U2", "U3", "U4"]
    assert [labels(cases).count(x) for x in evaluation.CLASSES] == [4, 4, 4]


def test_heldout_set_has_at_least_eight_per_class():
    cases = fixtures_mod.load_fixtures(HELDOUT)
    for label in evaluation.CLASSES:
        assert labels(cases).count(label) >= 8, label


@pytest.mark.parametrize("path", [CALIBRATION, HELDOUT], ids=["calibration", "heldout"])
def test_every_fixture_is_well_formed(path):
    cases = fixtures_mod.load_fixtures(path)
    assert len({c.id for c in cases}) == len(cases)
    for c in cases:
        if c.label == "true_match":
            assert c.target_ref in INCIDENT_REFS, f"{c.id}: target {c.target_ref} is not in incidents.yaml"
        else:
            assert c.target_ref is None, f"{c.id}: only a true_match has a target"
        assert c.code.strip() and re.search(rf"\b{re.escape(c.symbol.split('.')[-1])}\b", c.code), f"{c.id}: symbol {c.symbol} not in its code"


def test_heldout_true_matches_cover_several_incidents_not_just_one():
    targets = {c.target_ref for c in fixtures_mod.load_fixtures(HELDOUT) if c.label == "true_match"}
    assert len(targets) >= 5


def test_heldout_shares_nothing_with_the_tuning_set():
    tuning = fixtures_mod.load_fixtures(CALIBRATION)
    heldout = fixtures_mod.load_fixtures(HELDOUT)

    def norm(code):
        return {ln.strip() for ln in code.splitlines() if len(ln.strip()) >= 15}

    assert not {c.id for c in tuning} & {c.id for c in heldout}
    assert not {c.symbol for c in tuning} & {c.symbol for c in heldout}
    assert not {c.file_path for c in tuning} & {c.file_path for c in heldout}
    tuning_lines = set().union(*(norm(c.code) for c in tuning))
    for c in heldout:
        assert not norm(c.code) & tuning_lines, f"{c.id} repeats a line of the tuning set: {norm(c.code) & tuning_lines}"
    assert len({c.file_path for c in heldout}) == len(heldout)  # --full writes each case to its own file


def test_the_heldout_file_says_it_is_never_used_for_tuning():
    assert "NEVER used for tuning" in HELDOUT.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# B21: no fixture inside a prompt
# ---------------------------------------------------------------------------


def contamination(prompt_text: str, cases: list[FixtureCase]) -> list[str]:
    """Fixture ids whose symbol, or any substantial line of code, appears in the prompt."""
    hits = []
    prompt_lines = {ln.strip() for ln in prompt_text.splitlines()}
    for c in cases:
        code_lines = {ln.strip() for ln in c.code.splitlines() if len(ln.strip()) >= 20}
        if re.search(rf"\b{re.escape(c.symbol)}\b", prompt_text) or code_lines & prompt_lines:
            hits.append(c.id)
    return hits


@pytest.mark.parametrize("name", PROMPT_NAMES)
def test_no_prompt_contains_a_fixture(name):
    cases = fixtures_mod.load_fixtures(CALIBRATION) + fixtures_mod.load_fixtures(HELDOUT)
    assert contamination(prompts.load_prompt(name), cases) == [], f"{name} contains fixture code"


def test_the_contamination_check_catches_the_old_triage_examples():
    """The check must fail on the prompt as it was before Phase 6 (B21)."""
    old_examples = (
        "    _thumb_cache = cachetools.TTLCache(maxsize=500, ttl=3600)\n"
        "    def get_thumbnail(image_id):\n"
        "    def connect_with_backoff(max_attempts=5):\n"
    )
    assert contamination(old_examples, fixtures_mod.load_fixtures(CALIBRATION)) == ["HN1", "HN2"]


def test_triage_prompt_keeps_two_worked_hard_negatives():
    text = prompts.load_prompt("triage.txt")
    assert "Two worked examples of hard negatives" in text
    assert text.count("Verdict: NO_MATCH") == 2 and "Example 1." in text and "Example 2." in text


def test_prompt_sha256_is_the_hash_of_the_loaded_text():
    import hashlib

    assert prompts.sha256("triage.txt") == hashlib.sha256(prompts.load_prompt("triage.txt").encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------


def case(id_, label, target=None):
    return FixtureCase(id=id_, label=label, target_ref=target, file_path=f"{id_}.py", symbol=f"f_{id_}", code=f"def f_{id_}(): pass\n")


def result(id_, label, target=None, cands=(), triage=None, full=None, threshold=0.35):
    return evaluation.CaseResult(
        case=case(id_, label, target),
        candidates=[RetrievalCandidate(incident_ref=r, similarity=s, matched_variant="mechanism") for r, s in cands],
        threshold=threshold,
        triage={"verdict": triage} if triage else None,
        full=full,
    )


def test_retrieval_positive_needs_the_target_for_a_true_match_but_any_candidate_for_a_negative():
    wrong_incident = result("a", "true_match", "OMEN-001", [("OMEN-002", 0.9)])
    right_incident = result("b", "true_match", "OMEN-001", [("OMEN-002", 0.9), ("OMEN-001", 0.4)])
    below_floor = result("c", "true_match", "OMEN-001", [("OMEN-001", 0.34)])
    at_floor = result("d", "true_match", "OMEN-001", [("OMEN-001", 0.35)])
    negative = result("e", "hard_negative", None, [("OMEN-003", 0.36)])
    assert [r.positive_at("retrieval") for r in (wrong_incident, right_incident, below_floor, at_floor, negative)] == [False, True, False, True, True]


def test_stages_are_cumulative():
    missed_by_retrieval = result("a", "true_match", "OMEN-001", [("OMEN-001", 0.2)], triage="MATCH")  # a stray verdict must not count
    assert not missed_by_retrieval.positive_at("triage")
    r = result("b", "true_match", "OMEN-001", [("OMEN-001", 0.5)], triage="MATCH")
    assert r.positive_at("retrieval") and r.positive_at("triage") and not r.positive_at("full")  # Adjudicator not run
    r.full = {"verdict": "confirmed", "incident_ref": "OMEN-001"}
    assert r.positive_at("full")
    r.full = {"verdict": "confirmed", "incident_ref": "OMEN-004"}  # confirmed against the wrong incident
    assert not r.positive_at("full")
    r.full = {"verdict": "unverified", "incident_ref": "OMEN-001"}
    assert not r.positive_at("full")
    r.triage = {"verdict": "NO_MATCH"}
    r.full = {"verdict": "confirmed", "incident_ref": "OMEN-001"}
    assert not r.positive_at("full")  # Triage rejected it: the later stage cannot revive it
    negative = result("n", "unrelated", None, [("OMEN-001", 0.9)], triage="MATCH", full={"verdict": "confirmed", "incident_ref": "OMEN-006"})
    assert negative.positive_at("full")  # any confirmed finding on a negative is a false positive


def test_score_stage_counts_precision_and_recall():
    results = [
        result("t1", "true_match", "OMEN-001", [("OMEN-001", 0.9)]),
        result("t2", "true_match", "OMEN-002", [("OMEN-002", 0.9)]),
        result("t3", "true_match", "OMEN-003", [("OMEN-003", 0.1)]),
        result("t4", "true_match", "OMEN-004", [("OMEN-004", 0.9)]),
        result("h1", "hard_negative", None, [("OMEN-001", 0.8)]),
        result("h2", "hard_negative", None, [("OMEN-001", 0.1)]),
        result("u1", "unrelated", None, [("OMEN-005", 0.5)]),
        result("u2", "unrelated", None, []),
    ]
    s = evaluation.score_stage(results, "retrieval")
    assert s["confusion"] == {"tp": 3, "fp": 2, "fn": 1, "tn": 2}
    assert s["precision"] == 0.6 and s["recall"] == 0.75
    assert s["survivors"] == {"true_match": ["t1", "t2", "t4"], "hard_negative": ["h1"], "unrelated": ["u1"]}
    assert s["survivor_counts"] == {"true_match": 3, "hard_negative": 1, "unrelated": 1}


def test_precision_is_none_when_nothing_is_reported_and_recall_none_without_positives():
    only_negatives = [result("h", "hard_negative", None, [("OMEN-001", 0.1)])]
    s = evaluation.score_stage(only_negatives, "retrieval")
    assert s["precision"] is None and s["recall"] is None
    assert evaluation.score_stage([], "triage")["confusion"] == {"tp": 0, "fp": 0, "fn": 0, "tn": 0}


def test_write_results_never_overwrites_an_earlier_result(tmp_path):
    report = {"date": "2026-11-12", "x": 1}
    first = evaluation.write_results(report, tmp_path / "eval")
    second = evaluation.write_results({**report, "x": 2}, tmp_path / "eval")
    assert first.name == "results-2026-11-12.json" and second.name == "results-2026-11-12-2.json"
    assert json.loads(first.read_text(encoding="utf-8"))["x"] == 1


# ---------------------------------------------------------------------------
# `omen eval` end to end: fake embedder, fake runner
# ---------------------------------------------------------------------------

DIM = 8
ORTHOGONAL = DIM - 1

# id -> (label, target incident number, similarity to that incident, Triage, Adjudicator)
PLAN = {
    "A": ("true_match", 1, 0.90, "MATCH", "confirmed"),  # found, kept by every stage
    "B": ("true_match", 2, 0.20, "MATCH", "confirmed"),  # retrieval misses it: below the floor
    "C": ("true_match", 1, 0.90, "NO_MATCH", None),  # Triage rejects a true match
    "D": ("hard_negative", 1, 0.80, "NO_MATCH", None),  # retrieval admits it, Triage rejects it
    "E": ("hard_negative", 2, 0.70, "MATCH", "rejected"),  # Triage is fooled, the Adjudicator is not
    "F": ("unrelated", 1, 0.10, "MATCH", "confirmed"),  # never retrieved: later stages must not matter
    "G": ("unrelated", 1, 0.40, "NO_MATCH", None),  # retrieved but rejected
}
SYMBOLS = {k: f"case_{k.lower()}" for k in PLAN}
EXPECTED = {  # stage -> (TP, FP, FN, TN)
    "retrieval": (2, 3, 1, 1),
    "triage": (1, 1, 2, 3),
    "full": (1, 0, 2, 4),
}


def write_fixture_file(path: Path, plan=PLAN) -> Path:
    entries = []
    for key, (label, n, _sim, _t, _a) in plan.items():
        entries.append(
            {
                "id": key,
                "label": label,
                "target_ref": f"OMEN-00{n}" if label == "true_match" else None,
                "file_path": f"fx/{SYMBOLS[key]}.py",
                "symbol": SYMBOLS[key],
                "code": f"def {SYMBOLS[key]}(x):\n    return x + {ord(key)}\n",
            }
        )
    path.write_text(yaml.safe_dump({"fixtures": entries}), encoding="utf-8")
    return path


def fake_embedder(plan=PLAN):
    def embed(texts):
        out = []
        for text in texts:
            m = re.search(r"AXIS(\d)", text)
            if m:
                v = [0.0] * DIM
                v[int(m.group(1))] = 1.0
            else:
                key = next((k for k in plan if f"def {SYMBOLS[k]}(" in text), None)
                assert key, f"unexpected text embedded: {text[:60]!r}"
                _label, n, sim, *_ = plan[key]
                v = [0.0] * DIM
                v[n] = sim
                v[ORTHOGONAL] = math.sqrt(1 - sim * sim)
            out.append(v)
        return out

    return embed


class EvalRunner:
    """Answers from PLAN, by the symbol in the prompt. Records which roles ran."""

    def __init__(self, plan=PLAN):
        self.plan = plan
        self.calls: list[tuple[str, str]] = []

    def _key(self, prompt):
        return next(k for k in self.plan if SYMBOLS[k] in prompt)

    async def run_structured(self, instruction, prompt, schema, think=False):
        key = self._key(prompt)
        _label, n, _sim, triage, adjudicator = self.plan[key]
        if schema is TriageVerdict:
            self.calls.append(("triage", key))
            return TriageVerdict(code_mechanism="m", verdict=triage, confidence="high", reasoning="r")
        self.calls.append(("adjudicator", key))
        path = re.search(r"CODE CHUNK \(([^:]+):", prompt).group(1)
        return schema.model_validate(
            {
                "verdict": adjudicator,
                "code_mechanism": "m",
                "reasoning": "r",
                "evidence_lines": [f"{path}:1"] if adjudicator == "confirmed" else [],
                "confidence": "high",
                "incident_ref": f"OMEN-00{n}",
            }
        )

    async def run_tooled(self, instruction, prompt, tools, budget, think=True, on_step=None, on_text=None):
        from omen import tools as tools_mod
        from omen.contracts import Transcript

        key = self._key(prompt)
        self.calls.append(("investigator", key))
        path = re.search(r"CODE CHUNK \(([^:]+):", prompt).group(1)
        wrapped, steps = tools_mod.budgeted_tools(tools, budget, on_step=on_step)
        reader = next(fn for fn in wrapped if fn.__name__ == "read_code")
        reader(file_path=path, start_line=1, end_line=2)  # only works if --full wrote the case's code to the temp repo
        return Transcript(steps=steps, final_text="looked", stopped_reason=runners._stopped_reason(budget, steps, "looked"))


@pytest.fixture
def eval_env(tmp_path, monkeypatch):
    """A ledger of six incidents on orthogonal axes, the fake embedder, a
    fixture file and a results directory. Returns (fixtures_path, results_dir, runner)."""
    monkeypatch.setattr(vectors, "_embed", fake_embedder())
    conn = store.connect()
    for n in range(1, 7):
        store.upsert_incident(
            conn,
            IncidentSeed(
                ref=f"OMEN-00{n}", title=f"Incident {n}", failure_mechanism=f"AXIS{n} mechanism", what_happened="x", the_rule="y",
                surface_forms=[f"AXIS{n} surface form"],
            ),
        )
    vectors.reindex(store.list_incidents(conn))
    runner = EvalRunner()
    monkeypatch.setattr(runners, "ADKRoleRunner", lambda: runner)
    return write_fixture_file(tmp_path / "fx.yaml"), tmp_path / "results", runner


def eval_argv(fixtures_path, results_dir, *flags):
    return ["eval", "--fixtures-path", str(fixtures_path), "--results-dir", str(results_dir), "--profile", "gpu", "--no-warmup", *flags]


@pytest.mark.asyncio
async def test_eval_scores_every_stage_and_writes_the_results_file(eval_env, capsys):
    fixtures_path, results_dir, runner = eval_env

    assert await cli.main(eval_argv(fixtures_path, results_dir, "--full")) == cli.EXIT_OK

    files = list(results_dir.glob("results-*.json"))
    assert len(files) == 1
    report = json.loads(files[0].read_text(encoding="utf-8"))  # the JSON round-trips
    assert report["n_cases"] == {"true_match": 3, "hard_negative": 2, "unrelated": 2}
    assert report["threshold"] == 0.35 and report["runner"] == "adk" and report["profile"] == "gpu"
    assert set(report["prompt_sha256"]) == {"triage.txt", "investigator.txt", "adjudicator.txt"}
    assert report["prompt_sha256"]["triage.txt"] == prompts.sha256("triage.txt")
    assert len(report["fixtures_sha256"]) == 64
    for stage, (tp, fp, fn, tn) in EXPECTED.items():
        assert report["stages"][stage]["confusion"] == {"tp": tp, "fp": fp, "fn": fn, "tn": tn}, stage
    assert report["stages"]["retrieval"]["survivors"] == {"true_match": ["A", "C"], "hard_negative": ["D", "E"], "unrelated": ["G"]}
    assert report["stages"]["triage"]["survivors"] == {"true_match": ["A"], "hard_negative": ["E"], "unrelated": []}
    assert report["stages"]["full"]["survivors"] == {"true_match": ["A"], "hard_negative": [], "unrelated": []}
    assert report["stages"]["retrieval"]["precision"] == 0.4 and report["stages"]["retrieval"]["recall"] == 0.6667
    assert report["stages"]["triage"]["precision"] == 0.5 and report["stages"]["triage"]["recall"] == 0.3333
    assert report["stages"]["full"]["precision"] == 1.0
    by_id = {c["id"]: c for c in report["cases"]}
    assert by_id["A"]["full"]["verdict"] == "confirmed" and by_id["A"]["full"]["tool_calls"] == 1
    assert by_id["E"]["full"]["raw_verdict"] == "rejected"
    assert by_id["B"]["triage"] is None  # below the floor: Triage never saw it
    out = capsys.readouterr().out
    assert "precision" in out and "recall" in out and "retrieval" in out and "wrote " in out
    # Triage ran only for chunks that cleared the floor, the Investigator and Adjudicator only for Triage positives
    assert [k for r, k in runner.calls if r == "triage"] == ["A", "C", "D", "E", "G"]
    assert sorted(k for r, k in runner.calls if r == "investigator") == ["A", "E"]
    assert sorted(k for r, k in runner.calls if r == "adjudicator") == ["A", "E"]


@pytest.mark.asyncio
async def test_eval_without_full_stops_after_triage(eval_env):
    fixtures_path, results_dir, runner = eval_env
    assert await cli.main(eval_argv(fixtures_path, results_dir)) == cli.EXIT_OK
    report = json.loads(next(results_dir.glob("results-*.json")).read_text(encoding="utf-8"))
    assert list(report["stages"]) == ["retrieval", "triage"]
    assert {r for r, _ in runner.calls} == {"triage"}


@pytest.mark.asyncio
async def test_eval_retrieval_only_never_builds_a_runner_or_warms_up(eval_env, monkeypatch):
    fixtures_path, results_dir, _ = eval_env

    def boom(*a, **k):
        raise AssertionError("retrieval-only must not touch a model")

    monkeypatch.setattr(runners, "ADKRoleRunner", boom)
    monkeypatch.setattr(runners, "DirectOllamaRunner", boom)
    monkeypatch.setattr(warmup, "warm_up", boom)
    argv = ["eval", "--fixtures-path", str(fixtures_path), "--results-dir", str(results_dir), "--profile", "gpu", "--retrieval-only"]
    assert await cli.main(argv) == cli.EXIT_OK
    report = json.loads(next(results_dir.glob("results-*.json")).read_text(encoding="utf-8"))
    assert list(report["stages"]) == ["retrieval"] and report["runner"] is None
    assert report["stages"]["retrieval"]["confusion"] == {"tp": 2, "fp": 3, "fn": 1, "tn": 1}


@pytest.mark.asyncio
async def test_eval_runs_the_direct_runner_when_asked(eval_env, monkeypatch):
    fixtures_path, results_dir, _ = eval_env
    direct = EvalRunner()
    monkeypatch.setattr(runners, "DirectOllamaRunner", lambda: direct)
    assert await cli.main(eval_argv(fixtures_path, results_dir, "--runner", "direct")) == cli.EXIT_OK
    assert direct.calls and json.loads(next(results_dir.glob("results-*.json")).read_text(encoding="utf-8"))["runner"] == "direct"


@pytest.mark.asyncio
async def test_eval_makes_no_llm_call_and_no_warmup_when_nothing_clears_retrieval(eval_env, monkeypatch):
    fixtures_path, results_dir, runner = eval_env
    quiet = {k: v for k, v in PLAN.items() if k in ("B", "F")}  # both below the floor
    write_fixture_file(fixtures_path, quiet)
    monkeypatch.setattr(vectors, "_embed", fake_embedder())
    warm = []
    monkeypatch.setattr(warmup, "warm_up", lambda *a, **k: warm.append(1) or 0.0)
    argv = ["eval", "--fixtures-path", str(fixtures_path), "--results-dir", str(results_dir), "--profile", "gpu"]
    assert await cli.main(argv) == cli.EXIT_OK
    assert runner.calls == [] and warm == []


@pytest.mark.asyncio
async def test_eval_errors_are_user_errors(eval_env, tmp_path, capsys):
    fixtures_path, results_dir, _ = eval_env
    assert await cli.main(eval_argv(tmp_path / "nope.yaml", results_dir)) == cli.EXIT_USER_ERROR
    assert await cli.main(eval_argv(fixtures_path, results_dir, "--full", "--retrieval-only")) == cli.EXIT_USER_ERROR
    empty = tmp_path / "empty.yaml"
    empty.write_text("fixtures: []\n", encoding="utf-8")
    assert await cli.main(eval_argv(empty, results_dir)) == cli.EXIT_USER_ERROR
    assert "no fixtures" in capsys.readouterr().out
    assert not results_dir.exists()


@pytest.mark.asyncio
async def test_eval_with_an_empty_ledger_is_a_user_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(vectors, "_embed", fake_embedder())
    fixtures_path = write_fixture_file(tmp_path / "fx.yaml")
    assert await cli.main(eval_argv(fixtures_path, tmp_path / "results")) == cli.EXIT_USER_ERROR
    assert "ledger is empty" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_a_second_eval_on_the_same_day_keeps_the_first_result(eval_env):
    fixtures_path, results_dir, _ = eval_env
    for _ in range(2):
        assert await cli.main(eval_argv(fixtures_path, results_dir)) == cli.EXIT_OK
    assert len(list(results_dir.glob("results-*.json"))) == 2


def test_eval_defaults_to_the_held_out_file():
    ns = cli.build_parser().parse_args(["eval"])
    assert Path(ns.fixtures_path) == Path("fixtures") / "heldout.yaml" and ns.full is False and ns.runner == "adk"
    assert Path(ns.results_dir) == Path("eval")
