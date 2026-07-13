#!/usr/bin/env python3
"""Print cumulative nestor-lean savings across all recorded sessions."""
import json
import os

base = os.environ.get("CLAUDE_PLUGIN_DATA") or os.path.join(
    os.path.expanduser("~"), ".nestor-lean"
)
sessions_dir = os.path.join(base, "sessions")

total_chars = 0
read_refs = 0
grep_compressions = 0
sessions = 0

if os.path.isdir(sessions_dir):
    for name in os.listdir(sessions_dir):
        try:
            with open(os.path.join(sessions_dir, name), "r", encoding="utf-8") as f:
                s = json.load(f)
        except Exception:
            continue
        sessions += 1
        total_chars += s.get("saved_chars", 0)
        read_refs += s.get("read_refs", 0)
        grep_compressions += s.get("grep_compressions", 0)

print("nestor-lean savings (state retained for ~48h per session)")
print("=" * 56)
print("Sessions tracked:      {}".format(sessions))
print("Duplicate reads deduped: {}".format(read_refs))
print("Grep outputs compressed: {}".format(grep_compressions))
print("Chars saved:           {:,}".format(total_chars))
print("~Tokens saved:         {:,}  (chars/4 estimate)".format(total_chars // 4))
