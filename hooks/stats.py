#!/usr/bin/env python3
"""Print cumulative nestor-lean savings across all recorded sessions."""
import json
import os

base = os.environ.get("CLAUDE_PLUGIN_DATA") or os.path.join(
    os.path.expanduser("~"), ".nestor-lean"
)
sessions_dir = os.path.join(base, "sessions")

totals = {
    "saved_chars": 0,
    "read_refs": 0,
    "diff_reads": 0,
    "read_collapses": 0,
    "grep_compressions": 0,
    "bash_collapses": 0,
    "bash_routes": 0,
    "rtk_pipes": 0,
    "rtk_rewrites": 0,
    "mcp_compressions": 0,
    "codemaps": 0,
}
sessions = 0

if os.path.isdir(sessions_dir):
    for name in os.listdir(sessions_dir):
        try:
            with open(os.path.join(sessions_dir, name), "r", encoding="utf-8") as f:
                s = json.load(f)
        except Exception:
            continue
        sessions += 1
        for k in totals:
            totals[k] += s.get(k, 0)

print("nestor-lean savings (state retained for ~48h per agent context)")
print("=" * 60)
print("Agent contexts tracked:   {}".format(sessions))
print("Duplicate reads -> refs:  {}".format(totals["read_refs"]))
print("Changed reads -> diffs:   {}".format(totals["diff_reads"]))
print("Codemaps served:          {}".format(totals["codemaps"]))
print("Read outputs collapsed:   {}".format(totals["read_collapses"]))
print("Grep outputs compressed:  {}".format(totals["grep_compressions"]))
print("MCP outputs compressed:   {}".format(totals["mcp_compressions"]))
print("rtk filters applied:      {}".format(totals["rtk_pipes"]))
print("rtk command rewrites:     {}".format(totals["rtk_rewrites"]))
print("Built-in routes fired:    {}".format(totals["bash_routes"]))
print("Bash outputs collapsed:   {}".format(totals["bash_collapses"]))
print("Chars saved:              {:,}".format(totals["saved_chars"]))
print("~Tokens saved:            {:,}  (chars/4 estimate)".format(totals["saved_chars"] // 4))
