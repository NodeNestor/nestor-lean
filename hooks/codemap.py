#!/usr/bin/env python3
"""Project-level codemap: a token-cheap structural map of a codebase.

Walks a directory tree and produces, in one output:
  1. a compact folder tree with per-directory file-type counts
  2. signature lines (classes, functions, imports — no implementation
     bodies) of every code file, with real line numbers

One codemap call replaces the read-a-dozen-files orientation phase when
exploring an unfamiliar codebase. It is an ORIENTATION view: line numbers are
real so follow-up Reads with offset/limit (or full Reads before editing)
slot right in.

Usage:
    python codemap.py <directory> [--max-files N] [--max-chars N]

Zero dependencies (stdlib only). Shares its language parsers with the
nestor-lean dispatcher.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dispatch import CODE_EXTS, SIG_FAMILY, SIG_PATTERNS  # noqa: E402

IGNORE_DIRS = {
    ".git", ".hg", ".svn", ".vs", ".idea", ".vscode", "node_modules",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "venv",
    ".venv", "env", "bin", "obj", "dist", "build", "out", "target",
    "packages", "TestResults", ".next", ".nuxt", "coverage",
    "unsloth_compiled_cache", ".godot", "runs", "weights", "checkpoints",
}
LISTED_ONLY_LABEL = {
    ".css": "CSS", ".scss": "SCSS", ".png": "img", ".jpg": "img",
    ".jpeg": "img", ".gif": "img", ".svg": "svg", ".ico": "img",
    ".woff": "font", ".woff2": "font", ".ttf": "font", ".md": "doc",
    ".txt": "doc", ".json": "json", ".yml": "yaml", ".yaml": "yaml",
    ".toml": "toml", ".xml": "xml", ".csproj": "proj", ".sln": "proj",
    ".lock": "lock",
}
MAX_FILE_BYTES = 1024 * 1024
MAX_SIGS_PER_FILE = 80


def parse_signatures(path, ext):
    fam = SIG_FAMILY.get(ext)
    pat = SIG_PATTERNS.get(fam)
    if not pat:
        return None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read(MAX_FILE_BYTES)
    except Exception:
        return None
    sigs = []
    hidden = 0
    for i, line in enumerate(text.splitlines(), 1):
        if pat.match(line):
            if len(sigs) < MAX_SIGS_PER_FILE:
                sigs.append("  {}: {}".format(i, line.rstrip()[:160]))
            else:
                hidden += 1
    if hidden:
        sigs.append("  ... +{} more signature lines (Read the file for the rest)".format(hidden))
    return sigs


def walk(root):
    """Yield (relative_dir, dirnames, filenames) with ignores applied,
    deterministic order."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames if d not in IGNORE_DIRS and not d.startswith(".")
        )
        yield os.path.relpath(dirpath, root), dirnames, sorted(filenames)


def build(root, max_files, max_chars):
    root = os.path.abspath(root)
    tree_lines = []
    sig_sections = []
    type_counts = {}
    code_files = 0
    listed_files = 0
    parsed = 0
    skipped_after_cap = 0
    sig_budget_left = True
    sig_chars = 0

    printed_dirs = set()

    def print_dir(rel_dir):
        """Emit a tree line for rel_dir, printing unprinted ancestors first."""
        if rel_dir in printed_dirs or rel_dir == ".":
            return
        parent = os.path.dirname(rel_dir)
        if parent:
            print_dir(parent)
        printed_dirs.add(rel_dir)
        depth = rel_dir.count(os.sep)
        tree_lines.append("{}{}/".format("  " * depth, rel_dir.split(os.sep)[-1]))

    for rel, _dirs, files in walk(root):
        if not files:
            continue
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        indent = "  " * depth
        if rel != ".":
            print_dir(rel)
        per_type = {}
        code_here = []
        for name in files:
            ext = os.path.splitext(name)[1].lower()
            type_counts[ext] = type_counts.get(ext, 0) + 1
            if ext in CODE_EXTS:
                code_here.append(name)
                code_files += 1
            else:
                per_type[ext] = per_type.get(ext, 0) + 1
                listed_files += 1
        for name in code_here:
            tree_lines.append("{}{}".format(indent, name))
        if per_type:
            summary = ", ".join(
                "{} {}".format(n, LISTED_ONLY_LABEL.get(e, e or "other"))
                for e, n in sorted(per_type.items())
            )
            tree_lines.append("{}[{}]".format(indent, summary))

        for name in code_here:
            if parsed >= max_files or not sig_budget_left:
                skipped_after_cap += 1
                continue
            fpath = os.path.join(root, rel, name) if rel != "." else os.path.join(root, name)
            sigs = parse_signatures(fpath, os.path.splitext(name)[1].lower())
            if sigs is None:
                continue
            parsed += 1
            relname = os.path.normpath(os.path.join(rel, name)) if rel != "." else name
            section = "### {}\n{}".format(relname, "\n".join(sigs) if sigs else "  (no top-level signatures)")
            sig_chars += len(section)
            if sig_chars > max_chars:
                sig_budget_left = False
                skipped_after_cap += 1
                continue
            sig_sections.append(section)

    out = ["# Codemap: {}".format(root)]
    out.append(
        "{} code files ({} parsed{}), {} other files | bodies elided -- "
        "line numbers are real; Read any file in full before quoting or editing it.".format(
            code_files, parsed,
            ", {} beyond cap".format(skipped_after_cap) if skipped_after_cap else "",
            listed_files,
        )
    )
    out.append("\n## Tree")
    out.extend(tree_lines)
    top_types = sorted(type_counts.items(), key=lambda kv: -kv[1])[:12]
    out.append("\n## File types")
    out.append(", ".join("{}: {}".format(e or "(none)", n) for e, n in top_types))
    out.append("\n## Signatures")
    out.extend(sig_sections)
    if skipped_after_cap:
        out.append(
            "\n[codemap] {} code files not shown (file/char cap). Re-run on a "
            "subdirectory, or raise --max-files/--max-chars.".format(skipped_after_cap)
        )
    return "\n".join(out)


def main(argv):
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        print(__doc__)
        return 2 if len(argv) < 2 else 0
    root = argv[1]
    if not os.path.isdir(root):
        print("codemap: not a directory: {}".format(root))
        return 2
    max_files = 400
    max_chars = 60000
    args = argv[2:]
    for i, a in enumerate(args):
        if a == "--max-files" and i + 1 < len(args):
            max_files = int(args[i + 1])
        elif a == "--max-chars" and i + 1 < len(args):
            max_chars = int(args[i + 1])
    print(build(root, max_files, max_chars))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
