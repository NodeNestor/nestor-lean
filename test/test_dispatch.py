#!/usr/bin/env python3
"""Standalone test suite for hooks/dispatch.py — no framework needed.

Runs the dispatcher as a subprocess exactly the way Claude Code does
(JSON on stdin, JSON or nothing on stdout), including a fake
rolling-context /lean/status server for the invalidation path.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
DISPATCH = os.path.join(HERE, "..", "hooks", "dispatch.py")


def run(payload, env_extra=None):
    env = dict(os.environ)
    env.pop("ROLLING_CONTEXT_PORT", None)
    env["NESTOR_LEAN_RC_URL"] = env.get("NESTOR_LEAN_RC_URL", "http://127.0.0.1:1")
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
    out = json.loads(p.stdout)["hookSpecificOutput"]["updatedToolOutput"]
    # normalize structured shapes to their text for assertions
    if isinstance(out, dict):
        f = out.get("file")
        if isinstance(f, dict) and isinstance(f.get("content"), str):
            return f["content"]
        for k in ("output", "content", "text", "result", "stdout"):
            if isinstance(out.get(k), str):
                return out[k]
    return out


def run_raw(payload, env_extra=None):
    env = dict(os.environ)
    env.pop("ROLLING_CONTEXT_PORT", None)
    env["NESTOR_LEAN_RC_URL"] = env.get("NESTOR_LEAN_RC_URL", "http://127.0.0.1:1")
    env.update(env_extra or {})
    p = subprocess.run(
        [sys.executable, DISPATCH], input=json.dumps(payload),
        capture_output=True, text=True, env=env,
    )
    assert p.returncode == 0, p.stderr
    if not p.stdout.strip():
        return None
    return json.loads(p.stdout)["hookSpecificOutput"]["updatedToolOutput"]


def write_transcript(path, texts):
    """Minimal Claude Code transcript JSONL with assistant text entries."""
    with open(path, "w", encoding="utf-8") as f:
        for t in texts:
            f.write(json.dumps({
                "type": "assistant",
                "message": {"role": "assistant",
                            "content": [{"type": "text", "text": t}]},
            }) + "\n")


def numbered(body_lines, start=1):
    return "\n".join("{:>6}→{}".format(i + start, l) for i, l in enumerate(body_lines))


class FakeRC(BaseHTTPRequestHandler):
    last_injection_ts = 0.0

    def do_GET(self):
        body = json.dumps({
            "status": "ok",
            "last_injection_ts": FakeRC.last_injection_ts,
            "stored_compressions": 1,
        }).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def main():
    tmp = tempfile.mkdtemp(prefix="nestor-lean-test-")
    explore_transcript = os.path.join(tmp, "explore.jsonl")
    debug_transcript = os.path.join(tmp, "debug.jsonl")
    write_transcript(explore_transcript, [
        "Let me look around the codebase to understand how routing works.",
    ])
    write_transcript(debug_transcript, [
        "There's an exception in the logs — tracing the traceback to find the failing call.",
    ])
    env = {"CLAUDE_PLUGIN_DATA": tmp}

    # =====================================================================
    # 1. Read dedup cycle (unchanged from v0.1)
    # =====================================================================
    target = os.path.join(tmp, "notes.txt")
    body = "\n".join("note line {} with some real content".format(i) for i in range(120))
    with open(target, "w", encoding="utf-8") as f:
        f.write(body)
    ev = {
        "session_id": "s1", "transcript_path": explore_transcript,
        "hook_event_name": "PostToolUse", "tool_name": "Read",
        "tool_input": {"file_path": target}, "tool_output": numbered(body.splitlines()),
    }
    assert run(ev, env) is None, "first read passes through"
    note = run(ev, env)
    assert note and "Duplicate read skipped" in note, "second read -> reference"
    assert target in note and "digest" in note and "lines" in note, "note must orient"
    assert run(ev, env) is None, "escape valve: read after note is full"
    assert run(ev, env) is not None, "then dedups again"

    # different agent (different transcript) does NOT share state
    ev_agent2 = dict(ev, transcript_path=debug_transcript)
    assert run(ev_agent2, env) is None, "other agent context starts fresh"

    # =====================================================================
    # 2. PreCompact clears knowledge; SessionEnd deletes state
    # =====================================================================
    run({"hook_event_name": "PreCompact", "session_id": "s1",
         "transcript_path": explore_transcript, "trigger": "auto"}, env)
    assert run(ev, env) is None, "after PreCompact the next read is full again"
    assert run(ev, env) is not None, "and dedup resumes after that"
    run({"hook_event_name": "SessionEnd", "session_id": "s1",
         "transcript_path": explore_transcript}, env)
    assert run(ev, env) is None, "after SessionEnd dedup knowledge is cleared -> full read"

    # =====================================================================
    # 3. rolling-context invalidation
    # =====================================================================
    srv = HTTPServer(("127.0.0.1", 0), FakeRC)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    rc_env = dict(env, NESTOR_LEAN_RC_URL="http://127.0.0.1:{}".format(srv.server_port))
    rc_file = os.path.join(tmp, "rc_test.txt")
    with open(rc_file, "w") as f:
        f.write(body)
    ev_rc = dict(ev, tool_input={"file_path": rc_file}, session_id="s-rc")
    FakeRC.last_injection_ts = 0.0
    assert run(ev_rc, rc_env) is None, "first read records"
    assert run(ev_rc, rc_env) is not None, "no injection since -> reference OK"
    assert run(ev_rc, rc_env) is None, "escape valve"
    # a compression injection happens NOW -> next dedup opportunity must
    # serve full content instead of a reference. Wait out the probe cache.
    time.sleep(11)
    FakeRC.last_injection_ts = time.time()
    assert run(ev_rc, rc_env) is None, "read after injection serves full (record refreshed)"
    srv.shutdown()

    # =====================================================================
    # 4. codemap: exploring -> map; debugging -> full; re-read -> full
    # =====================================================================
    code_file = os.path.join(tmp, "service.py")
    chunks = []
    for i in range(40):
        chunks.append("class Service{}:".format(i))
        chunks.append("    def handle_{}(self, request):".format(i))
        for j in range(12):
            chunks.append("        value_{} = compute(request, {})".format(j, j))
        chunks.append("        return value_0")
    code_body = "\n".join(chunks)
    with open(code_file, "w", encoding="utf-8") as f:
        f.write(code_body)
    ev_code = {
        "session_id": "s2", "transcript_path": explore_transcript,
        "hook_event_name": "PostToolUse", "tool_name": "Read",
        "tool_input": {"file_path": code_file},
        "tool_output": numbered(code_body.splitlines()),
    }
    m = run(ev_code, env)
    assert m and "STRUCTURAL MAP" in m, "big code file while exploring -> codemap"
    assert "class Service0:" in m and "def handle_0" in m, "signatures kept"
    assert "implementation" in m, "elision markers present"
    assert "compute(request, 3)" not in m, "bodies elided"
    assert run(ev_code, env) is None, "re-read after map -> full content"

    # debugging intent -> never a codemap
    ev_code_dbg = dict(ev_code, transcript_path=debug_transcript, session_id="s3")
    assert run(ev_code_dbg, env) is None, "debug intent -> full code read"

    # codemap disabled by env
    ev_code_off = dict(ev_code, session_id="s4",
                       tool_input={"file_path": code_file})
    r = run(ev_code_off, dict(env, NESTOR_LEAN_CODEMAP="0", CLAUDE_PLUGIN_DATA=tmp + "-off"))
    assert r is None, "codemap can be disabled"

    # =====================================================================
    # 5. Read duplicate-collapse for non-code files
    # =====================================================================
    log_file = os.path.join(tmp, "app.log")
    log_lines = (["boot ok"] + ["WARN retry queue full"] * 200
                 + ["shutdown"] + ["unique {}".format(i) for i in range(30)])
    with open(log_file, "w", encoding="utf-8") as f:
        f.write("\n".join(log_lines))
    ev_log = {
        "session_id": "s5", "transcript_path": explore_transcript,
        "hook_event_name": "PostToolUse", "tool_name": "Read",
        "tool_input": {"file_path": log_file},
        "tool_output": numbered(log_lines),
    }
    r = run(ev_log, env)
    assert r and "repetitive content collapsed" in r, "dup-heavy log collapses"
    assert "repeats 199x" in r and "through line 201" in r, "marker keeps numbering understandable"
    assert "unique 29" in r, "unique lines survive"

    # =====================================================================
    # 6. Grep: compresses, but never for error hunts
    # =====================================================================
    dup = "src/app.py:{}:    logger.info('retrying request')"
    uniq = "src/app.py:{}:    unique_call_{}()"
    glines = [dup.format(i + 1) for i in range(60)] + [uniq.format(100 + i, i) for i in range(40)]
    gtext = "\n".join(glines * 3)
    ev_grep = {
        "session_id": "s6", "transcript_path": explore_transcript,
        "hook_event_name": "PostToolUse", "tool_name": "Grep",
        "tool_input": {"pattern": "logger", "output_mode": "content"},
        "tool_output": gtext,
    }
    g = run(ev_grep, env)
    assert g and "grep output compressed" in g and "repeats" in g and "capped" in g
    ev_grep_err = dict(ev_grep, tool_input={"pattern": "TimeoutError", "output_mode": "content"})
    assert run(ev_grep_err, env) is None, "error-hunting grep passes through"
    ev_grep_files = dict(ev_grep, tool_input={"pattern": "logger"})
    assert run(ev_grep_files, env) is None, "files_with_matches passes through"

    # =====================================================================
    # 7. Bash duplicate collapse
    # =====================================================================
    bash_out = "\n".join(["Restoring packages..."] * 300 + ["Build succeeded.", "0 Warning(s)"] * 2
                         + ["step {}".format(i) for i in range(50)])
    ev_bash = {
        "session_id": "s7", "transcript_path": explore_transcript,
        "hook_event_name": "PostToolUse", "tool_name": "Bash",
        "tool_input": {"command": "dotnet build"},
        "tool_output": bash_out,
    }
    b = run(ev_bash, env)
    assert b and "command output collapsed" in b and "repeats 299x" in b
    assert "step 49" in b, "unique lines survive"
    small_bash = dict(ev_bash, tool_output="ok")
    assert run(small_bash, env) is None, "small command output untouched"

    # =====================================================================
    # 8. shape preservation: live Read payloads carry a structured
    #    tool_response; the replacement must come back in the SAME shape
    #    (Claude Code validates it against the tool's output schema).
    # =====================================================================
    shaped = os.path.join(tmp, "shaped.txt")
    with open(shaped, "w", encoding="utf-8") as f:
        f.write(body)
    ev_shaped = {
        "session_id": "s8", "transcript_path": explore_transcript,
        "hook_event_name": "PostToolUse", "tool_name": "Read",
        "tool_input": {"file_path": shaped},
        "tool_response": {
            "type": "text",
            "file": {"filePath": shaped, "content": body,
                     "numLines": body.count("\n") + 1,
                     "startLine": 1, "totalLines": body.count("\n") + 1},
        },
    }
    assert run(ev_shaped, env) is None, "first shaped read passes through"
    raw = run_raw(ev_shaped, env)
    assert isinstance(raw, dict) and raw.get("type") == "text", "dict in -> dict out"
    assert "Duplicate read skipped" in raw["file"]["content"], "note inside file.content"
    assert raw["file"]["numLines"] == raw["file"]["content"].count("\n") + 1, "numLines consistent"

    # =====================================================================
    # 9. disable switch
    # =====================================================================
    assert run(ev, dict(env, NESTOR_LEAN_DISABLE="1")) is None

    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()
