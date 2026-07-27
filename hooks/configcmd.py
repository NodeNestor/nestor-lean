#!/usr/bin/env python3
"""Show or change nestor-lean's effective configuration.

  python configcmd.py                     show every setting and where it came from
  python configcmd.py preset aggressive   write a preset to the user config
  python configcmd.py bash_min_chars 500  write one setting to the user config
  python configcmd.py --project <k> <v>   write to ./.nestor-lean.json instead
  python configcmd.py --reset             clear the user config

Environment variables always win over files, and are reported as such so a
setting that "won't change" is never a mystery.
"""
import json
import os
import sys

import config


def _write(path, key, value):
    blob = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                blob = json.load(f) or {}
        except Exception:
            blob = {}
    blob[key] = value
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(blob, f, indent=2, sort_keys=True)
        f.write("\n")
    return path


def show():
    values, sources = config.resolve()
    print("nestor-lean configuration")
    print("=" * 74)
    print("preset: {}   (presets: {})".format(
        values["preset"], ", ".join(sorted(config.PRESETS))))
    print("-" * 74)
    print("{:<26}{:>10}  {:<16} {}".format("setting", "value", "from", "meaning"))
    for name in sorted(config.SETTINGS):
        if name == "preset":
            continue
        _env, _kind, _default, helptext = config.SETTINGS[name]
        print("{:<26}{:>10}  {:<16} {}".format(
            name, str(values[name]), sources[name], helptext))
    print("-" * 74)
    print("user config:    {}{}".format(
        config.USER_CONFIG, "" if os.path.exists(config.USER_CONFIG) else "  (none yet)"))
    proj = os.path.join(os.getcwd(), config.PROJECT_CONFIG)
    print("project config: {}{}".format(
        proj, "" if os.path.exists(proj) else "  (none)"))
    print("\nfloors are a pre-filter, not the safety net: the hook runs on every")
    print("matched call regardless, and min_saving_ratio discards any transform")
    print("that fails to pay for itself. Lowering floors buys coverage cheaply.")


def main():
    args = sys.argv[1:]
    target = config.USER_CONFIG
    if args and args[0] == "--project":
        target = os.path.join(os.getcwd(), config.PROJECT_CONFIG)
        args = args[1:]

    if not args:
        show()
        return

    if args[0] == "--reset":
        try:
            os.remove(target)
            print("removed {}".format(target))
        except FileNotFoundError:
            print("nothing to remove at {}".format(target))
        return

    if len(args) != 2:
        print(__doc__.strip())
        sys.exit(2)

    key, raw = args
    if key == "preset":
        if raw not in config.PRESETS:
            print("unknown preset {!r} — choose from: {}".format(
                raw, ", ".join(sorted(config.PRESETS))))
            sys.exit(2)
        value = raw
    elif key in config.SETTINGS:
        try:
            value = config._coerce(config.SETTINGS[key][1], raw)
        except Exception:
            print("could not read {!r} as {}".format(
                raw, config.SETTINGS[key][1].__name__))
            sys.exit(2)
    else:
        print("unknown setting {!r}. Known: {}".format(
            key, ", ".join(sorted(config.SETTINGS))))
        sys.exit(2)

    path = _write(target, key, value)
    print("set {} = {} in {}".format(key, value, path))

    env = config.SETTINGS[key][0]
    if os.environ.get(env):
        print("NOTE: {} is set in the environment and overrides this file.".format(env))
    print("Applies to the next tool call; no restart needed.")


if __name__ == "__main__":
    main()
