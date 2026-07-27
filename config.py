"""
Bot Configuration
"""

import os
from dotenv import load_dotenv

load_dotenv(override=True)

def _parse_hex_color(value: str | None, default: int) -> int:
    if not value:
        return default
    s = value.strip()
    if s.startswith("#"):
        s = s[1:]
    if s.lower().startswith("0x"):
        s = s[2:]
    try:
        return int(s, 16)
    except Exception:
        return default


def _parse_int(value: str | None, default: int) -> int:
    if value is None:
        return default
    try:
        return int(str(value).strip())
    except Exception:
        return default


class Config:
    # Bot Token
    TOKEN = os.getenv('DISCORD_TOKEN')
    PREFIX = os.getenv('PREFIX', ',')
    
    # TTS - ElevenLabs (most realistic AI voices)
    ELEVENLABS_API_KEY = os.getenv('ELEVENLABS_API_KEY', '')
    
    # Global embed side color (set `EMBED_ACCENT_COLOR` to a hex like `#A020F0`)
    EMBED_ACCENT_COLOR = _parse_hex_color(os.getenv("EMBED_ACCENT_COLOR"), 0x5865F2)

    # Embed Colors (forced to the global accent color)
    COLOR_SUCCESS = EMBED_ACCENT_COLOR
    COLOR_ERROR = EMBED_ACCENT_COLOR
    COLOR_WARNING = EMBED_ACCENT_COLOR
    COLOR_INFO = EMBED_ACCENT_COLOR
    COLOR_MOD = EMBED_ACCENT_COLOR
    COLOR_BRAND = EMBED_ACCENT_COLOR
    COLOR_EMBED = EMBED_ACCENT_COLOR
    COLOR_PINK = EMBED_ACCENT_COLOR
    COLOR_GOLD = EMBED_ACCENT_COLOR
    COLOR_DARK_RED = EMBED_ACCENT_COLOR
    COLOR_ADMIN = EMBED_ACCENT_COLOR
    COLOR_OWNER = EMBED_ACCENT_COLOR

    # Log embed sizing (best-effort padding; 0 keeps embeds compact)
    LOG_EMBED_TARGET_LINES = int(os.getenv("LOG_EMBED_TARGET_LINES", "0"))

    # Welcome system
    # NOTE: In multi-server mode, per-guild settings in the database should be preferred.
    # These env vars act as a global fallback only.
    WELCOME_CHANNEL_ID = int(os.getenv("WELCOME_CHANNEL_ID", "0"))
    WELCOME_SERVER_NAME = os.getenv("WELCOME_SERVER_NAME", "")
    WELCOME_SYSTEM_NAME = os.getenv("WELCOME_SYSTEM_NAME", "Welcome System")
    # Welcome card accent (border/pill); set to a hex like `#D9D1B2`
    WELCOME_CARD_ACCENT_COLOR = _parse_hex_color(os.getenv("WELCOME_CARD_ACCENT_COLOR"), 0xD9D1B2)

    # Branding (Components v2 panels)
    SERVER_LOGO_URL = os.getenv(
        "SERVER_LOGO_URL",
        "",
    )
    SERVER_BANNER_URL = os.getenv(
        "SERVER_BANNER_URL",
        "https://images.unsplash.com/photo-1550751827-4bd374c3f58b",
    )

    # Verification
    VERIFY_TUTORIAL_VIDEO_URL = os.getenv(
        "VERIFY_TUTORIAL_VIDEO_URL",
        "https://example.com/verification-tutorial.mp4",
    )
    
    # Emojis (can be unicode or custom emoji string like <:check:123456789012345678>)
    # Note: Discord does not accept raw SVG as message/reaction emoji input.
    # Upload/custom-create emoji in a server first, then set the env var to the mention string.
    EMOJI_SUCCESS = os.getenv("EMOJI_SUCCESS", "\u2705")
    EMOJI_ERROR = os.getenv("EMOJI_ERROR", "\u274c")
    EMOJI_WARNING = os.getenv("EMOJI_WARNING", "\u26a0\ufe0f")
    EMOJI_INFO = os.getenv("EMOJI_INFO", "\u2139\ufe0f")
    EMOJI_LOADING = os.getenv("EMOJI_LOADING", "\u23f3")
    EMOJI_LOCK = os.getenv("EMOJI_LOCK", "\U0001f512")
    EMOJI_UNLOCK = os.getenv("EMOJI_UNLOCK", "\U0001f513")
    EMOJI_BAN = os.getenv("EMOJI_BAN", "\U0001f528")
    EMOJI_KICK = os.getenv("EMOJI_KICK", "\U0001f462")
    EMOJI_MUTE = os.getenv("EMOJI_MUTE", "\U0001f507")
    EMOJI_WARN = os.getenv("EMOJI_WARN", "\u26a0\ufe0f")

    # Auto-create per-guild custom status emojis from local assets when needed.
    AUTO_CREATE_STATUS_EMOJIS = str(os.getenv("AUTO_CREATE_STATUS_EMOJIS", "1")).strip().lower() in {"1", "true", "yes", "on"}
    STATUS_SUCCESS_EMOJI_NAME = os.getenv("STATUS_SUCCESS_EMOJI_NAME", "mod_success")
    STATUS_ERROR_EMOJI_NAME = os.getenv("STATUS_ERROR_EMOJI_NAME", "mod_error")
    STATUS_WARNING_EMOJI_NAME = os.getenv("STATUS_WARNING_EMOJI_NAME", "mod_warn")
    STATUS_INFO_EMOJI_NAME = os.getenv("STATUS_INFO_EMOJI_NAME", "mod_info")
    STATUS_LOCK_EMOJI_NAME = os.getenv("STATUS_LOCK_EMOJI_NAME", "mod_lock")
    STATUS_UNLOCK_EMOJI_NAME = os.getenv("STATUS_UNLOCK_EMOJI_NAME", "mod_unlock")
    STATUS_LOADING_EMOJI_NAME = os.getenv("STATUS_LOADING_EMOJI_NAME", "mod_loading")
    STATUS_BAN_EMOJI_NAME = os.getenv("STATUS_BAN_EMOJI_NAME", "mod_ban")
    STATUS_KICK_EMOJI_NAME = os.getenv("STATUS_KICK_EMOJI_NAME", "mod_kick")
    STATUS_MUTE_EMOJI_NAME = os.getenv("STATUS_MUTE_EMOJI_NAME", "mod_mute")
    STATUS_WARN_EMOJI_NAME = os.getenv("STATUS_WARN_EMOJI_NAME", "mod_warn")
    STATUS_EMOJI_CREATE_REASON = os.getenv(
        "STATUS_EMOJI_CREATE_REASON",
        "Auto-create status emojis for moderation responses.",
    )

    # Status embed visual floor (helps avoid tiny embeds on short messages).
    STATUS_EMBED_MIN_WIDTH_CHARS = _parse_int(os.getenv("STATUS_EMBED_MIN_WIDTH_CHARS"), 32)
    # Limits
    MAX_WARNINGS = 5
    MAX_MESSAGE_LENGTH = 2000
    PURGE_LIMIT = 1000
    
    # Timeouts (seconds)
    BUTTON_TIMEOUT = 180
    MENU_TIMEOUT = 60
    TICKET_CLOSE_DELAY = 5
    
    # Staff Sanction System
    WARNS_PER_STRIKE = 3
    STRIKES_FOR_BAN = 3
    STAFF_BAN_DAYS = 7
    
    # AutoMod Defaults
    DEFAULT_SPAM_THRESHOLD = 5
    DEFAULT_SPAM_TIMEOUT = 60
    DEFAULT_MUTE_DURATION = 3600
    DEFAULT_CAPS_PERCENTAGE = 70
    DEFAULT_MAX_MENTIONS = 5

