# AutoMod Package

## Folder Structure

- `__init__.py` - extension loader and public exports.
- `commands.py` - main cog, listeners, and slash commands.
- `config.py` - default settings and example values.
- `engine.py` - rule evaluation, cooldowns, offense escalation, join tracking.
- `rules.py` - anti-spam, anti-link, anti-invite, mentions, caps, bad words, duplicates, fast messages, and new account checks.
- `punishments.py` - warn, timeout/mute, kick, and ban execution.
- `logging.py` - log embeds for deleted messages and punishments.
- `storage.py` - database-backed settings with JSON fallback.
- `panel.py` - `/automod status` panel helpers.
- `models.py` - shared dataclasses and enums.
- `utils.py` - parsing, normalization, domain matching, and permission helpers.
- `example_config.json` - copyable example settings.

## Dependencies

The repo already includes the required libraries:

- Python 3.11+
- `discord.py`
- `aiosqlite` when using the repo database
- `python-dotenv` for the existing bot config

## Loading

The main bot already loads:

```python
await bot.load_extension("cogs.automod")
```

For a fresh discord.py bot, add that line in `setup_hook`.

## Beginner Setup

1. Give the bot `Manage Messages`, `Moderate Members`, `Kick Members`, and `Ban Members`.
2. Put the bot role above members it should moderate.
3. Run `/automod setup log_channel:#your-log-channel`.
4. Run `/automod status`.
5. Add phrases with `/automod badwords add phrase:...`.
6. Whitelist staff roles or safe channels with `/automod whitelist add`.

## Customization

- Enable or disable modules with `/automod enable module:<name>` and `/automod disable module:<name>`.
- Change punishments with `/automod punishment set`.
- Change thresholds with `/automod thresholds set`.
- Use `/automod config key:<setting> value:<value>` for advanced settings.
- Repeated offenses escalate through `automod_escalation` in `config.py`.
