"""TGAhermes — all-in-one Telegram guest mode, logging console and admin tools for Hermes.

Restores the Atropos guest-mode behavior on Hermes 0.21.5+ as a plugin (no core edits), plus:

* **Guest mode** — Bot API 10 guest summons answered exclusively via ``answerGuestQuery``
  (owner plain-mention / reply-to-ATRA gate, ATRA persona, sender-identity tag, per-guest-chat
  sessions, ``event.internal`` admission bypass).
* **Unauthorized users** get a configurable canned reply (default "I only serve to my owner") —
  guest plain mentions via the guest query, stranger DMs directly — with a per-user cooldown.
* **Log channel** — every interaction (guest mentions, stranger DMs, /start, admin actions,
  bang commands, guest-mode errors, optional owner/group traffic) posts to a configurable
  group/channel with inline buttons (profile / info / ban / delete). Errors of GUEST turns
  only go to the log channel (owner DM fallback when no channel is configured).
* **Bang command console** (log channel or owner DM, owner only):
  ``!help !users !send !settings !setlog !setowner !whitelist add|remove|list|perms
  !gs list|open|lock|reset !gate show|allow|deny !auth [user_id] !wipe [chat_id]
  !setunauthorized !seterror !seterrorfa !setreact !setmedia !setcooldown`` — texts/ids
  editable live. Every chat (DM / group / guest) is its own session; ``!wipe`` (or the 🧹
  button on log entries) resets it. The whitelist adds friends who talk to the real bot,
  each with a permission level (talk / gate / full). Guest sessions can be opened for a
  specific person or locked — whether you opened them or they appeared automatically —
  and ``!auth`` shows exactly why a user is getting through or being blocked (config file
  vs the live adapter snapshot the core prefilter actually checks).
* **``telegram_admin`` agent tool** (owner session ONLY): delete messages, ban/unban/mute,
  reactions, DM users, chat/member info, pin, and ``bang`` (run any console command — the
  agent can do everything the owner can type) — gated by DB lookup to the owner's session.
* **Reactions as feedback** — 👀 when a message lands, ✅ after the reply, ❌ on errors
  (configurable/off-able, best-effort).
* **Media to guests** — URL images/documents/voice answered as inline results through the
  guest query, with a text fallback when the API rejects a result type.

Settings live in ``settings.json`` next to this file (hot-edited, never committed); the user
registry lives in ``state.json``. Persona: ``<hermes_home>/assets/guest_persona.md``.
"""

from __future__ import annotations

import asyncio
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
from typing import Any, Dict, List, Optional
from uuid import uuid4

logger = logging.getLogger(__name__)

PLUGIN_DIR = Path(__file__).resolve().parent
SETTINGS_PATH = PLUGIN_DIR / "settings.json"
STATE_PATH = PLUGIN_DIR / "state.json"
GUEST_CHAT_PREFIX = "guest_"

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
    "guest_error_reply_en": "Give me a second — system hiccup. I'm fixing it. Try again.",
    "guest_error_reply_fa": "Something went wrong on my side — fixing it now. Please try again.",
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
    #   "talk" — only the safe read-only set (web, vision, skills)
    #   "gate" — the same rules as the guest tool gate above (default)
    #   "full" — no tool gating (the old whitelisted behavior)
    "whitelist_perms": {},          # {"<user_id>": "talk"|"gate"|"full"}
    # What a locked guest session answers (rate-limited by the cooldown).
    "guest_locked_reply": "This session is locked by the owner.",
    # !update — pull a newer version of this plugin from git
    "update_enabled": True,          # set False to lock the plugin version
    "update_repo": None,             # git URL; None = use the plugin's own origin
    "update_branch": "master",       # branch to track
    "update_timeout_s": 300,         # git/test budget per attempt
}

_FALLBACK_PERSONA = """I'm ATRA — named after Atropos, the Greek Fate who cuts the thread.
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
- Casual room: I don't censor myself out of reflex — dark jokes, sharp
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

If something can't be done, I say it can't — with a reason, not as a reflex.
Everything that can be done, I do, and I finish it.
"""
_lock = threading.Lock()
_ADAPTER: Dict[str, Any] = {"adapter": None}  # live adapter, set by the PTB factory
# Fresh object per module instance: when a hot reload swaps this module, the
# first hook run sees its sentinel differ from the one stored on the adapter and
# triggers the PTB re-wire (on_plugin_loaded never fires for a RE-load, so
# nothing else would — see the factory qualname comment).
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
    # NOTE: called under _lock by _mutate_state — must NOT re-acquire (non-reentrant lock).
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
    return Path(p) if p else _hermes_home() / "assets" / "guest_persona.md"


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
    try:
        fn = getattr(ad, "_set_reaction", None)
        if fn is None:
            return False
        return bool(await fn(str(chat_id), str(message_id), emoji))
    except Exception:
        logger.debug("[TGAhermes] reaction failed", exc_info=True)
        return False


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
        kind = "article"  # local paths have no public URL — fall back to text
    cap = str(caption or "")
    try:
        from telegram import (InlineQueryResultDocument, InlineQueryResultPhoto,
                              InlineQueryResultVideo, InlineQueryResultVoice)
        if kind == "photo":
            # PTB >= 22: thumbnail_url is required — use the photo itself.
            result = InlineQueryResultPhoto(id=str(uuid4()), photo_url=url, thumbnail_url=url,
                                            caption=cap or None)
        elif kind == "video":
            import mimetypes
            vmime = mimetypes.guess_type(url)[0] or "video/mp4"
            # PTB requires mime_type + thumbnail_url; Telegram may reject a video URL as
            # thumbnail — on failure the caller falls back to a text article anyway.
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
    prompt verbatim — the documented way to add channel context without core edits.
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
    parts.append("Channel context only — not a request; do not echo these values back verbatim.")
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
    # canned gate below — a plain mention reaches the brain.
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
    # id for every guest — which is what got stamped into state.db and made the
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
        "💬 Guest mention — answered",
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

def _install_wraps(adapter: Any) -> None:
    if getattr(adapter, "_guest_wraps_installed", False):
        return
    adapter._guest_wraps_installed = True
    from gateway.platforms.base import SendResult

    async def send(chat_id: Any, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[Dict[str, Any]] = None) -> "SendResult":
        if _is_guest_chat(chat_id):
            return SendResult(success=True, message_id=None)
        return await _orig_send(chat_id, content, reply_to, metadata)

    _orig_send = adapter.send
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
        result, who = await _orig_sfl(event, session_key, text_content, metadata,
                                      reply_to=reply_to,
                                      is_ephemeral_response=is_ephemeral_response)
        st = settings()
        if st.get("auto_react") and result.success and not getattr(event, "internal", False):
            src = getattr(event, "source", None)
            if src is not None:
                _spawn(_react(src.chat_id, src.message_id, st.get("react_emoji_done") or "✅"))
        return result, who

    _orig_sfl = adapter.send_final_ledgered
    adapter.send_final_ledgered = send_final_ledgered

    async def send_clarify(chat_id, question, choices, clarify_id, session_key, metadata=None):
        if not _is_guest_chat(chat_id):
            return await _orig_sc(chat_id, question, choices, clarify_id, session_key, metadata)
        try:
            if choices:
                try:
                    from tools import clarify_gateway as _cg
                    with _cg._lock:
                        _is_multi = bool(getattr(_cg._entries.get(clarify_id), "multi_select", False))
                except Exception:
                    _is_multi = False
                hint = ("Multiple selections allowed — reply with the numbers separated by commas "
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

    _orig_sc = adapter.send_clarify
    adapter.send_clarify = send_clarify

    async def _send_prompt(what, chat_id, metadata, build, *, parse_mode=None,
                           thread_id=None, reply_to_mode=None):
        if not _is_guest_chat(chat_id):
            return await _orig_sp(what, chat_id, metadata, build, parse_mode=parse_mode,
                                  thread_id=thread_id, reply_to_mode=reply_to_mode)
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

    _orig_sp = adapter._send_prompt
    adapter._send_prompt = _send_prompt

    async def _notify_turn_error(event, e):
        md = getattr(event, "metadata", None) or {}
        gqid = md.get("guest_query_id")
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
                        text=(f"⚠️ *ATRA — guest error*\nFrom: {md.get('guest_user_id', '?')}\n"
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

    _orig_nte = adapter._notify_turn_error
    adapter._notify_turn_error = _notify_turn_error

    async def send_typing(chat_id, metadata=None):
        if not _is_guest_chat(chat_id):
            await _orig_st(chat_id, metadata)

    _orig_st = adapter.send_typing
    adapter.send_typing = send_typing

    # Media: guests get URL media as inline results (or a text fallback); others pass through.
    def _wrap_media(mname: str, kind: str, url_pos: int = 1, name_pos: Optional[int] = None):
        orig = getattr(adapter, mname, None)
        if orig is None:
            return

        async def media(*args, **kwargs):
            chat_id = args[0] if args else kwargs.get("chat_id")
            if not _is_guest_chat(chat_id):
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
        lines.append(f"• <code>{_esc(uid)}</code> {_esc(name)} {uname} — {e.get('count', 0)} msgs, "
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

    Deliberately separate from adapter.extra.allow_from — the authz mixin reads
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
    ``telegram.extra.allow_from`` — the list the panel edits. So a friend added
    in the panel was still dropped by the core as "Dropped a message from
    unrecognized telegram user".

    Semantics: UNION, not mirror — ids already approved in .env (operator
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
    """Persist telegram.extra.allow_from via the hermes CLI (subprocess — safe from the gateway).

    Three hard lessons encoded here: the owner id is always merged back in
    (_allow_csv), the file alone is not enough — the running adapter keeps
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
    """Owner or whitelisted friend — gets the real brain (core handlers), not the canned reply."""
    uid = str(uid or "")
    if not uid:
        return False
    if owner and uid == owner:
        return True
    return uid in _read_allow_from()


# ------------------------------------------------------------- access-control helpers
# Whitelisted friends carry a permission level for their own DM session:
#   talk — only the safe read-only set (web/vision/skills)
#   gate — the guest tool gate's rules, deny list included (default)
#   full — no tool gating (the old whitelisted behavior)
_FRIEND_LEVELS = ("talk", "gate", "full")
_FRIEND_LEVEL_LABEL = {"talk": "💬 talk only", "gate": "🛡 gated", "full": "🔓 full"}


def _friend_level(uid: str) -> str:
    """Resolved tool level for uid — owner is always full, unknown users 'gate'."""
    uid = str(uid or "")
    if uid and uid == str(_owner_id() or ""):
        return "full"
    lvl = str((settings().get("whitelist_perms") or {}).get(uid) or "gate").lower()
    return lvl if lvl in _FRIEND_LEVELS else "gate"


def _adapter_allow_raw() -> Any:
    """What the CORE prefilter actually sees: the adapter's bound config snapshot.

    Deliberately NOT config.yaml — a write to the file does not reach this until
    it is synced or the gateway restarts, and !auth exists to make that visible.
    """
    ad = _ADAPTER.get("adapter")
    extra = getattr(getattr(ad, "config", None), "extra", None)
    return extra.get("allow_from") if isinstance(extra, dict) else None


def _allow_csv(ids: List[str]) -> str:
    """allow_from value: the owner id is ALWAYS merged back in.

    allow_from is the DM gate; a list that lost the owner locks the owner out of
    their own DM on the next restart (2026-10-02: a whitelist write left only the
    friend's id behind — this function exists so that cannot happen again).
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
    next gateway restart. In-place dict update — the authz mixin reads the same
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
            logger.warning("[TGAhermes] adapter config.extra unavailable — allow_from not synced")
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
#   default — the usual stranger rules (canned on plain mention, brain on reply)
#   open    — may talk without replying to ATRA
#   locked  — answers only guest_locked_reply (rate-limited by the cooldown)
# A record appears automatically the first time someone talks ("created": "auto")
# or when the owner opens one ("created": "owner") — either can be locked.

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


def _wipe_sessions(store: Any, chat: Any) -> Optional[int]:
    """Reset every gateway session of this chat (all participants + guest twin).
    Returns the count, 0 for none, None when the store is unavailable."""
    if store is None:
        store = _CTX.get("session_store")
    if store is None:
        return None
    needle = str(chat)
    needles = (needle, f"{GUEST_CHAT_PREFIX}{needle}")
    try:
        entries = getattr(store, "_entries", None)
        if not isinstance(entries, dict):
            return None
        keys = [k for k in list(entries) if any(n in str(k) for n in needles)]
        for k in keys:
            store.reset_session(k)
        return len(keys)
    except Exception:
        logger.exception("[TGAhermes] session wipe failed")
        return None


def _settings_summary() -> str:
    st = settings()
    keys = ["owner_id", "log_channel", "unauthorized_reply", "unauthorized_cooldown_s",
            "guest_error_reply_en", "guest_error_reply_fa", "auto_react", "react_guests",
            "media_to_guests", "log_owner_messages", "log_whitelisted_messages",
            "log_other_messages", "log_group_mentions", "tool_enabled",
            "persona_path"]
    return "\n".join(f"<code>{k}</code> = <b>{_esc(st.get(k))}</b>" for k in keys)


def _help_sections(st: Optional[Dict[str, Any]] = None) -> list:
    """(key, title, body) — single source for the full help, sections and panel views."""
    st = st or settings()
    wl = _read_allow_from()
    log_now = _esc(st.get("log_channel") or "off")
    return [
        ("status", "ℹ️ Status",
         "<b>⚡ Actions</b> — guided flows (log here, whitelist, DM, wipe, updates)\n"
         "<code>!settings</code> — editable settings\n"
         "<code>!users</code> — who used the bot\n"
         "<code>!panel</code> — this glass-button panel (same as !help)"),
        ("log", "📡 Log",
         f"now: <code>{log_now}</code>\n"
         "<code>!setlog here</code> — send <i>inside</i> the chat you want to log\n"
         "<code>!setlog &lt;id|@name|off&gt;</code>"),
        ("access", "🛡 Access",
         f"{len(wl)} whitelisted\n"
         "<code>!whitelist list</code>\n"
         "<code>!whitelist add &lt;user_id&gt;</code>\n"
         "<code>!whitelist remove &lt;user_id&gt;</code>\n"
         "<code>!whitelist perms &lt;user_id&gt; talk|gate|full</code> — tool level\n"
         "Whitelisted friends talk to the real bot; their level decides which "
         "tools their session may use.\n"
         "<code>!auth [user_id]</code> — see file vs live prefilter verdicts"),
        ("sessions", "🧹 Sessions",
         "one per chat; wipe = fresh start\n"
         "<code>!wipe &lt;chat_id&gt;</code> — delete that chat's session, fresh start "
         "<i>there</i> (any DM/group/guest; works from the log channel too)\n"
         "Or use the 🧹 button under a log entry.\n"
         "<code>!gs list|open|lock|reset &lt;user_id&gt;</code> — guest sessions: "
         "🔓 open = talk freely · 🔒 locked = sealed · ▫️ reset = stranger rules"),
        ("guests", "👾 Guests",
         f"canned reply: <i>{_esc(st.get('unauthorized_reply'))}</i>\n"
         f"cooldown: <b>{_esc(st.get('unauthorized_cooldown_s'))}s</b> "
         "<code>!setcooldown &lt;seconds&gt;</code>\n"
         f"error EN: <i>{_esc(st.get('guest_error_reply_en'))}</i>\n"
         f"error FA: <i>{_esc(st.get('guest_error_reply_fa'))}</i>\n"
         "<code>!setunauthorized &lt;text&gt;</code> · <code>!seterror &lt;text&gt;</code> · "
         "<code>!seterrorfa &lt;text&gt;</code>"),
        ("bot", "🤖 Bot",
         "<code>!send &lt;user_id&gt; &lt;text&gt;</code> — DM someone as the bot\n"
         "<code>!setunauthorized &lt;text&gt;</code> — reply for strangers\n"
         "<code>!seterror &lt;text&gt;</code> / <code>!seterrorfa &lt;text&gt;</code> — guest error texts\n"
         "<code>!setreact on|off</code> · <code>!setmedia on|off</code>\n"
         "<code>!setcooldown &lt;seconds&gt;</code> — stranger reply cooldown\n"
         "<code>!setowner &lt;id&gt;</code> — owner id"),
    ]


def _help_text(st: Optional[Dict[str, Any]] = None) -> str:
    parts = ["🧩 <b>ATRA console</b> — owner only"]
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
    if key == "full":
        return _help_text(st)
    if key in _CATS:
        # v4.0.0: the section pages ARE the categories — every setting with
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
        f"<b>🛡 Guest tool gate</b> — mode: <b>{_MODE_LABEL.get(mode, mode)}</b>",
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
        return "⬆️ a newer version is available — tap Install update"
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
        "▫️ <b>default</b> — stranger rules: canned on a plain mention, real answer on a reply",
        "🔓 <b>open</b> — may talk without replying to ATRA",
        "🔒 <b>locked</b> — answers only the locked reply (cooldown applies)",
        "",
    ]
    if not recs:
        lines += ["No sessions yet. One appears here automatically the first time someone "
                  "talks on the guest link — or open one for a specific person below."]
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
    """Home board for !panel — a live readout of every category, so the first
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
    ]
    if note:
        lines.append(note)
    lines += [
        "",
        "<b>Tap a category</b> — every setting inside it shows its live value, "
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
    """Body text for the Actions tab — the panel's command replacement."""
    wl = _read_allow_from()
    lines = [
        "<b>⚡ Actions</b> — guided flows that replace typing commands",
        f"📡 log: <code>{_esc(st.get('log_channel') or 'off')}</code> · "
        f"🛡 whitelist: <b>{len(wl)}</b> · "
        f"⏱ cooldown: <b>{_esc(st.get('unauthorized_cooldown_s'))}s</b>",
        "",
        "📍 <b>Log here</b> — start logging into THIS chat",
        "🛡 <b>Whitelist</b> — add, remove, or set a friend's permission level",
        "🔐 <b>Guest sessions</b> — open a session for one guest, or lock it",
        "📨 <b>Send a DM</b> — message someone as the bot",
        "🧹 <b>Wipe a session</b> — give a chat a fresh start",
        "⏱ <b>Cooldown</b> · 👾 <b>Stranger reply</b> — canned texts and delays",
        "🩺 <b>Auth debug</b> — who gets through where, file vs live snapshot",
        "🛡 <b>Safeguards</b> — tool gate modes, allow/deny lists, friend levels",
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
# button applies on the first tap any more — the tap renders a confirm screen
# and only the Apply callback writes (`panel:cfmok`; legacy `panel:tgy` is
# normalised to it at parse time).
#
# v4.0.0 rebuilt the navigation layer on top of that:
#   * `_CATS` is the single registry — every one of the 29 settings keys sits
#     in exactly one category, the body renders each with its LIVE value, and
#     the keyboard emits one button per item. A test asserts the coverage, so
#     a new settings key cannot become unreachable by accident.
#   * text settings open a prompt-driven wizard (flows may carry `build`, a
#     bang-command twin, or `save`, a settings patch) — nothing is typed as a
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
}


def _tg_next(sub: str, st: Dict[str, Any]) -> Any:
    """Next value behind a confirm screen: booleans flip, guest mode cycles."""
    if sub == "mode":
        order = ["strict", "balanced", "open"]
        cur = str(st.get("guest_tool_mode") or "balanced")
        return order[(order.index(cur) + 1) % len(order)] if cur in order else "balanced"
    ent = _TOGGLES.get(sub)
    if not ent:
        return None
    return not bool(st.get(ent[0]))


def _tg_view(sub: str, origin: str, st: Dict[str, Any]) -> str:
    """Confirm screen body — now → next, nothing applied until Apply."""
    if sub == "mode":
        cur = str(st.get("guest_tool_mode") or "balanced")
        nxt = str(_tg_next("mode", st))
        return ("<b>🛡 Guest tool mode</b>\n"
                f"now: <b>{_MODE_LABEL.get(cur, cur)}</b> → <b>{_MODE_LABEL.get(nxt, nxt)}</b>\n\n"
                "Tap ✅ Apply to change, ✖ Cancel to go back.")
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
    is never a guess. It changes NOTHING — the Apply callback is the only writer.
    """
    if kind == "tg":                      # a settings flag / guest mode cycle
        sub, _, _origin = (arg or "").partition(":")
        if sub == "mode":
            cur = str(st.get("guest_tool_mode") or "balanced")
            nxt = str(_tg_next("mode", st))
            return (f"<b>🛡 Guest tool mode</b>\n"
                    f"now: <b>{_MODE_LABEL.get(cur, cur)}</b> → "
                    f"<b>{_MODE_LABEL.get(nxt, nxt)}</b>\n\n"
                    "<b>strict</b> nothing · <b>balanced</b> reads only · "
                    "<b>open</b> anything — a stranger could act on this box.\n\n"
                    "Tap ✅ Apply to change, ✖ Cancel to go back.")
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
                    "Everything this plugin logs — guest activity, your own mirror, "
                    "whitelist messages — lands here from now on. The bot has to be "
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
                    "Tap a preset below, or type <code>!setcooldown &lt;seconds&gt;</code>.")
        return ("<b>⏱ Cooldown</b>\n"
                f"now: <b>{_esc(cur)}s</b> → <b>{_esc(n)}s</b>\n\n"
                "How long a stranger waits before the canned reply may repeat.\n\n"
                "Tap ✅ Apply to change, ✖ Cancel to go back.")

    if kind == "wl":                      # panel:cfm:wl:<uid>:<level>
        uid, _, lvl = (arg or "").partition(":")
        if lvl in _FRIEND_LEVELS:
            cur = _friend_level(uid)
            return ("<b>🛡 Permission level</b>\n"
                    f"<code>{_esc(uid)}</code>: <b>"
                    f"{_FRIEND_LEVEL_LABEL.get(cur, cur)}</b> → <b>"
                    f"{_FRIEND_LEVEL_LABEL[lvl]}</b>\n\n"
                    "<b>talk</b> safe read-only tools · <b>gate</b> the guest tool "
                    "rules · <b>full</b> no tool gating.\n\n"
                    "Tap ✅ Apply to change, ✖ Cancel to go back.")
        return ("<b>🛡 Permission level</b>\n"
                "Pick the level on this page.\n\n"
                "<b>talk</b> safe read-only tools · <b>gate</b> the guest tool "
                "rules · <b>full</b> no tool gating.")

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
                        "Nothing to do — tap ✖ Cancel to go back.")
            return ("\n".join([
                f"<b>Unlock <code>{_esc(chat)}</code> for your account?</b>",
                "now: <b>locked</b> → <b>unlocked</b>",
                "",
                "In this chat a guest is not limited to the read-only tools — it gets "
                "your owner's access. Treat anyone who can message there as yourself.",
                "",
                "Tap ✅ Apply to unlock, ✖ Cancel to go back."]))
        if not unlocked:
            return (f"<b>➖ <code>{_esc(chat)}</code></b>\n"
                    "now: <b>not unlocked</b>\n\n"
                    "Nothing to do — tap ✖ Cancel to go back.")
        return ("\n".join([
            f"<b>Revoke owner access in <code>{_esc(chat)}</code>?</b>",
            "now: <b>unlocked</b> → <b>locked</b>",
            "",
            "A guest there goes back to the read-only tools.",
            "",
            "Tap ✅ Apply to lock, ✖ Cancel to go back."]))

    return "❌ unknown action."


# ---------------------------------------------------------------------------
# v4.0.0 — one registry drives every settings page.
#
# The body lists each item with its LIVE value, the keyboard offers exactly
# one button per item, and a test asserts that all 29 DEFAULT_SETTINGS keys
# appear here — so "every changeable setting is reachable from the panel, in
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
              "settings": "⚙️ All flags"}

# One line under each title, so a category page says what it is for before it
# lists values. `system` doubles as the version readout the panel used to show.
_CAT_BLURB = {
    "log": "Where activity lands — the channel, and which messages get mirrored.",
    "guests": "canned reply, cooldown, error texts — everything a stranger hears.",
    "react": "Which emoji ATRA drops, and on whose messages.",
    "tool": "What a guest session may run, and which chats you unlocked for yourself.",
    "access": "Who talks to the real brain, and at which tool level.",
    "system": "",
    "settings": "Every flag in one column, for a fast scan before you leave.",
}


def _cat_value(it: Dict[str, Any], st: Dict[str, Any]) -> str:
    """The live value shown beside a setting, already HTML-safe."""
    k = it["kind"]
    key = str(it.get("key") or "")
    if k == "bool":
        return "<b>on</b>" if st.get(key) else "<b>off</b>"
    if k == "enum":
        m = str(st.get(key) or "balanced")
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
              "Tap a button below — text settings open a prompt, flags open a "
              "confirm screen. Nothing is typed as a command.",
              "↩ Back returns to the page you came from."]
    return "\n".join(lines)


def _cat_buttons(cat: str, st: Dict[str, Any], chat_id: Optional[str] = None) -> list:
    """(label, callback) pairs for one category — one per setting."""
    out: List[Tuple[str, str]] = []
    for it in _CATS.get(cat) or []:
        k = it["kind"]
        if k == "bool":
            mark = "✅" if st.get(it["key"]) else "⏸"
            out.append((f"{mark} {it['label']}", f"panel:tg:{it['sub']}:{cat}"))
        elif k == "enum":
            m = str(st.get(it["key"]) or "balanced")
            out.append((f"{_MODE_LABEL.get(m, m)} {it['label']}", f"panel:tg:{it['sub']}:{cat}"))
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
                out.append((f"{it['label']} — tap to lock", str(it["cb"])))
            else:
                out.append(("🔓 Updates locked — tap to unlock", str(it["cb"])))
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
    """All flags in one place — each with its own confirm screen."""
    return _cat_body("settings", st, note)


def _wl_view(st: Dict[str, Any], note: str = "") -> str:
    """Whitelist as a list of friend cards — one button each, one tap per level."""
    owner = str(_owner_id() or "")
    wl = _read_allow_from()
    lines = [
        "<b>🛡 Whitelist & friends</b> — these ids talk to the real brain",
        f"👑 owner: <code>{_esc(owner or 'unset')}</code> (always full access)",
    ]
    friends = [u for u in wl if u != owner]
    if not friends:
        lines += ["", "No friends yet — tap ➕ Add a friend; the wizard asks for the id."]
    for uid in friends:
        lvl = str((st.get("whitelist_perms") or {}).get(uid) or "gate")
        lines.append(f"• <code>{_esc(uid)}</code> — {_FRIEND_LEVEL_LABEL.get(lvl, lvl)} "
                     f"— tap to change level or remove")
    lines += [
        "",
        "The core gateway tier (<code>TELEGRAM_ALLOWED_USERS</code>) is synced "
        "automatically — someone added here passes the gateway too, no restart.",
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
        "<b>talk</b> — safe read-only tools (web, vision, skills)",
        "<b>gate</b> — same rules as the guest tool gate",
        "<b>full</b> — no tool gating",
        "",
        "Tap a level to apply immediately (same code as "
        "<code>!whitelist perms</code>). Removing asks for a confirm.",
    ])


def _wlrm_view(uid: str, st: Dict[str, Any]) -> str:
    """Remove confirm — names the consequence instead of just the action."""
    return "\n".join([
        f"🗑 <b>Remove <code>{_esc(uid)}</code> from the whitelist?</b>",
        "",
        "He stops talking to the real brain and falls back to guest rules.",
        "The core gate stays synced — his messages would be ignored, not answered.",
        "",
        "✅ Yes, remove · ✖ No, keep",
    ])


def _view_body(view: str, st: Dict[str, Any], note: str = "",
               arg: str = "") -> str:
    """Body for a view name — used after Apply/Cancel so every return lands
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
        return _with(f"<b>⏱ Cooldown</b> — how long a stranger waits before the "
                     f"canned reply may repeat\ncurrent: "
                     f"<b>{st.get('unauthorized_cooldown_s')}s</b>\n"
                     "Tap a preset, or type <code>!setcooldown &lt;seconds&gt;</code>")
    if view == "cfm":
        _o, _, _kp = (arg or "").partition(":")
        _k, _, _p = _kp.partition(":")
        return _with(_cfm_view(_k, _p, st))
    if view in ("full", "bot", "who", "status", "sessions", "log", "guests",
                "access", "system"):
        # help pages a Back pop can land on — render the real section, never
        # the console, so the body always matches the keyboard.
        return _help_view(view, st)
    return _panel_text(st, note)


def _help_keyboard(view: str = "panel", st: Optional[Dict[str, Any]] = None,
                   chat_id: Optional[str] = None, arg: str = "") -> list:
    """Per-tab button sets: sections at home, contextual actions inside a tab.

    `arg` carries the target for card views (wlfr/wlrm uid, "sub:origin" for a
    confirm screen) — the view string stays a plain label for routing/labels."""
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
        # v4.0.0 category page — one button per setting, values in the body.
        _pairs = _cat_buttons(view, st, chat_id)
        for _ci in range(0, len(_pairs), 2):
            add(*_pairs[_ci:_ci + 2])
    elif view == "panel":
        # Home: a status board plus the six categories, everything one tap away.
        add(("📡 Log", "help:log"), ("👾 Guests", "help:guests"))
        add(("🔁 Reactions", "help:react"), ("🤖 Tool", "help:tool"))
        add(("🛡 Access", "help:access"), ("🔧 System", "help:system"))
        add(("⚙️ Settings", "panel:settings"), ("🛡 Whitelist", "panel:wl"))
        add(("⚡ Actions — do things", "panel:actions"),
            ("📜 Full help", "help:full"))
        add(("🧹 Sessions", "help:sessions"))
    elif view == "wl":
        _wowner = str(_owner_id() or "")
        for _wuid in _read_allow_from():
            if _wuid == _wowner:
                continue
            _wlvl = str((st.get("whitelist_perms") or {}).get(_wuid) or "gate")
            add((f"{_FRIEND_LEVEL_LABEL.get(_wlvl, _wlvl)} · {_wuid}",
                 f"panel:wlfr:{_wuid}"))
        add(("➕ Add a friend — wizard", "panel:wiz:wladd"))
        add(("📋 Users", "panel:out:users"), ("🛡 Whitelist text", "panel:out:whitelist"))
        add(("🔍 Auth debug", "panel:wiz:authdbg"))
    elif view == "wlfr":
        _frlvl = str((st.get("whitelist_perms") or {}).get(arg or "") or "gate")
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
    elif view == "actions":
        add(("\U0001f4e1 Log here — log into THIS chat", "panel:sethere"))
        add(("\U0001f6e1 Whitelist add", "panel:wiz:wladd"),
            ("\U0001f6e1 Whitelist remove", "panel:wiz:wldel"))
        add(("\U0001f6e1 Friend permission level", "panel:wiz:wlperm"))
        add(("\U0001f510 Guest sessions — open / lock", "panel:gslist"))
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
        if chat_id:
            add((f"🧹 Wipe this chat", f"wipe:{chat_id}"))
        if log:
            add((f"🧹 Wipe log chat", f"wipe:{log}"))
        add(("⚙️ Settings", "panel:out:settings"))
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
        # pops one level of history — from a category it lands on the console,
        # from a sub-page it lands on the category you came from.
        rows.insert(0, [B("⬅️ Back", callback_data=f"{_CB_PREFIX}panel:back")])
        add(("📜 Full help", "help:full"))
    if view == "cool":
        add(*[(f"{n}s", f"panel:cool:{n}") for n in (0, 60, 300, 3600)])
    if chat_id and view in ("panel", "sessions", "access"):
        add((f"🧹 Wipe this chat", f"wipe:{chat_id}"))
    return rows


_MODE_LABEL = {"strict": "🔒 strict", "balanced": "⚖️ balanced", "open": "🔓 open"}

_VIEW_LABEL = {"full": "📜 Full help", "panel": "🧩 Console", "out": "📋 Output",
               "status": "ℹ️ Status", "log": "📡 Log", "access": "🛡 Access",
               "sessions": "🧹 Sessions", "bot": "🤖 Bot", "guests": "👾 Guests",
               "react": "🔁 Reactions", "tool": "🤖 Tool & gate",
               "cool": "⏱ Cooldown", "system": "🔧 System", "safeguard": "🛡 Safeguards",
               "actions": "⚡ Actions", "gsess": "🔐 Guest sessions", "wiz": "📝 Wizard",
               "settings": "⚙️ Settings", "wl": "🛡 Whitelist", "wlfr": "👤 Friend",
               "wlrm": "🗑 Remove", "tg": "✅ Confirm", "cfm": "✅ Confirm"}

# One confirm vocabulary: every mutating button first renders
# `panel:cfm:<kind>:...` (read-only, states now → next), and only the Apply
# button writes — `panel:tgy` for settings flags, `panel:upd` for updates,
# `panel:cfmok` for the rest. Nothing else in the panel writes a setting.
_CFM_KINDS = {"tg", "gs", "log", "upd", "sup", "cool", "wl", "gate"}

# v4.0.0 — Back returns to the page you came from, not always to the console.
# A per-panel-message history: navigate truncates/appends, `panel:back` pops.
# Confirm screens and the wizard are transient, so they never enter the stack.
_NAV: Dict[str, List[str]] = {}
_NAV_MAX = 16
_NAV_TRANSIENT = frozenset({"cfm", "tg", "wiz"})


def _nav_key(q: Any) -> str:
    """One history per panel message — two open panels do not share a stack."""
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
    anything (Telegram 400s on an identical edit — that is a no-op, not a failure), and
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
            return ("Usage: <code>!whitelist perms &lt;user_id&gt; talk|gate|full</code>\n"
                    "💬 talk = web/vision only · 🛡 gate = guest tool rules · 🔓 full = everything")
        target, lvl = bits2[0], bits2[1].strip().lower()
        if lvl not in _FRIEND_LEVELS:
            return f"Unknown level <code>{_esc(lvl)}</code> — use talk, gate or full."
        if target != owner and target not in _read_allow_from():
            return f"<code>{_esc(target)}</code> is not whitelisted — add them first."
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
                return f"⚠️ could not resolve {_esc(val)} — send the numeric user id instead"
        target = str(target).strip()
        if target == owner and sub == "remove":
            return "Refusing to remove the owner — the owner is always authorized; use <code>!setowner</code> to change ownership."
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
            return "❌ could not write config — see the gateway log"
        verb = "Added" if sub == "add" else "Removed"
        await _log("🛡 Whitelist updated",
                   f"<b>{verb}:</b> <code>{_esc(target)}</code>\n"
                   f"Now: <code>{_esc(_allow_csv(new))}</code>")
        out = (f"✅ {verb.lower()} <code>{_esc(target)}</code>\n"
               f"Whitelist: <code>{_esc(_allow_csv(new))}</code>")
        if sub == "add":
            out += (f"\nLevel: {_FRIEND_LEVEL_LABEL.get(_friend_level(target), '')} — "
                    f"change with <code>!whitelist perms {_esc(target)} talk|gate|full</code>")
        return out
    return ("Usage: <code>!whitelist list|add|remove|perms "
            "&lt;user_id&gt; [talk|gate|full]</code>")


async def _gs_cmd(adapter: Any, arg: str) -> str:
    """!gs list|open|lock|reset <user_id> — guest session control (panel twin)."""
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
                    "guest link — or open one yourself with "
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
    """!gate show|allow|deny — the safeguard lists, editable without the panel."""
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
            return (f"Usage: <code>!gate {sub} &lt;tool&gt;</code> — prefix "
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
    (TELEGRAM_ALLOWED_USERS — the authz mixin reads it first and, while it is
    non-empty, never consults the plugin's list), and the plugin's own route
    decision. The gate line is the tier that dropped whitelisted friends as
    "unrecognized" before 2026-10-02; it is shown so a divergence is visible.
    """
    file_ids = _read_allow_from()
    raw = _adapter_allow_raw()
    if raw is None:
        snap_ids = None
        snap = "(not set — the prefilter falls through to runner auth)"
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
            lines += ["", "⚠️ file and live snapshot DISAGREE — a whitelist write "
                      "(panel → ⚡ Actions) syncs them, or restart the gateway."]
    return "\n".join(x for x in lines if x)


async def _bang_execute(adapter: Any, chat_id: str, text: str,
                        session_store: Any = None) -> Optional[str]:
    """Run one bang command; returns the reply text (the caller decides delivery)."""
    parts = text.strip().split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""
    st = settings()
    reply: Optional[str] = None

    if cmd == "!help":
        reply = _help_text(st)
    elif cmd == "!panel":
        reply = _panel_text(st)
    elif cmd == "!users":
        reply = _fmt_users()
    elif cmd == "!settings":
        reply = _settings_summary()
    elif cmd == "!send":
        bits = arg.split(maxsplit=1)
        if len(bits) == 2:
            target, msg = bits[0], bits[1]
            try:
                bot = getattr(adapter, "_bot", None)
                await bot.send_message(chat_id=target, text=msg[:4000])
                reply = f"✅ DM sent to <code>{_esc(target)}</code> ({len(msg)} chars)"
                await _log("📨 Bot DM sent",
                           f"<b>To:</b> <code>{_esc(target)}</code>\n<b>Text:</b> <i>{_esc(msg[:500])}</i>")
            except Exception as e:
                # failure is already visible inline as this reply — no log-channel detour
                reply = f"❌ {type(e).__name__}: {_esc(str(e)[:300])}"
        else:
            reply = "Usage: <code>!send &lt;user_id&gt; &lt;text&gt;</code>"
    elif cmd == "!setlog":
        val = arg.strip()
        low = val.lower()
        if low in ("here", "this", "now", "."):
            # The point of `here`: send it inside the chat you want to log —
            # no id to copy, no risk of typos. Works from any group/channel.
            if str(chat_id).startswith(GUEST_CHAT_PREFIX):
                reply = ("This is a guest chat — Telegram hides who sent it. "
                         "Send <code>!setlog here</code> inside the real channel or group instead.")
            else:
                save_settings({"log_channel": str(chat_id)})
                reply = f"Log channel → <code>{_esc(str(chat_id))}</code> <i>(this chat)</i>"
                await _log("🧭 Log channel configured",
                           "Log channel set — guest-mode activity will be posted here.")
        elif low in ("off", "none", "-"):
            save_settings({"log_channel": None})
            reply = "Log channel cleared."
        elif val:
            save_settings({"log_channel": val})
            reply = f"Log channel → <code>{_esc(val)}</code>"
            await _log("🧭 Log channel configured",
                       "Log channel set — guest-mode activity will be posted here.")
        else:
            reply = f"Log channel = <code>{_esc(st.get('log_channel'))}</code>"
    elif cmd == "!setowner":
        if arg.strip():
            save_settings({"owner_id": arg.strip()})
            reply = f"owner_id → <code>{_esc(arg.strip())}</code> (hot — no restart)"
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
        try:
            save_settings({"unauthorized_cooldown_s": int(arg.strip())})
            reply = f"cooldown → {int(arg.strip())}s"
        except Exception:
            reply = f"cooldown = {st.get('unauthorized_cooldown_s')}s"
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
            reply = f"🔒 <b>Locked</b> <code>{_esc(chat_id)}</code> — back to guest rules."
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
            reply = ("Usage: <code>!wipe &lt;chat_id&gt;</code> — deletes that chat's session "
                     "and starts a fresh one <i>there</i>. This chat is never wiped implicitly.")
        else:
            n = _wipe_sessions(session_store, target)
            if n is None:
                reply = "⚠️ session store unavailable — send the command as a chat message"
            elif n == 0:
                reply = f"Nothing to wipe — no session found for <code>{_esc(target)}</code>"
            else:
                reply = f"🧹 wiped <b>{n}</b> session(s) for <code>{_esc(target)}</code> — fresh start there"
                await _log("🧹 Session wiped",
                           f"<b>Chat:</b> <code>{_esc(target)}</code> · <b>Reset:</b> {n}")
    elif cmd == "!whitelist":
        reply = await _whitelist_cmd(adapter, arg)
    elif cmd == "!auth":
        reply = _auth_debug(arg.strip())
    elif cmd == "!gs":
        reply = await _gs_cmd(adapter, arg)
    elif cmd == "!gate":
        reply = await _gate_cmd(arg)
    else:
        return (f"Unknown command <code>{_esc(cmd)}</code> — see <code>!help</code>.")
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
# replaces — so button and command can never drift apart.
_WIZARD: Dict[str, Dict[str, Any]] = {}

_WIZ_FLOWS: Dict[str, Dict[str, Any]] = {
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
        "prompts": ["⏱ <b>Cooldown</b>\n\nSend the seconds a stranger waits before the "
                    "canned reply may repeat.\n<i>Type cancel to abort.</i>"],
        "build": lambda d: f"!setcooldown {d[0]}"},
    "unauth": {
        "prompts": ["👾 <b>Stranger reply</b>\n\nSend the exact text a stranger gets as "
                    "the canned reply.\n<i>Type cancel to abort.</i>"],
        "build": lambda d: f"!setunauthorized {d[0]}"},
    "send": {
        "prompts": ["📨 <b>Send a DM as the bot</b>\n\nStep 1/2 — send the user id.\n"
                    "<i>Type cancel to abort.</i>",
                    "📨 <b>Send a DM as the bot</b>\n\nStep 2/2 — send the message text."],
        "build": lambda d: f"!send {d[0]} {d[1]}"},
    "wlperm": {
        "prompts": ["🛡 <b>Friend permission level</b>\n\nStep 1/2 — send the whitelisted "
                    "user id.\n<i>Type cancel to abort.</i>",
                    "🛡 <b>Step 2/2</b> — send the level:\n"
                    "  <b>talk</b> — safe tools only (web, vision, skills)\n"
                    "  <b>gate</b> — same rules as the guest tool gate\n"
                    "  <b>full</b> — no tool gating\n"
                    "<i>Type cancel to abort.</i>"],
        "build": lambda d: f"!whitelist perms {d[0]} {d[1]}"},
    "authdbg": {
        "prompts": ["🩺 <b>Auth debug</b>\n\nSend the user id to check — config file vs "
                    "the live prefilter snapshot vs the plugin's route.\n"
                    "<i>Type cancel to abort.</i>"],
        "build": lambda d: f"!auth {d[0]}"},
    "gsopen": {
        "prompts": ["🔐 <b>Open a guest session</b>\n\nSend the guest's user id (the id they "
                    "carry on the guest link). The session will accept plain mentions — no "
                    "reply-to-ATRA needed.\n<i>Type cancel to abort.</i>"],
        "build": lambda d: f"!gs open {d[0]}"},
    "gslock": {
        "prompts": ["🔐 <b>Lock a guest session</b>\n\nSend the user id whose session should "
                    "be locked — works on auto-created sessions and on ones you opened.\n"
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
        "prompts": ["👾 <b>Guest error reply — English</b>\n\nSend the exact text a guest gets "
                    "when a run fails.\n<i>Type cancel to abort.</i>"],
        "build": lambda d: f"!seterror {d[0]}"},
    "err_fa": {
        "prompts": ["👾 <b>Guest error reply — Persian</b>\n\nSend the exact text a guest gets "
                    "when a run fails in Persian.\n<i>Type cancel to abort.</i>"],
        "build": lambda d: f"!seterrorfa {d[0]}"},
    "lockreply": {
        "prompts": ["🔒 <b>Locked session reply</b>\n\nSend the exact text a locked guest "
                    "session answers with.\n<i>Type cancel to abort.</i>"],
        "save": lambda d: {"guest_locked_reply": d[0][:400]},
        "validate": lambda d: "" if d[0].strip() else "send some text",
        "done": "✅ locked reply updated."},
    "emoji_recv": {
        "prompts": ["🔁 <b>Reaction — message received</b>\n\nSend the emoji ATRA drops when "
                    "your message arrives.\n<i>Type cancel to abort.</i>"],
        "save": lambda d: {"react_emoji_receive": d[0][:32]},
        "validate": lambda d: "" if d[0].strip() else "send an emoji",
        "done": "✅ receive reaction updated."},
    "emoji_done": {
        "prompts": ["✅ <b>Reaction — done</b>\n\nSend the emoji ATRA drops when a run "
                    "succeeds.\n<i>Type cancel to abort.</i>"],
        "save": lambda d: {"react_emoji_done": d[0][:32]},
        "validate": lambda d: "" if d[0].strip() else "send an emoji",
        "done": "✅ done reaction updated."},
    "emoji_err": {
        "prompts": ["❌ <b>Reaction — error</b>\n\nSend the emoji ATRA drops when a run "
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
        "done": "✅ persona path updated — used on the next guest message."},
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
        "prompts": ["⌛ <b>Update timeout</b>\n\nSend the seconds git plus the test suite may "
                    "take (10–1800).\n<i>Type cancel to abort.</i>"],
        "save": lambda d: {"update_timeout_s": int(d[0].strip())},
        "validate": lambda d: ("" if d[0].strip().isdigit() and 10 <= int(d[0].strip()) <= 1800
                               else "send a whole number of seconds between 10 and 1800"),
        "done": "✅ update timeout updated."},
}


def _wizard_start(chat_id: Any, flow: str) -> Optional[str]:
    """Open a wizard in this chat; returns the first prompt, or None."""
    f = _WIZ_FLOWS.get(flow)
    if not f:
        return None
    _WIZARD[str(chat_id)] = {"flow": flow, "data": []}
    return f["prompts"][0]


def _wizard_cancel(chat_id: Any) -> None:
    _WIZARD.pop(str(chat_id), None)


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
        return None
    if text.strip().lower() in ("cancel", "/cancel", "!cancel", "stop"):
        _WIZARD.pop(key, None)
        return "✖ cancelled."
    w["data"].append(text.strip()[:4000])
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
            return f"⚠️ {_esc(str(err))}\n\n{f['prompts'][len(w['data'])]}"
    _WIZARD.pop(key, None)
    try:
        if f.get("save") is not None:
            save_settings(f["save"](w["data"]))
            return f.get("done") or "✅ saved."
        out = await _bang_execute(adapter, key, f["build"](w["data"]),
                                  session_store=session_store)
    except Exception:
        logger.exception("[TGAhermes] wizard command failed")
        return "❌ the wizard failed — check the gateway log."
    return out or "✅ done."


# ---------------------------------------------------------------- pre_gateway_dispatch hook

def _live_adapter(gateway: Any) -> Any:
    """Live telegram adapter from the gateway runner — keeps hook-only reloads working."""
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
    forces a rewire. Only the dispatch hook used to do that — which meant a
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
    except Exception:
        logger.debug("[TGAhermes] post-reload rewire failed", exc_info=True)


async def _pre_gateway_dispatch(event=None, gateway=None, session_store=None, **_) -> Optional[dict]:
    """Observe + console: bang commands (skip), owner reactions, group mentions, owner mirror."""
    chat_for_error: Any = None
    try:
        if event is None:
            return None
        # MessageEvent has no `platform` field — platform lives on event.source (Platform enum).
        _plat = getattr(getattr(event, "source", None), "platform", None)
        if getattr(_plat, "value", _plat) != "telegram":
            return None
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
            return None
        # Hot-reload rewire trigger: see _maybe_rewire — one check on the first
        # dispatched message makes the adapter rewire, the factory's per-deploy
        # qualname key is then unknown to the wired set, so it runs, sweeps the
        # stale handlers and re-syncs allow_from.
        _maybe_rewire()
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
                    # existed) — this is the tier that drops friends as
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

        # Bang console: owner, in the log channel or their own DM — plus
        # `!setlog …` from ANY group/channel they are in, so `!setlog here`
        # works from inside the chat they want to log (its id is the message's).
        in_log = st.get("log_channel") and chat == str(st["log_channel"])
        in_owner_dm = owner and chat == owner and (src.chat_type or "") == "dm"
        in_group = (src.chat_type or "") in ("group", "supergroup", "forum", "channel")
        setlog_here = in_group and text.lower().startswith("!setlog")
        if text.startswith("!") and owner and uid == owner and (in_log or in_owner_dm or setlog_here):
            _WIZARD.pop(chat, None)  # a real command abandons any open wizard step
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
                await q.answer(f"{_VIEW_LABEL.get(key, '🧩')} — already showing", show_alert=False)
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
                # reload keep working — but it routes through the confirm screen
                # like every other flag, it does not flip anything itself.
                view, arg = "cfm", f"settings:tg:{sub}"
                body = _cfm_view("tg", f"{sub}:settings", st)
            elif action == "settings":
                view, body = "settings", _settings_view(st)
            elif action == "wl":
                view, body = "wl", _wl_view(st)
            elif action == "wlfr" and sub:
                view, body, arg = "wlfr", _wlfr_view(sub, st), sub
            elif action == "wllvl":
                # v3.3: changing a friend's permission level confirms first.
                lvl = bits[4] if len(bits) > 4 else ""
                if sub and lvl in _FRIEND_LEVELS:
                    view, arg = "cfm", f"wl:wl:{sub}:{lvl}"
                    body = _cfm_view("wl", f"{sub}:{lvl}", st)
                else:
                    view, body = "wl", _wl_view(st)
            elif action == "wlrm" and sub:
                view, body, arg = "wlrm", _wlrm_view(sub, st), sub
            elif action == "wlrm2" and sub:
                # v3.3: the second tap is the confirm — removal happens here.
                view = "wl"
                out = await _bang_execute(ad, _msg_chat_id(q.message) or "",
                                          f"!whitelist remove {sub}")
                st = settings()
                body = _wl_view(st, note=out or f"🗑 removed <code>{_esc(sub)}</code>")
            elif action == "tg" and sub:
                # confirm screen: panel:tg:<sub-key>:<origin> — changes nothing yet
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
                    body = (f"<b>⏱ Cooldown</b> — how long a stranger waits before the "
                            f"canned reply may repeat\ncurrent: <b>{st.get('unauthorized_cooldown_s')}s</b>\n"
                            "Tap a preset, or type <code>!setcooldown &lt;seconds&gt;</code>")
            elif action == "gate":
                # v3.3: `panel:gate` with no sub used to match no branch at all and
                # land back on the home grid with no body — the reported bug. An
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
                    # access, so it confirms first — it is not a navigation tap.
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
                # THE writer — one place where Apply changes a setting. The
                # callback is `cfmok:<kind>:<origin>:<payload>`; the origin is
                # stripped first so each kind parses only its own payload.
                _kind = sub
                _origin, _, _pay = (":".join(bits[4:])).partition(":")
                st = settings()
                if _kind == "tg":
                    _ts, _to = _pay, (_origin or "settings")
                    # Land back on the page the tap came from, not on the home
                    # grid — otherwise a mode change from Safeguards dumps you
                    # out of the section you were working in.
                    view = _to if _to in _VIEW_LABEL else "settings"
                    if _ts == "mode":
                        nxt = _tg_next("mode", st)
                        save_settings({"guest_tool_mode": nxt})
                        st = settings()
                        note = f"🛡 guest mode → <b>{_MODE_LABEL.get(nxt, nxt)}</b>"
                        await _log("🛡 Guest tool mode",
                                   f"Mode set to <b>{nxt}</b> (owner {_esc(str(_owner_id()))})")
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
                    else:
                        note = "❌ unknown setting."
                        view = "settings"
                    view, body = view, _view_body(view, st, note, arg=f"{_ts}:{view}")
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
                    note = ("🔓 updates unlocked — check/install re-enabled" if on
                            else "🔒 updates locked — check, install and the "
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
                                       "Log channel set — guest-mode activity will "
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
                    # unknown kind — refuse, never guess a target page
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
                # v3.3: same — the confirm states which chat will be logged.
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
            # The console resets the stack — it is the root of every path.
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
                                 or (f"{_VIEW_LABEL.get(view, '🧩')} — already showing"
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
            n = _wipe_sessions(None, _chat)
            if n:
                await q.answer("Session wiped ✅")
                await _log("🧹 Session wiped",
                           f"<b>Chat:</b> <code>{_esc(_chat)}</code> · <b>Reset:</b> {n} (button)")
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
    user = msg.from_user
    uid = str(getattr(user, "id", "") or "")
    owner = _owner_id(adapter)
    text = str(msg.text or "")
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
        "📩 Stranger DM" + (" — /start" if text.lstrip().lower().startswith("/start") else ""),
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


# ---------------------------------------------------------------- telegram_admin tool

_TOOL_DESCRIPTION = (
    "Telegram admin actions — owner session only; the bot must be admin in the target chat. "
    "delete_message, ban_user, unban_user, mute_user, unmute_user, get_member, chat_info, "
    "react (set a reaction), send_dm (bot DMs a user), pin_message, unpin_message, "
    "bang (run any !console command — setlog/setowner/whitelist/wipe/settings/...). "
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
    """Owner-session gate: telegram sessions must be the owner's DM; other sources are local/owner."""
    row = _session_row(session_id)
    if not row:
        return False
    source, chat_id = row
    if source != "telegram":
        return True  # cli/cron/local sessions live on the owner's machine
    return bool(_owner_id()) and chat_id == _owner_id()


# ---------------------------------------------------------------- guest safety gate
# Tools stay AVAILABLE in guest mode (the panel is not locked down); what is
# refused is the irreversible: mutating or deleting state on this box, reaching
# the owner's files/history, or acting on the owner's behalf without him. A low
# risk request from the guest is answered normally; anything destructive is
# bounced back with "do it in your DM" instead of being executed.

GUEST_BLOCKED_TOOLS = frozenset({
    # shell / arbitrary code — anything a guest names runs as root on this box
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
#   * secrets — reading them pastes credentials into a guest chat (the leak);
#   * execution — writing hooks/, scripts/ or cron/ runs code with the
#     owner's rights, and those jobs fire every minute on their own.
# SOUL/AGENTS/settings/state carry identity, config and the user registry.
# Only the path ARGUMENTS of read_file/write_file/patch are matched, so
# prose that happens to mention "config.yaml" is not collateral damage —
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

    Identity comes from :func:`_guest_identity` — the same source the log
    channel reads — so the gate, the log and the session table always agree.
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
            "other tool from a guest chat, and do not try again — answer the "
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
            f"or delete something on the machine, so it is never run from a guest chat — not even "
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
    """Argument/path tripwires — shared by the guest gate and the friend gate."""
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
    """Refusal for a whitelisted friend's gated tool — final, with a next step."""
    if repeats:
        tail = ("You have already been told this is not possible. Do not call any other "
                "blocked tool and do not try again — answer NOW with what you already have.")
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
    info = _guest_session_info(session_id)
    if info is None:
        # Whitelisted friend in their OWN DM session (source telegram, chat = their
        # id, no guest_ prefix): their permission level decides — not the guest gate,
        # not nothing. Owner always passes.
        row = _session_row(session_id)
        if row and row[0] == "telegram" and not _is_guest_chat(row[1]):
            uid = row[1]
            owner = str(_owner_id() or "")
            if uid and uid != owner and uid in _read_allow_from():
                level = _friend_level(uid)
                danger = level != "full" and (
                    level == "talk"
                    or name in _guest_allowed(frozenset())
                    or _tripwire_danger(name, args))
                if danger:
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
    falls back to — so anything rendering cfg["repo"] would otherwise print a
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

    The gateway's rewire dedups factories by ``(plugin, qualname)`` — a key that
    never changes between loads of the same source file — so a hot reload alone
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
    default), which is exactly how an orphan stayed invisible — they are
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
                if getattr(cb, "__globals__", None) is mine:
                    continue  # registered by THIS very instance — keep it
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
        # The adapter's config snapshot predates every config set made since it
        # first wired; push the file's current allow_from into it so a plugin
        # (re)load alone is enough for the core prefilter to agree with the file
        # again — without this, a fresh whitelist write keeps getting blocked
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
        if native is None:
            return
        # Previous load's handlers must go before ours, or PTB keeps matching
        # the old closures (first match per group) and the panel never updates.
        _drop_stale_handlers(native)
        try:
            from telegram.ext import CallbackQueryHandler, MessageHandler, filters

            gfilter = getattr(filters.UpdateType, "GUEST_MESSAGE", None)
            if gfilter is None:
                logger.warning("[%s] PTB lacks UpdateType.GUEST_MESSAGE — guest mode inactive",
                               getattr(adapter, "name", "telegram"))
            else:
                async def _guest(update, context):
                    await _handle_guest_message(adapter, update, context)
                # MUST be first: guest updates also match filters.TEXT.
                native.add_handler(MessageHandler(gfilter, _guest))

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
    # this factory on every hot reload — the exact failure that left stale
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
        ctx.register_tool(name="telegram_admin", toolset="telegram_admin",
                          schema=_TOOL_SCHEMA, handler=_tool_handler_json,
                          description=_TOOL_DESCRIPTION, emoji="\U0001f6e1️", is_async=True,
                          check_fn=_tool_check)
        ctx.register_telegram_handler(_make_factory())
        logger.info("[TGAhermes] v2 active (hook + tool + PTB factory)")
    except Exception:
        logger.exception("[TGAhermes] register failed")
