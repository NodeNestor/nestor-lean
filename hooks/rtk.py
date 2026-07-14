#!/usr/bin/env python3
"""rtk integration for nestor-lean.

rtk (https://github.com/rtk-ai/rtk) is a maintained Rust binary with ~50
per-command output filters. Rather than reimplement those parsers, nestor-lean
uses the real binary in the SAFE direction: `rtk pipe --filter <name>` reads
already-captured command output on stdin and returns a compressed version —
so the actual command runs UNTOUCHED and we only reshape what enters context.

This module locates the binary and maps a shell command to the right pipe
filter. The download/bootstrap lives in rtk_bootstrap.py.
"""
import os
import shutil
import subprocess

# Filters rtk's `pipe` mode accepts (from `rtk pipe --filter bogus`).
PIPE_FILTERS = {
    "cargo-test", "pytest", "go-test", "go-build", "tsc", "vitest", "grep",
    "rg", "find", "fd", "git-log", "git-diff", "git-status", "log", "mypy",
    "ruff-check", "ruff-format", "prettier",
}

RTK_PIPE_TIMEOUT = 15


def find_rtk():
    """Locate an rtk binary: explicit env, then the plugin-data cache, then
    PATH. Returns an absolute path or None."""
    env = os.environ.get("NESTOR_LEAN_RTK")
    if env and os.path.isfile(env):
        return env
    base = os.environ.get("CLAUDE_PLUGIN_DATA") or os.path.join(
        os.path.expanduser("~"), ".nestor-lean"
    )
    for name in ("rtk.exe", "rtk"):
        cand = os.path.join(base, "rtk", name)
        if os.path.isfile(cand):
            return cand
    found = shutil.which("rtk")
    return found


def _tokens(command):
    try:
        # POSIX-ish split without importing shlex quirks on Windows paths;
        # good enough to read program + subcommand.
        return command.replace("\t", " ").split()
    except Exception:
        return []


def pipe_filter_for(command):
    """Map a shell command to an rtk pipe filter name, or None.

    Only simple commands are considered — anything with a shell operator is
    left alone (we cannot know which part produced the captured output)."""
    if not command or any(op in command for op in ("|", "&&", "||", ";", "$(", "`", ">", "<")):
        return None
    toks = _tokens(command)
    if not toks:
        return None
    prog = os.path.basename(toks[0]).lower()
    if prog.endswith(".exe"):
        prog = prog[:-4]
    sub = toks[1].lower() if len(toks) > 1 else ""

    if prog == "pytest":
        return "pytest"
    if prog == "vitest":
        return "vitest"
    if prog == "tsc":
        return "tsc"
    if prog == "mypy":
        return "mypy"
    if prog == "prettier":
        return "prettier"
    if prog in ("grep", "rg", "find", "fd"):
        return prog
    if prog == "git":
        if sub in ("log", "diff", "status"):
            return "git-" + sub
        return None
    if prog == "cargo" and sub == "test":
        return "cargo-test"
    if prog == "go":
        if sub == "test":
            return "go-test"
        if sub == "build":
            return "go-build"
        return None
    if prog == "ruff":
        return "ruff-format" if sub == "format" else "ruff-check"
    return None


def run_pipe(rtk_path, filt, text):
    """Filter captured output through `rtk pipe --filter <filt>`. Returns the
    compressed text, or None on any failure (fail open)."""
    if filt not in PIPE_FILTERS:
        return None
    try:
        p = subprocess.run(
            [rtk_path, "pipe", "--filter", filt],
            input=text.encode("utf-8", "replace"),
            capture_output=True,
            timeout=RTK_PIPE_TIMEOUT,
        )
    except Exception:
        return None
    if p.returncode == 0 and p.stdout:
        out = p.stdout.decode("utf-8", "replace")
        if out.strip():
            return out
    return None


def rewrite_command(rtk_path, command):
    """Ask rtk for the born-compressed equivalent of a simple command (used by
    the opt-in PreToolUse rewrite path). Returns the rewritten command string
    or None. Only simple commands are offered — no shell operators."""
    if not command or any(op in command for op in ("|", "&&", "||", ";", "$(", "`", ">", "<")):
        return None
    toks = _tokens(command)
    if toks and os.path.basename(toks[0]).lower().startswith("rtk"):
        return None  # already routed through rtk
    try:
        p = subprocess.run(
            [rtk_path, "rewrite", command],
            capture_output=True,
            timeout=5,
        )
    except Exception:
        return None
    out = (p.stdout or b"").decode("utf-8", "replace").strip()
    if out and out != command.strip():
        return out
    return None
