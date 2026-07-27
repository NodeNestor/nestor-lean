#!/usr/bin/env python3
"""Every knob in one place, with presets and a documented resolution order.

Resolution, lowest priority first:

  1. built-in defaults (the `balanced` preset)
  2. a named preset — `preset` in a config file, or NESTOR_LEAN_PRESET
  3. ~/.nestor-lean/config.json          (this machine)
  4. .nestor-lean.json in the project    (this repo, overrides the machine)
  5. NESTOR_LEAN_* environment variables (always win)

Environment variables keep their historical names, so nothing that already
works stops working.

The floors are worth understanding before tuning them: the hook process runs on
every matched tool call whether or not a floor is met, so a floor does not save
a process — it skips a few microseconds of in-process work. `min_saving_ratio`
is the real safety mechanism: any transform that fails to save that fraction is
discarded and the original passes through. Lowering floors therefore costs
almost nothing and mostly buys coverage.
"""
import json
import os

# name -> (env var, kind, default, help)
SETTINGS = {
    "preset": ("NESTOR_LEAN_PRESET", str, "balanced",
               "conservative | balanced | aggressive"),

    "min_saving_ratio": ("NESTOR_LEAN_MIN_SAVING_RATIO", float, 0.20,
                         "a transform must save this fraction or be discarded"),
    "dedup_window": ("NESTOR_LEAN_DEDUP_WINDOW", int, 1200,
                     "seconds a read stays dedup/diff-able"),
    "collapse_min_run": ("NESTOR_LEAN_COLLAPSE_MIN_RUN", int, 5,
                         "identical consecutive lines before collapsing"),
    "layout_min_saving": ("NESTOR_LEAN_LAYOUT_MIN_SAVING", int, 200,
                          "chars of layout waste before stripping alone is worth a header"),

    "min_dedup_chars": ("NESTOR_LEAN_MIN_DEDUP_CHARS", int, 1500, "Read floor"),
    "grep_min_chars": ("NESTOR_LEAN_GREP_MIN_CHARS", int, 2000, "Grep floor"),
    "bash_min_chars": ("NESTOR_LEAN_BASH_MIN_CHARS", int, 1500, "shell floor"),
    "mcp_min_chars": ("NESTOR_LEAN_MCP_MIN_CHARS", int, 3000, "MCP floor"),
    "web_min_chars": ("NESTOR_LEAN_WEB_MIN_CHARS", int, 3000, "WebFetch/WebSearch floor"),
    "glob_min_chars": ("NESTOR_LEAN_GLOB_MIN_CHARS", int, 2000, "Glob floor"),
    "report_min_chars": ("NESTOR_LEAN_REPORT_MIN_CHARS", int, 4000, "Agent report floor"),
    # Swept on a read-heavy sample: 12k -> 6k took savings from 27.8% to 32.8%,
    # and 6k -> 3k added only 1.5 more points while mapping files small enough
    # that the map is barely smaller than the file. 6k is the knee.
    "codemap_min_chars": ("NESTOR_LEAN_CODEMAP_MIN_CHARS", int, 6000,
                          "structural map floor while exploring"),
    "codemap_debug_min_chars": ("NESTOR_LEAN_CODEMAP_DEBUG_MIN_CHARS", int, 40000,
                                "structural map floor while error-hunting"),
    "grep_per_file_cap": ("NESTOR_LEAN_GREP_PER_FILE_CAP", int, 25,
                          "max grep matches kept per file"),
    "diff_max_change_ratio": ("NESTOR_LEAN_DIFF_MAX_CHANGE_RATIO", float, 0.45,
                              "above this much change, a full read beats a diff"),

    "diff": ("NESTOR_LEAN_DIFF", bool, True, "differential reads"),
    "codemap": ("NESTOR_LEAN_CODEMAP", bool, True, "automatic structural maps"),
    "mcp": ("NESTOR_LEAN_MCP", bool, True, "MCP output compression"),
    "web": ("NESTOR_LEAN_WEB", bool, True, "WebFetch/WebSearch compression"),
    "bash_routes": ("NESTOR_LEAN_BASH_ROUTES", bool, True, "built-in command routes"),
    "rtk_pipe": ("NESTOR_LEAN_RTK_PIPE", bool, True, "rtk pipe tier"),
}

# Only the deltas from `balanced`.
PRESETS = {
    "balanced": {},
    "conservative": {
        "min_saving_ratio": 0.30,
        "min_dedup_chars": 4000,
        "grep_min_chars": 4000,
        "bash_min_chars": 4000,
        "mcp_min_chars": 4000,
        "web_min_chars": 6000,
        "glob_min_chars": 4000,
        "report_min_chars": 8000,
        "codemap_min_chars": 20000,
        "codemap_debug_min_chars": 80000,
        "layout_min_saving": 600,
    },
    # Floors near zero. Because the hook already runs per call and
    # min_saving_ratio still gates every transform, this trades a little CPU
    # for coverage rather than trading away correctness.
    "aggressive": {
        "min_saving_ratio": 0.15,
        "min_dedup_chars": 500,
        "grep_min_chars": 500,
        "bash_min_chars": 500,
        "mcp_min_chars": 500,
        "web_min_chars": 500,
        "glob_min_chars": 500,
        "report_min_chars": 500,
        "codemap_min_chars": 3000,
        "codemap_debug_min_chars": 12000,
        "layout_min_saving": 60,
    },
}

USER_CONFIG = os.path.join(
    os.environ.get("NESTOR_LEAN_HOME")
    or os.path.join(os.path.expanduser("~"), ".nestor-lean"),
    "config.json",
)
PROJECT_CONFIG = ".nestor-lean.json"


def _coerce(kind, raw):
    if kind is bool:
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() not in ("0", "false", "no", "off", "")
    if kind is int:
        return int(float(str(raw).strip()))
    if kind is float:
        return float(str(raw).strip())
    return str(raw).strip()


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def resolve():
    """Return (values, sources) — sources says where each value came from."""
    values, sources = {}, {}
    for name, (_env, _kind, default, _help) in SETTINGS.items():
        values[name] = default
        sources[name] = "default"

    user_file = _read_json(USER_CONFIG)
    project_file = _read_json(os.path.join(os.getcwd(), PROJECT_CONFIG))

    preset_name = (os.environ.get("NESTOR_LEAN_PRESET")
                   or project_file.get("preset")
                   or user_file.get("preset")
                   or "balanced")
    for name, val in PRESETS.get(str(preset_name), {}).items():
        if name in values:
            values[name] = val
            sources[name] = "preset:" + str(preset_name)
    values["preset"] = str(preset_name)

    for label, blob in (("user config", user_file), ("project config", project_file)):
        for name, raw in blob.items():
            if name not in SETTINGS or name == "preset":
                continue
            try:
                values[name] = _coerce(SETTINGS[name][1], raw)
                sources[name] = label
            except Exception:
                pass

    for name, (env, kind, _d, _h) in SETTINGS.items():
        raw = os.environ.get(env)
        if raw is None or raw == "":
            continue
        try:
            values[name] = _coerce(kind, raw)
            sources[name] = "env " + env
        except Exception:
            pass

    return values, sources


VALUES, SOURCES = resolve()


def get(name):
    return VALUES[name]
