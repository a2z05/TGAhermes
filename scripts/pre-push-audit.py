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

Personal identifiers (owner id, owner name, guest ids, log channel, bot
username) are NOT hardcoded here: they live in .audit-identities.json, which
is gitignored and read on every run. Edit that file to change what counts as
a leak; nothing here needs to change. If the file is missing, only the
portable rules below apply.

Usage: pre-push-audit.py [--allow]
Exit 0 = safe to push, 1 = blocked.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

IDENTITY_FILE = ".audit-identities.json"

# Repo-relative names of the files that legitimately contain the patterns
# (this audit's own source). Compared as plain strings: git always reports
# repo-relative paths, so a resolved absolute Path never matches.
# This audit's own source. It is safe to skip: since the identity patterns
# moved to the gitignored config file, it contains no real identifiers.
SELF_RELPATHS = {"scripts/pre-push-audit.py", "scripts/install-audit-hook.sh"}
# Skipped only while git does not track it, so committing it is not an escape.
ALWAYS_SKIP = {"scripts/pre-push-audit.py", "scripts/install-audit-hook.sh"}


def load_identities(root: str) -> dict:
    """Read the local, gitignored list of identifiers that must not leak.

    Personal identifiers used to be hardcoded here, which meant the public
    repo itself carried the owner's Telegram id, name and guest ids. They now
    live in a local config file that is never committed, so the audit travels
    with the code without carrying anyone's identity.
    """
    path = Path(root) / IDENTITY_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        print(f"audit: {IDENTITY_FILE} is unreadable ({exc}); "
              f"portable rules still apply")
        return {}
    return data if isinstance(data, dict) else {}


def is_ignored(path: str) -> bool:
    """True when git does not track the file (so it cannot be pushed)."""
    r = subprocess.run(["git", "check-ignore", "-q", "--", path],
                       capture_output=True, check=False)
    if r.returncode == 0:
        return True
    tracked = subprocess.run(["git", "ls-files", "--error-unmatch", path],
                             capture_output=True, check=False)
    return tracked.returncode != 0


def build_rules(ids: dict) -> list[tuple[str, re.Pattern[str]]]:
    """Portable rules, plus identity rules fed from the local config."""
    rules: list[tuple[str, re.Pattern[str]]] = []
    owner = str(ids.get("owner_telegram_id") or "").strip()
    if owner:
        rules.append(("owner-telegram-id", re.compile(r"(?<!\d)" + re.escape(owner) + r"(?!\d)")))
    guests = [str(g).strip() for g in (ids.get("guest_telegram_ids") or []) if str(g).strip()]
    if guests:
        rules.append(("guest-real-id", re.compile(
            r"(?<!\d)(?:" + "|".join(re.escape(g) for g in guests) + r")(?!\d)")))
    channel = str(ids.get("log_channel_id") or "").strip()
    if channel:
        rules.append(("log-channel-id", re.compile(r"(?<!\d)" + re.escape(channel) + r"(?!\d)")))
    bot = str(ids.get("bot_username") or "").strip()
    if bot:
        rules.append(("bot-username", re.compile(re.escape(bot))))
    name = str(ids.get("owner_name") or "").strip()
    if name:
        # Word-boundary so the public GitHub handle (name + digits) is allowed.
        rules.append(("owner-name-literal",
                      re.compile(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])")))
    for extra in (ids.get("extra_forbidden") or []):
        if str(extra).strip():
            rules.append((f"forbidden-term-{str(extra).strip()[:20]}",
                          re.compile(re.escape(str(extra).strip()))))

    rules += [
        ("telegram-bot-token", re.compile(r"\d{8,12}:[A-Za-z0-9_-]{30,}")),
        ("github-token", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")),
        ("aws-key", re.compile(r"AKIA[0-9A-Z]{16}")),
        ("openai-key", re.compile(r"sk-[A-Za-z0-9_-]{20,}")),
    ]
    # Host-specific paths come from the local config too, so this file stays
    # portable. An install with a non-standard home still gets host-path checks.
    for hp in (ids.get("forbidden_paths") or []):
        if str(hp).strip():
            rules.append(("host-path", re.compile(re.escape(str(hp).strip()))))
    if ids.get("block_farsi", True):
        # Written as escapes on purpose: a literal range would make this file
        # match its own rule, and any history scrubber would corrupt it.
        rules.append(("farsi-text", re.compile(
            "[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]")))
    return rules


def git(*args: str) -> str:
    try:
        out = subprocess.run(
            ["git", *args], capture_output=True, text=True, timeout=120, check=False
        )
        return out.stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def scan(label: str, rev: str | None,
         rules: list[tuple[str, re.Pattern[str]]]) -> list[str]:
    """Return BLOCKED lines for one tree (rev=None means the working tree)."""
    problems: list[str] = []
    files = git("ls-tree", "-r", "--name-only", rev) if rev else git("ls-files")
    for path in files.splitlines():
        path = path.strip()
        if not path:
            continue
        if path in ALWAYS_SKIP:
            continue
        if path == IDENTITY_FILE and (rev is None or is_ignored(path)):
            continue  # local config: untracked, so it cannot be pushed
        blob = git("cat-file", "blob", f"{rev}:{path}" if rev else f":{path}")
        if not blob:
            continue
        for lineno, line in enumerate(blob.splitlines(), 1):
            for name, pattern in rules:
                if pattern.search(line):
                    problems.append(f"BLOCKED [{name}] {label}\n  {path}:{lineno}: {line.strip()[:160]}")
    if not problems:
        print(f"  ok  {label}")
    return problems


def scan_text(label: str, text: str,
              rules: list[tuple[str, re.Pattern[str]]]) -> list[str]:
    """Apply every rule to free-form text (commit messages, author headers)."""
    problems: list[str] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for name, pattern in rules:
            if pattern.search(line):
                snippet = line.strip()[:160]
                problems.append(f"BLOCKED [{name}] {label}\n  line {lineno}: {snippet}")
    if not problems:
        print(f"  ok  {label}")
    return problems


def scan_messages(revs: list[str], rules: list[tuple[str, re.Pattern[str]]]) -> list[str]:
    """Commit messages leak just as easily as file contents.

    This was a real miss: two commits described the bug in the owner's own
    words, including the owner's Telegram id, and the tree scan never saw it.
    """
    problems: list[str] = []
    for rev in revs:
        raw = git("cat-file", "commit", rev)
        if not raw:
            continue
        header, _, body = raw.partition("\n\n")
        author_lines = [ln for ln in header.splitlines()
                        if ln.startswith(("author ", "committer "))]
        problems += scan_text(f"identity {rev[:8]}", "\n".join(author_lines), rules)
        problems += scan_text(f"message {rev[:8]}", body, rules)
    return problems


def scan_unreachable(rules: list[tuple[str, re.Pattern[str]]]) -> list[str]:
    """Dangling blobs are pushed to some forges and can be fetched by SHA.

    A tree scan cannot see them, so sweep the object store too.
    """
    out = git("fsck", "--unreachable", "--dangling", "--no-progress")
    problems: list[str] = []
    for oid in re.findall(r"(?:unreachable|dangling) blob ([0-9a-f]{7,40})", out):
        blob = git("cat-file", "blob", oid)
        if blob:
            problems += scan_text(f"dangling {oid[:8]}", blob, rules)
    return problems


def main() -> int:
    root = git("rev-parse", "--show-toplevel").strip()
    if not root:
        print("audit: not a git repo")
        return 1
    print(f"pre-push audit: {git('rev-parse', '--abbrev-ref', 'HEAD').strip()}")

    rules = build_rules(load_identities(root))
    if not any(n.startswith(("owner-telegram-id", "guest-real-id", "owner-name"))
               for n, _ in rules):
        print(f"warning: no {IDENTITY_FILE} found; only portable rules active")

    revs = git("rev-list", "--all").split()
    blocked: list[str] = []
    blocked += scan("working tree", None, rules)
    for commit in revs:
        blocked += scan(f"history {commit[:8]}", commit, rules)
    blocked += scan_messages(revs, rules)
    blocked += scan_unreachable(rules)

    tracked = git("ls-files").split()
    for name in ("settings.json", "state.json"):
        if name in tracked:
            blocked.append(f"BLOCKED [live-state] tracked per-install file: {name}")
    for path in tracked:
        base = Path(path).name
        if base in ("settings.json", "state.json") and path != base:
            blocked.append(f"BLOCKED [live-state] tracked per-install file: {path}")

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