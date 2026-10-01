# TGAhermes

![Hermes 0.21.5+](https://img.shields.io/badge/Hermes-0.21.5%2B-3b82f6)
![Bot API 10+](https://img.shields.io/badge/Bot%20API-10%2B-2AABEE)
![License](https://img.shields.io/badge/license-MIT-22c55e)

**A Telegram gate, console, and admin tool for [Hermes](https://github.com/NousResearch/hermes-agent).**

Your bot gets one front door: strangers reach it through a guest mode you
control, your own conversations stay private, and every interaction lands in
a log channel you pick — with buttons to act on it. Install it as a plugin;
no core edits, no forked Hermes.

Requires Hermes **0.21.5+** and a **Bot API 10+** bot token.

## What you get

- **🚪 Guest gate** — anyone who finds the guest link can talk to your bot.
  Strangers get an editable canned reply with a per-user cooldown; your own
  id passes straight through.
- **📋 Log channel** — guest mentions, stranger DMs, admin actions, and
  errors all land in one channel, each entry carrying **Profile · Info ·
  Delete · Ban** buttons.
- **🖥 Admin panel** — `!panel` opens a console of buttons (status, log,
  access, sessions, guests, system) with wizards for the common jobs, so
  day-to-day changes rarely require typing a command.
- **🛡 Admin tool** — ask your agent in plain language ("delete that
  message", "ban @user") and it runs — restricted to your own session by a
  fail-closed database check.
- **✨ Feedback and media** — 👀 ✅ ❌ reactions on the message lifecycle,
  and URL media (photos, documents, voice) delivered straight into guest
  chats.
- **⬆️ Self-update** — check and install updates from the panel; set
  `update_enabled` to `false` to lock a version.

**Sessions are isolated:** your DM, each group, and each guest chat are
separate sessions — guest traffic never mixes into your own transcripts.

## Install

### Option 1 — let your agent do it (recommended)

Send your Hermes agent this repo link and say:

```
install this plugin for me : https://github.com/a2z05/TGAhermes
```

It follows [AGENT.md](AGENT.md) — a step-by-step guide written for agents —
runs the install, verifies the plugin loaded, and tells you what to do next.
You never touch the terminal.

### Option 2 — one command

```bash
hermes plugins install https://github.com/a2z05/TGAhermes --enable
```

Confirm it landed:

```bash
hermes plugins list --plain | grep -i TGAhermes
```

You want `enabled` in front of `TGAhermes`. A running gateway usually picks
the plugin up on the spot; if it doesn't:

```bash
hermes gateway restart
```

<details>
<summary>Manual install (if the CLI isn't an option)</summary>

```bash
git clone https://github.com/a2z05/TGAhermes.git "$HERMES_HOME/plugins/TGAhermes"
hermes plugins enable TGAhermes
hermes gateway restart
```

`$HERMES_HOME` is your Hermes home directory (`~/.hermes` for most people).

</details>

## Set it up — three messages

Message your bot directly and send:

1. `!setowner <your telegram id>` — sets you as the owner. A numeric id,
   not a username. Hot, no restart.
2. `!setlog <chat id>` — where activity gets logged. Or go into the group
   or channel you want and send `!setlog here`. Make the bot an admin there
   if you want the delete/ban buttons to work.
3. `!panel` — opens the admin panel.

That's the whole setup. The panel's **Actions** view has wizards for the
usual jobs — set the log channel, whitelist a friend, change the reply
strangers get — so most of the time you never have to type a command at all.

## The commands

| Command | What it does |
|---|---|
| `!panel` / `!help` | the admin panel and full command list |
| `!settings` | show every setting |
| `!users` | who has used the bot |
| `!setowner <id>` | change the owner |
| `!setlog <id\|@name\|here\|off>` | set or clear the log channel |
| `!whitelist list\|add\|remove <id>` | let friends past the gate |
| `!wipe <chat_id>` | fresh start for that chat |
| `!send <user_id> <text>` | have the bot DM someone |
| `!setcooldown <s>` | gap between canned replies |
| `!setunauthorized <text>` | reply strangers get |
| `!seterror <text>` / `!seterrorfa <text>` | guest error texts (English / Persian) |
| `!setreact on\|off` / `!setmedia on\|off` | reaction feedback, media to guests |
| `!guestlock on\|off` | unlock this guest chat for yourself |

## Settings

Everything lives in `$HERMES_HOME/plugins/TGAhermes/settings.json`. It's read
on every use, so edits apply immediately — no restart. Start from
[`settings.example.json`](settings.example.json) if you'd rather edit a file
than send commands.

## Updating

Open `!panel` → **🔧 System** → **Check for update**, then **Install
update**. Your agent can do the same through its update tool. Lock the
version by setting `update_enabled` to `false` in `settings.json`.

## When something's wrong

- **No log entries** — the bot can't post in the log channel. Add it as
  admin, then `!setlog here` again.
- **Strangers treated as you** — `owner_id` is wrong; it's a numeric id,
  not a username.
- **Plugin not there after a restart** — run `hermes plugins list` and read
  the status column.

[AGENT.md](AGENT.md) has a longer troubleshooting table — that's the file
your agent follows.

## Development

Clone the repo, then run the suite:

```bash
python tests/test_plugin.py
```

Tests that need a live Hermes install (real sessions DB, installed
`config.yaml`) skip themselves on a machine that has none — everything else
runs offline with fakes.

## License

MIT
