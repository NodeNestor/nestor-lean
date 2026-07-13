---
name: codemap
description: Generate a token-cheap structural map of a whole codebase directory — folder tree, file-type counts, and every code file's signatures (classes/functions/imports with real line numbers, no implementation bodies). Use this INSTEAD of reading many files when starting to explore or orient in an unfamiliar codebase, planning where a change goes, or answering "how is this project structured". Do NOT use it when debugging a specific error or before editing a file — read the real files for that.
---

# codemap

Run the codemap script on the directory you want to understand:

```
python "${CLAUDE_PLUGIN_ROOT}/hooks/codemap.py" <absolute-directory-path>
```

Optional flags: `--max-files N` (default 400 parsed code files), `--max-chars N` (default 60000 output budget). For very large repos, run it on the subdirectory you care about rather than raising the caps.

## How to use the output

- The map is an **orientation view**: every signature line carries its **real line number**, so you can follow up with targeted `Read` calls using `offset`/`limit`, or `Grep` for specifics.
- **Before editing or quoting a file, always Read it in full** — the map elides implementation bodies, and edits require exact file content.
- Directories like `node_modules`, `.git`, `bin`, `obj`, `venv`, `dist` are skipped automatically.
- If the output notes files beyond the cap, re-run on the relevant subdirectory instead of raising caps.
