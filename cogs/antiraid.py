"""Anti-raid: join-rate detection, account-age filter, lockdown, verification gate, raid log.

Lockdown does two things:
  1. removes Send Messages / Add Reactions / Connect / thread creation from @everyone
     (one API call; the previous permissions are saved and restored on unlock), and
  2. applies `raid_action` (kick or timeout) to anyone who joins while it is active.
Channels that explicitly allow @everyone to talk through their own overrides stay open.
"""
from __future__ import annotations

import datetime
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

from core.checks import UserError, guarded, reply
from core.modlog import GREEN, RED, can_act_on, send_log
from core.ttl import SlidingCounter

from .antispam import INVITE_RE  # tiny import, shared regex

log = logging.getLogger("antiraid")

SCHEMA = """
CREATE TABLE IF NOT EXISTS raid_log (
    id       INTEGER PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    at       REAL NOT NULL,
    event    TEXT NOT NULL,
    detail   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS raid_log_guild ON raid_log (guild_id, id);
"""

LOCKED_OFF = ("send_messages", "send_messages_in_threads", "create_public_threads",
              "create_private_threads", "add_reactions", "connect")


class VerifyButton(discord.ui.DynamicItem[discord.ui.Button], template=r"antiraid:verify"):
    """Persistent button: survives restarts because Discord sends us its custom_id."""

    def __init__(self) -> None:
        super().__init__(discord.ui.Button(label="Verify", style=discord.ButtonStyle.success,
                                           custom_id="antiraid:verify", emoji="✅"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):  # noqa: ANN001
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("AntiRaid")
        if cog is not None:
            await guarded(interaction, cog.verify(interaction))


class AntiRaid(commands.Cog):
    antiraid = app_commands.Group(name="antiraid", description="Anti-raid settings", guild_only=True,
                                  default_permissions=discord.Permissions(manage_guild=True))

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        get = lambda key, default: bot.cfg.get(f"antiraid.{key}", default)  # noqa: E731
        self.join_limit = int(get("join_limit", 8))
        self.joins = SlidingCounter(float(get("join_window", 10)))
        self.min_age_hours = float(get("min_account_age_hours", 0))
        self.age_action = str(get("age_action", "kick"))
        self.raid_action = str(get("raid_action", "kick"))
        self.lockdown_minutes = float(get("lockdown_minutes", 10))
        self.new_member_minutes = float(get("new_member_minutes", 10))
        self.new_member_mentions = int(get("new_member_mention_limit", 4))
        self.new_member_action = str(get("new_member_action", "ban"))
        self.timeout_minutes = int(get("timeout_minutes", 60))
        self.locked: dict[int, bool] = {}            # guild -> lockdown active (cached from settings)

    async def cog_load(self) -> None:
        await self.bot.db.script(SCHEMA)
        self.bot.add_dynamic_items(VerifyButton)
        self.bot.scheduler.register("antiraid_unlock", self._job_unlock)

    async def cog_unload(self) -> None:
        self.bot.remove_dynamic_items(VerifyButton)
        self.bot.scheduler.unregister("antiraid_unlock")

    # ---- state ----
    async def is_locked(self, guild_id: int) -> bool:
        cached = self.locked.get(guild_id)
        if cached is None:
            cached = self.locked[guild_id] = bool(await self.bot.settings.get(guild_id, "lockdown"))
        return cached

    def record(self, guild: discord.Guild, event: str, detail: str = "") -> None:
        self.bot.db.write_later("INSERT INTO raid_log (guild_id, at, event, detail) VALUES (?, ?, ?, ?)",
                                (guild.id, time.time(), event, detail[:500]))

    async def act(self, member: discord.Member, action: str, reason: str) -> str:
        """Apply kick / timeout / ban to a member. Returns what actually happened."""
        me = member.guild.me
        blocked = can_act_on(me, member)
        if action in ("none", "flag"):
            return "flagged only"
        if blocked:
            return f"nothing ({blocked})"
        try:
            if action == "ban" and me.guild_permissions.ban_members:
                await member.ban(reason=reason, delete_message_seconds=3600)
                return "banned"
            if action == "kick" and me.guild_permissions.kick_members:
                await member.kick(reason=reason)
                return "kicked"
            if action == "timeout" and me.guild_permissions.moderate_members:
                await member.timeout(datetime.timedelta(minutes=self.timeout_minutes), reason=reason)
                return f"timed out {self.timeout_minutes} min"
            return f"nothing (I lack the permission to {action})"
        except discord.HTTPException as exc:
            return f"failed ({exc.text or exc.status})"

    # ---- lockdown ----
    async def lock(self, guild: discord.Guild, reason: str, minutes: float) -> str:
        if await self.is_locked(guild.id):
            return "Lockdown is already active."
        me = guild.me
        note = ""
        if me.guild_permissions.manage_roles:
            everyone = guild.default_role
            perms = everyone.permissions
            await self.bot.settings.set(guild.id, "lockdown_saved_perms", perms.value)
            perms.update(**{name: False for name in LOCKED_OFF})
            try:
                await everyone.edit(permissions=perms, reason=f"Lockdown: {reason}")
            except discord.HTTPException as exc:
                note = f" I could not edit @everyone ({exc.text or exc.status})."
        else:
            note = " I lack Manage Roles, so @everyone can still talk; only new joins are handled."
        await self.bot.settings.set(guild.id, "lockdown", True)
        self.locked[guild.id] = True
        await self.bot.scheduler.cancel_ref(f"unlock:{guild.id}")
        if minutes > 0:
            await self.bot.scheduler.add(time.time() + minutes * 60, "antiraid_unlock",
                                         {"guild": guild.id}, ref=f"unlock:{guild.id}")
        until = f"Auto-unlock in {minutes:.0f} min." if minutes > 0 else "Stays locked until `/unlock`."
        self.record(guild, "lockdown", reason)
        await send_log(self.bot, guild, "LOCKDOWN ACTIVE", f"{reason}\n{until}{note}", colour=RED)
        return f"Lockdown active. {until}{note}"

    async def unlock(self, guild: discord.Guild, reason: str) -> str:
        if not await self.is_locked(guild.id):
            return "The server is not locked."
        saved = await self.bot.settings.get(guild.id, "lockdown_saved_perms")
        note = ""
        if saved is not None and guild.me.guild_permissions.manage_roles:
            try:
                await guild.default_role.edit(permissions=discord.Permissions(saved), reason=f"Unlock: {reason}")
            except discord.HTTPException as exc:
                note = f" I could not restore @everyone ({exc.text or exc.status}); fix it in Server Settings > Roles."
        await self.bot.settings.delete(guild.id, "lockdown")
        await self.bot.settings.delete(guild.id, "lockdown_saved_perms")
        self.locked[guild.id] = False
        self.joins.reset(guild.id)
        await self.bot.scheduler.cancel_ref(f"unlock:{guild.id}")
        self.record(guild, "unlock", reason)
        await send_log(self.bot, guild, "Lockdown lifted", f"{reason}{note}", colour=GREEN)
        return f"Lockdown lifted.{note}"

    async def _job_unlock(self, payload: dict) -> None:
        guild = self.bot.get_guild(payload.get("guild", 0))
        if guild is not None:
            await self.unlock(guild, "Automatic unlock after the lockdown timer")

    # ---- events ----
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot:
            return
        guild = member.guild
        if await self.is_locked(guild.id):
            result = await self.act(member, self.raid_action, "Joined during lockdown")
            self.record(guild, "join-during-lockdown", f"{member} ({member.id}): {result}")
            return
        count = self.joins.hit(guild.id)
        if self.join_limit and count >= self.join_limit:
            await self.lock(guild, f"{count} joins in {self.joins.window:.0f} seconds", self.lockdown_minutes)
            result = await self.act(member, self.raid_action, "Joined during a raid")
            self.record(guild, "join-during-lockdown", f"{member} ({member.id}): {result}")
            return
        if self.min_age_hours > 0:
            age = (discord.utils.utcnow() - member.created_at).total_seconds() / 3600
            if age < self.min_age_hours:
                result = await self.act(member, self.age_action,
                                        f"Account is {age:.1f}h old (minimum {self.min_age_hours:g}h)")
                self.record(guild, "young-account", f"{member} ({member.id}), {age:.1f}h old: {result}")
                await send_log(self.bot, guild, "New account filtered",
                               f"{member.mention} (`{member.id}`) is {age:.1f} hours old.",
                               colour=RED, fields={"Action": result})

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Brand-new members who mass-mention or drop invite links are removed immediately."""
        author = message.author
        if message.guild is None or author.bot or not isinstance(author, discord.Member) or author.joined_at is None:
            return
        if (discord.utils.utcnow() - author.joined_at).total_seconds() > self.new_member_minutes * 60:
            return
        if author.guild_permissions.manage_messages:
            return
        mentions = len(message.raw_mentions) + len(message.raw_role_mentions)
        if message.mention_everyone or mentions >= self.new_member_mentions:
            what = "mass mentions"
        elif INVITE_RE.search(message.content):
            what = "an invite link"
        else:
            return
        if message.channel.permissions_for(message.guild.me).manage_messages:
            try:
                await message.delete()
            except discord.HTTPException:
                pass
        result = await self.act(author, self.new_member_action, f"New member posted {what}")
        self.record(message.guild, "new-member-spam", f"{author} ({author.id}) {what}: {result}")
        await send_log(self.bot, message.guild, "New member removed",
                       f"{author.mention} (`{author.id}`) posted {what} within "
                       f"{self.new_member_minutes:g} minutes of joining.",
                       colour=RED, fields={"Action": result, "Message": (message.content or "-")[:500]})

    # ---- verification gate ----
    async def verify(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        role = guild.get_role(await self.bot.settings.get(guild.id, "verify_role") or 0)
        if role is None:
            await reply(interaction, "Verification is not set up on this server.")
            return
        if await self.is_locked(guild.id):
            await reply(interaction, "The server is in lockdown. Try again later.")
            return
        member = interaction.user
        if role in member.roles:
            await reply(interaction, "You are already verified.")
            return
        if not guild.me.guild_permissions.manage_roles or role >= guild.me.top_role:
            await reply(interaction, "I cannot give that role. An admin must move my role above it.")
            return
        try:
            await member.add_roles(role, reason="Passed the verification button")
        except discord.HTTPException:
            await reply(interaction, "Something went wrong giving you the role. Tell a moderator.")
            return
        await reply(interaction, "You are verified. Welcome!")

    # ---- commands ----
    @app_commands.command(description="PANIC: lock the server now. Stays locked until /unlock.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def panic(self, interaction: discord.Interaction, reason: str = "Manual panic mode") -> None:
        await interaction.response.defer(ephemeral=True)
        text = await self.lock(interaction.guild, f"{reason} (by {interaction.user})", 0)
        await interaction.followup.send(text, ephemeral=True)

    @app_commands.command(name="unlock", description="Lift the lockdown and restore normal permissions.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def unlock_server(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        text = await self.unlock(interaction.guild, f"Unlocked by {interaction.user}")
        await interaction.followup.send(text, ephemeral=True)

    @antiraid.command(name="status", description="Show limits, lockdown state and the recent raid log.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def status(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        rows = await self.bot.db.fetchall(
            "SELECT at, event, detail FROM raid_log WHERE guild_id = ? ORDER BY id DESC LIMIT 10", (guild.id,))
        role_id = await self.bot.settings.get(guild.id, "verify_role")
        lines = [
            f"**Lockdown:** {'ACTIVE' if await self.is_locked(guild.id) else 'off'}",
            f"Raid trigger: {self.join_limit} joins in {self.joins.window:.0f}s "
            f"(now {self.joins.count(guild.id)}) · joiners during lockdown: {self.raid_action}",
            f"Auto-unlock: {f'{self.lockdown_minutes:g} min' if self.lockdown_minutes > 0 else 'manual'}",
            f"Minimum account age: {f'{self.min_age_hours:g}h ({self.age_action})' if self.min_age_hours > 0 else 'off'}",
            f"New members ({self.new_member_minutes:g} min): {self.new_member_mentions}+ mentions or an invite = {self.new_member_action}",
            f"Verification role: {f'<@&{role_id}>' if role_id else 'not set'}",
            "", "**Raid log**",
        ]
        lines += [f"<t:{int(r['at'])}:R> **{r['event']}** {r['detail']}" for r in rows] or ["empty"]
        await interaction.response.send_message("\n".join(lines)[:2000], ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    @antiraid.command(name="verify-setup", description="Post a Verify button that gives members a role.")
    @app_commands.describe(role="Role given on verification", channel="Where to post the button",
                           text="Message shown above the button")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def verify_setup(self, interaction: discord.Interaction, role: discord.Role,
                           channel: discord.TextChannel | None = None,
                           text: str = "Press the button to get access to the server.") -> None:
        channel = channel or interaction.channel
        me = interaction.guild.me
        if role.is_default() or role.managed:
            raise UserError("Pick a normal role, not @everyone or a bot's role.")
        if not me.guild_permissions.manage_roles or role >= me.top_role:
            raise UserError(f"I cannot hand out {role.mention}. Give me Manage Roles and drag my role above it.")
        perms = channel.permissions_for(me)
        if not (perms.view_channel and perms.send_messages):
            raise UserError(f"I cannot send messages in {channel.mention}.")
        view = discord.ui.View(timeout=None)
        view.add_item(VerifyButton())
        await channel.send(text[:2000], view=view, allowed_mentions=discord.AllowedMentions.none())
        await self.bot.settings.set(interaction.guild_id, "verify_role", role.id)
        await interaction.response.send_message(
            f"Verification button posted in {channel.mention}. It gives {role.mention}.\n"
            "To make it a real gate, hide your other channels from @everyone and allow them for that role.",
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @antiraid.command(name="verify-off", description="Stop the Verify button from giving a role.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def verify_off(self, interaction: discord.Interaction) -> None:
        await self.bot.settings.delete(interaction.guild_id, "verify_role")
        await interaction.response.send_message("Verification switched off.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AntiRaid(bot))
