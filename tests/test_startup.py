"""Offline test of the startup/retry logic: simulates "no internet" then "bad token".

    Windows:  .venv\\Scripts\\python.exe tests\\test_startup.py
    Linux:    .venv/bin/python tests/test_startup.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp  # noqa: E402
import discord  # noqa: E402

import bot as botmod  # noqa: E402
from core.config import Config  # noqa: E402


async def main() -> None:
    os.environ["DISCORD_TOKEN"] = "not-a-real-token"
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
        cfg = Config({"bot": {"data_dir": folder}, "modules": {}}, Path(folder) / "config.toml")

        attempts = 0
        waits: list[float] = []
        real_sleep = asyncio.sleep

        async def fake_login(self, token):  # noqa: ANN001
            nonlocal attempts
            attempts += 1
            if attempts <= 3:
                raise aiohttp.ClientConnectionError("simulated: no internet")
            raise discord.LoginFailure("simulated: bad token")

        async def fast_sleep(seconds, *args, **kwargs):  # noqa: ANN001
            waits.append(seconds)
            await real_sleep(0)

        discord.http.HTTPClient.static_login = fake_login
        botmod.asyncio.sleep = fast_sleep
        try:
            code = await botmod.run_bot(cfg)
        finally:
            botmod.asyncio.sleep = real_sleep

        assert attempts == 4, attempts
        assert len(waits) == 3 and waits[0] < waits[1] < waits[2], waits
        assert code == botmod.EXIT_CONFIG, code
        print(f"  ok  3 failed connections retried with growing waits: {[round(w, 1) for w in waits]}s")
        print(f"  ok  bad token stops with exit code {code} (start script will not restart)")

        # Intents follow the enabled modules.
        none = botmod.build_intents(cfg)
        assert [n for n, on in none if on] == ["guilds"]
        some = botmod.build_intents(Config({"modules": {"antispam": True, "music": True}}, cfg.path))
        assert {n for n, on in some if on} == {"guilds", "guild_messages", "message_content", "voice_states"}
        assert botmod.privileged_in_use(some) == ["Message Content Intent"]
        print("  ok  intents are derived from enabled modules only")
    print("\nStartup checks passed.")


if __name__ == "__main__":
    asyncio.run(main())
