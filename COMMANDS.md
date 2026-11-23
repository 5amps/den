# Command reference

All 79 slash commands, generated from the code by `tools/gen_commands.py`.
In Discord, `/help` shows the same list for the modules that are switched on.

- `<name>` is required, `[name]` is optional.
- **Who** is the permission Discord asks for by default. Server admins can change it under
  Server Settings > Integrations.
- A command only exists while its module is enabled in `config.toml`.

## New here? Start with these

| I want to... | Do this |
|---|---|
| See what the bot can do | `/help` |
| Play a song | Join a voice channel, type `/play`, start typing the title, pick it |
| See or change what is playing | `/nowplaying`, `/queue`, `/skip`, `/pause`, `/stop` |
| Answer an event | `/event list`, then press Going / Maybe / Can't go on the event |
| Talk to staff privately | Press the button on the ticket panel |
| Check the bot is alive | `/ping` |

## First-time setup for server admins

| Step | Command |
|---|---|
| 1. Choose where logs go | `/config log-channel #mod-log` |
| 2. Say who is staff | `/config staff-role @Moderator` |
| 3. Create a ticket type and post the panel | `/ticket category-add Support @Moderator`, then `/ticket panel` |
| 4. Greet new members | `/welcome set #welcome` |
| 5. Optional verify button | `/antiraid verify-setup @Member` |
| 6. Check everything | `/status`, `/antispam status`, `/antiraid status` |

## ⚙️ Core

Health checks, help and server settings. Module name in `config.toml`: `system` (always on).

**Start here:** `/help` shows everything. `/ping` checks the bot is alive.

| Command | What it does | Who |
|---|---|---|
| `/config log-channel [channel]` | Choose where moderation, raid and ticket logs are posted. | Needs Manage Server |
| `/config show` | Show this server's bot settings. | Needs Manage Server |
| `/config staff-role <role>` | Add or remove a role that counts as staff for bot commands. | Needs Manage Server |
| `/help [module]` | How to use the bot: quick start and every command. | Everyone |
| `/ping` | Check that the bot is alive and how fast it responds. | Everyone |
| `/restart` | Owner: restart the bot (the start script brings it back). | Needs Administrator |
| `/shutdown` | Owner: stop the bot until it is started again by hand. | Needs Administrator |
| `/status` | Show RAM, CPU and GPU use, uptime and which modules are running. | Needs Manage Server |
| `/sync` | Owner: re-upload slash commands to Discord. | Needs Administrator |

<details><summary>Options explained</summary>

**`/config log-channel`**
- `channel` (optional): Leave empty to switch logging off

**`/help`**
- `module` (optional): Pick a module to see each of its commands with options

</details>

## 🎵 Music

Play your own music files in a voice channel. Module name in `config.toml`: `music`.

**Start here:** Join a voice channel, type `/play`, start typing a song title and pick one. `/queue` shows what is next, `/skip` moves on, `/stop` ends it.

| Command | What it does | Who |
|---|---|---|
| `/clear` | Remove all upcoming tracks but keep the current one playing. | Everyone |
| `/leave` | Disconnect the bot from voice. | Everyone |
| `/loop <mode>` | Repeat the current track, the whole queue, or nothing. | Everyone |
| `/nowplaying` | Show what is playing right now. | Everyone |
| `/pause` | Pause the music. | Everyone |
| `/play <query>` | Play a track from the local music library. | Everyone |
| `/playall [search] [shuffle]` | Queue every library track matching some text (or the whole library). | Everyone |
| `/playlist delete <name>` | Delete one of your playlists. | Everyone |
| `/playlist list` | List this server's saved playlists. | Everyone |
| `/playlist load <name> [shuffle]` | Add a saved playlist to the queue. | Everyone |
| `/playlist save <name>` | Save the current track and queue as a playlist. | Everyone |
| `/queue [page]` | Show the upcoming tracks. | Everyone |
| `/remove <number>` | Remove one track from the queue by its number. | Everyone |
| `/rescan_library` | Staff: re-scan the music folders for new or removed files. | Needs Manage Server |
| `/resume` | Resume paused music. | Everyone |
| `/seek <position>` | Jump to a position in the current track. | Everyone |
| `/shuffle` | Shuffle the upcoming tracks. | Everyone |
| `/skip` | Skip the current track. | Everyone |
| `/stop` | Stop the music and clear the queue. | Everyone |
| `/volume <percent>` | Set the volume (100 = original, uses the least CPU). | Everyone |
| `/youtube <query>` | Play a YouTube link or the first search result. | Everyone |

<details><summary>Options explained</summary>

**`/play`**
- `query`: Start typing a title, then pick from the list

**`/playall`**
- `search` (optional): Part of a title or folder name; leave empty for everything
- `shuffle` (optional): Add them in random order

**`/playlist save`**
- `name`: Playlist name (an existing playlist of yours is replaced)

**`/queue`**
- `page` (optional): Page number (10 tracks per page)

**`/remove`**
- `number`: Position shown by /queue

**`/seek`**
- `position`: For example 90, 1:30 or 1:02:03

**`/volume`**
- `percent`: 0 to 200

**`/youtube`**
- `query`: A YouTube link, or words to search for

</details>

## 🎬 Video

Audio from local video files, and short clips. Module name in `config.toml`: `video`.

**Start here:** `/video play` works like `/play` but for video files (sound only).

| Command | What it does | Who |
|---|---|---|
| `/video clip <file> [start] [seconds]` | Post a short clip of a local video file in this channel. | Everyone |
| `/video play <file>` | Play the audio of a local video file in your voice channel. | Everyone |
| `/video rescan` | Staff: re-scan the video folders. | Everyone |

<details><summary>Options explained</summary>

**`/video clip`**
- `file`: Start typing a file name
- `start` (optional): Where the clip begins, e.g. 1:30
- `seconds` (optional): Clip length in seconds

**`/video play`**
- `file`: Start typing a file name, then pick from the list

</details>

## 📂 Media folders

Choose which folders on the bot's PC hold music and videos. Module name in `config.toml`: `library`.

**Start here:** Bot owner: `/library setup` opens a panel; pick a folder from the list or type a path.

| Command | What it does | Who |
|---|---|---|
| `/library add <type> <folder>` | Owner: add a music or video folder. | Needs Administrator |
| `/library remove <type> <folder>` | Owner: stop using a music or video folder. | Needs Administrator |
| `/library setup` | Owner: pick the music and video folders from a list. | Needs Administrator |

<details><summary>Options explained</summary>

**`/library add`**
- `type`: Music or video
- `folder`: Start typing a path; pick from the suggestions

**`/library remove`**
- `type`: Music or video
- `folder`: Pick the folder to remove

</details>

## 🍿 Watch parties

Watch a local video together in the browser, in sync. Module name in `config.toml`: `watch`.

**Start here:** `/watch start`, pick a video, open the link and press Join. Pause and seek are shared.

| Command | What it does | Who |
|---|---|---|
| `/watch end <party>` | End a watch party (yours, or any if you are staff). | Everyone |
| `/watch start <file>` | Watch a local video together in the browser, in sync. | Everyone |

<details><summary>Options explained</summary>

**`/watch end`**
- `party`: Pick the watch party to end

**`/watch start`**
- `file`: Start typing a video name, then pick from the list

</details>

## 📅 Events

Events with Going / Maybe / Can't go buttons. Module name in `config.toml`: `rsvp`.

**Start here:** `/event list` shows what is coming up. Press a button on an event to answer.

| Command | What it does | Who |
|---|---|---|
| `/event attendees <event_id>` | Show everyone's answer for an event. | Everyone |
| `/event cancel <event_id>` | Close an event early. Its buttons stop working. | Everyone |
| `/event create <title> <when> [description] [capacity] [reminder_minutes]` | Create an event with Going / Maybe / Can't go buttons. | Everyone |
| `/event list` | List upcoming events on this server. | Everyone |

<details><summary>Options explained</summary>

**`/event cancel`**
- `event_id`: The number in the event's footer

**`/event create`**
- `title`: Event name
- `when`: 2026-12-24 18:30, 18:30, or 'in 2h 30m' (the bot PC's time zone)
- `description` (optional): Details
- `capacity` (optional): Maximum people going (0 = unlimited)
- `reminder_minutes` (optional): Ping people who are going this many minutes before (0 = no reminder)

</details>

## 🎫 Tickets

Private help channels with staff. Module name in `config.toml`: `tickets`.

**Start here:** Press the button on the ticket panel. Use `/ticket close` when you are done.

| Command | What it does | Who |
|---|---|---|
| `/ticket add <member>` | Staff: let another member see this ticket. | Everyone |
| `/ticket category-add <name> <staff_role> [channel_category] [intro]` | Admin: create or update a ticket type. | Everyone |
| `/ticket category-remove <name>` | Admin: delete a ticket type. | Everyone |
| `/ticket claim` | Staff: take responsibility for this ticket. | Everyone |
| `/ticket close [reason]` | Close this ticket and save its transcript. | Everyone |
| `/ticket panel [channel] [title] [text]` | Admin: post the message with the ticket buttons. | Everyone |
| `/ticket remove <member>` | Staff: remove a member from this ticket. | Everyone |

<details><summary>Options explained</summary>

**`/ticket category-add`**
- `name`: Shown on the button, e.g. Support
- `staff_role`: Role that can see and handle these tickets
- `channel_category` (optional): Discord category the ticket channels are created in
- `intro` (optional): First message shown inside a new ticket

**`/ticket panel`**
- `channel` (optional): Where to post it (default: here)

</details>

## ⏰ Reminders

The bot DMs you when it is time. Module name in `config.toml`: `reminders`.

**Start here:** `/remind when: in 2h what: stretch` · `/reminders list` shows yours.

| Command | What it does | Who |
|---|---|---|
| `/remind <when> <what> [repeat]` | Remind you about something later, by DM. | Everyone |
| `/reminders cancel <reminder>` | Cancel one of your reminders. | Everyone |
| `/reminders list` | Show your upcoming reminders. | Everyone |

<details><summary>Options explained</summary>

**`/remind`**
- `when`: e.g. in 2h 30m, 45m, 18:30, tomorrow 9:00, 2026-12-24 18:30
- `what`: What to remind you about
- `repeat` (optional): Repeat every day or week

**`/reminders cancel`**
- `reminder`: Pick the reminder to cancel

</details>

## 📊 Polls

Discord's own polls: live bars, results posted automatically. Module name in `config.toml`: `polls`.

**Start here:** `/poll create` with options like `Pizza | Tacos | Sushi`.

| Command | What it does | Who |
|---|---|---|
| `/poll create <question> <options> [hours] [multiple]` | Start a poll in this channel. Discord counts the votes. | Everyone |
| `/poll end <poll>` | End a poll now and show the result (your own polls, or any if staff). | Everyone |

<details><summary>Options explained</summary>

**`/poll create`**
- `question`: The question
- `options`: 2 to 10 options separated by |, e.g. Pizza | Tacos
- `hours` (optional): How long it runs, 1 to 768 hours (default 24)
- `multiple` (optional): Allow picking more than one option

**`/poll end`**
- `poll`: Pick the poll to end

</details>

## 🔨 Moderation

Warnings, timeouts, kicks, bans and clean-up. Module name in `config.toml`: `moderation`.

**Start here:** Staff only. `/warn`, `/timeout`, `/purge`; `/history` shows a member's record.

| Command | What it does | Who |
|---|---|---|
| `/ban <user> [reason] [delete_hours]` | Ban a user. Works on people who already left, by ID. | Needs Ban Members |
| `/clearwarnings <user>` | Clear a member's active warnings (the history is kept). | Needs Moderate Members |
| `/history <user>` | Show a member's warnings and past moderation actions. | Needs Moderate Members |
| `/kick <member> [reason]` | Kick a member from the server. | Needs Kick Members |
| `/purge <amount> [member] [contains]` | Delete recent messages in this channel. | Needs Manage Messages |
| `/slowmode <seconds>` | Set slow mode for this channel. | Needs Manage Channels |
| `/timeout <member> <duration> [reason]` | Temporarily stop a member from talking. | Needs Moderate Members |
| `/unban <user_id> [reason]` | Unban a user by their ID. | Needs Ban Members |
| `/untimeout <member>` | Remove a member's timeout. | Needs Moderate Members |
| `/warn <member> <reason>` | Warn a member. The warning is saved and they are told by DM. | Needs Moderate Members |

<details><summary>Options explained</summary>

**`/ban`**
- `delete_hours` (optional): Also delete their messages from the last N hours (0-168)

**`/purge`**
- `amount`: How many recent messages to look through (max 500)
- `member` (optional): Only delete messages from this member
- `contains` (optional): Only delete messages containing this text

**`/slowmode`**
- `seconds`: Seconds between messages per person (0 = off, max 21600)

**`/timeout`**
- `duration`: For example 10m, 2h or 1d 12h (max 28d)

**`/unban`**
- `user_id`: Right-click the user > Copy User ID, or take it from /history or the log

</details>

## 🛡️ Anti-spam

Automatic spam filtering. Module name in `config.toml`: `antispam`.

**Start here:** Works by itself. Staff: `/antispam status` shows the limits and recent actions.

| Command | What it does | Who |
|---|---|---|
| `/antispam ignore-channel <channel>` | Toggle a channel where anti-spam is off. | Needs Manage Server |
| `/antispam ignore-role <role>` | Toggle a role that anti-spam never touches. | Needs Manage Server |
| `/antispam status` | Show the active limits and recent actions. | Needs Manage Server |

## 🚨 Anti-raid

Raid detection and lockdown. Module name in `config.toml`: `antiraid`.

**Start here:** Works by itself. Staff: `/panic` locks the server at once, `/unlock` lifts it.

| Command | What it does | Who |
|---|---|---|
| `/antiraid status` | Show limits, lockdown state and the recent raid log. | Needs Manage Server |
| `/antiraid verify-off` | Stop the Verify button from giving a role. | Needs Manage Server |
| `/antiraid verify-setup <role> [channel] [text]` | Post a Verify button that gives members a role. | Needs Manage Server |
| `/panic [reason]` | PANIC: lock the server now. Stays locked until /unlock. | Needs Manage Server |
| `/unlock` | Lift the lockdown and restore normal permissions. | Needs Manage Server |

<details><summary>Options explained</summary>

**`/antiraid verify-setup`**
- `role`: Role given on verification
- `channel` (optional): Where to post the button
- `text` (optional): Message shown above the button

</details>

## 🧰 Utility

Welcome messages, auto-role and reaction roles. Module name in `config.toml`: `utility`.

**Start here:** Staff: `/welcome set`, `/autorole`, `/reactionrole add`.

| Command | What it does | Who |
|---|---|---|
| `/autorole [role]` | Give every new member a role automatically. No role = switch off. | Needs Manage Roles |
| `/reactionrole add <message_link> <emoji> <role>` | Reacting with an emoji on a message gives a role. | Needs Manage Roles |
| `/reactionrole list` | List this server's reaction roles. | Needs Manage Roles |
| `/reactionrole remove <message_link> <emoji>` | Stop an emoji on a message from giving a role. | Needs Manage Roles |
| `/welcome leave <channel> [text]` | Announce when a member leaves. | Needs Manage Server |
| `/welcome off` | Switch off welcome and leave messages. | Needs Manage Server |
| `/welcome set <channel> [text]` | Greet new members in a channel. | Needs Manage Server |

<details><summary>Options explained</summary>

**`/reactionrole add`**
- `message_link`: Right-click the message > Copy Message Link
- `emoji`: The emoji to react with

**`/welcome leave`**
- `channel`: Where to post
- `text` (optional): Use {name} {server} {count}. Empty = default text

**`/welcome set`**
- `channel`: Where to post
- `text` (optional): Use {user} {name} {server} {count}. Empty = default text

</details>
