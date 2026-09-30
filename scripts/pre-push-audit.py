#!/usr/bin/env python3
"""Pre-publish audit for this PUBLIC repo.

Installed as .git/hooks/pre-push (see install-audit-hook.sh), so nothing
reaches GitHub without passing it. Checks the tracked working tree AND every
commit in history, because a leak deleted from HEAD is still readable in the
old blob.

Rules:
  1. no real Telegram ids / usernames / chat ids (owner, guests, log channel)
  2. no credentials (bot tokens, gh tokens, cloud keys, api keys)
  3. no per-install paths or host details
  4. no Farsi text (the public cut is English-only)
  5. no live settings/state files tracked

The real values are assembled from fragments at runtime so that running
git-filter-repo over this file cannot sanitise the patterns out of it, and
this file itself is excluded from its own scan.

Usage: pre-push-audit.py [--allow]
Exit 0 = safe to push, 1 = blocked.
"""

from __future__ import annotations

import re
import subprocess
import sys

# Repo-relative names of the files that legitimately contain the patterns
# (this audit's own source). Compared as plain strings: git always reports
# repo-relative paths, so a resolved absolute Path never matches.
SELF_RELPATHS = {"scripts/pre-push-audit.py", "scripts/install-audit-hook.sh"}

OWNER_ID = "583" + "817" + "5445"
GUEST_IDS = ("679" + "498" + "5749", "689" + "497" + "6376")
LOG_CHANNEL = "-" + "100" + "374" + "471" + "8087"
BOT_USERNAME = "ATRA" + "vbot"
OWNER_NAME = "a" + "2" + "z"

RULES: list[tuple[str, re.Pattern[str]]] = [
    ("owner-telegram-id", re.compile(r"(?<!\d)" + OWNER_ID + r"(?!\d)")),
    ("guest-real-id", re.compile(r"(?<!\d)(?:" + "|".join(GUEST_IDS) + r")(?!\d)")),
    ("log-channel-id", re.compile(r"(?<!\d)" + re.escape(LOG_CHANNEL) + r"(?!\d)")),
    ("bot-username", re.compile(re.escape(BOT_USERNAME))),
    ("owner-name-literal", re.compile(r"(?<![\w-])" + OWNER_NAME + r"(?![\w-])")),
    ("telegram-bot-token", re.compile(r"\d{8,12}:[A-Za-z0-9_-]{30,}")),
    ("github-token", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")),
    ("aws-key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("openai-key", re.compile(r"sk-[A-Za-z0-9_-]{20,}")),
    ("host-path", re.compile(r"/host/home/(?:state\.db|\.9router|plugins)|\.live_secret|<private-repo>")),
    ("farsi-text", re.compile(r"[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]")),
]


def git(*args: str) -> str:
    try:
        out = subprocess.run(
            ["git", *args], capture_output=True, text=True, timeout=120, check=False
        )
        return out.stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def scan(label: str, rev: str | None) -> list[str]:
    """Return BLOCKED lines for one tree (rev=None means the working tree)."""
    problems: list[str] = []
    files = git("ls-tree", "-r", "--name-only", rev) if rev else git("ls-files")
    for path in files.splitlines():
        path = path.strip()
        if not path or path in SELF_RELPATHS:
            continue
        blob = git("cat-file", "blob", f"{rev}:{path}" if rev else f":{path}")
        if not blob:
            continue
        for lineno, line in enumerate(blob.splitlines(), 1):
            for name, pattern in RULES:
                if pattern.search(line):
                    problems.append(f"BLOCKED [{name}] {label}\n  {path}:{lineno}: {line.strip()[:160]}")
    if not problems:
        print(f"  ok  {label}")
    return problems


def main() -> int:
    root = git("rev-parse", "--show-toplevel").strip()
    if not root:
        print("audit: not a git repo")
        return 1
    print(f"pre-push audit: {git('rev-parse', '--abbrev-ref', 'HEAD').strip()}")

    blocked: list[str] = []
    blocked += scan("working tree", None)
    for commit in git("rev-list", "--all").split():
        blocked += scan(f"history {commit[:8]}", commit)

    tracked = git("ls-files")
    for name in ("settings.json", "state.json"):
        if name in tracked.split():
            blocked.append(f"BLOCKED [live-state] tracked per-install file: {name}")

    if blocked:
        print("\n".join(blocked))
        print(f"\npush BLOCKED: {len(blocked)} problem line(s).")
        if "--allow" in sys.argv:
            print("--allow given: pushing anyway.")
            return 0
        return 1
    print("audit clean - push allowed")
    return 0


if __name__ == "__main__":
    sys.exit(main())