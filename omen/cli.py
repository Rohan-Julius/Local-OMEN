"""The deterministic orchestrator. Pipeline stage order lives here; agentic
tool loops live inside individual roles (agents.py / runners.py, from Phase
6 on). PLAN.md: ADK is async, so main() is a coroutine even for the
synchronous subcommands; `entrypoint()` is the sync wrapper behind the
`omen` console script.

Exit codes (audit B18): 0 success, 1 user error (bad arguments, a missing
input file, an unknown ref, an empty ledger), 2 reserved for `--fail-on`
(production plan Phase 7), 3 runtime or model error.

ADK is imported only by commands that run a model (audit B28): runners.py
is imported inside `_make_runner`, and runners.py imports agents.py (the
only google.adk importer) only when an ADK role runs.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
import uuid
from pathlib import Path

from omen import (
    archivist,
    config,
    evidence,
    fixtures as fixtures_mod,
    librarian,
    prompts,
    report,
    scout,
    sifter,
    store,
    tools as tools_mod,
    vectors,
)
from omen.contracts import (
    Finding,
    GatedChunk,
    RetrievalCandidate,
    RunRecord,
    ToolCallRecord,
    Transcript,
    TriageVerdict,
    adjudicator_schema,
)

# Windows consoles default to a legacy codepage that mangles em-dashes and
# other non-ASCII content living in incident/postmortem text (renders as
# "�"). Force UTF-8 stdout/stderr so arbitrary ledger content prints
# correctly regardless of host codepage.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

EXIT_OK = 0
EXIT_USER_ERROR = 1
EXIT_FINDINGS = 2  # reserved: `--fail-on` (production plan Phase 7)
EXIT_RUNTIME_ERROR = 3

log = logging.getLogger("omen")


def _user_error(message: str) -> int:
    print(f"error: {message}")
    return EXIT_USER_ERROR


def _make_runner(name: str):
    """The only place the CLI imports runners.py, so commands that never
    run a model never import an LLM framework (B28)."""
    from omen import runners

    return runners.DirectOllamaRunner() if name == "direct" else runners.ADKRoleRunner()


def cmd_seed(args: argparse.Namespace) -> int | None:
    if not Path(args.yaml_path).is_file():
        return _user_error(f"{args.yaml_path} is not a file")
    conn = store.connect()
    incidents = store.seed_from_yaml(conn, Path(args.yaml_path))
    print(f"Seeded {len(incidents)} incident(s) from {args.yaml_path}.")
    print("Run `omen reindex` to rebuild the vector index.")


def cmd_reindex(args: argparse.Namespace) -> int | None:
    conn = store.connect()
    incidents = store.list_incidents(conn)
    n = vectors.reindex(incidents)
    print(f"Reindexed {len(incidents)} incident(s), {n} variant(s) total.")


def cmd_memory_list(args: argparse.Namespace) -> int | None:
    conn = store.connect()
    incidents = store.list_incidents(conn)
    if not incidents:
        # Listing an empty ledger is a valid answer, not an error.
        print("No incidents in the ledger yet. Run `omen seed incidents.yaml`.")
        return EXIT_OK
    for inc in incidents:
        print(f"{inc.ref}  [{inc.learned_by}]  {inc.title}  ({len(inc.surface_forms)} surface forms)")


def cmd_memory_forget(args: argparse.Namespace) -> int | None:
    conn = store.connect()
    if not store.forget_incident(conn, args.ref):
        print(f"No incident with ref {args.ref!r} found.")
        return EXIT_USER_ERROR
    vectors.delete_incident_variants(args.ref)
    print(f"Forgot {args.ref} (removed from ledger and vector index).")


def _start_run(conn, mission: str, repo_path: Path, scope: str, runner: str) -> str:
    run_id = uuid.uuid4().hex[:12]
    store.start_run(
        conn,
        RunRecord(
            run_id=run_id,
            mission=mission,
            repo_path=str(repo_path),
            scope=scope,
            llm_model=config.LLM_MODEL,
            embed_model=config.EMBED_MODEL,
            runner=runner,
        ),
    )
    return run_id


def _step_recorder(conn, run_id: str, label: str, indent: str = "  ", on_record=None):
    """The Scribe's per-tool-call path, shared by `scan` (label = chunk
    symbol) and `learn` (label = the source being learned from, audit
    B15): print the call, persist it to `tool_calls` with its note (cap,
    duplicate or error, audit B12), then call `on_record`."""
    seq = 0

    def on_step(step) -> None:
        nonlocal seq
        seq += 1
        note = f"  ({step.note})" if step.note else ""
        print(f"{indent}[tool] {step.tool_name}({step.args}) -> {str(step.result)[:100]!r}{note}")
        store.record_tool_call(
            conn,
            ToolCallRecord(
                run_id=run_id,
                chunk_symbol=label,
                seq=seq,
                tool_name=step.tool_name,
                args_json=json.dumps(step.args),
                result_summary=str(step.result)[:200],
                ms=step.ms,
                note=step.note,
            ),
        )
        if on_record:
            on_record(step)

    return on_step


def _postmortem_root(args: argparse.Namespace) -> tuple[Path, str] | None:
    """The directory the Archivist's read_file is confined to, and the
    postmortem's path relative to it. Default: the postmortem's own
    directory, never the cwd. The cwd is normally the Omen project root,
    which holds `.env` and `omen_store/` (audit B4). `--allow-dir` widens
    the root to a directory that must contain the postmortem."""
    postmortem = Path(args.postmortem_path).resolve()
    allow_dir = getattr(args, "allow_dir", None)
    root = Path(allow_dir).resolve() if allow_dir else postmortem.parent
    try:
        rel = postmortem.relative_to(root)
    except ValueError:
        print(f"error: {args.postmortem_path} is not inside --allow-dir {allow_dir}")
        return None
    return root, rel.as_posix()


async def _cmd_learn_postmortem(args: argparse.Namespace, conn, runner) -> int | None:
    confined = _postmortem_root(args)
    if confined is None:
        return EXIT_USER_ERROR
    root, rel_path = confined
    source = f"postmortem:{args.postmortem_path}"
    start = time.perf_counter()
    run_id = _start_run(conn, "learn", root, source, args.runner)
    fields = {"n_investigated": 0, "n_tool_calls": 0, "n_written": 0}
    try:
        print(f"[archivist] reading {args.postmortem_path}...")
        transcript = await archivist.learn_from_postmortem(
            runner,
            conn,
            root,
            rel_path,
            on_step=_step_recorder(conn, run_id, source),
            source=source,
        )
        fields["n_investigated"] = 1
        fields["n_tool_calls"] = len(transcript.steps)
        print(f"[archivist] stopped_reason={transcript.stopped_reason}  {len(transcript.steps)} tool call(s)")
        print(f"[archivist] summary: {transcript.final_text}")

        ref = archivist.written_ref(transcript)
        if ref:
            fields["n_written"] = 1
            print(f"Learned/updated {ref}. Run `omen memory forget {ref}` to undo.")
        else:
            print("No incident was written.")
    finally:
        ms = round((time.perf_counter() - start) * 1000)
        store.finish_run(conn, run_id, ms_investigate=ms, ms_total=ms, **fields)


async def _cmd_learn_git(args: argparse.Namespace, conn, runner) -> int | None:
    # The repo being learned from is a plain argument (--repo, default the
    # cwd), exactly like `omen scan <path>`.
    start = time.perf_counter()
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
    run_id = _start_run(conn, "learn", repo_root, f"git:{args.from_git}", args.runner)
    fields = {"n_investigated": 0, "n_tool_calls": 0, "n_written": 0}
    archivist_watch = report.Stopwatch()
    try:
        for c in to_learn:
            print(f"\n[archivist] learning from {c.sha[:8]} ({c.reason}): {c.subject}")
            with archivist_watch.lap():
                transcript = await archivist.learn_from_commit(
                    runner, conn, repo_root, c.sha, on_step=_step_recorder(conn, run_id, f"git:{c.sha[:12]}")
                )
            fields["n_investigated"] += 1
            fields["n_tool_calls"] += len(transcript.steps)
            print(f"[archivist] stopped_reason={transcript.stopped_reason}  {len(transcript.steps)} tool call(s)")
            print(f"[archivist] summary: {transcript.final_text}")
            ref = archivist.written_ref(transcript)
            if ref:
                print(f"  -> {ref}")
                learned.append(ref)
            else:
                print("  -> no incident written")
    finally:
        fields["n_written"] = len(learned)
        store.finish_run(
            conn,
            run_id,
            ms_investigate=archivist_watch.total_ms,
            ms_total=round((time.perf_counter() - start) * 1000),
            **fields,
        )

    if learned:
        print(f"\nLearned/updated {len(learned)} incident(s): {', '.join(learned)}. Run `omen memory forget <ref>` to undo any of them.")
    else:
        print("\nNo incidents were written.")


async def cmd_learn(args: argparse.Namespace) -> int | None:
    if bool(args.postmortem_path) == bool(args.from_git):
        return _user_error("pass exactly one of a postmortem path or --from-git <range>")
    if args.postmortem_path and not Path(args.postmortem_path).is_file():
        return _user_error(f"{args.postmortem_path} is not a file")
    if args.from_git and not Path(args.repo).is_dir():
        return _user_error(f"--repo {args.repo} is not a directory")

    conn = store.connect()
    runner = _make_runner(args.runner)

    if args.from_git:
        return await _cmd_learn_git(args, conn, runner)
    return await _cmd_learn_postmortem(args, conn, runner)


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


def _format_investigator_prompt(conn, chunk, candidates: list[RetrievalCandidate]) -> str:
    """The chunk and the candidate incidents only. Triage's verdict and
    mechanism used to be passed here; the Investigator's summary could
    echo them into the Adjudicator's prompt, so the Adjudicator was not
    blind to Triage (audit B7)."""
    lines = [
        f"CODE CHUNK ({chunk.file_path}:{chunk.symbol}, lines {chunk.start_line}-{chunk.end_line}):",
        chunk.content,
        "",
    ]
    lines += _format_candidate_block(conn, candidates)
    lines.append("")
    lines.append("Investigate whether this chunk actually shares a candidate incident's failure mechanism.")
    return "\n".join(lines)


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


def _chosen_candidate(verdict, candidates: list[RetrievalCandidate]) -> RetrievalCandidate:
    """The candidate the Adjudicator ruled on (audit B17). The per-call
    schema restricts `incident_ref` to these refs, so the fallback to the
    top candidate only fires for a runner that ignored the schema."""
    for cand in candidates:
        if cand.incident_ref == verdict.incident_ref:
            return cand
    log.warning("adjudicator returned incident_ref %r, not a candidate; recording against %s",
                verdict.incident_ref, candidates[0].incident_ref)
    return candidates[0]


async def _triage_and_investigate(
    args: argparse.Namespace,
    conn,
    repo_path: Path,
    gated: list[GatedChunk],
    run_id: str,
    stats: report.ScanStats,
) -> list[Finding]:
    runner = _make_runner(args.runner)
    triage_instruction = prompts.load_prompt("triage.txt")
    investigator_instruction = prompts.load_prompt("investigator.txt")
    adjudicator_instruction = prompts.load_prompt("adjudicator.txt")

    triage_watch = report.Stopwatch()
    investigate_watch = report.Stopwatch()
    adjudicate_watch = report.Stopwatch()
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
        investigator_prompt = _format_investigator_prompt(conn, c, gc.candidates)

        def count_call(step) -> None:
            stats.n_tool_calls += 1

        on_step = _step_recorder(conn, run_id, c.symbol, indent="    ", on_record=count_call)

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
        schema = adjudicator_schema([cand.incident_ref for cand in gc.candidates])
        with adjudicate_watch.lap():
            raw_adj_verdict = await runner.run_structured(
                adjudicator_instruction, adjudicator_prompt, schema, think=config.THINK_DEFAULT
            )
        adj_verdict, downgrade_reason = evidence.validate_confirmed(raw_adj_verdict, c, transcript, repo_path)
        downgrade_note = f"  (downgraded from confirmed: {downgrade_reason})" if downgrade_reason else ""
        print(f"  [adjudicator] verdict={adj_verdict.verdict}{downgrade_note}  confidence={adj_verdict.confidence}")
        print(f"  [adjudicator] incident: {adj_verdict.incident_ref}")
        print(f"  [adjudicator] mechanism: {adj_verdict.code_mechanism}")
        print(f"  [adjudicator] reasoning: {adj_verdict.reasoning}")
        for line in adj_verdict.evidence_lines:
            print(f"  [adjudicator] evidence: {line}")

        if adj_verdict.verdict == "confirmed":
            stats.n_confirmed += 1

        chosen = _chosen_candidate(adj_verdict, gc.candidates)
        finding = Finding(
            run_id=run_id,
            incident_ref=chosen.incident_ref,
            file_path=c.file_path,
            start_line=c.start_line,
            end_line=c.end_line,
            symbol=c.symbol,
            similarity=chosen.similarity,
            verdict=adj_verdict.verdict,
            code_mechanism=adj_verdict.code_mechanism,
            reasoning=adj_verdict.reasoning,
            evidence="; ".join(adj_verdict.evidence_lines),
            confidence=adj_verdict.confidence,
            tool_calls=len(transcript.steps),
            downgrade_reason=downgrade_reason,
        )
        store.record_finding(conn, finding)
        findings.append(finding)

    stats.ms_triage = triage_watch.total_ms
    stats.ms_investigate = investigate_watch.total_ms
    stats.ms_adjudicate = adjudicate_watch.total_ms
    return findings


async def cmd_scan(args: argparse.Namespace) -> int | None:
    if not Path(args.path).is_dir():
        return _user_error(f"{args.path} is not a directory")
    if not args.dry_run and not store.list_incidents(store.connect()):
        # Nothing to match against: a scan cannot find anything, so say so
        # instead of embedding the code and recording an empty run.
        return _user_error("the incident ledger is empty - run `omen seed <incidents.yaml>` and `omen reindex` first")
    total_start = time.perf_counter()
    repo_path = Path(args.path).resolve()
    scout_watch = report.Stopwatch()
    with scout_watch.lap():
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
    run_id = _start_run(conn, "scan", repo_path, scope_result.scope, args.runner)
    stats = report.ScanStats(
        n_files=len(scope_result.files),
        n_chunks=len(chunks),
        n_cache_hits=embed_stats.n_cache_hits,
        n_gated=len(gated),
        ms_scout=scout_watch.total_ms,
        ms_embed=embed_watch.total_ms,
    )

    findings: list[Finding] = []
    if gated:
        findings = await _triage_and_investigate(args, conn, repo_path, gated, run_id, stats)

    # From the start of the command: Scout and embedding included (audit B14).
    stats.ms_total = round((time.perf_counter() - total_start) * 1000)
    store.finish_run(conn, run_id, **stats.as_run_fields())
    report.print_summary(stats, findings)


def cmd_calibrate(args: argparse.Namespace) -> int | None:
    """PLAN.md Phase 4: sweep the similarity floor over the 12 labeled
    fixtures and print where true positives stop surviving and where hard
    negatives start being rejected — the "knee" to set SIMILARITY_THRESHOLD
    to. If a true_match can't be pushed above any reasonable floor, per
    PLAN.md that's a signal about incidents.yaml's surface forms, not the
    threshold — do not chase it by lowering the floor.
    """
    if not Path(args.fixtures_path).is_file():
        return _user_error(f"{args.fixtures_path} is not a file (pass --fixtures-path)")
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


class _Parser(argparse.ArgumentParser):
    """argparse exits with 2 on bad arguments; Omen reserves 2 for
    `--fail-on` (Phase 7), so argument errors exit 1 like every other user
    error. Subparsers inherit this class."""

    def error(self, message: str):
        self.print_usage(sys.stderr)
        self.exit(EXIT_USER_ERROR, f"{self.prog}: error: {message}\n")


def _add_global_options(parser: argparse.ArgumentParser, suppress: bool) -> None:
    """--store/--verbose/--quiet, accepted before or after the subcommand.
    Subcommand copies use SUPPRESS so an option given before the
    subcommand is not reset by the subcommand's default."""
    default = argparse.SUPPRESS if suppress else None
    flag_default = argparse.SUPPRESS if suppress else False
    parser.add_argument(
        "--store",
        default=default,
        help="Store directory (default: $OMEN_HOME, else ./omen_store)",
    )
    noise = parser.add_mutually_exclusive_group()
    noise.add_argument("-v", "--verbose", action="store_true", default=flag_default, help="Show debug diagnostics")
    noise.add_argument("-q", "--quiet", action="store_true", default=flag_default, help="Show only errors in diagnostics")


def build_parser() -> argparse.ArgumentParser:
    common = _Parser(add_help=False)
    _add_global_options(common, suppress=True)

    parser = _Parser(prog="omen")
    _add_global_options(parser, suppress=False)
    sub = parser.add_subparsers(dest="command", required=True)
    _add_top_parser = sub.add_parser
    sub.add_parser = lambda name, **kwargs: _add_top_parser(name, parents=[common], **kwargs)

    p_seed = sub.add_parser("seed", help="Load incidents.yaml into the SQLite ledger")
    p_seed.add_argument("yaml_path")
    p_seed.set_defaults(func=cmd_seed)

    p_reindex = sub.add_parser("reindex", help="Rebuild the Chroma index from SQLite")
    p_reindex.set_defaults(func=cmd_reindex)

    p_memory = sub.add_parser("memory", help="Inspect or edit the incident ledger")
    memory_sub = p_memory.add_subparsers(dest="memory_command", required=True)
    _add_memory_parser = memory_sub.add_parser
    memory_sub.add_parser = lambda name, **kwargs: _add_memory_parser(name, parents=[common], **kwargs)

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
        "--allow-dir",
        default=None,
        help="With a postmortem path, the directory the Archivist may read from "
        "(default: the postmortem's own directory). Secrets, databases, omen_store/ "
        "and .git/ are always refused.",
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


class _DiagnosticFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        text = f"{record.levelname.lower()}: {record.getMessage()}"
        if record.exc_info:
            text += "\n" + self.formatException(record.exc_info)
        return text


def _configure_logging(verbose: bool, quiet: bool) -> None:
    """Diagnostics go to stderr through `logging`; normal command output
    stays on stdout and is not affected by --verbose/--quiet."""
    level = logging.DEBUG if verbose else logging.ERROR if quiet else logging.WARNING
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_DiagnosticFormatter())
    root = logging.getLogger("omen")
    root.handlers[:] = [handler]
    root.setLevel(level)
    root.propagate = False


async def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.verbose and args.quiet:  # one given before the subcommand, one after
        parser.error("argument -q/--quiet: not allowed with argument -v/--verbose")
    command = " ".join(filter(None, [args.command, getattr(args, "memory_command", None)]))
    _configure_logging(args.verbose, args.quiet)
    if args.store:
        config.set_store_dir(args.store)
    log.debug("store: %s", config.STORE_DIR.resolve())
    log.debug("ollama host: %s", config.OLLAMA_HOST)

    start = time.perf_counter()
    try:
        result = args.func(args)
        if asyncio.iscoroutine(result):
            result = await result
    except Exception as e:  # runtime or model error: one line, not a traceback (B18)
        log.debug("traceback:", exc_info=True)
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        return EXIT_RUNTIME_ERROR
    finally:
        log.debug("%s finished in %.2fs", command, time.perf_counter() - start)
    return EXIT_OK if result is None else int(result)


def entrypoint() -> None:
    """Console-script entry point (`omen = omen.cli:entrypoint`)."""
    sys.exit(asyncio.run(main()))


if __name__ == "__main__":
    entrypoint()
