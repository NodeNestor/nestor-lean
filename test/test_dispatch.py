#!/usr/bin/env python3
"""Standalone smoke test for hooks/dispatch.py — no framework needed.

Runs the dispatcher as a subprocess exactly the way Claude Code does
(JSON on stdin, JSON or nothing on stdout).
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DISPATCH = os.path.join(HERE, "..", "hooks", "dispatch.py")


def run(payload, env_extra=None):
    env = dict(os.environ)
    env.update(env_extra or {})
    p = subprocess.run(
        [sys.executable, DISPATCH],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
    )
    assert p.returncode == 0, p.stderr
    if not p.stdout.strip():
        return None
    return json.loads(p.stdout)["hookSpecificOutput"]["updatedToolOutput"]


def main():
    tmp = tempfile.mkdtemp(prefix="nestor-lean-test-")
    env = {"CLAUDE_PLUGIN_DATA": tmp}

    # --- Read dedup ---------------------------------------------------
    target = os.path.join(tmp, "big_file.py")
    body = "\n".join("line {} of a reasonably long file".format(i) for i in range(200))
    with open(target, "w", encoding="utf-8") as f:
        f.write(body)

    read_event = {
        "session_id": "sess-test-1",
        "hook_event_name": "PostToolUse",
        "tool_name": "Read",
        "tool_input": {"file_path": target},
        "tool_output": body,
    }

    r1 = run(read_event, env)
    assert r1 is None, "first read must pass through, got: %r" % r1

    r2 = run(read_event, env)
    assert r2 is not None and "Duplicate read skipped" in r2, "second read must dedup"

    r3 = run(read_event, env)
    assert r3 is None, "read after a reference must serve full content"

    r4 = run(read_event, env)
    assert r4 is not None, "and the one after that dedups again"

    # changed file must always pass through
    with open(target, "a", encoding="utf-8") as f:
        f.write("\nnew line")
    read_event["tool_output"] = body + "\nnew line"
    r5 = run(read_event, env)
    assert r5 is None, "changed file must pass through"

    # different session must not share state
    other = dict(read_event, session_id="sess-test-2")
    assert run(other, env) is None, "new session starts fresh"

    # small files never dedup
    small = os.path.join(tmp, "small.txt")
    with open(small, "w") as f:
        f.write("tiny")
    small_event = dict(read_event, tool_input={"file_path": small}, tool_output="tiny")
    assert run(small_event, env) is None
    assert run(small_event, env) is None, "small file must never dedup"

    # --- Grep compression ----------------------------------------------
    dup_line = "src/app.py:{}:    logger.info('retrying request')"
    uniq_line = "src/app.py:{}:    unique_call_number_{}()"
    lines = []
    for i in range(60):
        lines.append(dup_line.format(i + 1))
    for i in range(40):
        lines.append(uniq_line.format(100 + i, i))
    grep_text = "\n".join(lines) * 3  # make it big enough to trigger

    grep_event = {
        "session_id": "sess-test-1",
        "hook_event_name": "PostToolUse",
        "tool_name": "Grep",
        "tool_input": {"pattern": "retry", "output_mode": "content"},
        "tool_output": grep_text,
    }
    g1 = run(grep_event, env)
    assert g1 is not None and "grep output compressed" in g1, "large grep must compress"
    assert len(g1) < len(grep_text) * 0.8, "must actually save space"
    assert "repeats" in g1, "identical matches must be collapsed with counts"
    assert "capped" in g1, "per-file cap note must appear"

    # files_with_matches mode passes through untouched
    fw = dict(grep_event, tool_input={"pattern": "retry"})
    assert run(fw, env) is None

    # small grep passes through
    small_grep = dict(grep_event, tool_output="src/app.py:1:x")
    assert run(small_grep, env) is None

    # disable switch
    env_off = dict(env, NESTOR_LEAN_DISABLE="1")
    assert run(read_event, env_off) is None

    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()
