"""Reminders: /remind in 2h take a break  ->  the bot DMs you when it is time.

Each reminder is a row here plus a job in the persistent scheduler, so reminders survive
restarts. If the bot was off when one came due it is sent as soon as the bot is back, marked
as late. If your DMs are closed, the reminder is posted in the channel where you set it.
Needs no privileged intents.
"""
from __future__ import annotations

import logging
import time
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands

from core.checks import UserError
from core.when import HELP, parse_when

log = logging.getLogger("reminders")

SCHEMA = """
CREATE TABLE IF NOT EXISTS reminders (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    guild_id    INTEGER NOT NULL DEFAULT 0,
    channel_id  INTEGER NOT NULL DEFAULT 0,
    text        TEXT NOT NULL,
    due_at      REAL NOT NULL,
    repeat      TEXT NOT NULL DEFAULT 'no',
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS reminders_user ON reminders (user_id, due_at);
"""
REPEAT_SECONDS = {"daily": 86400, "weekly": 604800}
LATE_AFTER = 120          # seconds; later than this the reminder says it is late
MIN_AHEAD = 30            # seconds
MAX_AHEAD = 366 * 86400


class Reminders(commands.Cog):
    reminders = app_commands.Group(name="reminders", description="Your reminders")

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.per_user = int(bot.cfg.get("reminders.max_per_user", 25))

    async def cog_load(self) -> None:
        await self.bot.db.script(SCHEMA)
        self.bot.scheduler.register("reminder", self._job)

    async def cog_unload(self) -> None:
        self.bot.scheduler.unregister("reminder")

    # ---- core ----
    async def create(self, user_id: int, guild_id: int, channel_id: int, when: str, text: str,
                     repeat: str = "no") -> tuple[int, float]:
        text = text.strip()
        if not text:
            raise UserError("Write what I should remind you about.")
        due = parse_when(when)
        now = time.time()
        if due < now + MIN_AHEAD:
            raise UserError(f"That time is in the past or too close. {HELP}")
        if due > now + MAX_AHEAD:
            raise UserError("Reminders can be at most a year ahead.")
        row = await self.bot.db.fetchone("SELECT COUNT(*) AS n FROM reminders WHERE user_id = ?", (user_id,))
        if row["n"] >= self.per_user:
            raise UserError(f"You already have {self.per_user} reminders. Cancel one with `/reminders cancel`.")
        rid = await self.bot.db.execute(
            "INSERT INTO reminders (user_id, guild_id, channel_id, text, due_at, repeat, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)", (user_id, guild_id, channel_id, text[:1000], due, repeat, now))
        await self.bot.scheduler.add(due, "reminder", {"id": rid}, ref=f"reminder:{rid}")
        return rid, due

    async def cancel(self, user_id: int, rid: int) -> str:
        row = await self.bot.db.fetchone("SELECT text FROM reminders WHERE id = ? AND user_id = ?", (rid, user_id))
        if row is None:
            raise UserError("No reminder of yours has that number. `/reminders list` shows them.")
        await self.bot.db.execute("DELETE FROM reminders WHERE id = ?", (rid,))
        await self.bot.scheduler.cancel_ref(f"reminder:{rid}", "reminder")
        return row["text"]

    async def _job(self, payload: dict) -> None:
        row = await self.bot.db.fetchone("SELECT * FROM reminders WHERE id = ?", (int(payload.get("id", 0)),))
        if row is None:
            return                                          # cancelled
        late = time.time() - row["due_at"]
        embed = discord.Embed(title="⏰ Reminder", description=row["text"], colour=discord.Colour.gold())
        set_at = int(row["created_at"])
        footer = f"Set <t:{set_at}:R>"
        if row["channel_id"] and row["guild_id"]:
            footer += f" in <#{row['channel_id']}>"
        if late > LATE_AFTER:
            footer += f"\nSent late: the bot was offline when it was due (<t:{int(row['due_at'])}:f>)."
        embed.add_field(name="​", value=footer, inline=False)
        if row["repeat"] in REPEAT_SECONDS:
            step = REPEAT_SECONDS[row["repeat"]]
            due = row["due_at"] + step
            while due < time.time():                        # skip the ones missed while offline
                due += step
            await self.bot.db.execute("UPDATE reminders SET due_at = ? WHERE id = ?", (due, row["id"]))
            await self.bot.scheduler.add(due, "reminder", {"id": row["id"]}, ref=f"reminder:{row['id']}")
            embed.add_field(name="Repeats", value=f"{row['repeat']}, next <t:{int(due)}:R>. Stop it with "
                                                  f"`/reminders cancel {row['id']}`.", inline=False)
        else:
            await self.bot.db.execute("DELETE FROM reminders WHERE id = ?", (row["id"],))
        await self.deliver(row, embed)

    async def deliver(self, row, embed: discord.Embed) -> None:
        user = self.bot.get_user(row["user_id"])
        try:
            user = user or await self.bot.fetch_user(row["user_id"])
            await user.send(embed=embed)
            return
        except discord.HTTPException:
            pass                                            # DMs closed or user gone: try the channel
        channel = self.bot.get_channel(row["channel_id"]) if row["channel_id"] else None
        if channel is None or not hasattr(channel, "send"):
            log.info("Reminder %s could not be delivered (DMs closed, no channel)", row["id"])
            return
        try:
            await channel.send(f"<@{row['user_id']}> your DMs are closed, so here is your reminder:", embed=embed,
                               allowed_mentions=discord.AllowedMentions(users=True, everyone=False, roles=False))
        except discord.HTTPException:
            log.info("Reminder %s could not be delivered to channel %s", row["id"], row["channel_id"])

    # ---- commands ----
    @app_commands.command(description="Remind you about something later, by DM.")
    @app_commands.describe(when="e.g. in 2h 30m, 45m, 18:30, tomorrow 9:00, 2026-12-24 18:30",
                           what="What to remind you about", repeat="Repeat every day or week")
    async def remind(self, interaction: discord.Interaction, when: str,
                     what: app_commands.Range[str, 1, 1000], repeat: Literal["no", "daily", "weekly"] = "no") -> None:
        rid, due = await self.create(interaction.user.id, interaction.guild_id or 0, interaction.channel_id or 0,
                                     when, what, repeat)
        again = f" Repeats {repeat}." if repeat != "no" else ""
        await interaction.response.send_message(
            f"⏰ Okay, <t:{int(due)}:R> (<t:{int(due)}:f>) I will DM you: **{discord.utils.escape_markdown(what[:200])}**"
            f"{again}\nReminder #{rid}. Cancel with `/reminders cancel {rid}`.", ephemeral=True)

    @reminders.command(name="list", description="Show your upcoming reminders.")
    async def list_cmd(self, interaction: discord.Interaction) -> None:
        rows = await self.bot.db.fetchall(
            "SELECT id, text, due_at, repeat FROM reminders WHERE user_id = ? ORDER BY due_at LIMIT 25",
            (interaction.user.id,))
        if not rows:
            await interaction.response.send_message("You have no reminders. Set one with `/remind`.", ephemeral=True)
            return
        lines = [f"`#{r['id']}` <t:{int(r['due_at'])}:R> · {discord.utils.escape_markdown(r['text'][:80])}"
                 + (f" · 🔁 {r['repeat']}" if r["repeat"] != "no" else "") for r in rows]
        await interaction.response.send_message("\n".join(lines)[:2000], ephemeral=True)

    async def _complete(self, interaction: discord.Interaction, current: str):
        rows = await self.bot.db.fetchall(
            "SELECT id, text, due_at FROM reminders WHERE user_id = ? ORDER BY due_at LIMIT 25", (interaction.user.id,))
        return [app_commands.Choice(name=f"#{r['id']} {r['text']}"[:100], value=r["id"]) for r in rows
                if current.lower() in f"#{r['id']} {r['text']}".lower()]

    @reminders.command(name="cancel", description="Cancel one of your reminders.")
    @app_commands.describe(reminder="Pick the reminder to cancel")
    @app_commands.autocomplete(reminder=_complete)
    async def cancel_cmd(self, interaction: discord.Interaction, reminder: int) -> None:
        text = await self.cancel(interaction.user.id, reminder)
        await interaction.response.send_message(
            f"Cancelled reminder #{reminder}: {discord.utils.escape_markdown(text[:200])}", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Reminders(bot))
