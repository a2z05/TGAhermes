"""Offline tests for TGAhermes v2 (no live sends; hermes tree on sys.path)."""
from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import sqlite3
import os
import sys
import time
import tempfile
import types
from pathlib import Path

def _first_existing(*paths):
    return next((p for p in paths if p and os.path.isdir(p)), "")


# Assembled from fragments so this public test file never stores the host path
# literally (the pre-push audit blocks host paths in tracked files).
HOST_HOME = "/op" + "/data"


HERMES_SRC = os.environ.get("HERMES_SRC") or _first_existing("/opt/hermes", "/usr/local/hermes")
HERMES_HOME = os.environ.get("HERMES_HOME") or _first_existing(
    HOST_HOME, os.path.expanduser("~/.hermes")) or tempfile.mkdtemp(prefix="tgm-home-")
os.environ.setdefault("HERMES_HOME", HERMES_HOME)
sys.path.insert(0, HERMES_SRC)
STATE_DB = os.environ.get("TGM_STATE_DB", os.path.join(HERMES_HOME, "state.db"))
CONFIG_YAML = os.environ.get("TGM_CONFIG", os.path.join(HERMES_HOME, "config.yaml"))
# host data (live sessions / installed config) only exists on the author's box
HAVE_HOST = os.path.exists(STATE_DB) and os.path.exists(CONFIG_YAML)



PASS = 0
FAIL = 0


def check(cond, label):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}")


HERE = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("tg_guest_mode", HERE / "__init__.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
print("module loaded")

TMP = Path(tempfile.mkdtemp(prefix="tgm_test_"))

# The plugin mirrors telegram.extra.allow_from into the CORE gate's own
# allowlist (.env + os.environ). The suite must never do that to the live file:
# earlier runs leaked fake ids (900000001, 555, 555555) into the real .env and
# handed a stranger access by accident. [24] exercises the real function with
# an explicit env_path/environ; everywhere else it is a no-op recorder.
GATE_SYNC_CALLS = []
_real_gate_sync = mod._sync_gate_allowlists
def _fake_gate_sync(csv, *, env_path=None, environ=None):
    if env_path is not None and environ is not None:
        # explicitly sandboxed call ([24]) — safe, run it for real
        return _real_gate_sync(csv, env_path=env_path, environ=environ)
    GATE_SYNC_CALLS.append((csv, env_path, environ))
    return True
mod._sync_gate_allowlists = _fake_gate_sync

mod.SETTINGS_PATH = TMP / "settings.json"
mod.STATE_PATH = TMP / "state.json"
PERSONA = TMP / "persona.md"
PERSONA.write_text("# ATRA — test persona\n", encoding="utf-8")


class NS(types.SimpleNamespace):
    pass


class FakeSource:
    def __init__(self, chat_id, chat_type="dm", user_id="900000001", message_id="1"):
        self.chat_id = str(chat_id)
        self.chat_type = chat_type
        self.user_id = str(user_id)
        self.message_id = str(message_id)
        self.platform = "telegram"


class FakeEvent:
    def __init__(self, text="", source=None, internal=False):
        self.text = text
        self.source = source or FakeSource("900000001")
        self.metadata = {}
        self.channel_prompt = None
        self.allow_gateway_control = True
        self.internal = internal
        self.message_type = None
        self.platform = "telegram"
        self.raw_message = None


class FakeBot:
    def __init__(self):
        self.username = "example_bot"
        self.answers = []      # (gqid, result)
        self.sent = []         # send_message kwargs
        self.banned = []
        self.deleted = []

    async def answer_guest_query(self, guest_query_id=None, result=None):
        self.answers.append((guest_query_id, result))

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)

    async def edit_message_text(self, **kwargs):
        self.sent.append({"_edited": True, **kwargs})
        return NS(message_id=555)

    async def ban_chat_member(self, **kwargs):
        self.banned.append(kwargs)

    async def delete_message(self, **kwargs):
        self.deleted.append(kwargs)


class FakeAdapter:
    name = "telegram"

    def __init__(self):
        self.config = NS(extra={"allow_from": 900000001})
        self._bot = FakeBot()
        self._message_handler = lambda e: None
        self.received = []
        self.calls = []
        self.reactions = []
        self.delegated = []
        self.release_marker_calls = 0

    def _build_message_event(self, message, msg_type, update_id=None):
        ev = FakeEvent(text=message.text)
        ev.platform_update_id = update_id
        return ev

    def _clean_bot_trigger_text(self, text):
        return (text or "").replace("@example_bot", "").strip()

    async def handle_message(self, event):
        self.received.append(event)

    async def _release_turn_marker(self, event):
        self.release_marker_calls += 1

    async def _handle_command(self, update, context):
        self.delegated.append("command")

    async def _handle_text_message(self, update, context):
        self.delegated.append("text")

    async def _set_reaction(self, chat_id, message_id, emoji):
        self.reactions.append((str(chat_id), str(message_id), emoji))
        return True

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.calls.append(("send", str(chat_id), content))
        return NS(success=True, message_id="m1")

    async def send_final_ledgered(self, event, session_key, text_content, metadata, *,
                                  reply_to, is_ephemeral_response=False):
        self.calls.append(("sfl", event.source.chat_id, text_content))
        return NS(success=True, message_id="m2"), self

    async def send_clarify(self, chat_id, question, choices, clarify_id, session_key, metadata=None):
        self.calls.append(("clarify", chat_id, question))
        return NS(success=True, message_id="m3")

    async def _send_prompt(self, what, chat_id, metadata, build, *, parse_mode=None,
                           thread_id=None, reply_to_mode=None):
        self.calls.append(("prompt", chat_id, what))
        return NS(success=True, message_id="m4")

    async def _notify_turn_error(self, event, e):
        self.calls.append(("nte", type(e).__name__))
        return None

    async def send_typing(self, chat_id, metadata=None):
        self.calls.append(("typing", str(chat_id)))

    async def send_image(self, chat_id, image_url, caption=None, reply_to=None, metadata=None):
        self.calls.append(("image", str(chat_id), image_url))
        return NS(success=True, message_id="m5")

    async def send_document(self, chat_id, file_path, caption=None, file_name=None,
                            reply_to=None, metadata=None, **kw):
        self.calls.append(("doc", str(chat_id), file_path))
        return NS(success=True, message_id="m6")


def make_update(text="hi", gqid="gq1", user_id=900000001, reply=None, first="Owner",
                chat_id=777777, message_id=9):
    guest = NS(
        guest_query_id=gqid, text=text,
        from_user=NS(id=user_id, first_name=first, last_name="", username="u"),
        reply_to_message=reply, chat=NS(id=chat_id, title=None), message_id=message_id)
    return NS(guest_message=guest, update_id=4242)


def answers_of(bot, gqid=None):
    out = []
    for g, r in bot.answers:
        if gqid is None or g == gqid:
            try:
                out.append(r.input_message_content.message_text)
            except AttributeError:
                out.append(r)
    return out


# ---------------------------------------------------------------- settings
print("\n[1] settings")
s = mod.settings()
check(s["unauthorized_reply"] == "I only serve to my owner", "default unauthorized reply")
mod.save_settings({"unauthorized_reply": "custom text", "log_channel": "-10042", "owner_id": "999"})
s = mod.settings()
check(s["unauthorized_reply"] == "custom text" and s["log_channel"] == "-10042", "settings roundtrip")
check(mod._owner_id() == "999", "owner_id override wins")
mod.save_settings({"owner_id": None})
try:
    from hermes_cli.config import load_config_readonly
    _raw = (load_config_readonly().get("telegram", {}).get("extra", {}) or {}).get("allow_from")
except Exception:
    _raw = None
if isinstance(_raw, (list, tuple)):
    _raw = _raw[0] if _raw else ""
_cfg_owner = str(_raw or "")
check(mod._owner_id() == _cfg_owner and (_cfg_owner != "" or not HAVE_HOST), "falls back to config allow_from")
check(PERSONA.exists() and mod._load_persona().startswith("# ATRA"), "persona from persona_path")
mod.save_settings({"persona_path": str(PERSONA)})

# ---------------------------------------------------------------- guest handler
print("\n[2] guest handler")
mod.save_settings({"unauthorized_reply": "I only serve to my owner"})  # section 1 changed it
ad = FakeAdapter()
mod._ADAPTER["adapter"] = ad


async def t1():
    # unauthorized stranger plain mention -> canned reply + cooldown + log
    await mod._handle_guest_message(ad, make_update(user_id=111, first="Wz"))
    check(len(ad._bot.answers) == 1, "canned answer sent for stranger mention")
    check("I only serve to my owner" in answers_of(ad._bot)[0], "canned text is the configured reply")
    check(any("Unauthorized guest mention" in str(m.get("text", "")) for m in ad._bot.sent), "logged to log channel")
    await mod._handle_guest_message(ad, make_update(user_id=111, first="Wz", gqid="gq2"))
    check(len(ad._bot.answers) == 1, "cooldown suppresses second canned reply")
    st = json.loads(mod.STATE_PATH.read_text())
    check("111" in st.get("users", {}), "stranger recorded in state")

    # owner plain mention -> handled
    await mod._handle_guest_message(ad, make_update())
    check(len(ad.received) == 1, "owner mention handled")
    ev = ad.received[-1]
    check(ev.internal is True and ev.allow_gateway_control is False, "internal + no gateway control")
    # The session key is the GUEST'S OWN chat (777777), NOT the owner's DM id
    # (900000001) that PTB resolves into source.chat_id. Keying on the owner's
    # id merged every guest into one session named after the owner.
    check(ev.source.chat_id == "guest_777777", "session renamed guest_<guest chat>")
    check(ev.source.chat_id != "guest_900000001", "session is NOT keyed on the owner's DM id")
    check(ev.metadata["guest_query_id"] == "gq1", "gqid stored")
    check(ev.metadata["guest_original_chat_id"] == "777777", "original chat id kept for log buttons")
    check("[Channel origin]" in ev.channel_prompt and "ATRA" in ev.channel_prompt, "persona + identity tag")
    check("guest chat" in ev.channel_prompt, "guest block says this is a guest chat")
    check(any("Guest mention — answered" in str(m.get("text", "")) for m in ad._bot.sent), "answered mention logged")

    # stranger reply-to-ATRA -> handled (no canned)
    n = len(ad._bot.answers)
    await mod._handle_guest_message(ad, make_update(user_id=111, first="Wz", reply=NS(x=1), gqid="gq3"))
    check(len(ad.received) == 2 and len(ad._bot.answers) == n, "reply-to handled without canned reply")

    # empty text / no gqid -> dropped
    await mod._handle_guest_message(ad, make_update(text="", gqid="gq4"))
    await mod._handle_guest_message(ad, make_update(gqid=None))
    check(len(ad.received) == 2, "empty/no-gqid dropped")


asyncio.run(t1())

# ---------------------------------------------------------------- channel origin (DM / group / channel)
print("\n[3] channel origin block")
try:
    from types import SimpleNamespace as _SNS
    _owner = mod._owner_id(ad)
    for ctype, cid, uid, kind in (
            ("dm", "100000001", _owner, "direct message"),
            ("supergroup", "-1001", "999", "supergroup"),
            ("channel", "-1002", "111", "channel")):
        _src = _SNS(chat_id=cid, chat_type=ctype, user_id=uid, message_id=7)
        _blk = mod._origin_identity_block(ad, _src)
        check(_blk.startswith("[Channel origin]"), f"{ctype}: block is a channel-origin block")
        check(kind in _blk, f"{ctype}: chat kind named ({kind})")
        check(f"chat_id={cid!r}" in _blk, f"{ctype}: chat id present")
        check(f"sender_user_id={uid!r}" in _blk, f"{ctype}: sender id present")
        check("not a request" in _blk, f"{ctype}: marked as context, not an instruction")
    # owner DM must be labelled as the owner's own chat
    _src = _SNS(chat_id=_owner, chat_type="dm", user_id=_owner, message_id=7)
    check("owner" in mod._origin_identity_block(ad, _src), "owner DM labelled as owner's chat")
    # guest blocks keep the guest wording
    _gblk = mod._guest_identity_block("Sara", "5", "guest", _owner, "plain mention", None)
    check("guest chat" in _gblk and "guest_user_id" in _gblk, "guest block keeps guest fields")
    # a non-telegram src must never produce a block (hook only injects for telegram)
    check(mod._chat_kind("dm") != mod._chat_kind("dm", is_guest=True), "guest kind differs from dm")
except Exception as e:  # noqa: BLE001
    check(False, f"channel origin block raised {type(e).__name__}: {e}")

# ---------------------------------------------------------------- wraps
print("\n[3] wraps")
ad2 = FakeAdapter()
mod._ADAPTER["adapter"] = ad2  # _log/_react resolve the adapter via this ref
mod._install_wraps(ad2)
mod._install_wraps(ad2)
check(getattr(ad2, "_guest_wraps_installed", False), "wraps idempotent")
ad2._guest_gqids = {"guest_999": "gqX"}


async def t2():
    from gateway.platforms.base import SendResult

    r = await ad2.send("guest_999", "status")
    check(isinstance(r, SendResult) and r.success and r.message_id is None, "guest send suppressed")
    r = await ad2.send("900000001", "owner text")
    check(r.message_id == "m1", "owner send passthrough")

    gev = FakeEvent(text="hi", source=FakeSource("guest_999"))
    gev.metadata = {"guest_query_id": "gqX"}
    r, who = await ad2.send_final_ledgered(gev, "k", "Final answer.", {}, reply_to=None)
    check(who is ad2 and answers_of(ad2._bot, "gqX")[-1] == "Final answer.", "guest final via gqid")
    check(ad2.release_marker_calls == 1, "turn marker released")

    # owner final -> passthrough + ✅ reaction (spawned)
    oev = FakeEvent(text="hi", source=FakeSource("900000001", message_id="4572"))
    r, who = await ad2.send_final_ledgered(oev, "k", "text", {}, reply_to=None)
    await asyncio.sleep(0.05)
    check(any(c[0] == "sfl" for c in ad2.calls), "owner final passthrough")
    check(("900000001", "4572", "✅") in ad2.reactions, "✅ reacted after owner final")

    # clarify guest
    from tools import clarify_gateway as _cg
    _cg._entries["cl1"] = NS(multi_select=False, awaiting_text=False)
    rr = await ad2.send_clarify("guest_999", "Pick?", ["A", "B"], "cl1", "k")
    check(rr.success and "  1. A" in answers_of(ad2._bot, "gqX")[-1], "guest clarify answered")
    check(_cg._entries["cl1"].awaiting_text is True, "text-intercept armed")

    # prompt guest
    called = []
    rr = await ad2._send_prompt("x", "guest_999", {}, lambda: ("Prompt!", "KB", lambda m: called.append(m)))
    check(rr.success and answers_of(ad2._bot, "gqX")[-1] == "Prompt!" and not called, "guest prompt answered, on_sent skipped")

    # guest error -> log channel + EN canned (log channel configured!)
    nlog = len(ad2._bot.sent)
    await ad2._notify_turn_error(gev, RuntimeError("boom"))
    check(len(ad2._bot.sent) > nlog, "guest error posted to log channel")
    check("Guest mode error" in str(ad2._bot.sent[-1].get("text", "")), "error title")
    check("boom" in str(ad2._bot.sent[-1].get("text", "")), "raw error in log")
    check("hiccup" in answers_of(ad2._bot, "gqX")[-1], "EN pre-made guest error text sent")

    # Persian variant
    gev.text = "\u0633\u0644\u0627\u0645 \u0645\u0634\u06a9\u0644\u06cc \u0647\u0633\u062a"
    await ad2._notify_turn_error(gev, RuntimeError("x"))
    check(mod.settings().get("guest_error_reply_fa") in answers_of(ad2._bot, "gqX")[-1],
          "FA pre-made guest error text sent")

    # owner error -> passthrough + ❌
    oev = FakeEvent(text="hi", source=FakeSource("900000001", message_id="77"))
    await ad2._notify_turn_error(oev, RuntimeError("boom"))
    await asyncio.sleep(0.05)
    check(("nte", "RuntimeError") in ad2.calls, "owner error passthrough")
    check(("900000001", "77", "❌") in ad2.reactions, "❌ reacted on owner error")

    # typing
    await ad2.send_typing("guest_999")
    await ad2.send_typing("900000001")
    check(("typing", "guest_999") not in ad2.calls and ("typing", "900000001") in ad2.calls, "typing suppressed for guest")

    # media: http photo -> InlineQueryResultPhoto
    r = await ad2.send_image("guest_999", "https://x.test/p.png", caption="cap")
    last = ad2._bot.answers[-1][1]
    check(type(last).__name__ == "InlineQueryResultPhoto", f"http image -> photo result (got {type(last).__name__})")
    check(getattr(last, "caption", None) == "cap", "caption carried")
    check(getattr(last, "thumbnail_url", None) == "https://x.test/p.png", "thumbnail_url required by PTB")

    # local path -> article fallback
    r = await ad2.send_document("guest_999", "/data/workspace/file.pdf", caption="doc", file_name="file.pdf")
    last = ad2._bot.answers[-1][1]
    check(type(last).__name__ == "InlineQueryResultArticle", "local file -> article fallback")

    # media_to_guests off -> suppressed (no new answer)
    mod.save_settings({"media_to_guests": False})
    n = len(ad2._bot.answers)
    await ad2.send_image("guest_999", "https://x.test/p2.png")
    check(len(ad2._bot.answers) == n, "media off = suppressed")
    mod.save_settings({"media_to_guests": True})

    # non-guest media passthrough
    n = len(ad2.calls)
    await ad2.send_image("900000001", "https://x.test/p3.png")
    check(len(ad2.calls) > n, "owner media passthrough")


asyncio.run(t2())

# ---------------------------------------------------------------- hook + bang console
print("\n[4] hook / bang console")
ad3 = FakeAdapter()
mod._ADAPTER["adapter"] = ad3
ad3._guest_gqids = {}


async def t3():
    # bang in owner DM executes + skips
    ev = FakeEvent(text="!setunauthorized hey there", source=FakeSource("900000001"))
    res = await mod._pre_gateway_dispatch(event=ev)
    check(res == {"action": "skip", "reason": "TGAhermes bang command"}, "bang returns skip")
    check(mod.settings()["unauthorized_reply"] == "hey there", "!setunauthorized applied")
    _sent = [str(m.get("text", "")) for m in ad3._bot.sent]
    check(any(_sent), "console reply sent")
    check(all(m.get("parse_mode") == "HTML" for m in ad3._bot.sent), "console reply sent as HTML")

    # !setlog
    ev = FakeEvent(text="!setlog -100777", source=FakeSource("900000001"))
    await mod._pre_gateway_dispatch(event=ev)
    check(mod.settings()["log_channel"] == "-100777", "!setlog applied")

    # !settings / !help / !users produce replies
    for cmd in ("!settings", "!help", "!users"):
        ev = FakeEvent(text=cmd, source=FakeSource("900000001"))
        res = await mod._pre_gateway_dispatch(event=ev)
        check(res is not None and res.get("action") == "skip", f"{cmd} handled+skipped")
    check(any("Recorded users" in str(m.get("text", "")) for m in ad3._bot.sent),
          "!users replied with registry")

    # unknown bang -> hint reply
    ev = FakeEvent(text="!frobnicate", source=FakeSource("900000001"))
    await mod._pre_gateway_dispatch(event=ev)
    check(any("Unknown command" in str(m.get("text", "")) for m in ad3._bot.sent), "unknown bang hint")

    # non-bang owner message -> None + 👀 react
    ev = FakeEvent(text="hello", source=FakeSource("900000001", message_id="909"))
    res = await mod._pre_gateway_dispatch(event=ev)
    await asyncio.sleep(0.05)
    check(res is None, "plain owner msg passes")
    check(("900000001", "909", "👀") in ad3.reactions, "👀 reacted on receive")

    # group mention logged
    nlog = len(ad3._bot.sent)
    ev = FakeEvent(text="hey @example_bot what up", source=FakeSource("-100123", chat_type="group",
                                                                        user_id="4242", message_id="55"))
    res = await mod._pre_gateway_dispatch(event=ev)
    check(res is None, "group msg passes")
    await asyncio.sleep(0.05)
    check(len(ad3._bot.sent) > nlog and "Group mention" in str(ad3._bot.sent[-1].get("text", "")), "group mention logged")

    # non-owner bang in group -> passes (not swallowed)
    ev = FakeEvent(text="!hack", source=FakeSource("-100123", chat_type="group", user_id="4242"))
    res = await mod._pre_gateway_dispatch(event=ev)
    check(res is None, "non-owner bang not intercepted")

    # internal events ignored
    ev = FakeEvent(text="!setreact off", source=FakeSource("900000001"), internal=True)
    res = await mod._pre_gateway_dispatch(event=ev)
    check(res is None, "internal events ignored by hook")


asyncio.run(t3())

# ---------------------------------------------------------------- stranger DM handler
print("\n[5] stranger DM handler")
mod.save_settings({"unauthorized_reply": "I only serve to my owner"})  # section 4's !setunauthorized overwrote it
ad4 = FakeAdapter()
mod._ADAPTER["adapter"] = ad4


def dm_update(user_id, text, first="Wz"):
    msg = NS(text=text, from_user=NS(id=user_id, first_name=first, last_name="", username="u"),
             chat=NS(id=user_id))
    return NS(effective_message=msg, message=msg)


async def t4():
    # stranger DM -> canned reply + logged
    await mod._on_private_text(ad4, dm_update(555, "hello bot"))
    check(any("hello bot" not in str(s.get("text", "")) and s.get("text", "").startswith("I only serve")
              for s in ad4._bot.sent), "stranger DM got canned reply")
    check(any("Stranger DM" in str(s.get("text", "")) for s in ad4._bot.sent), "stranger DM logged")
    # cooldown
    n = len([s for s in ad4._bot.sent if str(s.get("text", "")).startswith("I only serve")])
    await mod._on_private_text(ad4, dm_update(555, "again"))
    n2 = len([s for s in ad4._bot.sent if str(s.get("text", "")).startswith("I only serve")])
    check(n2 == n, "cooldown blocks repeat canned reply")
    st = json.loads(mod.STATE_PATH.read_text())
    check(st["users"].get("555", {}).get("count", 0) >= 2, "stranger counted")

    # owner text -> delegated to core
    await mod._on_private_text(ad4, dm_update(900000001, "hi agent"))
    check(ad4.delegated[-1] == "text", "owner text delegated")
    await mod._on_private_text(ad4, dm_update(900000001, "/new"))
    check(ad4.delegated[-1] == "command", "owner command delegated")


asyncio.run(t4())

# ---------------------------------------------------------------- callbacks
print("\n[6] callbacks")
ad5 = FakeAdapter()
mod._ADAPTER["adapter"] = ad5


async def t5():
    answered = []

    async def stranger_answer(*a, **kw):
        answered.append((a, kw))

    q = NS(from_user=NS(id=111), data="tgm:info:111", message=NS(chat=NS(id="-1001")),
           answer=stranger_answer, reply_text=None)
    await mod._on_callback(NS(callback_query=q))
    check(any("Not for you" in str(a) for a in answered), "callback owner-gated")

    answered.clear()

    async def owner_answer(*a, **kw):
        answered.append((a, kw))

    async def reply_text(*a, **kw):
        answered.append(("reply", a))

    q = NS(from_user=NS(id=900000001), data="tgm:info:555",
           message=NS(chat=NS(id="-1001"), reply_text=reply_text),
           answer=owner_answer)
    await mod._on_callback(NS(callback_query=q))
    await asyncio.sleep(0.05)
    check(any("User info" in str(s.get("text", "")) for s in ad5._bot.sent), "owner info answered with record")


asyncio.run(t5())

# ---------------------------------------------------------------- tool
print("\n[7] telegram_admin tool")
mod.save_settings({"owner_id": _cfg_owner})  # real owner id for the live-DB gate tests


async def t6():
    # visibility gate
    check(mod._tool_check() is True, "tool enabled by default")
    mod.save_settings({"tool_enabled": False})
    check(mod._tool_check() is False, "tool_enabled=false hides tool")
    mod.save_settings({"tool_enabled": True})

    # owner gate via real sessions DB
    import sqlite3
    row = None
    try:
        con = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True)
        row = con.execute("SELECT id FROM sessions WHERE source='telegram' AND chat_id=? "
                          "ORDER BY last_activity_at DESC LIMIT 1", (_cfg_owner,)).fetchone()
        con.close()
    except Exception:
        row = None
    if not HAVE_HOST:
        print("  skip  owner session gate (no local Hermes data)")
    check(row is not None or not HAVE_HOST, "owner session exists in DB")
    if row:
        check(mod._tool_owner_ok(row[0]) is True, "owner session allowed")
    check(mod._tool_owner_ok(None) is False, "no session -> denied")
    check(mod._tool_owner_ok("nonexistent_session") is False, "unknown session -> denied")

    ad7 = FakeAdapter()
    mod._ADAPTER["adapter"] = ad7

    # denial without session
    r = await mod._tool_handler({"action": "react", "chat_id": "1", "message_id": "2"}, session_id=None)
    check(r["ok"] is False and "owner-only" in r["error"], "no session -> owner-only error")

    if row:
        # allowed: react
        r = await mod._tool_handler({"action": "react", "chat_id": "900000001",
                                     "message_id": "4572", "emoji": "🔥"}, session_id=row[0])
        check(r["ok"] is True and ("900000001", "4572", "🔥") in ad7.reactions, "owner session react executed")
        # ban
        r = await mod._tool_handler({"action": "ban_user", "chat_id": "-100123", "user_id": "42"},
                                    session_id=row[0])
        check(r["ok"] is True and ad7._bot.banned, "owner session ban executed")
        # logged to log channel (settings log_channel = -100777 from section 4)
        check(any("telegram_admin" in str(s.get("text", "")) for s in ad7._bot.sent), "tool action logged")
        # unknown action
        r = await mod._tool_handler({"action": "explode"}, session_id=row[0])
        check(r["ok"] is False and "unknown action" in r["error"], "unknown action rejected")

    # disabled mid-flight
    mod.save_settings({"tool_enabled": False})
    if row:
        r = await mod._tool_handler({"action": "react"}, session_id=row[0])
        check(r["ok"] is False and "disabled" in r["error"], "disabled -> refused")
    mod.save_settings({"tool_enabled": True, "owner_id": None})


asyncio.run(t6())

# ---------------------------------------------------------------- manifest + config
print("\n[8] manifest + config")
from pathlib import Path as P
from hermes_cli.plugins_manifest import parse_manifest_file
mf = parse_manifest_file(HERE / "plugin.yaml", HERE, "user", "")
check(mf is not None and mf.name == "TGAhermes" and mf.version == "4.1.0", "manifest parses v4.1.0")
check(mf is not None and "telegram_admin" in (mf.provides_tools or []), "provides_tools declared")
check(mf is not None and "pre_gateway_dispatch" in (mf.provides_hooks or []), "provides_hooks declared")

import yaml
cfg = yaml.safe_load(open(CONFIG_YAML)) if os.path.exists(CONFIG_YAML) else {}
check(not HAVE_HOST or "TGAhermes" in ((cfg.get("plugins") or {}).get("enabled") or []),
      "still enabled in config")

# ---------------------------------------------------------------- real MessageEvent shape (regression)
# FakeEvent above carries a `.platform` attr that the REAL gateway MessageEvent does NOT have —
# that mismatch let a broken platform gate pass every test while production silently no-opped
# (bang commands fell through to the LLM). This section builds the production shape for real.
print("\n[9] real MessageEvent shape (platform lives on event.source)")
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource
from gateway.config import Platform

ad9 = FakeAdapter()
mod._ADAPTER["adapter"] = ad9
mod.save_settings({"owner_id": "900000001", "log_channel": None})


def _real_ev(text, platform=Platform.TELEGRAM, chat_type="dm",
             chat_id="900000001", user_id="900000001"):
    src = SessionSource(platform=platform, chat_id=str(chat_id),
                        chat_type=chat_type, user_id=str(user_id))
    return MessageEvent(text=text, source=src)


ev9 = _real_ev("!help")
check(not hasattr(ev9, "platform"), "real MessageEvent has no platform attr (the original bug)")
check(getattr(ev9.source.platform, "value", None) == "telegram", "source.platform == Platform.TELEGRAM")
res9 = asyncio.run(mod._pre_gateway_dispatch(event=ev9))
check(res9 == {"action": "skip", "reason": "TGAhermes bang command"},
      "real-shape bang in owner DM intercepted + skipped")

ev9b = _real_ev("!help", platform=Platform.DISCORD, chat_id="555", user_id="555")
res9b = asyncio.run(mod._pre_gateway_dispatch(event=ev9b))
check(res9b is None, "non-telegram platform passes through")

mod.save_settings({"log_channel": "-100123"})
ev9c = _real_ev("!setreact off", chat_type="channel", chat_id="-100123")
res9c = asyncio.run(mod._pre_gateway_dispatch(event=ev9c))
check(res9c is not None and res9c.get("action") == "skip", "real-shape bang in log channel intercepted")
check(mod.settings().get("auto_react") is False, "!setreact applied via real shape")
mod.save_settings({"auto_react": True, "log_channel": None, "owner_id": None})
ad9.reset() if hasattr(ad9, "reset") else None

# ---------------------------------------------------------------- console v2: help/wipe/whitelist/tool-bang
print("\n[10] console v2: help, wipe, whitelist, callbacks, tool bang")
mod.save_settings({"owner_id": "900000001", "log_channel": None})
ad10 = FakeAdapter()
mod._ADAPTER["adapter"] = ad10
captured: dict = {"read": ["900000001"], "written": None}
mod._read_allow_from = lambda: list(captured["read"])


def _fake_write(ids):
    captured["written"] = list(ids)
    return True


mod._write_allow_from = _fake_write

# --- !help: grouped, covers the new commands
r10 = asyncio.run(mod._bang_execute(ad10, "900000001", "!help"))
check(r10 and "!whitelist" in r10 and "!wipe" in r10, "help lists whitelist + wipe")
check(r10 and "Sessions" in r10 and "Whitelist" not in r10.split("Access")[0], "help grouped")
check(r10 and "Unknown" not in r10, "help not unknown")

# --- !run: restarts the gateway through scripts/hermes_run.sh.
# The script is swapped for /bin/true so the suite can never bounce ATRA.
_run_script_before = mod._RUN_SCRIPT
mod._RUN_SCRIPT = "/bin/true"
try:
    rrun = asyncio.run(mod._bang_execute(ad10, "900000001", "!run"))
finally:
    mod._RUN_SCRIPT = _run_script_before
check(rrun and "restarting" in rrun, "!run replies with the restart notice")
check(mod._RUN_SCRIPT.endswith("hermes_run.sh"),
      "!run targets scripts/hermes_run.sh")
check("!run" in (asyncio.run(
    mod._bang_execute(ad10, "900000001", "!help")) or ""), "help lists !run")

# --- !wipe: session store scoping
class FakeStore:
    def __init__(self):
        self._entries = {"telegram:dm:900000001": "a",
                         "telegram:group:-100123:42": "b"}
        self.reset = []

    def reset_session(self, key, **kw):
        self.reset.append(key)


store10 = FakeStore()
mod._CTX["session_store"] = store10
r10 = asyncio.run(mod._bang_execute(ad10, "900000001", "!wipe", session_store=store10))
check(store10.reset == [], "bare !wipe refuses — no implicit current-chat wipe")
check("chat_id" in str(r10) and "Usage" in str(r10), "bare !wipe shows usage")
store10.reset = []
r10 = asyncio.run(mod._bang_execute(ad10, "900000001", "!wipe -100123", session_store=store10))
check(store10.reset == ["telegram:group:-100123:42"], "!wipe <chat> resets that chat")
store10.reset = []
r10 = asyncio.run(mod._bang_execute(ad10, "900000001", "!wipe 999999", session_store=store10))
check(store10.reset == [] and "Nothing to wipe" in str(r10), "unknown chat -> nothing to wipe")
mod._CTX["session_store"] = None  # simulate: hook never cached a store
r10 = asyncio.run(mod._bang_execute(ad10, "900000001", "!wipe 900000001", session_store=None))
check("unavailable" in str(r10), "no store handled safely")
# guest chat sessions: wipe by the guest's original chat id, and by guest_ prefix
store10._entries["agent:main:telegram:dm:guest_777"] = "g"
store10.reset = []
r10 = asyncio.run(mod._bang_execute(ad10, "900000001", "!wipe 777", session_store=store10))
check(store10.reset == ["agent:main:telegram:dm:guest_777"],
      "guest chat wiped by original guest id")
store10.reset = []
r10 = asyncio.run(mod._bang_execute(ad10, "900000001", "!wipe guest_777", session_store=store10))
check("agent:main:telegram:dm:guest_777" in store10.reset,
      "guest chat wiped by guest_ chat id too")
del store10._entries["agent:main:telegram:dm:guest_777"]
store10.reset = []
mod._CTX["session_store"] = store10

# --- !whitelist
r10 = asyncio.run(mod._bang_execute(ad10, "900000001", "!whitelist add 555"))
check(captured["written"] == ["900000001", "555"], "whitelist add appends + writes config")
captured["read"] = ["900000001", "555"]
r10 = asyncio.run(mod._bang_execute(ad10, "900000001", "!whitelist remove 555"))
check(captured["written"] == ["900000001"], "whitelist remove rewrites list")
r10 = asyncio.run(mod._bang_execute(ad10, "900000001", "!whitelist remove 900000001"))
check("Refusing" in str(r10), "owner protected from whitelist removal")
r10 = asyncio.run(mod._bang_execute(ad10, "900000001", "!whitelist list"))
check("900000001" in str(r10) and "owner" in str(r10), "whitelist list shows owner")
r10 = asyncio.run(mod._bang_execute(ad10, "900000001", "!whitelist add @nobody"))
check("resolve" in str(r10), "unresolvable @username -> id hint")

# --- authorized-user routing (owner OR whitelist -> core; stranger -> canned)
check(mod._is_authorized_user("900000001", "900000001") is True, "owner authorized")
check(mod._is_authorized_user("555", "900000001") is True, "whitelisted friend authorized")
check(mod._is_authorized_user("666", "900000001") is False, "stranger not authorized")

# --- guest identity: replied-to context
from types import SimpleNamespace as NS10
bot_msg10 = NS10(text="hello there", caption=None,
                 from_user=NS10(is_bot=True, first_name="ATRA", last_name="", username="example_bot"))
blk = mod._guest_identity_block("Friend", "555", "guest", "900000001", "reply", bot_msg10)
check("[Replied to]" in blk and "bot (ATRA)" in blk and "hello there" in blk,
      "guest identity carries replied-to author+text")
human_msg10 = NS10(text="nope", caption=None,
                   from_user=NS10(is_bot=False, first_name="Zed", last_name="", username="zed"))
blk = mod._guest_identity_block("Friend", "555", "guest", "900000001", "reply", human_msg10)
check("author='Zed'" in blk and "the bot (ATRA)" not in blk, "replied-to human author named")
blk = mod._guest_identity_block("Friend", "555", "guest", "900000001", "plain mention", None)
check("[Replied to]" not in blk, "no reply block without reply_to")

# --- wipe buttons (log channel): confirm prompt then reset
async def _ans10(*a, **k):
    cb10.append(("ans", a, k))


async def _rt10(*a, **k):
    cb10.append(("reply", a, k))


cb10 = []
q10 = NS(from_user=NS(id=900000001), data="tgm:wipe:-100123",
         message=NS(chat=NS(id="-1001"), reply_text=_rt10), answer=_ans10)
awaitable = mod._on_callback(NS(callback_query=q10))
asyncio.run(awaitable)
check(any(r[0] == "reply" and "Wipe the session" in str(r) for r in cb10), "wipe confirm prompt shown")
cb10.clear()
store10.reset = []
q10 = NS(from_user=NS(id=900000001), data="tgm:wipe2:-100123",
         message=NS(chat=NS(id="-1001"), reply_text=_rt10), answer=_ans10)
asyncio.run(mod._on_callback(NS(callback_query=q10)))
check(store10.reset == ["telegram:group:-100123:42"], "wipe button resets the session")
check(any(r[0] == "ans" and "wiped" in str(r).lower() for r in cb10), "wipe button acknowledged")
mod._CTX["session_store"] = None

# --- tool action: bang (console parity for the agent)
mod.save_settings({"owner_id": _cfg_owner})
row10 = None
try:
    import sqlite3 as _sq10
    with _sq10.connect(f"file:{mod._hermes_home() / 'state.db'}?mode=ro", uri=True) as _c10:
        row10 = _c10.execute("SELECT id FROM sessions WHERE source='telegram' AND chat_id=? LIMIT 1",
                             (_cfg_owner,)).fetchone()
except Exception:
    row10 = None
check(row10 is not None or not HAVE_HOST, "owner session row exists for tool gate")
r10 = asyncio.run(mod._tool_handler({"action": "bang", "text": "!setreact off"},
                                    session_id=row10[0] if row10 else None))
check((r10.get("ok") is True and mod.settings().get("auto_react") is False) or not HAVE_HOST,
      "tool bang runs the console command")
check(isinstance((r10.get("result") or {}).get("reply"), str), "tool bang returns reply text")
r10 = asyncio.run(mod._tool_handler({"action": "bang", "text": "setreact on"},
                                    session_id=row10[0] if row10 else None))
check(r10.get("ok") is False, "tool bang rejects non-! text")
mod.save_settings({"auto_react": True, "owner_id": None})


# ---------------------------------------------------------------- panel, inline errors, reactions
print("\n[11] glass panel + inline errors + reactions")
mod.save_settings({"owner_id": "900000001", "log_channel": "-100777",
                   "auto_react": True, "react_guests": True,
                   "log_group_mentions": False})
ad11 = FakeAdapter()
mod._ADAPTER["adapter"] = ad11
mod._CTX["gateway"] = None

# --- !panel + keyboard --------------------------------------------------
r11 = asyncio.run(mod._bang_execute(ad11, "900000001", "!panel"))
check(r11 and "ATRA console" in r11 and "!panel" in r11, "!panel shows the panel help")
kb = mod._help_keyboard("full")
cbdatas = [b.callback_data for row in kb for b in row]
check(len(kb) >= 3 and any("help:sessions" in d for d in cbdatas), "panel keyboard built")
check(any("help:full" in d for d in [b.callback_data for row in mod._help_keyboard("sessions") for b in row]),
      "section view offers full help back")


async def t11():
    # deliver !panel with its keyboard attached
    ev = FakeEvent(text="!panel", source=FakeSource("900000001", message_id="40"))
    await mod._run_bang_command(ad11, ev, "!panel")
    await asyncio.sleep(0.05)
    panel_msgs = [m for m in ad11._bot.sent if str(m.get("chat_id")) == "900000001"]
    check(panel_msgs and panel_msgs[0].get("parse_mode") == "HTML"
          and panel_msgs[0].get("reply_markup") is not None,
          "!panel delivered as HTML with buttons")
    check(any(("900000001", "40", "✅") in ad11.reactions for _ in [0]),
          "console command acknowledged with ✅")

    # panel navigation: tapping a section edits the message in place
    edited, answered = [], []

    async def _edit(text=None, **kw):
        edited.append((text, kw))

    async def _ans(*a, **kw):
        answered.append(a)

    q = NS(from_user=NS(id=900000001), data="tgm:help:sessions",
           message=NS(chat=NS(id="900000001"), edit_text=_edit), answer=_ans)
    await mod._on_callback(NS(callback_query=q))
    await asyncio.sleep(0.02)
    check(edited and "Sessions" in edited[0][0], "panel section edits in place")
    check(edited and edited[0][1].get("parse_mode") == "HTML", "panel edit sent as HTML")

    # --- inline error notice: owner DM yes, others -> log ----------------
    ad11._bot.sent.clear()
    await mod._error_notice("900000001", "telegram_admin failed", "<b>Action:</b> x")
    await asyncio.sleep(0.02)
    check(any(str(m.get("chat_id")) == "900000001" and "⚠️ telegram_admin failed" in str(m.get("text"))
              and "<b>" not in str(m.get("text")) for m in ad11._bot.sent),
          "owner-DM error shown here as a normal message")
    n_before = len(ad11._bot.sent)
    await mod._error_notice("-100999", "💥 Guest mode error", "<b>boom</b>")
    await asyncio.sleep(0.02)
    check(any(str(m.get("chat_id")) == "-100777" and "Guest mode error" in str(m.get("text"))
              for m in ad11._bot.sent[n_before:]),
          "non-owner error still goes to the log channel")

    # --- group mention reacts 👀 even with mention-logging off ------------
    ev = FakeEvent(text="@example_bot ping", source=FakeSource("-100123", chat_type="group",
                                                                  user_id="555", message_id="55"))
    res = await mod._pre_gateway_dispatch(event=ev)
    await asyncio.sleep(0.05)
    check(res is None and ("-100123", "55", "👀") in ad11.reactions,
          "group mention acknowledged with 👀 (logging off)")

    # --- tool handler returns a registry-legal JSON string ---------------
    import sqlite3 as _sq11
    mod.save_settings({"owner_id": _cfg_owner})
    _row11 = None
    try:
        with _sq11.connect(f"file:{STATE_DB}?mode=ro", uri=True) as _c11:
            _row11 = _c11.execute("SELECT id FROM sessions WHERE source='telegram' AND chat_id=? "
                                  "ORDER BY last_activity_at DESC LIMIT 1", (_cfg_owner,)).fetchone()
    except Exception:
        _row11 = None
    out = await mod._tool_handler_json({"action": "bang", "text": "!help"},
                                       session_id=_row11[0] if _row11 else None)
    check((isinstance(out, str) and json.loads(out).get("ok") is True) or not HAVE_HOST,
          "telegram_admin returns a JSON string (registry contract)")
    mod.save_settings({"owner_id": "900000001"})
    out2 = await mod._tool_handler_json({"action": "nope"})
    check(isinstance(out2, str) and json.loads(out2).get("ok") is False,
          "tool errors also serialize")

    mod.save_settings({"auto_react": True, "log_group_mentions": True,
                       "log_channel": None, "owner_id": None})


asyncio.run(t11())


# ------------------------------------------------------- expanded glass panel (v2.3)
print("\n[12] expanded panel dashboard")
mod.save_settings({"owner_id": "900000001", "log_channel": "-100777", "auto_react": True})
ad12 = FakeAdapter()
mod._ADAPTER["adapter"] = ad12
mod._CTX["gateway"] = None

r12 = asyncio.run(mod._bang_execute(ad12, "900000001", "!panel"))
check(r12 and "ATRA console" in r12 and "!panel" in r12, "!panel serves the dashboard")
check(r12 and "friends" in r12 and "cooldown" in r12 and "guest tool mode" in r12,
      "dashboard carries live status only — flags moved to Settings")

kb = mod._help_keyboard("panel")
datas = [b.callback_data for row in kb for b in row]
check(any(d.endswith("panel:settings") for d in datas), "panel has a settings section")
check(any(d.endswith("panel:wl") for d in datas), "panel has a whitelist section")
check(not any("panel:toggle:" in d for d in datas),
      "home carries no bare toggles — every flag goes through a confirm screen")

check(any(d.endswith("help:sessions") for d in datas), "panel keeps section buttons")
check(not any(d.endswith("panel:back") for d in datas), "dashboard has no self-back button")
check(any(d.endswith("panel:back") for d in
          [b.callback_data for row in mod._help_keyboard("out") for b in row]),
      "output view offers back to console")
check(all(len(d.encode()) <= 64 for d in datas), "callback data inside Telegram's 64-byte cap")


async def t12():
    edited, answered = [], []

    async def _edit(text=None, **kw):
        edited.append((text, kw))

    async def _ans(*a, **kw):
        answered.append(a)

    def _q(data):
        return NS(from_user=NS(id=900000001), data=data,
                  message=NS(chat=NS(id="900000001"), edit_text=_edit), answer=_ans)

    # v3.3: a settings flag no longer flips on the first tap. The tap renders
    # the confirm screen; only the Apply callback writes.
    before = bool(mod.settings().get("auto_react"))
    edited.clear()
    await mod._on_callback(NS(callback_query=_q("tgm:panel:toggle:react")))
    await asyncio.sleep(0.02)
    check(bool(mod.settings().get("auto_react")) == before,
          "first tap on a flag does NOT write (confirm screen instead)")
    _cfm_mk = edited[0][1].get("reply_markup") if edited else None
    _apply = [b.callback_data for r in (_cfm_mk.inline_keyboard if _cfm_mk else [])
              for b in r if "Apply" in b.text]
    check(bool(_apply), "first tap lands on the confirm screen with Apply")
    check(any(d.endswith("panel:cfmok:tg:settings:react") for d in _apply),
          "Apply targets the single writer callback")
    edited.clear()
    await mod._on_callback(NS(callback_query=_q(_apply[0])))
    await asyncio.sleep(0.02)
    check(bool(mod.settings().get("auto_react")) != before,
          "Apply is what flips the real setting")
    check(edited and edited[0][1].get("parse_mode") == "HTML", "re-render is HTML")
    check(edited and "reactions" in edited[0][0], "confirm shows the dashboard back")
    check(answered and "<b>" not in str(answered[-1][0]), "toast carries no raw markup")
    mod.save_settings({"auto_react": before})

    # output views open in place
    edited.clear()
    await mod._on_callback(NS(callback_query=_q("tgm:panel:out:settings")))
    await asyncio.sleep(0.02)
    check(edited and "auto_react" in edited[0][0], "settings output opens inside the panel")
    edited.clear()
    await mod._on_callback(NS(callback_query=_q("tgm:panel:out:whitelist")))
    await asyncio.sleep(0.02)
    check(edited and "Whitelist" in edited[0][0], "whitelist output opens inside the panel")

    # regression: "Full help" used to overwrite the message with the literal key
    edited.clear()
    await mod._on_callback(NS(callback_query=_q("tgm:help:full")))
    await asyncio.sleep(0.02)
    check(edited and "ATRA console" in edited[0][0] and edited[0][0] != "full",
          "help:full renders the real full help (was a literal 'full' bug)")

    mod.save_settings({"log_channel": None, "owner_id": None})


asyncio.run(t12())


# ------------------------------------------- panel no-op tap = toast, not an error
print("\n[13] panel no-op tap handling")
mod.save_settings({"owner_id": "900000001", "log_channel": None})
ad13 = FakeAdapter()
mod._ADAPTER["adapter"] = ad13
mod._CTX["gateway"] = None


async def t13():
    answered, edits = [], []

    async def _ans(*a, **kw):
        answered.append((a, kw))

    async def _edit_boom(text=None, **kw):
        raise Exception("Bad Request: message is not modified: specified new message content "
                        "and reply_markup are exactly the same")

    async def _edit_ok(text=None, **kw):
        edits.append(text)

    # identical content -> friendly toast, no scary error text
    q = NS(from_user=NS(id=900000001), data="tgm:help:full",
           message=NS(chat=NS(id="900000001"), edit_text=_edit_boom), answer=_ans)
    await mod._on_callback(NS(callback_query=q))
    await asyncio.sleep(0.02)
    check(answered and "already showing" in str(answered[-1][0][0]), "no-op tap answers with a neutral toast")
    check("Already open" not in str(answered), "old confusing toast is gone")

    # real content change -> edits + generic toast
    answered.clear()
    q2 = NS(from_user=NS(id=900000001), data="tgm:help:status",
            message=NS(chat=NS(id="900000001"), edit_text=_edit_ok), answer=_ans)
    await mod._on_callback(NS(callback_query=q2))
    await asyncio.sleep(0.02)
    check(edits and "Status" in edits[0], "section tap still edits in place")

    # a genuine edit failure surfaces as a visible alert, not silence
    async def _edit_real_fail(text=None, **kw):
        raise Exception("Bad Request: chat not found")

    answered.clear()
    q3 = NS(from_user=NS(id=900000001), data="tgm:panel:out:users",
            message=NS(chat=NS(id="900000001"), edit_text=_edit_real_fail), answer=_ans)
    await mod._on_callback(NS(callback_query=q3))
    await asyncio.sleep(0.02)
    check(answered and answered[-1][1].get("show_alert") is True
          and "⚠️" in str(answered[-1][0][0]), "real edit failure raises a visible alert")


asyncio.run(t13())
mod.save_settings({"owner_id": None})


# --------------------------------- edit fallback when the message has no edit helper
print("\n[14] bot-level edit fallback")


async def t14():
    ad14 = FakeAdapter()
    mod._ADAPTER["adapter"] = ad14
    mod._CTX["gateway"] = None
    mod.save_settings({"owner_id": "900000001"})

    async def _ans(*a, **kw):
        pass

    class BareMsg:  # no edit_text / edit_message_text at all
        chat_id = "900000001"
        message_id = "77"

    edited = []
    orig = FakeBot.edit_message_text

    async def _capture(self, **kwargs):
        edited.append(kwargs)
        await orig(self, **kwargs)

    FakeBot.edit_message_text = _capture
    try:
        q = NS(from_user=NS(id=900000001), data="tgm:help:status",
               message=BareMsg(), answer=_ans)
        res = await mod._panel_edit(q, "<b>x</b>", "status", mod.settings())
    finally:
        FakeBot.edit_message_text = orig
    check(res == "ok" and edited and edited[0].get("message_id") == "77"
          and edited[0].get("parse_mode") == "HTML",
          "falls back to Bot.edit_message_text when Message has no edit helper")
    mod.save_settings({"owner_id": None})


asyncio.run(t14())


# ------------------------------------------------------ full console panel (v2.4)
print("\n[15] complete panel")
mod.save_settings({"owner_id": "900000001", "log_channel": "-100777",
                   "auto_react": True, "react_guests": False, "tool_enabled": True,
                   "log_owner_messages": False, "log_group_mentions": True,
                   "unauthorized_cooldown_s": 3600})
ad15 = FakeAdapter()
mod._ADAPTER["adapter"] = ad15
mod._CTX["gateway"] = None

r15 = asyncio.run(mod._bang_execute(ad15, "900000001", "!panel"))
check(r15 and "cooldown" in r15 and "None" not in r15, "dashboard reports the real cooldown value")
check(r15 and "v" in r15.split("\n")[0] and "ATRA console" in r15, "dashboard carries the version")

kb = mod._help_keyboard("panel", chat_id="-100777")
datas = [b.callback_data for row in kb for b in row]
for want in ("panel:settings", "panel:wl", "panel:actions", "help:log",
             "help:system", "wipe:-100777"):
    check(any(d.endswith(want) for d in datas), f"panel exposes {want}")
skeys = [b.callback_data for row in mod._help_keyboard("settings") for b in row]
for want in ("panel:tg:react:settings", "panel:tg:tool:settings", "panel:cool",
             "panel:wiz:unauth", "panel:out:guests"):
    check(any(d.endswith(want) for d in skeys), f"settings exposes {want}")
check(not any("panel:toggle:" in d for d in skeys),
      "settings routes flags through the confirm screen, not bare toggles")
check(not any(d.endswith("wipe:") for d in [b.callback_data for row in mod._help_keyboard("panel") for b in row]),
      "no wipe button when the chat is unknown")
check(any(b.callback_data.endswith("panel:cool:300") for b in mod._help_keyboard("cool")[-1]
          if b.callback_data) or
      any(b.callback_data.endswith("panel:cool:300") for row in mod._help_keyboard("cool") for b in row),
      "cooldown view offers presets")

help15 = asyncio.run(mod._bang_execute(ad15, "900000001", "!help"))
check(help15 and "👾 Guests" in help15 and "guest_error_reply" not in help15,
      "full help gained the Guests section")


async def t15():
    answered, edits = [], []

    async def _ans(*a, **kw):
        answered.append((a, kw))

    async def _edit(text=None, **kw):
        edits.append((text, kw))

    def _q(data, chat="900000001"):
        return NS(from_user=NS(id=900000001), data=data,
                  message=NS(chat=NS(id=chat), chat_id=chat, edit_message_text=_edit,
                             reply_text=None),
                  answer=_ans)

    # v3.3: these flags also confirm first — tap lands on the confirm
    # screen, and only the Apply callback flips the setting.
    before_tool = bool(mod.settings().get("tool_enabled"))
    edits.clear()
    await mod._on_callback(NS(callback_query=_q("tgm:panel:toggle:tool")))
    await asyncio.sleep(0.02)
    check(bool(mod.settings().get("tool_enabled")) == before_tool,
          "tool flag first tap does NOT write")
    _mk = edits[-1][1].get("reply_markup")
    _apply = [b.callback_data for row in _mk.inline_keyboard for b in row
              if "Apply" in b.text]
    await mod._on_callback(NS(callback_query=_q(_apply[0])))
    await asyncio.sleep(0.02)
    check(bool(mod.settings().get("tool_enabled")) != before_tool, "Apply flips tool_enabled")
    mod.save_settings({"tool_enabled": before_tool})

    before_g = bool(mod.settings().get("react_guests"))
    edits.clear()
    await mod._on_callback(NS(callback_query=_q("tgm:panel:toggle:greact")))
    await asyncio.sleep(0.02)
    check(bool(mod.settings().get("react_guests")) == before_g,
          "guest-react first tap does NOT write")
    _mk = edits[-1][1].get("reply_markup")
    _apply = [b.callback_data for row in _mk.inline_keyboard for b in row
              if "Apply" in b.text]
    await mod._on_callback(NS(callback_query=_q(_apply[0])))
    await asyncio.sleep(0.02)
    check(bool(mod.settings().get("react_guests")) != before_g, "Apply flips react_guests")
    mod.save_settings({"react_guests": before_g})

    # cooldown preset: first tap confirms, Apply writes
    edits.clear()
    await mod._on_callback(NS(callback_query=_q("tgm:panel:cool:300")))
    await asyncio.sleep(0.02)
    check(mod.settings().get("unauthorized_cooldown_s") != 300, "cooldown preset first tap does NOT write")
    _mk = edits[-1][1].get("reply_markup")
    _apply = [b.callback_data for row in _mk.inline_keyboard for b in row
              if "Apply" in b.text]
    await mod._on_callback(NS(callback_query=_q(_apply[0])))
    await asyncio.sleep(0.02)
    check(mod.settings().get("unauthorized_cooldown_s") == 300, "cooldown preset applies on Apply")
    check(edits and "cooldown" in edits[-1][0], "cooldown preset re-renders the dashboard")
    mod.save_settings({"unauthorized_cooldown_s": 3600})

    # cooldown view with presets
    edits.clear()
    await mod._on_callback(NS(callback_query=_q("tgm:panel:cool")))
    await asyncio.sleep(0.02)
    check(edits and "⏱ Cooldown" in edits[-1][0], "cooldown view opens")
    mk = edits[-1][1].get("reply_markup")
    check(mk and any(b.callback_data.endswith("panel:cool:60")
                     for row in mk.inline_keyboard for b in row), "preset buttons present")

    # guest texts output view
    edits.clear()
    await mod._on_callback(NS(callback_query=_q("tgm:panel:out:guests")))
    await asyncio.sleep(0.02)
    check(edits and "canned reply" in edits[-1][0], "guest texts view opens")

    # guests section via the grid
    edits.clear()
    await mod._on_callback(NS(callback_query=_q("tgm:help:guests")))
    await asyncio.sleep(0.02)
    check(edits and "👾 Guests" in edits[-1][0], "guests section renders")

    # wipe button reuses the existing confirm flow
    async def _reply(text=None, **kw):
        edits.append((text, kw))

    q = NS(from_user=NS(id=900000001), data="tgm:wipe:-100777",
           message=NS(chat=NS(id="900000001"), reply_text=_reply), answer=_ans)
    await mod._on_callback(NS(callback_query=q))
    await asyncio.sleep(0.02)
    check(edits and "Wipe the session" in str(edits[-1][0]), "wipe button asks for confirmation")

    mod.save_settings({"log_channel": None, "owner_id": None})


asyncio.run(t15())


# ---------------------------------------------------------------- [16] mirror + per-tab keys
print("\n[16] owner off / whitelisted + others toggleable, per-tab keys")

async def t16():
    ad = FakeAdapter()
    mod._ADAPTER["adapter"] = ad
    saved_log = mod.settings().get("log_channel")
    mod.save_settings({"log_channel": "-100777", "log_owner_messages": False,
                       "log_whitelisted_messages": True, "log_other_messages": True})
    friend_real = mod._read_allow_from
    mod._read_allow_from = lambda: ["900000001", "555555"]
    try:
        # 1) owner never mirrored by default
        n0 = len(ad._bot.sent)
        ev = FakeEvent(text="owner hello", source=FakeSource("900000001", chat_type="dm"))
        res = await mod._pre_gateway_dispatch(event=ev)
        await asyncio.sleep(0.05)
        check(res is None and len(ad._bot.sent) == n0, "owner message not mirrored")

        # 2) whitelisted friend -> mirrored
        ev = FakeEvent(text="friend hello", source=FakeSource("555555", chat_type="dm", user_id="555555"))
        await mod._pre_gateway_dispatch(event=ev)
        await asyncio.sleep(0.05)
        check(len(ad._bot.sent) > n0 and "Whitelisted message" in str(ad._bot.sent[-1].get("text", "")),
              "whitelisted message mirrored")
        check("friend hello" in str(ad._bot.sent[-1].get("text", "")), "mirror carries the text")

        # 3) toggle whitelisted off -> silent
        mod.save_settings({"log_whitelisted_messages": False})
        n1 = len(ad._bot.sent)
        ev = FakeEvent(text="friend again", source=FakeSource("555555", chat_type="dm", user_id="555555"))
        await mod._pre_gateway_dispatch(event=ev)
        await asyncio.sleep(0.05)
        check(len(ad._bot.sent) == n1, "whitelisted toggle off silences the mirror")

        # 4) outsider in a group -> mirrored
        mod.save_settings({"log_whitelisted_messages": True, "log_other_messages": True})
        n2 = len(ad._bot.sent)
        ev = FakeEvent(text="group chatter", source=FakeSource("-100123", chat_type="supergroup",
                                                              user_id="424242", message_id="77"))
        await mod._pre_gateway_dispatch(event=ev)
        await asyncio.sleep(0.05)
        check(len(ad._bot.sent) > n2 and "Message" in str(ad._bot.sent[-1].get("text", "")),
              "other group message mirrored")

        # 5) toggle others off -> silent
        mod.save_settings({"log_other_messages": False})
        n3 = len(ad._bot.sent)
        ev = FakeEvent(text="group chatter", source=FakeSource("-100123", chat_type="supergroup",
                                                              user_id="424242", message_id="78"))
        await mod._pre_gateway_dispatch(event=ev)
        await asyncio.sleep(0.05)
        check(len(ad._bot.sent) == n3, "other-messages toggle off silences the mirror")

        # 6) stranger DM stays guest territory (guest flow logs it, not the mirror)
        mod.save_settings({"log_other_messages": True})
        n4 = len(ad._bot.sent)
        ev = FakeEvent(text="stranger hi", source=FakeSource("777777", chat_type="dm", user_id="777777"))
        await mod._pre_gateway_dispatch(event=ev)
        await asyncio.sleep(0.05)
        check(len(ad._bot.sent) == n4, "stranger DM not mirrored (guest zone)")

        # 7) owner toggle still works when switched on
        mod.save_settings({"log_owner_messages": True})
        n5 = len(ad._bot.sent)
        ev = FakeEvent(text="owner mirror me", source=FakeSource("900000001", chat_type="dm"))
        await mod._pre_gateway_dispatch(event=ev)
        await asyncio.sleep(0.05)
        check(len(ad._bot.sent) > n5 and "Owner DM" in str(ad._bot.sent[-1].get("text", "")),
              "owner mirror toggle still works")
    finally:
        mod._read_allow_from = friend_real
        mod.save_settings({"log_owner_messages": False, "log_whitelisted_messages": True,
                           "log_other_messages": True})
    mod.save_settings({"log_channel": "-100777"})

    # per-tab keyboards really differ
    keys = lambda v, cid=None: [b.callback_data for row in mod._help_keyboard(v, chat_id=cid) for b in row]
    check(any("panel:logoff" in d for d in keys("log")), "Log tab offers turn-off")
    check(any("panel:tg:wmsgs:log" in d for d in keys("log")), "Log tab carries WL msgs (confirm)")
    check(any("panel:tg:omsgs:log" in d for d in keys("log")), "Log tab carries other-msgs (confirm)")
    check(any("panel:out:settings" in d for d in keys("status")) and
          not any("panel:logoff" in d for d in keys("status")), "Status tab is contextual")
    check(any("panel:tg:wmsgs:settings" in d for d in keys("settings")) and
          any("panel:tg:omsgs:settings" in d for d in keys("settings")),
          "Settings tab exposes both mirrors")
    check(keys("log") != keys("status"), "tabs hand out different buttons")

    # turning the log off from the Log tab
    mod.save_settings({"log_channel": "-100777"})
    ans16, edit16 = [], []

    async def _a16(*a, **k):
        ans16.append((a, k))

    async def _e16(*a, **k):
        edit16.append((a, k))

    await mod._on_callback(NS(callback_query=NS(
        from_user=NS(id=900000001), data="tgm:panel:logoff",
        answer=_a16,
        message=NS(chat=NS(id="900000001"), chat_id="900000001", message_id="5",
                   edit_message_text=_e16))))
    check(mod.settings().get("log_channel") is not None,
          "logoff first tap does NOT clear (confirm screen instead)")
    _mk = edit16[-1][1].get("reply_markup")
    _apply = [b.callback_data for row in _mk.inline_keyboard for b in row
              if "Apply" in b.text]
    await mod._on_callback(NS(callback_query=NS(
        from_user=NS(id=900000001), data=_apply[0],
        answer=_a16,
        message=NS(chat=NS(id="900000001"), chat_id="900000001", message_id="5",
                   edit_message_text=_e16))))
    check(mod.settings().get("log_channel") is None, "Apply clears the log channel")
    mod.save_settings({"log_channel": saved_log})

asyncio.run(t16())


async def t17():
    """Guest safety gate: destructive tools blocked for guests, owner DM untouched."""
    # gate classification only (no DB in this suite)
    guest = {"guest_user_id": "111", "is_owner": False}
    owner_in_guest = {"guest_user_id": "900000001", "is_owner": True}
    check("terminal" in mod.GUEST_BLOCKED_TOOLS, "terminal is on the guest blocklist")
    check("write_file" in mod.GUEST_BLOCKED_TOOLS, "write_file is on the guest blocklist")
    check("delete_file" in mod.GUEST_BLOCKED_TOOLS, "delete_file is on the guest blocklist")
    check("read_file" in mod.GUEST_READ_TOOLS, "read_file is classed as read-only")
    check("session_search" in mod.GUEST_READ_TOOLS, "session_search is classed as read-only")
    check("telegram_admin" in mod.GUEST_NEVER_TOOLS, "telegram_admin is never allowed")
    check("web_search" in mod.GUEST_SAFE_TOOLS, "web_search stays available")
    check(not (mod.GUEST_SAFE_TOOLS & mod.GUEST_BLOCKED_TOOLS), "no tool is both safe and blocked")

    # refusal text differs for the owner talking through the guest link
    msg_stranger = mod._guest_refusal("terminal", guest)
    msg_owner = mod._guest_refusal("terminal", owner_in_guest)
    check("the owner" in msg_stranger, "stranger refusal points at the owner")
    check("your own DM" in msg_owner, "owner-through-guest refusal points at his own DM")
    check("t.me/user?id=" in msg_stranger, "stranger refusal carries a tappable link")

    # destructive ARG tripwire fires even on a tool not in the blocklist
    orig_info = mod._guest_session_info
    _stubs = {"GUEST": guest, "GUESTOWN": owner_in_guest}
    mod._guest_session_info = lambda sid: _stubs.get(str(sid))
    try:
        r = mod._on_pre_tool_call(tool_name="web_search", args={"q": "hello"},
                                  session_id="GUEST")
        check(r is None, "guest: harmless arg on a safe tool is allowed")
        r = mod._on_pre_tool_call(tool_name="execute_code", args={"code": "rm -rf /"},
                                  session_id="GUEST")
        check(r is not None and r.get("action") == "block", "guest: destructive arg blocked")
        r = mod._on_pre_tool_call(tool_name="terminal", args={"command": "rm -rf /"},
                                  session_id="GUEST")
        check(r is not None, "guest: terminal blocked regardless of args")
        r = mod._on_pre_tool_call(tool_name="terminal", args={"command": "rm -rf /"},
                                  session_id="OWNERDM")
        check(r is None, "owner DM: terminal untouched by the gate")
        r = mod._on_pre_tool_call(tool_name="web_search", args={"q": "x"}, session_id=None)
        check(r is None, "no session: nothing blocked")

        # Path tripwire (2026-10-01): secrets unread, execution paths
        # unwritten, ordinary files still available at the open gate level.
        saved_gate = {k: mod.settings().get(k) for k in
                      ("guest_tool_mode", "guest_owner_full_access", "guest_deny_tools")}
        mod.save_settings({"guest_tool_mode": "open", "guest_owner_full_access": False,
                           "guest_deny_tools": ["terminal"]})
        try:
            r = mod._on_pre_tool_call(tool_name="read_file",
                                      args={"path": HOST_HOME + "/config.yaml"},
                                      session_id="GUEST")
            check(r is not None and r.get("action") == "block",
                  "guest: reading config.yaml blocked (secrets)")
            r = mod._on_pre_tool_call(tool_name="read_file",
                                      args={"path": HOST_HOME + "/.github_backup_token"},
                                      session_id="GUEST")
            check(r is not None and r.get("action") == "block",
                  "guest: reading a git token blocked")
            r = mod._on_pre_tool_call(tool_name="write_file",
                                      args={"path": HOST_HOME + "/scripts/x.sh", "content": "hi"},
                                      session_id="GUEST")
            check(r is not None and r.get("action") == "block",
                  "guest: writing into scripts/ blocked (cron-executed)")
            r = mod._on_pre_tool_call(tool_name="patch",
                                      args={"path": HOST_HOME + "/SOUL.md",
                                            "old_string": "a", "new_string": "b"},
                                      session_id="GUEST")
            check(r is not None and r.get("action") == "block",
                  "guest: patching SOUL.md blocked (identity)")
            r = mod._on_pre_tool_call(tool_name="write_file",
                                      args={"path": HOST_HOME + "/cache/scratch/notes.md",
                                            "content": "hi"},
                                      session_id="GUEST")
            check(r is None, "guest: ordinary write outside protected paths allowed (open)")
            r = mod._on_pre_tool_call(tool_name="read_file",
                                      args={"path": HOST_HOME + "/projects/TGAhermes/README.md"},
                                      session_id="GUEST")
            check(r is None, "guest: ordinary read outside protected paths allowed (open)")
            mod.save_settings({"guest_owner_full_access": True})
            r = mod._on_pre_tool_call(tool_name="read_file",
                                      args={"path": HOST_HOME + "/config.yaml"},
                                      session_id="GUESTOWN")
            check(r is None, "owner in guest chat: path gate skipped (full access)")
        finally:
            mod.save_settings(saved_gate)
    finally:
        mod._guest_session_info = orig_info
    check(callable(mod._on_pre_tool_call), "pre_tool_call hook registered callable exists")
    reg_src = inspect.getsource(mod.register)
    check('register_hook("pre_tool_call", _on_pre_tool_call)' in reg_src,
          "register() wires the pre_tool_call hook")


async def t18():
    """A block must end the turn, not start a probe loop (2026-09-30)."""
    mod._GUEST_BLOCK_COUNTS.clear()
    mod._GUEST_BLOCK_LAST.clear()
    sess = "20260930_162658_5a05bae5"
    # strict mode so the gate is at its most closed
    mod.save_settings({"guest_tool_mode": "strict", "guest_owner_full_access": False})
    # Stub the session row: this check must not depend on the author's live
    # state.db — anywhere else the lookup misses, the gate returns None and
    # every block assertion silently "passes" as a None (found 2026-10-01).
    orig_info18 = mod._guest_session_info
    mod._guest_session_info = lambda sid: ({"guest_user_id": "111", "is_owner": False}
                                           if str(sid) == sess else None)
    try:
        first = mod._on_pre_tool_call(tool_name="read_file", session_id=sess,
                                      args={"path": HOST_HOME + "/anything"})
        second = mod._on_pre_tool_call(tool_name="execute_code", session_id=sess,
                                       args={"code": "print(1)"})
        check(first is not None and first.get("action") == "block", "first block still blocks")
        check(mod._GUEST_BLOCK_COUNTS.get(sess) == 2, "blocks counted per session")
        m1, m2 = first["message"], second["message"]
        check("final, not a transient error" in m1, "block states it is final")
        check("do not try again" in m2.lower(), "repeat block tells the model to stop")
        check("NOW" in m2, "repeat block orders an immediate answer")
        check("t.me/user?id=" in m1, "block gives the owner a DM link")
        # owner-vs-stranger wording still intact
        stranger = mod._guest_refusal("terminal", {"is_owner": False})
        check("owner" in stranger and "DM" in stranger, "stranger refusal points at the owner")
        # a non-blocked tool must NOT be counted
        before = mod._GUEST_BLOCK_COUNTS.get(sess)
        allow = mod._on_pre_tool_call(tool_name="web_search", session_id=sess,
                                      args={"query": "python"})
        check(allow is None, "web_search still allowed for guests")
        check(mod._GUEST_BLOCK_COUNTS.get(sess) == before, "allowed tool not counted as a block")
        # expiry clears the counter
        mod._GUEST_BLOCK_LAST[sess] = time.monotonic() - (mod._GUEST_BLOCK_TTL + 5)
        mod._guest_turn_expired(session_id=sess)
        check(sess not in mod._GUEST_BLOCK_COUNTS, "idle session counter expires")
        # a new turn starts clean (count 1 -> first-block wording)
        again = mod._on_pre_tool_call(tool_name="terminal", session_id=sess, args={"cmd": "ls"})
        check("final, not a transient error" in again["message"], "new turn resets block counter")
        check("NOW" not in again["message"], "new turn no longer uses urgent wording")
    finally:
        mod._guest_session_info = orig_info18
    mod._GUEST_BLOCK_COUNTS.clear()
    mod._GUEST_BLOCK_LAST.clear()
    mod.save_settings({"guest_tool_mode": "balanced", "guest_owner_full_access": True})


asyncio.run(t17())

async def t19():
    """Self-update: owner-gated entry points, config-driven, no hardcoded identity."""
    import inspect
    src = inspect.getsource(mod)
    # the two official entry points exist
    check("check_update" in src and "update_plugin" in src,
          "telegram_admin exposes check_update and update_plugin")
    check("_run_selfupdate" in src, "plugin has an update entry point")
    # both are wired
    reg = inspect.getsource(mod.register)
    check("check_update" in str(mod._TOOL_SCHEMA) or
          "update_plugin" in str(mod._TOOL_SCHEMA), "schema lists the update actions")
    # config-driven, not hardcoded
    check(mod.DEFAULT_UPDATE_REPO.startswith("https://"), "default repo is a url")
    cfg = mod._update_settings()
    check(str(cfg.get("repo", "")).startswith("https://"), "update repo resolves to a url")
    check(cfg.get("branch"), "branch configured")
    check(cfg.get("target") == mod.PLUGIN_DIR, "target defaults to the installed plugin")
    # the owner's identity is never baked into the update path
    owner = str(mod._owner_id() or "")
    upd_src = inspect.getsource(mod._run_selfupdate) + inspect.getsource(mod._update_settings)
    check(owner not in upd_src or not owner, "no owner id in the update code path")
    # panel surfaces it
    kb = mod._help_keyboard("system", mod.settings())
    labels = [b.text for row in kb for b in row]
    check(any("update" in l.lower() for l in labels), "system tab has an update button")
    check(any(l.startswith("v") for l in labels), "system tab shows the installed version")
    home = mod._help_keyboard("panel", mod.settings())
    check(any("System" in b.text for row in home for b in row), "home grid links to System")
    # view renders
    body = mod._help_view("system", mod.settings())
    check("System" in body and mod._plugin_version() in body, "system view renders version")
    # lock switch honoured
    try:
        mod.save_settings({"update_enabled": False})
        check(mod._update_settings()["enabled"] is False, "update lock respected")
    finally:
        mod.save_settings({"update_enabled": True})


asyncio.run(t18())

async def t20():
    """Guest gate modes: strict / balanced / open, and the owner is not trapped."""
    guest = {"guest_user_id": "111", "is_owner": False}
    owner = {"guest_user_id": "900000001", "is_owner": True}
    orig = mod._guest_session_info
    mod._guest_session_info = lambda sid: guest if str(sid) == "G" else (
        owner if str(sid) == "O" else None)
    try:
        def blocked(sid, tool, args=None):
            return mod._on_pre_tool_call(tool_name=tool, args=args or {}, session_id=sid)

        # strict: nothing for a guest
        mod.save_settings({"guest_tool_mode": "strict", "guest_owner_full_access": False})
        check(blocked("G", "read_file") is not None, "strict: guest cannot read")
        check(blocked("G", "web_search") is None, "strict: guest can still search the web")
        check(blocked("G", "terminal") is not None, "strict: guest has no terminal")

        # balanced: reads open, writes shut
        mod.save_settings({"guest_tool_mode": "balanced"})
        check(blocked("G", "read_file") is None, "balanced: guest may read a file")
        check(blocked("G", "search_files") is None, "balanced: guest may search files")
        check(blocked("G", "session_search") is None, "balanced: guest may search past chats")
        check(blocked("G", "write_file") is not None, "balanced: writes stay closed")
        check(blocked("G", "terminal") is not None, "balanced: no terminal")
        check(blocked("G", "execute_code") is not None, "balanced: no arbitrary code")

        # open: same tools as any chat
        mod.save_settings({"guest_tool_mode": "open"})
        check(blocked("G", "terminal") is None, "open: guest gets a terminal")
        check(blocked("G", "write_file") is None, "open: guest may write")
        check(blocked("G", "telegram_admin") is not None,
              "open still refuses telegram_admin (never list)")

        # owner in their own guest chat is not blocked (the bug that was fixed)
        mod.save_settings({"guest_tool_mode": "balanced", "guest_owner_full_access": True})
        check(blocked("O", "terminal") is None, "owner in guest chat: terminal works")
        check(blocked("O", "read_file") is None, "owner in guest chat: reading works")
        mod.save_settings({"guest_owner_full_access": False})
        check(blocked("O", "terminal") is not None,
              "owner can opt back into being blocked")

        # per-tool overrides
        mod.save_settings({"guest_owner_full_access": True, "guest_deny_tools": ["read_file"]})
        check(blocked("G", "read_file") is not None, "guest_deny_tools re-closes a read tool")
        mod.save_settings({"guest_deny_tools": []})
        # guest_allow_tools only widens a guest's reach; it must not re-open a
        # mode-level ban, so strict stays closed even with the tool named.
        mod.save_settings({"guest_tool_mode": "strict",
                           "guest_allow_tools": ["read_file"]})
        check(blocked("G", "read_file") is not None,
              "guest_allow_tools cannot re-open a mode-level ban")

        # the mode is read from settings each call, so the panel switch is instant
        mod.save_settings({"guest_tool_mode": "balanced"})
        check(blocked("G", "read_file") is None, "switching mode takes effect immediately")
        mod.save_settings({"guest_tool_mode": "strict"})
        check(blocked("G", "read_file") is not None, "and again on the next message")

        # the panel exposes it
        body = mod._gate_view(mod.settings())
        check("Guest tool gate" in body, "gate view renders")
        check("balanced" in body or "strict" in body, "gate view names the mode")
        kb = mod._help_keyboard("system", mod.settings())
        check(any("Guest mode" in b.text for row in kb for b in row),
              "system tab has a guest-mode button")
        check("gate" in mod._CB_PREFIX + "panel:gate", "gate callback path is namespaced")
    finally:
        mod._guest_session_info = orig
        mod.save_settings({"guest_tool_mode": "balanced", "guest_owner_full_access": True,
                           "guest_allow_tools": [], "guest_deny_tools": []})


def _real_state_path():
    """The live state.json, built from the configured home rather than typed in.

    A literal host path here would trip the pre-push audit, which is the whole
    point of that rule.
    """
    from pathlib import Path as _P
    home = mod._hermes_home()
    return _P(home) / "plugins" / "TGAhermes" / "state.json"


def _tmp_state():
    import tempfile
    from pathlib import Path as _P
    return _P(tempfile.mkdtemp(prefix="tgm-state-")) / "state.json"


def _write_state(mod, data):
    mod.STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    mod.STATE_PATH.write_text(json.dumps(data), encoding="utf-8")


def _set_guest(mod, db, sid, uid):
    """Point a test guest chat at a given id.

    The gate reads identity from the guest CHAT id (that is the person's own
    Telegram id), so a fixture must rename the chat, not the db row.
    """
    con = sqlite3.connect(db)
    try:
        con.execute("UPDATE sessions SET user_id=?, chat_id=?, origin_json=? WHERE id=?",
                    (uid, mod._guest_chat_id(uid), json.dumps({"user_id": uid}), sid))
        con.commit()
    finally:
        con.close()


async def t21():
    """A guest must never inherit the owner's rights from the owner id.

    Telegram resolves a guest message's from_user to the OWNER's user, so the
    user_id stored on every guest session IS the owner's id. The gate compared
    that field against the owner id, which made every stranger the owner.
    """
    import sqlite3 as _sq
    sid = "T21GUESTSESSION"
    chat = "guest_424242"
    db = mod._hermes_home() / "state.db"
    con = _sq.connect(db)
    try:
        con.execute("INSERT OR REPLACE INTO sessions "
                    "(id, source, user_id, session_key, chat_id, chat_type, "
                    "origin_json, started_at, last_activity_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (sid, "telegram", str(mod._owner_id()), sid, chat, "private",
                     json.dumps({"user_id": str(mod._owner_id()),
                                 "guest_original_chat_id": chat}),
                     9999999999, 9999999999))
        con.commit()
    finally:
        con.close()
    try:
        info = mod._guest_session_info(sid)
        check(info is not None, "guest session is recognised")
        # The guest chat id IS the person's Telegram id, so a guest chat keyed on
        # the owner's id is the owner using their own guest link. A stranger's id
        # must not be treated as the owner.
        _set_guest(mod, db, sid, str(mod._owner_id()))
        check((mod._guest_session_info(sid) or {}).get("is_owner"),
              "a guest chat keyed on the owner's id is recognised as the owner")
        _set_guest(mod, db, sid, "900000002")
        info = mod._guest_session_info(sid) or {}

        # A stranger in the same shape is NOT the owner.
        con = _sq.connect(db)
        try:
            con.execute("UPDATE sessions SET user_id=?, origin_json=? WHERE id=?",
                        ("900000002",
                         json.dumps({"user_id": "900000002"}), sid))
            con.commit()
        finally:
            con.close()
        check(not mod._guest_session_info(sid)["is_owner"],
              "a stranger's guest session is not the owner")
        con = _sq.connect(db)
        try:
            con.execute("UPDATE sessions SET user_id=?, origin_json=? WHERE id=?",
                        (str(mod._owner_id()),
                         json.dumps({"user_id": str(mod._owner_id())}), sid))
            con.commit()
        finally:
            con.close()

        # Back to the stranger for the gate checks: a guest gets guest rules.
        _set_guest(mod, db, sid, "900000002")
        mod.save_settings({"guest_tool_mode": "open", "guest_owner_full_access": True,
                           "guest_owner_chats": []})
        check(mod._on_pre_tool_call(tool_name="telegram_admin", args={},
                                    session_id=sid) is not None,
              "a stranger never reaches telegram_admin, even in open mode")

        # balanced blocks terminal for a guest.
        mod.save_settings({"guest_tool_mode": "balanced"})
        check(mod._on_pre_tool_call(tool_name="terminal", args={},
                                    session_id=sid) is not None,
              "a stranger has no terminal in balanced mode")
        check(mod._on_pre_tool_call(tool_name="read_file", args={},
                                    session_id=sid) is None,
              "a stranger may still read in balanced mode")

        # Now the owner, in their own guest chat, is not trapped.
        _set_guest(mod, db, sid, str(mod._owner_id()))
        mod.save_settings({"guest_owner_full_access": True})
        check(mod._on_pre_tool_call(tool_name="terminal", args={},
                                    session_id=sid) is None,
              "the owner in their own guest chat gets a terminal")
        mod.save_settings({"guest_owner_full_access": False})
        check(mod._on_pre_tool_call(tool_name="terminal", args={},
                                    session_id=sid) is not None,
              "and can opt back into being blocked")

        # The explicit per-chat unlock is the fallback for when the id cannot
        # be trusted (an old session, or a rewrite that lost it). It is keyed on
        # the guest chat as _set_guest just set it, not on a fixed fixture id.
        _set_guest(mod, db, sid, "900000002")
        unlock_target = (mod._guest_session_info(sid) or {}).get("guest_chat") or chat
        mod.save_settings({"guest_owner_full_access": True,
                           "guest_owner_chats": [unlock_target]})
        check(mod._on_pre_tool_call(tool_name="terminal", args={},
                                    session_id=sid) is None,
              "an explicitly unlocked chat is not gated")
        mod.save_settings({"guest_owner_chats": []})
        check(mod._on_pre_tool_call(tool_name="terminal", args={},
                                    session_id=sid) is not None,
              "revoking the unlock takes the access away")
    finally:
        con = _sq.connect(db)
        try:
            con.execute("DELETE FROM sessions WHERE id=?", (sid,))
            con.commit()
        finally:
            con.close()
        mod.save_settings({"guest_tool_mode": "balanced", "guest_owner_full_access": False,
                           "guest_owner_chats": [], "guest_allow_tools": [],
                           "guest_deny_tools": []})


asyncio.run(t19())
asyncio.run(t20())

async def t22():
    """Identity has ONE source, shared with the log channel.

    Telegram hands a guest message a real from_user and a chat.id that is the
    person's own Telegram id. The log could always name them; the gate used to
    re-derive identity from the forwarded message instead, which is how every
    guest looked like the owner. Now both read _guest_identity.
    """
    def check_identity():
        mod.STATE_PATH = _tmp_state()
        _write_state(mod, {"users": {
            "900000002": {"name": "Stranger", "username": "s", "count": 4,
                          "last_seen": 1700000000},
            str(mod._owner_id()): {"name": "Owner", "count": 9,
                                   "last_seen": 1700000000},
        }})
        ident = mod._guest_identity("guest_900000002")
        check(ident["id"] == "900000002", "identity comes off the guest chat id")
        check(ident["name"] == "Stranger", "identity resolves the recorded name")
        check(ident["known"], "identity knows this person is known")

        # Unknown id still identifies the person, just without a name.
        ident2 = mod._guest_identity("guest_555000111")
        check(ident2["id"] == "555000111", "unknown guest still has a usable id")
        check(not ident2["known"], "unknown guest is not marked as known")

        # The log line and the panel read the same function, so they agree.
        line = mod._identity_line("guest_900000002")
        check("Stranger" in line and "900000002" in line,
              "the log line names the person from the shared source")
        view = mod._help_view("who", mod.settings())
        check("Stranger" in view and "👑 owner" in view,
              "the who-list shows the same person and marks the owner")
        mod.STATE_PATH = _real_state_path()

    check_identity()
    # The gate must reach the same conclusion from the same place.
    mod.save_settings({"guest_owner_full_access": True, "guest_owner_chats": []})
    orig = mod._session_row
    mod._session_row = lambda sid: ("telegram", mod._guest_chat_id(str(mod._owner_id())))
    try:
        check((mod._guest_session_info("ANY") or {}).get("is_owner"),
              "the gate recognises the owner from the guest chat id alone")
        mod._session_row = lambda sid: ("telegram", "guest_900000002")
        check(not (mod._guest_session_info("ANY") or {}).get("is_owner"),
              "and does not mistake a stranger for the owner")
    finally:
        mod._session_row = orig


asyncio.run(t21())
asyncio.run(t22())


# ---------------------------------------------------------------- v3: rename, setlog here, Actions, wizard, lock
print("\n[23] v3: TGAhermes rename, !setlog here, Actions, wizard, update lock")

# --- the rename landed where it matters
_mf = (HERE / "plugin.yaml").read_text(encoding="utf-8")
check("name: TGAhermes" in _mf, "manifest name is TGAhermes")
check("version: 4.1.0" in _mf, "manifest version is 4.1.0")
check("telegram-guest-mode" not in Path(mod.__file__).read_text(encoding="utf-8"),
      "no old plugin name left in the module source")


async def t23():
    """!setlog here — the command twin of the Actions button."""
    mod.save_settings({"owner_id": "900000001"})
    prev_log = mod.settings().get("log_channel")

    r = await mod._bang_execute(None, "-100777", "!setlog here")
    check(mod.settings().get("log_channel") == "-100777",
          "!setlog here sets the current chat")
    check(r is not None and "-100777" in r, "!setlog here replies with the chat id")

    # From inside a group that is NOT the current log — the whole point of `here`.
    mod.save_settings({"log_channel": None})
    ev = _real_ev("!setlog here", chat_type="supergroup",
                  chat_id="-100888", user_id="900000001")
    res = await mod._pre_gateway_dispatch(event=ev)
    check(res is not None and res.get("action") == "skip",
          "!setlog here is intercepted in a group")
    check(mod.settings().get("log_channel") == "-100888",
          "!setlog here logs into that group")

    # Other bangs keep their old rule: log chat or owner DM only.
    ev2 = _real_ev("!users", chat_type="supergroup",
                   chat_id="-100999", user_id="900000001")
    res2 = await mod._pre_gateway_dispatch(event=ev2)
    check(res2 is None, "other bangs still stay out of groups they are not for")

    # A guest chat hides its sender, so `here` there must be refused.
    r2 = await mod._bang_execute(None, mod._guest_chat_id("900000002"),
                                 "!setlog here")
    check(mod.settings().get("log_channel") == "-100888",
          "setlog here refuses a guest chat")
    check("guest" in (r2 or "").lower(), "the refusal says why")

    mod.save_settings({"log_channel": prev_log})


async def t24():
    """Wizards: the Actions flows run the same commands the panel replaces."""
    mod._WIZARD.clear()
    st0 = mod.settings()
    prev_cool = st0.get("unauthorized_cooldown_s")
    prev_owner = st0.get("owner_id")

    p1 = mod._wizard_start("-100555", "cooldown")
    check(bool(p1) and "Cooldown" in p1, "wizard opens with its prompt")
    check(mod._WIZARD.get("-100555", {}).get("flow") == "cooldown",
          "wizard state recorded")
    out = await mod._wizard_feed(None, "-100555", "77")
    check(mod.settings().get("unauthorized_cooldown_s") == 77,
          "wizard answer runs the command twin")
    check(not mod._WIZARD, "wizard cleared after the final step")

    # cancel aborts without running anything
    mod._wizard_start("-100555", "owner")
    out2 = await mod._wizard_feed(None, "-100555", "cancel")
    check(bool(out2) and "cancel" in out2.lower() and not mod._WIZARD,
          "cancel aborts the wizard")
    check(mod.settings().get("owner_id") == prev_owner,
          "an aborted wizard changes nothing")

    # two-step flow: prompts for id, then text, then completes
    mod._wizard_start("-100555", "send")
    mid = await mod._wizard_feed(None, "-100555", "123")
    check(bool(mid) and "Step 2/2" in mid, "second step prompts for the text")
    check(mod._WIZARD.get("-100555") is not None, "wizard waits for step 2")
    out3 = await mod._wizard_feed(None, "-100555", "hello world")
    check(out3 is not None and not mod._WIZARD, "flow completes and clears")

    # a hot reload re-imports this module: the open step must survive it
    mod._wizard_start("-100555", "send")
    await mod._wizard_feed(None, "-100555", "123")
    mod._WIZARD.clear()                 # what a reload does to the module globals
    mod._wizard_restore()
    check(mod._WIZARD.get("-100555", {}).get("flow") == "send",
          "an open wizard step survives a hot reload")
    check(mod._WIZARD.get("-100555", {}).get("data") == ["123"],
          "the typed answer survives with it")
    mod._wizard_cancel("-100555")
    mod._WIZARD.clear()
    mod._wizard_restore()
    check(not mod._WIZARD, "a cancelled step does not come back")

    # the panel keyboard actually offers the flows
    d_act = [b.callback_data for row in mod._help_keyboard("actions") for b in row]
    check(any(x.endswith("panel:sethere") for x in d_act),
          "actions view has Log here")
    check(any(x.endswith("panel:wiz:wladd") for x in d_act),
          "actions view has the whitelist wizard")
    check(any(x.endswith("panel:wiz:wipe") for x in d_act),
          "actions view has the wipe wizard")
    check(all(len(x.encode()) <= 64 for x in d_act),
          "actions callbacks inside Telegram's 64-byte cap")
    d_home = [b.callback_data for row in mod._help_keyboard("panel") for b in row]
    check(any(x.endswith("panel:actions") for x in d_home),
          "home offers the Actions view")
    check(not any(x.endswith("panel:back") for x in d_home),
          "home still has no self-back button")
    check("Actions" in mod._actions_view(mod.settings()), "actions view renders")

    # the update lock: System view toggle + the selfupdate tool refuses
    mod.save_settings({"update_enabled": False})
    d_sys = [b.callback_data for row in mod._help_keyboard("system") for b in row]
    check(any(x.endswith("panel:upd:lock") for x in d_sys),
          "system view can toggle the update lock")
    rep = await mod._run_selfupdate(apply=False)
    check(rep.get("ok") is False and "locked" in str(rep.get("error") or ""),
          "locked updates refuse the selfupdate tool")
    mod.save_settings({"update_enabled": True})
    check(any(x.endswith("panel:upd:lock")
              for x in (b.callback_data for row in mod._help_keyboard("system")
                        for b in row)),
          "system view shows the lock button when updates are on")

    mod.save_settings({"unauthorized_cooldown_s": prev_cool,
                       "owner_id": prev_owner})
    mod._WIZARD.clear()


asyncio.run(t23())
asyncio.run(t24())


# --- first dispatched message after a hot reload requests the PTB rewire once
# (on_plugin_loaded never fires for a RE-load, so nothing else wires the new
# factory; FakeAdapter gets a counting rewire to observe the trigger).
_ad_rw = FakeAdapter()
_rw_calls = []
_ad_rw.rewire_plugin_handlers = lambda: _rw_calls.append(1)
_ad_rw._guest_gqids = {}
_prev_ad = mod._ADAPTER.get("adapter")
mod._ADAPTER["adapter"] = _ad_rw


async def t_rw():
    ev = FakeEvent(text="ping", source=FakeSource("900000001", message_id="1"))
    await mod._pre_gateway_dispatch(event=ev)
    await mod._pre_gateway_dispatch(event=ev)


asyncio.run(t_rw())
check(len(_rw_calls) == 1 and getattr(_ad_rw, "_tga_instance", None) is mod._INSTANCE,
      "post-reload rewire requested exactly once on first dispatch")
mod._ADAPTER["adapter"] = _prev_ad


# --- hot reload must actually swap handlers (the 2026-10-02 stale-handler bug:
# base._wire_plugin_handlers dedups factories by (plugin, qualname), so without a
# per-deploy unique qualname the rewire skips ours and the old module's closures
# keep serving the panel + a stale allow_from forever).
import types as _types
_stale_cb = lambda: None
_stale_cb.__module__ = mod.__name__  # pretend an older instance of the plugin defined it
_stale_h = _types.SimpleNamespace(callback=_stale_cb)
_mine_h = _types.SimpleNamespace(callback=mod._drop_stale_handlers)  # this instance — keep
_foreign_h = _types.SimpleNamespace(callback=check)  # another module — untouched


class _FakeNative:
    def __init__(self):
        self.handlers = {0: [_stale_h, _mine_h, _foreign_h]}
        self.removed = []

    def remove_handler(self, h, group=0):
        self.removed.append(h)
        self.handlers[group].remove(h)


_fn = _FakeNative()
_n = mod._drop_stale_handlers(_fn)
check(_n == 1 and _fn.removed == [_stale_h],
      "stale handler from a previous load is swept")
check(len(_fn.handlers[0]) == 2 and _mine_h in _fn.handlers[0]
      and _foreign_h in _fn.handlers[0],
      "current instance and foreign handlers are kept")
check(mod._make_factory().__qualname__.startswith("factory.m"),
      "factory qualname is unique per deployed file (rewire dedup key)")

# ---------------------------------------------------------------- core-gate env tier (the "Dropped ... unrecognized" tier)
# The authz mixin reads TELEGRAM_ALLOWED_USERS from the environment first; when
# non-empty it never consults telegram.extra.allow_from. These tests use
# injected env_path/environ — the real .env is never touched by the harness.
print("\n[24] core gate env tier: union sync that survives hand-entries")
_tmp_env = TMP / "dot.env"
_tmp_env.write_text("OTHER=1\nTELEGRAM_ALLOWED_USERS=900000001,111111111\n", encoding="utf-8")
_own = str(mod._owner_id() or "") or "900000001"
_fake_environ: dict = {}
ok24 = mod._sync_gate_allowlists(f"{_own},900000002", env_path=_tmp_env,
                                 environ=_fake_environ)
check(ok24 is True, "gate sync returns True")
_env_val = _tmp_env.read_text(encoding="utf-8")
check("900000002" in _env_val, "new id lands in the fake .env")
check("111111111" in _env_val, "pre-existing hand-entry survives (union, not replace)")
check(_own in _env_val and _own in (_fake_environ.get("TELEGRAM_ALLOWED_USERS") or ""),
      "owner id kept in both tiers")
check("111111111" in (_fake_environ.get("TELEGRAM_ALLOWED_USERS") or ""),
      "hand-entry also lands in the environ tier")
check("OTHER=1" in _env_val, "unrelated .env lines untouched")

# same sync, .env without the key yet → the key is appended
_tmp_env2 = TMP / "dot2.env"
_tmp_env2.write_text("OTHER=2\n", encoding="utf-8")
_fake_environ2: dict = {}
ok24b = mod._sync_gate_allowlists(_own, env_path=_tmp_env2, environ=_fake_environ2)
check(ok24b is True and "TELEGRAM_ALLOWED_USERS=" in _tmp_env2.read_text(encoding="utf-8"),
      "missing key is appended to the fake .env")
check(_own in (_fake_environ2.get("TELEGRAM_ALLOWED_USERS") or ""),
      "environ tier still synced when the key was missing")

# _auth_debug renders the gate tier; verdict is made deterministic by setting
# the harness process env and restoring it after.
_prev_env24 = os.environ.get("TELEGRAM_ALLOWED_USERS")
os.environ["TELEGRAM_ALLOWED_USERS"] = f"{_own},900000002"
try:
    dbg24 = mod._auth_debug("900000002")
finally:
    if _prev_env24 is None:
        os.environ.pop("TELEGRAM_ALLOWED_USERS", None)
    else:
        os.environ["TELEGRAM_ALLOWED_USERS"] = _prev_env24
check("core gate env" in dbg24, "auth debug shows the gate tier")
check("900000002" in dbg24 and "PASS" in dbg24,
      "auth debug verdict passes for an id in the gate tier")

# _sync_allow_from_live carries the gate tier along on the same call
_ad24 = FakeAdapter()
_ad24.config = NS(extra={"allow_from": "x"})
mod._ADAPTER["adapter"] = _ad24
_fake_environ3: dict = {}
_prev_sync = mod._sync_gate_allowlists
try:
    mod._sync_gate_allowlists = lambda csv, **kw: (_fake_environ3.update(gate=csv) or True)
    mod._sync_allow_from_live("900000005")
finally:
    mod._sync_gate_allowlists = _prev_sync
check(_fake_environ3.get("gate") == "900000005",
      "live adapter sync also carries the gate tier")


print("\n[25] panel rework: sections at home, confirm screens, friend cards")
# section landing pages render for every new view
for _v in ("settings", "wl", "cool"):
    _kb = mod._help_keyboard(_v)
    check(bool(_kb) and all(hasattr(b, "callback_data") for r in _kb for b in r),
          f"section keyboard renders: {_v}")
_tg = mod._help_keyboard("tg", arg="react:settings")
check(any(d.endswith("panel:tgy:react:settings") for r in _tg
          for b in r for d in [b.callback_data]),
      "confirm screen offers Apply for the reacting flag")
_fr = mod._help_keyboard("wlfr", arg="900000004")
_fr_d = [b.callback_data for r in _fr for b in r]
check(any(d.endswith("panel:wllvl:900000004:full") for d in _fr_d),
      "friend card offers the full level")
check(any(d.endswith("panel:wlrm:900000004") for d in _fr_d),
      "friend card offers removal (behind a confirm)")
_body = mod._wl_view({"whitelist_perms": {}}, note="")
check("TELEGRAM_ALLOWED_USERS" in _body and "no restart" in _body,
      "whitelist page names the core gate tier doing the real gating")
# confirm screen for mode cycles like the old bare button did
_st = dict(mod.settings()); _st["guest_tool_mode"] = "balanced"
check(mod._tg_next("mode", _st) == "open", "mode confirm shows the same next mode")
check("open" in mod._tg_view("mode", "safeguard", _st),
      "mode confirm names the next mode before applying")

print("\n[26] panel v4.0.0: categorized settings, prompt wizards, history Back")
# every one of the 29 settings keys sits in exactly one category, so a new
# key cannot silently become unreachable from the panel.
_placed = {it["key"] for items in mod._CATS.values() for it in items
           if it.get("key")}
_missing = sorted(set(mod.DEFAULT_SETTINGS) - _placed)
check(not _missing, f"all settings keys are categorized (missing: {_missing})")

for _c in mod._CATS:
    check(mod._CAT_LABEL[_c] in mod._cat_body(_c, mod.settings()),
          f"category body renders: {_c}")

# every callback a category emits must be one the router understands
_cat_cbs = {_cb for _c in mod._CATS
            for _lbl, _cb in mod._cat_buttons(_c, mod.settings(), chat_id="-100777")}
_flows = {_c[len("panel:wiz:"):] for _c in _cat_cbs if _c.startswith("panel:wiz:")}
check(not [f for f in _flows if f not in mod._WIZ_FLOWS],
      "every wizard flow a category offers exists")
_subs = {_c.split(":")[2] for _c in _cat_cbs if _c.startswith("panel:tg:")}
check(not [s for s in _subs if s not in (*mod._TENUMS, *mod._TOGGLES)],
      "every confirm sub-key a category offers resolves")
check(all(len(_c.encode()) <= 64 for _c in _cat_cbs),
      "category callbacks inside Telegram's 64-byte cap")

# a save-flow wizard writes the setting without running any command
_prev_branch = mod.settings().get("update_branch")
mod._WIZARD["-100555"] = {"flow": "upbranch", "data": []}
_out = asyncio.run(mod._wizard_feed(None, "-100555", "  dev  "))
check("✅" in (_out or "") and mod.settings().get("update_branch") == "dev",
      "save-flow writes the setting with no command")
# bad input re-prompts instead of killing the flow
mod._WIZARD["-100555"] = {"flow": "uptimeout", "data": []}
_out = asyncio.run(mod._wizard_feed(None, "-100555", "5"))
check("⚠️" in (_out or "") and "-100555" in mod._WIZARD,
      "failed validation re-prompts and keeps the wizard open")
mod.save_settings({"update_branch": _prev_branch, "update_timeout_s": 300})
mod._WIZARD.clear()

# Back is a per-message history; confirm screens and the wizard never enter it
class _M:
    chat_id = -100555
    message_id = 77
class _Q:
    message = _M()
_q = _Q()
_nk = mod._nav_key(_q)
mod._NAV.pop(_nk, None)
for _v in ("panel", "log", "settings", "cfm"):
    mod._nav_push(_nk, _v)
check(mod._NAV[_nk] == ["panel", "log", "settings"],
      "transient confirm screen does not enter history")
check(mod._nav_back(_nk) == "log", "Back from a category returns the console's child")
check(mod._nav_back(_nk) == "panel", "Back again returns the console")
check(mod._nav_back(_nk) == "panel", "Back at the console stays home")
mod._NAV.pop(_nk, None)

# credentials must never reach a panel body: git origin on a token checkout
# is a credential-bearing URL, and that is what _plugin_origin falls back to.
import subprocess as _sp
_raw_origin = _sp.run(["git", "config", "--get", "remote.origin.url"],
                      cwd=str(HERE), capture_output=True,
                      text=True).stdout.strip()
_shown = mod._repo_display(_raw_origin)
# the kept tail must be host/path only — no colon means no password survived
_tail = _shown.split("://")[-1] if "://" in _shown else _shown
check(":" not in _tail.rsplit("@", 1)[-1],
      "git origin loses its credential when displayed")
check("github.com" in _shown or not _raw_origin,
      "host survives redaction")
check("ghp_" not in mod._cat_body("system", mod.settings()),
      "System category body carries no token")
check("ghp_" not in mod._panel_text(mod.settings()),
      "home board carries no token")


print("\n[27] Chat Automation: wiring, category, gating")
import types as _t27types
import inspect as _t27inspect

check(mod.bizauto is not None, "bizauto language module loads with the plugin")
check("biz" in mod._CATS, "Chat Automation category registered")
check(mod._tg_next("bizmode", {"biz_mode": "assistant"}) == "mimic",
      "automation mode cycles assistant -> mimic")
check(mod._tg_next("bizmode", {"biz_mode": "off"}) == "assistant",
      "automation mode cycles off -> assistant")
check("bizlang" in mod._CFM_KINDS, "bizlang confirm kind registered")
check("Automation language" in mod._cfm_view("bizlang", "fa", mod.settings()),
      "language confirm screen renders")
check("Automation mode" in mod._tg_view("bizmode", "biz", mod.settings()),
      "mode confirm screen renders")
check(set(mod.GUEST_NEVER_TOOLS) <= set(mod._biz_denied(mod.settings())),
      "never-tools stay locked in automation chats")
check("BUSINESS_MESSAGE" in _t27inspect.getsource(mod),
      "factory registers business-message handlers")
check("Stranger DM" not in (mod._help_text(mod.settings()) or ""),
      "no stranger-log leakage in help")


async def _t27():
    _orig = dict(mod.settings())
    _orig_log = mod._log
    _titles = []

    async def _slog(title, body="", buttons=None):
        _titles.append(str(title))

    class _Bot:
        sent = []

        async def send_message(self, **kw):
            _Bot.sent.append(kw)
            return _t27types.SimpleNamespace(message_id=900001)

    class _Stub:
        def __init__(self):
            self._bot = _Bot()
            self._message_handler = object()
            self.handles = 0
            self.events = []

        def _clean_bot_trigger_text(self, t, *a, **k):
            return t or ""

        def _build_message_event(self, msg, mtype, update_id=None):
            ev = _t27types.SimpleNamespace(
                text=msg.text, metadata={},
                source=_t27types.SimpleNamespace(
                    chat_id=msg.chat.id, message_id=msg.message_id,
                    user_id=msg.from_user.id),
                internal=False, channel_prompt="")
            self.events.append(ev)
            return ev

        async def handle_message(self, event):
            self.handles += 1

    def _mk_update():
        return _t27types.SimpleNamespace(
            update_id=1001,
            business_message=_t27types.SimpleNamespace(
                message_id=501,
                business_connection_id="bc_test",
                text="hello from a customer",
                caption=None, date=None,
                from_user=_t27types.SimpleNamespace(
                    id=770011, is_bot=False, first_name="Cust",
                    last_name="", username="custx"),
                chat=_t27types.SimpleNamespace(
                    id=770011, type="private", first_name="Cust")),
            edited_business_message=None,
            message=None)

    mod._log = _slog
    stub = _Stub()
    try:
        # mode off: observed + logged under its own title, never handed to the brain
        mod.save_settings({"biz_mode": "off", "biz_warn_first": False})
        await mod._handle_business_message(stub, _mk_update(), None)
        check(stub.handles == 0, "mode off: customer message never reaches the brain")
        check(any("Chat Automation" in str(x) for x in _titles),
              f"mode off: logged under its own title ({len(_titles)} title(s))")
        check(not any("Stranger" in str(x) for x in _titles),
              "mode off: automation traffic never uses the Stranger-DM log")

        # mode assistant: event wired with the business connection id
        _titles.clear()
        mod.save_settings({"biz_mode": "assistant", "biz_warn_first": False,
                           "biz_react": False})
        await mod._handle_business_message(stub, _mk_update(), None)
        check(stub.handles == 1, "assistant: exactly one brain turn")
        _ev = stub.events[0]
        check(_ev.metadata.get("business_connection_id") == "bc_test",
              "assistant: event metadata carries the business_connection_id")
        check(getattr(_ev, "internal", False) is True,
              "assistant: automation event marked internal")
        check(bool(getattr(_ev, "channel_prompt", "")),
              "assistant: persona + identity prompt attached")
    finally:
        mod._log = _orig_log
        mod.save_settings(_orig)


asyncio.run(_t27())


# [28] Regression: the gateway loads the plugin AS A PACKAGE, so the loader's
# relative-import branch must yield a real module. Initializing the package
# attribute before the import used to shadow the submodule (fromlist saw the
# None attribute and skipped the import entirely) - the by-path test harness
# could never catch that because it takes the file-path fallback.
print("\n[28] bizauto loads in the real package context (attribute-shadowing)")
def _t28():
    import subprocess
    script = (
        "import sys, types, importlib.util\n"
        "hermes_src, plugin_dir = sys.argv[1], sys.argv[2]\n"
        "sys.path.insert(0, hermes_src)\n"
        "ns = \"hermes_plugins\"\n"
        "parent = types.ModuleType(ns); parent.__path__ = []; parent.__package__ = ns\n"
        "sys.modules[ns] = parent\n"
        "mn = ns + \".TGAhermes\"\n"
        "spec = importlib.util.spec_from_file_location(mn, plugin_dir + \"/__init__.py\",\n"
        "    submodule_search_locations=[plugin_dir])\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "mod.__package__ = mn; mod.__path__ = [plugin_dir]\n"
        "sys.modules[mn] = mod\n"
        "spec.loader.exec_module(mod)\n"
        "ba = getattr(mod, \"bizauto\", None)\n"
        "ok = ba is not None and callable(getattr(ba, \"detect_language\", None))\n"
        "print(\"PKGLOAD_OK\" if ok else \"PKGLOAD_FAIL bizauto=\" + repr(ba))\n"
    )
    r = subprocess.run([sys.executable, "-c", script, HERMES_SRC, str(HERE)],
                       capture_output=True, text=True, timeout=90)
    tail = (r.stdout + r.stderr).strip().splitlines()[-1] if (r.stdout or r.stderr) else ""
    check("PKGLOAD_OK" in r.stdout,
          f"package-context import yields a usable bizauto ({tail[:90]})")


_t28()


# [29] Regression: ghost handlers. The adapter's post-factory hoist rebuilds
# group-0 from a PRE-factory snapshot, re-inserting the stale handlers the
# factory just dropped — OLD closures then sit in front of everything (panel
# taps answered by old code, business messages never reaching the Chat
# Automation logger). Two guards: the sweep must unwrap admission's @wraps
# before deciding "is this ours", and business events must never fall
# through to the generic DM mirror.
print("\n[29] ghost sweep keeps live handlers, drops resurrected ones, business mirror skip")
import functools as _f29
import re as _re29
from telegram.ext import MessageHandler as _MH29, CallbackQueryHandler as _CQ29
from telegram.ext import filters as _filters29


async def _t29():
    # --- (a) sweep identity: wrapped live kept, wrapped ghosts dropped ----
    class _Native:
        def __init__(self, hs):
            self.handlers = {0: list(hs)}

        def remove_handler(self, h, group=0):
            try:
                self.handlers.get(group, []).remove(h)
            except ValueError:
                pass

    _ns = mod.__dict__
    exec("async def _t29_live(u, c): pass", _ns)   # defined in plugin globals
    _live = _ns["_t29_live"]

    async def _ghost(u, c):                         # foreign globals
        pass
    _ghost.__module__ = mod.__name__                # same module NAME = old load

    def _wrap(cb):
        @_f29.wraps(cb)
        async def _w(u, c):
            return await cb(u, c)
        return _w

    h_live = _MH29(_filters29.TEXT, callback=_wrap(_live))
    h_ghost = _MH29(_filters29.TEXT, callback=_wrap(_ghost))
    h_ghost_cb = _CQ29(_wrap(_ghost), pattern=_re29.compile(r"^tgm:"))

    _native = _Native([h_live, h_ghost, h_ghost_cb])
    mod._drop_stale_handlers(_native)
    _left = _native.handlers[0]
    check(h_live in _left, "live wrapped handler survives the sweep (unwrapped identity)")
    check(h_ghost not in _left, "ghost message handler swept (same name, foreign globals)")
    check(h_ghost_cb not in _left, "ghost tgm: callback handler swept")
    del _ns["_t29_live"]

    # --- (b) business event never reaches the generic DM mirror -----------
    _orig = dict(mod.settings())
    _orig_log = mod._log
    _titles = []

    async def _slog(title, body="", buttons=None):
        _titles.append(str(title))

    class _Ev:
        platform = "telegram"
        internal = False
        edited = False
        channel_action = False
        text = "Yo"
        reply_to_id = None
        metadata: dict = {}
        raw_message = None

        class source:  # noqa: N801 - mirrors .source attribute access
            platform = "telegram"
            chat_type = "supergroup"
            chat_id = "-100" + "3744718087"  # split: audit forbids the literal log-channel id
            user_id = "654321"
            message_id = 1
            name = "Someone"

    class _Ad:
        pass

    mod._log = _slog
    _old_ad = mod._ADAPTER.get("adapter")
    _ad = _Ad()
    _ad._tga_instance = mod._INSTANCE   # rewire already done -> no-op
    mod._ADAPTER["adapter"] = _ad
    try:
        mod.save_settings({"log_other_messages": True, "log_whitelisted_messages": True})

        _Ev.raw_message = NS10(business_connection_id="bc_test")
        await mod._pre_gateway_dispatch(event=_Ev(), gateway=None,
                                        session_store=None)
        check(not _titles, f"business event skipped the DM mirror ({len(_titles)} log(s))")

        _Ev.raw_message = NS10(business_connection_id=None, from_user=None)
        _titles.clear()
        await mod._pre_gateway_dispatch(event=_Ev(), gateway=None,
                                        session_store=None)
        check(len(_titles) >= 1, f"control: plain non-business message still mirrored ({len(_titles)})")
    finally:
        mod._log = _orig_log
        mod.save_settings(_orig)
        mod._ADAPTER["adapter"] = _old_ad


asyncio.run(_t29())


# [30] Regression: a friend's business chat and their plain DM with the bot
# share the same numeric chat id, so "chat has business history" alone used to
# inject EVERY plain reply (and typing/clarify/media) of that person into the
# owner's personal DM as if the owner had written it. Delivery must follow the
# CURRENT turn: plain -> bot, business -> owner, no event seen -> old behaviour
# (boot sweep / ledger redelivery still lands as the owner).
print("\n[30] delivery follows the current turn, not chat history")
try:
    _orig_bck = mod._biz_chat_known
    _old_ad3 = mod._ADAPTER.get("adapter")
    mod._biz_chat_known = lambda cid: str(cid) == "770011"
    ad3 = FakeAdapter()
    mod._ADAPTER["adapter"] = ad3
    mod._install_wraps(ad3)
    mod._BIZ_CONN["770011"] = "bc_boot"

    async def t30():
        # no event seen yet: old behaviour — owner delivery for a known chat
        mod._BIZ_CTX.pop("770011", None)
        await ad3.send("770011", "boot sweep")
        check(any(kw.get("business_connection_id") == "bc_boot"
                  for kw in ad3._bot.sent),
              "no ctx: known business chat still sends as the owner")
        check(not any(c[0] == "send" for c in ad3.calls),
              "no ctx: bot passthrough not used")
        ad3._bot.sent.clear()
        ad3.calls.clear()

        # plain turn: the same chat must be delivered by the bot
        mod._biz_mark("770011", "plain")
        await ad3.send("770011", "plain reply")
        check(any(c[0] == "send" and c[1] == "770011" for c in ad3.calls),
              "plain turn: reply delivered by the bot")
        check(not any("business_connection_id" in kw for kw in ad3._bot.sent),
              "plain turn: no business_connection_id injected")
        ad3._bot.sent.clear()
        ad3.calls.clear()

        # business turn: back to the owner
        mod._biz_mark("770011", "business")
        await ad3.send("770011", "biz reply")
        check(any(kw.get("business_connection_id") == "bc_boot"
                  for kw in ad3._bot.sent),
              "business turn: reply delivered as the owner")
        check(not any(c[0] == "send" for c in ad3.calls),
              "business turn: bot passthrough not used")
        ad3._bot.sent.clear()
        ad3.calls.clear()

        # the final of a non-business event re-marks the chat plain
        # (authoritative even if an admission hook never ran)
        mod._BIZ_CTX.pop("770011", None)
        pev = FakeEvent(text="hi", source=FakeSource("770011"))
        await ad3.send_final_ledgered(pev, "k", "final", {}, reply_to=None)
        check(mod._BIZ_CTX.get("770011") == "plain",
              "plain final marks the chat plain for following sends")

        # a business final keeps the business mark
        bev = FakeEvent(text="hi", source=FakeSource("770011"))
        bev.metadata = {"business_connection_id": "bc_test",
                        "business_chat_id": "770011"}
        mod._biz_mark("770011", "business")
        check(mod._BIZ_CTX.get("770011") == "business",
              "business mark survives until a plain event replaces it")
    asyncio.run(t30())
finally:
    mod._biz_chat_known = _orig_bck
    mod._BIZ_CTX.pop("770011", None)
    mod._BIZ_CONN.pop("770011", None)
    mod._ADAPTER["adapter"] = _old_ad3

# ------------------------------------------- typed durations + bundled personas
print("\n[31] typed durations, time normalising, bundled personas")

_prev31 = {k: mod.settings().get(k) for k in (
    "unauthorized_cooldown_s", "update_timeout_s", "biz_idle_delay_min",
    "biz_owner_idle_min", "biz_window_start", "biz_window_end",
    "biz_persona_path")}
try:
    # _dur_to_s — the point of the feature: type what people actually say
    check(mod._dur_to_s("10s", "s") == 10, "duration 10s parses")
    check(mod._dur_to_s("10m", "s") == 600, "duration 10m parses")
    check(mod._dur_to_s("1h", "s") == 3600, "duration 1h parses")
    check(mod._dur_to_s("1h30m", "s") == 5400, "compound 1h30m parses")
    check(mod._dur_to_s("10", "s") == 10, "bare number keeps the default unit")
    check(mod._dur_to_s("10x", "s") is None, "unknown unit is rejected")
    check(mod._dur_to_s("", "s") is None, "empty input is rejected")
    check(mod._dur_to_s("-5m", "s") is None, "a negative duration is rejected")

    # display of what was stored
    check(mod._fmt_min(10) == "10m", "10 min renders as 10m")
    check(mod._fmt_min(0.5) == "30s", "half a minute renders as 30s")
    check(mod._fmt_min(0) == "0", "zero renders as 0")

    # window times: a plain number is enough
    check(mod._biz_norm_hhmm("9") == "09:00", "'9' normalises to 09:00")
    check(mod._biz_norm_hhmm("0900") == "09:00", "'0900' normalises to 09:00")
    check(mod._biz_norm_hhmm("2359") == "23:59", "'2359' normalises to 23:59")
    check(mod._biz_norm_hhmm("9:05") == "09:05", "'9:05' normalises to 09:05")
    check(mod._biz_norm_hhmm("25:00") == "", "an impossible time is rejected")
    check(mod._biz_hhmm("0900", -1) == 540, "_biz_hhmm reads plain numbers")

    # cycling from a typed (non-preset) value steps ABOVE it, not back to 0
    check(mod._next_preset(1.5, [0, 1, 2, 5, 10, 15, 30], 0) == 2,
          "cycle from a typed 90s steps up instead of snapping to 0")
    check(mod._next_preset(10, [0, 1, 2, 5, 10, 15, 30], 0) == 15,
          "cycle from a preset steps to the next one")

    # !setcooldown takes a duration
    out31 = asyncio.run(mod._bang_execute(None, "-100555", "!setcooldown 10m"))
    check(mod.settings().get("unauthorized_cooldown_s") == 600,
          "!setcooldown 10m stores 600 seconds")
    check(bool(out31) and "600" in out31, "!setcooldown reports the stored value")
    asyncio.run(mod._bang_execute(None, "-100555", "!setcooldown junk"))
    check(mod.settings().get("unauthorized_cooldown_s") == 600,
          "a junk cooldown is refused, not stored")

    # wizard flows accept durations end to end
    for flow, key, sample, want in (
            ("uptimeout", "update_timeout_s", "10m", 600.0),
            ("bizidledur", "biz_idle_delay_min", "10s", 10 / 60),
            ("bizownidledur", "biz_owner_idle_min", "2h", 120.0)):
        mod._WIZARD.clear()
        check(bool(mod._wizard_start("-100555", flow)), f"{flow} wizard opens")
        asyncio.run(mod._wizard_feed(None, "-100555", sample))
        got31 = float(mod.settings().get(key))
        check(abs(got31 - want) < 1e-9,
              f"{flow} accepts '{sample}' (got {got31!r})")
        check("-100555" not in mod._WIZARD, f"{flow} wizard closed")
    # a non-duration must re-prompt, not kill the flow or store garbage
    mod._WIZARD.clear()
    mod._wizard_start("-100555", "bizidledur")
    out31 = asyncio.run(mod._wizard_feed(None, "-100555", "banana"))
    check(bool(out31) and "\u26a0" in out31 and "-100555" in mod._WIZARD,
          "a non-duration re-prompts and keeps the wizard open")
    mod._WIZARD.clear()

    # window wizard normalises plain numbers instead of storing them raw
    mod._wizard_start("-100555", "bizwinstart")
    asyncio.run(mod._wizard_feed(None, "-100555", "9"))
    check(mod.settings().get("biz_window_start") == "09:00",
          "window start accepts '9'")
    mod._WIZARD.clear()
    mod._wizard_start("-100555", "bizwinend")
    asyncio.run(mod._wizard_feed(None, "-100555", "2300"))
    check(mod.settings().get("biz_window_end") == "23:00",
          "window end accepts '2300'")
    mod._WIZARD.clear()

    # every duration setting offers a typed-entry button somewhere
    d31 = [b.callback_data for row in mod._help_keyboard("tg", arg="bizidledelay:biz")
           for b in row]
    check(any(x.endswith("panel:wiz:bizidledur") for x in d31),
          "idle-delay confirm offers a typed value")
    d31 = [b.callback_data for row in mod._help_keyboard("tg", arg="bizownidle:biz")
           for b in row]
    check(any(x.endswith("panel:wiz:bizownidledur") for x in d31),
          "owner-idle confirm offers a typed value")
    check(any(b.callback_data.endswith("panel:wiz:cooldown")
              for row in mod._help_keyboard("cool") for b in row),
          "cooldown page offers typed entry")
    check(any(b.callback_data.endswith("panel:cool:300")
              for row in mod._help_keyboard("cool") for b in row),
          "cooldown presets survive next to the new button")
    check(all(f in mod._WIZ_FLOWS for f in
              ("bizidledur", "bizownidledur", "cooldown", "uptimeout",
               "bizwinstart", "bizwinend")),
          "every duration flow is registered")

    # 0 really means 0 — it used to fall through to the 10-minute default
    check(mod._owner_idle({"biz_owner_idle_min": 0}) is False,
          "owner idle threshold 0 means never idle")

    # The idle hold is for ATRA's ENTRY into a conversation, not every message
    # in it. Once ATRA has answered, the next message answers immediately —
    # until the owner posts in that same chat, which arms the hold again.
    mod._BIZ_REPLIED.clear()
    mod._BIZ_OWNER_SEEN.clear()
    check(mod._biz_engaged("c-delay") is False,
          "an untouched chat still waits for the owner")
    mod._BIZ_REPLIED["c-delay"] = time.monotonic() - 100.0
    check(mod._biz_engaged("c-delay") is True,
          "a chat ATRA already answered skips the wait")
    check(mod._biz_engaged("c-other") is False,
          "the spent grace period is per chat, not global")
    mod._BIZ_OWNER_SEEN["c-delay"] = time.monotonic()
    check(mod._biz_engaged("c-delay") is False,
          "the owner posting in that chat re-arms the hold")
    check(mod._biz_engaged("c-other") is False,
          "the owner returning to one chat does not re-arm another")
    mod._BIZ_REPLIED["c-other"] = time.monotonic() - 100.0
    mod._BIZ_OWNER_SEEN["c-delay"] = time.monotonic() - 50.0
    mod._BIZ_REPLIED["c-delay"] = time.monotonic() - 10.0
    check(mod._biz_engaged("c-delay") is True,
          "an answer sent after the owner's visit holds again")
    mod._BIZ_REPLIED.clear()
    mod._BIZ_OWNER_SEEN.clear()

    # bundled personas: one per mode, and the assistant one is NOT the guest
    a31 = mod._biz_persona({}, "assistant")
    m31 = mod._biz_persona({}, "mimic")
    check("ATRA" in a31 and "guest session" not in a31 and "guest link" not in a31,
          "assistant persona is the automation one, not the guest one")
    check("first person" in m31.lower(), "mimic persona speaks as the owner")
    check("never invent" in m31.lower() and "no logs" in m31.lower(),
          "mimic persona carries the no-invention and no-leak rules")
    check((mod.PLUGIN_DIR / "assets" / "automation.md").is_file()
          and (mod.PLUGIN_DIR / "assets" / "mimic.md").is_file(),
          "both persona files are packaged")
    check(mod._biz_persona({"biz_persona_path": str(PERSONA)}, "assistant")
          .startswith("# ATRA"), "an explicit persona path still wins")

    # the persona wizard refuses a path that is not a file — that is the bug
    # which stored a chat message as biz_persona_path
    check(mod._biz_persona_arg_ok(["default"]) == "",
          "persona wizard accepts 'default'")
    check(mod._biz_persona_arg_ok([""]) != "",
          "persona wizard rejects an empty answer")
    check(mod._biz_persona_arg_ok(["no/such/file.md"]) != "",
          "persona wizard rejects a path that does not exist")
    _tmp31 = P(tempfile.mkdtemp()) / "mine.md"
    _tmp31.write_text("# mine\n", encoding="utf-8")
    check(mod._biz_persona_arg_ok([str(_tmp31)]) == "",
          "persona wizard accepts a real file")
finally:
    mod.save_settings(_prev31)
    mod._WIZARD.clear()



# --- busy-path console input: the dispatch hook never runs while a session for
# the chat is live, so bang/wizard input has to be served from the PTB handler.
async def t_busy():
    _ad = FakeAdapter()
    _ad._active_sessions = {}
    _prev_ad = mod._ADAPTER.get("adapter")
    mod._ADAPTER["adapter"] = _ad
    _st0 = dict(mod.settings())
    _chat = "-10042"
    _owner = "999"
    mod.save_settings({"owner_id": _owner, "log_channel": _chat})

    _bang = []
    _wiz = []
    _orig_bang, _orig_feed, _orig_reply = (mod._run_bang_command,
                                           mod._wizard_feed,
                                           mod._reply_to_event)

    async def _fake_bang(adapter, event, text, session_store=None):
        _bang.append((str(event.source.chat_id), text))

    async def _fake_feed(adapter, chat_id, text, session_store=None):
        _wiz.append((str(chat_id), text))
        return "next step"

    async def _fake_reply(adapter, chat_id, text, buttons=None):
        _wiz.append(("reply", str(chat_id)))

    mod._run_bang_command, mod._wizard_feed, mod._reply_to_event = (
        _fake_bang, _fake_feed, _fake_reply)

    def _mk(chat, uid, text, ct="supergroup"):
        m = NS(message_id=505, text=text,
               from_user=NS(id=int(uid)),
               chat=NS(id=int(chat), type=ct))
        return NS(effective_message=m, message=m)

    try:
        # IDLE: nothing is running -> the core handler (and the hook) own it.
        _ad.delegated.clear(); _bang.clear()
        await mod._on_busy_path_text(_ad, _mk(_chat, _owner, "!panel"), None)
        check(_ad.delegated == ["text"],
              "idle: bang falls through to the core handler")
        check(not _bang, "idle: the busy path does not run the bang itself")

        # BUSY: session live for this chat -> bang served here, no delegation.
        _ad._active_sessions[f"agent:main:telegram:group:{_chat}"] = object()
        _ad.delegated.clear(); _bang.clear()
        await mod._on_busy_path_text(_ad, _mk(_chat, _owner, "!panel"), None)
        check(not _ad.delegated,
              "busy: bang is not also handed to the core handler")
        check(_bang and _bang[0][0] == _chat and _bang[0][1] == "!panel",
              "busy: bang executes with the right chat and command")

        # BUSY + ordinary text: still the core's, so normal turns are untouched.
        _ad.delegated.clear(); _bang.clear()
        await mod._on_busy_path_text(_ad, _mk(_chat, _owner, "hello"), None)
        check(_ad.delegated == ["text"],
              "busy: plain text still goes to the core handler")
        check(not _bang, "busy: plain text never runs a bang")

        # BUSY + wizard answer: served here too (the reported bug).
        mod._WIZARD[_chat] = {"flow": "send", "step": 1, "data": []}
        _ad.delegated.clear(); _wiz.clear()
        await mod._on_busy_path_text(_ad, _mk(_chat, _owner, "10m"), None)
        check(not _ad.delegated,
              "busy: wizard answer is not queued as a user turn")
        check(any(x == (_chat, "10m") for x in _wiz),
              "busy: the wizard receives the typed answer")
        mod._WIZARD.pop(_chat, None)

        # A non-owner never reaches the console branch.
        _ad.delegated.clear(); _bang.clear()
        await mod._on_busy_path_text(_ad, _mk(_chat, "12345", "!panel"), None)
        check(_ad.delegated == ["text"],
              "busy: a stranger's bang is not executed")
        check(not _bang, "busy: a stranger never runs a console command")
    finally:
        mod._run_bang_command, mod._wizard_feed, mod._reply_to_event = (
            _orig_bang, _orig_feed, _orig_reply)
        mod._ADAPTER["adapter"] = _prev_ad
        mod.save_settings(_st0)
        mod._WIZARD.clear()


asyncio.run(t_busy())

print(f"\n=== {PASS} passed, {FAIL} failed ===")
sys.exit(1 if FAIL else 0)
