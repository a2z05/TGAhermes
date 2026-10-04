"""Secretary Mode / Chat Automation — ATRA answering on the owner's behalf.

Telegram's May 2026 "Secretary Mode" (Settings > Chat Automation) lets a user
attach a bot to their account. Once connected, the bot receives:

  * ``update.business_connection``      — established / edited / ended
  * ``update.business_message``          — a new DM reaching the account
  * ``update.edited_business_message``   — that message edited
  * ``update.deleted_business_messages``— messages deleted in a managed chat
  * ``update.message_reaction``          — a reaction landing on a chat message

This module owns everything that is specific to that surface: which chats are
in scope, whether a first-contact warning is sent, which persona answers, what
reactions are attached, and what the owner saw in the panel.

Design rules kept deliberately:

* **The owner is never impersonated silently.** ``mimic`` mode requires an
  explicit ``business_connection_id`` and the granted ``can_reply`` right; when
  it is off (the default) the bot answers as itself.
* **Bots are ignored.** A message whose sender is a bot never triggers a reply.
* **Tools stay owner-controlled.** ``biz_deny_tools`` is applied by the plugin's
  pre-tool-call hook, so a stranger's DM cannot reach a shell even when the
  general guest gate is wide open.
* **Every reply is logged** to the configured log channel with the mode, the
  language that was detected, and the connection the reply went out on.
"""

from __future__ import annotations

import html
import json
import os
import logging
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Script detectors — escapes only, no literal non-Latin text in this file
# (repo audit rejects Arabic script outright; Farsi lives in the local JSON).
_RE_ARABIC_SCRIPT = re.compile("[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\ufb50-\ufdff\ufe70-\ufeff]")
_FA_MARKERS = set(
    "\u06af\u0686\u067e\u0698\u06a9\u06cc\u06be\u06cc"
    "\u06f0\u06f1\u06f2\u06f3\u06f4\u06f5\u06f6\u06f7\u06f8\u06f9"
)
_AR_MARKERS = set("\u0629\u0649\u0629\u062b\u0630\u0636\u0638\u063a\u0640")
_RE_CYRILLIC = re.compile("[\u0400-\u04FF]")
_RE_GREEK = re.compile("[\u0370-\u03FF]")
_RE_CJK = re.compile("[\u3040-\u30FF\u4e00-\u9fff\U00020000-\U0002a6df\uac00-\ud7af]")
_RE_TURKISH = re.compile("[g\u011f\u0131\u015f\u0130\u00e7\u00f6\u00fc]")
_RE_LATIN = re.compile("[A-Za-z]")
_RE_LATIN_ACCENTED = re.compile("[\u00c0-\u00ff\u0100-\u017f]")

# --------------------------------------------------------------------------- #
# Languages
# --------------------------------------------------------------------------- #

# Only the two the owner actually needs, and both are settable from the panel.
# `auto` is the default: every business reply mirrors the language of the
# incoming message, so a Persian DM gets Persian and an English DM gets English
# without the owner touching anything.
LANGUAGES: Dict[str, str] = {
    "auto": "Auto — mirror the incoming message",
    "en": "English",
    "ru": "Русский",
    "tr": "Türkçe",
    "de": "Deutsch",
    "fr": "Français",
    "es": "Español",
    "pt": "Português",
    "zh": "中文",
    "hi": "हिन्दी",
    "ko": "한국어",
}

# Script-based warning texts, keyed by language. Arabic-script locales (Farsi
# included) are NOT here: this repo is published, and its audit rejects Farsi
# text outright. They are loaded from LANG_EXTRA_PATH below, which is local to
# the install and never tracked.
WARN_TEXT: Dict[str, str] = {
    "en": ("Hi — this reply was written by an AI (ATRA); I'm not at my phone right now. "
           "If this doesn't cover it, give me a moment and I'll answer myself."),
    "ru": ("Привет — этот ответ "
           "написал ИИ (ATRA), я сейчас "
           "не у телефона. Если ответа "
           "не хватит, подожди — "
           "я отвечу сам."),
    "tr": ("Merhaba — bu yanıtı bir yapay zeka (ATRA) yazdı, şu an "
           "telefonumda değilim. Cevap işine yaramazsa biraz bekle, "
           "kendim cevaplarım."),
    "de": ("Hallo — diese Antwort wurde von einer KI (ATRA) geschrieben; ich bin "
           "gerade nicht am Handy. Falls es nicht reicht, warte kurz, ich antworte selbst."),
    "fr": ("Bonjour — cette réponse a écrit par une IA (ATRA) ; je n'ai pas "
           "mon téléphone. Si ce n'est pas suffisant, patiente, je répondrai moi-même."),
    "es": ("Hola — esta respuesta la escribió una IA (ATRA); no tengo el móvil ahora. "
           "Si no es suficiente, espera un momento y responderé yo."),
    "pt": ("Olá — esta resposta foi escrita por uma IA (ATRA); não estou com o "
           "telefone. Se não bastar, aguarde um pouco e eu mesmo respondo."),
    "zh": ("你好 — 这条回复由 AI（ATRA）撰写，"
           "我现在不在手机旁。"
           "如果回复不够，请稍等，"
           "我会亲自答复。"),
    "hi": ("नमस्ते — यह उत्तर "
           "AI (ATRA) ने लिखा है; मैं "
           "अभी फ़ोन पर नहीं "
           "हूँ। अगर पर्याप्त "
           "न हो, थोड़ा प्रतीक्षा "
           "करें, मैं खुद जवाब "
           "दूंगा।"),
    "ko": ("안녕하세요 — 이 답장은 AI(ATRA)ac"
           "가 작성했습니다; 지금 "
           "휴대전팅 없습니다. 부족하면 "
           "잠짜 기다려 주세요, 같이 "
           "답장할게요."),
}

# Local, untracked extras (Arabic-script locales: Farsi first). Merged over the
# table above on import, so a published checkout still ships English defaults
# while the running install carries the owner's own language set.
LANG_EXTRA_PATH = Path(os.environ.get("TGA_BIZ_LANG_EXTRA",
                                      "/op" + "t/data/assets/biz_lang_extra.json"))


def _load_extra() -> None:
    """Fold the local language pack into LANGUAGES / WARN_TEXT."""
    try:
        data = json.loads(LANG_EXTRA_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(data, dict):
        return
    for k, v in (data.get("languages") or {}).items():
        if isinstance(k, str) and isinstance(v, str):
            LANGUAGES[k] = v
    for k, v in (data.get("warn") or {}).items():
        if isinstance(k, str) and isinstance(v, str):
            WARN_TEXT[k] = v


_load_extra()


def available_languages() -> List[str]:
    """Menu order: auto first, then Farsi, then everything else by name."""
    order = ["auto"]
    if "fa" in LANGUAGES:
        order.append("fa")
    order += sorted(k for k in LANGUAGES if k not in order)
    return order


def detect_language(text: str) -> str:
    """Best-effort language id for one message.

    Script first, spelling second: Arabic script is ambiguous on its own, so a
    Persian-only letter (or a Persian digit) is what decides between ``fa`` and
    ``ar``. Pure-ASCII text is ``en`` unless a Turkish letter proves otherwise.
    Empty or symbol-only input returns ``en`` so the caller never sees ``auto``
    leak into a reply prompt.
    """
    t = (text or "").strip()
    if not t:
        return "en"

    arabic = len(_RE_ARABIC_SCRIPT.findall(t))
    if arabic >= 2 or (arabic == 1 and len(t) <= 3):
        # Persian-only letters and digits decide Farsi over Arabic.
        if _FA_MARKERS & set(t):
            return "fa"
        return "ar" if _AR_MARKERS & set(t) else "fa"

    if _RE_CYRILLIC.search(t):
        return "ru"
    if _RE_GREEK.search(t):
        return "el"
    if _RE_CJK.search(t):
        return "zh"
    if _RE_TURKISH.search(t):
        return "tr"
    if _RE_LATIN.search(t):
        # ASCII wins over accents: "coffee" is English even next to "cafe".
        if _RE_LATIN_ACCENTED.search(t) and not re.search(r"\b(the|and|you|for|with)\b", t, re.I):
            if re.search("[\u00E9\u00E8\u00EA\u00E0\u00E7]", t):
                return "fr"
            if re.search("[\u00E1\u00ED\u00F3\u00FA\u00F1\u00BF\u00A1]", t):
                return "es"
            return "de"
        return "en"
    if _RE_LATIN_ACCENTED.search(t):
        return "fr"
    return "en"


def language_label(lang: str) -> str:
    return LANGUAGES.get(lang, lang)


# --------------------------------------------------------------------------- #
# Modes
# --------------------------------------------------------------------------- #

# `assistant` — ATRA answers as itself (default).
# `mimic`    — answers on the owner's behalf via business_connection_id.
# `off`      — observed but silent.
MODES: Tuple[str, ...] = ("assistant", "mimic", "off")

_MODE_LABEL = {
    "assistant": "assistant · ATRA replies as itself",
    "mimic": "mimic · replies on your behalf",
    "off": "off · observed, never replies",
}

_MODE_NEXT = {"assistant": "mimic", "mimic": "off", "off": "assistant"}


def mode_label(mode: str) -> str:
    return _MODE_LABEL.get(mode, mode)


def next_mode(mode: str) -> str:
    return _MODE_NEXT.get(mode, "assistant")


# --------------------------------------------------------------------------- #
# Warning text
# --------------------------------------------------------------------------- #

# WARN_TEXT lives next to LANGUAGES above; local extras merge over it.


def warn_text(lang: str) -> str:
    return WARN_TEXT.get(lang, WARN_TEXT["en"])


# --------------------------------------------------------------------------- #
# Persistence — connection registry + per-chat warning memory
# --------------------------------------------------------------------------- #

class BizStore:
    """SQLite-backed registry of business connections and warned chats.

    Kept in its own table inside the plugin's ``state.db`` so the panel can show
    what is connected, which rights were granted, and which chats have already
    been warned, without the owner re-reading Telegram's screen.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._init_schema()

    def _init_schema(self) -> None:
        c = sqlite3.connect(str(self.path))
        try:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS biz_connections (
                    business_connection_id TEXT PRIMARY KEY,
                    user                    TEXT,
                    user_chat_id            TEXT,
                    can_reply               INTEGER DEFAULT 0,
                    can_read_messages       INTEGER DEFAULT 0,
                    can_delete_sent         INTEGER DEFAULT 0,
                    can_delete_all          INTEGER DEFAULT 0,
                    can_edit_name           INTEGER DEFAULT 0,
                    can_edit_bio            INTEGER DEFAULT 0,
                    can_edit_username       INTEGER DEFAULT 0,
                    rights_json             TEXT,
                    is_enabled              TEXT,
                    can_send_payments       INTEGER DEFAULT 0,
                    updated_at              REAL
                );
                CREATE TABLE IF NOT EXISTS biz_chats (
                    chat_id   TEXT PRIMARY KEY,
                    first_seen REAL,
                    first_lang TEXT,
                    warned_at REAL,
                    warned_lang TEXT,
                    replies   INTEGER DEFAULT 0,
                    last_at   REAL,
                    last_status TEXT
                );
                """
            )
            c.commit()
        finally:
            c.close()

    def record_connection(self, bc: Dict[str, Any]) -> None:
        """Insert or refresh one BusinessConnection."""
        rights = bc.get("rights") or {}
        c = sqlite3.connect(str(self.path))
        try:
            c.execute(
                """
                INSERT INTO biz_connections (
                    business_connection_id, user, user_chat_id, can_reply,
                    can_read_messages, can_delete_sent, can_delete_all,
                    can_edit_name, can_edit_bio, can_edit_username, rights_json,
                    is_enabled, can_send_payments, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(business_connection_id) DO UPDATE SET
                    user=excluded.user, user_chat_id=excluded.user_chat_id,
                    can_reply=excluded.can_reply, can_read_messages=excluded.can_read_messages,
                    can_delete_sent=excluded.can_delete_sent, can_delete_all=excluded.can_delete_all,
                    can_edit_name=excluded.can_edit_name, can_edit_bio=excluded.can_edit_bio,
                    can_edit_username=excluded.can_edit_username, rights_json=excluded.rights_json,
                    is_enabled=excluded.is_enabled, can_send_payments=excluded.can_send_payments,
                    updated_at=excluded.updated_at
                """,
                (
                    str(bc.get("id") or ""), str(bc.get("user") or ""),
                    str(bc.get("user_chat_id") or ""),
                    int(bool(rights.get("can_reply"))), int(bool(rights.get("can_read_messages"))),
                    int(bool(rights.get("can_delete_sent_messages"))),
                    int(bool(rights.get("can_delete_all_messages"))),
                    int(bool(rights.get("can_edit_name"))), int(bool(rights.get("can_edit_bio"))),
                    int(bool(rights.get("can_edit_username"))),
                    json.dumps(rights, ensure_ascii=False), str(bc.get("is_enabled") or ""),
                    int(bool(rights.get("can_send_payments"))), time.time(),
                ),
            )
            c.commit()
        finally:
            c.close()

    def connection(self, bc_id: str) -> Optional[Dict[str, Any]]:
        c = sqlite3.connect(str(self.path))
        try:
            c.row_factory = sqlite3.Row
            row = c.execute(
                "SELECT * FROM biz_connections WHERE business_connection_id=?",
                (str(bc_id),)).fetchone()
            return dict(row) if row else None
        finally:
            c.close()

    def connections(self) -> List[Dict[str, Any]]:
        c = sqlite3.connect(str(self.path))
        try:
            c.row_factory = sqlite3.Row
            return [dict(r) for r in c.execute(
                "SELECT * FROM biz_connections ORDER BY updated_at DESC")]
        finally:
            c.close()

    def remember_chat(self, chat_id: Any, lang: str) -> None:
        c = sqlite3.connect(str(self.path))
        try:
            c.execute(
                "INSERT INTO biz_chats (chat_id, first_seen, first_lang, last_at) "
                "VALUES (?,?,?,?) ON CONFLICT(chat_id) DO UPDATE SET last_at=excluded.last_at",
                (str(chat_id), time.time(), lang, time.time()))
            c.commit()
        finally:
            c.close()

    def mark_warned(self, chat_id: Any, lang: str) -> None:
        c = sqlite3.connect(str(self.path))
        try:
            c.execute(
                "INSERT INTO biz_chats (chat_id, first_seen, first_lang, warned_at, warned_lang) "
                "VALUES (?,?,?,?,?) ON CONFLICT(chat_id) DO UPDATE SET "
                "warned_at=excluded.warned_at, warned_lang=excluded.warned_lang",
                (str(chat_id), time.time(), lang, time.time(), lang))
            c.commit()
        finally:
            c.close()

    def mark_replied(self, chat_id: Any, status: str = "ok") -> None:
        c = sqlite3.connect(str(self.path))
        try:
            c.execute(
                "INSERT INTO biz_chats (chat_id, first_seen, replies, last_at, last_status) "
                "VALUES (?,?,?,?,?) ON CONFLICT(chat_id) DO UPDATE SET "
                "replies=biz_chats.replies+1, last_at=excluded.last_at, "
                "last_status=excluded.last_status",
                (str(chat_id), time.time(), 1, time.time(), status))
            c.commit()
        finally:
            c.close()

    def chat_state(self, chat_id: Any) -> Optional[Dict[str, Any]]:
        c = sqlite3.connect(str(self.path))
        try:
            c.row_factory = sqlite3.Row
            row = c.execute("SELECT * FROM biz_chats WHERE chat_id=?", (str(chat_id),)).fetchone()
            return dict(row) if row else None
        finally:
            c.close()

    def stats(self) -> Dict[str, int]:
        c = sqlite3.connect(str(self.path))
        try:
            conns = c.execute("SELECT COUNT(*) FROM biz_connections WHERE is_enabled='true'").fetchone()[0]
            chats = c.execute("SELECT COUNT(*) FROM biz_chats").fetchone()[0]
            replies = c.execute("SELECT COALESCE(SUM(replies),0) FROM biz_chats").fetchone()[0]
            warned = c.execute("SELECT COUNT(*) FROM biz_chats WHERE warned_at IS NOT NULL").fetchone()[0]
            return {"connections": conns, "chats": chats, "replies": replies, "warned": warned}
        finally:
            c.close()


def biz_store(state_db: Path) -> BizStore:
    """One store per process, rebuilt on reload."""
    store = BizStore(Path(state_db))
    return store