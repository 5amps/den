"""Memory, CPU, GPU and disk readings without psutil."""
from __future__ import annotations

import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path


def rss_mb() -> float:
    """Resident memory of this process in MB (0.0 if it cannot be read)."""
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            class Counters(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = Counters()
            counters.cb = ctypes.sizeof(Counters)
            kernel32 = ctypes.windll.kernel32
            psapi = ctypes.windll.psapi
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
            if not psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
                return 0.0
            return counters.WorkingSetSize / 1_048_576
        with open("/proc/self/statm", encoding="ascii") as fh:  # Linux
            pages = int(fh.read().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE") / 1_048_576
    except Exception:  # noqa: BLE001 - a status readout must never raise
        try:
            import resource

            peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            return peak / (1_048_576 if sys.platform == "darwin" else 1024)
        except Exception:  # noqa: BLE001
            return 0.0


def free_disk_mb(path: Path) -> float:
    try:
        return shutil.disk_usage(path).free / 1_048_576
    except OSError:
        return 0.0


def file_mb(path: Path) -> float:
    try:
        return path.stat().st_size / 1_048_576
    except OSError:
        return 0.0


# ---------------------------------------------------------------------------- CPU / RAM / GPU usage
# Everything below reads the operating system directly (ctypes on Windows, /proc on Linux and
# similar systems), so no extra package is needed. Any reading that is not available is None.
_CORES = os.cpu_count() or 1


@dataclass
class Usage:
    bot_cpu: float | None           # % of the whole CPU (all cores = 100 %), like Task Manager
    bot_ram_mb: float
    helpers: int                    # child processes: ffmpeg, yt-dlp
    helpers_cpu: float | None
    helpers_ram_mb: float
    pc_cpu: float | None
    pc_ram_used_mb: float | None
    pc_ram_total_mb: float | None
    cores: int = _CORES


@dataclass
class _Snap:
    t: float
    own: float                                  # CPU seconds used by the bot
    kids: dict[int, tuple[float, float]]        # pid -> (CPU seconds, RAM MB)
    system: tuple[float, float] | None          # (busy seconds, total seconds) for the whole PC


def _descendants(rows: list[tuple[int, int]], parent: int) -> list[int]:
    """All children and grandchildren of parent, from (pid, parent pid) rows."""
    parents, pids, changed = {parent}, [], True
    while changed:
        changed = False
        for pid, ppid in rows:
            if ppid in parents and pid not in parents and pid != 0:
                parents.add(pid)
                pids.append(pid)
                changed = True
    return pids


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.windll.kernel32
    _psapi = ctypes.windll.psapi

    class _Entry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]

    class _Mem(ctypes.Structure):
        _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

    class _Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

    _k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_Entry)]
    _k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_Entry)]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    _psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(_Counters), wintypes.DWORD]
    _INVALID = wintypes.HANDLE(-1).value

    def _ft(ft: wintypes.FILETIME) -> float:
        return ((ft.dwHighDateTime << 32) | ft.dwLowDateTime) / 1e7

    def _children(parent: int) -> list[int]:
        snap = _k32.CreateToolhelp32Snapshot(2, 0)          # TH32CS_SNAPPROCESS
        if not snap or snap == _INVALID:
            return []
        rows: list[tuple[int, int]] = []
        try:
            entry = _Entry()
            entry.dwSize = ctypes.sizeof(_Entry)
            ok = _k32.Process32FirstW(snap, ctypes.byref(entry))
            while ok:
                rows.append((entry.th32ProcessID, entry.th32ParentProcessID))
                ok = _k32.Process32NextW(snap, ctypes.byref(entry))
        finally:
            _k32.CloseHandle(snap)
        return _descendants(rows, parent)

    def _proc_reading(pid: int) -> tuple[float, float] | None:
        handle = _k32.OpenProcess(0x1000, False, pid)        # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            c, e, k, u = (wintypes.FILETIME() for _ in range(4))
            if not _k32.GetProcessTimes(handle, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
                return None
            counters = _Counters()
            counters.cb = ctypes.sizeof(_Counters)
            ok = _psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb)
            return _ft(k) + _ft(u), counters.WorkingSetSize / 1_048_576 if ok else 0.0
        finally:
            _k32.CloseHandle(handle)

    def _system_times() -> tuple[float, float] | None:
        idle, kernel, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
        if not _k32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            return None
        total = _ft(kernel) + _ft(user)                      # kernel time includes idle time
        return total - _ft(idle), total

    def system_memory_mb() -> tuple[float, float] | None:
        """(used, total) RAM of the whole PC in MB."""
        mem = _Mem()
        mem.dwLength = ctypes.sizeof(_Mem)
        if not _k32.GlobalMemoryStatusEx(ctypes.byref(mem)):
            return None
        return (mem.ullTotalPhys - mem.ullAvailPhys) / 1_048_576, mem.ullTotalPhys / 1_048_576

else:
    _TICK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
    _PAGE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096

    def _stat(pid: int) -> list[str] | None:
        try:
            with open(f"/proc/{pid}/stat", encoding="ascii", errors="replace") as fh:
                return fh.read().rsplit(")", 1)[1].split()
        except (OSError, IndexError):
            return None

    def _children(parent: int) -> list[int]:
        rows: list[tuple[int, int]] = []
        try:
            names = os.listdir("/proc")
        except OSError:
            return []
        for name in names:
            if name.isdigit():
                fields = _stat(int(name))
                if fields:
                    rows.append((int(name), int(fields[1])))
        return _descendants(rows, parent)

    def _proc_reading(pid: int) -> tuple[float, float] | None:
        fields = _stat(pid)
        if not fields:
            return None
        try:
            with open(f"/proc/{pid}/statm", encoding="ascii") as fh:
                ram = int(fh.read().split()[1]) * _PAGE / 1_048_576
        except (OSError, IndexError, ValueError):
            ram = 0.0
        return (int(fields[11]) + int(fields[12])) / _TICK, ram

    def _system_times() -> tuple[float, float] | None:
        try:                                                 # some systems refuse this: reading is None
            with open("/proc/stat", encoding="ascii") as fh:
                values = [int(v) for v in fh.readline().split()[1:]]
        except (OSError, ValueError):
            return None
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        total = sum(values[:8])
        return (total - idle) / _TICK, total / _TICK

    def system_memory_mb() -> tuple[float, float] | None:
        """(used, total) RAM of the whole PC in MB."""
        try:
            info = {}
            with open("/proc/meminfo", encoding="ascii") as fh:
                for line in fh:
                    key, value = line.split(":", 1)
                    info[key] = int(value.split()[0]) / 1024
            return info["MemTotal"] - info.get("MemAvailable", info.get("MemFree", 0)), info["MemTotal"]
        except (OSError, KeyError, ValueError):
            return None


class Sampler:
    """CPU use is a rate, so it needs two readings. Each read() compares with the previous one;
    the very first read takes a short 0.5 s sample. Blocking: call it in a thread."""

    def __init__(self) -> None:
        self._last: _Snap | None = None

    def _snap(self) -> _Snap:
        times = os.times()
        kids: dict[int, tuple[float, float]] = {}
        try:
            for pid in _children(os.getpid()):
                reading = _proc_reading(pid)
                if reading:
                    kids[pid] = reading
        except Exception:  # noqa: BLE001 - a status readout must never raise
            kids = {}
        try:
            system = _system_times()
        except Exception:  # noqa: BLE001
            system = None
        return _Snap(time.monotonic(), times.user + times.system, kids, system)

    def read(self) -> Usage:
        base = self._last
        snap = self._snap()
        if base is None or snap.t - base.t < 0.3:
            base = snap
            time.sleep(0.5)
            snap = self._snap()
        self._last = snap
        wall = max(snap.t - base.t, 1e-6) * _CORES

        def pct(seconds: float) -> float:
            return max(0.0, min(100.0, seconds / wall * 100))

        kids_cpu = sum(cpu - base.kids.get(pid, (0.0, 0.0))[0] for pid, (cpu, _) in snap.kids.items())
        pc_cpu = None
        if snap.system and base.system and snap.system[1] > base.system[1]:
            busy = (snap.system[0] - base.system[0]) / (snap.system[1] - base.system[1]) * 100
            pc_cpu = max(0.0, min(100.0, busy))
        try:
            memory = system_memory_mb()
        except Exception:  # noqa: BLE001
            memory = None
        return Usage(
            bot_cpu=pct(snap.own - base.own), bot_ram_mb=rss_mb(),
            helpers=len(snap.kids), helpers_cpu=pct(kids_cpu),
            helpers_ram_mb=sum(ram for _, ram in snap.kids.values()),
            pc_cpu=pc_cpu, pc_ram_used_mb=memory[0] if memory else None,
            pc_ram_total_mb=memory[1] if memory else None)


async def gpu_report() -> list[str]:
    """One line per NVIDIA GPU: name, how busy it is, memory. Empty if there is no NVIDIA GPU
    (other GPUs have no standard tool to ask). The bot itself never uses the GPU."""
    from . import proc

    if not proc.which("nvidia-smi"):
        return []
    try:
        code, out, _ = await proc.run(
            ["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"], timeout=5)
    except Exception:  # noqa: BLE001
        return []
    if code != 0:
        return []
    lines = []
    for row in out.decode("utf-8", "replace").strip().splitlines():
        parts = [p.strip() for p in row.split(",")]
        if len(parts) == 4:
            try:
                lines.append(f"{parts[0]} · {parts[1]}% busy · "
                             f"{float(parts[2]) / 1024:.1f} / {float(parts[3]) / 1024:.1f} GB")
            except ValueError:
                continue
    return lines
