"""Verify the update system: check mode is read-only, and both entry points work.

Run with the hermes venv:  /opt/hermes/.venv/bin/python scripts/test-selfupdate.py
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
# The installed plugin dir. This test file is public, so it must not carry one
# machine's paths: point TGM_PLUGIN_DIR at the install to exercise the
# read-only check, or leave it unset and that case is skipped.
PLUGIN_DIR = Path(os.environ["TGM_PLUGIN_DIR"]) if os.environ.get("TGM_PLUGIN_DIR") else None
sys.path.insert(0, str(REPO))

import selfupdate as su  # noqa: E402

PASS = FAIL = 0
REPO_URL = "https://github.com/a2z05/TGAhermes.git"


def check(cond: bool, label: str) -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}")


def fingerprint(root: Path) -> dict:
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and "__pycache__" not in str(p):
            out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def main() -> int:
    print("version helpers")
    check(su.read_version(REPO / "plugin.yaml") != "?", "reads the repo version")
    check(su.read_version(Path("/nonexistent")) == "0.0.0", "missing file -> 0.0.0")
    check(su.vtuple("2.10.0") > su.vtuple("2.9.9"), "numeric compare, not lexical")
    check(su.vtuple("2.6.0") == su.vtuple("2.6.0"), "equal versions compare equal")
    check(su.vtuple("") == (0,), "empty version is safe")

    print("protected files")
    check("settings.json" in su.PROTECTED, "settings.json is protected")
    check("state.json" in su.PROTECTED, "state.json is protected")

    print("check mode changes nothing")
    if PLUGIN_DIR is None:
        print("  SKIP: set TGM_PLUGIN_DIR to the install to test check mode")
    elif not PLUGIN_DIR.is_dir():
        print(f"  SKIP: {PLUGIN_DIR} is not a directory")
    else:
        before = fingerprint(PLUGIN_DIR)
        info = su.check_update(PLUGIN_DIR, REPO_URL, "master", timeout=180)
        after = fingerprint(PLUGIN_DIR)
        check(before == after, "check mode is read-only (byte-identical)")
        check("error" not in info, f"check succeeded (err={info.get('error', '')[:60]})")
        check(info.get("remote_version") != "unknown", "got a remote version")
        check(isinstance(info.get("files_changed"), int), "reported files_changed")
        check("newer_available" in info, "reported newer_available")

    print("no-op update is a no-op")
    tmp = Path(tempfile.mkdtemp(prefix="su-test-"))
    try:
        target = tmp / "plugin"
        target.mkdir()
        (target / "plugin.yaml").write_text("name: t\nversion: 9.9.9\n")
        (target / "settings.json").write_text('{"owner_id": "secret"}')
        before = fingerprint(target)
        res = su.apply_update(target, "https://invalid.invalid/x.git", "master",
                              home=tmp, backup_root=tmp / "b", timeout=30)
        check(res.get("ok") is False, "unreachable repo -> ok False")
        check("error" in res, "unreachable repo -> error message")
        check(fingerprint(target) == before, "failed update touched nothing")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("formatting")
    txt = su.format_report({"local_version": "2.6.0", "remote_version": "2.7.0",
                            "remote_sha": "abcdef1234", "files_changed": 3,
                            "newer_available": True, "action": "updated 3 file(s)",
                            "tests": "=== 235 passed, 0 failed ===",
                            "reload": "reloaded", "backup": "/tmp/b"})
    check("2.6.0" in txt and "2.7.0" in txt, "report shows both versions")
    check("235 passed" in txt, "report shows the test result")
    err = su.format_report({"error": "boom <script>"})
    check("&lt;script&gt;" in err, "report escapes html in errors")

    print(f"\n=== {PASS} passed, {FAIL} failed ===")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
