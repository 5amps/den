"""Choose the music and video folders from Discord, no config file editing needed.

    /library setup    a panel: pick a folder from a list or type a path, remove one, rescan
    /library add      add a folder directly (the folder box suggests folders as you type)
    /library remove   stop using a folder

Owner only. These commands look at the file system of the PC that runs the bot, so they
are limited to the owner of the bot application (the person whose Developer Portal account
created it), not server admins. Replies are only visible to the person who ran them.
The bot only ever READS the chosen folders; it never writes, moves or deletes your files.

Folders picked here are stored in the bot's database (data/bot.db) and are used together
with any folders listed in config.toml.
"""
from __future__ import annotations

import asyncio
import logging
import os
import string
from pathlib import Path
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands

from cogs.music import AUDIO_EXTENSIONS
from cogs.video import VIDEO_EXTENSIONS
from core.checks import UserError, owner_only
from core.settings import GLOBAL

log = logging.getLogger("library")

# kind -> (config key, library table kind, label, default extensions, extensions config key)
KINDS: dict[str, tuple[str, str, str, list[str], str]] = {
    "music": ("music.library_dirs", "audio", "🎵 Music", AUDIO_EXTENSIONS, "music.extensions"),
    "video": ("video.library_dirs", "video", "🎬 Video", VIDEO_EXTENSIONS, "video.extensions"),
}
CUSTOM = "__custom__"
MAX_FOLDERS = 20                    # per kind; keeps the panel inside Discord's 25-option limit


# ---------------------------------------------------------------------------- folder helpers (no Discord)
def normalize(text: str) -> Path:
    """Turn what someone typed or pasted into an absolute path."""
    text = text.strip().strip('"').strip("'").strip()
    return Path(os.path.expandvars(os.path.expanduser(text))).resolve()


def problem_with(path: Path) -> str | None:
    """Why this folder cannot be used, or None if it is fine."""
    if not path.exists():
        return f"`{path}` does not exist on the PC running the bot. Check the spelling, or pick one from the list."
    if not path.is_dir():
        return f"`{path}` is a file, not a folder. Pick the folder that contains it."
    if path.parent == path:
        return f"`{path}` is a whole drive. That is too much to scan; pick a folder inside it, like Music."
    if path == Path.home():
        return "Your whole user folder is too much to scan. Pick a folder inside it, like Music or Videos."
    if not os.access(path, os.R_OK | os.X_OK):
        return f"The bot is not allowed to read `{path}`. Pick another folder or fix its permissions."
    return None


def same(a: str | Path, b: str | Path) -> bool:
    return os.path.normcase(str(a)) == os.path.normcase(str(b))


def inside(child: str | Path, parent: str | Path) -> bool:
    """True if child is parent or somewhere below it."""
    c, p = os.path.normcase(str(child)), os.path.normcase(str(parent)).rstrip("\\/")
    return c == p or c.startswith(p + os.sep)


def suggested_folders(kind: str) -> list[Path]:
    """Usual places for media on this PC, the most likely ones first. Only folders that exist."""
    home = Path.home()
    first = ["Music"] if kind == "music" else ["Videos", "Movies"]
    names = first + ["Downloads", "Desktop", "Documents"] + (["Videos"] if kind == "music" else ["Music"])
    candidates = [home / n for n in names] + [home / "OneDrive" / n for n in names]
    result: list[Path] = []
    for path in candidates:
        try:
            if path.is_dir() and not any(same(path, r) for r in result):
                result.append(path)
        except OSError:
            continue
    return result


def drives() -> list[str]:
    if os.name != "nt":
        return ["/"]
    return [f"{letter}:\\" for letter in string.ascii_uppercase if os.path.isdir(f"{letter}:\\")]


def complete_path(current: str, kind: str, limit: int = 25) -> list[str]:
    """Folder suggestions for what has been typed so far (runs in a thread)."""
    typed = current.strip().strip('"')
    if not typed:
        return [str(p) for p in suggested_folders(kind)][:limit]
    if os.name == "nt" and len(typed) <= 2 and typed[:1].isalpha():      # "D" or "D:" -> drives
        return [d for d in drives() if d.lower().startswith(typed.lower())][:limit]
    expanded = os.path.expandvars(os.path.expanduser(typed))
    if expanded.endswith(("\\", "/")):
        base, prefix = Path(expanded), ""
    else:
        base, prefix = Path(expanded).parent, Path(expanded).name.lower()
    try:
        names = sorted((e.name for e in os.scandir(base) if e.is_dir() and not e.name.startswith((".", "$"))),
                       key=str.lower)
    except OSError:
        return []
    out = []
    if not prefix and base.is_dir():
        out.append(str(base))
    out += [str(base / n) for n in names if n.lower().startswith(prefix)]
    return [p for p in out if len(p) <= 100][:limit]


def short(path: str, width: int = 100) -> str:
    return path if len(path) <= width else "…" + path[-(width - 1):]


# ---------------------------------------------------------------------------- cog
class Library(commands.Cog):
    library = app_commands.Group(
        name="library", description="Owner: choose the music and video folders",
        guild_only=True, default_permissions=discord.Permissions(administrator=True))

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @property
    def music(self):
        return self.bot.get_cog("Music")

    def video_on(self) -> bool:
        return self.bot.get_cog("Video") is not None

    def kinds(self) -> list[str]:
        return ["music", "video"] if self.video_on() else ["music"]

    def config_dirs(self, kind: str) -> list[str]:
        return [str(Path(str(p)).expanduser()) for p in (self.bot.cfg.get(KINDS[kind][0], []) or [])]

    def picked_dirs(self, kind: str) -> list[str]:
        return self.music.extra_dirs.setdefault(KINDS[kind][0], [])

    async def save(self, kind: str) -> None:
        key = KINDS[kind][0]
        await self.bot.settings.set(GLOBAL, f"dirs:{key}", self.picked_dirs(kind))

    async def rescan(self, kind: str) -> tuple[int, int, int]:
        key, table_kind, _, extensions, ext_key = KINDS[kind]
        return await self.music.rescan(table_kind, self.music.library_dirs(key), extensions, ext_key)

    async def add_folder(self, kind: str, text: str) -> str:
        if not text.strip():
            raise UserError("Type a folder path or pick one from the list.")
        path = await asyncio.to_thread(normalize, text)
        problem = await asyncio.to_thread(problem_with, path)
        if problem:
            raise UserError(problem)
        current = self.config_dirs(kind) + self.picked_dirs(kind)
        for existing in current:
            if inside(path, existing):
                what = "already in the list" if same(path, existing) else f"already included, because it is inside `{existing}`"
                raise UserError(f"`{path}` is {what}.")
        if len(self.picked_dirs(kind)) >= MAX_FOLDERS:
            raise UserError(f"You can add up to {MAX_FOLDERS} {kind} folders. Remove one first.")
        self.picked_dirs(kind).append(str(path))
        await self.save(kind)
        added, _, total = await self.rescan(kind)
        log.info("%s folder added: %s (%d files found)", kind, path, total)
        noun = "track" if kind == "music" else "video file"
        found = f"{added} new {noun}(s) found" if added else f"no {noun}s in it yet; put files there and press **Rescan**"
        return f"Added `{path}`: {found}. Library total: {total}."

    async def remove_folder(self, kind: str, text: str) -> str:
        picked = self.picked_dirs(kind)
        match = next((p for p in picked if same(p, text) or same(p, normalize(text))), None)
        if match is None:
            if any(same(p, text) for p in self.config_dirs(kind)):
                raise UserError(f"`{text}` is set in config.toml. Remove it there and restart the bot.")
            raise UserError(f"`{text}` is not one of the {kind} folders.")
        picked.remove(match)
        await self.save(kind)
        _, removed, total = await self.rescan(kind)
        log.info("%s folder removed: %s", kind, match)
        return f"Removed `{match}`. {removed} file(s) left the library; {total} remain."

    async def counts(self, kind: str) -> dict[str, int]:
        """Files in the library per folder."""
        rows = await self.bot.db.fetchall("SELECT path FROM library WHERE kind = ?", (KINDS[kind][1],))
        folders = self.config_dirs(kind) + self.picked_dirs(kind)
        result = {f: 0 for f in folders}
        for row in rows:
            for folder in folders:
                if inside(row["path"], folder):
                    result[folder] += 1
                    break
        return result

    # ---- panel ----
    async def panel_embed(self, status: str = "") -> discord.Embed:
        embed = discord.Embed(
            title="📂 Media folders",
            colour=discord.Colour.blurple(),
            description=(
                (f"{status}\n\n" if status else "")
                + "Choose folders on **the PC running the bot**. Every file inside them (subfolders too) "
                  "shows up in `/play`" + (" and `/video play`" if self.video_on() else "") + ".\n"
                  "Added new files later? Press **Rescan**. The bot only reads these folders; "
                  "it never changes or deletes your files."))
        for kind in self.kinds():
            _, _, label, extensions, ext_key = KINDS[kind]
            config = self.config_dirs(kind)
            counts = await self.counts(kind)
            lines = []
            for folder, n in counts.items():
                tag = " · *config.toml*" if any(same(folder, c) for c in config) else ""
                missing = "" if os.path.isdir(folder) else " · ⚠️ *not found*"
                lines.append(f"`{short(folder, 70)}` · {n} file(s){tag}{missing}")
            exts = " ".join(sorted({str(e).lower() for e in (self.bot.cfg.get(ext_key) or extensions)}))
            value = "\n".join(lines) if lines else "*No folder yet. Pick one below.*"
            embed.add_field(name=label, value=f"{value}\n-# File types: {exts}"[:1024], inline=False)
        if not self.video_on():
            embed.set_footer(text="Video folders: the video module is off (set video = true in config.toml).")
        return embed

    async def send_panel(self, interaction: discord.Interaction, status: str = "", edit: bool = False) -> None:
        embed = await self.panel_embed(status)
        view = PanelView(self, interaction.user.id)
        if edit:
            await interaction.edit_original_response(embed=embed, view=view)
        else:
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    # ---- commands ----
    @library.command(name="setup", description="Owner: pick the music and video folders from a list.")
    @owner_only()
    async def setup_cmd(self, interaction: discord.Interaction) -> None:
        await self.send_panel(interaction)

    async def _complete_add(self, interaction: discord.Interaction, current: str):
        if not await interaction.client.is_owner(interaction.user):
            return []
        kind = getattr(interaction.namespace, "type", None) or "music"
        paths = await asyncio.to_thread(complete_path, current, kind)
        return [app_commands.Choice(name=short(p), value=p) for p in paths]

    async def _complete_remove(self, interaction: discord.Interaction, current: str):
        if not await interaction.client.is_owner(interaction.user):
            return []
        kind = getattr(interaction.namespace, "type", None) or "music"
        return [app_commands.Choice(name=short(p), value=p) for p in self.picked_dirs(kind)
                if current.lower() in p.lower()][:25]

    @library.command(name="add", description="Owner: add a music or video folder.")
    @app_commands.describe(type="Music or video", folder="Start typing a path; pick from the suggestions")
    @app_commands.autocomplete(folder=_complete_add)
    @owner_only()
    async def add(self, interaction: discord.Interaction, type: Literal["music", "video"], folder: str) -> None:
        if type == "video" and not self.video_on():
            raise UserError("The video module is off. Set `video = true` under [modules] in config.toml and restart.")
        await interaction.response.defer(ephemeral=True, thinking=True)
        await interaction.followup.send(await self.add_folder(type, folder), ephemeral=True)

    @library.command(name="remove", description="Owner: stop using a music or video folder.")
    @app_commands.describe(type="Music or video", folder="Pick the folder to remove")
    @app_commands.autocomplete(folder=_complete_remove)
    @owner_only()
    async def remove(self, interaction: discord.Interaction, type: Literal["music", "video"], folder: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await interaction.followup.send(await self.remove_folder(type, folder), ephemeral=True)


# ---------------------------------------------------------------------------- panel UI
class PathModal(discord.ui.Modal):
    def __init__(self, cog: Library, kind: str):
        super().__init__(title=f"Add a {kind} folder")
        self.cog, self.kind = cog, kind
        example = str(Path.home() / ("Music" if kind == "music" else "Videos"))
        self.path = discord.ui.TextInput(
            label="Folder path on the bot's PC", placeholder=example[:100], max_length=400,
            style=discord.TextStyle.short)
        self.add_item(self.path)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        await self.cog.send_panel(interaction, await run(self.cog.add_folder(self.kind, self.path.value)), edit=True)


async def run(coro) -> str:
    """Run a panel action and turn a problem into a status line instead of an error popup."""
    try:
        return "✅ " + await coro
    except UserError as exc:
        return f"⚠️ {exc}"
    except Exception:  # noqa: BLE001
        log.exception("Library panel action failed")
        return "⚠️ Something went wrong. Details are in the bot's log."


class AddSelect(discord.ui.Select):
    def __init__(self, cog: Library, kind: str, row: int):
        self.cog, self.kind = cog, kind
        taken = cog.config_dirs(kind) + cog.picked_dirs(kind)
        self.paths = [str(p) for p in suggested_folders(kind) if not any(inside(p, t) for t in taken)][:24]
        options = [discord.SelectOption(label=Path(p).name[:100], description=short(p), value=str(i), emoji="📁")
                   for i, p in enumerate(self.paths)]
        options.append(discord.SelectOption(label="Type a folder path…", value=CUSTOM, emoji="⌨️",
                                            description="Any folder on the bot's PC, e.g. on another drive"))
        super().__init__(placeholder=f"➕ Add a {kind} folder…", options=options, row=row)

    async def callback(self, interaction: discord.Interaction) -> None:
        choice = self.values[0]
        if choice == CUSTOM:
            await interaction.response.send_modal(PathModal(self.cog, self.kind))
            return
        await interaction.response.defer()
        await self.cog.send_panel(interaction, await run(self.cog.add_folder(self.kind, self.paths[int(choice)])), edit=True)


class RemoveSelect(discord.ui.Select):
    def __init__(self, cog: Library, row: int):
        self.cog = cog
        options = []
        for kind in cog.kinds():
            for i, path in enumerate(cog.picked_dirs(kind)):
                options.append(discord.SelectOption(
                    label=Path(path).name[:100] or path[:100], description=short(path),
                    value=f"{kind}:{i}", emoji="🎵" if kind == "music" else "🎬"))
        disabled = not options
        if disabled:
            options = [discord.SelectOption(label="Nothing to remove", value="none")]
        super().__init__(placeholder="➖ Remove a folder…" if not disabled else "➖ No folders added from Discord yet",
                         options=options[:25], row=row, disabled=disabled)

    async def callback(self, interaction: discord.Interaction) -> None:
        kind, index = self.values[0].split(":")
        picked = self.cog.picked_dirs(kind)
        await interaction.response.defer()
        if int(index) >= len(picked):
            status = "⚠️ That list was out of date. Try again."
        else:
            status = await run(self.cog.remove_folder(kind, picked[int(index)]))
        await self.cog.send_panel(interaction, status, edit=True)


class PanelView(discord.ui.View):
    def __init__(self, cog: Library, owner_id: int):
        super().__init__(timeout=900)
        self.cog, self.owner_id = cog, owner_id
        row = 0
        for kind in cog.kinds():
            self.add_item(AddSelect(cog, kind, row))
            row += 1
        self.add_item(RemoveSelect(cog, row))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id

    @discord.ui.button(label="Rescan", emoji="🔄", style=discord.ButtonStyle.primary, row=3)
    async def rescan_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer()
        parts = []
        for kind in self.cog.kinds():
            added, removed, total = await self.cog.rescan(kind)
            parts.append(f"{KINDS[kind][2]}: {total} file(s), {added} new, {removed} gone")
        await self.cog.send_panel(interaction, "🔄 " + " · ".join(parts), edit=True)

    @discord.ui.button(label="Done", style=discord.ButtonStyle.secondary, row=3)
    async def done_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(view=None)
        self.stop()
