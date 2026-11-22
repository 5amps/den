"""One place for every module to write to the server's log channel. Never raises."""
from __future__ import annotations

import logging

import discord

log = logging.getLogger("modlog")

RED = discord.Colour.red()
ORANGE = discord.Colour.orange()
GREEN = discord.Colour.green()
BLUE = discord.Colour.blurple()
GREY = discord.Colour.light_grey()


async def send_log(bot, guild: discord.Guild, title: str, description: str = "", *,
                   colour: discord.Colour = BLUE, file: discord.File | None = None,
                   fields: dict[str, str] | None = None) -> bool:
    """Post an embed to the channel set with /config log-channel. Returns True if it was sent."""
    try:
        channel_id = await bot.settings.get(guild.id, "log_channel")
        channel = guild.get_channel(channel_id) if channel_id else None
        if channel is None:
            return False
        perms = channel.permissions_for(guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            return False
        if file is not None and not perms.attach_files:
            file = None
        embed = discord.Embed(title=title[:256], description=description[:4000], colour=colour,
                              timestamp=discord.utils.utcnow())
        for name, value in (fields or {}).items():
            embed.add_field(name=name[:256], value=(value or "-")[:1024])
        kwargs = {"file": file} if file is not None else {}
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none(), **kwargs)
        return True
    except discord.HTTPException as exc:
        log.warning("Could not write to the log channel in %s: %s", guild.name, exc)
        return False
    except Exception:  # noqa: BLE001
        log.exception("Log channel write failed")
        return False


def can_act_on(actor: discord.Member, target: discord.Member) -> str | None:
    """Why `actor` (a moderator, or the bot itself) may not punish `target`; None if allowed."""
    guild = actor.guild
    if target.id == guild.owner_id:
        return "that member owns the server"
    if target.id == actor.id:
        return "that is the same account"
    if actor.id != guild.owner_id and target.top_role >= actor.top_role:
        who = "my" if actor.id == guild.me.id else "your"
        return f"their highest role is not below {who} highest role"
    return None
