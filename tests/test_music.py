"""Offline tests for the music module: library, probing, ffmpeg sources, YouTube cache.

    .venv\\Scripts\\python.exe tests\\test_music.py            (add --youtube to test a real download)
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cogs import music  # noqa: E402
from core import proc  # noqa: E402
from core.config import Config  # noqa: E402
from core.db import Database  # noqa: E402
from core.settings import Settings  # noqa: E402

passed = 0


def ok(name: str, condition: bool) -> None:
    global passed
    if not condition:
        raise AssertionError(name)
    passed += 1
    print(f"  ok  {name}")


def make(path: Path, args: list[str]) -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args, str(path)], check=True)


def drain(source: music.CountedSource, limit: int = 100000) -> int:
    while source.frames < limit and source.read():
        pass
    frames = source.frames
    source.cleanup()
    return frames


async def main() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
        tmp = Path(folder)
        lib = tmp / "lib"
        (lib / "Album One").mkdir(parents=True)
        tone = ["-f", "lavfi", "-i", "sine=frequency=440:duration=10"]
        make(lib / "Album One" / "First Song.mp3", [*tone, "-c:a", "libmp3lame", "-q:a", "7"])
        make(lib / "Album One" / "Second 100%_Song.opus", [*tone, "-c:a", "libopus"])
        make(lib / "Odd Frames.opus", [*tone, "-c:a", "libopus", "-frame_duration", "60"])
        make(lib / "Clip.mp4", ["-f", "lavfi", "-i", "testsrc=duration=10:size=160x120:rate=10", *tone,
                                "-c:v", "mpeg4", "-c:a", "aac", "-shortest"])
        (lib / "notes.txt").write_text("not music")
        (lib / "broken.mp3").write_bytes(b"this is not audio")

        cfg = Config({"bot": {"data_dir": str(tmp)},
                      "music": {"library_dirs": [str(lib)], "youtube": True, "cache_dir": str(tmp / "cache"),
                                "cache_max_mb": 1}}, tmp / "config.toml")
        db = Database(tmp / "bot.db")
        await db.open()
        bot = SimpleNamespace(cfg=cfg, db=db, settings=Settings(db))
        cog = music.Music(bot)  # type: ignore[arg-type]
        await db.script(music.SCHEMA)
        scan = lambda: cog.rescan("audio", cog.library_dirs(), music.AUDIO_EXTENSIONS, "music.extensions")  # noqa: E731

        print("library")
        ok("scan finds 5 audio files, ignores .txt", await scan() == (5, 0, 5))
        ok("rescan is a no-op", (await scan())[:2] == (0, 0))
        ok("search by title", (await cog.find("audio", "first", 1)).title == "First Song")
        ok("search by folder name", len(await cog.search("audio", "album one", 10)) == 2)
        ok("LIKE wildcards are escaped", [r["title"] for r in await cog.search("audio", "100%_", 10)] == ["Second 100%_Song"])
        ok("no match returns None", await cog.find("audio", "zzzz", 1) is None)
        choices = await cog.complete("audio", "song")
        ok("autocomplete returns #id values", len(choices) == 2 and choices[0].value.startswith("#"))
        ok("autocomplete value resolves", (await cog.find("audio", choices[0].value, 1)) is not None)
        os.remove(lib / "Album One" / "First Song.mp3")
        ok("removed file leaves the library", (await scan())[1] == 1)
        make(lib / "Album One" / "First Song.mp3", [*tone, "-c:a", "libmp3lame", "-q:a", "7"])
        ok("new file is picked up", (await scan())[0] == 1)

        print("probe")
        mp3 = str(lib / "Album One" / "First Song.mp3")
        opus = str(lib / "Album One" / "Second 100%_Song.opus")
        codec, dur = await music.probe(mp3)
        ok("mp3: needs encoding, duration known", codec == "mp3" and 9.5 < dur < 10.6)
        codec, dur = await music.probe(opus)
        ok("opus 20 ms: passthrough allowed", codec == "opus" and 9.5 < dur < 10.6)
        ok("opus 60 ms: passthrough refused", (await music.probe(str(lib / "Odd Frames.opus")))[0] == "opus-reencode")
        ok("video file: audio stream found", (await music.probe(str(lib / "Clip.mp4")))[0] == "aac")
        ok("broken file: no codec", (await music.probe(str(lib / "broken.mp3")))[0] is None)

        print("ffmpeg sources (packets are 20 ms each, a 10 s file is about 500)")
        common = dict(bitrate=96, complexity=5)
        n = drain(music.build_source(opus, offset=0, volume=100, passthrough=True, **common))
        ok(f"passthrough: {n} packets", 495 <= n <= 505)
        n = drain(music.build_source(opus, offset=4, volume=100, passthrough=True, **common))
        ok(f"passthrough seek to 4 s: {n} packets", 295 <= n <= 305)
        n = drain(music.build_source(mp3, offset=0, volume=100, passthrough=False, **common))
        ok(f"mp3 encode: {n} packets", 495 <= n <= 510)
        n = drain(music.build_source(mp3, offset=7.5, volume=40, passthrough=False, **common))
        ok(f"mp3 encode, seek 7.5 s, volume 40%: {n} packets", 120 <= n <= 135)
        n = drain(music.build_source(str(lib / "Clip.mp4"), offset=0, volume=100, passthrough=False, **common))
        ok(f"video file as audio: {n} packets", 490 <= n <= 510)
        n = drain(music.build_source(str(lib / "Odd Frames.opus"), offset=0, volume=100, passthrough=False, **common))
        ok(f"60 ms opus re-encoded to 20 ms: {n} packets", 495 <= n <= 505)
        n = drain(music.build_source(str(lib / "broken.mp3"), offset=0, volume=100, passthrough=False, **common))
        ok("broken file yields nothing (player will skip it)", n == 0)
        src = music.build_source(mp3, offset=0, volume=100, passthrough=False, **common)
        for _ in range(50):
            src.read()
        ok("position tracking", abs(src.position - 1.0) < 0.001)
        process = src.inner._process
        src.cleanup()
        await asyncio.sleep(0.3)
        ok("cleanup kills ffmpeg mid-track", process.poll() is not None)

        print("helpers")
        ok("parse_time", (music.parse_time("90"), music.parse_time("1:30"), music.parse_time("1:02:03")) == (90, 90, 3723))
        ok("fmt_time", (music.fmt_time(75), music.fmt_time(3723), music.fmt_time(None)) == ("1:15", "1:02:03", "?:??"))
        t = music.Track("local", "x.mp3", "T", 12.5, None, 7)
        ok("track json round trip", music.Track.from_json(t.to_json()).to_json() == t.to_json())

        print("youtube cache (no network)")
        cache = tmp / "cache"
        cache.mkdir()
        for i in range(4):                                   # four 400 KB files, cap is 1 MB
            f = cache / f"v{i}.webm"
            f.write_bytes(b"0" * 400_000)
            await db.execute("INSERT INTO cache_index VALUES (?, ?, ?, ?)", (f"v{i}", str(f), 400_000, 1000 + i))
        cog.cache_keys_in_use = lambda: {"v0"}               # v0 is "playing": must survive
        await cog.youtube.evict()
        left = sorted(p.name for p in cache.iterdir())
        ok(f"LRU eviction keeps in-use + newest: {left}", left == ["v0.webm", "v3.webm"])
        ok("cached file is reused without downloading",
           await cog.youtube.ensure(music.Track("yt", "v3", "x")) == str(cache / "v3.webm"))
        try:
            await cog.youtube.resolve("https://example.com/a.mp3", 1)
            refused = False
        except music.UserError:
            refused = True
        ok("non-YouTube links refused", refused)

        if "--youtube" in sys.argv:
            print("youtube (network)")
            cog.youtube.cache_max = 500 * 1_048_576
            started = time.perf_counter()
            track = await cog.youtube.resolve("https://www.youtube.com/watch?v=jNQXAC9IVRw", 1)
            ok(f"resolve: '{track.title}' {track.duration}s in {time.perf_counter() - started:.1f}s", track.ref == "jNQXAC9IVRw")
            started = time.perf_counter()
            first, second = await asyncio.gather(cog.youtube.ensure(track), cog.youtube.ensure(track))
            ok(f"one download for two waiters in {time.perf_counter() - started:.1f}s", first == second and os.path.isfile(first))
            codec, _ = await music.probe(first)
            n = drain(music.build_source(first, offset=0, volume=100, passthrough=codec == "opus", **common))
            ok(f"downloaded audio is {codec}, {n} packets via passthrough", codec == "opus" and 900 <= n <= 960)
            try:
                await cog.youtube.resolve("https://www.youtube.com/watch?v=00000000000", 1)
                bad = False
            except music.UserError as exc:
                bad = True
                print(f"      bad link message: {exc}")
            ok("bad link gives a readable error", bad)

        await proc.stop_all()
        await db.close()
    print(f"\nAll {passed} music checks passed.")


if __name__ == "__main__":
    asyncio.run(main())
