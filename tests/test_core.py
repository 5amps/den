"""Offline tests for the core layer. No Discord connection, no token needed.

    Windows:  .venv\\Scripts\\python.exe tests\\test_core.py
    Linux:    .venv/bin/python tests/test_core.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import proc  # noqa: E402
from core.config import ConfigError, load_config, load_env  # noqa: E402
from core.db import Database  # noqa: E402
from core.scheduler import Scheduler  # noqa: E402
from core.settings import Settings  # noqa: E402
from core.ttl import SlidingCounter, TTLDict  # noqa: E402

passed = 0


def ok(name: str, condition: bool) -> None:
    global passed
    if not condition:
        raise AssertionError(name)
    passed += 1
    print(f"  ok  {name}")


async def test_db(tmp: Path) -> None:
    print("database")
    db = Database(tmp / "t.db")
    await db.open()
    await db.script("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY, v TEXT UNIQUE)")
    ok("insert returns rowid", await db.execute("INSERT INTO t (v) VALUES (?)", ("a",)) == 1)
    # 500 concurrent writes must all land, batched into a handful of transactions.
    started = time.perf_counter()
    await asyncio.gather(*(db.execute("INSERT INTO t (v) VALUES (?)", (f"k{i}",)) for i in range(500)))
    elapsed = time.perf_counter() - started
    row = await db.fetchone("SELECT COUNT(*) AS n FROM t")
    ok(f"500 concurrent writes stored ({elapsed * 1000:.0f} ms)", row["n"] == 501)
    # A failing statement raises for its caller only; neighbours in the batch still commit.
    results = await asyncio.gather(
        db.execute("INSERT INTO t (v) VALUES (?)", ("x",)),
        db.execute("INSERT INTO t (v) VALUES (?)", ("a",)),       # UNIQUE violation
        db.execute("INSERT INTO t (v) VALUES (?)", ("y",)),
        return_exceptions=True,
    )
    ok("bad statement isolated", isinstance(results[1], Exception) and not isinstance(results[0], Exception))
    ok("neighbours committed", (await db.fetchone("SELECT COUNT(*) AS n FROM t"))["n"] == 503)
    ok("update returns rowcount", await db.execute("UPDATE t SET v = v || '!' WHERE id <= 3") == 3)
    db.write_later("INSERT INTO t (v) VALUES (?)", ("later",))
    ok("write_later lands", (await db.fetchone("SELECT 1 AS x FROM t WHERE v = 'later'")) is not None)
    ok("WAL mode", (await db.fetchone("PRAGMA journal_mode"))[0] == "wal")

    settings = Settings(db)
    await settings.set(42, "staff_roles", [1, 2, 3])
    ok("settings round trip", await Settings(db).get(42, "staff_roles") == [1, 2, 3])
    ok("settings default", await settings.get(42, "nope", "d") == "d")
    await settings.delete(42, "staff_roles")
    ok("settings delete", await Settings(db).get(42, "staff_roles") is None)
    await db.close()

    # Data survives a reopen.
    db2 = Database(tmp / "t.db")
    await db2.open()
    ok("persisted after reopen", (await db2.fetchone("SELECT COUNT(*) AS n FROM t"))["n"] == 504)
    await db2.close()


async def test_scheduler(tmp: Path) -> None:
    print("scheduler")
    db = Database(tmp / "s.db")
    await db.open()
    sched = Scheduler(db)
    fired: list[tuple[str, float]] = []
    flaky_calls = 0

    async def on_a(payload: dict) -> None:
        fired.append((payload["n"], time.time()))

    async def on_flaky(payload: dict) -> None:
        nonlocal flaky_calls
        flaky_calls += 1
        raise RuntimeError("boom")

    sched.register("a", on_a)
    sched.register("flaky", on_flaky)
    now = time.time()
    await sched.add(now - 100, "a", {"n": "overdue"})          # was due while "PC was off"
    await sched.add(now + 0.6, "a", {"n": "later"})
    await sched.add(now + 0.3, "a", {"n": "sooner"})
    cancelled = await sched.add(now + 0.2, "a", {"n": "cancelled"})
    await sched.add(now + 0.2, "a", {"n": "by-ref"}, ref="event:1")
    await sched.add(now + 0.1, "unknown-kind", {"n": "no handler"})
    await sched.add(now + 0.1, "flaky", {})
    await sched.cancel(cancelled)
    await sched.cancel_ref("event:1")
    sched.start()
    await asyncio.sleep(1.2)
    ok("order: overdue, sooner, later", [n for n, _ in fired] == ["overdue", "sooner", "later"])
    ok("timing within 150 ms", abs(fired[2][1] - (now + 0.6)) < 0.15)
    ok("job without handler kept for later",
       (await db.fetchone("SELECT COUNT(*) AS n FROM jobs WHERE kind = 'unknown-kind'"))["n"] == 1)
    row = await db.fetchone("SELECT attempts, run_at FROM jobs WHERE kind = 'flaky'")
    ok("failed job re-queued with attempt count", flaky_calls == 1 and row is not None and row["attempts"] == 1)
    await sched.stop()
    await db.close()


def test_ttl() -> None:
    print("ttl counters")
    counter = SlidingCounter(window=5)
    ok("counts inside window", [counter.hit("u", now=t) for t in (0, 1, 2)] == [1, 2, 3])
    ok("old hits expire", counter.hit("u", now=6.5) == 2)
    ok("keys independent", counter.hit("other", now=6.5) == 1)
    counter.sweep(now=100)
    ok("sweep drops idle keys", len(counter) == 0)
    for i in range(3000):                       # auto-sweep keeps memory bounded
        counter.hit(i, now=200 + i)
    ok("auto-sweep bounds memory", len(counter) < 1100)

    cache = TTLDict(ttl=10)
    cache.set("k", "v", now=0)
    ok("ttl get", cache.get("k", now=5) == "v")
    ok("ttl expiry", cache.get("k", now=11) is None and len(cache) == 0)


async def test_proc() -> None:
    print("subprocess control")
    py = sys.executable
    code, out, _ = await proc.run([py, "-c", "print('hi')"], timeout=20)
    ok("run captures output", code == 0 and out.strip() == b"hi")
    started = time.perf_counter()
    try:
        await proc.run([py, "-c", "import time; time.sleep(60)"], timeout=1)
        timed_out = False
    except proc.ProcessError:
        timed_out = True
    ok(f"timeout kills the process ({time.perf_counter() - started:.1f}s)", timed_out and proc.live_count() == 0)
    try:
        await proc.run(["definitely-not-a-real-program-xyz"], timeout=5)
        missing = False
    except proc.ProcessError as exc:
        missing = "not found" in str(exc)
    ok("missing program gives a clear error", missing)
    # A parent that spawns a child: both must die on stop().
    child = await proc.start([py, "-c",
                              "import subprocess, sys, time;"
                              "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']);"
                              "time.sleep(60)"])
    await asyncio.sleep(0.8)
    await proc.stop(child, grace=0.2)
    ok("stop reaps the process", child.returncode is not None and proc.live_count() == 0)
    task = asyncio.create_task(proc.run([py, "-c", "import time; time.sleep(60)"], timeout=30))
    await asyncio.sleep(0.5)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    ok("cancelled task leaves no process", proc.live_count() == 0)


def test_config(tmp: Path) -> None:
    print("config")
    env = tmp / ".env"
    env.write_text('# comment\nPOTATO_A=1\nPOTATO_B="two words"\nexport POTATO_C=3\n', encoding="utf-8")
    load_env(env)
    ok(".env parsing", (os.environ["POTATO_A"], os.environ["POTATO_B"], os.environ["POTATO_C"]) == ("1", "two words", "3"))
    good = tmp / "good.toml"
    good.write_text("[bot]\nmessage_cache = 10\n[modules]\nmusic = true\nrsvp = false\n", encoding="utf-8")
    cfg = load_config(good)
    ok("toml lookup", cfg.get("bot.message_cache") == 10 and cfg.get("bot.nope", 7) == 7)
    ok("enabled modules", cfg.enabled_modules == ["music"])
    for name, body in (("typo.toml", "[modules]\nmusik = true\n"), ("broken.toml", "[modules\n")):
        bad = tmp / name
        bad.write_text(body, encoding="utf-8")
        try:
            load_config(bad)
            raised = False
        except ConfigError:
            raised = True
        ok(f"{name} rejected with a clear error", raised)


async def main() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
        tmp = Path(folder)
        test_config(tmp)
        test_ttl()
        await test_db(tmp)
        await test_scheduler(tmp)
        await test_proc()
    print(f"\nAll {passed} checks passed.")


if __name__ == "__main__":
    asyncio.run(main())
