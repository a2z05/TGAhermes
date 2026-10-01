# AGENT.md — install TGAhermes

## What you are being asked to do

One sentence: install the TGAhermes plugin into this Hermes installation,
check that it actually loaded, and tell the human what to do next.

The human-facing instructions live in [README.md](README.md). This file is
for you. Follow it in order, don't improvise, and report what really
happened — including anything that failed.

---

## Step 0 — prerequisites

Find the CLI:

```bash
command -v hermes || echo /opt/hermes/.venv/bin/hermes
```

Use `hermes` if it printed a path, otherwise the `/opt/hermes/.venv/bin/hermes`
fallback. Then check both exist:

```bash
hermes --version              # expect 0.21.5 or newer
hermes gateway status         # a gateway should be present (running or installable)
```

If Hermes is missing or older than 0.21.5, stop and say so. Do not upgrade
someone else's install behind their back.

Plugin name and source:

- Name: `TGAhermes` (version 3.0.0)
- Source: `https://github.com/a2z05/TGAhermes`
- Installs to: `$HERMES_HOME/plugins/TGAhermes`
- `$HERMES_HOME` is `~/.hermes` unless the environment says otherwise — read
  it from the environment, never hardcode it.

---

## Step 1 — install

```bash
hermes plugins install https://github.com/a2z05/TGAhermes --enable
```

Expected output (wording varies slightly by version):

```
Warning: custom (unreviewed) source — not from the Hermes catalog.
Cloning https://github.com/a2z05/TGAhermes...

✓ Installed
│ Plugin installed: https://github.com/a2z05/TGAhermes
│ Location: $HERMES_HOME/plugins/TGAhermes

✓ Plugin TGAhermes enabled.

  Plugin TGAhermes requests the following capabilities:
    tools.override — Replace built-in tools (e.g. shell_exec, write_file) ...
  Non-interactive session: capabilities NOT granted (fail closed). ...

Restart the gateway for the plugin to take effect:
  hermes gateway restart
```

`--enable` skips the confirmation prompt, which you can't answer in a
non-interactive session. Never drop it.

The `tools.override` line matters: in a non-interactive session the
capability is **not** granted, so the plugin's `telegram_admin` tool stays
closed until you grant it in Step 3.

---

## Step 2 — verify the install

```bash
hermes plugins list --plain | grep -i TGAhermes
```

Expected: one line like

```
enabled      git      3.0.0    TGAhermes
```

The first column must say `enabled`. Then confirm the manifest agrees:

```bash
hermes plugins show TGAhermes
```

Expected: `TGAhermes v3.0.0` and `Status: enabled`.

**If `grep` finds nothing**, the manifest name and the directory name may
disagree (a manual copy keeps the old name). Read the real name and use it
everywhere below:

```bash
hermes plugins list --plain --user
```

Then enable whatever name it printed:

```bash
hermes plugins enable <name-from-list>
```

---

## Step 3 — grant the capability

```bash
hermes plugins enable TGAhermes --allow-tool-override
hermes plugins capabilities TGAhermes
```

Expected last line:

```
  tools.override: granted
```

Without this, the plugin loads but its admin tool refuses to run. Safe to
run even if the plugin is already enabled.

---

## Step 4 — reload and health-check the gateway

Installing nudges a running gateway to reload its plugins. If no gateway
answered, a restart is required:

```bash
hermes gateway restart
```

Then check the gateway is alive:

```bash
curl -s http://127.0.0.1:8642/health
```

Expected:

```
{"status": "ok", "platform": "hermes-agent", "version": "..."}
```

If the health endpoint fails, the gateway is down — that's a gateway
problem, not a plugin problem. Report it as such.

---

## Step 5 — configuration

Settings live next to the plugin:

```
$HERMES_HOME/plugins/TGAhermes/settings.json
```

Two keys matter at first run: `owner_id` and `log_channel`. The file is
created on the first save, so you do **not** have to write it by hand — the
owner commands below do it, and they are the preferred route because they're
validated by the plugin itself. If the human prefers editing a file, start
from `settings.example.json` in the plugin directory.

Send these as messages to the bot, in the human's own DM with it:

1. `!setowner <their telegram id>` — sets `owner_id`. Hot, no restart.
2. `!setlog <chat id>` — sets `log_channel`. Or, sent from inside the
   target channel/group, `!setlog here`. `!setlog off` clears it.
   The bot must be able to post in that chat; make it admin there, otherwise
   log entries won't arrive.
3. `!panel` — opens the admin panel.

If a command isn't recognised (for example `here` is answered with the raw
word instead of a chat id), fall back to `!setlog <chat id>` and say so in
your report.

Full owner command list: `!panel !help !settings !users !setowner
!setlog <id|@name|here|off> !whitelist list|add|remove <id>
!wipe <chat_id> !send <user_id> <text> !setcooldown <s>
!setunauthorized <text> !seterror <text> !seterrorfa <text>
!setreact on|off !setmedia on|off !guestlock on|off`.

The panel's **Actions** view has wizards for these — mention to the human
that they can click instead of type.

---

## Step 6 — verification checklist

Do not skip any of these:

- [ ] `hermes plugins list --plain | grep -i TGAhermes` shows `enabled`.
- [ ] `hermes plugins show TGAhermes` shows `Status: enabled`.
- [ ] `hermes plugins capabilities TGAhermes` shows `tools.override: granted`.
- [ ] `curl -s http://127.0.0.1:8642/health` returns `"status": "ok"`.
- [ ] `settings.json` exists at `$HERMES_HOME/plugins/TGAhermes/settings.json`
      after the owner commands ran, with `owner_id` set.
- [ ] A real test message from the human got a reply, and a line appeared in
      the log channel.

---

## Step 7 — report

Tell the human plainly:

- version installed and where it lives
- the three first-config commands (or the ones you already ran)
- that the panel's Actions view replaces typing commands
- anything that failed, without dressing it up

---

## Updating later

Two ways, no third:

1. `!panel` → **🔧 System** → **Check for update**, then **Install update**.
2. Re-run Step 1 (`hermes plugins install ... --enable`) to reinstall fresh.

Your agent-side option is the plugin's update tool, which refuses while
updates are locked (`update_enabled: false` in `settings.json` — the panel
shows 🔒 Updates locked). If it refuses, tell the human the version is
locked on purpose; don't try to unlock it yourself.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Plugin not listed, or listed as `not enabled` | install didn't finish, or enable was skipped | `hermes plugins install https://github.com/a2z05/TGAhermes --enable`, then `hermes plugins enable TGAhermes` |
| `No plugin named 'TGAhermes'` | directory name and manifest name disagree | `hermes plugins list --plain --user`, use the name printed there |
| `hermes: command not found` | CLI not on PATH | use `/opt/hermes/.venv/bin/hermes` |
| Settings changes have no effect | looking at the wrong file | the path is `$HERMES_HOME/plugins/TGAhermes/settings.json` — read `$HERMES_HOME` from the environment |
| Log channel silent | bot can't post there | add the bot as admin in that chat, then `!setlog here` again |
| `!setlog here` echoed literally | build wants an id | use `!setlog <chat id>` |
| Strangers answered as the owner | wrong `owner_id` | numeric telegram id, not a username: `!setowner <id>` |
| `telegram_admin` tool unavailable | capability not granted | `hermes plugins enable TGAhermes --allow-tool-override` |
| Health endpoint unreachable | gateway down | `hermes gateway restart`, then re-check `/health` |
| Updates refused | locked via panel | human choice — report it, don't unlock |

---

## Ground rules

- **Never print, ask for, or paste secrets.** No bot tokens, no API keys, no
  passwords — not into the chat, not into logs, not into files you write.
  If a token is needed, tell the human where to put it and let them do it.
- **Never edit `config.yaml` by hand.** Use `hermes config set <key> <value>`
  if a config change is genuinely needed; a stray indent can break the
  running gateway.
- **Don't touch anything outside `$HERMES_HOME/plugins/TGAhermes`.**
- **Report honestly.** If a step didn't work, say which one and why.
