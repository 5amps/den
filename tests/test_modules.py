"""Offline tests for anti-spam, anti-raid, tickets, RSVP, moderation and command definitions.

    .venv\\Scripts\\python.exe tests\\test_modules.py
"""
from __future__ import annotations

import asyncio
import datetime
import re
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import discord  # noqa: E402

import bot as botmod  # noqa: E402
from cogs import antispam, moderation, rsvp, tickets  # noqa: E402
from core.checks import UserError  # noqa: E402
from core.config import KNOWN_MODULES, Config  # noqa: E402

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


class FakeResponse:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def is_done(self) -> bool:
        return False

    async def send_message(self, content=None, **kwargs) -> None:
        self.sent.append(content)


def interaction(user_id: int, guild_id: int = 1):
    return SimpleNamespace(user=SimpleNamespace(id=user_id), guild_id=guild_id, response=FakeResponse(),
                           guild=None, permissions=discord.Permissions.none())


async def main() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
        tmp = Path(folder)
        cfg = Config({"bot": {"data_dir": str(tmp)}, "modules": {m: True for m in KNOWN_MODULES},
                      "antispam": {"block_links": True, "allowed_domains": ["youtube.com"]},
                      "rsvp": {"refresh_seconds": 0.05}}, tmp / "config.toml")
        bot = botmod.PotatoBot(cfg)
        await bot.prepare()

        print("all modules")
        ok("every module loads", not bot.failed_modules and len(bot.loaded_modules) == len(KNOWN_MODULES))
        commands_seen = list(bot.tree.walk_commands())
        bad = [c.qualified_name for c in commands_seen
               if not re.fullmatch(r"[-_a-z0-9]{1,32}", c.name) or not 1 <= len(c.description) <= 100]
        ok(f"{len(commands_seen)} command definitions are valid for Discord", not bad)
        top = bot.tree.get_commands()
        ok(f"{len(top)} top-level commands (Discord allows 100)", len(top) <= 100)
        for command in top:
            command.to_dict(bot.tree)                       # raises if a definition cannot be uploaded
        ok("all commands serialise for upload", True)
        ok("/youtube is hidden while YouTube is off", bot.tree.get_command("youtube") is None)

        print("help")
        from core import helpdata
        system = bot.get_cog("System")
        sections = helpdata.collect(bot)
        listed = {i.name for *_, infos in sections for i in infos}
        real = {c.qualified_name for c in commands_seen if not hasattr(c, "commands")}
        ok(f"/help lists every registered command ({len(listed)})", listed == real)
        ok("every module has a heading and a 'start here' line", all(h and s and st for _, h, s, st, _ in sections))

        class Capture(FakeResponse):
            async def send_message(self, content=None, **kwargs) -> None:
                self.sent.append(kwargs.get("embed"))

        async def show(module=None):
            fake = interaction(1)
            fake.response = Capture()
            await system.help.callback(system, fake, module)
            return fake.response.sent[-1]

        def fits(embed) -> bool:                      # Discord's hard limits for one embed
            return (len(embed) <= 6000 and len(embed.fields) <= 25 and len(embed.description or "") <= 4096
                    and all(1 <= len(f.value) <= 1024 for f in embed.fields))

        overview = await show()
        ok(f"/help overview fits in one message ({len(overview)} of 6000 characters)", fits(overview))
        pages = [await show(name) for name, *_ in sections]
        ok(f"/help for each of the {len(pages)} modules fits", all(fits(e) for e in pages))
        usage = {i.name: i for *_, infos in sections for i in infos}
        ok("usage strings mark required and optional options",
           usage["timeout"].usage == "/timeout <member> <duration> [reason]" and usage["playlist save"].usage == "/playlist save <name>")
        ok("staff commands are labelled", usage["ban"].access == "Needs Ban Members" and usage["play"].access == "Everyone"
           and usage["config log-channel"].access == "Needs Manage Server")
        try:
            await show("nonsense")
            unknown = False
        except UserError:
            unknown = True
        ok("unknown module gives a clear error", unknown)

        print("anti-spam rules")
        spam: antispam.AntiSpam = bot.get_cog("AntiSpam")
        check = lambda text, user=1, mentions=0, everyone=False, now=0.0: spam.check(1, user, text, mentions, everyone, now)  # noqa: E731
        ok("normal message passes", check("hello there, how is everyone doing today") is None)
        ok("7th message in 8 s is rate spam", [check(f"m{i}", user=2, now=i * 0.5) for i in range(7)][-1] == "rate")
        ok("slow chatter passes", all(check(f"m{i}", user=3, now=i * 3.0) is None for i in range(12)))
        ok("4 identical messages are duplicates", [check("buy now", user=4, now=i * 5.0) for i in range(4)][-1] == "duplicate")
        ok("mass mentions", check("hi", user=5, mentions=6) == "mentions")
        ok("@everyone with a mention", check("hi", user=6, mentions=1, everyone=True) == "mentions")
        ok("invite link", check("join discord.gg/abcdef", user=7) == "invite")
        ok("blocked link", check("see https://evil.example/x", user=8) == "link")
        ok("allowed domain and subdomain pass", check("https://www.youtube.com/watch?v=1 https://music.youtube.com/a", user=9) is None)
        ok("look-alike domain is blocked", check("https://notyoutube.com/x", user=10) == "link")
        ok("emoji flood", check("😀" * 16, user=11) == "emoji")
        ok("caps", check("THIS IS A VERY LOUD MESSAGE INDEED", user=12) == "caps")
        ok("short caps pass", check("OK THEN", user=13) is None)
        ok("users are counted separately", check("x", user=14) is None)

        print("moderation")
        ok("durations", (moderation.parse_duration("10m"), moderation.parse_duration("2h"), moderation.parse_duration("1d 12h"))
           == (datetime.timedelta(minutes=10), datetime.timedelta(hours=2), datetime.timedelta(days=1, hours=12)))
        ok("bad durations refused", all(raises(moderation.parse_duration, t) for t in ("", "soon", "0m", "40d", "-5m")))

        print("events")
        now = time.time()
        ok("relative time", abs(rsvp.parse_when("in 2h 30m") - (now + 9000)) < 5 and abs(rsvp.parse_when("1d") - (now + 86400)) < 5)
        target = datetime.datetime.now() + datetime.timedelta(days=3)
        ok("absolute time", abs(rsvp.parse_when(target.strftime("%Y-%m-%d %H:%M")) - target.replace(second=0, microsecond=0).timestamp()) < 1)
        ok("clock time is always in the future", now < rsvp.parse_when("00:00") <= now + 86400)
        ok("nonsense time refused", raises(rsvp.parse_when, "next blue moon"))

        events: rsvp.Rsvp = bot.get_cog("Rsvp")
        event_id = await bot.db.execute(
            "INSERT INTO events (guild_id, channel_id, creator_id, title, starts_at, capacity) VALUES (1, 1, 1, 'Game night', ?, 2)",
            (now + 3600,))

        async def press(user: int, status: str) -> str:
            fake = interaction(user)
            await events.respond(fake, event_id, status)
            return fake.response.sent[-1]

        async def statuses() -> dict[int, str]:
            rows = await bot.db.fetchall("SELECT user_id, status FROM rsvps WHERE event_id = ?", (event_id,))
            return {r["user_id"]: r["status"] for r in rows}

        await press(101, "going")
        await press(102, "going")
        ok("third person is waitlisted when capacity is 2", "waitlist" in await press(103, "going"))
        await press(104, "maybe")
        ok("answers stored", await statuses() == {101: "going", 102: "going", 103: "waitlist", 104: "maybe"})
        await press(101, "no")
        ok("waitlist moves up when a spot frees", (await statuses())[103] == "going" and (await statuses())[101] == "no")
        await press(104, "maybe")
        ok("pressing the same button again removes the answer", 104 not in await statuses())
        await press(104, "going")
        ok("capacity is never exceeded", list((await statuses()).values()).count("going") == 2)
        embed = await events.render(await bot.db.fetchone("SELECT * FROM events WHERE id = ?", (event_id,)))
        ok("embed shows counts", any("Going (2/2)" in f.name for f in embed.fields))
        await bot.db.execute("UPDATE events SET closed = 1 WHERE id = ?", (event_id,))
        try:
            await press(105, "going")
            closed = False
        except UserError:
            closed = True
        ok("closed events refuse answers", closed)
        await asyncio.sleep(0.2)                           # let the debounced refresh run (no guild: no-op)

        print("tickets")
        cog: tickets.Tickets = bot.get_cog("Tickets")
        class Author:
            id = 5

            def __str__(self) -> str:
                return "Sam <script>"

        class Msg:
            def __init__(self, text: str) -> None:
                self.author = Author()
                self.created_at = datetime.datetime.now(datetime.timezone.utc)
                self.clean_content = text
                self.attachments = [SimpleNamespace(url="https://cdn.example/file.png")]
                self.embeds = []

        class Channel:
            name = "ticket-1-sam"
            guild = SimpleNamespace(id=1, name="Default")

            def history(self, **kwargs):
                async def gen():
                    for text in ("Hello, I need help", "<b>bold?</b> & \"quotes\"", "Thanks!"):
                        yield Msg(text)
                return gen()

        row = {"id": 1, "category_name": "Support", "opener_id": 5, "opened_at": now}
        bot.cfg._data["tickets"] = {"transcript_format": "both"}
        paths, count = await cog.save_transcript(Channel(), row)
        ok("transcript saved as html and text", count == 3 and sorted(p.suffix for p in paths) == [".html", ".txt"])
        page = paths[0].read_text(encoding="utf-8") if paths[0].suffix == ".html" else paths[1].read_text(encoding="utf-8")
        ok("html is escaped (no injected tags)", "&lt;b&gt;bold?&lt;/b&gt;" in page and "<script>" not in page and "<b>bold" not in page)
        ok("attachment links kept", "https://cdn.example/file.png" in page)
        ok("ticket channel names are safe", tickets.slug("Sám Ω!! name with spaces") == "s-m-name-with-spaces" and tickets.slug("###") == "user")

        print("intents")
        lite = Config({"modules": {"rsvp": True, "moderation": True}}, cfg.path)
        ok("events + moderation alone need no privileged intents", botmod.privileged_in_use(botmod.build_intents(lite)) == [])

        await bot.shutdown()
    print(f"\nAll {passed} module checks passed.")


if __name__ == "__main__":
    asyncio.run(main())
