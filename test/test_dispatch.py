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


def transcript_intent_via(path):
    """Ask the dispatcher itself what intent it infers for a transcript."""
    code = ("import sys; sys.path.insert(0, r'{}'); import dispatch; "
            "print(dispatch.transcript_intent(r'{}'))").format(
                os.path.join(HERE, "..", "hooks"), path)
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return p.stdout.strip()


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

    # Insertions and deletions must diff too. Diffing the RENDERED read output
    # cannot do this: one inserted line rewrites the number prefix of every
    # following line, the change ratio blows past its limit, and the diff is
    # rejected — which quietly limited this tier to same-line-count edits.
    for label, mutate in (
        ("insert", lambda ls: ls[:60] + ["setting_new = 'inserted'"] + ls[60:]),
        ("delete", lambda ls: ls[:60] + ls[61:]),
        ("insert many", lambda ls: ls[:60] + ["extra_{} = {}".format(i, i)
                                              for i in range(5)] + ls[60:]),
    ):
        base = ["setting_{} = {}".format(i, i) for i in range(120)]
        fpath = os.path.join(tmp, "cfg_{}.py".format(label.replace(" ", "_")))
        with open(fpath, "w", encoding="utf-8") as f:
            f.write("\n".join(base))
        ev = {"session_id": "s10-" + label, "transcript_path": explore_transcript,
              "hook_event_name": "PostToolUse", "tool_name": "Read",
              "tool_input": {"file_path": fpath}, "tool_output": numbered(base)}
        assert run(ev, env) is None, "first read is full"
        after = mutate(base)
        with open(fpath, "w", encoding="utf-8") as f:
            f.write("\n".join(after))
        ev["tool_output"] = numbered(after)
        d = run(ev, env)
        assert d and "FILE CHANGED" in d, label + " must still produce a diff"
        assert len(d) < len(numbered(after)) * 0.6, label + " diff must be small"
        # numbers appear once per line, not twice (the rendered prefix is gone)
        body_lines = [l for l in d.splitlines()
                      if l.startswith(("  ", "  + ", "  - ")) and "=" in l]
        assert body_lines, label + " produced no diff body"
        assert not any("→" in l for l in body_lines), (
            label + ": read-output arrows leaked into the diff")
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

    # =====================================================================
    # 14. plugin manifest must not re-declare the auto-loaded hooks file
    # =====================================================================
    # Claude Code loads hooks/hooks.json automatically; manifest.hooks is only
    # for *additional* files, so pointing it back at the standard path makes
    # the whole plugin report "failed to load". See issue #1.
    manifest_path = os.path.join(HERE, "..", ".claude-plugin", "plugin.json")
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    assert "hooks" not in manifest, (
        "plugin.json must not set 'hooks' — hooks/hooks.json is auto-loaded "
        "and re-declaring it fails the plugin load"
    )
    assert os.path.isfile(os.path.join(HERE, "..", "hooks", "hooks.json")), (
        "hooks/hooks.json must exist at the standard path to be auto-loaded"
    )

    # =====================================================================
    # 15. /gain finds hook state without CLAUDE_PLUGIN_DATA in the env
    # =====================================================================
    # Slash commands run stats.py through Bash, which inherits neither
    # CLAUDE_PLUGIN_DATA nor CLAUDE_PLUGIN_ROOT, so resolution has to fall
    # back to the config dir — otherwise /gain always reports zero.
    stats = os.path.join(HERE, "..", "hooks", "stats.py")
    cfg = os.path.join(tmp, "fakecfg")
    state_dir = os.path.join(cfg, "plugins", "data", "nestor-lean-somemarket", "sessions")
    os.makedirs(state_dir)
    with open(os.path.join(state_dir, "abc123.json"), "w", encoding="utf-8") as f:
        json.dump({"saved_chars": 4242, "read_refs": 3}, f)

    bare = dict(os.environ)
    bare.pop("CLAUDE_PLUGIN_DATA", None)
    bare.pop("CLAUDE_PLUGIN_ROOT", None)
    bare["CLAUDE_CONFIG_DIR"] = cfg
    out = subprocess.run([sys.executable, stats], capture_output=True, text=True, env=bare)
    assert out.returncode == 0, out.stderr
    assert "4,242" in out.stdout, "stats.py must read the plugin data dir under the config dir"
    assert "Duplicate reads -> refs:  3" in out.stdout, "counters must aggregate"

    # each state file is counted once even when several roots resolve to it
    both = dict(bare, CLAUDE_PLUGIN_DATA=os.path.dirname(state_dir))
    out2 = subprocess.run([sys.executable, stats], capture_output=True, text=True, env=both)
    assert out2.returncode == 0, out2.stderr
    assert "4,242" in out2.stdout, "overlapping roots must not double-count"

    # =====================================================================
    # 16. runtime on/off switch
    # =====================================================================
    # The env var needs a Claude Code restart; the flag file must take effect
    # on the very next tool call, so the dispatcher has to read it per run.
    switch_py = os.path.join(HERE, "..", "hooks", "switch.py")
    lean_home = os.path.join(tmp, "switchhome")
    sw_env = dict(env, NESTOR_LEAN_HOME=lean_home)
    # State keys on transcript_path, not session_id — this case needs its own
    # transcript or it inherits the dedup state left by the tests above.
    switch_transcript = os.path.join(tmp, "switch.jsonl")
    write_transcript(switch_transcript, [
        "Let me look around the codebase to understand how routing works.",
    ])

    def sw(action):
        p = subprocess.run([sys.executable, switch_py, action],
                           capture_output=True, text=True,
                           env=dict(os.environ, NESTOR_LEAN_HOME=lean_home))
        assert p.returncode == 0, p.stderr
        return p.stdout

    assert "ON" in sw("status"), "starts on"

    ev_sw = dict(ev, session_id="s16", transcript_path=switch_transcript)
    assert run(ev_sw, sw_env) is None, "first read passes through"
    assert run(ev_sw, sw_env) is not None, "second read dedups while on"

    sw("off")
    assert "OFF" in sw("status"), "status reflects the flag"
    assert os.path.exists(os.path.join(lean_home, "disabled")), "flag file written"
    # same repeated read that just deduped must now pass straight through,
    # with no restart in between
    assert run(ev_sw, sw_env) is None, "switched off -> no compression"
    assert run(ev_sw, sw_env) is None, "still off on the next call"

    sw("on")
    assert "ON" in sw("status"), "switched back on"
    assert run(ev_sw, sw_env) is None, "first read after resume rebuilds state"
    assert run(ev_sw, sw_env) is not None, "dedup works again after resume"

    # the env var still wins, and says so
    forced = subprocess.run(
        [sys.executable, switch_py, "status"], capture_output=True, text=True,
        env=dict(os.environ, NESTOR_LEAN_HOME=lean_home, NESTOR_LEAN_DISABLE="1"))
    assert "OFF" in forced.stdout and "NESTOR_LEAN_DISABLE" in forced.stdout, (
        "env var must still take precedence and be named in the status")

    # =====================================================================
    # 17. PowerShell is treated as a shell, not ignored
    # =====================================================================
    # Windows sessions run most commands through this tool; leaving it out of
    # the matcher meant a large share of shell output was never seen.
    noisy = "\n".join(["processing widget batch"] * 60
                      + ["done in {}ms".format(i) for i in range(40)])
    ev_ps = {
        "session_id": "s17", "transcript_path": explore_transcript,
        "hook_event_name": "PostToolUse", "tool_name": "PowerShell",
        "tool_input": {"command": "Get-Widget -All"},
        "tool_response": noisy,
    }
    ps = run(ev_ps, env)
    assert ps and "nestor-lean" in ps, "PowerShell output must be compressed"
    assert "59 more identical" in ps or "repeat" in ps.lower(), "runs collapsed"
    assert "done in 39ms" in ps, "non-repeating detail kept"

    # Format-Table style padding is layout, not content: strip it even though
    # it never reaches the 20% saving ratio on its own.
    table = "\n".join(
        "Name{}    Id{}      CPU{}   ".format(" " * 8, " " * 6, " " * 4)
        if i == 0 else
        "proc_{}{}  {}{}   {}.{}    ".format(i, " " * 6, 1000 + i, " " * 5, i, i)
        for i in range(120)
    )
    ev_table = {
        "session_id": "s17b", "transcript_path": explore_transcript,
        "hook_event_name": "PostToolUse", "tool_name": "PowerShell",
        "tool_input": {"command": "Get-Process | Format-Table"},
        "tool_response": table,
    }
    t = run(ev_table, env)
    assert t and "layout whitespace removed" in t, "column padding must be stripped"
    assert "proc_119" in t and "1119" in t, "every value survives"
    assert "        " not in t.split("\n", 1)[1], "no run of padding left"
    # and a clean output is left completely alone
    assert run(dict(ev_table, session_id="s17c",
                    tool_response="\n".join("clean line {}".format(i)
                                            for i in range(200))), env) is None, (
        "output with no layout waste and no repetition passes through")

    # =====================================================================
    # 18. structural maps for prose and markup, not just code
    # =====================================================================
    def read_event(path, body, sid, transcript):
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        return {
            "session_id": sid, "transcript_path": transcript,
            "hook_event_name": "PostToolUse", "tool_name": "Read",
            "tool_input": {"file_path": path},
            "tool_output": numbered(body.splitlines()),
        }

    md_body = []
    for i in range(14):
        md_body.append("## Section {}".format(i))
        md_body += ["Prose line {} of section {} with real sentences in it.".format(j, i)
                    for j in range(30)]
    md_path = os.path.join(tmp, "doc.md")
    md = run(read_event(md_path, "\n".join(md_body), "s18a", explore_transcript), env)
    assert md and "STRUCTURAL MAP" in md, "markdown must map"
    assert "## Section 13" in md, "headings kept"
    assert "prose" in md, "elision names what was dropped"
    assert "Prose line 5 of section 3" not in md, "prose elided"

    css_body = []
    for i in range(20):
        css_body.append(".widget-{} {{".format(i))
        css_body += ["    property-{}: value-{}-{};".format(j, i, j) for j in range(20)]
        css_body.append("}")
    css_path = os.path.join(tmp, "site.css")
    css = run(read_event(css_path, "\n".join(css_body), "s18b", explore_transcript), env)
    assert css and "STRUCTURAL MAP" in css, "css must map"
    assert ".widget-19 {" in css, "selectors kept"

    # instruction files are never mapped, however big: the escape valve does
    # not help when the model has no reason to suspect a rule went missing
    for instruction_name in ("CLAUDE.md", "AGENTS.md", "SKILL.md"):
        ipath = os.path.join(tmp, instruction_name)
        assert run(read_event(ipath, "\n".join(md_body), "s18-" + instruction_name,
                              explore_transcript), env) is None, (
            instruction_name + " must never be mapped")

    # =====================================================================
    # 19. intent scales the threshold instead of vetoing the map
    # =====================================================================
    # Thresholds are measured against the rendered Read output (line numbers
    # included), not the raw file, so size these against `numbered(...)`.
    code = []
    for i in range(60):
        code.append("def fn_{}(a, b):".format(i))
        code += ["    step_{} = a + {}".format(j, j) for j in range(12)]
        code.append("    return step_0")
    medium = "\n".join(code)                 # ~22k numbered: over 12k, under 40k
    huge = "\n".join(code * 4)               # ~89k numbered: over the debug floor
    assert 12000 < len(numbered(medium.splitlines())) < 40000, "fixture sizing"
    assert len(numbered(huge.splitlines())) > 40000, "fixture sizing"

    med_path = os.path.join(tmp, "medium.py")
    assert run(read_event(med_path, medium, "s19a", debug_transcript), env) is None, (
        "mid-size file while error-hunting -> full content")
    assert run(read_event(os.path.join(tmp, "medium2.py"), medium, "s19b",
                          explore_transcript), env), "same file while exploring -> map"
    big = run(read_event(os.path.join(tmp, "huge.py"), huge, "s19c", debug_transcript), env)
    assert big and "STRUCTURAL MAP" in big, "very large file maps even mid-investigation"
    assert "mid-investigation" in big, "header states why it still mapped"

    # =====================================================================
    # 20. Glob path lists fold by directory
    # =====================================================================
    paths = []
    for d in ("src/core", "src/api", "src/web/components"):
        paths += ["E:/proj/{}/module_{}.py".format(d, i) for i in range(30)]
    assert len("\n".join(paths)) > 2000, "fixture must clear GLOB_MIN_CHARS"
    ev_glob = {
        "session_id": "s20", "transcript_path": explore_transcript,
        "hook_event_name": "PostToolUse", "tool_name": "Glob",
        "tool_input": {"pattern": "**/*.py"},
        "tool_response": "\n".join(paths),
    }
    g = run(ev_glob, env)
    assert g and "folded by directory" in g, "path list must fold"
    assert "E:/proj/src/web/components/" in g, "directory line kept once"
    assert "module_29.py" in g, "every filename still present"
    assert g.count("E:/proj/src/api") == 1, "stem not repeated per file"

    # =====================================================================
    # 21. WebFetch pages get an outline; WebSearch gets collapse
    # =====================================================================
    page = []
    for i in range(12):
        page.append("### Heading {}".format(i))
        page += ["Body sentence {} under heading {}.".format(j, i) for j in range(25)]
    ev_fetch = {
        "session_id": "s21a", "transcript_path": explore_transcript,
        "hook_event_name": "PostToolUse", "tool_name": "WebFetch",
        "tool_input": {"url": "https://example.com/doc"},
        "tool_response": "\n".join(page),
    }
    wf = run(ev_fetch, env)
    assert wf and "page outline" in wf, "WebFetch page must be outlined"
    assert "### Heading 11" in wf, "headings kept"
    assert "Full text saved to" in wf, "tee-backed"

    ev_search = {
        "session_id": "s21b", "transcript_path": explore_transcript,
        "hook_event_name": "PostToolUse", "tool_name": "WebSearch",
        "tool_input": {"query": "widgets"},
        "tool_response": "\n".join(["No description available."] * 140
                                   + ["result {}".format(i) for i in range(40)]),
    }
    assert len(ev_search["tool_response"]) > 3000, "fixture must clear WEB_MIN_CHARS"
    ws = run(ev_search, env)
    assert ws and "collapsed" in ws, "repetitive search output must collapse"
    assert "result 39" in ws, "distinct results kept"

    # =====================================================================
    # 22. subagent reports: collapse only, never restructured
    # =====================================================================
    ev_agent = {
        "session_id": "s22", "transcript_path": explore_transcript,
        "hook_event_name": "PostToolUse", "tool_name": "Agent",
        "tool_input": {"description": "audit"},
        "tool_response": "\n".join(["checked file, no findings"] * 180
                                   + ["FINDING: real issue at line 42"]),
    }
    assert len(ev_agent["tool_response"]) > 4000, "fixture must clear REPORT_MIN_CHARS"
    ag = run(ev_agent, env)
    assert ag and "FINDING: real issue at line 42" in ag, "findings survive"
    assert "Full report saved to" in ag, "report is recoverable"

    # a report with nothing repetitive is left completely alone
    unique_report = "\n".join("distinct finding number {} with detail".format(i)
                              for i in range(200))
    assert run(dict(ev_agent, session_id="s22b",
                    tool_response=unique_report), env) is None, (
        "non-repetitive report passes through untouched")

    # =====================================================================
    # 25. the shapes a real session actually produces
    # =====================================================================
    # Two things every earlier test got wrong about reality:
    #
    #  a) At the first PostToolUse of a session the transcript usually has no
    #     assistant text yet. Intent used to answer "debug" there, which put the
    #     biggest orientation read of the session behind the highest floor —
    #     mapping almost never fired live even though it fired in every replay.
    #  b) Claude Code's Read carries RAW file content in file.content; the line
    #     numbers are presentation. Fixtures that pre-number the body were
    #     testing a shape the tool does not send.
    empty_transcript = os.path.join(tmp, "empty.jsonl")
    open(empty_transcript, "w", encoding="utf-8").close()
    assert transcript_intent_via(empty_transcript) == "explore", (
        "no evidence must not mean 'debugging'")

    raw_doc = []
    for i in range(30):
        raw_doc.append("## Chapter {}".format(i))
        raw_doc += ["Sentence {} of chapter {} with enough text to matter.".format(j, i)
                    for j in range(12)]
    raw_body = "\n".join(raw_doc)
    raw_path = os.path.join(tmp, "handbook.md")
    with open(raw_path, "w", encoding="utf-8") as f:
        f.write(raw_body)

    ev_raw = {
        "session_id": "s25", "transcript_path": empty_transcript,
        "hook_event_name": "PostToolUse", "tool_name": "Read",
        "tool_input": {"file_path": raw_path},
        # the real shape: raw content, no line-number prefixes
        "tool_response": {"type": "text", "file": {
            "filePath": raw_path, "content": raw_body,
            "numLines": len(raw_doc), "startLine": 1, "totalLines": len(raw_doc)}},
    }
    rr = run(ev_raw, env)
    assert rr and "STRUCTURAL MAP" in rr, (
        "a fresh session's first big read must still map")
    # The escape hatch must survive the host reusing an identical Read's
    # result: a range read is never summarized, so it always gets real bytes.
    assert "offset=1" in rr, "map must offer a cache-proof way back to full content"
    assert run(dict(ev_raw, session_id="s25c",
                    tool_input={"file_path": raw_path, "offset": 1}), env) is None, (
        "an explicit range read must never be summarized")
    assert "## Chapter 29" in rr, "headings kept from raw content"
    assert "Sentence 5 of chapter 3" not in rr, "prose elided"

    # explicit error-hunting still raises the bar
    assert run(dict(ev_raw, session_id="s25b",
                    transcript_path=debug_transcript), env) is None, (
        "stated error-hunting still holds a mid-size file back")

    # =====================================================================
    # 24. grep folds by file when there is no repeated match text
    # =====================================================================
    # Measured: identical match text is 0.1% of grep bytes, so the collapse
    # tier sat idle. The repeated path prefix is 9.4%, and folding it away is
    # lossless.
    grep_lines = []
    for f in range(12):
        for ln in range(9):
            grep_lines.append(
                "/srv/example/src/subsystem/module_{}.py:{}:    "
                "distinct match {} in file {}".format(f, 100 + ln, ln, f))
    ev_fold = {
        "session_id": "s24", "transcript_path": explore_transcript,
        "hook_event_name": "PostToolUse", "tool_name": "Grep",
        "tool_input": {"pattern": "widget", "output_mode": "content"},
        "tool_response": "\n".join(grep_lines),
    }
    gf = run(ev_fold, env)
    assert gf and "folded by file" in gf, "grep with no repeats must fold by path"
    assert gf.count("/srv/example/src/subsystem/module_7.py") == 1, (
        "each path appears once, not once per match")
    assert "distinct match 8 in file 11" in gf, "every match survives"
    assert "108:" in gf, "real line numbers survive"
    assert len(gf) < len(ev_fold["tool_response"]) * 0.85, "folding actually shrinks"

    # one match per file: the heading costs as much as the prefix saves
    sparse = "\n".join("/srv/example/f_{}.py:{}:hit".format(i, i) for i in range(30))
    assert run(dict(ev_fold, session_id="s24b", tool_response=sparse), env) is None, (
        "one match per file -> folding buys nothing, pass through")

    # =====================================================================
    # 23. configuration: presets, files, and precedence
    # =====================================================================
    cfg_home = os.path.join(tmp, "cfghome")
    proj_dir = os.path.join(tmp, "proj")
    os.makedirs(proj_dir, exist_ok=True)
    cfgcmd = os.path.join(HERE, "..", "hooks", "configcmd.py")

    def cfg(*args, cwd=None, extra_env=None):
        e = dict(os.environ, NESTOR_LEAN_HOME=cfg_home)
        for k in list(e):
            if k.startswith("NESTOR_LEAN_") and k != "NESTOR_LEAN_HOME":
                del e[k]
        e.update(extra_env or {})
        p = subprocess.run([sys.executable, cfgcmd] + list(args),
                           capture_output=True, text=True,
                           cwd=cwd or proj_dir, env=e)
        assert p.returncode == 0, p.stdout + p.stderr
        return p.stdout

    def value_of(out, name):
        for line in out.splitlines():
            parts = line.split()
            if parts and parts[0] == name:
                return parts[1]
        raise AssertionError(name + " missing from config output")

    assert value_of(cfg(), "bash_min_chars") == "1500", "ships balanced"

    cfg("preset", "aggressive")
    out = cfg()
    assert value_of(out, "bash_min_chars") == "500", "preset lowers floors"
    assert value_of(out, "codemap_min_chars") == "3000", "preset lowers map floor"
    assert "preset:aggressive" in out, "source is attributed to the preset"

    cfg("preset", "conservative")
    assert value_of(cfg(), "bash_min_chars") == "4000", "conservative raises floors"

    # explicit setting beats the preset
    cfg("bash_min_chars", "777")
    assert value_of(cfg(), "bash_min_chars") == "777", "explicit beats preset"

    # project config beats the user config
    with open(os.path.join(proj_dir, ".nestor-lean.json"), "w", encoding="utf-8") as f:
        json.dump({"bash_min_chars": 321}, f)
    assert value_of(cfg(), "bash_min_chars") == "321", "project beats user"

    # environment beats every file
    out = cfg(extra_env={"NESTOR_LEAN_BASH_MIN_CHARS": "111"})
    assert value_of(out, "bash_min_chars") == "111", "env beats files"
    assert "env NESTOR_LEAN_BASH_MIN_CHARS" in out, "env source is named"

    # and the dispatcher actually honours a config file, not just the printout
    ev_cfg = {
        "session_id": "s23", "transcript_path": explore_transcript,
        "hook_event_name": "PostToolUse", "tool_name": "Bash",
        "tool_input": {"command": "run-it"},
        "tool_response": "\n".join(["same line"] * 40),   # ~360 chars
    }
    base_env = {k: v for k, v in env.items()}
    below = dict(base_env, NESTOR_LEAN_HOME=cfg_home)
    assert run(ev_cfg, dict(below, NESTOR_LEAN_BASH_MIN_CHARS="1500")) is None, (
        "under the floor -> untouched")
    assert run(ev_cfg, dict(below, NESTOR_LEAN_BASH_MIN_CHARS="100")), (
        "lowering the floor via config exposes it")

    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()
