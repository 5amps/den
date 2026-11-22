"""Music: local library first, optional YouTube, queue, playlists, resume after restart.

CPU notes
  * Audio that is already Opus (most .opus/.ogg files, everything from YouTube) is passed
    through untouched at 100% volume: ffmpeg only repackages it (about 1-3% of one core).
  * Everything else is encoded once by ffmpeg straight to Opus. Python never touches PCM.
  * Changing volume or seeking restarts ffmpeg at the current position (a short gap).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import discord
from discord import app_commands
from discord.ext import commands

from core import proc, sysinfo
from core.checks import UserError, is_staff, missing_permissions, staff_only

log = logging.getLogger("music")

SCHEMA = """
CREATE TABLE IF NOT EXISTS library (
    id    INTEGER PRIMARY KEY,
    path  TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    kind  TEXT NOT NULL DEFAULT 'audio'
);
CREATE TABLE IF NOT EXISTS playlists (
    id       INTEGER PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    owner_id INTEGER NOT NULL,
    name     TEXT NOT NULL COLLATE NOCASE,
    UNIQUE (guild_id, name)
);
CREATE TABLE IF NOT EXISTS playlist_tracks (
    playlist_id INTEGER NOT NULL REFERENCES playlists (id) ON DELETE CASCADE,
    pos         INTEGER NOT NULL,
    data        TEXT NOT NULL,
    PRIMARY KEY (playlist_id, pos)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS queue_state (
    guild_id        INTEGER PRIMARY KEY,
    channel_id      INTEGER NOT NULL,
    text_channel_id INTEGER,
    position        REAL NOT NULL DEFAULT 0,
    loop            TEXT NOT NULL DEFAULT 'off',
    tracks          TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cache_index (
    key       TEXT PRIMARY KEY,
    path      TEXT NOT NULL,
    bytes     INTEGER NOT NULL,
    last_used REAL NOT NULL
) WITHOUT ROWID;
"""

AUDIO_EXTENSIONS = [".mp3", ".flac", ".ogg", ".opus", ".m4a", ".wav", ".aac", ".wma", ".mp4"]
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
FRAME = 0.02            # seconds of audio per Opus packet sent to Discord
MAX_FAILS = 3           # consecutive broken tracks before playback stops
LOOP_MODES = ("off", "track", "queue")
RESCAN_EVERY = 20.0     # seconds between automatic library re-scans triggered by a failed search
# ffmpeg's own messages go nowhere. discord.py needs a real file object here, not subprocess.DEVNULL.
_NULL = open(os.devnull, "wb")  # noqa: SIM115 - lives as long as the process


# ---------------------------------------------------------------------------- helpers
def fmt_time(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "?:??"
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def parse_time(text: str) -> float:
    """'90', '1:30' or '1:02:03' -> seconds."""
    parts = text.strip().split(":")
    if not 1 <= len(parts) <= 3 or not all(p.strip().isdigit() for p in parts):
        raise UserError("Time must look like `90`, `1:30` or `1:02:03`.")
    total = 0
    for part in parts:
        total = total * 60 + int(part)
    return float(total)


def like(text: str) -> str:
    """Escape user text for a SQL LIKE '%...%' search."""
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def scan_folders(folders: list[str], extensions: set[str]) -> dict[str, str]:
    """Walk library folders (runs in a thread). Returns {full path: title}."""
    found: dict[str, str] = {}
    for base in folders:
        for root, dirnames, files in os.walk(base, followlinks=False):
            dirnames[:] = [d for d in dirnames if not d.startswith((".", "$"))]
            for name in files:
                stem, ext = os.path.splitext(name)
                if ext.lower() in extensions:
                    found[os.path.join(root, name)] = stem
    return found


@dataclass(slots=True)
class Track:
    kind: str                       # "local" or "yt"
    ref: str                        # file path, or YouTube video id
    title: str
    duration: float | None = None
    url: str | None = None
    requester: int = 0
    codec: str | None = None        # filled in by probe(): "opus" means passthrough is possible
    probed: bool = False

    def to_json(self) -> dict[str, Any]:
        return {"k": self.kind, "r": self.ref, "t": self.title, "d": self.duration,
                "u": self.url, "q": self.requester}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Track":
        return cls(kind=data["k"], ref=data["r"], title=data.get("t") or "Unknown",
                   duration=data.get("d"), url=data.get("u"), requester=data.get("q") or 0)

    @property
    def label(self) -> str:
        title = discord.utils.escape_markdown(self.title)
        return f"[{title}](<{self.url}>)" if self.url else title


class CountedSource(discord.AudioSource):
    """Wraps the ffmpeg source and counts packets so we always know the play position."""

    def __init__(self, inner: discord.FFmpegOpusAudio, offset: float):
        self.inner = inner
        self.offset = offset
        self.frames = 0

    def read(self) -> bytes:
        data = self.inner.read()
        if data:
            self.frames += 1
        return data

    def is_opus(self) -> bool:
        return True

    def cleanup(self) -> None:
        self.inner.cleanup()        # kills and reaps ffmpeg

    @property
    def played(self) -> float:
        return self.frames * FRAME

    @property
    def position(self) -> float:
        return self.offset + self.played


def build_source(path: str, *, offset: float, volume: int, passthrough: bool,
                 bitrate: int, complexity: int) -> CountedSource:
    """Start ffmpeg for one track. Blocking (process spawn), so call it in a thread."""
    before = "-nostdin -hide_banner"
    if offset > 0:
        before += f" -ss {offset:.2f}"
    options = "-vn -sn -dn"                              # audio only: also handles video files
    if passthrough:
        options += " -avoid_negative_ts make_zero"
    else:
        options += f" -compression_level {complexity}"
        if volume != 100:
            options += f" -filter:a volume={volume / 100:.2f}"
    inner = discord.FFmpegOpusAudio(
        path, bitrate=bitrate, codec="copy" if passthrough else None,
        before_options=before, options=options, stderr=_NULL,
    )
    return CountedSource(inner, offset)


async def probe(path: str) -> tuple[str | None, float | None]:
    """(codec usable for passthrough or None, duration in seconds or None)."""
    if not proc.which("ffprobe"):
        return None, None
    try:
        code, out, _ = await proc.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0", "-read_intervals", "%+#4",
             "-show_entries", "stream=codec_name:format=duration:packet=duration_time",
             "-of", "json", path],
            timeout=20,
        )
        if code != 0:
            return None, None
        data = json.loads(out.decode("utf-8", "replace"))
    except (proc.ProcessError, ValueError):
        return None, None
    duration = None
    try:
        duration = float(data.get("format", {}).get("duration"))
    except (TypeError, ValueError):
        pass
    streams = data.get("streams") or []
    codec = streams[0].get("codec_name") if streams else None
    if codec == "opus":
        # Discord needs 20 ms packets. Opus files with other frame sizes must be re-encoded.
        packets = data.get("packets") or []
        try:
            ok = bool(packets) and all(abs(float(p["duration_time"]) - FRAME) < 0.0005 for p in packets)
        except (KeyError, TypeError, ValueError):
            ok = False
        if not ok:
            codec = "opus-reencode"
    return codec, duration


# ---------------------------------------------------------------------------- YouTube
class YouTube:
    """yt-dlp as a short-lived subprocess plus an on-disk LRU cache of downloaded audio."""

    def __init__(self, cog: "Music"):
        self.cog = cog
        self.db = cog.bot.db
        cfg = cog.bot.cfg
        self.cache_dir: Path = cfg.path_for("music.cache_dir", "data/cache")
        self.cache_max = int(cfg.get("music.cache_max_mb", 1024)) * 1_048_576
        self.max_minutes = int(cfg.get("music.max_track_minutes", 180))
        self.extra = [str(a) for a in (cfg.get("music.ytdlp_args", []) or [])]
        self._downloads: dict[str, asyncio.Task] = {}

    def _base(self) -> list[str]:
        exe = proc.which("yt-dlp")
        if not exe:
            raise UserError("yt-dlp is not installed, so YouTube is unavailable. "
                            "Install it with `pip install -U yt-dlp` (or `winget install yt-dlp`).")
        return [exe, "--ignore-config", "--no-playlist", "--no-warnings", "--encoding", "utf-8", *self.extra]

    @staticmethod
    def _error(stderr: bytes, fallback: str) -> str:
        lines = [ln.strip() for ln in stderr.decode("utf-8", "replace").splitlines() if ln.strip()]
        errors = [ln for ln in lines if ln.startswith("ERROR")]
        text = (errors or lines or [fallback])[-1]
        return re.sub(r"^ERROR:\s*(\[[^\]]+\]\s*)?", "", text)[:300]

    async def resolve(self, query: str, requester: int) -> Track:
        """Look up one video by link or search text. No download yet."""
        query = query.strip()
        if re.match(r"^https?://", query, re.I):
            host = (urlparse(query).hostname or "").lower()
            if host not in YOUTUBE_HOSTS:
                raise UserError("Only YouTube links are supported. For other audio, add the file to your library.")
            target = query
        else:
            target = f"ytsearch1:{query}"
        code, out, err = await proc.run(
            [*self._base(), "--skip-download", "--print",
             "%(id)s\t%(duration)s\t%(is_live)s\t%(webpage_url)s\t%(title)s", "--", target],
            timeout=60,
        )
        line = next((ln for ln in out.decode("utf-8", "replace").splitlines() if ln.count("\t") >= 4), None)
        if code != 0 or line is None:
            raise UserError(f"YouTube lookup failed: {self._error(err, 'no result')}. "
                            "If this keeps happening, update yt-dlp.")
        video_id, duration, is_live, url, title = line.split("\t", 4)
        if is_live == "True":
            raise UserError("Live streams are not supported.")
        try:
            seconds: float | None = float(duration)
        except ValueError:
            seconds = None
        if seconds is not None and seconds > self.max_minutes * 60:
            raise UserError(f"That video is longer than the {self.max_minutes}-minute limit.")
        return Track("yt", video_id, title, seconds, url, requester)

    async def ensure(self, track: Track) -> str:
        """Path of the cached audio for a track, downloading it first if needed."""
        row = await self.db.fetchone("SELECT path FROM cache_index WHERE key = ?", (track.ref,))
        if row is not None:
            if os.path.isfile(row["path"]):
                self.db.write_later("UPDATE cache_index SET last_used = ? WHERE key = ?", (time.time(), track.ref))
                return row["path"]
            await self.db.execute("DELETE FROM cache_index WHERE key = ?", (track.ref,))
        task = self._downloads.get(track.ref)
        if task is None:
            task = asyncio.create_task(self._download(track), name=f"yt-{track.ref}")
            self._downloads[track.ref] = task
            task.add_done_callback(lambda _t, key=track.ref: self._downloads.pop(key, None))
        # shield: one listener giving up must not cancel a download others are waiting on
        return await asyncio.shield(task)

    async def _download(self, track: Track) -> str:
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise UserError(f"Cannot create the cache folder: {exc}") from exc
        if sysinfo.free_disk_mb(self.cache_dir) < 200:
            raise UserError("Less than 200 MB of disk space is free, so nothing new can be downloaded.")
        url = track.url or f"https://www.youtube.com/watch?v={track.ref}"
        code, out, err = await proc.run(
            [*self._base(), "--no-progress", "-f", "bestaudio[acodec=opus]/bestaudio/best",
             "--max-filesize", "300M", "-o", str(self.cache_dir / "%(id)s.%(ext)s"),
             "--print", "after_move:filepath", "--", url],
            timeout=900,
        )
        lines = [ln.strip() for ln in out.decode("utf-8", "replace").splitlines() if ln.strip()]
        path = lines[-1] if lines else ""
        if code != 0 or not path or not os.path.isfile(path):
            raise UserError(f"Download failed: {self._error(err, 'file too large or unavailable')}")
        size = os.path.getsize(path)
        await self.db.execute(
            "INSERT INTO cache_index (key, path, bytes, last_used) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (key) DO UPDATE SET path = excluded.path, bytes = excluded.bytes, "
            "last_used = excluded.last_used",
            (track.ref, path, size, time.time()),
        )
        await self.evict()
        return path

    async def evict(self) -> None:
        """Delete least-recently-used files until the cache fits its size cap."""
        rows = await self.db.fetchall("SELECT key, path, bytes FROM cache_index ORDER BY last_used")
        total = sum(row["bytes"] for row in rows)
        if total <= self.cache_max:
            return
        keep = self.cog.cache_keys_in_use()
        for row in rows:
            if total <= self.cache_max:
                break
            if row["key"] in keep:
                continue
            try:
                os.remove(row["path"])
            except FileNotFoundError:
                pass
            except OSError as exc:
                log.warning("Cannot delete cached file %s: %s", row["path"], exc)
                continue
            total -= row["bytes"]
            await self.db.execute("DELETE FROM cache_index WHERE key = ?", (row["key"],))

    async def startup_cleanup(self) -> None:
        """Remove half-finished downloads and index rows whose file is gone."""
        def leftovers() -> None:
            if self.cache_dir.is_dir():
                for item in self.cache_dir.iterdir():
                    if item.suffix in (".part", ".ytdl"):
                        try:
                            item.unlink()
                        except OSError:
                            pass
        await asyncio.to_thread(leftovers)
        rows = await self.db.fetchall("SELECT key, path FROM cache_index")
        gone = [(row["key"],) for row in rows if not os.path.isfile(row["path"])]
        if gone:
            await self.db.executemany("DELETE FROM cache_index WHERE key = ?", gone)

    def cancel(self) -> None:
        for task in list(self._downloads.values()):
            task.cancel()


# ---------------------------------------------------------------------------- player
class Player:
    """Queue and playback state for one server."""

    def __init__(self, cog: "Music", guild: discord.Guild):
        self.cog = cog
        self.guild = guild
        self.queue: deque[Track] = deque()
        self.current: Track | None = None
        self.loop_mode = "off"
        self.volume = 100
        self.text_channel_id: int | None = None
        self.source: CountedSource | None = None
        self.auto_paused = False
        self._token = 0                 # identifies the active ffmpeg; stale callbacks are ignored
        self._skip = False
        self._fails = 0
        self._lock = asyncio.Lock()
        self._idle: asyncio.TimerHandle | None = None

    # ---- state ----
    @property
    def vc(self) -> discord.VoiceClient | None:
        client = self.guild.voice_client
        return client if isinstance(client, discord.VoiceClient) else None

    @property
    def position(self) -> float:
        return self.source.position if self.source else 0.0

    def is_active(self) -> bool:
        vc = self.vc
        return bool(vc and (vc.is_playing() or vc.is_paused()))

    def humans(self) -> int:
        vc = self.vc
        if vc is None or vc.channel is None:
            return 0
        count = 0
        for user_id in vc.channel.voice_states:
            member = self.guild.get_member(user_id)
            if user_id != self.guild.me.id and not (member and member.bot):
                count += 1
        return count

    def save(self, position: float | None = None) -> None:
        """Persist the queue so it can be resumed after a restart (batched background write)."""
        vc = self.vc
        tracks = ([self.current] if self.current else []) + list(self.queue)
        if not tracks or vc is None or vc.channel is None:
            self.cog.bot.db.write_later("DELETE FROM queue_state WHERE guild_id = ?", (self.guild.id,))
            return
        self.cog.bot.db.write_later(
            "INSERT OR REPLACE INTO queue_state (guild_id, channel_id, text_channel_id, position, loop, tracks) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (self.guild.id, vc.channel.id, self.text_channel_id,
             self.position if position is None else position, self.loop_mode,
             json.dumps([t.to_json() for t in tracks], separators=(",", ":"))),
        )

    async def announce(self, text: str) -> None:
        channel = self.guild.get_channel_or_thread(self.text_channel_id) if self.text_channel_id else None
        if channel is None or missing_permissions(channel, "view_channel", "send_messages"):
            return
        try:
            await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass

    # ---- idle timer (one timer handle, no polling) ----
    def cancel_idle(self) -> None:
        if self._idle is not None:
            self._idle.cancel()
            self._idle = None

    def schedule_idle(self) -> None:
        self.cancel_idle()
        minutes = float(self.cog.bot.cfg.get("music.idle_disconnect_minutes", 5))
        if minutes <= 0:
            return
        loop = asyncio.get_running_loop()
        self._idle = loop.call_later(
            minutes * 60, lambda: asyncio.create_task(self.cog.destroy(self, "Left the voice channel after being idle."))
        )

    # ---- playback ----
    async def start_next(self, offset: float = 0.0, resume_current: bool = False) -> None:
        """Advance to the next playable track (or go idle when the queue is empty)."""
        async with self._lock:
            while True:
                vc = self.vc
                if vc is None or not vc.is_connected():
                    return
                if resume_current and self.current is not None:
                    track = self.current
                elif self.loop_mode == "track" and self.current is not None and not self._skip:
                    track = self.current
                else:
                    if self.loop_mode == "queue" and self.current is not None:
                        self.queue.append(self.current)
                    track = self.queue.popleft() if self.queue else None
                self._skip = False
                resume_current = False
                self.current = track
                if track is None:
                    self.source = None
                    self.save()
                    self.schedule_idle()
                    return
                try:
                    await self._start(track, offset)
                    return
                except UserError as exc:
                    await self.announce(f"Skipping **{discord.utils.escape_markdown(track.title)}**: {exc}")
                except Exception:  # noqa: BLE001
                    log.exception("Could not start %s", track.ref)
                    await self.announce(f"Skipping **{discord.utils.escape_markdown(track.title)}**: it could not be played.")
                offset = 0.0
                self._fails += 1
                if self.loop_mode != "off":
                    self.loop_mode = "off"          # never spin forever on a broken track
                self.current = None
                if self._fails >= MAX_FAILS:
                    self._fails = 0
                    self.queue.clear()
                    self.source = None
                    self.save()
                    self.schedule_idle()
                    await self.announce(f"Stopped: {MAX_FAILS} tracks in a row could not be played.")
                    return

    async def _start(self, track: Track, offset: float) -> None:
        vc = self.vc
        if vc is None or not vc.is_connected():
            raise UserError("I am not connected to a voice channel.")
        path = await self.cog.path_for(track)
        if not track.probed:
            track.codec, duration = await probe(path)
            track.duration = duration or track.duration
            track.probed = True
        if track.duration and offset >= track.duration:
            offset = 0.0
        cfg = self.cog.bot.cfg
        passthrough = (track.codec == "opus" and self.volume == 100
                       and bool(cfg.get("music.passthrough", True)))
        try:
            source = await asyncio.to_thread(
                build_source, path, offset=offset, volume=self.volume, passthrough=passthrough,
                bitrate=int(cfg.get("music.bitrate_kbps", 96)),
                complexity=int(cfg.get("music.opus_complexity", 5)),
            )
        except discord.ClientException as exc:
            raise UserError("ffmpeg is not installed or not on PATH. See the README troubleshooting section.") from exc
        self._token += 1
        token = self._token
        loop = asyncio.get_running_loop()
        if vc.is_playing() or vc.is_paused():
            vc.stop()
        try:
            vc.play(source, after=lambda error: loop.call_soon_threadsafe(self._after, token, error))
        except discord.ClientException as exc:
            source.cleanup()
            raise UserError(f"Could not start playback: {exc}") from exc
        self.source = source
        self.auto_paused = False
        self.cancel_idle()
        self.save(position=offset)
        if bool(cfg.get("music.announce_tracks", False)):
            asyncio.create_task(self.announce(f"Now playing: **{discord.utils.escape_markdown(track.title)}**"))
        self.cog.prebuffer(self)
        if bool(cfg.get("bot.voice_debug", False)):
            asyncio.create_task(self._debug_report())
        if self.humans() == 0:
            self.cog.pause_for_empty(self)

    async def _debug_report(self) -> None:
        """voice_debug only: log the connection's health a few seconds into a track."""
        from cogs.system import voice_report
        for delay in (3, 12):
            await asyncio.sleep(delay)
            vc = self.vc
            if vc is None:
                return
            log.info("VOICE REPORT: %s", voice_report(vc))

    def _after(self, token: int, error: Exception | None) -> None:
        """Runs on the event loop when ffmpeg's stream ends."""
        if token != self._token or self.cog.closing:
            return                      # superseded by a seek/volume restart, or shutting down
        if error is not None:
            log.warning("Playback error in guild %s: %s", self.guild.id, error)
        source, track = self.source, self.current
        played = source.played if source else 0.0
        expected = (track.duration - source.offset) if (track and track.duration and source) else None
        if played < 1.0 and (expected is None or expected > 3.0) and not self._skip:
            self._fails += 1            # ended almost instantly: unreadable or broken file
            if self.loop_mode == "track":
                self.loop_mode = "off"
            if self._fails >= MAX_FAILS:
                self._fails = 0
                self.queue.clear()
                self.current = None
                self.save()
                self.schedule_idle()
                asyncio.create_task(self.announce(f"Stopped: {MAX_FAILS} tracks in a row could not be played."))
                return
            if track is not None:
                asyncio.create_task(self.announce(
                    f"Skipping **{discord.utils.escape_markdown(track.title)}**: ffmpeg could not read it."))
        else:
            self._fails = 0
        asyncio.create_task(self.start_next())

    async def restart_at(self, offset: float) -> None:
        """Re-open the current track at a position (used by seek and volume changes)."""
        if self.current is None:
            raise UserError("Nothing is playing.")
        async with self._lock:
            vc = self.vc
            was_paused = bool(vc and vc.is_paused())
            await self._start(self.current, max(0.0, offset))
            if was_paused and self.vc:
                self.vc.pause()

    def skip(self) -> None:
        self._skip = True
        vc = self.vc
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()                   # triggers _after -> start_next
        else:
            asyncio.create_task(self.start_next())

    def stop(self) -> None:
        self._token += 1                # invalidate the pending _after callback
        self.queue.clear()
        self.current = None
        self.source = None
        vc = self.vc
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()
        self.save()
        self.schedule_idle()


# ---------------------------------------------------------------------------- cog
class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.players: dict[int, Player] = {}
        self.youtube_enabled = bool(bot.cfg.get("music.youtube", False))
        self.youtube = YouTube(self)
        self.max_queue = int(bot.cfg.get("music.max_queue", 500))
        self.closing = False
        self._restored = False
        self._last_scan = 0.0
        self._tasks: set[asyncio.Task] = set()

    # ---- lifecycle ----
    async def cog_load(self) -> None:
        await self.bot.db.script(SCHEMA)
        if not proc.which("ffmpeg"):
            log.warning("ffmpeg was not found. Music commands will fail until it is installed.")
        if bool(self.bot.cfg.get("music.scan_on_start", True)):
            self._spawn(self._scan_quietly())
        if self.youtube_enabled:
            self._spawn(self.youtube.startup_cleanup())

    async def cog_unload(self) -> None:
        self.closing = True
        self.youtube.cancel()
        for task in list(self._tasks):
            task.cancel()
        for player in list(self.players.values()):
            player.cancel_idle()
            player.save()               # exact position, so a restart resumes mid-track
            vc = player.vc
            if vc is not None:
                vc.stop()               # kills ffmpeg
                try:
                    await vc.disconnect(force=True)
                except Exception:  # noqa: BLE001
                    pass
        self.players.clear()

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _scan_quietly(self) -> None:
        try:
            added, removed, total = await self.rescan("audio", self.library_dirs(), AUDIO_EXTENSIONS, "music.extensions")
            log.info("Music library: %d track(s) (%d new, %d removed)", total, added, removed)
        except Exception:  # noqa: BLE001
            log.exception("Library scan failed")

    # ---- library ----
    def library_dirs(self, key: str = "music.library_dirs") -> list[str]:
        folders = []
        for item in self.bot.cfg.get(key, []) or []:
            path = Path(str(item)).expanduser()
            if path.is_dir():
                folders.append(str(path))
            else:
                log.warning("%s: folder not found: %s", key, item)
        return folders

    async def rescan(self, kind: str, folders: list[str], default_ext: list[str], ext_key: str) -> tuple[int, int, int]:
        """Sync the library table with what is on disk. Returns (added, removed, total)."""
        extensions = {str(e).lower() if str(e).startswith(".") else f".{str(e).lower()}"
                      for e in (self.bot.cfg.get(ext_key) or default_ext)}
        found = await asyncio.to_thread(scan_folders, folders, extensions)
        rows = await self.bot.db.fetchall("SELECT path FROM library WHERE kind = ?", (kind,))
        known = {row["path"] for row in rows}
        new = [(path, title, kind) for path, title in found.items() if path not in known]
        gone = [(path,) for path in known if path not in found]
        if new:
            await self.bot.db.executemany("INSERT OR IGNORE INTO library (path, title, kind) VALUES (?, ?, ?)", new)
        if gone:
            await self.bot.db.executemany("DELETE FROM library WHERE path = ?", gone)
        return len(new), len(gone), len(found)

    async def refresh_library(self) -> bool:
        """Re-scan the music folders so files added while the bot runs are found.

        Called when a search comes up empty; at most once every RESCAN_EVERY seconds.
        Returns True if new files were added.
        """
        now = time.monotonic()
        if now - self._last_scan < RESCAN_EVERY:
            return False
        self._last_scan = now
        try:
            added, _, _ = await self.rescan("audio", self.library_dirs(), AUDIO_EXTENSIONS, "music.extensions")
        except Exception:  # noqa: BLE001
            log.exception("Library re-scan failed")
            return False
        return added > 0

    async def search(self, kind: str, text: str, limit: int) -> list:
        """Library rows matching text: exact title first, then shorter titles."""
        return await self.bot.db.fetchall(
            "SELECT id, path, title FROM library WHERE kind = ? AND (title LIKE ? ESCAPE '\\' OR path LIKE ? ESCAPE '\\') "
            "ORDER BY (title = ? COLLATE NOCASE) DESC, (title LIKE ? ESCAPE '\\') DESC, length(title), title LIMIT ?",
            (kind, like(text), like(text), text, like(text), limit),
        )

    async def find(self, kind: str, query: str, requester: int) -> Track | None:
        """Resolve an autocomplete value ('#id') or free text to a library track."""
        query = query.strip()
        row = None
        if query.startswith("#") and query[1:].isdigit():
            row = await self.bot.db.fetchone(
                "SELECT path, title FROM library WHERE id = ? AND kind = ?", (int(query[1:]), kind))
        if row is None:
            rows = await self.search(kind, query, 1)
            row = rows[0] if rows else None
        if row is None:
            return None
        return Track("local", row["path"], row["title"], requester=requester)

    async def complete(self, kind: str, current: str) -> list[app_commands.Choice[str]]:
        try:
            if current.strip():
                rows = await self.search(kind, current.strip(), 25)
            else:
                rows = await self.bot.db.fetchall(
                    "SELECT id, title FROM library WHERE kind = ? ORDER BY title LIMIT 25", (kind,))
        except Exception:  # noqa: BLE001 - autocomplete must never raise
            return []
        if not rows and kind == "audio" and current.strip():
            # Maybe the file was copied in after the last scan: look again in the background
            # so the next keystroke finds it.
            self._spawn(self.refresh_library())
        return [app_commands.Choice(name=row["title"][:100], value=f"#{row['id']}") for row in rows]

    async def path_for(self, track: Track) -> str:
        if track.kind == "local":
            if not os.path.isfile(track.ref):
                raise UserError("the file is missing (moved, deleted, or its drive is unplugged)")
            return track.ref
        if not self.youtube_enabled:
            raise UserError("YouTube is switched off in config.toml")
        return await self.youtube.ensure(track)

    # ---- helpers used by players ----
    def cache_keys_in_use(self) -> set[str]:
        keys: set[str] = set()
        for player in self.players.values():
            upcoming = ([player.current] if player.current else []) + list(player.queue)[:2]
            keys.update(t.ref for t in upcoming if t.kind == "yt")
        return keys

    def prebuffer(self, player: Player) -> None:
        """Get the next track ready while the current one plays (download or probe)."""
        if not player.queue:
            return
        nxt = player.queue[0]

        async def work() -> None:
            try:
                path = await self.path_for(nxt)
                if not nxt.probed:
                    nxt.codec, duration = await probe(path)
                    nxt.duration = duration or nxt.duration
                    nxt.probed = True
            except UserError:
                pass                    # reported when the track is actually reached
            except Exception:  # noqa: BLE001
                log.exception("Pre-buffer failed for %s", nxt.ref)

        self._spawn(work())

    def pause_for_empty(self, player: Player) -> None:
        vc = player.vc
        if vc and vc.is_playing():
            vc.pause()
            player.auto_paused = True
            player.save()
        player.schedule_idle()

    def player(self, guild: discord.Guild) -> Player:
        player = self.players.get(guild.id)
        if player is None:
            player = self.players[guild.id] = Player(self, guild)
        return player

    async def destroy(self, player: Player, reason: str | None = None, keep_state: bool = False) -> None:
        """Stop everything for one server and leave voice."""
        self.players.pop(player.guild.id, None)
        player.cancel_idle()
        player._token += 1
        player.queue.clear()
        player.current = None
        vc = player.vc
        if vc is not None:
            vc.stop()
            try:
                await vc.disconnect(force=True)
            except Exception:  # noqa: BLE001
                pass
        if not keep_state:
            self.bot.db.write_later("DELETE FROM queue_state WHERE guild_id = ?", (player.guild.id,))
        if reason:
            await player.announce(reason)

    # ---- connecting ----
    async def connect(self, interaction: discord.Interaction) -> Player:
        """Join (or confirm we share) the caller's voice channel and return the player."""
        guild = interaction.guild
        assert guild is not None
        state = getattr(interaction.user, "voice", None)
        if state is None or state.channel is None:
            raise UserError("Join a voice channel first.")
        channel = state.channel
        if isinstance(channel, discord.StageChannel):
            raise UserError("Stage channels are not supported. Use a normal voice channel.")
        player = self.player(guild)
        vc = player.vc
        if vc is not None and vc.is_connected():
            if vc.channel.id != channel.id:
                if player.is_active() and player.humans() > 0:
                    raise UserError(f"I am already playing in {vc.channel.mention}.")
                await self._check_voice_perms(channel)
                await vc.move_to(channel)
        else:
            await self._check_voice_perms(channel)
            if guild.voice_client is not None:          # stale client from a dropped connection
                try:
                    await guild.voice_client.disconnect(force=True)
                except Exception:  # noqa: BLE001
                    pass
            try:
                await channel.connect(timeout=20, reconnect=True, self_deaf=True)
            except asyncio.TimeoutError as exc:
                raise UserError("Connecting to voice timed out. Check the internet connection and try again.") from exc
            except discord.ClientException as exc:
                raise UserError(f"Could not join voice: {exc}") from exc
            except RuntimeError as exc:
                raise UserError(f"Voice support is not installed correctly ({exc}). Run the setup script again.") from exc
            player.volume = int(await self.bot.settings.get(guild.id, "music_volume", self.bot.cfg.get("music.default_volume", 100)))
        player.text_channel_id = interaction.channel_id
        player.cancel_idle()
        return player

    @staticmethod
    async def _check_voice_perms(channel: discord.VoiceChannel) -> None:
        missing = missing_permissions(channel, "view_channel", "connect", "speak")
        if missing:
            raise UserError(f"I need these permissions in {channel.mention}: {', '.join(missing)}.")
        if channel.user_limit and len(channel.voice_states) >= channel.user_limit \
                and not channel.permissions_for(channel.guild.me).move_members:
            raise UserError(f"{channel.mention} is full.")

    async def controls(self, interaction: discord.Interaction) -> Player:
        """Player for a control command; the caller must share the bot's voice channel."""
        guild = interaction.guild
        assert guild is not None
        player = self.players.get(guild.id)
        if player is None or player.vc is None or not player.vc.is_connected():
            raise UserError("I am not in a voice channel. Use `/play` first.")
        state = getattr(interaction.user, "voice", None)
        same = state is not None and state.channel is not None and state.channel.id == player.vc.channel.id
        if not same and not await is_staff(interaction):
            raise UserError(f"Join {player.vc.channel.mention} to control the music.")
        return player

    async def enqueue(self, interaction: discord.Interaction, tracks: list[Track], *, shuffle: bool = False) -> str:
        """Connect if needed, add tracks, start playback. Returns a message for the user."""
        if not tracks:
            raise UserError("Nothing to add.")
        player = await self.connect(interaction)
        room = self.max_queue - len(player.queue)
        if room <= 0:
            raise UserError(f"The queue is full ({self.max_queue} tracks).")
        if shuffle:
            random.shuffle(tracks)
        added = tracks[:room]
        player.queue.extend(added)
        starting = not player.is_active() and player.current is None
        if starting:
            await player.start_next()
        else:
            player.save()
            if len(player.queue) == len(added):
                self.prebuffer(player)
        if len(added) == 1:
            track = added[0]
            if starting and player.current is track:
                return f"Now playing: **{track.label}** ({fmt_time(track.duration)})"
            if starting:
                return "That track could not be played."
            return f"Queued #{len(player.queue)}: **{track.label}**"
        note = f" ({len(tracks) - len(added)} left out, queue is full)" if len(added) < len(tracks) else ""
        return f"Added {len(added)} tracks{note}."

    # ---- events ----
    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._restored:
            return
        self._restored = True
        if bool(self.bot.cfg.get("music.resume_on_start", True)):
            self._spawn(self._restore())
        else:
            await self.bot.db.execute("DELETE FROM queue_state")

    async def _restore(self) -> None:
        """Pick up saved queues after a restart, if people are still in the channel."""
        rows = await self.bot.db.fetchall("SELECT * FROM queue_state")
        for row in rows:
            guild = self.bot.get_guild(row["guild_id"])
            channel = guild.get_channel(row["channel_id"]) if guild else None
            try:
                tracks = [Track.from_json(item) for item in json.loads(row["tracks"])]
            except (ValueError, KeyError, TypeError):
                tracks = []
            listeners = [uid for uid in channel.voice_states if uid != self.bot.user.id] if isinstance(channel, discord.VoiceChannel) else []
            if not tracks or not listeners or missing_permissions(channel, "view_channel", "connect", "speak"):
                await self.bot.db.execute("DELETE FROM queue_state WHERE guild_id = ?", (row["guild_id"],))
                continue
            try:
                if guild.voice_client is None:
                    await channel.connect(timeout=20, reconnect=True, self_deaf=True)
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not resume music in %s: %s", guild.name, exc)
                await self.bot.db.execute("DELETE FROM queue_state WHERE guild_id = ?", (row["guild_id"],))
                continue
            player = self.player(guild)
            player.text_channel_id = row["text_channel_id"]
            player.loop_mode = row["loop"] if row["loop"] in LOOP_MODES else "off"
            player.volume = int(await self.bot.settings.get(guild.id, "music_volume", self.bot.cfg.get("music.default_volume", 100)))
            player.current = tracks[0]
            player.queue.extend(tracks[1:])
            await player.start_next(offset=max(0.0, row["position"] - 2.0), resume_current=True)
            log.info("Resumed %d track(s) in %s", len(tracks), guild.name)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState,
                                    after: discord.VoiceState) -> None:
        player = self.players.get(member.guild.id)
        if player is None or self.closing or before.channel == after.channel:
            return
        if member.id == self.bot.user.id:
            if after.channel is None:
                # Possibly a brief network reconnect; only clean up if we are still gone shortly after.
                await asyncio.sleep(5)
                vc = player.vc
                if self.players.get(member.guild.id) is player and (vc is None or not vc.is_connected()):
                    await self.destroy(player)
            return
        vc = player.vc
        if vc is None or vc.channel is None or vc.channel not in (before.channel, after.channel):
            return
        if player.humans() == 0:
            self.pause_for_empty(player)
        else:
            if player.auto_paused and vc.is_paused():
                vc.resume()
            player.auto_paused = False
            if player.current is not None:
                player.cancel_idle()

    # ---- commands: playing ----
    @app_commands.command(description="Play a track from the local music library.")
    @app_commands.describe(query="Start typing a title, then pick from the list")
    @app_commands.guild_only()
    async def play(self, interaction: discord.Interaction, query: str) -> None:
        await interaction.response.defer()
        track = await self.find("audio", query, interaction.user.id)
        if track is None:
            self._last_scan = 0.0                           # an explicit /play always looks again
            if await self.refresh_library():
                track = await self.find("audio", query, interaction.user.id)
        if track is None:
            hint = " Use `/youtube` for online music." if self.youtube_enabled else ""
            raise UserError(f"Nothing in the library matches `{query[:80]}`.{hint}")
        await interaction.followup.send(await self.enqueue(interaction, [track]))

    @play.autocomplete("query")
    async def _play_complete(self, interaction: discord.Interaction, current: str):
        return await self.complete("audio", current)

    @app_commands.command(description="Queue every library track matching some text (or the whole library).")
    @app_commands.describe(search="Part of a title or folder name; leave empty for everything",
                           shuffle="Add them in random order")
    @app_commands.guild_only()
    async def playall(self, interaction: discord.Interaction, search: str = "", shuffle: bool = False) -> None:
        await interaction.response.defer()
        if search.strip():
            rows = await self.bot.db.fetchall(
                "SELECT path, title FROM library WHERE kind = 'audio' AND (title LIKE ? ESCAPE '\\' OR path LIKE ? ESCAPE '\\') "
                "ORDER BY path LIMIT ?", (like(search.strip()), like(search.strip()), self.max_queue))
        else:
            order = "random()" if shuffle else "path"
            rows = await self.bot.db.fetchall(
                f"SELECT path, title FROM library WHERE kind = 'audio' ORDER BY {order} LIMIT ?", (self.max_queue,))
        if not rows:
            raise UserError("No matching tracks in the library.")
        tracks = [Track("local", r["path"], r["title"], requester=interaction.user.id) for r in rows]
        await interaction.followup.send(await self.enqueue(interaction, tracks, shuffle=shuffle))

    @app_commands.command(name="youtube", description="Play a YouTube link or the first search result.")
    @app_commands.describe(query="A YouTube link, or words to search for")
    @app_commands.guild_only()
    @app_commands.checks.cooldown(3, 30.0, key=lambda i: i.user.id)
    async def youtube_play(self, interaction: discord.Interaction, query: str) -> None:
        if not self.youtube_enabled:
            raise UserError("YouTube is switched off in config.toml.")
        await interaction.response.defer()
        if getattr(interaction.user, "voice", None) is None:
            raise UserError("Join a voice channel first.")
        track = await self.youtube.resolve(query, interaction.user.id)
        await interaction.followup.send(await self.enqueue(interaction, [track]))

    # ---- commands: controls ----
    @app_commands.command(description="Pause the music.")
    @app_commands.guild_only()
    async def pause(self, interaction: discord.Interaction) -> None:
        player = await self.controls(interaction)
        if not player.vc.is_playing():
            raise UserError("Nothing is playing.")
        player.vc.pause()
        player.auto_paused = False
        player.save()
        await interaction.response.send_message("Paused.")

    @app_commands.command(description="Resume paused music.")
    @app_commands.guild_only()
    async def resume(self, interaction: discord.Interaction) -> None:
        player = await self.controls(interaction)
        if not player.vc.is_paused():
            raise UserError("Nothing is paused.")
        player.vc.resume()
        player.auto_paused = False
        player.cancel_idle()
        await interaction.response.send_message("Resumed.")

    @app_commands.command(description="Skip the current track.")
    @app_commands.guild_only()
    async def skip(self, interaction: discord.Interaction) -> None:
        player = await self.controls(interaction)
        if player.current is None:
            raise UserError("Nothing is playing.")
        title = player.current.label
        player.skip()
        await interaction.response.send_message(f"Skipped **{title}**.")

    @app_commands.command(description="Stop the music and clear the queue.")
    @app_commands.guild_only()
    async def stop(self, interaction: discord.Interaction) -> None:
        player = await self.controls(interaction)
        player.stop()
        await interaction.response.send_message("Stopped and cleared the queue.")

    @app_commands.command(description="Disconnect the bot from voice.")
    @app_commands.guild_only()
    async def leave(self, interaction: discord.Interaction) -> None:
        player = await self.controls(interaction)
        await self.destroy(player)
        await interaction.response.send_message("Left the voice channel.")

    @app_commands.command(description="Set the volume (100 = original, uses the least CPU).")
    @app_commands.describe(percent="0 to 200")
    @app_commands.guild_only()
    async def volume(self, interaction: discord.Interaction, percent: app_commands.Range[int, 0, 200]) -> None:
        player = await self.controls(interaction)
        await interaction.response.defer()
        player.volume = percent
        await self.bot.settings.set(interaction.guild_id, "music_volume", percent)
        if player.current is not None and player.is_active():
            await player.restart_at(player.position)
        await interaction.followup.send(f"Volume set to {percent}%.")

    @app_commands.command(description="Jump to a position in the current track.")
    @app_commands.describe(position="For example 90, 1:30 or 1:02:03")
    @app_commands.guild_only()
    async def seek(self, interaction: discord.Interaction, position: str) -> None:
        player = await self.controls(interaction)
        if player.current is None:
            raise UserError("Nothing is playing.")
        target = parse_time(position)
        duration = player.current.duration
        if duration and target >= duration:
            raise UserError(f"The track is only {fmt_time(duration)} long.")
        await interaction.response.defer()
        await player.restart_at(target)
        await interaction.followup.send(f"Jumped to {fmt_time(target)}.")

    @app_commands.command(description="Repeat the current track, the whole queue, or nothing.")
    @app_commands.choices(mode=[app_commands.Choice(name="Off", value="off"),
                                app_commands.Choice(name="Current track", value="track"),
                                app_commands.Choice(name="Whole queue", value="queue")])
    @app_commands.guild_only()
    async def loop(self, interaction: discord.Interaction, mode: app_commands.Choice[str]) -> None:
        player = await self.controls(interaction)
        player.loop_mode = mode.value
        player.save()
        await interaction.response.send_message(f"Loop: {mode.name}.")

    @app_commands.command(description="Shuffle the upcoming tracks.")
    @app_commands.guild_only()
    async def shuffle(self, interaction: discord.Interaction) -> None:
        player = await self.controls(interaction)
        if len(player.queue) < 2:
            raise UserError("There are not enough tracks in the queue to shuffle.")
        items = list(player.queue)
        random.shuffle(items)
        player.queue = deque(items)
        player.save()
        self.prebuffer(player)
        await interaction.response.send_message(f"Shuffled {len(items)} tracks.")

    @app_commands.command(description="Remove one track from the queue by its number.")
    @app_commands.describe(number="Position shown by /queue")
    @app_commands.guild_only()
    async def remove(self, interaction: discord.Interaction, number: app_commands.Range[int, 1, 10000]) -> None:
        player = await self.controls(interaction)
        if number > len(player.queue):
            raise UserError(f"The queue only has {len(player.queue)} track(s).")
        track = player.queue[number - 1]
        del player.queue[number - 1]
        player.save()
        await interaction.response.send_message(f"Removed **{track.label}**.")

    @app_commands.command(description="Remove all upcoming tracks but keep the current one playing.")
    @app_commands.guild_only()
    async def clear(self, interaction: discord.Interaction) -> None:
        player = await self.controls(interaction)
        count = len(player.queue)
        player.queue.clear()
        player.save()
        await interaction.response.send_message(f"Cleared {count} track(s).")

    # ---- commands: info ----
    @app_commands.command(description="Show what is playing right now.")
    @app_commands.guild_only()
    async def nowplaying(self, interaction: discord.Interaction) -> None:
        player = self.players.get(interaction.guild_id)
        if player is None or player.current is None:
            raise UserError("Nothing is playing.")
        track, pos = player.current, player.position
        if track.duration:
            filled = min(20, int(20 * pos / track.duration))
            bar = "▬" * filled + "🔘" + "▬" * (20 - filled)
        else:
            bar = ""
        state = "Paused" if player.vc and player.vc.is_paused() else "Playing"
        lines = [f"**{track.label}**", f"{bar} `{fmt_time(pos)} / {fmt_time(track.duration)}`",
                 f"{state} · volume {player.volume}% · loop {player.loop_mode} · {len(player.queue)} in queue"]
        await interaction.response.send_message("\n".join(lines), allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(description="Show the upcoming tracks.")
    @app_commands.describe(page="Page number (10 tracks per page)")
    @app_commands.guild_only()
    async def queue(self, interaction: discord.Interaction, page: app_commands.Range[int, 1, 1000] = 1) -> None:
        player = self.players.get(interaction.guild_id)
        if player is None or (player.current is None and not player.queue):
            raise UserError("The queue is empty.")
        items = list(player.queue)
        pages = max(1, (len(items) + 9) // 10)
        page = min(page, pages)
        lines = []
        if player.current is not None:
            lines.append(f"**Now:** {player.current.label} `{fmt_time(player.position)} / {fmt_time(player.current.duration)}`")
        for number, track in enumerate(items[(page - 1) * 10: page * 10], start=(page - 1) * 10 + 1):
            lines.append(f"`{number}.` {track.label} `{fmt_time(track.duration)}`" if track.duration else f"`{number}.` {track.label}")
        lines.append(f"Page {page}/{pages} · {len(items)} queued · loop {player.loop_mode}")
        await interaction.response.send_message("\n".join(lines)[:2000], allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(description="Staff: re-scan the music folders for new or removed files.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    @staff_only()
    async def rescan_library(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        folders = self.library_dirs()
        if not folders:
            raise UserError("No valid folders are set in `music.library_dirs` in config.toml.")
        added, removed, total = await self.rescan("audio", folders, AUDIO_EXTENSIONS, "music.extensions")
        await interaction.followup.send(f"Library: {total} track(s). {added} new, {removed} removed.", ephemeral=True)

    # ---- commands: playlists ----
    playlist = app_commands.Group(name="playlist", description="Saved playlists", guild_only=True)

    async def _playlist_names(self, interaction: discord.Interaction, current: str):
        try:
            rows = await self.bot.db.fetchall(
                "SELECT name FROM playlists WHERE guild_id = ? AND name LIKE ? ESCAPE '\\' ORDER BY name LIMIT 25",
                (interaction.guild_id, like(current.strip())))
        except Exception:  # noqa: BLE001
            return []
        return [app_commands.Choice(name=row["name"][:100], value=row["name"][:100]) for row in rows]

    @playlist.command(name="save", description="Save the current track and queue as a playlist.")
    @app_commands.describe(name="Playlist name (an existing playlist of yours is replaced)")
    async def playlist_save(self, interaction: discord.Interaction, name: app_commands.Range[str, 1, 50]) -> None:
        player = self.players.get(interaction.guild_id)
        tracks = (([player.current] if player.current else []) + list(player.queue)) if player else []
        if not tracks:
            raise UserError("There is nothing playing or queued to save.")
        name = name.strip()
        db = self.bot.db
        row = await db.fetchone("SELECT id, owner_id FROM playlists WHERE guild_id = ? AND name = ?",
                                (interaction.guild_id, name))
        if row is not None:
            if row["owner_id"] != interaction.user.id and not await is_staff(interaction):
                raise UserError("A playlist with that name belongs to someone else.")
            playlist_id = row["id"]
            await db.execute("DELETE FROM playlist_tracks WHERE playlist_id = ?", (playlist_id,))
        else:
            playlist_id = await db.execute(
                "INSERT INTO playlists (guild_id, owner_id, name) VALUES (?, ?, ?)",
                (interaction.guild_id, interaction.user.id, name))
        await db.executemany(
            "INSERT INTO playlist_tracks (playlist_id, pos, data) VALUES (?, ?, ?)",
            [(playlist_id, pos, json.dumps(t.to_json(), separators=(",", ":"))) for pos, t in enumerate(tracks)])
        await interaction.response.send_message(f"Saved playlist **{discord.utils.escape_markdown(name)}** ({len(tracks)} tracks).")

    @playlist.command(name="load", description="Add a saved playlist to the queue.")
    @app_commands.autocomplete(name=_playlist_names)
    async def playlist_load(self, interaction: discord.Interaction, name: str, shuffle: bool = False) -> None:
        await interaction.response.defer()
        rows = await self.bot.db.fetchall(
            "SELECT t.data FROM playlist_tracks t JOIN playlists p ON p.id = t.playlist_id "
            "WHERE p.guild_id = ? AND p.name = ? ORDER BY t.pos", (interaction.guild_id, name.strip()))
        if not rows:
            raise UserError("No playlist with that name.")
        tracks = []
        for row in rows:
            try:
                track = Track.from_json(json.loads(row["data"]))
            except (ValueError, KeyError, TypeError):
                continue
            if track.kind == "yt" and not self.youtube_enabled:
                continue
            track.requester = interaction.user.id
            tracks.append(track)
        if not tracks:
            raise UserError("That playlist has no playable tracks (YouTube is switched off).")
        await interaction.followup.send(await self.enqueue(interaction, tracks, shuffle=shuffle))

    @playlist.command(name="list", description="List this server's saved playlists.")
    async def playlist_list(self, interaction: discord.Interaction) -> None:
        rows = await self.bot.db.fetchall(
            "SELECT p.name, p.owner_id, COUNT(t.pos) AS n FROM playlists p "
            "LEFT JOIN playlist_tracks t ON t.playlist_id = p.id WHERE p.guild_id = ? "
            "GROUP BY p.id ORDER BY p.name LIMIT 50", (interaction.guild_id,))
        if not rows:
            raise UserError("No playlists saved yet. Use `/playlist save` while music is queued.")
        lines = [f"**{discord.utils.escape_markdown(r['name'])}** · {r['n']} tracks · <@{r['owner_id']}>" for r in rows]
        await interaction.response.send_message("\n".join(lines)[:2000], allowed_mentions=discord.AllowedMentions.none())

    @playlist.command(name="delete", description="Delete one of your playlists.")
    @app_commands.autocomplete(name=_playlist_names)
    async def playlist_delete(self, interaction: discord.Interaction, name: str) -> None:
        row = await self.bot.db.fetchone("SELECT id, owner_id FROM playlists WHERE guild_id = ? AND name = ?",
                                         (interaction.guild_id, name.strip()))
        if row is None:
            raise UserError("No playlist with that name.")
        if row["owner_id"] != interaction.user.id and not await is_staff(interaction):
            raise UserError("Only the playlist's owner or staff can delete it.")
        await self.bot.db.execute("DELETE FROM playlists WHERE id = ?", (row["id"],))
        await interaction.response.send_message(f"Deleted playlist **{discord.utils.escape_markdown(name.strip())}**.")


async def setup(bot: commands.Bot) -> None:
    cog = Music(bot)
    await bot.add_cog(cog)
    if not cog.youtube_enabled:
        bot.tree.remove_command("youtube")
