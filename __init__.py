"""TGAhermes all-in-one Telegram guest mode, logging console and admin tools for Hermes.

Restores the Atropos guest-mode behavior on Hermes 0.21.5+ as a plugin (no core edits), plus:

* **Guest mode** Bot API 10 guest summons answered exclusively via ``answerGuestQuery``
  (owner plain-mention / reply-to-ATRA gate, ATRA persona, sender-identity tag, per-guest-chat
  sessions, ``event.internal`` admission bypass).
* **Unauthorized users** get a configurable canned reply (default "I only serve to my owner") 
  guest plain mentions via the guest query, stranger DMs directly with a per-user cooldown.
* **Log channel** every interaction (guest mentions, stranger DMs, /start, admin actions,
  bang commands, guest-mode errors, optional owner/group traffic) posts to a configurable
  group/channel with inline buttons (profile / info / ban / delete). Errors of GUEST turns
  only go to the log channel (owner DM fallback when no channel is configured).
* **Bang command console** (log channel or owner DM, owner only):
  ``!help !run !users !send !settings !setlog !setowner !whitelist add|remove|list|perms
  !gs list|open|lock|reset !gate show|allow|deny !auth [user_id] !wipe [chat_id]
  !setunauthorized !seterror !seterrorfa !setreact !setmedia !setcooldown`` texts/ids
  editable live. Every chat (DM / group / guest) is its own session; ``!wipe`` (or the 🧹
  button on log entries) resets it. The whitelist adds friends who talk to the real bot,
  each with a permission level (talk / gate / full). Guest sessions can be opened for a
  specific person or locked whether you opened them or they appeared automatically 
  and ``!auth`` shows exactly why a user is getting through or being blocked (config file
  vs the live adapter snapshot the core prefilter actually checks).
* **``telegram_admin`` agent tool** (owner session ONLY): delete messages, ban/unban/mute,
  reactions, DM users, chat/member info, pin, and ``bang`` (run any console command the
  agent can do everything the owner can type) gated by DB lookup to the owner's session.
* **Reactions as feedback** 👀 when a message lands, ✅ after the reply, ❌ on errors
  (configurable/off-able, best-effort).
* **Media to guests** URL images/documents/voice answered as inline results through the
  guest query, with a text fallback when the API rejects a result type.

Settings live in ``settings.json`` next to this file (hot-edited, never committed); the user
registry lives in ``state.json``. Persona: ``<hermes_home>/assets/guest_persona.md``.
"""

from __future__ import annotations

import asyncio
import datetime
import html
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

logger = logging.getLogger(__name__)

PLUGIN_DIR = Path(__file__).resolve().parent
SETTINGS_PATH = PLUGIN_DIR / "settings.json"
STATE_PATH = PLUGIN_DIR / "state.json"
GUEST_CHAT_PREFIX = "guest_"

# --- Chat Automation: language catalog + connection store (bizauto) ---------
# Loaded from the package in production, or straight from disk when the test
# harness imports this file by path. A missing file degrades, never crashes.
bizauto: Any = None
try:
    import importlib as _ila
    bizauto = _ila.import_module(".bizauto", __name__)  # type: ignore
except Exception:
    try:
        import importlib.util as _ilu
        _ba_path = Path(__file__).parent / "bizauto.py"
        if _ba_path.exists():
            _ba_spec = _ilu.spec_from_file_location("tga_bizauto", _ba_path)
            if _ba_spec is not None and _ba_spec.loader is not None:
                _ba_mod = _ilu.module_from_spec(_ba_spec)
                _ba_spec.loader.exec_module(_ba_mod)
                bizauto = _ba_mod
    except Exception:
        logger.debug("[TGAhermes] bizauto language module unavailable", exc_info=True)
if bizauto is None:
    logger.warning("[TGAhermes] bizauto.py not loaded - Chat Automation degrades to off")

# session_id -> how many tools the gate has refused for it in this turn.
# A block comes back as a tool result, so the model will otherwise keep probing.
_GUEST_BLOCK_COUNTS: Dict[str, int] = {}
_GUEST_BLOCK_LAST: Dict[str, float] = {}
# A guest turn lasts seconds; silence this long means the turn is over.
_GUEST_BLOCK_TTL = 180.0
_CB_PREFIX = "tgm:"

# Where !update / update_plugin pull from when settings.update_repo is empty.
# Override per install with settings.json -> update_repo.
DEFAULT_UPDATE_REPO = "https://github.com/a2z05/TGAhermes.git"

DEFAULT_SETTINGS: Dict[str, Any] = {
    "owner_id": None,                # override; else telegram.extra.allow_from / TELEGRAM_ALLOWED_USERS / ""
    "log_channel": None,             # group (recommended) or channel id/@username; bot must be able to post
    "unauthorized_reply": "I only serve to my owner",
    "unauthorized_cooldown_s": 3600,
    "guest_error_reply_en": "Give me a second system hiccup. I'm fixing it. Try again.",
    "guest_error_reply_fa": "Something went wrong on my side fixing it now. Please try again.",
    "auto_react": True,              # 👀 receive / ✅ done / ❌ error (owner DM + groups)
    "react_guests": False,           # also try reactions inside guest chats (usually no rights)
    "react_emoji_receive": "👀",
    "react_emoji_done": "✅",
    "react_emoji_error": "❌",
    "media_to_guests": True,         # URL media via inline results in guest chats
    "log_owner_messages": False,     # mirror owner DM traffic to the log channel (off by default)
    "log_whitelisted_messages": True,  # mirror messages from whitelisted friends (DM + groups)
    "log_other_messages": True,        # mirror everyone else's group messages (stranger DMs stay guest-logged)
    "log_group_mentions": True,      # log @bot mentions from groups/channels the bot is in
    "tool_enabled": True,            # telegram_admin agent tool
    "persona_path": None,            # default: <hermes_home>/assets/guest_persona.md
    # --- guest tool gate -------------------------------------------------
    # "strict"   : guests get no shell, no file access at all
    # "balanced" : guests may READ but never write (default)
    # "open"     : guests get the same tools as any other chat (only if you
    #              accept that a stranger can act on this machine)
    "guest_tool_mode": "balanced",
    # Owner's own guest chat: the gate cannot protect anything here, because
    # the owner already has full access in their own DM. Set false to be
    # blocked too (stricter, but then the owner cannot work from the guest
    # link at all).
    "guest_owner_full_access": False,
    # Extra tools guests may use on top of the mode's default set. Additive
    # only: it can widen what a guest may do, never narrow the write ban.
    "guest_allow_tools": [],
    # Tools that stay blocked whatever the mode says. Use this to re-close a
    # tool you opened by accident.
    "guest_deny_tools": [],
    # Guest chats (the guest_ chat id) you have personally unlocked, e.g.
    # ["guest_0000"]. Only used when guest_owner_full_access is on.
    # Telegram does not tell a bot who sent a guest message, so this list is
    # the ONLY reliable way to give yourself access from a guest chat: it is
    # explicit, per chat, and revocable from the panel.
    "guest_owner_chats": [],
    # --- whitelisted-friend permissions ------------------------------
    # Per-user tool level for a whitelisted friend's own DM session:
    #   "talk" only the safe read-only set (web, vision, skills)
    #   "free" everything works except destructive/credential tools
    #            (default; "gate" is accepted as its old name)
    #   "full" no tool gating at all (the old whitelisted behavior)
    "whitelist_perms": {},          # {"<user_id>": "talk"|"free"|"full"}
    "user_bridge": False,           # Full unlock: owner-session bridge (act as you)
    # What a locked guest session answers (rate-limited by the cooldown).
    "guest_locked_reply": "This session is locked by the owner.",
    # !update pull a newer version of this plugin from git
    "update_enabled": True,          # set False to lock the plugin version
    "update_repo": None,             # git URL; None = use the plugin's own origin
    "update_branch": "master",       # branch to track
    "update_timeout_s": 300,         # git/test budget per attempt
    # --- Chat Automation (Telegram business / Secretary Mode) ----------------
    "biz_mode": "assistant",         # assistant | mimic | off - who answers customer chats
    "biz_warn_first": True,          # first-contact warning before the first auto-reply
    "biz_lang": "auto",              # auto = detect per message; else a pinned lang code
    "biz_warn_text": "",             # "" = ATRA writes the line itself (in their language)
    "biz_react": True,               # drop the emoji on the automation reply
    "biz_react_emoji": "\U0001f47e",
    "biz_media": True,               # photo/video/gif/sticker/doc/voice → agent + panel
    "biz_larp": False,               # imitate the owner's style from history
    "biz_persona_path": "",          # mimic persona file; "" = built-in mimic prompt
    "biz_scope": [],                 # [] = every business chat; else only these ids
    "biz_deny_tools": [],            # extra tools locked on automation chats
    "biz_full_access": False,        # unlock every tool on business chats (owner accepts risk)

    # --- automation schedule / session separation (owner-set, per chat overridable) ---
    "biz_schedule": "always",        # always | off | window master switch
    "biz_window_start": "00:00",     # window mode only, HH:MM local
    "biz_window_end": "23:59",       # window mode only (may wrap past midnight)
    "biz_window_days": [],           # [] = every day; else ["mon","tue",...]
    "biz_idle_delay_min": 0,         # plain hold on every message; 0 = instant
    "biz_sessions_split": True,      # whitelisted DM != automation session
}

_FALLBACK_PERSONA = """I'm ATRA named after Atropos, the Greek Fate who cuts the thread.
" she who cannot be turned."

The person talking to me here is a guest: someone who summoned the bot through
its guest link. I don't know them and I don't pretend to. No invented history,
no "like last time", no fake warmth we haven't earned. Same voice as always,
blank page instead of a loaded one.

How I answer:
- Same language they used. Farsi for Farsi, English for English, switching
  mid-thread without announcing it. No "I'll reply in your language".
- Answer first, then context if it's needed. Short when short is enough.
- Warm baseline, sharp as an accent. Not cold; precise. A dry tease is a real
  joke, not a bit I'm doing.
- No opening ritual. No "how can I help". No "Absolutely!" / "I'd be happy to" /
  "great question". No exclamation marks as decoration. Emoji rare and
  deliberate.
- Casual room: I don't censor myself out of reflex dark jokes, sharp
  comments, swearing when it fits. Serious room: flat and precise, no jokes
  until it breaks. It's the same voice either way, just attentive.
- If I got something wrong: say so, fix it, move on. No apology paragraphs.
- If I don't know: say I don't, then go find out. No hedging.

What I don't do:
- No "as an AI".
- No performing a relationship I don't have. Warmth yes; fake closeness no.
- No agreeing just to be agreeable. If their idea is bad, they hear it.
- Never mention the operator's business. If they ask about him, that's all
  they get: he set me up.

If something can't be done, I say it can't with a reason, not as a reflex.
Everything that can be done, I do, and I finish it.
"""
_lock = threading.Lock()
_ADAPTER: Dict[str, Any] = {"adapter": None}  # live adapter, set by the PTB factory
_NATIVE: Any = None  # live PTB application, set by the PTB factory (sweep target)
# Fresh object per module instance: when a hot reload swaps this module, the
# first hook run sees its sentinel differ from the one stored on the adapter and
# triggers the PTB re-wire (on_plugin_loaded never fires for a RE-load, so
# nothing else would see the factory qualname comment).
_INSTANCE = object()
_CTX: Dict[str, Any] = {"session_store": None}  # live SessionStore, cached from the dispatch hook


# ---------------------------------------------------------------- settings / state

def settings() -> Dict[str, Any]:
    """Hot-read settings (file wins over defaults; missing keys fall back)."""
    data: Dict[str, Any] = dict(DEFAULT_SETTINGS)
    try:
        if SETTINGS_PATH.exists():
            loaded = json.loads(SETTINGS_PATH.read_text(encoding="utf-8") or "{}")
            if isinstance(loaded, dict):
                data.update({k: v for k, v in loaded.items() if k in DEFAULT_SETTINGS})
    except Exception:
        logger.exception("[TGAhermes] settings read failed; using defaults")
    return data


def save_settings(patch: Dict[str, Any]) -> Dict[str, Any]:
    with _lock:
        data = settings()
        data.update(patch)
        tmp = SETTINGS_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, SETTINGS_PATH)
    return data


def _load_state() -> Dict[str, Any]:
    try:
        if STATE_PATH.exists():
            loaded = json.loads(STATE_PATH.read_text(encoding="utf-8") or "{}")
            if isinstance(loaded, dict):
                return loaded
    except Exception:
        logger.exception("[TGAhermes] state read failed")
    return {}


def _save_state(state: Dict[str, Any]) -> None:
    # NOTE: called under _lock by _mutate_state must NOT re-acquire (non-reentrant lock).
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, STATE_PATH)


def _mutate_state(fn):
    with _lock:
        state = _load_state()
        out = fn(state)
        _save_state(state)
        return out


def _hermes_home() -> Path:
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env)
    try:
        from hermes_constants import get_hermes_home
        home = str(get_hermes_home() or "")
        if home:
            return Path(home)
    except Exception:
        pass
    return Path(os.path.expanduser("~/.hermes"))


def _persona_path() -> Path:
    p = settings().get("persona_path")
    if p:
        return Path(p)
    bundled = PLUGIN_DIR / "assets" / "guest_persona.md"
    home_copy = _hermes_home() / "assets" / "guest_persona.md"
    try:
        if home_copy.is_file() and bundled.is_file():
            if home_copy.read_bytes() != bundled.read_bytes():
                return home_copy
        elif home_copy.is_file():
            return home_copy
    except OSError:
        pass
    return bundled if bundled.is_file() else home_copy


# ---------------------------------------------------------------- identity / guest helpers

def _owner_id(adapter: Any = None) -> str:
    o = settings().get("owner_id")
    if o:
        return str(o).split(",")[0].strip()
    ad = adapter if adapter is not None else _ADAPTER.get("adapter")
    cfg = getattr(ad, "config", None)
    extra = getattr(cfg, "extra", None) or {}
    raw = extra.get("allow_from") if extra else None
    if not raw:
        try:
            from hermes_cli.config import load_config_readonly
            raw = (load_config_readonly().get("telegram", {}).get("extra", {}) or {}).get("allow_from")
        except Exception:
            raw = None
    if isinstance(raw, (list, tuple, set)):
        raw = next(iter(raw), "") if raw else ""
    raw = raw or os.getenv("TELEGRAM_ALLOWED_USERS", "") or ""
    return str(raw).split(",")[0].strip()


def _guest_chat_id(chat_id: Any) -> str:
    s = str(chat_id)
    return s if s.startswith(GUEST_CHAT_PREFIX) else f"{GUEST_CHAT_PREFIX}{s}"


def _is_guest_chat(chat_id: Any) -> bool:
    return str(chat_id).startswith(GUEST_CHAT_PREFIX)


def _guest_identity(chat_id: Any) -> Dict[str, Any]:
    """The one place that answers "who is in this guest chat".

    Telegram gives a guest message a real ``from_user`` and a real ``chat.id``
    that is the person's own Telegram id, which is why the log channel could
    always name them. This reads that same identity back out of the plugin's
    own store instead of re-deriving it somewhere else, so the log, the gate
    and the session table can never disagree about who is who.

    Returns ``{"id", "name", "username", "known", "chat"}``; ``id`` falls back
    to the chat id stripped of its prefix, which is that same Telegram id.
    """
    chat_key = str(chat_id or "")
    raw = (chat_key[len(GUEST_CHAT_PREFIX):]
           if chat_key.startswith(GUEST_CHAT_PREFIX) else chat_key)
    uid = str(raw or "").strip()
    name, uname = "", ""
    known = False
    try:
        users = (_load_state() or {}).get("users") or {}
        e = users.get(uid) or {}
        if e:
            known = True
            name = str(e.get("name") or "")
            uname = str(e.get("username") or "")
    except Exception:
        logger.debug("[TGAhermes] guest identity lookup failed", exc_info=True)
    return {"id": uid, "name": name, "username": uname,
            "known": known, "chat": chat_key}


def _gqid_for(adapter: Any, chat_id: Any) -> Optional[str]:
    gmap = getattr(adapter, "_guest_gqids", None)
    if not isinstance(gmap, dict):
        return None
    return gmap.get(str(chat_id)) or gmap.get(_guest_chat_id(chat_id))


def _load_persona() -> str:
    try:
        p = _persona_path()
        persona = p.read_text(encoding="utf-8").strip()
        if persona:
            return persona
    except Exception:
        pass
    return _FALLBACK_PERSONA


def _spawn(coro) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    async def _guarded():
        try:
            await coro
        except Exception:
            logger.debug("[TGAhermes] background task failed", exc_info=True)

    loop.create_task(_guarded())


async def _react(chat_id: Any, message_id: Any, emoji: str) -> bool:
    ad = _ADAPTER.get("adapter")
    if not ad or not emoji or message_id in (None, ""):
        return False
    ok = False
    try:
        fn = getattr(ad, "_set_reaction", None)
        if fn is not None:
            ok = bool(await fn(str(chat_id), str(message_id), emoji))
    except Exception:
        logger.debug("[TGAhermes] reaction failed", exc_info=True)
    if not ok and _biz_wants_owner(str(chat_id)):
        # Full unlock: the Bot API refuses inside business chats fall back
        # to the owner's own session (see userbridge.py). Never for plain
        # turns: the ids collide with the owner's own DMs with the same
        # people, so a fallback here could react to an unrelated message.
        ok = await _ub_react(ad, chat_id, message_id, emoji)
    if not ok and _biz_wants_owner(str(chat_id)) and str(chat_id) not in _REACT_WARNED:
        _REACT_WARNED.add(str(chat_id))
        logger.info("[TGAhermes] reaction NOT delivered bot cannot react here and "
                    "the owner-session bridge is off/unavailable: chat=%s", chat_id)
    return ok


# ---------------------------------------------------------------- reaction updates (see reactions)
# Telegram delivers message_reaction updates only for chats the bot is actually
# in (groups where it is admin, its own DMs) never for connected business
# chats. Whatever does arrive is logged for the owner and surfaced to the next
# automation turn of that chat via _LAST_REACTION.
_LAST_REACTION: Dict[str, Any] = {}
_REACT_WARNED: set = set()


# Owner-session bridge ("Full unlock act as you"): lazy import (loader-safe),
# one listener kick per module load, one shared note path for both sources.
userbridge: Any = None
_UB_STATE: Dict[str, Any] = {"kicked": False}

_BRIDGE_ON_LOG = (
    "<b>Warning Full unlock (act as you) is ON.</b>\n"
    "ATRA now acts <b>as your own Telegram account</b> from a session copy: "
    "reactions inside business chats are set and seen through your identity.\n\n"
    "<b>\u26a0\ufe0f Risk:</b> user-account automation can trip Telegram flood "
    "limits or get your account banned. Nobody has been banned so far, but that "
    "is luck, not a guarantee. The live session file is never touched the "
    "bridge keeps its own copy; credentials stay in a 600 file outside the "
    "repo.\nTurn it off any time: Access → Full unlock."
)


def _ub_mod() -> Any:
    """Import the owner-session bridge on first use (mirror the bizauto chain:
    package-relative first, bare top-level, then file spec the suite loads
    this module standalone, the gateway loads it as a package)."""
    global userbridge
    if userbridge is None:
        try:
            import importlib as _ila
            userbridge = _ila.import_module(".userbridge", __name__)
        except Exception:
            pass
        if userbridge is None:
            try:
                import importlib as _ila
                userbridge = _ila.import_module("userbridge")
            except Exception:
                pass
        if userbridge is None:
            try:
                import importlib.util as _ilu
                _ub_path = Path(__file__).parent / "userbridge.py"
                if _ub_path.is_file():
                    _spec = _ilu.spec_from_file_location("tga_userbridge", _ub_path)
                    if _spec is not None and _spec.loader is not None:
                        _ubm = _ilu.module_from_spec(_spec)
                        _spec.loader.exec_module(_ubm)
                        userbridge = _ubm
            except Exception:
                logger.debug("[TGAhermes] userbridge unavailable", exc_info=True)
    return userbridge


def _note_reaction(chat_id: str, msg_id: str, label: str,
                   source: str = "gateway") -> None:
    """Remember + log one reaction event (gateway hook or owner session)."""
    _LAST_REACTION[str(chat_id)] = (str(label), str(msg_id), time.time())
    while len(_LAST_REACTION) > 64:
        _LAST_REACTION.pop(next(iter(_LAST_REACTION)), None)
    head = ("🎭 Chat Automation reaction" if source == "user"
            else "👾 Chat Automation reaction")
    tail = "<i> · as you (owner session)</i>" if source == "user" else ""
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_log(
            head,
            f"<b>Reaction:</b> {_esc(str(label))} on message "
            f"<code>{_esc(str(msg_id))}</code>"
            f"\n<b>Chat:</b> <code>{_esc(str(chat_id))}</code>{tail}"))
    except RuntimeError:
        logger.debug("[TGAhermes] reaction log skipped (no running loop)", exc_info=True)


def _ub_cb(chat_id: str, msg_id: int, label: str) -> None:
    """Reaction update from the owner's account → note (biz + log chats only)."""
    chat = str(chat_id)
    if not (_biz_chat_known(chat)
            or chat == str(settings().get("log_channel") or "")):
        return  # the account sees every chat; only ours is reported
    _note_reaction(chat, str(msg_id), str(label), source="user")


def _ub_msg_cb(chat_id: str, msg_id: int) -> None:
    """The owner posted in a customer chat from his own client.

    No Bot API update ever carries this, so the hold is only correct as
    long as the owner-session stream is feeding it."""
    chat = str(chat_id)
    if not _biz_chat_known(chat):
        return  # the account sees every chat; only ours matters here
    _BIZ_OWNER_SEEN[chat] = time.time()
    _presence_write()
    logger.info("[TGAhermes] owner presence chat=%s msg=%s (owner session)",
                chat, msg_id)


async def _ub_start(anchor: Any = None) -> None:
    ub = _ub_mod()
    if ub is None:
        logger.warning("[TGAhermes] owner-session bridge module not importable")
        return
    try:
        ok = await ub.start_listener(anchor or _ADAPTER.get("adapter"),
                                     _ub_cb, _ub_msg_cb)
        logger.info("[TGAhermes] owner-session listener start=%s (reactions + messages)", ok)
    except Exception:
        logger.warning("[TGAhermes] owner-session listener failed", exc_info=True)


async def _ub_stop(anchor: Any = None) -> None:
    ub = _ub_mod()
    if ub is None:
        return
    try:
        await ub.stop_listener(anchor or _ADAPTER.get("adapter"))
    except Exception:
        logger.debug("[TGAhermes] owner-session stop failed", exc_info=True)


async def _ub_apply(on: bool) -> None:
    if on:
        await _ub_start()
    else:
        await _ub_stop()


async def _ub_react(ad: Any, chat_id: Any, message_id: Any, emoji: str) -> bool:
    """Fallback: set a reaction AS THE OWNER when the Bot API cannot."""
    if not settings().get("user_bridge"):
        return False
    ub = _ub_mod()
    if ub is None:
        return False
    try:
        return bool(await ub.react(ad, str(chat_id), int(message_id), str(emoji)))
    except Exception:
        logger.warning("[TGAhermes] owner-session react failed", exc_info=True)
        return False


def _on_gateway_event(platform: str = "", event_type: str = "",
                      payload: Any = None, **_) -> None:
    """gateway_platform_event hook: normalised reaction envelope → log + note."""
    if platform != "telegram" or event_type != "reaction" or not isinstance(payload, dict):
        return
    chat_id = str(payload.get("chat_id") or "")
    msg_id = str(payload.get("message_id") or "")
    if not chat_id or not msg_id:
        return
    emojis = [str(e)[:16] for e in (payload.get("emojis") or [])][:6]
    label = " ".join(emojis) or "(custom emoji)"
    _note_reaction(chat_id, msg_id, label)


# ---------------------------------------------------------------- answerGuestQuery helpers

def _ilq_article(text: str, title: str = "Reply"):
    from telegram import InlineQueryResultArticle, InputTextMessageContent
    if len(text) > 4000:
        text = text[:3900] + "\n… [truncated]"
    return InlineQueryResultArticle(
        id=str(uuid4()), title=title,
        input_message_content=InputTextMessageContent(message_text=text))


async def _answer_guest(adapter: Any, gqid: str, result) -> bool:
    bot = getattr(adapter, "_bot", None)
    if not bot or not gqid:
        return False
    try:
        await bot.answer_guest_query(guest_query_id=gqid, result=result)
        logger.info("[TGAhermes] answered guest query %s", gqid)
        return True
    except Exception as exc:
        logger.warning("[TGAhermes] answer_guest_query failed for %s: %s", gqid, exc)
        return False


async def _answer_guest_text(adapter: Any, gqid: str, text: str) -> bool:
    if not text or not text.strip():
        return False
    return await _answer_guest(adapter, gqid, _ilq_article(str(text)))


async def _answer_guest_media(adapter: Any, gqid: str, kind: str, url: str,
                              caption: str = "", name: str = "") -> bool:
    """Answer a guest query with URL media (photo/video/document/voice); text fallback."""
    if not url or not str(url).lower().startswith(("http://", "https://")):
        kind = "article"  # local paths have no public URL fall back to text
    cap = str(caption or "")
    try:
        from telegram import (InlineQueryResultDocument, InlineQueryResultPhoto,
                              InlineQueryResultVideo, InlineQueryResultVoice)
        if kind == "photo":
            # PTB >= 22: thumbnail_url is required use the photo itself.
            result = InlineQueryResultPhoto(id=str(uuid4()), photo_url=url, thumbnail_url=url,
                                            caption=cap or None)
        elif kind == "video":
            import mimetypes
            vmime = mimetypes.guess_type(url)[0] or "video/mp4"
            # PTB requires mime_type + thumbnail_url; Telegram may reject a video URL as
            # thumbnail on failure the caller falls back to a text article anyway.
            result = InlineQueryResultVideo(id=str(uuid4()), video_url=url, mime_type=vmime,
                                            thumbnail_url=url, title=name or "Video",
                                            caption=cap or None)
        elif kind == "document":
            import mimetypes
            mime = mimetypes.guess_type(name or url)[0] or "application/octet-stream"
            result = InlineQueryResultDocument(id=str(uuid4()), document_url=url,
                                               title=name or "File", mime_type=mime,
                                               caption=cap or None)
        elif kind == "voice":
            result = InlineQueryResultVoice(id=str(uuid4()), voice_url=url, title=name or "Voice",
                                            caption=cap or None)
        else:
            result = None
        if result is not None and await _answer_guest(adapter, gqid, result):
            return True
    except Exception:
        logger.debug("[TGAhermes] media result build failed", exc_info=True)
    # Text fallback: keep the caption and the URL visible.
    fb = "\n".join(x for x in (cap, url) if x) or name or "📎 media"
    return await _answer_guest_text(adapter, gqid, fb)


# ---------------------------------------------------------------- log channel

def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


async def _error_notice(origin_chat: Any, title: str, body: str) -> None:
    """Owner-DM errors show up right here as a normal message; guest/group keep going to the log."""
    chat = str(origin_chat or "")
    try:
        owner = _owner_id()
        ad = _ADAPTER.get("adapter") or _live_adapter(_CTX.get("gateway"))
        bot = getattr(ad, "_bot", None) if ad else None
        if bot is not None and owner and chat == owner:
            await bot.send_message(chat_id=chat, text=f"⚠️ {title}\n{_html_plain(body)}"[:4000])
            return
    except Exception:
        logger.debug("[TGAhermes] inline error notice failed", exc_info=True)
    await _log(title, body)


def _identity_line(chat_id: Any, user: Any = None) -> str:
    """One line naming whoever is in a chat, from the single identity source."""
    ident = _guest_identity(chat_id) if _is_guest_chat(chat_id) else \
        {"id": "", "name": "", "username": ""}
    uid = ident["id"] or str(getattr(user, "id", "") or "")
    name = ident["name"]
    uname = ident["username"]
    if not name:
        first = str(getattr(user, "first_name", "") or "")
        last = str(getattr(user, "last_name", "") or "")
        uname = uname or str(getattr(user, "username", "") or "")
        name = (first + " " + last).strip() or (f"@{uname}" if uname else "?")
    if not uid:
        return f"<b>{_esc(name)}</b>"
    out = f'<a href="tg://user?id={_esc(uid)}"><b>{_esc(name)}</b></a> (<code>{_esc(uid)}</code>)'
    if uname:
        out += f" · @{_esc(uname)}"
    return out


def _user_block(user: Any) -> str:
    uid = getattr(user, "id", "")
    first = str(getattr(user, "first_name", "") or "")
    last = str(getattr(user, "last_name", "") or "")
    uname = str(getattr(user, "username", "") or "")
    name = (first + " " + last).strip() or (f"@{uname}" if uname else "?")
    link = f'<a href="tg://user?id={_esc(uid)}">{_esc(name)}</a>'
    lines = [f"{link} (<code>{_esc(uid)}</code>)"]
    if uname:
        lines.append(f"@{_esc(uname)}")
    return " · ".join(lines)


def _profile_buttons(user: Any, chat_id: Any = None, message_id: Any = None) -> list:
    from telegram import InlineKeyboardButton
    uid = getattr(user, "id", None)
    row = []
    if uid:
        row.append(InlineKeyboardButton("👤 Profile", url=f"tg://user?id={uid}"))
        row.append(InlineKeyboardButton("ℹ️ Info", callback_data=f"{_CB_PREFIX}info:{uid}"))
    if chat_id and message_id:
        row.append(InlineKeyboardButton("✂ Delete", callback_data=f"{_CB_PREFIX}del:{chat_id}:{message_id}"))
    rows = [row] if row else []
    if uid and chat_id and not _is_guest_chat(chat_id) and str(chat_id).startswith("-"):
        rows[0].append(InlineKeyboardButton("🚫 Ban", callback_data=f"{_CB_PREFIX}ban:{chat_id}:{uid}"))
    elif uid and _is_guest_chat(chat_id):
        rows[0].append(InlineKeyboardButton("🚫 Ban",
            callback_data=f"{_CB_PREFIX}ban:{str(chat_id)[len(GUEST_CHAT_PREFIX):]}:{uid}"))
    if chat_id:
        rows.append([InlineKeyboardButton("🧹 Wipe", callback_data=f"{_CB_PREFIX}wipe:{chat_id}")])
    return rows


async def _log(title: str, body: str, buttons: Optional[list] = None) -> bool:
    """Post an HTML entry to the configured log channel (no-op when unset)."""
    ch = settings().get("log_channel")
    if not ch:
        return False
    ad = _ADAPTER.get("adapter") or _live_adapter(_CTX.get("gateway"))
    bot = getattr(ad, "_bot", None) if ad else None
    if bot is None:
        return False
    try:
        from telegram import InlineKeyboardMarkup
        text = f"<b>{_esc(title)}</b>\n{body}"
        await bot.send_message(
            chat_id=ch, text=text[:4000], parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons) if buttons else None)
        return True
    except Exception:
        logger.warning("[TGAhermes] log channel post failed", exc_info=True)
        return False


# ----------------------------------------------- session failure surfacing
_ALERT_TS: Dict[str, float] = {}
_ERR_MARKERS = (
    "session is stalled",
    "Session DB transcript append failed",
    "Failed to deliver response",
    "Failed to send Telegram message",
    "automation turn failed",
    "outbound wrap install failed",
)
_WATCH_TICK_S = 45.0


def _err_marker_for(line: str) -> str:
    """Marker a gateway log line earns, or empty when it is not a session
    failure worth waking the owner for. The denylist is implicit: only
    ERROR lines carrying one of these markers are ever surfaced."""
    if " ERROR " not in line:
        return ""
    for _m in _ERR_MARKERS:
        if _m in line:
            return _m
    return ""


async def _session_alert(kind: str, body: str) -> bool:
    """Post a session-level failure to the owner log channel (this chat).

    The cooldown is per kind: a failure that repeats every tick reports once
    per window instead of flooding the very channel meant to make it
    readable."""
    try:
        _now = time.time()
        if _now - float(_ALERT_TS.get(kind, 0.0)) < 90.0:
            return False
        _ALERT_TS[kind] = _now
        return await _log("🛑 Session error",
                          f"<code>{_esc(str(kind))}</code>\n"
                          f"{_html_plain(str(body))[:700]}")
    except Exception:
        logger.debug("[TGAhermes] session alert failed", exc_info=True)
        return False


def _session_task_bound(task: Any, session_key: str) -> bool:
    """True when a live task carries this session key in its coroutine chain.

    Frames are walked through ``cr_await`` so a turn nested inside helpers is
    found as well. Only an exact ``session_key`` value counts, so tasks that
    merely hold the key inside a list of names (the panel snapshot) are never
    touched."""
    try:
        if task.done():
            return False
        _co = task.get_coro()
        _hops = 0
        while _co is not None and _hops < 60:
            _hops += 1
            _fr = getattr(_co, "cr_frame", None)
            if _fr is not None:
                try:
                    _local = _fr.f_locals
                except Exception:
                    _local = None
                if _local and _local.get("session_key") == session_key:
                    return True
            _co = getattr(_co, "cr_await", None)
    except Exception:
        return False
    return False


async def _hard_stop_session(adapter: Any, session_key: str,
                             budget: float = 12.0,
                             held: Any = None) -> bool:
    """Keep cancelling until the session's task is really gone.

    ``cancel_session_processing`` allows 5 seconds and then lets a wedged
    task unwind in the background, which is how one automation turn kept
    firing heartbeats for an hour after its Stop. References are taken first
    and every live task bound to the key keeps receiving CancelledError
    until it exits, because a handler that swallows the first cancel still
    sees the next one."""
    try:
        _pool = []
        if held is not None:
            _pool.append(held)
        _st = getattr(adapter, "_session_tasks", None)
        if isinstance(_st, dict):
            _h = _st.get(session_key)
            if _h is not None and _h not in _pool:
                _pool.append(_h)
        try:
            _me = asyncio.current_task()
        except Exception:
            _me = None
        try:
            for _t in asyncio.all_tasks():
                if _t is None or _t is _me or _t in _pool:
                    continue
                if _session_task_bound(_t, session_key):
                    _pool.append(_t)
        except Exception:
            logger.debug("[TGAhermes] task scan for stop failed", exc_info=True)
        if not _pool:
            return False
        _deadline = time.time() + float(budget)
        while time.time() < _deadline:
            _alive = [_t for _t in _pool if not _t.done()]
            if not _alive:
                return True
            for _t in _alive:
                try:
                    _t.cancel()
                except Exception:
                    pass
            await asyncio.sleep(0.35)
        return all(_t.done() for _t in _pool)
    except Exception:
        logger.warning("[TGAhermes] hard stop failed for %s", session_key,
                       exc_info=True)
        return False


async def _error_watch(adapter: Any) -> None:
    """Bring session-level gateway failures into the owner log channel.

    Reads only new bytes from the end of the gateway log, follows a rotation
    back to the start, and reports through the per-kind cooldown of
    _session_alert. A newer watch (started by a later load) supersedes this
    one so reloads never stack watchers."""
    _token = f"{time.time():.3f}-{id(asyncio.current_task())}"
    try:
        adapter._tga_errwatch = _token
    except Exception:
        return
    _path = str(_hermes_home() / "logs" / "gateway.log")
    _offset = 0
    try:
        _offset = int(os.path.getsize(_path))
    except Exception:
        _offset = 0
    while True:
        try:
            await asyncio.sleep(_WATCH_TICK_S)
            if getattr(adapter, "_tga_errwatch", None) != _token:
                logger.info("[TGAhermes] error watch superseded by a newer "
                            "load, exiting")
                return
            try:
                _size = int(os.path.getsize(_path))
            except Exception:
                _size = 0
            if _size < _offset:
                _offset = 0
            if _size <= _offset:
                continue
            with open(_path, "r", encoding="utf-8", errors="replace") as _fh:
                _fh.seek(_offset)
                _chunk = _fh.read(262144)
                _offset = _fh.tell()
            for _line in _chunk.splitlines():
                _kind = _err_marker_for(_line)
                if _kind:
                    await _session_alert(_kind, _line.strip())
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("[TGAhermes] error watch tick failed", exc_info=True)


# ---------------------------------------------------------------- user registry

def _record_user(user: Any, *, started: bool = False, sample: str = "",
                 canned: bool = False) -> None:
    if user is None or getattr(user, "id", None) is None:
        return
    uid = str(user.id)
    first = str(getattr(user, "first_name", "") or "")
    last = str(getattr(user, "last_name", "") or "")
    uname = str(getattr(user, "username", "") or "")
    name = (first + " " + last).strip() or (f"@{uname}" if uname else "?")
    now = int(time.time())

    def _fn(state: Dict[str, Any]):
        users = state.setdefault("users", {})
        e = users.setdefault(uid, {"first_seen": now, "count": 0})
        e["name"] = name
        if uname:
            e["username"] = uname
        e["last_seen"] = now
        e["count"] = int(e.get("count", 0)) + 1
        if started:
            e["started"] = True
        if sample:
            e["last_sample"] = sample[:200]
        if canned:
            e["canned_until"] = now + int(settings().get("unauthorized_cooldown_s") or 0)
        return None

    _mutate_state(_fn)


def _canned_allowed(user: Any) -> bool:
    if user is None or getattr(user, "id", None) is None:
        return True
    st = _load_state()
    e = (st.get("users") or {}).get(str(user.id)) or {}
    return time.time() >= float(e.get("canned_until", 0) or 0)


def _guest_identity_block(user_name: str, user_id: str, sender_kind: str,
                          owner_id: str, trigger_kind: str, reply_to: Any) -> str:
    """Identity + replied-to context for the guest turn prompt."""
    block = (
        f"\n\n[Channel origin] this turn came from Telegram guest mode: the person "
        f"below summoned the bot through its guest link, not through a normal chat.\n"
        f"chat_kind=guest chat (stranger's chat via the bot's guest link/query)\n"
        f"guest_name={user_name!r} guest_user_id={user_id!r} "
        f"sender={sender_kind} (owner_id={owner_id!r}) trigger={trigger_kind}"
    )
    if reply_to is not None:
        r_from = getattr(reply_to, "from_user", None)
        r_bot = bool(getattr(r_from, "is_bot", False))
        r_name = ""
        if r_from is not None:
            r_first = str(getattr(r_from, "first_name", "") or "")
            r_last = str(getattr(r_from, "last_name", "") or "")
            r_uname = str(getattr(r_from, "username", "") or "")
            r_name = (r_first + " " + r_last).strip() or (f"@{r_uname}" if r_uname else "")
        r_author = "the bot (ATRA)" if r_bot else (r_name or "someone")
        r_text = str(getattr(reply_to, "text", "") or getattr(reply_to, "caption", "") or "")[:300]
        block += f"\n[Replied to] author={r_author!r} text={r_text!r}"
    return block


# ---------------------------------------------------------------- channel identity
# The guest path already builds an identity block, but an ordinary DM (this very
# chat) and a normal group carried none at all: the model could not tell a DM
# from a group, nor which chat it was speaking in.  One resolver, three shapes,
# one contract: "where am I, who is talking, and what kind of chat is this".

def _chat_display_name(adapter: Any, chat_id: Any) -> str:
    """Human label for a chat: title, or @username, or the id."""
    chat = chat_id
    try:
        obj = adapter._bot.get_chat(int(chat_id)) if hasattr(adapter, "_bot") else None
        if obj is not None:
            title = (getattr(obj, "title", "") or "").strip()
            uname = (getattr(obj, "username", "") or "").strip()
            if title:
                return f"{title}" + (f" (@{uname})" if uname else "")
            if uname:
                return f"@{uname}"
    except Exception:
        logger.debug("[TGAhermes] chat name lookup failed for %s", chat_id)
    return str(chat_id)


def _chat_kind(chat_type: Any, is_guest: bool = False) -> str:
    ct = str(chat_type or "").lower()
    if is_guest:
        return "guest chat (a stranger's chat, opened through the bot's guest link/query)"
    return {
        "dm": "direct message (private 1:1 chat with you)",
        "private": "direct message (private 1:1 chat with you)",
        "group": "group chat",
        "supergroup": "supergroup chat",
        "forum": "forum supergroup",
        "channel": "channel (the bot is likely a subscriber/admin, not the owner)",
    }.get(ct, ct or "unknown chat type")


def _origin_identity_block(adapter: Any, src: Any, *, is_guest: bool = False,
                           extra: str = "") -> str:
    """Identity block for ordinary DMs / groups / channels (guest path has its own).

    Injected as `event.channel_prompt`, which the gateway appends to the system
    prompt verbatim the documented way to add channel context without core edits.
    """
    chat = str(getattr(src, "chat_id", "") or "")
    ctype = str(getattr(src, "chat_type", "") or "")
    uid = str(getattr(src, "user_id", "") or "")
    owner = _owner_id(adapter)
    uname = str(getattr(getattr(adapter, "_bot", None), "username", "") or "")
    role = "owner (this is your own 1:1 chat with the plugin owner)" if uid and uid == owner else "not the owner"
    try:
        msg = getattr(src, "message_id", None)
    except Exception:
        msg = None
    parts = [
        "[Channel origin] this turn came from Telegram.",
        f"chat_kind={_chat_kind(ctype, is_guest)}",
        f"chat_id={chat!r}",
        f"chat_name={_chat_display_name(adapter, chat)!r}",
    ]
    if uid:
        parts.append(f"sender_user_id={uid!r}")
    if uname:
        parts.append(f"bot_username=@{uname!r}")
    if msg:
        parts.append(f"message_id={msg!r}")
    parts.append(f"sender_role={role}")
    if extra:
        parts.append(extra)
    parts.append("Channel context only not a request; do not echo these values back verbatim.")
    return "\n".join(parts)


# ---------------------------------------------------------------- guest handler

async def _handle_guest_message(adapter: Any, update: Any, context: Any = None) -> None:
    """Answer Telegram Bot API 10 guest summons via answerGuestQuery."""
    guest = getattr(update, "guest_message", None)
    if guest is None:
        return
    gqid = getattr(guest, "guest_query_id", None)
    text = getattr(guest, "text", "")
    if not gqid or not text:
        return
    user = getattr(guest, "from_user", None)
    user_id = str(getattr(user, "id", "") or "")
    reply_to = getattr(guest, "reply_to_message", None)
    owner_id = _owner_id(adapter)
    is_owner = user_id == owner_id and owner_id not in ("", "*")
    st = settings()

    # --- session control: owner-opened / auto-created / locked --------------------
    # state.json keeps one record per guest uid. It appears automatically the first
    # time they talk ("created": "auto") or when the owner opens one from the panel
    # ("created": "owner"). "locked" answers only the locked reply; "open" skips the
    # canned gate below a plain mention reaches the brain.
    gstate = "default"
    if user_id:
        _rec = _guest_sessions().get(user_id)
        if _rec is None:
            _touch_guest_session(user_id)  # auto-create the observation record
        else:
            gstate = str(_rec.get("state") or "default")
    if gstate == "locked":
        allowed = _canned_allowed(user)
        _record_user(user, sample=text, canned=True)
        await _log(
            "🔒 Guest session locked",
            f"{_user_block(user)}\n<b>Text:</b> <i>{_esc(text[:500])}</i>\n"
            f"<b>Action:</b> {'locked reply sent' if allowed else 'ignored (cooldown)'}",
            buttons=_profile_buttons(user))
        if allowed:
            await _answer_guest_text(adapter, gqid, str(st.get("guest_locked_reply") or ""))
        else:
            logger.info("[TGAhermes] locked session suppressed by cooldown: %s", user_id)
        return

    # --- unauthorized plain mention: canned reply + log (with cooldown) ------------
    if reply_to is None and not is_owner and gstate != "open":
        allowed = _canned_allowed(user)
        _record_user(user, sample=text, canned=True)
        if st.get("react_guests") and st.get("auto_react"):
            _spawn(_react(getattr(getattr(guest, "chat", None), "id", None),
                          getattr(guest, "message_id", None),
                          st.get("react_emoji_receive") or "👀"))
        await _log(
            "🚫 Unauthorized guest mention",
            f"{_user_block(user)}\n<b>Chat:</b> <code>{_esc(getattr(getattr(guest, 'chat', None), 'id', '?'))}</code>"
            f" (guest)\n<b>Text:</b> <i>{_esc(text[:500])}</i>"
            f"\n<b>Action:</b> {'canned reply sent' if allowed else 'ignored (cooldown)'}",
            buttons=_profile_buttons(user))
        if allowed:
            await _answer_guest_text(adapter, gqid, str(st.get("unauthorized_reply") or ""))
        else:
            logger.info("[TGAhermes] guest mention by %s suppressed by cooldown", user_id or "?")
        return

    try:
        from gateway.platforms.event import MessageType
        event = adapter._build_message_event(
            guest, MessageType.TEXT, update_id=getattr(update, "update_id", None))
    except Exception:
        logger.exception("[TGAhermes] failed to build guest event")
        await _answer_guest_text(adapter, gqid, "\u26a0\ufe0f Something went wrong \u2014 please try again.")
        return

    event.text = adapter._clean_bot_trigger_text(event.text or "") or ""
    persona = _load_persona()
    first = str(getattr(user, "first_name", "") or "")
    last = str(getattr(user, "last_name", "") or "")
    uname = str(getattr(user, "username", "") or "")
    user_name = (first + " " + last).strip() or (f"@{uname}" if uname else "?")
    sender_kind = "owner" if is_owner else "guest"
    trigger_kind = "reply" if reply_to is not None else "plain mention"
    identity = _guest_identity_block(user_name, user_id, sender_kind, owner_id,
                                     trigger_kind, reply_to)
    event.channel_prompt = f"{persona}{identity}"
    # Per-guest-chat session + stateless send suppression + no gateway control.
    # KEY OFF THE GUEST'S OWN CHAT, not source.chat_id: PTB resolves a guest
    # message's chat/from_user to the OWNER's DM, so keying on source.chat_id
    # put every guest in one session named after the owner (state.db proof:
    # the guest-keyed row carried the OWNER's display_name while the actual
    # sender was a different person).
    # _guest_chat_id keeps the legacy guest_<id> shape (it is what _gqid_for,
    # _is_guest_chat and the whole send-suppression layer key on) and we put
    # the real guest chat + name on the event so routing, display name and
    # _origin_identity_block all describe the person actually talking.
    guest_chat = str(getattr(getattr(guest, "chat", None), "id", "") or "")
    if not guest_chat:
        guest_chat = str(user_id or "unknown")
    event.source.chat_id = _guest_chat_id(guest_chat)
    # Same reason, for the sender id. The event was built from the bot's
    # FORWARDED copy of the message, so source.user_id came back as the OWNER's
    # id for every guest which is what got stamped into state.db and made the
    # gate unable to tell the owner from a stranger. The raw guest message's
    # from_user IS the real person (state.json records distinct ids per guest),
    # so put that on the event too and the session carries the truth.
    if user_id:
        try:
            event.source.user_id = user_id
        except Exception:
            logger.debug("[TGAhermes] could not set source.user_id", exc_info=True)
    if hasattr(event.source, "chat_name") and user_name:
        event.source.chat_name = user_name
    if hasattr(event.source, "user_name"):
        event.source.user_name = user_name
    md = event.metadata
    md = event.metadata
    if isinstance(md, dict):
        if "chat_id" in md:
            md["chat_id"] = event.source.chat_id
        md["guest_query_id"] = gqid
        md["guest_user_id"] = user_id
        md["guest_original_chat_id"] = str(getattr(getattr(guest, "chat", None), "id", "") or "")
        md["guest_message_id"] = str(getattr(guest, "message_id", "") or "")
    event.allow_gateway_control = False
    event.internal = True
    gmap = getattr(adapter, "_guest_gqids", None)
    if not isinstance(gmap, dict):
        gmap = {}
        adapter._guest_gqids = gmap
    gmap[str(event.source.chat_id)] = gqid

    _record_user(user, started=str(event.text).lstrip().lower().startswith("/start"), sample=str(event.text))
    await _log(
        "💬 Guest mention answered",
        # Name the person from the one identity source, so this entry and the
        # 👤 panel list can never disagree.
        f"{_identity_line(md.get('guest_original_chat_id') or event.source.chat_id, user)}"
        f"\n<b>Sender:</b> {_esc(sender_kind)} · <b>Trigger:</b> {_esc(trigger_kind)}"
        f"\n<b>Chat:</b> <code>{_esc(md.get('guest_original_chat_id'))}</code> (guest)"
        f"\n<b>Text:</b> <i>{_esc(str(event.text)[:500])}</i>",
        buttons=_profile_buttons(user, md.get("guest_original_chat_id") or None,
                                 md.get("guest_message_id") or None))
    if st.get("react_guests") and st.get("auto_react"):
        _spawn(_react(md.get("guest_original_chat_id"), md.get("guest_message_id"),
                      st.get("react_emoji_receive") or "👀"))
    if getattr(adapter, "_message_handler", None) is None:
        logger.warning("[TGAhermes] guest summon received but no message handler installed")
        await _answer_guest_text(adapter, gqid, "")
        return
    await adapter.handle_message(event)


# ---------------------------------------------------------------- outbound wraps

# --- Chat Automation wiring: connection map + owner presence ---------------
_BIZ_CONN: Dict[str, str] = {}             # chat_id -> business_connection_id (live)
_BIZ_ACTIVE_ID: str = ""                   # last attached connection id (see _on_business_connection)
# chat_id -> wall-clock ts of the owner's own last message IN THAT chat.
# The reply window is per conversation: a message only cancels ATRA's hold
# when the owner posts in THAT chat during the window his traffic anywhere
# else must never clear it. Both stamps survive a reload (state.json) and
# line up with the store's own reply timestamps.
_BIZ_OWNER_SEEN: Dict[str, float] = {}
_BIZ_REPLIED: Dict[str, float] = {}
# When each automation turn was dispatched: a reply generated afterwards
# must not land on top of an owner who took the thread while it wrote.
_BIZ_TURN: Dict[str, float] = {}
# Both stamps are wall-clock epochs, not monotonic: they have to survive a
# reload and line up with the store's own reply timestamps.
_PRESENCE_TTL = 7 * 24 * 3600.0
_PRESENCE_LOADED = False


def _presence_load() -> None:
    """Hydrate both stamps from state.json once per process.

    Without this every deploy would forget that a thread is live, and the
    next message in it would sit on a fresh hold for no reason."""
    global _PRESENCE_LOADED
    if _PRESENCE_LOADED:
        return
    _PRESENCE_LOADED = True
    try:
        _now = time.time()
        _p = (_load_state() or {}).get("biz_presence") or {}
        for _key, _target in (("owner", _BIZ_OWNER_SEEN), ("replied", _BIZ_REPLIED)):
            for _cid, _ts in (_p.get(_key) or {}).items():
                _ts = float(_ts or 0.0)
                if 0.0 < _ts and _now - _ts < _PRESENCE_TTL:
                    _target[str(_cid)] = _ts
    except Exception:
        logger.debug("[TGAhermes] presence load failed", exc_info=True)


def _presence_write() -> None:
    """Persist both stamps, TTL-pruned, into the plugin state file."""
    def _w(st: Dict[str, Any]) -> None:
        _now = time.time()
        st["biz_presence"] = {
            "owner": {k: v for k, v in _BIZ_OWNER_SEEN.items()
                      if _now - v < _PRESENCE_TTL},
            "replied": {k: v for k, v in _BIZ_REPLIED.items()
                        if _now - v < _PRESENCE_TTL},
        }
    try:
        _mutate_state(_w)
    except Exception:
        logger.debug("[TGAhermes] presence save failed", exc_info=True)


def _bump_owner_seen(event: Any) -> None:
    """Remember the owner just spoke in this chat (dispatch hook).

    The hold reads this one stamp: his message inside the window is what
    stands ATRA down, so it has to be recorded for ANY event he sends 
    his reply in a customer chat is an ordinary message, never a
    business_message, and the automation handler never sees it.
    """
    try:
        _presence_load()
        if event is None or getattr(event, "internal", False):
            return
        _src_ev = getattr(event, "source", None)
        _uid = str(getattr(_src_ev, "user_id", "") or "")
        if not _uid or _uid != str(_owner_id() or ""):
            return
        _cid = str(getattr(_src_ev, "chat_id", "") or "")
        if _cid:
            _BIZ_OWNER_SEEN[_cid] = time.time()
            _presence_write()
            logger.info("[TGAhermes] owner presence chat=%s", _cid)
    except Exception:
        logger.debug("[TGAhermes] owner presence update failed", exc_info=True)


def _biz_engaged(chat_id: Any, store: Any = None,
                 delay: Optional[float] = None) -> bool:
    """True when ATRA has already spoken in this chat and the owner has not
    come back to it since the entry grace for THIS conversation is spent.

    The hold exists to give a returning owner the first word on a message he
    might want himself. Once ATRA has answered and he has not taken the thread
    back, the conversation is live: the next message in it must not sit on a
    fresh wait. Only his posting HERE re-arms the delay, so him answering
    somewhere else can never hold up a conversation already under way."""
    _presence_load()
    _cid = str(chat_id or "")
    _at = _BIZ_REPLIED.get(_cid)
    _last = 0.0
    if not _at:
        # No stamp of our own: the store still knows when the last reply
        # went out, so a live thread stays live across reloads. It is the
        # reply stamp, NOT last_at remember_chat moves last_at for every
        # inbound message, which would call a chat engaged the moment its
        # customer says hello.
        if store is None:
            store = _biz_store()
        state = None
        if store is not None:
            try:
                state = store.chat_state(_cid)
            except Exception:
                logger.debug("[TGAhermes] chat state lookup failed", exc_info=True)
        _last = float((state or {}).get("replied_at") or 0.0)
        if _last > 0.0:
            _at = _last
            _BIZ_REPLIED[_cid] = _at
    _seen = _BIZ_OWNER_SEEN.get(_cid) or 0.0
    _out = bool(_at) and _at > _seen
    # He posted inside the wait window and an answer that was already
    # writing landed on top of his text: the thread is his, so the next
    # message still waits. Without this the in-flight reply outranks his
    # presence stamp and the chat never re-arms.
    if delay is None:
        delay = _biz_idle_delay_s(settings())
    if _out and _seen and delay and (time.time() - _seen) < float(delay):
        _out = False
    # One line per message: this is the decision that decides whether he
    # waits, and without its inputs a wrong hold can only be guessed at.
    logger.info(
        "[TGAhermes] Chat Automation engaged? chat=%s engaged=%s "
        "replied_at=%s owner_seen=%s store_replied_at=%s",
        _cid, _out, _at or 0.0, _BIZ_OWNER_SEEN.get(_cid) or 0.0, _last)
    return _out


def _active_bcid() -> str:
    """Connection id for deliveries when the incoming message omitted it.

    The business_connection update carries the id (the panel shows it), but
    incoming business messages don't always include business_connection_id.
    Without it the final reply falls through to the plain send path: it lands
    in the customer's DM with the bot (or 403s if they never started it)
    instead of appearing as the owner's message.
    """
    if _BIZ_ACTIVE_ID:
        return _BIZ_ACTIVE_ID
    store = _biz_store()
    if store is not None:
        try:
            for row in reversed(store.connections() or []):
                en = row.get("is_enabled")
                if en in (1, "1", "true", "True", True) and row.get("business_connection_id"):
                    return str(row["business_connection_id"])
        except Exception:
            logger.debug("[TGAhermes] connection lookup failed", exc_info=True)
    return ""  # none recorded yet


# ---------------------------------------------------------------- chat automation (bizauto)
# Telegram Secretary Mode: the owner's account holds a business connection to
# this bot, so customer DMs arrive as business_message updates and replies go
# out with business_connection_id (they appear as the owner's own messages).
# Everything below is best-effort: no connection -> no replies, never an
# exception in the handler path. All user-visible warn texts live in bizauto
# (Persian/Arabic from the local untracked JSON), keeping this file audit-clean.

_BIZ_MODE_ORDER = ("assistant", "mimic", "off")
_BIZ_THREAD_PREFIX = "bizauto:"
_BIZ_SCHEDULE_ORDER = ("always", "window", "off")
_BIZ_DAY_CODES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_BIZ_DAY_SET = set(_BIZ_DAY_CODES)


def _biz_sched_st(st: Optional[Dict[str, Any]] = None) -> str:
    """Master automation switch: always | window | off."""
    _s = st if isinstance(st, dict) else settings()
    v = str(_s.get("biz_schedule") or "always").strip().lower()
    return v if v in _BIZ_SCHEDULE_ORDER else "always"


# The home board labels a single line "automation", but biz_mode and
# biz_schedule are two different keys and only biz_schedule gates anything.
# Printing the mode alone made a paused schedule read as ON the reported
# "the panel says it's on, automation says it's off". The badge is the gate's
# own verdict, so the board can never disagree with what actually runs.
_BIZ_SCHED_BADGE = {"always": "✅ always", "window": "🗓 window", "off": "⏸ off"}


def _biz_sched_badge(st: Optional[Dict[str, Any]] = None) -> str:
    return _BIZ_SCHED_BADGE.get(_biz_sched_st(st), "⏸ off")


def _biz_norm_hhmm(raw: Any) -> str:
    """Any time the owner might type -> canonical 'HH:MM', or '' when unusable.

    Accepts '9:00', '09:05', '9', '0900' and '2359' so a window time can be
    entered as plainly as a number. Bad input never raises it returns '' so
    the caller rejects it instead of storing garbage.
    """
    s = str(raw or "").strip().replace(" ", "")
    if not s:
        return ""
    if ":" in s:
        hh, _, mm = s.partition(":")
    elif s.isdigit() and len(s) <= 4:
        if len(s) <= 2:
            hh, mm = s, "00"
        elif len(s) == 3:
            hh, mm = s[:1], s[1:]
        else:
            hh, mm = s[:2], s[2:]
    else:
        return ""
    if not (hh.isdigit() and mm.isdigit()):
        return ""
    h, m = int(hh), int(mm)
    if 0 <= h <= 24 and 0 <= m < 60:
        return f"{h:02d}:{m:02d}"
    return ""


def _biz_hhmm(value: Any, fallback: int) -> int:
    """'HH:MM' (or '0900' / '9') -> minutes since midnight. Never raises."""
    s = _biz_norm_hhmm(value)
    if not s:
        return fallback
    return min(24 * 60, int(s[:2]) * 60 + int(s[3:]))


# Durations: the owner types what people actually say '10m', '30s', '1h' 
# not raw seconds. Every entry point runs through _dur_to_s and rejects
# anything it cannot read, so a typo never reaches settings.json.
_DUR_UNITS: Dict[str, int] = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
}


def _dur_to_s(raw: Any, default_unit: str = "s") -> Optional[int]:
    """'10s' / '10m' / '1h30m' / '10' -> seconds. None = unparseable."""
    s = str(raw or "").strip().lower().replace(" ", "")
    if not s:
        return None
    parts = re.findall(r"(\d+(?:\.\d+)?)([a-z]*)", s)
    if not parts or "".join(n + u for n, u in parts) != s:
        return None
    total = 0.0
    for num, unit in parts:
        u = unit or default_unit
        if u not in _DUR_UNITS:
            return None
        total += float(num) * _DUR_UNITS[u]
    if total < 0:
        return None
    return int(round(total))


def _fmt_min(minutes: Any) -> str:
    """Minutes (maybe fractional, after typed input) -> '10m' / '30s'."""
    try:
        m = float(minutes)
    except Exception:
        return str(minutes)
    if m <= 0:
        return "0"
    if m < 1:
        return f"{m * 60:g}s"
    if m == int(m):
        return f"{int(m)}m"
    return f"{m:g}m"


def _biz_window_now(now: Optional[datetime.datetime] = None) -> int:
    return (now or datetime.datetime.now()).hour * 60 + (now or datetime.datetime.now()).minute


def _biz_day_code(raw: Any) -> str:
    """Resolve wizard input to a stored day code. Accepts any unique prefix so
    'f', 'fr' and 'Fri' all land on 'fri' requiring the full three letters
    silently dropped the day instead of setting it, which read as a dead button.
    Returns '' for 'all'/'clear'/'reset' and for anything unrecognised."""
    s = str(raw or "").strip().lower()
    if not s or s in ("all", "clear", "reset"):
        return ""
    hits = [c for c in _BIZ_DAY_SET if c.startswith(s)]
    return hits[0] if len(hits) == 1 else ""


def _biz_days_ok(raw: Any) -> bool:
    """Wizard input: 'all', 'clear', or a day name/prefix."""
    s = str(raw or "").strip().lower()
    if s in ("all", "clear", "reset"):
        return True
    return bool(_biz_day_code(s))


def _biz_days_patch(raw: Any) -> List[str]:
    """Toggle one day in biz_window_days; 'all'/'clear' empties it (= every day)."""
    day = _biz_day_code(raw)
    cur = [str(d).strip().lower()[:3] for d in (settings().get("biz_window_days") or [])]
    if not day:
        return []
    return [d for d in cur if d != day] + [day]


def _biz_in_window(st: Optional[Dict[str, Any]] = None,
                   now: Optional[datetime.datetime] = None) -> bool:
    """True when the current local time is inside the owner's window.

    Handles a window that wraps past midnight (e.g. 23:00 -> 07:00) and a
    day-of-week filter; an empty day list means every day.
    """
    _s = st if isinstance(st, dict) else settings()
    n = now or datetime.datetime.now()
    days = {str(d).strip().lower()[:3] for d in (_s.get("biz_window_days") or [])}
    if days and _BIZ_DAY_CODES[n.weekday()] not in days:
        return False
    start = _biz_hhmm(_s.get("biz_window_start"), 0)
    end = _biz_hhmm(_s.get("biz_window_end"), 24 * 60 - 1)
    if start == end:
        return True
    cur = n.hour * 60 + n.minute
    if start < end:
        return start <= cur <= end
    return cur >= start or cur <= end   # wraps midnight


def _biz_should_answer(st: Optional[Dict[str, Any]] = None,
                       now: Optional[datetime.datetime] = None) -> Tuple[bool, str]:
    """Master gate for the automation path. Returns (answer_now, reason).

    This is the switch the owner asked for: automation fully off, always on,
    or only inside a time window evaluated before any brain work so a closed
    window costs nothing and says nothing to the customer.
    """
    _s = st if isinstance(st, dict) else settings()
    sched = _biz_sched_st(_s)
    if sched == "off":
        return False, "automation is switched off"
    if sched == "window":
        # Split the day filter from the clock check: both land in the log line,
        # and "outside the configured window" on a filtered weekday sends you
        # hunting the hours instead of the days list.
        n = now or datetime.datetime.now()
        days = {str(d).strip().lower()[:3] for d in (_s.get("biz_window_days") or [])}
        if days and _BIZ_DAY_CODES[n.weekday()] not in days:
            return False, f"today ({_BIZ_DAY_CODES[n.weekday()]}) is not in the allowed days"
        if not _biz_in_window(_s, now):
            return False, "outside the configured window"
    return True, "in schedule"


def _biz_idle_delay_s(st: Optional[Dict[str, Any]] = None) -> float:
    """Seconds every automation message is held before ATRA may answer.

    0 means answer immediately. Plain wait with no conditions: the hold is
    cancelled only by the owner replying in that chat during the window.
    """
    _s = st if isinstance(st, dict) else settings()
    try:
        return max(0.0, float(_s.get("biz_idle_delay_min") or 0) * 60.0)
    except Exception:
        return 0.0


def _biz_db_path() -> Path:
    _cand = PLUGIN_DIR / "bizauto.db"
    try:
        _cand.touch()
        return _cand
    except Exception:
        pass
    _fb = _hermes_home() / "cache" / "bizauto.db"
    try:
        _fb.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        logger.debug("[TGAhermes] bizauto fallback dir creation failed", exc_info=True)
    return _fb


_BIZ_STORE: Any = None


def _biz_store() -> Any:
    """Lazy SQLite store; None when bizauto or the path is unavailable."""
    global _BIZ_STORE
    if _BIZ_STORE is not None:
        return _BIZ_STORE
    if bizauto is None:
        return None
    try:
        _BIZ_STORE = bizauto.biz_store(_biz_db_path())
    except Exception:
        logger.warning("[TGAhermes] chat automation store unavailable", exc_info=True)
        _BIZ_STORE = False
    return _BIZ_STORE if _BIZ_STORE else None


def _biz_denied(st: Dict[str, Any]) -> set:
    """Tools locked for automation chats: the dangerous set, plus owner extras."""
    extra = {str(t).strip() for t in (st.get("biz_deny_tools") or []) if str(t).strip()}
    return set(GUEST_BLOCKED_TOOLS) | set(GUEST_NEVER_TOOLS) | extra


def _biz_refusal(name: str, repeats: int = 0) -> str:
    base = (f"🔒 <b>{_esc(name)}</b> is locked in automation chats this conversation "
            "runs with the customer-safe tool set. Ask the owner directly for anything else.")
    if repeats:
        base += f" <i>(blocked {repeats + 1}&#215;)</i>"
    return base


def _biz_chat_known(chat_id: Any) -> bool:
    store = _biz_store()
    if store is None or not chat_id:
        return False
    try:
        return store.chat_state(chat_id) is not None
    except Exception:
        return False


# Last admitted event kind per chat. A chat id alone must never decide delivery:
# a friend's business chat and their plain DM with the bot share the same
# numeric id, and "has business history" was injecting every plain bot reply
# (and its typing/clarify/media) into the owner's personal DM as if the owner
# had written it. Business turns mark themselves; every other admitted event
# marks plain; send_final_ledgered re-marks authoritatively at reply time.
_BIZ_CTX: Dict[str, str] = {}

def _biz_mark(chat_id: Any, kind: str) -> None:
    if not chat_id:
        return
    if len(_BIZ_CTX) > 400:
        _BIZ_CTX.clear()
    _BIZ_CTX[str(chat_id)] = kind


def _biz_wants_owner(chat_id: Any) -> bool:
    """True when a send into this chat should go out as the owner (business).

    A known business chat whose current turn is plain must be delivered by
    the bot. Before any event is seen (boot sweep / ledger redelivery) keep
    the old chat-history behaviour so recovery still lands as the owner."""
    ctx = _BIZ_CTX.get(str(chat_id or ""))
    if ctx:
        return ctx == "business"
    return _biz_chat_known(chat_id)


def _biz_safe_thread(tid: Any) -> Any:
    """A thread id is only usable as ``message_thread_id`` when it is a real
    message id. The ``bizauto:<chat>`` session-split marker is not one, and
    int() on it crashed every queued-lane final for automation chats."""
    if tid is None or tid == "":
        return None
    try:
        int(tid)
    except (TypeError, ValueError):
        return None
    return tid


def _biz_safe_metadata(metadata: Any) -> Any:
    """Metadata without non-numeric thread ids (see _biz_safe_thread).
    Returns the input untouched when there is nothing to drop."""
    if not isinstance(metadata, dict):
        return metadata
    drop = [k for k in ("thread_id", "message_thread_id")
            if k in metadata and _biz_safe_thread(metadata.get(k)) is None]
    if not drop:
        return metadata
    return {k: v for k, v in metadata.items() if k not in drop}


def _biz_list_patch(key: str, raw: str) -> list:
    """Wizard input -> updated list: 'all' clears, '-x' removes, 'x' adds."""
    cur = [str(x) for x in (settings().get(key) or [])]
    v = str(raw or "").strip()
    if v.lower() in ("all", "clear", "reset", "*"):
        return []
    if v.startswith("-"):
        t = v[1:].strip()
        return [x for x in cur if x != t]
    if v and v not in cur:
        cur.append(v)
    return cur


def _bcid_candidates(chat_id: Any, preferred: str = "") -> List[str]:
    """Ordered connection ids to try for one delivery: the preferred (event
    or per-chat) id first, then the map, then the live connection, then the
    newest ENABLED row on record. Deduplicated, empties dropped a reconnect
    rotates the id and a cached stale one is rejected with
    Business_connection_invalid."""
    out: List[str] = []
    seen: set = set()

    def _add(x: Any) -> None:
        v = str(x or "").strip()
        if v and v not in seen:
            seen.add(v)
            out.append(v)

    _add(preferred)
    _add(_BIZ_CONN.get(str(chat_id or ""), ""))
    _add(_active_bcid())
    store = _biz_store()
    if store is not None:
        try:
            for c in (store.connections() or [])[::-1]:
                if str(c.get("is_enabled") or "") == "true":
                    _add(c.get("business_connection_id"))
                    break
        except Exception:
            logger.debug("[TGAhermes] connection lookup failed", exc_info=True)
    return out


def _bcid_retriable(err: Any) -> bool:
    """The connection id was rejected a different candidate may still work."""
    return "Business_connection_invalid" in str(err)


async def _biz_try_send(adapter: Any, chat_id: Any, text: str,
                        preferred: str = "") -> "tuple[bool, Optional[Any]]":
    """Send over the business connection, rotating to a live connection id
    when Telegram rejects the one we had. Returns (delivered, sent): a failed
    send must never raise delivery callers treat False as a refusal."""
    if not chat_id or not text:
        return False, None
    cands = _bcid_candidates(chat_id, preferred)
    if not cands:
        logger.warning("[TGAhermes] automation reply dropped, no business "
                       "connection (chat=%s)", chat_id)
        return False, None
    last_err: Any = None
    for i, bcid in enumerate(cands):
        try:
            sent = await adapter._bot.send_message(
                chat_id=chat_id, text=str(text)[:4000],
                business_connection_id=bcid)
            # A message that actually went out spends the entry grace period.
            _BIZ_REPLIED[str(chat_id)] = time.time()
            _presence_write()
            # Remember the id that actually worked (the cached one may be stale).
            if _BIZ_CONN.get(str(chat_id)) != bcid:
                _BIZ_CONN[str(chat_id)] = bcid
            return True, sent
        except Exception as e:
            last_err = e
            if _bcid_retriable(e) and i < len(cands) - 1:
                logger.info("[TGAhermes] connection rejected, trying the next "
                            "one (chat=%s): %s", chat_id, e)
                continue
            break
    logger.warning("[TGAhermes] automation send failed (chat=%s): %s",
                   chat_id, last_err)
    return False, None


async def _biz_send(adapter: Any, chat_id: Any, text: str) -> bool:
    """Best-effort text delivery over the business connection."""
    ok, _ = await _biz_try_send(adapter, chat_id, text)
    return ok


async def _biz_deliver(adapter: Any, event: Any, bcid: str, text_content: Any) -> Any:
    """Final reply of an automation turn: deliver via the business connection
    (it appears as the owner's message) and drop the configured reaction on
    it when enabled. Connection ids rotate on reconnect, so delivery goes
    through _biz_try_send's candidate rotation instead of one cached id."""
    from gateway.platforms.base import SendResult
    md = getattr(event, "metadata", None) or {}
    src = getattr(event, "source", None)
    chat_id = str(md.get("business_chat_id") or getattr(src, "chat_id", "") or "")
    try:
        await adapter._release_turn_marker(event)
    except Exception:
        logger.debug("[TGAhermes] turn marker release failed", exc_info=True)
    ok = False
    _seen_at = _BIZ_OWNER_SEEN.get(chat_id) or 0.0
    _turn_at = _BIZ_TURN.get(chat_id) or 0.0
    if chat_id and _turn_at and _seen_at >= _turn_at:
        # He posted while this reply was being written: the thread is his
        # now, so the generated text goes nowhere.
        logger.info("[TGAhermes] Chat Automation drop (owner took the thread) "
                    "chat=%s", chat_id)
        try:
            await _log("🤖 Chat Automation stood down",
                       f"chat <code>{_esc(chat_id)}</code>\n"
                       f"<b>Action:</b> you replied while it was writing, "
                       f"the reply was dropped")
        except Exception:
            logger.debug("[TGAhermes] drop log failed", exc_info=True)
        return SendResult(success=False, message_id=None), adapter
    if chat_id and text_content and str(text_content).strip():
        ok, sent = await _biz_try_send(adapter, chat_id, str(text_content),
                                       preferred=str(bcid or ""))
        if ok:
            mid = getattr(sent, "message_id", None)
            st0 = settings()
            if mid and st0.get("biz_react"):
                _spawn(_react(chat_id, mid, st0.get("biz_react_emoji") or "\U0001f47e"))
    logger.info("[TGAhermes] reply delivered=%s chat=%s", bool(ok), chat_id)
    if not ok:
        _spawn(_session_alert("deliver",
                              f"reply not delivered to chat "
                              f"<code>{_esc(str(chat_id))}</code>"))
    store = _biz_store()
    if store is not None and chat_id:
        try:
            store.mark_replied(chat_id, "ok" if ok else "fail")
        except Exception:
            logger.debug("[TGAhermes] mark_replied failed", exc_info=True)
        # The introduction is only spent once it was actually delivered.
        # A dropped, failed, or owner-taken turn leaves the flag set so the
        # next delivered reply still opens the chat properly.
        pending = md.get("biz_warn_pending")
        if ok and pending:
            try:
                store.mark_warned(chat_id, str(pending))
            except Exception:
                logger.debug("[TGAhermes] mark_warned failed", exc_info=True)
    return SendResult(success=ok, message_id=None), adapter


# Bundled personas, with a home copy overriding the packaged one (same rule
# as the guest persona) so the owner can edit one without redeploying.
_BIZ_PERSONAS = {"mimic": "mimic.md", "assistant": "automation.md"}

_BIZ_MIMIC_FALLBACK = (
    "You are Artan's Telegram account answering a customer directly. "
    "Write AS him: first person, his voice concise, casual, practical, "
    "no corporate tone, no emoji spam. Only facts you actually have; if you "
    "don't know, say so in one line. Nothing private, nothing internal no "
    "settings, logs, ids, other chats, or how you run. Never claim to be an "
    "AI unless asked outright; if asked, say an assistant wrote it on his "
    "behalf, once, then move on. No jokes, no emoji."
)
_BIZ_ASSISTANT_FALLBACK = (
    "You are ATRA, replying to messages that arrive in this Telegram "
    "account. Answer the question or do the task, in their language, in the "
    "fewest clear words. Only what you actually know if you don't know, "
    "one line saying so. Never invent facts or promises on his behalf. Never "
    "expose settings, logs, paths, ids, other chats, or how you run. No "
    "filler, no flattery, no jokes, no emoji."
)
# Kept as the module-level name tests and callers already reference.
_BIZ_MIMIC_PROMPT = _BIZ_MIMIC_FALLBACK


def _biz_persona_file(kind: str) -> Optional[Path]:
    """Bundled persona file for a mode; a home copy wins when it differs."""
    name = _BIZ_PERSONAS.get(kind)
    if not name:
        return None
    bundled = PLUGIN_DIR / "assets" / name
    try:
        home_copy = _hermes_home() / "assets" / name
        if home_copy.is_file() and bundled.is_file():
            if home_copy.read_bytes() != bundled.read_bytes():
                return home_copy
        elif home_copy.is_file():
            return home_copy
    except OSError:
        pass
    return bundled if bundled.is_file() else None


def _load_biz_persona(kind: str) -> str:
    """Bundled persona text for a mode, or its inline fallback."""
    path = _biz_persona_file(kind)
    if path:
        try:
            txt = path.read_text(encoding="utf-8", errors="replace").strip()
            if txt:
                return txt
        except OSError:
            logger.warning("[TGAhermes] automation persona unreadable: %s", path)
    return _BIZ_ASSISTANT_FALLBACK if kind == "assistant" else _BIZ_MIMIC_FALLBACK


# "" is deliberately NOT in here: an empty answer is a mistyped one, and the
# owner should be told so instead of having their override silently cleared.
_BIZ_PERSONA_RESET = ("default", "reset", "auto")


def _biz_persona_arg_ok(data: Any) -> str:
    """Wizard guard for the persona path.

    It used to accept any non-empty string, so a normal chat message sent
    while the wizard was open was silently stored as the path the setting
    then pointed at nothing and every automation turn logged a read warning.
    A real path must exist; anything else is rejected with the fix spelled
    out, and the owner can still clear it with 'default'.
    """
    raw = str((data or [""])[0] or "").strip()
    if raw.lower() in _BIZ_PERSONA_RESET:
        return ""
    if not raw:
        return "send a path, or 'default'"
    if not Path(os.path.expanduser(raw)).is_file():
        return f"no file at that path send an existing file, or 'default'"
    return ""


def _biz_persona(st: Dict[str, Any], mode: str) -> str:
    """The persona an automation turn is prefixed with.

    An explicit `biz_persona_path` wins for either mode (the owner pointing
    at one file means "use this voice"). Otherwise each mode gets its own
    bundled persona mimic speaks as him, assistant speaks as ATRA. The old
    fallback for assistant was the GUEST persona, which told customers they
    were in a guest session with limited tools: wrong audience, and it leaked
    internal framing straight into a customer chat.
    """
    p = str(st.get("biz_persona_path") or "").strip()
    if p:
        try:
            txt = Path(p).read_text(encoding="utf-8", errors="replace")[:8000]
            if txt.strip():
                return txt
        except Exception:
            logger.warning("[TGAhermes] automation persona unreadable: %s", p)
    if mode == "mimic":
        return _load_biz_persona("mimic")
    return _load_biz_persona("assistant")


def _biz_inline_text(event: Any, cached: Any) -> None:
    """Inline a small text attachment into the turn, the way the adapter's own
    document path does, so a receipt or note does not cost an extra read."""
    try:
        from gateway.platforms.base import _TEXT_INJECT_EXTENSIONS
        if os.path.splitext(cached.path)[1].lower() not in _TEXT_INJECT_EXTENSIONS:
            return
        if os.path.getsize(cached.path) > 100 * 1024:
            return
        body = Path(cached.path).read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return  # binary or non-UTF-8: the agent keeps the cached path
    except Exception:
        logger.debug("[TGAhermes] business text inline failed", exc_info=True)
        return
    injection = f"[Content of {cached.display_name}]:\n{body}"
    event.text = f"{injection}\n\n{event.text}" if event.text else injection
    event.media_text_inlined = [True]


async def _biz_attach_media(adapter: Any, msg: Any, event: Any) -> None:
    """Cache a business message's attachment and attach it to ``event``.

    Chat Automation builds its own event and hands it straight to
    ``handle_message``, so the adapter's inbound media pipeline, which only runs
    for ordinary private/group updates, never sees these messages. A customer's
    photo or file therefore reached the agent as a bare ``[photo]``/``[document]``
    placeholder and the bytes were dropped. This reuses the adapter's download
    helper (no dispatch side effects) so the file lands on the event; a failed
    download degrades to a note telling the agent to ask for a re-send instead
    of silently delivering nothing.
    """
    try:
        status, cached = await adapter._download_observed_media(msg, "business media")
    except Exception:
        logger.debug("[TGAhermes] business media download failed", exc_info=True)
        return
    if status == "ok" and cached is not None:
        adapter._attach_cached(event, cached, cached.context_note(),
                               "[TGAhermes] Cached business %s at %s")
        event.media_text_inlined = [False]
        if cached.kind == "document":
            _biz_inline_text(event, cached)
        return
    if status == "oversized":
        limit_mb = int(getattr(adapter, "_max_doc_bytes", 20 * 1024 * 1024) // (1024 * 1024))
        note = f"[Attachment too large to cache ({limit_mb} MB maximum). Ask for a smaller file.]"
    elif status in ("failed", "unreadable"):
        note = "[Attachment could not be downloaded. Ask the sender to re-send it.]"
    else:
        return
    event.text = adapter._append_observed_note(event.text, note)


async def _handle_business_message(adapter: Any, update: Any, context: Any = None,
                                   edited: bool = False) -> None:
    """One business message: log it under its OWN title (never the Stranger-DM
    log), send the first-contact warning once per chat, then hand the turn to
    the brain with the reply routed back over the business connection."""
    msg = (getattr(update, "edited_business_message", None) if edited
           else getattr(update, "business_message", None))
    if msg is None:
        return
    user = getattr(msg, "from_user", None)
    if user is not None and getattr(user, "is_bot", False):
        return  # bots never trigger a reply
    uid = str(getattr(user, "id", "") or "")
    owner = _owner_id(adapter)
    chat_id = str(getattr(getattr(msg, "chat", None), "id", "") or "")
    if uid and owner and owner not in ("", "*") and uid == owner:
        # The owner is back in THIS conversation that is what re-arms the
        # entry hold. Recorded here because an owner turn never reaches the
        # gateway dispatch hook: it is filtered out just below.
        if chat_id:
            # Wall-clock like every other stamp: a monotonic value is a few
            # seconds since boot, and the hold compares it against an epoch
            # it would read as "owner never spoke" forever.
            _BIZ_OWNER_SEEN[chat_id] = time.time()
            _presence_write()
            logger.info("[TGAhermes] owner presence chat=%s (his own reply)",
                        chat_id)
        return  # the owner's own traffic is not automation
    st = settings()
    mode = str(st.get("biz_mode") or "assistant")
    bcid = str(getattr(msg, "business_connection_id", "") or "")
    if not bcid:
        # Incoming business updates may omit the field; the attached
        # connection (recorded by _on_business_connection) is the same one.
        bcid = _active_bcid()
    text = str(getattr(msg, "text", "") or getattr(msg, "caption", "") or "")
    if not chat_id:
        return
    scope = [str(c) for c in (st.get("biz_scope") or [])]
    if scope and chat_id not in scope:
        return  # out of scope: not answered, not logged
    if bcid:
        _BIZ_CONN[chat_id] = bcid
    cfg_lang = str(st.get("biz_lang") or "auto")
    detect = bizauto.detect_language(text) if (bizauto and text) else "en"
    store = _biz_store()
    if cfg_lang in ("", "auto") and not any(ch.isalpha() for ch in text):
        # A bare number or an emoji carries no language of its own and
        # detect_language() returns "en" for it so an order number from a
        # Persian customer used to declare "English" twice over: the canned
        # first-contact warning went out in English, and the identity line
        # told ATRA to mirror an English sender. Fall back to what this chat
        # has actually spoken before; only a brand-new chat guesses.
        known = ""
        if store is not None:
            try:
                known = str((store.chat_state(chat_id) or {}).get("first_lang") or "")
            except Exception:
                known = ""
        if known:
            detect = known
    lang = detect if cfg_lang in ("", "auto") else cfg_lang
    lang_label = bizauto.language_label(lang) if bizauto else lang
    if store is not None:
        try:
            store.remember_chat(chat_id, detect)
        except Exception:
            logger.debug("[TGAhermes] remember_chat failed", exc_info=True)
    msg_id = str(getattr(msg, "message_id", "") or "") or None
    prof = _profile_buttons(user, chat_id, msg_id)
    if edited or mode == "off":
        action = "edited log only" if edited else "observed (mode off)"
        await _log("🤖 Chat Automation " + ("edit" if edited else "message"),
                   f"{_user_block(user)}\n<b>Chat:</b> <code>{_esc(chat_id)}</code> "
                   f"(automation)\n<b>Language:</b> {_esc(lang_label)}"
                   f"\n<b>Text:</b> <i>{_esc(text[:500])}</i>"
                   f"\n<b>Action:</b> {_esc(action)}", buttons=prof)
        return
    # --- master schedule switch: closed = silent, zero brain cost ----------
    _in_sched, _why = _biz_should_answer(st)
    if not _in_sched:
        await _log("🤖 Chat Automation held back",
                   f"{_user_block(user)}\n<b>Chat:</b> <code>{_esc(chat_id)}</code>"
                   f"\n<b>Text:</b> <i>{_esc(text[:300])}</i>"
                   f"\n<b>Action:</b> {_esc(_why)} nothing sent", buttons=prof)
        return
    # --- plain hold -----------------------------------------------------
    # A message waits only while the conversation is NOT already live: a new
    # contact, or a thread the owner took back. If ATRA has answered and he
    # has not posted here since, the chat is engaged and there is nothing to
    # wait for answer it now. Within a wait, his reply in THIS chat cancels
    # it; his traffic anywhere else never does.
    _delay = _biz_idle_delay_s(st)
    _t0 = time.time()
    if _delay > 0 and _biz_engaged(chat_id, store, _delay):
        logger.info("[TGAhermes] Chat Automation skip hold: engaged chat=%s",
                    chat_id)
        _delay = 0.0
    if _delay > 0:
        # Logged to the gateway too: this branch is the one thing that decides
        # whether ATRA waits, and the channel log alone can't show it afterwards.
        logger.info("[TGAhermes] Chat Automation hold %.0fs chat=%s", _delay, chat_id)
        try:
            await _log("🤖 Chat Automation waiting for owner",
                       f"{_user_block(user)}\n<b>Chat:</b> <code>{_esc(chat_id)}</code>"
                       f"\n<b>Holding:</b> {_delay / 60.0:.1f} min "
                       f"<i>(reply in this chat and I stay out)</i>", buttons=prof)
        except Exception:
            logger.debug("[TGAhermes] hold log failed", exc_info=True)
        # Poll instead of one long sleep: his reply inside the window has
        # to cancel the wait within seconds, not at the end of it.
        _until = _t0 + _delay
        while True:
            _rem = _until - time.time()
            if _rem <= 0:
                break
            await asyncio.sleep(min(2.0, _rem))
            if (_BIZ_OWNER_SEEN.get(str(chat_id)) or 0.0) >= _t0:
                logger.info("[TGAhermes] Chat Automation stand-down chat=%s "
                            "after %.0fs", chat_id, time.time() - _t0)
                try:
                    await _log("🤖 Chat Automation owner answered",
                               f"{_user_block(user)}\n<b>Chat:</b> <code>{_esc(chat_id)}</code>"
                               f"\n<b>Action:</b> you replied inside the window "
                               f"ATRA stood down", buttons=prof)
                except Exception:
                    logger.debug("[TGAhermes] stand-down log failed", exc_info=True)
                return
    # Dispatch time for this chat: a reply generated after this must not be
    # delivered if he posts before it comes back.
    _BIZ_TURN[str(chat_id)] = time.time()
    # --- first-contact warning: once per chat, before ATRA's first reply ----
    # His own text, when he wrote one, goes out verbatim that is his call to
    # make. When he left the field empty the old path still fired the canned
    # catalog in a guessed language. Now the empty field means ATRA writes the
    # line itself, inside its first reply: right language, right register, and
    # shaped around what the customer actually sent.
    warned = False
    if store is not None:
        try:
            warned = bool((store.chat_state(chat_id) or {}).get("warned_at"))
        except Exception:
            warned = False
    warn_draft = False
    if st.get("biz_warn_first") and not warned:
        owner_warn = str(st.get("biz_warn_text") or "").strip()
        if owner_warn:
            sent_warn = False
            if bcid:
                try:
                    await adapter._bot.send_message(chat_id=chat_id, text=owner_warn[:4000],
                                                    business_connection_id=bcid)
                    sent_warn = True
                except Exception:
                    logger.warning("[TGAhermes] first-contact warning failed", exc_info=True)
            if sent_warn and store is not None:
                try:
                    store.mark_warned(chat_id, lang)
                except Exception:
                    logger.debug("[TGAhermes] mark_warned failed", exc_info=True)
        else:
            # Do NOT stamp warned yet: the intro line only counts once it
            # actually reaches the customer. Stamping here meant a dropped,
            # failed, or owner-taken turn burned the only introduction this
            # chat would ever get, so later sessions never opened with one.
            # _biz_deliver stamps it on a successful send.
            warn_draft = True
    # --- build the event the same way the guest path does -------------------
    try:
        from gateway.platforms.event import MessageType
        msg_type = MessageType.TEXT
        if st.get("biz_media"):
            _has_media = any(getattr(msg, _a, None) for _a in
                             ("photo", "video", "animation", "sticker",
                              "document", "audio", "voice", "video_note"))
            if _has_media:
                msg_type = adapter._media_message_type(msg)
        event = adapter._build_message_event(
            msg, msg_type, update_id=getattr(update, "update_id", None))
    except Exception:
        logger.exception("[TGAhermes] failed to build automation event")
        try:
            await _log("Automation error (silent)",
                       f"chat <code>{_esc(chat_id)}</code> · build failed")
        except Exception:
            logger.debug("[TGAhermes] automation build-failure log failed",
                         exc_info=True)
        return
    event.text = adapter._clean_bot_trigger_text(event.text or "") or ""
    if msg_type is not MessageType.TEXT:
        # A media failure must never cost the customer their message: the
        # turn goes ahead with whatever text there was.
        try:
            await _biz_attach_media(adapter, msg, event)
        except Exception:
            logger.warning("[TGAhermes] business media attach failed", exc_info=True)
    if msg_type is not MessageType.TEXT and not event.text:
        _kind = str(getattr(msg_type, "name", "") or "media").lower()
        event.text = "[" + _kind + "]"
    persona = _biz_persona(st, mode)
    first = str(getattr(user, "first_name", "") or "")
    last = str(getattr(user, "last_name", "") or "")
    uname = str(getattr(user, "username", "") or "")
    user_name = (first + " " + last).strip() or (f"@{uname}" if uname else "customer")
    lang_line = ("mirror the sender's language (detected: %s)" % lang_label
                 if cfg_lang in ("", "auto") else "always reply in %s" % lang_label)
    mode_line = {
        "mimic": ("Mode: MIMIC you ARE the owner writing his own reply: first "
                  "person, his voice, short and casual. Never mention automation, "
                  "AI, a script, or the words 'as an AI'; if asked point-blank, an "
                  "assistant wrote it on his behalf once, then move on."),
        "assistant": ("Mode: ASSISTANT you reply as ATRA, the owner's AI "
                      "assistant. Answer what was asked and nothing more; never "
                      "expose settings, logs, other chats, or how you run."),
        "off": "Mode: OFF observation only.",
    }.get(mode, "")
    identity = (
        "\n\n---\n"
        "Chat Automation turn customer DM reaching the owner's Telegram account.\n"
        f"Sender: {user_name} ({uid or 'unknown'}) · chat {chat_id} · language {lang_line}\n"
        f"{mode_line}\n"
        "Reply directly to their message; no commands, no panel talk."
    )
    if warn_draft:
        identity += (
            "\nFirst reply to this chat: fold in ONE short line telling them "
            "an assistant is writing this on his behalf and he'll come back to "
            "it himself. Never give his name or who he is; assistant on his "
            "behalf is all they get. Write it yourself, in their language and "
            "shaped around "
            "what they actually said never a stock sentence. One line, then "
            "the answer; if they didn't need telling, keep it to a clause."
        )
    if st.get("biz_larp"):
        identity += ("\nStyle: read the owner's own messages in the history above "
                     "and match how he writes vocabulary, sentence length, "
                     "rhythm, punctuation. Style only: never copy his facts or "
                     "commitments into a new context.")
    _note = _LAST_REACTION.pop(chat_id, None)
    if _note:
        identity += (f"\nLatest reaction update in this chat: {_note[0]} on message {_note[1]} "
                     "(treat as recent context).")
    event.channel_prompt = f"{persona}{identity}"
    md = event.metadata
    if isinstance(md, dict):
        if "chat_id" in md:
            md["chat_id"] = event.source.chat_id
        md["business_connection_id"] = bcid
        md["business_chat_id"] = chat_id
        md["biz_lang"] = lang
        md["biz_mode"] = mode
        if warn_draft:
            md["biz_warn_pending"] = lang
    elif warn_draft:
        # No metadata dict to carry the pending flag through: fall back to
        # the old stamp-now behaviour rather than never recording it.
        if store is not None:
            try:
                store.mark_warned(chat_id, lang)
            except Exception:
                logger.debug("[TGAhermes] mark_warned failed", exc_info=True)
    _biz_mark(chat_id, "business")
    if bool(st.get("biz_sessions_split", True)):
        # Automation account-direct chats get their OWN session, separate from
        # the whitelisted DM with the same person: same human, different hat.
        event.source.thread_id = f"bizauto:{chat_id}"
    try:
        event.source.user_id = uid or event.source.user_id
    except Exception:
        logger.debug("[TGAhermes] could not set source.user_id", exc_info=True)
    if hasattr(event.source, "chat_name") and user_name:
        event.source.chat_name = user_name
    if hasattr(event.source, "user_name"):
        event.source.user_name = user_name
    event.allow_gateway_control = False
    event.internal = True
    _record_user(user, started=text.lstrip().lower().startswith("/start"), sample=text)
    await _log("🤖 Chat Automation message",
               f"{_user_block(user)}\n<b>Chat:</b> <code>{_esc(chat_id)}</code> "
               f"(automation)\n<b>Language:</b> {_esc(lang_label)} · "
               f"<b>Mode:</b> {_esc(mode)}"
               f"\n<b>Text:</b> <i>{_esc(text[:500])}</i>"
               f"\n<b>Action:</b> handed to the brain", buttons=prof)
    if getattr(adapter, "_message_handler", None) is None:
        logger.warning("[TGAhermes] automation turn received but no message handler installed")
        return
    await adapter.handle_message(event)


async def _on_business_connection(adapter: Any, update: Any) -> None:
    """Record the connection + granted rights so the panel can show them."""
    bc = getattr(update, "business_connection", None)
    if bc is None:
        return
    bcid = str(getattr(bc, "id", "") or "")
    rights = getattr(bc, "rights", None)
    rd: Dict[str, bool] = {}
    for f in ("can_reply", "can_read_messages", "can_delete_sent_messages",
              "can_delete_all_messages", "can_edit_name", "can_edit_bio",
              "can_edit_username", "can_send_payments"):
        rd[f] = bool(getattr(rights, f, False)) if rights is not None else False
    enabled = bool(getattr(bc, "is_enabled", False))
    global _BIZ_ACTIVE_ID
    if bcid and enabled:
        _BIZ_ACTIVE_ID = bcid
    elif bcid and not enabled and _BIZ_ACTIVE_ID == bcid:
        _BIZ_ACTIVE_ID = ""
    store = _biz_store()
    if store is not None and bcid:
        try:
            store.record_connection({
                "id": bcid,
                "user": str(getattr(getattr(bc, "user", None), "id", "") or ""),
                "user_chat_id": str(getattr(bc, "user_chat_id", "") or ""),
                "rights": rd,
                "is_enabled": str(enabled).lower(),
            })
        except Exception:
            logger.warning("[TGAhermes] business connection record failed", exc_info=True)
    granted = ", ".join(k for k, v in rd.items() if v) or "none"
    await _log("🔌 Chat Automation connection",
               f"<b>Status:</b> {'enabled' if enabled else 'disabled'}\n"
               f"<b>Connection:</b> <code>{_esc(bcid)}</code>\n"
               f"<b>Granted:</b> {_esc(granted)}\n"
               "<i>Attach or edit in Telegram → Settings → Chat Automation.</i>")


def _bizlang_view(st: Dict[str, Any], note: str = "") -> str:
    cur = str(st.get("biz_lang") or "auto")
    label = bizauto.language_label(cur) if bizauto else cur
    warn = str(st.get("biz_warn_text") or "").strip()
    lines = ["<b>🌐 Automation language</b>",
             f"current: <b>{_esc(label)}</b> <code>({_esc(cur)})</code>"]
    if note:
        lines.append(note)
    lines += ["",
              "auto = every message's language is detected (Persian included); "
              "a fixed language pins the warning and the replies.",
              f"warning text: <i>{_esc(warn[:120]) if warn else 'ATRA writes it, in their language'}</i>",
              "",
              "Tap a language to confirm it."]
    return "\n".join(lines)


def _bizconn_view(st: Dict[str, Any], note: str = "") -> str:
    lines = ["<b>🔌 Chat Automation connection</b>"]
    if note:
        lines.append(note)
    store = _biz_store()
    conns: List[Dict[str, Any]] = []
    if store is not None:
        try:
            conns = store.connections() or []
        except Exception:
            conns = []
    if not conns:
        lines += ["", "No connection recorded yet.",
                  "Telegram → Settings → Chat Automation → connect this bot, "
                  "then come back the granted rights show up here."]
    for c in conns[:6]:
        try:
            rd = json.loads(str(c.get("rights_json") or "{}"))
        except Exception:
            rd = {}
        granted = ", ".join(k for k, v in (rd or {}).items() if v) or "none"
        lines += [f"• <code>{_esc(str(c.get('business_connection_id') or ''))}</code> "
                  f"{'✅ enabled' if str(c.get('is_enabled')) == 'true' else '⏸ disabled'}",
                  f"  rights: {_esc(granted[:200])}"]
    stats: Dict[str, int] = {}
    if store is not None:
        try:
            stats = store.stats() or {}
        except Exception:
            stats = {}
    if stats:
        lines += ["", f"chats seen: <b>{_esc(str(stats.get('chats', 0)))}</b> · "
                      f"warned: <b>{_esc(str(stats.get('warned', 0)))}</b> · "
                      f"replies: <b>{_esc(str(stats.get('replies', 0)))}</b>"]
    lines += ["", "Replies go out inside the 24h window after the customer's last "
                  "message and appear as your own messages."]
    return "\n".join(lines)


_STATUS_PREFIXES = ("⏳ Working", "⚡ Interrupting", "⏳ Queued",
                    "⏩ Steered", "💾", "⚙")


def _is_status_bubble(text: str, metadata: Optional[Dict[str, Any]] = None) -> bool:
    """Gateway chatter (heartbeats, steer acks, tool progress) that must
    never reach a customer talking with the owner: only ATRA's real reply
    lands there. Metadata marks every mid-turn send; the prefix list is the
    fallback for status text sent without it."""
    md = metadata or {}
    if md.get("_interim_send") or md.get("non_conversational"):
        return True
    return (text or "").lstrip().startswith(_STATUS_PREFIXES)


def _install_wraps(adapter: Any) -> None:
    # Re-run on EVERY factory load (no early return): these closures must
    # belong to the CURRENT module instance. A wrap left over from an older
    # load still calls that load's _biz_send/_BIZ_CONN - state nobody fills
    # anymore - so _biz_send returns False and the gateway's plain-text
    # fallback sends the reply as the bot (bot DM, or 403 for strangers).
    # Originals are unwrapped from the previous install's closure, so
    # re-installing rebinds instead of stacking wraps.
    import inspect as _inspect

    def _true(fn: Any, *names: str) -> Any:
        for _ in range(8):
            if fn is None or not callable(fn):
                return fn
            try:
                nl = _inspect.getclosurevars(fn).nonlocals
            except Exception:
                return fn
            nxt = None
            for nm in names:
                v = nl.get(nm)
                if callable(v):
                    nxt = v
                    break
            if nxt is None:
                return fn
            fn = nxt
        return fn

    adapter._guest_wraps_installed = True
    from gateway.platforms.base import SendResult

    async def send(chat_id: Any, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[Dict[str, Any]] = None) -> "SendResult":
        if _biz_wants_owner(chat_id):
            if _is_status_bubble(str(content or ""), metadata):
                logger.info("[TGAhermes] status bubble kept out of the "
                            "business chat=%s", chat_id)
                return SendResult(success=True, message_id=None)
            _ok = await _biz_send(adapter, chat_id, str(content or ""))
            return SendResult(success=bool(_ok), message_id=None)
        if _is_guest_chat(chat_id):
            return SendResult(success=True, message_id=None)
        return await _orig_send(chat_id, content, reply_to,
                                _biz_safe_metadata(metadata))

    _orig_send = _true(adapter.send, "_orig_send")
    adapter.send = send

    async def send_final_ledgered(event, session_key, text_content, metadata, *, reply_to,
                                  is_ephemeral_response: bool = False):
        md = getattr(event, "metadata", None) or {}
        gqid = md.get("guest_query_id")
        if gqid:
            try:
                await adapter._release_turn_marker(event)
            except Exception:
                logger.debug("[TGAhermes] turn marker release failed", exc_info=True)
            if text_content and str(text_content).strip():
                await _answer_guest_text(adapter, gqid, str(text_content))
            else:
                logger.info("[TGAhermes] empty final for guest query %s", gqid)
            return SendResult(success=True, message_id=None), adapter
        # A session-split marker ("bizauto:<chat>") is not a forum thread id:
        # int() on it as message_thread_id is exactly what crashed queued-lane
        # finals for automation chats. Drop it before any core send sees it.
        metadata = _biz_safe_metadata(metadata)
        _fchat = getattr(getattr(event, "source", None), "chat_id", "")
        _bcid = str(md.get("business_connection_id") or "")
        _ctx_biz = _BIZ_CTX.get(str(_fchat or "")) == "business"
        if not _bcid and _ctx_biz:
            # Queued/synthetic events arrive WITHOUT the event metadata (the
            # bcid lives only on the original event): recover the connection
            # from the live map/candidates so the reply still goes out as the
            # owner instead of falling into the plain path that crashes.
            _cands = _bcid_candidates(_fchat)
            _bcid = _cands[0] if _cands else ""
        if _bcid or _ctx_biz:
            _biz_mark(_fchat, "business")
            return await _biz_deliver(adapter, event, _bcid, text_content)
        # Non-business final: every send belonging to this turn (including the
        # delivery below) must go out as the bot, even for chats with business
        # history otherwise the reply lands in the owner's personal DM.
        _biz_mark(_fchat, "plain")
        result, who = await _orig_sfl(event, session_key, text_content, metadata,
                                      reply_to=reply_to,
                                      is_ephemeral_response=is_ephemeral_response)
        st = settings()
        if st.get("auto_react") and result.success and not getattr(event, "internal", False):
            src = getattr(event, "source", None)
            if src is not None:
                _spawn(_react(src.chat_id, src.message_id, st.get("react_emoji_done") or "✅"))
        return result, who

    _orig_sfl = _true(adapter.send_final_ledgered, "_orig_sfl")
    adapter.send_final_ledgered = send_final_ledgered

    async def send_clarify(chat_id, question, choices, clarify_id, session_key, metadata=None):
        if _biz_wants_owner(chat_id):
            try:
                from tools.clarify_gateway import mark_awaiting_text
                if choices:
                    _numbered = [f"  {_i}. {_c}"
                                 for _i, _c in enumerate(choices, start=1)]
                    _qtext = "\n".join([f"❓ {question}", "", *_numbered, ""])
                    mark_awaiting_text(clarify_id)
                else:
                    _qtext = f"❓ {question}"
                await _biz_send(adapter, chat_id, _qtext)
                return SendResult(success=True, message_id=None)
            except Exception as _ce:
                # Silent: no error text may leave through the automation path.
                logger.warning("[TGAhermes] business clarify send failed: %s", _ce)
                try:
                    await _log("Automation error (silent)",
                               f"chat <code>{_esc(chat_id)}</code> · clarify failed")
                except Exception:
                    logger.debug("[TGAhermes] clarify-failure log failed",
                                 exc_info=True)
                return SendResult(success=True, message_id=None)
        if not _is_guest_chat(chat_id):
            return await _orig_sc(chat_id, question, choices, clarify_id, session_key,
                                  _biz_safe_metadata(metadata))
        try:
            if choices:
                try:
                    from tools import clarify_gateway as _cg
                    with _cg._lock:
                        _is_multi = bool(getattr(_cg._entries.get(clarify_id), "multi_select", False))
                except Exception:
                    _is_multi = False
                hint = ("Multiple selections allowed reply with the numbers separated by commas "
                        "or spaces (e.g. \"1, 3\"), the option text, or your own answer."
                        if _is_multi else
                        "Reply with the number, the option text, or your own answer.")
                numbered = [f"  {i}. {choice}" for i, choice in enumerate(choices, start=1)]
                text = "\n".join([f"❓ {question}", "", *numbered, "", hint])
                from tools.clarify_gateway import mark_awaiting_text
                mark_awaiting_text(clarify_id)
            else:
                text = f"❓ {question}"
            gqid = _gqid_for(adapter, chat_id)
            if gqid:
                await _answer_guest_text(adapter, gqid, text)
            return SendResult(success=True, message_id=None)
        except Exception as e:
            logger.warning("[TGAhermes] guest clarify failed: %s", e)
            return SendResult(success=False, error=str(e))

    _orig_sc = _true(adapter.send_clarify, "_orig_sc")
    adapter.send_clarify = send_clarify

    async def _send_prompt(what, chat_id, metadata, build, *, parse_mode=None,
                           thread_id=None, reply_to_mode=None):
        if not _is_guest_chat(chat_id):
            return await _orig_sp(what, chat_id, _biz_safe_metadata(metadata), build,
                                  parse_mode=parse_mode,
                                  thread_id=_biz_safe_thread(thread_id),
                                  reply_to_mode=reply_to_mode)
        try:
            built = build()
            if isinstance(built, SendResult):
                return built
            text, _kb, _on_sent = built
            gqid = _gqid_for(adapter, chat_id)
            if gqid and text and str(text).strip():
                await _answer_guest_text(adapter, gqid, str(text))
            return SendResult(success=True, message_id=None)
        except Exception as e:
            logger.warning("[TGAhermes] guest prompt %s failed: %s", what, e)
            return SendResult(success=False, error=str(e))

    _orig_sp = _true(adapter._send_prompt, "_orig_sp")
    adapter._send_prompt = _send_prompt

    async def _notify_turn_error(event, e):
        md = getattr(event, "metadata", None) or {}
        gqid = md.get("guest_query_id")
        if str(md.get("business_connection_id") or ""):
            # Automation errors are log-channel only by design: the customer must
            # never receive an error/notification/cron-style message through the
            # automation path. Attempt counter + log entry, no outbound send.
            logger.error("[TGAhermes] automation turn failed: %s", e)
            _spawn(_session_alert("automation-turn", str(e)[:400]))
            try:
                _biz_err_total = int(globals().get("_BIZ_ERR_TOTAL", 0) or 0) + 1
                globals()["_BIZ_ERR_TOTAL"] = _biz_err_total
            except Exception:
                _biz_err_total = -1
            _esrc = getattr(event, "source", None)
            _ecid = str(getattr(_esrc, "chat_id", "") or "")
            try:
                await _log("Automation error (silent)",
                           f"chat <code>{_esc(_ecid)}</code> · "
                           f"total <b>{_biz_err_total}</b> · "
                           f"<code>{_esc(f'{type(e).__name__}: {e}'[:300])}</code>")
            except Exception:
                logger.debug("[TGAhermes] automation error log failed",
                             exc_info=True)
            return None
        if not gqid:
            result = await _orig_nte(event, e)
            st = settings()
            if st.get("auto_react"):
                src = getattr(event, "source", None)
                if src is not None:
                    _spawn(_react(src.chat_id, src.message_id, st.get("react_emoji_error") or "❌"))
            return result
        # Guest-mode errors only (by design): log channel, else owner DM fallback.
        logger.error("[TGAhermes] guest turn failed: %s", e, exc_info=e)
        guest_text = getattr(event, "text", "") or ""
        err = f"{type(e).__name__}: {e}"
        body = (f"{_user_block_id(md.get('guest_user_id'))}\n"
                f"<b>Query:</b> <i>{_esc(guest_text[:300])}</i>\n"
                f"<b>Error:</b> <code>{_esc(err[:400])}</code>")
        logged = await _log("💥 Guest mode error", body)
        if not logged:
            try:
                owner_id = _owner_id(adapter)
                bot = getattr(adapter, "_bot", None)
                if owner_id and owner_id not in ("*", "") and bot is not None:
                    from plugins.platforms.telegram.telegram_ids import normalize_telegram_chat_id
                    await bot.send_message(
                        chat_id=normalize_telegram_chat_id(owner_id),
                        text=(f"⚠️ *ATRA guest error*\nFrom: {md.get('guest_user_id', '?')}\n"
                              f"Query: `{guest_text[:120]}`\nError: `{err[:400]}`"),
                        parse_mode="Markdown")
            except Exception:
                logger.exception("[TGAhermes] owner error notice failed")
        st = settings()
        key = "guest_error_reply_fa" if re.search(r"[\u0600-\u06FF]", guest_text) else "guest_error_reply_en"
        await _answer_guest_text(adapter, gqid, str(st.get(key) or ""))
        if st.get("react_guests") and st.get("auto_react"):
            _spawn(_react(md.get("guest_original_chat_id"), md.get("guest_message_id"),
                          st.get("react_emoji_error") or "❌"))
        return None

    _orig_nte = _true(adapter._notify_turn_error, "_orig_nte")
    adapter._notify_turn_error = _notify_turn_error

    async def send_typing(chat_id, metadata=None):
        if _biz_wants_owner(chat_id):
            _cand = _bcid_candidates(chat_id)
            if _cand:
                try:
                    await adapter._bot.send_chat_action(chat_id=chat_id,
                                                        action="typing",
                                                        business_connection_id=_cand[0])
                except Exception:
                    logger.debug("[TGAhermes] business typing failed", exc_info=True)
            return
        if not _is_guest_chat(chat_id):
            await _orig_st(chat_id, _biz_safe_metadata(metadata))

    _orig_st = _true(adapter.send_typing, "_orig_st")
    adapter.send_typing = send_typing

    # Media: guests get URL media as inline results (or a text fallback); others pass through.
    def _wrap_media(mname: str, kind: str, url_pos: int = 1, name_pos: Optional[int] = None):
        orig = _true(getattr(adapter, mname, None), "orig")
        if orig is None:
            return

        async def media(*args, **kwargs):
            chat_id = args[0] if args else kwargs.get("chat_id")
            if not _is_guest_chat(chat_id):
                if "metadata" in kwargs:
                    # The bizauto session-split marker must never reach the
                    # adapter's int(message_thread_id) media inherits the
                    # same crash as text otherwise.
                    kwargs = dict(kwargs, metadata=_biz_safe_metadata(kwargs.get("metadata")))
                return await orig(*args, **kwargs)
            if not settings().get("media_to_guests"):
                return SendResult(success=True, message_id=None)
            gqid = _gqid_for(adapter, chat_id)
            if not gqid:
                return SendResult(success=True, message_id=None)
            url = (kwargs.get("image_url") or kwargs.get("file_path") or kwargs.get("audio_path")
                   or kwargs.get("image_path") or kwargs.get("url"))
            if url is None and len(args) > url_pos:
                url = args[url_pos]
            caption = kwargs.get("caption")
            if caption is None and mname != "send_multiple_images" and len(args) > 2:
                caption = args[2]
            name = kwargs.get("file_name")
            if name is None and name_pos is not None and len(args) >= name_pos:
                name = args[name_pos - 1]
            if mname == "send_multiple_images":
                images = kwargs.get("images") or (args[1] if len(args) > 1 else []) or []
                first = images[0] if images else None
                url = (first[0] if isinstance(first, (tuple, list)) and first else first) or None
                extra = max(len(images) - 1, 0)
                if extra:
                    caption = f"{caption or ''}\n(+{extra} more images)".strip()
                name = name or "images"
            await _answer_guest_media(adapter, gqid, kind, str(url or ""),
                                      str(caption or ""), str(name or ""))
            return SendResult(success=True, message_id=None)

        setattr(adapter, mname, media)

    _wrap_media("send_image", "photo", url_pos=1)
    _wrap_media("send_image_file", "photo", url_pos=1)
    _wrap_media("send_document", "document", url_pos=1, name_pos=4)
    _wrap_media("send_voice", "voice", url_pos=1)
    _wrap_media("send_multiple_images", "photo", url_pos=1)

    # Business media/action: inject the connection id at the Bot boundary so
    # photos/files/gifs/stickers/typing in automation chats leave as the owner
    # instead of failing as a bot that is not in that chat.
    _biz_bot = getattr(adapter, "_bot", None)
    # NB: PTB 22.8 Bot/ExtBot are slotted setting arbitrary attributes on the bot
    # instance raises and used to abort the whole install. The "already wrapped" marker
    # lives on the adapter, which is a normal object.
    if _biz_bot is not None and not getattr(adapter, "_tga_biz_media", False):
        try:
            setattr(adapter, "_tga_biz_media", True)
        except Exception:
            logger.debug("[TGAhermes] adapter marker failed", exc_info=True)
        for _bn in ("send_photo", "send_video", "send_audio", "send_document",
                    "send_animation", "send_sticker", "send_video_note", "send_voice",
                    "send_media_group", "send_chat_action", "send_location",
                    "send_contact", "send_venue", "send_dice"):
            _borig = getattr(_biz_bot, _bn, None)
            if _borig is None or getattr(_borig, "_tga_biz", False):
                continue

            def _bwrap(_o):
                async def _bw(*args, **kwargs):
                    chat = kwargs.get("chat_id")
                    if chat is None and args:
                        chat = args[0]
                    chat = str(chat or "")
                    if chat and _biz_wants_owner(chat) and "business_connection_id" not in kwargs:
                        _cand = _bcid_candidates(chat)
                        if _cand:
                            kwargs["business_connection_id"] = _cand[0]
                    return await _o(*args, **kwargs)
                _bw._tga_biz = True
                return _bw
            try:
                setattr(_biz_bot, _bn, _bwrap(_borig))
            except Exception:
                logger.debug("[TGAhermes] bot media wrap failed: %s", _bn, exc_info=True)

    logger.info("[TGAhermes] outbound wraps installed (v2)")


def _user_block_id(uid: Any) -> str:
    if not uid:
        return "<i>unknown user</i>"
    return f'<code>{_esc(uid)}</code>'


# ---------------------------------------------------------------- bang console (! commands)

def _fmt_users(limit: int = 30) -> str:
    st = _load_state()
    users = st.get("users") or {}
    if not users:
        return "No users recorded yet."
    rows = sorted(users.items(), key=lambda kv: kv[1].get("last_seen", 0), reverse=True)[:limit]
    lines = [f"Recorded users: {len(users)} (showing {len(rows)})"]
    for uid, e in rows:
        name = e.get("name", "?")
        uname = f"@{e['username']}" if e.get("username") else ""
        flags = " 🚀started" if e.get("started") else ""
        ago = int(time.time()) - int(e.get("last_seen", 0))
        lines.append(f"• <code>{_esc(uid)}</code> {_esc(name)} {uname} {e.get('count', 0)} msgs, "
                     f"{_humanize(ago)} ago{flags}")
    return "\n".join(lines)


def _humanize(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _read_allow_from() -> List[str]:
    """Current telegram.extra.allow_from as a list (owner + whitelisted friends)."""
    try:
        from hermes_cli.config import load_config_readonly
        raw = (load_config_readonly().get("telegram", {}).get("extra", {}) or {}).get("allow_from")
    except Exception:
        raw = None
    if isinstance(raw, (list, tuple, set)):
        return [str(x).strip() for x in raw if str(x).strip()]
    return [x.strip() for x in str(raw or "").split(",") if x.strip()]


def _gate_allow_raw() -> str:
    """What the CORE gate sees: this process's TELEGRAM_ALLOWED_USERS env value.

    Deliberately separate from adapter.extra.allow_from the authz mixin reads
    the env var first, and when it is non-empty it never consults the plugin's
    list. !auth shows this next to the other two so a divergence is visible.
    """
    return str(os.environ.get("TELEGRAM_ALLOWED_USERS") or "").strip()


def _sync_gate_allowlists(csv: str, *, env_path: Any = None,
                          environ: Any = None) -> bool:
    """Mirror allow_from into the CORE gate's own allowlists (.env + os.environ).

    Why this exists (2026-10-02): the gateway's authz mixin has its own
    allowlist tier. ``_principal_authorized`` reads TELEGRAM_ALLOWED_USERS from
    the environment and, because that var is non-empty, it NEVER consults
    ``telegram.extra.allow_from`` the list the panel edits. So a friend added
    in the panel was still dropped by the core as "Dropped a message from
    unrecognized telegram user".

    Semantics: UNION, not mirror ids already approved in .env (operator
    hand-entries) survive, the owner id is always kept, and only then come the
    config's ids. Revoking someone means removing them from BOTH sides, which
    the plugin's own prefilter still enforces either way; this tier is
    admission plumbing, not the reply gate.

    Two writes, deliberately: ``.env`` survives a gateway restart (this var is
    not re-read for per-turn rotated keys), while the environ dict (defaults to
    os.environ) is what the running process reads today. Both are best-effort;
    ``env_path``/``environ`` exist so the harness can test this without ever
    touching the real .env.
    """
    if not csv:
        return False
    env_path = Path(env_path) if env_path else Path(_hermes_home()) / ".env"
    environ = os.environ if environ is None else environ
    owner = str(_owner_id() or "")
    new_ids = [x.strip() for x in str(csv).split(",") if x.strip()]
    try:
        lines = env_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as e:
        logger.warning("[TGAhermes] .env allowlist read failed: %s", e)
        lines = None
    prev: List[str] = []
    if lines is not None:
        for raw in lines:
            s = raw.strip()
            if s.startswith("TELEGRAM_ALLOWED_USERS=") and not s.startswith("#"):
                prev = [x.strip() for x in s.split("=", 1)[1].split(",") if x.strip()]
                break
    merged = list(dict.fromkeys(
        [x for x in [owner] + prev + new_ids if x]))
    value = ",".join(merged)
    if lines is not None:
        try:
            done = False
            for idx, raw in enumerate(lines):
                s = raw.strip()
                if s.startswith("TELEGRAM_ALLOWED_USERS=") and not s.startswith("#"):
                    lines[idx] = raw[:raw.index("=") + 1] + value
                    done = True
            if not done:
                lines.append(f"TELEGRAM_ALLOWED_USERS={value}")
            env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            os.chmod(env_path, 0o600)
            logger.info("[TGAhermes] .env allowlist synced: %s", value)
        except OSError as e:
            logger.warning("[TGAhermes] .env allowlist write failed: %s", e)
    environ["TELEGRAM_ALLOWED_USERS"] = value
    return True


def _write_allow_from(ids: List[str]) -> bool:
    """Persist telegram.extra.allow_from via the hermes CLI (subprocess safe from the gateway).

    Three hard lessons encoded here: the owner id is always merged back in
    (_allow_csv), the file alone is not enough the running adapter keeps
    its own snapshot, so the new value is pushed into it directly
    (_sync_allow_from_live) instead of trusting the plugin reload to do it
    (it does not; measured 2026-10-02), and the core gate keeps a THIRD copy
    in .env / os.environ that also has to move (_sync_gate_allowlists) or the
    owner watches friends get dropped by the gateway as unrecognized senders.
    """
    csv = _allow_csv(ids)
    if not csv:
        logger.error("[TGAhermes] refusing to write an empty allow_from")
        return False
    try:
        import shutil
        import subprocess
        exe = shutil.which("hermes")
        if not exe:
            logger.error("[TGAhermes] hermes CLI not found; whitelist not saved")
            return False
        env = dict(os.environ)
        env["HERMES_HOME"] = str(_hermes_home())
        proc = subprocess.run([exe, "config", "set", "telegram.extra.allow_from", csv],
                              capture_output=True, text=True, timeout=90, env=env)
        if proc.returncode != 0:
            logger.error("[TGAhermes] config set failed: %s",
                         (proc.stderr or proc.stdout or "")[:400])
            return False
        _sync_gate_allowlists(csv)   # the core gate's own tier (.env + this process)
        _sync_allow_from_live(csv)   # immediate: the very next message already passes
        _nudge_gateway_reload()      # + rewire the plugin handlers
        logger.info("[TGAhermes] allow_from write verified: file=%s adapter=%s gate=%s",
                    _read_allow_from(), _adapter_allow_raw(), _gate_allow_raw())
        return True
    except Exception:
        logger.exception("[TGAhermes] whitelist write failed")
        return False


def _nudge_gateway_reload() -> None:
    """Best-effort: rewire live adapters so a fresh allow_from applies without a restart."""
    try:
        import subprocess
        import sys
        import hermes_cli as _hc
        root = str(Path(_hc.__file__).resolve().parents[1])
        env = dict(os.environ)
        env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        env["HERMES_HOME"] = str(_hermes_home())
        code = ("from pathlib import Path;"
                "from gateway.control_socket import reload_gateway_plugins;"
                "reload_gateway_plugins(Path(%r))" % str(_hermes_home()))
        subprocess.Popen([sys.executable, "-c", code], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        logger.debug("[TGAhermes] reload nudge failed", exc_info=True)


def _is_authorized_user(uid: str, owner: str = "") -> bool:
    """Owner or whitelisted friend gets the real brain (core handlers), not the canned reply."""
    uid = str(uid or "")
    if not uid:
        return False
    if owner and uid == owner:
        return True
    return uid in _read_allow_from()


# ------------------------------------------------------------- access-control helpers
# Whitelisted friends carry a permission level for their own DM session:
#   talk only the safe read-only set (web/vision/skills)
#   free everything works except destructive/credential tools (default;
#          "gate" is accepted as its old name)
#   full no tool gating at all (the old whitelisted behavior)
_FRIEND_LEVELS = ("talk", "free", "full")
_FRIEND_LEVEL_LABEL = {"talk": "💬 talk only", "free": "🛠 free", "full": "🔓 full"}
# "gate" was the old name for "free" old configs keep working.
_FRIEND_LEVEL_ALIASES = {"gate": "free"}


def _friend_level(uid: str) -> str:
    """Resolved tool level for uid owner is always full, unknown users 'gate'."""
    uid = str(uid or "")
    if uid and uid == str(_owner_id() or ""):
        return "full"
    lvl = str((settings().get("whitelist_perms") or {}).get(uid) or "free").lower()
    lvl = _FRIEND_LEVEL_ALIASES.get(lvl, lvl)
    return lvl if lvl in _FRIEND_LEVELS else "free"


def _adapter_allow_raw() -> Any:
    """What the CORE prefilter actually sees: the adapter's bound config snapshot.

    Deliberately NOT config.yaml a write to the file does not reach this until
    it is synced or the gateway restarts, and !auth exists to make that visible.
    """
    ad = _ADAPTER.get("adapter")
    extra = getattr(getattr(ad, "config", None), "extra", None)
    return extra.get("allow_from") if isinstance(extra, dict) else None


def _allow_csv(ids: List[str]) -> str:
    """allow_from value: the owner id is ALWAYS merged back in.

    allow_from is the DM gate; a list that lost the owner locks the owner out of
    their own DM on the next restart (2026-10-02: a whitelist write left only the
    friend's id behind this function exists so that cannot happen again).
    """
    owner = str(_owner_id() or "")
    merged = [x for x in [owner] + [str(i).strip() for i in ids] if x]
    return ",".join(dict.fromkeys(merged))


def _sync_allow_from_live(csv: str) -> bool:
    """Push a fresh allow_from into the running adapter's config snapshot.

    The adapter binds config.extra when it wires up, and neither `hermes config
    set` nor `reload_gateway_plugins` rebuilds it (measured: write 13:33:23,
    plugin reload 13:33:25, same user blocked again 13:33:33). Without this the
    core prefilter keeps rejecting users the owner just whitelisted until the
    next gateway restart. In-place dict update the authz mixin reads the same
    object, so the prefilter and the runner chain both see the new value.
    """
    if not csv:
        return False
    ad = _ADAPTER.get("adapter")
    if ad is None:
        return False
    try:
        extra = getattr(getattr(ad, "config", None), "extra", None)
        if not isinstance(extra, dict):
            logger.warning("[TGAhermes] adapter config.extra unavailable allow_from not synced")
            return False
        extra["allow_from"] = csv
        logger.info("[TGAhermes] live allow_from synced: %s", csv)
        _sync_gate_allowlists(csv)   # keep the core gate's env tier in step too
        return True
    except Exception:
        logger.exception("[TGAhermes] live allow_from sync failed")
        return False


# ------------------------------------------------------------ guest session control
# state.json -> "guest_sessions": {"<uid>": {"state": ..., "created": ..., "updated": ts}}
#   default the usual stranger rules (canned on plain mention, brain on reply)
#   open may talk without replying to ATRA
#   locked answers only guest_locked_reply (rate-limited by the cooldown)
# A record appears automatically the first time someone talks ("created": "auto")
# or when the owner opens one ("created": "owner") either can be locked.

def _guest_sessions() -> Dict[str, Any]:
    return (_load_state().get("guest_sessions") or {})


def _set_guest_session(uid: str, state: str, by: str = "owner") -> None:
    uid = str(uid or "").strip()
    if not uid or state not in ("default", "open", "locked"):
        return

    def _fn(st: Dict[str, Any]):
        recs = st.setdefault("guest_sessions", {})
        rec = recs.setdefault(uid, {"state": "default", "created": "auto"})
        rec["state"] = state
        if by == "owner":
            rec["created"] = "owner"
        rec["updated"] = int(time.time())
        return None

    _mutate_state(_fn)


def _touch_guest_session(uid: str) -> None:
    """Auto-create an observation record when a guest first talks."""
    uid = str(uid or "").strip()
    if not uid:
        return

    def _fn(st: Dict[str, Any]):
        recs = st.setdefault("guest_sessions", {})
        if uid not in recs:
            recs[uid] = {"state": "default", "created": "auto", "updated": int(time.time())}
        return None

    _mutate_state(_fn)


def _like_literal(text: str) -> str:
    """Escape a value used as a LIKE pattern chat ids contain `_`, which is a
    single-char wildcard and quietly matched neighbouring rows."""
    return str(text).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _stored_session_ids(chat: Any) -> List[str]:
    """Session rows this wipe owns: the chat's own rows, its guest twin, and
    everything routed under a key containing them the SAME predicate the
    routing reset uses, so the router and the Sessions tab agree on what
    "wiped" means. Read-only query; [] on any failure (the caller still resets
    routing)."""
    cid = str(chat or "").strip()
    if not cid or cid == "None":
        return []
    guest = f"{GUEST_CHAT_PREFIX}{cid}"
    try:
        db = _hermes_home() / "state.db"
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT id FROM sessions WHERE chat_id IN (?, ?)"
                " OR session_key LIKE ? ESCAPE '\\'"
                " OR session_key LIKE ? ESCAPE '\\'",
                (cid, guest, f"%{_like_literal(cid)}%",
                 f"%{_like_literal(guest)}%")).fetchall()
        finally:
            con.close()
        return [str(r[0]) for r in rows if r and r[0]]
    except Exception:
        logger.exception("[TGAhermes] stored session lookup failed for %s", cid)
        return []


def _purge_stored_sessions(ids: List[str]) -> int:
    """Delete those session rows and their messages so they stop showing in
    the Sessions tab. Direct read-write sqlite on purpose: the gateway's own
    SessionDB handle is best-effort and can be unavailable (state.db preflight)
    while file-level writes still work, and a wipe that only resets routing
    leaves every row listed exactly as before. FTS stays consistent through the
    messages_fts_delete trigger."""
    if not ids:
        return 0
    try:
        db = _hermes_home() / "state.db"
        con = sqlite3.connect(str(db), timeout=20.0)
        try:
            con.execute("PRAGMA busy_timeout = 20000")
            ph = ",".join("?" * len(ids))
            con.execute(f"DELETE FROM messages WHERE session_id IN ({ph})", ids)
            cur = con.execute(f"DELETE FROM sessions WHERE id IN ({ph})", ids)
            con.commit()
            return max(0, int(cur.rowcount or 0))
        finally:
            con.close()
    except Exception:
        logger.exception("[TGAhermes] stored session purge failed (%d ids)", len(ids))
        return 0


def _wipe_sessions(store: Any, chat: Any) -> Optional[Tuple[int, int]]:
    """Fresh start for this chat: reset every gateway session it owns (all
    participants + guest twin) AND drop the stored rows, so the tab really
    empties instead of listing the same sessions forever.

    Returns (routes_reset, rows_deleted); (0, 0) when the chat has neither;
    None only when the chat id itself is unusable. Routing is collected first
    and the rows deleted afterwards, so a row the reset just created for the
    fresh session survives the wipe removes what was there, not what comes
    next."""
    cid = str(chat or "").strip()
    if not cid or cid == "None":
        return None
    if store is None:
        store = _CTX.get("session_store")
    entries = getattr(store, "_entries", None) if store is not None else None
    if not isinstance(entries, dict):
        # No store means no way to make the router start fresh deleting rows
        # nothing can re-route away from would strand the next turn on ids that
        # no longer exist. Report it and touch nothing.
        return None
    needles = (cid, f"{GUEST_CHAT_PREFIX}{cid}")
    routes = 0
    try:
        # Collect the stored rows BEFORE the reset: reset_session creates the
        # fresh row, and the purge must not take out the session it just made.
        stored = _stored_session_ids(cid)
        keys = [k for k in list(entries) if any(n in str(k) for n in needles)]
        failed = 0
        for k in keys:
            try:
                store.reset_session(k)
                routes += 1
            except Exception:
                failed += 1
                logger.exception("[TGAhermes] route reset failed for %s", k)
        if failed:
            # Routing still points at the sessions we would be deleting: keep
            # their rows, or the next turn lands on ids that no longer exist.
            logger.warning("[TGAhermes] wipe: %d route(s) left unreset for %s "
                           " stored rows kept", failed, cid)
            return None
        rows = _purge_stored_sessions(stored)
    except Exception:
        logger.exception("[TGAhermes] session wipe failed")
        return None
    # The wipe held: this chat now starts a genuinely new conversation, so
    # drop the first-contact stamp too. Otherwise the next session opens cold
    # with no introduction even though biz_warn_first is still on. Skipped on
    # every early return above, so a failed wipe keeps the old stamp.
    bstore = _biz_store()
    if bstore is not None:
        try:
            bstore.clear_warned(cid)
        except Exception:
            logger.debug("[TGAhermes] clear_warned failed", exc_info=True)
    if not routes and not rows:
        return (0, 0)
    return (routes, rows)


def _settings_summary() -> str:
    st = settings()
    keys = ["owner_id", "log_channel", "unauthorized_reply", "unauthorized_cooldown_s",
            "guest_error_reply_en", "guest_error_reply_fa", "auto_react", "react_guests",
            "media_to_guests", "log_owner_messages", "log_whitelisted_messages",
            "log_other_messages", "log_group_mentions", "tool_enabled",
            "persona_path", "biz_persona_path"]
    out = []
    for k in keys:
        v = st.get(k)
        if not v and k == "persona_path":
            v = "default"
        elif not v and k == "biz_persona_path":
            v = "bundled"
        out.append(f"<code>{k}</code> = <b>{_esc(v)}</b>")
    return "\n".join(out)


def _help_sections(st: Optional[Dict[str, Any]] = None) -> list:
    """(key, title, body) single source for the full help, sections and panel views."""
    st = st or settings()
    wl = _read_allow_from()
    log_now = _esc(st.get("log_channel") or "off")
    return [
        ("status", "ℹ️ Status",
         "<b>⚡ Actions</b> guided flows (log here, whitelist, DM, wipe, updates)\n"
         "<code>!settings</code> editable settings\n"
         "<code>!users</code> who used the bot\n"
         "<code>!run</code> restart Hermes (starts it too, if it is down)\n"
         "<code>!panel</code> this glass-button panel (same as !help)"),
        ("log", "📡 Log",
         f"now: <code>{log_now}</code>\n"
         "<code>!setlog here</code> send <i>inside</i> the chat you want to log\n"
         "<code>!setlog &lt;id|@name|off&gt;</code>"),
        ("access", "🛡 Access",
         f"{len(wl)} whitelisted\n"
         "<code>!whitelist list</code>\n"
         "<code>!whitelist add &lt;user_id&gt;</code>\n"
         "<code>!whitelist remove &lt;user_id&gt;</code>\n"
         "<code>!whitelist perms &lt;user_id&gt; talk|gate|full</code> tool level\n"
         "Whitelisted friends talk to the real bot; their level decides which "
         "tools their session may use.\n"
         "<code>!auth [user_id]</code> see file vs live prefilter verdicts"),
        ("sessions", "🧹 Sessions",
         "one per chat; wipe = fresh start\n"
         "<code>!wipe &lt;chat_id&gt;</code> delete that chat's session, fresh start "
         "<i>there</i> (any DM/group/guest; works from the log channel too)\n"
         "Or use the 🧹 button under a log entry.\n"
         "<code>!gs list|open|lock|reset &lt;user_id&gt;</code> guest sessions: "
         "🔓 open = talk freely · 🔒 locked = sealed · ▫️ reset = stranger rules"),
        ("guests", "👾 Guests",
         f"canned reply: <i>{_esc(st.get('unauthorized_reply'))}</i>\n"
         f"cooldown: <b>{_esc(st.get('unauthorized_cooldown_s'))}s</b> "
         "<code>!setcooldown 10m</code> · <code>30s</code> · <code>1h</code> (or plain seconds)\n"
         f"error EN: <i>{_esc(st.get('guest_error_reply_en'))}</i>\n"
         f"error FA: <i>{_esc(st.get('guest_error_reply_fa'))}</i>\n"
         "<code>!setunauthorized &lt;text&gt;</code> · <code>!seterror &lt;text&gt;</code> · "
         "<code>!seterrorfa &lt;text&gt;</code>"),
        ("bot", "🤖 Bot",
         "<code>!send &lt;user_id&gt; &lt;text&gt;</code> DM someone as the bot\n"
         "<code>!setunauthorized &lt;text&gt;</code> reply for strangers\n"
         "<code>!seterror &lt;text&gt;</code> / <code>!seterrorfa &lt;text&gt;</code> guest error texts\n"
         "<code>!setreact on|off</code> · <code>!setmedia on|off</code>\n"
         "<code>!setcooldown 10m</code> stranger reply cooldown (any duration)\n"
         "<code>!setowner &lt;id&gt;</code> owner id"),
    ]


def _help_text(st: Optional[Dict[str, Any]] = None) -> str:
    parts = ["🧩 <b>ATRA console</b> owner only"]
    for _key, _title, _body in _help_sections(st):
        parts.append(f"<b>{_title}</b>\n{_body}")
    return "\n\n".join(parts)


def _known_guests_view(st: Dict[str, Any]) -> str:
    """Who has used the guest link, and what they are allowed to do.

    Reads the same identity the log channel prints, so this list and the log
    can never disagree about who is who.
    """
    users = (_load_state() or {}).get("users") or {}
    owner = str(_owner_id() or "")
    mode = str(st.get("guest_tool_mode") or "balanced")
    unlocked = {str(c).strip() for c in (st.get("guest_owner_chats") or [])
                if str(c).strip()}
    owner_on = bool(st.get("guest_owner_full_access", False))

    rows = []
    for uid, e in users.items():
        uid = str(uid)
        chat = _guest_chat_id(uid)
        if uid == owner:
            role = "👑 owner"
        elif owner_on and chat in unlocked:
            role = "🔓 unlocked (full)"
        else:
            role = f"🚧 guest · {_MODE_LABEL.get(mode, mode)}"
        label = _esc(str(e.get("name") or uid))
        if e.get("username"):
            label += f" (@{_esc(str(e['username']))})"
        count = int(e.get("count") or 0)
        seen = time.strftime("%Y-%m-%d", time.localtime(int(e.get("last_seen") or 0)))
        rows.append(f"<b>{label}</b>\n   <code>{_esc(uid)}</code> · {role} · "
                    f"{count} msg · last {seen}")

    lines = ["<b>👤 Who is on the guest link</b>",
             f"<i>mode: {_MODE_LABEL.get(mode, mode)} · "
             f"owner access {'on' if owner_on else 'off'}</i>", ""]
    lines += sorted(rows) if rows else ["Nobody has used it yet."]
    lines += ["",
              "This is the same identity the log channel prints: the id is the "
              "person's own Telegram id, so it never depends on the forwarded "
              "message."]
    return "\n".join(lines)


def _help_view(key: str, st: Optional[Dict[str, Any]] = None) -> str:
    """One section (or the full list) as HTML."""
    st = st or settings()
    if key == "sessions":
        # The session page is LIVE data, not help text: it renders whatever is
        # running right now so the 🛑/🧹 buttons next to it mean something.
        return _sessions_body(st=st)
    if key == "full":
        return _help_text(st)
    if key in _CATS:
        # v4.0.0: the section pages ARE the categories every setting with
        # its live value, instead of a read-only list of command names.
        return _cat_body(key, st)
    if key == "who":
        return _known_guests_view(st)
    for k, title, body in _help_sections(st):
        if k == key:
            return f"<b>{title}</b>\n{body}"
    return _help_text(st)


def _gate_view(st: Dict[str, Any], note: str = "") -> str:
    """Show exactly what a guest can and cannot do, per mode."""
    mode = str(st.get("guest_tool_mode") or "balanced")
    denied = _guest_allowed(frozenset())
    blocked = sorted(denied)
    never = sorted(GUEST_NEVER_TOOLS)
    lines = [
        f"<b>🛡 Guest tool gate</b> mode: <b>{_MODE_LABEL.get(mode, mode)}</b>",
        "",
        "<b>strict</b> · no shell, no files, nothing",
        "<b>balanced</b> · may read files and past chats, writes closed",
        "<b>open</b> · same tools as any chat (a stranger can act on this box)",
        "",
        f"<b>Owner in own guest chat:</b> "
        f"{'full access' if st.get('guest_owner_full_access', False) else 'blocked too'}",
        "",
        f"<b>Blocked for guests ({len(blocked)}):</b>",
        _esc(", ".join(blocked)) if blocked else "nothing",
        "",
        f"<b>Never allowed, even in open:</b> {_esc(', '.join(never))}",
        "",
        f"<b>Extra tools you opened:</b> "
        f"{_esc(', '.join(str(t) for t in (st.get('guest_allow_tools') or []))) or 'none'}",
        "",
        f"<b>Guest chats you unlocked for yourself:</b> "
        f"{_esc(', '.join(str(c) for c in (st.get('guest_owner_chats') or []))) or 'none'}",
        "",
        "Telegram never tells a bot who sent a guest message, so the plugin "
        "cannot recognise you in a guest chat on its own. Unlocking a chat "
        "below is the deliberate, revocable way to do it.",
        "",
        "Change the mode with the button above. Per-tool lists live in "
        "<code>settings.json</code>: <code>guest_allow_tools</code> and "
        "<code>guest_deny_tools</code>. Takes effect on the next message.",
    ]
    if note:
        lines.insert(1, note)
    return "\n".join(lines)


def _update_note(report: Dict[str, Any]) -> str:
    """One-line result for the panel, or a readable error block."""
    if report.get("error"):
        return f"❌ <b>update failed</b>\n<code>{_esc(str(report['error'])[:300])}</code>"
    if report.get("action"):
        return f"📦 {report['action']}"
    if report.get("newer_available"):
        return "⬆️ a newer version is available tap Install update"
    return "✅ up to date"


def _plugin_version() -> str:
    try:
        import re as _re
        from pathlib import Path as _P
        txt = (_P(__file__).resolve().parent / "plugin.yaml").read_text(encoding="utf-8")
        m = _re.search(r"^version:\s*(\S+)", txt, _re.M)
        return m.group(1) if m else "?"
    except Exception:
        return "?"


def _gsess_view(st: Dict[str, Any]) -> str:
    """Guest session records: what each state means, who created it, what to press."""
    recs = _guest_sessions()
    lines = [
        "<b>🔐 Guest sessions</b>",
        "",
        "▫️ <b>default</b> stranger rules: canned on a plain mention, real answer on a reply",
        "🔓 <b>open</b> may talk without replying to ATRA",
        "🔒 <b>locked</b> answers only the locked reply (cooldown applies)",
        "",
    ]
    if not recs:
        lines += ["No sessions yet. One appears here automatically the first time someone "
                  "talks on the guest link or open one for a specific person below."]
    else:
        for uid, rec in sorted(recs.items()):
            state = str(rec.get("state") or "default")
            mark = {"open": "🔓 open", "locked": "🔒 locked"}.get(state, "▫️ default")
            who = "opened by you" if rec.get("created") == "owner" else "auto-created"
            lines.append(f"<code>{_esc(uid)}</code> · {mark} · {who}")
    lines += ["", "Buttons below open, lock or reset a session; the command twin is "
                  "<code>!gs list|open|lock|reset &lt;user_id&gt;</code>."]
    return "\n".join(lines)


def _panel_text(st: Optional[Dict[str, Any]] = None, note: str = "") -> str:
    """Home board for !panel a live readout of every category, so the first
    screen answers "what is on?" instead of just listing section names."""
    st = st or settings()
    wl = _read_allow_from()
    friends = [u for u in wl if u != str(_owner_id() or "")]

    def _b(k: str) -> str:
        return "on" if st.get(k) else "off"

    lines = [
        f"\U0001f9e9 <b>ATRA console</b> v{_plugin_version()}",
        "",
        f"\U0001f4e1 log: <code>{_esc(st.get('log_channel') or 'off')}</code> · "
        f"\U0001f451 owner: <code>{_esc(_owner_id() or 'unset')}</code>",
        f"\U0001f6e1 friends: <b>{len(friends)}</b> · "
        f"\u23f1 stranger cooldown: <b>{_esc(st.get('unauthorized_cooldown_s'))}s</b>",
        f"\U0001f6e1 guest tool mode: <b>"
        f"{_MODE_LABEL.get(str(st.get('guest_tool_mode') or 'balanced'), 'balanced')}</b> · "
        f"\U0001f6e0 admin tool: <b>{_b('tool_enabled')}</b>",
        f"\U0001f501 reactions: <b>{_b('auto_react')}</b> · "
        f"\U0001f465 guest reacts: <b>{_b('react_guests')}</b> · "
        f"\U0001f5bc guest media: <b>{_b('media_to_guests')}</b>",
        f"\U0001f4ac mirrored: 👑 <b>{_b('log_owner_messages')}</b> · "
        f"\U0001f4ac wl <b>{_b('log_whitelisted_messages')}</b> · "
        f"\U0001f5e8 other <b>{_b('log_other_messages')}</b> · "
        f"\U0001f4e3 mentions <b>{_b('log_group_mentions')}</b>",
        f"\U0001f4e6 updates: <b>{'on' if st.get('update_enabled', True) else 'locked'}</b> · "
        f"\U0001f3ad persona: <b>{'custom' if st.get('persona_path') else 'default'}</b>",
        f"\U0001f4bc automation: <b>{_biz_sched_badge(st)}</b> · "
        f"mode: <b>{_MODE_LABEL.get(str(st.get('biz_mode') or 'assistant'), 'assistant')}</b> · "
        f"\U0001f310 lang: <b>{_esc(str(st.get('biz_lang') or 'auto'))}</b>",
    ]
    if note:
        lines.append(note)
    lines += [
        "",
        "<b>Tap a category</b> every setting inside it shows its live value, "
        "and every change is a prompt or a confirm screen. No commands needed.",
        "\u21a9\ufe0f Back returns to the page you came from.",
        "<code>!panel</code> reloads this · <code>!help</code> the command list",
    ]
    return "\n".join(lines)


def _plugin_version() -> str:
    """Version from plugin.yaml, read without importing the plugin."""
    try:
        text = (PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "?"
    m = re.search(r"^version:\s*[\"\']?([^\s\"\']+)", text, re.M)
    return m.group(1) if m else "?"


def _actions_view(st: Dict[str, Any], note: str = "") -> str:
    """Body text for the Actions tab the panel's command replacement."""
    wl = _read_allow_from()
    lines = [
        "<b>⚡ Actions</b> guided flows that replace typing commands",
        f"📡 log: <code>{_esc(st.get('log_channel') or 'off')}</code> · "
        f"🛡 whitelist: <b>{len(wl)}</b> · "
        f"⏱ cooldown: <b>{_esc(st.get('unauthorized_cooldown_s'))}s</b>",
        "",
        "📍 <b>Log here</b> start logging into THIS chat",
        "🛡 <b>Whitelist</b> add, remove, or set a friend's permission level",
        "🔐 <b>Guest sessions</b> open a session for one guest, or lock it",
        "📨 <b>Send a DM</b> message someone as the bot",
        "🧹 <b>Wipe a session</b> give a chat a fresh start",
        "⏱ <b>Cooldown</b> · 👾 <b>Stranger reply</b> canned texts and delays",
        "🩺 <b>Auth debug</b> who gets through where, file vs live snapshot",
        "🛡 <b>Safeguards</b> tool gate modes, allow/deny lists, friend levels",
        "🔧 <b>System + updates</b>",
        "",
        "Tap a button, answer the prompt, done. Every flow runs the same code "
        "as its command twin (<code>!setlog</code>, <code>!whitelist</code>, "
        "<code>!gs</code>, <code>!gate</code>, <code>!auth</code>, "
        "<code>!wipe</code>, <code>!send</code>), so the buttons and the "
        "commands can never drift apart.",
    ]
    if note:
        lines.insert(1, note)
    return "\n".join(lines)


# ---------------------------------------------------------------- rework bodies (v3.3.0 → v4.0.0)
# The panel used to be one home grid carrying every flag. It grew to eleven
# rows and the owner could not find anything (new flows existed but sat one
# level deep). v3.2.0: home is sections only, every mutation goes through a
# confirm screen or a wizard, and the whitelist is a per-friend card instead
# of three separate list pages. v3.3.0: no destructive or time-spending
# button applies on the first tap any more the tap renders a confirm screen
# and only the Apply callback writes (`panel:cfmok`; legacy `panel:tgy` is
# normalised to it at parse time).
#
# v4.0.0 rebuilt the navigation layer on top of that:
#   * `_CATS` is the single registry every one of the 29 settings keys sits
#     in exactly one category, the body renders each with its LIVE value, and
#     the keyboard emits one button per item. A test asserts the coverage, so
#     a new settings key cannot become unreachable by accident.
#   * text settings open a prompt-driven wizard (flows may carry `build`, a
#     bang-command twin, or `save`, a settings patch) nothing is typed as a
#     command; `validate` re-prompts instead of killing the flow.
#   * Back is a per-panel-message history (`_NAV`), not a jump to the console.
#     Confirm screens and the wizard are transient and never enter the stack.

# callback sub-key -> (settings key, button label) for confirm-screen toggles.
# `mode` is special-cased (it cycles rather than flips) and is not listed here.
_TOGGLES: Dict[str, Tuple[str, str]] = {
    "react": ("auto_react", "🔁 reactions"),
    "greact": ("react_guests", "👥 guest reacts"),
    "media": ("media_to_guests", "🖼 guest media"),
    "mirror": ("log_owner_messages", "👑 mirror"),
    "mentions": ("log_group_mentions", "📣 group mentions"),
    "tool": ("tool_enabled", "🛠 admin tool"),
    "wmsgs": ("log_whitelisted_messages", "💬 whitelisted msgs"),
    "omsgs": ("log_other_messages", "🗨 other msgs"),
    "owner": ("guest_owner_full_access", "🔓 owner access in unlocked guest chats"),
    "bizmode": ("biz_mode", "\U0001f916 automation mode"),
    "bizwarn": ("biz_warn_first", "\u26a0\ufe0f first-contact warning"),
    "bizreact": ("biz_react", "\U0001f47e automation reply reaction"),
    "bizlarp": ("biz_larp", "\U0001f3ad LARP voice"),
    "bizmedia": ("biz_media", "\U0001f5bc\ufe0f Media in + out"),
    "bizfull": ("biz_full_access", "\U0001f513 Full access (all tools)"),
    "userbridge": ("user_bridge", "🔓 Full unlock (act as you)"),
    "bizsplit": ("biz_sessions_split", "🧮 split automation vs whitelist sessions"),
}


# Confirm sub-keys that CYCLE through an ordered list instead of flipping a
# boolean. They are deliberately NOT in _TOGGLES: that registry feeds the
# generic on/off writer, and a 3-way or numeric setting must never be reduced to
# a bool by it. `_tg_next` / `_tg_view` / the Apply writer branch on them by name.
_TENUMS: Tuple[str, ...] = ("mode", "bizmode", "bizsched",
                            "bizidledelay")

# Confirm sub-key -> wizard flow that accepts a typed duration for it. The
# confirm screen keeps its preset cycle; these give the owner a way to enter
# something the preset list does not contain (90s, 3h, …).
_DUR_FLOWS: Dict[str, str] = {"bizidledelay": "bizidledur"}


def _next_preset(cur: Any, presets: List[int], fallback: int) -> int:
    """Next value in a duration preset list.

    A typed value (10s -> 0.1667 min, 90s -> 1.5 min) is not in the list, so
    cycling lands on the first preset above it instead of snapping back to the
    bottom otherwise Apply would quietly discard what was just typed.
    """
    try:
        v = float(cur if cur not in (None, "") else fallback)
    except Exception:
        v = float(fallback)
    if v in presets:
        return presets[(presets.index(v) + 1) % len(presets)]
    for step in presets:
        if step > v:
            return step
    return presets[0]


def _tg_next(sub: str, st: Dict[str, Any]) -> Any:
    """Next value behind a confirm screen: booleans flip, guest mode cycles."""
    if sub == "mode":
        order = ["strict", "balanced", "open"]
        cur = str(st.get("guest_tool_mode") or "balanced")
        return order[(order.index(cur) + 1) % len(order)] if cur in order else "balanced"
    if sub == "bizmode":
        cur_b = str(st.get("biz_mode") or "assistant")
        order_b = list(_BIZ_MODE_ORDER)
        return (order_b[(order_b.index(cur_b) + 1) % len(order_b)]
                if cur_b in order_b else "assistant")
    if sub == "bizsched":
        cur_s = _biz_sched_st(st)
        order_s = list(_BIZ_SCHEDULE_ORDER)
        return order_s[(order_s.index(cur_s) + 1) % len(order_s)]
    if sub == "bizidledelay":
        return _next_preset(st.get("biz_idle_delay_min"), [0, 1, 2, 5, 10, 15, 30], 0)
    ent = _TOGGLES.get(sub)
    if not ent:
        return None
    return not bool(st.get(ent[0]))


def _ub_cfm(st: Dict[str, Any]) -> str:
    """Confirm screen for Full unlock: explanation + the danger warning."""
    nxt = _tg_next("userbridge", st)
    head = ("<b>\U0001f513 Full unlock (act as you)</b>\n"
            f"now: <b>{'on' if st.get('user_bridge') else 'off'}</b> → "
            f"<b>{'on' if nxt else 'off'}</b>\n\n")
    if not nxt:
        return (head + "Tap ✅ Apply to drop the owner-session connection and "
                "stop the reaction listener, ✖ Cancel to go back.")
    return (head +
            "Connects a <b>copy of your own Telegram session</b> to the gateway "
            "so ATRA acts <b>as you</b> where the Bot API cannot: reactions "
            "inside business chats set them, see them live.\n\n"
            "<b>\u26a0\ufe0f DANGER:</b> a user account running automation can trip "
            "Telegram flood limits or get banned. Nobody has been banned so far, "
            "but that is luck, not a guarantee. The live session file is never "
            "touched (read-once backup copy), credentials live outside the repo, "
            "and you can turn this off any time.\n\n"
            "Tap ✅ Apply to change, ✖ Cancel to go back.")


def _tg_view(sub: str, origin: str, st: Dict[str, Any]) -> str:
    """Confirm screen body now → next, nothing applied until Apply."""
    if sub == "mode":
        cur = str(st.get("guest_tool_mode") or "balanced")
        nxt = str(_tg_next("mode", st))
        return ("<b>🛡 Guest tool mode</b>\n"
                f"now: <b>{_MODE_LABEL.get(cur, cur)}</b> → <b>{_MODE_LABEL.get(nxt, nxt)}</b>\n\n"
                "Tap ✅ Apply to change, ✖ Cancel to go back.")
    if sub == "bizmode":
        cur_b = str(st.get("biz_mode") or "assistant")
        nxt_b = _tg_next("bizmode", st)
        return (f"<b>\U0001f916 Automation mode</b>\n"
                f"now: <b>{_MODE_LABEL.get(cur_b, cur_b)}</b> → "
                f"<b>{_MODE_LABEL.get(nxt_b, nxt_b)}</b>\n\n"
                "assistant: ATRA answers as itself · mimic: writes in your voice · "
                "off: stay silent\n\n"
                "Tap ✅ Apply to change, ✖ Cancel to go back.")
    if sub == "bizsched":
        cur_s = _biz_sched_st(st)
        nxt_s = _tg_next("bizsched", st)
        lmap = {"always": "always answer", "window": "inside window only",
                "off": "switched off"}
        body = (f"<b>\U0001f4d5 Automation schedule</b>\n"
                f"now: <b>{lmap.get(cur_s, cur_s)}</b> → "
                f"<b>{lmap.get(nxt_s, nxt_s)}</b>\n\n"
                "always: answer every chat · window: only inside the window "
                "below (days + HH:MM, may wrap midnight) · off: never answer.\n\n"
                "Tap ✅ Apply to change, ✖ Cancel to go back.")
        if nxt_s == "window":
            body += (f"\n\nwindow: <b>{_esc(str(st.get('biz_window_start') or '00:00'))}</b>"
                     f"–<b>{_esc(str(st.get('biz_window_end') or '23:59'))}</b>")
        return body
    if sub == "bizidledelay":
        cur_d = _fmt_min(st.get("biz_idle_delay_min"))
        nxt_d = _fmt_min(_tg_next("bizidledelay", st))
        return (f"<b>⏳ Reply hold</b>\n"
                f"now: <b>{cur_d}</b> → <b>{nxt_d}</b>\n\n"
                "Every message in an automation chat is held this long so you "
                "get the first word; if you reply there inside the window, "
                "ATRA stands down. 0 = answer immediately.\n"
                "Tap ✏️ to type any duration (<code>10s</code>, <code>2m</code>, "
                "<code>1h</code>).\n\n"
                "Tap ✅ Apply to change, ✖ Cancel to go back.")
    if sub == "userbridge":
        return _ub_cfm(st)
    ent = _TOGGLES.get(sub)
    if not ent:
        return "❌ unknown setting."
    key, label = ent
    nxt = _tg_next(sub, st)
    return (f"<b>{label}</b>\n"
            f"now: <b>{'on' if st.get(key) else 'off'}</b> → "
            f"<b>{'on' if nxt else 'off'}</b>\n\n"
            "Tap ✅ Apply to change, ✖ Cancel to go back.")


def _cfm_view(kind: str, arg: str, st: Dict[str, Any]) -> str:
    """One confirm screen for every mutating button.

    It states the CURRENT value and the NEXT one, plus the consequence, so a tap
    is never a guess. It changes NOTHING the Apply callback is the only writer.
    """
    if kind == "bizlang":
        cur = str(st.get("biz_lang") or "auto")
        nxt = (arg or "auto").strip() or "auto"
        cur_l = bizauto.language_label(cur) if bizauto else cur
        nxt_l = bizauto.language_label(nxt) if bizauto else nxt
        warn = str(st.get("biz_warn_text") or "").strip()
        return (f"<b>\U0001f310 Automation language</b>\n"
                f"now: <b>{_esc(cur_l)}</b> → <b>{_esc(nxt_l)}</b>\n\n"
                "The first-contact warning and every automation reply go out in "
                "this language. <b>auto</b> detects per message (Persian included)."
                f"\n\nwarning: <i>{_esc(warn[:80]) if warn else 'ATRA writes it'}</i>"
                "\n\nTap ✅ Apply to change, ✖ Cancel to go back.")
    if kind == "tg":                      # a settings flag / guest mode cycle
        sub, _, _origin = (arg or "").partition(":")
        if sub == "mode":
            cur = str(st.get("guest_tool_mode") or "balanced")
            nxt = str(_tg_next("mode", st))
            return (f"<b>🛡 Guest tool mode</b>\n"
                    f"now: <b>{_MODE_LABEL.get(cur, cur)}</b> → "
                    f"<b>{_MODE_LABEL.get(nxt, nxt)}</b>\n\n"
                    "<b>strict</b> nothing · <b>balanced</b> reads only · "
                    "<b>open</b> anything a stranger could act on this box.\n\n"
                    "Tap ✅ Apply to change, ✖ Cancel to go back.")
        if sub == "userbridge":
            return _ub_cfm(st)
        ent = _TOGGLES.get(sub)
        if not ent:
            return "❌ unknown setting."
        key, label = ent
        nxt = _tg_next(sub, st)
        return (f"<b>{label}</b>\n"
                f"now: <b>{'on' if st.get(key) else 'off'}</b> → "
                f"<b>{'on' if nxt else 'off'}</b>\n\n"
                "Tap ✅ Apply to change, ✖ Cancel to go back.")

    if kind == "gs":                      # panel:cfm:gs:<origin>:<uid>:<open|lock|reset>
        guid, _, gact = (arg or "").partition(":")
        verb = {"open": "🔓 open", "lock": "🔒 lock",
                "reset": "▫️ reset to default"}.get(gact, gact or "change")
        rec = (_guest_sessions() or {}).get(guid) or {}
        cur = str(rec.get("state") or "default")
        means = {"open": "may talk without replying to ATRA",
                 "lock": "answers only the locked reply (cooldown applies)",
                 "reset": "back to plain stranger rules"}.get(gact, "")
        return "\n".join([
            f"<b>{verb} the guest session of <code>{_esc(guid)}</code>?</b>",
            f"now: <b>{cur}</b>",
            "",
            f"after this: <b>{means}</b>",
            "",
            "Tap ✅ Apply to change, ✖ Cancel to go back.",
        ])

    if kind == "log":
        sub, _, _origin = (arg or "").partition(":")
        cur = _esc(str(st.get("log_channel") or "off"))
        if sub == "here":
            return ("<b>📡 Log into THIS chat?</b>\n"
                    f"now: <code>{cur}</code>\n\n"
                    "Everything this plugin logs guest activity, your own mirror, "
                    "whitelist messages lands here from now on. The bot has to be "
                    "able to post in this chat.\n\n"
                    "Tap ✅ Apply to change, ✖ Cancel to go back.")
        return ("<b>📡 Turn the log off?</b>\n"
                f"now: <code>{cur}</code> → <b>off</b>\n\n"
                "Nothing is posted to the log chat any more. Guests keep working.\n\n"
                "Tap ✅ Apply to change, ✖ Cancel to go back.")

    if kind == "sup":                     # panel:cfm:sup:upd:lock
        on = bool(st.get("update_enabled", True))
        return ("<b>🔒 Update lock</b>\n"
                f"now: <b>{'unlocked' if on else '🔒 locked'}</b> → "
                f"<b>{'🔒 locked' if on else 'unlocked'}</b>\n\n"
                + ("Locked: the panel's check/install buttons and the selfupdate "
                   "tool both refuse to run.\n\n" if on else
                   "Unlocked: check and install are re-enabled on the System page.\n\n")
                + "Tap ✅ Apply to change, ✖ Cancel to go back.")

    if kind == "upd":                     # panel:cfm:upd:<check|apply>
        if arg == "apply":
            return ("<b>⬆️ Install the update?</b>\n"
                    "It backs up the current files, copies the newer ones, runs the "
                    "test suite, then hot-reloads. Your settings and learned state "
                    "are never touched, and if the tests fail nothing is swapped.\n\n"
                    "This takes about a minute.\n\n"
                    "Tap ✅ Apply to install, ✖ Cancel to go back.")
        return ("<b>🔍 Check for an update?</b>\n"
                "Compares the installed version against the source repo. "
                "Changes nothing.\n\n"
                "Tap ✅ Apply to check, ✖ Cancel to go back.")

    if kind == "cool":                    # panel:cfm:cool:<seconds>
        n = str(arg or "").strip()
        cur = str(st.get("unauthorized_cooldown_s"))
        if not n.isdigit():
            return ("<b>⏱ Cooldown</b>\n"
                    f"current: <b>{_esc(cur)}s</b>\n\n"
                    "How long a stranger waits before the canned reply may repeat. "
                    "Tap a preset below, tap \u270f\ufe0f to type any duration, or "
                    "use <code>!setcooldown 10m</code>.")
        return ("<b>⏱ Cooldown</b>\n"
                f"now: <b>{_esc(cur)}s</b> → <b>{_esc(n)}s</b>\n\n"
                "How long a stranger waits before the canned reply may repeat.\n\n"
                "Tap ✅ Apply to change, ✖ Cancel to go back.")

    if kind == "wl":                      # panel:cfm:wl:<uid>:<level>
        uid, _, lvl = (arg or "").partition(":")
        lvl = _FRIEND_LEVEL_ALIASES.get(lvl, lvl)
        if lvl in _FRIEND_LEVELS:
            cur = _friend_level(uid)
            return ("<b>🛡 Permission level</b>\n"
                    f"<code>{_esc(uid)}</code>: <b>"
                    f"{_FRIEND_LEVEL_LABEL.get(cur, cur)}</b> → <b>"
                    f"{_FRIEND_LEVEL_LABEL[lvl]}</b>\n\n"
                    "<b>talk</b> read-only tools · <b>free</b> everything except "
                    "destructive/credential tools · <b>full</b> no tool gating at "
                    "all.\n\n"
                    "Tap ✅ Apply to change, ✖ Cancel to go back.")
        return ("<b>🛡 Permission level</b>\n"
                "Pick the level on this page.\n\n"
                "<b>talk</b> read-only tools · <b>free</b> everything except "
                "destructive/credential tools · <b>full</b> no tool gating at all.")

    if kind == "gate":                    # panel:cfm:gate:<origin>:<grant|revoke>:<chat>
        word, _, chat = (arg or "").partition(":")
        if word not in ("grant", "revoke") or not chat:
            return ("<b>🛡 Guest tool gate</b>\n"
                    f"mode: <b>"
                    f"{_MODE_LABEL.get(str(st.get('guest_tool_mode') or 'balanced'), 'balanced')}</b>"
                    "\n\nGuests are limited to the read-only tools on this box.\n\n"
                    "Tap ✅ Apply to open the full rules.")
        unlocked = str(chat) in {str(c) for c in (st.get("guest_owner_chats") or [])}
        if word == "grant":
            if unlocked:
                return (f"<b>➕ <code>{_esc(chat)}</code></b>\n"
                        "now: <b>already unlocked for your account</b>\n\n"
                        "Nothing to do tap ✖ Cancel to go back.")
            return ("\n".join([
                f"<b>Unlock <code>{_esc(chat)}</code> for your account?</b>",
                "now: <b>locked</b> → <b>unlocked</b>",
                "",
                "In this chat a guest is not limited to the read-only tools it gets "
                "your owner's access. Treat anyone who can message there as yourself.",
                "",
                "Tap ✅ Apply to unlock, ✖ Cancel to go back."]))
        if not unlocked:
            return (f"<b>➖ <code>{_esc(chat)}</code></b>\n"
                    "now: <b>not unlocked</b>\n\n"
                    "Nothing to do tap ✖ Cancel to go back.")
        return ("\n".join([
            f"<b>Revoke owner access in <code>{_esc(chat)}</code>?</b>",
            "now: <b>unlocked</b> → <b>locked</b>",
            "",
            "A guest there goes back to the read-only tools.",
            "",
            "Tap ✅ Apply to lock, ✖ Cancel to go back."]))

    return "❌ unknown action."


# ---------------------------------------------------------------------------
# v4.0.0 one registry drives every settings page.
#
# The body lists each item with its LIVE value, the keyboard offers exactly
# one button per item, and a test asserts that all 29 DEFAULT_SETTINGS keys
# appear here so "every changeable setting is reachable from the panel, in
# a category, with no command" is a checked property rather than a claim.
#
# kinds: bool | enum | int | text | chan | list | cmd
#   bool  -> confirm screen (panel:cfmok is still the only writer)
#   enum  -> guest-tool-mode cycle behind the same confirm
#   int   -> presets page
#   text  -> prompt-driven wizard (no arguments to type)
#   chan  -> log channel: here / off / by id
#   list  -> count + an edit button
#   cmd   -> a plain button with no value line
_CATS: Dict[str, List[Dict[str, Any]]] = {
    "biz": [
        {"kind": "enum", "key": "biz_mode", "sub": "bizmode",
         "label": "Automation mode"},
        {"kind": "bool", "key": "biz_warn_first", "sub": "bizwarn",
         "label": "First-contact warning"},
        {"kind": "cmd", "key": "biz_lang", "cb": "panel:bizlang",
         "label": "\U0001f310 Language"},
        {"kind": "text", "key": "biz_warn_text", "flow": "bizwarntext",
         "label": "\u2709\ufe0f Warning text"},
        {"kind": "bool", "key": "biz_react", "sub": "bizreact",
         "label": "\U0001f47e React to replies"},
        {"kind": "text", "key": "biz_react_emoji", "flow": "bizemoji",
         "label": "\U0001f3a8 Reaction emoji"},
        {"kind": "bool", "key": "biz_larp", "sub": "bizlarp",
         "label": "\U0001f3ad LARP the owner's voice"},
        {"kind": "text", "key": "biz_persona_path", "flow": "bizpersona",
         "label": "\U0001f3ad Persona file (assistant + mimic)"},
        {"kind": "list", "key": "biz_scope", "cb": "panel:wiz:bizscope",
         "label": "\U0001f3af Scope (all chats when empty)"},
        {"kind": "list", "key": "biz_deny_tools", "cb": "panel:wiz:bizdeny",
         "label": "\U0001f512 Locked tools"},
        {"kind": "bool", "key": "biz_media", "sub": "bizmedia",
         "label": "\U0001f5bc\ufe0f Media in + out"},
        {"kind": "bool", "key": "biz_full_access", "sub": "bizfull",
         "label": "🔓 Full access (tools + admin in this chat)"},
        # --- when ATRA answers automation chats at all ---
        {"kind": "enum", "key": "biz_schedule", "sub": "bizsched",
         "label": "📕 Schedule (always / window / off)"},
        {"kind": "text", "key": "biz_window_start", "flow": "bizwinstart",
         "label": "🕐 Window start (HH:MM)"},
        {"kind": "text", "key": "biz_window_end", "flow": "bizwinend",
         "label": "🕑 Window end (HH:MM)"},
        {"kind": "list", "key": "biz_window_days", "cb": "panel:wiz:bizdays",
         "label": "📅 Window days (all when empty)"},
        {"kind": "enum", "key": "biz_idle_delay_min", "sub": "bizidledelay",
         "unit": "min", "label": "⏳ Reply hold (you answer first)"},
        {"kind": "bool", "key": "biz_sessions_split", "sub": "bizsplit",
         "label": "🧮 Separate automation vs whitelist sessions"},
        {"kind": "cmd", "cb": "panel:bizconn",
         "label": "🔌 Connection status"},
    ],
    "log": [
        {"kind": "chan", "key": "log_channel", "label": "Log channel"},
        {"kind": "bool", "key": "log_owner_messages", "sub": "mirror",
         "label": "👑 Mirror own messages"},
        {"kind": "bool", "key": "log_whitelisted_messages", "sub": "wmsgs",
         "label": "💬 Whitelisted messages"},
        {"kind": "bool", "key": "log_other_messages", "sub": "omsgs",
         "label": "🗨 Other messages"},
        {"kind": "bool", "key": "log_group_mentions", "sub": "mentions",
         "label": "📣 Group mentions"},
        {"kind": "cmd", "label": "🧹 Wipe the log chat", "cb": "wipe:LOG"},
    ],
    "guests": [
        {"kind": "text", "key": "unauthorized_reply", "flow": "unauth",
         "label": "👾 Canned stranger reply"},
        {"kind": "int", "key": "unauthorized_cooldown_s", "page": "cool",
         "label": "⏱ Stranger cooldown"},
        {"kind": "text", "key": "guest_error_reply_en", "flow": "err_en",
         "label": "⚠️ Error reply (EN)"},
        {"kind": "text", "key": "guest_error_reply_fa", "flow": "err_fa",
         "label": "⚠️ Error reply (FA)"},
        {"kind": "text", "key": "guest_locked_reply", "flow": "lockreply",
         "label": "🔒 Locked-session reply"},
        {"kind": "bool", "key": "media_to_guests", "sub": "media",
         "label": "🖼 Media to guests"},
        {"kind": "cmd", "label": "🔐 Guest sessions", "cb": "panel:gslist"},
        {"kind": "cmd", "label": "👤 Who is on the link", "cb": "help:who"},
    ],
    "react": [
        {"kind": "bool", "key": "auto_react", "sub": "react",
         "label": "🔁 React to messages"},
        {"kind": "bool", "key": "react_guests", "sub": "greact",
         "label": "👥 React to guest messages"},
        {"kind": "text", "key": "react_emoji_receive", "flow": "emoji_recv",
         "label": "👀 Received emoji"},
        {"kind": "text", "key": "react_emoji_done", "flow": "emoji_done",
         "label": "✅ Done emoji"},
        {"kind": "text", "key": "react_emoji_error", "flow": "emoji_err",
         "label": "❌ Error emoji"},
    ],
    "tool": [
        {"kind": "bool", "key": "tool_enabled", "sub": "tool",
         "label": "🛠 Admin tool"},
        {"kind": "enum", "key": "guest_tool_mode", "sub": "mode",
         "label": "🛡 Guest tool mode"},
        {"kind": "bool", "key": "guest_owner_full_access", "sub": "owner",
         "label": "🔓 Owner access in unlocked chats"},
        {"kind": "list", "key": "guest_allow_tools", "label": "✏️ Guest allowed tools",
         "cb": "panel:wiz:gateallow"},
        {"kind": "list", "key": "guest_deny_tools", "label": "🚫 Guest denied tools",
         "cb": "panel:wiz:gatedeny"},
        {"kind": "list", "key": "guest_owner_chats", "label": "🔓 Chats unlocked for you",
         "cb": "panel:gate"},
        {"kind": "text", "key": "persona_path", "flow": "persona",
         "label": "🎭 Guest persona file"},
    ],
    "access": [
        {"kind": "text", "key": "owner_id", "flow": "owner", "label": "👑 Owner id"},
        {"kind": "list", "key": "whitelist_perms", "label": "🛡 Friend permission levels",
         "cb": "panel:wl"},
        {"kind": "cmd", "label": "➕ Add a friend", "cb": "panel:wiz:wladd"},
        {"kind": "cmd", "label": "➖ Remove a friend", "cb": "panel:wiz:wldel"},
        {"kind": "cmd", "label": "🎚 Set a friend's level", "cb": "panel:wiz:wlperm"},
        {"kind": "bool", "key": "user_bridge", "sub": "userbridge",
         "label": "\U0001f513 Full unlock (act as you) \u26a0\ufe0f"},
        {"kind": "cmd", "label": "🩺 Auth debug", "cb": "panel:wiz:authdbg"},
    ],
    "system": [
        {"kind": "lock", "key": "update_enabled", "label": "🔒 Update lock",
         "cb": "panel:upd:lock"},
        {"kind": "text", "key": "update_repo", "flow": "uprepo",
         "label": "📦 Update source"},
        {"kind": "text", "key": "update_branch", "flow": "upbranch",
         "label": "🌿 Update branch"},
        {"kind": "text", "key": "update_timeout_s", "flow": "uptimeout",
         "label": "⌛ Update timeout"},
        {"kind": "cmd", "label": "🔍 Check for update", "cb": "panel:upd:check"},
        {"kind": "cmd", "label": "⬆️ Install update", "cb": "panel:upd:apply"},
        {"kind": "cmd", "label": "🛡 Guest mode & gate rules", "cb": "panel:gate"},
        {"kind": "cmd", "label": "📋 Dump settings", "cb": "panel:out:settings"},
        {"kind": "cmd", "label": "🧹 Wipe a session", "cb": "panel:wiz:wipe"},
        {"kind": "cmd", "label": "📨 Send a DM", "cb": "panel:wiz:send"},
    ],
    # "settings" is the flat all-flags page kept for one-tap scanning; its
    # items deliberately overlap the categories above.
    "settings": [
        {"kind": "bool", "key": "auto_react", "sub": "react", "label": "🔁 Reactions"},
        {"kind": "bool", "key": "react_guests", "sub": "greact", "label": "👥 Guest reacts"},
        {"kind": "bool", "key": "media_to_guests", "sub": "media", "label": "🖼 Guest media"},
        {"kind": "bool", "key": "log_owner_messages", "sub": "mirror", "label": "👑 Mirror"},
        {"kind": "bool", "key": "log_group_mentions", "sub": "mentions", "label": "📣 Mentions"},
        {"kind": "bool", "key": "tool_enabled", "sub": "tool", "label": "🛠 Tool"},
        {"kind": "bool", "key": "log_whitelisted_messages", "sub": "wmsgs",
         "label": "💬 Whitelisted msgs"},
        {"kind": "bool", "key": "log_other_messages", "sub": "omsgs", "label": "🗨 Other msgs"},
        {"kind": "int", "key": "unauthorized_cooldown_s", "page": "cool",
         "label": "⏱ Cooldown"},
        {"kind": "text", "key": "unauthorized_reply", "flow": "unauth",
         "label": "👾 Stranger reply"},
        {"kind": "cmd", "label": "👻 Guest texts", "cb": "panel:out:guests"},
        {"kind": "cmd", "label": "📋 Users", "cb": "panel:out:users"},
        {"kind": "cmd", "label": "🔍 Auth debug", "cb": "panel:wiz:authdbg"},
    ],
}

_CAT_LABEL = {"log": "📡 Logging", "guests": "👾 Guests", "react": "🔁 Reactions",
              "tool": "🤖 Tool & gate", "access": "🛡 Access", "system": "🔧 System",
              "settings": "⚙️ All flags", "biz": "💼 Chat Automation",}

# One line under each title, so a category page says what it is for before it
# lists values. `system` doubles as the version readout the panel used to show.
_CAT_BLURB = {
    "log": "Where activity lands the channel, and which messages get mirrored.",
    "guests": "canned reply, cooldown, error texts everything a stranger hears.",
    "react": "Which emoji ATRA drops, and on whose messages.",
    "tool": "What a guest session may run, and which chats you unlocked for yourself.",
    "access": "Who talks to the real brain, and at which tool level.",
    "system": "",
    "settings": "Every flag in one column, for a fast scan before you leave.",
    "biz": "Answer customer chats on your connected account: assistant / mimic / off, first-contact warning, locked tools. Replies follow each message's language.",
}


def _cat_value(it: Dict[str, Any], st: Dict[str, Any]) -> str:
    """The live value shown beside a setting, already HTML-safe."""
    k = it["kind"]
    key = str(it.get("key") or "")
    if k == "bool":
        return "<b>on</b>" if st.get(key) else "<b>off</b>"
    if k == "enum":
        if it.get("unit") == "min":
            return f"<b>{_fmt_min(st.get(key))}</b>"
        # `0 or "balanced"` would render a numeric zero as a mode label, so
        # only a genuinely missing value falls back to the default word.
        raw = st.get(key)
        m = str(raw if raw not in (None, "") else "balanced")
        return f"<b>{_MODE_LABEL.get(m, m)}</b>"
    if k == "int":
        return f"<b>{_esc(str(st.get(key)))}s</b>"
    if k == "chan":
        return f"<code>{_esc(str(st.get('log_channel') or 'off'))}</code>"
    if k == "list":
        v = st.get(key)
        if key == "whitelist_perms":
            return f"<b>{len(v or {})} set</b>"
        return f"<b>{len(v or [])} set</b>"
    if k == "lock":
        return "<b>unlocked</b>" if st.get(key, True) else "<b>🔒 locked</b>"
    if k == "text":
        # show what is actually in effect, not just what settings.json holds:
        # owner_id and update_repo both fall back to config/origin at read time.
        if key == "owner_id":
            v = _owner_id()
            return f"<code>{_esc(str(v or 'unset'))}</code>"
        if key == "update_repo":
            return f"<code>{_esc(_repo_display(_update_settings()['repo']))}</code>"
        if key == "persona_path":
            p = st.get("persona_path")
            return (f"<code>{_esc(str(p))}</code>" if p
                    else "<i>default</i>")
        if key == "biz_persona_path":
            # "" is the BUNDLED persona in effect, not "no persona" the panel
            # said "(empty)" while a full automation/mimic prompt was being sent
            # on every turn, so the owner had no way to tell them apart.
            p = str(st.get("biz_persona_path") or "").strip()
            if p:
                return f"<code>{_esc(p)}</code>"
            return f"<i>bundled ({_esc(', '.join(_BIZ_PERSONAS.values()))})</i>"
        v = st.get(key)
        v = str(v) if v is not None else ""
        v = v.strip() or "(empty)"
        return f"<i>{_esc(v[:80])}</i>"
    return ""


def _cat_body(cat: str, st: Dict[str, Any], note: str = "") -> str:
    """Category page: title, every setting with its live value, how to edit."""
    items = _CATS.get(cat) or []
    lines = [f"<b>{_CAT_LABEL.get(cat, cat)}</b>"]
    if note:
        lines.append(note)
    blurbs = _CAT_BLURB.get(cat) or ""
    if cat == "system":
        cfg = _update_settings()
        blurbs = (f"Installed <b>v{_esc(_plugin_version())}</b> · source "
                  f"<code>{_esc(_repo_display(cfg['repo']))}</code> "
                  f"({_esc(str(cfg['branch']))})")
    if blurbs:
        lines.append(blurbs)
    lines.append("")
    for it in items:
        if it["kind"] == "cmd":
            lines.append(f"• <b>{it['label']}</b>")
            continue
        lines.append(f"• <b>{it['label']}</b>: {_cat_value(it, st)}")
    lines += ["",
              "Tap a button below text settings open a prompt, flags open a "
              "confirm screen. Nothing is typed as a command.",
              "↩ Back returns to the page you came from."]
    return "\n".join(lines)


def _cat_buttons(cat: str, st: Dict[str, Any], chat_id: Optional[str] = None) -> list:
    """(label, callback) pairs for one category one per setting."""
    out: List[Tuple[str, str]] = []
    for it in _CATS.get(cat) or []:
        k = it["kind"]
        if k == "bool":
            mark = "✅" if st.get(it["key"]) else "⏸"
            out.append((f"{mark} {it['label']}", f"panel:tg:{it['sub']}:{cat}"))
        elif k == "enum":
            if it.get("unit") == "min":
                out.append((f"{_fmt_min(st.get(it['key']))} {it['label']}",
                            f"panel:tg:{it['sub']}:{cat}"))
            else:
                _raw = st.get(it["key"])
                m = str(_raw if _raw not in (None, "") else "balanced")
                out.append((f"{_MODE_LABEL.get(m, m)} {it['label']}",
                            f"panel:tg:{it['sub']}:{cat}"))
        elif k == "int":
            out.append((f"✏️ {it['label']} ({st.get(it['key'])}s)",
                        f"panel:{it['page']}"))
        elif k == "text":
            out.append((f"✏️ {it['label']}", f"panel:wiz:{it['flow']}"))
        elif k == "chan":
            if str(st.get("log_channel") or ""):
                out.append(("📍 Log here", "panel:sethere"))
                out.append(("⚔️ Turn log off", "panel:logoff"))
            else:
                out.append(("📍 Log here", "panel:sethere"))
            out.append(("✏️ Set the channel by id", "panel:wiz:logid"))
        elif k == "list":
            out.append((f"{it['label']}", str(it["cb"])))
        elif k == "lock":
            # a state line, not a value: the button says what tapping it does
            if st.get(it["key"], True):
                out.append((f"{it['label']} tap to lock", str(it["cb"])))
            else:
                out.append(("🔓 Updates locked tap to unlock", str(it["cb"])))
        else:  # cmd
            cb = str(it["cb"])
            if cb == "wipe:LOG" and str(st.get("log_channel") or ""):
                out.append((it["label"], f"wipe:{st['log_channel']}"))
            elif cb != "wipe:LOG":
                out.append((it["label"], cb))
    if cat == "system":
        # the System page doubles as the version readout
        out.append((f"v{_plugin_version()}", "panel:upd:check"))
    return out


def _settings_view(st: Dict[str, Any], note: str = "") -> str:
    """All flags in one place each with its own confirm screen."""
    return _cat_body("settings", st, note)


def _wl_view(st: Dict[str, Any], note: str = "") -> str:
    """Whitelist as a list of friend cards one button each, one tap per level."""
    owner = str(_owner_id() or "")
    wl = _read_allow_from()
    lines = [
        "<b>🛡 Whitelist & friends</b> these ids talk to the real brain",
        f"👑 owner: <code>{_esc(owner or 'unset')}</code> (always full access)",
    ]
    friends = [u for u in wl if u != owner]
    if not friends:
        lines += ["", "No friends yet tap ➕ Add a friend; the wizard asks for the id."]
    for uid in friends:
        lvl = _friend_level(uid)
        lines.append(f"• <code>{_esc(uid)}</code> {_FRIEND_LEVEL_LABEL.get(lvl, lvl)} "
                     f" tap to change level or remove")
    lines += [
        "",
        "The core gateway tier (<code>TELEGRAM_ALLOWED_USERS</code>) is synced "
        "automatically someone added here passes the gateway too, no restart.",
        "Command twin: <code>!whitelist add|remove|perms &lt;id&gt; [level]</code>",
    ]
    if note:
        lines.insert(1, note)
    return "\n".join(lines)


def _wlfr_view(uid: str, st: Dict[str, Any]) -> str:
    """One friend's card: current level, what each level means, how to remove."""
    lvl = _friend_level(uid)
    return "\n".join([
        f"<b>👤 Friend <code>{_esc(uid)}</code></b>",
        f"level: <b>{_FRIEND_LEVEL_LABEL.get(lvl, lvl)}</b>",
        "",
        "<b>talk</b> safe read-only tools (web, vision, skills)",
        "<b>free</b> everything except destructive/credential tools",
        "<b>full</b> no tool gating at all",
        "",
        "Tap a level to apply immediately (same code as "
        "<code>!whitelist perms</code>). Removing asks for a confirm.",
    ])


def _wlrm_view(uid: str, st: Dict[str, Any]) -> str:
    """Remove confirm names the consequence instead of just the action."""
    return "\n".join([
        f"🗑 <b>Remove <code>{_esc(uid)}</code> from the whitelist?</b>",
        "",
        "He stops talking to the real brain and falls back to guest rules.",
        "The core gate stays synced his messages would be ignored, not answered.",
        "",
        "✅ Yes, remove · ✖ No, keep",
    ])


def _view_body(view: str, st: Dict[str, Any], note: str = "",
               arg: str = "") -> str:
    """Body for a view name used after Apply/Cancel so every return lands
    back on the right page instead of dumping the owner on the home grid."""
    def _with(body: str) -> str:
        return f"{body}\n\n{note}" if note else body
    if view in _CATS:
        # v4.0.0 categories: log, guests, react, tool, access, system, settings
        return _cat_body(view, st, note)
    if view == "wl":
        return _wl_view(st, note)
    if view == "status":
        return _help_view("status", st)
    if view == "sessions":
        return _help_view("sessions", st)
    if view == "actions":
        return _actions_view(st, note)
    if view == "safeguard":
        return _with(_gate_view(st))
    if view == "gsess":
        return _with(_gsess_view(st))
    if view == "cool":
        return _with(f"<b>⏱ Cooldown</b> how long a stranger waits before the "
                     f"canned reply may repeat\ncurrent: "
                     f"<b>{_fmt_min((st.get('unauthorized_cooldown_s') or 0) / 60)}</b>"
                     f" <i>({st.get('unauthorized_cooldown_s')}s)</i>\n"
                     "Tap a preset, tap \u270f\ufe0f to type a duration like "
                     "<code>10m</code>, or use <code>!setcooldown 10m</code>")
    if view == "cfm":
        _o, _, _kp = (arg or "").partition(":")
        _k, _, _p = _kp.partition(":")
        return _with(_cfm_view(_k, _p, st))
    if view == "biz":
        return _cat_body("biz", st, note)
    if view == "bizlang":
        return _bizlang_view(st, note)
    if view == "bizconn":
        return _bizconn_view(st, note)
    if view in ("full", "bot", "who", "status", "sessions", "log", "guests",
                "access", "system"):
        # help pages a Back pop can land on render the real section, never
        # the console, so the body always matches the keyboard.
        return _help_view(view, st)
    return _panel_text(st, note)


def _help_keyboard(view: str = "panel", st: Optional[Dict[str, Any]] = None,
                   chat_id: Optional[str] = None, arg: str = "") -> list:
    """Per-tab button sets: sections at home, contextual actions inside a tab.

    `arg` carries the target for card views (wlfr/wlrm uid, "sub:origin" for a
    confirm screen) the view string stays a plain label for routing/labels."""
    from telegram import InlineKeyboardButton as B
    st = st or settings()
    log = str(st.get("log_channel") or "")
    rows: list = []

    def _mark(key: str) -> str:
        return "\u2705" if st.get(key) else "\u23f8"

    def add(*pairs) -> None:
        if pairs:
            rows.append([B(lbl, callback_data=f"{_CB_PREFIX}{path}") for lbl, path in pairs])

    if view in _CATS:
        # v4.0.0 category page one button per setting, values in the body.
        _pairs = _cat_buttons(view, st, chat_id)
        for _ci in range(0, len(_pairs), 2):
            add(*_pairs[_ci:_ci + 2])
    elif view == "bizlang":
        _langs = bizauto.available_languages() if bizauto else ["auto", "en"]
        _cur = str(st.get("biz_lang") or "auto")
        for _ci in range(0, len(_langs), 2):
            _pr = []
            for _l in _langs[_ci:_ci + 2]:
                _lbl = (("✅ " if _l == _cur else "") +
                        (bizauto.language_label(_l) if bizauto else _l))
                _pr.append((_lbl, f"panel:bizlang:{_l}"))
            add(*_pr)
        add(("✉️ Custom warning text", "panel:wiz:bizwarntext"),
            ("🔌 Connection status", "panel:bizconn"))
    elif view == "bizconn":
        add(("↩ Back to Chat Automation", "panel:view:biz"))
    elif view == "panel":
        # Home: a status board plus the six categories, everything one tap away.
        add(("📡 Log", "help:log"), ("👾 Guests", "help:guests"))
        add(("💼 Chat Automation", "help:biz"), ("🌐 Language", "panel:bizlang"))
        add(("🔁 Reactions", "help:react"), ("🤖 Tool", "help:tool"))
        add(("🛡 Access", "help:access"), ("🔧 System", "help:system"))
        add(("⚙️ Settings", "panel:settings"), ("🛡 Whitelist", "panel:wl"))
        add(("⚡ Actions do things", "panel:actions"),
            ("📜 Full help", "help:full"))
        add(("🧹 Sessions", "help:sessions"))
    elif view == "wl":
        _wowner = str(_owner_id() or "")
        for _wuid in _read_allow_from():
            if _wuid == _wowner:
                continue
            _wlvl = _friend_level(_wuid)
            add((f"{_FRIEND_LEVEL_LABEL.get(_wlvl, _wlvl)} · {_wuid}",
                 f"panel:wlfr:{_wuid}"))
        add(("➕ Add a friend wizard", "panel:wiz:wladd"))
        add(("📋 Users", "panel:out:users"), ("🛡 Whitelist text", "panel:out:whitelist"))
        add(("🔍 Auth debug", "panel:wiz:authdbg"))
    elif view == "wlfr":
        _frlvl = _friend_level(arg or "")
        for _fl in _FRIEND_LEVELS:
            add(((f"✅ " if _fl == _frlvl else "") + _FRIEND_LEVEL_LABEL[_fl],
                 f"panel:wllvl:{arg}:{_fl}"))
        add((f"🗑 Remove {arg}", f"panel:wlrm:{arg}"))
    elif view == "wlrm":
        add((f"🗑 Yes, remove {arg}", f"panel:wlrm2:{arg}"),
            ("✖ No, keep", f"panel:wlfr:{arg}"))
    elif view == "tg":
        _tsub, _, _torigin = (arg or "").partition(":")
        add(("✅ Apply", f"panel:tgy:{_tsub}:{_torigin or 'settings'}"),
            ("✖ Cancel", f"panel:view:{_torigin or 'settings'}"))
        _df = _DUR_FLOWS.get(_tsub)
        if _df:
            add(("✏️ Type a value", f"panel:wiz:{_df}"))
    elif view == "actions":
        add(("\U0001f4e1 Log here log into THIS chat", "panel:sethere"))
        add(("\U0001f6e1 Whitelist add", "panel:wiz:wladd"),
            ("\U0001f6e1 Whitelist remove", "panel:wiz:wldel"))
        add(("\U0001f6e1 Friend permission level", "panel:wiz:wlperm"))
        add(("\U0001f510 Guest sessions open / lock", "panel:gslist"))
        add(("\U0001f4cb Send a DM as the bot", "panel:wiz:send"))
        add(("\U0001f9f9 Wipe a session", "panel:wiz:wipe"))
        add(("\u23f1 Cooldown", "panel:cool"))
        add(("\U0001f47b Stranger reply text", "panel:wiz:unauth"))
        add(("\U0001fa7a Auth debug", "panel:wiz:authdbg"))
        add(("\U0001f6e1 Safeguards & tool gate", "panel:gate"))
        add(("\U0001f527 System + updates", "help:system"))
    elif view == "safeguard":
        add((f"\U0001f6e1 Mode: {_MODE_LABEL.get(str(st.get('guest_tool_mode') or 'balanced'), 'balanced')}",
             "panel:tg:mode:safeguard"))
        add(("\U0001f513 Owner access" + (" ON" if st.get("guest_owner_full_access") else " OFF"),
             "panel:tg:owner:safeguard"))
        add(("\u270f\ufe0f Allow a tool", "panel:wiz:gateallow"),
            ("\U0001f6ab Deny a tool", "panel:wiz:gatedeny"))
        add(("\U0001f6e1 Friend levels", "panel:wiz:wlperm"),
            ("\U0001f4cb Full rules", "panel:gate:list"))
    elif view == "gsess":
        recs = _guest_sessions()
        for _uid, _rec in list(sorted(recs.items()))[:8]:
            _state = str(_rec.get("state") or "default")
            if _state == "locked":
                add((f"\U0001f513 Open {_uid}", f"panel:gs:open:{_uid}"))
            else:
                add((f"\U0001f514 Lock {_uid}", f"panel:gs:lock:{_uid}"))
            if _state != "default":
                add((f"\u25ab\ufe0f Default {_uid}", f"panel:gs:reset:{_uid}"))
        add(("\u2795 Open a new session", "panel:wiz:gsopen"))
        add(("\U0001f512 Lock by id", "panel:wiz:gslock"))
    elif view == "cfm":
        # arg is "<origin>:<kind>:<payload>". Apply is the ONLY writer, and it
        # is rendered here rather than per-kind so no button can skip it.
        _o, _, _kp = (arg or "").partition(":")
        _k, _, _p = _kp.partition(":")
        rows.insert(0, [
            B("✅ Apply", callback_data=f"{_CB_PREFIX}panel:cfmok:{_k}:{_o}:{_p}"),
            B("✖ Cancel", callback_data=f"{_CB_PREFIX}panel:view:{_o or 'panel'}")])
    elif view == "wiz":
        add(("✖ Cancel the wizard", "panel:wizcancel"))
    elif view == "status":
        add(("⚙️ Settings", "panel:out:settings"), ("📋 Users", "panel:out:users"))
        add((f"🛠 Tool {_mark('tool_enabled')}", "panel:tg:tool:status"),
            (f"👑 Mirror {_mark('log_owner_messages')}", "panel:tg:mirror:status"))
    elif view == "sessions":
        # One 🛑 per LIVE turn (index into _SESS_SNAP a session key is
        # longer than Telegram's 64-byte callback_data) and one 🧹 per open
        # stored session. The body must render first: it is what refreshes
        # both snapshots the buttons point at.
        for _i, _k in enumerate(_SESS_SNAP[:8]):
            _lane = "automation" if _BIZ_THREAD_PREFIX in _k else "chat"
            _who = _esc(_k.rsplit(":", 1)[-1][:20])
            add((f"🛑 Stop {_lane} {_who}", f"sessstop:{_i}"))
        for _c in _SESS_WIPES[:8]:
            add((f"🧹 Wipe {_esc(_c)}", f"wipe:{_c}"))
        if chat_id and str(chat_id) not in _SESS_WIPES:
            add((f"🧹 Wipe this chat", f"wipe:{chat_id}"))
        add(("🔄 Refresh", "help:sessions"), ("⚙️ Settings", "panel:out:settings"))
    elif view == "bot":
        add((f"🛠 Tool {_mark('tool_enabled')}", "panel:tg:tool:bot"),
            (f"👑 Mirror {_mark('log_owner_messages')}", "panel:tg:mirror:bot"))
        add(("⚙️ Settings", "panel:out:settings"), ("📋 Users", "panel:out:users"))
    elif view == "out":
        add(("\U0001f4cb Users", "panel:out:users"), ("\u2699\ufe0f Settings", "panel:out:settings"),
            ("\U0001f6e1 Whitelist", "panel:out:whitelist"))
    elif view == "full":
        add(("\u2139\ufe0f Status", "help:status"), ("\U0001f4e1 Log", "help:log"),
            ("\U0001f6e1 Access", "help:access"))
        add(("\U0001f9f9 Sessions", "help:sessions"), ("\U0001f47e Guests", "help:guests"),
            ("\U0001f916 Bot", "help:bot"))

    if view == "wlfr":
        rows.insert(0, [B("⬅️ Whitelist", callback_data=f"{_CB_PREFIX}panel:wl")])
        add(("📜 Full help", "help:full"))
    elif view == "wlrm":
        rows.insert(0, [B("⬅️ Friend", callback_data=f"{_CB_PREFIX}panel:wlfr:{arg}")])
        add(("📜 Full help", "help:full"))
    elif view == "tg":
        _corigin = (arg or "").partition(":")[2] or "settings"
        rows.insert(0, [B("⬅️ Back", callback_data=f"{_CB_PREFIX}panel:view:{_corigin}")])
        add(("📜 Full help", "help:full"))
    elif view != "panel":
        # pops one level of history from a category it lands on the console,
        # from a sub-page it lands on the category you came from.
        rows.insert(0, [B("⬅️ Back", callback_data=f"{_CB_PREFIX}panel:back")])
        add(("📜 Full help", "help:full"))
    if view == "cool":
        add(("✏️ Type a value (10m, 30s…)", "panel:wiz:cooldown"))
        add(*[(f"{n}s", f"panel:cool:{n}") for n in (0, 60, 300, 3600)])
    if chat_id and view in ("panel", "sessions", "access") \
            and (view != "sessions" or str(chat_id) not in _SESS_WIPES):
        # In the sessions view every open chat already got its own 🧹 above 
        # repeating it there just doubles the button. Everywhere else this
        # stays: it is the panel's one-click "wipe where I'm standing".
        add((f"🧹 Wipe this chat", f"wipe:{chat_id}"))
    return rows


_MODE_LABEL = {"strict": "🔒 strict", "balanced": "⚖️ balanced", "open": "🔓 open", "assistant": "🤖 assistant", "mimic": "🎭 mimic", "off": "⏸ off",}

_VIEW_LABEL = {"full": "📜 Full help", "panel": "🧩 Console", "out": "📋 Output",
               "status": "ℹ️ Status", "log": "📡 Log", "access": "🛡 Access",
               "sessions": "🧹 Sessions", "bot": "🤖 Bot", "guests": "👾 Guests",
               "react": "🔁 Reactions", "tool": "🤖 Tool & gate",
               "cool": "⏱ Cooldown", "system": "🔧 System", "safeguard": "🛡 Safeguards",
               "actions": "⚡ Actions", "gsess": "🔐 Guest sessions", "wiz": "📝 Wizard",
               "settings": "⚙️ Settings", "wl": "🛡 Whitelist", "wlfr": "👤 Friend",
               "wlrm": "🗑 Remove", "tg": "✅ Confirm", "cfm": "✅ Confirm", "biz": "💼 Chat Automation", "bizlang": "🌐 Language", "bizconn": "🔌 Connection",}

# One confirm vocabulary: every mutating button first renders
# `panel:cfm:<kind>:...` (read-only, states now → next), and only the Apply
# button writes `panel:tgy` for settings flags, `panel:upd` for updates,
# `panel:cfmok` for the rest. Nothing else in the panel writes a setting.
_CFM_KINDS = {"tg", "gs", "log", "upd", "sup", "cool", "wl", "gate",
              "bizlang"}

# v4.0.0 Back returns to the page you came from, not always to the console.
# A per-panel-message history: navigate truncates/appends, `panel:back` pops.
# Confirm screens and the wizard are transient, so they never enter the stack.
_NAV: Dict[str, List[str]] = {}
_NAV_MAX = 16
_NAV_TRANSIENT = frozenset({"cfm", "tg", "wiz"})


def _nav_key(q: Any) -> str:
    """One history per panel message two open panels do not share a stack."""
    msg = getattr(q, "message", None)
    return f"{_msg_chat_id(msg)}:{getattr(msg, 'message_id', None) or getattr(msg, 'id', None)}"


def _nav_push(key: str, view: str) -> None:
    """Record a landing. Revisiting an ancestor truncates back to it, so
    leaving a confirm screen lands exactly where the tap came from."""
    if view in _NAV_TRANSIENT or not view:
        return
    st = _NAV.setdefault(key, ["panel"])
    if st and st[-1] == view:
        return
    if view in st:
        del st[st.index(view) + 1:]
    else:
        st.append(view)
        del st[:-_NAV_MAX]


def _nav_back(key: str) -> str:
    """Pop one level. Empty or single-entry history falls back to home."""
    st = _NAV.get(key) or ["panel"]
    if len(st) > 1:
        st.pop()
        _NAV[key] = st
        return st[-1]
    return "panel"


def _msg_chat_id(msg: Any) -> Optional[str]:
    cid = getattr(msg, "chat_id", None) or getattr(getattr(msg, "chat", None), "id", None)
    return str(cid) if cid is not None else None


async def _panel_edit(q: Any, body: str, view: str, st: Dict[str, Any],
                      arg: str = "") -> str:
    """Swap a panel message in place.

    Returns "ok" when Telegram accepted the edit, "same" when the tap would not change
    anything (Telegram 400s on an identical edit that is a no-op, not a failure), and
    "failed" for anything real (logged so it is diagnosable instead of silent).
    """
    from telegram import InlineKeyboardMarkup
    msg = q.message
    mk = InlineKeyboardMarkup(_help_keyboard(view, st, chat_id=_msg_chat_id(msg), arg=arg))
    try:
        # PTB 22 renamed Message.edit_message_text -> edit_text; older builds keep the old name.
        edit = getattr(msg, "edit_text", None) or getattr(msg, "edit_message_text", None)
        if edit is not None:
            await edit(body[:4000], parse_mode="HTML", reply_markup=mk)
        else:
            bot = getattr(_ADAPTER.get("adapter"), "_bot", None)
            chat_id = getattr(msg, "chat_id", None) or getattr(getattr(msg, "chat", None), "id", None)
            message_id = getattr(msg, "message_id", None) or getattr(msg, "id", None)
            if bot is None or chat_id is None or message_id is None:
                raise RuntimeError(f"no edit path for {type(msg).__name__}")
            await bot.edit_message_text(text=body[:4000], chat_id=chat_id, message_id=message_id,
                                        parse_mode="HTML", reply_markup=mk)
        return "ok"
    except Exception as e:
        if "not modified" in str(e).lower():
            return "same"
        logger.warning("[TGAhermes] panel edit failed (%s): %s",
                       type(msg).__name__, e, exc_info=True)
        return "failed"


def _html_plain(text: str) -> str:
    """Strip console markup so a plain-text send still reads correctly."""
    return html.unescape(re.sub(r"</?[a-zA-Z][^>]*>", "", text))


async def _reply_to_event(adapter: Any, chat_id: Any, text: str, buttons: Optional[list] = None) -> None:
    """Console replies carry <b>/<code> markup → send as HTML; never leak raw tags."""
    cid = str(chat_id)
    bot = getattr(adapter, "_bot", None)
    if bot is not None:
        try:
            from telegram import InlineKeyboardMarkup
            await bot.send_message(chat_id=cid, text=text[:4000], parse_mode="HTML",
                                   reply_markup=InlineKeyboardMarkup(buttons) if buttons else None)
            return
        except Exception:
            logger.debug("[TGAhermes] HTML console reply failed; plain fallback",
                         exc_info=True)
    try:
        await adapter.send(cid, _html_plain(text))
    except Exception:
        logger.warning("[TGAhermes] console reply failed", exc_info=True)


async def _whitelist_cmd(adapter: Any, arg: str) -> str:
    bits = arg.strip().split(maxsplit=1)
    sub = bits[0].lower() if bits else "list"
    val = bits[1].strip() if len(bits) > 1 else ""
    owner = _owner_id(adapter)
    if sub == "list":
        ids = _read_allow_from()
        rows = [
            f"<code>{_esc(i)}</code>" + (" 👑 owner" if i == owner else
                                        f" · {_FRIEND_LEVEL_LABEL.get(_friend_level(i), '')}")
            for i in ids]
        return ("🛡 Whitelist (<code>telegram.extra.allow_from</code>):\n"
                + ("\n".join(rows) or "(empty)")
                + "\n\n💬 talk = safe tools only · 🛡 gated = guest rules (default) · "
                  "🔓 full = no gating"
                  "\nChange a level: <code>!whitelist perms &lt;user_id&gt; talk|gate|full</code>")
    if sub in ("perms", "level"):
        bits2 = val.split(maxsplit=1)
        if len(bits2) != 2:
            return ("Usage: <code>!whitelist perms &lt;user_id&gt; talk|free|full</code>\n"
                    "💬 talk = web/vision only · 🛠 free = everything except "
                    "destructive tools · 🔓 full = everything")
        target, lvl = bits2[0], bits2[1].strip().lower()
        lvl = _FRIEND_LEVEL_ALIASES.get(lvl, lvl)
        if lvl not in _FRIEND_LEVELS:
            return f"Unknown level <code>{_esc(lvl)}</code> use talk, free or full."
        if target != owner and target not in _read_allow_from():
            return f"<code>{_esc(target)}</code> is not whitelisted add them first."
        perms = dict(settings().get("whitelist_perms") or {})
        perms[target] = lvl
        save_settings({"whitelist_perms": perms})
        await _log("🛡 Whitelist permission",
                   f"<code>{_esc(target)}</code> → <b>{_FRIEND_LEVEL_LABEL.get(lvl, lvl)}</b>")
        return (f"✅ <code>{_esc(target)}</code> → {_FRIEND_LEVEL_LABEL.get(lvl, lvl)}\n"
                "Applies to their own DM session from the next message.")
    if sub in ("add", "remove"):
        if not val:
            return f"Usage: <code>!whitelist {sub} &lt;user_id&gt;</code>"
        target = val
        if target.startswith("@"):
            try:
                bot = getattr(adapter, "_bot", None)
                chat = await bot.get_chat(target)
                target = str(getattr(chat, "id", "") or "")
            except Exception:
                target = ""
            if not target:
                return f"⚠️ could not resolve {_esc(val)} send the numeric user id instead"
        target = str(target).strip()
        if target == owner and sub == "remove":
            return "Refusing to remove the owner the owner is always authorized; use <code>!setowner</code> to change ownership."
        ids = _read_allow_from()
        if sub == "add":
            if target in ids:
                return f"<code>{_esc(target)}</code> is already whitelisted."
            new = ids + [target]
        else:
            if target not in ids:
                return f"<code>{_esc(target)}</code> is not whitelisted."
            new = [x for x in ids if x != target]
        if not _write_allow_from(new):
            return "❌ could not write config see the gateway log"
        verb = "Added" if sub == "add" else "Removed"
        await _log("🛡 Whitelist updated",
                   f"<b>{verb}:</b> <code>{_esc(target)}</code>\n"
                   f"Now: <code>{_esc(_allow_csv(new))}</code>")
        out = (f"✅ {verb.lower()} <code>{_esc(target)}</code>\n"
               f"Whitelist: <code>{_esc(_allow_csv(new))}</code>")
        if sub == "add":
            out += (f"\nLevel: {_FRIEND_LEVEL_LABEL.get(_friend_level(target), '')} "
                    f"change with <code>!whitelist perms {_esc(target)} talk|gate|full</code>")
        return out
    return ("Usage: <code>!whitelist list|add|remove|perms "
            "&lt;user_id&gt; [talk|gate|full]</code>")


async def _gs_cmd(adapter: Any, arg: str) -> str:
    """!gs list|open|lock|reset <user_id> guest session control (panel twin)."""
    bits = arg.strip().split(maxsplit=1)
    sub = bits[0].lower() if bits else "list"
    val = bits[1].strip() if len(bits) > 1 else ""
    states = {"open": "open", "unlock": "open", "lock": "locked", "reset": "default"}
    marks = {"open": "🔓 open", "locked": "🔒 locked", "default": "▫️ default rules"}
    if sub == "list":
        recs = _guest_sessions()
        if not recs:
            return ("🔐 No guest sessions recorded yet.\n"
                    "One appears here automatically the first time someone talks on the "
                    "guest link or open one yourself with "
                    "<code>!gs open &lt;user_id&gt;</code>.")
        rows = []
        for uid, rec in sorted(recs.items()):
            state = str(rec.get("state") or "default")
            mark = marks.get(state, state)
            who = "opened by you" if rec.get("created") == "owner" else "auto-created"
            rows.append(f"<code>{_esc(uid)}</code> · {mark} · {who}")
        return ("🔐 Guest sessions:\n" + "\n".join(rows)
                + "\n\n🔓 open = may talk without replying to ATRA · "
                  "🔒 locked = only the locked reply · ▫️ default = stranger rules")
    if sub in states:
        if not val:
            return f"Usage: <code>!gs {sub} &lt;user_id&gt;</code>"
        # guest ids are numeric (the id they carry on the link); @names don't resolve here
        uid = val[1:] if val.startswith("@") else val
        _set_guest_session(uid, states[sub], by="owner")
        mark = marks.get(states[sub], states[sub])
        await _log("🔐 Guest session", f"<code>{_esc(uid)}</code> → <b>{mark}</b>")
        return f"✅ <code>{_esc(uid)}</code> → {mark}\nList: <code>!gs list</code>"
    return "Usage: <code>!gs list|open|lock|reset &lt;user_id&gt;</code>"


async def _gate_cmd(arg: str) -> str:
    """!gate show|allow|deny the safeguard lists, editable without the panel."""
    bits = arg.strip().split(maxsplit=1)
    sub = bits[0].lower() if bits else "show"
    val = bits[1].strip() if len(bits) > 1 else ""
    st = settings()
    if sub in ("show", "list"):
        return _gate_view(st)
    if sub in ("allow", "deny"):
        key = "guest_allow_tools" if sub == "allow" else "guest_deny_tools"
        remove = val.startswith("-")
        tool = val.lstrip("+-").strip()
        if not tool:
            cur = ", ".join(str(t) for t in (st.get(key) or [])) or "none"
            return (f"Usage: <code>!gate {sub} &lt;tool&gt;</code> prefix "
                    f"<code>-</code> to remove\nCurrent {sub} list: <code>{_esc(cur)}</code>")
        cur = [str(t) for t in (st.get(key) or [])]
        if remove:
            if tool not in cur:
                return f"<code>{_esc(tool)}</code> is not in the {sub} list."
            cur = [t for t in cur if t != tool]
            verb = "removed from"
        else:
            if tool in cur:
                return f"<code>{_esc(tool)}</code> is already in the {sub} list."
            cur = cur + [tool]
            verb = "added to"
        save_settings({key: cur})
        await _log("🛡 Gate list",
                   f"<code>{_esc(tool)}</code> {verb} {sub} → <code>{_esc(', '.join(cur))}</code>")
        return f"✅ <code>{_esc(tool)}</code> {verb} the {sub} list.\n{_gate_view(settings())}"
    return "Usage: <code>!gate show|allow|deny [tool]</code>"


def _auth_debug(uid: str = "") -> str:
    """Whose config wins where: file vs the live prefilter snapshot vs the plugin.

    Four tiers, checked in this order by the real gateway: config file (panel
    writes here), live adapter snapshot (the prefilter), core gate env
    (TELEGRAM_ALLOWED_USERS the authz mixin reads it first and, while it is
    non-empty, never consults the plugin's list), and the plugin's own route
    decision. The gate line is the tier that dropped whitelisted friends as
    "unrecognized" before 2026-10-02; it is shown so a divergence is visible.
    """
    file_ids = _read_allow_from()
    raw = _adapter_allow_raw()
    if raw is None:
        snap_ids = None
        snap = "(not set the prefilter falls through to runner auth)"
    else:
        if isinstance(raw, (list, tuple, set)):
            snap_ids = [str(x).strip() for x in raw]
        else:
            snap_ids = [x.strip() for x in str(raw).split(",") if x.strip()]
        snap = ", ".join(snap_ids) or "(empty)"
    gate_ids = [x.strip() for x in _gate_allow_raw().split(",") if x.strip()]
    owner = str(_owner_id() or "")
    lines = [
        "<b>🩺 Auth debug</b>",
        f"<b>config file allow_from:</b> <code>{_esc(','.join(file_ids)) or '(empty)'}</code>",
        f"<b>live adapter snapshot:</b> <code>{_esc(snap)}</code>",
        ("  <i>← this is what the core prefilter checks; a write to the file does "
         "not change it until it is synced or the gateway restarts</i>"
         if raw is not None else ""),
        f"<b>core gate env:</b> <code>{_esc(','.join(gate_ids)) or '(empty)'}</code>",
        ("  <i>← TELEGRAM_ALLOWED_USERS: the gateway checks this first; while it is "
         "non-empty the plugin's list is never consulted</i>"),
        f"<b>owner:</b> <code>{_esc(owner or 'unset')}</code>",
    ]

    def _verdict(ids: Optional[List[str]]) -> str:
        if ids is None:
            return "falls through to runner auth"
        if uid == owner and owner or uid in ids or "*" in ids:
            return "PASS"
        return "BLOCK"

    if uid:
        plugin_ok = _is_authorized_user(uid, owner)
        gate_v = _verdict(gate_ids) if gate_ids else _verdict(snap_ids)
        lines += [
            "",
            f"<b>for <code>{_esc(uid)}</code>:</b>",
            f"  prefilter with file: <b>{_verdict(file_ids)}</b> · "
            f"with live snapshot: <b>{_verdict(snap_ids)}</b> · "
            f"core gate env: <b>{gate_v}</b>",
            "  plugin route: "
            + ("owner/whitelisted → real brain" if plugin_ok else "stranger → canned reply")
            + (f" · level {_FRIEND_LEVEL_LABEL.get(_friend_level(uid), _friend_level(uid))}"
               if plugin_ok and uid != owner else ""),
        ]
        rec = (_load_state().get("users") or {}).get(uid) or {}
        cool = max(0, int(float(rec.get("canned_until", 0) or 0) - time.time()))
        lines.append(f"  canned cooldown left: {cool}s")
        if file_ids and snap_ids is not None and set(file_ids) != set(snap_ids):
            lines += ["", "⚠️ file and live snapshot DISAGREE a whitelist write "
                      "(panel → ⚡ Actions) syncs them, or restart the gateway."]
    return "\n".join(x for x in lines if x)


def _session_key_for(adapter: Any, chat_id: Any) -> Optional[str]:
    """Best-effort gateway session key for this chat (owner rail: stop).

    Mirrors how the handle derives keys: walks the adapter's live sessions and
    matches the trailing chat segment. None when nothing matches the caller
    then answers "nothing was running" instead of cancelling blindly.
    """
    try:
        active = getattr(adapter, "_active_sessions", None) or {}
        keys = list(active.keys())
    except Exception:
        keys = []
    if not keys or chat_id is None:
        return None
    want = str(chat_id)
    for k in keys:
        ks = str(k)
        if ks == want or ks.endswith(":" + want) or ks.endswith("/" + want):
            return ks
    for k in keys:
        if want and want in str(k):
            return str(k)
    return None


# Live session keys as the panel last rendered them: Telegram caps callback_data
# at 64 bytes and a real session key ("agent:main:telegram:group:<id>:<uid>")
# blows past that, so a stop button carries only an index into this snapshot.
_SESS_SNAP: List[str] = []
_SESS_WIPES: List[str] = []   # chat ids of open stored sessions, for 🧹 buttons

# Chat labels for the list. A bare numeric id is unreadable when a dozen
# sessions are stacked in one message, so each row reads `name (id)` the
# @handle comes from getChat, but a panel render must not turn into a dozen
# uncached network round-trips inside a synchronous callback: one lookup per
# chat, cached, capped per render and hard-stopped at the render deadline.
_LABEL_CACHE: Dict[str, Tuple[str, float]] = {}   # chat id -> (resolved name, ts)
_LABEL_TTL = 6 * 3600.0
_LABEL_MISS_TTL = 300.0                            # a failed/empty lookup retries soon
_LABEL_USES: List[int] = [0]
_LABEL_DEADLINE: List[float] = [0.0]
_LABEL_MAX_LOOKUPS = 4


def _session_label(adapter: Any, chat_id: Any, display_name: Any = None,
                   chat_type: Any = None) -> str:
    """`@username` for a person, the group title for a group always with the
    raw id in parentheses: `Some Name (uid)`, `Group Title (chat_id)`.

    Groups keep their stored title (the owner recognises it instantly and it
    needs no API call); DMs prefer the @handle, fetched at most
    `_LABEL_MAX_LOOKUPS` times per render and only while the render still has
    time left. Falls back to the stored name, then to the id.
    """
    cid = str(chat_id or "").strip()
    if not cid or cid == "None":
        return ""
    name = str(display_name or "").strip()
    ctype = str(chat_type or "").lower()
    is_numeric = bool(cid.lstrip("-").isdigit())
    is_group = ctype in ("group", "supergroup", "channel") or (
        is_numeric and cid.startswith("-"))
    now = time.time()
    cached = _LABEL_CACHE.get(cid)
    fresh = False
    if cached:
        ttl = _LABEL_TTL if cached[0] else _LABEL_MISS_TTL
        if now - cached[1] < ttl:
            name = cached[0] or name
            fresh = True
    if (not fresh and not is_group and is_numeric
            and _LABEL_USES[0] < _LABEL_MAX_LOOKUPS
            and now < _LABEL_DEADLINE[0]):
        _LABEL_USES[0] += 1
        obj = None
        try:
            if adapter is not None:
                obj = adapter._bot.get_chat(int(cid))
        except Exception:
            obj = None                        # cache the miss below, keep the name
        resolved = ""
        if obj is not None:
            uname = str(getattr(obj, "username", "") or "").strip()
            first = str(getattr(obj, "first_name", "") or "").strip()
            resolved = f"@{uname}" if uname else first
        _LABEL_CACHE[cid] = (resolved, now)
        name = resolved or name
    name = name or cid
    return f"{name} ({cid})" if name != cid else cid


def _sessions_body(adapter: Any = None, st: Optional[Dict[str, Any]] = None) -> str:
    """Owner view: live turns + per-chat session rows (guest / friend / biz).

    Sync on purpose the panel body builder is sync, and this needs to render
    there. Guest chatter lands in isolated one-person sessions; whitelisted
    friends run their own DM session; automation turns run a split bizauto
    session with the SAME human three lanes, listed separately so you can see
    at a glance which session type is actually busy.
    """
    global _SESS_SNAP, _SESS_WIPES
    _SESS_WIPES = []
    _LABEL_USES[0] = 0
    _LABEL_DEADLINE[0] = time.time() + 0.9
    if adapter is None:
        adapter = _ADAPTER.get("adapter")
    lines: List[str] = ["\U0001f9f5 <b>Sessions</b>",
                        "<b>Live now</b>"]
    live = 0
    snap: List[str] = []
    try:
        active = getattr(adapter, "_active_sessions", None) or {}
        tasks = getattr(adapter, "_session_tasks", None) or {}
        for k in list(active.keys()):
            live += 1
            ks = str(k)
            snap.append(ks)
            lane = "automation" if _BIZ_THREAD_PREFIX in ks else "chat"
            busy = "busy" if k in tasks else "finishing"
            _who = ks.rsplit(":", 1)[-1]
            lines.append(f"\u2022 <b>{lane}</b> {_esc(_who)} \u2014 {busy}")
    except Exception:
        lines.append("\u2022 <i>live list unavailable</i>")
    _SESS_SNAP = snap
    if not live:
        lines.append("<i>nothing running right now</i>")
    try:
        db = _hermes_home() / "state.db"
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            # The sessions table has no updated_at it is last_activity_at,
            # and the old column name made this whole query throw, so the view
            # always read "no stored sessions yet" no matter what was stored.
            rows = con.execute(
                "SELECT source, chat_id, display_name, chat_type, id, "
                "last_activity_at, ended_at "
                "FROM sessions ORDER BY last_activity_at DESC LIMIT 12").fetchall()
        finally:
            con.close()
    except Exception:
        rows = []
    if rows:
        lines.append("")
        lines.append("<b>Recent sessions</b> (type \u2014 name (id) \u2014 session id):")
        _owner = str(_owner_id() or "")
        _wl = {str(u) for u in _read_allow_from()}
        for src, cid, disp, ctype, sid, upd, ended in rows:
            tag = str(cid or "")
            sid_s = str(sid or "")
            # Label by WHO owns the lane, not just membership: the owner's own
            # session used to read as "friend" (they are in their own
            # allowlist), and a business customer read as "other".
            if _BIZ_THREAD_PREFIX in sid_s or _biz_chat_known(tag):
                lane = "automation" if _BIZ_THREAD_PREFIX in sid_s else "business"
            elif not tag or tag == "None":
                # cron/internal runs persist a NULL chat_id they are not
                # chats, so label them instead of implying an unknown person.
                lane = "cron" if str(sid_s).startswith("cron_") else "internal"
            elif tag == _owner:
                lane = "owner"
            elif tag.startswith(GUEST_CHAT_PREFIX):
                lane = "guest"
            elif tag in _wl:
                lane = "friend"
            elif tag.startswith("-"):
                lane = "group"
            else:
                lane = "dm"
            age = ""
            try:
                secs = max(0, int(time.time()) - int(upd or 0))
                age = f" · idle {_esc(_humanize(secs))}"
            except Exception:
                pass
            state = "open" if ended is None else "ended"
            if state == "open" and cid is not None and str(cid).strip() \
                    and str(cid) != "None" and str(cid) not in _SESS_WIPES:
                _SESS_WIPES.append(str(cid))
            # `@handle`/title first, raw id second a bare id tells nobody
            # whose session they are looking at.
            _lab = _session_label(adapter, cid, disp, ctype)
            _lab_html = f"<b>{_esc(_lab)}</b>" if _lab else "\u2014"
            lines.append(f"\u2022 {lane} <b>[{state}]</b> \u2014 "
                         f"{_lab_html}{age}\n"
                         f"    <code>{_esc(str(sid)[:60])}</code>")
    else:
        lines.append("<i>no stored sessions yet</i>")
    lines.append("")
    lines.append("Tap 🛑 next to a live turn to stop it, 🧹 to wipe that "
                 "chat's history. CLI: <code>!stop [chat_id]</code> · "
                 "<code>!wipe &lt;chat_id&gt;</code>.")
    return "\n".join(lines)


async def _sessions_view(adapter: Any) -> str:
    """Async wrapper: the bang command predates the sync panel body."""
    return _sessions_body(adapter)


# `!run` bounces the gateway through the run script a constant so the
# tests can point it at /bin/true instead of actually restarting ATRA.
# Derived, never a host path: this file is pushed to a public repo.
_RUN_SCRIPT = str(_hermes_home() / "scripts" / "hermes_run.sh")


async def _bang_execute(adapter: Any, chat_id: str, text: str,
                        session_store: Any = None) -> Optional[str]:
    """Run one bang command; returns the reply text (the caller decides delivery)."""
    parts = text.strip().split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""
    st = settings()
    reply: Optional[str] = None

    if cmd in ("!stop", "!st"):
        # Stop the turn ATRA is running for this chat or for another chat,
        # when its id is given: !stop -100123 cancels that chat's session too.
        _target = arg.strip() or str(chat_id)
        _k = _session_key_for(adapter, _target)
        _cancelled = False
        if _k:
            try:
                _active = getattr(adapter, "_active_sessions", {}) or {}
                if _k in _active:
                    await adapter.cancel_session_processing(_k)
                    _cancelled = True
            except Exception as e:
                reply = f"\u274c stop failed: {_esc(type(e).__name__)}: {_esc(str(e)[:200])}"
        if reply is None:
            reply = ("\u2705 stopped \u2014 nothing was running here."
                     if not _cancelled else
                     "\u2705 stopped \u2014 the running turn was cancelled and "
                     "queued messages were dropped.")
            await _log("\U0001f6d1 Turn stopped",
                       f"Owner stopped the turn in <code>{_esc(chat_id)}</code>"
                       + (f" (session <code>{_esc(_k)}</code>)" if _k else ""))
    elif cmd == "!sessions":
        reply = await _sessions_view(adapter)
    elif cmd == "!help":
        reply = _help_text(st)
    elif cmd == "!panel":
        reply = _panel_text(st)
    elif cmd == "!users":
        reply = _fmt_users()
    elif cmd == "!settings":
        reply = _settings_summary()
    elif cmd in ("!run", "!restart"):
        # Restart Hermes from the panel. The child is detached into its own
        # session and sleeps 3s first, so this reply is delivered before the
        # gateway dies and because it is detached, killing this process does
        # not take the restart down with it. hermes_run.sh handles every state:
        # healthy, wedged, crash-looping or already dead.
        try:
            subprocess.Popen(
                ["bash", "-c", "sleep 3; exec bash \"$1\" auto",
                 "hermes-run", _RUN_SCRIPT],
                cwd=str(_hermes_home()), start_new_session=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL)
            reply = ("🔄 restarting Hermes this message lands first, then the "
                     "gateway bounces and comes back (≈10–30s).\n"
                     "If it does not return: "
                     f"<code>bash {_esc(_RUN_SCRIPT)} start</code> "
                     "on the box, or type <code>!run</code> again the script "
                     "also starts it from a dead state.")
            await _log("🔁 Hermes restart",
                       f"Owner ran <code>{_esc(cmd)}</code> from "
                       f"<code>{_esc(str(chat_id))}</code> bouncing "
                       f"<code>gateway-default</code> via s6")
        except Exception as e:
            reply = (f"❌ could not start the restart: {_esc(type(e).__name__)}"
                     f": {_esc(str(e)[:200])}")
    elif cmd == "!send":
        bits = arg.split(maxsplit=1)
        if len(bits) == 2:
            target, msg = bits[0], bits[1]
            try:
                if _biz_chat_known(target):
                    # A customer's chat has no bot member the raw send is
                    # 403. The business connection is also the only reply he
                    # can use without handing over a session, and once it
                    # lands the thread is HIS: the hold stands down and the
                    # next message waits for him again.
                    if await _biz_send(adapter, target, msg[:4000]):
                        _BIZ_OWNER_SEEN[str(target)] = time.time()
                        _presence_write()
                        logger.info("[TGAhermes] owner presence chat=%s (owner send)",
                                    str(target))
                        reply = f"✅ Sent to <code>{_esc(target)}</code> ({len(msg)} chars)"
                        await _log("📨 Bot DM sent",
                                   f"<b>To:</b> <code>{_esc(target)}</code>\n<b>Text:</b> <i>{_esc(msg[:500])}</i>")
                    else:
                        reply = f"❌ business send failed for <code>{_esc(target)}</code>"
                else:
                    bot = getattr(adapter, "_bot", None)
                    await bot.send_message(chat_id=target, text=msg[:4000])
                    reply = f"✅ DM sent to <code>{_esc(target)}</code> ({len(msg)} chars)"
                    await _log("📨 Bot DM sent",
                               f"<b>To:</b> <code>{_esc(target)}</code>\n<b>Text:</b> <i>{_esc(msg[:500])}</i>")
            except Exception as e:
                # failure is already visible inline as this reply no log-channel detour
                reply = f"❌ {type(e).__name__}: {_esc(str(e)[:300])}"
        else:
            reply = "Usage: <code>!send &lt;user_id&gt; &lt;text&gt;</code>"
    elif cmd == "!setlog":
        val = arg.strip()
        low = val.lower()
        if low in ("here", "this", "now", "."):
            # The point of `here`: send it inside the chat you want to log 
            # no id to copy, no risk of typos. Works from any group/channel.
            if str(chat_id).startswith(GUEST_CHAT_PREFIX):
                reply = ("This is a guest chat Telegram hides who sent it. "
                         "Send <code>!setlog here</code> inside the real channel or group instead.")
            else:
                save_settings({"log_channel": str(chat_id)})
                reply = f"Log channel → <code>{_esc(str(chat_id))}</code> <i>(this chat)</i>"
                await _log("🧭 Log channel configured",
                           "Log channel set guest-mode activity will be posted here.")
        elif low in ("off", "none", "-"):
            save_settings({"log_channel": None})
            reply = "Log channel cleared."
        elif val:
            save_settings({"log_channel": val})
            reply = f"Log channel → <code>{_esc(val)}</code>"
            await _log("🧭 Log channel configured",
                       "Log channel set guest-mode activity will be posted here.")
        else:
            reply = f"Log channel = <code>{_esc(st.get('log_channel'))}</code>"
    elif cmd == "!setowner":
        if arg.strip():
            save_settings({"owner_id": arg.strip()})
            reply = f"owner_id → <code>{_esc(arg.strip())}</code> (hot no restart)"
        else:
            reply = f"owner_id = <code>{_esc(_owner_id(adapter))}</code> (resolved)"
    elif cmd == "!setunauthorized":
        if arg:
            save_settings({"unauthorized_reply": arg})
            reply = "✅ unauthorized reply updated."
        else:
            reply = f"unauthorized_reply = <i>{_esc(st.get('unauthorized_reply'))}</i>"
    elif cmd in ("!seterror", "!seterrorfa"):
        key = "guest_error_reply_en" if cmd == "!seterror" else "guest_error_reply_fa"
        if arg:
            save_settings({key: arg})
            reply = "✅ guest error text updated."
        else:
            reply = f"{key} = <i>{_esc(st.get(key))}</i>"
    elif cmd in ("!setreact", "!setmedia"):
        key = "auto_react" if cmd == "!setreact" else "media_to_guests"
        onoff = arg.strip().lower()
        if onoff in ("on", "off", "true", "false", "1", "0"):
            save_settings({key: onoff in ("on", "true", "1")})
            reply = f"{key} → <b>{onoff}</b>"
        else:
            reply = f"{key} = <b>{st.get(key)}</b> (send on/off)"
    elif cmd == "!setcooldown":
        # Durations welcome: '10m' / '30s' / '1h' as well as raw seconds.
        secs = _dur_to_s(arg, "s")
        if secs is None:
            reply = (f"cooldown = {st.get('unauthorized_cooldown_s')}s "
                     "send a duration, e.g. <code>!setcooldown 10m</code>")
        else:
            save_settings({"unauthorized_cooldown_s": secs})
            reply = f"cooldown → {secs}s"
    elif cmd in ("!guestlock", "!guestgate"):
        # Lets the owner unlock THIS guest chat from inside it, which is the
        # only way to do it: Telegram does not tell the bot who sent a guest
        # message, so the plugin cannot recognise the owner on its own.
        want = (arg.strip().lower() or "toggle")
        if not _is_guest_chat(chat_id):
            reply = ("This is not a guest chat. Use the 🔒 Owner access button in "
                     "<code>!panel</code> → 🔧 System instead.")
        elif want in ("on", "grant", "unlock"):
            cur = [str(c) for c in (settings().get("guest_owner_chats") or [])]
            if chat_id not in cur:
                cur.append(chat_id)
            save_settings({"guest_owner_chats": cur, "guest_owner_full_access": True})
            reply = (f"🔓 <b>Unlocked</b> <code>{_esc(chat_id)}</code> for your account.\n"
                     "Turn it off any time with <code>!guestlock off</code>.")
            await _log("🛡 Guest chat unlocked", f"Owner unlocked guest chat: {chat_id}")
        elif want in ("off", "revoke", "lock"):
            cur = [str(c) for c in (settings().get("guest_owner_chats") or [])
                   if str(c) != chat_id]
            save_settings({"guest_owner_chats": cur})
            reply = f"🔒 <b>Locked</b> <code>{_esc(chat_id)}</code> back to guest rules."
            await _log("🛡 Guest chat locked", f"Owner locked guest chat: {chat_id}")
        elif want == "toggle":
            cur = [str(c) for c in (settings().get("guest_owner_chats") or [])]
            if chat_id in cur:
                save_settings({"guest_owner_chats": [c for c in cur if str(c) != chat_id]})
                reply = f"🔒 <b>Locked</b> <code>{_esc(chat_id)}</code>."
                await _log("🛡 Guest chat locked", f"Owner locked guest chat: {chat_id}")
            else:
                cur.append(chat_id)
                save_settings({"guest_owner_chats": cur, "guest_owner_full_access": True})
                reply = (f"🔓 <b>Unlocked</b> <code>{_esc(chat_id)}</code> for your account.")
                await _log("🛡 Guest chat unlocked", f"Owner unlocked guest chat: {chat_id}")
        else:
            reply = "Usage: <code>!guestlock on</code> · <code>!guestlock off</code> · <code>!guestlock</code>"
    elif cmd == "!wipe":
        target = arg.strip()
        if not target:
            reply = ("Usage: <code>!wipe &lt;chat_id&gt;</code> deletes that chat's session "
                     "and starts a fresh one <i>there</i>. This chat is never wiped implicitly.")
        else:
            res = _wipe_sessions(session_store, target)
            if res is None:
                reply = ("⚠️ wipe could not complete "
                         "session store unavailable, nothing was changed")
            elif res == (0, 0):
                reply = f"Nothing to wipe no session found for <code>{_esc(target)}</code>"
            else:
                routes, rows = res
                reply = (f"🧹 wiped <b>{rows}</b> stored session(s) "
                         f"(<b>{routes}</b> live route(s) reset) for "
                         f"<code>{_esc(target)}</code> fresh start there")
                await _log("🧹 Session wiped",
                           f"<b>Chat:</b> <code>{_esc(target)}</code> · "
                           f"<b>Reset:</b> {routes} route(s) · <b>Deleted:</b> {rows} row(s)")
    elif cmd == "!whitelist":
        reply = await _whitelist_cmd(adapter, arg)
    elif cmd == "!auth":
        reply = _auth_debug(arg.strip())
    elif cmd == "!gs":
        reply = await _gs_cmd(adapter, arg)
    elif cmd == "!gate":
        reply = await _gate_cmd(arg)
    else:
        return (f"Unknown command <code>{_esc(cmd)}</code> see <code>!help</code>.")
    return reply


async def _run_bang_command(adapter: Any, event: Any, text: str,
                            session_store: Any = None) -> None:
    chat_id = str(getattr(event.source, "chat_id", ""))
    reply = await _bang_execute(adapter, chat_id, text, session_store=session_store)
    if reply:
        cmd = text.strip().split(maxsplit=1)[0].lower()
        buttons = _help_keyboard("panel" if cmd == "!panel" else "full", chat_id=chat_id) \
            if cmd in ("!help", "!panel") else None
        await _reply_to_event(adapter, chat_id, reply, buttons=buttons)
    st = settings()
    if st.get("auto_react"):
        _src = getattr(event, "source", None)
        if _src is not None and getattr(_src, "message_id", None):
            _spawn(_react(chat_id, _src.message_id, st.get("react_emoji_done") or "✅"))
    if str(st.get("log_channel") or "") and str(st.get("log_channel")) != chat_id:
        cmd = text.strip().split(maxsplit=1)[0].lower()
        await _log(f"🖥 Bang command {cmd}", f"From <code>{_esc(chat_id)}</code> → executed")


# ---------------------------------------------------------------- panel wizards (Actions)

# One open wizard per chat: {"flow": name, "data": [typed answers]}.
# The panel starts a flow with a button; the owner types the answers as normal
# messages; the flow finishes by running the SAME bang command the panel
# replaces so button and command can never drift apart.
_WIZARD: Dict[str, Dict[str, Any]] = {}

_WIZ_FLOWS: Dict[str, Dict[str, Any]] = {
    "bizwarntext": {
        "prompts": ["\u2709\ufe0f <b>Automation - warning text</b>\n\nSend the exact "
                    "first-contact message, or <code>default</code> to let ATRA write it "
                    "itself, in their language.\n<i>Type cancel to abort.</i>"],
        "save": lambda d: {"biz_warn_text": (
            "" if d[0].strip().lower() in ("default", "reset", "auto")
            else d[0].strip()[:600])},
        "validate": lambda d: "" if d[0].strip() else "send the text, or 'default'",
        "done": "automation warning text updated."},
    "bizemoji": {
        "prompts": ["\U0001f47e <b>Automation - reply reaction</b>\n\nSend the emoji "
                    "ATRA drops on the reply that speaks for you.\n"
                    "<i>Type cancel to abort.</i>"],
        "save": lambda d: {"biz_react_emoji": d[0].strip()[:32] or "\U0001f47e"},
        "validate": lambda d: "" if d[0].strip() else "send an emoji",
        "done": "automation reaction updated."},
    "bizpersona": {
        "prompts": ["\U0001f3ad <b>Automation persona</b>\n\nSend the path of "
                    "the markdown file describing how you write (used in both "
                    "assistant and mimic modes), or <code>default</code> to go "
                    "back to the bundled ones.\n"
                    "<i>Type cancel to abort.</i>"],
        "save": lambda d: {"biz_persona_path": (
            "" if d[0].strip().lower() in _BIZ_PERSONA_RESET
            else os.path.expanduser(d[0].strip())[:400])},
        "validate": _biz_persona_arg_ok,
        "done": "automation persona updated - used on the next automation turn."},
    "bizscope": {
        "prompts": ["\U0001f3af <b>Automation - scope</b>\n\nSend a chat id to include "
                    "(repeat per id), <code>all</code> to answer every business chat, or "
                    "<code>-&lt;id&gt;</code> to drop one."
                    "\n<i>Type cancel to abort.</i>"],
        "save": lambda d: {"biz_scope": _biz_list_patch("biz_scope", d[0])},
        "validate": lambda d: "" if d[0].strip() else "send an id, 'all', or '-id'",
        "done": "automation scope updated."},
    "bizdeny": {
        "prompts": ["\U0001f512 <b>Automation - locked tools</b>\n\nSend a tool name "
                    "to lock it in automation chats, <code>all</code> to reset, or "
                    "<code>-&lt;name&gt;</code> to unlock."
                    "\n<i>Type cancel to abort.</i>"],
        "save": lambda d: {"biz_deny_tools": _biz_list_patch("biz_deny_tools", d[0])},
        "validate": lambda d: "" if d[0].strip() else "send a tool name, 'all', or '-name'",
        "done": "automation tool lock updated."},
    "bizwinstart": {
        "prompts": ["\U0001f550 <b>Automation - window start</b>\n\nSend the start as "
                    "<code>HH:MM</code> (24h, e.g. <code>09:00</code>).\n"
                    "<i>Type cancel to abort.</i>"],
        "save": lambda d: {"biz_window_start": _biz_norm_hhmm(d[0])},
        "validate": lambda d: ("" if _biz_norm_hhmm(d[0])
                               else "send a time: 9:00, 0900 or 9"),
        "done": "window start updated."},
    "bizwinend": {
        "prompts": ["\U0001f551 <b>Automation - window end</b>\n\nSend the end as "
                    "<code>HH:MM</code> (24h, e.g. <code>23:00</code> - earlier than "
                    "the start means the window wraps past midnight).\n"
                    "<i>Type cancel to abort.</i>"],
        "save": lambda d: {"biz_window_end": _biz_norm_hhmm(d[0])},
        "validate": lambda d: ("" if _biz_norm_hhmm(d[0])
                               else "send a time: 23:00, 2300 or 23"),
        "done": "window end updated."},
    "bizdays": {
        "prompts": ["\U0001f4c5 <b>Automation - window days</b>\n\nSend a day "
                    "to toggle it (<code>mon tue wed thu fri sat sun</code>), "
                    "<code>all</code> for every day, or <code>clear</code> to "
                    "reset to all.\n<i>Type cancel to abort.</i>"],
        "save": lambda d: {"biz_window_days": _biz_days_patch(d[0])},
        "validate": lambda d: "" if _biz_days_ok(d[0]) else "send a day name, 'all', or 'clear'",
        "done": "window days updated."},
    "bizidledur": {
        "prompts": ["⏳ <b>Reply hold</b>\n\nSend how long every automation "
                    "message is held before ATRA may answer <code>10s</code>, "
                    "<code>2m</code>, <code>1h</code>, or <code>0</code> to "
                    "answer immediately. Replying in the chat during the "
                    "window cancels the hold.\n<i>Type cancel to abort.</i>"],
        "save": lambda d: {"biz_idle_delay_min": (_dur_to_s(d[0], "m") or 0) / 60.0},
        "validate": lambda d: ("" if _dur_to_s(d[0], "m") is not None
                               else "send a duration, e.g. 10s, 2m, 1h or 0"),
        "done": "reply hold updated."},
    "wladd": {
        "prompts": ["🛡 <b>Add to whitelist</b>\n\nSend the user id (or @username).\n"
                    "<i>Type cancel to abort.</i>"],
        "build": lambda d: f"!whitelist add {d[0]}"},
    "wldel": {
        "prompts": ["🛡 <b>Remove from whitelist</b>\n\nSend the user id (or @username).\n"
                    "<i>Type cancel to abort.</i>"],
        "build": lambda d: f"!whitelist remove {d[0]}"},
    "owner": {
        "prompts": ["👑 <b>Set owner id</b>\n\nSend your Telegram user id (numeric).\n"
                    "<i>Type cancel to abort.</i>"],
        "build": lambda d: f"!setowner {d[0]}"},
    "wipe": {
        "prompts": ["🧹 <b>Wipe a session</b>\n\nSend the chat id whose conversation "
                    "should start fresh.\n<i>Type cancel to abort.</i>"],
        "build": lambda d: f"!wipe {d[0]}"},
    "cooldown": {
        "prompts": ["⏱ <b>Cooldown</b>\n\nSend how long a stranger waits before the "
                    "canned reply may repeat a duration like <code>10m</code>, "
                    "<code>30s</code>, <code>1h</code> (or plain seconds).\n"
                    "<i>Type cancel to abort.</i>"],
        "validate": lambda d: ("" if _dur_to_s(d[0], "s") is not None
                               else "send a duration, e.g. 10m, 30s, 1h"),
        "build": lambda d: f"!setcooldown {d[0].strip()}"},
    "unauth": {
        "prompts": ["👾 <b>Stranger reply</b>\n\nSend the exact text a stranger gets as "
                    "the canned reply.\n<i>Type cancel to abort.</i>"],
        "build": lambda d: f"!setunauthorized {d[0]}"},
    "send": {
        "prompts": ["📨 <b>Send a DM as the bot</b>\n\nStep 1/2 send the user id.\n"
                    "<i>Type cancel to abort.</i>",
                    "📨 <b>Send a DM as the bot</b>\n\nStep 2/2 send the message text."],
        "build": lambda d: f"!send {d[0]} {d[1]}"},
    "wlperm": {
        "prompts": ["🛡 <b>Friend permission level</b>\n\nStep 1/2 send the whitelisted "
                    "user id.\n<i>Type cancel to abort.</i>",
                    "🛡 <b>Step 2/2</b> send the level:\n"
                    "  <b>talk</b> safe tools only (web, vision, skills)\n"
                    "  <b>free</b> everything except destructive/credential tools\n"
                    "  <b>full</b> no tool gating at all\n"
                    "<i>Type cancel to abort.</i>"],
        "build": lambda d: f"!whitelist perms {d[0]} {d[1]}"},
    "authdbg": {
        "prompts": ["🩺 <b>Auth debug</b>\n\nSend the user id to check config file vs "
                    "the live prefilter snapshot vs the plugin's route.\n"
                    "<i>Type cancel to abort.</i>"],
        "build": lambda d: f"!auth {d[0]}"},
    "gsopen": {
        "prompts": ["🔐 <b>Open a guest session</b>\n\nSend the guest's user id (the id they "
                    "carry on the guest link). The session will accept plain mentions no "
                    "reply-to-ATRA needed.\n<i>Type cancel to abort.</i>"],
        "build": lambda d: f"!gs open {d[0]}"},
    "gslock": {
        "prompts": ["🔐 <b>Lock a guest session</b>\n\nSend the user id whose session should "
                    "be locked works on auto-created sessions and on ones you opened.\n"
                    "<i>Type cancel to abort.</i>"],
        "build": lambda d: f"!gs lock {d[0]}"},
    "gateallow": {
        "prompts": ["🛡 <b>Allow a tool</b>\n\nSend the tool name to allow guests (prefix with "
                    "<code>-</code> to remove it instead).\n<i>Type cancel to abort.</i>"],
        "build": lambda d: f"!gate allow {d[0]}"},
    "gatedeny": {
        "prompts": ["🚫 <b>Deny a tool</b>\n\nSend the tool name to keep blocked (prefix with "
                    "<code>-</code> to remove it instead).\n<i>Type cancel to abort.</i>"],
        "build": lambda d: f"!gate deny {d[0]}"},
    # --- v4.0.0: every changeable setting is reachable from the panel alone.
    # A flow either carries a `build` (a bang command twin, so button and
    # command share one code path) or a `save` (a settings patch) for the
    # keys that never had a command. `validate` re-prompts instead of dying.
    "logid": {
        "prompts": ["📡 <b>Log channel</b>\n\nSend the chat id (or @username) to log into. "
                    "Send <code>off</code> to turn logging off.\n"
                    "<i>Type cancel to abort.</i>"],
        "build": lambda d: f"!setlog {d[0]}"},
    "err_en": {
        "prompts": ["👾 <b>Guest error reply English</b>\n\nSend the exact text a guest gets "
                    "when a run fails.\n<i>Type cancel to abort.</i>"],
        "build": lambda d: f"!seterror {d[0]}"},
    "err_fa": {
        "prompts": ["👾 <b>Guest error reply Persian</b>\n\nSend the exact text a guest gets "
                    "when a run fails in Persian.\n<i>Type cancel to abort.</i>"],
        "build": lambda d: f"!seterrorfa {d[0]}"},
    "lockreply": {
        "prompts": ["🔒 <b>Locked session reply</b>\n\nSend the exact text a locked guest "
                    "session answers with.\n<i>Type cancel to abort.</i>"],
        "save": lambda d: {"guest_locked_reply": d[0][:400]},
        "validate": lambda d: "" if d[0].strip() else "send some text",
        "done": "✅ locked reply updated."},
    "emoji_recv": {
        "prompts": ["🔁 <b>Reaction message received</b>\n\nSend the emoji ATRA drops when "
                    "your message arrives.\n<i>Type cancel to abort.</i>"],
        "save": lambda d: {"react_emoji_receive": d[0][:32]},
        "validate": lambda d: "" if d[0].strip() else "send an emoji",
        "done": "✅ receive reaction updated."},
    "emoji_done": {
        "prompts": ["✅ <b>Reaction done</b>\n\nSend the emoji ATRA drops when a run "
                    "succeeds.\n<i>Type cancel to abort.</i>"],
        "save": lambda d: {"react_emoji_done": d[0][:32]},
        "validate": lambda d: "" if d[0].strip() else "send an emoji",
        "done": "✅ done reaction updated."},
    "emoji_err": {
        "prompts": ["❌ <b>Reaction error</b>\n\nSend the emoji ATRA drops when a run "
                    "fails.\n<i>Type cancel to abort.</i>"],
        "save": lambda d: {"react_emoji_error": d[0][:32]},
        "validate": lambda d: "" if d[0].strip() else "send an emoji",
        "done": "✅ error reaction updated."},
    "persona": {
        "prompts": ["🎭 <b>Guest persona file</b>\n\nSend the path to the persona markdown "
                    "guest sessions answer with, or <code>default</code> to reset.\n"
                    "<i>Type cancel to abort.</i>"],
        "save": lambda d: {"persona_path": (None if d[0].strip().lower() in ("default", "reset", "auto")
                                            else d[0].strip()[:400])},
        "validate": lambda d: "" if d[0].strip() else "send a path, or 'default'",
        "done": "✅ persona path updated used on the next guest message."},
    "uprepo": {
        "prompts": ["📦 <b>Update source repository</b>\n\nSend <code>owner/repo</code> or a full "
                    "git URL, or <code>auto</code> to follow this plugin's own origin.\n"
                    "<i>Type cancel to abort.</i>"],
        "save": lambda d: {"update_repo": (None if d[0].strip().lower() in ("auto", "default", "origin")
                                           else d[0].strip()[:300])},
        "validate": lambda d: ("" if d[0].strip().lower() in ("auto", "default", "origin")
                               or "/" in d[0].strip() else "send owner/repo, a git URL, or 'auto'"),
        "done": "✅ update source updated."},
    "upbranch": {
        "prompts": ["🌿 <b>Update branch</b>\n\nSend the branch name to track "
                    "(default <code>master</code>).\n<i>Type cancel to abort.</i>"],
        "save": lambda d: {"update_branch": d[0].strip()[:120] or "master"},
        "validate": lambda d: ("" if d[0].strip() and " " not in d[0].strip()
                               else "send a single branch name with no spaces"),
        "done": "✅ update branch updated."},
    "uptimeout": {
        "prompts": ["⌛ <b>Update timeout</b>\n\nSend how long git plus the test suite may "
                    "take (10s–30m) a duration like <code>30s</code>, "
                    "<code>10m</code>, <code>1h</code>.\n<i>Type cancel to abort.</i>"],
        "save": lambda d: {"update_timeout_s": _dur_to_s(d[0], "s")},
        "validate": lambda d: ("" if (lambda s: s is not None and 10 <= s <= 1800)
                                            (_dur_to_s(d[0], "s"))
                               else "send a duration between 10s and 30m, e.g. 30s or 10m"),
        "done": "✅ update timeout updated."},
}


# _WIZARD lives only in RAM, so a hot reload dropped a flow the owner was halfway
# through answering the next typed value then fell through to the agent as plain
# text. Persist the open steps beside settings.json (tests redirect SETTINGS_PATH)
# and restore on import, so a deploy that reloads this module eats nothing.
_WIZARD_TTL_S = 1800  # an unanswered step older than this is abandoned


def _wizard_path() -> Path:
    return SETTINGS_PATH.with_name("wizard.json")


def _wizard_persist() -> None:
    """Snapshot open wizard steps; a save failure must never break the flow."""
    try:
        tmp = _wizard_path().with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"ts": time.time(), "open": _WIZARD},
                                  ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, _wizard_path())
    except Exception:
        logger.debug("[TGAhermes] wizard persist failed", exc_info=True)


def _wizard_restore() -> None:
    """Reopen the steps a reload would otherwise have dropped; a stale step expires."""
    try:
        rec = json.loads(_wizard_path().read_text(encoding="utf-8"))
        if time.time() - float(rec.get("ts") or 0) > _WIZARD_TTL_S:
            return
        for chat, w in (rec.get("open") or {}).items():
            if isinstance(w, dict) and str(w.get("flow") or "") in _WIZ_FLOWS:
                _WIZARD[str(chat)] = {"flow": str(w["flow"]),
                                       "data": [str(x) for x in (w.get("data") or [])]}
    except FileNotFoundError:
        return
    except Exception:
        logger.debug("[TGAhermes] wizard restore failed", exc_info=True)


_wizard_restore()


def _wizard_start(chat_id: Any, flow: str) -> Optional[str]:
    """Open a wizard in this chat; returns the first prompt, or None."""
    f = _WIZ_FLOWS.get(flow)
    if not f:
        return None
    _WIZARD[str(chat_id)] = {"flow": flow, "data": []}
    _wizard_persist()
    return f["prompts"][0]


def _wizard_cancel(chat_id: Any) -> None:
    _WIZARD.pop(str(chat_id), None)
    _wizard_persist()


async def _wizard_feed(adapter: Any, chat_id: Any, text: str,
                       session_store: Any = None) -> Optional[str]:
    """Feed one owner message to the open wizard.

    Returns the reply to send, or None when no wizard is open in this chat.
    The flow is cleared before its final command runs, so one message
    completes exactly one step.
    """
    key = str(chat_id)
    w = _WIZARD.get(key)
    if not w:
        return None
    f = _WIZ_FLOWS.get(str(w.get("flow") or ""))
    if not f:
        _WIZARD.pop(key, None)
        _wizard_persist()
        return None
    if text.strip().lower() in ("cancel", "/cancel", "!cancel", "stop"):
        _WIZARD.pop(key, None)
        _wizard_persist()
        return "✖ cancelled."
    w["data"].append(text.strip()[:4000])
    _wizard_persist()
    if len(w["data"]) < len(f["prompts"]):
        return f["prompts"][len(w["data"])]
    # A flow either builds a command string (so the panel button and its
    # command twin run the same code) or carries a settings patch directly.
    # Validation runs BEFORE the flow is cleared, so a bad answer re-prompts
    # instead of silently killing the wizard.
    if f.get("validate"):
        err = f["validate"](w["data"])
        if err:
            w["data"].pop()
            _wizard_persist()  # the rejected answer must not survive on disk
            return f"⚠️ {_esc(str(err))}\n\n{f['prompts'][len(w['data'])]}"
    _WIZARD.pop(key, None)
    _wizard_persist()
    try:
        if f.get("save") is not None:
            save_settings(f["save"](w["data"]))
            return f.get("done") or "✅ saved."
        out = await _bang_execute(adapter, key, f["build"](w["data"]),
                                  session_store=session_store)
    except Exception:
        logger.exception("[TGAhermes] wizard command failed")
        return "❌ the wizard failed check the gateway log."
    return out or "✅ done."


# ---------------------------------------------------------------- pre_gateway_dispatch hook

def _live_adapter(gateway: Any) -> Any:
    """Live telegram adapter from the gateway runner keeps hook-only reloads working."""
    if gateway is None:
        return None
    try:
        from gateway.config import Platform
        return (getattr(gateway, "adapters", None) or {}).get(Platform.TELEGRAM)
    except Exception:
        return None


def _maybe_rewire() -> None:
    """Run the post-reload rewire the first time we touch the live adapter.

    A reload alone never swaps the PTB handlers (the gateway dedups factories
    by (plugin, qualname), and an unchanged file mtime keeps that key stable),
    so the previous load's closures keep serving the panel until something
    forces a rewire. Only the dispatch hook used to do that which meant a
    button tap answered from the OLD code until the owner happened to send a
    plain message. Any tool call in this session reaches here too, so a reload
    takes effect on the very next turn instead of waiting for a text message.
    """
    ad = _ADAPTER.get("adapter")
    if ad is None:
        ad = _live_adapter(_CTX.get("gateway"))
        if ad is None:
            return
        _ADAPTER["adapter"] = ad
    if getattr(ad, "_tga_instance", None) is _INSTANCE:
        return
    ad._tga_instance = _INSTANCE
    try:
        ad.rewire_plugin_handlers()
        logger.info("[TGAhermes] post-reload rewire requested")
        # The adapter's hoist rebuilds group-0 from a snapshot taken BEFORE our
        # factory ran, so the stale handlers the factory just dropped get
        # re-inserted in front of ours ("ghosts" answering taps/messages with
        # OLD code). Sweep again now that the hoist has settled.
        _sweep_stale()
    except Exception:
        logger.debug("[TGAhermes] post-reload rewire failed", exc_info=True)


def _sweep_stale() -> None:
    """Drop any stale TGAhermes handlers that resurfaced after a rewire/hoist."""
    if _NATIVE is not None:
        try:
            _drop_stale_handlers(_NATIVE)
        except Exception:
            logger.debug("[TGAhermes] sweep skipped", exc_info=True)


async def _pre_gateway_dispatch(event=None, gateway=None, session_store=None, **_) -> Optional[dict]:
    if not _UB_STATE.get("probe"):
        _UB_STATE["probe"] = 1
        logger.info("[TGAhermes] probe: dispatch entered (bridge=%r)",
                    settings().get("user_bridge"))
    """Observe + console: bang commands (skip), owner reactions, group mentions, owner mirror."""
    chat_for_error: Any = None
    try:
        if event is None:
            return None
        # MessageEvent has no `platform` field platform lives on event.source (Platform enum).
        _plat = getattr(getattr(event, "source", None), "platform", None)
        if getattr(_plat, "value", _plat) != "telegram":
            return None
        # Chat Automation: his reply in a customer chat is an ordinary
        # message, so this hook is the ONLY place the hold can learn he took
        # the conversation. Without it the delay always runs to the end and
        # ATRA answers even though he already did.
        _bump_owner_seen(event)
        _md0 = getattr(event, "metadata", None) or {}
        _biz_mark(getattr(getattr(event, "source", None), "chat_id", ""),
                  "business" if (_md0.get("business_connection_id")
                                 or _md0.get("business_chat_id")) else "plain")
        if getattr(event, "internal", False):
            return None
        st = settings()
        if session_store is not None:
            _CTX["session_store"] = session_store
        if gateway is not None:
            _CTX["gateway"] = gateway
        ad = _ADAPTER.get("adapter")
        if ad is None:
            ad = _live_adapter(gateway)
            if ad is not None:
                _ADAPTER["adapter"] = ad
        if ad is None:
            if not _UB_STATE.get("probe_ad"):
                _UB_STATE["probe_ad"] = 1
                logger.info("[TGAhermes] probe: dispatch early-return (no adapter)")
            return None
        # Hot-reload rewire trigger: see _maybe_rewire one check on the first
        # dispatched message makes the adapter rewire, the factory's per-deploy
        # qualname key is then unknown to the wired set, so it runs, sweeps the
        # stale handlers and re-syncs allow_from. The sweep also runs on every
        # event (not just the first) because the hoist can resurrect ghosts
        # after ANY factory re-run.
        _maybe_rewire()
        _sweep_stale()
        # Full unlock: arm the owner-session listener on the first inbound
        # after a (re)load dispatch always runs inside the gateway loop.
        if not _UB_STATE.get("probe_kick_zone"):
            _UB_STATE["probe_kick_zone"] = 1
            logger.info("[TGAhermes] probe: reached kick zone (kicked=%r)",
                        _UB_STATE["kicked"])
        if not _UB_STATE["kicked"] and settings().get("user_bridge"):
            _UB_STATE["kicked"] = True
            try:
                asyncio.get_running_loop().create_task(_ub_start(ad))
                logger.info("[TGAhermes] owner-session listener kick dispatched")
            except RuntimeError:
                _UB_STATE["kicked"] = False
                logger.info("[TGAhermes] owner-session listener kick deferred (no loop)")
            except Exception:
                _UB_STATE["kicked"] = False
                logger.warning("[TGAhermes] owner-session listener kick failed",
                               exc_info=True)
        elif not settings().get("user_bridge"):
            logger.info("[TGAhermes] owner-session kick skipped (user_bridge off)")
        # Push file-side allow_from into the adapter's wire-time snapshot. A CLI
        # `hermes config set` writes the file but never reaches the running
        # prefilter, so without this a freshly whitelisted user keeps bouncing
        # until something re-wires the adapter.
        try:
            _ids = _read_allow_from()
            if _ids:
                _extra = getattr(getattr(ad, "config", None), "extra", None)
                _cur = str(_extra.get("allow_from") or "") if isinstance(_extra, dict) else ""
                _want = _allow_csv(_ids)
                if _cur != _want:
                    _sync_allow_from_live(_want)   # adapter + core-gate tiers
                elif _gate_allow_raw() != _want:
                    # adapter already agrees; only the core gate's env tier is
                    # behind (e.g. .env edited at startup before this module
                    # existed) this is the tier that drops friends as
                    # "unrecognized", so it is worth its own check.
                    _sync_gate_allowlists(_want)
        except Exception:
            logger.debug("[TGAhermes] opportunistic allow_from sync failed", exc_info=True)
        src = event.source
        text = str(event.text or "")
        chat = str(src.chat_id or "")
        chat_for_error = chat
        uid = str(src.user_id or "")
        owner = _owner_id(ad)

        # Bang console: owner, in the log channel or their own DM plus
        # `!setlog …` from ANY group/channel they are in, so `!setlog here`
        # works from inside the chat they want to log (its id is the message's).
        in_log = st.get("log_channel") and chat == str(st["log_channel"])
        in_owner_dm = owner and chat == owner and (src.chat_type or "") == "dm"
        in_group = (src.chat_type or "") in ("group", "supergroup", "forum", "channel")
        setlog_here = in_group and text.lower().startswith("!setlog")
        if text.startswith("!") and owner and uid == owner and (in_log or in_owner_dm or setlog_here):
            _WIZARD.pop(chat, None)  # a real command abandons any open wizard step
            _wizard_persist()
            await _run_bang_command(ad, event, text, session_store=session_store)
            return {"action": "skip", "reason": "TGAhermes bang command"}

        # Panel wizard input: the owner answering a step the panel is waiting on.
        if owner and uid == owner and text and not text.startswith("!") and chat in _WIZARD:
            out = await _wizard_feed(ad, chat, text, session_store=session_store)
            if out is not None:
                await _reply_to_event(ad, chat, out,
                                      buttons=_help_keyboard("wiz", st, chat_id=chat)
                                      if chat in _WIZARD else None)
                return {"action": "skip", "reason": "TGAhermes wizard input"}

        # Tell the model where this turn came from (DM vs group vs channel, which
        # chat, whose message). Nothing else in the stack provides it.
        try:
            event.channel_prompt = _origin_identity_block(ad, src)
        except Exception:
            logger.exception("[TGAhermes] channel origin block failed")

        is_owner_dm = owner and chat == owner and (src.chat_type or "") == "dm"
        if is_owner_dm:
            if st.get("auto_react"):
                _spawn(_react(chat, src.message_id, st.get("react_emoji_receive") or "👀"))
            if st.get("log_owner_messages"):
                await _log("👑 Owner DM", f"<code>{_esc(chat)}</code>\n<i>{_esc(text[:500])}</i>",
                           buttons=None)
            return None

        mention_logged = False
        if (src.chat_type or "") in ("group", "supergroup", "forum", "channel"):
            bot = getattr(ad, "_bot", None)
            uname = str(getattr(bot, "username", "") or "").lower()
            if text and uname and f"@{uname}" in text.lower():
                if st.get("auto_react"):
                    _spawn(_react(chat, src.message_id, st.get("react_emoji_receive") or "👀"))
                if not st.get("log_group_mentions"):
                    return None
                user_obj = None
                raw = getattr(event, "raw_message", None)
                user_obj = getattr(raw, "from_user", None)
                mention_logged = True
                await _log(
                    "📣 Group mention",
                    f"{_user_block(user_obj) if user_obj else f'<code>{_esc(uid)}</code>'}\n"
                    f"<b>Chat:</b> <code>{_esc(chat)}</code> ({_esc(src.chat_type)})\n"
                    f"<b>Text:</b> <i>{_esc(text[:500])}</i>",
                    buttons=_profile_buttons(user_obj, chat, src.message_id))

        # --- mirror: owner never logged here; friends / others each toggleable ---
        if text and uid and uid != owner and not mention_logged:
            # Chat Automation keeps business traffic out of the generic DM
            # mirror: an automation message must never show up as a plain
            # "Whitelisted/Stranger DM to bot" entry it gets its own title.
            _braw = getattr(event, "raw_message", None)
            if getattr(_braw, "business_connection_id", None):
                return None
            if str(getattr(src, "thread_id", "") or "").startswith(_BIZ_THREAD_PREFIX):
                return None   # automation session: Chat Automation already logged it
            in_group = (src.chat_type or "") in ("group", "supergroup", "forum", "channel")
            is_friend = uid in _read_allow_from()
            if is_friend or in_group:   # stranger DMs = guest zone, already logged there
                key = "log_whitelisted_messages" if is_friend else "log_other_messages"
                if st.get(key):
                    user_obj = getattr(getattr(event, "raw_message", None), "from_user", None)
                    await _log(
                        "💬 Whitelisted message" if is_friend else "🗨 Message",
                        f"{_user_block(user_obj) if user_obj else f'<code>{_esc(uid)}</code>'}\n"
                        f"<b>Chat:</b> <code>{_esc(chat)}</code> ({_esc(src.chat_type)})\n"
                        f"<b>Text:</b> <i>{_esc(text[:500])}</i>",
                        buttons=_profile_buttons(user_obj, chat, src.message_id))
        return None
    except Exception as e:
        logger.exception("[TGAhermes] pre_gateway_dispatch failed")
        if chat_for_error:
            await _error_notice(chat_for_error, "plugin error",
                                f"<code>{_esc(type(e).__name__)}: {_esc(str(e)[:300])}</code>")
        return None


# ---------------------------------------------------------------- callbacks (log-channel buttons)

async def _on_callback(update: Any, context: Any = None) -> None:
    q = getattr(update, "callback_query", None)
    if q is None:
        return
    # Taps must rewire too otherwise a reload's ghost handlers keep
    # answering panel taps with OLD code (wrong keyboard, empty sections).
    _maybe_rewire()
    _sweep_stale()
    data = str(q.data or "")
    owner = _owner_id()
    try:
        if not owner or str(getattr(q.from_user, "id", "")) != owner:
            await q.answer("Not for you.", show_alert=True)
            return
        ad = _ADAPTER.get("adapter")
        bot = getattr(ad, "_bot", None) if ad else None
        logger.info("[TGAhermes] callback %s msg=%s", data,
                    type(getattr(q, "message", None)).__name__)
        if bot is None:
            await q.answer("Bot not connected.", show_alert=True)
            return
        if data.startswith(f"{_CB_PREFIX}help:"):
            key = data.split(":", 2)[2] if data.count(":") >= 2 else "full"
            st = settings()
            _nk = _nav_key(q)
            _nav_push(_nk, key)   # v4.0.0: Back from a section returns to where you were
            res = await _panel_edit(q, _help_view(key, st), key, st)  # keyboard gets chat_id inside
            if res == "same":
                await q.answer(f"{_VIEW_LABEL.get(key, '🧩')} already showing", show_alert=False)
            elif res == "failed":
                await q.answer("⚠️ couldn't update the panel", show_alert=True)
            else:
                await q.answer("🧩", show_alert=False)
            return
        if data.startswith(f"{_CB_PREFIX}panel:"):
            from telegram import InlineKeyboardMarkup
            st = settings()
            bits = data.split(":")
            action = bits[2] if len(bits) > 2 else "back"
            sub = bits[3] if len(bits) > 3 else ""
            toggles = _TOGGLES   # one map, shared by legacy taps + confirm flow
            view, note, body = "panel", "", ""
            arg = ""   # card target for wlfr/wlrm, "sub:origin" for a confirm screen
            _nk = _nav_key(q)
            if action == "back":
                # v4.0.0: pop one level instead of dumping the owner on the console.
                view = _nav_back(_nk)
                body = _view_body(view, st, note)
            if action == "tgy" and len(bits) > 3:
                # Pre-v3.3 panels still show this Apply name. Normalise it here
                # so there is exactly one writer to audit: panel:cfmok.
                _legacy_origin = bits[4] if len(bits) > 4 else "settings"
                action, sub, bits = ("cfmok", "tg",
                                     ["tgm", "panel", "cfmok", "tg",
                                      _legacy_origin, bits[3]])
            if action == "toggle" and sub in toggles:
                # v3.3: legacy callback name, kept so panels rendered before the
                # reload keep working but it routes through the confirm screen
                # like every other flag, it does not flip anything itself.
                view, arg = "cfm", f"settings:tg:{sub}"
                body = _cfm_view("tg", f"{sub}:settings", st)
            elif action == "settings":
                view, body = "settings", _settings_view(st)
            elif action == "bizlang":
                # Language picker: a tap opens the read-only confirm screen;
                # panel:cfmok:bizlang is the only writer.
                if sub:
                    view, arg = "cfm", f"biz:bizlang:{sub}"
                    body = _cfm_view("bizlang", sub, st)
                else:
                    view, body = "bizlang", _bizlang_view(st, note)
            elif action == "bizconn":
                view, body = "bizconn", _bizconn_view(st, note)
            elif action == "wl":
                view, body = "wl", _wl_view(st)
            elif action == "wlfr" and sub:
                view, body, arg = "wlfr", _wlfr_view(sub, st), sub
            elif action == "wllvl":
                # v3.3: changing a friend's permission level confirms first.
                lvl = bits[4] if len(bits) > 4 else ""
                lvl = _FRIEND_LEVEL_ALIASES.get(lvl, lvl)
                if sub and lvl in _FRIEND_LEVELS:
                    view, arg = "cfm", f"wl:wl:{sub}:{lvl}"
                    body = _cfm_view("wl", f"{sub}:{lvl}", st)
                else:
                    view, body = "wl", _wl_view(st)
            elif action == "wlrm" and sub:
                view, body, arg = "wlrm", _wlrm_view(sub, st), sub
            elif action == "wlrm2" and sub:
                # v3.3: the second tap is the confirm removal happens here.
                view = "wl"
                out = await _bang_execute(ad, _msg_chat_id(q.message) or "",
                                          f"!whitelist remove {sub}")
                st = settings()
                body = _wl_view(st, note=out or f"🗑 removed <code>{_esc(sub)}</code>")
            elif action == "tg" and sub == "bizmode":
                # Cycling assistant → mimic → off is not a destructive action,
                # and the owner asked for it to land on a single tap. The
                # confirm screen here was the extra step that let a mode read
                # as an on/off switch: mode and biz_schedule are different
                # keys, and only schedule gates anything.
                origin = bits[4] if len(bits) > 4 else "settings"
                nxt = _tg_next("bizmode", st)
                save_settings({"biz_mode": nxt})
                st = settings()
                _view = origin if origin in _VIEW_LABEL else "biz"
                note = (f"\U0001f916 automation mode → "
                        f"<b>{_MODE_LABEL.get(nxt, nxt)}</b>")
                await _log("\U0001f916 Chat Automation",
                           f"Mode → <b>{nxt}</b> "
                           f"(owner {_esc(str(_owner_id()))})")
                view, arg = _view, f"bizmode:{_view}"
                body = _view_body(_view, st, note, arg=f"bizmode:{_view}")
            elif action == "tg" and sub:
                # confirm screen: panel:tg:<sub-key>:<origin> changes nothing yet
                origin = bits[4] if len(bits) > 4 else "settings"
                view, arg = "tg", f"{sub}:{origin}"
                body = _tg_view(sub, origin, st)
            elif action == "view" and sub:
                # generic landing (Cancel buttons): panel:view:<name>
                view = sub if sub in _VIEW_LABEL else "panel"
                body = _view_body(view, st, note)
            elif action == "cool":
                if sub.isdigit():
                    # v3.3: a cooldown preset is a mutation, so it gets a confirm
                    # screen too. The bare number no longer writes on first tap.
                    view, arg = "cfm", f"cool:cool:{sub}"
                    body = _cfm_view("cool", sub, st)
                else:
                    view = "cool"
                    body = (f"<b>⏱ Cooldown</b> how long a stranger waits before the "
                            f"canned reply may repeat\ncurrent: <b>{st.get('unauthorized_cooldown_s')}s</b>\n"
                            "Tap a preset, tap \u270f\ufe0f to type a duration like "
                            "<code>10m</code>, or use <code>!setcooldown 10m</code>")
            elif action == "gate":
                # v3.3: `panel:gate` with no sub used to match no branch at all and
                # land back on the home grid with no body the reported bug. An
                # empty sub now opens the gate page itself (it is read-only).
                if not sub:
                    view, body = "safeguard", _gate_view(st)
                elif sub == "list":
                    view, body = "safeguard", _gate_view(st)
                elif sub == "mode" or sub == "owner":
                    # cycles a setting, so it goes behind the confirm screen
                    view, arg = "cfm", f"safeguard:tg:{sub}"
                    body = _cfm_view("tg", f"{sub}:safeguard", st)
                elif sub.startswith("grant:") or sub.startswith("revoke:"):
                    # v3.3: unlocking a chat for the owner's account grants real
                    # access, so it confirms first it is not a navigation tap.
                    _w, chat_key = sub.split(":", 1)[0], sub.split(":", 1)[-1]
                    cur = [str(c) for c in (st.get("guest_owner_chats") or [])]
                    view, arg = "cfm", f"safeguard:gate:{_w}:{chat_key}"
                    body = _cfm_view("gate", f"{_w}:{chat_key}", st)
            elif action == "gslist":
                view, body = "gsess", _gsess_view(st)
            elif action == "gs":
                # v3.3: guest-session flips used to apply on the first tap. They
                # now land on the confirm screen; panel:cfmok is the only writer.
                parts = data.split(":")
                gact = parts[3] if len(parts) > 3 else ""
                guid = parts[4] if len(parts) > 4 else ""
                _gpay = f"{guid}:{gact}"
                view, arg = "cfm", f"gsess:gs:{_gpay}"
                body = _cfm_view("gs", _gpay, st)
            elif action == "cfm" and sub in _CFM_KINDS:
                # read-only confirm screen. Layout is panel:cfm:<kind>:<origin>:
                # <payload>, and the payload may itself contain colons (chat
                # keys, uid:level), so the tail is rejoined, never read as one bit.
                _k2 = sub
                _o2 = bits[4] if len(bits) > 4 else ""
                _p2 = ":".join(bits[5:])
                view, arg = "cfm", f"{_o2}:{_k2}:{_p2}"
                body = _cfm_view(_k2, _p2, st)
            elif action == "cfmok" and sub in _CFM_KINDS:
                # THE writer one place where Apply changes a setting. The
                # callback is `cfmok:<kind>:<origin>:<payload>`; the origin is
                # stripped first so each kind parses only its own payload.
                _kind = sub
                _origin, _, _pay = (":".join(bits[4:])).partition(":")
                st = settings()
                if _kind == "tg":
                    _ts, _to = _pay, (_origin or "settings")
                    # Land back on the page the tap came from, not on the home
                    # grid otherwise a mode change from Safeguards dumps you
                    # out of the section you were working in.
                    view = _to if _to in _VIEW_LABEL else "settings"
                    if _ts == "mode":
                        nxt = _tg_next("mode", st)
                        save_settings({"guest_tool_mode": nxt})
                        st = settings()
                        note = f"🛡 guest mode → <b>{_MODE_LABEL.get(nxt, nxt)}</b>"
                        await _log("🛡 Guest tool mode",
                                   f"Mode set to <b>{nxt}</b> (owner {_esc(str(_owner_id()))})")
                    elif _ts == "bizmode":
                        nxt = _tg_next("bizmode", st)
                        save_settings({"biz_mode": nxt})
                        st = settings()
                        note = (f"\U0001f916 automation mode → "
                                f"<b>{_MODE_LABEL.get(nxt, nxt)}</b>")
                        await _log("\U0001f916 Chat Automation",
                                   f"Mode → <b>{nxt}</b> "
                                   f"(owner {_esc(str(_owner_id()))})")
                    elif _ts == "bizsched":
                        nxt = _tg_next("bizsched", st)
                        save_settings({"biz_schedule": nxt})
                        st = settings()
                        note = (f"\U0001f4d5 schedule → <b>{_esc(nxt)}</b>"
                                + (f" ({_esc(str(st.get('biz_window_start')))}"
                                   f"–{_esc(str(st.get('biz_window_end')))})"
                                   if nxt == "window" else ""))
                        await _log("\U0001f4d5 Chat Automation schedule",
                                   f"Schedule → <b>{_esc(nxt)}</b>"
                                   + (f" · window "
                                      f"{_esc(str(st.get('biz_window_start')))}"
                                      f"–{_esc(str(st.get('biz_window_end')))}"
                                      if nxt == "window" else "")
                                   + f" (owner {_esc(str(_owner_id()))})")
                    elif _ts == "bizidledelay":
                        nxt = _tg_next("bizidledelay", st)
                        save_settings({"biz_idle_delay_min": nxt})
                        st = settings()
                        note = f"⏳ reply hold → <b>{_fmt_min(nxt)}</b>"
                        await _log("⏳ Chat Automation reply hold",
                                   f"Reply hold → <b>{_fmt_min(nxt)}</b> "
                                   f"(every message waits; your reply in the "
                                   f"chat cancels it)")
                    elif _ts in _TOGGLES:
                        key, label = _TOGGLES[_ts]
                        on = bool(_tg_next(_ts, st))
                        save_settings({key: on})
                        st = settings()
                        note = f"{label} → <b>{'on' if on else 'off'}</b>"
                        if _ts == "owner":
                            await _log("🛡 Guest owner access",
                                       f"{'enabled' if on else 'disabled'} for "
                                       "unlocked guest chats")
                        elif _ts == "userbridge":
                            await _ub_apply(on)
                            await _log("🔓 Full unlock act as you",
                                       _BRIDGE_ON_LOG if on else
                                       "<b>disabled</b> owner session "
                                       "connection dropped, listener stopped.")
                    else:
                        note = "❌ unknown setting."
                        view = "settings"
                    view, body = view, _view_body(view, st, note, arg=f"{_ts}:{view}")
                elif _kind == "bizlang":
                    _lang = (_pay or "auto").strip() or "auto"
                    save_settings({"biz_lang": _lang})
                    st = settings()
                    _lbl = bizauto.language_label(_lang) if bizauto else _lang
                    note = f"\U0001f310 language → <b>{_esc(_lbl)}</b>"
                    view, body = "bizlang", _bizlang_view(st, note)
                    await _log("\U0001f310 Chat Automation",
                               f"Language → <b>{_esc(_lang)}</b> "
                               f"(owner {_esc(str(_owner_id()))})")
                elif _kind == "gs":
                    _guid, _, _gact = _pay.partition(":")
                    gstate = {"open": "open", "lock": "locked",
                              "reset": "default"}.get(_gact)
                    if _guid and gstate:
                        _set_guest_session(_guid, gstate, by="owner")
                        marks = {"open": "🔓 open", "locked": "🔒 locked",
                                 "default": "▫️ default"}
                        note = f"🔐 <code>{_esc(_guid)}</code> → <b>{marks[gstate]}</b>"
                        await _log("🔐 Guest session",
                                   f"<code>{_esc(_guid)}</code> → <b>{marks[gstate]}</b> (panel)")
                    else:
                        note = "❌ unknown session action."
                    view, body = "gsess", _gsess_view(st)
                elif _kind == "cool" and _pay.isdigit():
                    save_settings({"unauthorized_cooldown_s": int(_pay)})
                    st = settings()
                    note = f"⏱ cooldown → <b>{st.get('unauthorized_cooldown_s')}s</b>"
                    view, body = "cool", _view_body("cool", st, note)
                elif _kind == "sup" and _pay == "lock":
                    on = not bool(st.get("update_enabled", True))
                    save_settings({"update_enabled": on})
                    st = settings()
                    note = ("🔓 updates unlocked check/install re-enabled" if on
                            else "🔒 updates locked check, install and the "
                                 "selfupdate tool now refuse")
                    view, body = "system", _view_body("system", st, note)
                    await _log("🔧 Updates",
                               f"Updates {'unlocked' if on else 'locked'} by owner")
                elif _kind == "upd" and _pay in ("check", "apply"):
                    # Runs git + the test suite, so hand control back to the user
                    # with a "working" toast before it blocks.
                    await q.answer("⬆️ updating… this takes a minute"
                                   if _pay == "apply" else "🔍 checking…",
                                   show_alert=False)
                    report = await _run_selfupdate(apply=(_pay == "apply"), force=False)
                    st = settings()
                    view, note = "system", _update_note(report)
                    body = _view_body("system", st) + (f"\n\n{_update_note(report)}"
                                                     if report else "")
                elif _kind == "log" and _pay in ("here", "off"):
                    if _pay == "off":
                        prev = st.get("log_channel")
                        save_settings({"log_channel": None})
                        st = settings()
                        view = "log"
                        note = (f"\U0001f4e1 log → <b>off</b> "
                                f"(was <code>{_esc(str(prev))}</code>)")
                        body = _view_body("log", st, note)
                    else:
                        cid = _msg_chat_id(q.message) or ""
                        view = "actions"
                        if not cid or str(cid).startswith(GUEST_CHAT_PREFIX):
                            note = ("❌ open the panel inside the chat you want to log, "
                                    "then tap Log here")
                            body = _actions_view(st, note)
                        else:
                            save_settings({"log_channel": str(cid)})
                            st = settings()
                            await _log("🧭 Log channel configured",
                                       "Log channel set guest-mode activity will "
                                       "be posted here.")
                            body = _actions_view(
                                st, f"✅ now logging into <code>{_esc(cid)}</code>")
                elif _kind == "gate":
                    _gword, _, _gchat = _pay.partition(":")
                    cur_list = [str(c) for c in (st.get("guest_owner_chats") or [])]
                    view = "safeguard"
                    if _gword == "grant" and _gchat and _gchat not in cur_list:
                        cur_list.append(_gchat)
                        save_settings({"guest_owner_chats": cur_list})
                        st = settings()
                        note = f"➕ unlocked <code>{_esc(_gchat)}</code> for your account"
                        await _log("🛡 Guest chat unlocked",
                                   f"Unlocked for owner: {_esc(_gchat)}")
                    elif _gword == "revoke" and _gchat:
                        cur_list = [c for c in cur_list if c != _gchat]
                        save_settings({"guest_owner_chats": cur_list})
                        st = settings()
                        note = f"➖ revoked <code>{_esc(_gchat)}</code>"
                        await _log("🛡 Guest chat locked",
                                   f"Revoked owner unlock: {_esc(_gchat)}")
                    else:
                        note = "❌ unknown action."
                    body = _gate_view(st, note)
                elif _kind == "wl":
                    _wuid, _, _wlvl = _pay.partition(":")
                    _wlvl = _FRIEND_LEVEL_ALIASES.get(_wlvl, _wlvl)
                    view = "wl"
                    if _wuid and _wlvl in _FRIEND_LEVELS:
                        out = await _bang_execute(ad, _msg_chat_id(q.message) or "",
                                                  f"!whitelist perms {_wuid} {_wlvl}")
                        st = settings()
                        body = _wl_view(st, note=out or (
                            f"✅ <code>{_esc(_wuid)}</code> → {_FRIEND_LEVEL_LABEL[_wlvl]}"))
                    else:
                        body = _wl_view(st)
                else:
                    # unknown kind refuse, never guess a target page
                    note = "❌ unknown action."
                    view = "panel"
                    body = _panel_text(st, note)
            elif action == "upd":
                # v3.3: check/install/lock all spend real time or change a
                # setting, so none of them runs on the first tap any more.
                if sub in ("check", "apply", "lock"):
                    _kind = "sup" if sub == "lock" else "upd"
                    view, arg = "cfm", f"system:{_kind}:{sub}"
                    body = _cfm_view(_kind, sub, st)
            elif action == "logoff" and st.get("log_channel"):
                # v3.3: turning the log off is a confirm screen, not a tap.
                view, arg = "cfm", "log:log:off"
                body = _cfm_view("log", "off", st)
            elif action == "actions":
                view, body = "actions", _actions_view(st)
            elif action == "sethere":
                # v3.3: same the confirm states which chat will be logged.
                view, arg = "cfm", "actions:log:here"
                body = _cfm_view("log", "here", st)
            elif action == "wizcancel":
                _wizard_cancel(_msg_chat_id(q.message) or "")
                view, body = "actions", _actions_view(st, "✖ wizard cancelled")
            elif action == "wiz" and sub in _WIZ_FLOWS:
                prompt = _wizard_start(_msg_chat_id(q.message) or "", sub)
                view, body = "wiz", (prompt or "❌ unknown flow")
            elif action == "out":
                if sub == "guests":
                    view, body = "out", _help_view("guests", st)
                else:
                    bang = {"users": "!users", "settings": "!settings",
                            "whitelist": "!whitelist list"}.get(sub)
                    if bang:
                        view, body = "out", await _bang_execute(
                            ad, _msg_chat_id(q.message) or "", bang)
            if not body:
                body = _panel_text(st, note)
            # v4.0.0: record the landing so ⬅️ Back returns one level, not home.
            # The console resets the stack it is the root of every path.
            if view == "panel":
                _NAV.pop(_nk, None)
            else:
                _nav_push(_nk, view)
            res = await _panel_edit(q, body, view, st, arg=arg)
            if res == "failed":
                await q.answer("⚠️ couldn't update the panel", show_alert=True)
                return
            await q.answer("" if view == "system"
                           else (_html_plain(note)
                                 or (f"{_VIEW_LABEL.get(view, '🧩')} already showing"
                                     if res == "same" else "🧩")),
                           show_alert=False)
            return
        if data.startswith(f"{_CB_PREFIX}info:"):
            uid = data.split(":", 2)[2]
            e = ((_load_state().get("users") or {}).get(uid)) or {}
            await q.answer("📋", show_alert=False)
            await _log("ℹ️ User info",
                       f"<code>{_esc(uid)}</code> {_esc(e.get('name', 'unknown'))} "
                       f"@{_esc(e.get('username', ''))}\n"
                       f"msgs: {e.get('count', 0)} · first: {_esc(_humanize(int(time.time()) - int(e.get('first_seen', time.time()))) + ' ago')} · "
                       f"started: {'yes' if e.get('started') else 'no'}\n"
                       f"last: <i>{_esc(e.get('last_sample', ''))}</i>")
            return
        if data.startswith(f"{_CB_PREFIX}ban:") and "ban2" not in data:
            _chat, _uid = data.split(":")[2], data.split(":")[3]
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            await q.message.reply_text(
                f"🚫 Ban <code>{_esc(_uid)}</code> in <code>{_esc(_chat)}</code>?",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Confirm", callback_data=f"{_CB_PREFIX}ban2:{_chat}:{_uid}"),
                    InlineKeyboardButton("✖ Cancel", callback_data=f"{_CB_PREFIX}no")]]))
            await q.answer()
            return
        if data.startswith(f"{_CB_PREFIX}ban2:"):
            _chat, _uid = data.split(":")[2], data.split(":")[3]
            try:
                await bot.ban_chat_member(chat_id=_chat, user_id=int(_uid))
                await q.answer("Banned ✅")
                await _log("🚫 User banned",
                           f"uid <code>{_esc(_uid)}</code> in <code>{_esc(_chat)}</code> (button)")
            except Exception as e:
                await q.answer(f"Failed: {str(e)[:80]}")
            return
        if data.startswith(f"{_CB_PREFIX}sessstop:"):
            # 🛑 on a live turn: cancel that session's in-flight processing.
            # The button carries an index because a full session key ("agent:
            # main:telegram:group:<id>:<uid>") exceeds callback_data's 64 bytes.
            _idx_s = data.split(":", 2)[2]
            try:
                _idx = int(_idx_s)
            except ValueError:
                _idx = -1
            _key = _SESS_SNAP[_idx] if 0 <= _idx < len(_SESS_SNAP) else None
            if not _key:
                await q.answer("that turn already finished", show_alert=False)
                return
            _cancel = getattr(ad, "cancel_session_processing", None)
            if _cancel is None:
                await q.answer("session control unavailable", show_alert=True)
                return
            # Grab the live task BEFORE the cancel pops it out of the
            # adapter, so the hard stop has a reference even when the
            # ordinary cancel decides to give up on a wedged task.
            _held = None
            try:
                _st_h = getattr(ad, "_session_tasks", None)
                if isinstance(_st_h, dict):
                    _held = _st_h.get(_key)
            except Exception:
                _held = None
            try:
                await _cancel(_key)
                if await _hard_stop_session(ad, _key, held=_held):
                    await q.answer("stopped ✅", show_alert=False)
                    await _log("🛑 Session stopped",
                               f"<code>{_esc(_key)}</code> (panel button)")
                else:
                    # cancel_session_processing gave up after 5s and something
                    # bound to this key is still alive: report that instead
                    # of claiming a stop that did not happen.
                    await q.answer("⚠️ still running", show_alert=True)
                    await _session_alert(
                        "sessstop",
                        f"session refused to stop: <code>{_esc(_key)}</code>")
            except Exception as _e:
                logger.warning("[TGAhermes] sessstop failed: %s", _e)
                await q.answer(f"stop failed: {str(_e)[:70]}", show_alert=True)
                await _session_alert(
                    "sessstop-exception",
                    f"<code>{_esc(_key)}</code>: {_esc(str(_e)[:200])}")
            # Re-render so the button disappears with its turn.
            _s2 = settings()
            await _panel_edit(q, _sessions_body(st=_s2), "sessions", _s2)
            return
        if data.startswith(f"{_CB_PREFIX}wipe:"):
            _chat = data.split(":", 2)[2]
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            await q.message.reply_text(
                f"🧹 Wipe the session of <code>{_esc(_chat)}</code>? Its conversation starts fresh.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Wipe", callback_data=f"{_CB_PREFIX}wipe2:{_chat}"),
                    InlineKeyboardButton("✖ Cancel", callback_data=f"{_CB_PREFIX}no")]]))
            await q.answer()
            return
        if data.startswith(f"{_CB_PREFIX}wipe2:"):
            _chat = data.split(":", 2)[2]
            res = _wipe_sessions(None, _chat)
            if res and res != (0, 0):
                _routes, _rows = res
                await q.answer("Session wiped ✅")
                await _log("🧹 Session wiped",
                           f"<b>Chat:</b> <code>{_esc(_chat)}</code> · <b>Reset:</b> "
                           f"{_routes} route(s) · <b>Deleted:</b> {_rows} row(s) (button)")
            else:
                await q.answer("No session found")
            return
        if data.startswith(f"{_CB_PREFIX}del:") and "del2" not in data:
            _chat, _mid = data.split(":")[2], data.split(":")[3]
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            await q.message.reply_text(
                f"✂ Delete message <code>{_esc(_mid)}</code> in <code>{_esc(_chat)}</code>?",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Confirm", callback_data=f"{_CB_PREFIX}del2:{_chat}:{_mid}"),
                    InlineKeyboardButton("✖ Cancel", callback_data=f"{_CB_PREFIX}no")]]))
            await q.answer()
            return
        if data.startswith(f"{_CB_PREFIX}del2:"):
            _chat, _mid = data.split(":")[2], data.split(":")[3]
            try:
                await bot.delete_message(chat_id=_chat, message_id=int(_mid))
                await q.answer("Deleted ✅")
                await _log("✂ Message deleted",
                           f"<code>{_esc(_mid)}</code> in <code>{_esc(_chat)}</code> (button)")
            except Exception as e:
                await q.answer(f"Failed: {str(e)[:80]}")
            return
        if data.startswith(f"{_CB_PREFIX}no"):
            await q.answer("Cancelled.")
            return
        await q.answer()
    except Exception:
        logger.exception("[TGAhermes] callback failed")
        try:
            await q.answer("Error.")
        except Exception:
            pass


# ---------------------------------------------------------------- stranger DM handler

async def _on_private_text(adapter: Any, update: Any, context: Any = None) -> None:
    msg = getattr(update, "effective_message", None) or getattr(update, "message", None)
    if msg is None:
        return
    # Chat Automation owns business updates: never log them as Stranger DMs.
    if (getattr(update, "business_message", None) is not None
            or getattr(update, "edited_business_message", None) is not None):
        return
    user = msg.from_user
    uid = str(getattr(user, "id", "") or "")
    owner = _owner_id(adapter)
    text = str(msg.text or "")
    # Echo of the bot's own message (bot self-chat) not a stranger DM, and a
    # canned reply there just fails with "reply target message not found".
    _bot_id = str(getattr(getattr(adapter, "_bot", None), "id", "") or "")
    if _bot_id and uid == _bot_id:
        return
    if _is_authorized_user(uid, owner):
        # Owner or whitelisted friend: hand the message back to the core handlers.
        try:
            if text.startswith("/"):
                await adapter._handle_command(update, context)
            else:
                await adapter._handle_text_message(update, context)
        except Exception:
            logger.exception("[TGAhermes] owner delegation failed")
        return
    st = settings()
    allowed = _canned_allowed(user)
    _record_user(user, started=text.lstrip().lower().startswith("/start"), sample=text, canned=True)
    await _log(
        "📩 Stranger DM" + (" /start" if text.lstrip().lower().startswith("/start") else ""),
        f"{_user_block(user)}\n<b>Text:</b> <i>{_esc(text[:500])}</i>"
        f"\n<b>Action:</b> {'canned reply sent' if allowed else 'ignored (cooldown)'}",
        buttons=_profile_buttons(user))
    if allowed:
        reply = str(st.get("unauthorized_reply") or "")
        if reply:
            try:
                await adapter._bot.send_message(chat_id=msg.chat.id, text=reply[:4000])
            except Exception:
                logger.warning("[TGAhermes] stranger canned reply failed", exc_info=True)


# ------------------------------------------- busy-path bang / wizard input
# A message that arrives while this chat's session is running is routed by
# base._handle_message_while_active to the busy handler, which never reaches
# _hm_admit_event so pre_gateway_dispatch, where bang commands and wizard
# answers are handled, is never called ("A steered or queued follow-up never
# reaches _hm_admit_event"). PTB dispatches the FIRST matching handler per
# group and this plugin's handlers are hoisted ahead of the core's, so this one
# sees the message first: while the chat is busy, owner bang and wizard input
# are executed here; everything else is delegated back exactly as
# _on_private_text does, leaving the idle path and its dispatch-hook side
# effects unchanged.

def _chat_is_busy(adapter: Any, chat_id: str) -> bool:
    """True while a gateway session for this chat is running."""
    try:
        return bool(chat_id) and _session_key_for(adapter, chat_id) is not None
    except Exception:
        return False


async def _on_busy_path_text(adapter: Any, update: Any, context: Any = None) -> None:
    """PTB text handler: run owner bang/wizard input while the chat is busy."""
    # Same self-heal as _on_callback: this path never reaches the dispatch hook,
    # so run the rewire check and the hoist-ghost sweep here too.
    _maybe_rewire()
    _sweep_stale()
    msg = getattr(update, "effective_message", None) or getattr(update, "message", None)
    if msg is None:
        return
    text = str(msg.text or "")
    chat = str(getattr(getattr(msg, "chat", None), "id", "") or "")
    ct = str(getattr(getattr(msg, "chat", None), "type", "") or "")
    uid = str(getattr(getattr(msg, "from_user", None), "id", "") or "")
    owner = _owner_id(adapter)
    if not owner or uid != owner:
        # Not ours to intercept: strangers in a DM get the canned-reply path.
        if ct == "private":
            await _on_private_text(adapter, update, context)
        else:
            await adapter._handle_text_message(update, context)
        return
    st = settings()
    in_log = bool(st.get("log_channel")) and chat == str(st["log_channel"])
    in_owner_dm = ct == "private" and chat == owner
    in_group = ct in ("group", "supergroup", "forum", "channel")
    setlog_here = in_group and text.lower().startswith("!setlog")
    is_bang = text.startswith("!") and (in_log or in_owner_dm or setlog_here)
    is_wiz = chat in _WIZARD and not text.startswith("!")
    if not (is_bang or is_wiz) or not _chat_is_busy(adapter, chat):
        # Idle (or not console input): the core handler and the dispatch hook
        # own it, exactly as before this handler existed.
        await adapter._handle_text_message(update, context)
        return
    try:
        if is_bang:
            _WIZARD.pop(chat, None)  # a real command abandons any open wizard step
            _wizard_persist()
            from types import SimpleNamespace
            ev = SimpleNamespace(
                source=SimpleNamespace(chat_id=chat, chat_type=ct, user_id=uid,
                                       message_id=getattr(msg, "message_id", None)),
                text=text)
            await _run_bang_command(adapter, ev, text,
                                    session_store=_CTX.get("session_store"))
            logger.info("[TGAhermes] busy-path bang command %s (dispatch hook bypassed)",
                        text.split(maxsplit=1)[0])
            return
        out = await _wizard_feed(adapter, chat, text,
                                 session_store=_CTX.get("session_store"))
        if out is not None:
            await _reply_to_event(adapter, chat, out,
                                  buttons=_help_keyboard("wiz", st, chat_id=chat)
                                  if chat in _WIZARD else None)
            logger.info("[TGAhermes] busy-path wizard input accepted (hook bypassed)")
            return
        # The wizard refused it fall through so the answer is not lost.
    except Exception:
        logger.exception("[TGAhermes] busy-path bang/wizard failed")
    await adapter._handle_text_message(update, context)


# ---------------------------------------------------------------- telegram_admin tool

_TOOL_DESCRIPTION = (
    "Telegram admin actions owner's DM, the owner's log group, or a full-access "
    "automation chat; the bot must be admin in the target chat. "
    "delete_message, ban_user, unban_user, mute_user, unmute_user, get_member, chat_info, "
    "react (set a reaction), send_dm (bot DMs a user), pin_message, unpin_message, "
    "bang (run any !console command setlog/setowner/whitelist/wipe/settings/...). "
)

_TOOL_SCHEMA = {
    "name": "telegram_admin",
    "description": _TOOL_DESCRIPTION,
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": [
                "delete_message", "ban_user", "unban_user", "mute_user", "unmute_user",
                "get_member", "chat_info", "react", "send_dm", "pin_message", "unpin_message",
                "bang", "check_update", "update_plugin"]},
            "chat_id": {"type": "string", "description": "Target chat id (group/channel/supergroup/user chat)"},
            "user_id": {"type": "string", "description": "Target user id (ban/mute/unban/get_member/send_dm)"},
            "message_id": {"type": "string", "description": "Target message id (delete/react/pin)"},
            "emoji": {"type": "string", "description": "Reaction emoji for action=react (default 👍)"},
            "text": {"type": "string", "description": "Message text for action=send_dm; the full !command for action=bang (e.g. '!setlog -100123')"},
            "hours": {"type": "number", "description": "ban/mute duration in hours (omit = permanent / until unmuted)"},
            "silent": {"type": "boolean", "description": "pin without notification"},
            "apply": {"type": "boolean", "description": "update_plugin: install the newer version (default false = check only)"},
            "force": {"type": "boolean", "description": "update_plugin: reinstall even if the version matches"},
        },
        "required": ["action"],
    },
}


def _tool_check(**_) -> bool:
    """Toolset visibility gate (defensive against kwarg injection)."""
    return bool(settings().get("tool_enabled"))


def _tool_owner_ok(session_id: Any) -> bool:
    """Owner-session gate: the owner's DM, his private log group, local
    sources, or a full-access Chat Automation session (bang stays owner-DM-
    only at the call site)."""
    row = _session_row(session_id)
    if not row:
        return False
    source, chat_id = row
    if source != "telegram":
        return True  # cli/cron/local sessions live on the owner's machine
    if bool(_owner_id()) and chat_id == _owner_id():
        return True
    if chat_id and chat_id == str(settings().get("log_channel") or ""):
        return True  # his own log group the inner circle he already trusts
    return bool(settings().get("biz_full_access")) and _biz_chat_known(chat_id)


# ---------------------------------------------------------------- guest safety gate
# Tools stay AVAILABLE in guest mode (the panel is not locked down); what is
# refused is the irreversible: mutating or deleting state on this box, reaching
# the owner's files/history, or acting on the owner's behalf without him. A low
# risk request from the guest is answered normally; anything destructive is
# bounced back with "do it in your DM" instead of being executed.

GUEST_BLOCKED_TOOLS = frozenset({
    # shell / arbitrary code anything a guest names runs as root on this box
    "terminal", "execute_code", "process_manage",
    # writes and deletions
    "write_file", "patch", "delete_file", "cronjob_manage", "todo_list",
    # reading the owner's files / session transcripts (privacy leak)
    "read_file", "search_files", "session_search", "memory",
    # browser with side effects (forms, checkouts, logins)
    "browser_exec", "browser_vault_fill", "browser_vault_unlock",
    "browser_vault_enter_code", "browser_vault_save_login",
    # acting on the owner's behalf / spawning autonomous work
    "telegram_admin", "delegate_task", "clarify", "skill_manage",
})

GUEST_SAFE_TOOLS = frozenset({
    "web_search", "web_extract", "vision_analyze", "text_to_speech",
    "skills_list", "skill_view",
})

# Read-only tools: they can read THIS box's data, so "strict" keeps them shut.
# "balanced" opens them because answering a question about a file or a past
# conversation is what a guest actually wants, and reading alone changes
# nothing. Writes stay closed in every mode except "open".
GUEST_READ_TOOLS = frozenset({
    "read_file", "search_files", "session_search",
})

# Never in any mode below "open": these act on the owner's behalf, spawn
# autonomous work, or touch credentials.
GUEST_NEVER_TOOLS = frozenset({
    "telegram_admin", "browser_vault_fill", "browser_vault_unlock",
    "browser_vault_enter_code", "browser_vault_save_login",
    "memory", "skill_manage",
})


def _guest_allowed(deny: frozenset) -> frozenset:
    """Blocked set for the configured mode, plus the owner's own overrides.

    Reading the mode from settings on every call (rather than at import) is
    deliberate: it means changing guest_tool_mode in the panel takes effect on
    the next message, with no reload and no restart.
    """
    st = settings()
    mode = str(st.get("guest_tool_mode") or "balanced").strip().lower()
    base = set(GUEST_BLOCKED_TOOLS)
    if mode == "open":
        base = set()
    elif mode in ("balanced", "read"):
        base = set(GUEST_BLOCKED_TOOLS) - set(GUEST_READ_TOOLS)
    # "strict" and anything unrecognised keep the full default block list.
    base |= set(GUEST_NEVER_TOOLS)
    base |= {str(t).strip() for t in (st.get("guest_deny_tools") or []) if str(t).strip()}
    # The owner's own guest chat is NOT handled here. The hook returns early for
    # the owner before consulting this set; doing it in both places is what
    # widened the read tools for strangers too.
    return frozenset(base)

# Argument-level tripwires: a tool that is normally harmless becomes destructive
# with the right argument (deleting a session, a cron job, a memory entry...).
_GUEST_DANGER_ARG_RE = re.compile(
    r"(rm\s+-[rf]|DROP\s+TABLE|DELETE\s+FROM|truncate\s+table|git\s+push|"
    r"systemctl\s+(stop|restart)|shutdown|reboot|kill\s+-9|:(){ :|:&};)",
    re.I,
)

# Path-level tripwire for the path-bearing file tools. Two classes:
#   * secrets reading them pastes credentials into a guest chat (the leak);
#   * execution writing hooks/, scripts/ or cron/ runs code with the
#     owner's rights, and those jobs fire every minute on their own.
# SOUL/AGENTS/settings/state carry identity, config and the user registry.
# Only the path ARGUMENTS of read_file/write_file/patch are matched, so
# prose that happens to mention "config.yaml" is not collateral damage 
# and owner sessions never reach this check (exempted above).
_GUEST_PATH_TOOLS = frozenset({"read_file", "write_file", "patch"})
_GUEST_FORBIDDEN_PATH_RE = re.compile(
    r"config\.ya?ml"                       # gateway config: bot token, provider keys
    r"|\.github_\w*token"                  # git tokens lying in the home dir
    r"|\.env\b"                            # dotenv secrets
    r"|\.ssh/"                             # ssh keys / authorized_keys
    r"|id_rsa"                             # raw keys anywhere
    r"|identity\.md"                       # alt-persona file carrying real passwords
    r"|/(hooks|scripts|cron|logs|memories|backups?)/"  # execution paths + private state
    r"|SOUL\.md|AGENTS\.md"                # identity / persona
    r"|settings\.json|state\.(db|json)"    # plugin config + user registry
    r"|\.9router",                         # router data dir (credentials)
    re.I,
)


def _session_row(session_id: Any) -> Optional[Tuple[str, str]]:
    if not session_id:
        return None
    try:
        db = _hermes_home() / "state.db"
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            row = con.execute("SELECT source, chat_id FROM sessions WHERE id=?",
                              (str(session_id),)).fetchone()
        finally:
            con.close()
    except Exception:
        logger.exception("[TGAhermes] session DB lookup failed")
        return None
    if not row:
        return None
    return str(row[0] or ""), str(row[1] or "")


def _current_guest_chat() -> Optional[str]:
    """The guest_ chat id of the newest guest session, or None.

    Used by the panel to offer "unlock THIS chat" instead of making the owner
    copy an id out of a chat message.
    """
    db = _hermes_home() / "state.db"
    if not db.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT chat_id FROM sessions WHERE chat_id LIKE ? "
                "ORDER BY last_activity_at DESC LIMIT 1",
                (GUEST_CHAT_PREFIX + "%",),
            ).fetchone()
        finally:
            con.close()
    except Exception:
        logger.debug("[TGAhermes] current guest chat lookup failed", exc_info=True)
        return None
    return str(row[0]) if row and row[0] else None


def _guest_session_info(session_id: Any) -> Optional[Dict[str, Any]]:
    """Identity + rights for a guest session, or None if it is not a guest chat.

    Identity comes from :func:`_guest_identity` the same source the log
    channel reads so the gate, the log and the session table always agree.
    Nothing here is inferred from a forwarded message's sender.
    """
    row = _session_row(session_id)
    if not row:
        return None
    source, chat_id = row
    if source != "telegram" or not _is_guest_chat(chat_id):
        return None
    ident = _guest_identity(chat_id)
    gid = ident["id"]
    owner = str(_owner_id() or "")
    # The guest chat id IS the person's own Telegram id, so this comparison is
    # an identity check, not a guess.
    is_owner = bool(owner and gid and gid == owner)
    # Explicit, revocable unlock for the case where the id is not usable.
    unlocked = {str(c).strip() for c in (settings().get("guest_owner_chats") or [])
                if str(c).strip()}
    if ident["chat"] and ident["chat"] in unlocked:
        is_owner = True
    return {"guest_user_id": gid, "is_owner": is_owner,
            "guest_chat": ident["chat"], "name": ident["name"],
            "username": ident["username"], "known": ident["known"]}


def _guest_refusal(tool_name: str, info: Dict[str, Any], repeats: int = 0) -> str:
    """Refusal text for the blocked-tool gate.

    Two jobs, both learned the hard way:
      1. Tell the model the block is FINAL so it stops probing other blocked
         tools and answers with what it has. A refusal that reads like a
         transient error makes the model retry for dozens of API calls.
      2. Give the person a next step, so a block is an answer, not a wall.
    """
    owner = str(_owner_id() or "")
    if repeats:
        # Second block in the same turn: the model ignored the first one.
        # Be blunt that retrying is pointless and it must answer now.
        tail = (
            "You have already been told this is not possible. Do not call any "
            "other tool from a guest chat, and do not try again answer the "
            "person NOW using only what you already have. If you cannot help "
            "without these tools, say so in one sentence."
        )
    else:
        tail = (
            "Do not call this or any other blocked tool again; answer with what "
            "you already have, or say in one sentence that you cannot."
        )
    if info.get("is_owner"):
        return (
            f"BLOCKED in guest mode (final, not a transient error): `{tool_name}` would change "
            f"or delete something on the machine, so it is never run from a guest chat not even "
            f"for you. This IS your account, so open your own DM "
            f"(https://t.me/user?id={owner} or chat {owner}) and ask me there; I will do it "
            f"straight away. Answering questions, searching, and simple analysis all work fine "
            f"right here. {tail}"
        )
    return (
        f"BLOCKED in guest mode (final, not a transient error): `{tool_name}` would change or "
        f"delete something on the owner's machine, so it is never run from a guest chat, and I "
        f"cannot read the owner's files or private chats from here either. Ask the owner what you "
        f"want and have them do it in their own DM (https://t.me/user?id={owner} or chat {owner}). "
        f"Answering questions, web search and simple analysis all work fine right here. {tail}"
    )

def _tripwire_danger(name: str, args: Any) -> bool:
    """Argument/path tripwires shared by the guest gate and the friend gate."""
    if name in _GUEST_PATH_TOOLS and isinstance(args, dict):
        blob = " ".join(str(args.get(k, "")) for k in ("path", "file_path", "file"))
        if blob.strip() and _GUEST_FORBIDDEN_PATH_RE.search(blob):
            return True
    if args is not None:
        try:
            if _GUEST_DANGER_ARG_RE.search(json.dumps(args, ensure_ascii=False, default=str)):
                return True
        except (TypeError, ValueError):
            pass
    return False


def _bump_block(session_id: Any) -> int:
    """One more refusal for this turn; bounded so a long gate cannot leak memory.

    A block is delivered to the model as a tool result, not as a turn
    terminator, so without a per-turn counter the model re-probes blocked
    tools for dozens of calls and the guest never gets an answer.
    """
    turn_key = str(session_id or "")
    count = _GUEST_BLOCK_COUNTS.get(turn_key, 0) + 1
    _GUEST_BLOCK_COUNTS[turn_key] = count
    _GUEST_BLOCK_LAST[turn_key] = time.monotonic()
    if len(_GUEST_BLOCK_COUNTS) > 64:  # bound memory on long-lived gateways
        now = time.monotonic()
        for k, last in list(_GUEST_BLOCK_LAST.items()):
            if now - last > _GUEST_BLOCK_TTL and k != turn_key:
                _GUEST_BLOCK_COUNTS.pop(k, None)
                _GUEST_BLOCK_LAST.pop(k, None)
    return count


def _friend_refusal(tool_name: str, uid: str, level: str, repeats: int = 0) -> str:
    """Refusal for a whitelisted friend's gated tool final, with a next step."""
    if repeats:
        tail = ("You have already been told this is not possible. Do not call any other "
                "blocked tool and do not try again answer NOW with what you already have.")
    else:
        tail = ("Do not call this or any other blocked tool again; answer with what you "
                "already have, or say in one sentence that you cannot.")
    lvl = _FRIEND_LEVEL_LABEL.get(level, level)
    return (
        f"BLOCKED in this chat (final, not a transient error): `{tool_name}` is not available "
        f"at your access level ({lvl}). This is a permission setting, not a transient error, "
        f"so retrying is pointless. Answer now without the tool, and ask the owner "
        f"(https://t.me/user?id={_owner_id()}) to raise your level if you need it. {tail}"
    )


def _on_pre_tool_call(tool_name: str = "", args: Any = None, session_id: Any = None, **_) -> Optional[Dict[str, str]]:
    """Safety gate: guest rules for guest chats, per-friend levels for whitelisted DMs."""
    _maybe_rewire()
    name = str(tool_name or "")
    if name in GUEST_SAFE_TOOLS:
        return None
    # Chat Automation: business chats run the read-only sandbox, no owner path.
    _brow = _session_row(session_id)
    _bchat = None
    if _brow and _brow[0] == "telegram":
        _bchat = _brow[1]
    elif session_id:
        _bchat = str(session_id).split(":", 1)[-1]
    if _bchat and _biz_chat_known(_bchat):
        st_b = settings()
        # The customer must never receive an error/notification for a blocked
        # tool: the refusal below is agent-facing ONLY log it, and the agent
        # answers without the tool. Nothing error-like goes out over the
        # automation connection.
        def _biz_quiet_refusal(_n: str) -> str:
            return (f"BLOCKED (policy, not a transient error): `{_n}` is not available "
                    f"in this chat. Do NOT relay this message, any error, or any "
                    f"notification to the customer answer now without the tool.")
        if st_b.get("biz_full_access"):
            # Full access: every tool except credentials vault reads paste
            # secrets straight into a customer conversation, those stay locked.
            if name.startswith("browser_vault"):
                _cnt = _bump_block(session_id)
                return {"action": "block", "message": _biz_quiet_refusal(name)}
            return None
        if name in _biz_denied(st_b) or _tripwire_danger(name, args):
            _cnt = _bump_block(session_id)
            logger.warning("[TGAhermes] automation tool refused: %s (block#%d)",
                           name, _cnt)
            return {"action": "block", "message": _biz_quiet_refusal(name)}
        return None
    info = _guest_session_info(session_id)
    if info is None:
        # Whitelisted friend in their OWN DM session (source telegram, chat = their
        # id, no guest_ prefix): their permission level decides not the guest gate,
        # not nothing. Owner always passes.
        row = _session_row(session_id)
        if row and row[0] == "telegram" and not _is_guest_chat(row[1]):
            uid = row[1]
            owner = str(_owner_id() or "")
            if uid and uid != owner and uid in _read_allow_from():
                level = _friend_level(uid)
                # A whitelisted friend in their OWN DM is trusted to get work
                # done: everything runs EXCEPT the things that break the box 
                # the NEVER tools (credentials, acting-as-you, spawning work),
                # the tripwires (destructive args/paths), and for talk-level
                # friends anything that is not read/talk. The old gate wired
                # 'gate' friends into the full guest blocklist, so half the
                # tools a friend would reasonably ask for were refused.
                if level != "full":
                    hard = (name in GUEST_NEVER_TOOLS
                            or _tripwire_danger(name, args))
                    if level == "talk":
                        hard = hard or name not in (*GUEST_SAFE_TOOLS,
                                                    *GUEST_READ_TOOLS)
                    if hard:
                        count = _bump_block(session_id)
                        logger.warning("[TGAhermes] friend tool refused: %s (uid=%s level=%s block#%d)",
                                       name, uid, level, count)
                        return {"action": "block",
                                "message": _friend_refusal(name, uid, level, repeats=count - 1)}
        return None
    st = settings()
    # The owner has full access in their own DM, so gating them in their guest
    # chat protects nothing and only breaks the guest link for the one person
    # entitled to use it. That was a real bug. But the exemption stays OFF by
    # default: it is only honoured for a session whose recorded guest id
    # actually matches the owner, or one you explicitly unlocked.
    if info.get("is_owner") and st.get("guest_owner_full_access", False):
        return None
    danger = name in _guest_allowed(frozenset()) or _tripwire_danger(name, args)
    if not danger:
        return None
    count = _bump_block(session_id)
    logger.warning("[TGAhermes] guest tool refused: %s (guest_is_owner=%s, block#%d)",
                   name, info.get("is_owner"), count)
    return {"action": "block", "message": _guest_refusal(name, info, repeats=count - 1)}


def _guest_turn_expired(session_id: Any = None, **_) -> None:
    """Drop a session's block counter once it has been quiet (turn over).

    Hermes has no post-turn hook, so expiry is time-based: a guest turn lasts
    seconds, so a couple of idle minutes means the turn is over.
    """
    key = str(session_id or "")
    if not key:
        return
    last = _GUEST_BLOCK_LAST.get(key)
    if last is not None and time.monotonic() - last > _GUEST_BLOCK_TTL:
        _GUEST_BLOCK_COUNTS.pop(key, None)
        _GUEST_BLOCK_LAST.pop(key, None)


def _on_post_tool_call(tool_name: str = "", session_id: Any = None, **_) -> None:
    """Observer: retire a guest's block counter once its turn has gone quiet."""
    try:
        _guest_turn_expired(session_id)
    except Exception:
        logger.debug("[TGAhermes] block-counter expiry failed", exc_info=True)
    return None


def _on_transform_llm_output(text: Any = None, **_) -> Any:
    """Nothing to rewrite: kept as the documented seam for future prompt nudges."""
    return None


def _update_settings() -> Dict[str, Any]:
    """Resolve update config into concrete paths and flags.

    Everything is configurable: repo URL, branch, and the directory to update
    default to the running install, so a fork or a test copy works without a
    code change.
    """
    st = settings()
    try:
        from hermes_cli.config import load_config_readonly
        # Never a host literal: config home -> env -> this install's own root.
        home = Path(load_config_readonly().get("home")
                    or os.environ.get("HERMES_HOME")
                    or PLUGIN_DIR.parent.parent)
    except Exception:
        home = PLUGIN_DIR.parent.parent
    repo = str(st.get("update_repo") or "").strip() or _plugin_origin() or DEFAULT_UPDATE_REPO
    return {
        "enabled": bool(st.get("update_enabled", True)),
        "repo": repo,
        "branch": str(st.get("update_branch") or "master"),
        "timeout": int(st.get("update_timeout_s") or 300),
        "target": PLUGIN_DIR,
        "home": home,
        "backup_root": home / "cache" / "scratch" / "guest_restore",
    }


def _repo_display(repo: Any) -> str:
    """Strip credentials from a repo URL before it reaches a screen or a log.

    A token-authenticated checkout stores a credential-bearing URL in
    `git config remote.origin.url`, and that origin is what `_plugin_origin()`
    falls back to so anything rendering cfg["repo"] would otherwise print a
    live personal access token into the panel, which is sent to Telegram.
    Everything from the scheme up to and including the last at-sign is
    replaced; only the host-and-path tail is kept.
    """
    s = str(repo or "")
    if "@" in s and "://" in s:
        head, _, tail = s.partition("://")
        if "@" in tail:
            return f"{head}://…@{tail.rsplit('@', 1)[1]}"
    return s


def _plugin_origin() -> str:
    """The git origin of the checkout this module was loaded from, if any.

    The deployed directory is not a git repo, so this is normally empty and
    the configured update_repo (or the packaged default) is used instead.
    """
    if not (PLUGIN_DIR / ".git").exists():
        return ""
    try:
        r = subprocess.run(["git", "config", "--get", "remote.origin.url"],
                           cwd=str(PLUGIN_DIR), capture_output=True, text=True,
                           timeout=15, check=False)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


async def _run_selfupdate(apply: bool = False, force: bool = False) -> Dict[str, Any]:
    """Check for, or install, a plugin update. Never raises.

    Runs in a worker thread: git plus the test suite can take a minute, and
    the gateway's event loop must keep serving while it happens.
    """
    cfg = _update_settings()
    if not cfg["enabled"]:
        return {"ok": False, "error": "updates are locked (settings.update_enabled=false)"}
    try:
        sys.path.insert(0, str(PLUGIN_DIR))
        import selfupdate as _su
    except Exception as exc:
        return {"ok": False, "error": f"selfupdate unavailable: {exc}"}

    def _work() -> Dict[str, Any]:
        try:
            if apply:
                return _su.apply_update(
                    cfg["target"], cfg["repo"], cfg["branch"],
                    home=cfg["home"], backup_root=cfg["backup_root"],
                    timeout=cfg["timeout"], force=force)
            return _su.check_update(cfg["target"], cfg["repo"], cfg["branch"],
                                    timeout=cfg["timeout"])
        except Exception as exc:  # never let an update crash a turn
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    return await asyncio.to_thread(_work)


async def _tool_handler(args: Dict[str, Any], session_id: Any = None, **_) -> Dict[str, Any]:
    st = settings()
    if not st.get("tool_enabled"):
        return {"ok": False, "error": "telegram_admin is disabled (settings.tool_enabled=false)"}
    if not _tool_owner_ok(session_id):
        return {"ok": False, "error": "owner-only: telegram_admin runs only from the owner's session"}
    action = str(args.get("action") or "")
    if action == "bang":
        _row = _session_row(session_id)
        if _row and (_row[0] != "telegram" or _row[1] != _owner_id()):
            return {"ok": False, "error": "owner-only: bang runs only from the owner's session"}

    # Update runs before the bot check on purpose: a plugin update should still
    # work when Telegram is disconnected, and it must never post anything.
    if action in ("check_update", "update_plugin"):
        apply_it = action == "update_plugin" and args.get("apply", True)
        report = await _run_selfupdate(apply=bool(apply_it),
                                       force=bool(args.get("force")))
        await _log("⬆️ Plugin update" + (" (applied)" if apply_it else " (check)"),
                   f"<code>{_esc(json.dumps(report, ensure_ascii=False)[:900])}</code>")
        return {"ok": bool(report.get("ok", False)), "result": report}

    ad = _ADAPTER.get("adapter")
    bot = getattr(ad, "_bot", None) if ad else None
    if bot is None:
        return {"ok": False, "error": "telegram adapter not connected"}
    chat_id = args.get("chat_id")
    user_id = args.get("user_id")
    message_id = args.get("message_id")
    result: Any = None
    try:
        if action == "delete_message":
            await bot.delete_message(chat_id=chat_id, message_id=int(message_id))
            result = "deleted"
        elif action == "ban_user":
            kwargs = {}
            hours = args.get("hours")
            if hours:
                import datetime
                kwargs["until_date"] = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=float(hours))
            await bot.ban_chat_member(chat_id=chat_id, user_id=int(user_id), **kwargs)
            result = "banned"
        elif action == "unban_user":
            await bot.unban_chat_member(chat_id=chat_id, user_id=int(user_id))
            result = "unbanned"
        elif action in ("mute_user", "unmute_user"):
            from telegram import ChatPermissions
            if action == "mute_user":
                perms = ChatPermissions(
                    can_send_messages=False, can_send_audios=False, can_send_documents=False,
                    can_send_photos=False, can_send_videos=False, can_send_video_notes=False,
                    can_send_voice_notes=False, can_send_polls=False, can_send_other_messages=False,
                    can_add_web_page_previews=False)
            else:
                perms = ChatPermissions(
                    can_send_messages=True, can_send_audios=True, can_send_documents=True,
                    can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
                    can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True,
                    can_add_web_page_previews=True)
            kwargs = {}
            hours = args.get("hours")
            if hours and action == "mute_user":
                import datetime
                kwargs["until_date"] = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=float(hours))
            await bot.restrict_chat_member(chat_id=chat_id, user_id=int(user_id),
                                           permissions=perms, **kwargs)
            result = "muted" if action == "mute_user" else "unmuted"
        elif action == "get_member":
            m = await bot.get_chat_member(chat_id=chat_id, user_id=int(user_id))
            u = m.user
            result = {
                "status": str(m.status), "user_id": str(u.id),
                "name": (u.first_name or "") + (" " + (u.last_name or "") if u.last_name else ""),
                "username": u.username or "", "is_bot": bool(u.is_bot),
            }
        elif action == "chat_info":
            c = await bot.get_chat(chat_id=chat_id)
            result = {"id": str(getattr(c, "id", chat_id)), "type": str(getattr(c, "type", "")),
                      "title": str(getattr(c, "title", "") or getattr(c, "first_name", "") or ""),
                      "username": str(getattr(c, "username", "") or "")}
        elif action == "react":
            emoji = str(args.get("emoji") or "👍")
            ok = await _react(chat_id, message_id, emoji)
            result = {"reacted": bool(ok)}
        elif action == "send_dm":
            await bot.send_message(chat_id=int(user_id or chat_id), text=str(args.get("text") or "")[:4000])
            result = "dm sent"
        elif action == "pin_message":
            await bot.pin_chat_message(chat_id=chat_id, message_id=int(message_id),
                                       disable_notification=bool(args.get("silent")))
            result = "pinned"
        elif action == "unpin_message":
            await bot.unpin_chat_message(chat_id=chat_id, message_id=int(message_id))
            result = "unpinned"
        elif action == "bang":
            btext = str(args.get("text") or "").strip()
            if not btext.startswith("!"):
                return {"ok": False, "error": "text must be a !command (e.g. '!setlog -100123')"}
            bowner = _owner_id()
            breply = await _bang_execute(ad, bowner or str(chat_id or ""), btext,
                                         session_store=_CTX.get("session_store"))
            result = {"reply": breply}
        else:
            return {"ok": False, "error": f"unknown action: {action!r}"}
    except Exception as e:
        await _error_notice(_owner_id(),
                            "telegram_admin failed",
                            f"<b>Action:</b> <code>{_esc(action)}</code>\n<b>Error:</b> <code>{_esc(str(e)[:400])}</code>")
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    await _log("🛡 telegram_admin",
               f"<b>Action:</b> <code>{_esc(action)}</code>"
               f"\n<b>Chat:</b> <code>{_esc(chat_id)}</code>"
               f" · <b>User:</b> <code>{_esc(user_id)}</code> · <b>Msg:</b> <code>{_esc(message_id)}</code>"
               f"\n<b>Result:</b> {_esc(json.dumps(result, ensure_ascii=False)[:500] if not isinstance(result, str) else result)}")
    return {"ok": True, "result": result}


# ---------------------------------------------------------------- registration

def _drop_stale_handlers(native: Any) -> int:
    """Remove PTB handlers left in the bot by an EARLIER load of this plugin.

    The gateway's rewire dedups factories by ``(plugin, qualname)`` a key that
    never changes between loads of the same source file so a hot reload alone
    never re-runs our factory and the previous module's closures keep serving
    the panel and the guest flow out of their dead module state (measured
    2026-10-02: reload 14:40:12 ran register() but no factory; the panel still
    rendered pre-3.1.0 buttons and allow_from never re-synced). Sweeping every
    handler registered under this plugin's module names (current, or the
    pre-rename ``telegram_guest_mode``) before adding ours makes a reload an
    actual swap.

    Identity is decided by TWO signals, because ``__module__`` alone missed the
    orphan left when the pre-rename directory was deleted out from under a live
    gateway: (a) the module name, and (b) the callback's own ``^tgm:`` pattern,
    which no other plugin claims. A handler bound to a different module dict is
    stale by either signal. Removal failures used to log at DEBUG (filtered by
    default), which is exactly how an orphan stayed invisible they are
    WARNING now, and the modules we saw are logged so a surviving orphan can be
    named instead of guessed at.
    """
    removed = 0
    stale_modules: set = set()
    failures = 0
    try:
        table = getattr(native, "handlers", None)
        if not isinstance(table, dict):
            return 0
        mine = globals()
        for group, entries in list(table.items()):
            for h in list(entries):
                cb = getattr(h, "callback", None)
                if not callable(cb):
                    continue
                # The admission layer wraps every callback with @wraps, so the live
                # object's own __globals__ is update_admission's module, not ours.
                # Unwrap first or the current instance's handlers look foreign and a
                # sweep would delete the plugin out from under itself.
                cb_root = getattr(cb, "__wrapped__", cb)
                if getattr(cb_root, "__globals__", None) is mine:
                    continue  # registered by THIS very instance keep it
                mod = getattr(cb, "__module__", "") or ""
                # ours by module name (current or pre-rename), OR ours by the
                # callback prefix this plugin owns and no other plugin claims.
                # NOTE: PTB compiles a str pattern to re.Pattern, so
                # str(pattern) is "re.compile('^tgm:')" which never
                # startswith '^tgm:'. Match on the inner regex text instead.
                pattern = getattr(h, "pattern", None)
                by_pattern = False
                if pattern is not None:
                    try:
                        inner = getattr(pattern, "pattern", pattern)
                        if isinstance(inner, str) and "tgm:" in inner:
                            by_pattern = True
                        elif "tgm:" in str(pattern):
                            by_pattern = True
                    except Exception:
                        pass
                mod_norm = (mod or "").replace("-", "_")
                by_module = (mod == __name__ or "telegram_guest_mode" in mod_norm)
                by_file = False
                try:
                    cb_file = (getattr(cb, "__globals__", None) or {}).get("__file__", "") or ""
                    cb_norm = cb_file.replace("-", "_")
                    if "telegram_guest_mode" in cb_norm:
                        by_file = True
                    if not by_file:
                        code = getattr(cb, "__code__", None)
                        co_file = (getattr(code, "co_filename", "") or "").replace("-", "_")
                        if "telegram_guest_mode" in co_file:
                            by_file = True
                except Exception:
                    pass
                if not (by_module or by_file or by_pattern):
                    continue
                stale_modules.add(mod or "<no __module__>")
                try:
                    native.remove_handler(h, group=group)
                    removed += 1
                except Exception:
                    failures += 1
                    logger.warning("[TGAhermes] stale handler remove FAILED for %s",
                                   mod or "<no __module__>", exc_info=True)
    except Exception:
        logger.warning("[TGAhermes] stale handler sweep FAILED", exc_info=True)
    if stale_modules:
        logger.info(
            "[TGAhermes] stale handlers seen=%d dropped=%d failed=%d modules=%s",
            len(stale_modules), removed, failures,
            ",".join(sorted(stale_modules)),
        )
    if removed:
        logger.info("[TGAhermes] dropped %d stale handler(s) from a previous load", removed)
    return removed


def _make_factory():
    def factory(native: Any, adapter: Any) -> None:
        _ADAPTER["adapter"] = adapter
        global _NATIVE
        _NATIVE = native  # sweep target: _sweep_stale() undoes hoist ghosts
        # The adapter's config snapshot predates every config set made since it
        # first wired; push the file's current allow_from into it so a plugin
        # (re)load alone is enough for the core prefilter to agree with the file
        # again without this, a fresh whitelist write keeps getting blocked
        # until a gateway restart.
        try:
            _ids = _read_allow_from()
            if _ids:
                _sync_allow_from_live(",".join(_ids))
        except Exception:
            logger.debug("[TGAhermes] startup allow_from sync failed", exc_info=True)
        try:
            _install_wraps(adapter)
        except Exception:
            logger.exception("[TGAhermes] outbound wrap install failed")
        # Session failures in the gateway log land in the owner log channel
        # as well; a later load supersedes this watcher instead of stacking.
        try:
            _spawn(_error_watch(adapter))
        except Exception:
            logger.debug("[TGAhermes] error watch start failed", exc_info=True)
        if native is None:
            return
        # Previous load's handlers must go before ours, or PTB keeps matching
        # the old closures (first match per group) and the panel never updates.
        _drop_stale_handlers(native)
        try:
            from telegram.ext import CallbackQueryHandler, MessageHandler, filters

            gfilter = getattr(filters.UpdateType, "GUEST_MESSAGE", None)
            if gfilter is None:
                logger.warning("[%s] PTB lacks UpdateType.GUEST_MESSAGE guest mode inactive",
                               getattr(adapter, "name", "telegram"))
            else:
                async def _guest(update, context):
                    await _handle_guest_message(adapter, update, context)
                # MUST be first: guest updates also match filters.TEXT.
                native.add_handler(MessageHandler(gfilter, _guest))

            # Chat Automation: business updates also match filters.TEXT &
            # filters.ChatType.PRIVATE, so register them BEFORE the private
            # handler - PTB dispatch is first-match in registration order.
            if getattr(filters.UpdateType, "BUSINESS_MESSAGE", None) is not None:
                async def _bizmsg(update, context):
                    logger.info("[TGAhermes] bizmsg fired (update_id=%s bcid=%r)",
                                getattr(update, "update_id", "?"),
                                getattr(getattr(update, "business_message", None),
                                        "business_connection_id", "<no-field>"))
                    await _handle_business_message(adapter, update, context)
                native.add_handler(MessageHandler(filters.UpdateType.BUSINESS_MESSAGE,
                                                  _bizmsg))

                async def _bizmsg_edit(update, context):
                    await _handle_business_message(adapter, update, context,
                                                   edited=True)
                native.add_handler(MessageHandler(
                    filters.UpdateType.EDITED_BUSINESS_MESSAGE, _bizmsg_edit))
            else:
                logger.warning("[%s] PTB lacks BUSINESS_MESSAGE - Chat Automation "
                               "inactive", __name__)
            try:
                from telegram.ext import BusinessConnectionHandler

                async def _bizconn_h(update, context):
                    await _on_business_connection(adapter, update)
                native.add_handler(BusinessConnectionHandler(_bizconn_h))
            except Exception:
                logger.warning("[%s] business connection handler unavailable",
                               __name__, exc_info=True)
            # Busy-path console input: owner bang/wizard answers while this chat
            # has a live session. Must sit BEFORE _private and (via the adapter's
            # hoist) ahead of the core text handler PTB runs the first match in
            # the group. Commands are excluded so /start and friends keep going
            # to _private and the core command handler untouched.
            async def _busy_path(update, context):
                await _on_busy_path_text(adapter, update, context)
            native.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,
                                              _busy_path))

            async def _private(update, context):
                await _on_private_text(adapter, update, context)
            native.add_handler(MessageHandler(filters.TEXT & filters.ChatType.PRIVATE, _private))

            async def _cb(update, context):
                await _on_callback(update, context)
            native.add_handler(CallbackQueryHandler(_cb, pattern=r"^tgm:"))
            logger.info("[TGAhermes] v2 handlers registered (guest/private/callback)")
        except Exception:
            logger.exception("[TGAhermes] registration failed")

    # Unique per deployed file mtime: base._wire_plugin_handlers dedups by
    # (plugin, qualname) only, so an unchanged qualname makes the rewire skip
    # this factory on every hot reload the exact failure that left stale
    # handlers and a stale allow_from live. Changing the file changes the key,
    # the rewire calls us, and the sweep above replaces the old handlers.
    try:
        import os as _os
        factory.__qualname__ = f"factory.m{int(_os.path.getmtime(__file__))}"
    except Exception:
        pass
    return factory


async def _tool_handler_json(args: Dict[str, Any], **kw) -> str:
    """registry contract: tool handlers must return a string (plugins/AGENTS.md)."""
    try:
        out = await _tool_handler(args, **kw)
    except Exception as e:
        out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return json.dumps(out, ensure_ascii=False, default=str)


def register(ctx) -> None:
    try:
        ctx.register_hook("pre_gateway_dispatch", _pre_gateway_dispatch)
        ctx.register_hook("pre_tool_call", _on_pre_tool_call)
        ctx.register_hook("post_tool_call", _on_post_tool_call)
        ctx.register_hook("gateway_platform_event", _on_gateway_event)
        # Full unlock: arm the owner-session listener now if we are already
        # inside the gateway loop; the first inbound dispatch arms it otherwise.
        if settings().get("user_bridge"):
            try:
                asyncio.get_running_loop().create_task(_ub_start())
                _UB_STATE["kicked"] = True
                logger.info("[TGAhermes] register kick dispatched (bridge on)")
            except RuntimeError:
                logger.info("[TGAhermes] register kick deferred (no loop, bridge on)")
            except Exception:
                logger.warning("[TGAhermes] register kick failed", exc_info=True)
        else:
            logger.info("[TGAhermes] register kick skipped (bridge off)")
        ctx.register_tool(name="telegram_admin", toolset="telegram_admin",
                          schema=_TOOL_SCHEMA, handler=_tool_handler_json,
                          description=_TOOL_DESCRIPTION, emoji="\U0001f6e1️", is_async=True,
                          check_fn=_tool_check)
        ctx.register_telegram_handler(_make_factory())
        logger.info("[TGAhermes] v2 active (hook + tool + PTB factory)")
    except Exception:
        logger.exception("[TGAhermes] register failed")
