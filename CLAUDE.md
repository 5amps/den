# Den: notes for AI assistants

- **Setting up or running the bot for the user:** follow [AI_SETUP.md](AI_SETUP.md) step by step.
  Never ask for, print or commit the bot token.
- **Changing code:** one module per file in `cogs/`, shared helpers in `core/`. Each module lists
  its gateway intents in `MODULE_INTENTS` in `bot.py` and its name in `KNOWN_MODULES` in
  `core/config.py`. Commands that post into a channel for someone must call
  `core.checks.user_may_post`. Staff-only commands need a server-side check (`staff_only()`,
  `has_permissions` or `owner_only()`), not only `default_permissions`.
- **Tests (no token needed):** `python tests/test_core.py`, `test_modules.py`, `test_music.py`,
  `test_watch.py`, `test_startup.py`. After changing commands run `python tools/gen_commands.py`
  to rebuild `COMMANDS.md`.
- Never commit `.env`, `config.toml` or `data/`.
