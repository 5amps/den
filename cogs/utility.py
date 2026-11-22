"""Utilities: welcome/leave messages, auto-role, reaction roles, message logs, /help."""
from __future__ import annotations

import logging
import re

import discord
from discord import app_commands
from discord.ext import commands

from core.checks import UserError
from core.modlog import GREEN, GREY, ORANGE, send_log

log = logging.getLogger("utility")

SCHEMA = """
CREATE TABLE IF NOT EXISTS reaction_roles (
    guild_id   INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    emoji      TEXT NOT NULL,
    role_id    INTEGER NOT NULL,
    PRIMARY KEY (message_id, emoji)
) WITHOUT ROWID;
"""

LINK_RE = re.compile(r"channels/(\d+)/(\d+)/(\d+)")
DEFAULT_WELCOME = "Welcome {user} to **{server}**! You are member #{count}."
DEFAULT_LEAVE = "**{name}** left the server."


def emoji_key(emoji: discord.PartialEmoji) -> str:
    """Custom emoji are keyed by ID (names can change); standard emoji by their character."""
    return str(emoji.id) if emoji.id else emoji.name


def fill(template: str, *, mention: str, name: str, guild: discord.Guild) -> str:
    return (template.replace("{user}", mention).replace("{name}", discord.utils.escape_markdown(name))
            .replace("{server}", guild.name).replace("{count}", str(guild.member_count or "?")))[:2000]


class Utility(commands.Cog):
    welcome = app_commands.Group(name="welcome", description="Welcome and leave messages", guild_only=True,
                                 default_permissions=discord.Permissions(manage_guild=True))
    reactionrole = app_commands.Group(name="reactionrole", description="Roles given by reacting to a message",
                                      guild_only=True, default_permissions=discord.Permissions(manage_roles=True))

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.rr: dict[int, dict[str, int]] = {}          # message id -> {emoji key: role id}

    async def cog_load(self) -> None:
        await self.bot.db.script(SCHEMA)
        for row in await self.bot.db.fetchall("SELECT message_id, emoji, role_id FROM reaction_roles"):
            self.rr.setdefault(row["message_id"], {})[row["emoji"]] = row["role_id"]

    # ---- joins and leaves ----
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        guild = member.guild
        raid = self.bot.get_cog("AntiRaid")
        if raid is not None and await raid.is_locked(guild.id):
            return                                          # no greeting or role during a raid
        settings = self.bot.settings
        role = guild.get_role(await settings.get(guild.id, "autorole") or 0)
        if role is not None and not member.bot:
            if guild.me.guild_permissions.manage_roles and role < guild.me.top_role:
                try:
                    await member.add_roles(role, reason="Auto-role")
                except discord.HTTPException as exc:
                    log.warning("Auto-role failed in %s: %s", guild.name, exc)
            else:
                log.warning("Auto-role in %s skipped: my role must be above %s", guild.name, role.name)
        channel = guild.get_channel(await settings.get(guild.id, "welcome_channel") or 0)
        if channel is not None and channel.permissions_for(guild.me).send_messages:
            text = fill(await settings.get(guild.id, "welcome_text", DEFAULT_WELCOME),
                        mention=member.mention, name=member.name, guild=guild)
            try:
                await channel.send(text, allowed_mentions=discord.AllowedMentions(users=[member]))
            except discord.HTTPException:
                pass

    @commands.Cog.listener()
    async def on_raw_member_remove(self, payload: discord.RawMemberRemoveEvent) -> None:
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None or payload.user.id == self.bot.user.id:
            return
        channel = guild.get_channel(await self.bot.settings.get(guild.id, "leave_channel") or 0)
        if channel is not None and channel.permissions_for(guild.me).send_messages:
            text = fill(await self.bot.settings.get(guild.id, "leave_text", DEFAULT_LEAVE),
                        mention=payload.user.mention, name=payload.user.name, guild=guild)
            try:
                await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                pass

    # ---- reaction roles ----
    async def _reaction(self, payload: discord.RawReactionActionEvent, add: bool) -> None:
        mapping = self.rr.get(payload.message_id)
        if not mapping or payload.user_id == self.bot.user.id or payload.guild_id is None:
            return
        role_id = mapping.get(emoji_key(payload.emoji))
        guild = self.bot.get_guild(payload.guild_id)
        role = guild.get_role(role_id) if guild and role_id else None
        if role is None or not guild.me.guild_permissions.manage_roles or role >= guild.me.top_role:
            return
        try:
            member = payload.member or guild.get_member(payload.user_id) or await guild.fetch_member(payload.user_id)
            if member.bot:
                return
            if add:
                await member.add_roles(role, reason="Reaction role")
            else:
                await member.remove_roles(role, reason="Reaction role removed")
        except discord.HTTPException as exc:
            log.warning("Reaction role failed in %s: %s", guild.name, exc)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        await self._reaction(payload, True)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        await self._reaction(payload, False)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        if payload.message_id in self.rr:                   # panel deleted: forget its reaction roles
            self.rr.pop(payload.message_id, None)
            await self.bot.db.execute("DELETE FROM reaction_roles WHERE message_id = ?", (payload.message_id,))
        message = payload.cached_message
        guild = self.bot.get_guild(payload.guild_id) if payload.guild_id else None
        if guild is None or message is None or message.author.bot:
            return                                          # only messages still in the small cache are logged
        if not bool(self.bot.cfg.get("utility.log_deleted_messages", True)):
            return
        extra = f"\n{len(message.attachments)} attachment(s)" if message.attachments else ""
        await send_log(self.bot, guild, "Message deleted",
                       f"By {message.author.mention} in <#{payload.channel_id}>\n\n{message.content[:1800]}{extra}",
                       colour=GREY)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if before.guild is None or before.author.bot or before.content == after.content:
            return
        if not bool(self.bot.cfg.get("utility.log_edited_messages", True)):
            return
        await send_log(self.bot, before.guild, "Message edited",
                       f"By {before.author.mention} in {before.channel.mention} · [jump]({after.jump_url})",
                       colour=ORANGE, fields={"Before": before.content[:1000] or "-", "After": after.content[:1000] or "-"})

    # ---- commands: welcome / leave / autorole ----
    @welcome.command(name="set", description="Greet new members in a channel.")
    @app_commands.describe(channel="Where to post", text="Use {user} {name} {server} {count}. Empty = default text")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def welcome_set(self, interaction: discord.Interaction, channel: discord.TextChannel,
                          text: app_commands.Range[str, 1, 1500] | None = None) -> None:
        if not channel.permissions_for(interaction.guild.me).send_messages:
            raise UserError(f"I cannot send messages in {channel.mention}.")
        await self.bot.settings.set(interaction.guild_id, "welcome_channel", channel.id)
        await self.bot.settings.set(interaction.guild_id, "welcome_text", text or DEFAULT_WELCOME)
        preview = fill(text or DEFAULT_WELCOME, mention=interaction.user.mention, name=interaction.user.name,
                       guild=interaction.guild)
        await interaction.response.send_message(f"Welcome messages go to {channel.mention}. Preview:\n{preview}",
                                                ephemeral=True)

    @welcome.command(name="leave", description="Announce when a member leaves.")
    @app_commands.describe(channel="Where to post", text="Use {name} {server} {count}. Empty = default text")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def leave_set(self, interaction: discord.Interaction, channel: discord.TextChannel,
                        text: app_commands.Range[str, 1, 1500] | None = None) -> None:
        if not channel.permissions_for(interaction.guild.me).send_messages:
            raise UserError(f"I cannot send messages in {channel.mention}.")
        await self.bot.settings.set(interaction.guild_id, "leave_channel", channel.id)
        await self.bot.settings.set(interaction.guild_id, "leave_text", text or DEFAULT_LEAVE)
        await interaction.response.send_message(f"Leave messages go to {channel.mention}.", ephemeral=True)

    @welcome.command(name="off", description="Switch off welcome and leave messages.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def welcome_off(self, interaction: discord.Interaction) -> None:
        for key in ("welcome_channel", "welcome_text", "leave_channel", "leave_text"):
            await self.bot.settings.delete(interaction.guild_id, key)
        await interaction.response.send_message("Welcome and leave messages are off.", ephemeral=True)

    @app_commands.command(description="Give every new member a role automatically. No role = switch off.")
    @app_commands.default_permissions(manage_roles=True)
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_roles=True)
    async def autorole(self, interaction: discord.Interaction, role: discord.Role | None = None) -> None:
        if role is None:
            await self.bot.settings.delete(interaction.guild_id, "autorole")
            await interaction.response.send_message("Auto-role is off.", ephemeral=True)
            return
        self._check_role(interaction, role)
        await self.bot.settings.set(interaction.guild_id, "autorole", role.id)
        await interaction.response.send_message(f"New members will get {role.mention}.", ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    @staticmethod
    def _check_role(interaction: discord.Interaction, role: discord.Role) -> None:
        me = interaction.guild.me
        if role.is_default() or role.managed:
            raise UserError("Pick a normal role, not @everyone or a bot's own role.")
        if not me.guild_permissions.manage_roles or role >= me.top_role:
            raise UserError(f"I cannot hand out {role.mention}. Give me Manage Roles and drag my role above it.")
        if interaction.user.id != interaction.guild.owner_id and role >= interaction.user.top_role:
            raise UserError("You can only use roles below your own highest role.")

    # ---- commands: reaction roles ----
    async def _locate(self, interaction: discord.Interaction, link: str) -> discord.PartialMessage:
        match = LINK_RE.search(link)
        if not match or int(match[1]) != interaction.guild_id:
            raise UserError("Give me a message link from this server (right-click the message > Copy Message Link).")
        channel = interaction.guild.get_channel_or_thread(int(match[2]))
        if channel is None:
            raise UserError("I cannot see the channel that message is in.")
        return channel.get_partial_message(int(match[3]))

    @reactionrole.command(name="add", description="Reacting with an emoji on a message gives a role.")
    @app_commands.describe(message_link="Right-click the message > Copy Message Link", emoji="The emoji to react with")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def rr_add(self, interaction: discord.Interaction, message_link: str, emoji: str, role: discord.Role) -> None:
        self._check_role(interaction, role)
        message = await self._locate(interaction, message_link)
        partial = discord.PartialEmoji.from_str(emoji.strip())
        try:
            await message.add_reaction(partial)
        except discord.NotFound as exc:
            raise UserError("I could not find that message.") from exc
        except discord.HTTPException as exc:
            raise UserError("I could not react with that emoji. Use a standard emoji or one from this server, "
                            "and check I have Add Reactions and Read Message History there.") from exc
        key = emoji_key(partial)
        await self.bot.db.execute(
            "INSERT OR REPLACE INTO reaction_roles (guild_id, channel_id, message_id, emoji, role_id) VALUES (?, ?, ?, ?, ?)",
            (interaction.guild_id, message.channel.id, message.id, key, role.id))
        self.rr.setdefault(message.id, {})[key] = role.id
        await interaction.response.send_message(f"Reacting with {emoji} on that message now gives {role.mention}.",
                                                ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @reactionrole.command(name="remove", description="Stop an emoji on a message from giving a role.")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def rr_remove(self, interaction: discord.Interaction, message_link: str, emoji: str) -> None:
        message = await self._locate(interaction, message_link)
        key = emoji_key(discord.PartialEmoji.from_str(emoji.strip()))
        if self.rr.get(message.id, {}).pop(key, None) is None:
            raise UserError("That emoji is not a reaction role on that message.")
        if not self.rr.get(message.id):
            self.rr.pop(message.id, None)
        await self.bot.db.execute("DELETE FROM reaction_roles WHERE message_id = ? AND emoji = ?", (message.id, key))
        await interaction.response.send_message("Reaction role removed.", ephemeral=True)

    @reactionrole.command(name="list", description="List this server's reaction roles.")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def rr_list(self, interaction: discord.Interaction) -> None:
        rows = await self.bot.db.fetchall(
            "SELECT channel_id, message_id, emoji, role_id FROM reaction_roles WHERE guild_id = ? LIMIT 40",
            (interaction.guild_id,))
        if not rows:
            raise UserError("No reaction roles are set up.")
        lines = []
        for r in rows:
            shown = f"<:e:{r['emoji']}>" if r["emoji"].isdigit() else r["emoji"]
            link = f"https://discord.com/channels/{interaction.guild_id}/{r['channel_id']}/{r['message_id']}"
            lines.append(f"{shown} gives <@&{r['role_id']}> on [this message]({link})")
        await interaction.response.send_message("\n".join(lines)[:2000], ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    # ---- help ----
    @app_commands.command(description="List the bot's commands.")
    async def help(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(title="Commands", colour=GREEN,
                              description="Commands you lack permission for may be hidden by Discord.")
        for name, cog in sorted(self.bot.cogs.items()):
            parts = []
            for command in cog.walk_app_commands():
                if isinstance(command, app_commands.Group):
                    continue
                parts.append(f"`/{command.qualified_name}`")
            if parts:
                embed.add_field(name=name, value=" ".join(parts)[:1024], inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Utility(bot))
