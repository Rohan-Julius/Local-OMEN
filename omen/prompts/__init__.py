"""Role prompts, shipped as package data so the CLI works from any
directory. Previously they were read from a cwd-relative `prompts/`
folder, which forced every command to run from the project root.

The `.txt` files here are the prompts verbatim. Ground rule 3 of
docs/PRODUCTION_PLAN.md applies to any change to them.
"""
from __future__ import annotations

import hashlib
from importlib import resources


def load_prompt(name: str) -> str:
    """Return the text of one prompt file, e.g. `load_prompt("triage.txt")`."""
    return resources.files(__name__).joinpath(name).read_text(encoding="utf-8")


def sha256(name: str) -> str:
    """SHA-256 of the prompt text as loaded (LF line endings), the value the
    Progress log and `omen eval` results record for each prompt."""
    return hashlib.sha256(load_prompt(name).encode("utf-8")).hexdigest()
