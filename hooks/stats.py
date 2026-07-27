#!/usr/bin/env python3
"""Print cumulative nestor-lean savings across all recorded sessions."""
import json
import os

import switch

def candidate_bases():
    """Every place hook state could live, most authoritative first.

    Hooks get CLAUDE_PLUGIN_DATA from Claude Code, but /gain runs stats.py
    through Bash, which inherits neither CLAUDE_PLUGIN_DATA nor
    CLAUDE_PLUGIN_ROOT (the command body's ${CLAUDE_PLUGIN_ROOT} is expanded
    before the shell sees it). Reading only the env var — or only the legacy
    ~/.nestor-lean fallback — therefore reports zero on a real install, so
    also walk up from wherever this script actually lives:

        <config>/plugins/cache/<marketplace>/<plugin>/<version>/hooks/stats.py
        <config>/plugins/data/<plugin>-<marketplace>/sessions
    """
    seen = set()
    bases = []

    def add(path):
        if not path:
            return
        try:
            key = os.path.realpath(path)
        except OSError:
            key = os.path.abspath(path)
        if key in seen:
            return
        seen.add(key)
        bases.append(path)

    def add_siblings_of(start):
        """From any path inside the plugins tree, find plugins/data/nestor-lean*."""
        if not start:
            return
        d = os.path.abspath(start)
        while True:
            if os.path.basename(d) == "plugins":
                data = os.path.join(d, "data")
                if os.path.isdir(data):
                    for name in sorted(os.listdir(data)):
                        if name == "nestor-lean" or name.startswith("nestor-lean-"):
                            add(os.path.join(data, name))
                return
            parent = os.path.dirname(d)
            if parent == d:
                return
            d = parent

    def add_from_config(config_dir):
        add_siblings_of(os.path.join(config_dir, "plugins", "x"))

    add(os.environ.get("CLAUDE_PLUGIN_DATA"))

    # A plugin installed from a local/directory marketplace runs in place, so
    # CLAUDE_PLUGIN_ROOT (and this file) can sit outside the plugins tree —
    # go at the config dir directly too.
    add_from_config(os.environ.get("CLAUDE_CONFIG_DIR")
                    or os.path.join(os.path.expanduser("~"), ".claude"))
    add_siblings_of(os.environ.get("CLAUDE_PLUGIN_ROOT"))
    add_siblings_of(os.path.dirname(os.path.abspath(__file__)))
    add(os.path.join(os.path.expanduser("~"), ".nestor-lean"))
    return bases

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

counted = set()

for base in candidate_bases():
    sessions_dir = os.path.join(base, "sessions")
    if not os.path.isdir(sessions_dir):
        continue
    for name in sorted(os.listdir(sessions_dir)):
        path = os.path.join(sessions_dir, name)
        try:
            key = os.path.realpath(path)
        except OSError:
            key = os.path.abspath(path)
        if key in counted:
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                s = json.load(f)
        except Exception:
            continue
        counted.add(key)
        sessions += 1
        for k in totals:
            totals[k] += s.get(k, 0)

print("nestor-lean savings (state retained for ~48h per agent context)")
print("=" * 60)
if switch.is_disabled():
    # Otherwise a switched-off plugin just looks like a broken one.
    print("STATUS: OFF — nothing is being compressed (/nestor-lean:on to resume)")
    print("-" * 60)
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
