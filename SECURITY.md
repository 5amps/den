# Security

Den runs on your own computer and has a key to your Discord server, so here is what it
protects against, how, and what stays your responsibility.

## Reporting a problem

Please open a GitHub issue **without** exploit details and ask for a private contact, or use
GitHub's "Report a vulnerability" button on the Security tab if it is enabled. Never post a bot
token in an issue.

## Attacks considered and how Den handles them

| Attack | Protection | Tested by |
|---|---|---|
| **Stolen bot token** | The token lives only in `.env`, which is git-ignored and created with owner-only permissions on Linux. It is never logged or shown; the setup wizard checks it without printing it. | `.gitignore`, wizard |
| **Members using staff commands** | Every staff command is checked by the bot itself (`has_permissions`, `staff_only`, `owner_only`), not only by Discord's menu settings, which server admins can change. | `test_modules.py` (`/status` check) |
| **Making the bot post where you can't** | `/poll create` and `/watch start` refuse if you couldn't post there yourself (for example, muted, or a read-only channel). | `test_modules.py` |
| **Mass pings through the bot** | `@everyone`, `@here` and role pings are switched off for every message the bot sends. | `bot.py` (`allowed_mentions`) |
| **Moderating someone above you** | Kick, ban and timeout refuse targets whose top role is at or above yours or the bot's. | `core/modlog.py` (`can_act_on`) |
| **Command injection** | Helper programs (ffmpeg, ffprobe, yt-dlp, cloudflared) are started with an argument list, never through a shell. User text is placed after `--` where a tool accepts options. YouTube links are checked against a list of YouTube hosts. | `core/proc.py`, `test_core.py` |
| **SQL injection** | All database queries use parameters. The only built-in SQL text is fixed (sort order, placeholder lists). | review |
| **HTML injection in ticket transcripts** | Message text is escaped before it is written to the HTML transcript. | `test_modules.py` |
| **Browsing your files from Discord** | `/library` (folder picker) is limited to the bot **owner**, including its folder suggestions as you type. Whole drives and your entire user folder are refused. The bot only reads media folders and never writes, moves or deletes your files. | `test_modules.py` |
| **Watch party: guessing links** | Each party gets a random 128-bit link. The server has no index page, lists no folders, serves only that party's one video, and answers everything else with 404. | `test_watch.py` |
| **Watch party: scripts in the page** | The page has a strict Content-Security-Policy, no third-party scripts, `X-Frame-Options: DENY` and `Referrer-Policy: no-referrer` (the link is not leaked to other sites). Video titles are HTML-escaped. | `test_watch.py` |
| **Watch party: abusing the sync connection** | Messages are capped at 4 KB, limited to about 10 per second per viewer, and must be one of play/pause/seek/ping with a valid number. Viewers per party (30) and parties (3) are capped. Silent connections are closed. | `test_watch.py` |
| **Watch party: tying up your CPU** | Re-encoding a whole film is limited to staff (`watch.full_convert`), and only one conversion runs at a time. Cheap repackaging is allowed for everyone. | `test_watch.py` |
| **Watch party: an always-open port** | The web server only listens **while a party runs** and stops when the last one ends. | `test_watch.py` |
| **Flooding commands** | Cooldowns on heavy commands (clips, polls, watch parties). Caps on queue length, reminders per person (25) and clip length. | module code |
| **Spam and raids** | The anti-spam and anti-raid modules (they need the two privileged intents). | `test_modules.py` |

## Your part

- **Keep the token secret.** If it ever leaks: Developer Portal > Bot > **Reset Token**, paste the
  new one into `.env`. The old one stops working at once.
- **Give the bot only the permissions in the invite link.** Never give it Administrator.
- **Role order:** the bot can only manage roles and members below its own role.
- **Watch parties share a video with anyone who has the link** while the party runs. Post links
  only where you want them seen. With `watch.tunnel = "cloudflared"` the link works from the whole
  internet; without it, only your local network.
- **Keep ffmpeg, yt-dlp and Python up to date.** They read media files, including downloads if
  YouTube is on.
- `voice_debug = true` writes short-lived voice keys to the log. Switch it off when you're done
  and don't share that log.
- Ticket transcripts and the database in `data/` contain your members' messages and IDs. Treat
  that folder as private.
