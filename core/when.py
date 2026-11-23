"""Read times people type: 'in 2h 30m', '18:30', 'tomorrow 9:00', '2026-12-24 18:30'.

Shared by events and reminders. Absolute times use the clock/time zone of the PC running the bot.
"""
from __future__ import annotations

import datetime
import re
import time

from .checks import UserError

RELATIVE_RE = re.compile(
    r"^(?:in\s+)?(?:(\d+)\s*w(?:eeks?)?)?\s*(?:(\d+)\s*d(?:ays?)?)?\s*(?:(\d+)\s*h(?:ours?|rs?)?)?\s*"
    r"(?:(\d+)\s*m(?:in(?:ute)?s?)?)?$", re.I)
DATE_FORMATS = ("%Y-%m-%d %H:%M", "%d-%m-%Y %H:%M", "%d/%m/%Y %H:%M", "%Y-%m-%d", "%H:%M")
HELP = "Use `in 2h 30m`, `45m`, `18:30`, `tomorrow 9:00` or `2026-12-24 18:30`."


def parse_when(text: str) -> float:
    """Text -> epoch seconds. Raises UserError with examples if it cannot be read."""
    text = " ".join(text.strip().lower().split())
    match = RELATIVE_RE.match(text)
    if match and any(match.groups()):
        weeks, days, hours, minutes = (int(g or 0) for g in match.groups())
        return time.time() + weeks * 604800 + days * 86400 + hours * 3600 + minutes * 60
    tomorrow = text.startswith("tomorrow")
    if tomorrow:
        rest = text[len("tomorrow"):].strip().removeprefix("at ").strip() or "9:00"
        try:
            clock = datetime.datetime.strptime(rest, "%H:%M")
        except ValueError:
            raise UserError(f"I could not read that time. {HELP}") from None
        day = datetime.datetime.now() + datetime.timedelta(days=1)
        return day.replace(hour=clock.hour, minute=clock.minute, second=0, microsecond=0).timestamp()
    for fmt in DATE_FORMATS:
        try:
            parsed = datetime.datetime.strptime(text, fmt)
        except ValueError:
            continue
        now = datetime.datetime.now()
        if fmt == "%H:%M":
            parsed = now.replace(hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0)
            if parsed <= now:
                parsed += datetime.timedelta(days=1)
        elif fmt == "%Y-%m-%d":
            parsed = parsed.replace(hour=12)
        return parsed.timestamp()
    raise UserError(f"I could not read that time. {HELP}")
