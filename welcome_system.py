"""Welcome system – generates a gothic-styled welcome card with a spooky font.

Styled after the Mimu rose-aesthetic welcome format with a dark, gothic vibe.
Uses the Creepster font for the guild name and decorative Unicode for atmosphere.
"""

from __future__ import annotations

import asyncio
import io
import logging
from pathlib import Path
from typing import Optional

import discord
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont, ImageFilter

LOGGER = logging.getLogger("enzo-bot.welcome")

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
SPOOKY_FONT_PATH = BASE_DIR / "assets" / "Creepster.ttf"


def _welcome_bg_path() -> Path:
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


WELCOME_BG_PATH = _welcome_bg_path()

# ── Style constants ───────────────────────────────────────────────────────────
CARD_WIDTH = 900
CARD_HEIGHT = 400
AVATAR_SIZE = 100
OVERLAY_COLOR = (0, 0, 0, 140)  # Semi-transparent dark overlay
ACCENT_RED = (180, 40, 40)
TEXT_WHITE = (240, 240, 240)
TEXT_DIM = (180, 170, 170)
BORDER_COLOR = (120, 20, 20)
GUILD_NAME = "Soul"

# Spooky Unicode decorators
DECO_LEFT = "꧁"
DECO_RIGHT = "꧂"
SEPARATOR = "─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─"


def _load_font(size: int, *, spooky: bool = False) -> ImageFont.FreeTypeFont:
    """Load the Creepster font or fall back to default."""
    if spooky and SPOOKY_FONT_PATH.exists():
        try:
            return ImageFont.truetype(str(SPOOKY_FONT_PATH), size)
        except Exception:
            LOGGER.warning("Failed to load Creepster font, using default")
    # Fallback: try common system fonts
    for name in ("arial.ttf", "DejaVuSans.ttf", "FreeSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _make_circle_avatar(avatar_bytes: bytes, size: int) -> Image.Image:
    """Create a circular avatar with a red border glow."""
    avatar = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA")
    avatar = avatar.resize((size, size), Image.LANCZOS)

    # Create circular mask
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    draw.ellipse((0, 0, size - 1, size - 1), fill=255)

    circular = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    circular.paste(avatar, (0, 0), mask)

    # Create the border ring
    border_size = size + 8
    bordered = Image.new("RGBA", (border_size, border_size), (0, 0, 0, 0))
    border_draw = ImageDraw.Draw(bordered)
    # Outer ring (dark red glow)
    border_draw.ellipse(
        (0, 0, border_size - 1, border_size - 1), outline=BORDER_COLOR, width=3
    )
    # Inner subtle glow
    border_draw.ellipse(
        (2, 2, border_size - 3, border_size - 3), outline=(80, 15, 15, 180), width=1
    )

    # Paste avatar centered on border
    bordered.paste(circular, (4, 4), circular)
    return bordered


def get_ordinal(n: int) -> str:
    if 11 <= (n % 100) <= 13:
        return f"{n}th"
    return f"{n}{['th', 'st', 'nd', 'rd', 'th', 'th', 'th', 'th', 'th', 'th'][n % 10]}"


def _generate_welcome_card(
    username: str,
    avatar_bytes: bytes,
    member_count: int,
    bg_path: Path,
    server_name: str,
) -> io.BytesIO:
    """Generate the gothic welcome card image."""
    # Load or create background
    if bg_path.exists():
        bg = Image.open(bg_path).convert("RGBA")
        bg = bg.resize((CARD_WIDTH, CARD_HEIGHT), Image.LANCZOS)
    else:
        bg = Image.new("RGBA", (CARD_WIDTH, CARD_HEIGHT), (20, 5, 5, 255))

    # Apply a slight blur for depth
    bg_blurred = bg.filter(ImageFilter.GaussianBlur(radius=1.5))

    # Create the card
    card = Image.new("RGBA", (CARD_WIDTH, CARD_HEIGHT), (0, 0, 0, 0))
    card.paste(bg_blurred, (0, 0))

    # Dark overlay for readability
    overlay = Image.new("RGBA", (CARD_WIDTH, CARD_HEIGHT), OVERLAY_COLOR)
    card = Image.alpha_composite(card, overlay)

    # Add a subtle red vignette at edges
    vignette = Image.new("RGBA", (CARD_WIDTH, CARD_HEIGHT), (0, 0, 0, 0))
    vignette_draw = ImageDraw.Draw(vignette)
    # Top/bottom dark gradient bars
    for y in range(60):
        alpha = int(180 * (1 - y / 60))
        vignette_draw.line([(0, y), (CARD_WIDTH, y)], fill=(10, 0, 0, alpha))
        vignette_draw.line(
            [(0, CARD_HEIGHT - 1 - y), (CARD_WIDTH, CARD_HEIGHT - 1 - y)],
            fill=(10, 0, 0, alpha),
        )
    card = Image.alpha_composite(card, vignette)

    draw = ImageDraw.Draw(card)

    # Load fonts
    font_guild = _load_font(52, spooky=True)  # Creepster for server name
    font_welcome = _load_font(22, spooky=True)  # Creepster for "WELCOME"
    font_username = _load_font(26)  # Normal for username
    font_separator = _load_font(14)  # Small for separator
    font_count = _load_font(16)  # Small for member count

    # ── Layout (top to bottom, centered) ──────────────────────────────
    cx = CARD_WIDTH // 2

    # 1. Circular avatar at top
    avatar_img = _make_circle_avatar(avatar_bytes, AVATAR_SIZE)
    avatar_x = cx - avatar_img.width // 2
    avatar_y = 30
    card.paste(avatar_img, (avatar_x, avatar_y), avatar_img)

    # 2. Username below avatar
    username_y = avatar_y + avatar_img.height + 12
    username_text = username
    bbox = draw.textbbox((0, 0), username_text, font=font_username)
    tw = bbox[2] - bbox[0]
    draw.text(
        (cx - tw // 2, username_y), username_text, fill=TEXT_WHITE, font=font_username
    )

    # 3. Separator line
    sep_y = username_y + 36
    bbox = draw.textbbox((0, 0), SEPARATOR, font=font_separator)
    tw = bbox[2] - bbox[0]
    draw.text((cx - tw // 2, sep_y), SEPARATOR, fill=TEXT_DIM, font=font_separator)

    # 4. "WELCOME" text in spooky font
    welcome_y = sep_y + 28
    welcome_text = "W E L C O M E"
    bbox = draw.textbbox((0, 0), welcome_text, font=font_welcome)
    tw = bbox[2] - bbox[0]
    # Red glow behind text (draw slightly offset in multiple directions)
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            if dx == 0 and dy == 0:
                continue
            draw.text(
                (cx - tw // 2 + dx, welcome_y + dy),
                welcome_text,
                fill=(120, 20, 20, 120),
                font=font_welcome,
            )
    draw.text(
        (cx - tw // 2, welcome_y), welcome_text, fill=ACCENT_RED, font=font_welcome
    )

    # 5. Guild name in big spooky Creepster
    guild_y = welcome_y + 38
    guild_text = f"{DECO_LEFT} {server_name or GUILD_NAME} {DECO_RIGHT}"
    bbox = draw.textbbox((0, 0), guild_text, font=font_guild)
    tw = bbox[2] - bbox[0]
    # Glow effect
    for dx in (-2, -1, 0, 1, 2):
        for dy in (-2, -1, 0, 1, 2):
            if dx == 0 and dy == 0:
                continue
            draw.text(
                (cx - tw // 2 + dx, guild_y + dy),
                guild_text,
                fill=(100, 10, 10, 80),
                font=font_guild,
            )
    draw.text((cx - tw // 2, guild_y), guild_text, fill=TEXT_WHITE, font=font_guild)

    # 6. Member count at bottom
    count_y = guild_y + 65
    count_text = f"The {get_ordinal(member_count)} member has arrived."
    bbox = draw.textbbox((0, 0), count_text, font=font_count)
    tw = bbox[2] - bbox[0]
    draw.text((cx - tw // 2, count_y), count_text, fill=TEXT_DIM, font=font_count)

    # ── Red border frame ──────────────────────────────────────────────
    border_overlay = Image.new("RGBA", (CARD_WIDTH, CARD_HEIGHT), (0, 0, 0, 0))
    border_draw = ImageDraw.Draw(border_overlay)
    # Left red accent bar
    border_draw.rectangle([(0, 0), (4, CARD_HEIGHT)], fill=(130, 20, 20, 200))
    # Thin top/bottom lines
    border_draw.rectangle([(0, 0), (CARD_WIDTH, 2)], fill=(80, 15, 15, 150))
    border_draw.rectangle(
        [(0, CARD_HEIGHT - 2), (CARD_WIDTH, CARD_HEIGHT)], fill=(80, 15, 15, 150)
    )
    card = Image.alpha_composite(card, border_overlay)

    # Save to buffer
    buf = io.BytesIO()
    card.convert("RGB").save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf


def _welcome_image_attachment(bg_path: Path) -> tuple[io.BytesIO, str]:
    """Return the configured welcome image unchanged for Discord embedding."""
    path = bg_path if bg_path.exists() else WELCOME_BG_PATH
    data = path.read_bytes()
    suffix = path.suffix.lower()
    extension = (
        suffix[1:] if suffix in {".gif", ".png", ".jpg", ".jpeg", ".webp"} else "png"
    )
    if extension == "jpeg":
        extension = "jpg"
    return io.BytesIO(data), f"welcome.{extension}"


class WelcomeSystem:
    """Handles welcome messages for new members joining the server."""

    def __init__(
        self,
        bot: commands.Bot,
    ) -> None:
        self.bot = bot
        self.accent_color = 0x5865F2
        self.background_path = WELCOME_BG_PATH

    def setup(self) -> None:
        """Register the on_member_join listener."""
        old_listener = getattr(self.bot, "_welcome_system_listener", None)
        if old_listener is not None:
            self.bot.remove_listener(old_listener, "on_member_join")
        self.bot.add_listener(self._on_member_join, "on_member_join")
        self.bot._welcome_system_listener = self._on_member_join
        LOGGER.info("Welcome system active")

    async def _guild_settings(self, guild_id: int) -> dict[str, object]:
        store = getattr(self.bot, "guild_settings", None)
        if store is None:
            return {}
        try:
            return await asyncio.to_thread(store.get_settings, guild_id)
        except Exception:
            LOGGER.exception("Failed to load welcome settings for guild %s", guild_id)
            return {}

    @staticmethod
    def _positive_int(value: object, fallback: int = 0) -> int:
        try:
            channel_id = int(value)
        except (TypeError, ValueError):
            return fallback
        return channel_id if channel_id > 0 else fallback

    def _server_name_from_settings(
        self,
        guild: discord.Guild,
        settings: dict[str, object],
    ) -> str:
        configured = settings.get("server_name")
        if configured and isinstance(configured, str) and configured.strip():
            return configured.strip()
        return guild.name

    def _background_path_from_settings(self, settings: dict[str, object]) -> Path:
        configured = settings.get("welcome_wallpaper_path")
        if isinstance(configured, str) and configured.strip():
            path = Path(configured.strip())
            if not path.is_absolute():
                path = BASE_DIR / path
            if path.exists():
                return path
        return self.background_path

    async def _resolve_welcome_channel(
        self, guild: discord.Guild
    ) -> Optional[discord.TextChannel]:
        settings = await self._guild_settings(guild.id)
        configured = settings.get("welcome_channel_id")
        channel_id = self._positive_int(configured, 0)
        if channel_id <= 0:
            return None
        channel = guild.get_channel(channel_id)
        if channel is None:
            try:
                channel = await guild.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                LOGGER.warning(
                    "Welcome channel %s not found in guild %s", channel_id, guild.id
                )
                return None
        return channel if isinstance(channel, discord.TextChannel) else None

    async def _resolve_rules_mention(
        self,
        guild: discord.Guild,
        settings: Optional[dict[str, object]] = None,
    ) -> str:
        if settings is None:
            settings = await self._guild_settings(guild.id)
        configured = settings.get("rules_channel_id")
        channel_id = self._positive_int(configured, 0)
        return f"<#{channel_id}>" if channel_id > 0 else "#rules"

    async def _on_member_join(self, member: discord.Member) -> None:
        """Triggered when a new member joins."""
        if member.bot:
            return
        channel = await self._resolve_welcome_channel(member.guild)
        if channel is None:
            return
        await self.send_welcome(member, channel=channel)

    async def send_welcome(
        self,
        member: discord.Member,
        *,
        channel: Optional[discord.abc.Messageable] = None,
    ) -> bool:
        """Generate and send the welcome card + message."""
        if channel is None:
            channel = await self._resolve_welcome_channel(member.guild)
        if channel is None:
            return False

        settings = await self._guild_settings(member.guild.id)
        background_path = self._background_path_from_settings(settings)

        try:
            image_buffer, attachment_name = await asyncio.to_thread(
                _welcome_image_attachment, background_path
            )
            file = discord.File(image_buffer, filename=attachment_name)
            embed = discord.Embed(color=0x000000)
            embed.set_image(url=f"attachment://{attachment_name}")
            await channel.send(content=member.mention, embed=embed, file=file)
            return True
        except (discord.Forbidden, discord.HTTPException) as exc:
            LOGGER.warning("Failed to send welcome message: %s", exc)
            return False


def init_welcome_system(
    bot: commands.Bot,
) -> WelcomeSystem:
    """Factory function matching the contract expected by bot.py."""
    return WelcomeSystem(bot)
