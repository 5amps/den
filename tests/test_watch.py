"""Offline tests for watch parties: the real web server, range requests, sync between two
viewers, and converting a file browsers cannot play. No Discord connection needed.

    Windows:  .venv\\Scripts\\python.exe tests\\test_watch.py
    Linux:    .venv/bin/python tests/test_watch.py
"""
from __future__ import annotations

import asyncio
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp  # noqa: E402

import bot as botmod  # noqa: E402
from cogs import watch  # noqa: E402
from core.checks import UserError  # noqa: E402
from core.config import Config  # noqa: E402

passed = 0


def ok(name: str, condition: bool) -> None:
    global passed
    if not condition:
        raise AssertionError(name)
    passed += 1
    print(f"  ok  {name}")


def raises(fn, *args) -> bool:
    try:
        fn(*args)
    except UserError:
        return True
    return False


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_video(path: Path, container_args: list[str]) -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=4",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
                    *container_args, str(path)], check=True)


async def next_state(ws, want_op: str = "state", timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        msg = await ws.receive(timeout=max(0.1, deadline - time.monotonic()))
        data = json.loads(msg.data)
        if data.get("op") == want_op:
            return data


async def main() -> None:
    print("format decisions")
    ok("mp4 h264 + aac plays directly", watch.plan_for(".mp4", "h264", "aac", "yuv420p") == "direct")
    ok("webm vp9 + opus plays directly", watch.plan_for(".webm", "vp9", "opus", "yuv420p") == "direct")
    ok("mkv h264 + aac is only repackaged", watch.plan_for(".mkv", "h264", "aac", "yuv420p") == "remux")
    ok("mkv h264 + ac3 re-encodes only the sound", watch.plan_for(".mkv", "h264", "ac3", "yuv420p") == "audio")
    ok("avi mpeg4 is converted", watch.plan_for(".avi", "mpeg4", "mp3", "yuv420p") == "transcode")
    ok("10-bit h264 is converted", watch.plan_for(".mp4", "h264", "aac", "yuv420p10le") == "transcode")
    ok("audio-only file refused", raises(watch.plan_for, ".mp4", None, "aac", None))
    ok("position moves while playing", abs(watch.expected_position({"paused": False, "pos": 10, "at": 100}, 105) - 15) < 1e-9)
    ok("position holds while paused", watch.expected_position({"paused": True, "pos": 10, "at": 100}, 105) == 10)

    if not shutil.which("ffmpeg"):
        print("\nffmpeg not found: skipping the server tests.")
        return

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
        tmp = Path(folder)
        mp4, mkv = tmp / "clip.mp4", tmp / "clip.mkv"
        make_video(mp4, ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest"])
        make_video(mkv, ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest"])
        port = free_port()
        cfg = Config({"bot": {"data_dir": str(tmp / "data")},
                      "modules": {"music": True, "video": True, "watch": True},
                      "watch": {"port": port, "bind": "127.0.0.1"}}, tmp / "config.toml")
        bot = botmod.DenBot(cfg)
        await bot.prepare()
        cog = bot.get_cog("Watch")
        ok("watch module loads", cog is not None)

        print("web server")
        base = f"http://127.0.0.1:{port}"
        async with aiohttp.ClientSession() as http:
            ok("server is off while no party runs", cog.runner is None)
            await cog.start_server()
            session = watch.Session("T" * 22, "Test <clip>", str(mp4), 1, 1, 1)
            cog.sessions[session.token] = session
            async with http.get(f"{base}/w/{session.token}/media") as r:
                ok("video refused while preparing", r.status == 503)
            await cog.prepare(session)
            ok("mp4 is served as is", session.serve == mp4 and not session.temp and session.duration and session.duration > 3)
            async with http.get(f"{base}/w/{session.token}") as r:
                page = await r.text()
                ok("player page served", r.status == 200 and session.token in page)
                ok("title is escaped in the page", "Test &lt;clip&gt;" in page and "<clip>" not in page)
                ok("page has a strict content policy", "default-src 'none'" in r.headers.get("Content-Security-Policy", ""))
            async with http.get(f"{base}/w/wrong-token") as r:
                ok("wrong link gets 404", r.status == 404)
            async with http.get(f"{base}/") as r:
                ok("no index page", r.status == 404)
            async with http.get(f"{base}/w/{session.token}/media", headers={"Range": "bytes=100-199"}) as r:
                body = await r.read()
                ok("seeking works (range request answered with 206)", r.status == 206 and len(body) == 100)
                ok("served as video/mp4", r.headers.get("Content-Type", "").startswith("video/mp4"))

            print("sync between viewers")
            a = await http.ws_connect(f"{base}/w/{session.token}/ws")
            first = await next_state(a, "ready")
            ok("new viewer is told the video is ready", first["op"] == "ready")
            b = await http.ws_connect(f"{base}/w/{session.token}/ws")
            await next_state(b, "ready")
            state = await next_state(a)
            while state["viewers"] < 2:
                state = await next_state(a)
            ok("viewer count reaches 2", state["viewers"] == 2)
            await a.send_str(json.dumps({"op": "play", "pos": 1.5}))
            got = await next_state(b)
            while got.get("action") != "play":
                got = await next_state(b)
            ok("play from one viewer reaches the other", got["paused"] is False and abs(got["pos"] - 1.5) < 1e-6)
            await b.send_str(json.dumps({"op": "seek", "pos": 3.0}))
            got = await next_state(a)
            while got.get("action") != "seek":
                got = await next_state(a)
            ok("seek keeps playing and moves everyone", got["paused"] is False and got["pos"] == 3.0)
            await a.send_str(json.dumps({"op": "pause", "pos": 3.2}))
            got = await next_state(b)
            while got.get("action") != "pause":
                got = await next_state(b)
            ok("pause reaches the other viewer", got["paused"] is True and got["pos"] == 3.2)
            await a.send_str(json.dumps({"op": "seek", "pos": -5}))
            await a.send_str("not json")
            await a.send_str(json.dumps({"op": "rm -rf", "pos": 1}))
            await a.send_str(json.dumps({"op": "ping"}))
            got = await next_state(a)
            while got.get("action") is not None:
                got = await next_state(a)
            ok("bad messages are ignored", session.state["pos"] == 3.2 and session.state["paused"] is True)
            await a.send_str(json.dumps({"op": "seek", "pos": 99999}))
            await asyncio.sleep(0.3)
            ok("seek past the end is clamped", session.state["pos"] <= session.duration)

            print("leaving")
            c = await http.ws_connect(f"{base}/w/{session.token}/ws")
            await next_state(c, "ready")
            await asyncio.sleep(0.3)
            ok("third viewer counted", session.viewers() == 3)
            await c.close()
            await asyncio.sleep(0.5)
            ok("closing the tab lowers the count", session.viewers() == 2)
            d = await http.ws_connect(f"{base}/w/{session.token}/ws")
            await next_state(d, "ready")
            await asyncio.sleep(0.3)
            await d.send_str(json.dumps({"op": "bye"}))
            await asyncio.sleep(0.5)
            ok("a page saying bye leaves at once", session.viewers() == 2)
            e = await http.ws_connect(f"{base}/w/{session.token}/ws")
            await next_state(e, "ready")
            await asyncio.sleep(0.3)
            ghost = next(ws for ws, t in session.seen.items() if t == max(session.seen.values()))
            session.seen[ghost] = time.time() - watch.ACTIVE_SECONDS - 1
            ok("a silent background tab stops counting", session.viewers() == 2)
            session.seen[ghost] = time.time() - watch.ACTIVE_SECONDS * 2 - 1
            await cog.housekeeping()
            await asyncio.sleep(0.3)
            ok("a long-silent connection is hung up", ghost not in session.sockets and len(session.sockets) == 2)
            got = await next_state(a)
            ok("others are told the new count", got["viewers"] == 2)
            await e.close()

            print("converting for browsers")
            other = watch.Session("M" * 22, "mkv", str(mkv), 1, 1, 1)
            cog.sessions[other.token] = other
            await cog.prepare(other)
            ok("mkv is repackaged to mp4", other.ready.is_set() and other.temp and other.serve.suffix == ".mp4"
               and other.serve.is_file())
            converted = other.serve

            print("conversion policy")
            avi = tmp / "old.avi"
            make_video(avi, ["-c:v", "mpeg4", "-c:a", "mp3", "-shortest"])
            cog.convert_policy = "staff"
            try:
                await cog.plan(str(avi), staff=False)
                refused = False
            except UserError:
                refused = True
            ok("members cannot start a slow full conversion", refused)
            ok("staff can", (await cog.plan(str(avi), staff=True))[0] == "transcode")
            ok("cheap repackaging is allowed for everyone", (await cog.plan(str(mkv), staff=False))[0] == "remux")
            cog.convert_policy = "off"
            try:
                await cog.plan(str(avi), staff=True)
                off = False
            except UserError:
                off = True
            ok("conversion can be switched off completely", off)

            print("ending")
            await cog.end_session(other.token, "test")
            ok("converted copy deleted at the end", not converted.exists())
            ok("server keeps running while a party is left", cog.runner is not None)
            await cog.end_session(session.token, "test")
            msg = await a.receive(timeout=5)
            while msg.type == aiohttp.WSMsgType.TEXT and json.loads(msg.data).get("op") != "ended":
                msg = await a.receive(timeout=5)
            ok("viewers are told the party ended", msg.type != aiohttp.WSMsgType.TEXT or json.loads(msg.data)["op"] == "ended")
            ok("server stops when the last party ends", cog.runner is None)
            for ws in (a, b):
                await ws.close()
        await bot.shutdown()
    print(f"\nAll {passed} watch checks passed.")


if __name__ == "__main__":
    asyncio.run(main())
