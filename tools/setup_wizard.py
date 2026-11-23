"""Den setup wizard: checks everything, fixes what it can, and says exactly what is left.

    Windows:  .venv\\Scripts\\python.exe tools\\setup_wizard.py
    Linux:    .venv/bin/python tools/setup_wizard.py

Safe to run any number of times. What it does by itself:
  * creates .env and config.toml from the templates if they are missing
  * checks the token with Discord (never prints it) and builds your invite link
  * waits for you to add the bot to a server (--wait 600), then puts that server's ID in
    config.toml so slash commands show up instantly
  * reads which privileged intents are switched on and tells you which modules need them;
    --disable-missing switches those modules off instead, so the bot starts cleanly
  * checks Python, the installed packages and ffmpeg

What only a human can do (Discord requires it): create the bot application, copy its token
into .env, switch on the intents, and open the invite link. The wizard prints those steps
whenever one is missing.

Options:
  --wait SECONDS       keep checking until the bot is in a server (default: do not wait)
  --disable-missing    switch off modules whose intents are not enabled in the portal
  --guild ID           use this server ID for dev_guild_id when the bot is in several
  --json               print the result as JSON (for scripts and AI assistants)
Exit code: 0 = ready to start, 1 = something is left to do (see the output).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

API = "https://discord.com/api/v10"
# View Channels, Send Messages, Embed Links, Attach Files, Read Message History, Add Reactions,
# Manage Messages, Manage Channels, Manage Roles, Kick, Ban, Moderate Members, Connect, Speak, Send Polls
INVITE_PERMISSIONS = 564049736756310
PORTAL = "https://discord.com/developers/applications"
FLAG = {"members": (1 << 14) | (1 << 15), "message_content": (1 << 18) | (1 << 19)}
INTENT_NAME = {"members": "Server Members Intent", "message_content": "Message Content Intent"}
PLACEHOLDER = "paste-your-token-here"


class Report:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def add(self, key: str, ok: bool | None, text: str, todo: str = "") -> None:
        """ok: True done, False needs action, None optional/info."""
        self.items.append({"check": key, "ok": ok, "text": text, "todo": todo})

    @property
    def ready(self) -> bool:
        return all(i["ok"] is not False for i in self.items)

    def print(self) -> None:
        mark = {True: "[ OK ]", False: "[TODO]", None: "[ -- ]"}
        print()
        for item in self.items:
            print(f"{mark[item['ok']]} {item['text']}")
            if item["todo"]:
                for line in item["todo"].splitlines():
                    print(f"         {line}")
        print()
        print("Ready. Start the bot with start.bat (Windows) or ./start.sh (Linux)." if self.ready
              else "Do the [TODO] steps above, then run this wizard again.")


def discord_get(path: str, token: str) -> tuple[int, object]:
    request = urllib.request.Request(API + path, headers={"Authorization": f"Bot {token}",
                                                          "User-Agent": "DiscordBot (den-setup, 1.0)"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0, None


def read_token() -> str:
    from core.config import load_env
    load_env(ROOT / ".env")
    return (os.environ.get("DISCORD_TOKEN") or "").strip()


def set_config_value(text: str, section: str, key: str, value: str) -> str:
    """Replace `key = ...` inside [section] of a TOML file, keeping its comment."""
    pattern = re.compile(rf"(^\[{re.escape(section)}\][^\[]*?^{re.escape(key)}\s*=\s*)([^#\n]*?)(\s*(#.*)?)$",
                         re.M | re.S)
    if not pattern.search(text):
        raise ValueError(f"{key} not found under [{section}]")
    return pattern.sub(lambda m: m.group(1) + value + m.group(3), text, count=1)


def main() -> int:
    parser = argparse.ArgumentParser(description="Den setup wizard")
    parser.add_argument("--wait", type=int, default=0)
    parser.add_argument("--disable-missing", action="store_true")
    parser.add_argument("--guild", type=int, default=0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = Report()
    windows = os.name == "nt"

    # ---- this computer ----
    report.add("python", sys.version_info >= (3, 11), f"Python {sys.version.split()[0]}",
               "" if sys.version_info >= (3, 11) else "Install Python 3.11 or newer.")
    try:
        import discord  # noqa: F401
        report.add("packages", True, f"discord.py {discord.__version__} installed")
    except ImportError:
        report.add("packages", False, "Python packages are not installed",
                   "Run setup.bat (Windows) or ./setup.sh (Linux).")
    ff = shutil.which("ffmpeg")
    report.add("ffmpeg", True if ff else None, "ffmpeg found" if ff else "ffmpeg not found (only music and video need it)",
               "" if ff else ("Install: winget install Gyan.FFmpeg   (then open a new window)" if windows
                              else "Install: sudo apt install ffmpeg"))

    # ---- files ----
    env = ROOT / ".env"
    if not env.is_file():
        shutil.copyfile(ROOT / ".env.example", env)
        if not windows:
            os.chmod(env, 0o600)
    cfg_path = ROOT / "config.toml"
    if not cfg_path.is_file():
        shutil.copyfile(ROOT / "config.example.toml", cfg_path)
    report.add("files", True, ".env and config.toml exist")

    # ---- token ----
    token = read_token()
    create_steps = (f"1. Open {PORTAL} and click New Application (any name, e.g. Den).\n"
                    "2. Left menu: Bot. Click Reset Token, then Copy.\n"
                    f"3. Open the file {env} in a text editor and paste it after DISCORD_TOKEN=\n"
                    "   (never paste the token in a chat, screenshot or issue)")
    app = None
    if not token or token == PLACEHOLDER:
        report.add("token", False, "No bot token in .env yet", create_steps)
    else:
        status, me = discord_get("/users/@me", token)
        if status == 401:
            report.add("token", False, "Discord rejected the token in .env (wrong, or it was reset)", create_steps)
        elif status != 200:
            report.add("token", False, "Could not reach Discord to check the token", "Check the internet connection.")
        else:
            report.add("token", True, f"Token works: the bot is {me['username']}")
            _, app = discord_get("/applications/@me", token)

    # ---- intents, invite, server ----
    if app:
        from core.config import load_config
        import bot as botmod
        cfg = load_config(cfg_path)
        needed: dict[str, list[str]] = {}
        for module in cfg.enabled_modules:
            for intent in botmod.MODULE_INTENTS.get(module, ()):
                if intent in FLAG:
                    needed.setdefault(intent, []).append(module)
        flags = int(app.get("flags") or 0)
        missing = {i: mods for i, mods in needed.items() if not flags & FLAG[i]}
        if missing and args.disable_missing:
            text = cfg_path.read_text(encoding="utf-8")
            off = sorted({m for mods in missing.values() for m in mods})
            for module in off:
                text = set_config_value(text, "modules", module, "false")
            cfg_path.write_text(text, encoding="utf-8")
            report.add("intents", None, f"Switched off {', '.join(off)} (their intents are off in the portal)",
                       "To use them later: switch the intents on (below), set them back to true in config.toml.\n"
                       + "\n".join(f"{INTENT_NAME[i]}: {', '.join(mods)}" for i, mods in missing.items()))
        elif missing:
            lines = [f"{INTENT_NAME[i]}  (needed by {', '.join(mods)})" for i, mods in missing.items()]
            report.add("intents", False, "Privileged intents are off in the Developer Portal",
                       f"Open {PORTAL} > your app > Bot > Privileged Gateway Intents.\n"
                       "Switch on:\n  " + "\n  ".join(lines) + "\nThen click Save Changes.\n"
                       "Or run this wizard with --disable-missing to start without those modules.")
        else:
            report.add("intents", True, "Privileged intents: everything the enabled modules need is on"
                       if needed else "No privileged intents needed by the enabled modules")

        invite = (f"https://discord.com/oauth2/authorize?client_id={app['id']}"
                  f"&scope=bot+applications.commands&permissions={INVITE_PERMISSIONS}")
        deadline = time.time() + max(0, args.wait)
        guilds = []
        while True:
            status, data = discord_get("/users/@me/guilds", token)
            guilds = data if status == 200 and isinstance(data, list) else []
            if guilds or time.time() >= deadline:
                break
            time.sleep(5)
        if not guilds:
            report.add("server", False, "The bot is not in any server yet",
                       f"Open this link and pick your server:\n{invite}")
        else:
            names = ", ".join(f"{g['name']} ({g['id']})" for g in guilds[:5])
            report.add("server", True, f"Bot is in: {names}")
            report.add("invite", None, "Invite link for more servers", invite)
            current = int(cfg.get("bot.dev_guild_id", 0) or 0)
            ids = [int(g["id"]) for g in guilds]
            choice = args.guild or (ids[0] if len(ids) == 1 else 0)
            if current in ids and not args.guild:
                report.add("guild", True, f"dev_guild_id is set ({current}): commands appear instantly there")
            elif choice in ids:
                text = cfg_path.read_text(encoding="utf-8")
                cfg_path.write_text(set_config_value(text, "bot", "dev_guild_id", str(choice)), encoding="utf-8")
                report.add("guild", True, f"Set dev_guild_id = {choice} in config.toml")
            else:
                report.add("guild", None, "Bot is in several servers; commands sync to all of them within an hour",
                           "For instant commands in one server: run again with --guild <server ID>.")

    # ---- what is left inside Discord ----
    report.add("folders", None, "Music and video folders: pick them in Discord after starting",
               "Type /library setup in your server (bot owner only).")

    if args.json:
        print(json.dumps({"ready": report.ready, "items": report.items}, indent=2))
    else:
        report.print()
    return 0 if report.ready else 1


if __name__ == "__main__":
    sys.exit(main())
