"""Per-server settings stored in SQLite, cached in memory after the first read.

Values are JSON, so a setting can be a number, string, list or dict.
guild_id 0 is used for bot-wide values (for example the slash-command sync hash).
"""
from __future__ import annotations

import json
from typing import Any

from .db import Database

GLOBAL = 0


class Settings:
    def __init__(self, db: Database):
        self._db = db
        self._cache: dict[int, dict[str, Any]] = {}

    async def _load(self, guild_id: int) -> dict[str, Any]:
        cached = self._cache.get(guild_id)
        if cached is None:
            rows = await self._db.fetchall(
                "SELECT key, value FROM guild_settings WHERE guild_id = ?", (guild_id,)
            )
            cached = {}
            for row in rows:
                try:
                    cached[row["key"]] = json.loads(row["value"])
                except ValueError:
                    continue
            self._cache[guild_id] = cached
        return cached

    async def get(self, guild_id: int, key: str, default: Any = None) -> Any:
        return (await self._load(guild_id)).get(key, default)

    async def all(self, guild_id: int) -> dict[str, Any]:
        return dict(await self._load(guild_id))

    async def set(self, guild_id: int, key: str, value: Any) -> None:
        (await self._load(guild_id))[key] = value
        await self._db.execute(
            "INSERT INTO guild_settings (guild_id, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT (guild_id, key) DO UPDATE SET value = excluded.value",
            (guild_id, key, json.dumps(value, separators=(",", ":"))),
        )

    async def delete(self, guild_id: int, key: str) -> None:
        (await self._load(guild_id)).pop(key, None)
        await self._db.execute(
            "DELETE FROM guild_settings WHERE guild_id = ? AND key = ?", (guild_id, key)
        )

    def forget(self, guild_id: int) -> None:
        """Drop a guild from the memory cache (the bot left that server)."""
        self._cache.pop(guild_id, None)
