"""Config loading: one TOML file for settings, one .env file for secrets.

Standard library only (tomllib), so there is nothing extra to install or keep in RAM.
"""
from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

# Modules that can be switched on in [modules]. "system" is always loaded.
KNOWN_MODULES = (
    "music",
    "video",
    "tickets",
    "rsvp",
    "antiraid",
    "antispam",
    "moderation",
    "utility",
)


class ConfigError(Exception):
    """The config file or .env is missing or invalid. Not worth auto-restarting on."""


def load_env(path: Path) -> None:
    """Read KEY=VALUE lines into os.environ. Real environment variables win."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[7:].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


class Config:
    """Read-only view of config.toml with dotted-path lookups."""

    def __init__(self, data: dict[str, Any], path: Path):
        self._data = data
        self.path = path

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def section(self, name: str) -> dict[str, Any]:
        value = self._data.get(name)
        return value if isinstance(value, dict) else {}

    def enabled(self, module: str) -> bool:
        return bool(self.get(f"modules.{module}", False))

    @property
    def enabled_modules(self) -> list[str]:
        return [m for m in KNOWN_MODULES if self.enabled(m)]

    def path_for(self, dotted: str, default: str) -> Path:
        """Resolve a path setting relative to the bot folder."""
        p = Path(str(self.get(dotted, default))).expanduser()
        return p if p.is_absolute() else ROOT / p

    @property
    def data_dir(self) -> Path:
        return self.path_for("bot.data_dir", "data")

    @property
    def token(self) -> str:
        token = os.environ.get("DISCORD_TOKEN", "").strip()
        if not token or token.lower().startswith("paste"):
            raise ConfigError(
                "DISCORD_TOKEN is not set. Copy .env.example to .env and paste your bot token into it."
            )
        return token


def load_config(path: str | os.PathLike | None = None) -> Config:
    """Load .env and the TOML config. Path order: argument, BOT_CONFIG env var, config.toml."""
    load_env(ROOT / ".env")
    cfg_path = Path(path or os.environ.get("BOT_CONFIG") or ROOT / "config.toml")
    if not cfg_path.is_absolute():
        cfg_path = ROOT / cfg_path
    if not cfg_path.is_file():
        raise ConfigError(f"Config file not found: {cfg_path}")
    try:
        # utf-8-sig: Notepad and PowerShell may save with a byte-order mark, which TOML rejects.
        data = tomllib.loads(cfg_path.read_text(encoding="utf-8-sig"))
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{cfg_path.name} is not saved as UTF-8 text: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{cfg_path.name} has a syntax error: {exc}") from exc

    modules = data.get("modules", {})
    if not isinstance(modules, dict):
        raise ConfigError("[modules] must be a table of name = true/false")
    unknown = sorted(set(modules) - set(KNOWN_MODULES))
    if unknown:
        raise ConfigError(
            f"Unknown module(s) in [modules]: {', '.join(unknown)}. "
            f"Known modules: {', '.join(KNOWN_MODULES)}. "
            "To add your own, list it in KNOWN_MODULES in core/config.py."
        )
    for name, value in modules.items():
        if not isinstance(value, bool):
            raise ConfigError(f"modules.{name} must be true or false, got {value!r}")
    return Config(data, cfg_path)
