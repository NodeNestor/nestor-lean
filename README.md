# nestor-lean

[![MIT License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
![Zero Python deps](https://img.shields.io/badge/python%20deps-zero-orange.svg)

Input-side token compression for Claude Code. Where [rolling-context](https://github.com/NodeNestor/claude-rolling-context) manages tokens *already in* your context, nestor-lean stops waste **before it enters** — and the two cooperate when both are installed. Everything is lossless-or-recoverable: the model either sees the real bytes, or an honest pointer/diff/tee to bytes it can get back with one more call. Every transform fails open.

## What it does

### 1. Read dedup-by-reference
An identical re-read of an unchanged file (same path/offset/limit, same content, same agent, within 20 min) is replaced by an orienting note (age, size, digest, outline) pointing at your earlier read. Escape valve: the next identical Read returns full content.

### 2. Differential reads (changed files → just the diff)
The big one. When you read a file you read before **and it changed**, nestor-lean sends only the changed hunks as a unified diff with real line numbers — everything else is "unchanged from what you already have." Read → edit → read-to-verify loops, which normally resend the whole file, now resend ~the diff. Only fires when your earlier read was served in full and is provably still in context (guarded by compaction + rolling-context signals); wholesale rewrites fall back to a full read; escape valve always applies.

### 3. Project codemap (a skill the model invokes)
`codemap` maps an **entire directory in one call** — folder tree, file-type counts, every code file's signatures with real line numbers, bodies elided. Measured 84% char reduction on a real 51-file repo. For orientation; the skill tells the model to Read real files before editing. Also fires automatically on large single-file code reads while *exploring* (never while error-hunting).

### 4. Command output compression — real rtk + built-in routes
Three tiers, best first:

- **rtk (the real binary).** [rtk](https://github.com/rtk-ai/rtk) is a maintained Rust tool with ~50 per-command output filters. nestor-lean uses it the **safe way**: `rtk pipe --filter <name>` reshapes the *already-captured* output — your command runs completely untouched, we only shrink what enters context. Covers pytest, vitest, tsc, git log/diff/status, cargo/go test, mypy, ruff, prettier, grep, and more. Measured: a 300-test pytest run 8,679 → 322 chars (96%). rtk is lazy-downloaded (checksum-verified) on first session **only if you opt in** with `NESTOR_LEAN_RTK_DOWNLOAD=1`, or point `NESTOR_LEAN_RTK` at an existing binary.
- **Built-in routes** (no binary needed): `dotnet build`, test runners, and `npm/pnpm/yarn/pip install` — keep errors/warnings/summary, drop the spam, dedupe repeated diagnostics.
- **Generic collapse**: any command's runs of identical consecutive lines collapse with explicit counts.

All command compression is backed by a **tee file**: the full output is written to disk and its path is in the header, so nothing aggressive is ever unrecoverable — one Read gets it all back.

**Opt-in max coverage:** set `NESTOR_LEAN_RTK_REWRITE=1` to also rewrite supported *simple* commands to their rtk equivalent before they run (`git status` → `rtk git status`), covering rtk's full ~50-command set born-compressed. Off by default because it changes the command that actually executes.

### 5. Grep compression
Identical matches within a file collapse with counts; per-file caps. Skipped entirely for error-hunting patterns (every occurrence may matter).

### 6. MCP tool-output compression
MCP servers routinely return large JSON or HTML. nestor-lean shrinks it **deterministically and losslessly-or-recoverably**: minify pretty-printed JSON (whole-output or inside ```json fences — still valid JSON, exact values preserved), drop `<script>`/`<style>`/comment blocks the model never needs, and collapse runs of identical lines. Tee-backed, so the full untouched output is one Read away. Structured data is never paraphrased. Measured live: a script/style-heavy HTML payload from a real MCP tool, 22,711 → ~750 chars (97%). Mixed outputs containing images are left untouched.

## Context awareness

- **Per-agent scoping** — state keyed by `transcript_path`; simultaneous subagents never share dedup/diff knowledge.
- **Compaction** — a `PreCompact` hook clears "already seen" knowledge and stored diff bases before compaction runs; `SessionEnd` too.
- **rolling-context** — polls the proxy's `/lean/status` (v1.9.0+); any read recorded before the last compression injection is never turned into a reference or diff base — full content is served instead.

## Install

```
/plugin marketplace add https://github.com/NodeNestor/nestor-plugins
/plugin install nestor-lean
```

Requires Python 3.7+ (stdlib only). To enable the real-rtk tier, either install rtk yourself and it's auto-detected, or set `NESTOR_LEAN_RTK_DOWNLOAD=1` to let the SessionStart hook fetch the checksum-verified binary into the plugin's data dir.

## Observe the savings

```
/nestor-lean:gain
```

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `NESTOR_LEAN_DISABLE` | — | `1` disables everything |
| `NESTOR_LEAN_DIFF` | `1` | `0` disables differential reads |
| `NESTOR_LEAN_CODEMAP` | `1` | `0` disables auto single-file codemap |
| `NESTOR_LEAN_RTK_PIPE` | `1` | `0` disables the rtk pipe tier |
| `NESTOR_LEAN_RTK_DOWNLOAD` | — | `1` lets SessionStart download rtk |
| `NESTOR_LEAN_RTK_REWRITE` | — | `1` enables PreToolUse command rewriting (re-executes via rtk) |
| `NESTOR_LEAN_RTK` | — | explicit path to an rtk binary |
| `NESTOR_LEAN_BASH_ROUTES` | `1` | `0` disables built-in command routes |
| `NESTOR_LEAN_MCP` | `1` | `0` disables MCP output compression |
| `NESTOR_LEAN_DEDUP_WINDOW` | `1200` | seconds a read stays dedup/diff-able |
| `NESTOR_LEAN_GREP_PER_FILE_CAP` | `25` | max grep matches kept per file |

## How it works

Hooks: `PostToolUse` (Read/Grep/Bash/MCP transforms), `PreToolUse` (opt-in rtk rewrite), `PreCompact`/`SessionEnd` (invalidation), `SessionStart` (opt-in rtk bootstrap). Replacements are rebuilt in each tool's original output shape (Claude Code validates `updatedToolOutput` against the tool's schema). State is per-agent JSON + content blobs under `${CLAUDE_PLUGIN_DATA}`, pruned after 48h. Pure-stdlib Python; the only optional external piece is the rtk binary, which is never required.

## Test

```
python test/test_dispatch.py
```

Covers dedup + escape valve, differential reads (small change → diff, wholesale → full), codemap intent gating, all three command tiers (mock rtk, built-in routes, collapse), rtk PreToolUse rewrite, grep, PreCompact/SessionEnd + rolling-context invalidation, shape preservation, and every disable switch.

## License

MIT (the rtk binary is separately licensed by its authors, Apache-2.0, and downloaded only on opt-in).
