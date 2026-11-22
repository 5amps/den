"""Tickets: button panel, private channels, claim/close, transcripts saved on your disk.

Setup in Discord:
  /ticket category-add   one per kind of ticket (Support, Reports, ...), each with a staff role
  /ticket panel          posts the message with one button per category
"""
from __future__ import annotations

import asyncio
import html
import io
import logging
import re
import time
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from core.checks import UserError, guarded, is_staff
from core.modlog import BLUE, GREY, send_log

log = logging.getLogger("tickets")

SCHEMA = """
CREATE TABLE IF NOT EXISTS ticket_categories (
    id            INTEGER PRIMARY KEY,
    guild_id      INTEGER NOT NULL,
    name          TEXT NOT NULL COLLATE NOCASE,
    parent_id     INTEGER,
    staff_role_id INTEGER NOT NULL,
    intro         TEXT NOT NULL DEFAULT '',
    UNIQUE (guild_id, name)
);
CREATE TABLE IF NOT EXISTS tickets (
    id          INTEGER PRIMARY KEY,
    guild_id    INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,
    category_id INTEGER NOT NULL,
    opener_id   INTEGER NOT NULL,
    claimer_id  INTEGER,
    status      TEXT NOT NULL DEFAULT 'open',
    opened_at   REAL NOT NULL,
    closed_at   REAL,
    closed_by   INTEGER,
    transcript  TEXT
);
CREATE INDEX IF NOT EXISTS tickets_channel ON tickets (channel_id);
CREATE INDEX IF NOT EXISTS tickets_opener ON tickets (guild_id, opener_id, status);
"""

HTML_HEAD = """<!doctype html><html><head><meta charset="utf-8"><title>{title}</title><style>
body{{font-family:Segoe UI,Arial,sans-serif;background:#313338;color:#dbdee1;margin:0;padding:24px}}
h1{{font-size:20px;color:#fff}}.meta{{color:#949ba4;margin-bottom:20px}}
.m{{padding:6px 0;border-top:1px solid #3f4147}}.a{{color:#fff;font-weight:600}}
.t{{color:#949ba4;font-size:12px;margin-left:8px}}.c{{white-space:pre-wrap;word-wrap:break-word;margin-top:2px}}
.x{{color:#949ba4;font-size:13px}}a{{color:#00a8fc}}</style></head><body>
<h1>{title}</h1><div class="meta">{meta}</div>
"""


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:20] or "user"


class OpenButton(discord.ui.DynamicItem[discord.ui.Button], template=r"ticket:open:(?P<id>\d+)"):
    def __init__(self, category_id: int, label: str = "Open ticket") -> None:
        super().__init__(discord.ui.Button(label=label[:80], style=discord.ButtonStyle.primary,
                                           custom_id=f"ticket:open:{category_id}", emoji="🎫"))
        self.category_id = category_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):  # noqa: ANN001
        return cls(int(match["id"]), item.label or "Open ticket")

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("Tickets")
        if cog is not None:
            await guarded(interaction, cog.open_ticket(interaction, self.category_id))


class ClaimButton(discord.ui.DynamicItem[discord.ui.Button], template=r"ticket:claim"):
    def __init__(self) -> None:
        super().__init__(discord.ui.Button(label="Claim", style=discord.ButtonStyle.success, custom_id="ticket:claim"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):  # noqa: ANN001
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("Tickets")
        if cog is not None:
            await guarded(interaction, cog.claim_ticket(interaction))


class CloseButton(discord.ui.DynamicItem[discord.ui.Button], template=r"ticket:close"):
    def __init__(self) -> None:
        super().__init__(discord.ui.Button(label="Close", style=discord.ButtonStyle.danger, custom_id="ticket:close"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):  # noqa: ANN001
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("Tickets")
        if cog is not None:
            await guarded(interaction, cog.close_ticket(interaction, "Closed with the button"))


class Tickets(commands.Cog):
    ticket = app_commands.Group(name="ticket", description="Ticket system", guild_only=True)

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.closing: set[int] = set()                  # channel ids being closed right now
        self.opening: set[tuple[int, int]] = set()      # (guild, user) with a ticket being created

    async def cog_load(self) -> None:
        await self.bot.db.script(SCHEMA)
        self.bot.add_dynamic_items(OpenButton, ClaimButton, CloseButton)

    async def cog_unload(self) -> None:
        self.bot.remove_dynamic_items(OpenButton, ClaimButton, CloseButton)

    # ---- helpers ----
    async def _ticket_here(self, interaction: discord.Interaction):
        row = await self.bot.db.fetchone(
            "SELECT t.*, c.staff_role_id, c.name AS category_name FROM tickets t "
            "LEFT JOIN ticket_categories c ON c.id = t.category_id "
            "WHERE t.channel_id = ? AND t.status = 'open'", (interaction.channel_id,))
        if row is None:
            raise UserError("This is not an open ticket channel.")
        return row

    async def _is_ticket_staff(self, interaction: discord.Interaction, row) -> bool:
        if await is_staff(interaction):
            return True
        return any(role.id == row["staff_role_id"] for role in getattr(interaction.user, "roles", []))

    async def _categories(self, interaction: discord.Interaction, current: str):
        try:
            rows = await self.bot.db.fetchall(
                "SELECT name FROM ticket_categories WHERE guild_id = ? ORDER BY name LIMIT 25", (interaction.guild_id,))
        except Exception:  # noqa: BLE001
            return []
        return [app_commands.Choice(name=r["name"], value=r["name"]) for r in rows
                if current.lower() in r["name"].lower()]

    # ---- open ----
    async def open_ticket(self, interaction: discord.Interaction, category_id: int) -> None:
        guild, user = interaction.guild, interaction.user
        await interaction.response.defer(ephemeral=True)
        cat = await self.bot.db.fetchone(
            "SELECT * FROM ticket_categories WHERE id = ? AND guild_id = ?", (category_id, guild.id))
        if cat is None:
            raise UserError("This ticket type no longer exists. Ask staff to post a new panel.")
        limit = int(self.bot.cfg.get("tickets.max_open_per_user", 1))
        key = (guild.id, user.id)
        if key in self.opening:
            raise UserError("Your ticket is already being created.")
        self.opening.add(key)
        try:
            rows = await self.bot.db.fetchall(
                "SELECT id, channel_id FROM tickets WHERE guild_id = ? AND opener_id = ? AND status = 'open'",
                (guild.id, user.id))
            live = []
            for row in rows:
                if guild.get_channel(row["channel_id"]) is None:        # channel deleted by hand
                    await self.bot.db.execute(
                        "UPDATE tickets SET status = 'closed', closed_at = ? WHERE id = ?", (time.time(), row["id"]))
                else:
                    live.append(row)
            if len(live) >= limit:
                raise UserError(f"You already have an open ticket: <#{live[0]['channel_id']}>")
            me = guild.me
            if not me.guild_permissions.manage_channels:
                raise UserError("I need the Manage Channels permission to create tickets. Tell an admin.")
            parent = guild.get_channel(cat["parent_id"]) if cat["parent_id"] else None
            if not isinstance(parent, discord.CategoryChannel):
                parent = None
            staff_role = guild.get_role(cat["staff_role_id"])
            talk = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True,
                                               attach_files=True, embed_links=True)
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                user: talk,
                me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True,
                                                manage_channels=True, attach_files=True, embed_links=True),
            }
            if staff_role is not None:
                overwrites[staff_role] = talk
            ticket_id = await self.bot.db.execute(
                "INSERT INTO tickets (guild_id, channel_id, category_id, opener_id, opened_at) VALUES (?, 0, ?, ?, ?)",
                (guild.id, cat["id"], user.id, time.time()))
            try:
                channel = await guild.create_text_channel(
                    f"ticket-{ticket_id}-{slug(user.name)}", category=parent, overwrites=overwrites,
                    topic=f"{cat['name']} ticket #{ticket_id} for {user} ({user.id})",
                    reason=f"Ticket opened by {user}")
            except discord.HTTPException as exc:
                await self.bot.db.execute("DELETE FROM tickets WHERE id = ?", (ticket_id,))
                raise UserError(f"I could not create the ticket channel: {exc.text or exc.status}. "
                                "I need Manage Channels and Manage Roles.") from exc
            await self.bot.db.execute("UPDATE tickets SET channel_id = ? WHERE id = ?", (channel.id, ticket_id))
        finally:
            self.opening.discard(key)

        view = discord.ui.View(timeout=None)
        view.add_item(ClaimButton())
        view.add_item(CloseButton())
        embed = discord.Embed(
            title=f"{cat['name']} ticket #{ticket_id}",
            description=cat["intro"] or "Describe your issue and a staff member will be with you shortly.",
            colour=BLUE)
        ping = f"{user.mention}" + (f" {staff_role.mention}" if staff_role is not None else "")
        try:
            await channel.send(ping, embed=embed, view=view,
                               allowed_mentions=discord.AllowedMentions(users=[user], roles=[staff_role] if staff_role else False))
        except discord.HTTPException:
            pass
        await interaction.followup.send(f"Your ticket is ready: {channel.mention}", ephemeral=True)
        await send_log(self.bot, guild, "Ticket opened", f"#{ticket_id} {channel.mention} by {user.mention}",
                       colour=BLUE, fields={"Type": cat["name"]})

    # ---- claim ----
    async def claim_ticket(self, interaction: discord.Interaction) -> None:
        row = await self._ticket_here(interaction)
        if not await self._is_ticket_staff(interaction, row):
            raise UserError("Only staff can claim tickets.")
        if row["claimer_id"]:
            raise UserError(f"Already claimed by <@{row['claimer_id']}>.")
        await self.bot.db.execute("UPDATE tickets SET claimer_id = ? WHERE id = ?", (interaction.user.id, row["id"]))
        await interaction.response.send_message(f"{interaction.user.mention} will handle this ticket.",
                                                allowed_mentions=discord.AllowedMentions.none())

    # ---- close + transcript ----
    async def close_ticket(self, interaction: discord.Interaction, reason: str) -> None:
        row = await self._ticket_here(interaction)
        is_opener = interaction.user.id == row["opener_id"]
        if not is_opener and not await self._is_ticket_staff(interaction, row):
            raise UserError("Only staff or the person who opened the ticket can close it.")
        if is_opener and not bool(self.bot.cfg.get("tickets.opener_can_close", True)) \
                and not await self._is_ticket_staff(interaction, row):
            raise UserError("Only staff can close tickets on this server.")
        channel = interaction.channel
        if channel.id in self.closing:
            raise UserError("This ticket is already closing.")
        self.closing.add(channel.id)
        try:
            await interaction.response.send_message("Saving the transcript and closing this ticket...")
            paths, count = await self.save_transcript(channel, row)
            await self.bot.db.execute(
                "UPDATE tickets SET status = 'closed', closed_at = ?, closed_by = ?, transcript = ? WHERE id = ?",
                (time.time(), interaction.user.id, str(paths[0]) if paths else None, row["id"]))
            guild = interaction.guild
            summary = (f"#{row['id']} ({row['category_name'] or 'ticket'}) opened by <@{row['opener_id']}>, "
                       f"closed by {interaction.user.mention}")
            fields = {"Messages": str(count), "Reason": reason[:500],
                      "Saved to": "\n".join(f"`{p}`" for p in paths) or "not saved"}

            def attach() -> discord.File | None:
                if not paths or paths[0].stat().st_size > guild.filesize_limit - 100_000:
                    return None
                return discord.File(paths[0], filename=paths[0].name)

            await send_log(self.bot, guild, "Ticket closed", summary, colour=GREY, file=attach(), fields=fields)
            if bool(self.bot.cfg.get("tickets.dm_transcript", True)) and paths:
                try:
                    opener = guild.get_member(row["opener_id"]) or await self.bot.fetch_user(row["opener_id"])
                    file = attach()
                    if file is not None:
                        await opener.send(f"Your ticket #{row['id']} in **{guild.name}** was closed. Transcript attached.",
                                          file=file)
                except discord.HTTPException:
                    pass                                    # DMs closed: nothing to do
            await asyncio.sleep(float(self.bot.cfg.get("tickets.delete_delay_seconds", 5)))
            try:
                await channel.delete(reason=f"Ticket #{row['id']} closed by {interaction.user}")
            except discord.HTTPException as exc:
                await channel.send(f"I could not delete this channel ({exc.text or exc.status}). "
                                   "The ticket is closed; delete the channel by hand.")
        finally:
            self.closing.discard(channel.id)

    async def save_transcript(self, channel: discord.TextChannel, row) -> tuple[list[Path], int]:
        """Read the channel history and write it to data/transcripts as HTML and/or text."""
        fmt = str(self.bot.cfg.get("tickets.transcript_format", "html")).lower()
        limit = int(self.bot.cfg.get("tickets.transcript_max_messages", 2000))
        title = f"Ticket #{row['id']} - {channel.name}"
        meta = (f"Server: {channel.guild.name} | Type: {row['category_name']} | Opener: {row['opener_id']} | "
                f"Opened: {time.strftime('%Y-%m-%d %H:%M', time.localtime(row['opened_at']))} | "
                f"Closed: {time.strftime('%Y-%m-%d %H:%M')}")
        web = io.StringIO()
        txt = io.StringIO()
        web.write(HTML_HEAD.format(title=html.escape(title), meta=html.escape(meta)))
        txt.write(f"{title}\n{meta}\n{'-' * 60}\n")
        count = 0
        try:
            async for msg in channel.history(limit=limit, oldest_first=True):
                count += 1
                stamp = msg.created_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")
                extras = [a.url for a in msg.attachments] + [f"[embed] {e.title or e.description or ''}"[:200] for e in msg.embeds]
                body = msg.clean_content
                txt.write(f"[{stamp}] {msg.author} ({msg.author.id}): {body}\n")
                for extra in extras:
                    txt.write(f"    {extra}\n")
                web.write(f'<div class="m"><span class="a">{html.escape(str(msg.author))}</span>'
                          f'<span class="t">{stamp}</span><div class="c">{html.escape(body)}</div>')
                for extra in extras:
                    safe = html.escape(extra)
                    web.write(f'<div class="x"><a href="{safe}">{safe}</a></div>' if extra.startswith("http")
                              else f'<div class="x">{safe}</div>')
                web.write("</div>\n")
        except discord.HTTPException as exc:
            log.warning("Transcript for ticket %s is incomplete: %s", row["id"], exc)
        web.write("</body></html>\n")
        folder: Path = self.bot.cfg.data_dir / "transcripts" / str(channel.guild.id)
        outputs = []
        if fmt in ("html", "both"):
            outputs.append((folder / f"ticket-{row['id']}.html", web.getvalue()))
        if fmt in ("text", "txt", "both"):
            outputs.append((folder / f"ticket-{row['id']}.txt", txt.getvalue()))

        def write() -> list[Path]:
            folder.mkdir(parents=True, exist_ok=True)
            done = []
            for path, text in outputs:
                path.write_text(text, encoding="utf-8")
                done.append(path)
            return done

        try:
            return await asyncio.to_thread(write), count
        except OSError as exc:
            log.error("Could not save the transcript for ticket %s: %s", row["id"], exc)
            return [], count

    # ---- commands: inside a ticket ----
    @ticket.command(name="close", description="Close this ticket and save its transcript.")
    async def cmd_close(self, interaction: discord.Interaction, reason: str = "No reason given") -> None:
        await self.close_ticket(interaction, reason)

    @ticket.command(name="claim", description="Staff: take responsibility for this ticket.")
    async def cmd_claim(self, interaction: discord.Interaction) -> None:
        await self.claim_ticket(interaction)

    @ticket.command(name="add", description="Staff: let another member see this ticket.")
    async def cmd_add(self, interaction: discord.Interaction, member: discord.Member) -> None:
        row = await self._ticket_here(interaction)
        if not await self._is_ticket_staff(interaction, row):
            raise UserError("Only staff can add people to a ticket.")
        await interaction.channel.set_permissions(
            member, view_channel=True, send_messages=True, read_message_history=True, attach_files=True,
            reason=f"Added to ticket by {interaction.user}")
        await interaction.response.send_message(f"{member.mention} was added to this ticket.",
                                                allowed_mentions=discord.AllowedMentions(users=[member]))

    @ticket.command(name="remove", description="Staff: remove a member from this ticket.")
    async def cmd_remove(self, interaction: discord.Interaction, member: discord.Member) -> None:
        row = await self._ticket_here(interaction)
        if not await self._is_ticket_staff(interaction, row):
            raise UserError("Only staff can remove people from a ticket.")
        if member.id == row["opener_id"]:
            raise UserError("The person who opened the ticket cannot be removed. Close the ticket instead.")
        await interaction.channel.set_permissions(member, overwrite=None, reason=f"Removed from ticket by {interaction.user}")
        await interaction.response.send_message(f"{member.mention} was removed from this ticket.",
                                                allowed_mentions=discord.AllowedMentions.none())

    # ---- commands: setup ----
    @ticket.command(name="category-add", description="Admin: create or update a ticket type.")
    @app_commands.describe(name="Shown on the button, e.g. Support", staff_role="Role that can see and handle these tickets",
                           channel_category="Discord category the ticket channels are created in",
                           intro="First message shown inside a new ticket")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def category_add(self, interaction: discord.Interaction, name: app_commands.Range[str, 1, 40],
                           staff_role: discord.Role, channel_category: discord.CategoryChannel | None = None,
                           intro: app_commands.Range[str, 1, 1500] = "") -> None:
        count = await self.bot.db.fetchone("SELECT COUNT(*) AS n FROM ticket_categories WHERE guild_id = ?",
                                           (interaction.guild_id,))
        exists = await self.bot.db.fetchone("SELECT id FROM ticket_categories WHERE guild_id = ? AND name = ?",
                                            (interaction.guild_id, name.strip()))
        if exists is None and count["n"] >= 20:
            raise UserError("A server can have at most 20 ticket types.")
        await self.bot.db.execute(
            "INSERT INTO ticket_categories (guild_id, name, parent_id, staff_role_id, intro) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (guild_id, name) DO UPDATE SET parent_id = excluded.parent_id, "
            "staff_role_id = excluded.staff_role_id, intro = excluded.intro",
            (interaction.guild_id, name.strip(), channel_category.id if channel_category else None, staff_role.id, intro))
        await interaction.response.send_message(
            f"Ticket type **{name.strip()}** saved (staff: {staff_role.mention}). Post it with `/ticket panel`.",
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @ticket.command(name="category-remove", description="Admin: delete a ticket type.")
    @app_commands.autocomplete(name=_categories)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def category_remove(self, interaction: discord.Interaction, name: str) -> None:
        removed = await self.bot.db.execute("DELETE FROM ticket_categories WHERE guild_id = ? AND name = ?",
                                            (interaction.guild_id, name.strip()))
        if not removed:
            raise UserError("No ticket type with that name.")
        await interaction.response.send_message(
            f"Ticket type **{name.strip()}** deleted. Old panel buttons for it stop working.", ephemeral=True)

    @ticket.command(name="panel", description="Admin: post the message with the ticket buttons.")
    @app_commands.describe(channel="Where to post it (default: here)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def panel(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None,
                    title: app_commands.Range[str, 1, 200] = "Need help?",
                    text: app_commands.Range[str, 1, 2000] = "Press a button below to open a private ticket with staff.") -> None:
        channel = channel or interaction.channel
        rows = await self.bot.db.fetchall(
            "SELECT id, name FROM ticket_categories WHERE guild_id = ? ORDER BY id LIMIT 20", (interaction.guild_id,))
        if not rows:
            raise UserError("Create at least one ticket type first with `/ticket category-add`.")
        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            raise UserError(f"I need View Channel, Send Messages and Embed Links in {channel.mention}.")
        view = discord.ui.View(timeout=None)
        for row in rows:
            view.add_item(OpenButton(row["id"], row["name"]))
        await channel.send(embed=discord.Embed(title=title, description=text, colour=BLUE), view=view)
        await interaction.response.send_message(f"Ticket panel posted in {channel.mention}.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Tickets(bot))
