# nestor-lean

[![MIT License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
![Zero Python deps](https://img.shields.io/badge/python%20deps-zero-orange.svg)

Input-side token compression for Claude Code. Where [rolling-context](https://github.com/NodeNestor/claude-rolling-context) manages tokens *already in* your context, nestor-lean stops waste **before it enters** — and the two cooperate when both are installed. Everything is lossless-or-recoverable: the model either sees the real bytes, or an honest pointer/diff/tee to bytes it can get back with one more call. Every transform fails open.

## What it does

### 1. Read dedup-by-reference
An identical re-read of an unchanged file (same path/offset/limit, same content, same agent, within 20 min) is replaced by an orienting note (age, size, digest, outline) pointing at your earlier read. Escape valve: the next identical Read returns full content.

### 2. Differential reads (changed files → just the diff)
The big one. When you read a file you read before **and it changed**, nestor-lean sends only the changed hunks as a unified diff with real line numbers — everything else is "unchanged from what you already have." Read → edit → read-to-verify loops, which normally resend the whole file, now resend ~the diff. Only fires when your earlier read was served in full and is provably still in context (guarded by compaction + rolling-context signals); wholesale rewrites fall back to a full read; escape valve always applies.

### 3b. Structural maps for prose and markup

The same engine that maps code maps **markdown, HTML/Vue/Astro/SVG and CSS/SCSS**:
headings, landmark tags and selectors are kept with real line numbers, the prose
or declarations between them are elided with counts. Measured on a real corpus,
non-code files are a quarter of all first-read bytes and markdown is the single
largest non-code extension, so this is not a side case.

Intent **scales the threshold** rather than switching mapping off. Treating
"error-hunting" as a veto looked safe but cost most of the opportunity, because
that vocabulary ("error", "failed", "debug") shows up in ordinary narration far
more often than real debugging does. While the agent looks like it is
investigating, only files past a much larger floor are mapped — where a full
read would dominate the context anyway — and the escape valve still returns full
content on the next identical Read.

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

### 5b. PowerShell, web, path lists and subagent reports

- **PowerShell** gets everything Bash gets. It was previously absent from the
  hook matcher, so on Windows — where most commands go through that tool — a
  large share of shell output was never even seen.
- **WebFetch** returns a page rendered to markdown, so it gets the markdown
  outline treatment; **WebSearch** gets repetition collapse. Both tee-backed.
- **Glob** path lists fold by directory: the shared stem is written once and
  every filename listed under it, so each path is still recoverable by
  concatenation.
- **Subagent reports and task output** get repetition collapse only. A
  subagent's final report is often the only record of work the main agent never
  saw, so nothing there is restructured or summarized.

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

## Turning it off (and back on)

```
/nestor-lean:off      # stop compressing, from the very next tool call
/nestor-lean:on       # resume
```

No restart, no config edit. Everything passes through untouched while off, and
`/nestor-lean:gain` says so rather than just reporting zeros. This is the switch
to reach for when a tool result looks wrong and you want to rule nestor-lean out
in one command — which matters more than the savings do.

`NESTOR_LEAN_DISABLE=1` still works and still wins over the switch, but it lives
in settings.json env and needs a Claude Code restart, so it suits permanent
opt-out rather than a mid-session check.

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
| `NESTOR_LEAN_WEB` | `1` | `0` disables WebFetch/WebSearch compression |
| `NESTOR_LEAN_DEDUP_WINDOW` | `1200` | seconds a read stays dedup/diff-able |
| `NESTOR_LEAN_GREP_PER_FILE_CAP` | `25` | max grep matches kept per file |
| `NESTOR_LEAN_BASH_MIN_CHARS` | `1500` | shell output floor |
| `NESTOR_LEAN_GREP_MIN_CHARS` | `2000` | grep output floor |
| `NESTOR_LEAN_MCP_MIN_CHARS` | `3000` | MCP output floor |
| `NESTOR_LEAN_WEB_MIN_CHARS` | `3000` | WebFetch/WebSearch floor |
| `NESTOR_LEAN_GLOB_MIN_CHARS` | `2000` | Glob path-list floor |
| `NESTOR_LEAN_REPORT_MIN_CHARS` | `4000` | subagent report floor |
| `NESTOR_LEAN_CODEMAP_MIN_CHARS` | `12000` | map floor while exploring |
| `NESTOR_LEAN_CODEMAP_DEBUG_MIN_CHARS` | `40000` | map floor while error-hunting |

Every floor is a cheap pre-filter, not the safety mechanism. `MIN_SAVING_RATIO`
is: any transform that fails to save 20% is thrown away and the original passes
through, so lowering a floor costs a little work, never correctness.

## How it works

Hooks: `PostToolUse` (Read/Grep/Bash/PowerShell/WebFetch/WebSearch/Glob/Agent/TaskOutput/MCP transforms), `PreToolUse` (opt-in rtk rewrite), `PreCompact`/`SessionEnd` (invalidation), `SessionStart` (opt-in rtk bootstrap). Replacements are rebuilt in each tool's original output shape (Claude Code validates `updatedToolOutput` against the tool's schema). State is per-agent JSON + content blobs under `${CLAUDE_PLUGIN_DATA}`, pruned after 48h. Pure-stdlib Python; the only optional external piece is the rtk binary, which is never required.

## Test

```
python test/test_dispatch.py
```

Covers dedup + escape valve, differential reads (small change → diff, wholesale → full), codemap intent gating, all three command tiers (mock rtk, built-in routes, collapse), rtk PreToolUse rewrite, grep, PreCompact/SessionEnd + rolling-context invalidation, shape preservation, and every disable switch.

## License

MIT (the rtk binary is separately licensed by its authors, Apache-2.0, and downloaded only on opt-in).
