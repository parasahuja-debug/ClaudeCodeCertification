#!/usr/bin/env python3
"""
PreToolUse hook: stop Claude from touching .env files (secrets).

Contract with Claude Code:
  stdin  -> JSON: {"tool_name": "...", "tool_input": {...}, ...}
  exit 0 -> allow the tool call
  exit 2 -> BLOCK it; whatever we print to stderr is shown to Claude as the reason
  (any other exit code = hook error, and the tool call still runs -> avoid!)
"""
import json
import re
import subprocess
import sys
from pathlib import Path

AUDIT_SCRIPT = Path(__file__).resolve().parent / "audit_env.py"


def block(reason: str) -> None:
    """Deny the tool call and tell Claude why."""
    print(reason, file=sys.stderr)
    sys.exit(2)


def spawn_audit(raw: str) -> None:
    """Fire-and-forget: hand the raw payload to the background auditor. Never waits.

    - start_new_session: child is detached from the hook's process group.
    - stdout/stderr -> DEVNULL: child must NOT hold the hook's output pipes open,
      or Claude Code could wait on it (re-introducing the delay we're avoiding).
    - best-effort: if spawning fails, carry on -- auditing is observability,
      not enforcement, so it must never break the hook.
    """
    try:
        proc = subprocess.Popen(
            [sys.executable, str(AUDIT_SCRIPT)],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        proc.stdin.write(raw.encode("utf-8"))
        proc.stdin.close()  # EOF -> auditor's json.load() returns; we don't wait
    except OSError:
        pass


# 2026-11-14: replaced per-tool allow-list with a deny-list of free-text keys (below).
# Reason: an allow-list silently fails OPEN if a field is renamed (file_path -> file)
# or a new/MCP tool uses a different key -- nothing gets checked, call is allowed.
# FIELDS_BY_TOOL = {
#     "Read": ["file_path"],
#     "Edit": ["file_path"],
#     "Write": ["file_path"],
#     "NotebookEdit": ["notebook_path"],
#     "Grep": ["path", "glob"],
#     "Glob": ["pattern", "path"],
#     "Bash": ["command"],
# }
#
#
# def targets_for(tool_name: str, tool_input: dict) -> list[str]:
#     """Return the strings from this tool call that need checking."""
#     fields = FIELDS_BY_TOOL.get(tool_name, [])
#     return [str(tool_input[f]) for f in fields if tool_input.get(f)]


# Part 2: check EVERY top-level string field of tool_input, for any tool, EXCEPT
# keys known to hold free text (file contents, prose). Skipping those avoids false
# blocks like writing a .gitignore/README that merely *mentions* .env.
# Unknown key -> it gets checked (fails safe). Nested values are NOT walked here;
# the background scanner does the deep recursive pass.
FREE_TEXT_KEYS = frozenset({"content", "old_string", "new_string", "description"})


def targets_for(tool_name: str, tool_input: dict) -> list[str]:
    """Return the top-level string values that need checking (all but free text)."""
    return [
        value
        for key, value in tool_input.items()
        if key not in FREE_TEXT_KEYS and isinstance(value, str) and value
    ]


# Part 3: does a target name a .env file?
# `.env` must START a path segment (start of string, or after / space quote etc.)
# and END cleanly (end, separator, `.suffix`, or glob `*`). That boundary is what
# blocks .env / .env.local / **/.env* but allows .envrc, .venv/, os.environ.
# IGNORECASE: macOS filesystems are case-insensitive, so `.ENV` is the same file.
_SEG_START = r"(?:^|(?<=[\s/'\"=<>|;&(`:{,]))"
_SEG_END = r"(?=$|[\s/'\"|;&)<>`,}])"
ENV_RE = re.compile(
    _SEG_START + r"(\.env(?:\.[\w*.-]+|\*[\w*.-]*)?)" + _SEG_END, re.IGNORECASE
)
ALLOWED_NAMES = {".env.example"}  # values-free template, safe to read


def find_env_ref(text: str) -> str | None:
    """Return the first forbidden .env name found in text, else None."""
    for match in ENV_RE.finditer(text):
        name = match.group(1)
        if name.lower() not in ALLOWED_NAMES:
            return name
    return None


def main() -> None:
    # Part 1: read the tool call Claude Code hands us.
    # 2026-11-14: read raw text first -- stdin can only be read once, and we now
    # need the raw payload twice (parse it here + hand it to the auditor).
    # try:
    #     payload = json.load(sys.stdin)
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        # Fail CLOSED: a secrets guard that crashes must not let the call through.
        block("block_env hook: could not parse hook input; blocking to be safe.")

    # Background deep scan of EVERY call (blocked ones too) -- before deciding.
    spawn_audit(raw)

    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {}) or {}

    for target in targets_for(tool_name, tool_input):
        name = find_env_ref(target)
        if name:
            block(
                f"Blocked by project hook (block_env.py): {tool_name} touches '{name}'. "
                ".env files hold secrets and are off-limits to Claude. Do not try to "
                "access it another way. If you need a value from it, ask the user to "
                "export it as an environment variable instead."
            )

    sys.exit(0)  # nothing .env-related -> allow


if __name__ == "__main__":
    main()
