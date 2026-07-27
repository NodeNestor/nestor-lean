#!/usr/bin/env python3
"""Runtime on/off switch for nestor-lean.

`NESTOR_LEAN_DISABLE=1` already exists, but it lives in settings.json env and
needs a Claude Code restart to take effect — useless at the moment you actually
want it, which is mid-session when output looks wrong and you want to know
whether nestor-lean caused it. This is a flag file the dispatcher checks on
every invocation instead, so /nestor-lean:off applies to the very next tool
call.

The flag deliberately lives at a fixed user-level path rather than in the
plugin data dir: slash commands run through Bash, which does not inherit
CLAUDE_PLUGIN_DATA, so a data-dir flag would be written somewhere the hook
never looks. A fixed path also survives version bumps, which move the plugin
cache directory.

  python switch.py on | off | status
"""
import os
import sys


def flag_dir():
    return os.environ.get("NESTOR_LEAN_HOME") or os.path.join(
        os.path.expanduser("~"), ".nestor-lean"
    )


def flag_path():
    return os.path.join(flag_dir(), "disabled")


def is_disabled():
    """True when compression should be skipped entirely."""
    if os.environ.get("NESTOR_LEAN_DISABLE") == "1":
        return True
    try:
        return os.path.exists(flag_path())
    except OSError:
        return False


def _set(disabled):
    path = flag_path()
    if disabled:
        os.makedirs(flag_dir(), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("nestor-lean disabled via /nestor-lean:off\n")
    else:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


def main():
    action = (sys.argv[1] if len(sys.argv) > 1 else "status").lower()

    if action == "off":
        _set(True)
        print("nestor-lean is now OFF — tool output passes through untouched.")
        print("Applies from the next tool call; no restart needed.")
        print("Turn it back on with /nestor-lean:on")
        return

    if action == "on":
        _set(False)
        if os.environ.get("NESTOR_LEAN_DISABLE") == "1":
            print("Flag cleared, but NESTOR_LEAN_DISABLE=1 is set in the")
            print("environment and still wins. Remove it from settings.json")
            print("and restart Claude Code.")
            return
        print("nestor-lean is now ON — compression active from the next tool call.")
        return

    if action == "status":
        if os.environ.get("NESTOR_LEAN_DISABLE") == "1":
            print("nestor-lean: OFF (NESTOR_LEAN_DISABLE=1 in the environment)")
        elif os.path.exists(flag_path()):
            print("nestor-lean: OFF (switched off with /nestor-lean:off)")
            print(f"  flag: {flag_path()}")
        else:
            print("nestor-lean: ON")
        return

    print(f"Unknown action {action!r} — expected on, off, or status")
    sys.exit(2)


if __name__ == "__main__":
    main()
