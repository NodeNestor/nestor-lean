#!/usr/bin/env python3
"""nestor-lean hook dispatcher.

Input-side token compression for Claude Code. All transforms fail open: any
error, ambiguity, or unparseable payload -> the original tool output passes
through unchanged.

Events handled (routed by hook_event_name):

  PostToolUse / Read
      1. dedup-by-reference: an identical read (path + offset/limit + content
         digest) already served in this agent context within the window is
         replaced by an orienting note (age, size, outline) pointing at the
         earlier read. Escape valve: the next identical Read after a note
         returns full content and resets the cycle.
      2. codemap: a large full-file read of a CODE file, while the agent is
         EXPLORING (intent inferred from the transcript tail; never while
         error-hunting), is replaced by a structural map — signature lines
         with real line numbers, bodies elided with counts. Same escape
         valve: re-read -> full content.
      3. duplicate collapse: large NON-code reads (logs, dumps) get runs of
         identical consecutive lines collapsed with explicit markers.

  PostToolUse / Grep
      content-mode output: identical match text collapsed with counts,
      per-file caps. Skipped entirely when the pattern looks like error
      hunting (the model likely needs every occurrence).

  PostToolUse / Bash
      large command output: consecutive identical lines collapsed uniq -c
      style with explicit markers.

  PreCompact
      Claude Code is about to compact this context -> all "the model already
      saw this" knowledge for the context is cleared.

  SessionEnd
      state file for the context is deleted.

Context scoping: state is keyed by transcript_path (unique per agent, so
simultaneous subagents never share dedup knowledge), falling back to
session_id. State files are atomic-write JSON, pruned after 48h.

rolling-context integration: when the rolling-context proxy is running, its
/lean/status endpoint reports the wall-clock time of the last compression
injection. Any read recorded before that moment may have been summarized out
of the model's context, so it is never turned into a reference — full content
is served instead. The signal is global across sessions (conservative: a
compression anywhere only costs savings, never correctness).
"""
import hashlib
import json
import os
import re
import sys
import time
import urllib.request


def _env_int(name, default):
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


DEDUP_WINDOW = _env_int("NESTOR_LEAN_DEDUP_WINDOW", 1200)
MIN_DEDUP_CHARS = _env_int("NESTOR_LEAN_MIN_DEDUP_CHARS", 1500)
GREP_MIN_CHARS = _env_int("NESTOR_LEAN_GREP_MIN_CHARS", 4000)
GREP_PER_FILE_CAP = _env_int("NESTOR_LEAN_GREP_PER_FILE_CAP", 25)
BASH_MIN_CHARS = _env_int("NESTOR_LEAN_BASH_MIN_CHARS", 4000)
COLLAPSE_MIN_RUN = _env_int("NESTOR_LEAN_COLLAPSE_MIN_RUN", 5)
CODEMAP_MIN_CHARS = _env_int("NESTOR_LEAN_CODEMAP_MIN_CHARS", 12000)
CODEMAP_ENABLED = os.environ.get("NESTOR_LEAN_CODEMAP", "1") != "0"
MIN_SAVING_RATIO = 0.20
HASH_CAP_BYTES = 4 * 1024 * 1024
STATE_MAX_AGE = 48 * 3600
RC_PROBE_TTL = 10  # seconds to cache the rolling-context probe result
RC_TIMEOUT = 0.25

CODE_EXTS = {
    ".py", ".pyw", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".cs",
    ".razor", ".cshtml", ".java", ".go", ".rs", ".rb", ".php", ".c", ".h",
    ".cpp", ".hpp", ".kt", ".swift", ".scala",
}

ERROR_HUNT = re.compile(
    r"error|exception|traceback|stack\s*trace|fail(ed|ing|ure)?\b|crash"
    r"|panic|fatal|bug\b|broken|regression|diagnos|debug",
    re.IGNORECASE,
)

# Claude Code Read output line: optional spaces, line number, arrow or tab.
READ_LINE = re.compile(r"^(\s*)(\d+)(→|\t)(.*)$")

SIG_FAMILY = {
    ".py": "py", ".pyw": "py",
    ".js": "js", ".mjs": "js", ".cjs": "js", ".ts": "js", ".tsx": "js",
    ".jsx": "js", ".kt": "js", ".swift": "js", ".scala": "js",
    ".cs": "cs", ".razor": "cs", ".cshtml": "cs", ".java": "cs",
    ".go": "go", ".rs": "go", ".rb": "py", ".php": "js",
    ".c": "c", ".h": "c", ".cpp": "c", ".hpp": "c",
}

SIG_PATTERNS = {
    "py": re.compile(r"^\s*(def |class |async def |import |from \S+ import |@\w)"),
    "js": re.compile(
        r"^\s*(export\b|import\b|function\b|class\b|interface\b|enum\b"
        r"|type \w+\s*=|(public|private|protected|static|abstract)\b"
        r"|(const|let|var) \w+\s*=\s*(async\s*)?(\(|function\b|\w+\s*=>))"
    ),
    "cs": re.compile(
        r"^\s*(namespace\b|using \w|(public|private|protected|internal|static"
        r"|abstract|sealed|partial|override|virtual|async)\b|class\b"
        r"|interface\b|enum\b|record\b|struct\b|\[\w)"
    ),
    "go": re.compile(r"^\s*(func\b|type\b|import\b|package\b|const\b|var\b|impl\b|pub\b|fn\b|struct\b|trait\b|mod\b|use \w)"),
    "c": re.compile(r"^\s*(#include|#define|typedef\b|struct\b|enum\b|union\b|static\b|extern\b|[A-Za-z_][\w\s\*]+\([^;]*\)\s*\{?\s*$)"),
}


# ---------------------------------------------------------------- state ----

def data_dir():
    base = os.environ.get("CLAUDE_PLUGIN_DATA") or os.path.join(
        os.path.expanduser("~"), ".nestor-lean"
    )
    d = os.path.join(base, "sessions")
    os.makedirs(d, exist_ok=True)
    return d


def context_key(payload):
    """One state scope per agent context.

    transcript_path is unique per agent (main conversation and each subagent
    write separate transcripts), so simultaneous agents never share dedup
    knowledge. session_id is the fallback.
    """
    raw = payload.get("transcript_path") or payload.get("session_id") or "unknown"
    return hashlib.sha1(str(raw).encode("utf-8", "replace")).hexdigest()[:16]


def state_path(key):
    return os.path.join(data_dir(), key + ".json")


def load_state(key):
    try:
        with open(state_path(key), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {
            "reads": {},
            "saved_chars": 0,
            "read_refs": 0,
            "read_collapses": 0,
            "grep_compressions": 0,
            "bash_collapses": 0,
            "codemaps": 0,
            "rc_probe": None,
        }


def save_state(key, state):
    try:
        tmp = state_path(key) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, state_path(key))
    except Exception:
        pass


def delete_state(key):
    try:
        os.remove(state_path(key))
    except Exception:
        pass


def prune_old_sessions():
    try:
        now = time.time()
        for name in os.listdir(data_dir()):
            p = os.path.join(data_dir(), name)
            if now - os.path.getmtime(p) > STATE_MAX_AGE:
                os.remove(p)
    except Exception:
        pass


# ------------------------------------------------- rolling-context probe ----

def rc_last_injection(state):
    """Timestamp of rolling-context's last compression injection, or None.

    None means: no proxy detected (or unreachable) — Claude Code compaction
    is then the only invalidation source, and PreCompact covers that.
    The probe result is cached in state for RC_PROBE_TTL seconds so we do at
    most one localhost round-trip every few reads.
    """
    now = time.time()
    cached = state.get("rc_probe")
    if cached and now - cached.get("at", 0) <= RC_PROBE_TTL:
        return cached.get("last_injection_ts") if cached.get("ok") else None

    url = os.environ.get("NESTOR_LEAN_RC_URL")
    if not url:
        port = os.environ.get("ROLLING_CONTEXT_PORT", "5588")
        url = "http://127.0.0.1:{}".format(port)
    probe = {"at": now, "ok": False, "last_injection_ts": None}
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/lean/status", timeout=RC_TIMEOUT) as r:
            d = json.load(r)
        probe["ok"] = True
        probe["last_injection_ts"] = float(d.get("last_injection_ts") or 0.0)
    except Exception:
        pass
    state["rc_probe"] = probe
    return probe["last_injection_ts"] if probe["ok"] else None


# ------------------------------------------------------- payload parsing ----

def extract_text_and_carrier(payload):
    """Locate the tool output's text and return (text, rebuild) where
    rebuild(new_text) produces a replacement with the SAME SHAPE as the
    original output.

    Claude Code validates updatedToolOutput against the tool's own output
    schema and silently discards mismatches ("does not match tool's output
    shape; using original output"), so a bare string only works for tools
    whose output IS a string. Observed live shapes:
      Read -> {"type": "text", "file": {"filePath", "content", ...}}
    """
    out = payload.get("tool_output")
    if out is None:
        out = payload.get("tool_response")

    if isinstance(out, str):
        return out, lambda new: new

    if isinstance(out, dict):
        # nested Read shape: {"file": {"content": str, "numLines": int, ...}}
        f = out.get("file")
        if isinstance(f, dict) and isinstance(f.get("content"), str):
            def rebuild_file(new, _out=out):
                repl = json.loads(json.dumps(_out))
                repl["file"]["content"] = new
                if "numLines" in repl["file"]:
                    repl["file"]["numLines"] = new.count("\n") + 1
                return repl
            return f["content"], rebuild_file

        for key in ("output", "content", "text", "result", "stdout"):
            v = out.get(key)
            if isinstance(v, str):
                def rebuild_key(new, _out=out, _key=key):
                    repl = json.loads(json.dumps(_out))
                    repl[_key] = new
                    return repl
                return v, rebuild_key

        # content-block list shape: [{"type": "text", "text": ...}, ...]
        blocks = out.get("content")
        if isinstance(blocks, list):
            texts = [
                b.get("text")
                for b in blocks
                if isinstance(b, dict) and isinstance(b.get("text"), str)
            ]
            if texts:
                def rebuild_blocks(new, _out=out):
                    repl = json.loads(json.dumps(_out))
                    repl["content"] = [{"type": "text", "text": new}]
                    return repl
                return "\n".join(texts), rebuild_blocks

    return None, None


def extract_text(payload):
    return extract_text_and_carrier(payload)[0]


def parse_read_lines(text):
    """Split Read output into (prefix_ws, lineno, sep, content) tuples where
    the line-number format is recognized, else None entries."""
    parsed = []
    for line in text.splitlines():
        m = READ_LINE.match(line)
        parsed.append(
            (m.group(1), int(m.group(2)), m.group(3), m.group(4)) if m else None
        )
    return parsed


# --------------------------------------------------------- intent (why?) ----

def transcript_intent(transcript_path):
    """"explore" or "debug", inferred from the most recent conversation text.

    Reads the tail of the agent's own transcript (the only place the model's
    intent is visible to a hook) and looks at the last few visible text
    passages from the user and assistant. Any error-hunting vocabulary ->
    "debug". Unreadable/unparseable -> "debug" (the conservative answer:
    debug mode disables codemap, never breaks anything).
    """
    try:
        size = os.path.getsize(transcript_path)
        with open(transcript_path, "rb") as f:
            if size > 65536:
                f.seek(-65536, os.SEEK_END)
            tail = f.read().decode("utf-8", "replace")
    except Exception:
        return "debug"

    texts = []
    for line in reversed(tail.splitlines()):
        if len(texts) >= 3:
            break
        try:
            entry = json.loads(line)
        except Exception:
            continue
        msg = entry.get("message") if isinstance(entry, dict) else None
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    t = block.get("text")
                    if isinstance(t, str) and t.strip():
                        texts.append(t)
    if not texts:
        return "debug"
    recent = "\n".join(texts[:3])
    return "debug" if ERROR_HUNT.search(recent) else "explore"


# ----------------------------------------------------------- transforms ----

def file_digest(path):
    st = os.stat(path)
    h = hashlib.sha256()
    h.update(str(st.st_size).encode())
    with open(path, "rb") as f:
        h.update(f.read(HASH_CAP_BYTES))
    return h.hexdigest()


def build_outline(text, ext, limit=6):
    """A few signature lines (with line numbers when available) so the
    reference note orients the model on WHAT it already read."""
    fam = SIG_FAMILY.get(ext)
    pat = SIG_PATTERNS.get(fam) if fam else None
    parsed = parse_read_lines(text)
    out = []
    for i, item in enumerate(parsed):
        if item is not None:
            _, lineno, _, content = item
        else:
            lineno, content = i + 1, text.splitlines()[i] if i < len(text.splitlines()) else ""
        candidate = content if pat else content.strip()
        if pat and not pat.match(content):
            continue
        if not pat and not candidate:
            continue
        out.append("    {}: {}".format(lineno, content.strip()[:100]))
        if len(out) >= limit:
            break
    return out


def dedup_note(fp, digest, age_min, text, size_bytes):
    lines = text.count("\n") + 1 if text else "?"
    ext = os.path.splitext(fp)[1].lower()
    outline = build_outline(text, ext) if text else []
    note = [
        "[nestor-lean] Duplicate read skipped — you already read this exact "
        "file ~{} min ago in this conversation and it has NOT changed since.".format(age_min),
        "  file:   {}".format(fp),
        "  size:   {} bytes, {} lines, digest {}".format(size_bytes, lines, digest[:12]),
    ]
    if outline:
        note.append("  it contains (outline of your earlier read):")
        note.extend(outline)
    note.append(
        "Use your earlier read of this file from this conversation. If that "
        "content is no longer in your context (e.g. it was compacted away), "
        "run the exact same Read again — the full contents will be returned."
    )
    return "\n".join(note)


def build_codemap(text, ext):
    """Structural map: signature lines kept with their real line numbers,
    bodies elided with explicit counts. Returns None if the file doesn't map
    cleanly or the map wouldn't save enough."""
    fam = SIG_FAMILY.get(ext)
    pat = SIG_PATTERNS.get(fam)
    if not pat:
        return None
    parsed = parse_read_lines(text)
    raw_lines = text.splitlines()
    kept = []       # (lineno, rendered_line)
    elided = 0
    sig_count = 0

    def flush_elided():
        nonlocal elided
        if elided > 0:
            kept.append((None, "        … +{} lines (implementation)".format(elided)))
            elided = 0

    for i, item in enumerate(parsed):
        if item is not None:
            _, lineno, sep, content = item
            rendered = "{:>6}{}{}".format(lineno, sep, content)
        else:
            lineno = i + 1
            content = raw_lines[i]
            rendered = "{:>6}→{}".format(lineno, content)
        if pat.match(content):
            flush_elided()
            kept.append((lineno, rendered))
            sig_count += 1
        else:
            elided += 1
    flush_elided()

    if sig_count < 8:
        return None  # too little structure to be a useful map
    body = "\n".join(r for _, r in kept)
    if len(body) >= len(text) * (1 - MIN_SAVING_RATIO):
        return None
    header = (
        "[nestor-lean] STRUCTURAL MAP (exploration read) — implementation "
        "bodies elided, {} signature/import lines kept with their real line "
        "numbers. This is enough to navigate and decide where to look. "
        "Before quoting or editing this file, re-run the exact same Read: "
        "the full contents will be returned.\n".format(sig_count)
    )
    return header + body


def collapse_duplicate_lines(text, min_run, preserve_read_numbers):
    """uniq -c style collapse of runs of identical consecutive lines.

    For Read output the shown line numbers of kept lines stay real; markers
    state the elided range so numbering stays understandable.
    """
    lines = text.splitlines()
    out = []
    i = 0
    collapsed_any = False
    while i < len(lines):
        line = lines[i]
        m = READ_LINE.match(line) if preserve_read_numbers else None
        content = m.group(4) if m else line
        j = i + 1
        while j < len(lines):
            m2 = READ_LINE.match(lines[j]) if preserve_read_numbers else None
            c2 = m2.group(4) if m2 else lines[j]
            if c2 != content:
                break
            j += 1
        run = j - i
        if run >= min_run and content.strip():
            out.append(line)
            if preserve_read_numbers and m:
                last_m = READ_LINE.match(lines[j - 1])
                last_no = last_m.group(2) if last_m else "?"
                out.append(
                    "      … [previous line repeats {}x, through line {}]".format(run - 1, last_no)
                )
            else:
                out.append("  … [previous line repeats {}x]".format(run - 1))
            collapsed_any = True
        else:
            out.extend(lines[i:j])
        i = j
    if not collapsed_any:
        return None
    return "\n".join(out)


# ------------------------------------------------------------- handlers ----

def handle_read(payload, state):
    ti = payload.get("tool_input") or {}
    fp = ti.get("file_path")
    if not fp or not os.path.isfile(fp):
        return None
    text = extract_text(payload)
    approx_len = len(text) if text is not None else 0
    if text is not None and approx_len < MIN_DEDUP_CHARS:
        return None
    try:
        digest = file_digest(fp)
        size_bytes = os.path.getsize(fp)
    except Exception:
        return None

    key = "{}|{}|{}".format(fp, ti.get("offset"), ti.get("limit"))
    now = time.time()
    rec = state["reads"].get(key)
    ext = os.path.splitext(fp)[1].lower()
    is_code = ext in CODE_EXTS

    # ---- 1. dedup-by-reference -----------------------------------------
    if rec and rec.get("digest") == digest and (now - rec.get("ts", 0)) <= DEDUP_WINDOW:
        if rec.get("ref_served") or rec.get("map_served"):
            # Escape valve: model asked again after a reference/map ->
            # it needs the real bytes. Serve full, reset cycle.
            rec.update(ts=now, ref_served=False, map_served=False)
            return None
        # rolling-context check: if a compression was injected AFTER this
        # read was recorded, the earlier content may be summarized away ->
        # never point at it; refresh the record with this full read instead.
        rc_ts = rc_last_injection(state)
        if rc_ts and rc_ts > rec.get("ts", 0):
            rec.update(ts=now, ref_served=False, map_served=False)
            return None
        age_min = max(1, int((now - rec.get("ts", now)) / 60))
        rec.update(ts=now, ref_served=True)
        saved = approx_len if approx_len else min(size_bytes, HASH_CAP_BYTES)
        state["saved_chars"] += max(saved - 600, 0)
        state["read_refs"] += 1
        return dedup_note(fp, digest, age_min, text or "", size_bytes)

    new_rec = {
        "digest": digest,
        "ts": now,
        "ref_served": False,
        "map_served": False,
    }
    state["reads"][key] = new_rec

    if text is None:
        return None

    # ---- 2. codemap for exploration reads of big code files -------------
    if (
        CODEMAP_ENABLED
        and is_code
        and ti.get("offset") is None
        and ti.get("limit") is None
        and approx_len >= CODEMAP_MIN_CHARS
        and transcript_intent(payload.get("transcript_path") or "") == "explore"
    ):
        cmap = build_codemap(text, ext)
        if cmap is not None:
            new_rec["map_served"] = True
            state["saved_chars"] += len(text) - len(cmap)
            state["codemaps"] += 1
            return cmap

    # ---- 3. duplicate collapse for big non-code reads --------------------
    if not is_code and approx_len >= GREP_MIN_CHARS:
        collapsed = collapse_duplicate_lines(
            text, COLLAPSE_MIN_RUN, preserve_read_numbers=True
        )
        if collapsed is not None and len(collapsed) < approx_len * (1 - MIN_SAVING_RATIO):
            header = (
                "[nestor-lean] repetitive content collapsed (identical "
                "consecutive lines shown once with explicit repeat counts; "
                "line numbers are real). Re-read with offset/limit for any "
                "exact region.\n"
            )
            state["saved_chars"] += approx_len - len(collapsed) - len(header)
            state["read_collapses"] += 1
            return header + collapsed

    return None


GREP_LINE = re.compile(r"^(.*?):(\d+):(.*)$")


def handle_grep(payload, state):
    ti = payload.get("tool_input") or {}
    if ti.get("output_mode") != "content":
        return None
    pattern = str(ti.get("pattern") or "")
    if ERROR_HUNT.search(pattern):
        return None  # error hunting: every occurrence may matter
    text = extract_text(payload)
    if not text or len(text) < GREP_MIN_CHARS:
        return None

    lines = text.splitlines()
    out_lines = []
    per_file_count = {}
    per_file_hidden = {}
    seen_content = {}
    for line in lines:
        m = GREP_LINE.match(line)
        if not m:
            out_lines.append(line)
            continue
        fname, _lineno, content = m.group(1), m.group(2), m.group(3)
        ckey = (fname, content.strip())
        if ckey in seen_content and content.strip():
            out_lines[seen_content[ckey]][1][0] += 1
            continue
        n = per_file_count.get(fname, 0) + 1
        per_file_count[fname] = n
        if n > GREP_PER_FILE_CAP:
            per_file_hidden[fname] = per_file_hidden.get(fname, 0) + 1
            continue
        seen_content[ckey] = len(out_lines)
        out_lines.append([line, [1]])

    rendered = []
    for item in out_lines:
        if isinstance(item, str):
            rendered.append(item)
        else:
            line, counts = item
            if counts[0] > 1:
                rendered.append(
                    "{}   [identical match repeats {}x in this file]".format(line, counts[0])
                )
            else:
                rendered.append(line)
    for fname, hidden in per_file_hidden.items():
        rendered.append(
            "[nestor-lean] {}: +{} more matches capped (run a narrower grep on this file for the rest)".format(fname, hidden)
        )

    new_text = "\n".join(rendered)
    if len(new_text) >= len(text) * (1 - MIN_SAVING_RATIO):
        return None
    header = (
        "[nestor-lean] grep output compressed: {} -> {} lines (identical "
        "matches collapsed with counts; capped at {} matches/file; shown line "
        "numbers are each match's first occurrence).\n".format(
            len(lines), len(rendered), GREP_PER_FILE_CAP
        )
    )
    state["saved_chars"] += len(text) - len(new_text) - len(header)
    state["grep_compressions"] += 1
    return header + new_text


def handle_bash(payload, state):
    text = extract_text(payload)
    if not text or len(text) < BASH_MIN_CHARS:
        return None
    collapsed = collapse_duplicate_lines(
        text, COLLAPSE_MIN_RUN, preserve_read_numbers=False
    )
    if collapsed is None or len(collapsed) >= len(text) * (1 - 0.15):
        return None
    header = (
        "[nestor-lean] command output collapsed (identical consecutive lines "
        "shown once with explicit repeat counts).\n"
    )
    state["saved_chars"] += len(text) - len(collapsed) - len(header)
    state["bash_collapses"] += 1
    return header + collapsed


# ----------------------------------------------------------------- main ----

def main():
    if os.environ.get("NESTOR_LEAN_DISABLE") == "1":
        return
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return

    dump = os.environ.get("NESTOR_LEAN_DEBUG_DUMP")
    if dump:
        try:
            with open(dump, "a", encoding="utf-8") as f:
                f.write(json.dumps(payload) + "\n")
        except Exception:
            pass

    event = payload.get("hook_event_name")
    key = context_key(payload)

    if event == "PreCompact":
        state = load_state(key)
        state["reads"] = {}
        save_state(key, state)
        return
    if event == "SessionEnd":
        delete_state(key)
        return

    tool = payload.get("tool_name")
    state = load_state(key)
    replacement = None
    try:
        if tool == "Read":
            replacement = handle_read(payload, state)
        elif tool == "Grep":
            replacement = handle_grep(payload, state)
        elif tool == "Bash":
            replacement = handle_bash(payload, state)
    except Exception:
        replacement = None

    save_state(key, state)
    prune_old_sessions()

    if replacement is not None:
        # Rebuild the replacement in the original output's shape — Claude
        # Code validates updatedToolOutput against the tool's output schema
        # and discards anything that doesn't match it.
        _, rebuild = extract_text_and_carrier(payload)
        if rebuild is None:
            return
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PostToolUse",
                        "updatedToolOutput": rebuild(replacement),
                    }
                }
            )
        )


if __name__ == "__main__":
    main()
