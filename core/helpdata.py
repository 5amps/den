"""Builds the command reference from the commands that are actually registered.

Used by /help in Discord and by tools/gen_commands.py (which writes COMMANDS.md), so the
documentation can never drift from the code.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from discord import app_commands

# Cog class name -> (heading, one-line summary, how a new member starts using it)
MODULES: dict[str, tuple[str, str, str]] = {
    "System": ("⚙️ Core", "Health checks, help and server settings.",
               "`/help` shows everything. `/ping` checks the bot is alive."),
    "Music": ("🎵 Music", "Play your own music files in a voice channel.",
              "Join a voice channel, type `/play`, start typing a song title and pick one. "
              "`/queue` shows what is next, `/skip` moves on, `/stop` ends it."),
    "Video": ("🎬 Video", "Audio from local video files, and short clips.",
              "`/video play` works like `/play` but for video files (sound only)."),
    "Library": ("📂 Media folders", "Choose which folders on the bot's PC hold music and videos.",
                "Bot owner: `/library setup` opens a panel; pick a folder from the list or type a path."),
    "Watch": ("🍿 Watch parties", "Watch a local video together in the browser, in sync.",
              "`/watch start`, pick a video, open the link and press Join. Pause and seek are shared."),
    "Rsvp": ("📅 Events", "Events with Going / Maybe / Can't go buttons.",
             "`/event list` shows what is coming up. Press a button on an event to answer."),
    "Tickets": ("🎫 Tickets", "Private help channels with staff.",
                "Press the button on the ticket panel. Use `/ticket close` when you are done."),
    "Reminders": ("⏰ Reminders", "The bot DMs you when it is time.",
                  "`/remind when: in 2h what: stretch` · `/reminders list` shows yours."),
    "Polls": ("📊 Polls", "Discord's own polls: live bars, results posted automatically.",
              "`/poll create` with options like `Pizza | Tacos | Sushi`."),
    "Moderation": ("🔨 Moderation", "Warnings, timeouts, kicks, bans and clean-up.",
                   "Staff only. `/warn`, `/timeout`, `/purge`; `/history` shows a member's record."),
    "AntiSpam": ("🛡️ Anti-spam", "Automatic spam filtering.",
                 "Works by itself. Staff: `/antispam status` shows the limits and recent actions."),
    "AntiRaid": ("🚨 Anti-raid", "Raid detection and lockdown.",
                 "Works by itself. Staff: `/panic` locks the server at once, `/unlock` lifts it."),
    "Utility": ("🧰 Utility", "Welcome messages, auto-role and reaction roles.",
                "Staff: `/welcome set`, `/autorole`, `/reactionrole add`."),
}
ORDER = list(MODULES)


@dataclass
class CommandInfo:
    name: str                       # "playlist save"
    description: str
    usage: str                      # "/playlist save <name>"
    access: str                     # "Everyone" or "Needs Manage Server"
    options: list[tuple[str, bool, str]] = field(default_factory=list)   # (name, required, description)


def _access(command: app_commands.Command) -> str:
    perms = command.default_permissions
    node = command.parent
    while perms is None and node is not None:
        perms = node.default_permissions
        node = node.parent
    if perms is None or perms.value == 0:
        return "Everyone"
    names = [name.replace("_", " ").title().replace("Guild", "Server") for name, on in perms if on]
    return "Needs " + ", ".join(names)


def describe(command: app_commands.Command) -> CommandInfo:
    options = [(p.display_name, p.required, "" if p.description == "…" else p.description) for p in command.parameters]
    usage = "/" + command.qualified_name + "".join(
        f" <{name}>" if required else f" [{name}]" for name, required, _ in options)
    return CommandInfo(command.qualified_name, command.description, usage, _access(command), options)


def collect(bot) -> list[tuple[str, str, str, str, list[CommandInfo]]]:
    """[(cog name, heading, summary, getting started, commands)] for every loaded module, in display order."""
    result = []
    names = sorted(bot.cogs, key=lambda n: ORDER.index(n) if n in ORDER else len(ORDER))
    for name in names:
        heading, summary, start = MODULES.get(name, (name, "", ""))
        infos = [describe(c) for c in bot.cogs[name].walk_app_commands() if isinstance(c, app_commands.Command)]
        infos = [i for i in infos if bot.tree.get_command(i.name.split(" ")[0]) is not None]   # hidden commands
        infos.sort(key=lambda i: i.name)
        if infos:
            result.append((name, heading, summary, start, infos))
    return result
