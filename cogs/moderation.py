"""Moderation: warn, timeout, kick, ban, unban, purge, slowmode, and a searchable history.

Every action is written to the local database (mod_actions) and to the log channel.
"""
from __future__ import annotations

import datetime
import logging
import re
import time

import discord
from discord import app_commands
from discord.ext import commands

from core.checks import UserError
from core.modlog import GREEN, ORANGE, RED, can_act_on, send_log

log = logging.getLogger("moderation")

SCHEMA = """
CREATE TABLE IF NOT EXISTS mod_actions (
    id       INTEGER PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    mod_id   INTEGER NOT NULL,
    action   TEXT NOT NULL,
    reason   TEXT NOT NULL DEFAULT '',
    at       REAL NOT NULL,
    active   INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS mod_actions_user ON mod_actions (guild_id, user_id, id);
"""

DURATION_RE = re.compile(r"^(?:(\d+)\s*d)?\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m(?:in)?)?$", re.I)


def parse_duration(text: str) -> datetime.timedelta:
    """'10m', '2h', '1d 12h' -> timedelta (Discord allows timeouts up to 28 days)."""
    match = DURATION_RE.match(text.strip())
    if not match or not any(match.groups()):
        raise UserError("Duration must look like `10m`, `2h` or `1d 12h`.")
    days, hours, minutes = (int(g or 0) for g in match.groups())
    delta = datetime.timedelta(days=days, hours=hours, minutes=minutes)
    if not datetime.timedelta(minutes=1) <= delta <= datetime.timedelta(days=28):
        raise UserError("A timeout must be between 1 minute and 28 days.")
    return delta


class Moderation(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self) -> None:
        await self.bot.db.script(SCHEMA)

    # ---- helpers ----
    def _guard(self, interaction: discord.Interaction, member: discord.Member) -> None:
        """Refuse actions Discord would reject anyway, with a clear reason."""
        if not isinstance(member, discord.Member):
            raise UserError("That user is not in this server.")
        if member.id == self.bot.user.id:
            raise UserError("I will not do that to myself.")
        for actor in (interaction.user, interaction.guild.me):
            problem = can_act_on(actor, member)
            if problem:
                raise UserError(f"That is not possible: {problem}.")

    async def _record(self, interaction: discord.Interaction, user: discord.abc.User, action: str,
                      reason: str, colour: discord.Colour, extra: dict[str, str] | None = None) -> int:
        case = await self.bot.db.execute(
            "INSERT INTO mod_actions (guild_id, user_id, mod_id, action, reason, at) VALUES (?, ?, ?, ?, ?, ?)",
            (interaction.guild_id, user.id, interaction.user.id, action, reason[:500], time.time()))
        fields = {"Moderator": interaction.user.mention, "Reason": reason or "-"}
        fields.update(extra or {})
        await send_log(self.bot, interaction.guild, f"{action.title()} · case #{case}",
                       f"{user.mention} (`{user.id}`)", colour=colour, fields=fields)
        return case

    @staticmethod
    async def _dm(user: discord.abc.User, text: str) -> None:
        try:
            await user.send(text)
        except discord.HTTPException:
            pass                                            # DMs closed

    # ---- commands ----
    @app_commands.command(description="Warn a member. The warning is saved and they are told by DM.")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(moderate_members=True)
    async def warn(self, interaction: discord.Interaction, member: discord.Member,
                   reason: app_commands.Range[str, 1, 500]) -> None:
        self._guard(interaction, member)
        case = await self._record(interaction, member, "warn", reason, ORANGE)
        total = await self.bot.db.fetchone(
            "SELECT COUNT(*) AS n FROM mod_actions WHERE guild_id = ? AND user_id = ? AND action = 'warn' AND active = 1",
            (interaction.guild_id, member.id))
        await self._dm(member, f"You were warned in **{interaction.guild.name}**: {reason}")
        await interaction.response.send_message(
            f"Warned {member.mention} (case #{case}). They now have {total['n']} active warning(s).",
            allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(description="Show a member's warnings and past moderation actions.")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(moderate_members=True)
    async def history(self, interaction: discord.Interaction, user: discord.User) -> None:
        rows = await self.bot.db.fetchall(
            "SELECT id, mod_id, action, reason, at, active FROM mod_actions "
            "WHERE guild_id = ? AND user_id = ? ORDER BY id DESC LIMIT 15", (interaction.guild_id, user.id))
        if not rows:
            raise UserError(f"{user} has a clean record here.")
        lines = [f"**History for {user}** (latest 15)"]
        for r in rows:
            cleared = "" if r["active"] else " ~cleared~"
            lines.append(f"`#{r['id']}` **{r['action']}**{cleared} <t:{int(r['at'])}:d> by <@{r['mod_id']}>: {r['reason'] or '-'}")
        await interaction.response.send_message("\n".join(lines)[:2000], ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(description="Clear a member's active warnings (the history is kept).")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(moderate_members=True)
    async def clearwarnings(self, interaction: discord.Interaction, user: discord.User) -> None:
        count = await self.bot.db.execute(
            "UPDATE mod_actions SET active = 0 WHERE guild_id = ? AND user_id = ? AND action = 'warn' AND active = 1",
            (interaction.guild_id, user.id))
        await interaction.response.send_message(f"Cleared {count} warning(s) for {user.mention}.",
                                                allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(description="Temporarily stop a member from talking.")
    @app_commands.describe(duration="For example 10m, 2h or 1d 12h (max 28d)")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.checks.bot_has_permissions(moderate_members=True)
    async def timeout(self, interaction: discord.Interaction, member: discord.Member, duration: str,
                      reason: app_commands.Range[str, 0, 500] = "") -> None:
        self._guard(interaction, member)
        delta = parse_duration(duration)
        if member.guild_permissions.administrator:
            raise UserError("Discord does not allow timing out administrators.")
        await member.timeout(delta, reason=f"{interaction.user}: {reason}"[:500])
        until = int((discord.utils.utcnow() + delta).timestamp())
        case = await self._record(interaction, member, "timeout", reason, ORANGE, {"Until": f"<t:{until}:f>"})
        await interaction.response.send_message(f"{member.mention} is timed out until <t:{until}:f> (case #{case}).",
                                                allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(description="Remove a member's timeout.")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.checks.bot_has_permissions(moderate_members=True)
    async def untimeout(self, interaction: discord.Interaction, member: discord.Member) -> None:
        self._guard(interaction, member)
        await member.timeout(None, reason=f"Timeout removed by {interaction.user}")
        await self._record(interaction, member, "untimeout", "", GREEN)
        await interaction.response.send_message(f"Timeout removed for {member.mention}.",
                                                allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(description="Kick a member from the server.")
    @app_commands.default_permissions(kick_members=True)
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(kick_members=True)
    @app_commands.checks.bot_has_permissions(kick_members=True)
    async def kick(self, interaction: discord.Interaction, member: discord.Member,
                   reason: app_commands.Range[str, 0, 500] = "") -> None:
        self._guard(interaction, member)
        await self._dm(member, f"You were kicked from **{interaction.guild.name}**. {reason}".strip())
        await member.kick(reason=f"{interaction.user}: {reason}"[:500])
        case = await self._record(interaction, member, "kick", reason, RED)
        await interaction.response.send_message(f"Kicked **{member}** (case #{case}).")

    @app_commands.command(description="Ban a user. Works on people who already left, by ID.")
    @app_commands.describe(delete_hours="Also delete their messages from the last N hours (0-168)")
    @app_commands.default_permissions(ban_members=True)
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(ban_members=True)
    @app_commands.checks.bot_has_permissions(ban_members=True)
    async def ban(self, interaction: discord.Interaction, user: discord.User,
                  reason: app_commands.Range[str, 0, 500] = "",
                  delete_hours: app_commands.Range[int, 0, 168] = 0) -> None:
        if isinstance(user, discord.Member):
            self._guard(interaction, user)
            await self._dm(user, f"You were banned from **{interaction.guild.name}**. {reason}".strip())
        elif user.id == self.bot.user.id:
            raise UserError("I will not do that to myself.")
        await interaction.guild.ban(user, reason=f"{interaction.user}: {reason}"[:500],
                                    delete_message_seconds=delete_hours * 3600)
        case = await self._record(interaction, user, "ban", reason, RED)
        await interaction.response.send_message(f"Banned **{user}** (case #{case}).")

    @app_commands.command(description="Unban a user by their ID.")
    @app_commands.describe(user_id="Right-click the user > Copy User ID, or take it from /history or the log")
    @app_commands.default_permissions(ban_members=True)
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(ban_members=True)
    @app_commands.checks.bot_has_permissions(ban_members=True)
    async def unban(self, interaction: discord.Interaction, user_id: str,
                    reason: app_commands.Range[str, 0, 500] = "") -> None:
        if not user_id.strip().isdigit():
            raise UserError("That is not a user ID. It should be a long number.")
        target = discord.Object(id=int(user_id.strip()))
        try:
            await interaction.guild.unban(target, reason=f"{interaction.user}: {reason}"[:500])
        except discord.NotFound as exc:
            raise UserError("That user is not banned.") from exc
        user = self.bot.get_user(target.id) or await self.bot.fetch_user(target.id)
        case = await self._record(interaction, user, "unban", reason, GREEN)
        await interaction.response.send_message(f"Unbanned **{user}** (case #{case}).")

    @app_commands.command(description="Delete recent messages in this channel.")
    @app_commands.describe(amount="How many recent messages to look through (max 500)",
                           member="Only delete messages from this member",
                           contains="Only delete messages containing this text")
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.checks.bot_has_permissions(manage_messages=True, read_message_history=True)
    async def purge(self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 500],
                    member: discord.Member | None = None, contains: str | None = None) -> None:
        await interaction.response.defer(ephemeral=True)
        needle = contains.lower() if contains else None

        def wanted(message: discord.Message) -> bool:
            if message.pinned:
                return False
            if member is not None and message.author.id != member.id:
                return False
            return needle is None or needle in message.content.lower()

        # Messages older than 14 days cannot be bulk-deleted; skip them instead of deleting one by one.
        cutoff = discord.utils.utcnow() - datetime.timedelta(days=14)
        deleted = await interaction.channel.purge(limit=amount, check=wanted, after=cutoff, oldest_first=False,
                                                  reason=f"Purge by {interaction.user}")
        await interaction.followup.send(f"Deleted {len(deleted)} message(s). Pinned messages and anything "
                                        "older than 14 days are left alone.", ephemeral=True)
        await self._record(interaction, member or interaction.user, "purge",
                           f"{len(deleted)} message(s) in #{interaction.channel}", ORANGE)

    @app_commands.command(description="Set slow mode for this channel.")
    @app_commands.describe(seconds="Seconds between messages per person (0 = off, max 21600)")
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def slowmode(self, interaction: discord.Interaction, seconds: app_commands.Range[int, 0, 21600]) -> None:
        await interaction.channel.edit(slowmode_delay=seconds, reason=f"Slow mode by {interaction.user}")
        await interaction.response.send_message("Slow mode is off." if seconds == 0 else f"Slow mode: {seconds}s.")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Moderation(bot))
