# Scan outputs, exit codes, pre-commit and CI

How to get `omen scan` results out of the terminal and into a pull request, a pre-commit hook or
a CI job. Added in production-plan Phase 7. Unit-tested with a fake runner; not yet run against a
live model (see `docs/PRODUCTION_PLAN.md`, "Pending live verification").

## Pre-commit

`omen scan --since HEAD --fail-on confirmed` scans the files changed since `HEAD` (staged,
unstaged and new) and fails when a finding is confirmed. This repository ships it as a
[pre-commit](https://pre-commit.com) hook (`.pre-commit-hooks.yaml`, id `omen-scan`). In the
repository you want to protect, add to `.pre-commit-config.yaml`:

```yaml
repos:
  - repo: https://github.com/<org>/Local-OMEN
    rev: <tag-or-commit>
    hooks:
      - id: omen-scan
```

Then `pre-commit install`. What to know before you rely on it:

- It needs a **running Ollama** with the Omen models pulled and a **seeded ledger** (`OMEN_HOME`,
  or `./omen_store` in the repository you commit in). If Ollama is down the hook exits 3 and the
  commit is blocked; skip it for one commit with `SKIP=omen-scan git commit`.
- It only runs when staged files include Python (`types: [python]`), and takes no file names:
  Omen picks the changed files itself.
- A scan takes seconds on a GPU and minutes on a CPU (see `docs/PRODUCTION_PLAN.md`, Phase 5). On a
  CPU machine, prefer running it in CI over blocking every commit.
- `language: python` makes pre-commit install Omen and its dependencies into its own environment.
  If Omen is already installed, use a local hook instead (this is the same command):

```yaml
repos:
  - repo: local
    hooks:
      - id: omen-scan
        name: omen scan
        entry: omen scan --since HEAD --fail-on confirmed
        language: system
        types: [python]
        pass_filenames: false
```

## Report formats

`omen scan [path] --format text|markdown|json|sarif [--output FILE] [--fail-on confirmed|unverified]`

| Format | What you get | Use it for |
|---|---|---|
| `text` (default) | Live progress, then the summary, on stdout, as before Phase 7 | A person at a terminal |
| `markdown` | A report with one section per finding: location, mechanism, reasoning, evidence, the rule, plus rejected and skipped chunks | A pull-request comment, a wiki page |
| `json` | Everything, stable keys, `schema_version` 1 | Scripts and dashboards |
| `sarif` | SARIF 2.1.0 | GitHub code scanning and other SARIF viewers |

**Where output goes.** For `markdown`, `json` and `sarif`, stdout carries **only the document**, so
`omen scan --format json | jq` works; the progress lines go to stderr. With `--output FILE` the
document is written to the file (missing folders are created), and stdout stays empty. With
`--format text --output FILE`, the progress still prints and the final summary goes to the file.
Errors in a machine format also go to stderr.

**An empty scan** (no changed Python files) still produces a valid, empty document in the machine
formats, so a CI step that uploads the file never fails because there was nothing to scan. The text
format prints only the scope line, as before.

**`--format`, `--output` and `--fail-on` need a full scan.** With `--dry-run` or
`--retrieval-only` they are a user error (exit 1), because those modes have no findings.

### JSON

Top level: `schema_version`, `tool` (`name`, `version`), `run_id`, `repo`, `scope`, `runner`,
`llm_model`, `embed_model`, `summary` (the run's counts and timings plus `findings` counts by
verdict), `incidents` (title, failure mechanism and rule for each incident a finding refers to),
`findings` and `skipped`. Each entry of `findings` has exactly the fields of the `Finding` model
(`incident_ref`, `file_path`, `start_line`, `end_line`, `symbol`, `similarity`, `verdict`,
`code_mechanism`, `reasoning`, `evidence`, `confidence`, `tool_calls`, `downgrade_reason`,
`stopped_reason`, ...), so `Finding(**entry)` rebuilds it. `skipped` lists chunks that a cap
(`--max-gated`, `--max-positives`) kept from being processed; they are never silently dropped.

### SARIF

- **One rule per incident** in the ledger (`ruleId` = the incident ref, e.g. `OMEN-001`; the rule
  carries the incident's title, failure mechanism and rule).
- **One result per `confirmed` or `unverified` finding**, at the chunk's file and line range
  (`%SRCROOT%`-relative `uri`, `startLine`, `endLine`), with the function name as a logical
  location. `confirmed` is level `error`, `unverified` is level `warning`.
- **The evidence is in the message**: the mechanism, the cited `path:line` evidence (or "none
  cited"), why a confirmation was downgraded, and the rule.
- A **`rejected`** finding means the Adjudicator ruled "not a repeat", so it is not an alert. It is
  in the json and markdown reports and in the database, not in SARIF.
- Each result has a stable `partialFingerprints` value (incident, file, symbol) so GitHub can track
  an alert across commits.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success. With `--fail-on`: no finding at or above the level |
| 1 | User error: bad arguments, a missing path, an empty ledger, a report option with `--dry-run` |
| 2 | `--fail-on` found a finding at or above the level |
| 3 | Runtime or model error (Ollama down, model missing, timeout) |

`--fail-on confirmed` fails on `confirmed` findings only. `--fail-on unverified` fails on
`confirmed` **and** `unverified` (an unverified finding is a likely repeat whose evidence could not
be validated). `rejected` findings never fail a run. Without `--fail-on`, findings never change the
exit code.

The report is written **before** the exit code is decided, so a failing CI step can still upload
it. A Triage-positive chunk that `--max-positives` kept from being investigated is not a finding,
so it cannot trip `--fail-on`; when that happens Omen prints a warning on stderr that the result
does not cover it. Raise `--max-positives` if that matters for your gate.

## Self-hosted GPU runner (example, not run)

GitHub-hosted runners have neither a GPU nor Ollama, so Omen's CI (`.github/workflows/ci.yml`) runs
only the unit tier. To scan pull requests with the real model, use a self-hosted runner that has:

1. a GPU (the 8 GB RTX 5050 class the project targets) and Ollama running as a service, with
   `gemma4:12b-it-q4_K_M` and `embeddinggemma` pulled;
2. Omen installed, and a persistent ledger directory set with `OMEN_HOME`, seeded once with
   `omen seed incidents.yaml && omen reindex`;
3. the runner label `gpu` in addition to `self-hosted`.

`docs/examples/omen-scan.workflow.yml` is a complete workflow for that setup. It checks out with full
history, runs `omen scan --since origin/<base> --format sarif --output omen.sarif --fail-on confirmed`,
and uploads the SARIF file with `github/codeql-action/upload-sarif` (with `if: always()`, so the
findings are uploaded even when `--fail-on` fails the scan step). It is **documented, not run**: it
sits under `docs/`, so GitHub never executes it, and it has not been tried on a runner. A unit test
checks that the file is valid YAML and that its scan command parses with the real CLI.

Security note: a self-hosted runner executes code from the pull request. Do not enable this on a
public repository that accepts pull requests from forks.
