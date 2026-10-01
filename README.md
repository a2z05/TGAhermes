# TGAhermes

A Telegram plugin for [Hermes](https://github.com/NousResearch/hermes-agent).

It puts a gate in front of your bot: strangers can reach it through the guest
link, your own chats stay private, and every interaction shows up in a log
channel you pick. There's an admin panel to run it all, and the plugin can
update itself.

Needs Hermes 0.21.5+ and a Bot API 10 bot token.

## What it does

- **Guest gate** — people who find the guest link get answered; casual
  mentions get a canned reply you control, with a cooldown.
- **Log channel** — everything lands in one channel, with buttons: profile,
  info, delete, ban.
- **Admin panel** — `!panel` opens a console of buttons: status, log, access,
  sessions, guests, system.
- **Telegram admin tool** — ask in plain language ("delete that message",
  "ban this user") and it runs, in your session only.
- **Reactions and media** — 👀 ✅ ❌ feedback, and URL media sent to guests.
- **Self-update** — pull a newer version from the panel without reinstalling.

Every chat — your DM, each group, each guest — is its own session, so guest
traffic never bleeds into your own conversations.

## Install

Two ways. Pick whichever suits you.

### Path A — give it to your agent (recommended)

Send your Hermes agent the link to this repo and say:

```bash
install this plugin for me : https://github.com/a2z05/TGAhermes
```

It reads [AGENT.md](AGENT.md) — a step-by-step guide written for agents —
runs the install, checks that the plugin loaded, and tells you what to do
next. You don't have to touch the terminal.

### Path B — install it yourself

```bash
hermes plugins install https://github.com/a2z05/TGAhermes --enable
```

Check that it landed:

```bash
hermes plugins list --plain | grep -i TGAhermes
```

You want `enabled` in front of `TGAhermes`. A running gateway usually picks
the plugin up on the spot; if it doesn't:

```bash
hermes gateway restart
```

**Manual fallback**, if the CLI isn't an option:

```bash
git clone https://github.com/a2z05/TGAhermes.git "$HERMES_HOME/plugins/TGAhermes"
hermes plugins enable TGAhermes
hermes gateway restart
```

`$HERMES_HOME` is your Hermes home directory (`~/.hermes` for most people).

## First config — three messages

Message your bot directly and send:

1. `!setowner <your telegram id>` — sets you as the owner. Hot, no restart.
2. `!setlog <chat id>` — where activity gets logged. Or go into the channel
   or group you want and send `!setlog here`. The bot must be able to post
   there (make it admin if you want the delete/ban buttons to work).
3. `!panel` — opens the admin panel.

That's the whole setup.

The panel has an **Actions** view with wizards for the common jobs — set the
log channel, whitelist a friend, change the reply strangers get — so most of
the time you never have to type a command at all.

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

Open `!panel` → **🔧 System** → **Check for update**, then **Install update**.
Your agent can do the same through its update tool. Lock the version by
setting `update_enabled` to `false` in `settings.json`.

## When something's wrong

- **No log entries** — the bot can't post in the log channel. Add it as
  admin, then `!setlog here` again.
- **Strangers treated as you** — `owner_id` is wrong; it's a numeric id, not
  a username.
- **Plugin not there after a restart** — run `hermes plugins list` and read
  the status column.

[AGENT.md](AGENT.md) has a longer troubleshooting table — that's the file
your agent follows.

## License

MIT
