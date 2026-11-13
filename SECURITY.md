# Security

## Reporting a vulnerability

Please report security problems **privately**, not in a public issue or pull request. On GitHub, use the
repository's **Security** tab, "Report a vulnerability" (a private security advisory). Include what you
found, how to reproduce it, and what it lets an attacker do. If that option is not available to you,
open a public issue that says only "I have a security report" and no details, and the maintainers will
arrange a private channel.

This is a small project without a dedicated security team: there is no response-time guarantee, but a
report will be read and acknowledged.

## Threat model in short

Omen reads proprietary source code and a written record of a team's own past failures, and an LLM acts on
that text. The full model, with evidence, is in `docs/complete info about project.md`, section 12.

**Assets:** the source code being scanned; the incident ledger (an attacker's roadmap if it leaks); the
integrity of the ledger (a poisoned memory affects every later scan); the host machine.

**Adversaries:** (a) a network observer or a third-party API, the main reason Omen is local; (b) the author
of scanned code, a postmortem, or a commit message, who can put instructions or crafted paths into text the
model reads; (c) a local user who can read the store directory.

### What is protected

- **No cloud model calls.** All generation goes to a local Ollama. ADK is configured not to use Vertex, and
  `GOOGLE_API_KEY` is removed from the environment before it is imported. Chroma telemetry is off and its
  default embedding model (which would download) is disabled. Unit tests check that constructing an agent and
  using Chroma open no network connection.
- **Path confinement.** `read_code`, `grep_symbol` and `read_file` resolve paths and refuse anything outside
  their root (traversal, absolute paths, symlinks). For `omen learn <postmortem>` the root is the
  postmortem's own folder (`--allow-dir` widens it); `.env`, `*.db`, `omen_store/` and `.git/` are always
  refused (B4).
- **Git argument injection closed.** A model-chosen commit id is validated and passed after
  `--end-of-options`, so it cannot be a git option such as `--output=<path>` (B3).
- **Bounded agents.** Tool calls, duplicate calls, tool time and per-call model time are capped by the
  orchestrator, not requested in a prompt. `--max-gated` and `--max-positives` bound a scan.
- **Read-only scan tools.** Only the `learn` tool sets can write, and only through `write_incident`, which
  validates `existing_ref` (B9).
- **Memory integrity.** A duplicate is merged, never overwritten (B8); every change is recorded in
  `incident_history`; `omen memory revert` undoes it; `omen doctor` detects index drift.
- **Evidence is checked in code.** A `confirmed` verdict is downgraded unless its cited `path:line` lines
  are real and were seen by the Investigator (B6).
- **Safe parsing.** YAML is read with `safe_load`.

### Known limits (open)

1. **Prompt injection through scanned code.** Chunk text goes into the Triage, Investigator and Adjudicator
   prompts. A comment addressed to the model could try to suppress a finding. Impact: false negatives, not
   code execution, because scan tools are read-only. Not mitigated beyond the system/user split.
2. **Memory poisoning through postmortems and commit messages.** The Archivist reads untrusted text and can
   write incidents. Mitigations: `--max-learn`, `--dry-run`, merge-not-overwrite, history and `memory
   revert`/`forget`. Review what `learn` wrote.
3. **Ledger and cache are unencrypted at rest** (`omen_store/`). Protect the directory with filesystem
   permissions.
4. **Ollama has no authentication.** It listens on loopback by default; exposing it (`OLLAMA_HOST=0.0.0.0`)
   exposes the model server.
5. **Supply chain.** Dependencies are pinned to exact versions in `pyproject.toml`, but there is no hash
   lock file, and model weights come from the Ollama registry by tag.
6. **Full network-off run not done.** The automated tests cover agent construction and Chroma; an
   end-to-end run with the network disabled has not been performed, and whether LiteLLM makes any outbound
   call at run time is not verified.
7. **Self-hosted CI runners.** A runner that scans pull requests executes content from them; do not use one
   for pull requests from forks (`docs/OUTPUTS.md`).
