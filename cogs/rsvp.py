"""Events with RSVP buttons: Going / Maybe / No, capacity + waitlist, reminders, auto-close.

Reminders and auto-close are rows in the persistent scheduler, so they survive restarts.
Button clicks are answered instantly; the public message is refreshed at most once every
couple of seconds per event, so a rush of clicks cannot hit Discord's edit rate limit.
"""
from __future__ import annotations

import asyncio
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

from core.checks import UserError, guarded, is_staff, reply
from core.modlog import BLUE, GREY
from core.when import parse_when

log = logging.getLogger("rsvp")

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY,
    guild_id    INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,
    message_id  INTEGER NOT NULL DEFAULT 0,
    creator_id  INTEGER NOT NULL,
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    starts_at   REAL NOT NULL,
    capacity    INTEGER NOT NULL DEFAULT 0,
    closed      INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS rsvps (
    event_id INTEGER NOT NULL REFERENCES events (id) ON DELETE CASCADE,
    user_id  INTEGER NOT NULL,
    status   TEXT NOT NULL,
    at       REAL NOT NULL,
    PRIMARY KEY (event_id, user_id)
) WITHOUT ROWID;
"""

LABELS = {"going": ("Going", "✅", discord.ButtonStyle.success),
          "maybe": ("Maybe", "❔", discord.ButtonStyle.secondary),
          "no": ("Can't go", "❌", discord.ButtonStyle.danger)}
class RsvpButton(discord.ui.DynamicItem[discord.ui.Button], template=r"rsvp:(?P<event>\d+):(?P<status>going|maybe|no)"):
    def __init__(self, event_id: int, status: str, disabled: bool = False) -> None:
        label, emoji, style = LABELS[status]
        super().__init__(discord.ui.Button(label=label, emoji=emoji, style=style, disabled=disabled,
                                           custom_id=f"rsvp:{event_id}:{status}"))
        self.event_id = event_id
        self.status = status

    @classmethod
    async def from_custom_id(cls, interaction, item, match):  # noqa: ANN001
        return cls(int(match["event"]), match["status"])

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("Rsvp")
        if cog is not None:
            await guarded(interaction, cog.respond(interaction, self.event_id, self.status))


def buttons(event_id: int, disabled: bool = False) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for status in LABELS:
        view.add_item(RsvpButton(event_id, status, disabled))
    return view


class Rsvp(commands.Cog):
    event = app_commands.Group(name="event", description="Events with RSVP", guild_only=True)

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._refresh: dict[int, asyncio.Task] = {}      # event id -> pending message refresh

    async def cog_load(self) -> None:
        await self.bot.db.script(SCHEMA)
        self.bot.add_dynamic_items(RsvpButton)
        self.bot.scheduler.register("rsvp_reminder", self._job_reminder)
        self.bot.scheduler.register("rsvp_close", self._job_close)

    async def cog_unload(self) -> None:
        self.bot.remove_dynamic_items(RsvpButton)
        self.bot.scheduler.unregister("rsvp_reminder")
        self.bot.scheduler.unregister("rsvp_close")
        for task in self._refresh.values():
            task.cancel()

    # ---- rendering ----
    async def render(self, event) -> discord.Embed:
        rows = await self.bot.db.fetchall(
            "SELECT user_id, status FROM rsvps WHERE event_id = ? ORDER BY at", (event["id"],))
        groups: dict[str, list[int]] = {"going": [], "waitlist": [], "maybe": [], "no": []}
        for row in rows:
            groups.setdefault(row["status"], []).append(row["user_id"])
        start = int(event["starts_at"])
        embed = discord.Embed(title=event["title"], description=event["description"] or None,
                              colour=GREY if event["closed"] else BLUE)
        embed.add_field(name="When", value=f"<t:{start}:F> (<t:{start}:R>)", inline=False)

        def names(ids: list[int]) -> str:
            shown = " ".join(f"<@{i}>" for i in ids[:30])
            return (shown + (f" +{len(ids) - 30} more" if len(ids) > 30 else "")) or "-"

        cap = event["capacity"]
        embed.add_field(name=f"✅ Going ({len(groups['going'])}{f'/{cap}' if cap else ''})", value=names(groups["going"]), inline=False)
        if groups["waitlist"]:
            embed.add_field(name=f"⏳ Waitlist ({len(groups['waitlist'])})", value=names(groups["waitlist"]), inline=False)
        embed.add_field(name=f"❔ Maybe ({len(groups['maybe'])})", value=names(groups["maybe"]))
        embed.add_field(name=f"❌ Can't go ({len(groups['no'])})", value=names(groups["no"]))
        embed.set_footer(text=f"Event #{event['id']}" + (" · closed" if event["closed"] else ""))
        return embed

    def queue_refresh(self, event_id: int) -> None:
        """Coalesce many clicks into one message edit."""
        if event_id in self._refresh:
            return

        async def later() -> None:
            try:
                await asyncio.sleep(float(self.bot.cfg.get("rsvp.refresh_seconds", 2)))
                self._refresh.pop(event_id, None)
                await self.refresh_now(event_id)
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                log.exception("Event refresh failed")
            finally:
                self._refresh.pop(event_id, None)

        self._refresh[event_id] = asyncio.create_task(later())

    async def refresh_now(self, event_id: int) -> None:
        event = await self.bot.db.fetchone("SELECT * FROM events WHERE id = ?", (event_id,))
        if event is None or not event["message_id"]:
            return
        guild = self.bot.get_guild(event["guild_id"])
        channel = guild.get_channel_or_thread(event["channel_id"]) if guild else None
        if channel is None:
            return
        try:
            await channel.get_partial_message(event["message_id"]).edit(
                embed=await self.render(event), view=buttons(event_id, disabled=bool(event["closed"])))
        except discord.NotFound:
            await self.bot.db.execute("UPDATE events SET closed = 1 WHERE id = ?", (event_id,))
        except discord.HTTPException as exc:
            log.warning("Could not update event %s: %s", event_id, exc)

    # ---- button handler ----
    async def respond(self, interaction: discord.Interaction, event_id: int, status: str) -> None:
        db = self.bot.db
        event = await db.fetchone("SELECT * FROM events WHERE id = ? AND guild_id = ?", (event_id, interaction.guild_id))
        if event is None:
            raise UserError("This event no longer exists.")
        if event["closed"]:
            raise UserError("This event is closed.")
        user_id = interaction.user.id
        current = await db.fetchone("SELECT status FROM rsvps WHERE event_id = ? AND user_id = ?", (event_id, user_id))
        previous = current["status"] if current else None
        final = status
        if status == "going":
            if previous in ("going", "waitlist"):
                # pressing Going twice removes the answer
                await db.execute("DELETE FROM rsvps WHERE event_id = ? AND user_id = ?", (event_id, user_id))
                await self._promote(event)
                self.queue_refresh(event_id)
                await reply(interaction, "Your answer was removed.")
                return
            if event["capacity"]:
                taken = await db.fetchone(
                    "SELECT COUNT(*) AS n FROM rsvps WHERE event_id = ? AND status = 'going'", (event_id,))
                if taken["n"] >= event["capacity"]:
                    final = "waitlist"
        elif previous == status:
            await db.execute("DELETE FROM rsvps WHERE event_id = ? AND user_id = ?", (event_id, user_id))
            self.queue_refresh(event_id)
            await reply(interaction, "Your answer was removed.")
            return
        await db.execute(
            "INSERT INTO rsvps (event_id, user_id, status, at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (event_id, user_id) DO UPDATE SET status = excluded.status, at = excluded.at",
            (event_id, user_id, final, time.time()))
        if previous == "going" and final != "going":
            await self._promote(event)
        self.queue_refresh(event_id)
        text = {"going": "You are going.", "maybe": "Marked as maybe.", "no": "Marked as not going.",
                "waitlist": "The event is full. You are on the waitlist and move up automatically if a spot opens."}[final]
        await reply(interaction, text)

    async def _promote(self, event) -> None:
        """Fill freed spots from the waitlist, oldest first."""
        if not event["capacity"]:
            return
        db = self.bot.db
        taken = await db.fetchone("SELECT COUNT(*) AS n FROM rsvps WHERE event_id = ? AND status = 'going'", (event["id"],))
        free = event["capacity"] - taken["n"]
        if free <= 0:
            return
        waiting = await db.fetchall(
            "SELECT user_id FROM rsvps WHERE event_id = ? AND status = 'waitlist' ORDER BY at LIMIT ?", (event["id"], free))
        for row in waiting:
            await db.execute("UPDATE rsvps SET status = 'going' WHERE event_id = ? AND user_id = ?",
                             (event["id"], row["user_id"]))
        if waiting:
            guild = self.bot.get_guild(event["guild_id"])
            channel = guild.get_channel_or_thread(event["channel_id"]) if guild else None
            if channel is not None:
                try:
                    await channel.send(
                        " ".join(f"<@{r['user_id']}>" for r in waiting) + f" a spot opened up for **{event['title']}**. You are now going.",
                        allowed_mentions=discord.AllowedMentions(users=True))
                except discord.HTTPException:
                    pass

    # ---- scheduled jobs ----
    async def _job_reminder(self, payload: dict) -> None:
        event = await self.bot.db.fetchone("SELECT * FROM events WHERE id = ?", (payload.get("event"),))
        if event is None or event["closed"] or event["starts_at"] < time.time() - 60:
            return
        guild = self.bot.get_guild(event["guild_id"])
        channel = guild.get_channel_or_thread(event["channel_id"]) if guild else None
        if channel is None:
            return
        rows = await self.bot.db.fetchall(
            "SELECT user_id FROM rsvps WHERE event_id = ? AND status = 'going' ORDER BY at", (event["id"],))
        if not rows:
            return
        ids = [r["user_id"] for r in rows]
        for index in range(0, len(ids), 80):                # stay under the 2000-character limit
            mentions = " ".join(f"<@{i}>" for i in ids[index:index + 80])
            head = f"Reminder: **{event['title']}** starts <t:{int(event['starts_at'])}:R>.\n" if index == 0 else ""
            await channel.send(head + mentions, allowed_mentions=discord.AllowedMentions(users=True))

    async def _job_close(self, payload: dict) -> None:
        await self.close_event(payload.get("event", 0))

    async def close_event(self, event_id: int) -> None:
        await self.bot.db.execute("UPDATE events SET closed = 1 WHERE id = ?", (event_id,))
        await self.bot.scheduler.cancel_ref(f"event:{event_id}")
        await self.refresh_now(event_id)

    # ---- commands ----
    async def _own_event(self, interaction: discord.Interaction, event_id: int):
        event = await self.bot.db.fetchone("SELECT * FROM events WHERE id = ? AND guild_id = ?",
                                           (event_id, interaction.guild_id))
        if event is None:
            raise UserError(f"There is no event #{event_id} on this server.")
        if event["creator_id"] != interaction.user.id and not await is_staff(interaction):
            raise UserError("Only the event's creator or staff can do that.")
        return event

    @event.command(name="create", description="Create an event with Going / Maybe / Can't go buttons.")
    @app_commands.describe(title="Event name", when="2026-12-24 18:30, 18:30, or 'in 2h 30m' (the bot PC's time zone)",
                           description="Details", capacity="Maximum people going (0 = unlimited)",
                           reminder_minutes="Ping people who are going this many minutes before (0 = no reminder)")
    async def create(self, interaction: discord.Interaction, title: app_commands.Range[str, 1, 200], when: str,
                     description: app_commands.Range[str, 0, 1500] = "",
                     capacity: app_commands.Range[int, 0, 10000] = 0,
                     reminder_minutes: app_commands.Range[int, 0, 10080] = 15) -> None:
        if not bool(self.bot.cfg.get("rsvp.anyone_can_create", False)) and not await is_staff(interaction):
            raise UserError("Only staff can create events on this server.")
        starts_at = parse_when(when)
        if starts_at <= time.time() + 30:
            raise UserError("That time is in the past.")
        perms = interaction.channel.permissions_for(interaction.guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            raise UserError("I need View Channel, Send Messages and Embed Links in this channel.")
        db = self.bot.db
        event_id = await db.execute(
            "INSERT INTO events (guild_id, channel_id, creator_id, title, description, starts_at, capacity) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (interaction.guild_id, interaction.channel_id, interaction.user.id, title, description, starts_at, capacity))
        event = await db.fetchone("SELECT * FROM events WHERE id = ?", (event_id,))
        await interaction.response.send_message(embed=await self.render(event), view=buttons(event_id))
        message = await interaction.original_response()
        await db.execute("UPDATE events SET message_id = ? WHERE id = ?", (message.id, event_id))
        ref = f"event:{event_id}"
        close_after = float(self.bot.cfg.get("rsvp.close_minutes_after_start", 0)) * 60
        await self.bot.scheduler.add(starts_at + close_after, "rsvp_close", {"event": event_id}, ref=ref)
        if reminder_minutes and starts_at - reminder_minutes * 60 > time.time():
            await self.bot.scheduler.add(starts_at - reminder_minutes * 60, "rsvp_reminder", {"event": event_id}, ref=ref)

    @event.command(name="cancel", description="Close an event early. Its buttons stop working.")
    @app_commands.describe(event_id="The number in the event's footer")
    async def cancel(self, interaction: discord.Interaction, event_id: int) -> None:
        event = await self._own_event(interaction, event_id)
        await interaction.response.defer(ephemeral=True)
        await self.close_event(event_id)
        await interaction.followup.send(f"Event **{event['title']}** closed.", ephemeral=True)

    @event.command(name="list", description="List upcoming events on this server.")
    async def list_events(self, interaction: discord.Interaction) -> None:
        rows = await self.bot.db.fetchall(
            "SELECT e.id, e.title, e.starts_at, e.channel_id, e.message_id, e.capacity, "
            "(SELECT COUNT(*) FROM rsvps r WHERE r.event_id = e.id AND r.status = 'going') AS going "
            "FROM events e WHERE e.guild_id = ? AND e.closed = 0 ORDER BY e.starts_at LIMIT 15", (interaction.guild_id,))
        if not rows:
            raise UserError("There are no open events.")
        lines = []
        for r in rows:
            link = f"https://discord.com/channels/{interaction.guild_id}/{r['channel_id']}/{r['message_id']}"
            cap = f"/{r['capacity']}" if r["capacity"] else ""
            lines.append(f"`#{r['id']}` [{discord.utils.escape_markdown(r['title'])}]({link}) · <t:{int(r['starts_at'])}:R> · {r['going']}{cap} going")
        await interaction.response.send_message("\n".join(lines)[:2000], ephemeral=True)

    @event.command(name="attendees", description="Show everyone's answer for an event.")
    async def attendees(self, interaction: discord.Interaction, event_id: int) -> None:
        event = await self.bot.db.fetchone("SELECT * FROM events WHERE id = ? AND guild_id = ?",
                                           (event_id, interaction.guild_id))
        if event is None:
            raise UserError(f"There is no event #{event_id} on this server.")
        await interaction.response.send_message(embed=await self.render(event), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Rsvp(bot))
