#!/usr/bin/env python3
"""
Background .env auditor -- launched DETACHED by block_env.py, never by Claude Code.

  stdin  -> the same raw hook JSON block_env.py received
  effect -> recursively scan EVERY key and value (free text included);
            append a line to the audit log ONLY if a .env reference is found.
  never blocks anything: by the time this runs, the hook has already decided.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Same regex as the hook -> one definition of "what counts as .env".
# (Works because Python puts this script's own folder on sys.path.)
from block_env import find_env_ref

# .claude/audit/env-audit.jsonl  (gitignored; one JSON object per line)
AUDIT_LOG = Path(__file__).resolve().parent.parent / "audit" / "env-audit.jsonl"


def walk(obj, path="tool_input"):
    """Yield (where, text) for every key and string value, at any depth."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield f"{path}.<key>", str(key)  # key names are checked too
            yield from walk(value, f"{path}.{key}")
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            yield from walk(item, f"{path}[{i}]")
    elif isinstance(obj, str):
        yield path, obj
    # numbers / bools / None can't hold a filename -> ignored


def main() -> None:
    # Part 2: scan and log. Any failure -> exit quietly; an auditor must never
    # make noise or interfere (it's observability, not enforcement).
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return

    tool_input = payload.get("tool_input", {}) or {}
    hits = []
    for where, text in walk(tool_input):
        name = find_env_ref(text)
        if name:
            hits.append({"where": where, "name": name})  # location, never the raw value

    if not hits:
        return  # log ONLY when something is suspicious

    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tool_name": payload.get("tool_name"),
        "tool_use_id": payload.get("tool_use_id"),
        "session_id": payload.get("session_id"),
        "hits": hits,
    }
    AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(AUDIT_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")  # one write = one whole line, safe for parallel appends


if __name__ == "__main__":
    main()
