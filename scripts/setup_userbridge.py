#!/usr/bin/env python3
"""Full-unlock setup: copy the owner's live session, write bridge creds.

Reads the LIVE selfbot session through SQLite's backup API (the original is
never opened for write), then writes the copy plus api_id/api_hash from the
selfbot .env into the deployed plugin dir: user.session + userbridge.json
(mode 600). Verifies the copy identifies the owner. Nothing here is tracked:
paths are derived like sync.sh derives them, ids are read at runtime from
the environment file, and the two outputs are written straight to the
deploy dir.

Usage:
  python3 scripts/setup_userbridge.py [--plugin-dir DIR] [--force] [--skip-verify]

Rerun only while Full unlock is OFF a running bridge holds its copy open.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
PROJECTS = HERE.parents[2]                      # .../projects
SELFBOT = PROJECTS / "selfbot_pro" / "selfbot_pro"
# sync.sh deploys to REPO/../../plugins/<name> same derivation here.
DEFAULT_PLUGIN_DIR = HERE.parents[3] / "plugins" / HERE.parents[1].name


def env_map(path: Path) -> dict:
    out = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--plugin-dir", default=str(DEFAULT_PLUGIN_DIR))
    ap.add_argument("--force", action="store_true",
                    help="overwrite an existing copy (bridge must be OFF)")
    ap.add_argument("--skip-verify", action="store_true")
    args = ap.parse_args()

    plugin = Path(args.plugin_dir)
    env_file = SELFBOT / ".env"
    env = env_map(env_file) if env_file.is_file() else {}
    session_name = env.get("SESSION_NAME")
    api_id, api_hash, owner = env.get("API_ID"), env.get("API_HASH"), env.get("OWNER_ID")
    if not (session_name and api_id and api_hash and owner):
        print(f"setup_userbridge: incomplete env at {env_file.name} need "
              "SESSION_NAME, API_ID, API_HASH, OWNER_ID", file=sys.stderr)
        return 1

    live = SELFBOT / f"{session_name}.session"
    if not live.is_file():
        print(f"setup_userbridge: live session not found: {live.name}",
              file=sys.stderr)
        return 1

    copy = plugin / "user.session"
    if copy.exists() and not args.force:
        print(f"setup_userbridge: {copy.name} already exists (rerun with "
              "--force while Full unlock is OFF to refresh it)")
        return 0

    # Backup API: a consistent snapshot that never opens the live file for write.
    src = sqlite3.connect(live.as_uri() + "?mode=ro", uri=True)
    dst = sqlite3.connect(str(copy))
    try:
        with dst:
            src.backup(dst)
    finally:
        dst.close()
        src.close()
    copy.chmod(0o600)

    creds = plugin / "userbridge.json"
    creds.write_text(json.dumps({
        "api_id": int(api_id),
        "api_hash": api_hash,
        "session_path": str(copy),
    }, indent=2) + "\n")
    creds.chmod(0o600)
    print(f"setup_userbridge: wrote {copy.name} + {creds.name} (mode 600)")

    if args.skip_verify:
        return 0

    deps = plugin / "deps"
    if deps.is_dir() and str(deps) not in sys.path:
        sys.path.insert(0, str(deps))
    try:
        from telethon import TelegramClient
    except Exception as exc:
        print(f"setup_userbridge: telethon unavailable ({exc}) run "
              "sync.sh deploy first (installs deps/), then rerun",
              file=sys.stderr)
        return 2

    async def who():
        tc = TelegramClient(str(copy), int(api_id), api_hash)
        await tc.connect()
        try:
            me = await tc.get_me()
            return me.id if me else None
        finally:
            await tc.disconnect()

    try:
        found = asyncio.run(who())
    except Exception as exc:
        print(f"setup_userbridge: verify failed: {exc}", file=sys.stderr)
        return 3
    if str(found) != str(owner):
        print(f"setup_userbridge: copy identifies {found}, expected the owner "
              f"({owner}) refusing to keep it", file=sys.stderr)
        return 4
    print("setup_userbridge: verified the copy identifies the owner")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
