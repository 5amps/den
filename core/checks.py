"""Shared permission checks, safe replies and human-readable error messages."""
from __future__ import annotations

import errno
import logging
import sqlite3

import discord
from discord import app_commands

from .proc import ProcessError

log = logging.getLogger("checks")


class UserError(app_commands.CheckFailure):
    """An expected problem whose message is shown to the user as-is (not logged as a bug)."""


async def reply(interaction: discord.Interaction, content: str | None = None, *,
                ephemeral: bool = True, **kwargs) -> None:
    """Answer an interaction whether or not it was already acknowledged. Never raises."""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=ephemeral, **kwargs)
        else:
            await interaction.response.send_message(content, ephemeral=ephemeral, **kwargs)
    except discord.HTTPException as exc:
        log.warning("Could not reply to interaction: %s", exc)


async def is_staff(interaction: discord.Interaction) -> bool:
    """True for members with Manage Server, or any role saved under the 'staff_roles' setting."""
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        return False
    if interaction.permissions.manage_guild or interaction.permissions.administrator:
        return True
    staff_roles = await interaction.client.settings.get(interaction.guild.id, "staff_roles", [])  # type: ignore[attr-defined]
    if not staff_roles:
        return False
    wanted = set(staff_roles)
    return any(role.id in wanted for role in interaction.user.roles)


def staff_only():
    """Slash-command check: Manage Server permission or a configured staff role."""

    async def predicate(interaction: discord.Interaction) -> bool:
        if await is_staff(interaction):
            return True
        raise app_commands.CheckFailure("This command is for server staff.")

    return app_commands.check(predicate)


def owner_only():
    """Slash-command check: only the owner of the bot application."""

    async def predicate(interaction: discord.Interaction) -> bool:
        if await interaction.client.is_owner(interaction.user):  # type: ignore[attr-defined]
            return True
        raise app_commands.CheckFailure("Only the bot owner can use this command.")

    return app_commands.check(predicate)


def user_may_post(interaction: discord.Interaction, *extra: str) -> None:
    """Refuse when the person could not post this themselves. Otherwise a member muted in a
    channel (or in a read-only announcements channel) could make the bot post there for them."""
    perms = interaction.permissions
    needed = ("send_messages", *extra)
    missing = [n for n in needed if not getattr(perms, n, False)]
    if missing:
        raise UserError("You cannot post that in this channel yourself, so I will not post it for you "
                        f"(missing: {', '.join(m.replace('_', ' ').title() for m in missing)}).")


def missing_permissions(channel: discord.abc.GuildChannel | discord.Thread, *names: str) -> list[str]:
    """Which of the named permissions the bot lacks in a channel (empty list = all good)."""
    perms = channel.permissions_for(channel.guild.me)
    return [name.replace("_", " ").title() for name in names if not getattr(perms, name, False)]


def _pretty(perms: list[str]) -> str:
    return ", ".join(p.replace("_", " ").title() for p in perms)


def describe_error(error: BaseException) -> tuple[str, bool]:
    """Turn an exception into (message for the user, whether it should be logged as a bug)."""
    if isinstance(error, app_commands.CommandInvokeError):
        error = error.original

    if isinstance(error, app_commands.MissingPermissions):
        return f"You need these permissions: {_pretty(error.missing_permissions)}.", False
    if isinstance(error, app_commands.BotMissingPermissions):
        return f"I am missing these permissions: {_pretty(error.missing_permissions)}.", False
    if isinstance(error, app_commands.CommandOnCooldown):
        return f"Slow down. Try again in {error.retry_after:.0f}s.", False
    if isinstance(error, app_commands.NoPrivateMessage):
        return "This command only works inside a server.", False
    if isinstance(error, app_commands.CheckFailure):
        return str(error) or "You are not allowed to use this command.", False
    if isinstance(error, app_commands.TransformerError):
        return "One of the values you entered is not valid.", False
    if isinstance(error, discord.Forbidden):
        return ("Discord refused that action. Check that my role has the needed permission "
                "and sits above the role or member I am acting on."), False
    if isinstance(error, discord.NotFound):
        return "That message, channel or member no longer exists.", False
    if isinstance(error, discord.HTTPException):
        if error.status == 429:
            return "Discord is rate limiting me. Try again in a moment.", False
        if error.status >= 500:
            return "Discord is having problems right now. Try again shortly.", False
        return f"Discord rejected the request: {error.text or error.status}", True
    if isinstance(error, ProcessError):
        return str(error), False
    if isinstance(error, sqlite3.OperationalError) and "full" in str(error).lower():
        return "The disk is full, so nothing could be saved. Free some space.", True
    if isinstance(error, OSError) and error.errno == errno.ENOSPC:
        return "The disk is full. Free some space and try again.", True
    return "Something went wrong. The error was written to the log.", True


async def guarded(interaction: discord.Interaction, coro) -> None:
    """Run a button/menu handler; show any failure to the user the same way slash commands do.

    Component callbacks do not pass through the command tree's error handler, so without
    this a failed button press would only show Discord's generic "interaction failed".
    """
    try:
        await coro
    except Exception as exc:  # noqa: BLE001
        message, is_bug = describe_error(exc)
        if is_bug:
            log.error("Component handler failed", exc_info=exc)
        await reply(interaction, message)
