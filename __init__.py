"""telegram-guest-mode — all-in-one Telegram guest mode, logging console and admin tools for Hermes.

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
  ``!help !users !send !settings !setlog !setowner !whitelist add|remove|list !wipe [chat_id]
  !setunauthorized !seterror !seterrorfa !setreact !setmedia !setcooldown`` — texts/ids
  editable live. Every chat (DM / group / guest) is its own session; ``!wipe`` (or the 🧹
  button on log entries) resets it. The whitelist adds friends who talk to the real bot.
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
        logger.exception("[telegram-guest-mode] settings read failed; using defaults")
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
        logger.exception("[telegram-guest-mode] state read failed")
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
            logger.debug("[telegram-guest-mode] background task failed", exc_info=True)

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
        logger.debug("[telegram-guest-mode] reaction failed", exc_info=True)
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
        logger.info("[telegram-guest-mode] answered guest query %s", gqid)
        return True
    except Exception as exc:
        logger.warning("[telegram-guest-mode] answer_guest_query failed for %s: %s", gqid, exc)
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
        logger.debug("[telegram-guest-mode] media result build failed", exc_info=True)
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
        logger.debug("[telegram-guest-mode] inline error notice failed", exc_info=True)
    await _log(title, body)


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
        logger.warning("[telegram-guest-mode] log channel post failed", exc_info=True)
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
        logger.debug("[telegram-guest-mode] chat name lookup failed for %s", chat_id)
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

    # --- unauthorized plain mention: canned reply + log (with cooldown) ------------
    if reply_to is None and not is_owner:
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
            logger.info("[telegram-guest-mode] guest mention by %s suppressed by cooldown", user_id or "?")
        return

    try:
        from gateway.platforms.event import MessageType
        event = adapter._build_message_event(
            guest, MessageType.TEXT, update_id=getattr(update, "update_id", None))
    except Exception:
        logger.exception("[telegram-guest-mode] failed to build guest event")
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
            logger.debug("[telegram-guest-mode] could not set source.user_id", exc_info=True)
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
        f"{_user_block(user)}\n<b>Sender:</b> {_esc(sender_kind)} · <b>Trigger:</b> {_esc(trigger_kind)}"
        f"\n<b>Chat:</b> <code>{_esc(md.get('guest_original_chat_id'))}</code> (guest)"
        f"\n<b>Text:</b> <i>{_esc(str(event.text)[:500])}</i>",
        buttons=_profile_buttons(user, md.get("guest_original_chat_id") or None,
                                 md.get("guest_message_id") or None))
    if st.get("react_guests") and st.get("auto_react"):
        _spawn(_react(md.get("guest_original_chat_id"), md.get("guest_message_id"),
                      st.get("react_emoji_receive") or "👀"))
    if getattr(adapter, "_message_handler", None) is None:
        logger.warning("[telegram-guest-mode] guest summon received but no message handler installed")
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
                logger.debug("[telegram-guest-mode] turn marker release failed", exc_info=True)
            if text_content and str(text_content).strip():
                await _answer_guest_text(adapter, gqid, str(text_content))
            else:
                logger.info("[telegram-guest-mode] empty final for guest query %s", gqid)
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
            logger.warning("[telegram-guest-mode] guest clarify failed: %s", e)
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
            logger.warning("[telegram-guest-mode] guest prompt %s failed: %s", what, e)
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
        logger.error("[telegram-guest-mode] guest turn failed: %s", e, exc_info=e)
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
                logger.exception("[telegram-guest-mode] owner error notice failed")
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
    logger.info("[telegram-guest-mode] outbound wraps installed (v2)")


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


def _write_allow_from(ids: List[str]) -> bool:
    """Persist telegram.extra.allow_from via the hermes CLI (subprocess — safe from the gateway)."""
    csv = ",".join(dict.fromkeys(str(x).strip() for x in ids if str(x).strip()))
    try:
        import shutil
        import subprocess
        exe = shutil.which("hermes")
        if not exe:
            logger.error("[telegram-guest-mode] hermes CLI not found; whitelist not saved")
            return False
        env = dict(os.environ)
        env["HERMES_HOME"] = str(_hermes_home())
        proc = subprocess.run([exe, "config", "set", "telegram.extra.allow_from", csv],
                              capture_output=True, text=True, timeout=90, env=env)
        if proc.returncode != 0:
            logger.error("[telegram-guest-mode] config set failed: %s",
                         (proc.stderr or proc.stdout or "")[:400])
            return False
        _nudge_gateway_reload()  # live adapters pick up the new allow_from now
        return True
    except Exception:
        logger.exception("[telegram-guest-mode] whitelist write failed")
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
        logger.debug("[telegram-guest-mode] reload nudge failed", exc_info=True)


def _is_authorized_user(uid: str, owner: str = "") -> bool:
    """Owner or whitelisted friend — gets the real brain (core handlers), not the canned reply."""
    uid = str(uid or "")
    if not uid:
        return False
    if owner and uid == owner:
        return True
    return uid in _read_allow_from()


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
        logger.exception("[telegram-guest-mode] session wipe failed")
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
         "<code>!settings</code> — editable settings\n"
         "<code>!users</code> — who used the bot\n"
         "<code>!panel</code> — this glass-button panel (same as !help)"),
        ("log", "📡 Log",
         f"now: <code>{log_now}</code>\n<code>!setlog &lt;id|@name|off&gt;</code>"),
        ("access", "🛡 Access",
         f"{len(wl)} whitelisted\n"
         "<code>!whitelist list</code>\n"
         "<code>!whitelist add &lt;user_id&gt;</code>\n"
         "<code>!whitelist remove &lt;user_id&gt;</code>\n"
         "Whitelisted friends talk to the real bot, not the canned reply."),
        ("sessions", "🧹 Sessions",
         "one per chat; wipe = fresh start\n"
         "<code>!wipe &lt;chat_id&gt;</code> — delete that chat's session, fresh start "
         "<i>there</i> (any DM/group/guest; works from the log channel too)\n"
         "Or use the 🧹 button under a log entry."),
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


def _help_view(key: str, st: Optional[Dict[str, Any]] = None) -> str:
    """One section (or the full list) as HTML."""
    st = st or settings()
    if key == "full":
        return _help_text(st)
    if key == "system":
        return _system_view(st)
    for k, title, body in _help_sections(st):
        if k == key:
            return f"<b>{title}</b>\n{body}"
    return _help_text(st)


def _system_view(st: Dict[str, Any]) -> str:
    """Body text for the System tab."""
    cfg = _update_settings()
    lines = [
        "<b>🔧 System</b>",
        f"<b>Installed version:</b> <code>{_esc(_plugin_version())}</code>",
        f"<b>Updates:</b> {'enabled' if st.get('update_enabled', True) else '🔒 locked'}",
        f"<b>Source:</b> <code>{_esc(cfg['repo'])}</code> "
        f"<code>({_esc(str(cfg['branch']))})</code>",
        "",
        "Checking compares the installed version against the source. Installing "
        "backs up the current files, copies the newer ones, runs the test suite, "
        "and only then hot-reloads. Your settings and learned state are never "
        "touched.",
    ]
    return "\n".join(lines)


def _gate_view(st: Dict[str, Any]) -> str:
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


def _panel_text(st: Optional[Dict[str, Any]] = None, note: str = "") -> str:
    """Dashboard for !panel — live status lines + the glass buttons beneath it."""
    st = st or settings()
    wl = _read_allow_from()

    def _flag(key: str) -> str:
        return "<b>on</b>" if st.get(key) else "<b>off</b>"

    lines = [
        f"🧩 <b>ATRA console</b> v{_plugin_version()} — owner only",
        f"📡 log: <code>{_esc(st.get('log_channel') or 'off')}</code> · "
        f"🛡 whitelist: <b>{len(wl)}</b> · 👑 owner: <code>{_esc(_owner_id() or 'unset')}</code>",
        f"🔁 reactions: {_flag('auto_react')} · 👥 guest reacts: {_flag('react_guests')} · "
        f"🖼 guest media: {_flag('media_to_guests')}",
        f"⏱ cooldown: <b>{_esc(st.get('unauthorized_cooldown_s'))}s</b> · "
        f"👑 mirror: {_flag('log_owner_messages')} · 📣 mentions: {_flag('log_group_mentions')} · "
        f"🛠 tool: {_flag('tool_enabled')}",
        f"💬 whitelisted msgs: {_flag('log_whitelisted_messages')} · "
        f"🗨 other msgs: {_flag('log_other_messages')}",
    ]
    if note:
        lines.append(note)
    lines += [
        "",
        "<b>Sections</b> — tap a button, or type a command:",
        "<code>!panel</code> · <code>!help</code> · <code>!wipe &lt;chat_id&gt;</code> · "
        "<code>!setlog &lt;id|off&gt;</code> · <code>!whitelist add &lt;id&gt;</code>",
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


def _help_keyboard(view: str = "panel", st: Optional[Dict[str, Any]] = None,
                   chat_id: Optional[str] = None) -> list:
    """Per-tab button sets: the full console grid at home, contextual actions inside a tab."""
    from telegram import InlineKeyboardButton as B
    st = st or settings()
    log = str(st.get("log_channel") or "")
    rows: list = []

    def _mark(key: str) -> str:
        return "\u2705" if st.get(key) else "\u23f8"

    def add(*pairs) -> None:
        if pairs:
            rows.append([B(lbl, callback_data=f"{_CB_PREFIX}{path}") for lbl, path in pairs])

    if view == "panel":
        add(("\u2139\ufe0f Status", "help:status"), ("\U0001f4e1 Log", "help:log"),
            ("\U0001f6e1 Access", "help:access"))
        add(("\U0001f9f9 Sessions", "help:sessions"), ("\U0001f47e Guests", "help:guests"),
            ("\U0001f916 Bot", "help:bot"))
        add(("\U0001f527 System", "help:system"))
        add(("\U0001f4dc Full help", "help:full"))
        add((f"\U0001f501 Reactions {_mark('auto_react')}", "panel:toggle:react"),
            (f"\U0001f465 Guest reacts {_mark('react_guests')}", "panel:toggle:greact"),
            (f"\U0001f5bc Guest media {_mark('media_to_guests')}", "panel:toggle:media"))
        add((f"\U0001f451 Mirror {_mark('log_owner_messages')}", "panel:toggle:mirror"),
            (f"\U0001f4e3 Mentions {_mark('log_group_mentions')}", "panel:toggle:mentions"),
            (f"\U0001f6e0 Tool {_mark('tool_enabled')}", "panel:toggle:tool"))
        add((f"\U0001f4ac WL msgs {_mark('log_whitelisted_messages')}", "panel:toggle:wmsgs"),
            (f"\U0001f5e8 Other msgs {_mark('log_other_messages')}", "panel:toggle:omsgs"))
        add(("\U0001f4cb Users", "panel:out:users"), ("\u2699\ufe0f Settings", "panel:out:settings"),
            ("\U0001f6e1 Whitelist", "panel:out:whitelist"))
        add(("\U0001f47b Guest texts", "panel:out:guests"),
            (f"\u23f1 Cooldown {st.get('unauthorized_cooldown_s')}s", "panel:cool"))
    elif view == "status":
        add(("\u2699\ufe0f Settings", "panel:out:settings"), ("\U0001f4cb Users", "panel:out:users"))
        add((f"\U0001f6e0 Tool {_mark('tool_enabled')}", "panel:toggle:tool"),
            (f"\U0001f451 Mirror {_mark('log_owner_messages')}", "panel:toggle:mirror"))
    elif view == "log":
        add((f"\U0001f4e3 Mentions {_mark('log_group_mentions')}", "panel:toggle:mentions"),
            (f"\U0001f451 Mirror {_mark('log_owner_messages')}", "panel:toggle:mirror"))
        add((f"\U0001f4ac WL msgs {_mark('log_whitelisted_messages')}", "panel:toggle:wmsgs"),
            (f"\U0001f5e8 Other msgs {_mark('log_other_messages')}", "panel:toggle:omsgs"))
        if log:
            add(("\U0001f4e1 Turn log off", "panel:logoff"), (f"\U0001f9f9 Wipe log chat", f"wipe:{log}"))
        else:
            add(("\U0001f4e1 Log is off", "panel:out:settings"))
    elif view == "access":
        add(("\U0001f6e1 Whitelist", "panel:out:whitelist"), ("\U0001f4cb Users", "panel:out:users"))
        if chat_id:
            add((f"\U0001f9f9 Wipe this chat", f"wipe:{chat_id}"))
    elif view == "sessions":
        if chat_id:
            add((f"\U0001f9f9 Wipe this chat", f"wipe:{chat_id}"))
        if log:
            add((f"\U0001f9f9 Wipe log chat", f"wipe:{log}"))
        add(("\u2699\ufe0f Settings", "panel:out:settings"))
    elif view == "guests":
        add((f"\u23f1 Cooldown {st.get('unauthorized_cooldown_s')}s", "panel:cool"),
            ("\U0001f47b Guest texts", "panel:out:guests"))
        add((f"\U0001f465 Guest reacts {_mark('react_guests')}", "panel:toggle:greact"),
            (f"\U0001f5bc Guest media {_mark('media_to_guests')}", "panel:toggle:media"))
    elif view == "bot":
        add((f"\U0001f6e0 Tool {_mark('tool_enabled')}", "panel:toggle:tool"),
            (f"\U0001f451 Mirror {_mark('log_owner_messages')}", "panel:toggle:mirror"))
        add(("\u2699\ufe0f Settings", "panel:out:settings"), ("\U0001f4cb Users", "panel:out:users"))
    elif view == "system":
        add((f"🛡 Guest mode: {_MODE_LABEL.get(str(st.get('guest_tool_mode') or 'balanced'), 'balanced')}",
             "panel:gate"))
        add(("⬇️ Guest tool rules", "panel:gate:list"))
        if st.get("update_enabled", True):
            add(("🔍 Check for update", "panel:upd:check"))
            add(("⬆️ Install update", "panel:upd:apply"))
        else:
            add(("🔒 Updates locked", "panel:upd:check"))
        add((f"v{_plugin_version()}", "panel:upd:check"))
    elif view == "out":
        add(("\U0001f4cb Users", "panel:out:users"), ("\u2699\ufe0f Settings", "panel:out:settings"),
            ("\U0001f6e1 Whitelist", "panel:out:whitelist"))
    elif view == "full":
        add(("\u2139\ufe0f Status", "help:status"), ("\U0001f4e1 Log", "help:log"),
            ("\U0001f6e1 Access", "help:access"))
        add(("\U0001f9f9 Sessions", "help:sessions"), ("\U0001f47e Guests", "help:guests"),
            ("\U0001f916 Bot", "help:bot"))

    if view != "panel":
        rows.insert(0, [B("\u2b05\ufe0f Console", callback_data=f"{_CB_PREFIX}panel:back")])
        add(("\U0001f4dc Full help", "help:full"))
    if view == "cool":
        add(*[(f"{n}s", f"panel:cool:{n}") for n in (0, 60, 300, 3600)])
    if chat_id and view in ("panel", "sessions", "access"):
        add(("\U0001f9f9 Wipe this chat", f"wipe:{chat_id}"))
    return rows


_MODE_LABEL = {"strict": "🔒 strict", "balanced": "⚖️ balanced", "open": "🔓 open"}

_VIEW_LABEL = {"full": "📜 Full help", "panel": "🧩 Console", "out": "📋 Output",
               "status": "ℹ️ Status", "log": "📡 Log", "access": "🛡 Access",
               "sessions": "🧹 Sessions", "bot": "🤖 Bot", "guests": "👾 Guests",
               "cool": "⏱ Cooldown", "system": "🔧 System"}


def _msg_chat_id(msg: Any) -> Optional[str]:
    cid = getattr(msg, "chat_id", None) or getattr(getattr(msg, "chat", None), "id", None)
    return str(cid) if cid is not None else None


async def _panel_edit(q: Any, body: str, view: str, st: Dict[str, Any]) -> str:
    """Swap a panel message in place.

    Returns "ok" when Telegram accepted the edit, "same" when the tap would not change
    anything (Telegram 400s on an identical edit — that is a no-op, not a failure), and
    "failed" for anything real (logged so it is diagnosable instead of silent).
    """
    from telegram import InlineKeyboardMarkup
    msg = q.message
    mk = InlineKeyboardMarkup(_help_keyboard(view, st, chat_id=_msg_chat_id(msg)))
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
        logger.warning("[telegram-guest-mode] panel edit failed (%s): %s",
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
            logger.debug("[telegram-guest-mode] HTML console reply failed; plain fallback",
                         exc_info=True)
    try:
        await adapter.send(cid, _html_plain(text))
    except Exception:
        logger.warning("[telegram-guest-mode] console reply failed", exc_info=True)


async def _whitelist_cmd(adapter: Any, arg: str) -> str:
    bits = arg.strip().split(maxsplit=1)
    sub = bits[0].lower() if bits else "list"
    val = bits[1].strip() if len(bits) > 1 else ""
    owner = _owner_id(adapter)
    if sub == "list":
        ids = _read_allow_from()
        rows = [f"<code>{_esc(i)}</code>" + (" 👑 owner" if i == owner else "") for i in ids]
        return "🛡 Whitelist (<code>telegram.extra.allow_from</code>):\n" + ("\n".join(rows) or "(empty)")
    if sub in ("add", "remove"):
        if not val:
            return "Usage: <code>!whitelist add|remove &lt;user_id&gt;</code>"
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
        ids = _read_allow_from()
        if sub == "add":
            if target in ids:
                return f"<code>{_esc(target)}</code> is already whitelisted."
            new = ids + [target]
        else:
            if target in ids and target == owner:
                return "Refusing to remove the owner — change the owner with <code>!setowner</code> first."
            if target not in ids:
                return f"<code>{_esc(target)}</code> is not whitelisted."
            new = [i for i in ids if i != target]
        if not _write_allow_from(new):
            return "❌ could not write config — see the gateway log"
        verb = "Added" if sub == "add" else "Removed"
        await _log("🛡 Whitelist updated",
                   f"<b>{verb}:</b> <code>{_esc(target)}</code>\nNow: <code>{_esc(','.join(new))}</code>")
        return f"✅ {verb.lower()} <code>{_esc(target)}</code>\nWhitelist: <code>{_esc(','.join(new))}</code>"
    return "Usage: <code>!whitelist list|add|remove &lt;user_id&gt;</code>"


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
        if val.lower() in ("off", "none", "-"):
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
    else:
        reply = f"Unknown command <code>{_esc(cmd)}</code> — try <code>!help</code>"
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
        src = event.source
        text = str(event.text or "")
        chat = str(src.chat_id or "")
        chat_for_error = chat
        uid = str(src.user_id or "")
        owner = _owner_id(ad)

        # Bang console: owner, in the log channel or their own DM.
        in_log = st.get("log_channel") and chat == str(st["log_channel"])
        in_owner_dm = owner and chat == owner and (src.chat_type or "") == "dm"
        if text.startswith("!") and owner and uid == owner and (in_log or in_owner_dm):
            await _run_bang_command(ad, event, text, session_store=session_store)
            return {"action": "skip", "reason": "telegram-guest-mode bang command"}

        # Tell the model where this turn came from (DM vs group vs channel, which
        # chat, whose message). Nothing else in the stack provides it.
        try:
            event.channel_prompt = _origin_identity_block(ad, src)
        except Exception:
            logger.exception("[telegram-guest-mode] channel origin block failed")

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
        logger.exception("[telegram-guest-mode] pre_gateway_dispatch failed")
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
        logger.info("[telegram-guest-mode] callback %s msg=%s", data,
                    type(getattr(q, "message", None)).__name__)
        if bot is None:
            await q.answer("Bot not connected.", show_alert=True)
            return
        if data.startswith(f"{_CB_PREFIX}help:"):
            key = data.split(":", 2)[2] if data.count(":") >= 2 else "full"
            st = settings()
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
            toggles = {"react": ("auto_react", "🔁 reactions"),
                       "greact": ("react_guests", "👥 guest reacts"),
                       "media": ("media_to_guests", "🖼 guest media"),
                       "mirror": ("log_owner_messages", "👑 mirror"),
                       "mentions": ("log_group_mentions", "📣 group mentions"),
                       "tool": ("tool_enabled", "🛠 admin tool"),
                       "wmsgs": ("log_whitelisted_messages", "💬 whitelisted msgs"),
                       "omsgs": ("log_other_messages", "🗨 other msgs")}
            view, note, body = "panel", "", ""
            if action == "toggle" and sub in toggles:
                key, label = toggles[sub]
                save_settings({key: not bool(st.get(key))})
                st = settings()
                note = f"{label} → <b>{'on' if st.get(key) else 'off'}</b>"
            elif action == "cool":
                if sub.isdigit():
                    save_settings({"unauthorized_cooldown_s": int(sub)})
                    st = settings()
                    note = f"⏱ cooldown → <b>{st.get('unauthorized_cooldown_s')}s</b>"
                else:
                    view = "cool"
                    body = (f"<b>⏱ Cooldown</b> — how long a stranger waits before the "
                            f"canned reply may repeat\ncurrent: <b>{st.get('unauthorized_cooldown_s')}s</b>\n"
                            "Tap a preset, or type <code>!setcooldown &lt;seconds&gt;</code>")
            elif action == "gate":
                if sub == "list":
                    view, body = "system", _gate_view(st)
                elif sub == "mode":
                    order = ["strict", "balanced", "open"]
                    cur = str(st.get("guest_tool_mode") or "balanced")
                    nxt = order[(order.index(cur) + 1) % len(order)] if cur in order else "balanced"
                    save_settings({"guest_tool_mode": nxt})
                    st = settings()
                    note = f"🛡 guest mode → <b>{_MODE_LABEL.get(nxt, nxt)}</b>"
                    body = _gate_view(st)
                    view = "system"
                    await _log("🛡 Guest tool mode",
                               f"Mode set to <b>{nxt}</b> (owner {_esc(str(_owner_id()))})")
                elif sub == "owner":
                    on = not bool(st.get("guest_owner_full_access", False))
                    save_settings({"guest_owner_full_access": on})
                    st = settings()
                    note = ("🔓 owner access ON in unlocked guest chats" if on
                            else "🔒 owner access OFF — everyone is gated here, you included")
                    body = _gate_view(st)
                    view = "system"
                    await _log("🛡 Guest owner access",
                               f"{'enabled' if on else 'disabled'} for unlocked guest chats")
                elif sub and sub.startswith("grant:"):
                    chat_key = sub.split(":", 1)[1].strip()
                    cur_list = [str(c) for c in (st.get("guest_owner_chats") or [])]
                    if chat_key and chat_key not in cur_list:
                        cur_list.append(chat_key)
                        save_settings({"guest_owner_chats": cur_list})
                    st = settings()
                    note = f"➕ unlocked <code>{_esc(chat_key)}</code> for your account"
                    body = _gate_view(st)
                    view = "system"
                    await _log("🛡 Guest chat unlocked", f"Unlocked for owner: {chat_key}")
                elif sub and sub.startswith("revoke:"):
                    chat_key = sub.split(":", 1)[1].strip()
                    cur_list = [str(c) for c in (st.get("guest_owner_chats") or [])
                                if str(c) != chat_key]
                    save_settings({"guest_owner_chats": cur_list})
                    st = settings()
                    note = f"➖ revoked <code>{_esc(chat_key)}</code>"
                    body = _gate_view(st)
                    view = "system"
                    await _log("🛡 Guest chat locked", f"Revoked owner unlock: {chat_key}")
            elif action == "upd":
                # Runs git + the test suite, so hand control back to the user
                # with a "working" toast before it blocks.
                if sub == "apply":
                    await q.answer("⬆️ updating… this takes a minute", show_alert=False)
                    report = await _run_selfupdate(apply=True, force=False)
                    note = _update_note(report)
                else:
                    await q.answer("🔍 checking…", show_alert=False)
                    report = await _run_selfupdate(apply=False)
                    note = _update_note(report)
                st = settings()
                view = "system"
                body = _system_view(st) + f"\n\n{_update_note(report)}"
            elif action == "logoff" and st.get("log_channel"):
                prev = st.get("log_channel")
                save_settings({"log_channel": None})
                st = settings()
                note = f"\U0001f4e1 log \u2192 <b>off</b> (was <code>{_esc(prev)}</code>)"
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
            res = await _panel_edit(q, body, view, st)
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
        logger.exception("[telegram-guest-mode] callback failed")
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
            logger.exception("[telegram-guest-mode] owner delegation failed")
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
                logger.warning("[telegram-guest-mode] stranger canned reply failed", exc_info=True)


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
        logger.exception("[telegram-guest-mode] session DB lookup failed")
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
        logger.debug("[telegram-guest-mode] current guest chat lookup failed", exc_info=True)
        return None
    return str(row[0]) if row and row[0] else None


def _guest_session_info(session_id: Any) -> Optional[Dict[str, Any]]:
    """Return ``{"guest_user_id": str, "is_owner": bool}`` for a guest session, else None."""
    row = _session_row(session_id)
    if not row:
        return None
    source, chat_id = row
    if source != "telegram" or not _is_guest_chat(chat_id):
        return None
    gid = ""
    try:
        db = _hermes_home() / "state.db"
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            oj = con.execute("SELECT origin_json FROM sessions WHERE id=?",
                             (str(session_id),)).fetchone()
        finally:
            con.close()
        if oj and oj[0]:
            origin = json.loads(oj[0]) or {}
            gid = str(origin.get("guest_sender_id") or origin.get("user_id") or "")
    except Exception:
        logger.debug("[telegram-guest-mode] origin_json read failed", exc_info=True)
    owner = str(_owner_id() or "")
    # The event now carries the real guest id, so this comparison is sound.
    # Sessions written before the fix still hold the forwarded message's id;
    # those simply do not match, which is the safe direction to fail.
    is_owner = bool(owner and gid and gid == owner)
    # Explicit unlock: you added this exact guest chat to guest_owner_chats.
    # Keyed on the guest's own chat id, not on an id Telegram fills with the
    # owner's, so this is the trustworthy path.
    chat_key = str(chat_id or "")
    unlocked = {str(c).strip() for c in (settings().get("guest_owner_chats") or []) if str(c).strip()}
    if chat_key and chat_key in unlocked:
        is_owner = True
    return {"guest_user_id": gid, "is_owner": is_owner, "guest_chat": chat_key}


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

def _on_pre_tool_call(tool_name: str = "", args: Any = None, session_id: Any = None, **_) -> Optional[Dict[str, str]]:
    """Guest safety gate: refuse destructive/leaking tools, let the rest through."""
    name = str(tool_name or "")
    if name in GUEST_SAFE_TOOLS:
        return None
    info = _guest_session_info(session_id)
    if info is None:
        return None
    st = settings()
    # The owner already has full access in their own DM, so gating them in
    # their guest chat protects nothing and only breaks the guest link for
    # the one person entitled to use it. This was the real bug: the owner was
    # blocked in their own guest chat, with no way to lift it.
    if info.get("is_owner") and st.get("guest_owner_full_access", True):
        return None
    denied = _guest_allowed(frozenset())
    danger = name in denied
    if not danger and args is not None:
        try:
            danger = bool(_GUEST_DANGER_ARG_RE.search(json.dumps(args, ensure_ascii=False, default=str)))
        except (TypeError, ValueError):
            danger = False
    if not danger:
        return None
    # A block is delivered to the model as a tool result, not as a turn
    # terminator, so without a per-turn counter the model re-probes blocked
    # tools for dozens of calls and the guest never gets an answer.
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
    logger.warning("[telegram-guest-mode] guest tool refused: %s (guest_is_owner=%s, block#%d)",
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
        logger.debug("[telegram-guest-mode] block-counter expiry failed", exc_info=True)
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
        home = Path(load_config_readonly().get("home") or "/host/home")
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

def _make_factory():
    def factory(native: Any, adapter: Any) -> None:
        _ADAPTER["adapter"] = adapter
        try:
            _install_wraps(adapter)
        except Exception:
            logger.exception("[telegram-guest-mode] outbound wrap install failed")
        if native is None:
            return
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
            logger.info("[telegram-guest-mode] v2 handlers registered (guest/private/callback)")
        except Exception:
            logger.exception("[telegram-guest-mode] registration failed")

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
        logger.info("[telegram-guest-mode] v2 active (hook + tool + PTB factory)")
    except Exception:
        logger.exception("[telegram-guest-mode] register failed")
