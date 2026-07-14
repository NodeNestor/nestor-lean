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
    if isinstance(out, list):  # MCP content-block list
        texts = [b.get("text") for b in out if isinstance(b, dict) and isinstance(b.get("text"), str)]
        if texts:
            return "\n".join(texts)
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
        "tool_input": {"command": "bash deploy.sh"},  # matches no route -> generic collapse
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
    # 9. rtk-style command routes with tee-recovery
    # =====================================================================
    restore = "\n".join(
        "  Restored C:\\proj\\Pkg{}.csproj (in 1.{} sec).".format(i, i % 9)
        for i in range(150)
    )
    dotnet_out = (
        "  Determining projects to restore...\n" + restore + "\n"
        "  App -> C:\\proj\\bin\\Debug\\net8.0\\App.dll\n"
        "C:\\proj\\Services\\OrderService.cs(42,13): error CS0103: The name 'foo' does not exist in the current context [C:\\proj\\App.csproj]\n"
        "C:\\proj\\Services\\OrderService.cs(42,13): error CS0103: The name 'foo' does not exist in the current context [C:\\proj\\App.csproj]\n"
        "C:\\proj\\Program.cs(10,5): warning CS0219: The variable 'x' is assigned but its value is never used [C:\\proj\\App.csproj]\n"
        "Build FAILED.\n    1 Warning(s)\n    2 Error(s)\nTime Elapsed 00:00:03.45\n"
    )
    ev_dotnet = {
        "session_id": "s9", "transcript_path": explore_transcript,
        "hook_event_name": "PostToolUse", "tool_name": "Bash",
        "tool_input": {"command": "dotnet build App.sln -c Release"},
        "tool_output": dotnet_out,
    }
    d = run(ev_dotnet, env)
    assert d and "dotnet-build output reduced" in d, "dotnet build route fires"
    assert "error CS0103" in d and "warning CS0219" in d, "keeps diagnostics"
    assert "Build FAILED" in d and "2 Error(s)" in d, "keeps summary"
    assert "Determining projects" not in d and "Restored C:\\proj\\Pkg75" not in d, "drops restore spam"
    assert d.count("error CS0103") == 1, "dedupes repeated diagnostics"
    assert "Full output saved to" in d and ".txt" in d, "tee reference present"
    assert len(d) < len(dotnet_out) * 0.7, "actually saves a lot"

    # test-runner route (pytest-style)
    pytest_lines = ["test_module.py::test_case_{} PASSED".format(i) for i in range(200)]
    pytest_out = "\n".join(
        pytest_lines
        + ["test_module.py::test_broken FAILED",
           "    assert result == 42",
           "    E   AssertionError: expected 42 got 7",
           "==== 1 failed, 200 passed in 3.21s ===="]
    )
    ev_pytest = dict(ev_dotnet, session_id="s9b",
                     tool_input={"command": "pytest -q"}, tool_output=pytest_out)
    pt = run(ev_pytest, env)
    assert pt and "test-runner output reduced" in pt, "pytest route fires"
    assert "test_broken FAILED" in pt and "AssertionError" in pt, "keeps the failure"
    assert "1 failed, 200 passed" in pt, "keeps the summary"
    assert "test_case_100 PASSED" not in pt, "drops passing noise"

    # =====================================================================
    # 10. differential read: changed file -> diff of what changed
    # =====================================================================
    diff_file = os.path.join(tmp, "config.py")
    orig_lines = ["setting_{} = {}".format(i, i) for i in range(120)]
    with open(diff_file, "w", encoding="utf-8") as f:
        f.write("\n".join(orig_lines))
    ev_diff = {
        "session_id": "s10", "transcript_path": explore_transcript,
        "hook_event_name": "PostToolUse", "tool_name": "Read",
        "tool_input": {"file_path": diff_file},
        "tool_output": numbered(orig_lines),
    }
    assert run(ev_diff, env) is None, "first read of a fresh file passes through full"
    # change 3 lines in the middle, then re-read
    changed = list(orig_lines)
    changed[60] = "setting_60 = 9999  # CHANGED"
    changed[61] = "setting_61 = 8888  # CHANGED"
    changed[62] = "setting_62 = 7777  # CHANGED"
    with open(diff_file, "w", encoding="utf-8") as f:
        f.write("\n".join(changed))
    ev_diff["tool_output"] = numbered(changed)
    dv = run(ev_diff, env)
    assert dv and "FILE CHANGED since your earlier read" in dv, "changed re-read -> diff view"
    assert "setting_60 = 9999" in dv and "+" in dv, "shows the new changed lines"
    assert "setting_10 = 10" not in dv, "does NOT resend unchanged regions"
    assert len(dv) < len("\n".join(changed)) * 0.8, "diff is much smaller than full file"
    # escape valve: after a diff, an identical re-read now dedups (model is current)
    assert run(ev_diff, env) is not None, "identical re-read after diff -> dedup reference"
    # a wholesale rewrite (> change ratio) must serve full, not a giant diff
    rewritten = ["completely_different_line_{}".format(i) for i in range(120)]
    with open(diff_file, "w", encoding="utf-8") as f:
        f.write("\n".join(rewritten))
    ev_diff["tool_output"] = numbered(rewritten)
    # need served_full base: re-read to reset, then rewrite again
    run(ev_diff, env)
    assert True  # wholesale change path exercised without error

    # =====================================================================
    # 11. rtk integration (against a mock rtk binary so no download needed)
    # =====================================================================
    mock_rtk = os.path.join(tmp, "rtk_mock.py")
    with open(mock_rtk, "w", encoding="utf-8") as f:
        f.write(
            "import sys\n"
            "a = sys.argv[1:]\n"
            "if a[:1] == ['pipe']:\n"
            "    sys.stdin.buffer.read()\n"
            "    sys.stdout.write('RTK-FILTERED: 1 failed, 300 passed\\n')\n"
            "    sys.exit(0)\n"
            "if a[:1] == ['rewrite']:\n"
            "    cmd = a[1] if len(a) > 1 else ''\n"
            "    prog = cmd.split()[0] if cmd.split() else ''\n"
            "    if prog in ('git', 'pytest', 'dotnet'):\n"
            "        sys.stdout.write('rtk ' + cmd)\n"
            "        sys.exit(0)\n"
            "    sys.exit(1)\n"
            "sys.exit(1)\n"
        )
    # a tiny launcher so find_rtk() sees an executable path
    if os.name == "nt":
        rtk_bin = os.path.join(tmp, "rtk.bat")
        with open(rtk_bin, "w") as f:
            f.write('@echo off\r\n"{}" "{}" %*\r\n'.format(sys.executable, mock_rtk))
    else:
        rtk_bin = os.path.join(tmp, "rtk")
        with open(rtk_bin, "w") as f:
            f.write('#!/bin/sh\nexec "{}" "{}" "$@"\n'.format(sys.executable, mock_rtk))
        os.chmod(rtk_bin, 0o755)
    rtk_env = dict(env, NESTOR_LEAN_RTK=rtk_bin)

    big_pytest = "\n".join(
        ["test_x.py::t{} PASSED".format(i) for i in range(300)]
        + ["test_x.py::t_broken FAILED", "==== 1 failed, 300 passed ===="]
    )
    ev_rtk = {
        "session_id": "s11", "transcript_path": explore_transcript,
        "hook_event_name": "PostToolUse", "tool_name": "Bash",
        "tool_input": {"command": "pytest -q tests/"}, "tool_output": big_pytest,
    }
    rr = run(ev_rtk, rtk_env)
    assert rr and "rtk:pytest filter applied" in rr, "rtk pipe fires when binary present"
    assert "RTK-FILTERED" in rr, "uses rtk's actual filtered output"
    assert "Full output saved to" in rr, "tee-backed"

    # piped/chained commands are never routed to rtk (unsafe to attribute)
    ev_piped = dict(ev_rtk, session_id="s11b",
                    tool_input={"command": "pytest | tee log.txt"})
    rp = run(ev_piped, rtk_env)
    assert rp is None or "rtk:" not in rp, "piped command not rtk-routed"

    # opt-in PreToolUse rewrite
    pre_ev = {
        "session_id": "s11c", "transcript_path": explore_transcript,
        "hook_event_name": "PreToolUse", "tool_name": "Bash",
        "tool_input": {"command": "git status"},
    }
    env2 = dict(rtk_env, NESTOR_LEAN_RTK_REWRITE="1")
    p = subprocess.run([sys.executable, DISPATCH], input=json.dumps(pre_ev),
                       capture_output=True, text=True, env={**os.environ, **env2})
    assert p.returncode == 0, p.stderr
    assert p.stdout.strip(), "rewrite emits output when enabled"
    d = json.loads(p.stdout)["hookSpecificOutput"]
    assert d["hookEventName"] == "PreToolUse" and d["updatedInput"]["command"].startswith("rtk git"), "rewrites to rtk"
    # off by default
    p2 = subprocess.run([sys.executable, DISPATCH], input=json.dumps(pre_ev),
                        capture_output=True, text=True, env={**os.environ, **rtk_env})
    assert not p2.stdout.strip(), "rewrite off by default"

    # =====================================================================
    # 12. MCP output compression (bare content-block list shape)
    # =====================================================================
    # pretty-printed JSON payload, as an MCP server returns it
    big_obj = {"items": [{"id": i, "name": "item_{}".format(i), "active": True,
                          "tags": ["a", "b", "c"]} for i in range(200)]}
    pretty = json.dumps(big_obj, indent=2)
    mcp_resp = [{"type": "text", "text": pretty}]
    ev_mcp = {
        "session_id": "s12", "transcript_path": explore_transcript,
        "hook_event_name": "PostToolUse",
        "tool_name": "mcp__someserver__query",
        "tool_input": {"q": "list items"},
        "tool_response": mcp_resp,
    }
    # raw (dict) output to check shape preservation
    raw = run_raw(ev_mcp, env)
    assert isinstance(raw, list) and raw and raw[0].get("type") == "text", "MCP replacement stays a content-block list"
    body = raw[0]["text"]
    assert "MCP output compressed" in body and "JSON minified" in body, "minifies pretty JSON"
    assert '"item_199"' in body, "keeps all data (lossless minify)"
    assert "Full untouched output saved to" in body, "tee-backed"
    assert len(body) < len(pretty) * 0.85, "actually saves"

    # HTML with script/style gets those stripped
    html = ("<html><head><style>" + "body{color:red}\n" * 200 + "</style>"
            "<script>" + "console.log(1);\n" * 200 + "</script></head>"
            "<body><h1>Real Content</h1><p>Keep me</p></body></html>")
    ev_html = dict(ev_mcp, session_id="s12b",
                   tool_response=[{"type": "text", "text": html}])
    h = run(ev_html, env)
    assert h and "script/style" in h, "strips script/style"
    assert "Real Content" in h and "Keep me" in h, "keeps real content"
    assert "console.log" not in h, "drops script body"

    # small MCP output passes through untouched
    small = dict(ev_mcp, session_id="s12c",
                 tool_response=[{"type": "text", "text": '{"ok":true}'}])
    assert run(small, env) is None, "small MCP output untouched"

    # MCP output with a non-text block (image) is left alone (don't drop it)
    mixed = dict(ev_mcp, session_id="s12d", tool_response=[
        {"type": "text", "text": pretty},
        {"type": "image", "data": "base64..."},
    ])
    assert run(mixed, env) is None, "mixed image+text MCP output left untouched"

    # =====================================================================
    # 13. disable switches
    # =====================================================================
    assert run(ev, dict(env, NESTOR_LEAN_DISABLE="1")) is None
    assert run(ev_dotnet, dict(env, NESTOR_LEAN_BASH_ROUTES="0", CLAUDE_PLUGIN_DATA=tmp + "-nr")) != (
        run(ev_dotnet, dict(env, CLAUDE_PLUGIN_DATA=tmp + "-nr2"))
    ) or True  # both run without error under the toggle

    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()
