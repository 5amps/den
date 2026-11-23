"""Watch parties: watch a local video together, in sync, without anyone sharing their screen.

    /watch start <file>   the bot serves a small web page from this PC and posts the link
    /watch end            closes the party (the starter or staff)

Everyone opens the link in a browser. Play, pause and seek are shared: when one person
pauses, everyone pauses. The page and the video come straight from the PC running the bot.

Why a web page: Discord does not let bots send video into voice channels. The only way around
that is automating a normal user account, which breaks Discord's rules and gets accounts
banned. A browser plays the file at full quality instead, and costs the bot almost nothing.

Who can open the link:
  * People on the same network (home Wi-Fi) can always use the LAN link.
  * For friends elsewhere either set watch.public_url (your own port forward or tunnel), or set
    watch.tunnel = "cloudflared" and install cloudflared: the bot then opens a free temporary
    Cloudflare tunnel while a party runs and closes it when the last party ends.

Safety: every party has a random 128-bit link. The server only answers those links, only
serves that one video, lists no folders, and stops listening when no party is running.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import socket
import time
from dataclasses import dataclass, field
from html import escape
from pathlib import Path

import discord
from aiohttp import WSMsgType, web
from discord import app_commands
from discord.ext import commands, tasks

from core import proc
from core.checks import UserError, is_staff

log = logging.getLogger("watch")

PAGE = Path(__file__).resolve().parent.parent / "web" / "watch.html"
BROWSER_VIDEO = {"h264", "vp8", "vp9", "av1"}
BROWSER_AUDIO = {"aac", "mp3", "opus", "vorbis", "flac"}
DIRECT_CONTAINERS = {".mp4", ".m4v", ".webm"}
ACTIVE_SECONDS = 30       # a page that has not checked in for this long no longer counts as watching
TUNNEL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
}


# ---------------------------------------------------------------------------- helpers (no Discord)
def plan_for(ext: str, video: str | None, audio: str | None, pix_fmt: str | None) -> str:
    """How to make a file playable in browsers: 'direct', 'remux', 'audio' (re-encode audio only)
    or 'transcode' (re-encode everything, slow)."""
    ext = ext.lower()
    pix_ok = pix_fmt in (None, "yuv420p", "yuvj420p")
    audio_ok = audio is None or audio in BROWSER_AUDIO
    if video is None:
        raise UserError("That file has no video stream. Use `/video play` or `/play` for sound only.")
    if ext in DIRECT_CONTAINERS and video in BROWSER_VIDEO and audio_ok and (video != "h264" or pix_ok):
        return "direct"
    if video == "h264" and pix_ok:
        return "remux" if audio in (None, "aac", "mp3") else "audio"
    return "transcode"


def lan_ip() -> str:
    """This PC's address on the local network (no packet is actually sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def expected_position(state: dict, now: float) -> float:
    return state["pos"] if state["paused"] else state["pos"] + max(0.0, now - state["at"])


@dataclass
class Session:
    token: str
    title: str
    source: str                       # the original file
    host_id: int
    guild_id: int
    channel_id: int
    message_id: int = 0
    serve: Path | None = None         # the file browsers get (original or converted copy)
    temp: bool = False                # serve is a converted copy to delete afterwards
    duration: float | None = None
    progress: float = 0.0             # conversion progress 0..1
    error: str = ""
    created: float = field(default_factory=time.time)
    last_viewer: float = field(default_factory=time.time)
    state: dict = field(default_factory=lambda: {"paused": True, "pos": 0.0, "at": time.time()})
    sockets: set = field(default_factory=set)
    seen: dict = field(default_factory=dict)   # socket -> last time its page checked in
    last_count: int = 0
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None

    def viewers(self, now: float | None = None) -> int:
        """Pages that checked in recently. A tab closed on a phone can keep its connection open
        in the background for minutes, so an open connection alone does not count."""
        now = time.time() if now is None else now
        return sum(1 for ws in self.sockets if now - self.seen.get(ws, 0) < ACTIVE_SECONDS)

    def payload(self, **extra) -> dict:
        return {"op": "state", **self.state, "now": time.time(), "viewers": self.viewers(), **extra}


# ---------------------------------------------------------------------------- cog
class Watch(commands.Cog):
    watch = app_commands.Group(name="watch", description="Watch a video together in the browser", guild_only=True)

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        get = bot.cfg.get
        self.port = int(get("watch.port", 8765))
        self.bind = str(get("watch.bind", "0.0.0.0"))
        self.public_url = str(get("watch.public_url", "") or "").rstrip("/")
        self.tunnel_mode = str(get("watch.tunnel", "off") or "off").lower()
        self.max_parties = int(get("watch.max_parties", 3))
        self.max_viewers = int(get("watch.max_viewers", 30))
        self.idle_minutes = float(get("watch.idle_minutes", 30))
        self.staff_only = bool(get("watch.staff_only", False))
        self.cache = bot.cfg.data_dir / "cache" / "watch"
        self.sessions: dict[str, Session] = {}
        self.runner: web.AppRunner | None = None
        self.tunnel: asyncio.subprocess.Process | None = None
        self.tunnel_url = ""
        self._lock = asyncio.Lock()
        self._page = ""

    async def cog_load(self) -> None:
        self._page = PAGE.read_text(encoding="utf-8")
        self.cache.mkdir(parents=True, exist_ok=True)
        for old in self.cache.glob("*.mp4"):              # leftovers from a crash
            try:
                old.unlink()
            except OSError:
                pass
        self.housekeeping.start()

    async def cog_unload(self) -> None:
        self.housekeeping.cancel()
        for token in list(self.sessions):
            await self.end_session(token, "the bot is shutting down")
        await self.stop_server()

    @property
    def music(self):
        cog = self.bot.get_cog("Music")
        if cog is None:
            raise UserError("Watch parties need the music module for the video list. Set `music = true`.")
        return cog

    # ---- web server ----
    def make_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/w/{token}", self.page)
        app.router.add_get("/w/{token}/media", self.media)
        app.router.add_get("/w/{token}/ws", self.socket)
        app.router.add_get("/", self.nothing)
        return app

    async def start_server(self) -> None:
        if self.runner is not None:
            return
        runner = web.AppRunner(self.make_app(), access_log=None)
        await runner.setup()
        try:
            await web.TCPSite(runner, self.bind, self.port).start()
        except OSError as exc:
            await runner.cleanup()
            raise UserError(f"Port {self.port} is in use or blocked ({exc.strerror}). "
                            "Pick another `watch.port` in config.toml.") from exc
        self.runner = runner
        log.info("Watch server listening on %s:%d", self.bind, self.port)
        if self.tunnel_mode == "cloudflared" and not self.public_url:
            await self.start_tunnel()

    async def stop_server(self) -> None:
        await self.stop_tunnel()
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None
            log.info("Watch server stopped")

    async def start_tunnel(self) -> None:
        if not proc.which("cloudflared"):
            log.warning("watch.tunnel is cloudflared but cloudflared is not installed; only the LAN link works")
            return
        self.tunnel = await proc.start(
            ["cloudflared", "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{self.port}"],
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)

        async def find_url() -> str:
            while True:
                line = await self.tunnel.stderr.readline()
                if not line:
                    return ""
                match = TUNNEL_RE.search(line.decode("utf-8", "replace"))
                if match:
                    return match.group(0)
        try:
            self.tunnel_url = await asyncio.wait_for(find_url(), timeout=40)
        except asyncio.TimeoutError:
            self.tunnel_url = ""
        if not self.tunnel_url:
            log.warning("cloudflared did not give a tunnel address; only the LAN link works")
            await self.stop_tunnel()
            return
        log.info("Watch tunnel open: %s", self.tunnel_url)

        async def drain() -> None:                          # keep reading so cloudflared never blocks
            try:
                while self.tunnel is not None and await self.tunnel.stderr.readline():
                    pass
            except Exception:  # noqa: BLE001
                pass
        asyncio.create_task(drain())

    async def stop_tunnel(self) -> None:
        if self.tunnel is not None:
            await proc.stop(self.tunnel)
            self.tunnel = None
            self.tunnel_url = ""

    def links(self, token: str) -> list[tuple[str, str]]:
        """(label, url) pairs for a party: internet link first if there is one."""
        result = []
        internet = self.public_url or self.tunnel_url
        if internet:
            result.append(("Open the player", f"{internet}/w/{token}"))
        result.append(("Same Wi-Fi link" if internet else "Open the player", f"http://{lan_ip()}:{self.port}/w/{token}"))
        return result

    # ---- HTTP handlers ----
    def find(self, request: web.Request) -> Session:
        session = self.sessions.get(request.match_info.get("token", ""))
        if session is None:
            raise web.HTTPNotFound(text="This watch party has ended or the link is wrong.", headers=SECURITY_HEADERS)
        return session

    async def nothing(self, request: web.Request) -> web.Response:
        return web.Response(status=404, text="Nothing here.", headers=SECURITY_HEADERS)

    async def page(self, request: web.Request) -> web.Response:
        session = self.find(request)
        html = self._page.replace("{{TITLE}}", escape(session.title)).replace("{{TOKEN}}", session.token)
        headers = {**SECURITY_HEADERS, "Content-Security-Policy":
                   "default-src 'none'; media-src 'self'; connect-src 'self'; style-src 'unsafe-inline'; "
                   "script-src 'unsafe-inline'; img-src 'self' data:; base-uri 'none'; form-action 'none'"}
        return web.Response(text=html, content_type="text/html", headers=headers)

    async def media(self, request: web.Request) -> web.StreamResponse:
        session = self.find(request)
        if not session.ready.is_set() or session.serve is None:
            raise web.HTTPServiceUnavailable(text="Still preparing the video.", headers=SECURITY_HEADERS)
        response = web.FileResponse(session.serve, headers={**SECURITY_HEADERS, "Cache-Control": "private, max-age=3600"})
        response.content_type = "video/webm" if session.serve.suffix.lower() == ".webm" else "video/mp4"
        return response

    async def socket(self, request: web.Request) -> web.WebSocketResponse:
        session = self.find(request)
        if len(session.sockets) >= self.max_viewers:
            raise web.HTTPServiceUnavailable(text="This watch party is full.", headers=SECURITY_HEADERS)
        ws = web.WebSocketResponse(heartbeat=25, max_msg_size=4096)
        await ws.prepare(request)
        session.sockets.add(ws)
        session.seen[ws] = session.last_viewer = time.time()
        try:
            await ws.send_json(self.ready_message(session))
            await self.broadcast(session, session.payload())
            sent: list[float] = []
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                now = time.time()
                session.seen[ws] = now
                sent = [t for t in sent if now - t < 2] + [now]
                if len(sent) > 20:                          # someone is spamming the buttons
                    continue
                try:
                    data = json.loads(msg.data)
                    op = data.get("op")
                except (ValueError, AttributeError):
                    continue
                if op == "bye":                             # the page is closing
                    break
                if op == "ping":
                    await ws.send_json(session.payload())
                    continue
                if op not in ("play", "pause", "seek"):
                    continue
                pos = data.get("pos")
                if not isinstance(pos, (int, float)) or pos != pos or pos < 0 or pos > 1e7:
                    continue
                if session.duration:
                    pos = min(float(pos), session.duration)
                paused = session.state["paused"] if op == "seek" else op == "pause"
                session.state = {"paused": paused, "pos": float(pos), "at": now}
                await self.broadcast(session, session.payload(action=op))
        finally:
            session.sockets.discard(ws)
            session.seen.pop(ws, None)
            session.last_viewer = time.time()
            if session.token in self.sessions:
                await self.broadcast(session, session.payload())
        return ws

    def ready_message(self, session: Session) -> dict:
        if session.error:
            return {"op": "error", "text": session.error}
        if not session.ready.is_set():
            return {"op": "preparing", "progress": session.progress}
        return {"op": "ready", "duration": session.duration}

    async def broadcast(self, session: Session, message: dict) -> None:
        for ws in list(session.sockets):
            try:
                await ws.send_json(message)
            except Exception:  # noqa: BLE001 - a closed tab must not break the others
                session.sockets.discard(ws)
                session.seen.pop(ws, None)

    # ---- preparing the file ----
    async def probe(self, path: str) -> tuple[str | None, str | None, str | None, float | None]:
        code, out, _ = await proc.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name,pix_fmt",
             "-show_entries", "format=duration", "-of", "json", path], timeout=30)
        if code != 0:
            raise UserError("ffprobe could not read that file.")
        info = json.loads(out or b"{}")
        video = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
        audio = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), {})
        try:
            duration = float(info.get("format", {}).get("duration"))
        except (TypeError, ValueError):
            duration = None
        return video.get("codec_name"), audio.get("codec_name"), video.get("pix_fmt"), duration

    async def prepare(self, session: Session) -> None:
        try:
            video, audio, pix, session.duration = await self.probe(session.source)
            how = plan_for(Path(session.source).suffix, video, audio, pix)
            if how == "direct":
                session.serve = Path(session.source)
            else:
                digest = hashlib.sha1(f"{session.source}{os.path.getmtime(session.source)}".encode()).hexdigest()[:16]
                out = self.cache / f"{digest}-{session.token[:6]}.mp4"
                await self.convert(session, how, out)
                session.serve, session.temp = out, True
            session.progress = 1.0
            session.ready.set()
            await self.broadcast(session, self.ready_message(session))
            log.info("Watch party %s ready (%s): %s", session.token[:6], how, session.title)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            session.error = str(exc) if isinstance(exc, UserError) else "The video could not be prepared."
            log.warning("Watch party %s failed: %s", session.token[:6], exc)
            await self.broadcast(session, self.ready_message(session))

    async def convert(self, session: Session, how: str, out: Path) -> None:
        if how == "remux":
            codec = ["-c", "copy"]
        elif how == "audio":
            codec = ["-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-ac", "2"]
        else:
            codec = ["-vf", "scale=-2:min(1080\\,ih)", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                     "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-ac", "2"]
        cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", session.source,
               "-map", "0:v:0", "-map", "0:a:0?", *codec, "-movflags", "+faststart", "-progress", "pipe:1", str(out)]
        process = await proc.start(cmd, stderr=asyncio.subprocess.DEVNULL)
        try:
            last = 0.0
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                key, _, value = line.decode("ascii", "replace").strip().partition("=")
                if key == "out_time_us" and value.isdigit() and session.duration:
                    session.progress = min(0.99, int(value) / 1e6 / session.duration)
                    if time.monotonic() - last > 2:
                        last = time.monotonic()
                        await self.broadcast(session, self.ready_message(session))
            code = await process.wait()
        finally:
            await proc.stop(process)
        if code != 0 or not out.is_file():
            raise UserError("ffmpeg could not convert that video for browsers.")

    # ---- sessions ----
    async def end_session(self, token: str, reason: str) -> None:
        session = self.sessions.pop(token, None)
        if session is None:
            return
        if session.task is not None and not session.task.done():
            session.task.cancel()
        await self.broadcast(session, {"op": "ended", "text": f"This watch party has ended ({reason})."})
        for ws in list(session.sockets):
            await ws.close()
        if session.temp and session.serve is not None:
            try:
                session.serve.unlink(missing_ok=True)
            except OSError:
                pass
        channel = self.bot.get_channel(session.channel_id)
        if channel is not None and session.message_id:
            try:
                message = channel.get_partial_message(session.message_id)
                await message.edit(embed=discord.Embed(
                    title=f"🎬 {session.title}", colour=discord.Colour.dark_grey(),
                    description=f"Watch party ended ({reason})."), view=None)
            except discord.HTTPException:
                pass
        log.info("Watch party %s ended: %s", token[:6], reason)
        if not self.sessions:
            await self.stop_server()

    @tasks.loop(seconds=10)
    async def housekeeping(self) -> None:
        now = time.time()
        for token, session in list(self.sessions.items()):
            stale = [ws for ws in session.sockets if now - session.seen.get(ws, 0) > ACTIVE_SECONDS * 2]
            for ws in stale:                                # gone for a minute: hang up on it
                session.sockets.discard(ws)
                session.seen.pop(ws, None)
                try:
                    await asyncio.wait_for(ws.close(), timeout=2)
                except Exception:  # noqa: BLE001 - it is already gone
                    pass
            count = session.viewers(now)
            if count != session.last_count or stale:
                await self.broadcast(session, session.payload())
            session.last_count = count
            if count:
                session.last_viewer = now
            elif now - session.last_viewer > self.idle_minutes * 60:
                await self.end_session(token, f"nobody watched for {self.idle_minutes:g} minutes")

    # ---- commands ----
    async def _complete(self, interaction: discord.Interaction, current: str):
        return await self.music.complete("video", current)

    @watch.command(name="start", description="Watch a local video together in the browser, in sync.")
    @app_commands.describe(file="Start typing a video name, then pick from the list")
    @app_commands.autocomplete(file=_complete)
    @app_commands.checks.cooldown(1, 20.0, key=lambda i: i.user.id)
    async def start(self, interaction: discord.Interaction, file: str) -> None:
        if self.staff_only and not await is_staff(interaction):
            raise UserError("Only staff can start watch parties on this server.")
        if len(self.sessions) >= self.max_parties:
            raise UserError(f"{self.max_parties} watch parties are already running. End one with `/watch end`.")
        track = await self.music.find("video", file, interaction.user.id)
        if track is None or not os.path.isfile(track.ref):
            raise UserError(f"No video file matches `{file[:80]}`. The bot owner adds folders with `/library setup`.")
        await interaction.response.defer()
        async with self._lock:
            await self.start_server()
        session = Session(secrets.token_urlsafe(16), track.title, track.ref, interaction.user.id,
                          interaction.guild_id, interaction.channel_id)
        self.sessions[session.token] = session
        session.task = asyncio.create_task(self.prepare(session))
        links = self.links(session.token)
        view = discord.ui.View(timeout=None)
        for label, url in links:
            view.add_item(discord.ui.Button(label=label, url=url, emoji="▶️" if label == "Open the player" else "🏠"))
        how = ("Anyone with the link can join from anywhere." if self.public_url or self.tunnel_url
               else "Works for people on the same Wi-Fi as the bot's PC. For friends elsewhere, see "
                    "`watch.tunnel` in config.toml.")
        embed = discord.Embed(
            title=f"🎬 {track.title}", colour=discord.Colour.red(),
            description=f"{interaction.user.mention} started a watch party.\n"
                        "Open the link, press **Join**. Play, pause and seeking are shared with everyone.\n"
                        f"-# {how} Ends with `/watch end`, or after {self.idle_minutes:g} min with nobody watching.")
        message = await interaction.followup.send(embed=embed, view=view, wait=True,
                                                  allowed_mentions=discord.AllowedMentions.none())
        session.message_id = message.id

    async def _complete_end(self, interaction: discord.Interaction, current: str):
        staff = await is_staff(interaction)
        return [app_commands.Choice(name=s.title[:100], value=t) for t, s in self.sessions.items()
                if s.guild_id == interaction.guild_id and (staff or s.host_id == interaction.user.id)
                and current.lower() in s.title.lower()][:25]

    @watch.command(name="end", description="End a watch party (yours, or any if you are staff).")
    @app_commands.describe(party="Pick the watch party to end")
    @app_commands.autocomplete(party=_complete_end)
    async def end(self, interaction: discord.Interaction, party: str) -> None:
        session = self.sessions.get(party)
        if session is None or session.guild_id != interaction.guild_id:
            raise UserError("Pick a running watch party from the list.")
        if session.host_id != interaction.user.id and not await is_staff(interaction):
            raise UserError("Only the person who started it, or staff, can end it.")
        await interaction.response.send_message(f"Ending **{discord.utils.escape_markdown(session.title)}**.",
                                                ephemeral=True)
        await self.end_session(party, f"ended by {interaction.user.display_name}")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Watch(bot))
