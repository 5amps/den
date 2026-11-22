"""Potato Bot entry point.

    python bot.py            run the bot
    python bot.py --check    validate config, database and modules without connecting

Exit codes (used by start.bat / start.sh):
    0   clean shutdown, do not restart
    1   crashed or /restart after running normally, restart quickly
    2   failed before it ever came online, restart with a growing delay
    78  configuration problem (bad token, intents not enabled), do not restart
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import random
import sys
import time
from pathlib import Path

if sys.version_info < (3, 11):
    sys.exit("Python 3.11 or newer is required.")

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from core import proc, sysinfo
from core.checks import describe_error, reply
from core.config import ROOT, Config, ConfigError, load_config
from core.db import Database
from core.log import setup_logging
from core.scheduler import Scheduler
from core.settings import GLOBAL, Settings

log = logging.getLogger("bot")

EXIT_OK, EXIT_CRASH, EXIT_NEVER_READY, EXIT_CONFIG = 0, 1, 2, 78
NEEDS_INTENTS = -1      # internal: Discord refused the privileged intents we asked for

# Gateway intents each module needs. Only enabled modules contribute, so a bot running
# tickets + RSVP alone asks Discord for almost nothing.
MODULE_INTENTS: dict[str, tuple[str, ...]] = {
    "music": ("voice_states",),
    "video": ("voice_states",),
    "tickets": ("message_content",),                      # transcripts need message text
    "rsvp": (),
    "antiraid": ("members", "guild_messages", "message_content"),
    "antispam": ("guild_messages", "message_content"),
    "moderation": (),
    "utility": ("members", "guild_messages", "message_content", "guild_reactions"),
}
PRIVILEGED = {
    "members": "Server Members Intent",
    "message_content": "Message Content Intent",
    "presences": "Presence Intent",
}


def build_intents(cfg: Config) -> discord.Intents:
    intents = discord.Intents.none()
    intents.guilds = True
    for module in cfg.enabled_modules:
        for name in MODULE_INTENTS.get(module, ()):
            setattr(intents, name, True)
    for name in cfg.get("bot.extra_intents", []) or []:
        if name not in discord.Intents.VALID_FLAGS:
            raise ConfigError(f"bot.extra_intents: '{name}' is not a Discord intent name")
        setattr(intents, name, True)
    return intents


def privileged_in_use(intents: discord.Intents) -> list[str]:
    return [label for name, label in PRIVILEGED.items() if getattr(intents, name)]


class Tree(app_commands.CommandTree):
    """Routes every slash-command failure to one short, private message."""

    async def on_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        message, is_bug = describe_error(error)
        if is_bug:
            name = interaction.command.qualified_name if interaction.command else "?"
            log.error("Command /%s failed", name, exc_info=getattr(error, "original", error))
        await reply(interaction, message)


class PotatoBot(commands.Bot):
    def __init__(self, cfg: Config):
        intents = build_intents(cfg)
        # Keep the member cache tiny: no startup chunking, only members sitting in voice.
        cache_flags = discord.MemberCacheFlags.none()
        cache_flags.voice = intents.voice_states
        status = str(cfg.get("bot.status", "") or "")
        super().__init__(
            command_prefix=commands.when_mentioned,   # slash commands only; no prefix parsing
            help_command=None,
            tree_cls=Tree,
            intents=intents,
            member_cache_flags=cache_flags,
            chunk_guilds_at_startup=False,
            max_messages=int(cfg.get("bot.message_cache", 500)) or None,
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True),
            activity=discord.Game(status) if status else None,
        )
        self.cfg = cfg
        self.db = Database(cfg.data_dir / "bot.db")
        self.settings = Settings(self.db)
        self.scheduler = Scheduler(self.db)
        self.started_at = time.time()
        self.loaded_modules: list[str] = []
        self.failed_modules: list[str] = []
        self.was_ready = False
        self.exit_code = EXIT_OK
        self._prepared = False

    # ------------------------------------------------------------------ startup
    async def prepare(self) -> None:
        """Open storage and load enabled modules. Safe to call more than once."""
        if self._prepared:
            return
        self._prepared = True
        await self.db.open()
        await self.load_extension("cogs.system")
        for module in self.cfg.enabled_modules:
            if not (ROOT / "cogs" / f"{module}.py").is_file():
                log.warning("Module '%s' is enabled but cogs/%s.py does not exist; skipped", module, module)
                self.failed_modules.append(module)
                continue
            try:
                await self.load_extension(f"cogs.{module}")
                self.loaded_modules.append(module)
            except Exception:  # noqa: BLE001 - one broken module must not stop the others
                log.exception("Module '%s' failed to load; the bot continues without it", module)
                self.failed_modules.append(module)
        log.info("Modules loaded: %s", ", ".join(self.loaded_modules) or "(none, core only)")

    async def setup_hook(self) -> None:
        await self.prepare()
        self.scheduler.start(self.wait_until_ready)
        await self.sync_commands()

    async def sync_commands(self, force: bool = False) -> str:
        """Upload slash commands only when their definitions changed since the last sync."""
        dev_guild = int(self.cfg.get("bot.dev_guild_id", 0) or 0)
        target = discord.Object(id=dev_guild) if dev_guild else None
        payload = sorted(
            json.dumps(cmd.to_dict(self.tree), sort_keys=True, default=str)
            for cmd in self.tree.get_commands()
        )
        digest = hashlib.sha1(f"{dev_guild}|{'|'.join(payload)}".encode()).hexdigest()
        if not force and await self.settings.get(GLOBAL, "command_hash") == digest:
            return "unchanged"
        try:
            if target is not None:
                self.tree.copy_global_to(guild=target)
            synced = await self.tree.sync(guild=target)
        except discord.HTTPException as exc:
            log.warning("Slash-command sync failed (will retry next start): %s", exc)
            return f"failed: {exc}"
        await self.settings.set(GLOBAL, "command_hash", digest)
        where = f"dev server {dev_guild}" if dev_guild else "all servers (can take up to an hour to appear)"
        log.info("Synced %d slash command(s) to %s", len(synced), where)
        return f"synced {len(synced)} command(s)"

    # ------------------------------------------------------------------ events
    async def on_ready(self) -> None:
        self.was_ready = True
        log.info("Online as %s in %d server(s). RAM %.0f MB.", self.user, len(self.guilds), sysinfo.rss_mb())

    async def on_resumed(self) -> None:
        log.info("Connection resumed after a drop.")

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        self.settings.forget(guild.id)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        log.info("Added to server %s (%s).", guild.name, guild.id)
        if guild.id == int(self.cfg.get("bot.dev_guild_id", 0) or 0):
            # The start-up sync fails if the bot was not in the server yet; do it now.
            await self.sync_commands(force=True)

    async def on_error(self, event_method: str, /, *args, **kwargs) -> None:
        log.exception("Unhandled error in %s", event_method)

    # ------------------------------------------------------------------ shutdown
    async def shutdown(self) -> None:
        try:
            await self.scheduler.stop()
            if not self.is_closed():
                await self.close()          # unloads modules, which stop their own players
        except Exception:  # noqa: BLE001
            log.exception("Error while closing the Discord connection")
        try:
            await proc.stop_all()           # no ffmpeg / yt-dlp left behind
        finally:
            await self.db.close()


# ---------------------------------------------------------------------- single instance
def acquire_lock(path: Path):
    """Hold an OS-level lock so two copies cannot answer the same commands. None if taken."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+")  # noqa: SIM115 - must stay open for the life of the process
    try:
        handle.seek(0)
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


# ---------------------------------------------------------------------- run modes
async def run_bot(cfg: Config) -> int:
    token = cfg.token
    bot = PotatoBot(cfg)
    needed = privileged_in_use(bot.intents)
    if needed:
        log.info("Privileged intents in use (enable them in the Developer Portal): %s", ", ".join(needed))
    try:
        # Log in with our own backoff so starting with no internet waits instead of crashing.
        delay = 2.0
        while True:
            try:
                await bot.login(token)
                break
            except discord.LoginFailure:
                log.error("Discord rejected the token. Put a valid DISCORD_TOKEN in .env "
                          "(Developer Portal > Bot > Reset Token).")
                return EXIT_CONFIG
            except (aiohttp.ClientError, OSError, asyncio.TimeoutError, discord.HTTPException) as exc:
                wait = delay + random.uniform(0, delay / 4)
                log.warning("Cannot reach Discord (%s). Retrying in %.0fs.", exc.__class__.__name__, wait)
                await bot.http.close()
                await asyncio.sleep(wait)
                delay = min(delay * 2, 300.0)
        # From here discord.py reconnects by itself with exponential backoff and resumes sessions.
        await bot.connect(reconnect=True)
        return bot.exit_code
    except discord.PrivilegedIntentsRequired:
        log.error(
            "Discord refused the connection because these intents are not switched on: %s.\n"
            "Fix: discord.com/developers/applications > your app > Bot > Privileged Gateway Intents, "
            "enable them, save, then start the bot again.", ", ".join(needed) or "(unknown)",
        )
        return NEEDS_INTENTS
    except asyncio.CancelledError:
        return EXIT_OK      # Ctrl+C
    except Exception:  # noqa: BLE001 - log it, then let the start script restart us
        log.exception("Fatal error")
        return EXIT_CRASH if bot.was_ready else EXIT_NEVER_READY
    finally:
        await bot.shutdown()


async def run_check(cfg: Config) -> int:
    """Offline self-test: everything except the network connection."""
    bot = PotatoBot(cfg)
    try:
        await bot.prepare()
        try:
            cfg.token
            token_state = "set"
        except ConfigError:
            token_state = "MISSING (copy .env.example to .env and paste your token)"
        intents = [name for name, on in bot.intents if on]
        lines = [
            f"Python          {sys.version.split()[0]}",
            f"discord.py      {discord.__version__}",
            f"Config          {cfg.path}",
            f"Token           {token_state}",
            f"Database        {bot.db.path} (ok)",
            f"Modules loaded  {', '.join(bot.loaded_modules) or '(core only)'}",
            f"Modules failed  {', '.join(bot.failed_modules) or 'none'}",
            f"Slash commands  {len(bot.tree.get_commands())}",
            f"Intents         {', '.join(intents)}",
            f"Privileged      {', '.join(privileged_in_use(bot.intents)) or 'none'}",
            f"ffmpeg          {proc.which('ffmpeg') or 'NOT FOUND (needed for music/video only)'}",
            f"yt-dlp          {proc.which('yt-dlp') or 'not found (needed for YouTube only)'}",
            f"Free disk       {sysinfo.free_disk_mb(cfg.data_dir):.0f} MB",
            f"RAM right now   {sysinfo.rss_mb():.1f} MB (before connecting)",
        ]
        print("\n".join(lines))
        return EXIT_OK if not bot.failed_modules else EXIT_CRASH
    finally:
        await bot.shutdown()


def main() -> int:
    parser = argparse.ArgumentParser(description="Potato Bot")
    parser.add_argument("--config", help="path to a config file (default: config.toml)")
    parser.add_argument("--check", action="store_true", help="validate setup without connecting")
    args = parser.parse_args()

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"CONFIG ERROR: {exc}")
        return EXIT_CONFIG
    setup_logging(cfg.data_dir / "logs", cfg.get("bot.log_level", "INFO"), bool(cfg.get("bot.voice_debug", False)))

    lock = acquire_lock(cfg.data_dir / "bot.lock")
    if lock is None:
        print("Another copy of the bot is already running with this data folder. Close it first.")
        return EXIT_CONFIG
    try:
        if args.check:
            return asyncio.run(run_check(cfg))
        code = asyncio.run(run_bot(cfg))
        if code == NEEDS_INTENTS:
            # Run with what Discord allows instead of refusing to start at all.
            blocked = [m for m in cfg.enabled_modules if set(MODULE_INTENTS.get(m, ())) & set(PRIVILEGED)]
            if not blocked or not bool(cfg.get("bot.start_without_intents", True)):
                return EXIT_CONFIG
            cfg.disabled.update(blocked)
            log.warning("Starting WITHOUT these modules until the intents are enabled: %s", ", ".join(blocked))
            code = asyncio.run(run_bot(cfg))
            if code == NEEDS_INTENTS:
                return EXIT_CONFIG
        return code
    except ConfigError as exc:
        print(f"CONFIG ERROR: {exc}")
        return EXIT_CONFIG
    except KeyboardInterrupt:
        return EXIT_OK
    finally:
        lock.close()


if __name__ == "__main__":
    sys.exit(main())
