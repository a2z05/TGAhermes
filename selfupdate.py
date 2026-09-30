"""Self-update for the deployed plugin: fetch a newer version from git.

Called from two places, both owner-gated:

  * the ``telegram_admin`` tool, action ``update_plugin`` (with dry_run)
  * the ``!update`` console button in the panel

Why it is a module and not a shell one-liner: an update rewrites the very
file the gateway is running, so it has to be careful about ordering, about
never touching per-install state, and about proving the new code works
before it goes live. That is easier to get right in code than in a script.

Safety properties, in order of importance:

  1. settings.json and state.json are never written. The owner's identity,
     texts, toggles and learned per-chat state survive every update.
  2. Nothing is copied until the target files are known and a backup of the
     current deployed copy exists on disk.
  3. A version bump is required. A remote that is not strictly newer is
     reported and left alone, so a re-clone can never downgrade or loop.
  4. The plugin's own test suite runs after the copy and before the reload.
     A failing suite means: files are already in place, nothing is reloaded,
     and the backup path is returned so it can be restored by hand.
  5. Hot reload goes through the gateway control socket, never SIGUSR1,
     because a signal mid-turn would drop a conversation in flight.

Repository URL, branch, target directory and timeouts all come from settings
so an install can point this at a fork without editing code.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Per-install files an update must never overwrite.
PROTECTED = {"settings.json", "state.json"}

DEFAULT_REPO = "https://github.com/a2z05/TGAhermes.git"


def _sh(args: List[str], timeout: int = 300, cwd: Optional[str] = None) -> Tuple[int, str]:
    try:
        r = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                           timeout=timeout, check=False)
        return r.returncode, (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)


def read_version(plugin_yaml: Path) -> str:
    """Read `version:` without importing the plugin (import has side effects)."""
    try:
        text = plugin_yaml.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "0.0.0"
    m = re.search(r"^version:\s*[\"']?([^\s\"']+)", text, re.M)
    return m.group(1) if m else "0.0.0"


def vtuple(v: str) -> Tuple[int, ...]:
    parts = re.findall(r"\d+", v or "")
    return tuple(int(p) for p in parts[:3]) or (0,)


def _changed_files(repo: Path, target: Path) -> List[str]:
    """Tracked files whose deployed copy is missing or different.

    Only files the repo tracks are considered, which is what keeps untracked
    local additions and per-install state out of the update entirely.
    """
    code, out = _sh(["git", "ls-files"], cwd=str(repo))
    if code != 0:
        return []
    changed: List[str] = []
    for rel in out.split():
        if not rel or "__pycache__" in rel or rel.endswith(".pyc"):
            continue
        if Path(rel).name in PROTECTED:
            continue
        src = repo / rel
        dst = target / rel
        if not src.is_file():
            continue
        if not dst.exists() or src.read_bytes() != dst.read_bytes():
            changed.append(rel)
    return changed


def _backup(target: Path, backup_root: Path) -> Optional[Path]:
    try:
        backup_root.mkdir(parents=True, exist_ok=True)
        dest = backup_root / f"plugin-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copytree(target, dest,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        return dest
    except OSError:
        return None


def _run_tests(target: Path, timeout: int) -> Tuple[bool, str]:
    test = target / "tests" / "test_plugin.py"
    if not test.exists():
        return True, "no deployed test suite, skipped"
    for py in ("/opt/hermes/.venv/bin/python", "python3"):
        if py == "python3" and not shutil.which(py):
            continue
        code, out = _sh([py, str(test)], timeout=timeout)
        # The suite logs warnings after the summary, so the last line is not
        # the result. Match the summary explicitly and fall back to the tail.
        summary = ""
        for line in reversed(out.splitlines()):
            if "passed" in line and "failed" in line:
                summary = line.strip()
                break
        if not summary:
            summary = out.splitlines()[-1].strip() if out else ""
        if code != 0:
            return False, summary or f"{py} exited {code}"
        return True, summary
    return True, "no usable python, skipped"


def _hot_reload(home: Path, timeout: int) -> Tuple[bool, str]:
    """Reload plugins via the gateway control socket (never SIGUSR1)."""
    code, out = _sh(
        ["/opt/hermes/.venv/bin/python", "-c",
         "from pathlib import Path;"
         "from gateway.control_socket import query_gateway_control;"
         f"print(query_gateway_control(Path({str(home)!r}), 'reload-plugins',"
         f" params={{'home': {str(home)!r}}}))"],
        timeout=timeout,
        cwd="/opt/hermes",
    )
    if code != 0:
        return False, out[:200]
    if "reloaded': True" in out.replace('"', "'"):
        return True, "reloaded"
    return False, out[:200]


def check_update(target: Path, repo_url: str, branch: str,
                 timeout: int = 300) -> Dict[str, Any]:
    """Compare deployed version against the remote without changing anything."""
    info: Dict[str, Any] = {
        "local_version": read_version(target / "plugin.yaml"),
        "remote_version": "unknown",
        "remote_sha": "unknown",
        "files_changed": 0,
        "repo": repo_url,
    }
    tmp = Path(tempfile.mkdtemp(prefix="tgm-update-"))
    try:
        code, out = _sh(["git", "clone", "--depth", "1", "--branch", branch,
                         repo_url, str(tmp / "r")], timeout=timeout)
        if code != 0:
            low = out.lower()
            if "authentication failed" in low or "invalid username or token" in low:
                # A dead token or a private remote. Say so plainly: the bare
                # git error is easy to misread as a network problem.
                info["error"] = ("GitHub rejected the credentials. If the repo is "
                                 "private, the stored token is dead or lacks access. "
                                 f"git said: {out.strip()[:200]}")
            elif "not found" in low or "repository not found" in low:
                info["error"] = f"Repo not found or not public: {repo_url}"
            else:
                info["error"] = f"clone failed: {out[:200]}"
            return info
        repo = tmp / "r"
        info["remote_version"] = read_version(repo / "plugin.yaml")
        _, sha = _sh(["git", "rev-parse", "--short", "HEAD"], cwd=str(repo))
        info["remote_sha"] = sha or "unknown"
        info["files_changed"] = len(_changed_files(repo, target))
        info["newer_available"] = (
            vtuple(info["remote_version"]) > vtuple(info["local_version"]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return info


def apply_update(target: Path, repo_url: str, branch: str,
                 home: Path, backup_root: Path, timeout: int = 300,
                 force: bool = False) -> Dict[str, Any]:
    """Install a newer version. Returns a report; never raises."""
    out: Dict[str, Any] = {"ok": False}
    if not target.is_dir():
        out["error"] = f"plugin dir not found: {target}"
        return out

    out.update(check_update(target, repo_url, branch, timeout))
    if out.get("error"):
        return out
    if not (out.get("newer_available") or force):
        out["ok"] = True
        out["action"] = ("force reinstall" if force else "already up to date")
        return out
    if not force and not out.get("files_changed"):
        out["ok"] = True
        out["action"] = "newer version, but no file differences"
        return out

    tmp = Path(tempfile.mkdtemp(prefix="tgm-update-"))
    try:
        code, msg = _sh(["git", "clone", "--depth", "1", "--branch", branch,
                         repo_url, str(tmp / "r")], timeout=timeout)
        if code != 0:
            out["error"] = f"clone failed: {msg[:200]}"
            return out
        repo = tmp / "r"
        changed = _changed_files(repo, target)
        if not changed:
            out["ok"] = True
            out["action"] = "nothing to copy"
            return out

        backup = _backup(target, backup_root)
        out["backup"] = str(backup) if backup else None
        if backup is None:
            out["error"] = "could not create a backup; refusing to update"
            return out

        copied, errors = 0, []
        for rel in changed:
            src, dst = repo / rel, target / rel
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                copied += 1
            except OSError as exc:
                errors.append(f"{rel}: {exc}")
        out["files_copied"] = copied
        if errors:
            out["error"] = "copy failed: " + "; ".join(errors[:3])
            return out

        ok, detail = _run_tests(target, timeout)
        out["tests"] = detail
        if not ok:
            out["error"] = "tests failed; files in place but NOT reloaded"
            out["action"] = "restore from backup"
            return out

        reloaded, rdetail = _hot_reload(home, timeout)
        out["reload"] = rdetail
        out["ok"] = reloaded
        out["action"] = (f"updated {copied} file(s) to {out['remote_version']}"
                         if reloaded else "files copied but RELOAD FAILED")
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def format_report(info: Dict[str, Any]) -> str:
    """Render an update report as a short HTML block for Telegram."""
    from html import escape as _e

    if info.get("error"):
        return (f"❌ <b>Update failed</b>\n"
                f"<code>{_e(str(info['error'])[:300])}</code>")

    lines = [
        f"📦 <b>Plugin update</b>",
        f"<b>Installed:</b> <code>{_e(str(info.get('local_version', '?')))}</code>",
        f"<b>Available:</b> <code>{_e(str(info.get('remote_version', '?')))}</code>"
        f" <code>({_e(str(info.get('remote_sha', '?'))[:8])})</code>",
        f"<b>Files differing:</b> {info.get('files_changed', 0)}",
    ]
    if info.get("action"):
        lines.append(f"<b>Result:</b> {_e(str(info['action']))}")
    if info.get("tests"):
        lines.append(f"<b>Tests:</b> <code>{_e(str(info['tests'])[:120])}</code>")
    if info.get("reload"):
        lines.append(f"<b>Reload:</b> {_e(str(info['reload']))}")
    if info.get("backup"):
        lines.append(f"<b>Backup:</b> <code>{_e(str(info['backup']))}</code>")
    return "\n".join(lines)
