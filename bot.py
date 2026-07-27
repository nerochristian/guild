import asyncio
import base64
import io
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
import discord
from discord import app_commands
from discord.ext import commands
import aiohttp
import psycopg2
import psycopg2.extras

try:
    from openai import AsyncOpenAI
except ImportError:
    AsyncOpenAI = None

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env", override=False)

from components_v2 import (
    branded_asset_files,
    branded_asset_url,
    branded_panel_container,
    ensure_valk_emojis,
    ensure_layout_view_action_rows,
    get_valk_emoji,
)
from ticket_system import init_ticket_system
from jail_system import init_jail_system
from level_system import init_level_system

from economy import init_economy_system
from afk_system import init_afk_system
from website.backend import (
    SetupPortalView,
    SetupWebsiteConfig,
    create_setup_session,
    register_setup_routes,
    setup_url,
)
from website.frontend import (
    settings_int as _settings_int,
    settings_text as _settings_text,
)

try:
    from safety_system import BotSafetyStore
except ModuleNotFoundError:
    logging.getLogger("enzo-bot").warning(
        "safety_system.py is missing; using local BotSafetyStore fallback."
    )

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
                                guild_id BIGINT NOT NULL,
                                module TEXT NOT NULL DEFAULT 'global',
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
                            "ALTER TABLE bot_safety_state ADD COLUMN IF NOT EXISTS module TEXT NOT NULL DEFAULT 'global'"
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
                return True if row is None else bool(row[0])
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
                            INSERT INTO bot_safety_state
                                (guild_id, module, enabled, updated_by, updated_at, reason)
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

        def get_status(
            self, guild_id: int, module: str = "global"
        ) -> dict[str, object]:
            conn = self._connect()
            try:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT enabled, updated_by, updated_at, reason
                        FROM bot_safety_state
                        WHERE guild_id = %s AND module = %s
                        """,
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


class BotGuildSettingsStore:
    def __init__(self, db_url: str) -> None:
        if not db_url:
            raise RuntimeError(
                "DATABASE_URL is required; configure it before starting the bot."
            )
        self.db_url = db_url

    def _connect(self):
        return psycopg2.connect(self.db_url, cursor_factory=psycopg2.extras.DictCursor)

    def initialize(self) -> None:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS bot_guild_settings (
                            guild_id BIGINT PRIMARY KEY,
                            announcement_channel_id BIGINT,
                            welcome_channel_id BIGINT,
                            rules_channel_id BIGINT,
                            agent_channel_id BIGINT,
                            level_channel_id BIGINT,
                            inactive_channel_id BIGINT,
                            server_name TEXT,
                            welcome_wallpaper_path TEXT,
                            ticket_support_role_id BIGINT,
                            bypass_role_id BIGINT,
                            level_min_xp INT DEFAULT 15,
                            level_max_xp INT DEFAULT 25,
                            invite_xp INT DEFAULT 500,
                            max_transcript_messages INT DEFAULT 5000,
                            level_reward_roles JSONB,
                            femboy_mode BOOLEAN DEFAULT TRUE,
                            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                        )
                        """
                    )
                    for column in (
                        "announcement_channel_id BIGINT",
                        "welcome_channel_id BIGINT",
                        "rules_channel_id BIGINT",
                        "agent_channel_id BIGINT",
                        "level_channel_id BIGINT",
                        "inactive_channel_id BIGINT",
                        "server_name TEXT",
                        "welcome_wallpaper_path TEXT",
                        "ticket_support_role_id BIGINT",
                        "bypass_role_id BIGINT",
                        "level_min_xp INT DEFAULT 15",
                        "level_max_xp INT DEFAULT 25",
                        "invite_xp INT DEFAULT 500",
                        "max_transcript_messages INT DEFAULT 5000",
                        "level_reward_roles JSONB",
                        "updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP",
                        "nsfw_enabled BOOLEAN DEFAULT FALSE",
                        "femboy_mode BOOLEAN DEFAULT TRUE",
                    ):
                        cursor.execute(
                            f"ALTER TABLE bot_guild_settings ADD COLUMN IF NOT EXISTS {column}"
                        )
        finally:
            conn.close()

    def get_settings(self, guild_id: int) -> dict[str, object]:
        conn = self._connect()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT announcement_channel_id, welcome_channel_id, rules_channel_id,
                           agent_channel_id, level_channel_id,
                           inactive_channel_id, server_name, welcome_wallpaper_path, nsfw_enabled,
                           ticket_support_role_id, bypass_role_id, level_min_xp, level_max_xp,
                           invite_xp, max_transcript_messages, level_reward_roles, femboy_mode
                    FROM bot_guild_settings
                    WHERE guild_id = %s
                    """,
                    (guild_id,),
                )
                row = cursor.fetchone()
            return dict(row) if row is not None else {}
        finally:
            conn.close()

    def save_settings(
        self,
        guild_id: int,
        *,
        announcement_channel_id: Optional[int] = None,
        welcome_channel_id: Optional[int] = None,
        rules_channel_id: Optional[int] = None,
        agent_channel_id: Optional[int] = None,
        level_channel_id: Optional[int] = None,
        inactive_channel_id: Optional[int] = None,
        server_name: Optional[str] = None,
        welcome_wallpaper_path: Optional[str] = None,
        nsfw_enabled: Optional[bool] = None,
        ticket_support_role_id: Optional[int] = None,
        bypass_role_id: Optional[int] = None,
        level_min_xp: Optional[int] = None,
        level_max_xp: Optional[int] = None,
        invite_xp: Optional[int] = None,
        max_transcript_messages: Optional[int] = None,
        level_reward_roles: Optional[dict[int, int]] = None,
        femboy_mode: Optional[bool] = None,
    ) -> dict[str, object]:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO bot_guild_settings (
                            guild_id, announcement_channel_id, welcome_channel_id,
                            rules_channel_id, agent_channel_id,
                            level_channel_id, inactive_channel_id, server_name,
                            welcome_wallpaper_path, nsfw_enabled,
                            ticket_support_role_id, bypass_role_id, level_min_xp,
                            level_max_xp, invite_xp, max_transcript_messages, level_reward_roles,
                            femboy_mode
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (guild_id) DO UPDATE SET
                            announcement_channel_id = COALESCE(EXCLUDED.announcement_channel_id, bot_guild_settings.announcement_channel_id),
                            welcome_channel_id = COALESCE(EXCLUDED.welcome_channel_id, bot_guild_settings.welcome_channel_id),
                            rules_channel_id = COALESCE(EXCLUDED.rules_channel_id, bot_guild_settings.rules_channel_id),
                            agent_channel_id = COALESCE(EXCLUDED.agent_channel_id, bot_guild_settings.agent_channel_id),
                            level_channel_id = COALESCE(EXCLUDED.level_channel_id, bot_guild_settings.level_channel_id),
                            inactive_channel_id = COALESCE(EXCLUDED.inactive_channel_id, bot_guild_settings.inactive_channel_id),
                            server_name = COALESCE(EXCLUDED.server_name, bot_guild_settings.server_name),
                            welcome_wallpaper_path = COALESCE(EXCLUDED.welcome_wallpaper_path, bot_guild_settings.welcome_wallpaper_path),
                            nsfw_enabled = COALESCE(EXCLUDED.nsfw_enabled, bot_guild_settings.nsfw_enabled),
                            ticket_support_role_id = COALESCE(EXCLUDED.ticket_support_role_id, bot_guild_settings.ticket_support_role_id),
                            bypass_role_id = COALESCE(EXCLUDED.bypass_role_id, bot_guild_settings.bypass_role_id),
                            level_min_xp = COALESCE(EXCLUDED.level_min_xp, bot_guild_settings.level_min_xp),
                            level_max_xp = COALESCE(EXCLUDED.level_max_xp, bot_guild_settings.level_max_xp),
                            invite_xp = COALESCE(EXCLUDED.invite_xp, bot_guild_settings.invite_xp),
                            max_transcript_messages = COALESCE(EXCLUDED.max_transcript_messages, bot_guild_settings.max_transcript_messages),
                            level_reward_roles = COALESCE(EXCLUDED.level_reward_roles, bot_guild_settings.level_reward_roles),
                            femboy_mode = COALESCE(EXCLUDED.femboy_mode, bot_guild_settings.femboy_mode),
                            updated_at = CURRENT_TIMESTAMP
                        RETURNING announcement_channel_id, welcome_channel_id, rules_channel_id,
                                  agent_channel_id, level_channel_id,
                                  inactive_channel_id, server_name, welcome_wallpaper_path, nsfw_enabled,
                                  ticket_support_role_id, bypass_role_id, level_min_xp, level_max_xp,
                                  invite_xp, max_transcript_messages, femboy_mode
                        """,
                        (
                            guild_id,
                            announcement_channel_id,
                            welcome_channel_id,
                            rules_channel_id,
                            agent_channel_id,
                            level_channel_id,
                            inactive_channel_id,
                            server_name,
                            welcome_wallpaper_path,
                            nsfw_enabled,
                            ticket_support_role_id,
                            bypass_role_id,
                            level_min_xp,
                            level_max_xp,
                            invite_xp,
                            max_transcript_messages,
                            json.dumps(level_reward_roles) if level_reward_roles is not None else None,
                            femboy_mode,
                        ),
                    )
                    row = cursor.fetchone()
            return dict(row) if row is not None else {}
        finally:
            conn.close()

    def reset_level_settings(self, guild_id: int) -> dict[str, object]:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO bot_guild_settings (
                            guild_id, level_channel_id, level_min_xp, level_max_xp,
                            invite_xp, level_reward_roles, updated_at
                        )
                        VALUES (%s, NULL, 15, 25, 500, NULL, CURRENT_TIMESTAMP)
                        ON CONFLICT (guild_id) DO UPDATE SET
                            level_channel_id = NULL,
                            level_min_xp = 15,
                            level_max_xp = 25,
                            invite_xp = 500,
                            level_reward_roles = NULL,
                            updated_at = CURRENT_TIMESTAMP
                        """,
                        (guild_id,),
                    )
            return self.get_settings(guild_id)
        finally:
            conn.close()


try:
    from welcome_system import init_welcome_system
except ModuleNotFoundError:
    init_welcome_system = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
LOGGER = logging.getLogger("enzo-bot")


def get_env_int(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None or not raw_value.strip():
        return default

    try:
        return int(raw_value.strip())
    except ValueError:
        LOGGER.warning(
            "Invalid integer for %s: %r. Using default %s.", name, raw_value, default
        )
        return default


def get_env_int_set(name: str) -> set[int]:
    raw_value = os.getenv(name, "")
    allowed_ids: set[int] = set()
    for part in raw_value.split(","):
        item = part.strip()
        if not item:
            continue
        try:
            allowed_ids.add(int(item))
        except ValueError:
            LOGGER.warning("Invalid integer in %s: %r. Skipping.", name, item)
    return allowed_ids


BOT_TOKEN: Optional[str] = os.getenv("DISCORD_BOT_TOKEN")
DEV_GUILD_ID: Optional[str] = os.getenv("DISCORD_GUILD_ID")
SERVER_NAME = os.getenv("SERVER_NAME", "Soul")
OFFICER_ROLE_ID = get_env_int("OFFICER_ROLE_ID", 0)
D1_OFFICER_ROLE_ID = get_env_int("D1_OFFICER_ROLE_ID", 0)
D2_OFFICER_ROLE_ID = get_env_int("D2_OFFICER_ROLE_ID", 0)
TICKET_SUPPORT_ROLE_ID = get_env_int("TICKET_SUPPORT_ROLE_ID", OFFICER_ROLE_ID)
ANNOUNCEMENT_CHANNEL_ID = get_env_int("ANNOUNCEMENT_CHANNEL_ID", 0)
COMMAND_RESTRICTED_GUILD_ID = get_env_int("COMMAND_RESTRICTED_GUILD_ID", 0)
COMMAND_ALLOWED_CHANNEL_IDS = get_env_int_set("COMMAND_ALLOWED_CHANNEL_IDS")

ACCENT_COLOR = 0x5865F2
SUPER_USER_IDS = {1269772767516033025, 1470873417614889124}
ALLOWED_USER_IDS = get_env_int_set("ALLOWED_USER_IDS") | SUPER_USER_IDS

COMMAND_CHANNEL_EXEMPTIONS = {
    "blacklist",
    "unblacklist",
    "hug",
    "slap",
    "pat",
    "kiss",
    "poke",
    "cuddle",
    "bite",
    "tickle",
    "headpat",
    "bonk",
    "yeet",
    "feed",
    "baka",
    "kill",
    "marry",
    "marriage",
    "marrage",
    "propose",
    "divorce",
    "steal",
    "lick",
    "stare",
    "throw",
    "carry",
    "highfive",
    "handhold",
    "peck",
    "nuzzle",
    "glomp",
    "tackle",
    "cry",
    "blush",
    "neko",
    "smug",
    "dance",
    "laugh",
    "facepalm",
    "pout",
    "shrug",
    "wave",
    "wink",
    "yawn",
    "naughty",
    "roast",
    "compliment",
    "the-big-reset",
}
SETUP_TOKEN_TTL_SECONDS = get_env_int("SETUP_TOKEN_TTL_SECONDS", 30 * 60)
SETUP_MAX_UPLOAD_BYTES = get_env_int("SETUP_MAX_UPLOAD_BYTES", 8 * 1024 * 1024)
SETUP_UPLOAD_DIR = BASE_DIR / "assets" / "setup_welcome"
APPLICATION_IMAGE_READ_TIMEOUT_S = get_env_int("APPLICATION_IMAGE_READ_TIMEOUT_S", 20)
APPLICATION_AI_TIMEOUT_S = get_env_int("APPLICATION_AI_TIMEOUT_S", 180)
MAX_APPLICATION_IMAGE_BYTES = get_env_int(
    "MAX_APPLICATION_IMAGE_BYTES", 8 * 1024 * 1024
)
APPLICATION_CHANNEL_CLOSE_DELAY_S = get_env_int("APPLICATION_CHANNEL_CLOSE_DELAY_S", 5)
APPLICATION_INACTIVITY_REMINDER_S = get_env_int(
    "APPLICATION_INACTIVITY_REMINDER_S", 5 * 60
)
APPLICATION_INACTIVITY_CLOSE_S = get_env_int("APPLICATION_INACTIVITY_CLOSE_S", 15 * 60)
APPLICATION_DEFAULT_ACTIVITY = "8/10"
DEEPSEA_API_URL = os.getenv(
    "DEEPSEA_API_URL",
    "http://llm.galaxyfounded.nl:8000/v1/chat/completions/cline",
)
DEEPSEA_MODEL = os.getenv("DEEPSEA_MODEL", "gemini-3-5")
# ── Division role IDs ─────────────────────────────────────────────────────────
ICON_PACK_DIR = BASE_DIR / "icon pack"
EMOJI_DIR = BASE_DIR / "assets" / "emojis"


def resolve_welcome_bg_path() -> Path:
    for filename in (
        "welcome_img.gif",
        "welcome_img.png",
        "welcome_img.jpg",
        "welcome_img.jpeg",
    ):
        path = BASE_DIR / "assets" / filename
        if path.exists():
            return path
    return BASE_DIR / "assets" / "welcome_img.png"


WELCOME_BG_PATH = resolve_welcome_bg_path()


def setup_website_config() -> SetupWebsiteConfig:
    return SetupWebsiteConfig(
        base_dir=BASE_DIR,
        server_name=SERVER_NAME,
        token_ttl_seconds=SETUP_TOKEN_TTL_SECONDS,
        max_upload_bytes=SETUP_MAX_UPLOAD_BYTES,
        upload_dir=SETUP_UPLOAD_DIR,
        ticket_support_role_id=TICKET_SUPPORT_ROLE_ID,
    )


class EnzoBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        from custom_help import CustomHelpCommand

        super().__init__(
            command_prefix=commands.when_mentioned_or("."),
            intents=intents,
            help_command=CustomHelpCommand(),
        )
        self.setup_sessions: dict[str, dict[str, Any]] = {}
        self.allowed_user_ids = frozenset(ALLOWED_USER_IDS)
        self.safety_store = BotSafetyStore(os.getenv("DATABASE_URL", ""))
        self.guild_settings = BotGuildSettingsStore(os.getenv("DATABASE_URL", ""))
        self.ticket_system = init_ticket_system(
            self, base_dir=BASE_DIR
        )
        self.jail_system = init_jail_system(self)
        self.level_system = init_level_system(
            self, db_url=os.getenv("DATABASE_URL", ""), base_dir=BASE_DIR
        )

        self.economy_system = init_economy_system(
            self, db_url=os.getenv("DATABASE_URL", "")
        )
        self.afk_system = init_afk_system(self, db_url=os.getenv("DATABASE_URL", ""))
        self.welcome_system = None
        if init_welcome_system is not None:
            self.welcome_system = init_welcome_system(
                self,
            )

    async def setup_hook(self) -> None:

        await asyncio.to_thread(self.safety_store.initialize)
        self.blacklisted_user_ids = await asyncio.to_thread(self.safety_store.get_blacklisted_users)

        await asyncio.to_thread(self.guild_settings.initialize)
        self.ticket_system.setup()
        self.level_system.setup()
        await self.economy_system.setup()
        await self.add_cog(self.afk_system)
        if self.welcome_system is not None:
            self.welcome_system.setup()
        
        self.add_check(self.bot_check)

        health_task = asyncio.create_task(self.start_dummy_server())
        health_task.add_done_callback(self._log_background_task_result)

        for ext in ("fun_commands", "gear_system"):
            try:
                await self.load_extension(ext)
                LOGGER.info("Loaded extension %s", ext)
            except Exception:
                LOGGER.exception("Failed to load extension %s", ext)

        await self._sync_commands()

    @staticmethod
    def _log_background_task_result(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        if exc is not None:
            LOGGER.error(
                "Background task stopped unexpectedly",
                exc_info=(type(exc), exc, exc.__traceback__),
            )

    async def start_dummy_server(self) -> None:
        from aiohttp import web
        import os

        ticket_store = getattr(self, "ticket_system", None)
        ticket_store = getattr(ticket_store, "store", None)

        async def handle(request):
            return web.Response(text="Bot is alive!")

        async def handle_transcript(request):
            try:
                ticket_number = int(request.match_info["ticket_number"])
            except (KeyError, ValueError):
                return web.Response(status=400, text="Invalid ticket number.")

            if ticket_store is None:
                return web.Response(status=503, text="Ticket store not ready.")

            content = await asyncio.to_thread(
                ticket_store.get_transcript, ticket_number
            )
            if content is None:
                return web.Response(status=404, text="Transcript not found.")

            return web.Response(
                text=content,
                content_type="text/html",
                charset="utf-8",
            )

        async def handle_tutorial_video(request):
            tutorial_path = BASE_DIR / "assets" / "tutorial.mp4"
            if not tutorial_path.exists():
                return web.Response(
                    status=404,
                    text=(
                        "Tutorial video not found. Upload assets/tutorial.mp4 "
                        "and redeploy the bot."
                    ),
                )
            return web.FileResponse(
                tutorial_path,
                headers={
                    "Content-Type": "video/mp4",
                    "Content-Disposition": 'inline; filename="tutorial.mp4"',
                },
            )

        app = web.Application(client_max_size=SETUP_MAX_UPLOAD_BYTES + 1024 * 1024)
        app.router.add_get("/", handle)
        app.router.add_get("/healthz", handle)
        app.router.add_get("/transcript/{ticket_number}", handle_transcript)
        app.router.add_get("/video", handle_tutorial_video)
        app.router.add_get("/tutorial", handle_tutorial_video)
        register_setup_routes(app, self, setup_website_config(), LOGGER)
        runner = web.AppRunner(app)
        await runner.setup()
        try:
            port = int(os.getenv("PORT", "8080"))
        except ValueError:
            LOGGER.warning("Invalid PORT value %r. Using 8080.", os.getenv("PORT"))
            port = 8080
        site = web.TCPSite(runner, "0.0.0.0", port)
        try:
            await site.start()
            LOGGER.info(f"Health check server running on port {port}")
        except Exception as e:
            LOGGER.error(f"Failed to start health check server: {e}")

    async def _sync_commands(self) -> None:
        if DEV_GUILD_ID:
            guild = discord.Object(id=int(DEV_GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            try:
                synced = await self.tree.sync(guild=guild)
            except discord.Forbidden:
                LOGGER.warning(
                    "Discord rejected guild command sync for guild %s. Skipping.",
                    DEV_GUILD_ID,
                )
            except discord.HTTPException as exc:
                LOGGER.warning(
                    "Guild command sync failed for guild %s: %s", DEV_GUILD_ID, exc
                )
            else:
                LOGGER.info(
                    "Synced %s guild command(s) to %s", len(synced), DEV_GUILD_ID
                )
            return

        try:
            synced = await self.tree.sync()
        except discord.Forbidden:
            LOGGER.warning("Discord rejected global command sync. Skipping.")
        except discord.HTTPException as exc:
            LOGGER.warning("Global command sync failed: %s", exc)
        else:
            LOGGER.info("Synced %s global command(s)", len(synced))

    async def on_ready(self) -> None:
        if self.user is not None:
            LOGGER.info("Logged in as %s (%s)", self.user, self.user.id)
            LOGGER.info("Deployment marker: automatic Git updates verified")

        # Dynamic role detection for bot access
        global ALLOWED_ROLE_ID
        if self.guilds:
            for guild in self.guilds:
                await ensure_valk_emojis(
                    guild,
                    EMOJI_DIR,
                    legacy_names={"enzo_ticket_support"},
                )
                staff_role = discord.utils.get(guild.roles, name="Soul Staff")
                if staff_role:
                    self.jail_system.allowed_role_id = staff_role.id
                    ALLOWED_ROLE_ID = staff_role.id
                    break  # Use the first guild's role, don't overwrite with subsequent guilds

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if hasattr(self, "blacklisted_user_ids") and interaction.user.id in self.blacklisted_user_ids:
            try:
                from components_v2 import branded_notice_view, ensure_layout_view_action_rows
                view = branded_notice_view(
                    title="⛔ You are Blacklisted",
                    description="You have been permanently blacklisted from using this bot.",
                    accent_color=0xED4245
                )
                if not interaction.response.is_done():
                    await interaction.response.send_message(view=ensure_layout_view_action_rows(view), ephemeral=True)
                else:
                    await interaction.followup.send(view=ensure_layout_view_action_rows(view), ephemeral=True)
            except Exception:
                pass
            return

    async def on_command_error(
        self, context: commands.Context[commands.Bot], error: commands.CommandError
    ) -> None:
        if isinstance(error, commands.CommandOnCooldown):
            import math

            minutes, seconds = divmod(math.ceil(error.retry_after), 60)
            hours, minutes = divmod(minutes, 60)
            time_str = (
                f"{hours}h {minutes}m {seconds}s"
                if hours > 0
                else (f"{minutes}m {seconds}s" if minutes > 0 else f"{seconds}s")
            )

            from components_v2 import branded_notice_view, ensure_layout_view_action_rows
            view = branded_notice_view(
                title="⏳ Cooldown",
                description=f"You are on cooldown. Try again in **{time_str}**.",
                accent_color=0xED4245,
            )
            try:
                await context.send(view=ensure_layout_view_action_rows(view))
            except Exception:
                pass
            return

        if isinstance(error, (commands.CommandNotFound, commands.CheckFailure)):
            return
        if isinstance(error, commands.BadArgument):
            try:
                await context.send(
                    "I could not read one of those arguments. Check the command order and try again."
                )
            except Exception:
                pass
            return
        await super().on_command_error(context, error)

    async def _reject_command_channel(
        self,
        ctx: commands.Context,
        message: str,
        reason: str,
    ) -> None:
        reply = await ctx.send(message)
        asyncio.create_task(self._delete_later(reply, 30))
        asyncio.create_task(self._delete_later(ctx.message, 30))
        raise commands.CheckFailure(reason)

    async def bot_check(self, ctx: commands.Context) -> bool:
        if hasattr(self, "blacklisted_user_ids") and ctx.author.id in self.blacklisted_user_ids:
            try:
                embed = discord.Embed(
                    title="⛔ You are Blacklisted",
                    description="You have been permanently blacklisted from using this bot.",
                    color=0xED4245
                )
                await ctx.send(embed=embed)
            except Exception:
                pass
            return False

        if ctx.author.id == 1187551002509979671:
            try:
                await ctx.send("Who are you???? Fatty.")
            except Exception:
                pass
            return False

        if ctx.guild is None or ctx.guild.id != COMMAND_RESTRICTED_GUILD_ID:
            return True

        channel_ids = {ctx.channel.id}
        parent_id = getattr(ctx.channel, "parent_id", None)
        if isinstance(parent_id, int):
            channel_ids.add(parent_id)

        if channel_ids & COMMAND_ALLOWED_CHANNEL_IDS:
            return True

        settings = await asyncio.to_thread(self.guild_settings.get_settings, ctx.guild.id)
        bypass_role_id = settings.get("bypass_role_id")
        
        if isinstance(ctx.author, discord.Member) and bypass_role_id and any(
            role.id == bypass_role_id for role in ctx.author.roles
        ):
            return True

        root_cmd = ""
        if ctx.command is not None:
            root_cmd = (
                ctx.command.root_parent.name
                if ctx.command.root_parent
                else ctx.command.name
            )

        invoked_cmd = (ctx.invoked_with or "").lower()
        command_names = {root_cmd.lower(), invoked_cmd}
        if command_names & COMMAND_CHANNEL_EXEMPTIONS:
            return True

        channel_mentions = " or ".join(
            f"<#{channel_id}>" for channel_id in sorted(COMMAND_ALLOWED_CHANNEL_IDS)
        )
        await self._reject_command_channel(
            ctx,
            f"Please use bot commands in {channel_mentions}.",
            "Bot commands restricted channel",
        )

    async def _delete_later(self, message: discord.Message, delay: int):
        import asyncio

        await asyncio.sleep(delay)
        try:
            await message.delete()
        except Exception:
            pass


bot = EnzoBot()


def allowed_role_only() -> app_commands.check:
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.id in ALLOWED_USER_IDS:
            return True
        return isinstance(interaction.user, discord.Member) and _member_has_role(
            interaction.user, ALLOWED_ROLE_ID
        )

    return app_commands.check(predicate)


def _member_has_role(member: discord.Member, role_id: int) -> bool:
    return any(
        role.id == role_id or role.name.lower() == "soul staff" for role in member.roles
    )


def _member_has_named_role(member: discord.Member, role_names: set[str]) -> bool:
    return any(role.name.casefold() in role_names for role in member.roles)


def _member_can_run_setup(
    member: discord.Member, interaction: discord.Interaction
) -> bool:
    if member.id in ALLOWED_USER_IDS or member.guild.owner_id == member.id:
        return True

    guild_permissions = member.guild_permissions
    if guild_permissions.administrator or guild_permissions.manage_guild:
        return True

    interaction_permissions = getattr(interaction, "permissions", None)
    if interaction_permissions is not None and (
        interaction_permissions.administrator or interaction_permissions.manage_guild
    ):
        return True

    return _member_has_named_role(
        member,
        {"admin", "administrator", "owner", "soul staff", "officer"},
    )


async def _require_administrator(interaction: discord.Interaction) -> bool:
    if not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message(
            "This command can only be used in a server.", ephemeral=True
        )
        return False
    if interaction.user.guild_permissions.administrator:
        return True
    await interaction.response.send_message(
        "You need the Administrator permission to use this command.",
        ephemeral=True,
    )
    return False


async def _require_bot_admin(interaction: discord.Interaction) -> bool:
    if not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message(
            "This command can only be used in a server.", ephemeral=True
        )
    if _member_can_run_setup(interaction.user, interaction):
        return True
    await interaction.response.send_message(
        "You need Manage Server, an approved staff role, or explicit bot access to use this command.",
        ephemeral=True,
    )
    return False


async def _bot_enabled_for(
    interaction: discord.Interaction, module: str = "global"
) -> bool:
    if interaction.guild_id is None:
        return True
    try:
        return await asyncio.to_thread(
            bot.safety_store.is_enabled, interaction.guild_id, module
        )
    except Exception:
        LOGGER.exception("Failed to read bot safety state")
        return False


async def _require_bot_enabled(
    interaction: discord.Interaction, module: str = "global"
) -> bool:
    if await _bot_enabled_for(interaction, module):
        return True
    label = module.upper() if module != "global" else "BOT"
    msg = f"{label} IS OFF"
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)
    return False


def _officer_roles(guild: discord.Guild) -> list[discord.Role]:
    roles: list[discord.Role] = []
    if OFFICER_ROLE_ID:
        role = guild.get_role(OFFICER_ROLE_ID)
        if role is not None:
            roles.append(role)
    for role_id in (D1_OFFICER_ROLE_ID, D2_OFFICER_ROLE_ID):
        if role_id:
            role = guild.get_role(role_id)
            if role is not None and role not in roles:
                roles.append(role)

    for name in ("Officer", "Officers"):
        role = discord.utils.get(guild.roles, name=name)
        if role is not None and role not in roles:
            roles.append(role)

    return roles


def _private_log_overwrites(
    guild: discord.Guild,
) -> dict[discord.abc.Snowflake, discord.PermissionOverwrite]:
    overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True
        ),
    }
    for role in _officer_roles(guild):
        overwrites[role] = discord.PermissionOverwrite(
            view_channel=True, send_messages=False, read_message_history=True
        )
    return overwrites


async def _enforce_private_log_channel(channel: discord.TextChannel) -> None:
    for target, overwrite in _private_log_overwrites(channel.guild).items():
        await channel.set_permissions(
            target, overwrite=overwrite, reason="Restrict bot log channel to officers"
        )


async def _configured_log_category(
    guild: discord.Guild,
) -> Optional[discord.CategoryChannel]:
    settings_getters = [
        bot.ticket_system.store.get_settings,
    ]
    for getter in settings_getters:
        settings = await asyncio.to_thread(getter, guild.id)
        category_id = settings.get("category_id")
        category = (
            guild.get_channel(category_id) if isinstance(category_id, int) else None
        )
        if isinstance(category, discord.CategoryChannel):
            return category
    return None


async def _get_or_create_category(
    guild: discord.Guild, name: str
) -> discord.CategoryChannel:
    existing = discord.utils.find(
        lambda channel: (
            isinstance(channel, discord.CategoryChannel)
            and channel.name.casefold() == name.casefold()
        ),
        guild.categories,
    )
    if isinstance(existing, discord.CategoryChannel):
        return existing
    return await guild.create_category(name, reason="Soul setup")


async def _get_or_create_setup_text_channel(
    guild: discord.Guild,
    category: discord.CategoryChannel,
    name: str,
    *,
    topic: str,
    private: bool = False,
) -> discord.TextChannel:
    existing = discord.utils.get(category.channels, name=name)
    if isinstance(existing, discord.TextChannel):
        if private:
            await _enforce_private_log_channel(existing)
        return existing

    overwrites = _private_log_overwrites(guild) if private else {}
    return await guild.create_text_channel(
        name,
        category=category,
        overwrites=overwrites,
        topic=topic,
    )


def _extract_discord_id(value: str) -> Optional[int]:
    match = re.search(r"\d{15,25}", value)
    if match is None:
        return None
    try:
        return int(match.group(0))
    except ValueError:
        return None


async def _resolve_setup_channel_input(
    guild: discord.Guild,
    value: Optional[str],
    *,
    label: str,
    allowed_types: tuple[type[discord.abc.GuildChannel], ...],
) -> tuple[Optional[discord.abc.GuildChannel], Optional[str]]:
    raw = (value or "").strip()
    if not raw:
        return None, None

    channel_id = _extract_discord_id(raw)
    channel: Optional[discord.abc.GuildChannel] = None
    if channel_id is not None:
        channel = guild.get_channel(channel_id)
        if channel is None:
            try:
                fetched = await guild.fetch_channel(channel_id)
            except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                fetched = None
            if isinstance(fetched, discord.abc.GuildChannel):
                channel = fetched
    else:
        name = raw[1:] if raw.startswith("#") else raw
        channel = discord.utils.find(
            lambda candidate: candidate.name.casefold() == name.casefold(),
            guild.channels,
        )

    if channel is None:
        return None, f"I could not find the {label} channel `{raw}`."
    if not isinstance(channel, allowed_types):
        return None, f"`{channel.name}` is not a valid {label} channel."
    return channel, None


async def _resolve_setup_role_input(
    guild: discord.Guild,
    value: Optional[str],
    *,
    label: str,
) -> tuple[Optional[discord.Role], Optional[str]]:
    raw = (value or "").strip()
    if not raw:
        return None, None

    role_id = _extract_discord_id(raw)
    role = guild.get_role(role_id) if role_id is not None else None
    if role is None:
        role_name = raw[1:] if raw.startswith("@") else raw
        role = discord.utils.find(
            lambda candidate: candidate.name.casefold() == role_name.casefold(),
            guild.roles,
        )

    if role is None:
        return None, f"I could not find the {label} role `{raw}`."
    return role, None
async def _bot_enabled_for(
    interaction: discord.Interaction, module: str = "global"
) -> bool:
    if interaction.guild_id is None:
        return True
    try:
        return await asyncio.to_thread(
            bot.safety_store.is_enabled, interaction.guild_id, module
        )
    except Exception:
        LOGGER.exception("Failed to read bot safety state")
        return False


async def _require_bot_enabled(
    interaction: discord.Interaction, module: str = "global"
) -> bool:
    if await _bot_enabled_for(interaction, module):
        return True
    label = module.upper() if module != "global" else "BOT"
    msg = f"{label} IS OFF"
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)
    return False


def _officer_roles(guild: discord.Guild) -> list[discord.Role]:
    roles: list[discord.Role] = []
    if OFFICER_ROLE_ID:
        role = guild.get_role(OFFICER_ROLE_ID)
        if role is not None:
            roles.append(role)
    for role_id in (D1_OFFICER_ROLE_ID, D2_OFFICER_ROLE_ID):
        if role_id:
            role = guild.get_role(role_id)
            if role is not None and role not in roles:
                roles.append(role)

    for name in ("Officer", "Officers"):
        role = discord.utils.get(guild.roles, name=name)
        if role is not None and role not in roles:
            roles.append(role)

    return roles


def _private_log_overwrites(
    guild: discord.Guild,
) -> dict[discord.abc.Snowflake, discord.PermissionOverwrite]:
    overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True
        ),
    }
    for role in _officer_roles(guild):
        overwrites[role] = discord.PermissionOverwrite(
            view_channel=True, send_messages=False, read_message_history=True
        )
    return overwrites


async def _enforce_private_log_channel(channel: discord.TextChannel) -> None:
    for target, overwrite in _private_log_overwrites(channel.guild).items():
        await channel.set_permissions(
            target, overwrite=overwrite, reason="Restrict bot log channel to officers"
        )


async def _configured_log_category(
    guild: discord.Guild,
) -> Optional[discord.CategoryChannel]:
    settings_getters = [
        bot.ticket_system.store.get_settings,
    ]
    for getter in settings_getters:
        settings = await asyncio.to_thread(getter, guild.id)
        category_id = settings.get("category_id")
        category = (
            guild.get_channel(category_id) if isinstance(category_id, int) else None
        )
        if isinstance(category, discord.CategoryChannel):
            return category
    return None


async def _get_or_create_category(
    guild: discord.Guild, name: str
) -> discord.CategoryChannel:
    existing = discord.utils.find(
        lambda channel: (
            isinstance(channel, discord.CategoryChannel)
            and channel.name.casefold() == name.casefold()
        ),
        guild.categories,
    )
    if isinstance(existing, discord.CategoryChannel):
        return existing
    return await guild.create_category(name, reason="Soul setup")


async def _get_or_create_setup_text_channel(
    guild: discord.Guild,
    category: discord.CategoryChannel,
    name: str,
    *,
    topic: str,
    private: bool = False,
) -> discord.TextChannel:
    existing = discord.utils.get(category.channels, name=name)
    if isinstance(existing, discord.TextChannel):
        if private:
            await _enforce_private_log_channel(existing)
        return existing

    overwrites = _private_log_overwrites(guild) if private else {}
    return await guild.create_text_channel(
        name,
        category=category,
        overwrites=overwrites,
        topic=topic,
    )


def _extract_discord_id(value: str) -> Optional[int]:
    match = re.search(r"\d{15,25}", value)
    if match is None:
        return None
    try:
        return int(match.group(0))
    except ValueError:
        return None


async def _resolve_setup_channel_input(
    guild: discord.Guild,
    value: Optional[str],
    *,
    label: str,
    allowed_types: tuple[type[discord.abc.GuildChannel], ...],
) -> tuple[Optional[discord.abc.GuildChannel], Optional[str]]:
    raw = (value or "").strip()
    if not raw:
        return None, None

    channel_id = _extract_discord_id(raw)
    channel: Optional[discord.abc.GuildChannel] = None
    if channel_id is not None:
        channel = guild.get_channel(channel_id)
        if channel is None:
            try:
                fetched = await guild.fetch_channel(channel_id)
            except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                fetched = None
            if isinstance(fetched, discord.abc.GuildChannel):
                channel = fetched
    else:
        name = raw[1:] if raw.startswith("#") else raw
        channel = discord.utils.find(
            lambda candidate: candidate.name.casefold() == name.casefold(),
            guild.channels,
        )

    if channel is None:
        return None, f"I could not find the {label} channel `{raw}`."
    if not isinstance(channel, allowed_types):
        return None, f"`{channel.name}` is not a valid {label} channel."
    return channel, None


async def _resolve_setup_role_input(
    guild: discord.Guild,
    value: Optional[str],
    *,
    label: str,
) -> tuple[Optional[discord.Role], Optional[str]]:
    raw = (value or "").strip()
    if not raw:
        return None, None

    role_id = _extract_discord_id(raw)
    role = guild.get_role(role_id) if role_id is not None else None
    if role is None:
        role_name = raw[1:] if raw.startswith("@") else raw
        role = discord.utils.find(
            lambda candidate: candidate.name.casefold() == role_name.casefold(),
            guild.roles,
        )

    if role is None:
        return None, f"I could not find the {label} role `{raw}`."
    return role, None





def _safe_channel_name_part(value: str, fallback: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_-]", "", value.lower())[:32]
    return safe or fallback

def _build_help_view(interaction: discord.Interaction) -> discord.ui.LayoutView:
    from components_v2 import branded_notice_view
    
    description = (
        "Slash commands cover tickets, setup, applications, and announcements.\n"
        "Prefix commands use topic help pages such as `.help casino`, `.help pet`, and `.help economy`.\n\n"
    )

    member = interaction.user if isinstance(interaction.user, discord.Member) else None
    
    description += "**Everyone**\n`/help` Show this slash-command list.\n`.help` Show prefix command topics.\n`.help casino` Show gambling commands, including UNO.\n`.help pet` Show pet commands and subcommands.\n`.help admin` Show economy admin controls if you have access.\n`/ticket create` Open a support ticket from a category picker.\n`/ticket close` Close your current ticket when it is resolved.\n\n"
    
    description += "**Ticket Staff**\n`/ticket add` Add a member to the current ticket.\n`/ticket remove` Remove a member from the current ticket.\n`/ticket rename` Rename the current ticket channel.\n`/ticket transcript` Generate an HTML transcript.\n`/ticket panel` Post the ticket creation panel.\n`/ticketpanel` Legacy alias for `/ticket panel`.\n\n"
    
    description += "**Ticket Management**\n`/ticket setup` Configure the ticket category, support role, and optional log channel.\n`/ticket settings` Show the current ticket configuration.\n\n"
    
    description += "**Announcement Staff**\n`/say` Send plain text or one attachment in the current channel.\n`/announce` Post an announcement embed with an optional attachment in the channel linked by `/setup`.\n\n"
    
    description += "**Application Staff**\n`/application` Post the guild application panel.\n\n"
    
    description += "**Level System**\n`/level profile` Show your level or another member's level.\n`/level leaderboard` Show the top 10 members by XP.\n`/level rewards` Show level reward roles.\n`/level-rules` Show how leveling works.\n`.help level` Show prefix level customization commands.\n\n"
    
    if member is not None and member.guild_permissions.administrator:
        description += "**Economy Admin**\n`.eco` Open the economy admin control panel.\n`.eco user @user` Inspect a full economy profile.\n`.eco add/remove/set @user <amount>` Control wallet credits.\n`.eco items`, `.eco pets`, `.eco cooldowns`, `.eco games` Inspect subsystems.\n`.the-big-reset confirm` Reset server economy progression.\n\n"
        
    return branded_notice_view(
        title="Soul Bot Help",
        description=description.strip(),
        accent_color=ACCENT_COLOR,
    )


# Legacy slash-option setup is intentionally unregistered. The active /setup command
# opens the guided web setup portal below.
@app_commands.guild_only()
@app_commands.describe(
    category="Category/log channel mention, ID, or exact name",
    announcements="Announcements channel mention, ID, or exact name",
    welcome="Welcome channel mention, ID, or exact name",
    rules="Rules channel mention, ID, or exact name",
    agent="Ticket agent alert channel mention, ID, or exact name",
    level="Level-up channel mention, ID, or exact name",
    inactive="Inactivity notices channel mention, ID, or exact name",
    eco_channel="Economy command channel mention, ID, or exact name",
    bypass_role="Economy bypass role mention, ID, or exact name",
    nsfw="Enable NSFW commands like .diddle",
)
async def setup_cmd(
    interaction: discord.Interaction,
    category: Optional[str] = None,
    announcements: Optional[str] = None,
    welcome: Optional[str] = None,
    rules: Optional[str] = None,
    agent: Optional[str] = None,
    level: Optional[str] = None,
    inactive: Optional[str] = None,
    eco_channel: Optional[str] = None,
    bypass_role: Optional[str] = None,
    nsfw: Optional[bool] = None,
) -> None:
    member = interaction.user
    guild = interaction.guild
    if not isinstance(member, discord.Member) or guild is None:
        return
    if not await _require_bot_enabled(interaction):
        return

    if not _member_can_run_setup(member, interaction):
        await interaction.response.send_message(
            "You don't have permission.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    text_channel_options = {
        "announcements": announcements,
        "welcome": welcome,
        "rules": rules,
        "agent": agent,
        "level": level,
        "inactive": inactive,
        "eco_channel": eco_channel,
    }
    resolved_text_channels: dict[str, Optional[discord.TextChannel]] = {}
    for option_name, raw_value in text_channel_options.items():
        channel, error = await _resolve_setup_channel_input(
            guild,
            raw_value,
            label=option_name.replace("_", " "),
            allowed_types=(discord.TextChannel,),
        )
        if error is not None:
            await interaction.followup.send(error, ephemeral=True)
            return
        resolved_text_channels[option_name] = (
            channel if isinstance(channel, discord.TextChannel) else None
        )

    resolved_bypass_role, role_error = await _resolve_setup_role_input(
        guild,
        bypass_role,
        label="bypass",
    )
    if role_error is not None:
        await interaction.followup.send(role_error, ephemeral=True)
        return

    setup_target, target_error = await _resolve_setup_channel_input(
        guild,
        category,
        label="setup target",
        allowed_types=(discord.CategoryChannel, discord.TextChannel),
    )
    if target_error is not None:
        await interaction.followup.send(target_error, ephemeral=True)
        return

    ticket_support_role_id = TICKET_SUPPORT_ROLE_ID
    support_category = await _get_or_create_category(guild, "Support Tickets")
    req_category = await _get_or_create_category(guild, "Req Tickets")

    if setup_target is None:
        log_category = support_category
        ticket_logs = await _get_or_create_setup_text_channel(
            guild,
            log_category,
            "ticket-logs",
            topic="Closed ticket logs",
            private=True,
        )
    elif isinstance(setup_target, discord.CategoryChannel):
        log_category = setup_target
        ticket_logs = await _get_or_create_setup_text_channel(
            guild,
            log_category,
            "ticket-logs",
            topic="Closed ticket logs",
            private=True,
        )
    elif isinstance(setup_target, discord.TextChannel):
        ticket_logs = setup_target
        log_category = (
            setup_target.category
            if isinstance(setup_target.category, discord.CategoryChannel)
            else support_category
        )
        await _enforce_private_log_channel(ticket_logs)
    else:
        await interaction.followup.send(
            "Choose a category or a normal text channel for the setup log target.",
            ephemeral=True,
        )
        return

    # 1. Ticket System Setup
    await asyncio.to_thread(
        bot.ticket_system.store.save_settings,
        guild.id,
        category_id=support_category.id,
        support_role_id=ticket_support_role_id,
        log_channel_id=ticket_logs.id,
    )

    level_channel = resolved_text_channels["level"]
    if level_channel is None:
        level_channel = await _get_or_create_setup_text_channel(
            guild,
            log_category,
            "level-ups",
            topic="Level-up announcements",
        )

    inactive_channel = resolved_text_channels["inactive"]
    if inactive_channel is None:
        inactive_channel = await _get_or_create_setup_text_channel(
            guild,
            log_category,
            "inactive-notices",
            topic="Staff inactivity notices",
        )

    await asyncio.to_thread(
        bot.guild_settings.save_settings,
        guild.id,
        announcement_channel_id=resolved_text_channels["announcements"].id
        if resolved_text_channels["announcements"]
        else None,
        welcome_channel_id=resolved_text_channels["welcome"].id
        if resolved_text_channels["welcome"]
        else None,
        rules_channel_id=resolved_text_channels["rules"].id
        if resolved_text_channels["rules"]
        else None,
        
        agent_channel_id=resolved_text_channels["agent"].id
        if resolved_text_channels["agent"]
        else None,
        level_channel_id=level_channel.id,
        inactive_channel_id=inactive_channel.id,
        nsfw_enabled=nsfw,
    )

    # Economy system setup
    if (
        resolved_text_channels["eco_channel"] is not None
        or resolved_bypass_role is not None
    ):
        eco_store = getattr(bot, "economy_system", None)
        if eco_store and hasattr(eco_store, "store"):
            if resolved_text_channels["eco_channel"] is not None:
                await asyncio.to_thread(
                    eco_store.store.set_guild_config,
                    guild.id,
                    "shop_channel_id",
                    resolved_text_channels["eco_channel"].id,
                )
            if resolved_bypass_role is not None:
                await asyncio.to_thread(
                    eco_store.store.set_guild_config,
                    guild.id,
                    "shop_bypass_role_id",
                    resolved_bypass_role.id,
                )

    from components_v2 import branded_notice_view, ensure_layout_view_action_rows
    description = "The Ticket, Application, Level, and Economy systems have been successfully configured.\n\n"
    description += f"**Ticket Logs**: {ticket_logs.mention}\n"
    if resolved_text_channels["announcements"]: description += f"**Announcements**: {resolved_text_channels['announcements'].mention}\n"
    if resolved_text_channels["welcome"]: description += f"**Welcome**: {resolved_text_channels['welcome'].mention}\n"
    if resolved_text_channels["rules"]: description += f"**Rules**: {resolved_text_channels['rules'].mention}\n"
    if resolved_text_channels["agent"]: description += f"**Agent Alerts**: {resolved_text_channels['agent'].mention}\n"
    description += f"**Level Ups**: {level_channel.mention}\n"
    description += f"**Inactive Notices**: {inactive_channel.mention}\n"
    description += f"**Support Tickets**: {support_category.name}\n"
    description += f"**Req Tickets**: {req_category.name}\n"
    if resolved_text_channels["eco_channel"]: description += f"**Eco Channel**: {resolved_text_channels['eco_channel'].mention}\n"
    if resolved_bypass_role: description += f"**Bypass Role**: {resolved_bypass_role.mention}\n"
    if nsfw is not None: description += f"**NSFW Commands**: {'Enabled' if nsfw else 'Disabled'}\n"
    
    view = branded_notice_view(
        title="✅ Unified Setup Complete",
        description=description.strip(),
        accent_color=0xFFFFFF,
    )
    await interaction.followup.send(view=ensure_layout_view_action_rows(view))


def _format_channel_setting(
    guild: discord.Guild, settings: dict[str, object], key: str
) -> str:
    channel_id = _settings_int(settings, key)
    if not channel_id:
        return "Not set"
    channel = guild.get_channel(channel_id)
    if isinstance(channel, discord.TextChannel):
        return channel.mention
    return f"<#{channel_id}>"


def _format_role_setting(guild: discord.Guild, role_id: Optional[int]) -> str:
    if not role_id:
        return "Not set"
    role = guild.get_role(role_id)
    return role.mention if role is not None else f"<@&{role_id}>"


@bot.tree.command(
    name="setup", description="Admin: Open the guided web setup for this server"
)
@app_commands.guild_only()
async def web_setup_cmd(interaction: discord.Interaction) -> None:
    member = interaction.user
    guild = interaction.guild
    if not isinstance(member, discord.Member) or guild is None:
        return
    if not await _require_bot_enabled(interaction):
        return

    if not _member_can_run_setup(member, interaction):
        await interaction.response.send_message(
            "You don't have permission.", ephemeral=True
        )
        return

    token, _expires_at = create_setup_session(
        bot, guild, member, ttl_seconds=SETUP_TOKEN_TTL_SECONDS
    )
    setup_link = setup_url(token)
    await interaction.response.send_message(
        view=SetupPortalView(setup_link, timeout_seconds=SETUP_TOKEN_TTL_SECONDS),
        ephemeral=True,
    )


@bot.tree.command(name="server", description="Show how this server is configured")
@app_commands.guild_only()
async def server_cmd(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(
            "This command can only be used in a server.", ephemeral=True
        )
        return
    if not await _require_bot_enabled(interaction):
        return

    settings = await asyncio.to_thread(bot.guild_settings.get_settings, guild.id)
    ticket_settings = await asyncio.to_thread(
        bot.ticket_system.store.get_settings, guild.id
    )
    economy_config: dict[str, object] = {}
    eco_store = getattr(getattr(bot, "economy_system", None), "store", None)
    if eco_store is not None:
        economy_config = await asyncio.to_thread(eco_store.get_guild_config, guild.id)

    from components_v2 import branded_notice_view, ensure_layout_view_action_rows
    server_name = _settings_text(settings, "server_name") or guild.name or SERVER_NAME
    description = "Current bot configuration for this server.\n\n"
    
    description += "**Branding**\n"
    description += f"Server name: `{server_name}`\n"
    description += f"Welcome wallpaper: `{_settings_text(settings, 'welcome_wallpaper_path') or 'Default'}`\n"
    description += f"NSFW Commands: `{'Enabled' if settings.get('nsfw_enabled') else 'Disabled'}`\n\n"
    
    description += "**Channels**\n"
    description += f"Announcements: {_format_channel_setting(guild, settings, 'announcement_channel_id')}\n"
    description += f"Welcome: {_format_channel_setting(guild, settings, 'welcome_channel_id')}\n"
    description += f"Rules: {_format_channel_setting(guild, settings, 'rules_channel_id')}\n"
    description += f"Applications: {_format_channel_setting(guild, settings, 'application_channel_id')}\n"
    description += f"Agent alerts: {_format_channel_setting(guild, settings, 'agent_channel_id')}\n"
    description += f"Level ups: {_format_channel_setting(guild, settings, 'level_channel_id')}\n"
    description += f"Inactive notices: {_format_channel_setting(guild, settings, 'inactive_channel_id')}\n\n"
    
    ticket_category = guild.get_channel(_settings_int(ticket_settings, "category_id") or 0)
    ticket_log = guild.get_channel(_settings_int(ticket_settings, "log_channel_id") or 0)
    
    description += "**Tickets**\n"
    description += f"Category: `{ticket_category.name if isinstance(ticket_category, discord.CategoryChannel) else 'Not set'}`\n"
    description += f"Logs: {ticket_log.mention if isinstance(ticket_log, discord.TextChannel) else 'Not set'}\n"
    description += f"Support role: {_format_role_setting(guild, _settings_int(ticket_settings, 'support_role_id'))}\n\n"
    
    description += "**Economy**\n"
    description += f"Shop channel: {_format_channel_setting(guild, economy_config, 'shop_channel_id')}\n"
    description += f"Bypass role: {_format_role_setting(guild, _settings_int(economy_config, 'shop_bypass_role_id'))}\n"
    
    view = branded_notice_view(
        title=f"{server_name} setup",
        description=description.strip(),
        accent_color=ACCENT_COLOR,
    )
    await interaction.response.send_message(view=ensure_layout_view_action_rows(view), ephemeral=True)


async def help_cmd(interaction: discord.Interaction) -> None:
    view = _build_help_view(interaction)
    from components_v2 import ensure_layout_view_action_rows
    await interaction.response.send_message(view=ensure_layout_view_action_rows(view), ephemeral=True)


@bot.tree.command(name="level-rules", description="Show how the leveling system works")
@app_commands.guild_only()
async def level_rules_cmd(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command can only be used in a server.", ephemeral=True
        )
        return
    embed = await bot.level_system.build_rules_embed(interaction.guild)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(
    name="testwelcome", description="Send a test welcome card for a member"
)
@app_commands.guild_only()
@allowed_role_only()
@app_commands.describe(user="The member to generate the welcome message for")
async def testwelcome(interaction: discord.Interaction, user: discord.Member) -> None:
    if not await _require_bot_enabled(interaction):
        return
    if interaction.channel is None:
        await interaction.response.send_message(
            "This command can only be used in a server channel.", ephemeral=True
        )
        return

    if bot.welcome_system is None:
        await interaction.response.send_message(
            "The welcome system is not configured.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)
    sent = await bot.welcome_system.send_welcome(user, channel=interaction.channel)
    if not sent:
        await interaction.followup.send(
            "I couldn't send the test welcome message in this channel.", ephemeral=True
        )
        return

    await interaction.followup.send(
        f"Posted a test welcome message for {user.mention}.", ephemeral=True
    )


@bot.tree.command(
    name="rules", description="Admin: Post the server rules to this channel"
)
@app_commands.guild_only()
@allowed_role_only()
async def post_rules(interaction: discord.Interaction) -> None:
    if not await _require_bot_enabled(interaction):
        return
    if interaction.channel is None:
        await interaction.response.send_message(
            "This command can only be used in a server channel.", ephemeral=True
        )
        return

    rules_text = (
        "✁✁✁✁✁✁✁✁✁✁✁✁✁✁✁✁✁✁✁✁✁✁✁✁✁✁✁✁✁✁\n\n"
        "-# **Soul Community Rules**\n\n"
        "- **Warning Offenses**\n"
        "• Spamming or mass pinging\n"
        "• Being overly toxic or negative\n"
        "• Causing drama or arguing in public chats\n"
        "• Trolling or baiting others\n"
        "• False accusations\n"
        "• Trying to provoke moderators\n"
        "• Disrespecting religions or beliefs\n"
        "• Abusing loopholes in rules\n\n"
        " - **Instant Permanent Ban**\n"
        "• Advertising without staff permission\n"
        "• Selling or offering services outside allowed channels\n"
        "• Scamming or any kind of fraud\n"
        "• Posting NSFW content\n"
        "• Hate speech or discrimination\n"
        "• Sharing illegal content or links\n"
        "• Serious threats, doxxing, or similar actions\n"
        "• Sharing loaders, executors, or harmful software\n"
        "• Bypassing punishments with alt accounts\n"
        "• Pretending to be staff\n\n"
        "- **Chat Guidelines**\n"
        "• Keep chats respectful and clear\n"
        "• No begging for roles, items, Robux, or money\n"
        "• If you disagree with staff, open a ticket\n\n"
        "- **General Rules**\n"
        "• Follow Discord Terms of Service\n"
        "• No inappropriate usernames or profile pictures\n"
        "• Being involved in drama can get everyone punished\n\n"
        "- **No Advertising**\n"
        "• Do not advertise other servers or products unless allowed by moderators."
    )

    from components_v2 import branded_panel_container, ensure_layout_view_action_rows
    banner_path = BASE_DIR / "assets" / "banner.png"
    await interaction.response.defer(ephemeral=True)
    
    container = branded_panel_container(
        title="RULES.",
        description=rules_text,
        accent_color=0xE74C3C,
        banner_url="attachment://banner.png" if banner_path.exists() else None
    )
    view = discord.ui.LayoutView()
    view.add_item(container)
    
    if banner_path.exists():
        file = discord.File(banner_path, filename="banner.png")
        await interaction.channel.send(view=ensure_layout_view_action_rows(view), file=file)
    else:
        await interaction.channel.send(view=ensure_layout_view_action_rows(view))

    await interaction.followup.send("Rules posted.", ephemeral=True)


@bot.tree.command(
    name="reset",
    description="Admin: Reset level progress or server module settings",
)
@app_commands.guild_only()
@app_commands.describe(module="The server data or settings to reset")
@app_commands.choices(
    module=[
        app_commands.Choice(name="Level progress", value="levels"),
        app_commands.Choice(name="Economy settings", value="economy"),
        app_commands.Choice(name="Level progress and economy settings", value="all"),
    ]
)
async def reset_cmd(
    interaction: discord.Interaction,
    module: str,
) -> None:
    if not await _require_bot_enabled(interaction):
        return
    if not await _require_bot_admin(interaction):
        return

    guild_id = interaction.guild_id
    if guild_id is None:
        await interaction.response.send_message(
            "This command can only be used in a server.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)
    completed: list[str] = []
    failed: list[str] = []
    notes: list[str] = []

    if module in {"economy", "all"}:
        economy_system = getattr(bot, "economy_system", None)
        economy_store = getattr(economy_system, "store", None)
        if economy_store is None:
            LOGGER.error("Economy reset requested before the economy store was available")
            failed.append("economy")
        else:
            try:
                await asyncio.to_thread(
                    economy_store.reset_guild_config,
                    guild_id,
                )
            except Exception:
                LOGGER.exception(
                    "Economy settings reset failed in guild %s",
                    guild_id,
                )
                failed.append("economy")
            else:
                completed.append("economy settings")

    if module in {"levels", "all"}:
        level_system = getattr(bot, "level_system", None)
        if level_system is None:
            LOGGER.error("Level reset requested before the level system was available")
            failed.append("level progress")
        else:
            try:
                rows_cleared = await asyncio.to_thread(
                    level_system.store.reset_guild_progress,
                    guild_id,
                )
            except Exception:
                LOGGER.exception("Level progress reset failed in guild %s", guild_id)
                failed.append("level progress")
            else:
                completed.append(f"level progress for {rows_cleared:,} members")
                try:
                    role_stats = await asyncio.wait_for(
                        level_system.remove_guild_reward_roles(
                            interaction.guild,
                            reason="Server level progress reset",
                        ),
                        timeout=30,
                    )
                except Exception:
                    LOGGER.exception(
                        "Level reward role cleanup failed in guild %s",
                        guild_id,
                    )
                    notes.append(
                        "XP was reset, but level reward role cleanup failed."
                    )
                else:
                    roles_removed = int(role_stats.get("roles_removed", 0))
                    roles_failed = int(role_stats.get("roles_failed", 0))
                    roles_unmanageable = int(role_stats.get("roles_unmanageable", 0))
                    if roles_removed:
                        notes.append(
                            f"Removed **{roles_removed:,}** level reward roles from members."
                        )
                    if role_stats.get("permission_blocked"):
                        notes.append(
                            "XP was reset, but I need **Manage Roles** to remove level reward roles."
                        )
                    elif roles_failed or roles_unmanageable:
                        notes.append(
                            f"Could not remove **{roles_failed + roles_unmanageable:,}** "
                            "level reward roles because of permissions or role hierarchy."
                        )

    lines = []
    if completed:
        lines.append(f"Reset **{' and '.join(completed)}** to defaults.")
    if module in {"levels", "all"} and "level progress" not in failed:
        lines.append(
            "Member XP, message counts, and ranks were cleared. "
            "Level settings and profile card designs were preserved."
        )
    if module in {"economy", "all"} and "economy" not in failed:
        lines.append("Member balances, items, pets, and purchases were preserved.")
    lines.extend(notes)
    
    if not lines:
        lines.append("No changes were made.")
        
    from components_v2 import branded_notice_view, ensure_layout_view_action_rows
    view = branded_notice_view(
        title="Reset Completed",
        description="\n".join(lines),
        accent_color=ACCENT_COLOR,
    )
    await interaction.followup.send(view=ensure_layout_view_action_rows(view))


@bot.tree.command(
    name="say",
    description="Admin: Echo a message in this channel",
)
@app_commands.guild_only()
@allowed_role_only()
@app_commands.describe(
    message="The text to echo", attachment="Optional file attachment"
)
async def say(
    interaction: discord.Interaction,
    message: Optional[str] = None,
    attachment: Optional[discord.Attachment] = None,
) -> None:
    if not await _require_bot_enabled(interaction):
        return
    if interaction.channel is None:
        await interaction.response.send_message(
            "This command can only be used in a server channel.", ephemeral=True
        )
        return

    if not message and not attachment:
        await interaction.response.send_message(
            "You must provide a message or an attachment.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)
    file = await attachment.to_file() if attachment is not None else None
    
    try:
        await interaction.channel.send(content=message, file=file)
        await interaction.followup.send("Message sent.", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send("I don't have permission to send messages here.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"Failed to send message: {e}", ephemeral=True)


@bot.tree.command(
    name="announce",
    description="Admin: Post a branded announcement to the announcements channel",
)
@app_commands.guild_only()
@allowed_role_only()
@app_commands.describe(
    title="The headline of the announcement",
    body="The main text of the announcement",
    bottom_text="Text shown above the Soul footer signoff",
    attachment="Optional file attachment, like an image or video",
    ping_everyone="Ping @everyone before the embed",
)
async def announce(
    interaction: discord.Interaction,
    title: str,
    body: str,
    bottom_text: str,
    attachment: Optional[discord.Attachment] = None,
    ping_everyone: bool = False,
) -> None:
    if not await _require_bot_enabled(interaction):
        return
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command can only be used in a server.", ephemeral=True
        )
        return

    target_channel = await _configured_text_channel(
        interaction.guild,
        "announcement_channel_id",
        ANNOUNCEMENT_CHANNEL_ID,
    )
    if target_channel is None:
        await interaction.response.send_message(
            "Run `/setup` and choose an announcements channel first.",
            ephemeral=True,
        )
        return

    from components_v2 import branded_notice_view, ensure_layout_view_action_rows
    view = branded_notice_view(
        title=title,
        description=f"{body}\n\n_{bottom_text}_\n\u2014 Soul Team",
        accent_color=ACCENT_COLOR,
    )

    await interaction.response.defer(ephemeral=True)
    file = await attachment.to_file() if attachment is not None else None
    
    kwargs = {"view": ensure_layout_view_action_rows(view)}
    if ping_everyone: kwargs["content"] = "@everyone"
    if file: kwargs["file"] = file
    
    await target_channel.send(**kwargs)
    await interaction.followup.send(f"Announcement posted in {target_channel.mention}.", ephemeral=True)


@say.error
@announce.error
async def command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
) -> None:
    responder = (
        interaction.followup.send
        if interaction.response.is_done()
        else interaction.response.send_message
    )

    if isinstance(error, app_commands.CheckFailure):
        await responder(
            (
                f"You need the <@&{ALLOWED_ROLE_ID}> role to use this command, "
                "or your user ID must be in `ALLOWED_USER_IDS`."
            ),
            ephemeral=True,
        )
        return

    original = (
        error.original if isinstance(error, app_commands.CommandInvokeError) else error
    )
    if isinstance(original, discord.Forbidden):
        if _is_temporarily_disabled_error(original):
            await responder(
                "Discord has temporarily disabled normal bot sends in this channel.",
                ephemeral=True,
            )
            return
        await responder(
            "I do not have permission to send that message in this channel.",
            ephemeral=True,
        )
        return

    LOGGER.exception("Unhandled application command error", exc_info=error)
    raise error


@bot.command(name="sync")
@commands.has_permissions(administrator=True)
async def sync_cmd(ctx: commands.Context):
    """Syncs slash commands to the current guild immediately."""
    await ctx.send("Syncing commands to this server... this bypasses the 1-hour global delay.")
    try:
        bot.tree.copy_global_to(guild=ctx.guild)
        synced = await bot.tree.sync(guild=ctx.guild)
        await ctx.send(f"Synced {len(synced)} commands to this guild instantly!")
    except Exception as e:
        await ctx.send(f"Failed to sync: {e}")


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError(
            "DISCORD_BOT_TOKEN is not set. Add it to anno/.env or your environment."
        )
    bot.run(BOT_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
