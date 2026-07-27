"""Settings storage for AutoMod.

The bot normally uses the repo database. A JSON fallback is included so this
package can still be dropped into a simple discord.py project.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import default_settings, merged_settings


class AutoModStorage:
    def __init__(self, bot: object, path: str | Path = "data/automod_settings.json") -> None:
        self.bot = bot
        self.path = Path(path)

    async def get_settings(self, guild_id: int) -> dict[str, Any]:
        db = getattr(self.bot, "db", None)
        if db is not None and hasattr(db, "get_settings"):
            return merged_settings(await db.get_settings(int(guild_id)))
        payload = self._read_file()
        return merged_settings(payload.get(str(guild_id), {}))

    async def update_settings(self, guild_id: int, changes: dict[str, Any]) -> dict[str, Any]:
        guild_id = int(guild_id)
        current = await self.get_settings(guild_id)
        current.update(changes)
        db = getattr(self.bot, "db", None)
        if db is not None and hasattr(db, "update_settings"):
            await db.update_settings(guild_id, current)
            return await self.get_settings(guild_id)
        payload = self._read_file()
        payload[str(guild_id)] = current
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return current

    async def reset_settings(self, guild_id: int) -> dict[str, Any]:
        defaults = default_settings()
        db = getattr(self.bot, "db", None)
        if db is not None and hasattr(db, "update_settings"):
            await db.update_settings(int(guild_id), defaults)
            return await self.get_settings(int(guild_id))
        payload = self._read_file()
        payload[str(int(guild_id))] = defaults
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return defaults

    def _read_file(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}
