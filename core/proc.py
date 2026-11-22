"""Managed subprocesses (ffmpeg, yt-dlp): timeouts, whole-tree kill, no zombies.

Every process started through here is tracked and is killed when it times out, when the
awaiting task is cancelled, or when the bot shuts down.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import subprocess
import sys
from typing import Sequence

log = logging.getLogger("proc")

_WINDOWS = sys.platform == "win32"
_live: set[asyncio.subprocess.Process] = set()


class ProcessError(Exception):
    """A helper program is missing, timed out, or could not be started."""


def which(name: str) -> str | None:
    """Full path of an executable on PATH (or next to the bot), else None."""
    return shutil.which(name)


def spawn_kwargs() -> dict:
    """Flags that put the child in its own group so the whole tree can be killed."""
    if _WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True}


async def start(cmd: Sequence[str], *, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE) -> asyncio.subprocess.Process:
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdin=stdin, stdout=stdout, stderr=stderr, **spawn_kwargs()
        )
    except FileNotFoundError as exc:
        raise ProcessError(f"'{cmd[0]}' was not found. Install it or add it to PATH.") from exc
    except OSError as exc:
        raise ProcessError(f"Could not start '{cmd[0]}': {exc}") from exc
    _live.add(proc)
    return proc


def kill_tree(pid: int) -> None:
    """Kill a process and everything it started. Synchronous and safe to call twice."""
    try:
        if _WINDOWS:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW, timeout=10, check=False,
            )
        else:
            os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, subprocess.TimeoutExpired, OSError):
        pass


async def stop(proc: asyncio.subprocess.Process, grace: float = 2.0) -> None:
    """Terminate a tracked process, escalate to a tree kill, and reap it."""
    try:
        if proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=grace)
            except asyncio.TimeoutError:
                await asyncio.get_running_loop().run_in_executor(None, kill_tree, proc.pid)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    log.warning("Process %s did not exit after kill", proc.pid)
    finally:
        _live.discard(proc)
        # Close our ends of the pipes now. Otherwise a killed process leaves them for the
        # garbage collector, which complains if that happens after the event loop closed.
        transport = getattr(proc, "_transport", None)
        if transport is not None:
            try:
                transport.close()
            except Exception:  # noqa: BLE001
                pass


async def run(cmd: Sequence[str], *, timeout: float, input_data: bytes | None = None,
              max_output: int = 4_000_000) -> tuple[int, bytes, bytes]:
    """Run a command to completion. Returns (exit code, stdout, stderr).

    Raises ProcessError if the program is missing or exceeds `timeout` seconds.
    """
    proc = await start(
        cmd, stdin=asyncio.subprocess.PIPE if input_data is not None else asyncio.subprocess.DEVNULL
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(input_data), timeout=timeout)
    except asyncio.TimeoutError as exc:
        await stop(proc, grace=0.5)
        raise ProcessError(f"'{cmd[0]}' timed out after {int(timeout)}s") from exc
    except asyncio.CancelledError:
        await asyncio.shield(stop(proc, grace=0.5))
        raise
    finally:
        _live.discard(proc)
    return proc.returncode or 0, out[:max_output], err[:max_output]


async def stop_all() -> None:
    """Kill every helper still running. Called once on shutdown."""
    for proc in list(_live):
        await stop(proc, grace=0.5)


def live_count() -> int:
    return sum(1 for p in _live if p.returncode is None)
