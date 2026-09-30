# AGENT.md — how to install this plugin

You are an agent. Someone just gave you a link to this repository. Your job:
install the plugin, configure it, verify it works, and report back plainly.

Do not ask the user to read instructions and do it themselves. You have shell
access. Read this file, follow it, then tell them what you did in their
language (Farsi for a Farsi speaker, English for an English speaker).

---

## What this is

`telegram-guest-mode` is a **Hermes Agent** plugin that turns a Telegram bot
into a guest-mode assistant. Strangers message the bot through its guest link
and get answered. A private owner chat stays private. Everything can be
inspected from a log channel with inline buttons.

It needs:

- Hermes Agent **0.21.5 or newer** (it registers a `pre_gateway_dispatch` hook)
- A Telegram bot token (from @BotFather)
- Python 3.11+ with `python-telegram-bot` (Hermes already ships it)

It does **not** need `pyrogram`, `telethon`, or a userbot session. If the
target is not a Telegram bot, stop and say so — don't improvise.

---

## Step 1 — check the environment

```bash
hermes --version                 # must be >= 0.21.5
python3 -c "import telegram; print(telegram.__version__)"
```

If Hermes is missing or older, **stop and report that**. Don't try to upgrade
someone else's agent install behind their back.

## Step 2 — install the plugin

```bash
git clone https://github.com/a2z05/telegram-guest-mode.git \
  "$HOME/hermes/plugins/telegram-guest-mode"
```

If the plugins directory doesn't exist, create it first:

```bash
mkdir -p "$HOME/hermes/plugins"
```

If the repository is already there, `git -C "$HOME/hermes/plugins/telegram-guest-mode" pull`.

## Step 3 — create the bot

The user needs a Telegram bot token:

1. Open Telegram, message **@BotFather**
2. `/newbot` → pick a name → pick a username ending in `bot`
3. Copy the token (looks like `100000001:AA...`)

**Two things that are easy to miss and that this plugin depends on:**

- **Inline mode must be ON** for the bot. In @BotFather: `/setinline` → choose
  placeholder text → send a placeholder. Without it, guest replies fail.
- The bot does not need to be an admin anywhere. Guest mode is a Bot API 10
  feature; it works on a plain bot token.

Never ask the user to paste their bot token into this chat or any log. Have
them write it into the settings file themselves (step 4), or set it as an
environment variable.

## Step 4 — settings

The plugin reads `settings.json` next to `__init__.py`. Start from the example:

```bash
cd "$HOME/hermes/plugins/telegram-guest-mode"
cp settings.example.json settings.json
```

Minimum viable config:

```json
{
  "owner_id": "100000001",
  "log_channel": "-1001000000010",
  "unauthorized_reply": "I only talk to my person."
}
```

- `owner_id` — the user's Telegram numeric id. The bot answers them privately and
  treats their DM as the command console.
- `log_channel` — a group/channel id the user controls, where the bot posts an
  audit line for every guest interaction, with buttons (profile / ban / delete).
  Negative ids for supergroups. The bot must be able to post there.
- `unauthorized_reply` — what a stranger gets when they mention the bot
  casually instead of replying to it.

Optional keys (all have sane defaults): `unauthorized_cooldown_s`,
`guest_error_reply_en`, `guest_error_reply_fa`, `auto_react`, `react_guests`,
`react_emoji_receive`, `react_emoji_done`, `react_emoji_error`,
`media_to_guests`, `log_owner_messages`, `log_whitelisted_messages`,
`log_other_messages`, `log_group_mentions`, `tool_enabled`, `persona_path`.

## Step 5 — persona (optional but recommended)

`persona_path` points at a markdown file that becomes the system prompt for
guest turns. A ready one ships in `assets/guest_persona.example.md`. Copy it
and let the user edit it:

```bash
cp assets/guest_persona.example.md ~/guest_persona.md
```

Without a persona file the plugin uses a built-in fallback so nothing breaks.

## Step 6 — enable and start

```bash
hermes plugins enable telegram-guest-mode     # if that subcommand exists
hermes plugins list                           # confirm it is loaded
hermes gateway restart
```

If the subcommand names differ in this Hermes version, run `hermes plugins --help`
and use the equivalent. Report the command you actually ran.

## Step 7 — verify (do not skip this)

```bash
curl -s http://127.0.0.1:8642/health
```

Expect `{"status": "ok", ...}`. Then check the log for the plugin loading
without exceptions.

Ask the user to do one real test and confirm it:

1. From **another account or a guest-friendly context**, open the bot's guest
   link and send a message.
2. Confirm a reply comes back.
3. Confirm the log channel received the audit line.
4. Confirm a casual mention (no reply-to) from a stranger gets
   `unauthorized_reply`.

If inline replies come back empty, the cause is almost always inline mode being
off in @BotFather. Check that before debugging anything else.

## Step 8 — report

Tell the user, in their language:

- the version you installed
- where the plugin lives
- what the bot username is and that **inline mode is required**
- that guest conversations are isolated per guest chat, and that replies are
  sent as inline results (not as normal messages)
- how to change the persona file
- how to reach the bang console (`!help`, `!panel`, `!users`, `!settings`) in
  the log channel or the owner's DM
- anything that failed, plainly — no pretending a step worked

---

## Operational notes

- **State lives in `state.json`** (user registry, cooldowns). It is written
  atomically. Don't delete it while the gateway runs.
- **Owner identity** comes from `settings.owner_id` first, then the Hermes
  Telegram `allow_from` config. An explicit `owner_id` always wins.
- **A note on Telegram's rules**: guest mode is a Bot API feature, so this is
  bot behaviour, not a userbot. Still worth telling the user that automating
  accounts can violate Telegram's terms — bots don't, users do.
- **Don't paste secrets into the conversation.** Tokens, ids and persona
  content belong in files, not chat.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Guest replies empty | inline mode off in @BotFather | `/setinline` on the bot |
| No log lines | bot can't post in `log_channel` | add it as admin / check the id |
| Bot answers strangers as owner | `owner_id` wrong | numeric id, not username |
| Plugin not loaded after restart | bad manifest | `hermes plugins list`, read the error |
| Guests get the generic fallback | `persona_path` wrong | path must be absolute |
