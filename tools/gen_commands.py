"""Writes COMMANDS.md from the commands the bot really registers. No token needed.

    Windows:  .venv\\Scripts\\python.exe tools\\gen_commands.py
    Linux:    .venv/bin/python tools/gen_commands.py

Run it after adding or changing a command, then commit COMMANDS.md.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import bot as botmod  # noqa: E402
from core import helpdata  # noqa: E402
from core.config import KNOWN_MODULES, Config  # noqa: E402


async def build() -> str:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
        # Every module on, YouTube on, so the reference covers the full command set.
        cfg = Config({"bot": {"data_dir": folder}, "modules": {m: True for m in KNOWN_MODULES},
                      "music": {"youtube": True}}, Path(folder) / "config.toml")
        bot = botmod.DenBot(cfg)
        await bot.prepare()
        sections = helpdata.collect(bot)
        await bot.shutdown()

    total = sum(len(infos) for *_, infos in sections)
    out = [
        "# Command reference",
        "",
        f"All {total} slash commands, generated from the code by `tools/gen_commands.py`.",
        "In Discord, `/help` shows the same list for the modules that are switched on.",
        "",
        "- `<name>` is required, `[name]` is optional.",
        "- **Who** is the permission Discord asks for by default. Server admins can change it under",
        "  Server Settings > Integrations.",
        "- A command only exists while its module is enabled in `config.toml`.",
        "",
        "## New here? Start with these",
        "",
        "| I want to... | Do this |",
        "|---|---|",
        "| See what the bot can do | `/help` |",
        "| Play a song | Join a voice channel, type `/play`, start typing the title, pick it |",
        "| See or change what is playing | `/nowplaying`, `/queue`, `/skip`, `/pause`, `/stop` |",
        "| Answer an event | `/event list`, then press Going / Maybe / Can't go on the event |",
        "| Talk to staff privately | Press the button on the ticket panel |",
        "| Check the bot is alive | `/ping` |",
        "",
        "## First-time setup for server admins",
        "",
        "| Step | Command |",
        "|---|---|",
        "| 1. Choose where logs go | `/config log-channel #mod-log` |",
        "| 2. Say who is staff | `/config staff-role @Moderator` |",
        "| 3. Create a ticket type and post the panel | `/ticket category-add Support @Moderator`, then `/ticket panel` |",
        "| 4. Greet new members | `/welcome set #welcome` |",
        "| 5. Optional verify button | `/antiraid verify-setup @Member` |",
        "| 6. Check everything | `/status`, `/antispam status`, `/antiraid status` |",
        "",
    ]
    for name, heading, summary, start, infos in sections:
        out += [f"## {heading}", "", f"{summary} Module name in `config.toml`: `{name.lower()}`"
                + (" (always on)." if name == "System" else "."), "", f"**Start here:** {start}", "",
                "| Command | What it does | Who |", "|---|---|---|"]
        for info in infos:
            out.append(f"| `{info.usage}` | {info.description} | {info.access} |")
        described = [(i, [(n, r, d) for n, r, d in i.options if d]) for i in infos]
        described = [(i, opts) for i, opts in described if opts]
        if described:
            out += ["", "<details><summary>Options explained</summary>", ""]
            for info, opts in described:
                out.append(f"**`/{info.name}`**")
                out += [f"- `{n}`{'' if r else ' (optional)'}: {d}" for n, r, d in opts]
                out.append("")
            out.append("</details>")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def main() -> None:
    text = asyncio.run(build())
    target = ROOT / "COMMANDS.md"
    target.write_text(text, encoding="utf-8", newline="\n")
    print(f"Wrote {target.name}: {text.count(chr(10))} lines")


if __name__ == "__main__":
    main()
