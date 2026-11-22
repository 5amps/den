"""Persistent job scheduler: one asyncio task that sleeps until the next job is due.

Jobs live in the `jobs` table, so reminders survive restarts. If the PC was off when a
job was due, it runs as soon as the bot is back (late, never silently dropped).

Usage from a module:
    bot.scheduler.register("rsvp_reminder", self.on_reminder)      # in cog_load
    await bot.scheduler.add(when_epoch, "rsvp_reminder", {"event": 5}, ref="event:5")
    await bot.scheduler.cancel_ref("event:5")
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable

from .db import Database

log = logging.getLogger("scheduler")

Handler = Callable[[dict[str, Any]], Awaitable[None]]

# Upper bound on one sleep. asyncio timers use a monotonic clock that can stall while the
# PC is suspended, so we re-check the wall clock at least this often. Cost: one indexed
# SELECT per minute.
_MAX_SLEEP = 60.0
_MAX_ATTEMPTS = 5
_RETRY_DELAY = 60.0


class Scheduler:
    def __init__(self, db: Database):
        self._db = db
        self._handlers: dict[str, Handler] = {}
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._running: set[asyncio.Task] = set()

    def register(self, kind: str, handler: Handler) -> None:
        self._handlers[kind] = handler
        self._wake.set()

    def unregister(self, kind: str) -> None:
        self._handlers.pop(kind, None)

    async def add(self, run_at: float, kind: str, payload: dict[str, Any] | None = None,
                  ref: str | None = None) -> int:
        job_id = await self._db.execute(
            "INSERT INTO jobs (run_at, kind, ref, payload) VALUES (?, ?, ?, ?)",
            (float(run_at), kind, ref, json.dumps(payload or {}, separators=(",", ":"))),
        )
        self._wake.set()
        return job_id

    async def cancel(self, job_id: int) -> None:
        await self._db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))

    async def cancel_ref(self, ref: str, kind: str | None = None) -> None:
        if kind is None:
            await self._db.execute("DELETE FROM jobs WHERE ref = ?", (ref,))
        else:
            await self._db.execute("DELETE FROM jobs WHERE ref = ? AND kind = ?", (ref, kind))

    def start(self, ready: Callable[[], Awaitable[Any]] | None = None) -> None:
        """Start the loop. `ready` is awaited first (the bot passes wait_until_ready)."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(ready), name="scheduler")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        for task in list(self._running):
            task.cancel()

    # ------------------------------------------------------------------ internals
    async def _loop(self, ready: Callable[[], Awaitable[Any]] | None) -> None:
        if ready is not None:
            await ready()
        while True:
            try:
                delay = await self._run_due()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the scheduler must survive any DB hiccup
                log.exception("Scheduler pass failed; retrying in %ds", int(_MAX_SLEEP))
                delay = _MAX_SLEEP
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    async def _run_due(self) -> float:
        """Dispatch every due job. Returns seconds to sleep before the next check."""
        kinds = list(self._handlers)
        if not kinds:
            return _MAX_SLEEP
        marks = ",".join("?" * len(kinds))
        while True:
            row = await self._db.fetchone(
                f"SELECT id, run_at, kind, ref, payload, attempts FROM jobs "
                f"WHERE kind IN ({marks}) ORDER BY run_at LIMIT 1",
                kinds,
            )
            if row is None:
                return _MAX_SLEEP
            wait = row["run_at"] - time.time()
            if wait > 0:
                return min(wait, _MAX_SLEEP)
            # Remove first so a crash mid-handler cannot replay the job forever;
            # a handler that raises is re-queued below with an attempt counter.
            await self._db.execute("DELETE FROM jobs WHERE id = ?", (row["id"],))
            task = asyncio.create_task(self._dispatch(dict(row)), name=f"job-{row['kind']}")
            self._running.add(task)
            task.add_done_callback(self._running.discard)

    async def _dispatch(self, job: dict[str, Any]) -> None:
        handler = self._handlers.get(job["kind"])
        if handler is None:
            return
        try:
            payload = json.loads(job["payload"])
        except ValueError:
            payload = {}
        try:
            await handler(payload)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            attempts = job["attempts"] + 1
            if attempts >= _MAX_ATTEMPTS:
                log.exception("Job %s failed %d times, dropping it", job["kind"], attempts)
                return
            log.exception("Job %s failed (attempt %d), retrying in %ds",
                          job["kind"], attempts, int(_RETRY_DELAY))
            await self._db.execute(
                "INSERT INTO jobs (run_at, kind, ref, payload, attempts) VALUES (?, ?, ?, ?, ?)",
                (time.time() + _RETRY_DELAY, job["kind"], job["ref"], job["payload"], attempts),
            )
            self._wake.set()
