# nestor-lean

[![MIT License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
![Zero Dependencies](https://img.shields.io/badge/dependencies-zero-orange.svg)

Input-side token compression for Claude Code. Where [rolling-context](https://github.com/NodeNestor/claude-rolling-context) manages tokens *already in* your context, nestor-lean stops waste **before it enters** — and when both are installed they actively cooperate (see below). Verified end-to-end in live Claude Code sessions.

## What it does (v0.2)

### 1. Read dedup-by-reference

Claude re-reads the same file constantly. nestor-lean hashes the file on every `Read`; an identical re-read (same path, offset/limit, unchanged content, within a 20-minute window, in the **same agent context**) is replaced with an orienting note instead of the full bytes:

```
[nestor-lean] Duplicate read skipped — you already read this exact file ~4 min ago
in this conversation and it has NOT changed since.
  file:   E:\Repos\myapp\src\router.py
  size:   18,692 bytes, 401 lines, digest 3fa1b2c9d0ee
  it contains (outline of your earlier read):
    12: class Router:
    48: def resolve(self, request):
    93: def register(self, route, handler):
Use your earlier read of this file from this conversation. If that content is no
longer in your context (e.g. it was compacted away), run the exact same Read
again — the full contents will be returned.
```

**Escape valve:** after a reference, the very next identical Read returns the full content and resets the cycle — the model can never be stranded without the bytes.

### 2. Codemap — structural maps for exploration reads

When the model reads a **large code file** while *exploring* (not while error-hunting — intent is inferred from the tail of the agent's own transcript), the read is replaced by a structural map: every signature/import line with its **real line number**, implementation bodies elided with explicit counts:

```
[nestor-lean] STRUCTURAL MAP (exploration read) — implementation bodies elided, ...
    12→class OrderService:
    14→    def place_order(self, cart, user):
        … +38 lines (implementation)
    53→    def cancel_order(self, order_id):
```

The header says exactly how to get the full file (re-run the same Read), and the same escape valve applies. Any error-hunting vocabulary in the recent conversation ("exception", "traceback", "failing"...) disables codemap entirely — when debugging, the model gets real bytes. Parsers cover Python, JS/TS, C#/Razor/Java, Go/Rust, C/C++ and more.

### 3. Duplicate collapse — reads, greps, and command output

- **Read** of duplicate-heavy non-code files (logs, dumps): runs of identical consecutive lines collapse to one line + `… [previous line repeats 199x, through line 201]` — real line numbers preserved, so `offset`/`limit` re-reads still make sense.
- **Grep** (content mode): identical match text within a file collapses with counts; matches capped per file with an explicit note. **Never** applied when the pattern looks like an error hunt — then every occurrence may matter.
- **Bash**: large command output gets `uniq -c` style collapse (`Restoring packages...` × 300 → one line + repeat marker).

All transforms only fire on large outputs with meaningful savings (>15–20%), always announce themselves, and always explain how to recover the elided content.

## Context awareness — how it knows what the model still has

A reference note is only safe if the earlier read is still in the model's context. Three mechanisms guard that:

1. **Per-agent scoping** — state is keyed by `transcript_path`, which is unique per agent. Simultaneous subagents each get their own dedup knowledge; agent A's read is never "already seen" for agent B.
2. **`PreCompact` hook** — when Claude Code compacts (auto or manual), all dedup knowledge for that context is cleared before the compaction runs. `SessionEnd` deletes the state entirely.
3. **rolling-context integration** — if the rolling-context proxy is running, nestor-lean polls its `/lean/status` endpoint (cached, 0.25s timeout, fail-open) for the timestamp of the last compression injection. Any read recorded *before* that moment is never turned into a reference — full content is served instead. The signal is conservative (global across sessions): a compression anywhere only costs savings, never correctness.

## What it never touches

- Reads of changed or never-before-seen files — full fidelity always.
- Code file *content* — signature lines in codemaps are verbatim; nothing is reformatted or paraphrased, so `Edit`'s exact-string matching keeps working (and the codemap header explicitly says to re-read before editing).
- Error-hunting greps, `files_with_matches`/`count` modes, small outputs.

Everything fails open: any error in the hook and the original output passes through unchanged.

## Install

```
/plugin marketplace add https://github.com/NodeNestor/nestor-plugins
/plugin install nestor-lean
```

Requires Python 3.7+ on PATH (stdlib only, no pip install). Manual/dev install: `claude --plugin-dir path/to/nestor-lean`.

## Observe the savings

```
/nestor-lean:gain
```

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `NESTOR_LEAN_DISABLE` | — | `1` disables all transforms |
| `NESTOR_LEAN_CODEMAP` | `1` | `0` disables codemap only |
| `NESTOR_LEAN_DEDUP_WINDOW` | `1200` | Seconds a read stays dedupable |
| `NESTOR_LEAN_MIN_DEDUP_CHARS` | `1500` | Reads smaller than this never dedup |
| `NESTOR_LEAN_CODEMAP_MIN_CHARS` | `12000` | Code reads smaller than this never map |
| `NESTOR_LEAN_GREP_MIN_CHARS` | `4000` | Grep outputs smaller than this never compress |
| `NESTOR_LEAN_GREP_PER_FILE_CAP` | `25` | Max matches kept per file |
| `NESTOR_LEAN_BASH_MIN_CHARS` | `4000` | Bash outputs smaller than this never collapse |
| `NESTOR_LEAN_COLLAPSE_MIN_RUN` | `5` | Min identical consecutive lines to collapse |
| `NESTOR_LEAN_RC_URL` | `http://127.0.0.1:5588` | rolling-context proxy to consult |

## How it works

One `PostToolUse` hook (plus `PreCompact`/`SessionEnd`) in pure-stdlib Python. Replacements are rebuilt in the **same shape as the tool's original output** — Claude Code validates `updatedToolOutput` against each tool's output schema and silently discards mismatches, so shape preservation is load-bearing. State is one atomic-write JSON file per agent context under `${CLAUDE_PLUGIN_DATA}`, pruned after 48h.

## Test

```
python test/test_dispatch.py
```

Covers the dedup/escape-valve cycle, PreCompact/SessionEnd invalidation, rolling-context invalidation (against a fake proxy), codemap intent gating, collapse transforms, error-hunt passthrough, shape preservation, and the disable switches.

## License

MIT
