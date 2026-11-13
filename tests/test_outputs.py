"""Phase 7 (production plan): `omen scan --format text|markdown|json|sarif`,
`--output`, `--fail-on` and exit code 2, plus the pre-commit hook and the
example CI workflow. A scripted fake runner and fake embeddings: no model,
no Ollama.
"""
from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest
import yaml

from omen import cli, render, report, runners
from omen.contracts import Finding, Incident, SkippedChunk
from tests.test_scan_audit import ScriptedRunner, candidate_refs, scan_env, verdict  # noqa: F401  (scan_env is a fixture)

ROOT = Path(__file__).resolve().parent.parent


def read_then(t, budget):
    t["read_code"](file_path="permissions.py", start_line=1, end_line=5)
    return "read the decorator; no invalidation anywhere"


def runner_for(kind: str) -> ScriptedRunner:
    """A scripted scan whose single finding has the given Adjudicator verdict."""
    evidence = ["permissions.py:3: lru_cache"] if kind == "confirmed" else []
    return ScriptedRunner(read_then, lambda p: verdict(verdict=kind, evidence=evidence, ref=candidate_refs(p)[0]))


async def scan(repo, monkeypatch, runner, *flags) -> int:
    monkeypatch.setattr(runners, "ADKRoleRunner", lambda: runner)
    return await cli.main(["scan", str(repo), "--all", "--profile", "gpu", "--no-warmup", *flags])


# ---------------------------------------------------------------------------
# a structural check of SARIF 2.1.0 (the required fields, not the full schema)
# ---------------------------------------------------------------------------


def assert_valid_sarif(doc: dict) -> None:
    assert doc["version"] == "2.1.0"
    assert doc["$schema"].endswith("sarif-2.1.0.json")
    assert isinstance(doc["runs"], list) and len(doc["runs"]) == 1
    run = doc["runs"][0]
    driver = run["tool"]["driver"]
    assert driver["name"] and driver["version"]
    rules = driver.get("rules", [])
    ids = [r["id"] for r in rules]
    assert len(set(ids)) == len(ids), "rule ids must be unique"
    for rule in rules:
        for key in ("shortDescription", "fullDescription", "help"):
            if key in rule:
                assert rule[key]["text"].strip(), f"{rule['id']}.{key}.text is required when the object is present"
        assert rule.get("defaultConfiguration", {}).get("level", "warning") in {"none", "note", "warning", "error"}
    assert isinstance(run["results"], list)
    for result in run["results"]:
        assert result["message"]["text"].strip()
        assert result["level"] in {"none", "note", "warning", "error"}
        assert result["ruleId"] in ids and rules[result["ruleIndex"]]["id"] == result["ruleId"]
        assert result["locations"], "a result needs a location"
        for loc in result["locations"]:
            physical = loc["physicalLocation"]
            uri = physical["artifactLocation"]["uri"]
            assert uri and "\\" not in uri and not uri.startswith("/") and ":" not in uri.split("/")[0]
            if "region" in physical:
                start = physical["region"]["startLine"]
                assert isinstance(start, int) and start >= 1
                if "endLine" in physical["region"]:
                    assert physical["region"]["endLine"] >= start
        assert all(isinstance(v, str) and v for v in result["partialFingerprints"].values())


def test_the_sarif_checker_itself_rejects_broken_documents():
    good = json.loads(render.render_sarif(make_report([finding()])))
    assert_valid_sarif(good)
    for mutate in (
        lambda d: d.pop("version"),
        lambda d: d["runs"][0]["tool"]["driver"].pop("name"),
        lambda d: d["runs"][0]["results"][0]["message"].pop("text"),
        lambda d: d["runs"][0]["results"][0].update(ruleIndex=5),
        lambda d: d["runs"][0]["results"][0]["locations"][0]["physicalLocation"]["region"].update(startLine=0),
        lambda d: d["runs"][0]["results"][0].update(level="critical"),
    ):
        broken = json.loads(json.dumps(good))
        mutate(broken)
        with pytest.raises((AssertionError, KeyError, IndexError)):
            assert_valid_sarif(broken)


# ---------------------------------------------------------------------------
# hand-built reports: every renderer, including the awkward cases
# ---------------------------------------------------------------------------

INCIDENTS = [
    Incident(
        id=1, created_at="2025-01-01", ref="OMEN-001", title="Stale permission cache after revocation",
        failure_mechanism="An authorization decision is cached with no invalidation path.", what_happened="x",
        the_rule="Invalidate the cache on the permission-changing event.", severity="sev1", learned_by="seed",
    ),
    Incident(id=2, created_at="2025-01-01", ref="OMEN-004", title="Check-then-act race", failure_mechanism="m", what_happened="x", the_rule="r"),
]


def finding(verdict_="confirmed", **kw) -> Finding:
    base = dict(
        run_id="r1", incident_ref="OMEN-001", file_path="src/permissions.py", start_line=3, end_line=5, symbol="has_access",
        similarity=0.912, verdict=verdict_, code_mechanism="caches a permission decision with no invalidation",
        reasoning="matches OMEN-001", evidence="src/permissions.py:3; src/permissions.py:4", confidence="high", tool_calls=2,
    )
    return Finding(**{**base, **kw})


def make_report(findings, skipped=(), incidents=INCIDENTS, ran=True) -> report.ScanReport:
    stats = report.ScanStats(n_files=2, n_chunks=5, n_gated=1, n_triaged=1, n_investigated=1, n_tool_calls=2, ms_total=1500)
    stats.skipped.extend(skipped)
    return report.ScanReport(
        repo_path=Path("/repo"), scope="all", run_id="abc123", stats=stats, findings=list(findings), incidents=list(incidents),
        runner="adk", llm_model="gemma-test", embed_model="embed-test", ran_pipeline=ran,
    )


def test_json_round_trips_every_finding():
    findings = [finding("confirmed"), finding("unverified", downgrade_reason="no tool call", start_line=None, end_line=None, symbol=None), finding("rejected", incident_ref="OMEN-004")]
    doc = json.loads(render.render_json(make_report(findings, skipped=[SkippedChunk(file_path="a.py", similarity=0.5, stage="triage", reason="cap", symbol="f")])))
    assert doc["schema_version"] == 1 and doc["tool"]["name"] == "omen" and doc["run_id"] == "abc123"
    assert [Finding.model_validate(f) for f in doc["findings"]] == findings  # exactly the Finding objects, nothing lost
    assert doc["summary"]["findings"] == {"confirmed": 1, "unverified": 1, "rejected": 1}
    assert doc["summary"]["n_chunks"] == 5 and doc["summary"]["n_skipped"] == 1
    assert doc["incidents"]["OMEN-001"]["title"].startswith("Stale permission") and "OMEN-004" in doc["incidents"]
    assert doc["skipped"][0]["reason"] == "cap"


def test_json_of_an_empty_scan_is_a_valid_empty_report():
    doc = json.loads(render.render_json(make_report([], ran=False)))
    assert doc["findings"] == [] and doc["skipped"] == [] and doc["summary"]["findings"] == {"confirmed": 0, "unverified": 0, "rejected": 0}


def test_sarif_has_one_rule_per_incident_and_one_result_per_reported_finding():
    findings = [finding("confirmed"), finding("unverified", file_path="b.py", incident_ref="OMEN-004"), finding("rejected", file_path="c.py")]
    doc = json.loads(render.render_sarif(make_report(findings)))
    assert_valid_sarif(doc)
    run = doc["runs"][0]
    assert [r["id"] for r in run["tool"]["driver"]["rules"]] == ["OMEN-001", "OMEN-004"]  # every incident, even unreferenced ones
    assert [r["level"] for r in run["results"]] == ["error", "warning"]  # confirmed, unverified; the rejected one is not an alert
    first = run["results"][0]
    assert (first["ruleId"], first["ruleIndex"]) == ("OMEN-001", 0) and run["results"][1]["ruleIndex"] == 1
    loc = first["locations"][0]["physicalLocation"]
    assert loc["artifactLocation"] == {"uri": "src/permissions.py", "uriBaseId": "%SRCROOT%"}
    assert loc["region"] == {"startLine": 3, "endLine": 5}
    assert first["locations"][0]["logicalLocations"] == [{"name": "has_access", "kind": "function"}]
    text = first["message"]["text"]
    assert "src/permissions.py:3; src/permissions.py:4" in text  # the evidence
    assert "caches a permission decision" in text and "Stale permission cache after revocation" in text and "Invalidate the cache" in text
    rule = run["tool"]["driver"]["rules"][0]
    assert rule["shortDescription"]["text"].startswith("Stale") and rule["help"]["text"].startswith("Invalidate")
    assert first["partialFingerprints"] != run["results"][1]["partialFingerprints"]


def test_sarif_edge_cases_keep_the_document_valid():
    odd = [
        finding("confirmed", start_line=None, end_line=None, symbol=None, evidence=None, file_path="dir\\mod.py"),  # no region, no evidence, Windows path
        finding("unverified", incident_ref="OMEN-099", downgrade_reason="no successful tool call"),  # incident forgotten after the scan
    ]
    doc = json.loads(render.render_sarif(make_report(odd)))
    assert_valid_sarif(doc)
    results = doc["runs"][0]["results"]
    assert "region" not in results[0]["locations"][0]["physicalLocation"]
    assert results[0]["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] == "dir/mod.py"
    assert "none cited" in results[0]["message"]["text"]
    assert "Downgraded from confirmed: no successful tool call" in results[1]["message"]["text"]
    assert [r["id"] for r in doc["runs"][0]["tool"]["driver"]["rules"]] == ["OMEN-001", "OMEN-004", "OMEN-099"]


def test_sarif_of_an_empty_scan_is_valid():
    doc = json.loads(render.render_sarif(make_report([], ran=False)))
    assert_valid_sarif(doc)
    assert doc["runs"][0]["results"] == [] and len(doc["runs"][0]["tool"]["driver"]["rules"]) == 2


def test_markdown_shows_findings_evidence_rejections_and_skips():
    findings = [finding("confirmed"), finding("unverified", downgrade_reason="no tool call", file_path="b.py"), finding("rejected", file_path="c.py", reasoning="safe")]
    skipped = [SkippedChunk(file_path="d.py", symbol="g", similarity=0.4, stage="investigation", reason="investigation skipped: cap (--max-positives 2)")]
    md = render.render_markdown(make_report(findings, skipped=skipped))
    assert md.startswith("# Omen scan report") and md.endswith("\n")
    assert "1 confirmed, 1 unverified, 1 rejected" in md
    assert "## CONFIRMED: OMEN-001 — Stale permission cache after revocation" in md
    assert "`src/permissions.py` · `has_access` · lines 3-5" in md and "src/permissions.py:3; src/permissions.py:4" in md
    assert "> Downgraded from confirmed: no tool call" in md and "**Rule:** Invalidate the cache" in md
    assert "## Rejected by the Adjudicator" in md and "`c.py:has_access`" in md
    assert "## Skipped because of caps (1)" in md and "`d.py:g`" in md and "--max-positives 2" in md


def test_markdown_with_no_findings():
    assert "No findings." in render.render_markdown(make_report([]))
    assert "No confirmed or unverified findings." in render.render_markdown(make_report([finding("rejected")]))


def test_text_format_is_the_terminal_summary():
    rep = make_report([finding()])
    text = render.render("text", rep)
    assert text.startswith("=" * 60) and "[CONFIRMED] OMEN-001  src/permissions.py:has_access (3-5)" in text
    assert "\n".join(report.summary_lines(rep.stats, rep.findings)).strip() == text.strip()
    with pytest.raises(ValueError):
        render.render("xml", rep)


# ---------------------------------------------------------------------------
# `omen scan` end to end
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scan_json_prints_only_the_document_on_stdout(scan_env, monkeypatch, capsys):  # noqa: F811
    repo, conn, _ = scan_env
    assert await scan(repo, monkeypatch, runner_for("confirmed"), "--format", "json") == cli.EXIT_OK
    captured = capsys.readouterr()
    doc = json.loads(captured.out)  # nothing but the document: progress lines would make this fail
    assert [(f["incident_ref"], f["verdict"], f["file_path"], f["symbol"]) for f in doc["findings"]] == [("OMEN-001", "confirmed", "permissions.py", "has_access")]
    assert Finding.model_validate(doc["findings"][0]).evidence.startswith("permissions.py:3")
    assert "[triage]" in captured.err and "=== permissions.py" in captured.err  # progress went to stderr
    assert doc["run_id"] == conn.execute("SELECT run_id FROM runs").fetchone()[0]  # the same run the database recorded


@pytest.mark.asyncio
async def test_scan_sarif_validates_and_locates_the_finding(scan_env, monkeypatch, capsys):  # noqa: F811
    repo, _, _ = scan_env
    assert await scan(repo, monkeypatch, runner_for("confirmed"), "--format", "sarif") == cli.EXIT_OK
    doc = json.loads(capsys.readouterr().out)
    assert_valid_sarif(doc)
    [result] = doc["runs"][0]["results"]
    assert result["ruleId"] == "OMEN-001" and result["level"] == "error"
    assert result["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] == "permissions.py"
    assert "permissions.py:3: lru_cache" in result["message"]["text"]  # the Adjudicator's evidence
    assert [r["id"] for r in doc["runs"][0]["tool"]["driver"]["rules"]] == ["OMEN-001", "OMEN-004"]


@pytest.mark.asyncio
async def test_scan_markdown_prints_a_readable_report(scan_env, monkeypatch, capsys):  # noqa: F811
    repo, _, _ = scan_env
    assert await scan(repo, monkeypatch, runner_for("confirmed"), "--format", "markdown") == cli.EXIT_OK
    out = capsys.readouterr().out
    assert out.startswith("# Omen scan report") and "## CONFIRMED: OMEN-001 — Stale permission cache" in out and "`permissions.py`" in out


@pytest.mark.asyncio
async def test_scan_text_is_unchanged_and_is_the_default(scan_env, monkeypatch, capsys):  # noqa: F811
    repo, _, _ = scan_env
    assert await scan(repo, monkeypatch, runner_for("confirmed")) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "[triage]" in out and "scan complete in" in out and "[CONFIRMED] OMEN-001  permissions.py:has_access" in out


@pytest.mark.asyncio
async def test_output_writes_the_file_and_keeps_stdout_empty(scan_env, monkeypatch, capsys, tmp_path):  # noqa: F811
    repo, _, _ = scan_env
    target = tmp_path / "reports" / "nested" / "omen.sarif"  # the folders do not exist yet
    assert await scan(repo, monkeypatch, runner_for("confirmed"), "--format", "sarif", "--output", str(target)) == cli.EXIT_OK
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "report written to" in captured.err
    assert_valid_sarif(json.loads(target.read_text(encoding="utf-8")))


@pytest.mark.asyncio
async def test_output_with_text_format_writes_the_summary_and_still_shows_progress(scan_env, monkeypatch, capsys, tmp_path):  # noqa: F811
    repo, _, _ = scan_env
    target = tmp_path / "summary.txt"
    assert await scan(repo, monkeypatch, runner_for("confirmed"), "--output", str(target)) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "[triage]" in out and "scan complete in" not in out
    written = target.read_text(encoding="utf-8")
    assert written.startswith("=" * 60) and "[CONFIRMED] OMEN-001" in written


@pytest.mark.asyncio
@pytest.mark.parametrize("fmt", ["json", "sarif", "markdown"])
async def test_an_empty_scan_still_produces_a_valid_document(scan_env, monkeypatch, capsys, fmt):  # noqa: F811
    repo, _, _ = scan_env
    (repo / "permissions.py").unlink()  # nothing to scan
    assert await scan(repo, monkeypatch, runner_for("confirmed"), "--format", fmt) == cli.EXIT_OK
    out = capsys.readouterr().out
    if fmt == "markdown":
        assert "No findings." in out
    else:
        doc = json.loads(out)
        if fmt == "sarif":
            assert_valid_sarif(doc)
            assert doc["runs"][0]["results"] == []
        else:
            assert doc["findings"] == [] and doc["summary"]["n_chunks"] == 0


@pytest.mark.asyncio
async def test_an_empty_text_scan_prints_no_summary(scan_env, monkeypatch, capsys):  # noqa: F811
    repo, _, _ = scan_env
    (repo / "permissions.py").unlink()
    assert await scan(repo, monkeypatch, runner_for("confirmed")) == cli.EXIT_OK
    assert "scan complete" not in capsys.readouterr().out  # as before Phase 7


@pytest.mark.asyncio
async def test_a_scan_with_no_incidents_to_report_is_valid_sarif(scan_env, monkeypatch, capsys):  # noqa: F811
    repo, _, _ = scan_env
    assert await scan(repo, monkeypatch, runner_for("rejected"), "--format", "sarif") == cli.EXIT_OK
    doc = json.loads(capsys.readouterr().out)
    assert_valid_sarif(doc)
    assert doc["runs"][0]["results"] == []  # a rejected finding is not a code-scanning alert


# ---------------------------------------------------------------------------
# --fail-on
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,fail_on,expected",
    [
        ("confirmed", "confirmed", cli.EXIT_FINDINGS),
        ("confirmed", "unverified", cli.EXIT_FINDINGS),  # unverified means "at or above"
        ("unverified", "confirmed", cli.EXIT_OK),
        ("unverified", "unverified", cli.EXIT_FINDINGS),
        ("rejected", "confirmed", cli.EXIT_OK),
        ("rejected", "unverified", cli.EXIT_OK),
        ("confirmed", None, cli.EXIT_OK),  # no --fail-on: findings never change the exit code
    ],
)
async def test_fail_on_exit_codes(scan_env, monkeypatch, capsys, kind, fail_on, expected):  # noqa: F811
    repo, _, _ = scan_env
    flags = ["--fail-on", fail_on] if fail_on else []
    assert cli.EXIT_FINDINGS == 2
    assert await scan(repo, monkeypatch, runner_for(kind), *flags) == expected
    err = capsys.readouterr().err
    assert ("finding(s) at or above that level" in err) == (expected == cli.EXIT_FINDINGS)


@pytest.mark.asyncio
async def test_the_report_is_still_written_when_fail_on_trips(scan_env, monkeypatch, capsys):  # noqa: F811
    repo, _, _ = scan_env
    assert await scan(repo, monkeypatch, runner_for("confirmed"), "--format", "json", "--fail-on", "confirmed") == cli.EXIT_FINDINGS
    assert json.loads(capsys.readouterr().out)["summary"]["findings"]["confirmed"] == 1


@pytest.mark.asyncio
async def test_fail_on_with_a_clean_scan_exits_zero(scan_env, monkeypatch):  # noqa: F811
    repo, _, _ = scan_env
    (repo / "permissions.py").unlink()
    assert await scan(repo, monkeypatch, runner_for("confirmed"), "--fail-on", "unverified") == cli.EXIT_OK


@pytest.mark.asyncio
async def test_fail_on_says_when_a_cap_left_a_positive_uninvestigated(scan_env, monkeypatch, capsys):  # noqa: F811
    repo, _, _ = scan_env
    code = await scan(repo, monkeypatch, runner_for("confirmed"), "--max-positives", "0", "--fail-on", "unverified")
    assert code == cli.EXIT_OK  # nothing was investigated, so there is no finding to fail on
    assert "not investigated because of --max-positives" in capsys.readouterr().err


@pytest.mark.asyncio
@pytest.mark.parametrize("flags", [["--format", "json"], ["--output", "x.txt"], ["--fail-on", "confirmed"]])
@pytest.mark.parametrize("preview", ["--dry-run", "--retrieval-only"])
async def test_report_options_need_a_full_scan(scan_env, monkeypatch, capsys, flags, preview):  # noqa: F811
    repo, _, _ = scan_env
    assert await scan(repo, monkeypatch, runner_for("confirmed"), preview, *flags) == cli.EXIT_USER_ERROR
    captured = capsys.readouterr()
    assert "apply to a full scan" in captured.out + captured.err


@pytest.mark.asyncio
async def test_scan_errors_in_a_machine_format_go_to_stderr(tmp_path, capsys):
    assert await cli.main(["scan", str(tmp_path / "missing"), "--format", "json"]) == cli.EXIT_USER_ERROR
    captured = capsys.readouterr()
    assert captured.out == "" and "is not a directory" in captured.err


def test_bad_format_and_bad_fail_on_are_user_errors_not_exit_two():
    parser = cli.build_parser()
    for argv in (["scan", "--format", "xml"], ["scan", "--fail-on", "rejected"]):
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(argv)
        assert exc.value.code == cli.EXIT_USER_ERROR
    ns = parser.parse_args(["scan"])
    assert (ns.format, ns.output, ns.fail_on) == ("text", None, None)


# ---------------------------------------------------------------------------
# pre-commit hook and the example CI workflow
# ---------------------------------------------------------------------------


def parses_as_scan_command(command: str) -> cli.argparse.Namespace:
    argv = shlex.split(command)
    assert argv[:2] == ["omen", "scan"], command
    return cli.build_parser().parse_args(argv[1:])


def test_pre_commit_hook_is_valid_and_its_command_is_a_real_scan():
    hooks = yaml.safe_load((ROOT / ".pre-commit-hooks.yaml").read_text(encoding="utf-8"))
    [hook] = hooks
    assert {"id", "name", "entry", "language"} <= set(hook)
    assert hook["id"] == "omen-scan" and hook["language"] == "python" and hook["pass_filenames"] is False
    ns = parses_as_scan_command(hook["entry"])
    assert ns.since == "HEAD" and ns.fail_on == "confirmed"


def test_the_documented_pre_commit_example_matches_the_hook():
    text = (ROOT / "docs" / "OUTPUTS.md").read_text(encoding="utf-8")
    assert "omen scan --since HEAD --fail-on confirmed" in text
    block = text.split("```yaml", 1)[1].split("```", 1)[0]
    config = yaml.safe_load(block)
    assert config["repos"][0]["hooks"][0]["id"] == "omen-scan"


def test_example_gpu_workflow_is_valid_and_not_part_of_this_repos_ci():
    path = ROOT / "docs" / "examples" / "omen-scan.workflow.yml"
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    triggers = workflow.get("on") or workflow.get(True)  # YAML 1.1 reads a bare `on` as True
    assert "pull_request" in triggers
    [job] = workflow["jobs"].values()
    assert "self-hosted" in job["runs-on"] and "gpu" in job["runs-on"]
    steps = job["steps"]
    commands = [s["run"] for s in steps if "run" in s]
    scan_cmd = next(c for c in commands if c.startswith("omen scan"))
    ns = parses_as_scan_command(scan_cmd.replace("${{ github.base_ref }}", "main"))  # the one expression in the command
    assert ns.format == "sarif" and ns.output and ns.fail_on == "confirmed"
    assert any("upload-sarif" in s.get("uses", "") for s in steps)
    assert not (ROOT / ".github" / "workflows" / path.name).exists()  # documented, never run by our CI
