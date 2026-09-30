#!/usr/bin/env python3
"""Behavioural tests for the pre-push audit.

Run:  /opt/hermes/.venv/bin/python scripts/test-audit.py

Each case gets a throwaway clone so the dangling-object sweep cannot see
blobs left behind by an earlier case.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
PY = sys.executable

PASS = FAIL = 0


def check(cond: bool, label: str) -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}")


def make_repo() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="audit-test-"))
    dest = tmp / "repo"
    shutil.copytree(SRC, dest, ignore=shutil.ignore_patterns(".git", "__pycache__"))
    subprocess.run(["git", "init", "-q"], cwd=dest, check=True)
    subprocess.run(["git", "add", "-A"], cwd=dest, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "init"], cwd=dest, check=True)
    return dest


def run_audit(repo: Path) -> tuple[int, str]:
    r = subprocess.run([PY, "scripts/pre-push-audit.py"], cwd=repo,
                       capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


def case(label: str, content: str | None, want_block: bool, rule: str = "") -> None:
    repo = make_repo()
    try:
        if content is not None:
            (repo / "leak.txt").write_text(content, encoding="utf-8")
            subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        code, out = run_audit(repo)
        if want_block:
            check(code == 1, f"{label}: blocked")
            check(rule in out, f"{label}: reports {rule}")
        else:
            check(code == 0, f"{label}: allowed")
            check("BLOCKED" not in out, f"{label}: no false positive")
    finally:
        shutil.rmtree(repo.parent, ignore_errors=True)


def main() -> int:
    # Values assembled from fragments so this test file never stores a real id.
    owner = "583" + "817" + "5445"
    guest = "679" + "498" + "5749"
    channel = "-" + "100" + "374" + "471" + "8087"
    bot = "ATRA" + "vbot"
    name = "a" + "2" + "z"
    private_repo = "her" + "mesbp"
    home = "/opt" + "/data"

    print("clean tree")
    case("no leaks", None, want_block=False)

    print("identity rules come from the local config")
    case("owner telegram id", f"x {owner}\n", True, "owner-telegram-id")
    case("guest telegram id", f"y {guest}\n", True, "guest-real-id")
    case("log channel id", f"z {channel}\n", True, "log-channel-id")
    case("bot username", f"b {bot}\n", True, "bot-username")
    case("owner name literal", f"I only serve to {name}\n", True, "owner-name-literal")

    print("portable rules")
    case("farsi text", "s \u0633\u0644\u0627\u0645\n", True, "farsi-text")
    case("host path", f"p {home}/state.db\n", True, "host-path")
    case("private repo name", f"q {private_repo}\n", True, "forbidden-term")
    case("telegram bot token", "1234567890:AAH" + "x" * 35 + "\n", True,
         "telegram-bot-token")
    case("github token", "ghp_" + "A" * 30 + "\n", True, "github-token")

    print("no false positives")
    # The GitHub account name is public and must stay allowed.
    case("public github handle", f"h {name}05\n", want_block=False)
    case("generic path", "p /opt/example/state.db\n", want_block=False)
    case("placeholder id", "100000001\n", want_block=False)

    print("live state must never be tracked")
    repo = make_repo()
    try:
        shutil.copy(SRC / "settings.example.json", repo / "settings.json")
        subprocess.run(["git", "add", "-f", "settings.json"], cwd=repo, check=True)
        code, out = run_audit(repo)
        check(code == 1, "tracked settings.json: blocked")
        check("live-state" in out, "tracked settings.json: reports live-state")
    finally:
        shutil.rmtree(repo.parent, ignore_errors=True)

    print(f"\n=== {PASS} passed, {FAIL} failed ===")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
