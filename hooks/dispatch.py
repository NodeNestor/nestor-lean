#!/usr/bin/env python3
"""nestor-lean PostToolUse dispatcher.

Two token-saving transforms, both fail-open (any error -> tool output passes
through unchanged):

  Read  -> dedup-by-reference: if the exact same file (same path, offset,
           limit, content digest) was already read this session inside the
           dedup window, replace the output with a short note pointing the
           model at its earlier read. If the model re-reads after receiving a
           reference, the full content is served (it clearly needs it) and
           the record resets. At most every other identical read is a
           reference, so the model can never get stuck without content.

  Grep  -> compression of content-mode output: identical match text repeated
           across a file is collapsed with a count, and matches are capped
           per file. Only applied when the output is large and the saving is
           meaningful; the note at the top tells the model what was elided
           and how to get the rest.

Zero dependencies (stdlib only). State is one small JSON file per session.
"""
import hashlib
import json
import os
import re
import sys
import time

DEDUP_WINDOW = int(os.environ.get("NESTOR_LEAN_DEDUP_WINDOW", "1200"))  # seconds
MIN_DEDUP_CHARS = int(os.environ.get("NESTOR_LEAN_MIN_DEDUP_CHARS", "1500"))
GREP_MIN_CHARS = int(os.environ.get("NESTOR_LEAN_GREP_MIN_CHARS", "4000"))
GREP_PER_FILE_CAP = int(os.environ.get("NESTOR_LEAN_GREP_PER_FILE_CAP", "25"))
MIN_SAVING_RATIO = 0.20
HASH_CAP_BYTES = 4 * 1024 * 1024  # hash at most the first 4MB of a file
STATE_MAX_AGE = 48 * 3600


def data_dir():
    base = os.environ.get("CLAUDE_PLUGIN_DATA") or os.path.join(
        os.path.expanduser("~"), ".nestor-lean"
    )
    d = os.path.join(base, "sessions")
    os.makedirs(d, exist_ok=True)
    return d


def state_path(session_id):
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", session_id or "unknown")
    return os.path.join(data_dir(), safe + ".json")


def load_state(session_id):
    try:
        with open(state_path(session_id), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"reads": {}, "saved_chars": 0, "read_refs": 0, "grep_compressions": 0}


def save_state(session_id, state):
    try:
        with open(state_path(session_id), "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception:
        pass


def prune_old_sessions():
    # opportunistic, best-effort cleanup of stale session state
    try:
        now = time.time()
        for name in os.listdir(data_dir()):
            p = os.path.join(data_dir(), name)
            if now - os.path.getmtime(p) > STATE_MAX_AGE:
                os.remove(p)
    except Exception:
        pass


def extract_text(payload):
    """Best-effort extraction of the tool output as a string."""
    out = payload.get("tool_output")
    if out is None:
        out = payload.get("tool_response")
    if isinstance(out, str):
        return out
    if isinstance(out, dict):
        for key in ("output", "content", "text", "result", "stdout", "file"):
            v = out.get(key)
            if isinstance(v, str):
                return v
            if isinstance(v, dict) and isinstance(v.get("content"), str):
                return v["content"]
        # content-block list shape: [{"type": "text", "text": ...}, ...]
        blocks = out.get("content")
        if isinstance(blocks, list):
            texts = [b.get("text") for b in blocks if isinstance(b, dict) and isinstance(b.get("text"), str)]
            if texts:
                return "\n".join(texts)
    return None


def file_digest(path):
    st = os.stat(path)
    h = hashlib.sha256()
    h.update(str(st.st_size).encode())
    with open(path, "rb") as f:
        h.update(f.read(HASH_CAP_BYTES))
    return h.hexdigest()


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
    except Exception:
        return None

    key = "{}|{}|{}".format(fp, ti.get("offset"), ti.get("limit"))
    now = time.time()
    rec = state["reads"].get(key)

    if rec and rec.get("digest") == digest and (now - rec.get("ts", 0)) <= DEDUP_WINDOW:
        if rec.get("ref_served"):
            # Model re-read after receiving a reference: it needs the real
            # content. Serve it in full and reset the record.
            rec["ts"] = now
            rec["ref_served"] = False
            return None
        age_min = max(1, int((now - rec.get("ts", now)) / 60))
        rec["ref_served"] = True
        rec["ts"] = now
        saved = approx_len if approx_len else min(os.path.getsize(fp), HASH_CAP_BYTES)
        state["saved_chars"] += max(saved - 300, 0)
        state["read_refs"] += 1
        return (
            "[nestor-lean] Duplicate read skipped: you already read this exact file "
            "earlier in this session (~{} min ago) and it has NOT changed since "
            "(digest {}). Refer to your earlier read of:\n  {}\n"
            "If that content is no longer available in your context, run the same "
            "Read again and the full contents will be returned.".format(
                age_min, digest[:12], fp
            )
        )

    state["reads"][key] = {
        "digest": digest,
        "ts": now,
        "first_ts": (rec or {}).get("first_ts", now),
        "ref_served": False,
    }
    return None


GREP_LINE = re.compile(r"^(.*?):(\d+):(.*)$")


def handle_grep(payload, state):
    ti = payload.get("tool_input") or {}
    if ti.get("output_mode") != "content":
        return None
    text = extract_text(payload)
    if not text or len(text) < GREP_MIN_CHARS:
        return None

    lines = text.splitlines()
    out_lines = []
    per_file_count = {}
    per_file_hidden = {}
    seen_content = {}  # (file, stripped content) -> first output index
    collapsed = 0

    for line in lines:
        m = GREP_LINE.match(line)
        if not m:
            out_lines.append(line)
            continue
        fname, _lineno, content = m.group(1), m.group(2), m.group(3)
        ckey = (fname, content.strip())

        if ckey in seen_content and content.strip():
            idx = seen_content[ckey]
            counts = out_lines[idx][1]
            counts[0] += 1
            collapsed += 1
            continue

        n = per_file_count.get(fname, 0) + 1
        per_file_count[fname] = n
        if n > GREP_PER_FILE_CAP:
            per_file_hidden[fname] = per_file_hidden.get(fname, 0) + 1
            continue
        counts = [1]
        seen_content[ckey] = len(out_lines)
        out_lines.append((line, counts))

    rendered = []
    for item in out_lines:
        if isinstance(item, str):
            rendered.append(item)
        else:
            line, counts = item
            if counts[0] > 1:
                rendered.append("{}   [identical match repeats {}x in this file]".format(line, counts[0]))
            else:
                rendered.append(line)
    for fname, hidden in per_file_hidden.items():
        rendered.append(
            "[nestor-lean] {}: +{} more matches capped (run a narrower grep on this file for the rest)".format(
                fname, hidden
            )
        )

    new_text = "\n".join(rendered)
    if len(new_text) >= len(text) * (1 - MIN_SAVING_RATIO):
        return None

    header = (
        "[nestor-lean] grep output compressed: {} -> {} lines "
        "(identical matches collapsed with counts; capped at {} matches/file; "
        "line numbers shown are each match's first occurrence).\n".format(
            len(lines), len(rendered), GREP_PER_FILE_CAP
        )
    )
    state["saved_chars"] += len(text) - len(new_text) - len(header)
    state["grep_compressions"] += 1
    return header + new_text


def main():
    if os.environ.get("NESTOR_LEAN_DISABLE") == "1":
        return
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return

    tool = payload.get("tool_name")
    session_id = payload.get("session_id", "unknown")
    state = load_state(session_id)

    replacement = None
    try:
        if tool == "Read":
            replacement = handle_read(payload, state)
        elif tool == "Grep":
            replacement = handle_grep(payload, state)
    except Exception:
        replacement = None

    save_state(session_id, state)
    prune_old_sessions()

    if replacement is not None:
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PostToolUse",
                        "updatedToolOutput": replacement,
                    }
                }
            )
        )


if __name__ == "__main__":
    main()
