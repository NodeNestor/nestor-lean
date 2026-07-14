#!/usr/bin/env python3
"""Lazy-download the rtk binary into the plugin data dir.

Wired as a SessionStart hook (and runnable by hand). On first run it fetches
the pinned rtk release for this platform, verifies its SHA-256 against the
release checksums file, extracts the binary, and caches it. Subsequent runs
are a no-op. Every failure is silent — nestor-lean simply runs without rtk and
falls back to its own routes.

Downloads only happen when opted in: set NESTOR_LEAN_RTK_DOWNLOAD=1 (or run
this script directly with --force). This keeps the plugin from fetching a
third-party binary unless the user asked for it.
"""
import hashlib
import io
import json
import os
import ssl
import sys
import tarfile
import urllib.request
import zipfile

REQUIRED_RTK_VERSION = "v0.42.3"
RTK_REPO = "rtk-ai/rtk"

TARGETS = {
    ("linux", "x86_64"): ("x86_64-unknown-linux-musl", "tar.gz", "rtk"),
    ("linux", "aarch64"): ("aarch64-unknown-linux-gnu", "tar.gz", "rtk"),
    ("linux", "arm64"): ("aarch64-unknown-linux-gnu", "tar.gz", "rtk"),
    ("darwin", "x86_64"): ("x86_64-apple-darwin", "tar.gz", "rtk"),
    ("darwin", "arm64"): ("aarch64-apple-darwin", "tar.gz", "rtk"),
    ("darwin", "aarch64"): ("aarch64-apple-darwin", "tar.gz", "rtk"),
    ("windows", "amd64"): ("x86_64-pc-windows-msvc", "zip", "rtk.exe"),
    ("windows", "x86_64"): ("x86_64-pc-windows-msvc", "zip", "rtk.exe"),
}


def _plat():
    import platform
    system = platform.system().lower()
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64", "x64"):
        machine = "x86_64" if system != "windows" else "amd64"
    if machine in ("aarch64", "arm64"):
        machine = "arm64" if system != "linux" else "aarch64"
    return TARGETS.get((system, machine)) or TARGETS.get((system, "x86_64"))


def rtk_dir():
    base = os.environ.get("CLAUDE_PLUGIN_DATA") or os.path.join(
        os.path.expanduser("~"), ".nestor-lean"
    )
    d = os.path.join(base, "rtk")
    os.makedirs(d, exist_ok=True)
    return d


def installed_ok(bin_name):
    p = os.path.join(rtk_dir(), bin_name)
    marker = os.path.join(rtk_dir(), ".version")
    if not os.path.isfile(p):
        return False
    try:
        with open(marker) as f:
            return f.read().strip() == REQUIRED_RTK_VERSION
    except Exception:
        return False


def _fetch(url):
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={"User-Agent": "nestor-lean"})
    with urllib.request.urlopen(req, timeout=60, context=ctx) as r:
        return r.read()


def _expected_sha(asset_name):
    """Pull the asset's SHA-256 from the release checksums.txt."""
    url = "https://github.com/{}/releases/download/{}/checksums.txt".format(
        RTK_REPO, REQUIRED_RTK_VERSION
    )
    try:
        text = _fetch(url).decode("utf-8", "replace")
    except Exception:
        return None
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].lstrip("*").endswith(asset_name):
            return parts[0].lower()
    return None


def bootstrap(force=False):
    plat = _plat()
    if plat is None:
        return None
    target, ext, bin_name = plat
    if installed_ok(bin_name):
        return os.path.join(rtk_dir(), bin_name)
    if not force and os.environ.get("NESTOR_LEAN_RTK_DOWNLOAD") != "1":
        return None

    asset = "rtk-{}.{}".format(target, ext)
    url = "https://github.com/{}/releases/download/{}/{}".format(
        RTK_REPO, REQUIRED_RTK_VERSION, asset
    )
    try:
        blob = _fetch(url)
    except Exception:
        return None

    expected = _expected_sha(asset)
    if expected:
        actual = hashlib.sha256(blob).hexdigest().lower()
        if actual != expected:
            return None  # refuse a binary that fails checksum

    dest = rtk_dir()
    try:
        if ext == "zip":
            with zipfile.ZipFile(io.BytesIO(blob)) as z:
                for member in z.namelist():
                    if os.path.basename(member) == bin_name:
                        with z.open(member) as src, open(os.path.join(dest, bin_name), "wb") as out:
                            out.write(src.read())
                        break
        else:
            with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as t:
                for member in t.getmembers():
                    if os.path.basename(member.name) == bin_name:
                        src = t.extractfile(member)
                        if src is not None:
                            with open(os.path.join(dest, bin_name), "wb") as out:
                                out.write(src.read())
                        break
        final = os.path.join(dest, bin_name)
        if not os.path.isfile(final):
            return None
        if not final.endswith(".exe"):
            os.chmod(final, 0o755)
        with open(os.path.join(dest, ".version"), "w") as f:
            f.write(REQUIRED_RTK_VERSION)
        return final
    except Exception:
        return None


def main():
    force = "--force" in sys.argv[1:]
    # Consume the SessionStart hook stdin if present (ignored).
    try:
        if not sys.stdin.isatty():
            sys.stdin.read()
    except Exception:
        pass
    path = bootstrap(force=force)
    if "--force" in sys.argv[1:] or "-v" in sys.argv[1:]:
        print("rtk: {}".format(path or "not installed (set NESTOR_LEAN_RTK_DOWNLOAD=1 or pass --force)"))


if __name__ == "__main__":
    main()
