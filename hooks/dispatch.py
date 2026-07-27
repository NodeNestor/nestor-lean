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
      2. structural map: a large full-file read of a MAPPABLE file (code,
         markdown, HTML/markup, CSS) is replaced by signature/heading/selector
         lines with real line numbers, bodies elided with counts. Intent
         (inferred from the transcript tail) scales the size floor rather than
         vetoing the map, because error-hunt vocabulary appears in ordinary
         narration far more often than real debugging does. Same escape valve:
         re-read -> full content.
      3. duplicate collapse: large NON-code reads (logs, dumps) get runs of
         identical consecutive lines collapsed with explicit markers.

  PostToolUse / Grep
      content-mode output: identical match text collapsed with counts,
      per-file caps. Skipped entirely when the pattern looks like error
      hunting (the model likely needs every occurrence).

  PostToolUse / Bash, PowerShell
      large command output: rtk filter, then built-in route, then consecutive
      identical lines collapsed uniq -c style with explicit markers.

  PostToolUse / WebFetch, WebSearch
      a fetched page is markdown, so it gets the same structural map as a
      document; search results get repetition collapse. Both tee-backed.

  PostToolUse / Glob
      path lists folded by directory — the shared stem written once, every
      filename listed under it, so each path is recoverable by concatenation.

  PostToolUse / Agent, TaskOutput
      repetition collapse only. A subagent report is often the sole record of
      work the main agent never saw, so it is never restructured.

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
import difflib
import hashlib
import json
import os
import re
import sys
import time
import urllib.request

import config
import switch


# Every knob resolves through config: defaults -> preset -> user config file ->
# project config file -> NESTOR_LEAN_* env. See hooks/config.py.
_c = config.get

DEDUP_WINDOW = _c("dedup_window")
MIN_DEDUP_CHARS = _c("min_dedup_chars")
GREP_MIN_CHARS = _c("grep_min_chars")
GREP_PER_FILE_CAP = _c("grep_per_file_cap")
# Floors are a pre-filter, not the safety mechanism. The hook process runs on
# every matched call regardless, so a floor skips in-process work rather than a
# spawn; MIN_SAVING_RATIO is what actually protects correctness.
BASH_MIN_CHARS = _c("bash_min_chars")
MCP_MIN_CHARS = _c("mcp_min_chars")
WEB_MIN_CHARS = _c("web_min_chars")
GLOB_MIN_CHARS = _c("glob_min_chars")
REPORT_MIN_CHARS = _c("report_min_chars")
WEB_ENABLED = _c("web")
MCP_ENABLED = _c("mcp")
COLLAPSE_MIN_RUN = _c("collapse_min_run")
# Layout stripping is content-preserving, so it only has to beat its own
# header rather than a saving ratio.
LAYOUT_MIN_SAVING = _c("layout_min_saving")
CODEMAP_MIN_CHARS = _c("codemap_min_chars")
# While the agent looks like it is error-hunting, only map files big enough
# that a full read would dominate the context anyway.
CODEMAP_DEBUG_MIN_CHARS = _c("codemap_debug_min_chars")
CODEMAP_ENABLED = _c("codemap")
BASH_ROUTES_ENABLED = _c("bash_routes")
RTK_ENABLED = _c("rtk_pipe")
TEE_MAX_AGE = 6 * 3600
DIFF_ENABLED = _c("diff")
DIFF_MAX_CONTENT = 512 * 1024       # don't blob/diff files larger than this
DIFF_MAX_CHANGE_RATIO = _c("diff_max_change_ratio")
DIFF_CONTEXT = 3                    # context lines around each changed hunk
MIN_SAVING_RATIO = _c("min_saving_ratio")
HASH_CAP_BYTES = 4 * 1024 * 1024
STATE_MAX_AGE = 48 * 3600
RC_PROBE_TTL = 10  # seconds to cache the rolling-context probe result
RC_TIMEOUT = 0.25

# Windows sessions run most of their commands through the PowerShell tool, so
# leaving it out meant a large share of shell output was never seen at all.
SHELL_TOOLS = ("Bash", "PowerShell")

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
    # Prose and markup map just as well as code: the "signature" is the
    # heading / selector / structural tag, the "body" is what it introduces.
    # Measured on a real corpus, non-code first reads are a quarter of all
    # read bytes (markdown alone the largest single non-code extension).
    ".md": "md", ".markdown": "md", ".mdx": "md", ".rst": "md",
    ".html": "html", ".htm": "html", ".astro": "html", ".vue": "html",
    ".xml": "html", ".svg": "html",
    ".css": "css", ".scss": "css", ".sass": "css", ".less": "css",
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
    # ATX headings, setext underlines and fence markers — enough to navigate a
    # document and decide which section to re-read.
    "md": re.compile(r"^\s{0,3}(#{1,6}\s+\S|```|~~~|={3,}\s*$|-{3,}\s*$)"),
    # Structural/landmark tags only; attributes and text content are the body.
    "html": re.compile(
        r"^\s*</?(!DOCTYPE|html|head|body|header|nav|main|section|article"
        r"|aside|footer|div|form|table|thead|tbody|script|style|link|meta"
        r"|title|template|slot|h[1-6]|ul|ol|dl|figure|canvas|svg|iframe"
        r"|component|router-view)\b",
        re.IGNORECASE,
    ),
    # Selector lines (anything opening a block) and at-rules.
    "css": re.compile(r"^\s*(@[a-z-]+|[.#&\[:a-zA-Z][^{};]*\{\s*$|[^{};]+,\s*$)"),
}

# Extensions whose structure a map can express. Broader than CODE_EXTS, which
# still governs the code-specific paths (error-hunt caution, collapse tier).
MAPPABLE_EXTS = frozenset(SIG_FAMILY)

# Documents that are INSTRUCTIONS rather than reference material. Eliding the
# prose under a heading is fine for a design doc the model is navigating; it is
# not fine for a file whose whole purpose is telling the model what to do, and
# the "re-read to get it all" escape valve does not help when the model has no
# reason to suspect it is missing a rule.
NEVER_MAP_NAMES = frozenset({
    "claude.md", "agents.md", "agent.md", "skill.md", "cursorrules",
    ".cursorrules", "copilot-instructions.md", "conventions.md", "rules.md",
})


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
            "diff_reads": 0,
            "grep_compressions": 0,
            "bash_collapses": 0,
            "bash_routes": 0,
            "rtk_pipes": 0,
            "rtk_rewrites": 0,
            "mcp_compressions": 0,
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

    # Bare content-block list — the shape MCP tools return:
    #   [{"type": "text", "text": "..."}, ...]
    if isinstance(out, list):
        texts = [
            b.get("text")
            for b in out
            if isinstance(b, dict) and isinstance(b.get("text"), str)
        ]
        # only carry when every block is plain text (don't drop images etc.)
        if texts and len(texts) == len([b for b in out if isinstance(b, dict)]):
            def rebuild_list(new):
                return [{"type": "text", "text": new}]
            return "\n".join(texts), rebuild_list

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


ELISION_NOUN = {
    "md": "prose", "html": "markup", "css": "declarations",
}


def build_codemap(text, ext, debugging=False):
    """Structural map: signature lines kept with their real line numbers,
    bodies elided with explicit counts. Returns None if the file doesn't map
    cleanly or the map wouldn't save enough."""
    fam = SIG_FAMILY.get(ext)
    pat = SIG_PATTERNS.get(fam)
    if not pat:
        return None
    noun = ELISION_NOUN.get(fam, "implementation")
    parsed = parse_read_lines(text)
    raw_lines = text.splitlines()
    kept = []       # (lineno, rendered_line)
    elided = 0
    sig_count = 0

    def flush_elided():
        nonlocal elided
        if elided > 0:
            kept.append((None, "        … +{} lines ({})".format(elided, noun)))
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
        "[nestor-lean] STRUCTURAL MAP ({}) — {} elided, {} structural lines "
        "kept with their real line numbers. This is enough to navigate and "
        "decide where to look. Before quoting or editing this file, re-run "
        "the exact same Read: the full contents will be returned.\n".format(
            "large file, mid-investigation" if debugging else "exploration read",
            noun, sig_count,
        )
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


# ------------------------------------------------ content blobs (diffs) ----

def blobs_root():
    base = os.environ.get("CLAUDE_PLUGIN_DATA") or os.path.join(
        os.path.expanduser("~"), ".nestor-lean"
    )
    return os.path.join(base, "blobs")


def blob_dir(ckey):
    d = os.path.join(blobs_root(), ckey)
    os.makedirs(d, exist_ok=True)
    return d


def blob_path(ckey, read_key):
    h = hashlib.sha1(read_key.encode("utf-8", "replace")).hexdigest()[:16]
    return os.path.join(blob_dir(ckey), h + ".txt")


def store_blob(ckey, read_key, text):
    try:
        p = blob_path(ckey, read_key)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, p)
        return True
    except Exception:
        return False


def load_blob(ckey, read_key):
    try:
        with open(blob_path(ckey, read_key), "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return None


def prune_blobs():
    try:
        root = blobs_root()
        if not os.path.isdir(root):
            return
        now = time.time()
        for ck in os.listdir(root):
            cdir = os.path.join(root, ck)
            if not os.path.isdir(cdir):
                continue
            remaining = 0
            for n in os.listdir(cdir):
                p = os.path.join(cdir, n)
                try:
                    if now - os.path.getmtime(p) > STATE_MAX_AGE:
                        os.remove(p)
                    else:
                        remaining += 1
                except Exception:
                    remaining += 1
            if remaining == 0:
                try:
                    os.rmdir(cdir)
                except Exception:
                    pass
    except Exception:
        pass


def content_lines(text):
    """Read output -> [(real_line_number, content)], prefixes removed.

    Diffing must happen on content, never on the rendered read output. Read
    output carries a line-number prefix per line, so inserting or deleting a
    single line rewrites the prefix of every line after it — SequenceMatcher
    then sees the whole tail as changed, the change ratio blows past its limit,
    and the diff is rejected. That silently reduced differential reads to the
    one edit shape that preserves line count.
    """
    parsed = parse_read_lines(text)
    raw = text.splitlines()
    out = []
    for i, item in enumerate(parsed):
        if item is not None:
            _, lineno, _sep, content = item
            out.append((lineno, content))
        else:
            out.append((i + 1, raw[i] if i < len(raw) else ""))
    return out


def build_diff_view(old_text, new_text, fp, age_min):
    """A unified-diff-style view of a changed file: only the changed hunks,
    with real (current) line numbers. Returns None if the change is too large
    to be worth a diff (caller then serves the full file)."""
    old_pairs = content_lines(old_text)
    new_pairs = content_lines(new_text)
    old_lines = [c for _n, c in old_pairs]
    new_lines = [c for _n, c in new_pairs]
    sm = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    if 1.0 - sm.ratio() > DIFF_MAX_CHANGE_RATIO:
        return None

    def lineno(k):
        return new_pairs[k][0] if k < len(new_pairs) else k + 1

    hunks = []
    for group in sm.get_grouped_opcodes(DIFF_CONTEXT):
        rendered = []
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                for k in range(j1, j2):
                    rendered.append("  {:>6}  {}".format(lineno(k), new_lines[k]))
            else:
                for k in range(i1, i2):
                    rendered.append("  -       {}".format(old_lines[k]))
                for k in range(j1, j2):
                    rendered.append("  + {:>6}  {}".format(lineno(k), new_lines[k]))
        first_new = lineno(group[0][3])
        hunks.append("@@ around line {} @@\n{}".format(first_new, "\n".join(rendered)))
    if not hunks:
        return None
    header = (
        "[nestor-lean] FILE CHANGED since your earlier read (~{} min ago). "
        "Only the changed regions are shown below; every other line is "
        "UNCHANGED from the version already in your context. '+' lines with "
        "numbers are the current file; '-' lines are what they replaced. "
        "Line numbers are the current file's real numbers. To get the full "
        "current file (e.g. before a large edit), run the exact same Read "
        "again.\n  file: {}\n".format(age_min, fp)
    )
    return header + "\n".join(hunks)


# --------------------------------------------- rtk-style command routes ----

def tee_dir():
    base = os.environ.get("CLAUDE_PLUGIN_DATA") or os.path.join(
        os.path.expanduser("~"), ".nestor-lean"
    )
    d = os.path.join(base, "tee")
    os.makedirs(d, exist_ok=True)
    return d


def write_tee(text):
    """Write full command output to a recovery file so an aggressive route can
    be undone by one Read. Returns the path, or None on failure."""
    try:
        d = tee_dir()
        now = time.time()
        for n in os.listdir(d):
            p = os.path.join(d, n)
            try:
                if now - os.path.getmtime(p) > TEE_MAX_AGE:
                    os.remove(p)
            except Exception:
                pass
        h = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:12]
        path = os.path.join(d, h + ".txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path
    except Exception:
        return None


def _dedupe_keep(lines):
    seen = set()
    out = []
    for l in lines:
        k = l.strip()
        if k and k in seen:
            continue
        seen.add(k)
        out.append(l)
    return out


class BashRoute:
    name = "base"

    def matches(self, command):
        return False

    def transform(self, text):
        """Return (compressed_text, kept_description) or None."""
        return None


class DotnetBuildRoute(BashRoute):
    """dotnet build/publish/run/msbuild/pack — keep diagnostics + summary."""
    name = "dotnet-build"
    _cmd = re.compile(r"\bdotnet\s+(build|publish|run|msbuild|pack)\b|\bmsbuild\b")
    _keep = re.compile(
        r"\)\s*:\s*(error|warning)\s"          # File.cs(1,2): error CS0103 ...
        r"|\bBuild (succeeded|FAILED)\b"
        r"|\b\d+\s+(Error|Warning)\(s\)"
        r"|\bTime Elapsed\b"
        r"|\berror\b(?!\s*[:=]\s*(0|false))"   # bare 'error' but not 'errors: 0'
        r"|: error |: warning ",
        re.I,
    )

    def matches(self, command):
        return bool(self._cmd.search(command))

    def transform(self, text):
        lines = text.splitlines()
        kept = _dedupe_keep([l for l in lines if l.strip() and self._keep.search(l)])
        if not kept:
            return None
        return "\n".join(kept), "compiler errors, warnings, and build summary"


class TestRunnerRoute(BashRoute):
    """Test runners (dotnet test, pytest, playwright, jest, vitest, go/cargo
    test) — keep failures, assertion detail, and the final summary; drop the
    passing-test noise."""
    name = "test-runner"
    _cmd = re.compile(
        r"\bdotnet\s+test\b|\bpytest\b|\bplaywright\s+test\b|\bnpx\s+playwright\b"
        r"|\bjest\b|\bvitest\b|\bgo\s+test\b|\bcargo\s+test\b|\bnpm\s+(run\s+)?test\b"
    )
    _keep = re.compile(
        r"\bFAIL(ED|URE)?\b|\bERROR\b|\bexception\b|\btraceback\b"
        r"|\bassert|\bexpect(ed)?\b"
        r"|^\s*✘|^\s*✗|^\s*×|^\s*[-–]\s"                      # failure bullets
        r"|\bPassed!|\bFailed!|\bTotal tests\b|\bTest Run\b"  # vstest
        r"|=+\s*\d+\s+(passed|failed|error)"                 # pytest summary
        r"|\b\d+\s+(passed|failed|skipped|pending|flaky)\b"  # jest/playwright/pw
        r"|\bok\b\s+\d|\b--- FAIL|\bPASS\b\s|\bFAIL\b\s"      # go test
        r"|\btest result:\b",                                # cargo
        re.I,
    )

    def matches(self, command):
        return bool(self._cmd.search(command))

    def transform(self, text):
        lines = text.splitlines()
        kept = [l for l in lines if l.strip() and self._keep.search(l)]
        if not kept:
            return None
        return "\n".join(kept), "failures, assertion detail, and the run summary"


class PackageInstallRoute(BashRoute):
    """npm/pnpm/yarn/pip install — keep the result summary + errors, drop the
    per-package progress spam."""
    name = "package-install"
    _cmd = re.compile(
        r"\b(npm|pnpm|yarn)\s+(install|i|ci|add)\b|\bpip3?\s+install\b"
    )
    _keep = re.compile(
        r"\berror\b|\bwarn(ing)?\b|\bfail|\bENO|\bpeer\b|\bdeprecated\b"
        r"|added \d+|removed \d+|changed \d+|audited \d+"
        r"|\d+ vulnerabilit|up to date|Successfully installed|Requirement already"
        r"|\bDone\b|\bPackages:|\+\d+",
        re.I,
    )

    def matches(self, command):
        return bool(self._cmd.search(command))

    def transform(self, text):
        lines = text.splitlines()
        kept = _dedupe_keep([l for l in lines if l.strip() and self._keep.search(l)])
        if not kept:
            return None
        return "\n".join(kept), "install summary, warnings, and errors"


BASH_ROUTES = [DotnetBuildRoute(), TestRunnerRoute(), PackageInstallRoute()]


# ------------------------------------------------------------- handlers ----

def handle_read(payload, state, ckey):
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
    rc_ts = rc_last_injection(state)
    base_still_valid = not (rc_ts and rec and rc_ts > rec.get("ts", 0))

    # ---- 1. dedup-by-reference (file unchanged) ------------------------
    if rec and rec.get("digest") == digest and (now - rec.get("ts", 0)) <= DEDUP_WINDOW:
        if rec.get("ref_served") or rec.get("map_served"):
            # Escape valve: model asked again after a reference/map ->
            # it needs the real bytes. Serve full, reset cycle.
            rec.update(ts=now, ref_served=False, map_served=False, served_full=True)
            return None
        # rolling-context check: if a compression was injected AFTER this
        # read was recorded, the earlier content may be summarized away ->
        # never point at it; refresh the record with this full read instead.
        if not base_still_valid:
            rec.update(ts=now, ref_served=False, map_served=False, served_full=True)
            return None
        age_min = max(1, int((now - rec.get("ts", now)) / 60))
        rec.update(ts=now, ref_served=True)
        saved = approx_len if approx_len else min(size_bytes, HASH_CAP_BYTES)
        state["saved_chars"] += max(saved - 600, 0)
        state["read_refs"] += 1
        return dedup_note(fp, digest, age_min, text or "", size_bytes)

    # ---- 2. differential read (file changed, base still in context) -----
    # Only against a base that was served IN FULL and is still valid; a diff
    # lets the model reconstruct the current file from what it already has.
    if (
        DIFF_ENABLED
        and rec is not None
        and rec.get("digest") != digest
        and rec.get("served_full")
        and rec.get("has_blob")
        and base_still_valid
        and (now - rec.get("ts", 0)) <= DEDUP_WINDOW
        and text is not None
        and approx_len >= MIN_DEDUP_CHARS
        and approx_len <= DIFF_MAX_CONTENT
    ):
        old_text = load_blob(ckey, key)
        if old_text is not None:
            age_min = max(1, int((now - rec.get("ts", now)) / 60))
            view = build_diff_view(old_text, text, fp, age_min)
            if view is not None and len(view) < approx_len * (1 - MIN_SAVING_RATIO):
                store_blob(ckey, key, text)
                rec.update(digest=digest, ts=now, ref_served=False,
                           map_served=False, served_full=True, has_blob=True)
                state["saved_chars"] += approx_len - len(view)
                state["diff_reads"] = state.get("diff_reads", 0) + 1
                return view

    new_rec = {
        "digest": digest,
        "ts": now,
        "ref_served": False,
        "map_served": False,
        "served_full": False,
        "has_blob": False,
    }
    state["reads"][key] = new_rec

    if text is None:
        return None

    # Store the full content as a blob so a future changed re-read can diff.
    if DIFF_ENABLED and approx_len <= DIFF_MAX_CONTENT:
        new_rec["has_blob"] = store_blob(ckey, key, text)

    # ---- 3. structural map for big first reads of mappable files ---------
    # Intent scales the threshold instead of switching the map off. Treating
    # error-hunting as a hard veto cost most of the opportunity: the vocabulary
    # ("error", "fail", "debug") appears in ordinary assistant narration, so
    # the veto fired far more often than real debugging did. A map is still a
    # good trade on a very large file while debugging — the model reads a
    # region next either way, and the escape valve returns full content on the
    # next identical Read.
    if (
        CODEMAP_ENABLED
        and ext in MAPPABLE_EXTS
        and os.path.basename(fp).lower() not in NEVER_MAP_NAMES
        and ti.get("offset") is None
        and ti.get("limit") is None
    ):
        debugging = transcript_intent(payload.get("transcript_path") or "") != "explore"
        floor = CODEMAP_DEBUG_MIN_CHARS if debugging else CODEMAP_MIN_CHARS
        if approx_len >= floor:
            cmap = build_codemap(text, ext, debugging=debugging)
            if cmap is not None:
                new_rec["map_served"] = True
                state["saved_chars"] += len(text) - len(cmap)
                state["codemaps"] += 1
                return cmap

    # ---- 4. duplicate collapse for big non-code reads --------------------
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

    # Served the file in full -> a valid base for a future differential read.
    new_rec["served_full"] = True
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


SCRIPT_STYLE = re.compile(r"(?is)<script\b.*?</script>|<style\b.*?</style>|<!--.*?-->")
FENCED_JSON = re.compile(r"```(?:json)?\s*\n(\{.*?\}|\[.*?\])\s*\n```", re.S)


def _minify_fenced_json(text):
    """Minify JSON inside ```json fences (common in MCP markdown). Lossless:
    only well-formed JSON blocks are rewritten, and only when smaller."""
    def repl(m):
        block = m.group(1)
        try:
            obj = json.loads(block)
        except Exception:
            return m.group(0)
        compact = json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
        if len(compact) < len(block) * 0.95:
            return "```json\n" + compact + "\n```"
        return m.group(0)
    return FENCED_JSON.sub(repl, text)


def handle_web(payload, state):
    """WebFetch / WebSearch output.

    WebFetch returns a page rendered to markdown, which is exactly what the
    structural map already understands: keep the headings, elide the prose,
    say how to get it back. WebSearch returns a result list, where the win is
    repetition rather than structure. Both are tee-backed and both refuse to
    apply unless they clear MIN_SAVING_RATIO, so a page that does not compress
    passes through untouched."""
    text = extract_text(payload)
    if not text or len(text) < WEB_MIN_CHARS:
        return None
    tool = payload.get("tool_name")
    out = None
    what = ""

    if tool == "WebFetch":
        cmap = build_codemap(text, ".md")
        if cmap is not None:
            out, what = cmap, "page outline"
    if out is None:
        collapsed = collapse_duplicate_lines(
            text, COLLAPSE_MIN_RUN, preserve_read_numbers=False
        )
        if collapsed is not None:
            out, what = collapsed, "repeated lines collapsed"

    if out is None or len(out) >= len(text) * (1 - MIN_SAVING_RATIO):
        return None

    tee = write_tee(text)
    header = "[nestor-lean] {} ({} -> {} chars).".format(what, len(text), len(out))
    if tee:
        header += " Full text saved to {} — Read it for anything elided.".format(tee)
    header += "\n"
    state["saved_chars"] += len(text) - len(out) - len(header)
    state["web_compressions"] = state.get("web_compressions", 0) + 1
    return header + out


def handle_glob(payload, state):
    """Path lists fold hard: most entries share a directory prefix.

    Emits one line per directory followed by its bare filenames, which keeps
    every path recoverable by concatenation while dropping the repeated stem."""
    text = extract_text(payload)
    if not text or len(text) < GLOB_MIN_CHARS:
        return None
    lines = [l for l in text.splitlines() if l.strip()]
    if len(lines) < 8:
        return None

    groups = []          # preserve first-seen directory order
    index = {}
    for line in lines:
        path = line.strip()
        sep = "\\" if path.rfind("\\") > path.rfind("/") else "/"
        cut = path.rfind(sep)
        if cut <= 0:
            head, tail = "", path
        else:
            head, tail = path[:cut], path[cut + 1:]
        if head not in index:
            index[head] = len(groups)
            groups.append((head, []))
        groups[index[head]][1].append(tail)

    if len(groups) >= len(lines) * 0.7:
        return None  # little shared structure, folding buys nothing

    parts = []
    for head, names in groups:
        parts.append("{}/  ({} files)".format(head or ".", len(names)))
        parts.append("    " + "  ".join(names))
    out = "\n".join(parts)
    if len(out) >= len(text) * (1 - MIN_SAVING_RATIO):
        return None

    header = (
        "[nestor-lean] {} paths folded by directory ({} dirs). Each full path "
        "is its directory line joined to a name below it.\n".format(
            len(lines), len(groups))
    )
    state["saved_chars"] += len(text) - len(out) - len(header)
    state["glob_folds"] = state.get("glob_folds", 0) + 1
    return header + out


def handle_report(payload, state):
    """Subagent reports and task output: prose we cannot safely restructure.

    Only the unambiguous win is taken — runs of identical lines — and only
    when it clears the saving floor. Everything else passes through, because a
    subagent's final report is often the only record of work the main agent
    never saw."""
    text = extract_text(payload)
    if not text or len(text) < REPORT_MIN_CHARS:
        return None
    collapsed = collapse_duplicate_lines(
        text, COLLAPSE_MIN_RUN, preserve_read_numbers=False
    )
    if collapsed is None or len(collapsed) >= len(text) * (1 - MIN_SAVING_RATIO):
        return None
    tee = write_tee(text)
    header = "[nestor-lean] repeated lines collapsed."
    if tee:
        header += " Full report saved to {}.".format(tee)
    header += "\n"
    state["saved_chars"] += len(text) - len(collapsed) - len(header)
    state["report_collapses"] = state.get("report_collapses", 0) + 1
    return header + collapsed


def handle_mcp(payload, state):
    """Deterministic, tee-backed compression of MCP tool output.

    MCP servers routinely return large JSON or HTML. We shrink it losslessly-
    or-recoverably: minify pretty-printed JSON, drop <script>/<style>/comment
    blocks the model never needs, and collapse runs of identical lines. The
    full output is teed to a file referenced in the header. Nothing is
    paraphrased; structured values are preserved exactly (minified JSON is
    still valid JSON)."""
    text = extract_text(payload)
    if not text or len(text) < MCP_MIN_CHARS:
        return None
    original = text
    out = text
    notes = []

    stripped = out.strip()
    if stripped[:1] in ("{", "["):
        try:
            obj = json.loads(stripped)
            compact = json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
            if len(compact) < len(out) * 0.95:
                out = compact
                notes.append("JSON minified")
        except Exception:
            pass
    elif "```" in out:
        new = _minify_fenced_json(out)
        if len(new) < len(out) * 0.95:
            out = new
            notes.append("fenced JSON minified")

    low = out.lower()
    if "<script" in low or "<style" in low or "<!--" in low:
        new = SCRIPT_STYLE.sub("", out)
        if len(new) < len(out) * 0.98:
            out = new
            notes.append("script/style/comments stripped")

    collapsed = collapse_duplicate_lines(out, COLLAPSE_MIN_RUN, preserve_read_numbers=False)
    if collapsed is not None and len(collapsed) < len(out):
        out = collapsed
        notes.append("duplicate lines collapsed")

    if not notes or len(out) >= len(original) * (1 - MIN_SAVING_RATIO):
        return None

    tee = write_tee(original)
    header = "[nestor-lean] MCP output compressed ({}): {} -> {} chars.".format(
        ", ".join(notes), len(original), len(out)
    )
    if tee:
        header += " Full untouched output saved to {} — Read it for anything dropped.".format(tee)
    header += "\n"
    state["saved_chars"] += len(original) - len(out) - len(header)
    state["mcp_compressions"] = state.get("mcp_compressions", 0) + 1
    return header + out


INTERIOR_PAD = re.compile(r"(?<=\S) {3,}(?=\S)")


def normalize_layout(text):
    """Drop layout-only whitespace: trailing spaces and interior column padding.

    Content-preserving by construction — every non-space character survives in
    order, and leading indentation (meaningful in JSON, YAML, tracebacks) is
    untouched. Only alignment is lost, which costs a model nothing.

    Measured worth ~11% of PowerShell output, which is mostly Format-Table
    padding. That is below MIN_SAVING_RATIO, so this deliberately does not go
    through the saving gate: the gate exists to reject transforms that might
    drop information for too little gain, and this one cannot drop any.
    """
    out = []
    changed = False
    for raw in text.split("\n"):
        line = raw.rstrip("\r")
        cr = raw != line
        new = INTERIOR_PAD.sub(" ", line.rstrip())
        if new != line:
            changed = True
        out.append(new + ("\r" if cr else ""))
    if not changed:
        return None
    return "\n".join(out)


def handle_bash(payload, state):
    text = extract_text(payload)
    if not text or len(text) < BASH_MIN_CHARS:
        return None
    command = str((payload.get("tool_input") or {}).get("command") or "")

    # Layout normalisation first: every later tier then works on, and reports,
    # the smaller text. On its own it is only emitted when it clears the header
    # cost, so a line with one stray trailing space is left alone.
    flattened = normalize_layout(text)
    layout_saved = len(text) - len(flattened) if flattened is not None else 0
    if flattened is not None and layout_saved > 0:
        text = flattened

    # ---- 0. real rtk parser via `rtk pipe` (no re-execution) ------------
    # If the rtk binary is available and the command maps to one of its
    # stream filters, reshape the captured output with rtk's own parser.
    # The real command already ran untouched; we only change what enters
    # context. Backed by a tee so nothing is unrecoverable.
    if RTK_ENABLED and command:
        try:
            import rtk as _rtk
            rtk_path = _rtk.find_rtk()
            filt = _rtk.pipe_filter_for(command) if rtk_path else None
            if rtk_path and filt:
                compressed = _rtk.run_pipe(rtk_path, filt, text)
                if compressed and len(compressed) < len(text) * (1 - MIN_SAVING_RATIO):
                    tee = write_tee(text)
                    header = "[nestor-lean] rtk:{} filter applied ({} -> {} lines).".format(
                        filt, len(text.splitlines()), len(compressed.splitlines())
                    )
                    if tee:
                        header += " Full output saved to {} — Read it for any dropped detail.".format(tee)
                    header += "\n"
                    state["saved_chars"] += len(text) - len(compressed) - len(header)
                    state["rtk_pipes"] = state.get("rtk_pipes", 0) + 1
                    return header + compressed
        except Exception:
            pass

    # ---- 1. format-aware route (built-in), backed by a full-output tee ---
    if BASH_ROUTES_ENABLED and command:
        for route in BASH_ROUTES:
            if not route.matches(command):
                continue
            result = route.transform(text)
            if not result:
                break
            compressed, kept_desc = result
            if len(compressed) >= len(text) * (1 - MIN_SAVING_RATIO):
                break
            tee = write_tee(text)
            header = "[nestor-lean] {} output reduced to {} ({} -> {} lines).".format(
                route.name, kept_desc, len(text.splitlines()), len(compressed.splitlines())
            )
            if tee:
                header += " Full output saved to {} — Read it for any dropped detail.".format(tee)
            header += "\n"
            state["saved_chars"] += len(text) - len(compressed) - len(header)
            state["bash_routes"] = state.get("bash_routes", 0) + 1
            return header + compressed

    # ---- 2. generic fallback: collapse runs of identical lines (lossless) -
    collapsed = collapse_duplicate_lines(
        text, COLLAPSE_MIN_RUN, preserve_read_numbers=False
    )
    if collapsed is not None and len(collapsed) < len(text) * (1 - 0.15):
        header = (
            "[nestor-lean] command output collapsed (identical consecutive "
            "lines shown once with explicit repeat counts).\n"
        )
        state["saved_chars"] += len(text) - len(collapsed) - len(header)
        state["bash_collapses"] += 1
        return header + collapsed

    # ---- 3. layout normalisation alone -----------------------------------
    # Nothing structural applied, but the padding strip already paid for
    # itself. No saving-ratio gate here: no characters of content were
    # removed, so there is nothing to trade off against.
    if layout_saved > LAYOUT_MIN_SAVING:
        header = (
            "[nestor-lean] layout whitespace removed (trailing spaces and "
            "column padding; every character of content is unchanged).\n"
        )
        if layout_saved > len(header):
            state["saved_chars"] += layout_saved - len(header)
            state["layout_strips"] = state.get("layout_strips", 0) + 1
            return header + text
    return None


# ----------------------------------------------------------------- main ----

def main():
    # Checked per invocation, not at import: /nestor-lean:off must take effect
    # on the very next tool call without a restart.
    if switch.is_disabled():
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

    if event == "PreToolUse":
        # Opt-in: rewrite a supported simple command to its rtk equivalent so
        # its output is born compressed. Off by default because it changes the
        # command that actually executes (rtk re-runs it). Enable with
        # NESTOR_LEAN_RTK_REWRITE=1.
        if (
            os.environ.get("NESTOR_LEAN_RTK_REWRITE") == "1"
            and payload.get("tool_name") in SHELL_TOOLS
        ):
            command = str((payload.get("tool_input") or {}).get("command") or "")
            try:
                import rtk as _rtk
                rtk_path = _rtk.find_rtk()
                if rtk_path and command:
                    new_cmd = _rtk.rewrite_command(rtk_path, command)
                    if new_cmd:
                        state = load_state(key)
                        state["rtk_rewrites"] = state.get("rtk_rewrites", 0) + 1
                        save_state(key, state)
                        print(json.dumps({
                            "hookSpecificOutput": {
                                "hookEventName": "PreToolUse",
                                "updatedInput": {"command": new_cmd},
                            }
                        }))
            except Exception:
                pass
        return

    if event in ("PreCompact", "SessionEnd"):
        # Compaction: everything the model "already saw" may be gone.
        # Session end: dedup knowledge must not leak into a resumed session.
        # Savings counters survive either way so /nestor-lean:gain still
        # reports them (files are pruned after 48h).
        state = load_state(key)
        state["reads"] = {}
        save_state(key, state)
        # Diff/dedup bases are only valid against content still in the model's
        # context — after a compaction or session end that is gone, so drop
        # this context's stored blobs too.
        try:
            cdir = os.path.join(blobs_root(), key)
            if os.path.isdir(cdir):
                for n in os.listdir(cdir):
                    try:
                        os.remove(os.path.join(cdir, n))
                    except Exception:
                        pass
                try:
                    os.rmdir(cdir)
                except Exception:
                    pass
        except Exception:
            pass
        return

    tool = payload.get("tool_name")
    state = load_state(key)
    replacement = None
    error = None
    try:
        if tool == "Read":
            replacement = handle_read(payload, state, key)
        elif tool == "Grep":
            replacement = handle_grep(payload, state)
        elif tool in SHELL_TOOLS:
            replacement = handle_bash(payload, state)
        elif WEB_ENABLED and tool in ("WebFetch", "WebSearch"):
            replacement = handle_web(payload, state)
        elif tool == "Glob":
            replacement = handle_glob(payload, state)
        elif tool in ("Agent", "Task", "TaskOutput"):
            replacement = handle_report(payload, state)
        elif MCP_ENABLED and tool and tool.startswith("mcp__"):
            replacement = handle_mcp(payload, state)
    except Exception as e:
        replacement = None
        error = "{}: {}".format(type(e).__name__, e)

    save_state(key, state)

    if dump:
        try:
            with open(dump, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "_lean_decision": bool(replacement),
                    "error": error,
                    "key": key,
                    "reads_after": len(state.get("reads", {})),
                    "data_dir": data_dir(),
                }) + "\n")
        except Exception:
            pass
    prune_old_sessions()
    prune_blobs()

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
