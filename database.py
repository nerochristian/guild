"""
Advanced Database Handler - Complete Schema Support
Supports ALL cogs with auto-migration and proper async patterns
Version: 3.3.0
"""

import discord
import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

import aiosqlite
import aiohttp
try:
    import asyncpg
except ImportError:
    asyncpg = None
try:
    from cogs.automod.config import AUTOMOD_SETTINGS
except ImportError:
    AUTOMOD_SETTINGS = {}

logger = logging.getLogger("ModBot.Database")

try:
    from utils.query_builder import QueryBuilder
except ImportError:
    QueryBuilder = None


_MODULE_ID_ALIASES = {
    "aimoderation": "aimod",
    "ai_moderation": "aimod",
    "ai_mod": "aimod",
    "automodv3": "automod",
    "auto_mod": "automod",
    "auto_moderation": "automod",
}

_FEATURE_MODULE_KEYS = {
    "automod": ("automod_enabled", True),
    "aimod": ("aimod_enabled", False),
}

_LOG_CHANNEL_MODULE_FIELDS = {
    "modChannel": ("mod_log_channel", "log_channel_mod"),
    "auditChannel": ("audit_log_channel", "log_channel_audit"),
    "messageChannel": ("message_log_channel", "log_channel_message"),
    "voiceChannel": ("voice_log_channel", "log_channel_voice"),
    "automodChannel": ("automod_log_channel", "log_channel_automod"),
    "reportChannel": ("report_log_channel", "log_channel_report"),
    "ticketChannel": ("ticket_log_channel", "log_channel_ticket"),
}


def _normalize_module_id(module_id: object) -> str:
    raw = str(module_id or "").strip().lower().replace("-", "_").replace(" ", "_")
    return _MODULE_ID_ALIASES.get(raw, raw)


def _coerce_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on", "enabled"}:
            return True
        if lowered in {"0", "false", "no", "off", "disabled"}:
            return False
    return default


def _canonical_modules(raw_modules: object) -> dict[str, Any]:
    if not isinstance(raw_modules, dict):
        return {}

    canonical: dict[str, Any] = {}
    for key, value in raw_modules.items():
        module_id = _normalize_module_id(key)
        if not isinstance(value, dict):
            continue
        existing = canonical.get(module_id, {})
        merged = dict(existing) if isinstance(existing, dict) else {}
        merged.update(value)
        if isinstance(existing, dict) and isinstance(existing.get("settings"), dict) and isinstance(value.get("settings"), dict):
            merged["settings"] = {**existing["settings"], **value["settings"]}
        canonical[module_id] = merged
    return canonical


def _ensure_module(modules: dict[str, Any], module_id: str) -> dict[str, Any]:
    module = modules.get(module_id)
    if not isinstance(module, dict):
        module = {}
        modules[module_id] = module
    return module


def _deep_merge_settings(base: Dict[str, Any], updates: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base or {})
    for key, value in (updates or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_settings(merged[key], value)
        else:
            merged[key] = value
    return merged


def _sync_logging_channel_settings(normalized: Dict[str, Any], modules: dict[str, Any]) -> None:
    logging_module = modules.get("logging")
    logging_settings = logging_module.get("settings") if isinstance(logging_module, dict) else None
    if not isinstance(logging_settings, dict):
        logging_settings = {}

    touched = False
    for module_key, (primary_key, alias_key) in _LOG_CHANNEL_MODULE_FIELDS.items():
        has_primary = primary_key in normalized
        has_alias = alias_key in normalized
        has_module = module_key in logging_settings

        flat_value = normalized.get(primary_key)
        if flat_value in (None, ""):
            flat_value = normalized.get(alias_key)

        if flat_value not in (None, ""):
            normalized[primary_key] = flat_value
            normalized[alias_key] = flat_value
            logging_settings[module_key] = flat_value
            touched = True
        elif has_primary or has_alias:
            normalized[primary_key] = None
            normalized[alias_key] = None
            logging_settings[module_key] = None
            touched = True
        elif has_module and logging_settings.get(module_key) not in (None, ""):
            normalized[primary_key] = logging_settings[module_key]
            normalized[alias_key] = logging_settings[module_key]
            touched = True
        elif has_module:
            normalized[primary_key] = None
            normalized[alias_key] = None
            touched = True

    if touched:
        module = _ensure_module(modules, "logging")
        module["settings"] = logging_settings


def normalize_runtime_settings(settings: Dict[str, Any]) -> Dict[str, Any]:
    """
    Keep flat runtime toggles and dashboard module blobs from fighting.

    Flat keys are what the cogs read on message/command events. When those keys
    exist they must win, otherwise a stale dashboard `modules` blob can turn a
    disabled feature back on after restart or a dashboard save.
    """
    normalized = dict(settings or {})
    modules = _canonical_modules(normalized.get("modules"))

    for module_id, (flat_key, default) in _FEATURE_MODULE_KEYS.items():
        module = modules.get(module_id)
        if flat_key in normalized:
            enabled = _coerce_bool(normalized.get(flat_key), default)
            normalized[flat_key] = enabled
            _ensure_module(modules, module_id)["enabled"] = enabled
        elif isinstance(module, dict) and "enabled" in module:
            normalized[flat_key] = _coerce_bool(module.get("enabled"), default)

    aimod = modules.get("aimod")
    aimod_settings = aimod.get("settings") if isinstance(aimod, dict) else None
    if "aimod_chat_enabled" in normalized:
        chat_enabled = _coerce_bool(normalized.get("aimod_chat_enabled"), False)
        normalized["aimod_chat_enabled"] = chat_enabled
        module = _ensure_module(modules, "aimod")
        module_settings = module.get("settings")
        if not isinstance(module_settings, dict):
            module_settings = {}
            module["settings"] = module_settings
        module_settings["chatEnabled"] = chat_enabled
    elif isinstance(aimod_settings, dict) and "chatEnabled" in aimod_settings:
        normalized["aimod_chat_enabled"] = _coerce_bool(aimod_settings.get("chatEnabled"), False)

    _sync_logging_channel_settings(normalized, modules)

    if modules:
        normalized["modules"] = modules
    return normalized



def _explicit_database_mode() -> Optional[str]:
    for env_key in ("DB_MODE", "DATABASE_MODE"):
        value = (os.getenv(env_key) or "").strip().lower()
        if value in {"sqlite", "postgres", "postgresql"}:
            return "postgres" if value in {"postgres", "postgresql"} else "sqlite"
    return None


def _is_postgres_url(database_url: str) -> bool:
    database_url = (database_url or "").strip().lower()
    return database_url.startswith("postgresql://") or database_url.startswith("postgres://")


def _normalize_postgres_url(database_url: str) -> str:
    database_url = (database_url or "").strip()
    if database_url.lower().startswith("postgres://"):
        return "postgresql://" + database_url[len("postgres://"):]
    return database_url


_POSTGRES_UPSERT_CONFLICT_COLUMNS: dict[str, tuple[str, ...]] = {
    "guild_settings": ("guild_id",),
    "ai_memory": ("user_id",),
    "guild_memory": ("guild_id",),
    "court_votes": ("session_id", "voter_id"),
    "giveaway_entries": ("giveaway_id", "user_id"),
    "blacklist": ("user_id",),
}

_POSTGRES_SERIAL_ID_TABLES = {
    "cases",
    "warnings",
    "mod_notes",
    "tempbans",
    "mod_stats",
    "reports",
    "tickets",
    "staff_sanctions",
    "court_sessions",
    "court_evidence",
    "court_votes",
    "modmail_threads",
    "modmail_messages",
    "modmail_blocks",
    "giveaways",
    "reaction_roles",
    "voice_roles",
    "quarantines",
    "dashboard_audit",
}


def _normalize_query_params(params: Any = None) -> tuple[Any, ...]:
    if params is None:
        return ()
    if isinstance(params, tuple):
        return params
    if isinstance(params, list):
        return tuple(params)
    return (params,)


def _convert_sqlite_placeholders(query: str) -> str:
    index = 0

    def repl(_: re.Match[str]) -> str:
        nonlocal index
        index += 1
        return f"${index}"

    return re.sub(r"\?", repl, query)


def _convert_sqlite_schema_sql(query: str) -> str:
    converted = query
    converted = re.sub(
        r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b",
        "BIGSERIAL PRIMARY KEY",
        converted,
        flags=re.IGNORECASE,
    )
    converted = re.sub(r"\bBOOLEAN\b", "BIGINT", converted, flags=re.IGNORECASE)
    converted = re.sub(r"\bINTEGER\b", "BIGINT", converted, flags=re.IGNORECASE)
    converted = re.sub(r"\bBLOB\b", "BYTEA", converted, flags=re.IGNORECASE)
    return converted


def _parse_insert_columns(columns: str) -> list[str]:
    return [column.strip().strip('"') for column in columns.split(",") if column.strip()]


def _convert_sqlite_insert_sql(query: str) -> tuple[str, bool]:
    stripped = query.strip()
    match = re.match(
        r"(?is)^INSERT\s+OR\s+(IGNORE|REPLACE)\s+INTO\s+([A-Za-z_][A-Za-z0-9_]*)\s*\((.*?)\)\s*VALUES\b",
        stripped,
    )
    if not match:
        plain_match = re.match(r"(?is)^INSERT\s+INTO\s+([A-Za-z_][A-Za-z0-9_]*)\b", stripped)
        if not plain_match:
            return query, False

        table_name = plain_match.group(1)
        if table_name in _POSTGRES_SERIAL_ID_TABLES and "RETURNING" not in stripped.upper():
            return stripped.rstrip().rstrip(";") + " RETURNING id", True
        return query, False

    mode = match.group(1).upper()
    table_name = match.group(2)
    columns = _parse_insert_columns(match.group(3))
    conflict_columns = _POSTGRES_UPSERT_CONFLICT_COLUMNS.get(table_name)

    converted = re.sub(
        r"(?is)^INSERT\s+OR\s+(IGNORE|REPLACE)\s+INTO\b",
        "INSERT INTO",
        stripped,
        count=1,
    ).rstrip().rstrip(";")

    if mode == "IGNORE":
        if conflict_columns:
            conflict_sql = ", ".join(conflict_columns)
            converted += f" ON CONFLICT ({conflict_sql}) DO NOTHING"
        else:
            converted += " ON CONFLICT DO NOTHING"
        return converted, False

    if not conflict_columns:
        raise ValueError(f"Missing conflict target for SQLite REPLACE on table '{table_name}'")

    update_columns = [column for column in columns if column not in conflict_columns]
    update_sql = ", ".join(f"{column} = EXCLUDED.{column}" for column in update_columns)
    conflict_sql = ", ".join(conflict_columns)
    converted += f" ON CONFLICT ({conflict_sql}) DO UPDATE SET {update_sql}"
    if table_name in _POSTGRES_SERIAL_ID_TABLES and "RETURNING" not in converted.upper():
        converted += " RETURNING id"
        return converted, True
    return converted, False




def _row_get(row: Any, column: str) -> Any:
    """Get a column value from a database row (dict or tuple).

    SQLite (aiosqlite) returns tuples, PostgreSQL (asyncpg via PostgresCompatCursor)
    returns dicts. This helper bridges both so call sites work either way.
    """
    if row is None:
        return None
    if isinstance(row, dict):
        return row.get(column)
    if isinstance(row, (tuple, list)):
        if len(row) == 1:
            return row[0]
        return row[0]
    try:
        return row[column]
    except (KeyError, IndexError, TypeError):
        try:
            return row[0]
        except (KeyError, IndexError, TypeError):
            return None


def _parse_status_rowcount(status: str) -> int:
    if not status:
        return 0
    parts = status.split()
    for part in reversed(parts):
        if part.isdigit():
            return int(part)
    return 0


class PostgresCompatCursor:
    def __init__(
        self,
        rows: Optional[list[Any]] = None,
        *,
        rowcount: int = 0,
        lastrowid: Optional[int] = None,
    ) -> None:
        # Convert asyncpg.Record objects to dicts for dict-like column access
        # asyncpg.Record supports both tuple and dict-like access, but converting
        # to dict ensures compatibility with code expecting Mapping protocol
        converted_rows = []
        for row in (rows or []):
            if hasattr(row, 'keys'):  # asyncpg.Record or similar dict-like object
                converted_rows.append(dict(row))
            elif isinstance(row, tuple):
                converted_rows.append(row)
            else:
                converted_rows.append(tuple(row))
        self._rows = converted_rows
        self._index = 0
        self.rowcount = rowcount
        self.lastrowid = lastrowid

    async def fetchone(self):
        if self._index >= len(self._rows):
            return None
        row = self._rows[self._index]
        self._index += 1
        return row

    async def fetchall(self):
        if self._index >= len(self._rows):
            return []
        rows = self._rows[self._index:]
        self._index = len(self._rows)
        return rows

    async def __aenter__(self) -> "PostgresCompatCursor":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class PostgresCompatConnection:
    def __init__(self, conn: "asyncpg.Connection") -> None:
        self._conn = conn
        self._transaction: Optional["asyncpg.Transaction"] = None

    async def _start_transaction(self) -> None:
        if self._transaction is not None:
            return
        self._transaction = self._conn.transaction()
        await self._transaction.start()

    async def _finish_transaction(self, *, commit: bool) -> None:
        if self._transaction is None:
            return
        try:
            if commit:
                await self._transaction.commit()
            else:
                await self._transaction.rollback()
        finally:
            self._transaction = None

    async def commit(self) -> None:
        await self._finish_transaction(commit=True)

    async def rollback(self) -> None:
        await self._finish_transaction(commit=False)

    async def execute(self, query: str, params: Any = None) -> PostgresCompatCursor:
        if asyncpg is None:
            raise RuntimeError("asyncpg is required for PostgreSQL mode")

        normalized_params = _normalize_query_params(params)
        stripped = (query or "").strip()
        upper = stripped.upper()

        if upper == "BEGIN":
            await self._start_transaction()
            return PostgresCompatCursor()

        if upper.startswith("PRAGMA "):
            pragma = upper[7:].strip()
            if pragma.startswith("PAGE_COUNT"):
                return PostgresCompatCursor([(0,)], rowcount=1)
            if pragma.startswith("PAGE_SIZE"):
                return PostgresCompatCursor([(8192,)], rowcount=1)
            return PostgresCompatCursor()

        if upper.startswith("VACUUM INTO"):
            return PostgresCompatCursor()

        if upper.startswith("CREATE TABLE") or upper.startswith("ALTER TABLE") or upper.startswith("CREATE INDEX"):
            stripped = _convert_sqlite_schema_sql(stripped)

        stripped, has_returning_id = _convert_sqlite_insert_sql(stripped)
        stripped = _convert_sqlite_placeholders(stripped)
        upper = stripped.upper()

        is_write = upper.startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP", "TRUNCATE"))
        if is_write and self._transaction is None:
            await self._start_transaction()

        try:
            if upper.startswith("SELECT") or " RETURNING " in upper:
                rows = await self._conn.fetch(stripped, *normalized_params)
                lastrowid = None
                if has_returning_id and rows:
                    lastrowid = rows[0][0]
                rowcount = len(rows)
                if has_returning_id and not rowcount:
                    rowcount = 0
                return PostgresCompatCursor(rows, rowcount=rowcount, lastrowid=lastrowid)

            status = await self._conn.execute(stripped, *normalized_params)
            return PostgresCompatCursor(rowcount=_parse_status_rowcount(status))
        except asyncpg.UniqueViolationError as exc:
            raise aiosqlite.IntegrityError(str(exc)) from exc
        except asyncpg.PostgresError as exc:
            raise aiosqlite.OperationalError(str(exc)) from exc

    async def close_pending_transaction(self) -> None:
        await self.rollback()


class SupabaseStorageMirror:
    """Mirror SQLite snapshots to Supabase Storage."""

    def __init__(self) -> None:
        self.url = (os.getenv("SUPABASE_URL") or "").strip().rstrip("/")
        self.key = (
            os.getenv("SUPABASE_SERVICE_ROLE_KEY")
            or os.getenv("SUPABASE_KEY")
            or os.getenv("SUPABASE_ANON_KEY")
            or ""
        ).strip()
        self.bucket = (os.getenv("SUPABASE_STORAGE_BUCKET") or "modbot-data").strip()
        self.object_path = (os.getenv("SUPABASE_STORAGE_DB_PATH") or "modbot/modbot.db").strip()
        self.enabled = bool(self.url and self.key and self.bucket and self.object_path)

    def _headers(self) -> Dict[str, str]:
        return {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
        }

    async def _ensure_bucket(self, session: aiohttp.ClientSession) -> bool:
        if not self.enabled:
            return False

        check_url = f"{self.url}/storage/v1/bucket/{self.bucket}"
        async with session.get(check_url, headers=self._headers()) as resp:
            if resp.status == 200:
                return True
            if resp.status != 404:
                body = await resp.text()
                logger.error("Supabase bucket check failed (%s): %s", resp.status, body[:500])
                return False

        create_url = f"{self.url}/storage/v1/bucket"
        payload = {"id": self.bucket, "name": self.bucket, "public": False}
        async with session.post(create_url, headers={**self._headers(), "Content-Type": "application/json"}, json=payload) as resp:
            if resp.status in {200, 201, 409}:
                return True
            body = await resp.text()
            logger.error("Supabase bucket create failed (%s): %s", resp.status, body[:500])
            return False

    async def download(self, destination_path: str) -> bool:
        if not self.enabled:
            return False

        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            if not await self._ensure_bucket(session):
                return False

            url = f"{self.url}/storage/v1/object/{self.bucket}/{self.object_path}"
            async with session.get(url, headers=self._headers()) as resp:
                if resp.status == 404:
                    return False
                if resp.status != 200:
                    body = await resp.text()
                    logger.error("Supabase DB download failed (%s): %s", resp.status, body[:500])
                    return False
                payload = await resp.read()

        os.makedirs(os.path.dirname(destination_path) or ".", exist_ok=True)
        with open(destination_path, "wb") as handle:
            handle.write(payload)
        return True

    async def upload(self, source_path: str) -> bool:
        if not self.enabled:
            return False

        if not os.path.exists(source_path):
            return False

        timeout = aiohttp.ClientTimeout(total=120)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            if not await self._ensure_bucket(session):
                return False

            url = f"{self.url}/storage/v1/object/{self.bucket}/{self.object_path}"
            headers = {
                **self._headers(),
                "Content-Type": "application/octet-stream",
                "x-upsert": "true",
            }

            with open(source_path, "rb") as handle:
                data = handle.read()

            async with session.post(url, headers=headers, data=data) as resp:
                if resp.status in {200, 201}:
                    return True
                body = await resp.text()
                logger.error("Supabase DB upload failed (%s): %s", resp.status, body[:500])
                return False

def _resolve_database_path() -> str:
    """Resolve SQLite path with env override and Railway volume fallback."""
    explicit_path = (
        os.getenv("DATABASE_PATH")
        or os.getenv("DB_PATH")
        or os.getenv("SQLITE_PATH")
    )
    if explicit_path and explicit_path.strip():
        return explicit_path.strip()

    database_url = (os.getenv("DATABASE_URL") or "").strip()
    if database_url.startswith("sqlite:///"):
        return database_url.replace("sqlite:///", "", 1)

    # Railway persistent volume mount path.
    if os.path.isdir("/app/data"):
        return "/app/data/modbot.db"

    return "modbot.db"


DATABASE_PATH = _resolve_database_path()

from db import (
    MemoryMixin,
    CasesMixin,
    StaffMixin,
    TicketsMixin,
    ModmailMixin,
    CourtMixin,
    GiveawaysMixin,
    RolesMixin,
    EnforcementMixin,
    BackupsMixin,
    AccessMixin,
)


class Database(MemoryMixin, CasesMixin, StaffMixin, TicketsMixin, ModmailMixin, CourtMixin, GiveawaysMixin, RolesMixin, EnforcementMixin, BackupsMixin, AccessMixin):
    """
    Main database handler with:
    - Async context manager support
    - Auto-migration for schema changes
    - Connection pooling (single persistent connection)
    - Thread-safe operations
    - Transaction support
    - Input validation
    """
    
    def __init__(self) -> None:
        explicit_mode = _explicit_database_mode()
        raw_database_url = _normalize_postgres_url((os.getenv("DATABASE_URL") or "").strip())

        if explicit_mode == "sqlite":
            self.database_url = ""
            self._is_postgres = False
        elif explicit_mode == "postgres":
            self.database_url = raw_database_url
            self._is_postgres = True
            if not _is_postgres_url(self.database_url):
                raise RuntimeError("DB_MODE=postgres requires DATABASE_URL to be a PostgreSQL URL")
        else:
            self.database_url = raw_database_url
            self._is_postgres = _is_postgres_url(self.database_url)

        self.db_path = DATABASE_PATH
        if not self._is_postgres:
            db_dir = os.path.dirname(self.db_path)
            if db_dir and db_dir.strip():
                try:
                    os.makedirs(db_dir, exist_ok=True)
                except Exception as e:
                    logger.error(f"Failed to create database directory {db_dir}: {e}")
            if explicit_mode == "sqlite":
                logger.info("Database mode: sqlite (%s) [forced by DB_MODE]", self.db_path)
            else:
                logger.info("Database mode: sqlite (%s)", self.db_path)
        else:
            if explicit_mode == "postgres":
                logger.info("Database mode: postgres [forced by DB_MODE]")
            else:
                logger.info("Database mode: postgres")
                
        self._lock = asyncio.Lock()
        self._initialized = False
        self._pool: Optional[Any] = None
        self._supabase_mirror = SupabaseStorageMirror()
        self._supabase_sync_interval_seconds = max(5, int(os.getenv("SUPABASE_SYNC_INTERVAL_SECONDS", "15")))
        self._supabase_last_token: Optional[tuple[tuple[str, int, int], ...]] = None
        self._supabase_sync_task: Optional[asyncio.Task] = None

        if self._is_postgres:
            logger.info("Supabase storage mirror disabled while PostgreSQL is the live database.")
        elif self._supabase_mirror.enabled:
            logger.info(
                "Supabase mirror enabled: bucket=%s, object=%s",
                self._supabase_mirror.bucket,
                self._supabase_mirror.object_path,
            )
        else:
            logger.info("Supabase mirror disabled (SUPABASE_URL/KEY not set).")

        # Query builder for dialect-aware SQL generation
        self._qb = QueryBuilder(is_postgres=self._is_postgres) if QueryBuilder else None

    @property
    def qb(self) -> "QueryBuilder":
        """Dialect-aware query builder (SQLite or PostgreSQL)."""
        return self._qb

    @property
    def pool(self) -> "Database":
        """Compatibility shim for legacy call sites expecting .pool.acquire()."""
        return self

    @asynccontextmanager
    async def acquire(self):
        """Compatibility shim for legacy pool acquire."""
        await self.init_pool()
        async with self.get_connection() as conn:
            yield conn
    
    async def init_pool(self) -> None:
        """Initialize database connection pool"""
        if self._pool is None:
            if self._is_postgres:
                if asyncpg is None:
                    raise RuntimeError("DATABASE_URL points to PostgreSQL but asyncpg is not installed")
                self._pool = await asyncpg.create_pool(
                    dsn=self.database_url,
                    min_size=1,
                    max_size=max(2, int(os.getenv("DATABASE_POOL_MAX_SIZE", "10"))),
                    command_timeout=60,
                )
                logger.info("Database connection pool initialized (PostgreSQL)")
                return

            await self._restore_sqlite_from_supabase()
            self._pool = await aiosqlite.connect(self.db_path)
            # Enable WAL mode for better concurrency
            await self._pool.execute("PRAGMA journal_mode=WAL")
            await self._pool.execute("PRAGMA synchronous=NORMAL")
            # Enable foreign keys
            await self._pool.execute("PRAGMA foreign_keys=ON")
            await self._pool.commit()

            # Ensure we can always restore from a recent remote snapshot.
            await self._sync_sqlite_to_supabase(force=True)
            self._start_supabase_sync_loop()
            logger.info("✅ Database connection pool initialized")
    
    async def _ensure_blacklist_table(self) -> None:
        """Ensure the global blacklist table exists before blacklist operations."""
        async with self.get_connection() as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS blacklist (
                    user_id INTEGER PRIMARY KEY,
                    reason TEXT,
                    added_by INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            await db.commit()

    @asynccontextmanager
    async def get_connection(self):
        """Get database connection as async context manager"""
        await self.init_pool()

        if self._is_postgres:
            raw_conn = await self._pool.acquire()
            compat_conn = PostgresCompatConnection(raw_conn)
            try:
                yield compat_conn
            finally:
                await compat_conn.close_pending_transaction()
                await self._pool.release(raw_conn)
            return

        if self._pool is not None:
            yield self._pool
        else:
            conn = await aiosqlite.connect(self.db_path)
            try:
                yield conn
            finally:
                await conn.close()

    def _sqlite_change_token(self) -> tuple[tuple[str, int, int], ...]:
        """Fingerprint DB + WAL sidecars to skip redundant uploads."""
        parts: list[tuple[str, int, int]] = []
        for suffix in ("", "-wal", "-shm"):
            path = f"{self.db_path}{suffix}"
            if not os.path.exists(path):
                continue
            try:
                stat = os.stat(path)
                parts.append((suffix or ".db", int(stat.st_mtime_ns), int(stat.st_size)))
            except OSError:
                continue
        return tuple(parts)

    async def _restore_sqlite_from_supabase(self) -> None:
        """Restore the SQLite file from Supabase before opening connections."""
        if self._is_postgres or not self._supabase_mirror.enabled:
            return

        tmp_file = tempfile.NamedTemporaryFile(prefix="modbot_supabase_restore_", suffix=".db", delete=False)
        tmp_path = tmp_file.name
        tmp_file.close()
        try:
            restored = await self._supabase_mirror.download(tmp_path)
            if not restored:
                return

            # Clear stale sidecars before replacing the main DB file.
            for sidecar_suffix in ("-wal", "-shm"):
                sidecar = f"{self.db_path}{sidecar_suffix}"
                if os.path.exists(sidecar):
                    try:
                        os.remove(sidecar)
                    except OSError:
                        pass

            os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
            os.replace(tmp_path, self.db_path)
            self._supabase_last_token = self._sqlite_change_token()
            logger.info("Restored SQLite database from Supabase mirror.")
        except Exception as exc:
            logger.error("Failed to restore SQLite database from Supabase: %s", exc)
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    async def _create_sqlite_snapshot(self, snapshot_path: str) -> bool:
        """Create a consistent snapshot of the live SQLite database."""
        if self._is_postgres:
            return False
        if os.path.exists(snapshot_path):
            try:
                os.remove(snapshot_path)
            except OSError:
                return False

        # If pool is unavailable, fallback to file copy.
        if self._pool is None:
            if not os.path.exists(self.db_path):
                return False
            shutil.copy2(self.db_path, snapshot_path)
            return True

        try:
            async with self._lock:
                # Flush WAL then vacuum into a standalone file.
                await self._pool.execute("PRAGMA wal_checkpoint(FULL)")
                escaped_path = snapshot_path.replace("'", "''")
                await self._pool.execute(f"VACUUM INTO '{escaped_path}'")
                await self._pool.commit()
            return os.path.exists(snapshot_path)
        except Exception as exc:
            logger.error("Failed to create SQLite snapshot for Supabase sync: %s", exc)
            return False

    async def _sync_sqlite_to_supabase(self, *, force: bool = False) -> None:
        """Upload current SQLite state to Supabase Storage."""
        if self._is_postgres or not self._supabase_mirror.enabled:
            return

        current_token = self._sqlite_change_token()
        if not current_token:
            return
        if not force and self._supabase_last_token == current_token:
            return

        tmp_file = tempfile.NamedTemporaryFile(prefix="modbot_supabase_snapshot_", suffix=".db", delete=False)
        snapshot_path = tmp_file.name
        tmp_file.close()
        try:
            created = await self._create_sqlite_snapshot(snapshot_path)
            if not created:
                return
            uploaded = await self._supabase_mirror.upload(snapshot_path)
            if uploaded:
                self._supabase_last_token = self._sqlite_change_token()
                logger.debug("Synced SQLite snapshot to Supabase.")
        except Exception as exc:
            logger.error("Supabase sync failed: %s", exc)
        finally:
            if os.path.exists(snapshot_path):
                try:
                    os.remove(snapshot_path)
                except OSError:
                    pass

    async def _supabase_sync_loop(self) -> None:
        """Periodic background sync so abrupt restarts don't lose recent writes."""
        try:
            while self._pool is not None and not self._is_postgres:
                await asyncio.sleep(self._supabase_sync_interval_seconds)
                await self._sync_sqlite_to_supabase(force=False)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Supabase sync loop crashed: %s", exc)

    def _start_supabase_sync_loop(self) -> None:
        if self._is_postgres or not self._supabase_mirror.enabled:
            return
        if self._supabase_sync_task and not self._supabase_sync_task.done():
            return
        self._supabase_sync_task = asyncio.create_task(self._supabase_sync_loop())
    
    @asynccontextmanager
    async def transaction(self):
        """
        Transaction context manager for safe database operations
        Automatically commits on success, rolls back on error
        """
        async with self._lock:
            async with self.get_connection() as conn:
                try:
                    await conn.execute("BEGIN")
                    yield conn
                    await conn.commit()
                except Exception:
                    await conn.rollback()
                    raise
    
    @staticmethod
    def _validate_guild_id(guild_id: int) -> None:
        """Validate guild ID"""
        if not isinstance(guild_id, int) or guild_id <= 0:
            raise ValueError(f"Invalid guild_id: {guild_id}")
    
    @staticmethod
    def _validate_user_id(user_id: int) -> None:
        """Validate user ID"""
        if not isinstance(user_id, int) or user_id <= 0:
            raise ValueError(f"Invalid user_id: {user_id}")
    
    # ==================== INITIALIZATION & MIGRATION ====================
    
    async def _migrate_schema(self, db: aiosqlite.Connection) -> None:
        """Auto-migrate schema by adding missing columns"""
        migrations = [
            # modmail_threads
            ("modmail_threads", "category", "TEXT DEFAULT 'general'"),
            ("modmail_threads", "priority", "TEXT DEFAULT 'normal'"),
            ("modmail_threads", "claimed_by", "INTEGER"),
            ("modmail_threads", "message_count", "INTEGER DEFAULT 0"),
            # modmail_messages
            ("modmail_messages", "is_staff", "BOOLEAN DEFAULT 0"),
            # court_sessions
            ("court_sessions", "jury_data", "TEXT DEFAULT '[]'"),
            # staff_sanctions
            ("staff_sanctions", "sanction_type", "TEXT DEFAULT 'warn'"),
            # tickets
            ("tickets", "details", "TEXT"),
            ("tickets", "claimed_by", "INTEGER"),
            ("tickets", "claimed_at", "TIMESTAMP"),
            ("tickets", "panel_message_id", "INTEGER"),
            # giveaways
            ("giveaways", "description", "TEXT"),
            ("giveaways", "bonus_role_id", "INTEGER"),
            ("giveaways", "bonus_amount", "INTEGER DEFAULT 0"),
            ("giveaways", "required_role_id", "INTEGER"),
            ("giveaways", "winners_role_id", "INTEGER"),
            ("giveaways", "image_url", "TEXT"),
            ("giveaways", "thumbnail_url", "TEXT"),
            ("giveaways", "banner_url", "TEXT"),
            ("giveaways", "dm_winners", "BOOLEAN DEFAULT 0"),
            ("giveaways", "reward", "TEXT"),
            # quarantines
            ("quarantines", "started_at", "TIMESTAMP"),
        ]
        
        for table, column, col_type in migrations:
            try:
                if getattr(self, "_is_postgres", False):
                    await db.execute(f"SAVEPOINT mig_{table}_{column}")
                    try:
                        await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
                        await db.execute(f"RELEASE SAVEPOINT mig_{table}_{column}")
                        logger.debug(f"✅ Added column '{column}' to '{table}'")
                    except Exception:
                        await db.execute(f"ROLLBACK TO SAVEPOINT mig_{table}_{column}")
                else:
                    await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
                    logger.debug(f"✅ Added column '{column}' to '{table}'")
            except Exception:
                pass  # Column already exists
    
    async def init_guild(self, guild_id: int) -> None:
        """Initialize all database tables and ensure guild exists"""
        self._validate_guild_id(guild_id)
        
        # Ensure pool is initialized
        await self.init_pool()
        
        async with self._lock:
            async with self.get_connection() as db:
                # ===== CORE GUILD SETTINGS =====
                # Create tables first; indexes are created after schema/migrations.
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS guild_settings (
                        guild_id INTEGER PRIMARY KEY,
                        settings TEXT DEFAULT '{}'
                    )
                """)
                
                # ===== MODERATION =====
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS cases (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        guild_id INTEGER,
                        case_number INTEGER,
                        user_id INTEGER,
                        moderator_id INTEGER,
                        action TEXT,
                        reason TEXT,
                        duration TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        active BOOLEAN DEFAULT 1
                    )
                """)
                
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS warnings (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        guild_id INTEGER,
                        user_id INTEGER,
                        moderator_id INTEGER,
                        reason TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS mod_notes (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        guild_id INTEGER,
                        user_id INTEGER,
                        moderator_id INTEGER,
                        note TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS tempbans (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        guild_id INTEGER,
                        user_id INTEGER,
                        moderator_id INTEGER,
                        reason TEXT,
                        expires_at TIMESTAMP,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS mod_stats (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        guild_id INTEGER,
                        moderator_id INTEGER,
                        action TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                
                # ===== REPORTS & TICKETS =====
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS reports (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        guild_id INTEGER,
                        reporter_id INTEGER,
                        reported_id INTEGER,
                        reason TEXT,
                        resolved BOOLEAN DEFAULT 0,
                        resolved_by INTEGER,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS tickets (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        guild_id INTEGER,
                        channel_id INTEGER,
                        user_id INTEGER,
                        ticket_number INTEGER,
                        category TEXT,
                        status TEXT DEFAULT 'open',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        closed_at TIMESTAMP
                    )
                """)
                
                # ===== STAFF SANCTIONS =====
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS staff_sanctions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        guild_id INTEGER,
                        staff_id INTEGER,
                        issuer_id INTEGER,
                        reason TEXT,
                        sanction_type TEXT DEFAULT 'warn',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                
                # ===== COURT SYSTEM =====
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS court_sessions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        channel_id INTEGER,
                        guild_id INTEGER,
                        case_type TEXT,
                        plaintiff_id INTEGER,
                        defendant_id INTEGER,
                        judge_id INTEGER,
                        reason TEXT,
                        verdict TEXT,
                        status TEXT DEFAULT 'open',
                        jury_data TEXT DEFAULT '[]',
                        started_at TEXT,
                        closed_at TEXT
                    )
                """)
                
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS court_evidence (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id INTEGER,
                        channel_id INTEGER,
                        title TEXT,
                        description TEXT,
                        link TEXT,
                        submitted_by INTEGER,
                        timestamp TEXT,
                        FOREIGN KEY (session_id) REFERENCES court_sessions(id)
                    )
                """)
                
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS court_votes (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id INTEGER,
                        voter_id INTEGER,
                        vote TEXT,
                        timestamp TEXT,
                        UNIQUE(session_id, voter_id),
                        FOREIGN KEY (session_id) REFERENCES court_sessions(id)
                    )
                """)
                
                # ===== MODMAIL SYSTEM =====
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS modmail_threads (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        guild_id INTEGER,
                        user_id INTEGER,
                        channel_id INTEGER,
                        category TEXT DEFAULT 'general',
                        priority TEXT DEFAULT 'normal',
                        opened_at TEXT,
                        closed_at TEXT,
                        status TEXT DEFAULT 'open',
                        claimed_by INTEGER,
                        message_count INTEGER DEFAULT 0
                    )
                """)
                
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS modmail_messages (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        thread_id INTEGER,
                        author_id INTEGER,
                        content TEXT,
                        timestamp TEXT,
                        is_staff BOOLEAN DEFAULT 0,
                        FOREIGN KEY (thread_id) REFERENCES modmail_threads(id)
                    )
                """)
                
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS modmail_blocks (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        guild_id INTEGER,
                        user_id INTEGER,
                        reason TEXT,
                        blocked_by INTEGER,
                        created_at TEXT
                    )
                """)
                
                # ===== UTILITY =====
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS giveaways (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        guild_id INTEGER,
                        channel_id INTEGER,
                        message_id INTEGER,
                        prize TEXT,
                        reward TEXT,
                        description TEXT,
                        winners INTEGER DEFAULT 1,
                        ends_at TIMESTAMP,
                        ended BOOLEAN DEFAULT 0,
                        host_id INTEGER,
                        bonus_role_id INTEGER,
                        bonus_amount INTEGER DEFAULT 0,
                        required_role_id INTEGER,
                        winners_role_id INTEGER,
                        image_url TEXT,
                        thumbnail_url TEXT,
                        banner_url TEXT,
                        dm_winners BOOLEAN DEFAULT 0,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

                await db.execute("""
                    CREATE TABLE IF NOT EXISTS giveaway_entries (
                        giveaway_id INTEGER,
                        user_id INTEGER,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (giveaway_id, user_id)
                    )
                """)
                
                # ===== REACTION ROLES =====
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS reaction_roles (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        guild_id INTEGER,
                        message_id INTEGER,
                        emoji TEXT,
                        role_id INTEGER,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                
                # ===== VOICE ROLES =====
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS voice_roles (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        guild_id INTEGER,
                        channel_id INTEGER,
                        role_id INTEGER,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                
                # ===== GLOBAL BLACKLIST =====
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS blacklist (
                        user_id INTEGER PRIMARY KEY,
                        reason TEXT,
                        added_by INTEGER,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

                # ===== WHITELIST SYSTEM =====
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS whitelist (
                        guild_id INTEGER,
                        user_id INTEGER,
                        added_by INTEGER,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (guild_id, user_id)
                    )
                """)

                # ===== AI MEMORY =====
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS ai_memory (
                        user_id INTEGER PRIMARY KEY,
                        memory_text TEXT,
                        last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

                # ===== GUILD MEMORY =====
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS guild_memory (
                        guild_id INTEGER PRIMARY KEY,
                        guild_name TEXT,
                        memory_text TEXT,
                        last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

                # ===== DELETED MESSAGE ATTACHMENTS =====
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS deleted_message_attachments (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        guild_id INTEGER NOT NULL,
                        channel_id INTEGER NOT NULL,
                        message_id INTEGER NOT NULL,
                        filename TEXT NOT NULL,
                        content_type TEXT,
                        original_url TEXT,
                        data BLOB NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

                # ===== QUARANTINES =====
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS quarantines (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        guild_id INTEGER,
                        user_id INTEGER,
                        moderator_id INTEGER,
                        reason TEXT,
                        roles_backup TEXT DEFAULT '[]',
                        expires_at TIMESTAMP,
                        active BOOLEAN DEFAULT 1,
                        started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

                # ===== DASHBOARD AUDIT =====
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS dashboard_audit (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        guild_id INTEGER NOT NULL,
                        user_id TEXT,
                        user_name TEXT,
                        action TEXT NOT NULL,
                        target TEXT NOT NULL,
                        changes TEXT DEFAULT '{}',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)


                # ===== AI SCHEDULER & PROJECTS =====
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS scheduled_tasks (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        guild_id INTEGER NOT NULL,
                        author_id INTEGER NOT NULL,
                        task_type TEXT NOT NULL,
                        payload TEXT DEFAULT '{}',
                        execute_at TIMESTAMP NOT NULL,
                        status TEXT DEFAULT 'pending',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS group_projects (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        guild_id INTEGER NOT NULL,
                        category_id INTEGER NOT NULL,
                        status TEXT DEFAULT 'active',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

                # ===== SERVER BACKUPS =====
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS server_backups (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        guild_id INTEGER NOT NULL,
                        created_by INTEGER NOT NULL,
                        triggered_by TEXT DEFAULT 'manual',
                        backup_data TEXT NOT NULL,
                        summary TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

                # ===== USER RISK SCORES =====
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS user_risk_scores (
                        guild_id INTEGER NOT NULL,
                        user_id INTEGER NOT NULL,
                        score INTEGER DEFAULT 0,
                        factors TEXT DEFAULT '{}',
                        last_calculated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (guild_id, user_id)
                    )
                """)

                # ===== BANNED USER PROFILES (alt detection) =====
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS banned_user_profiles (
                        user_id INTEGER PRIMARY KEY,
                        username TEXT NOT NULL DEFAULT '',
                        avatar_hash TEXT,
                        banned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        banned_from_guilds TEXT DEFAULT '[]'
                    )
                """)

                # ===== USER MESSAGES (behavior profiling) =====
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS user_messages (
                        message_id INTEGER PRIMARY KEY,
                        guild_id INTEGER NOT NULL,
                        channel_id INTEGER NOT NULL,
                        user_id INTEGER NOT NULL,
                        content TEXT NOT NULL,
                        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

                # ===== DASHBOARD TELEMETRY =====
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS automod_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        guild_id INTEGER NOT NULL,
                        user_id INTEGER NOT NULL,
                        channel_id INTEGER,
                        rule TEXT NOT NULL,
                        category TEXT NOT NULL,
                        severity TEXT NOT NULL,
                        action TEXT NOT NULL,
                        reason TEXT,
                        message_deleted BOOLEAN DEFAULT 0,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

                await db.execute("""
                    CREATE TABLE IF NOT EXISTS guild_member_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        guild_id INTEGER NOT NULL,
                        user_id INTEGER NOT NULL,
                        event_type TEXT NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                
                # Auto-migrate missing columns
                await self._migrate_schema(db)

                # Add indexes for better performance (best-effort).
                index_statements = [
                    """
                    CREATE INDEX IF NOT EXISTS idx_cases_guild_user
                    ON cases(guild_id, user_id)
                    """,
                    """
                    CREATE INDEX IF NOT EXISTS idx_cases_guild_active
                    ON cases(guild_id, active)
                    """,
                    """
                    CREATE INDEX IF NOT EXISTS idx_warnings_guild_user
                    ON warnings(guild_id, user_id)
                    """,
                    """
                    CREATE INDEX IF NOT EXISTS idx_mod_notes_guild_user
                    ON mod_notes(guild_id, user_id)
                    """,
                    """
                    CREATE INDEX IF NOT EXISTS idx_reports_guild_resolved
                    ON reports(guild_id, resolved)
                    """,
                    """
                    CREATE INDEX IF NOT EXISTS idx_tickets_guild_status
                    ON tickets(guild_id, status)
                    """,
                    """
                    CREATE INDEX IF NOT EXISTS idx_modmail_threads_guild_user
                    ON modmail_threads(guild_id, user_id, status)
                    """,
                    """
                    CREATE INDEX IF NOT EXISTS idx_deleted_message_attachments_message
                    ON deleted_message_attachments(guild_id, message_id)
                    """,
                    """
                    CREATE INDEX IF NOT EXISTS idx_user_messages_guild_user
                    ON user_messages(guild_id, user_id)
                    """,
                    """
                    CREATE INDEX IF NOT EXISTS idx_automod_events_guild_created
                    ON automod_events(guild_id, created_at)
                    """,
                    """
                    CREATE INDEX IF NOT EXISTS idx_member_events_guild_created
                    ON guild_member_events(guild_id, created_at)
                    """,
                ]
                for sql in index_statements:
                    try:
                        await db.execute(sql)
                    except Exception:
                        pass
                
                # Ensure guild exists
                await db.execute(
                    "INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)",
                    (guild_id,),
                )
                
                await db.commit()
                
                if not self._initialized:
                    logger.info(f"✅ Initialized database for guild {guild_id}")
                    self._initialized = True
    
    # ==================== SETTINGS ====================
    
    async def get_settings(self, guild_id: int) -> Dict[str, Any]:
        """Get guild settings"""
        self._validate_guild_id(guild_id)
        
        settings = {}
        try:
            async with self.get_connection() as db:
                cursor = await db.execute(
                    "SELECT settings FROM guild_settings WHERE guild_id = ?",
                    (guild_id,),
                )
                row = await cursor.fetchone()
                settings = json.loads(_row_get(row, "settings")) if row and _row_get(row, "settings") else {}
        except aiosqlite.OperationalError as e:
            if "no such table: guild_settings" not in str(e).lower():
                raise

            # Schema missing: initialize and retry once.
            await self.init_guild(guild_id)
            async with self.get_connection() as db:
                cursor = await db.execute(
                    "SELECT settings FROM guild_settings WHERE guild_id = ?",
                    (guild_id,),
                )
                row = await cursor.fetchone()
                settings = json.loads(_row_get(row, "settings")) if row and _row_get(row, "settings") else {}
                
        # Merge with defaults, but resolve explicit module/flat toggle state
        # before defaults can make a missing key look enabled.
        settings = normalize_runtime_settings(settings)
        merged = AUTOMOD_SETTINGS.copy()
        merged.update(settings)
        return normalize_runtime_settings(merged)
    
    async def update_settings(self, guild_id: int, settings: Dict[str, Any]) -> None:
        """Update guild settings"""
        self._validate_guild_id(guild_id)
        incoming_settings = dict(settings or {})
        
        async with self._lock:
            async with self.get_connection() as db:
                if incoming_settings:
                    cursor = await db.execute(
                        "SELECT settings FROM guild_settings WHERE guild_id = ?",
                        (guild_id,),
                    )
                    row = await cursor.fetchone()
                    existing_settings = json.loads(_row_get(row, "settings")) if row and _row_get(row, "settings") else {}
                    settings_to_store = _deep_merge_settings(existing_settings, incoming_settings)
                else:
                    # An empty dict is the admin reset path. Keep it destructive.
                    settings_to_store = {}

                settings_to_store = normalize_runtime_settings(settings_to_store)
                await db.execute(
                    """
                    INSERT OR REPLACE INTO guild_settings (guild_id, settings)
                    VALUES (?, ?)
                    """,
                    (guild_id, json.dumps(settings_to_store)),
                )
                await db.commit()
    
    async def set_setting(self, guild_id: int, key: str, value: Any) -> None:
        """Set a single setting"""
        settings = await self.get_settings(guild_id)
        settings[key] = value
        await self.update_settings(guild_id, settings)

    # ==================== AI MEMORY ====================





    # ==================== GUILD MEMORY ====================





    # ==================== DASHBOARD TELEMETRY ====================



    # ==================== CHANNEL MESSAGE BATCHES ====================


    # ==================== QUARANTINE ====================



    # ==================== MODERATION - CASES ====================
    
    
    
    
    
    # ==================== MODERATION - WARNINGS ====================
    

    
    
    
    
    # ==================== MODERATION - NOTES ====================
    
    
    
    # ==================== REPORTS ====================
    
    
    
    
    # ==================== TICKETS ====================
    
    

    
    
    
    # ==================== TEMPBANS ====================
    
    
    
    
    # ==================== STAFF SANCTIONS ====================
    
    
    
    
    
    
    
    
    # ==================== COURT SYSTEM ====================
    
    
    
    
    
    
    
    
    
    # ==================== MODMAIL SYSTEM ====================
    
    
    
    
    


    
    
    
    
    # ==================== GIVEAWAYS ====================
    
    




    
    
    # ==================== REACTION ROLES ====================
    
    
    
    
    # ==================== VOICE ROLES ====================
    
    
    
    
    # ==================== MOD STATS ====================
    
    
    # ==================== CLEANUP ====================
    
    
    
    
    # ==================== GLOBAL BLACKLIST ====================
    
    
    
    

    # ==================== WHITELIST SYSTEM ====================






    # ==================== SERVER BACKUPS ====================





    # ==================== USER RISK SCORES ====================




    # ==================== BANNED USER PROFILES ====================




    # ==================== BEHAVIOR PROFILING ====================



    # ==================== LIFECYCLE ====================

    async def close(self) -> None:
        """Flush WAL, do a final Supabase sync, and close the connection."""
        if self._supabase_sync_task and not self._supabase_sync_task.done():
            self._supabase_sync_task.cancel()
            try:
                await self._supabase_sync_task
            except asyncio.CancelledError:
                pass
            self._supabase_sync_task = None

        if self._pool is not None:
            try:
                if self._is_postgres:
                    await self._pool.close()
                else:
                    try:
                        await self._pool.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                        await self._pool.commit()
                    except Exception as exc:
                        logger.error("WAL checkpoint on close failed: %s", exc)

                    try:
                        await self._sync_sqlite_to_supabase(force=True)
                    except Exception as exc:
                        logger.error("Final Supabase sync on close failed: %s", exc)

                    await self._pool.close()
            except Exception as exc:
                logger.error("Database connection close failed: %s", exc)
            finally:
                self._pool = None

        logger.info("Database closed.")
