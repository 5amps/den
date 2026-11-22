"""Process memory and disk readings without psutil."""
from __future__ import annotations

import os
import shutil
import sys
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
        with open("/proc/self/statm", encoding="ascii") as fh:  # Linux and Android/Termux
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
