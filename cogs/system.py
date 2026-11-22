"""Always-on core commands: /ping, /status, /sync, /restart, /shutdown."""
from __future__ import annotations

import time

import discord
from discord import app_commands
from discord.ext import commands

from core import proc, sysinfo
from core.checks import owner_only


def _uptime(seconds: float) -> str:
    seconds = int(seconds)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m {secs}s"


def voice_report(voice) -> str:
    """One-line health summary of a voice connection, for diagnosing silence."""
    try:
        conn = voice._connection
        session = getattr(conn, "dave_session", None)
        source = getattr(voice, "source", None)
        frames = getattr(source, "frames", None)
        parts = [
            f"channel {voice.channel.mention}" if voice.channel else "no channel",
            "connected" if voice.is_connected() else "NOT connected",
            "playing" if voice.is_playing() else ("paused" if voice.is_paused() else "idle"),
            f"mode `{getattr(voice, 'mode', '?')}`",
            f"E2EE v{getattr(conn, 'dave_protocol_version', '?')} "
            f"{'ready' if session is not None and getattr(session, 'ready', False) else 'NOT ready'}",
            f"latency {voice.latency * 1000:.0f} ms" if voice.latency != float("inf") else "latency ?",
        ]
        if frames is not None:
            parts.append(f"{frames} packets sent")
        return " · ".join(parts)
    except Exception as exc:  # noqa: BLE001
        return f"unavailable ({exc})"


class System(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(description="Check that the bot is alive and how fast it responds.")
    async def ping(self, interaction: discord.Interaction) -> None:
        latency = self.bot.latency
        text = "connecting..." if latency != latency or latency == float("inf") else f"{latency * 1000:.0f} ms"
        await interaction.response.send_message(f"Pong. Gateway latency: {text}", ephemeral=True)

    @app_commands.command(description="Show memory use, uptime and which modules are running.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def status(self, interaction: discord.Interaction) -> None:
        bot = self.bot
        cfg = bot.cfg  # type: ignore[attr-defined]
        embed = discord.Embed(title="Bot status", colour=discord.Colour.green())
        embed.add_field(name="RAM", value=f"{sysinfo.rss_mb():.0f} MB")
        embed.add_field(name="Uptime", value=_uptime(time.time() - bot.started_at))  # type: ignore[attr-defined]
        embed.add_field(name="Servers", value=str(len(bot.guilds)))
        embed.add_field(name="Modules", value=", ".join(bot.loaded_modules) or "core only", inline=False)  # type: ignore[attr-defined]
        failed = bot.failed_modules  # type: ignore[attr-defined]
        if failed:
            embed.add_field(name="Failed to load", value=", ".join(failed), inline=False)
        embed.add_field(name="Database", value=f"{sysinfo.file_mb(bot.db.path):.1f} MB")  # type: ignore[attr-defined]
        embed.add_field(name="Free disk", value=f"{sysinfo.free_disk_mb(cfg.data_dir) / 1024:.1f} GB")
        embed.add_field(name="Helper processes", value=str(proc.live_count()))
        embed.add_field(name="ffmpeg", value="found" if proc.which("ffmpeg") else "missing")
        voice = interaction.guild.voice_client
        if voice is not None:
            embed.add_field(name="Voice", value=voice_report(voice), inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ---- server settings shared by every module ----
    config = app_commands.Group(name="config", description="Server settings for the bot", guild_only=True,
                                default_permissions=discord.Permissions(manage_guild=True))

    @config.command(name="log-channel", description="Choose where moderation, raid and ticket logs are posted.")
    @app_commands.describe(channel="Leave empty to switch logging off")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def config_log(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None) -> None:
        settings = self.bot.settings  # type: ignore[attr-defined]
        if channel is None:
            await settings.delete(interaction.guild_id, "log_channel")
            await interaction.response.send_message("Logging switched off.", ephemeral=True)
            return
        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            await interaction.response.send_message(
                f"I need View Channel, Send Messages and Embed Links in {channel.mention} first.", ephemeral=True)
            return
        await settings.set(interaction.guild_id, "log_channel", channel.id)
        await interaction.response.send_message(f"Logs will be posted in {channel.mention}.", ephemeral=True)

    @config.command(name="staff-role", description="Add or remove a role that counts as staff for bot commands.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def config_staff(self, interaction: discord.Interaction, role: discord.Role) -> None:
        settings = self.bot.settings  # type: ignore[attr-defined]
        roles = list(await settings.get(interaction.guild_id, "staff_roles", []))
        if role.id in roles:
            roles.remove(role.id)
            verb = "is no longer"
        else:
            roles.append(role.id)
            verb = "is now"
        await settings.set(interaction.guild_id, "staff_roles", roles)
        await interaction.response.send_message(f"{role.mention} {verb} a staff role.", ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    @config.command(name="show", description="Show this server's bot settings.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def config_show(self, interaction: discord.Interaction) -> None:
        data = await self.bot.settings.all(interaction.guild_id)  # type: ignore[attr-defined]
        if not data:
            await interaction.response.send_message("Nothing has been configured yet.", ephemeral=True)
            return
        lines = [f"`{key}` = `{str(value)[:80]}`" for key, value in sorted(data.items())]
        await interaction.response.send_message("\n".join(lines)[:2000], ephemeral=True)

    @app_commands.command(description="Owner: re-upload slash commands to Discord.")
    @app_commands.default_permissions(administrator=True)
    @owner_only()
    async def sync(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        result = await self.bot.sync_commands(force=True)  # type: ignore[attr-defined]
        await interaction.followup.send(f"Command sync: {result}", ephemeral=True)

    @app_commands.command(description="Owner: restart the bot (the start script brings it back).")
    @app_commands.default_permissions(administrator=True)
    @owner_only()
    async def restart(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message("Restarting...", ephemeral=True)
        self.bot.exit_code = 1  # type: ignore[attr-defined]
        await self.bot.close()

    @app_commands.command(description="Owner: stop the bot until it is started again by hand.")
    @app_commands.default_permissions(administrator=True)
    @owner_only()
    async def shutdown(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message("Shutting down.", ephemeral=True)
        self.bot.exit_code = 0  # type: ignore[attr-defined]
        await self.bot.close()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(System(bot))
