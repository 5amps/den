"""Always-on core commands: /help, /ping, /status, /config, /sync, /restart, /shutdown.

Also keeps a live "RAM ... CPU ..." line in the bot's status (bot.show_usage in config.toml).
"""
from __future__ import annotations

import asyncio
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core import helpdata, proc, sysinfo
from core.checks import UserError, owner_only, staff_only


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


log = logging.getLogger("system")


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}%"


def usage_fields(u: sysinfo.Usage, gpus: list[str]) -> list[tuple[str, str]]:
    """(name, value) lines for the resource part of /status."""
    pc_ram = ("n/a" if u.pc_ram_used_mb is None or not u.pc_ram_total_mb
              else f"{u.pc_ram_used_mb / 1024:.1f} / {u.pc_ram_total_mb / 1024:.1f} GB "
                   f"({u.pc_ram_used_mb / u.pc_ram_total_mb * 100:.0f}%)")
    helpers = (f"{u.helpers} running · RAM {u.helpers_ram_mb:.0f} MB · CPU {_pct(u.helpers_cpu)}"
               if u.helpers else "none running")
    gpu = "\n".join(gpus) if gpus else "No NVIDIA GPU found (other GPUs cannot be read)"
    return [
        ("🤖 Bot", f"RAM **{u.bot_ram_mb:.0f} MB** · CPU **{_pct(u.bot_cpu)}**"),
        ("🎞️ Helpers (ffmpeg, yt-dlp)", helpers),
        ("🖥️ Whole PC", f"CPU {_pct(u.pc_cpu)} of {u.cores} cores · RAM {pc_ram}"),
        ("🎮 GPU (whole PC)", f"{gpu}\n-# The bot does not use the GPU; ffmpeg decodes audio on the CPU."),
    ]


class System(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.sampler = sysinfo.Sampler()
        self.status_text = str(bot.cfg.get("bot.status", "") or "")  # type: ignore[attr-defined]
        self._presence = ""

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if bool(self.bot.cfg.get("bot.show_usage", True)) and not self.usage_loop.is_running():  # type: ignore[attr-defined]
            self.usage_loop.start()

    async def cog_unload(self) -> None:
        self.usage_loop.cancel()

    @tasks.loop(seconds=60)
    async def usage_loop(self) -> None:
        """Show RAM and CPU in the bot's status, so anyone can see what it costs at a glance."""
        u = await asyncio.to_thread(self.sampler.read)
        cpu = (u.bot_cpu or 0) + (u.helpers_cpu or 0)
        text = f"RAM {u.bot_ram_mb + u.helpers_ram_mb:.0f} MB · CPU {cpu:.1f}%"
        if self.status_text:
            text = f"{self.status_text} · {text}"
        if text != self._presence:
            self._presence = text
            await self.bot.change_presence(activity=discord.CustomActivity(name=text[:128]))

    @usage_loop.error
    async def _usage_error(self, error: BaseException) -> None:
        log.warning("Usage status update failed: %s", error)

    # ---- help: always available, built from the commands that are really registered ----
    async def _help_modules(self, interaction: discord.Interaction, current: str):
        choices = []
        for name, heading, _, _, _ in helpdata.collect(self.bot):
            if current.lower() in heading.lower() or current.lower() in name.lower():
                choices.append(app_commands.Choice(name=heading, value=name))
        return choices[:25]

    @app_commands.command(description="How to use the bot: quick start and every command.")
    @app_commands.describe(module="Pick a module to see each of its commands with options")
    @app_commands.autocomplete(module=_help_modules)
    async def help(self, interaction: discord.Interaction, module: str | None = None) -> None:
        sections = helpdata.collect(self.bot)
        colour = discord.Colour.green()
        if module is None:
            embed = discord.Embed(
                title="How to use this bot", colour=colour,
                description="Type `/` in any channel and pick a command; Discord shows each option as you type.\n"
                            "For details on one area use `/help module:` and pick it from the list.")
            for _, heading, summary, start, infos in sections:
                names = " ".join(f"`/{i.name}`" for i in infos)
                embed.add_field(name=heading, value=f"{summary}\n**Start here:** {start}\n{names}"[:1024], inline=False)
            embed.set_footer(text="Some commands are for staff only; Discord hides the ones you cannot use.")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        wanted = module.strip().lower()
        match = next((s for s in sections if s[0].lower() == wanted or wanted in s[1].lower()), None)
        if match is None:
            raise UserError("No such module, or it is switched off. Run `/help` to see what is available.")
        _, heading, summary, start, infos = match
        blocks = []
        for info in infos:
            lock = "" if info.access == "Everyone" else f" · 🔒 {info.access}"
            blocks.append(f"**`{info.usage}`**{lock}\n{info.description}")
        embed = discord.Embed(title=heading, colour=colour, description=f"{summary}\n**Start here:** {start}")
        chunk: list[str] = []
        for block in blocks:                    # embed fields hold 1024 characters each
            if chunk and sum(len(x) + 1 for x in chunk) + len(block) > 1000:
                embed.add_field(name="​", value="\n".join(chunk), inline=False)
                chunk = []
            chunk.append(block)
        if chunk:
            embed.add_field(name="​", value="\n".join(chunk), inline=False)
        embed.set_footer(text="<name> is required, [name] is optional.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(description="Check that the bot is alive and how fast it responds.")
    async def ping(self, interaction: discord.Interaction) -> None:
        latency = self.bot.latency
        text = "connecting..." if latency != latency or latency == float("inf") else f"{latency * 1000:.0f} ms"
        await interaction.response.send_message(f"Pong. Gateway latency: {text}", ephemeral=True)

    @app_commands.command(description="Show RAM, CPU and GPU use, uptime and which modules are running.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    @staff_only()
    async def status(self, interaction: discord.Interaction) -> None:
        bot = self.bot
        cfg = bot.cfg  # type: ignore[attr-defined]
        await interaction.response.defer(ephemeral=True, thinking=True)
        usage, gpus = await asyncio.gather(asyncio.to_thread(self.sampler.read), sysinfo.gpu_report())
        embed = discord.Embed(title="Bot status", colour=discord.Colour.green())
        for name, value in usage_fields(usage, gpus):
            embed.add_field(name=name, value=value[:1024], inline=False)
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
        embed.set_footer(text="CPU % is of the whole processor (all cores = 100%), like Task Manager.")
        await interaction.followup.send(embed=embed, ephemeral=True)

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
