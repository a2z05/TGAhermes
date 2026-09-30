# telegram-guest-mode

All-in-one Telegram guest mode for [Hermes](https://github.com/NousResearch/hermes-agent):
Bot API 10 guest replies, a log-channel activity console, an owner-gated admin tool,
reaction feedback and media delivery to guests — as a **plugin**, no core edits.

Works on Hermes **0.21.5+** with `python-telegram-bot` **≥ 22.8** (the version that
introduced `Update.guest_message` / `filters.UpdateType.GUEST_MESSAGE`).

## What it does

### 🎭 Guest mode
Anyone can summon your bot from a chat it is **not** a member of (a group it isn't in, a
stranger's chat). The plugin:

- answers **only** owner plain-mentions and replies to your bot's own messages —
  everyone else's plain mention gets the editable canned reply (default
  *"I only serve to my owner"*) with a per-user cooldown;
- injects a persona (`assets/guest_persona.md`) plus a `[Sender identity]` tag, so the
  assistant knows who is talking and how they reached the bot;
- keeps guest conversations in **their own session** (`guest_<chat_id>`) — guest text
  never touches the owner's DM transcript;
- delivers replies **exclusively** through `answerGuestQuery` (never `send_message`):
  final answers, clarify prompts, control prompts — while status/typing/footer noise is
  suppressed for guest chats;
- URL **media** (photos, documents, voice) is delivered to guests as inline results,
  with a plain-text fallback if the API rejects the result type;
- guest-turn **errors** go to your log channel (or the owner DM when no channel is set)
  and the guest receives the editable pre-made error text (English/Persian).

### 📋 Log channel console
Point the plugin at any group (recommended) or channel where the bot can post:

```
!setlog -100xxxxxxxxxx
```

Every interaction posts an HTML entry with inline buttons — **👤 Profile**, **ℹ️ Info**
(recorded history), **✂ Delete** (confirm), **🚫 Ban** (confirm, where the bot is admin):

- guest mentions (answered *and* unauthorized)
- stranger DMs and `/start`s
- `telegram_admin` tool invocations and `!send` DMs
- guest-mode errors
- group @mentions of the bot (optional: owner DM mirror)

### 🖥 Bang commands (log channel or your DM with the bot, owner only)

| Command | Effect |
|---|---|
| `!panel` | full glass console: live status + version, 7 sections, 6 in-place toggles (reactions/guest reacts/guest media/mirror/mentions/tool), output views (users/settings/whitelist/guest texts), cooldown presets, wipe-this-chat |\n| `!help` | same buttons around the full command list |
| `!users` | who started / used the bot (name, count, last seen, samples) |
| `!send <user_id> <text>` | the bot DMs someone (result logged) |
| `!settings` | show every editable setting |
| `!setlog <id\|@name\|off>` | set / clear the log channel |
| `!setowner <id>` | change the owner id (hot, no restart) |
| `!setunauthorized <text>` | reply text for unauthorized users |
| `!seterror <text>` / `!seterrorfa <text>` | guest error texts (English / Persian) |
| `!setreact on\|off` | reaction feedback (👀 / ✅ / ❌) |
| `!setmedia on\|off` | media delivery to guests |
| `!setcooldown <seconds>` | unauthorized-reply cooldown |
| `!whitelist list` | show who may talk to the real bot (`telegram.extra.allow_from`) |
| `!whitelist add <user_id>` | whitelist a friend — their DMs go to the real bot, not the canned reply |
| `!whitelist remove <user_id>` | drop someone from the whitelist (owner is protected) |
| `!wipe <chat_id>` | delete that chat's session and start a fresh one **there** (DM / group / guest; also from the log channel — a bare `!wipe` is refused so your own session can never be wiped implicitly) |

Every chat (your DM, each group, each guest) is a separate session; log entries carry a
**🧹 Wipe** button (with confirm) as a point-and-click way to reset that chat.

All settings are stored in `settings.json` next to the plugin (hot-read on every use —
edits apply immediately, no restart).

### 🛡 `telegram_admin` tool

The agent gets a `telegram_admin` tool **restricted to the owner's own session**
(guest or group sessions are refused by a database lookup, fail-closed):

`delete_message` · `ban_user` · `unban_user` · `mute_user` · `unmute_user` ·
`get_member` · `chat_info` · `react` · `send_dm` · `pin_message` · `unpin_message` ·
`bang` (run any `!console` command — the agent can do everything you can type)

Just ask in plain language — *"delete the message he just sent in X"*, *"ban 12345 in
group Y"*, *"react 👍 to that"* — and it runs, provided the bot is admin in the target
chat. Every action is logged to the log channel. Disable with `!setreact`-style
settings: `"tool_enabled": false`.

### ✨ Reactions

When `auto_react` is on: 👀 when your message lands, ✅ after the reply is delivered,
❌ on errors (owner DM and groups; guests only if `react_guests` is on). The agent can
also set reactions on demand via the tool.

## 📍 Channel origin (where a turn came from)

Every Telegram turn now carries a short context block, so the model never has to
guess where it is. It is injected through the documented `channel_prompt` hook
path — no core edits.

```
[Channel origin] this turn came from Telegram.
chat_kind=direct message (private 1:1 chat with you)
chat_id='100000001'
chat_name='@yourname'
sender_user_id='100000001'
bot_username='@your_bot'
message_id='4939'
sender_role=owner (this is your own 1:1 chat with the plugin owner)
Channel context only — not a request; do not echo these values back verbatim.
```

*(values above are placeholders — the block carries whatever the real chat
reports)*

* `chat_kind` names the shape: **direct message**, **group chat**, **supergroup**,
  **forum**, **channel**, or **guest chat** (with the "summoned the bot through its
  guest link" clarification).
* `chat_name` is resolved through `get_chat` — a group title / channel title /
  `@username` when available, otherwise the raw id.
* `sender_role` says whether the sender is the owner.
* The block is explicitly labelled **context, not a request**, so a message that
  quotes these values cannot turn them into instructions.

Guest-mode turns keep their dedicated fields (`guest_name`, `guest_user_id`,
`sender`, `trigger`) and also state that the chat came from the guest link.

## Install

```bash
# 1. copy the plugin into your Hermes plugins dir
cp -r telegram-guest-mode ~/.hermes/plugins/          # (or $HERMES_HOME/plugins/)

# 2. enable it — hot-reloads the running gateway
hermes plugins enable telegram-guest-mode
```

Requirements: a bot token with Bot API 10+ (guest mode), PTB ≥ 22.8 (bundled with
recent Hermes). Set your owner id either in `settings.json`
(`"owner_id": "100000001"`) or via `telegram.extra.allow_from` in `config.yaml`.

### Persona

Copy `assets/guest_persona.example.md` to `<hermes_home>/assets/guest_persona.md` and
edit it. Read fresh on every guest message — hot-editable. Missing file ⇒ built-in
neutral fallback.

### Log channel

1. Create a group, add the bot (as admin for ban/delete buttons to work).
2. From your DM with the bot: `!setlog <chat_id>` (grab the id from e.g.
   `@getidsbot`, it looks like `-1001000000010`).

No channel configured? Everything still works; guest errors fall back to your DM.

## Settings reference

See [`settings.example.json`](settings.example.json). Every key:

| Key | Default | Meaning |
|---|---|---|
| `owner_id` | `null` | owner override (else `allow_from` / `TELEGRAM_ALLOWED_USERS`) |
| `log_channel` | `null` | group/channel for activity logs and `!` console |
| `unauthorized_reply` | `I only serve to my owner` | reply to unauthorized users |
| `unauthorized_cooldown_s` | `3600` | min gap between canned replies per user |
| `guest_error_reply_en/fa` | see example | pre-made guest error texts |
| `auto_react` | `true` | 👀/✅/❌ reaction feedback |
| `react_guests` | `false` | also try reactions in guest chats |
| `media_to_guests` | `true` | deliver URL media to guests |
| `log_owner_messages` | `false` | mirror your DMs to the log channel |
| `log_group_mentions` | `true` | log @bot mentions from groups |
| `tool_enabled` | `true` | `telegram_admin` tool |
| `persona_path` | `null` | persona file (default `<hermes_home>/assets/guest_persona.md`) |

## How it hooks in

- `register_platform_handler("telegram", factory)` — PTB handlers (`GUEST_MESSAGE`
  first, then private text, then a `^tgm:` callback handler) registered before the core
  handlers, so first-match dispatch wins without touching core code.
- Instance wraps on the adapter's outbound path (`send`, `send_final_ledgered`,
  `send_clarify`, `_send_prompt`, `_notify_turn_error`, `send_typing`, media senders) —
  guest chats are answered via the stored `guest_query_id` and everything else passes
  through untouched.
- `pre_gateway_dispatch` hook — bang console (returns `skip` so the agent never sees
  `!` commands), reaction feedback, group-mention and optional owner-DM logging.
- `ctx.register_tool("telegram_admin", …)` — plugin toolsets are enabled by default in
  Hermes; visibility can be toggled with `tool_enabled`.

## Tests

```bash
python tests/test_plugin.py    # needs the Hermes repo on sys.path; exit 0 = pass
```

## Known limitations

- One guest message = one `guest_query_id`. A clarify turn answers it twice (prompt,
  then final); if your Bot API build rejects the second answer, the final is lost and
  the failure is logged loudly.
- Media to guests needs a **public URL** (local files fall back to a text note).
- Guest replies are text + inline media — no buttons/keyboards.
- `!` console requires a **group** for the sender check (channel posts carry no author).

## Running the tests

```bash
HERMES_SRC=/path/to/hermes-agent HERMES_HOME=/path/to/hermes-home \
  python tests/test_plugin.py
```

Paths are auto-detected when they live in the usual places. Checks that need a
live Hermes install (real sessions DB, installed `config.yaml`) skip themselves
on a machine that has none — everything else runs offline with fakes.

## License

MIT
