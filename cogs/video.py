"""Local video files: play their audio in voice, or post a short clip as a file.

Honest limit: Discord does not let bots send video or screen-share into a voice channel.
The only way is automating a normal user account ("self-bot"), which breaks Discord's
Terms of Service and gets accounts banned, so this module does not do it. What it does:
  * /video play  - queue the audio track of a video file (uses the music player)
  * /video clip  - cut a short piece, shrink it to fit the upload limit, post it in chat
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from core import proc
from core.checks import UserError, missing_permissions, staff_only

log = logging.getLogger("video")

VIDEO_EXTENSIONS = [".mp4", ".mkv", ".webm", ".avi", ".mov", ".m4v"]


def parse_time(text: str) -> float:
    parts = text.strip().split(":")
    if not 1 <= len(parts) <= 3 or not all(p.strip().isdigit() for p in parts):
        raise UserError("Time must look like `90`, `1:30` or `1:02:03`.")
    total = 0
    for part in parts:
        total = total * 60 + int(part)
    return float(total)


class Video(commands.Cog):
    video = app_commands.Group(name="video", description="Local video files", guild_only=True)

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @property
    def music(self):
        cog = self.bot.get_cog("Music")
        if cog is None:
            raise UserError("The video module needs the music module. Set `music = true` in config.toml.")
        return cog

    async def cog_load(self) -> None:
        if self.bot.get_cog("Music") is None:
            raise RuntimeError("video needs the music module: set music = true in [modules]")
        try:
            added, removed, total = await self.music.rescan(
                "video", self.music.library_dirs("video.library_dirs"), VIDEO_EXTENSIONS, "video.extensions")
            log.info("Video library: %d file(s) (%d new, %d removed)", total, added, removed)
        except Exception:  # noqa: BLE001
            log.exception("Video library scan failed")

    async def _complete(self, interaction: discord.Interaction, current: str):
        return await self.music.complete("video", current)

    @video.command(name="play", description="Play the audio of a local video file in your voice channel.")
    @app_commands.describe(file="Start typing a file name, then pick from the list")
    @app_commands.autocomplete(file=_complete)
    async def play(self, interaction: discord.Interaction, file: str) -> None:
        await interaction.response.defer()
        track = await self.music.find("video", file, interaction.user.id)
        if track is None:
            raise UserError(f"No video file matches `{file[:80]}`.")
        await interaction.followup.send(await self.music.enqueue(interaction, [track]))

    @video.command(name="clip", description="Post a short clip of a local video file in this channel.")
    @app_commands.describe(file="Start typing a file name", start="Where the clip begins, e.g. 1:30",
                           seconds="Clip length in seconds")
    @app_commands.autocomplete(file=_complete)
    @app_commands.checks.cooldown(1, 60.0, key=lambda i: i.guild_id)
    async def clip(self, interaction: discord.Interaction, file: str, start: str = "0",
                   seconds: app_commands.Range[int, 1, 600] = 20) -> None:
        cfg = self.bot.cfg
        max_seconds = int(cfg.get("video.clip_max_seconds", 60))
        if seconds > max_seconds:
            raise UserError(f"Clips can be at most {max_seconds} seconds (set in config.toml).")
        missing = missing_permissions(interaction.channel, "send_messages", "attach_files")
        if missing:
            raise UserError(f"I need these permissions here: {', '.join(missing)}.")
        if not proc.which("ffmpeg"):
            raise UserError("ffmpeg is not installed, so clips cannot be made.")
        track = await self.music.find("video", file, interaction.user.id)
        if track is None or not os.path.isfile(track.ref):
            raise UserError(f"No video file matches `{file[:80]}`.")
        begin = parse_time(start)
        await interaction.response.defer()

        # Pick a bitrate that lands under the server's upload limit instead of guessing a quality.
        limit = interaction.guild.filesize_limit - 400_000
        audio_k = 64
        video_k = int(min(1500, max(120, limit * 8 / seconds / 1000 - audio_k) * 0.92))
        height = int(cfg.get("video.clip_height", 480))
        out_dir: Path = cfg.data_dir / "tmp"
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"clip-{interaction.id}.mp4"
        cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
               "-ss", f"{begin:.2f}", "-t", str(seconds), "-i", track.ref,
               "-vf", f"scale=-2:min({height}\\,ih)", "-c:v", "libx264", "-preset", "veryfast",
               "-b:v", f"{video_k}k", "-maxrate", f"{video_k}k", "-bufsize", f"{video_k * 2}k",
               "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", f"{audio_k}k", "-ac", "2",
               "-movflags", "+faststart", "-fs", str(limit), str(out)]
        started = time.perf_counter()
        try:
            code, _, err = await proc.run(cmd, timeout=int(cfg.get("video.clip_timeout_seconds", 300)))
            if code != 0 or not out.is_file() or out.stat().st_size < 1000:
                detail = err.decode("utf-8", "replace").strip().splitlines()[-1:] or ["unknown error"]
                raise UserError(f"ffmpeg could not make the clip: {detail[0][:200]}")
            if out.stat().st_size > interaction.guild.filesize_limit:
                raise UserError("The clip came out larger than this server's upload limit. Try fewer seconds.")
            await interaction.followup.send(
                f"**{discord.utils.escape_markdown(track.title)}** from {start} ({seconds}s, "
                f"made in {time.perf_counter() - started:.0f}s)",
                file=discord.File(out, filename="clip.mp4"))
        finally:
            try:
                out.unlink(missing_ok=True)
            except OSError:
                pass

    @video.command(name="rescan", description="Staff: re-scan the video folders.")
    @staff_only()
    async def rescan(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        folders = self.music.library_dirs("video.library_dirs")
        if not folders:
            raise UserError("No valid folders are set in `video.library_dirs` in config.toml.")
        added, removed, total = await self.music.rescan("video", folders, VIDEO_EXTENSIONS, "video.extensions")
        await interaction.followup.send(f"Videos: {total} file(s). {added} new, {removed} removed.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Video(bot))
