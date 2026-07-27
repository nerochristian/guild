import logging
from datetime import datetime, timezone
from typing import Any, Optional

import discord
import psycopg2

from components_v2 import branded_panel_container

LOGGER = logging.getLogger("enzo-bot.safety")
SUPER_USER_IDS = {1269772767516033025}


class BotSafetyStore:
    def __init__(self, db_url: str) -> None:
        if not db_url:
            raise RuntimeError(
                "DATABASE_URL is required; configure it before starting the bot."
            )
        self.db_url = db_url

    def _connect(self):
        return psycopg2.connect(self.db_url)

    def initialize(self) -> None:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS bot_safety_state (
                            guild_id BIGINT,
                            module VARCHAR(50) DEFAULT 'global',
                            enabled BOOLEAN NOT NULL DEFAULT TRUE,
                            updated_by BIGINT,
                            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            reason TEXT,
                            PRIMARY KEY (guild_id, module)
                        )
                        """
                    )
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS blacklisted_users (
                            user_id BIGINT PRIMARY KEY,
                            added_by BIGINT,
                            reason TEXT,
                            added_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                        )
                        """
                    )
        finally:
            conn.close()
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "ALTER TABLE bot_safety_state ADD COLUMN IF NOT EXISTS module VARCHAR(50) NOT NULL DEFAULT 'global'"
                    )
                    cursor.execute(
                        """
                        DO $$
                        BEGIN
                            IF NOT EXISTS (
                                SELECT 1
                                FROM information_schema.key_column_usage
                                WHERE table_name = 'bot_safety_state'
                                  AND constraint_name = 'bot_safety_state_pkey'
                                  AND column_name = 'module'
                            ) THEN
                                ALTER TABLE bot_safety_state DROP CONSTRAINT IF EXISTS bot_safety_state_pkey CASCADE;
                                ALTER TABLE bot_safety_state ADD PRIMARY KEY (guild_id, module);
                            END IF;
                        END $$;
                        """
                    )
        finally:
            conn.close()

    def is_enabled(self, guild_id: int, module: str = "global") -> bool:
        conn = self._connect()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT enabled FROM bot_safety_state WHERE guild_id = %s AND module = %s",
                    (guild_id, module),
                )
                row = cursor.fetchone()
            if row is not None:
                return bool(row[0])
            # If not configured for the module, default to the global setting if checking a specific module
            if module != "global":
                return self.is_enabled(guild_id, "global")
            return True
        finally:
            conn.close()

    def set_enabled(
        self,
        guild_id: int,
        enabled: bool,
        updated_by: int,
        reason: str,
        module: str = "global",
    ) -> None:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO bot_safety_state (guild_id, module, enabled, updated_by, updated_at, reason)
                        VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP, %s)
                        ON CONFLICT (guild_id, module) DO UPDATE SET
                            enabled = EXCLUDED.enabled,
                            updated_by = EXCLUDED.updated_by,
                            updated_at = EXCLUDED.updated_at,
                            reason = EXCLUDED.reason
                        """,
                        (guild_id, module, enabled, updated_by, reason),
                    )
        finally:
            conn.close()

    def get_status(self, guild_id: int, module: str = "global") -> dict[str, object]:
        conn = self._connect()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT enabled, updated_by, updated_at, reason FROM bot_safety_state WHERE guild_id = %s AND module = %s",
                    (guild_id, module),
                )
                row = cursor.fetchone()
            if row is None:
                return {
                    "enabled": True,
                    "updated_by": None,
                    "updated_at": None,
                    "reason": "Default on",
                }
            updated_at = row[2]
            if isinstance(updated_at, datetime) and updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
            return {
                "enabled": bool(row[0]),
                "updated_by": row[1],
                "updated_at": updated_at,
                "reason": row[3],
            }
        finally:
            conn.close()

    def get_blacklisted_users(self) -> set[int]:
        conn = self._connect()
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT user_id FROM blacklisted_users")
                return {row[0] for row in cursor.fetchall()}
        finally:
            conn.close()

    def add_blacklist(self, user_id: int, added_by: int, reason: str = None) -> None:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO blacklisted_users (user_id, added_by, reason)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (user_id) DO UPDATE SET 
                            added_by = EXCLUDED.added_by,
                            reason = EXCLUDED.reason
                        """,
                        (user_id, added_by, reason),
                    )
        finally:
            conn.close()

    def remove_blacklist(self, user_id: int) -> bool:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute("DELETE FROM blacklisted_users WHERE user_id = %s", (user_id,))
                    return cursor.rowcount > 0
        finally:
            conn.close()
