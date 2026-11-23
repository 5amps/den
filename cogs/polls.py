"""Polls using Discord's own poll feature: /poll create "Pizza or tacos?" options: Pizza | Tacos

Discord stores the votes, shows the live bars and posts the result when the poll ends, so a
poll keeps working while the bot is offline and costs the bot nothing while it runs.
The bot only remembers which polls it posted, so /poll end can close one early.
Needs the "Send Polls" permission in the channel; no privileged intents.
"""
from __future__ import annotations

import datetime
import logging
import re
import time

import discord
from discord import app_commands
from discord.ext import commands

from core.checks import UserError, is_staff, missing_permissions

log = logging.getLogger("polls")

SCHEMA = """
CREATE TABLE IF NOT EXISTS polls (
    message_id  INTEGER PRIMARY KEY,
    guild_id    INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,
    creator_id  INTEGER NOT NULL,
    question    TEXT NOT NULL,
    ends_at     REAL NOT NULL
);
"""
MAX_ANSWERS = 10          # Discord's limits
MAX_ANSWER_LEN = 55
MAX_QUESTION_LEN = 300
MAX_HOURS = 768           # 32 days
SPLIT_RE = re.compile(r"\s*(?:\||;|\n)\s*")


def split_options(text: str) -> list[str]:
    """'Pizza | Tacos | Sushi' (or ; or commas when no | is used) -> clean, unique answers."""
    parts = SPLIT_RE.split(text.strip()) if SPLIT_RE.search(text) else text.split(",")
    answers: list[str] = []
    for part in (p.strip() for p in parts):
        if part and part.lower() not in (a.lower() for a in answers):
            answers.append(part)
    if len(answers) < 2:
        raise UserError("Give at least 2 options, separated by `|`. Example: `Pizza | Tacos | Sushi`.")
    if len(answers) > MAX_ANSWERS:
        raise UserError(f"Discord allows at most {MAX_ANSWERS} options.")
    long = [a for a in answers if len(a) > MAX_ANSWER_LEN]
    if long:
        raise UserError(f"Each option can be at most {MAX_ANSWER_LEN} characters: `{long[0][:60]}` is too long.")
    return answers


def build_poll(question: str, answers: list[str], hours: int, multiple: bool) -> discord.Poll:
    poll = discord.Poll(question=question[:MAX_QUESTION_LEN], duration=datetime.timedelta(hours=hours),
                        multiple=multiple)
    for answer in answers:
        poll.add_answer(text=answer)
    return poll


class Polls(commands.Cog):
    poll = app_commands.Group(name="poll", description="Polls", guild_only=True)

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.staff_only = bool(bot.cfg.get("polls.staff_only", False))

    async def cog_load(self) -> None:
        await self.bot.db.script(SCHEMA)

    @poll.command(name="create", description="Start a poll in this channel. Discord counts the votes.")
    @app_commands.describe(question="The question", options="2 to 10 options separated by |, e.g. Pizza | Tacos",
                           hours="How long it runs, 1 to 768 hours (default 24)",
                           multiple="Allow picking more than one option")
    @app_commands.checks.cooldown(1, 20.0, key=lambda i: i.user.id)
    async def create(self, interaction: discord.Interaction, question: app_commands.Range[str, 1, 300],
                     options: str, hours: app_commands.Range[int, 1, 768] = 24, multiple: bool = False) -> None:
        if self.staff_only and not await is_staff(interaction):
            raise UserError("Only staff can start polls on this server.")
        answers = split_options(options)
        missing = missing_permissions(interaction.channel, "view_channel", "send_messages", "send_polls")
        if missing:
            raise UserError(f"I need these permissions in this channel: {', '.join(missing)}.")
        message = await interaction.channel.send(
            content=f"📊 Poll by {interaction.user.mention}", poll=build_poll(question, answers, hours, multiple),
            allowed_mentions=discord.AllowedMentions.none())
        await self.bot.db.execute(
            "INSERT INTO polls (message_id, guild_id, channel_id, creator_id, question, ends_at) VALUES (?, ?, ?, ?, ?, ?)",
            (message.id, interaction.guild_id, interaction.channel_id, interaction.user.id, question,
             time.time() + hours * 3600))
        await interaction.response.send_message(
            f"Poll posted: {message.jump_url}\nIt ends <t:{int(time.time() + hours * 3600)}:R>. "
            "End it early with `/poll end`.", ephemeral=True)

    async def _complete(self, interaction: discord.Interaction, current: str):
        rows = await self.bot.db.fetchall(
            "SELECT message_id, question, creator_id FROM polls WHERE guild_id = ? AND ends_at > ? "
            "ORDER BY ends_at LIMIT 50", (interaction.guild_id, time.time()))
        staff = await is_staff(interaction)
        return [app_commands.Choice(name=r["question"][:100], value=str(r["message_id"])) for r in rows
                if (staff or r["creator_id"] == interaction.user.id) and current.lower() in r["question"].lower()][:25]

    @poll.command(name="end", description="End a poll now and show the result (your own polls, or any if staff).")
    @app_commands.describe(poll="Pick the poll to end")
    @app_commands.autocomplete(poll=_complete)
    async def end(self, interaction: discord.Interaction, poll: str) -> None:
        row = await self.bot.db.fetchone("SELECT * FROM polls WHERE message_id = ? AND guild_id = ?",
                                         (int(poll) if poll.isdigit() else 0, interaction.guild_id))
        if row is None:
            raise UserError("Pick one of the polls from the list.")
        if row["creator_id"] != interaction.user.id and not await is_staff(interaction):
            raise UserError("You can only end your own polls.")
        channel = self.bot.get_channel(row["channel_id"])
        if channel is None:
            raise UserError("That poll's channel is gone.")
        await interaction.response.defer(ephemeral=True)
        try:
            message = await channel.fetch_message(row["message_id"])
            await message.end_poll()
        except discord.NotFound:
            await interaction.followup.send("That poll was deleted.", ephemeral=True)
        except discord.HTTPException as exc:
            if "expired" not in (exc.text or "").lower():
                raise
            await interaction.followup.send("That poll has already ended.", ephemeral=True)
        else:
            await interaction.followup.send(f"Poll ended. Discord posts the result under it: {message.jump_url}",
                                            ephemeral=True)
        await self.bot.db.execute("DELETE FROM polls WHERE message_id = ?", (row["message_id"],))
        await self.bot.db.execute("DELETE FROM polls WHERE ends_at < ?", (time.time() - 86400,))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Polls(bot))
