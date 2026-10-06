"""Owner-session bridge — the panel's "Full unlock (act as you)".

WHAT IT IS
    The Bot API gives a bot connected to the owner's business account no way
    to set or read reactions inside business chats: setMessageReaction has no
    business_connection_id, and reaction updates only reach chats the bot is
    actually a member of. The owner's own user account can do both. With the
    panel toggle on, this bridge keeps a COPY of the owner's existing MTProto
    session connected from the gateway and acts as him.

    v1 powers: set a reaction as the owner, plus a live feed of reaction
    updates from the owner's account (the plugin filters which chats matter).

    DANGER: a user account that behaves like automation can trip Telegram
    flood limits or get banned. The owner accepts this risk; the panel shows
    the warning before the toggle can be applied.

SESSION SAFETY
    scripts/setup_userbridge.py reads the live session file ONCE through
    SQLite's backup API — the original is never opened for write — and stores
    the copy plus api_id/api_hash in userbridge.json (mode 600, beside the
    copy, never committed). The running selfbot keeps its own file untouched.

STATE
    client / listener task / current callback live on the adapter object the
    plugin hands in ("anchor"), which survives plugin reloads — so a reload
    refreshes the callback instead of stacking a second connection.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from typing import Any, Awaitable, Callable, Dict, Optional

logger = logging.getLogger("TGAhermes.userbridge")

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEPS = os.path.join(_HERE, "deps")
_CFG = os.path.join(_HERE, "userbridge.json")

ReactionCallback = Callable[[str, int, str], Any]


def _deps() -> None:
    """Make the vendored telethon importable (deploy-dir deps/ only)."""
    if os.path.isdir(_DEPS) and _DEPS not in sys.path:
        sys.path.insert(0, _DEPS)


def config() -> Optional[Dict[str, Any]]:
    """{api_id, api_hash, session_path} from the 600 file, or None."""
    try:
        with open(_CFG) as fh:
            cfg = json.load(fh)
        if cfg.get("api_id") and cfg.get("api_hash") and cfg.get("session_path"):
            return cfg
    except Exception:
        logger.debug("[userbridge] no usable %s", os.path.basename(_CFG))
    return None


def state(anchor: Any) -> Dict[str, Any]:
    """Per-adapter state bag (survives plugin reloads with the adapter)."""
    st = getattr(anchor, "_tga_ub_state", None)
    if not isinstance(st, dict):
        st = {}
        try:
            setattr(anchor, "_tga_ub_state", st)
        except Exception:
            pass
    return st


async def _drop(tc: Any) -> None:
    """Release a client we are discarding; never raise.

    Telethon's sqlite handle lives on the session object and only closes
    when the client disconnects (or is collected). Explicit is better than
    waiting for GC while the session lock is already contended.
    """
    if tc is None:
        return
    try:
        await tc.disconnect()
    except Exception:
        logger.debug("[userbridge] disconnect during drop failed", exc_info=True)


async def ensure(anchor: Any) -> Any:
    """Connected, authorized Telethon client for the owner's session copy."""
    if anchor is None:
        return None
    st = state(anchor)
    tc = st.get("client")
    if tc is not None:
        try:
            if await tc.is_user_authorized():
                return tc
        except Exception:
            logger.debug("[userbridge] cached client stale", exc_info=True)
        # Cached but unusable -> it still owns the session lock, so let go
        # before opening a replacement (otherwise the replacement is locked).
        await _drop(tc)
        st["client"] = None
        st["handler_added"] = False
    cfg = config()
    if cfg is None:
        return None
    if "lock" not in st:
        st["lock"] = asyncio.Lock()
    async with st["lock"]:
        tc = st.get("client")
        if tc is not None:
            return tc
        try:
            _deps()
            from telethon import TelegramClient  # vendored: deploy dir deps/
            path = str(cfg["session_path"])
            if not os.path.isfile(path):
                logger.warning("[userbridge] session copy missing: %s", path)
                return None
            tc = TelegramClient(path, int(cfg["api_id"]), str(cfg["api_hash"]))
            await tc.connect()
            if not await tc.is_user_authorized():
                await tc.disconnect()
                logger.error("[userbridge] session copy is not authorized")
                return None
            st["client"] = tc
            st["handler_added"] = False
            logger.info("[userbridge] owner session connected (copy)")
            return tc
        except Exception:
            logger.exception("[userbridge] connect failed")
            # A client that never connected still owns its sqlite handle.
            # Leaving it open holds the session lock, so the NEXT attempt
            # fails with "database is locked" and the loop feeds itself.
            await _drop(tc)
            return None


async def react(anchor: Any, chat_id: Any, message_id: Any, emoji: str) -> bool:
    """Set one reaction AS THE OWNER (used when the Bot API cannot)."""
    try:
        tc = await ensure(anchor)
        if tc is None:
            return False
        from telethon.tl.types import ReactionEmoji
        await tc.send_reaction(int(chat_id), int(message_id),
                               [ReactionEmoji(str(emoji))])
        logger.info("[userbridge] reacted as owner chat=%s msg=%s", chat_id, message_id)
        return True
    except Exception:
        logger.warning("[userbridge] reaction failed chat=%s msg=%s",
                       chat_id, message_id, exc_info=True)
        return False


def _handler(st: Dict[str, Any]) -> Callable[[Any], Awaitable[None]]:
    async def _on_update(event: Any) -> None:
        try:
            from telethon import utils as tl_utils
            from telethon.tl.types import ReactionCustomEmoji, ReactionEmoji
            cb = st.get("cb")
            peer = getattr(event, "peer", None)
            msg_id = int(getattr(event, "msg_id", 0) or 0)
            if cb is None or peer is None or not msg_id:
                return
            parts: list = []
            for r in (getattr(event, "added", None) or []):
                if isinstance(r, ReactionEmoji):
                    parts.append(str(r.emoticon or ""))
                elif isinstance(r, ReactionCustomEmoji):
                    parts.append("✨")
            parts = [p for p in parts if p]
            if not parts:
                return
            res = cb(str(tl_utils.get_peer_id(peer)), msg_id, " ".join(parts[:6]))
            if asyncio.iscoroutine(res):
                await res
        except Exception:
            logger.debug("[userbridge] reaction update dropped", exc_info=True)
    return _on_update


async def _loop(anchor: Any, st: Dict[str, Any]) -> None:
    """Reconnect forever; the owner's account stream stays open."""
    while not st.get("stop"):
        try:
            tc = st.get("client") or await ensure(anchor)
            if tc is None:
                await asyncio.sleep(15)
                continue
            if not st.get("handler_added"):
                _deps()
                from telethon import events
                tc.add_event_handler(_handler(st), events.NewReaction)
                st["handler_added"] = True
            await tc.run_until_disconnected()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("[userbridge] listener loop error", exc_info=True)
        # disconnected: drop the handle so ensure() reconnects fresh
        st["client"] = None
        st["handler_added"] = False
        if not st.get("stop"):
            await asyncio.sleep(3)


async def start_listener(anchor: Any, on_reaction: ReactionCallback) -> bool:
    """Start (or rebind) the reaction listener on this anchor's state."""
    if anchor is None:
        return False
    st = state(anchor)
    st["cb"] = on_reaction
    if config() is None:
        return False
    task = st.get("task")
    if task is not None and not task.done():
        return True  # already running: the callback above was just refreshed
    st["stop"] = False
    st["task"] = asyncio.get_running_loop().create_task(_loop(anchor, st))
    return True


async def stop_listener(anchor: Any) -> None:
    """Stop the listener and drop the connection (toggle off)."""
    if anchor is None:
        return
    st = state(anchor)
    st["stop"] = True
    task = st.pop("task", None)
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    tc = st.pop("client", None)
    st["handler_added"] = False
    if tc is not None:
        try:
            await tc.disconnect()
        except Exception:
            pass
    logger.info("[userbridge] listener stopped, connection dropped")
