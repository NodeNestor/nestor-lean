# nestor-lean

[![MIT License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
![Zero Dependencies](https://img.shields.io/badge/dependencies-zero-orange.svg)

Input-side token compression for Claude Code. Where [rolling-context](https://github.com/NodeNestor/claude-rolling-context) manages tokens *already in* your context, nestor-lean stops waste **before it enters**: repeated file reads become one-line references to what the model already saw, and bulky grep output gets collapsed. The two plugins are designed to run in tandem and need no coordination.

## What it does (v0.1)

### 1. Read dedup-by-reference

Claude re-reads the same file constantly — after edits elsewhere, after a subtask, "just to check". Every re-read of an unchanged file re-injects the full contents into context. nestor-lean hashes the file on every `Read`; if the **same path + offset/limit + content digest** was already read this session (within a 20-minute window), the output is replaced with:

```
[nestor-lean] Duplicate read skipped: you already read this exact file earlier in
this session (~4 min ago) and it has NOT changed since (digest 3fa1b2c9d0ee).
Refer to your earlier read of:
  E:\Repos\myapp\src\router.py
If that content is no longer available in your context, run the same Read again
and the full contents will be returned.
```

**Safety valve:** after serving a reference, the very next identical Read returns the full content and resets the cycle — the model can never get stuck without the bytes (e.g. if the earlier read was compressed away by rolling-context). Changed files, small files (<1.5K chars), and different offset/limit windows always pass through untouched.

### 2. Grep output compression

`Grep` in content mode over a real codebase easily returns hundreds of near-identical lines. nestor-lean collapses matches whose text is identical within a file (`[identical match repeats 47x in this file]`), caps matches per file (default 25, with an explicit "+N more capped" note), and only rewrites at all when the output is large (>4K chars) **and** the saving is >20%. The header tells the model exactly what was elided and how to get the rest.

## What it never touches

- **Read output of changed or never-before-seen files** — full fidelity always.
- **Code content itself** — nothing is reformatted, minified, or summarized. `Edit`'s exact-string matching keeps working because the model only ever sees real file bytes (or an honest pointer to bytes it already has).
- **Grep in `files_with_matches`/`count` mode**, small outputs, and everything else.

Everything fails open: any error in the hook and the original output passes through.

## Install

```
/plugin marketplace add https://github.com/NodeNestor/nestor-plugins
/plugin install nestor-lean
```

Requires Python 3.7+ on PATH (stdlib only, no pip install). Manual install: clone this repo and add it with `claude --plugin-dir`.

## Observe the savings

```
/nestor-lean:gain
```

Prints deduped reads, compressed greps, and estimated tokens saved across recent sessions.

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `NESTOR_LEAN_DISABLE` | — | `1` disables all transforms |
| `NESTOR_LEAN_DEDUP_WINDOW` | `1200` | Seconds a read stays dedupable |
| `NESTOR_LEAN_MIN_DEDUP_CHARS` | `1500` | Reads smaller than this never dedup |
| `NESTOR_LEAN_GREP_MIN_CHARS` | `4000` | Grep outputs smaller than this never compress |
| `NESTOR_LEAN_GREP_PER_FILE_CAP` | `25` | Max matches kept per file |

## How it works

A single `PostToolUse` hook (`hooks/dispatch.py`, pure stdlib) receives every `Read` and `Grep` result and may return `updatedToolOutput` to replace what the model sees. State is one small JSON file per session under `${CLAUDE_PLUGIN_DATA}` (or `~/.nestor-lean`), auto-pruned after 48h. No proxy, no network, no dependencies.

## Roadmap

- `codemap`: signature-only skeleton of a directory (classes/functions/imports, no bodies) for orientation reads during research — one call instead of a dozen full reads.
- WebFetch/MCP output routes (boilerplate stripping, repeat-fetch dedup).
- rtk-style Bash command routes for `dotnet`, Playwright, and docker output.

## Test

```
python test/test_dispatch.py
```

## License

MIT
