# AI setup guide for Den

This file is for AI coding assistants (Claude Code, Codex, Cursor, Copilot and others). When the
user asks you to **set up, install, run or start Den**, follow these steps in order. They get the
bot running with the user doing only the steps Discord reserves for humans.

## Rules

- **Never ask for the bot token in chat, and never print, log or commit it.** The user pastes it
  into `.env` themselves. To check it, run the wizard, which validates it without printing it.
- Never commit `.env`, `config.toml` or `data/`. They are git-ignored; keep it that way.
- Do not turn on `music.youtube` unless the user asks (it breaks YouTube's terms of service).
- Do not change `watch.bind`, `watch.tunnel` or `watch.public_url` unless the user asks. Opening
  the watch server to the internet is the user's decision.
- Explain each step that needs the user in one or two plain sentences, with the exact clicks.

## Steps

### 1. Python 3.11+ and ffmpeg

Check: `python --version` (Windows may use `py -3 --version`) and `ffmpeg -version`.

| Missing | Windows | Debian/Ubuntu |
|---|---|---|
| Python 3.11+ | `winget install Python.Python.3.12` (then open a new terminal) | `sudo apt install python3 python3-venv` |
| ffmpeg (music, video, watch parties) | `winget install Gyan.FFmpeg` (then open a new terminal) | `sudo apt install ffmpeg` |

### 2. Install

Windows, without the closing pause: `set DEN_NO_PAUSE=1 && setup.bat` in cmd, or
`$env:DEN_NO_PAUSE=1; .\setup.bat` in PowerShell. Linux: `chmod +x setup.sh start.sh && ./setup.sh`.

Setup creates `.venv`, installs the one dependency, creates `.env` and `config.toml`, then runs
the wizard. The first time it reports `[TODO] No bot token`. That is expected.

### 3. Ask the user to do the Discord steps (humans only)

Send the user this, adapted to their language:

> I need you to do a few things on Discord's website; I can't do them for you:
> 1. Open https://discord.com/developers/applications and click **New Application**. Name it
>    whatever you like (for example Den).
> 2. In the left menu, click **Bot**. Click **Reset Token**, then **Copy**.
> 3. Open the file `.env` in this folder with a text editor. Paste the token after
>    `DISCORD_TOKEN=`, then save. Please don't paste it here in the chat.
> 4. On the same Bot page, under **Privileged Gateway Intents**, switch on **Server Members
>    Intent** and **Message Content Intent**, then click **Save Changes**.
> Tell me when that's done.

### 4. Run the wizard and get the invite link

Windows: `.venv\Scripts\python.exe tools\setup_wizard.py --json`
Linux: `.venv/bin/python tools/setup_wizard.py --json`

Read the JSON. Every item has `check`, `ok` (true = done, false = needs action, null = info),
`text` and `todo`.

- `token` false: the token is missing or wrong. Ask the user to redo step 3.2–3.3.
- `server` false: give the user the invite link from its `todo` and ask them to open it and
  pick their server. Then run the wizard with `--wait 600 --json` so it waits until the bot has
  joined. It sets `dev_guild_id` by itself.
- `intents` false: ask the user to switch on the intents named in `todo`. If they don't want
  to, run the wizard with `--disable-missing`, which switches off only the modules that need them.
- `ffmpeg` null: install it (step 1) if the user wants music, video or watch parties.

Repeat until `"ready": true`.

### 5. Test and start

1. Run the offline tests and check each ends with "passed":
   `.venv\Scripts\python.exe tests\test_core.py` (likewise `test_modules.py`, `test_music.py`,
   `test_watch.py`, `test_startup.py`).
2. Start the bot: Windows `start.bat`, Linux `./start.sh`. Run it in the background if your tool
   allows it, or ask the user to double-click `start.bat`.
3. Confirm it is online: `data/logs/bot.log` contains `Online as <name>`.

### 6. Tell the user what to do in Discord

> Den is online. In your server:
> - `/help` lists everything.
> - `/library setup` picks your music and video folders (only you can use it, as the bot owner).
> - `/play` plays music once you're in a voice channel. `/watch start` starts a watch party.

## Troubleshooting

| Wizard or log says | Do this |
|---|---|
| `Discord rejected the token` | The user resets the token in the portal and pastes the new one into `.env`. |
| `intents are not switched on` | Step 3.4, or run the wizard with `--disable-missing`. |
| `Another copy of the bot is already running` | A bot process is already running from this folder. Stop that one; don't delete `data/bot.lock` while it runs. |
| Slash commands missing | `dev_guild_id` must be the server's ID (the wizard sets it). Then restart the bot. |
| Music is silent | Run `/status` and read the Voice line; see README > Troubleshooting. |

More detail: `README.md` (setup, permissions, troubleshooting) and `SECURITY.md`.
