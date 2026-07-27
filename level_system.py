import asyncio
import hashlib
import io
import logging
import os
import random
import math
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps
import psycopg2
import psycopg2.extras


LOGGER = logging.getLogger("enzo-bot.level-system")
ACCENT_COLOR = 0x000000

CARD_WIDTH = 320
CARD_HEIGHT = 96
RANK_CARD_WIDTH = 800
RANK_CARD_HEIGHT = 200
PROFILE_CARD_WIDTH = 700
PROFILE_CARD_HEIGHT = 295
PROFILE_OUTPUT_SCALE = 2
LEADERBOARD_CARD_WIDTH = 800
LEADERBOARD_CARD_HEIGHT = 680
MAX_BACKGROUND_BYTES = 6 * 1024 * 1024
LOCAL_BACKGROUND_PREFIX = "local:"
PROFILE_BACKGROUND_DIR = "profile_backgrounds"
DEFAULT_CARD_ACCENT = 0xFFFFFF
DEFAULT_AMBIENT_ACCENT = DEFAULT_CARD_ACCENT
DEFAULT_ICON_STYLE = "color"
ANIMATED_ICON_STYLE = "animated"
ANIMATED_ICON_PRICE = 1_000_000
RAINBOW_ICON_STYLE = "rainbow"
RAINBOW_ICON_PRICE = 10_000_000
ANIMATED_RAINBOW_ICON_STYLE = "animated_rainbow"
ANIMATED_RAINBOW_ICON_PRICE = 15_000_000
ANIMATED_RAINBOW_ICON_UPGRADE_PRICE = 5_000_000

COLOR_PRESETS: dict[str, int] = {
    "red": 0xFF3B4F,
    "dark red": 0x8B0000,
    "pink": 0xFF5BA7,
    "hot pink": 0xFF1493,
    "purple": 0x9B5CFF,
    "violet": 0x8A2BE2,
    "indigo": 0x4B0082,
    "blue": 0x4D8DFF,
    "navy": 0x1E3A8A,
    "cyan": 0x35D5FF,
    "aqua": 0x35D5FF,
    "teal": 0x14B8A6,
    "green": 0x45D483,
    "lime": 0x84CC16,
    "yellow": 0xFDE047,
    "gold": 0xF2C94C,
    "orange": 0xFF8A3D,
    "brown": 0x8B5A2B,
    "white": 0xFFFFFF,
    "gray": 0x9CA3AF,
    "grey": 0x9CA3AF,
    "silver": 0xC0C0C0,
    "black": 0x111111,
}

ICON_FILE_NAMES: dict[str, str] = {
    "chat": "chat.png",
    "eye": "eye-2.png",
    "link": "link-3.png",
    "mic": "mic-4.png",
    "user": "user-5.png",
}

ANIMATED_ICON_FILE_NAMES: dict[str, str] = {
    "chat": "chat_animated.gif",
    "eye": "eye-2_animated.gif",
    "link": "link-3_animated.gif",
    "mic": "mic-4_animated.gif",
    "user": "user-5_animated.gif",
}

TRACE_ICON_FILE_NAMES: dict[str, str] = {
    "chat": "chat_trace_slow.gif",
    "eye": "eye-2_trace_slow.gif",
    "link": "link-3_trace_slow.gif",
    "mic": "mic-4_trace_slow.gif",
    "user": "user-5_trace_slow.gif",
}

RAINBOW_TRACE_ICON_FILE_NAMES: dict[str, str] = {
    "chat": "chat_rainbow_gentle.gif",
    "eye": "eye-2_rainbow_gentle.gif",
    "link": "link-3_rainbow_gentle.gif",
    "mic": "mic-4_rainbow_gentle.gif",
    "user": "user-5_rainbow_gentle.gif",
}

_DEFAULT_LEVEL_REWARD_ROLES: dict[int, int] = {
    50: 1508713515429396552,
    45: 1509231048188235938,
    40: 1508713515429396551,
    30: 1509230760769355948,
    25: 1509230814284484628,
    20: 1508713515408691279,
    15: 1509230886321782886,
    10: 1508713515408691278,
    5: 1508713515408691277,
}


def get_level_reward_roles(settings: dict) -> dict[int, int]:
    if not settings:
        return dict(_DEFAULT_LEVEL_REWARD_ROLES)
    roles_raw = settings.get("level_reward_roles")
    if roles_raw is None:
        return dict(_DEFAULT_LEVEL_REWARD_ROLES)
    if isinstance(roles_raw, dict):
        try:
            return {int(k): int(v) for k, v in roles_raw.items()}
        except ValueError:
            pass
    return dict(_DEFAULT_LEVEL_REWARD_ROLES)


def _level_reward_role_name(reward_level: int) -> str:
    return f"Level {reward_level}"


@lru_cache(maxsize=None)
def xp_for_level(level: int) -> int:
    if level <= 0:
        return 0
    return (15 * (level**2)) + (60 * level) + 75


@lru_cache(maxsize=None)
def total_xp_for_level(level: int) -> int:
    return sum(xp_for_level(current) for current in range(1, level + 1))


def level_for_xp(xp: int) -> int:
    if xp <= 0:
        return 0
    level = 0
    while xp >= total_xp_for_level(level + 1):
        level += 1
    return level


def _icon_folder_name(color_name: str) -> str:
    return color_name.replace(" ", "_")


def _rgb_distance(left: int, right: int) -> int:
    left_rgb = ((left >> 16) & 255, (left >> 8) & 255, left & 255)
    right_rgb = ((right >> 16) & 255, (right >> 8) & 255, right & 255)
    return sum(
        (left_value - right_value) ** 2
        for left_value, right_value in zip(left_rgb, right_rgb)
    )


def _circle_mask_image(image: Image.Image) -> Image.Image:
    size = min(image.size)
    if image.size != (size, size):
        image = _cover_image(image, size, size)
    antialias = 4
    mask = Image.new("L", (size * antialias, size * antialias), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.ellipse((0, 0, (size * antialias) - 1, (size * antialias) - 1), fill=255)
    mask = mask.resize((size, size), Image.Resampling.LANCZOS)
    output = image.convert("RGBA").copy()
    current_alpha = output.getchannel("A")
    output.putalpha(
        Image.composite(current_alpha, Image.new("L", (size, size), 0), mask)
    )
    return output


@lru_cache(maxsize=128)
def _closest_icon_color_folder(base_dir: Path, accent: int) -> str:
    icons_dir = base_dir / "assets" / "icons"
    available = (
        {path.name for path in icons_dir.iterdir() if path.is_dir()}
        if icons_dir.exists()
        else set()
    )
    fallback = "white" if "white" in available else ""
    closest = fallback
    closest_distance: Optional[int] = None

    for color_name, color_value in COLOR_PRESETS.items():
        folder = _icon_folder_name(color_name)
        if folder not in available:
            continue
        distance = _rgb_distance(accent, color_value)
        if closest_distance is None or distance < closest_distance:
            closest = folder
            closest_distance = distance

    return closest


@lru_cache(maxsize=128)
def _closest_animation_color_folder(base_dir: Path, accent: int) -> str:
    icons_dir = base_dir / "assets" / "icons" / ANIMATED_ICON_STYLE
    available = (
        {path.name for path in icons_dir.iterdir() if path.is_dir()}
        if icons_dir.exists()
        else set()
    )
    fallback = "white" if "white" in available else ""
    closest = fallback
    closest_distance: Optional[int] = None

    for color_name, color_value in COLOR_PRESETS.items():
        folder = _icon_folder_name(color_name)
        if folder not in available:
            continue
        distance = _rgb_distance(accent, color_value)
        if closest_distance is None or distance < closest_distance:
            closest = folder
            closest_distance = distance

    return closest


@lru_cache(maxsize=256)
def _load_custom_icon(
    base_dir: Path,
    name: str,
    size: tuple[int, int],
    accent: Optional[int] = None,
) -> Optional[Image.Image]:
    icons_dir = base_dir / "assets" / "icons"
    filename = ICON_FILE_NAMES.get(name, f"{name}.png")
    color_folder = (
        _closest_icon_color_folder(base_dir, accent) if accent is not None else ""
    )
    icon_path = (
        icons_dir / color_folder / filename if color_folder else icons_dir / filename
    )
    if not icon_path.exists():
        return None
    try:
        icon = (
            Image.open(icon_path).convert("RGBA").resize(size, Image.Resampling.LANCZOS)
        )
        return _circle_mask_image(icon) if color_folder else icon
    except Exception as e:
        LOGGER.warning("Failed to load custom icon %s: %s", name, e)
        return None


@lru_cache(maxsize=64)
def _load_animated_custom_icon(
    base_dir: Path,
    name: str,
    size: tuple[int, int],
    style: str,
    accent: int,
) -> tuple[tuple[Image.Image, ...], tuple[int, ...]]:
    icons_dir = base_dir / "assets" / "icons"
    if style == RAINBOW_ICON_STYLE:
        icon_dir = icons_dir / RAINBOW_ICON_STYLE
        filename = ANIMATED_ICON_FILE_NAMES.get(name)
    elif style == ANIMATED_RAINBOW_ICON_STYLE:
        icon_dir = icons_dir / ANIMATED_ICON_STYLE / RAINBOW_ICON_STYLE
        filename = RAINBOW_TRACE_ICON_FILE_NAMES.get(name)
    elif style == ANIMATED_ICON_STYLE:
        color_folder = _closest_animation_color_folder(base_dir, accent)
        icon_dir = (
            icons_dir / ANIMATED_ICON_STYLE / color_folder
            if color_folder
            else icons_dir / ANIMATED_ICON_STYLE
        )
        filename = TRACE_ICON_FILE_NAMES.get(name)
    else:
        return (), ()
    if not filename:
        return (), ()
    icon_path = icon_dir / filename
    if not icon_path.exists():
        return (), ()
    try:
        image = Image.open(icon_path)
        frames: list[Image.Image] = []
        durations: list[int] = []
        frame_count = int(getattr(image, "n_frames", 1))
        for frame_index in range(frame_count):
            image.seek(frame_index)
            frame = image.convert("RGBA").resize(size, Image.Resampling.LANCZOS)
            frames.append(_circle_mask_image(frame))
            raw_dur = image.info.get("duration")
            durations.append(max(20, int(raw_dur) if raw_dur is not None else 100))
        return tuple(frames), tuple(durations)
    except Exception as e:
        LOGGER.warning("Failed to load animated custom icon %s: %s", name, e)
        return (), ()


def _duration_at(duration: int | list[int] | tuple[int, ...], index: int) -> int:
    if isinstance(duration, (list, tuple)):
        if not duration:
            return 100
        return int(duration[index % len(duration)])
    return int(duration)


def next_level_remaining(xp: int) -> int:
    level = level_for_xp(xp)
    return max(total_xp_for_level(level + 1) - xp, 0)


def _load_font(path: Path, size: int, *, bold: bool = False) -> ImageFont.ImageFont:
    if path.exists():
        try:
            return ImageFont.truetype(str(path), size)
        except OSError:
            LOGGER.warning("Failed to load font %s", path)
    for name in (
        ("Montserrat-ExtraBold.ttf" if bold else "Montserrat-Regular.ttf"),
        ("Montserrat-Bold.ttf" if bold else "Montserrat-Regular.ttf"),
        ("arialbd.ttf" if bold else "arial.ttf"),
        "DejaVuSans-Bold.ttf",
        "DejaVuSans.ttf",
    ):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


async def _read_avatar(member: discord.Member) -> Optional[bytes]:
    try:
        return await member.display_avatar.with_size(256).with_format("png").read()
    except Exception:
        return None


def _circle_avatar(avatar_bytes: bytes, size: int) -> Image.Image:
    render_size = size * 6
    avatar = (
        Image.open(io.BytesIO(avatar_bytes))
        .convert("RGBA")
        .resize((render_size, render_size), Image.LANCZOS)
    )
    mask = Image.new("L", (render_size, render_size), 0)
    draw = ImageDraw.Draw(mask)
    draw.ellipse((0, 0, render_size - 1, render_size - 1), fill=255)
    output = Image.new("RGBA", (render_size, render_size), (0, 0, 0, 0))
    output.paste(avatar, (0, 0), mask)
    return output.resize((size, size), Image.Resampling.LANCZOS)


def _cover_image(
    image: Image.Image, width: int, height: int, resample=Image.LANCZOS
) -> Image.Image:
    source_width, source_height = image.size
    if source_width <= 0 or source_height <= 0:
        return Image.new("RGBA", (width, height), (12, 14, 22, 255))
    scale = max(width / source_width, height / source_height)
    resized = image.resize(
        (int(source_width * scale), int(source_height * scale)), resample
    )
    left = (resized.width - width) // 2
    top = (resized.height - height) // 2
    return resized.crop((left, top, left + width, top + height))


def _scale_card_frames(frames: list[Image.Image], scale: int) -> list[Image.Image]:
    if scale <= 1:
        return frames
    return [
        frame.resize(
            (frame.width * scale, frame.height * scale), Image.Resampling.LANCZOS
        )
        for frame in frames
    ]


def _resolve_default_background(base_dir: Path) -> Path:
    for filename in (
        "welcome_img.gif",
        "welcome_img.png",
        "welcome_img.jpg",
        "welcome_img.jpeg",
    ):
        path = base_dir / "assets" / filename
        if path.exists():
            return path
    return base_dir / "assets" / "welcome_img.png"


def _format_compact_number(value: int) -> str:
    if value >= 1_000_000:
        compact = f"{value / 1_000_000:.1f}".rstrip("0").rstrip(".")
        return f"{compact}m"
    if value >= 1_000:
        compact = f"{value / 1_000:.1f}".rstrip("0").rstrip(".")
        return f"{compact}k"
    return str(value)


def _clean_card_name(name: str) -> str:
    normalized = unicodedata.normalize("NFKC", name)
    cleaned = []
    for char in normalized:
        if 32 <= ord(char) <= 126:
            cleaned.append(char)
        elif char.isspace():
            cleaned.append(" ")
    return " ".join("".join(cleaned).split()).strip()


def _is_level_up_channel(channel: discord.TextChannel) -> bool:
    normalized_name = "".join(
        character
        for character in unicodedata.normalize("NFKC", channel.name).casefold()
        if character.isalnum()
    )
    if normalized_name in {"levelup", "levelups"}:
        return True

    topic = (channel.topic or "").casefold()
    return "level-up announcement" in topic or "level up announcement" in topic


def _member_card_name(member: discord.Member) -> str:
    return (
        _clean_card_name(member.display_name)
        or _clean_card_name(member.name)
        or str(member.id)
    )


def _draw_stat_pill(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int, int, int],
    label: str,
    value: str,
    *,
    label_font: ImageFont.ImageFont,
    value_font: ImageFont.ImageFont,
    accent: tuple[int, int, int, int],
) -> None:
    x1, y1, x2, y2 = xy
    label_x = x1 + 12
    value_x = (
        x1
        + 12
        + (
            draw.textbbox((0, 0), label, font=label_font)[2]
            - draw.textbbox((0, 0), label, font=label_font)[0]
        )
        + 8
    )
    text_y = y1 + 7
    draw.text((label_x, text_y), label, fill=(168, 170, 192), font=label_font)
    draw.text((value_x, text_y), value, fill=accent, font=value_font)


def _center_text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int, int, int],
    text: str,
    font: ImageFont.ImageFont,
    fill: tuple[int, int, int, int],
) -> None:
    bbox = draw.textbbox((0, 0), text, font=font)
    width = bbox[2] - bbox[0]
    height = bbox[3] - bbox[1]
    x1, y1, x2, y2 = xy
    x = x1 + ((x2 - x1) - width) // 2 - bbox[0]
    y = y1 + ((y2 - y1) - height) // 2 - bbox[1]
    draw.text((x, y), text, fill=fill, font=font)


def _format_profile_duration(member: discord.Member) -> str:
    joined_at = member.joined_at or member.created_at
    delta = discord.utils.utcnow() - joined_at
    total_seconds = max(0, int(delta.total_seconds()))
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    return f"{days}d&{hours}h&{minutes}m"


def _money_progress(balance: int) -> tuple[float, int]:
    if balance <= 0:
        return 0.0, 1_000
    target = 1_000
    while target <= balance and target < 1_000_000_000:
        target *= 10
    return max(0.0, min(balance / target, 1.0)), target


def _fit_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    path: Path,
    *,
    max_width: int,
    start_size: int,
    min_size: int,
    bold: bool = False,
) -> ImageFont.ImageFont:
    size = start_size
    while size > min_size:
        font = _load_font(path, size, bold=bold)
        bbox = draw.textbbox((0, 0), text, font=font)
        if bbox[2] - bbox[0] <= max_width:
            return font
        size -= 2
    return _load_font(path, min_size, bold=bold)


def _parse_hex_color(value: str) -> int:
    normalized = " ".join(value.strip().lower().split())
    if normalized in COLOR_PRESETS:
        return COLOR_PRESETS[normalized]
    cleaned = normalized.removeprefix("#")
    if len(cleaned) != 6:
        raise ValueError(
            "Use a color name like yellow, red, black, purple, or a 6-digit hex code."
        )
    try:
        color = int(cleaned, 16)
    except ValueError as exc:
        raise ValueError(
            "Use a color name like yellow, red, black, purple, or a valid hex code."
        ) from exc
    return color


def _color_tuple(color: int, alpha: int = 255) -> tuple[int, int, int, int]:
    return ((color >> 16) & 255, (color >> 8) & 255, color & 255, alpha)


def _ambient_accent(color: int) -> int:
    return DEFAULT_AMBIENT_ACCENT if color == DEFAULT_CARD_ACCENT else color


def _validate_image_url(url: str) -> str:
    cleaned = url.strip()
    parsed = urllib.parse.urlparse(cleaned)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Background must be a valid http or https image URL.")

    # Auto-fix tenor links to point to the actual gif file
    if "tenor.com/view/" in cleaned and not cleaned.endswith(".gif"):
        cleaned += ".gif"

    return cleaned


def _is_discord_media_url(url: str) -> bool:
    host = urllib.parse.urlparse(url).netloc.lower()
    return any(
        domain in host for domain in ("discordapp.com", "discordapp.net", "discord.com")
    )


def _reject_discord_error_placeholder(url: str, data: bytes) -> None:
    if not _is_discord_media_url(url):
        return

    try:
        with Image.open(io.BytesIO(data)) as image:
            sample = (
                image.convert("RGB").resize((160, 80), Image.LANCZOS).convert("HSV")
            )
    except OSError:
        return

    pixels = list(sample.getdata())
    total = max(1, len(pixels))
    low_saturation = sum(1 for _, saturation, _ in pixels if saturation < 35) / total
    light_pixels = sum(1 for _, _, value in pixels if value > 145) / total
    dark_pixels = sum(1 for _, _, value in pixels if value < 55) / total

    if low_saturation > 0.78 and light_pixels > 0.45 and dark_pixels > 0.05:
        raise ValueError("Background image link is expired or no longer available.")


def _normalize_role_name(name: str) -> str:
    return " ".join(name.strip().lower().split())


def _download_image_bytes(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "SoulBot/1.0"})
    with urllib.request.urlopen(request, timeout=10) as response:
        content_type = response.headers.get("Content-Type", "")
        if content_type and not content_type.lower().startswith("image/"):
            raise ValueError("Background URL must point to an image.")
        data = response.read(MAX_BACKGROUND_BYTES + 1)
    if len(data) > MAX_BACKGROUND_BYTES:
        raise ValueError("Background image is too large. Use an image under 6 MB.")
    _reject_discord_error_placeholder(url, data)
    return data


def _local_background_reference(relative_path: str) -> str:
    normalized = relative_path.replace("\\", "/")
    return f"{LOCAL_BACKGROUND_PREFIX}{normalized}"


def _resolve_local_background_path(base_dir: Path, source: str) -> Path:
    relative = source[len(LOCAL_BACKGROUND_PREFIX) :].strip().replace("\\", "/")
    if not relative:
        raise ValueError("Saved background path is empty.")
    candidate = (base_dir / "assets" / relative).resolve()
    root = (base_dir / "assets" / PROFILE_BACKGROUND_DIR).resolve()
    if root not in candidate.parents and candidate != root:
        raise ValueError("Saved background path is outside the background store.")
    return candidate


def _read_background_bytes(source: str, base_dir: Path) -> bytes:
    cleaned = str(source or "").strip()
    if cleaned.startswith(LOCAL_BACKGROUND_PREFIX):
        path = _resolve_local_background_path(base_dir, cleaned)
        data = path.read_bytes()
        if len(data) > MAX_BACKGROUND_BYTES:
            raise ValueError("Saved background image is too large.")
        return data
    return _download_image_bytes(cleaned)


def _background_data_from_settings(settings: Optional[dict[str, Any]]) -> Optional[bytes]:
    raw_data = (settings or {}).get("background_data")
    if raw_data is None:
        return None
    return bytes(raw_data)


def _verify_image_bytes(data: bytes) -> None:
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.verify()
    except OSError as exc:
        raise ValueError("Background file must be a valid image.") from exc


def _card_file(card_io: io.BytesIO, base_name: str) -> discord.File:
    is_gif = card_io.getvalue()[:4] == b"GIF8"
    ext = "gif" if is_gif else "png"
    return discord.File(card_io, filename=f"{base_name}.{ext}")


def _static_card_png(card_io: io.BytesIO) -> io.BytesIO:
    card_io.seek(0)
    with Image.open(card_io) as image:
        image.seek(0)
        output = io.BytesIO()
        image.convert("RGB").save(output, format="PNG", optimize=True)
    output.seek(0)
    return output


class LevelStore:
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
                    def column_exists(table_name: str, column_name: str) -> bool:
                        cursor.execute(
                            """
                            SELECT 1
                            FROM information_schema.columns
                            WHERE table_name = %s AND column_name = %s
                            """,
                            (table_name, column_name),
                        )
                        return cursor.fetchone() is not None

                    def add_column_if_missing(
                        table_name: str, column_name: str, ddl: str
                    ) -> None:
                        if column_exists(table_name, column_name):
                            return
                        cursor.execute("SAVEPOINT optional_schema_change")
                        try:
                            cursor.execute("SET LOCAL lock_timeout = '5s'")
                            cursor.execute(ddl)
                        except psycopg2.errors.LockNotAvailable as exc:
                            cursor.execute("ROLLBACK TO SAVEPOINT optional_schema_change")
                            LOGGER.warning(
                                "Skipped optional schema update for %s.%s because the table is locked: %s",
                                table_name,
                                column_name,
                                exc,
                            )
                        else:
                            cursor.execute("RELEASE SAVEPOINT optional_schema_change")

                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS member_levels (
                            guild_id BIGINT NOT NULL,
                            user_id BIGINT NOT NULL,
                            xp BIGINT NOT NULL DEFAULT 0,
                            messages BIGINT NOT NULL DEFAULT 0,
                            last_xp_at TIMESTAMP,
                            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            PRIMARY KEY (guild_id, user_id)
                        )
                        """
                    )
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS level_card_settings (
                            guild_id BIGINT NOT NULL,
                            user_id BIGINT NOT NULL,
                            background_url TEXT,
                            background_data BYTEA,
                            accent_color INTEGER,
                            icon_style TEXT,
                            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            PRIMARY KEY (guild_id, user_id)
                        )
                        """
                    )
                    add_column_if_missing(
                        "level_card_settings",
                        "icon_style",
                        "ALTER TABLE level_card_settings ADD COLUMN icon_style TEXT",
                    )
                    add_column_if_missing(
                        "level_card_settings",
                        "background_data",
                        "ALTER TABLE level_card_settings ADD COLUMN background_data BYTEA",
                    )
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS member_card_presets (
                            guild_id BIGINT NOT NULL,
                            user_id BIGINT NOT NULL,
                            preset_name TEXT NOT NULL,
                            background_url TEXT,
                            background_data BYTEA,
                            accent_color INTEGER,
                            icon_style TEXT,
                            PRIMARY KEY (guild_id, user_id, preset_name)
                        )
                        """
                    )
                    add_column_if_missing(
                        "member_card_presets",
                        "icon_style",
                        "ALTER TABLE member_card_presets ADD COLUMN icon_style TEXT",
                    )
                    add_column_if_missing(
                        "member_card_presets",
                        "background_data",
                        "ALTER TABLE member_card_presets ADD COLUMN background_data BYTEA",
                    )
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS level_card_icon_purchases (
                            guild_id BIGINT NOT NULL,
                            user_id BIGINT NOT NULL,
                            icon_style TEXT NOT NULL,
                            purchased_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            PRIMARY KEY (guild_id, user_id, icon_style)
                        )
                        """
                    )
        finally:
            conn.close()

    def add_xp(
        self, guild_id: int, user_id: int, amount: int, *, count_message: bool = True
    ) -> dict[str, Any]:
        message_count = 1 if count_message else 0
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO member_levels (guild_id, user_id, xp, messages, last_xp_at, updated_at)
                        VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                        ON CONFLICT (guild_id, user_id) DO UPDATE SET
                            xp = member_levels.xp + EXCLUDED.xp,
                            messages = member_levels.messages + EXCLUDED.messages,
                            last_xp_at = CURRENT_TIMESTAMP,
                            updated_at = CURRENT_TIMESTAMP
                        RETURNING guild_id, user_id, xp, messages, last_xp_at
                        """,
                        (guild_id, user_id, amount, message_count),
                    )
                    row = cursor.fetchone()
            return dict(row)
        finally:
            conn.close()

    def get_member(self, guild_id: int, user_id: int) -> dict[str, Any]:
        conn = self._connect()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT guild_id, user_id, xp, messages, last_xp_at
                    FROM member_levels
                    WHERE guild_id = %s AND user_id = %s
                    """,
                    (guild_id, user_id),
                )
                row = cursor.fetchone()
            if row is None:
                return {
                    "guild_id": guild_id,
                    "user_id": user_id,
                    "xp": 0,
                    "messages": 0,
                    "last_xp_at": None,
                }
            return dict(row)
        finally:
            conn.close()

    def reset_guild_progress(self, guild_id: int) -> int:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "DELETE FROM member_levels WHERE guild_id = %s",
                        (guild_id,),
                    )
                    return cursor.rowcount
        finally:
            conn.close()

    def get_rank(self, guild_id: int, user_id: int) -> Optional[int]:
        conn = self._connect()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT rank FROM (
                        SELECT user_id, RANK() OVER (ORDER BY xp DESC, messages DESC, user_id ASC) AS rank
                        FROM member_levels
                        WHERE guild_id = %s
                    ) ranked
                    WHERE user_id = %s
                    """,
                    (guild_id, user_id),
                )
                row = cursor.fetchone()
            return int(row["rank"]) if row is not None else None
        finally:
            conn.close()

    def leaderboard(self, guild_id: int, limit: int = 10) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT user_id, xp, messages
                    FROM member_levels
                    WHERE guild_id = %s
                    ORDER BY xp DESC, messages DESC, user_id ASC
                    LIMIT %s
                    """,
                    (guild_id, limit),
                )
                rows = cursor.fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def reward_candidates(self, guild_id: int, min_reward_level: int) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT user_id, xp
                    FROM member_levels
                    WHERE guild_id = %s AND xp >= %s
                    """,
                    (guild_id, total_xp_for_level(min_reward_level)),
                )
                rows = cursor.fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def get_card_settings(self, guild_id: int, user_id: int) -> dict[str, Any]:
        conn = self._connect()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT background_url, background_data, accent_color, icon_style
                    FROM level_card_settings
                    WHERE guild_id = %s AND user_id = %s
                    """,
                    (guild_id, user_id),
                )
                row = cursor.fetchone()
            if row is None:
                return {
                    "background_url": None,
                    "accent_color": DEFAULT_CARD_ACCENT,
                    "icon_style": DEFAULT_ICON_STYLE,
                }
            data = dict(row)
            if data.get("background_data") is not None:
                data["background_data"] = bytes(data["background_data"])
            if data.get("accent_color") is None:
                data["accent_color"] = DEFAULT_CARD_ACCENT
            if not data.get("icon_style"):
                data["icon_style"] = DEFAULT_ICON_STYLE
            return data
        finally:
            conn.close()

    def save_card_settings(
        self,
        guild_id: int,
        user_id: int,
        *,
        background_url: Optional[str],
        accent_color: Optional[int],
        background_data: Optional[bytes] = None,
        icon_style: Optional[str] = None,
    ) -> dict[str, Any]:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO level_card_settings (
                            guild_id, user_id, background_url, background_data, accent_color, icon_style, updated_at
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                        ON CONFLICT (guild_id, user_id) DO UPDATE SET
                            background_url = COALESCE(EXCLUDED.background_url, level_card_settings.background_url),
                            background_data = CASE
                                WHEN EXCLUDED.background_data IS NOT NULL THEN EXCLUDED.background_data
                                WHEN EXCLUDED.background_url IS NOT NULL THEN NULL
                                ELSE level_card_settings.background_data
                            END,
                            accent_color = COALESCE(EXCLUDED.accent_color, level_card_settings.accent_color),
                            icon_style = COALESCE(EXCLUDED.icon_style, level_card_settings.icon_style),
                            updated_at = CURRENT_TIMESTAMP
                        RETURNING background_url, background_data, accent_color, icon_style
                        """,
                        (
                            guild_id,
                            user_id,
                            background_url,
                            psycopg2.Binary(background_data)
                            if background_data is not None
                            else None,
                            accent_color,
                            icon_style,
                        ),
                    )
                    row = cursor.fetchone()
            data = dict(row)
            if data.get("background_data") is not None:
                data["background_data"] = bytes(data["background_data"])
            if data.get("accent_color") is None:
                data["accent_color"] = DEFAULT_CARD_ACCENT
            if not data.get("icon_style"):
                data["icon_style"] = DEFAULT_ICON_STYLE
            return data
        finally:
            conn.close()

    def clear_card_settings(self, guild_id: int, user_id: int) -> None:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        DELETE FROM level_card_settings
                        WHERE guild_id = %s AND user_id = %s
                        """,
                        (guild_id, user_id),
                    )
        finally:
            conn.close()

    def has_icon_style_purchase(
        self, guild_id: int, user_id: int, icon_style: str
    ) -> bool:
        conn = self._connect()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT 1
                    FROM level_card_icon_purchases
                    WHERE guild_id = %s AND user_id = %s AND icon_style = %s
                    """,
                    (guild_id, user_id, icon_style),
                )
                return cursor.fetchone() is not None
        finally:
            conn.close()

    def save_icon_style_purchase(
        self, guild_id: int, user_id: int, icon_style: str
    ) -> None:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO level_card_icon_purchases (guild_id, user_id, icon_style)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (guild_id, user_id, icon_style) DO NOTHING
                        """,
                        (guild_id, user_id, icon_style),
                    )
        finally:
            conn.close()

    def save_preset(
        self,
        guild_id: int,
        user_id: int,
        preset_name: str,
        background_url: Optional[str],
        accent_color: Optional[int],
        icon_style: Optional[str] = None,
        background_data: Optional[bytes] = None,
    ) -> bool:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT COUNT(*) FROM member_card_presets WHERE guild_id = %s AND user_id = %s",
                        (guild_id, user_id),
                    )
                    count = cursor.fetchone()[0]

                    cursor.execute(
                        "SELECT 1 FROM member_card_presets WHERE guild_id = %s AND user_id = %s AND preset_name = %s",
                        (guild_id, user_id, preset_name),
                    )
                    exists = cursor.fetchone()

                    if count >= 5 and not exists:
                        return False

                    cursor.execute(
                        """
                        INSERT INTO member_card_presets (guild_id, user_id, preset_name, background_url, background_data, accent_color, icon_style)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (guild_id, user_id, preset_name) DO UPDATE SET
                            background_url = EXCLUDED.background_url,
                            background_data = EXCLUDED.background_data,
                            accent_color = EXCLUDED.accent_color,
                            icon_style = EXCLUDED.icon_style
                        """,
                        (
                            guild_id,
                            user_id,
                            preset_name,
                            background_url,
                            psycopg2.Binary(background_data)
                            if background_data is not None
                            else None,
                            accent_color,
                            icon_style,
                        ),
                    )
                    return True
        finally:
            conn.close()

    def load_preset(
        self, guild_id: int, user_id: int, preset_name: str
    ) -> Optional[dict]:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT background_url, background_data, accent_color, icon_style FROM member_card_presets WHERE guild_id = %s AND user_id = %s AND preset_name = %s",
                        (guild_id, user_id, preset_name),
                    )
                    row = cursor.fetchone()
                    if not row:
                        return None
                    data = dict(row)
                    if data.get("background_data") is not None:
                        data["background_data"] = bytes(data["background_data"])
                    if not data.get("icon_style"):
                        data["icon_style"] = DEFAULT_ICON_STYLE
                    return data
        finally:
            conn.close()

    def delete_preset(self, guild_id: int, user_id: int, preset_name: str) -> bool:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "DELETE FROM member_card_presets WHERE guild_id = %s AND user_id = %s AND preset_name = %s RETURNING 1",
                        (guild_id, user_id, preset_name),
                    )
                    return bool(cursor.fetchone())
        finally:
            conn.close()

    def get_all_presets(self, guild_id: int, user_id: int) -> list[dict]:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT preset_name, background_url, background_data, accent_color, icon_style FROM member_card_presets WHERE guild_id = %s AND user_id = %s",
                        (guild_id, user_id),
                    )
                    presets = []
                    for row in cursor.fetchall():
                        data = dict(row)
                        if data.get("background_data") is not None:
                            data["background_data"] = bytes(data["background_data"])
                        if not data.get("icon_style"):
                            data["icon_style"] = DEFAULT_ICON_STYLE
                        presets.append(data)
                    return presets
        finally:
            conn.close()


class LevelSystem:
    def __init__(self, bot: commands.Bot, *, db_url: str, base_dir: Path) -> None:
        self.bot = bot
        self.store = LevelStore(db_url)
        self.base_dir = base_dir
        self.background_path = _resolve_default_background(base_dir)
        self.spooky_font_path = base_dir / "assets" / "Creepster.ttf"
        self.montserrat_regular_path = self._font_path("Montserrat-Regular.ttf")
        self.montserrat_bold_path = self._font_path("Montserrat-Bold.ttf")
        self.montserrat_extrabold_path = self._font_path("Montserrat-ExtraBold.ttf")
        self.invite_cache: dict[int, dict[str, int]] = {}

    def _font_path(self, filename: str) -> Path:
        asset_path = self.base_dir / "assets" / filename
        if asset_path.exists():
            return asset_path
        windows_path = Path("C:/Windows/Fonts") / filename
        if windows_path.exists():
            return windows_path
        return Path("__system_font__")

    def _save_profile_background_bytes(
        self, guild_id: int, user_id: int, image_bytes: bytes
    ) -> str:
        _verify_image_bytes(image_bytes)
        with Image.open(io.BytesIO(image_bytes)) as image:
            image_format = str(image.format or "png").lower()
        extension = {
            "jpeg": "jpg",
            "jpg": "jpg",
            "png": "png",
            "gif": "gif",
            "webp": "webp",
        }.get(image_format, "png")
        digest = hashlib.sha256(image_bytes).hexdigest()[:24]
        filename = f"{guild_id}_{user_id}_{digest}.{extension}"
        directory = self.base_dir / "assets" / PROFILE_BACKGROUND_DIR
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / filename
        path.write_bytes(image_bytes)
        return _local_background_reference(f"{PROFILE_BACKGROUND_DIR}/{filename}")

    async def _card_settings(self, guild_id: int, user_id: int) -> dict[str, Any]:
        return await asyncio.to_thread(self.store.get_card_settings, guild_id, user_id)

    async def _economy_total_balance(self, guild_id: int, user_id: int) -> int:
        economy_system = getattr(self.bot, "economy_system", None)
        economy_store = getattr(economy_system, "store", None)
        if economy_store is None:
            return 0
        try:
            if hasattr(economy_store, "get_balances"):
                wallet, bank = await asyncio.to_thread(
                    economy_store.get_balances, guild_id, user_id
                )
                return int(wallet) + int(bank)
            if hasattr(economy_store, "get_balance"):
                return int(
                    await asyncio.to_thread(
                        economy_store.get_balance, guild_id, user_id
                    )
                )
        except Exception:
            LOGGER.exception(
                "Failed to load economy balance for %s in %s", user_id, guild_id
            )
        return 0

    async def _ensure_paid_icon_access(
        self,
        guild_id: int,
        user_id: int,
        *,
        icon_style: str,
        label: str,
        price: int,
        confirmed: bool,
        allow_purchase: bool,
        prefix_confirm_command: str,
    ) -> tuple[bool, str, bool]:
        owned = await asyncio.to_thread(
            self.store.has_icon_style_purchase,
            guild_id,
            user_id,
            icon_style,
        )
        if owned:
            return True, f"{label} icons equipped.", False

        if not allow_purchase:
            return (
                False,
                f"That member needs to buy {label.casefold()} icons from their own account first.",
                False,
            )

        if not confirmed:
            return (
                False,
                f"{label} icons cost **{price:,} coins**. Run {prefix_confirm_command} to pay and equip them.",
                False,
            )

        economy_system = getattr(self.bot, "economy_system", None)
        economy_store = getattr(economy_system, "store", None)
        if economy_store is None or not hasattr(economy_store, "remove_credits"):
            return (
                False,
                f"The economy store is not ready, so {label.casefold()} icons cannot be purchased right now.",
                False,
            )

        purchased = await asyncio.to_thread(
            economy_store.remove_credits, guild_id, user_id, price
        )
        if not purchased:
            balance = 0
            if hasattr(economy_store, "get_balance"):
                try:
                    balance = int(
                        await asyncio.to_thread(
                            economy_store.get_balance, guild_id, user_id
                        )
                    )
                except Exception:
                    LOGGER.exception(
                        "Failed to load balance for %s icon purchase by %s in %s",
                        icon_style,
                        user_id,
                        guild_id,
                    )
            return (
                False,
                f"You need **{price:,} coins** for {label.casefold()} icons. Wallet balance: **{balance:,} coins**.",
                False,
            )

        await asyncio.to_thread(
            self.store.save_icon_style_purchase, guild_id, user_id, icon_style
        )
        return (
            True,
            f"Paid **{price:,} coins** and equipped {label.casefold()} icons.",
            True,
        )

    async def _ensure_rainbow_icon_access(
        self,
        guild_id: int,
        user_id: int,
        *,
        confirmed: bool,
        allow_purchase: bool,
    ) -> tuple[bool, str, bool]:
        return await self._ensure_paid_icon_access(
            guild_id,
            user_id,
            icon_style=RAINBOW_ICON_STYLE,
            label="Rainbow",
            price=RAINBOW_ICON_PRICE,
            confirmed=confirmed,
            allow_purchase=allow_purchase,
            prefix_confirm_command="`.profile edit rainbow confirm`",
        )

    async def _ensure_animation_icon_access(
        self,
        guild_id: int,
        user_id: int,
        *,
        confirmed: bool,
        allow_purchase: bool,
    ) -> tuple[bool, str, bool]:
        return await self._ensure_paid_icon_access(
            guild_id,
            user_id,
            icon_style=ANIMATED_ICON_STYLE,
            label="Animated",
            price=ANIMATED_ICON_PRICE,
            confirmed=confirmed,
            allow_purchase=allow_purchase,
            prefix_confirm_command="`.profile edit animations confirm` or `.pedit animated confirm`",
        )

    async def _ensure_animated_rainbow_icon_access(
        self,
        guild_id: int,
        user_id: int,
        *,
        current_icon_style: Optional[str],
        confirmed: bool,
        allow_purchase: bool,
    ) -> tuple[bool, str, bool]:
        has_rainbow_equipped = (
            str(current_icon_style or "").casefold() == RAINBOW_ICON_STYLE
        )
        price = (
            ANIMATED_RAINBOW_ICON_UPGRADE_PRICE
            if has_rainbow_equipped
            else ANIMATED_RAINBOW_ICON_PRICE
        )
        return await self._ensure_paid_icon_access(
            guild_id,
            user_id,
            icon_style=ANIMATED_RAINBOW_ICON_STYLE,
            label="Animated rainbow",
            price=price,
            confirmed=confirmed,
            allow_purchase=allow_purchase,
            prefix_confirm_command="`.profile edit animated-rainbow confirm`",
        )

    async def _load_card_background(
        self,
        width: int,
        height: int,
        settings: Optional[dict[str, Any]] = None,
        *,
        blur: float = 2.0,
        animated: bool = True,
    ) -> tuple[list[Image.Image], int | list[int]]:
        background_url = (settings or {}).get("background_url")
        background_data = _background_data_from_settings(settings)
        if background_data is not None or background_url:
            try:
                if background_data is not None:
                    image_bytes = background_data
                else:
                    image_bytes = await asyncio.to_thread(
                        _read_background_bytes, str(background_url), self.base_dir
                    )

                def _process_gif():
                    with Image.open(io.BytesIO(image_bytes)) as img:
                        frames = []
                        durations: list[int] = []
                        if animated and getattr(img, "is_animated", False):
                            n_frames = getattr(img, "n_frames", 1)
                            for frame_idx in range(n_frames):
                                img.seek(frame_idx)
                                frame = _cover_image(
                                    img.convert("RGBA"),
                                    width,
                                    height,
                                    resample=Image.Resampling.NEAREST,
                                )
                                if blur:
                                    frame = frame.filter(
                                        ImageFilter.GaussianBlur(radius=blur)
                                    )
                                frames.append(frame)
                                raw_dur = img.info.get("duration")
                                durations.append(
                                    int(raw_dur) if raw_dur is not None else 100
                                )
                        else:
                            frame = _cover_image(img.convert("RGBA"), width, height)
                            if blur:
                                frame = frame.filter(ImageFilter.GaussianBlur(radius=blur))
                            frames.append(frame)
                        raw_dur = img.info.get("duration")
                        return frames, durations or (
                            int(raw_dur) if raw_dur is not None else 100
                        )

                return await asyncio.to_thread(_process_gif)
            except Exception as exc:
                LOGGER.warning(
                    "Failed to load custom level background %s: %s", background_url, exc
                )

        frames = []
        durations: list[int] = []
        if self.background_path.exists():
            try:
                with Image.open(self.background_path) as img:
                    if animated and getattr(img, "is_animated", False):
                        n_frames = getattr(img, "n_frames", 1)
                        for frame_idx in range(n_frames):
                            img.seek(frame_idx)
                            frame = _cover_image(
                                img.convert("RGBA"),
                                width,
                                height,
                                resample=Image.Resampling.NEAREST,
                            )
                            if blur:
                                frame = frame.filter(ImageFilter.GaussianBlur(radius=blur))
                            frames.append(frame)
                            raw_dur = img.info.get("duration")
                            durations.append(int(raw_dur) if raw_dur is not None else 100)
                        return frames, durations or 100
                    else:
                        bg = img.convert("RGBA").resize((width, height), Image.LANCZOS)
                        if blur:
                            bg = bg.filter(ImageFilter.GaussianBlur(radius=blur))
                        frames.append(bg)
            except Exception as exc:
                LOGGER.warning("Failed to load default background: %s", exc)
                frames.append(Image.new("RGBA", (width, height), (12, 14, 22, 255)))
        else:
            frames.append(Image.new("RGBA", (width, height), (12, 14, 22, 255)))
        return frames, 100

    def _settings_accent(self, settings: Optional[dict[str, Any]] = None) -> int:
        raw_color = (settings or {}).get("accent_color")
        try:
            return int(raw_color) if raw_color is not None else DEFAULT_CARD_ACCENT
        except (TypeError, ValueError):
            return DEFAULT_CARD_ACCENT

    def _reward_role_for_level(
        self, guild: discord.Guild, reward_level: int, settings: dict
    ) -> Optional[discord.Role]:
        level_reward_roles = get_level_reward_roles(settings)
        configured_role_id = level_reward_roles.get(reward_level)
        if configured_role_id:
            role = guild.get_role(configured_role_id)
            if role is not None:
                return role

        expected_name = _level_reward_role_name(reward_level).casefold()
        for role in guild.roles:
            if _normalize_role_name(role.name) == expected_name:
                return role
        return None

    async def _ensure_reward_role_for_level(
        self,
        guild: discord.Guild,
        reward_level: int,
        settings: dict,
    ) -> Optional[discord.Role]:
        role = self._reward_role_for_level(guild, reward_level, settings)
        if role is not None:
            return role

        bot_member = guild.me
        if bot_member is None and self.bot.user is not None:
            bot_member = guild.get_member(self.bot.user.id)
        if bot_member is None or not bot_member.guild_permissions.manage_roles:
            LOGGER.warning(
                "Missing Manage Roles permission to create level reward roles in %s",
                guild.id,
            )
            return None

        try:
            return await guild.create_role(
                name=_level_reward_role_name(reward_level),
                colour=discord.Colour.blurple(),
                mentionable=False,
                reason=f"Create missing level {reward_level} reward role",
            )
        except discord.Forbidden:
            LOGGER.warning(
                "Forbidden creating level reward role %s in %s", reward_level, guild.id
            )
        except discord.HTTPException as exc:
            LOGGER.warning(
                "Failed to create level reward role %s in %s: %s",
                reward_level,
                guild.id,
                exc,
            )
        return None

    async def ensure_reward_roles(self, guild: discord.Guild, settings: dict) -> None:
        level_reward_roles = get_level_reward_roles(settings)
        for reward_level in sorted(level_reward_roles):
            await self._ensure_reward_role_for_level(guild, reward_level, settings)

    def _reward_role_ids(self, guild: discord.Guild, settings: dict) -> set[int]:
        level_reward_roles = get_level_reward_roles(settings)
        role_ids = set(level_reward_roles.values())
        for level in level_reward_roles:
            role = self._reward_role_for_level(guild, level, settings)
            if role is not None:
                role_ids.add(role.id)
        return role_ids

    def setup(self) -> None:
        self.store.initialize()
        self.bot.add_listener(self.on_message, "on_message")
        self.bot.add_listener(self.on_ready, "on_ready")
        self.bot.add_listener(self.on_member_join, "on_member_join")
        self.bot.add_listener(self.on_invite_create, "on_invite_create")
        self.bot.add_listener(self.on_invite_delete, "on_invite_delete")
        self.register_commands()
        self.bot.tree.add_command(self.build_command_group())

    async def _levels_enabled(self, guild_id: int) -> bool:
        safety_store = getattr(self.bot, "safety_store", None)
        if safety_store is None:
            return True
        try:
            return await asyncio.to_thread(safety_store.is_enabled, guild_id, "level")
        except Exception as exc:
            LOGGER.warning("Failed to read level safety state; defaulting enabled: %s", exc)
            return True

    async def on_ready(self) -> None:
        await self.refresh_invite_cache()
        await self.sync_reward_roles()

    async def sync_reward_roles(self) -> None:
        for guild in self.bot.guilds:
            if not await self._levels_enabled(guild.id):
                continue
            settings = await asyncio.to_thread(self.bot.guild_settings.get_settings, guild.id)
            await self.ensure_reward_roles(guild, settings)
            level_reward_roles = get_level_reward_roles(settings)
            min_reward_level = min(level_reward_roles.keys()) if level_reward_roles else 9999
            rows = await asyncio.to_thread(self.store.reward_candidates, guild.id, min_reward_level)
            for row in rows:
                user_id = int(row["user_id"])
                member = guild.get_member(user_id)
                if member is None:
                    continue
                await self._apply_reward_role(member, level_for_xp(int(row["xp"])), settings)

    async def refresh_invite_cache(self) -> None:
        for guild in self.bot.guilds:
            self.invite_cache[guild.id] = await self._fetch_invite_uses(guild)

    async def _fetch_invite_uses(self, guild: discord.Guild) -> dict[str, int]:
        try:
            invites = await guild.invites()
        except discord.Forbidden:
            LOGGER.warning(
                "Missing Manage Guild permission to track invite XP in %s", guild.id
            )
            return {}
        except discord.HTTPException as exc:
            LOGGER.warning("Failed to fetch invites for %s: %s", guild.id, exc)
            return {}
        return {invite.code: int(invite.uses or 0) for invite in invites}

    async def on_invite_create(self, invite: discord.Invite) -> None:
        guild = invite.guild
        if isinstance(guild, discord.Guild):
            self.invite_cache[guild.id] = await self._fetch_invite_uses(guild)

    async def on_invite_delete(self, invite: discord.Invite) -> None:
        guild = invite.guild
        if isinstance(guild, discord.Guild):
            self.invite_cache[guild.id] = await self._fetch_invite_uses(guild)

    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot:
            return
        guild = member.guild
        if not await self._levels_enabled(guild.id):
            return

        before = self.invite_cache.get(guild.id, {})
        after = await self._fetch_invite_uses(guild)
        self.invite_cache[guild.id] = after
        used_code = next(
            (code for code, uses in after.items() if uses > before.get(code, 0)),
            None,
        )
        if used_code is None:
            return

        try:
            invite = discord.utils.get(await guild.invites(), code=used_code)
        except (discord.Forbidden, discord.HTTPException):
            invite = None
        inviter = invite.inviter if invite is not None else None
        if inviter is None or inviter.bot:
            return
        inviter_member = guild.get_member(inviter.id)
        if inviter_member is None:
            try:
                inviter_member = await guild.fetch_member(inviter.id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                inviter_member = None
        if not isinstance(inviter_member, discord.Member):
            return
        settings = await asyncio.to_thread(self.bot.guild_settings.get_settings, guild.id)
        invite_xp = settings.get("invite_xp") or 500
        await self._award_xp(
            guild=guild,
            member=inviter_member,
            amount=invite_xp,
            count_message=False,
            reason=f"Invited {member}",
        )

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return
        if not isinstance(message.author, discord.Member):
            return
        if not message.content.strip():
            return
        if not await self._levels_enabled(message.guild.id):
            return

        settings = await asyncio.to_thread(self.bot.guild_settings.get_settings, message.guild.id)
        level_min_xp = settings.get("level_min_xp") or 15
        level_max_xp = settings.get("level_max_xp") or 25
        xp_amount = random.randint(level_min_xp, level_max_xp)
        await self._award_xp(
            guild=message.guild,
            member=message.author,
            amount=xp_amount,
            count_message=True,
        )

    async def _award_xp(
        self,
        *,
        guild: discord.Guild,
        member: discord.Member,
        amount: int,
        count_message: bool,
        reason: str = "",
    ) -> None:
        before = await asyncio.to_thread(self.store.get_member, guild.id, member.id)
        before_level = level_for_xp(int(before["xp"]))
        after = await asyncio.to_thread(
            self.store.add_xp,
            guild.id,
            member.id,
            amount,
            count_message=count_message,
        )
        after_level = level_for_xp(int(after["xp"]))
        if reason:
            LOGGER.info(
                "Awarded %s XP to %s in %s: %s", amount, member.id, guild.id, reason
            )
        settings = await asyncio.to_thread(self.bot.guild_settings.get_settings, guild.id)
        await self._apply_reward_role(member, after_level, settings)
        if after_level <= before_level:
            return

        await self._send_level_up(guild, member, after_level)
        self.bot.dispatch("level_up", member, after_level)

    async def _apply_reward_role(self, member: discord.Member, level: int, settings: dict) -> None:
        level_reward_roles = get_level_reward_roles(settings)
        reward_level = max(
            (threshold for threshold in level_reward_roles if level >= threshold),
            default=0,
        )
        if reward_level <= 0:
            return

        reward_role = await self._ensure_reward_role_for_level(
            member.guild, reward_level, settings
        )
        if reward_role is None:
            LOGGER.warning(
                "Level reward role for level %s was not found in %s",
                reward_level,
                member.guild.id,
            )
            return

        bot_member = member.guild.me
        if bot_member is None and self.bot.user is not None:
            bot_member = member.guild.get_member(self.bot.user.id)
        if bot_member is None:
            LOGGER.warning(
                "Could not find bot member while updating level roles in %s",
                member.guild.id,
            )
            return
        if not bot_member.guild_permissions.manage_roles:
            LOGGER.warning(
                "Missing Manage Roles permission to update level roles in %s",
                member.guild.id,
            )
            return
        if reward_role >= bot_member.top_role:
            LOGGER.warning(
                "Cannot assign level role %s in %s because it is above or equal to the bot's top role",
                reward_role.id,
                member.guild.id,
            )
            return

        removable_ids = self._reward_role_ids(member.guild, settings) - {reward_role.id}
        roles_to_remove = [
            role
            for role in member.roles
            if role.id in removable_ids and role < bot_member.top_role
        ]
        try:
            if reward_role not in member.roles:
                await member.add_roles(reward_role, reason=f"Reached level {level}")
            if roles_to_remove:
                await member.remove_roles(
                    *roles_to_remove, reason=f"Reached level {level}"
                )
        except discord.Forbidden:
            LOGGER.warning(
                "Missing permissions to update level roles for %s in %s",
                member.id,
                member.guild.id,
            )
        except discord.HTTPException as exc:
            LOGGER.warning(
                "Failed to update level roles for %s in %s: %s",
                member.id,
                member.guild.id,
                exc,
            )

    async def remove_guild_reward_roles(
        self, guild: discord.Guild, *, reason: str = "The Big Reset"
    ) -> dict[str, int]:
        stats = {
            "members_scanned": 0,
            "member_scan_failed": 0,
            "roles_removed": 0,
            "roles_unmanageable": 0,
            "roles_failed": 0,
            "permission_blocked": 0,
        }

        bot_member = guild.me
        if bot_member is None and self.bot.user is not None:
            bot_member = guild.get_member(self.bot.user.id)
        if bot_member is None or not bot_member.guild_permissions.manage_roles:
            stats["permission_blocked"] = 1
            return stats

        settings = await asyncio.to_thread(self.bot.guild_settings.get_settings, guild.id)
        reward_role_ids = self._reward_role_ids(guild, settings)
        if not reward_role_ids:
            return stats

        try:
            await guild.chunk(cache=True)
        except (asyncio.TimeoutError, discord.Forbidden, discord.HTTPException):
            stats["member_scan_failed"] = 1

        stats["members_scanned"] = len(guild.members)
        for member in guild.members:
            if member.bot:
                continue

            roles_to_remove: list[discord.Role] = []
            for role in member.roles:
                if role.id not in reward_role_ids:
                    continue
                if role >= bot_member.top_role:
                    stats["roles_unmanageable"] += 1
                    continue
                roles_to_remove.append(role)

            if not roles_to_remove:
                continue

            try:
                await member.remove_roles(*roles_to_remove, reason=reason)
            except discord.Forbidden:
                stats["roles_failed"] += len(roles_to_remove)
            except discord.HTTPException:
                stats["roles_failed"] += len(roles_to_remove)
            else:
                stats["roles_removed"] += len(roles_to_remove)

        return stats

    async def _send_level_up(
        self, guild: discord.Guild, member: discord.Member, level: int
    ) -> None:
        target = await self._resolve_level_channel(guild)
        if target is None:
            LOGGER.warning("No configured level-up channel for guild %s", guild.id)
            return

        try:
            data = await asyncio.to_thread(self.store.get_member, guild.id, member.id)
            rank = await asyncio.to_thread(self.store.get_rank, guild.id, member.id)
            settings = await self._card_settings(guild.id, member.id)
            card = await self._generate_level_up_card(member, data, rank, settings)
            raw_upload_limit = getattr(guild, "filesize_limit", 8 * 1024 * 1024)
            try:
                upload_limit = int(raw_upload_limit)
            except (TypeError, ValueError):
                upload_limit = 8 * 1024 * 1024
            if len(card.getbuffer()) > upload_limit:
                original_size = len(card.getbuffer())
                card = await asyncio.to_thread(_static_card_png, card)
                LOGGER.info(
                    "Converted oversized level-up card to PNG for user %s in guild %s (%s -> %s bytes)",
                    member.id,
                    guild.id,
                    original_size,
                    len(card.getbuffer()),
                )
            file = _card_file(card, "level-up")
            await target.send(
                content=f"\N{PARTY POPPER} {member.mention} leveled up!",
                file=file,
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            LOGGER.warning(
                "Failed to send level-up message for user %s in guild %s: %s",
                member.id,
                guild.id,
                exc,
            )

    async def _resolve_level_channel(
        self, guild: discord.Guild
    ) -> Optional[discord.TextChannel]:
        store = getattr(self.bot, "guild_settings", None)
        if store is not None:
            try:
                settings = await asyncio.to_thread(store.get_settings, guild.id)
            except Exception:
                LOGGER.exception(
                    "Failed to load level channel settings for guild %s", guild.id
                )
            else:
                raw_channel_id = settings.get("level_channel_id")
                try:
                    channel_id = (
                        int(raw_channel_id) if raw_channel_id is not None else 0
                    )
                except (TypeError, ValueError):
                    channel_id = 0
                if channel_id > 0:
                    channel = guild.get_channel(channel_id)
                    if channel is None:
                        try:
                            channel = await guild.fetch_channel(channel_id)
                        except (
                            discord.NotFound,
                            discord.Forbidden,
                            discord.HTTPException,
                        ):
                            channel = None
                    if isinstance(channel, discord.TextChannel):
                        return channel

        fallback = next(
            (channel for channel in guild.text_channels if _is_level_up_channel(channel)),
            None,
        )
        if fallback is None:
            return None

        if store is not None:
            try:
                await asyncio.to_thread(
                    store.save_settings,
                    guild.id,
                    level_channel_id=fallback.id,
                )
            except Exception:
                LOGGER.exception(
                    "Failed to repair level channel setting for guild %s",
                    guild.id,
                )
            else:
                LOGGER.info(
                    "Repaired level channel setting for guild %s with channel %s",
                    guild.id,
                    fallback.id,
                )
        return fallback

    async def _generate_level_card(
        self, member: discord.Member, level: int
    ) -> io.BytesIO:
        avatar_bytes = await _read_avatar(member)
        if self.background_path.exists():
            bg = (
                Image.open(self.background_path)
                .convert("RGBA")
                .resize((CARD_WIDTH, CARD_HEIGHT), Image.LANCZOS)
            )
            bg = bg.filter(ImageFilter.GaussianBlur(radius=1.0))
        else:
            bg = Image.new("RGBA", (CARD_WIDTH, CARD_HEIGHT), (10, 10, 12, 255))

        overlay = Image.new("RGBA", (CARD_WIDTH, CARD_HEIGHT), (0, 0, 0, 170))
        card = Image.alpha_composite(bg, overlay)
        draw = ImageDraw.Draw(card)

        glow = Image.new("RGBA", (CARD_WIDTH, CARD_HEIGHT), (0, 0, 0, 0))
        glow_draw = ImageDraw.Draw(glow)
        glow_draw.rectangle(
            [(0, CARD_HEIGHT - 18), (CARD_WIDTH, CARD_HEIGHT)], fill=(95, 12, 24, 90)
        )
        card = Image.alpha_composite(card, glow)
        draw = ImageDraw.Draw(card)

        avatar_size = 58
        avatar_x = 24
        avatar_y = (CARD_HEIGHT - avatar_size) // 2
        
        if avatar_bytes:
            try:
                avatar = _circle_avatar(avatar_bytes, avatar_size)
                card.paste(avatar, (avatar_x, avatar_y), avatar)
                draw.ellipse(
                    (
                        avatar_x - 2,
                        avatar_y - 2,
                        avatar_x + avatar_size + 2,
                        avatar_y + avatar_size + 2,
                    ),
                    fill=_color_tuple(DEFAULT_CARD_ACCENT, 170),
                    width=2,
                )
            except Exception:
                pass

        label_font = _load_font(self.montserrat_bold_path, 19, bold=True)
        number_font = _load_font(self.montserrat_extrabold_path, 48, bold=True)

        level_text = str(level)
        label_text = "Level"
        label_bbox = draw.textbbox((0, 0), label_text, font=label_font)
        label_width = label_bbox[2] - label_bbox[0]
        center_x = 205
        draw.text(
            (center_x - label_width // 2, 14),
            label_text,
            fill=(238, 238, 240),
            font=label_font,
        )

        bbox = draw.textbbox((0, 0), level_text, font=number_font)
        number_width = bbox[2] - bbox[0]
        number_x = center_x - number_width // 2
        number_y = 38
        for dx, dy in ((-2, 0), (2, 0), (0, -2), (0, 2), (1, 1), (-1, -1)):
            draw.text(
                (number_x + dx, number_y + dy),
                level_text,
                fill=(0, 0, 0, 190),
                font=number_font,
            )
        draw.text(
            (number_x, number_y), level_text, fill=(255, 255, 255), font=number_font
        )

        buf = io.BytesIO()
        card.convert("RGB").save(buf, format="PNG", optimize=True)
        buf.seek(0)
        return buf

    async def _generate_level_up_card(
        self,
        member: discord.Member,
        data: dict[str, Any],
        rank: Optional[int],
        settings: Optional[dict[str, Any]] = None,
    ) -> io.BytesIO:
        xp = int(data["xp"])
        level = level_for_xp(xp)
        next_total = total_xp_for_level(level + 1)
        current_floor = total_xp_for_level(level)
        needed_for_next = max(next_total - current_floor, 1)
        earned_this_level = max(xp - current_floor, 0)
        progress = max(0.0, min(earned_this_level / needed_for_next, 1.0))

        accent = self._settings_accent(settings)
        ambient = _ambient_accent(accent)
        accent_color = _color_tuple(accent)
        avatar_bytes = await _read_avatar(member)
        has_custom_background = bool(
            (settings or {}).get("background_url")
            or _background_data_from_settings(settings) is not None
        )
        bg_frames, duration = await self._load_card_background(
            RANK_CARD_WIDTH,
            RANK_CARD_HEIGHT,
            settings,
            blur=0.0 if has_custom_background else 2.0,
            animated=has_custom_background,
        )

        def _render():
            foreground = Image.new(
                "RGBA", (RANK_CARD_WIDTH, RANK_CARD_HEIGHT), (0, 0, 0, 0)
            )
            overlay_alpha = 138 if has_custom_background else 220
            overlay = Image.new(
                "RGBA", (RANK_CARD_WIDTH, RANK_CARD_HEIGHT), (6, 7, 12, overlay_alpha)
            )
            foreground = Image.alpha_composite(foreground, overlay)

            rW, rH = RANK_CARD_WIDTH // 2, RANK_CARD_HEIGHT // 2
            glow = Image.new("RGBA", (rW, rH), (0, 0, 0, 0))
            glow_draw = ImageDraw.Draw(glow)
            glow_draw.ellipse(
                (185 // 2, 42 // 2, 760 // 2, 250 // 2), fill=_color_tuple(ambient, 12)
            )
            glow_draw.rectangle((610 // 2, 0, rW, rH), fill=_color_tuple(ambient, 15))
            glow = glow.filter(ImageFilter.GaussianBlur(radius=12))
            glow = glow.resize(
                (RANK_CARD_WIDTH, RANK_CARD_HEIGHT), Image.Resampling.BICUBIC
            )
            foreground = Image.alpha_composite(foreground, glow)
            draw = ImageDraw.Draw(foreground)

            avatar_size = 140
            avatar_x = 28
            avatar_y = 30
            if avatar_bytes:
                try:
                    avatar = _circle_avatar(avatar_bytes, avatar_size)
                    draw.ellipse(
                        (
                            avatar_x - 4,
                            avatar_y - 4,
                            avatar_x + avatar_size + 4,
                            avatar_y + avatar_size + 4,
                        ),
                        fill=accent_color,
                    )
                    foreground.paste(avatar, (avatar_x, avatar_y), avatar)
                except Exception:
                    pass

            system_font = self.montserrat_bold_path
            heavy_font_path = self.montserrat_extrabold_path
            name_text = f"@{_member_card_name(member)}"
            name_font = _fit_text(
                draw,
                name_text,
                heavy_font_path,
                max_width=360,
                start_size=36,
                min_size=22,
                bold=True,
            )
            stat_font = _load_font(system_font, 18, bold=True)
            stat_value_font = _load_font(heavy_font_path, 18, bold=True)
            label_font = _load_font(heavy_font_path, 22, bold=True)
            number_font = _fit_text(
                draw,
                str(level),
                heavy_font_path,
                max_width=116,
                start_size=46,
                min_size=30,
                bold=True,
            )

            text_x = 208
            draw.text((text_x, 37), name_text, fill=(248, 249, 255), font=name_font)

            level_box = (610, 33, 764, 126)
            label_text = "Level"
            label_bbox = draw.textbbox((0, 0), label_text, font=label_font)
            label_x = (
                level_box[0]
                + ((level_box[2] - level_box[0]) - (label_bbox[2] - label_bbox[0])) // 2
            )
            draw.text((label_x, 43), label_text, fill=(248, 249, 255), font=label_font)
            level_text = str(level)
            number_bbox = draw.textbbox((0, 0), level_text, font=number_font)
            number_w = number_bbox[2] - number_bbox[0]
            number_h = number_bbox[3] - number_bbox[1]
            number_x = level_box[0] + ((level_box[2] - level_box[0]) - number_w) // 2
            number_area_top = level_box[1] + 44
            number_area_bottom = level_box[3] - 9
            number_y = (
                number_area_top
                + ((number_area_bottom - number_area_top) - number_h) // 2
                - number_bbox[1]
            )
            for dx, dy in ((0, 1),):
                draw.text(
                    (number_x + dx, number_y + dy),
                    level_text,
                    fill=(0, 0, 0, 170),
                    font=number_font,
                )
            draw.text(
                (number_x, number_y), level_text, fill=(255, 255, 255), font=number_font
            )

            rank_value = f"#{rank}" if rank else "Unranked"
            xp_text = f"{_format_compact_number(xp)} / {_format_compact_number(next_total)} XP"
            rank_fill = (
                _color_tuple(ambient) if accent == DEFAULT_CARD_ACCENT else accent_color
            )
            _draw_stat_pill(
                draw,
                (text_x, 91, text_x + 244, 127),
                "XP",
                xp_text,
                label_font=stat_font,
                value_font=stat_value_font,
                accent=(248, 249, 255, 255),
            )
            _draw_stat_pill(
                draw,
                (text_x + 260, 91, text_x + 390, 127),
                "Rank",
                rank_value,
                label_font=stat_font,
                value_font=stat_value_font,
                accent=rank_fill,
            )

            bar_x = text_x
            bar_y = 164
            bar_w = 560
            bar_h = 14
            percent_text = f"{int(progress * 100)}%"
            percent_font = _load_font(system_font, 13, bold=True)
            percent_bbox = draw.textbbox((0, 0), percent_text, font=percent_font)
            draw.text(
                (bar_x + bar_w - (percent_bbox[2] - percent_bbox[0]), bar_y - 22),
                percent_text,
                fill=(180, 182, 200),
                font=percent_font,
            )
            draw.rounded_rectangle(
                (bar_x, bar_y, bar_x + bar_w, bar_y + bar_h),
                radius=8,
                fill=(28, 31, 50, 235),
            )
            fill_w = max(10, int(bar_w * progress)) if progress > 0 else 0
            if fill_w:
                fill_layer = Image.new(
                    "RGBA", (RANK_CARD_WIDTH, RANK_CARD_HEIGHT), (0, 0, 0, 0)
                )
                fill_draw = ImageDraw.Draw(fill_layer)
                fill_draw.rounded_rectangle(
                    (bar_x, bar_y - 7, bar_x + fill_w, bar_y + bar_h + 7),
                    radius=12,
                    fill=_color_tuple(ambient, 82),
                )
                fill_layer = fill_layer.filter(ImageFilter.GaussianBlur(radius=5))
                foreground = Image.alpha_composite(foreground, fill_layer)
                draw = ImageDraw.Draw(foreground)
                draw.rounded_rectangle(
                    (bar_x, bar_y, bar_x + fill_w, bar_y + bar_h),
                    radius=8,
                    fill=accent_color,
                )

            final_frames = []
            for bg_frame in bg_frames:
                final_frames.append(
                    Image.alpha_composite(bg_frame, foreground).convert("RGB")
                )
            final_frames = _scale_card_frames(final_frames, PROFILE_OUTPUT_SCALE)

            buf = io.BytesIO()
            if len(final_frames) > 1:
                final_frames[0].save(
                    buf,
                    format="GIF",
                    save_all=True,
                    append_images=final_frames[1:],
                    duration=duration,
                    loop=0,
                    optimize=True,
                )
            else:
                final_frames[0].save(buf, format="PNG", optimize=True)
            buf.seek(0)
            return buf

        return await asyncio.to_thread(_render)

    async def _generate_rank_card(
        self,
        member: discord.Member,
        data: dict[str, Any],
        rank: Optional[int],
        settings: Optional[dict[str, Any]] = None,
        preset_name: Optional[str] = None,
        money_balance: int = 0,
    ) -> io.BytesIO:
        xp = int(data["xp"])
        level = level_for_xp(xp)
        next_total = total_xp_for_level(level + 1)
        current_floor = total_xp_for_level(level)
        needed_for_next = max(next_total - current_floor, 1)
        earned_this_level = max(xp - current_floor, 0)
        progress = max(0.0, min(earned_this_level / needed_for_next, 1.0))
        money_balance = max(0, int(money_balance))
        money_progress, money_target = _money_progress(money_balance)

        accent = self._settings_accent(settings)
        ambient = _ambient_accent(accent)
        accent_color = _color_tuple(accent)
        icon_style = str(
            (settings or {}).get("icon_style") or DEFAULT_ICON_STYLE
        ).casefold()
        use_animated_icons = icon_style in {
            ANIMATED_ICON_STYLE,
            RAINBOW_ICON_STYLE,
            ANIMATED_RAINBOW_ICON_STYLE,
        }
        avatar_bytes = await _read_avatar(member)
        has_custom_background = bool(
            (settings or {}).get("background_url")
            or _background_data_from_settings(settings) is not None
        )
        scale = PROFILE_OUTPUT_SCALE
        render_width = PROFILE_CARD_WIDTH * scale
        render_height = PROFILE_CARD_HEIGHT * scale
        bg_frames, duration = await self._load_card_background(
            render_width,
            render_height,
            settings,
            blur=0.0 if has_custom_background else 2.0,
        )

        def _render():
            def xy(x: float, y: float) -> tuple[int, int]:
                return int(round(x * scale)), int(round(y * scale))

            def box(
                x1: float, y1: float, x2: float, y2: float
            ) -> tuple[int, int, int, int]:
                return (
                    int(round(x1 * scale)),
                    int(round(y1 * scale)),
                    int(round(x2 * scale)),
                    int(round(y2 * scale)),
                )

            def sized_font(
                path: Path, size: int, *, bold: bool = False
            ) -> ImageFont.ImageFont:
                return _load_font(path, size * scale, bold=bold)

            def alpha_for(
                fill: tuple[int, int, int, int], mask: Image.Image
            ) -> Image.Image:
                if fill[3] >= 255:
                    return mask
                return mask.point(lambda pixel: pixel * fill[3] // 255)

            def aa_shape(
                kind: str,
                xy: tuple[int, int, int, int],
                *,
                fill: tuple[int, int, int, int],
                radius: int = 0,
            ) -> None:
                x1, y1, x2, y2 = xy
                width = max(1, x2 - x1)
                height = max(1, y2 - y1)
                antialias = 4
                mask = Image.new("L", (width * antialias, height * antialias), 0)
                mask_draw = ImageDraw.Draw(mask)
                mask_box = (0, 0, (width * antialias) - 1, (height * antialias) - 1)
                if kind == "ellipse":
                    mask_draw.ellipse(mask_box, fill=255)
                else:
                    mask_draw.rounded_rectangle(
                        mask_box, radius=radius * antialias, fill=255
                    )
                mask = mask.resize((width, height), Image.Resampling.LANCZOS)
                shape = Image.new("RGBA", (width, height), fill)
                shape.putalpha(alpha_for(fill, mask))
                foreground.alpha_composite(shape, (x1, y1))

            def aa_ellipse(
                xy: tuple[int, int, int, int], fill: tuple[int, int, int, int]
            ) -> None:
                aa_shape("ellipse", xy, fill=fill)

            def aa_round(
                xy: tuple[int, int, int, int],
                *,
                radius: int,
                fill: tuple[int, int, int, int],
            ) -> None:
                aa_shape("round", xy, radius=radius, fill=fill)

            def fitted_font(
                text: str,
                path: Path,
                *,
                max_width: int,
                start_size: int,
                min_size: int,
                bold: bool = False,
            ) -> ImageFont.ImageFont:
                return _fit_text(
                    draw,
                    text,
                    path,
                    max_width=max_width * scale,
                    start_size=start_size * scale,
                    min_size=min_size * scale,
                    bold=bold,
                )

            foreground = Image.new("RGBA", (render_width, render_height), (0, 0, 0, 0))
            animated_icon_positions: list[
                tuple[str, tuple[int, int], tuple[int, int]]
            ] = []
            overlay_alpha = 100 if has_custom_background else 200
            overlay = Image.new(
                "RGBA", (render_width, render_height), (7, 8, 15, overlay_alpha)
            )
            foreground = Image.alpha_composite(foreground, overlay)

            rW, rH = render_width // 2, render_height // 2
            glow = Image.new("RGBA", (rW, rH), (0, 0, 0, 0))
            glow_draw = ImageDraw.Draw(glow)
            max_x = max(1, rW - 1)
            for x in range(rW):
                horizontal = (x / max_x) ** 1.45
                alpha = int(horizontal * 44)
                glow_draw.line([(x, 0), (x, rH)], fill=_color_tuple(ambient, alpha))
            glow_draw.ellipse(
                (rW - 220, -140, rW + 190, 260), fill=_color_tuple(ambient, 92)
            )
            glow_draw.ellipse(
                (rW - 420, -60, rW + 80, 310), fill=_color_tuple(ambient, 24)
            )
            glow = glow.filter(ImageFilter.GaussianBlur(radius=(24 * scale) // 2))
            glow = glow.resize((render_width, render_height), Image.Resampling.BICUBIC)
            foreground = Image.alpha_composite(foreground, glow)
            draw = ImageDraw.Draw(foreground)

            avatar_size = 86 * scale
            avatar_x = 31 * scale
            avatar_y = 26 * scale
            if avatar_bytes:
                try:
                    avatar = _circle_avatar(avatar_bytes, avatar_size)
                    aa_ellipse(
                        (
                            avatar_x - (2 * scale),
                            avatar_y - (2 * scale),
                            avatar_x + avatar_size + (2 * scale),
                            avatar_y + avatar_size + (2 * scale),
                        ),
                        fill=(12, 12, 14, 230),
                    )
                    foreground.paste(avatar, (avatar_x, avatar_y), avatar)
                except Exception:
                    aa_ellipse(
                        (
                            avatar_x - (2 * scale),
                            avatar_y - (2 * scale),
                            avatar_x + avatar_size + (2 * scale),
                            avatar_y + avatar_size + (2 * scale),
                        ),
                        fill=accent_color,
                    )
            else:
                aa_ellipse(
                    (
                        avatar_x - (2 * scale),
                        avatar_y - (2 * scale),
                        avatar_x + avatar_size + (2 * scale),
                        avatar_y + avatar_size + (2 * scale),
                    ),
                    fill=accent_color,
                )

            system_font = self.montserrat_bold_path
            heavy_font_path = self.montserrat_extrabold_path
            name_text = _member_card_name(member)
            name_font = _fit_text(
                draw,
                name_text,
                heavy_font_path,
                max_width=275 * scale,
                start_size=28 * scale,
                min_size=18 * scale,
                bold=True,
            )
            pill_font = sized_font(heavy_font_path, 20, bold=True)
            row_value_font = sized_font(heavy_font_path, 17, bold=True)
            icon_font = sized_font(heavy_font_path, 23, bold=True)
            badge_font = sized_font(heavy_font_path, 20, bold=True)

            draw.text(xy(140, 31), name_text, fill=(248, 248, 252, 255), font=name_font)
            invited_text = "INVITED BY: UNKNOWN"
            invited_font = fitted_font(
                invited_text,
                system_font,
                max_width=270,
                start_size=14,
                min_size=11,
                bold=True,
            )
            user_icon = _load_custom_icon(
                self.base_dir, "user", (18 * scale, 18 * scale), accent
            )
            if use_animated_icons:
                animated_icon_positions.append(
                    ("user", xy(141, 68), (18 * scale, 18 * scale))
                )
                draw.text(
                    xy(141 + 24, 70),
                    invited_text,
                    fill=(242, 242, 246, 235),
                    font=invited_font,
                )
            elif user_icon:
                foreground.alpha_composite(user_icon, xy(141, 68))
                draw.text(
                    xy(141 + 24, 70),
                    invited_text,
                    fill=(242, 242, 246, 235),
                    font=invited_font,
                )
            else:
                draw.text(
                    xy(141, 70),
                    invited_text,
                    fill=(242, 242, 246, 235),
                    font=invited_font,
                )

            def draw_top_icon(x: int, y: int, kind: str) -> None:
                icon_box = box(x, y - 2, x + 38, y + 36)
                icon_name = "link" if kind == "invite" else "eye"
                custom_img = _load_custom_icon(
                    self.base_dir, icon_name, (38 * scale, 38 * scale), accent
                )
                if use_animated_icons:
                    animated_icon_positions.append(
                        (icon_name, xy(x, y - 2), (38 * scale, 38 * scale))
                    )
                elif custom_img:
                    foreground.alpha_composite(custom_img, xy(x, y - 2))
                else:
                    aa_ellipse(icon_box, accent_color)

            def draw_top_pill(x: int, y: int, text: str, kind: str) -> None:
                pill_w = 210
                aa_round(
                    box(x, y, x + pill_w, y + 34),
                    radius=17 * scale,
                    fill=(10, 10, 12, 205),
                )
                draw_top_icon(x, y, kind)
                draw.text(
                    xy(x + 48, y + 6), text, fill=(255, 255, 255, 255), font=pill_font
                )

            draw_top_pill(460, 28, "INVITES: 0", "invite")
            draw_top_pill(460, 74, "NO RECORD.", "record")

            panel = (30, 123, 670, 285)
            aa_round(box(*panel), radius=36 * scale, fill=(70, 70, 74, 190))

            def draw_row(
                y: int,
                icon_text: str,
                title: str,
                row_progress: float,
                bar_text: str,
                badge_text: str,
                fill_color: tuple[int, int, int, int],
            ) -> None:
                icon_box = box(66, y + 11, 114, y + 59)
                icon_name = "chat" if icon_text == "XP" else "mic"
                custom_img = _load_custom_icon(
                    self.base_dir, icon_name, (48 * scale, 48 * scale), accent
                )
                if use_animated_icons:
                    animated_icon_positions.append(
                        (icon_name, xy(66, y + 11), (48 * scale, 48 * scale))
                    )
                elif custom_img:
                    foreground.alpha_composite(custom_img, xy(66, y + 11))
                else:
                    aa_ellipse(icon_box, accent_color)
                    _center_text(
                        draw, icon_box, icon_text, icon_font, (255, 255, 255, 255)
                    )

                fitted_title_font = fitted_font(
                    title,
                    heavy_font_path,
                    max_width=455,
                    start_size=22,
                    min_size=15,
                    bold=True,
                )
                draw.text(
                    xy(126, y), title, fill=(242, 242, 245, 255), font=fitted_title_font
                )
                bar_x, bar_y, bar_w, bar_h = (
                    126 * scale,
                    (y + 31) * scale,
                    448 * scale,
                    31 * scale,
                )
                aa_round(
                    (bar_x, bar_y, bar_x + bar_w, bar_y + bar_h),
                    radius=14 * scale,
                    fill=(36, 36, 39, 205),
                )
                fill_w = int(bar_w * max(0.0, min(row_progress, 1.0)))
                if fill_w > 0:
                    aa_round(
                        (bar_x, bar_y, bar_x + max(22 * scale, fill_w), bar_y + bar_h),
                        radius=14 * scale,
                        fill=fill_color,
                    )
                _center_text(
                    draw,
                    (bar_x, bar_y, bar_x + bar_w, bar_y + bar_h),
                    bar_text,
                    row_value_font,
                    (255, 255, 255, 255),
                )

                badge_box = box(586, y + 21, 634, y + 69)
                badge_fill = (255, 213, 0, 255)

                # Draw starburst / scallop
                badge_center_x = badge_box[0] + (badge_box[2] - badge_box[0]) // 2
                badge_center_y = badge_box[1] + (badge_box[3] - badge_box[1]) // 2
                badge_radius = (badge_box[2] - badge_box[0]) // 2
                bump_radius = badge_radius * 0.35
                bumps = 10
                for i in range(bumps):
                    angle = i * (2 * math.pi) / bumps
                    bx = badge_center_x + badge_radius * 0.92 * math.cos(angle)
                    by = badge_center_y + badge_radius * 0.92 * math.sin(angle)
                    aa_ellipse(
                        (
                            int(round(bx - bump_radius)),
                            int(round(by - bump_radius)),
                            int(round(bx + bump_radius)),
                            int(round(by + bump_radius)),
                        ),
                        badge_fill,
                    )
                aa_ellipse(
                    (
                        badge_center_x - badge_radius,
                        badge_center_y - badge_radius,
                        badge_center_x + badge_radius,
                        badge_center_y + badge_radius,
                    ),
                    badge_fill,
                )

                _center_text(draw, badge_box, badge_text, badge_font, (0, 0, 0, 255))

            rank_text = f"#{rank}" if rank else "#0"
            draw_row(
                130,
                "XP",
                f"TOTAL XP: {xp:,} • LEVEL {level}",
                progress,
                f"{earned_this_level:,}/{needed_for_next:,}",
                rank_text,
                accent_color,
            )
            draw_row(
                201,
                "$",
                f"MONEY: {money_balance:,} • CREDITS",
                money_progress,
                f"{_format_compact_number(money_balance)}/{_format_compact_number(money_target)}",
                "$",
                accent_color,
            )

            if preset_name:
                preset_font = sized_font(system_font, 14, bold=True)
                preset_text = preset_name.upper()
                preset_bbox = draw.textbbox((0, 0), preset_text, font=preset_font)
                preset_w = preset_bbox[2] - preset_bbox[0]
                draw.text(
                    (render_width - preset_w - (28 * scale), 12 * scale),
                    preset_text,
                    fill=accent_color,
                    font=preset_font,
                )

            final_frames = []

            mask_scale = 4
            mask = Image.new(
                "L", (render_width * mask_scale, render_height * mask_scale), 0
            )
            mask_draw = ImageDraw.Draw(mask)
            mask_draw.rounded_rectangle(
                (
                    0,
                    0,
                    (render_width * mask_scale) - 1,
                    (render_height * mask_scale) - 1,
                ),
                radius=28 * scale * mask_scale,
                fill=255,
            )
            mask = mask.resize((render_width, render_height), Image.Resampling.LANCZOS)

            animated_icons: dict[
                str, tuple[tuple[Image.Image, ...], tuple[int, ...]]
            ] = {}
            if use_animated_icons:
                for icon_name, _, icon_size in animated_icon_positions:
                    if icon_name in animated_icons:
                        continue
                    icon_frames, icon_durations = _load_animated_custom_icon(
                        self.base_dir,
                        icon_name,
                        icon_size,
                        icon_style,
                        accent,
                    )
                    if icon_frames:
                        animated_icons[icon_name] = (icon_frames, icon_durations)

            frame_count = max(
                [len(bg_frames)]
                + [len(icon_frames) for icon_frames, _ in animated_icons.values()],
                default=len(bg_frames),
            )
            final_durations: list[int] = []
            primary_icon_duration = next(
                (
                    icon_durations
                    for _, icon_durations in animated_icons.values()
                    if icon_durations
                ),
                (),
            )

            for frame_index in range(frame_count):
                bg_frame = bg_frames[frame_index % len(bg_frames)]
                if bg_frame.size != (render_width, render_height):
                    bg_frame = bg_frame.resize(
                        (render_width, render_height), Image.Resampling.LANCZOS
                    )
                frame = Image.alpha_composite(bg_frame, foreground).convert("RGBA")
                for icon_name, icon_pos, _ in animated_icon_positions:
                    icon_data = animated_icons.get(icon_name)
                    if not icon_data:
                        continue
                    icon_frames, _ = icon_data
                    icon_frame = icon_frames[frame_index % len(icon_frames)]
                    frame.alpha_composite(icon_frame, icon_pos)
                frame.putalpha(mask)
                final_frames.append(frame)
                final_durations.append(
                    _duration_at(primary_icon_duration, frame_index)
                    if primary_icon_duration
                    else _duration_at(duration, frame_index)
                )

            buf = io.BytesIO()
            if len(final_frames) > 1:
                final_frames[0].save(
                    buf,
                    format="GIF",
                    save_all=True,
                    append_images=final_frames[1:],
                    duration=final_durations,
                    loop=0,
                    optimize=True,
                )
            else:
                final_frames[0].save(buf, format="PNG", optimize=True)
            buf.seek(0)
            return buf

        return await asyncio.to_thread(_render)

    async def _fetch_leaderboard_backgrounds(
        self, guild_id: int, rows: list[dict[str, Any]]
    ) -> dict[int, bytes]:
        async def fetch_one(user_id: int):
            settings = await asyncio.to_thread(
                self.store.get_card_settings, guild_id, user_id
            )
            background_data = _background_data_from_settings(settings)
            if background_data is not None:
                return user_id, background_data
            if not settings or not settings.get("background_url"):
                return user_id, None
            try:
                data = await asyncio.to_thread(
                    _read_background_bytes, settings["background_url"], self.base_dir
                )
                return user_id, data
            except Exception:
                return user_id, None

        results = await asyncio.gather(
            *(fetch_one(int(r["user_id"])) for r in rows[:10])
        )
        return {uid: data for uid, data in results if data is not None}

    async def _generate_leaderboard_card(
        self,
        guild: discord.Guild,
        rows: list[dict[str, Any]],
        bg_data: dict[int, bytes] = None,
    ) -> io.BytesIO:
        if bg_data is None:
            bg_data = {}
        if self.background_path.exists():
            bg = (
                Image.open(self.background_path)
                .convert("RGBA")
                .resize(
                    (LEADERBOARD_CARD_WIDTH, LEADERBOARD_CARD_HEIGHT), Image.LANCZOS
                )
            )
            bg = bg.filter(ImageFilter.GaussianBlur(radius=2.0))
        else:
            bg = Image.new(
                "RGBA",
                (LEADERBOARD_CARD_WIDTH, LEADERBOARD_CARD_HEIGHT),
                (12, 14, 22, 255),
            )

        card = Image.alpha_composite(
            bg,
            Image.new(
                "RGBA",
                (LEADERBOARD_CARD_WIDTH, LEADERBOARD_CARD_HEIGHT),
                (6, 7, 12, 226),
            ),
        )
        rW, rH = LEADERBOARD_CARD_WIDTH // 2, LEADERBOARD_CARD_HEIGHT // 2
        glow = Image.new("RGBA", (rW, rH), (0, 0, 0, 0))
        glow_draw = ImageDraw.Draw(glow)
        ambient = _ambient_accent(DEFAULT_CARD_ACCENT)
        glow_draw.ellipse((260//2, -80//2, 900//2, 260//2), fill=_color_tuple(ambient, 10))
        glow_draw.rectangle((560//2, 0, rW, rH), fill=_color_tuple(ambient, 12))
        glow = glow.filter(ImageFilter.GaussianBlur(radius=12))
        glow = glow.resize((LEADERBOARD_CARD_WIDTH, LEADERBOARD_CARD_HEIGHT), Image.Resampling.BICUBIC)
        card = Image.alpha_composite(card, glow)
        draw = ImageDraw.Draw(card)

        system_font = self.montserrat_bold_path
        heavy_font_path = self.montserrat_extrabold_path
        title_font = _load_font(heavy_font_path, 38, bold=True)
        header_font = _load_font(system_font, 18, bold=True)
        stat_font = _load_font(heavy_font_path, 18, bold=True)
        rank_font = _load_font(heavy_font_path, 24, bold=True)

        draw.text((36, 30), "Level Leaderboard", fill=(248, 249, 255), font=title_font)
        draw.text((39, 78), guild.name, fill=(166, 168, 190), font=header_font)

        row_x = 36
        row_y = 122
        row_w = 728
        row_h = 46
        gap = 9

        for index, row in enumerate(rows[:10], start=1):
            y = row_y + ((row_h + gap) * (index - 1))
            user_id = int(row["user_id"])
            user_settings = await asyncio.to_thread(
                self.store.get_card_settings, guild.id, user_id
            )
            user_accent_int = self._settings_accent(user_settings)
            user_accent = _color_tuple(user_accent_int)

            xp = int(row["xp"])
            messages = int(row["messages"])
            level = level_for_xp(xp)
            next_total = total_xp_for_level(level + 1)
            current_floor = total_xp_for_level(level)
            needed_for_next = max(next_total - current_floor, 1)
            earned_this_level = max(xp - current_floor, 0)
            progress = max(0.0, min(earned_this_level / needed_for_next, 1.0))
            member = guild.get_member(user_id)
            name = (
                _member_card_name(member) if member is not None else f"User {user_id}"
            )

            if user_id in bg_data:
                try:
                    with Image.open(io.BytesIO(bg_data[user_id])) as row_bg_img:
                        row_bg_img.seek(0)
                        row_bg = row_bg_img.convert("RGBA")
                        row_bg = ImageOps.fit(
                            row_bg, (row_w, row_h), method=Image.LANCZOS
                        )

                        overlay = Image.new("RGBA", (row_w, row_h), (0, 0, 0, 150))
                        row_bg = Image.alpha_composite(row_bg, overlay)

                        mask = Image.new("L", (row_w, row_h), 0)
                        mask_draw = ImageDraw.Draw(mask)
                        mask_draw.rounded_rectangle(
                            (0, 0, row_w, row_h), radius=12, fill=255
                        )
                        row_bg.putalpha(mask)

                        card.paste(row_bg, (row_x, y), row_bg)
                except Exception:
                    row_fill = (
                        (20, 22, 34, 220)
                        if index > 3
                        else ((42, 43, 50, 235) if index == 1 else (30, 32, 42, 226))
                    )
                    draw.rounded_rectangle(
                        (row_x, y, row_x + row_w, y + row_h), radius=12, fill=row_fill
                    )
            else:
                row_fill = (
                    (20, 22, 34, 220)
                    if index > 3
                    else ((42, 43, 50, 235) if index == 1 else (30, 32, 42, 226))
                )
                draw.rounded_rectangle(
                    (row_x, y, row_x + row_w, y + row_h), radius=12, fill=row_fill
                )

            rank_color = user_accent if index == 1 else (203, 205, 220)
            rank_text = f"#{index}"
            rank_bbox = draw.textbbox((0, 0), rank_text, font=rank_font)
            draw.text(
                (row_x + 21 - ((rank_bbox[2] - rank_bbox[0]) // 2), y + 8),
                rank_text,
                fill=rank_color,
                font=rank_font,
            )

            avatar_size = 34
            avatar_x = row_x + 56
            avatar_y = y + 5
            if member is not None:
                avatar_bytes = await _read_avatar(member)
                if avatar_bytes:
                    try:
                        avatar = _circle_avatar(avatar_bytes, avatar_size)
                        card.paste(avatar, (avatar_x, avatar_y), avatar)
                    except Exception:
                        draw.ellipse(
                            (
                                avatar_x,
                                avatar_y,
                                avatar_x + avatar_size,
                                avatar_y + avatar_size,
                            ),
                            fill=(60, 60, 68),
                        )
                else:
                    draw.ellipse(
                        (
                            avatar_x,
                            avatar_y,
                            avatar_x + avatar_size,
                            avatar_y + avatar_size,
                        ),
                        fill=(60, 60, 68),
                    )
            else:
                draw.ellipse(
                    (
                        avatar_x,
                        avatar_y,
                        avatar_x + avatar_size,
                        avatar_y + avatar_size,
                    ),
                    fill=user_accent,
                )

            fitted_name_font = _fit_text(
                draw,
                name,
                heavy_font_path,
                max_width=260,
                start_size=21,
                min_size=15,
                bold=True,
            )
            draw.text(
                (row_x + 104, y + 10), name, fill=(248, 249, 255), font=fitted_name_font
            )

            draw.text(
                (row_x + 390, y + 12),
                f"Level {level}",
                fill=(248, 249, 255),
                font=stat_font,
            )
            draw.text(
                (row_x + 502, y + 12),
                f"{_format_compact_number(xp)} XP",
                fill=user_accent,
                font=stat_font,
            )
            draw.text(
                (row_x + 622, y + 12),
                f"{messages:,} msgs",
                fill=(166, 168, 190),
                font=stat_font,
            )

            bar_x = row_x + 104
            bar_y = y + row_h - 7
            bar_w = 610
            bar_h = 4
            draw.rounded_rectangle(
                (bar_x, bar_y, bar_x + bar_w, bar_y + bar_h),
                radius=2,
                fill=(32, 36, 57, 230),
            )
            fill_w = max(8, int(bar_w * progress)) if progress > 0 else 0
            if fill_w:
                draw.rounded_rectangle(
                    (bar_x, bar_y, bar_x + fill_w, bar_y + bar_h),
                    radius=2,
                    fill=user_accent,
                )

        buf = io.BytesIO()
        card.convert("RGB").save(buf, format="PNG", optimize=True)
        buf.seek(0)
        return buf

    def _rank_embed(
        self,
        guild: discord.Guild,
        member: discord.Member,
        data: dict[str, Any],
        rank: Optional[int],
    ) -> discord.Embed:
        xp = int(data["xp"])
        level = level_for_xp(xp)
        embed = discord.Embed(
            title=f"{member.display_name}'s Level",
            color=ACCENT_COLOR,
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Level", value=str(level), inline=True)
        embed.add_field(name="XP", value=f"{xp:,}", inline=True)
        embed.add_field(
            name="Rank", value=f"#{rank}" if rank else "Unranked", inline=True
        )
        embed.add_field(
            name="Messages", value=f"{int(data['messages']):,}", inline=True
        )
        embed.add_field(
            name="Next Level",
            value=f"{next_level_remaining(xp):,} XP needed",
            inline=True,
        )
        return embed

    def _rewards_embed(self, guild: discord.Guild, settings: dict) -> discord.Embed:
        lines = []
        level_reward_roles = get_level_reward_roles(settings)
        for level, role_id in sorted(level_reward_roles.items()):
            role = self._reward_role_for_level(guild, level, settings)
            role_text = role.mention if role is not None else f"<@&{role_id}>"
            lines.append(f"**Level {level}:** {role_text}")
        return discord.Embed(
            title="Level Rewards",
            description="\n".join(lines) if lines else "No level reward roles configured.",
            color=ACCENT_COLOR,
        )

    def register_commands(self) -> None:
        @commands.group(
            name="profile",
            invoke_without_command=True,
            help="(Profile) Show a member's current profile",
        )
        async def profile_cmd(
            ctx: commands.Context, member: Optional[discord.Member] = None
        ) -> None:
            if ctx.guild is None:
                await ctx.send("This command can only be used in a server.")
                return
            target = member or ctx.author
            msg = await ctx.send("Generating profile...")
            data, rank_number, settings, money_balance = await asyncio.gather(
                asyncio.to_thread(self.store.get_member, ctx.guild.id, target.id),
                asyncio.to_thread(self.store.get_rank, ctx.guild.id, target.id),
                self._card_settings(ctx.guild.id, target.id),
                self._economy_total_balance(ctx.guild.id, target.id),
            )
            card = await self._generate_rank_card(
                target, data, rank_number, settings, money_balance=money_balance
            )
            await msg.delete()
            await ctx.send(file=_card_file(card, "level-profile"))

        @profile_cmd.command(
            name="save", help="Save your current profile card settings as a preset"
        )
        async def profile_save(ctx: commands.Context, *, name: str):
            if ctx.guild is None:
                return
            settings = await self._card_settings(ctx.guild.id, ctx.author.id)
            bg_url = settings.get("background_url") if settings else None
            bg_data = _background_data_from_settings(settings)
            accent = settings.get("accent_color") if settings else None
            icon_style = settings.get("icon_style") if settings else DEFAULT_ICON_STYLE

            success = await asyncio.to_thread(
                self.store.save_preset,
                ctx.guild.id,
                ctx.author.id,
                name,
                bg_url,
                accent,
                icon_style,
                bg_data,
            )
            if success:
                msg = await ctx.send("Saving preset...")
                data = await asyncio.to_thread(
                    self.store.get_member, ctx.guild.id, ctx.author.id
                )
                rank_number = await asyncio.to_thread(
                    self.store.get_rank, ctx.guild.id, ctx.author.id
                )
                money_balance = await self._economy_total_balance(
                    ctx.guild.id, ctx.author.id
                )
                card = await self._generate_rank_card(
                    ctx.author,
                    data,
                    rank_number,
                    settings,
                    money_balance=money_balance,
                )
                await msg.delete()
                await ctx.send(
                    content=f"✅ Saved current profile card settings as preset `{name}`!",
                    file=_card_file(card, "saved-profile"),
                )
            else:
                await ctx.send(
                    "❌ You already have 5 presets! Please delete one first using `.ms delete <name>`."
                )

        @profile_cmd.command(name="delete", help="Delete a saved profile card preset")
        async def profile_delete(ctx: commands.Context, *, name: str):
            if ctx.guild is None:
                return
            success = await asyncio.to_thread(
                self.store.delete_preset, ctx.guild.id, ctx.author.id, name
            )
            if success:
                await ctx.send(f"✅ Deleted preset `{name}`.")
            else:
                await ctx.send(f"❌ Could not find a preset named `{name}`.")

        @profile_cmd.command(
            name="load",
            help="Load a saved profile preset or view all presets if no name is given",
        )
        async def profile_load(ctx: commands.Context, *, name: str = None):
            if ctx.guild is None:
                return

            if not name:
                presets = await asyncio.to_thread(
                    self.store.get_all_presets, ctx.guild.id, ctx.author.id
                )
                if not presets:
                    await ctx.send(
                        "You don't have any saved presets! Save one using `.ms save <name>`."
                    )
                    return

                msg = await ctx.send("Generating previews for your presets...")
                data = await asyncio.to_thread(
                    self.store.get_member, ctx.guild.id, ctx.author.id
                )
                rank_number = await asyncio.to_thread(
                    self.store.get_rank, ctx.guild.id, ctx.author.id
                )

                files = []
                for p in presets:
                    fake_settings = {
                        "background_url": p["background_url"],
                        "background_data": _background_data_from_settings(p),
                        "accent_color": p["accent_color"]
                        if p["accent_color"] is not None
                        else DEFAULT_CARD_ACCENT,
                        "icon_style": p.get("icon_style") or DEFAULT_ICON_STYLE,
                    }
                    # Clean filename by removing bad chars
                    safe_name = "".join(
                        c for c in p["preset_name"] if c.isalnum() or c in " _-"
                    )
                    money_balance = await self._economy_total_balance(
                        ctx.guild.id, ctx.author.id
                    )
                    card_bytes = await self._generate_rank_card(
                        ctx.author,
                        data,
                        rank_number,
                        fake_settings,
                        preset_name=p["preset_name"],
                        money_balance=money_balance,
                    )
                    files.append(_card_file(card_bytes, f"preset_{safe_name}"))

                await msg.delete()
                await ctx.send(
                    "Here are your saved presets! (The file names show the preset name). Use `.ms load <name>` to equip one.",
                    files=files,
                )
                return

            preset = await asyncio.to_thread(
                self.store.load_preset, ctx.guild.id, ctx.author.id, name
            )
            if not preset:
                await ctx.send(f"❌ Could not find a preset named `{name}`.")
                return

            await asyncio.to_thread(
                self.store.save_card_settings,
                ctx.guild.id,
                ctx.author.id,
                background_url=preset["background_url"],
                accent_color=preset["accent_color"],
                background_data=_background_data_from_settings(preset),
                icon_style=preset.get("icon_style") or DEFAULT_ICON_STYLE,
            )

            msg = await ctx.send("Loading preset...")
            data = await asyncio.to_thread(
                self.store.get_member, ctx.guild.id, ctx.author.id
            )
            rank_number = await asyncio.to_thread(
                self.store.get_rank, ctx.guild.id, ctx.author.id
            )
            settings = await self._card_settings(ctx.guild.id, ctx.author.id)
            money_balance = await self._economy_total_balance(
                ctx.guild.id, ctx.author.id
            )
            card = await self._generate_rank_card(
                ctx.author,
                data,
                rank_number,
                settings,
                money_balance=money_balance,
            )
            await msg.delete()
            await ctx.send(
                content=f"✅ Loaded preset `{name}`!",
                file=_card_file(card, "loaded-profile"),
            )

        @commands.command(
            name="mslb", help="(Leaderboard) Show the server level leaderboard"
        )
        async def mslb(ctx: commands.Context) -> None:
            if ctx.guild is None:
                await ctx.send("This command can only be used in a server.")
                return
            msg = await ctx.send("Fetching leaderboard...")
            rows = await asyncio.to_thread(self.store.leaderboard, ctx.guild.id, 10)
            if not rows:
                await msg.delete()
                await ctx.send(
                    embed=discord.Embed(
                        title="Level Leaderboard",
                        description="No XP has been earned yet.",
                        color=ACCENT_COLOR,
                    )
                )
                return
            await msg.edit(content="Fetching backgrounds...")
            bg_data = await self._fetch_leaderboard_backgrounds(ctx.guild.id, rows)
            card = await self._generate_leaderboard_card(ctx.guild, rows, bg_data)
            await msg.delete()
            await ctx.send(file=_card_file(card, "level-leaderboard"))

        @commands.command(name="rewards", help="(Rewards) Show level reward roles")
        async def rewards(ctx: commands.Context) -> None:
            if ctx.guild is None:
                await ctx.send("This command can only be used in a server.")
                return
            settings = await asyncio.to_thread(
                self.bot.guild_settings.get_settings, ctx.guild.id
            )
            await self.ensure_reward_roles(ctx.guild, settings)
            await ctx.send(embed=self._rewards_embed(ctx.guild, settings))

        @profile_cmd.command(
            name="edit",
            help="(Edit) Edit your profile card background and color. Usage: .profile edit [color/url/reset] or attach an image",
        )
        async def profile_edit(ctx: commands.Context, *, arg: str = "") -> None:
            if ctx.guild is None:
                await ctx.send("Use this in the server.")
                return

            if arg.lower() in ("tutorial", "help"):
                embed = discord.Embed(
                    title="🎨 Profile Customization Tutorial",
                    description="Personalize your `.ms` profile card with custom backgrounds, colors, and presets!",
                    color=0x2ECC71,
                )
                embed.add_field(
                    name="1️⃣ Change Background",
                    value="Use `.profile edit <url>` with an image link, or **upload an image** and type `.profile edit` as the comment.\n*Example: `.profile edit https://i.imgur.com/example.png`*",
                    inline=False,
                )
                embed.add_field(
                    name="2️⃣ Change Accent Color",
                    value="Use `.profile edit <color>` to change the text and progress bar color.\n*Example: `.profile edit yellow`, `.profile edit purple`, or `.profile edit #ff0000`.*",
                    inline=False,
                )
                embed.add_field(
                    name="3️⃣ Save Presets",
                    value="Love your current look? Save it as a preset so you can switch back to it anytime!\n*Example: `.ms save cool_red`*",
                    inline=False,
                )
                embed.add_field(
                    name="4️⃣ Load Presets",
                    value="Want to see your saved presets? Use `.ms load` to preview all of them, or `.ms load <name>` to equip one instantly!",
                    inline=False,
                )
                embed.add_field(
                    name="5️⃣ Reset to Default",
                    value="Made a mistake? Use `.profile edit reset` to clear your custom background and color.",
                    inline=False,
                )
                await ctx.send(embed=embed)
                return

            target_user = ctx.author

            if ctx.message.mentions:
                target_user = ctx.message.mentions[0]
                if target_user.id != ctx.author.id:
                    if not ctx.author.guild_permissions.administrator:
                        await ctx.send(
                            "You need Administrator permissions to edit someone else's profile!"
                        )
                        return
                    arg = (
                        arg.replace(f"<@{target_user.id}>", "")
                        .replace(f"<@!{target_user.id}>", "")
                        .strip()
                    )

            reset = arg.lower() == "reset"
            background_url = None
            background_is_upload = False
            color_arg = None
            normalized_arg = arg.strip().casefold()
            arg_tokens = normalized_arg.split()
            paid_confirm_tokens = {"confirm", "pay", "buy"}
            paid_icon_confirmed = any(
                token in paid_confirm_tokens for token in arg_tokens[1:]
            )
            wants_animated_rainbow_icons = bool(
                arg_tokens
                and (
                    arg_tokens[0]
                    in {
                        "animated-rainbow",
                        "animatedrainbow",
                        "rainbow-animation",
                        "rainbow-animations",
                        "rainbowanimated",
                    }
                    or (
                        len(arg_tokens) >= 2
                        and (
                            (arg_tokens[0] == "animated" and arg_tokens[1] == "rainbow")
                            or (
                                arg_tokens[0] == "rainbow"
                                and arg_tokens[1]
                                in {"animated", "animation", "animations"}
                            )
                        )
                    )
                )
            )
            wants_rainbow_icons = bool(
                not wants_animated_rainbow_icons
                and arg_tokens
                and arg_tokens[0] in {"rainbow", "rainbow-icons", "rainbowicons"}
            )
            wants_animated_icons = bool(
                not wants_animated_rainbow_icons
                and arg_tokens
                and arg_tokens[0]
                in {
                    "animation",
                    "animations",
                    "animated",
                    "animated-icons",
                    "animatedicons",
                }
            )
            rainbow_confirmed = paid_icon_confirmed
            animated_confirmed = paid_icon_confirmed
            animated_rainbow_confirmed = paid_icon_confirmed
            update_message = None

            if ctx.message.attachments:
                background_url = ctx.message.attachments[0].url
                background_is_upload = True
                if not paid_icon_confirmed:
                    wants_animated_rainbow_icons = False
                    wants_rainbow_icons = False
                    wants_animated_icons = False

            if (
                arg
                and not reset
                and not wants_rainbow_icons
                and not wants_animated_icons
                and not wants_animated_rainbow_icons
            ):
                if arg.startswith("http://") or arg.startswith("https://"):
                    background_url = arg
                elif not background_url:
                    color_arg = arg

            status_msg = None
            if reset:
                await asyncio.to_thread(
                    self.store.clear_card_settings, ctx.guild.id, target_user.id
                )
                settings = await self._card_settings(ctx.guild.id, target_user.id)
            else:
                if (
                    not background_url
                    and not color_arg
                    and not wants_rainbow_icons
                    and not wants_animated_icons
                    and not wants_animated_rainbow_icons
                ):
                    await ctx.send(
                        "Add a background attachment, a background URL, a color name, `animations`, `rainbow`, `animated-rainbow`, or type `reset`."
                    )
                    return
                current_settings = await self._card_settings(
                    ctx.guild.id, target_user.id
                )

                saved_background_url = None
                saved_background_data = None
                if background_url:
                    try:
                        saved_background_url = _validate_image_url(background_url)
                    except ValueError as exc:
                        await ctx.send(str(exc))
                        return

                accent_color = None
                if color_arg:
                    try:
                        accent_color = _parse_hex_color(color_arg)
                    except ValueError as exc:
                        await ctx.send(str(exc))
                        return

                icon_style = DEFAULT_ICON_STYLE if accent_color is not None else None
                if wants_animated_rainbow_icons:
                    (
                        can_use,
                        message,
                        _,
                    ) = await self._ensure_animated_rainbow_icon_access(
                        ctx.guild.id,
                        target_user.id,
                        current_icon_style=current_settings.get("icon_style"),
                        confirmed=animated_rainbow_confirmed,
                        allow_purchase=target_user.id == ctx.author.id,
                    )
                    if not can_use:
                        await ctx.send(message)
                        return
                    icon_style = ANIMATED_RAINBOW_ICON_STYLE
                    update_message = message
                elif wants_rainbow_icons:
                    can_use, message, _ = await self._ensure_rainbow_icon_access(
                        ctx.guild.id,
                        target_user.id,
                        confirmed=rainbow_confirmed,
                        allow_purchase=target_user.id == ctx.author.id,
                    )
                    if not can_use:
                        await ctx.send(message)
                        return
                    icon_style = RAINBOW_ICON_STYLE
                    update_message = message
                elif wants_animated_icons:
                    can_use, message, _ = await self._ensure_animation_icon_access(
                        ctx.guild.id,
                        target_user.id,
                        confirmed=animated_confirmed,
                        allow_purchase=target_user.id == ctx.author.id,
                    )
                    if not can_use:
                        await ctx.send(message)
                        return
                    icon_style = ANIMATED_ICON_STYLE
                    update_message = message

                if saved_background_url is not None:
                    try:
                        status_msg = await ctx.send("Loading banner...")
                        image_bytes = await asyncio.to_thread(
                            _download_image_bytes, saved_background_url
                        )
                        await asyncio.to_thread(_verify_image_bytes, image_bytes)
                        if background_is_upload:
                            saved_background_data = image_bytes
                            saved_background_url = await asyncio.to_thread(
                                self._save_profile_background_bytes,
                                ctx.guild.id,
                                target_user.id,
                                image_bytes,
                            )
                        await status_msg.edit(
                            content="Banner saved. Generating preview..."
                        )
                    except (
                        ValueError,
                        urllib.error.URLError,
                        OSError,
                        TimeoutError,
                    ) as exc:
                        if status_msg is not None:
                            await status_msg.edit(content=str(exc))
                        else:
                            await ctx.send(str(exc))
                        return
                settings = await asyncio.to_thread(
                    self.store.save_card_settings,
                    ctx.guild.id,
                    target_user.id,
                    background_url=saved_background_url,
                    accent_color=accent_color,
                    background_data=saved_background_data,
                    icon_style=icon_style,
                )

            data = await asyncio.to_thread(
                self.store.get_member, ctx.guild.id, target_user.id
            )
            rank_number = await asyncio.to_thread(
                self.store.get_rank, ctx.guild.id, target_user.id
            )
            money_balance = await self._economy_total_balance(
                ctx.guild.id, target_user.id
            )
            try:
                card = await self._generate_rank_card(
                    target_user,
                    data,
                    rank_number,
                    settings,
                    money_balance=money_balance,
                )
                await ctx.send(
                    content=(
                        f"{update_message} Preview for **{target_user.display_name}**."
                        if update_message
                        else (
                            f"Level card style updated for **{target_user.display_name}**."
                            if not reset
                            else f"Level card style reset for **{target_user.display_name}**."
                        )
                    ),
                    file=_card_file(card, "level-card-preview"),
                )
                if status_msg is not None:
                    try:
                        await status_msg.delete()
                    except discord.HTTPException:
                        pass
            except discord.HTTPException as exc:
                LOGGER.warning(
                    "Failed to send level card preview for %s in %s: %s",
                    target_user.id,
                    ctx.guild.id,
                    exc,
                )
                failure_text = "Your banner was saved, but the preview could not be uploaded. The animated banner may be too large for Discord."
                if status_msg is not None:
                    await status_msg.edit(content=failure_text)
                else:
                    await ctx.send(failure_text)
            except Exception as exc:
                LOGGER.exception(
                    "Failed to generate level card preview for %s in %s",
                    target_user.id,
                    ctx.guild.id,
                )
                failure_text = f"Your banner was saved, but I could not generate the preview: `{type(exc).__name__}`."
                if status_msg is not None:
                    await status_msg.edit(content=failure_text)
                else:
                    await ctx.send(failure_text)

        @commands.command(
            name="leveledit",
            aliases=["msedit"],
            help="Removed legacy command. Use `.profile edit` instead.",
        )
        async def legacy_leveledit(ctx: commands.Context, *, arg: str = "") -> None:
            await ctx.send(
                "❌ **This command has been removed.** Please use `.profile edit` instead."
            )

        @commands.command(
            name="ms", help="Removed legacy command. Use `.profile` instead."
        )
        async def legacy_ms(
            ctx: commands.Context, member: Optional[discord.Member] = None
        ) -> None:
            await ctx.send(
                "❌ **This command has been removed.** Please use `.profile` instead."
            )

        @commands.command(
            name="pedit",
            aliases=["profileedit"],
            help="Edit your profile card background and color.",
        )
        async def pedit(ctx: commands.Context, *, arg: str = "") -> None:
            await ctx.invoke(profile_edit, arg=arg)

        self.bot.add_command(profile_cmd)
        self.bot.add_command(mslb)
        self.bot.add_command(rewards)
        self.bot.add_command(legacy_leveledit)
        self.bot.add_command(legacy_ms)
        self.bot.add_command(pedit)

    def build_command_group(self) -> app_commands.Group:
        group = app_commands.Group(
            name="profile", description="Profile ranks, leaderboards, and customization"
        )

        @group.command(name="show", description="Show a member's current profile")
        @app_commands.guild_only()
        @app_commands.describe(member="Member to inspect")
        async def profile(
            interaction: discord.Interaction, member: Optional[discord.Member] = None
        ) -> None:
            if interaction.guild is None:
                await interaction.response.send_message(
                    "This command can only be used in a server.", ephemeral=True
                )
                return
            target = member or interaction.user
            if not isinstance(target, discord.Member):
                await interaction.response.send_message(
                    "Choose a server member.", ephemeral=True
                )
                return
            await interaction.response.defer()
            data, rank_number, settings, money_balance = await asyncio.gather(
                asyncio.to_thread(
                    self.store.get_member, interaction.guild.id, target.id
                ),
                asyncio.to_thread(
                    self.store.get_rank, interaction.guild.id, target.id
                ),
                self._card_settings(interaction.guild.id, target.id),
                self._economy_total_balance(interaction.guild.id, target.id),
            )
            card = await self._generate_rank_card(
                target, data, rank_number, settings, money_balance=money_balance
            )
            await interaction.followup.send(file=_card_file(card, "level-profile"))

        @group.command(
            name="leaderboard", description="Show the server level leaderboard"
        )
        @app_commands.guild_only()
        async def leaderboard(interaction: discord.Interaction) -> None:
            if interaction.guild is None:
                await interaction.response.send_message(
                    "This command can only be used in a server.", ephemeral=True
                )
                return
            await interaction.response.defer()
            rows = await asyncio.to_thread(
                self.store.leaderboard, interaction.guild.id, 10
            )
            if not rows:
                await interaction.followup.send(
                    embed=discord.Embed(
                        title="Level Leaderboard",
                        description="No XP has been earned yet.",
                        color=ACCENT_COLOR,
                    )
                )
                return
            bg_data = await self._fetch_leaderboard_backgrounds(
                interaction.guild.id, rows
            )
            card = await self._generate_leaderboard_card(
                interaction.guild, rows, bg_data
            )
            await interaction.followup.send(file=_card_file(card, "level-leaderboard"))

        @group.command(
            name="edit", description="Edit your level card background and color"
        )
        @app_commands.guild_only()
        @app_commands.describe(
            background="Upload an image to use as your level card background",
            background_url="Use an image URL as your level card background",
            color="Accent color name like yellow, red, black, purple, or a hex code",
            animated_icons="Equip animated profile icons for 1,000,000 coins",
            rainbow_icons="Equip rainbow profile icons for 10,000,000 coins",
            animated_rainbow_icons="Equip animated rainbow icons for 15,000,000 coins, or 5,000,000 with rainbow equipped",
            pay="Confirm paying for paid icons if you have not bought that style yet",
            reset="Reset your level card style",
        )
        async def edit(
            interaction: discord.Interaction,
            background: Optional[discord.Attachment] = None,
            background_url: Optional[str] = None,
            color: Optional[str] = None,
            animated_icons: bool = False,
            rainbow_icons: bool = False,
            animated_rainbow_icons: bool = False,
            pay: bool = False,
            reset: bool = False,
        ) -> None:
            if interaction.guild is None:
                await interaction.response.send_message(
                    "This command can only be used in a server.", ephemeral=True
                )
                return
            if not isinstance(interaction.user, discord.Member):
                await interaction.response.send_message(
                    "Use this in the server.", ephemeral=True
                )
                return

            await interaction.response.defer(ephemeral=True)
            update_message = None
            status_msg = None
            if reset:
                await asyncio.to_thread(
                    self.store.clear_card_settings,
                    interaction.guild.id,
                    interaction.user.id,
                )
                settings = await self._card_settings(
                    interaction.guild.id, interaction.user.id
                )
            else:
                selected_paid_styles = sum(
                    1
                    for selected in (
                        animated_icons,
                        rainbow_icons,
                        animated_rainbow_icons,
                    )
                    if selected
                )
                if selected_paid_styles > 1:
                    await interaction.followup.send(
                        "Choose one paid icon style at a time.", ephemeral=True
                    )
                    return

                if background is not None and background_url:
                    await interaction.followup.send(
                        "Use either an upload or a URL, not both.", ephemeral=True
                    )
                    return
                current_settings = await self._card_settings(
                    interaction.guild.id, interaction.user.id
                )

                saved_background_url: Optional[str] = None
                saved_background_data: Optional[bytes] = None
                background_is_upload = background is not None
                try:
                    if background is not None:
                        if (
                            background.content_type
                            and not background.content_type.lower().startswith("image/")
                        ):
                            await interaction.followup.send(
                                "The uploaded background has to be an image.",
                                ephemeral=True,
                            )
                            return
                        saved_background_url = _validate_image_url(background.url)
                    elif background_url:
                        saved_background_url = _validate_image_url(background_url)
                except ValueError as exc:
                    await interaction.followup.send(str(exc), ephemeral=True)
                    return

                accent_color: Optional[int] = None
                if color:
                    try:
                        accent_color = _parse_hex_color(color)
                    except ValueError as exc:
                        await interaction.followup.send(str(exc), ephemeral=True)
                        return

                icon_style = DEFAULT_ICON_STYLE if accent_color is not None else None
                if animated_rainbow_icons:
                    (
                        can_use,
                        message,
                        _,
                    ) = await self._ensure_animated_rainbow_icon_access(
                        interaction.guild.id,
                        interaction.user.id,
                        current_icon_style=current_settings.get("icon_style"),
                        confirmed=pay,
                        allow_purchase=True,
                    )
                    if not can_use:
                        await interaction.followup.send(
                            message.replace(
                                "Run `.profile edit animated-rainbow confirm` to pay and equip them.",
                                "Run `/profile edit animated_rainbow_icons:true pay:true` to pay and equip them.",
                            ),
                            ephemeral=True,
                        )
                        return
                    icon_style = ANIMATED_RAINBOW_ICON_STYLE
                    update_message = message
                elif rainbow_icons:
                    can_use, message, _ = await self._ensure_rainbow_icon_access(
                        interaction.guild.id,
                        interaction.user.id,
                        confirmed=pay,
                        allow_purchase=True,
                    )
                    if not can_use:
                        await interaction.followup.send(
                            message.replace(
                                "Run `.profile edit rainbow confirm` to pay and equip them.",
                                "Run `/profile edit rainbow_icons:true pay:true` to pay and equip them.",
                            ),
                            ephemeral=True,
                        )
                        return
                    icon_style = RAINBOW_ICON_STYLE
                    update_message = message
                elif animated_icons:
                    can_use, message, _ = await self._ensure_animation_icon_access(
                        interaction.guild.id,
                        interaction.user.id,
                        confirmed=pay,
                        allow_purchase=True,
                    )
                    if not can_use:
                        await interaction.followup.send(
                            message.replace(
                                "Run `.profile edit animations confirm` or `.pedit animated confirm` to pay and equip them.",
                                "Run `/profile edit animated_icons:true pay:true` to pay and equip them.",
                            ),
                            ephemeral=True,
                        )
                        return
                    icon_style = ANIMATED_ICON_STYLE
                    update_message = message

                if (
                    saved_background_url is None
                    and accent_color is None
                    and not rainbow_icons
                    and not animated_icons
                    and not animated_rainbow_icons
                ):
                    await interaction.followup.send(
                        "Add a background, background URL, color, animated icons, rainbow icons, animated rainbow icons, or set `reset` to true.",
                        ephemeral=True,
                    )
                    return

                if saved_background_url is not None:
                    try:
                        status_msg = await interaction.followup.send(
                            "Loading banner...", ephemeral=True, wait=True
                        )
                        image_bytes = await asyncio.to_thread(
                            _download_image_bytes, saved_background_url
                        )
                        await asyncio.to_thread(_verify_image_bytes, image_bytes)
                        if background_is_upload:
                            saved_background_data = image_bytes
                            saved_background_url = await asyncio.to_thread(
                                self._save_profile_background_bytes,
                                interaction.guild.id,
                                interaction.user.id,
                                image_bytes,
                            )
                        await status_msg.edit(
                            content="Banner saved. Generating preview..."
                        )
                    except (
                        ValueError,
                        urllib.error.URLError,
                        OSError,
                        TimeoutError,
                    ) as exc:
                        if status_msg is not None:
                            await status_msg.edit(content=str(exc))
                        else:
                            await interaction.followup.send(str(exc), ephemeral=True)
                        return

                settings = await asyncio.to_thread(
                    self.store.save_card_settings,
                    interaction.guild.id,
                    interaction.user.id,
                    background_url=saved_background_url,
                    accent_color=accent_color,
                    background_data=saved_background_data,
                    icon_style=icon_style,
                )

            data = await asyncio.to_thread(
                self.store.get_member, interaction.guild.id, interaction.user.id
            )
            rank_number = await asyncio.to_thread(
                self.store.get_rank, interaction.guild.id, interaction.user.id
            )
            money_balance = await self._economy_total_balance(
                interaction.guild.id, interaction.user.id
            )
            try:
                card = await self._generate_rank_card(
                    interaction.user,
                    data,
                    rank_number,
                    settings,
                    money_balance=money_balance,
                )
                await interaction.followup.send(
                    content=update_message
                    or (
                        "Level card style updated."
                        if not reset
                        else "Level card style reset."
                    ),
                    file=_card_file(card, "level-card-preview"),
                    ephemeral=True,
                )
                if status_msg is not None:
                    try:
                        await status_msg.delete()
                    except discord.HTTPException:
                        pass
            except discord.HTTPException as exc:
                LOGGER.warning(
                    "Failed to send level card preview for %s in %s: %s",
                    interaction.user.id,
                    interaction.guild.id,
                    exc,
                )
                failure_text = "Your banner was saved, but the preview could not be uploaded. The animated banner may be too large for Discord."
                if status_msg is not None:
                    await status_msg.edit(content=failure_text)
                else:
                    await interaction.followup.send(failure_text, ephemeral=True)
            except Exception as exc:
                LOGGER.exception(
                    "Failed to generate level card preview for %s in %s",
                    interaction.user.id,
                    interaction.guild.id,
                )
                failure_text = f"Your banner was saved, but I could not generate the preview: `{type(exc).__name__}`."
                if status_msg is not None:
                    await status_msg.edit(content=failure_text)
                else:
                    await interaction.followup.send(failure_text, ephemeral=True)

        @group.command(name="rewards", description="Show level reward roles")
        @app_commands.guild_only()
        async def rewards(interaction: discord.Interaction) -> None:
            if interaction.guild is None:
                await interaction.response.send_message(
                    "This command can only be used in a server.", ephemeral=True
                )
                return
            settings = await asyncio.to_thread(
                self.bot.guild_settings.get_settings, interaction.guild.id
            )
            await self.ensure_reward_roles(interaction.guild, settings)
            await interaction.response.send_message(
                embed=self._rewards_embed(interaction.guild, settings)
            )

        return group

    async def build_rules_embed(self, guild: discord.Guild) -> discord.Embed:
        settings = await asyncio.to_thread(self.bot.guild_settings.get_settings, guild.id)
        level_min_xp = settings.get("level_min_xp") or 15
        level_max_xp = settings.get("level_max_xp") or 25
        invite_xp = settings.get("invite_xp") or 500

        embed = discord.Embed(
            title="Level Rules",
            description=(
                "Earn XP by chatting normally in server channels.\n\n"
                f"**XP Gain:** `{level_min_xp}-{level_max_xp}` XP from every normal chat message. Bot messages and empty messages do not count.\n"
                f"**Invite XP:** `{invite_xp}` XP when someone joins using your invite.\n"
                "**Cooldown:** None. Every normal message gives XP.\n"
                "**Level Ups:** When you level up, a card is posted in the level-up channel configured with `/setup`.\n"
                "**Leaderboard:** `/level leaderboard` ranks members by XP.\n"
                "**Rewards:** Level reward roles are given automatically."
            ),
            color=ACCENT_COLOR,
        )
        reward_lines = []
        level_reward_roles = get_level_reward_roles(settings)
        for level, role_id in sorted(level_reward_roles.items()):
            role = self._reward_role_for_level(guild, level, settings)
            reward_lines.append(
                f"Level {level}: {role.mention if role else f'<@&{role_id}>'}"
            )
        embed.add_field(
            name="Reward Roles", value="\n".join(reward_lines), inline=False
        )
        embed.set_footer(text="Only you can see this.")
        return embed


def init_level_system(bot: commands.Bot, *, db_url: str, base_dir: Path) -> LevelSystem:
    return LevelSystem(bot, db_url=db_url, base_dir=base_dir)
