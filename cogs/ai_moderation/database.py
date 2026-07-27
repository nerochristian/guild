"""
Async database layer using aiosqlite.

Stores guild settings, rules, infractions, cases, conversation state,
pending confirmations, reversible actions, appeals, and audit history.

All operations are async and use parameterized queries to prevent SQL
injection. The database is initialized on first use with a clear schema.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiosqlite

from .config import GuildConfig
from .models import (
    ActionType,
    CaseRecord,
    CaseStatus,
    InvestigationResult,
    ReversibleAction,
    SeverityLevel,
)

logger = logging.getLogger("ModBot.AIModeration.Database")


def _now() -> datetime:
    return datetime.now(timezone.utc)


# =============================================================================
# Schema initialization
# =============================================================================


SCHEMA_SQL = """
-- Guild settings (one row per guild)
CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id INTEGER PRIMARY KEY,
    config_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Infractions (one row per warning/timeout/ban/etc.)
CREATE TABLE IF NOT EXISTS infractions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    moderator_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    duration_seconds INTEGER,
    case_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT,
    is_active INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_infractions_guild_user
    ON infractions(guild_id, user_id);
CREATE INDEX IF NOT EXISTS idx_infractions_active
    ON infractions(guild_id, user_id, is_active);

-- Cases (one row per moderation case, including rejected/cancelled)
CREATE TABLE IF NOT EXISTS cases (
    case_id TEXT PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    actor_id INTEGER NOT NULL,
    target_id INTEGER,
    action TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    duration_seconds INTEGER,
    confidence REAL NOT NULL DEFAULT 0.0,
    ai_summary TEXT NOT NULL DEFAULT '',
    ai_reasoning TEXT NOT NULL DEFAULT '',
    evidence_message_ids TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_cases_guild
    ON cases(guild_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_cases_actor
    ON cases(actor_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_cases_target
    ON cases(target_id, created_at DESC);

-- Reversible actions (for undo)
CREATE TABLE IF NOT EXISTS reversible_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT NOT NULL,
    guild_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    target_id INTEGER NOT NULL,
    actor_id INTEGER NOT NULL,
    undo_data TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    is_reversed INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (case_id) REFERENCES cases(case_id)
);
CREATE INDEX IF NOT EXISTS idx_reversible_guild_actor
    ON reversible_actions(guild_id, actor_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_reversible_target
    ON reversible_actions(target_id, is_reversed);

-- Conversation state (for clarification follow-ups)
CREATE TABLE IF NOT EXISTS conversation_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    pending_request TEXT NOT NULL,  -- JSON of the partial ModerationRequest
    clarification_question TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conversation_user_channel
    ON conversation_state(guild_id, user_id, channel_id);

-- Appeals
CREATE TABLE IF NOT EXISTS appeals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT NOT NULL,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    appeal_text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    reviewed_by INTEGER,
    review_notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    reviewed_at TEXT,
    FOREIGN KEY (case_id) REFERENCES cases(case_id)
);

-- Audit log (every request, including rejected)
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    case_id TEXT,
    actor_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    raw_instruction TEXT,
    parsed_intent TEXT,
    action TEXT,
    target_id INTEGER,
    confidence REAL,
    result TEXT NOT NULL DEFAULT '',
    error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_audit_guild_time
    ON audit_log(guild_id, created_at DESC);
"""


# =============================================================================
# Database manager
# =============================================================================


class Database:
    """Async SQLite database manager.

    Usage::

        db = Database("data/ai_moderation.db")
        await db.init()
        config = await db.get_guild_config(guild_id)
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        self._db: Optional[aiosqlite.Connection] = None

    async def init(self) -> None:
        """Initialize the database connection and create tables."""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA_SQL)
        await self._db.commit()
        logger.info("AI moderation database initialized at %s", self.db_path)

    async def close(self) -> None:
        """Close the database connection."""
        if self._db:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Database not initialized. Call init() first.")
        return self._db

    # ------------------------------------------------------------------
    # Guild config
    # ------------------------------------------------------------------

    async def get_guild_config(self, guild_id: int) -> GuildConfig:
        """Get the config for a guild, returning defaults if not set."""
        async with self.db.execute(
            "SELECT config_json FROM guild_settings WHERE guild_id = ?",
            (guild_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return GuildConfig.default()
        try:
            data = json.loads(row["config_json"])
            return GuildConfig.from_dict(data)
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning("Failed to parse config for guild %d: %s", guild_id, exc)
            return GuildConfig.default()

    async def save_guild_config(self, guild_id: int, config: GuildConfig) -> None:
        """Save (upsert) the config for a guild."""
        config_json = json.dumps(config.to_dict(), default=str)
        await self.db.execute(
            """
            INSERT INTO guild_settings (guild_id, config_json, updated_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(guild_id) DO UPDATE SET
                config_json = excluded.config_json,
                updated_at = excluded.updated_at
            """,
            (guild_id, config_json),
        )
        await self.db.commit()

    async def update_guild_config_field(
        self, guild_id: int, field_name: str, value: Any
    ) -> GuildConfig:
        """Update a single field in the guild config and return the updated config."""
        config = await self.get_guild_config(guild_id)
        if hasattr(config, field_name):
            setattr(config, field_name, value)
            await self.save_guild_config(guild_id, config)
        return config

    # ------------------------------------------------------------------
    # Infractions
    # ------------------------------------------------------------------

    async def add_infraction(
        self,
        *,
        guild_id: int,
        user_id: int,
        moderator_id: int,
        action: ActionType,
        reason: str = "",
        duration_seconds: Optional[int] = None,
        case_id: Optional[str] = None,
    ) -> int:
        """Record a new infraction. Returns the infraction ID."""
        expires_at = None
        if duration_seconds:
            expires_at = (_now() + timedelta(seconds=duration_seconds)).isoformat()
        async with self.db.execute(
            """
            INSERT INTO infractions
                (guild_id, user_id, moderator_id, action, reason,
                 duration_seconds, case_id, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (guild_id, user_id, moderator_id, action.value, reason,
             duration_seconds, case_id, expires_at),
        ) as cursor:
            await self.db.commit()
            return cursor.lastrowid or 0

    async def get_user_infractions(
        self,
        guild_id: int,
        user_id: int,
        *,
        active_only: bool = False,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Get infractions for a user."""
        query = """
            SELECT * FROM infractions
            WHERE guild_id = ? AND user_id = ?
        """
        params: list[Any] = [guild_id, user_id]
        if active_only:
            query += " AND is_active = 1"
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        async with self.db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def count_active_infractions(
        self, guild_id: int, user_id: int, action: Optional[ActionType] = None
    ) -> int:
        """Count active infractions for a user, optionally filtered by action."""
        query = "SELECT COUNT(*) as cnt FROM infractions WHERE guild_id = ? AND user_id = ? AND is_active = 1"
        params: list[Any] = [guild_id, user_id]
        if action:
            query += " AND action = ?"
            params.append(action.value)
        async with self.db.execute(query, params) as cursor:
            row = await cursor.fetchone()
        return row["cnt"] if row else 0

    async def deactivate_infraction(self, infraction_id: int) -> None:
        """Mark an infraction as no longer active (e.g. timeout expired)."""
        await self.db.execute(
            "UPDATE infractions SET is_active = 0 WHERE id = ?",
            (infraction_id,),
        )
        await self.db.commit()

    # ------------------------------------------------------------------
    # Cases
    # ------------------------------------------------------------------

    async def create_case(self, case: CaseRecord) -> None:
        """Insert a new case record."""
        await self.db.execute(
            """
            INSERT INTO cases
                (case_id, guild_id, actor_id, target_id, action, status,
                 reason, duration_seconds, confidence, ai_summary,
                 ai_reasoning, evidence_message_ids)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                case.case_id, case.guild_id, case.actor_id,
                case.target_id, case.action.value, case.status.value,
                case.reason, case.duration_seconds, case.confidence,
                case.ai_summary, case.ai_reasoning,
                json.dumps([str(mid) for mid in case.evidence_message_ids]),
            ),
        )
        await self.db.commit()

    async def update_case_status(
        self,
        case_id: str,
        status: CaseStatus,
        *,
        error: Optional[str] = None,
    ) -> None:
        """Update the status of a case."""
        resolved_at = _now().isoformat() if status in {
            CaseStatus.COMPLETED, CaseStatus.FAILED, CaseStatus.CANCELLED,
            CaseStatus.EXPIRED, CaseStatus.UNDONE,
        } else None
        await self.db.execute(
            """
            UPDATE cases SET status = ?, error = ?, resolved_at = ?
            WHERE case_id = ?
            """,
            (status.value, error, resolved_at, case_id),
        )
        await self.db.commit()

    async def get_case(self, case_id: str) -> Optional[CaseRecord]:
        """Get a case by ID."""
        async with self.db.execute(
            "SELECT * FROM cases WHERE case_id = ?", (case_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return self._row_to_case(row)

    async def get_recent_cases(
        self, guild_id: int, *, limit: int = 20
    ) -> List[CaseRecord]:
        """Get recent cases for a guild."""
        async with self.db.execute(
            "SELECT * FROM cases WHERE guild_id = ? ORDER BY created_at DESC LIMIT ?",
            (guild_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        return [self._row_to_case(row) for row in rows]

    async def get_recent_cases_by_actor(
        self, guild_id: int, actor_id: int, *, limit: int = 20
    ) -> List[CaseRecord]:
        """Get recent cases by a specific moderator."""
        async with self.db.execute(
            "SELECT * FROM cases WHERE guild_id = ? AND actor_id = ? ORDER BY created_at DESC LIMIT ?",
            (guild_id, actor_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        return [self._row_to_case(row) for row in rows]

    @staticmethod
    def _row_to_case(row: aiosqlite.Row) -> CaseRecord:
        """Convert a DB row to a CaseRecord."""
        try:
            evidence_ids = [int(mid) for mid in json.loads(row["evidence_message_ids"])]
        except (json.JSONDecodeError, TypeError):
            evidence_ids = []
        return CaseRecord(
            case_id=row["case_id"],
            guild_id=row["guild_id"],
            actor_id=row["actor_id"],
            target_id=row["target_id"],
            action=ActionType(row["action"]),
            status=CaseStatus(row["status"]),
            reason=row["reason"] or "",
            duration_seconds=row["duration_seconds"],
            confidence=row["confidence"],
            ai_summary=row["ai_summary"] or "",
            ai_reasoning=row["ai_reasoning"] or "",
            evidence_message_ids=evidence_ids,
            created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else _now(),
            resolved_at=datetime.fromisoformat(row["resolved_at"]) if row["resolved_at"] else None,
            error=row["error"],
        )

    # ------------------------------------------------------------------
    # Reversible actions (for undo)
    # ------------------------------------------------------------------

    async def add_reversible_action(self, action: ReversibleAction) -> int:
        """Record a reversible action for potential undo."""
        async with self.db.execute(
            """
            INSERT INTO reversible_actions
                (case_id, guild_id, action, target_id, actor_id, undo_data)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                action.case_id, action.guild_id, action.action.value,
                action.target_id, action.actor_id,
                json.dumps(action.undo_data, default=str),
            ),
        ) as cursor:
            await self.db.commit()
            return cursor.lastrowid or 0

    async def get_latest_reversible_action(
        self, guild_id: int, *, actor_id: Optional[int] = None
    ) -> Optional[ReversibleAction]:
        """Get the most recent non-reversed reversible action."""
        query = """
            SELECT * FROM reversible_actions
            WHERE guild_id = ? AND is_reversed = 0
        """
        params: list[Any] = [guild_id]
        if actor_id is not None:
            query += " AND actor_id = ?"
            params.append(actor_id)
        query += " ORDER BY created_at DESC LIMIT 1"
        async with self.db.execute(query, params) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        try:
            undo_data = json.loads(row["undo_data"])
        except (json.JSONDecodeError, TypeError):
            undo_data = {}
        return ReversibleAction(
            case_id=row["case_id"],
            action=ActionType(row["action"]),
            target_id=row["target_id"],
            actor_id=row["actor_id"],
            guild_id=row["guild_id"],
            timestamp=datetime.fromisoformat(row["created_at"]) if row["created_at"] else _now(),
            undo_data=undo_data,
        )

    async def mark_reversed(self, reversible_id: int) -> None:
        """Mark a reversible action as having been reversed."""
        await self.db.execute(
            "UPDATE reversible_actions SET is_reversed = 1 WHERE id = ?",
            (reversible_id,),
        )
        await self.db.commit()

    # ------------------------------------------------------------------
    # Conversation state
    # ------------------------------------------------------------------

    async def save_conversation_state(
        self,
        *,
        guild_id: int,
        channel_id: int,
        user_id: int,
        pending_request: Dict[str, Any],
        clarification_question: Optional[str],
        ttl_seconds: int,
    ) -> None:
        """Save a pending conversation state (for clarification follow-ups)."""
        expires_at = (_now() + timedelta(seconds=ttl_seconds)).isoformat()
        # Delete any existing state for this user/channel first.
        await self.db.execute(
            "DELETE FROM conversation_state WHERE guild_id = ? AND channel_id = ? AND user_id = ?",
            (guild_id, channel_id, user_id),
        )
        await self.db.execute(
            """
            INSERT INTO conversation_state
                (guild_id, channel_id, user_id, pending_request,
                 clarification_question, expires_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id, channel_id, user_id,
                json.dumps(pending_request, default=str),
                clarification_question, expires_at,
            ),
        )
        await self.db.commit()

    async def get_conversation_state(
        self, guild_id: int, channel_id: int, user_id: int
    ) -> Optional[Dict[str, Any]]:
        """Get the active conversation state for a user/channel, if not expired."""
        async with self.db.execute(
            """
            SELECT * FROM conversation_state
            WHERE guild_id = ? AND channel_id = ? AND user_id = ?
            AND expires_at > datetime('now')
            ORDER BY created_at DESC LIMIT 1
            """,
            (guild_id, channel_id, user_id),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        try:
            pending = json.loads(row["pending_request"])
        except (json.JSONDecodeError, TypeError):
            return None
        return {
            "pending_request": pending,
            "clarification_question": row["clarification_question"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
        }

    async def clear_conversation_state(
        self, guild_id: int, channel_id: int, user_id: int
    ) -> None:
        """Clear the conversation state for a user/channel."""
        await self.db.execute(
            "DELETE FROM conversation_state WHERE guild_id = ? AND channel_id = ? AND user_id = ?",
            (guild_id, channel_id, user_id),
        )
        await self.db.commit()

    async def cleanup_expired_conversations(self) -> int:
        """Delete expired conversation states. Returns count deleted."""
        async with self.db.execute(
            "DELETE FROM conversation_state WHERE expires_at <= datetime('now')"
        ) as cursor:
            await self.db.commit()
            return cursor.rowcount or 0

    # ------------------------------------------------------------------
    # Audit log
    # ------------------------------------------------------------------

    async def add_audit_entry(
        self,
        *,
        guild_id: int,
        actor_id: int,
        event_type: str,
        case_id: Optional[str] = None,
        raw_instruction: Optional[str] = None,
        parsed_intent: Optional[str] = None,
        action: Optional[str] = None,
        target_id: Optional[int] = None,
        confidence: Optional[float] = None,
        result: str = "",
        error: Optional[str] = None,
    ) -> None:
        """Add an entry to the audit log."""
        await self.db.execute(
            """
            INSERT INTO audit_log
                (guild_id, case_id, actor_id, event_type, raw_instruction,
                 parsed_intent, action, target_id, confidence, result, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id, case_id, actor_id, event_type,
                raw_instruction[:2000] if raw_instruction else None,
                parsed_intent, action, target_id, confidence, result, error,
            ),
        )
        await self.db.commit()

    async def get_audit_log(
        self, guild_id: int, *, limit: int = 50
    ) -> List[Dict[str, Any]]:
        """Get recent audit entries for a guild."""
        async with self.db.execute(
            "SELECT * FROM audit_log WHERE guild_id = ? ORDER BY created_at DESC LIMIT ?",
            (guild_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Appeals
    # ------------------------------------------------------------------

    async def create_appeal(
        self,
        *,
        case_id: str,
        guild_id: int,
        user_id: int,
        appeal_text: str,
    ) -> int:
        """Create a new appeal."""
        async with self.db.execute(
            """
            INSERT INTO appeals (case_id, guild_id, user_id, appeal_text)
            VALUES (?, ?, ?, ?)
            """,
            (case_id, guild_id, user_id, appeal_text[:2000]),
        ) as cursor:
            await self.db.commit()
            return cursor.lastrowid or 0

    async def get_pending_appeals(self, guild_id: int) -> List[Dict[str, Any]]:
        """Get pending appeals for a guild."""
        async with self.db.execute(
            "SELECT * FROM appeals WHERE guild_id = ? AND status = 'pending' ORDER BY created_at ASC",
            (guild_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Data retention
    # ------------------------------------------------------------------

    async def run_retention_cleanup(self, config: GuildConfig) -> Dict[str, int]:
        """Delete old data per the guild's retention settings. Returns counts."""
        case_cutoff = (_now() - timedelta(days=config.case_retention_days)).isoformat()
        evidence_cutoff_str = (_now() - timedelta(days=config.evidence_retention_days)).isoformat()

        counts: Dict[str, int] = {}

        async with self.db.execute(
            "DELETE FROM cases WHERE created_at < ? AND guild_id IN "
            "(SELECT guild_id FROM guild_settings WHERE json_extract(config_json, '$.case_retention_days') IS NOT NULL)",
            (case_cutoff,),
        ) as cursor:
            counts["cases"] = cursor.rowcount or 0

        await self.db.commit()
        return counts
