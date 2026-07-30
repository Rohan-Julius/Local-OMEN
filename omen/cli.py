"""The deterministic orchestrator. Pipeline stage order lives here; agentic
tool loops live inside individual roles (agents.py / runners.py, from Phase
6 on). PLAN.md: ADK is async, so the entry point is asyncio.run(main()) even
for today's synchronous subcommands.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from pathlib import Path

from omen import agents, archivist, config, fixtures as fixtures_mod, librarian, report, runners, scout, sifter, store, tools as tools_mod, vectors
from omen.contracts import AdjudicatorVerdict, Finding, GatedChunk, RetrievalCandidate, RunRecord, ToolCallRecord, Transcript, TriageVerdict

# Windows consoles default to a legacy codepage that mangles em-dashes and
# other non-ASCII content living in incident/postmortem text (renders as
# "�"). Force UTF-8 stdout/stderr so arbitrary ledger content prints
# correctly regardless of host codepage.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


def cmd_seed(args: argparse.Namespace) -> None:
    conn = store.connect()
    incidents = store.seed_from_yaml(conn, Path(args.yaml_path))
    print(f"Seeded {len(incidents)} incident(s) from {args.yaml_path}.")
    print("Run `omen reindex` to rebuild the vector index.")


def cmd_reindex(args: argparse.Namespace) -> None:
    conn = store.connect()
    incidents = store.list_incidents(conn)
    n = vectors.reindex(incidents)
    print(f"Reindexed {len(incidents)} incident(s), {n} variant(s) total.")


def cmd_memory_list(args: argparse.Namespace) -> None:
    conn = store.connect()
    incidents = store.list_incidents(conn)
    if not incidents:
        print("No incidents in the ledger yet. Run `omen seed incidents.yaml`.")
        return
    for inc in incidents:
        print(f"{inc.ref}  [{inc.learned_by}]  {inc.title}  ({len(inc.surface_forms)} surface forms)")


def cmd_memory_forget(args: argparse.Namespace) -> None:
    conn = store.connect()
    if not store.forget_incident(conn, args.ref):
        print(f"No incident with ref {args.ref!r} found.")
        return
    vectors.delete_incident_variants(args.ref)
    print(f"Forgot {args.ref} (removed from ledger and vector index).")


def _on_step_printer():
    def on_step(step) -> None:
        note = f"  ({step.note})" if step.note else ""
        print(f"  [tool] {step.tool_name}({step.args}) -> {str(step.result)[:100]!r}{note}")

    return on_step


async def _cmd_learn_postmortem(args: argparse.Namespace, conn, runner) -> None:
    print(f"[archivist] reading {args.postmortem_path}...")
    transcript = await archivist.learn_from_postmortem(
        runner, conn, Path.cwd(), args.postmortem_path, on_step=_on_step_printer()
    )
    print(f"[archivist] stopped_reason={transcript.stopped_reason}  {len(transcript.steps)} tool call(s)")
    print(f"[archivist] summary: {transcript.final_text}")

    ref = archivist.written_ref(transcript)
    if ref:
        print(f"Learned/updated {ref}. Run `omen memory forget {ref}` to undo.")
    else:
        print("No incident was written.")


async def _cmd_learn_git(args: argparse.Namespace, conn, runner) -> None:
    # Path(args.repo), not Path.cwd(): prompts load from a cwd-relative
    # "prompts/" (agents.PROMPTS_DIR), so the repo being learned from must
    # stay a plain argument, exactly like `omen scan <path>` — cwd has to
    # stay the omen project root for prompt loading to work at all.
    repo_root = Path(args.repo).resolve()
    candidates = sifter.select_candidates(repo_root, args.from_git, max_commits=args.max_commits)
    print(f"[sifter] {args.from_git}: {len(candidates)} candidate(s) out of up to {args.max_commits} commit(s) considered")
    for c in candidates:
        print(f"  {c.sha[:8]}  {c.reason:14}  {c.subject}")

    if args.dry_run:
        print("--dry-run: no commits will be learned")
        return

    to_learn = candidates[: args.max_learn]
    if len(candidates) > len(to_learn):
        print(f"--max-learn {args.max_learn}: only the first {len(to_learn)} candidate(s) will be learned")

    learned: list[str] = []
    for c in to_learn:
        print(f"\n[archivist] learning from {c.sha[:8]} ({c.reason}): {c.subject}")
        transcript = await archivist.learn_from_commit(runner, conn, repo_root, c.sha, on_step=_on_step_printer())
        print(f"[archivist] stopped_reason={transcript.stopped_reason}  {len(transcript.steps)} tool call(s)")
        print(f"[archivist] summary: {transcript.final_text}")
        ref = archivist.written_ref(transcript)
        if ref:
            print(f"  -> {ref}")
            learned.append(ref)
        else:
            print("  -> no incident written")

    if learned:
        print(f"\nLearned/updated {len(learned)} incident(s): {', '.join(learned)}. Run `omen memory forget <ref>` to undo any of them.")
    else:
        print("\nNo incidents were written.")


async def cmd_learn(args: argparse.Namespace) -> None:
    if bool(args.postmortem_path) == bool(args.from_git):
        print("error: pass exactly one of a postmortem path or --from-git <range>")
        return

    conn = store.connect()
    runner = runners.DirectOllamaRunner() if args.runner == "direct" else runners.ADKRoleRunner()

    if args.from_git:
        await _cmd_learn_git(args, conn, runner)
    else:
        await _cmd_learn_postmortem(args, conn, runner)


def _print_chunk_table(chunks) -> None:
    symbol_width = max(len(c.symbol) for c in chunks)
    for c in chunks:
        span = f"{c.start_line}-{c.end_line}"
        print(f"{c.file_path:40}  {c.symbol:{symbol_width}}  {span:>9}  {c.content_hash[:8]}")


def _format_candidate_block(conn, candidates: list[RetrievalCandidate]) -> list[str]:
    lines = ["CANDIDATE INCIDENTS:"]
    for cand in candidates:
        incident = store.get_incident(conn, cand.incident_ref)
        if incident is None:
            continue
        lines.append(f"{incident.ref}: {incident.title}")
        lines.append(f"  failure_mechanism: {incident.failure_mechanism}")
        lines.append(f"  the_rule: {incident.the_rule}")
    return lines


def _format_triage_prompt(conn, chunk, candidates: list[RetrievalCandidate]) -> str:
    lines = [
        f"CODE CHUNK ({chunk.file_path}:{chunk.symbol}, lines {chunk.start_line}-{chunk.end_line}):",
        chunk.content,
        "",
    ]
    lines += _format_candidate_block(conn, candidates)
    return "\n".join(lines)


def _format_investigator_prompt(chunk, verdict: TriageVerdict, candidates: list[RetrievalCandidate]) -> str:
    refs = ", ".join(c.incident_ref for c in candidates)
    return (
        f"Triage ruled MATCH on this chunk against candidate incident(s) {refs}.\n"
        f"Triage's stated mechanism: {verdict.code_mechanism}\n\n"
        f"CODE CHUNK ({chunk.file_path}:{chunk.symbol}, lines {chunk.start_line}-{chunk.end_line}):\n"
        f"{chunk.content}\n\n"
        f"Investigate whether this chunk actually shares the candidate incident's failure mechanism."
    )


def _format_adjudicator_prompt(conn, chunk, candidates: list[RetrievalCandidate], transcript: Transcript) -> str:
    """Deliberately built from `chunk`, `candidates`, and `transcript` alone
    — never from the TriageVerdict — so the Adjudicator stays blind to
    Triage's reasoning and verdict (PLAN.md Phase 7 / Verification:
    "Adjudicator independence")."""
    lines = [
        f"CODE CHUNK ({chunk.file_path}:{chunk.symbol}, lines {chunk.start_line}-{chunk.end_line}):",
        chunk.content,
        "",
    ]
    lines += _format_candidate_block(conn, candidates)
    lines.append("")
    lines.append("INVESTIGATION TRANSCRIPT:")
    if transcript.steps:
        for i, step in enumerate(transcript.steps, 1):
            lines.append(f"  [{i}] {step.tool_name}({step.args}) -> {step.result}")
            if step.note:
                lines.append(f"      note: {step.note}")
    else:
        lines.append("  (no tool calls)")
    lines.append("")
    lines.append("INVESTIGATOR'S SUMMARY:")
    lines.append(transcript.final_text or "(no summary — the investigation stopped before concluding)")
    return "\n".join(lines)


def _finalize_adjudicator_verdict(verdict: AdjudicatorVerdict) -> AdjudicatorVerdict:
    """PLAN.md Phase 7: "Empty evidence_lines on a positive -> auto-downgraded
    in code" — never left to the model's discretion."""
    if verdict.verdict == "confirmed" and not verdict.evidence_lines:
        return verdict.model_copy(update={"verdict": "unverified"})
    return verdict


async def _triage_and_investigate(
    args: argparse.Namespace,
    conn,
    repo_path: Path,
    gated: list[GatedChunk],
    run_id: str,
    stats: report.ScanStats,
) -> list[Finding]:
    runner = runners.DirectOllamaRunner() if args.runner == "direct" else runners.ADKRoleRunner()
    triage_instruction = agents.load_prompt("triage.txt")
    investigator_instruction = agents.load_prompt("investigator.txt")
    adjudicator_instruction = agents.load_prompt("adjudicator.txt")

    triage_watch = report.Stopwatch()
    investigate_watch = report.Stopwatch()
    findings: list[Finding] = []

    for gc in gated:
        c = gc.chunk
        print(f"\n=== {c.file_path}:{c.symbol} ({c.start_line}-{c.end_line}) ===")
        for cand in gc.candidates:
            print(f"  candidate: {cand.incident_ref}  similarity={cand.similarity:.3f}  ({cand.matched_variant})")

        triage_prompt = _format_triage_prompt(conn, c, gc.candidates)
        with triage_watch.lap():
            verdict = await runner.run_structured(triage_instruction, triage_prompt, TriageVerdict, think=config.THINK_DEFAULT)
        stats.n_triaged += 1
        print(f"  [triage] verdict={verdict.verdict}  confidence={verdict.confidence}")
        print(f"  [triage] mechanism: {verdict.code_mechanism}")
        print(f"  [triage] reasoning: {verdict.reasoning}")

        if verdict.verdict != "MATCH":
            continue

        print("  [investigator] investigating...")
        scan_tools = tools_mod.build_scan_tools(repo_path, conn)
        budget = tools_mod.ToolBudget()
        investigator_prompt = _format_investigator_prompt(c, verdict, gc.candidates)

        seq = 0

        def on_step(step) -> None:
            nonlocal seq
            seq += 1
            note = f"  ({step.note})" if step.note else ""
            print(f"    [tool] {step.tool_name}({step.args}) -> {step.result[:100]!r}{note}")
            store.record_tool_call(
                conn,
                ToolCallRecord(
                    run_id=run_id,
                    chunk_symbol=c.symbol,
                    seq=seq,
                    tool_name=step.tool_name,
                    args_json=json.dumps(step.args),
                    result_summary=step.result[:200],
                    ms=step.ms,
                ),
            )
            stats.n_tool_calls += 1

        with investigate_watch.lap():
            transcript = await runner.run_tooled(
                investigator_instruction,
                investigator_prompt,
                scan_tools,
                budget,
                think=config.INVESTIGATOR_THINK_DEFAULT,
                on_step=on_step,
            )
        stats.n_investigated += 1
        print(f"  [investigator] stopped_reason={transcript.stopped_reason}  {len(transcript.steps)} tool call(s)")
        print(f"  [investigator] summary: {transcript.final_text}")

        print("  [adjudicator] adjudicating...")
        adjudicator_prompt = _format_adjudicator_prompt(conn, c, gc.candidates, transcript)
        raw_adj_verdict = await runner.run_structured(
            adjudicator_instruction, adjudicator_prompt, AdjudicatorVerdict, think=config.THINK_DEFAULT
        )
        adj_verdict = _finalize_adjudicator_verdict(raw_adj_verdict)
        downgrade_note = (
            "  (downgraded: confirmed with no evidence_lines)" if adj_verdict.verdict != raw_adj_verdict.verdict else ""
        )
        print(f"  [adjudicator] verdict={adj_verdict.verdict}{downgrade_note}  confidence={adj_verdict.confidence}")
        print(f"  [adjudicator] mechanism: {adj_verdict.code_mechanism}")
        print(f"  [adjudicator] reasoning: {adj_verdict.reasoning}")
        for line in adj_verdict.evidence_lines:
            print(f"  [adjudicator] evidence: {line}")

        if adj_verdict.verdict == "confirmed":
            stats.n_confirmed += 1

        # AdjudicatorVerdict carries no structured incident_ref of its own
        # (it rules on the chunk, not on a specific candidate id) — the
        # candidate list is already similarity-sorted descending
        # (librarian.collapse_to_incidents), so the top one is the incident
        # this finding is recorded against.
        top_candidate = gc.candidates[0]
        finding = Finding(
            run_id=run_id,
            incident_ref=top_candidate.incident_ref,
            file_path=c.file_path,
            start_line=c.start_line,
            end_line=c.end_line,
            symbol=c.symbol,
            similarity=top_candidate.similarity,
            verdict=adj_verdict.verdict,
            code_mechanism=adj_verdict.code_mechanism,
            reasoning=adj_verdict.reasoning,
            evidence="; ".join(adj_verdict.evidence_lines),
            confidence=adj_verdict.confidence,
            tool_calls=len(transcript.steps),
        )
        store.record_finding(conn, finding)
        findings.append(finding)

    stats.ms_triage = triage_watch.total_ms
    stats.ms_investigate = investigate_watch.total_ms
    return findings


async def cmd_scan(args: argparse.Namespace) -> None:
    repo_path = Path(args.path).resolve()
    scope_result = scout.resolve_scope(repo_path, since=args.since, all_files=args.all)
    chunks = scout.chunk_files(scope_result.files, repo_root=repo_path)
    print(f"scope: {scope_result.scope}  ({len(scope_result.files)} file(s), {len(chunks)} chunk(s))")
    if not chunks:
        return

    if args.dry_run:
        _print_chunk_table(chunks)
        return

    conn = store.connect()

    embed_watch = report.Stopwatch()
    with embed_watch.lap():
        gated, embed_stats = librarian.run(conn, chunks)
    print(f"embed cache: {embed_stats.n_cache_hits}/{embed_stats.n_total} hits ({embed_stats.n_cache_misses} miss(es) embedded)")
    print(f"{len(gated)}/{len(chunks)} chunk(s) cleared the similarity threshold ({librarian.SIMILARITY_THRESHOLD})")

    if args.retrieval_only:
        if not gated:
            return
        for gc in gated:
            c = gc.chunk
            print(f"\n{c.file_path}:{c.symbol}  ({c.start_line}-{c.end_line})")
            for cand in gc.candidates:
                print(f"    {cand.incident_ref}  {cand.similarity:.3f}  {cand.matched_variant}")
        return

    # --dry-run and --retrieval-only never reach here, so neither shows up
    # in `runs` — both are explicitly no-generation previews, not scans.
    run_id = uuid.uuid4().hex[:12]
    store.start_run(
        conn,
        RunRecord(
            run_id=run_id,
            mission="scan",
            repo_path=str(repo_path),
            scope=scope_result.scope,
            llm_model=config.LLM_MODEL,
            embed_model=config.EMBED_MODEL,
            runner=args.runner,
        ),
    )
    total_start = time.perf_counter()
    stats = report.ScanStats(
        n_files=len(scope_result.files),
        n_chunks=len(chunks),
        n_cache_hits=embed_stats.n_cache_hits,
        n_gated=len(gated),
        ms_embed=embed_watch.total_ms,
    )

    findings: list[Finding] = []
    if gated:
        findings = await _triage_and_investigate(args, conn, repo_path, gated, run_id, stats)

    stats.ms_total = round((time.perf_counter() - total_start) * 1000)
    store.finish_run(conn, run_id, **stats.as_run_fields())
    report.print_summary(stats, findings)


def cmd_calibrate(args: argparse.Namespace) -> None:
    """PLAN.md Phase 4: sweep the similarity floor over the 12 labeled
    fixtures and print where true positives stop surviving and where hard
    negatives start being rejected — the "knee" to set SIMILARITY_THRESHOLD
    to. If a true_match can't be pushed above any reasonable floor, per
    PLAN.md that's a signal about incidents.yaml's surface forms, not the
    threshold — do not chase it by lowering the floor.
    """
    conn = store.connect()
    cases = fixtures_mod.load_fixtures(Path(args.fixtures_path))
    chunks = [fixtures_mod.fixture_to_chunk(f) for f in cases]
    embeddings, stats = librarian.embed_chunks(conn, chunks)
    print(f"embed cache: {stats.n_cache_hits}/{stats.n_total} hits\n")

    results = []
    for case, chunk in zip(cases, chunks):
        hits = vectors.query_chunk(embeddings[chunk.content_hash], n_results=librarian.QUERY_N_RESULTS)
        candidates = librarian.collapse_to_incidents(hits)
        results.append((case, candidates))

    print(f"{'id':4} {'label':13} {'target':8} top candidates")
    for case, candidates in results:
        cand_str = ", ".join(f"{c.incident_ref}={c.similarity:.3f}" for c in candidates) or "(none)"
        print(f"{case.id:4} {case.label:13} {case.target_ref or '-':8} {cand_str}")

    true_matches = [(c, cands) for c, cands in results if c.label == "true_match"]
    hard_negatives = [(c, cands) for c, cands in results if c.label == "hard_negative"]
    unrelated = [(c, cands) for c, cands in results if c.label == "unrelated"]

    print(f"\n{'threshold':>9}  {'TP survive':>10}  {'HN survive':>10}  {'unrelated survive':>17}")
    for i in range(0, 21):
        t = round(i * 0.05, 2)
        tp = sum(
            1 for case, cands in true_matches
            if any(c.incident_ref == case.target_ref and c.similarity >= t for c in cands)
        )
        hn = sum(1 for _, cands in hard_negatives if any(c.similarity >= t for c in cands))
        un = sum(1 for _, cands in unrelated if any(c.similarity >= t for c in cands))
        marker = "  <- current SIMILARITY_THRESHOLD" if abs(t - config.SIMILARITY_THRESHOLD) < 1e-9 else ""
        print(f"{t:>9.2f}  {tp:>7}/4     {hn:>7}/4     {un:>14}/4{marker}")

    print(
        "\nPLAN.md acceptance: all 4 true positives survive AND at most 2/4 hard "
        "negatives survive. Pick the highest threshold that still keeps 4/4 TP."
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="omen")
    sub = parser.add_subparsers(dest="command", required=True)

    p_seed = sub.add_parser("seed", help="Load incidents.yaml into the SQLite ledger")
    p_seed.add_argument("yaml_path")
    p_seed.set_defaults(func=cmd_seed)

    p_reindex = sub.add_parser("reindex", help="Rebuild the Chroma index from SQLite")
    p_reindex.set_defaults(func=cmd_reindex)

    p_memory = sub.add_parser("memory", help="Inspect or edit the incident ledger")
    memory_sub = p_memory.add_subparsers(dest="memory_command", required=True)

    p_memory_list = memory_sub.add_parser("list", help="List all incidents")
    p_memory_list.set_defaults(func=cmd_memory_list)

    p_memory_forget = memory_sub.add_parser("forget", help="Delete an incident by ref")
    p_memory_forget.add_argument("ref")
    p_memory_forget.set_defaults(func=cmd_memory_forget)

    p_learn = sub.add_parser("learn", help="Form a memory from a postmortem document or raw git history")
    p_learn.add_argument(
        "postmortem_path",
        nargs="?",
        default=None,
        help="Path to a postmortem document, relative to the current directory",
    )
    p_learn.add_argument(
        "--from-git",
        metavar="RANGE",
        default=None,
        help="Learn from raw git history instead of a postmortem, e.g. HEAD~40..HEAD",
    )
    p_learn.add_argument(
        "--repo",
        default=".",
        help="With --from-git, the repo to read history from (default: current directory)",
    )
    p_learn.add_argument(
        "--max-commits",
        type=int,
        default=config.MAX_COMMITS,
        help="Bound on how many commits from --from-git's range the Sifter's prefilter considers",
    )
    p_learn.add_argument(
        "--max-learn",
        type=int,
        default=config.MAX_LEARN,
        help="Bound on how many Sifter candidates the Archivist actually processes",
    )
    p_learn.add_argument(
        "--dry-run",
        action="store_true",
        help="With --from-git, print the Sifter's candidates and stop — write nothing",
    )
    p_learn.add_argument(
        "--runner",
        choices=["adk", "direct"],
        default="adk",
        help="adk (default) or direct — the reversibility hedge if ADK's tool path is flaky",
    )
    p_learn.set_defaults(func=cmd_learn)

    p_scan = sub.add_parser("scan", help="Investigate a codebase against the incident ledger")
    p_scan.add_argument("path", nargs="?", default=".")
    p_scan.add_argument("--since", default=None, help="Diff against this ref instead of the working tree")
    p_scan.add_argument("--all", action="store_true", help="Scan the whole tracked tree, not just changed files")
    p_scan.add_argument("--dry-run", action="store_true", help="Print the chunk table and stop (Scout+ast only)")
    p_scan.add_argument(
        "--retrieval-only",
        action="store_true",
        help="Embed + query Chroma, print ranked candidates, no generation (~2s, no LLM)",
    )
    p_scan.add_argument(
        "--runner",
        choices=["adk", "direct"],
        default="adk",
        help="adk (default) or direct — the reversibility hedge if ADK's schema or tool path is flaky",
    )
    p_scan.set_defaults(func=cmd_scan)

    p_calibrate = sub.add_parser("calibrate", help="Sweep the similarity floor over fixtures/fixtures.yaml")
    p_calibrate.add_argument("--fixtures-path", default=str(fixtures_mod.DEFAULT_FIXTURES_PATH))
    p_calibrate.set_defaults(func=cmd_calibrate)

    return parser


async def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    result = args.func(args)
    if asyncio.iscoroutine(result):
        await result


if __name__ == "__main__":
    asyncio.run(main())
