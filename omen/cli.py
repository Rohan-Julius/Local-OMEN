"""The deterministic orchestrator. Pipeline stage order lives here; agentic
tool loops live inside individual roles (agents.py / runners.py, from Phase
6 on). PLAN.md: ADK is async, so the entry point is asyncio.run(main()) even
for today's synchronous subcommands.
"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from omen import librarian, scout, store, vectors


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


def _print_chunk_table(chunks) -> None:
    symbol_width = max(len(c.symbol) for c in chunks)
    for c in chunks:
        span = f"{c.start_line}-{c.end_line}"
        print(f"{c.file_path:40}  {c.symbol:{symbol_width}}  {span:>9}  {c.content_hash[:8]}")


def cmd_scan(args: argparse.Namespace) -> None:
    repo_path = Path(args.path)
    scope_result = scout.resolve_scope(repo_path, since=args.since, all_files=args.all)
    chunks = scout.chunk_files(scope_result.files, repo_root=repo_path.resolve())
    print(f"scope: {scope_result.scope}  ({len(scope_result.files)} file(s), {len(chunks)} chunk(s))")
    if not chunks:
        return

    if args.dry_run:
        _print_chunk_table(chunks)
        return

    if args.retrieval_only:
        conn = store.connect()
        gated, stats = librarian.run(conn, chunks)
        print(
            f"embed cache: {stats.n_cache_hits}/{stats.n_total} hits "
            f"({stats.n_cache_misses} miss(es) embedded)"
        )
        if not gated:
            print(f"No chunks cleared the similarity threshold ({librarian.SIMILARITY_THRESHOLD}).")
            return
        for gc in gated:
            c = gc.chunk
            print(f"\n{c.file_path}:{c.symbol}  ({c.start_line}-{c.end_line})")
            for cand in gc.candidates:
                print(f"    {cand.incident_ref}  {cand.similarity:.3f}  {cand.matched_variant}")
        return

    print("Full scan pipeline (Triage/Investigator/Adjudicator) isn't built yet - Phase 3 gives retrieval only.")
    print("Showing the chunk table `--dry-run` would produce:")
    _print_chunk_table(chunks)


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
    p_scan.set_defaults(func=cmd_scan)

    return parser


async def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    asyncio.run(main())
