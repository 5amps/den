# Potato Bot

A self-hosted, all-in-one Discord bot built to run on weak hardware (dual-core, 2 GB RAM, HDD).

- Runs on your own PC. All data is one SQLite file in `data/`.
- One Python dependency (`discord.py`). No Docker, no Redis, no cloud services, no paid APIs.
- Every feature is a module you switch on or off in `config.toml`. A module that is off is never loaded.

**Build status:** phase (a) of (f). The core is done: config, database, scheduler, reconnect logic,
start scripts and the `/ping`, `/status`, `/sync`, `/restart`, `/shutdown` commands. Music, anti-spam,
anti-raid, tickets, RSVP and moderation are added in the next phases.

## What "local" means here

The bot program, your music files and all saved data stay on your PC. Two things are unavoidable:

1. **Discord itself is an online service.** The bot needs an internet connection to talk to it, and
   music is uploaded from your PC to Discord's voice servers while it plays (roughly 100 kbit/s).
2. **The bot only works while it is running.** When the PC is off or asleep, buttons, tickets,
   anti-spam and reminders do nothing. Reminders that came due while it was off are sent late, when
   it starts again.

## Setup

### 1. Create the bot in Discord

1. Open <https://discord.com/developers/applications> and click **New Application**.
2. Go to **Bot**. Click **Reset Token**, then **Copy**. This is your token. Keep it private.
3. On the same page, under **Privileged Gateway Intents**: leave everything off for now.
   Later phases need **Server Members Intent** and **Message Content Intent**; the bot tells you
   at startup which ones the enabled modules require.
4. Go to **Installation**, set Install Link to **None**. Then go to **OAuth2 > URL Generator**,
   tick `bot` and `applications.commands`, and tick these bot permissions:
   View Channels, Send Messages, Embed Links, Attach Files, Read Message History, Add Reactions,
   Manage Messages, Manage Channels, Manage Roles, Kick Members, Ban Members, Moderate Members,
   Connect, Speak.
5. Open the generated URL in your browser and add the bot to your server.

The equivalent ready-made link (replace `YOUR_APP_ID` with the Application ID from **General Information**):

```
https://discord.com/oauth2/authorize?client_id=YOUR_APP_ID&scope=bot+applications.commands&permissions=1099783334998
```

### 2. Install

**Windows:** double-click `setup.bat`.
**Linux:** `chmod +x setup.sh && ./setup.sh`

Setup creates a private Python environment in `.venv`, installs the dependency, creates `.env`
and runs a self-test. It needs Python 3.11 or newer. ffmpeg is only needed for music and video.

### 3. Add your token

Open `.env` in a text editor and paste the token:

```
DISCORD_TOKEN=your-token-here
```

### 4. (Recommended) set your server ID

In `config.toml`, set `dev_guild_id` to your server's ID. Slash commands then appear instantly.
With `0`, Discord can take up to an hour to show them.

### 5. Start

**Windows:** double-click `start.bat`.
**Linux:** `./start.sh`

The start script restarts the bot if it crashes. Stop it with Ctrl+C, or with `/shutdown` in Discord.

## Testing phase (a)

| Step | Expected result |
|---|---|
| `.venv\Scripts\python.exe tests\test_core.py` | `All 32 checks passed.` |
| `.venv\Scripts\python.exe tests\test_startup.py` | `Startup checks passed.` |
| `.venv\Scripts\python.exe bot.py --check` | A report with `Modules failed  none` and the RAM figure |
| Run `start.bat` | Log line `Online as <name> in 1 server(s). RAM .. MB.` |
| `/ping` in Discord | `Pong. Gateway latency: .. ms` |
| `/status` | Embed with RAM, uptime, modules |
| Unplug the network for a minute, plug it back | Log shows `Connection resumed after a drop.`, bot answers again |
| Start the bot with the network unplugged | `Cannot reach Discord ... Retrying in 2s`, then 4s, 8s ... up to 300s; connects once the network is back |
| Run `start.bat` a second time while it is running | `Another copy of the bot is already running` |
| `/restart` | Bot goes offline and returns within about 10 seconds |

On Linux use `.venv/bin/python` in place of `.venv\Scripts\python.exe`.

## Folder layout

```
potato-bot/
  bot.py            entry point: config, database, module loading, reconnect, exit codes
  config.toml       all settings, commented
  .env              your token (created by setup, never share it)
  requirements.txt
  setup.bat / setup.sh    one-time install
  start.bat / start.sh    run with auto-restart
  core/
    config.py       TOML + .env loading
    db.py           SQLite on one worker thread, automatic write batching
    settings.py     per-server settings, cached in memory
    scheduler.py    persistent timers (reminders, auto-close)
    ttl.py          expiring in-memory counters for anti-spam / anti-raid
    proc.py         ffmpeg / yt-dlp process control with timeouts and tree kill
    checks.py       permission checks and user-facing error messages
    sysinfo.py      RAM and disk readings
    log.py          console + rotating log file
  cogs/
    system.py       /ping /status /sync /restart /shutdown (always loaded)
  tests/            offline tests, no token needed
  data/             bot.db, logs/ (created automatically)
```

## Troubleshooting

| Problem | Fix |
|---|---|
| `DISCORD_TOKEN is not set` | Copy `.env.example` to `.env` and paste the token after `=`. |
| `Discord rejected the token` | The token is wrong or was reset. Developer Portal > Bot > Reset Token, paste the new one. |
| `intents are not switched on` | Developer Portal > Bot > Privileged Gateway Intents: enable the ones named in the message, save. |
| Slash commands do not appear | Set `dev_guild_id` in `config.toml` and restart, or wait up to an hour. Check the invite link included `applications.commands`. |
| `Another copy of the bot is already running` | Close the other window, or end `python.exe` in Task Manager. |
| `Python was not found` | Install Python 3.11+ from python.org and tick "Add python.exe to PATH". |
| `ffmpeg not found` | Windows: `winget install Gyan.FFmpeg`, then reopen the window. Linux: `sudo apt install ffmpeg`. |
| A module is listed under "Failed to load" in `/status` | Read `data/logs/bot.log`; the full error is there. The other modules keep running. |
| Bot keeps restarting | Read the last lines of `data/logs/bot.log`. Restart delay grows to 5 minutes, so it will not hammer Discord. |
| "The disk is full" | Free space. Nothing from the failed command was saved; the database itself is not damaged. |

## Adding a module (short version)

1. Create `cogs/yourmodule.py` with a `commands.Cog` class and an `async def setup(bot)` that calls `bot.add_cog(...)`.
   `cogs/system.py` is a minimal working example.
2. Add the name to `KNOWN_MODULES` in `core/config.py`, and to `MODULE_INTENTS` in `bot.py` with the
   gateway intents it needs (an empty tuple if it only uses slash commands and buttons).
3. Add `yourmodule = true` under `[modules]` in `config.toml`.
4. Inside the cog use `bot.db` (database), `bot.settings` (per-server settings), `bot.scheduler`
   (timers) and `bot.cfg` (config). Create your tables in `cog_load` with
   `await bot.db.script("CREATE TABLE IF NOT EXISTS ...")`.

A full walkthrough with a worked example is added in phase (f).
