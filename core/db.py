"""SQLite access that never blocks the event loop.

One worker thread owns the connection. Callers queue operations; the worker drains
everything that is waiting and commits it as a single transaction. Under load that
batches writes automatically (one fsync for many statements); when idle there is no
timer and no polling, the thread just sleeps on the queue.

Rules for callers:
  * execute / executemany / write_later  -> statements that change data
  * fetchone / fetchall                  -> SELECT only
"""
from __future__ import annotations

import asyncio
import logging
import queue
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable, Sequence

log = logging.getLogger("db")

CORE_SCHEMA = """
CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id INTEGER NOT NULL,
    key      TEXT    NOT NULL,
    value    TEXT    NOT NULL,
    PRIMARY KEY (guild_id, key)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS jobs (
    id       INTEGER PRIMARY KEY,
    run_at   REAL    NOT NULL,
    kind     TEXT    NOT NULL,
    ref      TEXT,
    payload  TEXT    NOT NULL DEFAULT '{}',
    attempts INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS jobs_run_at ON jobs (run_at);
CREATE INDEX IF NOT EXISTS jobs_ref ON jobs (ref);
"""

_MAX_BATCH = 256
_STOP = object()


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._closed = False

    # ------------------------------------------------------------------ lifecycle
    async def open(self) -> None:
        self._loop = asyncio.get_running_loop()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        opened: asyncio.Future = self._loop.create_future()
        self._thread = threading.Thread(target=self._run, args=(opened,), name="sqlite", daemon=True)
        self._thread.start()
        await opened
        await self.script(CORE_SCHEMA)

    async def close(self) -> None:
        if self._closed or self._thread is None:
            return
        self._closed = True
        self._queue.put(_STOP)
        await asyncio.get_running_loop().run_in_executor(None, self._thread.join, 10)

    # ------------------------------------------------------------------ public API
    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Run one write statement. Returns lastrowid for INSERT, else the affected row count."""
        return await self._submit("exec", sql, params)

    async def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> int:
        return await self._submit("many", sql, list(rows))

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        return await self._submit("one", sql, params)

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return await self._submit("all", sql, params)

    async def script(self, sql: str) -> None:
        """Run several statements (schema creation). Each module calls this for its own tables."""
        await self._submit("script", sql, ())

    def write_later(self, sql: str, params: Sequence[Any] = ()) -> None:
        """Fire-and-forget write for hot paths (logs, counters). Errors are logged, not raised."""
        if self._closed:
            return
        self._queue.put(("exec", sql, params, None))

    # ------------------------------------------------------------------ internals
    def _submit(self, kind: str, sql: str, params: Any) -> asyncio.Future:
        if self._closed or self._loop is None:
            raise RuntimeError("database is not open")
        future = self._loop.create_future()
        self._queue.put((kind, sql, params, future))
        return future

    def _resolve(self, future: asyncio.Future | None, result: Any, error: BaseException | None) -> None:
        if future is None:
            if error is not None:
                log.error("Background write failed: %s", error)
            return

        def setter() -> None:
            if future.done():
                return
            if error is not None:
                future.set_exception(error)
            else:
                future.set_result(result)

        try:
            self._loop.call_soon_threadsafe(setter)  # type: ignore[union-attr]
        except RuntimeError:
            pass  # event loop already closed during shutdown

    def _run(self, opened: asyncio.Future) -> None:
        try:
            conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")   # safe with WAL, far fewer fsyncs on an HDD
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA cache_size=-2000")     # 2 MB page cache cap
            conn.execute("PRAGMA temp_store=MEMORY")
            conn.execute("PRAGMA wal_autocheckpoint=200")
        except Exception as exc:  # noqa: BLE001 - report any open failure to the caller
            self._resolve(opened, None, exc)
            return
        self._resolve(opened, None, None)

        stop = False
        while not stop:
            batch = [self._queue.get()]
            while len(batch) < _MAX_BATCH:
                try:
                    batch.append(self._queue.get_nowait())
                except queue.Empty:
                    break
            if any(item is _STOP for item in batch):
                stop = True
                batch = [item for item in batch if item is not _STOP]
            if batch:
                self._run_batch(conn, batch)

        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        conn.close()

    def _run_batch(self, conn: sqlite3.Connection, batch: list) -> None:
        results: list[tuple[asyncio.Future | None, Any, BaseException | None]] = []
        in_tx = False
        for kind, sql, params, future in batch:
            try:
                if kind == "script":
                    if in_tx:
                        conn.execute("COMMIT")
                        in_tx = False
                    conn.executescript(sql)
                    results.append((future, None, None))
                    continue
                if kind in ("exec", "many") and not in_tx:
                    conn.execute("BEGIN")
                    in_tx = True
                if kind == "exec":
                    cur = conn.execute(sql, params)
                    is_insert = sql.lstrip()[:7].upper() in ("INSERT ", "REPLACE")
                    results.append((future, cur.lastrowid if is_insert else cur.rowcount, None))
                elif kind == "many":
                    cur = conn.executemany(sql, params)
                    results.append((future, cur.rowcount, None))
                elif kind == "one":
                    results.append((future, conn.execute(sql, params).fetchone(), None))
                else:
                    results.append((future, conn.execute(sql, params).fetchall(), None))
            except Exception as exc:  # noqa: BLE001 - one bad statement must not kill the worker
                results.append((future, None, exc))

        commit_error: BaseException | None = None
        if in_tx:
            try:
                conn.execute("COMMIT")
            except sqlite3.Error as exc:
                # Typically "database or disk is full". Nothing in this batch was saved.
                commit_error = exc
                log.error("Commit failed, %d statement(s) lost: %s", len(batch), exc)
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass

        for future, result, error in results:
            self._resolve(future, result, error or commit_error)
