"""Anti-spam: rate, duplicates, mentions, emoji, caps, invites and links, with escalation.

All counters live in memory with expiry (core/ttl.py). The message hot path does no
database reads and no network calls unless a rule is actually broken.
"""
from __future__ import annotations

import datetime
import logging
import re
import time
import zlib

import discord
from discord import app_commands
from discord.ext import commands

from core.modlog import ORANGE, can_act_on, send_log
from core.ttl import SlidingCounter, TTLDict

log = logging.getLogger("antispam")

SCHEMA = """
CREATE TABLE IF NOT EXISTS spam_log (
    id       INTEGER PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    rule     TEXT NOT NULL,
    action   TEXT NOT NULL,
    at       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS spam_log_guild ON spam_log (guild_id, at);
"""

INVITE_RE = re.compile(r"(?:discord\.gg|discord(?:app)?\.com/invite|discord\.me|dsc\.gg)/[\w-]{2,}", re.I)
URL_RE = re.compile(r"https?://([^\s/<>\"']+)", re.I)
CUSTOM_EMOJI_RE = re.compile(r"<a?:\w{2,32}:\d{15,22}>")
UNICODE_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002B00-\U00002BFF\U0001F1E6-\U0001F1FF]")

RULE_TEXT = {
    "rate": "sending messages too fast",
    "duplicate": "repeating the same message",
    "mentions": "mass mentions",
    "emoji": "too many emoji",
    "caps": "too many capital letters",
    "invite": "posting invite links",
    "link": "posting links",
}
ACTIONS = ("warn", "timeout", "kick", "ban")


class AntiSpam(commands.Cog):
    antispam = app_commands.Group(name="antispam", description="Anti-spam settings", guild_only=True,
                                  default_permissions=discord.Permissions(manage_guild=True))

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        get = lambda key, default: bot.cfg.get(f"antispam.{key}", default)  # noqa: E731
        self.msg_limit = int(get("message_limit", 6))
        self.dup_limit = int(get("duplicate_limit", 4))
        self.mention_limit = int(get("mention_limit", 6))
        self.emoji_limit = int(get("emoji_limit", 15))
        self.caps_percent = int(get("caps_percent", 80))
        self.caps_min = int(get("caps_min_length", 20))
        self.block_invites = bool(get("block_invites", True))
        self.block_links = bool(get("block_links", False))
        self.allowed_domains = {str(d).lower().removeprefix("www.") for d in get("allowed_domains", [])}
        self.delete = bool(get("delete_messages", True))
        self.timeout_minutes = int(get("timeout_minutes", 10))
        self.escalation = [a for a in get("escalation", ["warn", "timeout", "timeout", "kick", "ban"]) if a in ACTIONS] or ["warn"]
        self.rate = SlidingCounter(float(get("message_window", 8)))
        self.dups = SlidingCounter(float(get("duplicate_window", 30)))
        self.strikes = SlidingCounter(float(get("strike_decay_minutes", 60)) * 60)
        self.cooldown = TTLDict(5.0)          # one punishment per burst, not one per message
        self.ignore: dict[int, tuple[frozenset[int], frozenset[int]]] = {}   # guild -> (roles, channels)

    async def cog_load(self) -> None:
        await self.bot.db.script(SCHEMA)

    # ---- rule evaluation (pure, no I/O) ----
    def check(self, guild_id: int, user_id: int, content: str, mentions: int, everyone: bool,
              now: float | None = None) -> str | None:
        """Return the name of the first rule this message breaks, or None."""
        key = (guild_id, user_id)
        if self.msg_limit and self.rate.hit(key, now) > self.msg_limit:
            return "rate"
        if self.mention_limit and (mentions >= self.mention_limit or (everyone and mentions >= 1)):
            return "mentions"
        if not content:
            return None
        if self.dup_limit and len(content) >= 4:
            digest = zlib.crc32(content.lower().encode("utf-8", "ignore"))
            if self.dups.hit((guild_id, user_id, digest), now) >= self.dup_limit:
                return "duplicate"
        if self.block_invites and INVITE_RE.search(content):
            return "invite"
        if self.block_links:
            for host in URL_RE.findall(content):
                host = host.lower().split(":")[0].removeprefix("www.")
                if not any(host == d or host.endswith("." + d) for d in self.allowed_domains):
                    return "link"
        if self.emoji_limit and len(content) >= self.emoji_limit:
            emoji = len(CUSTOM_EMOJI_RE.findall(content)) + len(UNICODE_EMOJI_RE.findall(content))
            if emoji > self.emoji_limit:
                return "emoji"
        if self.caps_percent and len(content) >= self.caps_min:
            letters = [c for c in content if c.isalpha()]
            if len(letters) >= self.caps_min and sum(c.isupper() for c in letters) * 100 >= self.caps_percent * len(letters):
                return "caps"
        return None

    async def ignored(self, guild_id: int) -> tuple[frozenset[int], frozenset[int]]:
        cached = self.ignore.get(guild_id)
        if cached is None:
            roles = await self.bot.settings.get(guild_id, "antispam_ignored_roles", [])
            channels = await self.bot.settings.get(guild_id, "antispam_ignored_channels", [])
            cached = self.ignore[guild_id] = (frozenset(roles), frozenset(channels))
        return cached

    # ---- hot path ----
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot or message.webhook_id is not None:
            return
        author = message.author
        if not isinstance(author, discord.Member):
            return
        if author.guild_permissions.manage_messages:
            return                                          # staff are never filtered
        roles, channels = await self.ignored(message.guild.id)
        if message.channel.id in channels or getattr(message.channel, "parent_id", None) in channels:
            return
        if roles and any(role.id in roles for role in author.roles):
            return
        mentions = len(message.raw_mentions) + len(message.raw_role_mentions)
        rule = self.check(message.guild.id, author.id, message.content, mentions, message.mention_everyone)
        if rule is not None:
            await self.punish(message, author, rule)

    async def punish(self, message: discord.Message, member: discord.Member, rule: str) -> None:
        guild = message.guild
        me = guild.me
        perms = message.channel.permissions_for(me)
        if self.delete and perms.manage_messages:
            try:
                await message.delete()
            except discord.HTTPException:
                pass
        key = (guild.id, member.id)
        if key in self.cooldown:
            return
        self.cooldown.set(key, True)
        strike = self.strikes.hit(key)
        action = self.escalation[min(strike, len(self.escalation)) - 1]
        reason = f"Anti-spam: {RULE_TEXT[rule]} (strike {strike})"
        blocked = can_act_on(me, member)
        done = action
        try:
            if action != "warn" and blocked:
                done = f"none ({blocked})"
            elif action == "timeout":
                if me.guild_permissions.moderate_members:
                    await member.timeout(datetime.timedelta(minutes=self.timeout_minutes), reason=reason)
                else:
                    done = "none (I lack Moderate Members)"
            elif action == "kick":
                if me.guild_permissions.kick_members:
                    await member.kick(reason=reason)
                else:
                    done = "none (I lack Kick Members)"
            elif action == "ban":
                if me.guild_permissions.ban_members:
                    await member.ban(reason=reason, delete_message_seconds=600)
                else:
                    done = "none (I lack Ban Members)"
        except discord.HTTPException as exc:
            done = f"failed ({exc.text or exc.status})"
        if perms.send_messages and action in ("warn", "timeout"):
            note = "" if action == "warn" else f" Timed out for {self.timeout_minutes} minutes."
            try:
                await message.channel.send(f"{member.mention}, stop {RULE_TEXT[rule]}.{note}", delete_after=10,
                                           allowed_mentions=discord.AllowedMentions(users=[member]))
            except discord.HTTPException:
                pass
        self.bot.db.write_later(
            "INSERT INTO spam_log (guild_id, user_id, rule, action, at) VALUES (?, ?, ?, ?, ?)",
            (guild.id, member.id, rule, done, time.time()))
        await send_log(self.bot, guild, "Anti-spam", f"{member.mention} (`{member.id}`) in {message.channel.mention}",
                       colour=ORANGE, fields={"Rule": RULE_TEXT[rule], "Strike": str(strike), "Action": done,
                                              "Message": (message.content or "(no text)")[:500]})

    # ---- commands ----
    async def _toggle(self, interaction: discord.Interaction, key: str, target_id: int, mention: str) -> None:
        items = list(await self.bot.settings.get(interaction.guild_id, key, []))
        if target_id in items:
            items.remove(target_id)
            verb = "is filtered again"
        else:
            items.append(target_id)
            verb = "is now ignored by anti-spam"
        await self.bot.settings.set(interaction.guild_id, key, items)
        self.ignore.pop(interaction.guild_id, None)
        await interaction.response.send_message(f"{mention} {verb}.", ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    @antispam.command(name="ignore-role", description="Toggle a role that anti-spam never touches.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def ignore_role(self, interaction: discord.Interaction, role: discord.Role) -> None:
        await self._toggle(interaction, "antispam_ignored_roles", role.id, role.mention)

    @antispam.command(name="ignore-channel", description="Toggle a channel where anti-spam is off.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def ignore_channel(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        await self._toggle(interaction, "antispam_ignored_channels", channel.id, channel.mention)

    @antispam.command(name="status", description="Show the active limits and recent actions.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def status(self, interaction: discord.Interaction) -> None:
        roles, channels = await self.ignored(interaction.guild_id)
        rows = await self.bot.db.fetchall(
            "SELECT user_id, rule, action, at FROM spam_log WHERE guild_id = ? ORDER BY id DESC LIMIT 8",
            (interaction.guild_id,))
        lines = [
            f"Messages: more than {self.msg_limit} in {self.rate.window:.0f}s",
            f"Duplicates: {self.dup_limit} identical in {self.dups.window:.0f}s",
            f"Mentions: {self.mention_limit} per message · Emoji: {self.emoji_limit} · Caps: {self.caps_percent}%",
            f"Invites blocked: {self.block_invites} · Links blocked: {self.block_links}",
            f"Escalation: {' > '.join(self.escalation)} (strikes fade after {self.strikes.window / 60:.0f} min)",
            f"Ignored roles: {' '.join(f'<@&{r}>' for r in roles) or 'none'}",
            f"Ignored channels: {' '.join(f'<#{c}>' for c in channels) or 'none'}",
            "", "**Recent actions**",
        ]
        lines += [f"<t:{int(r['at'])}:R> <@{r['user_id']}> · {r['rule']} · {r['action']}" for r in rows] or ["none"]
        await interaction.response.send_message("\n".join(lines)[:2000], ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AntiSpam(bot))
