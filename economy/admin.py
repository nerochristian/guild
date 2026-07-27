import asyncio
import logging
import math
import os
import re
import secrets

random = secrets.SystemRandom()
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from time import monotonic
from typing import Any, Optional, Dict, Iterable

import io

import discord
from discord.ext import commands
from discord.ext import tasks
import psycopg2
import psycopg2.extras
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps
from level_system import (
    _card_file,
    _background_data_from_settings,
    _circle_avatar,
    _download_image_bytes,
    _fit_text,
    _format_compact_number,
    _load_font,
    _member_card_name,
    _read_avatar,
    _read_background_bytes,
    _validate_image_url,
    _verify_image_bytes,
)
from components_v2 import (
    branded_panel_container,
    ensure_layout_view_action_rows,
    get_valk_emoji,
    thumbnail_text_section,
)
from v2_embed import apply_v2_embed_layout
from .core import (
    CreditAmountOutOfRange,
    MAX_BIGINT,
    _discord_timestamp,
    _parse_credit_amount,
)
from .pets import _match_pet_item, _match_pet_line, _pet_display_name
from .shop import _match_economy_item, _match_shop_item_name


async def send_v2(ctx, embed: discord.Embed, view: discord.ui.View = None, **kwargs):
    if view is None:
        view = discord.ui.LayoutView(timeout=None)
    files = []
    if kwargs.get("file") is not None:
        files.append(kwargs["file"])
    files.extend(kwargs.get("files") or [])
    if embed is None:
        if isinstance(view, discord.ui.LayoutView):
            ensure_layout_view_action_rows(view)
    else:
        apply_v2_embed_layout(view, embed=embed, files=files or None)
    return await ctx.send(view=view, **kwargs)


async def edit_v2(
    msg: discord.Message, embed: discord.Embed, view: discord.ui.View = None, **kwargs
):
    if view is None:
        view = discord.ui.LayoutView(timeout=None)
    files = []
    if kwargs.get("file") is not None:
        files.append(kwargs["file"])
    files.extend(kwargs.get("files") or [])
    if embed is None:
        if isinstance(view, discord.ui.LayoutView):
            ensure_layout_view_action_rows(view)
    else:
        apply_v2_embed_layout(view, embed=embed, files=files or None)
    return await msg.edit(view=view, **kwargs)


LOGGER = logging.getLogger("enzo-bot.economy-system")
ACCENT_COLOR = 0x000000
ECONOMY_LEADERBOARD_CARD_WIDTH = 800
ECONOMY_LEADERBOARD_CARD_HEIGHT = 680
DEFAULT_ECONOMY_ACCENT = 0xF1C40F
SHOP_CHANNEL_NOTICE_SECONDS = 20


# Constants
def _getenv_int(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        return int(raw_value)
    except ValueError:
        LOGGER.warning("Invalid %s value %r. Using %s.", name, raw_value, default)
        return default


def _coerce_optional_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


CREDITS_PER_MESSAGE = 10
SHOP_REFRESH_HOURS = 24
SHOP_REFRESH_INTERVAL = timedelta(hours=SHOP_REFRESH_HOURS)
SHOP_REFRESH_TIMES = tuple(
    time(hour=hour, minute=0, tzinfo=timezone.utc)
    for hour in range(0, 24, SHOP_REFRESH_HOURS)
)
ECONOMY_ADMIN_USER_IDS = {
    int(x)
    for x in os.environ.get("ECONOMY_ADMIN_USER_IDS", "1470873417614889124").split(",")
    if x.strip()
}
BIG_RESET_USER_IDS = {1470873417614889124, 1269772767516033025}
SHOP_ICON = "\U0001f6d2"
COIN_ICON = "\U0001fa99"
STOCK_ICON = "\U0001f4e6"
AVAILABLE_ICON = "\U0001f7e2"
SOLD_OUT_ICON = "\U0001f534"
RARITY_BADGES = {
    "Common": "\U000026aa",
    "Uncommon": "\U0001f535",
    "Rare": "\U0001f7e2",
    "Epic": "\U0001f7e3",
    "Legendary": "\U0001f7e1",
    "Mythic": "\U0001f7e0",
    "Ultimate": "\U0001f534",
}
SHOP_STOCK_CHANCES = {
    "Common": 1.00,
    "Uncommon": 0.85,
    "Rare": 0.55,
    "Epic": 0.30,
    "Legendary": 0.15,
    "Mythic": 0.08,
    "Ultimate": 0.04,
}
SHOP_COMMON_RARITIES = {"Common", "Uncommon"}


def economy_admin_only() -> commands.check:
    async def predicate(ctx: commands.Context) -> bool:
        if ctx.author.id in ECONOMY_ADMIN_USER_IDS:
            return True
        return (
            isinstance(ctx.author, discord.Member)
            and ctx.author.guild_permissions.administrator
        )

    return commands.check(predicate)


def _parse_toggle(raw: str) -> Optional[bool]:
    normalized = (raw or "").strip().lower()
    if normalized in {"on", "true", "yes", "y", "1", "enable", "enabled"}:
        return True
    if normalized in {"off", "false", "no", "n", "0", "disable", "disabled"}:
        return False
    return None


def _fmt_dt(value: Optional[datetime]) -> str:
    if value is None:
        return "None"
    return f"<t:{_discord_timestamp(value)}:R>"


def _parse_duration_minutes(raw: str) -> Optional[int]:
    cleaned = (raw or "").strip().lower().replace(",", "")
    if not cleaned:
        return None
    if cleaned in {"0", "clear", "none", "unjail", "release"}:
        return 0
    if cleaned.isdigit():
        return int(cleaned)

    total_minutes = 0
    matched = False
    pattern = re.compile(r"(\d+)\s*(w|week|weeks|d|day|days|h|hr|hrs|hour|hours|m|min|mins|minute|minutes)")
    position = 0
    for match in pattern.finditer(cleaned):
        if cleaned[position:match.start()].strip():
            return None
        matched = True
        amount = int(match.group(1))
        unit = match.group(2)
        if unit in {"w", "week", "weeks"}:
            total_minutes += amount * 7 * 24 * 60
        elif unit in {"d", "day", "days"}:
            total_minutes += amount * 24 * 60
        elif unit in {"h", "hr", "hrs", "hour", "hours"}:
            total_minutes += amount * 60
        else:
            total_minutes += amount
        position = match.end()
    if not matched or cleaned[position:].strip():
        return None
    return total_minutes


def _format_duration_minutes(minutes: int) -> str:
    minutes = max(0, int(minutes))
    if minutes == 0:
        return "0 minutes"
    parts: list[str] = []
    units = (
        ("week", 7 * 24 * 60),
        ("day", 24 * 60),
        ("hour", 60),
        ("minute", 1),
    )
    remaining = minutes
    for label, size in units:
        value, remaining = divmod(remaining, size)
        if value:
            plural = "s" if value != 1 else ""
            parts.append(f"{value} {label}{plural}")
    return " ".join(parts)


def _format_quantity_map(values: dict[str, int], definitions: dict[str, dict[str, Any]]) -> str:
    if not values:
        return "None"
    lines: list[str] = []
    for key, quantity in sorted(values.items()):
        if int(quantity) <= 0:
            continue
        name = str(definitions.get(key, {}).get("name") or key)
        lines.append(f"`{key}` {name}: **{int(quantity):,}**")
    return "\n".join(lines) if lines else "None"


def _resolve_blackmarket_item(raw_item: str) -> Optional[str]:
    cleaned = " ".join((raw_item or "").strip().casefold().replace("_", " ").split())
    if not cleaned:
        return None
    matched_tool = _match_economy_item(cleaned)
    if matched_tool:
        return matched_tool
    lock_aliases = {
        "1": "lock_1",
        "lock 1": "lock_1",
        "lock_1": "lock_1",
        "bronze": "lock_1",
        "bronze lock": "lock_1",
        "2": "lock_2",
        "lock 2": "lock_2",
        "lock_2": "lock_2",
        "steel": "lock_2",
        "steel lock": "lock_2",
        "3": "lock_3",
        "lock 3": "lock_3",
        "lock_3": "lock_3",
        "vault": "lock_3",
        "vault lock": "lock_3",
    }
    return lock_aliases.get(cleaned)


def _parse_item_amount(
    raw: str, matcher
) -> tuple[Optional[str], Optional[int], Optional[str]]:
    parts = (raw or "").strip().split()
    if len(parts) < 2:
        return None, None, "Use an item name and an amount."

    def parse_signed(token: str) -> Optional[int]:
        try:
            return int(str(token).replace(",", ""))
        except (TypeError, ValueError):
            return None

    first_amount = parse_signed(parts[0])
    last_amount = parse_signed(parts[-1])
    if first_amount is not None:
        item_name = " ".join(parts[1:])
        return matcher(item_name), first_amount, None
    if last_amount is not None:
        item_name = " ".join(parts[:-1])
        return matcher(item_name), last_amount, None
    return None, None, "Amount must be a whole number."


def _chunk_text(lines: Iterable[str], limit: int = 950) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in lines:
        line_len = len(line) + 1
        if current and current_len + line_len > limit:
            chunks.append("\n".join(current))
            current = []
            current_len = 0
        current.append(line)
        current_len += line_len
    if current:
        chunks.append("\n".join(current))
    return chunks


def _format_stock_bar(stock: int, max_stock: int, width: int = 10) -> str:
    max_stock = max(1, int(max_stock))
    stock = max(0, min(int(stock), max_stock))
    filled = round((stock / max_stock) * width)
    if stock > 0:
        filled = max(1, filled)
    filled = min(width, filled)
    return "#" * filled + "-" * (width - filled)


def _roll_shop_stock(item: Dict[str, Any]) -> int:
    max_stock = max(0, int(item.get("max_stock", 0)))
    if max_stock <= 0:
        return 0

    rarity = str(item.get("rarity", "Common"))
    chance = SHOP_STOCK_CHANCES.get(rarity, 0.50)
    if random.random() > chance:
        return 0

    if rarity in SHOP_COMMON_RARITIES:
        minimum_stock = max(1, round(max_stock * 0.65))
    else:
        minimum_stock = 1
    return random.randint(minimum_stock, max_stock)


def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _shop_refresh_period_start(reference: Optional[datetime] = None) -> datetime:
    now = _as_utc(reference) or datetime.now(timezone.utc)
    period_hour = (now.hour // SHOP_REFRESH_HOURS) * SHOP_REFRESH_HOURS
    return now.replace(hour=period_hour, minute=0, second=0, microsecond=0)


def _next_shop_refresh_at(reference: Optional[datetime] = None) -> datetime:
    now = _as_utc(reference) or datetime.now(timezone.utc)
    period_start = _shop_refresh_period_start(now)
    return period_start + SHOP_REFRESH_INTERVAL


def _shop_refresh_due(
    last_refresh: Optional[datetime], reference: Optional[datetime] = None
) -> bool:
    refreshed_at = _as_utc(last_refresh)
    if refreshed_at is None:
        return True
    return refreshed_at < _shop_refresh_period_start(reference)


def _format_restock_countdown(last_refresh: Optional[datetime]) -> str:
    next_refresh = _next_shop_refresh_at()
    return f"<t:{int(next_refresh.timestamp())}:R>"


def _color_tuple(color: int, alpha: int = 255) -> tuple[int, int, int, int]:
    return ((color >> 16) & 255, (color >> 8) & 255, color & 255, alpha)


def _settings_accent(
    settings: Optional[dict[str, Any]], fallback: int = DEFAULT_ECONOMY_ACCENT
) -> int:
    raw_color = (settings or {}).get("accent_color")
    try:
        return int(raw_color) if raw_color is not None else fallback
    except (TypeError, ValueError):
        return fallback


# Shop Configuration
SHOP_ITEMS = {
    "Cutieeeeees": {
        "price": 250000,
        "max_stock": 20,
        "rarity": "Common",
        "color": 0xFFB6C1,
    },
    "Serenity": {
        "price": 750000,
        "max_stock": 15,
        "rarity": "Uncommon",
        "color": 0x3498DB,
    },
    "Ascendant": {
        "price": 1500000,
        "max_stock": 10,
        "rarity": "Rare",
        "color": 0x2ECC71,
    },
    "Royalty": {"price": 2500000, "max_stock": 5, "rarity": "Epic", "color": 0x9B59B6},
    "I Never Wanted It...": {
        "price": 3750000,
        "max_stock": 3,
        "rarity": "Legendary",
        "color": 0xF1C40F,
    },
    "Divinity": {
        "price": 4500000,
        "max_stock": 2,
        "rarity": "Mythic",
        "color": 0xE67E22,
    },
    "The One Who Remains": {
        "price": 5000000,
        "max_stock": 1,
        "rarity": "Ultimate",
        "color": 0xE74C3C,
    },
}

PRESTIGE_MAX_LEVEL = 10
PRESTIGE_BASE_COST = 5_000_000
PRESTIGE_COST_STEP = 2_500_000
PRESTIGE_BONUS_PER_LEVEL = 0.03
BOOSTER_BONUS_RATE = 0.10
TOTAL_BONUS_CAPS = {
    "gambling": 0.60,
    "passive": 0.50,
    "default": 0.75,
}
SHOP_ROLE_PERKS = {
    "Cutieeeeees": {
        "bonus_type": "daily",
        "bonus_value": 0.02,
        "label": "Daily rewards +2%",
    },
    "Serenity": {
        "bonus_type": "work_fish",
        "bonus_value": 0.04,
        "label": "Work and fishing +4%",
    },
    "Ascendant": {
        "bonus_type": "active",
        "bonus_value": 0.05,
        "label": "Active economy rewards +5%",
    },
    "Royalty": {
        "bonus_type": "gambling",
        "bonus_value": 0.08,
        "label": "Casino profits +8%",
    },
    "I Never Wanted It...": {
        "bonus_type": "crime",
        "bonus_value": 0.10,
        "label": "Crime rewards +10%",
    },
    "Divinity": {
        "bonus_type": "passive",
        "bonus_value": 0.12,
        "label": "Passive pet income +12%",
    },
    "The One Who Remains": {
        "bonus_type": "all",
        "bonus_value": 0.15,
        "label": "All perked payouts +15%",
    },
}
ROLE_BONUS_CAP = 0.30


def _prestige_cost(current_level: int) -> int:
    return PRESTIGE_BASE_COST + max(0, int(current_level)) * PRESTIGE_COST_STEP


def _bonus_type_applies(bonus_type: Optional[str], action: str) -> bool:
    if not bonus_type:
        return False
    if bonus_type == "all":
        return True
    if action == "daily":
        return bonus_type in {"daily", "active"}
    if action == "work":
        return bonus_type in {"work", "work_fish", "active"}
    if action in {"fish", "hunt"}:
        return bonus_type in {"fish_hunt", "work_fish", "active"}
    if action == "crime":
        return bonus_type in {"crime", "gambling_crime", "active"}
    if action == "gambling":
        return bonus_type in {"gambling", "gambling_crime"}
    if action == "passive":
        return bonus_type == "passive"
    return bonus_type == "active"


def _welcome_asset_path() -> Optional[Path]:
    assets_dir = Path(__file__).resolve().parent.parent / "assets"
    for filename in (
        "welcome_img.gif",
        "welcome_img.png",
        "welcome_img.jpg",
        "welcome_img.jpeg",
    ):
        path = assets_dir / filename
        if path.exists():
            return path
    return None


PET_MAX_ACCRUAL_HOURS = 24 * 7
PET_HAPPINESS_DECAY_PER_DAY = 5
PET_PLAY_COOLDOWN = timedelta(hours=12)
PET_MAX_OWNED = 15
PET_MAX_LEGENDARY_OR_FUSED = 3
PET_FEED_COSTS = {1: 4_000, 2: 6_000, 3: 8_000, 4: 10_500, 5: 12_500}
PET_ACTIVE_BONUS_CAP = 0.25
PET_GAMBLING_BONUS_CAP = 0.22
PET_PASSIVE_BONUS_CAP = 0.15
PET_LUCK_BONUS_CAP = 0.12
PET_RARITY_COLORS = {
    "Common": 0x95A5A6,
    "Uncommon": 0x3498DB,
    "Rare": 0x2ECC71,
    "Epic": 0x9B59B6,
    "Legendary": 0xF1C40F,
    "Hybrid": 0xE67E22,
}
PET_ITEM_DEFS = {
    "evolve_crystal": {
        "name": "Evolve Crystal",
        "aliases": {"evolve crystal", "evolve_crystal", "crystal", "ecrystal"},
    },

    "soul_shard": {
        "name": "Soul Shard",
        "aliases": {"soul shard", "soul_shard", "shard", "shards", "soulshard"},
    },
    "fusion_core": {
        "name": "Fusion Core",
        "aliases": {
            "fusion core",
            "fusion_core",
            "core",
            "basic",
            "basic core",
            "fusioncore",
        },
    },
    "greater_fusion_core": {
        "name": "Greater Fusion Core",
        "aliases": {
            "greater fusion core",
            "greater_fusion_core",
            "greater",
            "greater core",
            "gcore",
        },
    },
}
PET_CORE_SHOP = {
    "evolve_crystal": {"price": 500_000, "daily_stock": 5},

    "soul_shard": {"price": 10_000, "daily_stock": 999_999_999},
    "fusion_core": {"price": 1_600_000, "daily_stock": 8},
    "greater_fusion_core": {"price": 4_000_000, "daily_stock": 3},
}
PET_INFINITE_STOCK_THRESHOLD = 999_999_999
PET_LINES = {
    "kitsune": {
        "family": "Kitsune",
        "theme": "Gambling Luck Boost and casino profit",
        "stages": [
            {
                "name": "Kitsune Cub",
                "rarity": "Common",
                "price": 85_000,
                "daily_income": 1_100,
                "bonus_type": "gambling",
                "bonus_value": 0.03,
                "luck_bonus": 0.02,
            },
            {
                "name": "Shadow Kitsune",
                "rarity": "Uncommon",
                "daily_income": 4_000,
                "bonus_type": "gambling",
                "bonus_value": 0.06,
                "luck_bonus": 0.04,
                "evolve_credits": 210_000,
                "evolve_shards": 10,
                "time_days": 1,
            },
            {
                "name": "Eclipse Kitsune",
                "rarity": "Rare",
                "daily_income": 9_500,
                "bonus_type": "gambling",
                "bonus_value": 0.09,
                "luck_bonus": 0.06,
                "evolve_credits": 575_000,
                "evolve_shards": 25,
                "time_days": 2,
            },
            {
                "name": "Void Kitsune",
                "rarity": "Epic",
                "daily_income": 22_000,
                "bonus_type": "gambling",
                "bonus_value": 0.13,
                "luck_bonus": 0.08,
                "evolve_credits": 1_700_000,
                "evolve_shards": 50,
                "time_days": 3,
            },
            {
                "name": "Nine-Tails Sovereign",
                "rarity": "Legendary",
                "daily_income": 56_000,
                "bonus_type": "gambling",
                "bonus_value": 0.18,
                "luck_bonus": 0.10,
                "evolve_credits": 4_750_000,
                "evolve_shards": 100,
                "time_days": 5,
            },
        ],
    },
    "oni": {
        "family": "Oni",
        "theme": "Faster cooldowns and task earnings",
        "stages": [
            {
                "name": "Oni Spawn",
                "rarity": "Common",
                "price": 70_000,
                "daily_income": 1_050,
                "bonus_type": "active",
                "bonus_value": 0.03,
                "cooldown_reduction": 0.03,
            },
            {
                "name": "Horned Oni",
                "rarity": "Uncommon",
                "daily_income": 3_800,
                "bonus_type": "active",
                "bonus_value": 0.07,
                "evolve_credits": 200_000,
                "evolve_shards": 10,
                "time_days": 1,
                "cooldown_reduction": 0.07,
            },
            {
                "name": "Blood Oni",
                "rarity": "Rare",
                "daily_income": 9_000,
                "bonus_type": "active",
                "bonus_value": 0.10,
                "evolve_credits": 550_000,
                "evolve_shards": 25,
                "time_days": 2,
                "cooldown_reduction": 0.12,
            },
            {
                "name": "Tyrant Oni",
                "rarity": "Epic",
                "daily_income": 21_000,
                "bonus_type": "active",
                "bonus_value": 0.14,
                "evolve_credits": 1_650_000,
                "evolve_shards": 50,
                "time_days": 3,
                "cooldown_reduction": 0.18,
            },
            {
                "name": "Hellfire Oni Lord",
                "rarity": "Legendary",
                "daily_income": 54_000,
                "bonus_type": "active",
                "bonus_value": 0.18,
                "evolve_credits": 4_600_000,
                "evolve_shards": 100,
                "time_days": 5,
                "cooldown_reduction": 0.25,
            },
        ],
    },
    "seraphim": {
        "family": "Seraphim",
        "theme": "Highest passive income",
        "stages": [
            {
                "name": "Lesser Seraph",
                "rarity": "Common",
                "price": 110_000,
                "daily_income": 1_250,
                "bonus_type": "passive",
                "bonus_value": 0.04,
            },
            {
                "name": "Winged Seraph",
                "rarity": "Uncommon",
                "daily_income": 4_800,
                "bonus_type": "daily",
                "bonus_value": 0.08,
                "evolve_credits": 225_000,
                "evolve_shards": 10,
                "time_days": 1,
            },
            {
                "name": "Radiant Seraph",
                "rarity": "Rare",
                "daily_income": 12_000,
                "bonus_type": "passive",
                "bonus_value": 0.10,
                "evolve_credits": 600_000,
                "evolve_shards": 25,
                "time_days": 2,
            },
            {
                "name": "Arch-Seraph",
                "rarity": "Epic",
                "daily_income": 28_000,
                "bonus_type": "daily",
                "bonus_value": 0.16,
                "evolve_credits": 1_800_000,
                "evolve_shards": 50,
                "time_days": 3,
            },
            {
                "name": "Seraphim Sovereign",
                "rarity": "Legendary",
                "daily_income": 68_000,
                "bonus_type": "all",
                "bonus_value": 0.15,
                "evolve_credits": 5_000_000,
                "evolve_shards": 100,
                "time_days": 5,
            },
        ],
    },
    "abyssal": {
        "family": "Abyssal",
        "theme": "High-risk gambling multiplier",
        "stages": [
            {
                "name": "Abyssal Wisp",
                "rarity": "Common",
                "price": 95_000,
                "daily_income": 1_000,
                "bonus_type": "gambling",
                "bonus_value": 0.02,
                "gamble_multiplier": 0.03,
            },
            {
                "name": "Void Stalker",
                "rarity": "Uncommon",
                "daily_income": 3_600,
                "bonus_type": "gambling",
                "bonus_value": 0.05,
                "evolve_credits": 190_000,
                "evolve_shards": 10,
                "time_days": 1,
                "gamble_multiplier": 0.07,
            },
            {
                "name": "Eclipse Wraith",
                "rarity": "Rare",
                "daily_income": 8_600,
                "bonus_type": "gambling",
                "bonus_value": 0.08,
                "evolve_credits": 525_000,
                "evolve_shards": 25,
                "time_days": 2,
                "gamble_multiplier": 0.12,
            },
            {
                "name": "Abyssal Reaver",
                "rarity": "Epic",
                "daily_income": 20_000,
                "bonus_type": "gambling",
                "bonus_value": 0.12,
                "evolve_credits": 1_600_000,
                "evolve_shards": 50,
                "time_days": 3,
                "gamble_multiplier": 0.20,
            },
            {
                "name": "Void Sovereign",
                "rarity": "Legendary",
                "daily_income": 58_000,
                "bonus_type": "gambling",
                "bonus_value": 0.16,
                "evolve_credits": 4_900_000,
                "evolve_shards": 100,
                "time_days": 5,
                "gamble_multiplier": 0.30,
            },
        ],
    },
    "leviathan": {
        "family": "Leviathan",
        "theme": "Rare fishing jackpot and fishing mastery",
        "stages": [
            {
                "name": "Leviathan Hatchling",
                "rarity": "Legendary",
                "price": 5_400_000,
                "daily_income": 18_000,
                "bonus_type": "fish_hunt",
                "bonus_value": 0.08,
            },
            {
                "name": "Tide Leviathan",
                "rarity": "Legendary",
                "daily_income": 34_000,
                "bonus_type": "fish_hunt",
                "bonus_value": 0.11,
                "evolve_credits": 1_250_000,
                "evolve_shards": 10,
                "time_days": 1,
            },
            {
                "name": "Abyss Leviathan",
                "rarity": "Mythic",
                "daily_income": 52_000,
                "bonus_type": "fish_hunt",
                "bonus_value": 0.14,
                "evolve_credits": 2_750_000,
                "evolve_shards": 25,
                "time_days": 2,
            },
            {
                "name": "Elder Leviathan",
                "rarity": "Mythic",
                "daily_income": 76_000,
                "bonus_type": "fish_hunt",
                "bonus_value": 0.18,
                "evolve_credits": 5_500_000,
                "evolve_shards": 50,
                "time_days": 3,
            },
            {
                "name": "Worldscale Leviathan",
                "rarity": "Mythic",
                "daily_income": 110_000,
                "bonus_type": "fish_hunt",
                "bonus_value": 0.24,
                "evolve_credits": 10_000_000,
                "evolve_shards": 100,
                "time_days": 5,
            },
        ],
    },
    "kraken": {
        "family": "Kraken",
        "theme": "Ultra-rare fishing drop and abyssal luck",
        "available_in_shop": False,
        "stages": [
            {
                "name": "Kraken Spawn",
                "rarity": "Mythic",
                "price": 25_000_000,
                "daily_income": 32_000,
                "bonus_type": "fish_hunt",
                "bonus_value": 0.10,
                "luck_bonus": 0.04,
            },
            {
                "name": "Ink-Tide Kraken",
                "rarity": "Mythic",
                "daily_income": 58_000,
                "bonus_type": "fish_hunt",
                "bonus_value": 0.15,
                "luck_bonus": 0.06,
                "evolve_credits": 3_000_000,
                "evolve_shards": 25,
                "time_days": 2,
            },
            {
                "name": "Abyssal Kraken",
                "rarity": "Mythic",
                "daily_income": 88_000,
                "bonus_type": "fish_hunt",
                "bonus_value": 0.20,
                "luck_bonus": 0.08,
                "evolve_credits": 6_500_000,
                "evolve_shards": 50,
                "time_days": 3,
            },
            {
                "name": "Storm-Maw Kraken",
                "rarity": "Ultimate",
                "daily_income": 125_000,
                "bonus_type": "work_fish",
                "bonus_value": 0.24,
                "luck_bonus": 0.10,
                "evolve_credits": 12_000_000,
                "evolve_shards": 100,
                "time_days": 5,
            },
            {
                "name": "Infinite Deep Kraken",
                "rarity": "Ultimate",
                "daily_income": 175_000,
                "bonus_type": "all",
                "bonus_value": 0.20,
                "luck_bonus": 0.12,
                "evolve_credits": 20_000_000,
                "evolve_shards": 200,
                "time_days": 7,
            },
        ],
    },
}
PET_HYBRIDS = {
    frozenset(("kitsune", "oni")): {
        "name": "Kage-Oni Dreadfox",
        "daily_income": 78_000,
        "bonuses": [
            {"type": "gambling", "value": 0.22},
            {"type": "active", "value": 0.22},
        ],
        "luck_bonus": 0.12,
        "cooldown_reduction": 0.30,
    },
    frozenset(("kitsune", "seraphim")): {
        "name": "Astral Emissary Kitsune",
        "daily_income": 85_000,
        "bonuses": [
            {"type": "gambling", "value": 0.22},
            {"type": "all", "value": 0.18},
        ],
        "luck_bonus": 0.12,
    },
    frozenset(("kitsune", "abyssal")): {
        "name": "Void-Tail Sorcerer",
        "daily_income": 72_000,
        "bonuses": [
            {"type": "gambling", "value": 0.25},
        ],
        "luck_bonus": 0.12,
        "gamble_multiplier": 0.15,
    },
    frozenset(("oni", "seraphim")): {
        "name": "Equinox Archdemon",
        "daily_income": 82_000,
        "bonuses": [
            {"type": "active", "value": 0.22},
            {"type": "all", "value": 0.18},
        ],
        "cooldown_reduction": 0.30,
    },
    frozenset(("oni", "abyssal")): {
        "name": "Netherworld Emperor",
        "daily_income": 75_000,
        "bonuses": [
            {"type": "active", "value": 0.22},
            {"type": "gambling", "value": 0.18},
        ],
        "cooldown_reduction": 0.30,
        "gamble_multiplier": 0.15,
    },
    frozenset(("seraphim", "abyssal")): {
        "name": "Twilight Archangel",
        "daily_income": 80_000,
        "bonuses": [
            {"type": "all", "value": 0.18},
            {"type": "gambling", "value": 0.18},
        ],
        "gamble_multiplier": 0.15,
    },
    frozenset(("leviathan", "abyssal")): {
        "name": "Abyssal Tidecaller",
        "daily_income": 70_000,
        "bonuses": [
            {"type": "work_fish", "value": 0.24},
            {"type": "gambling", "value": 0.18},
        ],
        "fishing_luck": 0.18,
        "gamble_multiplier": 0.15,
    },
    frozenset(("leviathan", "seraphim")): {
        "name": "Elysian Tide-Lord",
        "daily_income": 78_000,
        "bonuses": [
            {"type": "work_fish", "value": 0.24},
            {"type": "all", "value": 0.18},
        ],
        "fishing_luck": 0.18,
    },
    frozenset(("leviathan", "oni")): {
        "name": "Volcanic Levi-Oni",
        "daily_income": 76_000,
        "bonuses": [
            {"type": "work_fish", "value": 0.24},
            {"type": "active", "value": 0.22},
        ],
        "fishing_luck": 0.18,
        "cooldown_reduction": 0.30,
    },
    frozenset(("leviathan", "kitsune")): {
        "name": "Oceanic Spectral-Fox",
        "daily_income": 74_000,
        "bonuses": [
            {"type": "work_fish", "value": 0.24},
            {"type": "gambling", "value": 0.22},
        ],
        "fishing_luck": 0.18,
        "luck_bonus": 0.12,
    },
}
ASSET_DIR = Path(__file__).resolve().parent.parent / "assets"
PET_ASSET_DIR = ASSET_DIR / "pets"
PET_SPRITE_DIR = ASSET_DIR / "sprites"
PET_SPRITE_PREFIXES = {
    "kitsune": "fox",
    "oni": "demon",
    "seraphim": "angel",
    "abyssal": "shadow",
    "leviathan": "leviathan",
    "kraken": "kraken",
}
PET_LEGACY_IMAGE_FILES = {
    "catalog": "pet_catalog.png",
    "kitsune": "kitsune_evolution.png",
    "oni": "oni_evolution.png",
    "seraphim": "seraphim_evolution.png",
    "abyssal": "abyssal_evolution.png",
}
for _name in ["kitsune", "oni", "seraphim", "abyssal", "kraken"]:
    for _i in range(1, 6):
        PET_LEGACY_IMAGE_FILES[f"{_name}_{_i}"] = f"{_name}_{_i}.png"
ECONOMY_ITEM_DEFS = {
    "lockpick": {
        "name": "Lockpick",
        "price": 7_500,
        "desc": "Basic tool for jewelry heists.",
        "max_stock": 100,
        "rarity": "Common",
        "aliases": {"lockpick", "pick", "lock"},
    },
    "advanced_lockpick": {
        "name": "Advanced Lockpick",
        "price": 37_500,
        "desc": "Increases success rate in jewelry heists.",
        "max_stock": 40,
        "rarity": "Uncommon",
        "aliases": {"advanced lockpick", "advanced", "apick", "advanced_lockpick"},
    },
    "drill": {
        "name": "Vault Drill",
        "price": 175_000,
        "desc": "Required for bank robberies.",
        "max_stock": 15,
        "rarity": "Rare",
        "aliases": {"drill", "vault drill", "vault_drill"},
    },
    "fake_id": {
        "name": "Fake ID",
        "price": 75_000,
        "desc": "Reduces your wanted level when caught.",
        "max_stock": 8,
        "rarity": "Epic",
        "aliases": {"fake id", "fake_id", "id"},
    },
    "getaway_car": {
        "name": "Getaway Car",
        "price": 400_000,
        "desc": "Greatly increases heist success odds and avoids jail.",
        "max_stock": 3,
        "rarity": "Legendary",
        "aliases": {"getaway car", "car", "getaway"},
    },
}
ECONOMY_ITEM_DEFS.update(
    {
        "fishing_rod_ii": {
            "name": "Reinforced Fishing Rod",
            "price": 250_000,
            "desc": "Fishing luck +5%; fishing cooldown -5%.",
            "max_stock": 0,
            "rarity": "Uncommon",
            "aliases": {"rod ii", "fishing rod ii", "reinforced rod", "better rod"},
        },
        "fishing_rod_iii": {
            "name": "Abyssal Fishing Rod",
            "price": 900_000,
            "desc": "Fishing luck +10%; fishing cooldown -10%.",
            "max_stock": 0,
            "rarity": "Rare",
            "aliases": {"rod iii", "fishing rod iii", "abyssal rod", "best rod"},
        },
        "pickaxe_ii": {
            "name": "Steel Pickaxe",
            "price": 250_000,
            "desc": "Mining luck +5%; mining cooldown -5%.",
            "max_stock": 0,
            "rarity": "Uncommon",
            "aliases": {"pickaxe ii", "steel pickaxe", "better pickaxe"},
        },
        "pickaxe_iii": {
            "name": "Crystal Pickaxe",
            "price": 900_000,
            "desc": "Mining luck +10%; mining cooldown -10%.",
            "max_stock": 0,
            "rarity": "Rare",
            "aliases": {"pickaxe iii", "crystal pickaxe", "best pickaxe"},
        },
        "hunting_weapon_ii": {
            "name": "Hunter's Bow",
            "price": 250_000,
            "desc": "Hunting luck +5%; hunting cooldown -5%.",
            "max_stock": 0,
            "rarity": "Uncommon",
            "aliases": {"bow", "hunter bow", "weapon ii", "hunting weapon ii"},
        },
        "hunting_weapon_iii": {
            "name": "Phantom Rifle",
            "price": 900_000,
            "desc": "Hunting luck +10%; hunting cooldown -10%.",
            "max_stock": 0,
            "rarity": "Rare",
            "aliases": {"rifle", "phantom rifle", "weapon iii", "hunting weapon iii"},
        },
        "luck_potion_i": {
            "name": "Luck Potion I",
            "price": 100_000,
            "desc": "Luck +10% for 5 minutes. Does not affect coinflip or All or Nothing.",
            "max_stock": 0,
            "rarity": "Uncommon",
            "aliases": {"luck i", "luck potion i", "luck1", "luck potion 1"},
        },
        "luck_potion_ii": {
            "name": "Luck Potion II",
            "price": 300_000,
            "desc": "Luck +15% for 5 minutes. Does not affect coinflip or All or Nothing.",
            "max_stock": 0,
            "rarity": "Rare",
            "aliases": {"luck ii", "luck potion ii", "luck2", "luck potion 2"},
        },
        "luck_potion_iii": {
            "name": "Luck Potion III",
            "price": 700_000,
            "desc": "Luck +20% for 5 minutes. Does not affect coinflip or All or Nothing.",
            "max_stock": 0,
            "rarity": "Epic",
            "aliases": {"luck iii", "luck potion iii", "luck3", "luck potion 3"},
        },
        "defense_potion_i": {
            "name": "Defense Potion I",
            "price": 100_000,
            "desc": "Incoming rob chance -10% for 10 minutes.",
            "max_stock": 0,
            "rarity": "Uncommon",
            "aliases": {"defense i", "defence i", "defense potion", "def potion"},
        },
        "shop_reset_token": {
            "name": "Shop Reset Button",
            "price": 100_000,
            "desc": "Instantly refreshes role shop and blackmarket stock.",
            "max_stock": 0,
            "rarity": "Rare",
            "aliases": {"reset shop", "shop reset", "shop button"},
        },
        "cooldown_reset_token": {
            "name": "Cooldown Reset Button",
            "price": 150_000,
            "desc": "Clears one economy command cooldown.",
            "max_stock": 0,
            "rarity": "Epic",
            "aliases": {"reset cooldown", "cooldown reset", "cd reset", "cooldown button"},
        },
    }
)
LOCK_LEVELS = {
    0: {
        "name": "No Lock",
        "price": 0,
        "penalty": 0.0,
        "desc": "No protection.",
        "max_stock": 0,
    },
    1: {
        "name": "Bronze Lock",
        "price": 125_000,
        "penalty": 0.08,
        "desc": "Lowers incoming .rob success by 8%.",
        "max_stock": 25,
        "rarity": "Uncommon",
    },
    2: {
        "name": "Steel Lock",
        "price": 500_000,
        "penalty": 0.16,
        "desc": "Lowers incoming .rob success by 16%.",
        "max_stock": 10,
        "rarity": "Rare",
    },
    3: {
        "name": "Vault Lock",
        "price": 2_000_000,
        "penalty": 0.28,
        "desc": "Lowers incoming .rob success by 28%.",
        "max_stock": 4,
        "rarity": "Epic",
    },
}
ROBBERY_COOLDOWNS = {
    "rob": timedelta(minutes=30),
    "jewelry": timedelta(hours=3),
    "bank": timedelta(hours=8),
    "team_bank": timedelta(hours=12),
}
ROBBERY_COOLDOWN_COLUMNS = {
    "rob": "last_rob_at",
    "jewelry": "last_jewelry_heist_at",
    "bank": "last_bank_heist_at",
    "team_bank": "last_team_heist_at",
}
HEIST_STEP_RISKS = {
    "jewelry": (0.10, 0.16, 0.18, 0.14),
    "bank": (0.14, 0.20, 0.24, 0.18),
    "team_bank": (0.12, 0.18, 0.22, 0.16),
}
HEIST_DEFS = {
    "jewelry": {
        "title": "Jewelry Theft",
        "base_success": 0.42,
        "min_payout": 35_000,
        "max_payout": 120_000,
        "fail_min": 35_000,
        "fail_max": 125_000,
        "wanted": 2,
        "jail_minutes": 30,
        "items": {"lockpick": 2},
    },
    "bank": {
        "title": "Bank Robbery",
        "base_success": 0.25,
        "min_payout": 120_000,
        "max_payout": 420_000,
        "fail_min": 20_000,
        "fail_max": 50_000,
        "wanted": 3,
        "jail_minutes": 75,
        "items": {"lockpick": 3, "drill": 1},
    },
    "team_bank": {
        "title": "Team Bank Robbery",
        "base_success": 0.33,
        "min_payout": 450_000,
        "max_payout": 1_250_000,
        "fail_min": 30_000,
        "fail_max": 50_000,
        "wanted": 3,
        "jail_minutes": 90,
        "items": {"lockpick": 2},
        "leader_items": {"drill": 1},
    },
}
CASINO_BET_LIMITS = {
    "slots": {"min": 50, "max": 250_000},
    "coinflip": {"min": 50, "max": 100_000},
    "dice": {"min": 50, "max": 150_000},
    "multidice": {"min": 50, "max": 150_000},
    "uno": {"min": 100, "max": 100_000},
    "roulette": {"min": 125, "max": 300_000},
    "blackjack": {"min": 50, "max": 75_000},
    "mines": {"min": 125, "max": 100_000},
    "poker": {"min": 250, "max": 75_000},
    "default": {"min": 50, "max": 100_000},
}
BLACKJACK_PAYOUT = 2.0
ALL_OR_NOTHING_MIN_BALANCE = 10_000
ALL_OR_NOTHING_WIN_CHANCE = 0.10
ALL_OR_NOTHING_WIN_MULTIPLIER = 5
ALL_OR_NOTHING_COOLDOWN = timedelta(days=7)




class AdminStoreMixin:
    def reset_member_cooldowns(self, guild_id: int, user_id: int) -> dict[str, int]:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "DELETE FROM member_cooldowns WHERE guild_id = %s AND user_id = %s",
                        (guild_id, user_id),
                    )
                    command_rows = cursor.rowcount
                    cursor.execute(
                        """
                        UPDATE member_security
                        SET
                            wanted_level = 0,
                            jail_until = NULL,
                            last_rob_at = NULL,
                            last_jewelry_heist_at = NULL,
                            last_bank_heist_at = NULL,
                            last_team_heist_at = NULL,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE guild_id = %s AND user_id = %s
                        """,
                        (guild_id, user_id),
                    )
                    robbery_rows = cursor.rowcount
            return {
                "command_cooldowns": command_rows,
                "robbery_cooldowns": robbery_rows,
                "jail_cleared": robbery_rows,
            }
        finally:
            self._pool.putconn(conn)

    def get_member_cooldowns(self, guild_id: int, user_id: int) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT command_name, expires_at
                    FROM member_cooldowns
                    WHERE guild_id = %s AND user_id = %s
                    ORDER BY expires_at ASC, command_name ASC
                    """,
                    (guild_id, user_id),
                )
                return [dict(row) for row in cursor.fetchall()]
        finally:
            self._pool.putconn(conn)

    def clear_member_cooldowns(
        self, guild_id: int, user_id: int, command_name: Optional[str] = None
    ) -> int:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    if command_name:
                        cursor.execute(
                            """
                            DELETE FROM member_cooldowns
                            WHERE guild_id = %s AND user_id = %s AND command_name = %s
                            """,
                            (guild_id, user_id, command_name),
                        )
                    else:
                        cursor.execute(
                            "DELETE FROM member_cooldowns WHERE guild_id = %s AND user_id = %s",
                            (guild_id, user_id),
                        )
                    return max(0, cursor.rowcount)
        finally:
            self._pool.putconn(conn)

    def set_member_cooldown_admin(
        self, guild_id: int, user_id: int, command_name: str, minutes: int
    ) -> Optional[datetime]:
        command_name = " ".join((command_name or "").strip().lower().split())
        if not command_name:
            return None
        minutes = max(0, int(minutes))
        if minutes == 0:
            self.clear_member_cooldowns(guild_id, user_id, command_name)
            return None

        expires_at = datetime.now(timezone.utc) + timedelta(minutes=minutes)
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO member_cooldowns (guild_id, user_id, command_name, expires_at)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (guild_id, user_id, command_name) DO UPDATE SET
                            expires_at = EXCLUDED.expires_at
                        RETURNING expires_at
                        """,
                        (guild_id, user_id, command_name, expires_at),
                    )
                    row = cursor.fetchone()
                    return row["expires_at"] if row else expires_at
        finally:
            self._pool.putconn(conn)

    def get_member_admin_snapshot(self, guild_id: int, user_id: int) -> dict[str, Any]:
        wallet, bank = self.get_balances(guild_id, user_id)
        active_games = [
            row
            for row in self.get_active_casino_games()
            if int(row.get("guild_id") or 0) == int(guild_id)
            and int(row.get("user_id") or 0) == int(user_id)
        ]
        return {
            "wallet": int(wallet),
            "bank": int(bank),
            "economy_items": self.get_economy_items(guild_id, user_id),
            "pet_items": self.get_pet_items(guild_id, user_id),
            "pets": self.get_user_pets(guild_id, user_id),
            "security": self.get_member_security(guild_id, user_id),
            "perks": self.get_member_perks(guild_id, user_id),
            "purchased_roles": self.get_purchased_roles(guild_id, user_id),
            "investments": self.get_user_investments(guild_id, user_id, 10),
            "cooldowns": self.get_member_cooldowns(guild_id, user_id),
            "active_games": active_games,
        }

    def reset_member_economy(self, guild_id: int, user_id: int) -> tuple[int, int]:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO member_economy (guild_id, user_id, credits, bank, updated_at)
                        VALUES (%s, %s, 0, 0, CURRENT_TIMESTAMP)
                        ON CONFLICT (guild_id, user_id) DO UPDATE SET
                            credits = 0,
                            bank = 0,
                            updated_at = CURRENT_TIMESTAMP
                        RETURNING credits, bank
                        """,
                        (guild_id, user_id),
                    )
                    row = cursor.fetchone()
                    return int(row["credits"]), int(row["bank"])
        finally:
            self._pool.putconn(conn)

    def wipe_member_completely(self, guild_id: int, user_id: int) -> dict[str, int]:
        counts: dict[str, int] = {}
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO member_economy (guild_id, user_id, credits, bank, updated_at)
                        VALUES (%s, %s, 0, 0, CURRENT_TIMESTAMP)
                        ON CONFLICT (guild_id, user_id) DO UPDATE SET
                            credits = 0,
                            bank = 0,
                            updated_at = CURRENT_TIMESTAMP
                        """,
                        (guild_id, user_id),
                    )
                    counts["member_economy"] = 1
                    for table in (
                        "active_casino_games",
                        "member_cooldowns",
                        "member_purchases",
                        "member_security",
                        "member_perks",
                        "member_effects",
                        "pet_fusion_history",
                        "user_pets",
                        "pet_items",
                        "economy_items",
                    ):
                        cursor.execute(
                            f"DELETE FROM {table} WHERE guild_id = %s AND user_id = %s",
                            (guild_id, user_id),
                        )
                        counts[table] = max(0, cursor.rowcount)
                    cursor.execute(
                        """
                        UPDATE member_investments
                        SET status = 'cancelled',
                            return_amount = 0,
                            multiplier = 0,
                            settled_at = CURRENT_TIMESTAMP
                        WHERE guild_id = %s AND user_id = %s AND status = 'active'
                        """,
                        (guild_id, user_id),
                    )
                    counts["member_investments"] = max(0, cursor.rowcount)
            return counts
        finally:
            self._pool.putconn(conn)

    def set_blackmarket_admin_entry(
        self,
        guild_id: int,
        item_key: str,
        *,
        stock: Optional[int] = None,
        discount: Optional[int] = None,
    ) -> dict[str, Any]:
        if stock is not None:
            stock = max(0, int(stock))
        if discount is not None:
            discount = max(0, min(100, int(discount)))

        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT current_stock, sale_discount
                        FROM blackmarket_inventory
                        WHERE guild_id = %s AND item_key = %s
                        FOR UPDATE
                        """,
                        (guild_id, item_key),
                    )
                    current = cursor.fetchone()
                    final_stock = (
                        stock
                        if stock is not None
                        else int(current["current_stock"]) if current else 0
                    )
                    final_discount = (
                        discount
                        if discount is not None
                        else int(current["sale_discount"]) if current else 0
                    )
                    cursor.execute(
                        """
                        INSERT INTO blackmarket_inventory (guild_id, item_key, current_stock, sale_discount, updated_at)
                        VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
                        ON CONFLICT (guild_id, item_key) DO UPDATE SET
                            current_stock = EXCLUDED.current_stock,
                            sale_discount = EXCLUDED.sale_discount,
                            updated_at = CURRENT_TIMESTAMP
                        RETURNING item_key, current_stock, sale_discount
                        """,
                        (guild_id, item_key, final_stock, final_discount),
                    )
                    row = dict(cursor.fetchone())
                    return {
                        "ok": True,
                        "item_key": str(row["item_key"]),
                        "stock": int(row["current_stock"]),
                        "discount": int(row["sale_discount"]),
                    }
        finally:
            self._pool.putconn(conn)

    def set_blackmarket_inventory_stock(self, guild_id: int, item_key: str, stock: int, discount: Optional[int] = None) -> dict[str, Any]:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    if discount is not None:
                        cursor.execute(
                            """
                            INSERT INTO blackmarket_inventory (guild_id, item_key, current_stock, sale_discount, updated_at)
                            VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
                            ON CONFLICT (guild_id, item_key) DO UPDATE SET
                                current_stock = EXCLUDED.current_stock, sale_discount = EXCLUDED.sale_discount, updated_at = CURRENT_TIMESTAMP
                            """,
                            (guild_id, item_key, stock, discount),
                        )
                    else:
                        cursor.execute(
                            """
                            INSERT INTO blackmarket_inventory (guild_id, item_key, current_stock, sale_discount, updated_at)
                            VALUES (%s, %s, %s, 0, CURRENT_TIMESTAMP)
                            ON CONFLICT (guild_id, item_key) DO UPDATE SET
                                current_stock = EXCLUDED.current_stock, updated_at = CURRENT_TIMESTAMP
                            """,
                            (guild_id, item_key, stock),
                        )
                    return {"ok": True, "item_key": item_key, "stock": stock, "discount": discount}
        finally:
            self._pool.putconn(conn)

    def remove_pet(self, guild_id: int, user_id: int, pet_id: int) -> dict[str, Any]:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute("DELETE FROM user_pets WHERE guild_id = %s AND user_id = %s AND id = %s RETURNING id", (guild_id, user_id, pet_id))
                    deleted = cursor.fetchone()
                    if deleted:
                        return {"ok": True, "deleted_id": deleted[0]}
                    return {"ok": False, "reason": "not_found"}
        finally:
            self._pool.putconn(conn)

    def duplicate_pet(self, guild_id: int, user_id: int, pet_id: int) -> dict[str, Any]:
        """Clone one owned pet while preventing a duplicate passive-income claim."""
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO user_pets (
                            guild_id, user_id, pet_key, stage, custom_name, is_fused,
                            fusion_data, fusion_parents, purchased_at, stage_started_at,
                            last_collected, last_cared_at, last_played_at, happiness, total_earned
                        )
                        SELECT
                            guild_id, user_id, pet_key, stage, custom_name, is_fused,
                            fusion_data, fusion_parents, CURRENT_TIMESTAMP, stage_started_at,
                            CURRENT_TIMESTAMP, last_cared_at, last_played_at, happiness, total_earned
                        FROM user_pets
                        WHERE guild_id = %s AND user_id = %s AND id = %s
                        RETURNING id, pet_key, stage, custom_name, is_fused, happiness
                        """,
                        (guild_id, user_id, pet_id),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        return {"ok": False, "reason": "not_found"}
                    return {"ok": True, "pet": dict(row)}
        finally:
            self._pool.putconn(conn)

    def reset_member_economy_items(self, guild_id: int, user_id: int) -> int:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute("DELETE FROM economy_items WHERE guild_id = %s AND user_id = %s", (guild_id, user_id))
                    return cursor.rowcount
        finally:
            self._pool.putconn(conn)

    def set_economy_item_quantity(
        self, guild_id: int, user_id: int, item_key: str, quantity: int
    ) -> dict[str, Any]:
        quantity = max(0, int(quantity))
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    if quantity <= 0:
                        cursor.execute(
                            """
                            DELETE FROM economy_items
                            WHERE guild_id = %s AND user_id = %s AND item_key = %s
                            """,
                            (guild_id, user_id, item_key),
                        )
                        return {"ok": True, "item_key": item_key, "quantity": 0}
                    cursor.execute(
                        """
                        INSERT INTO economy_items (guild_id, user_id, item_key, quantity, updated_at)
                        VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
                        ON CONFLICT (guild_id, user_id, item_key) DO UPDATE SET
                            quantity = EXCLUDED.quantity,
                            updated_at = CURRENT_TIMESTAMP
                        RETURNING quantity
                        """,
                        (guild_id, user_id, item_key, quantity),
                    )
                    return {
                        "ok": True,
                        "item_key": item_key,
                        "quantity": int(cursor.fetchone()["quantity"]),
                    }
        finally:
            self._pool.putconn(conn)

    def set_pet_item_quantity(
        self, guild_id: int, user_id: int, item_key: str, quantity: int
    ) -> dict[str, Any]:
        quantity = max(0, int(quantity))
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    if quantity <= 0:
                        cursor.execute(
                            """
                            DELETE FROM pet_items
                            WHERE guild_id = %s AND user_id = %s AND item_key = %s
                            """,
                            (guild_id, user_id, item_key),
                        )
                        return {"ok": True, "item_key": item_key, "quantity": 0}
                    cursor.execute(
                        """
                        INSERT INTO pet_items (guild_id, user_id, item_key, quantity, updated_at)
                        VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
                        ON CONFLICT (guild_id, user_id, item_key) DO UPDATE SET
                            quantity = EXCLUDED.quantity,
                            updated_at = CURRENT_TIMESTAMP
                        RETURNING quantity
                        """,
                        (guild_id, user_id, item_key, quantity),
                    )
                    return {
                        "ok": True,
                        "item_key": item_key,
                        "quantity": int(cursor.fetchone()["quantity"]),
                    }
        finally:
            self._pool.putconn(conn)

    def update_pet_admin(
        self,
        guild_id: int,
        user_id: int,
        pet_id: int,
        *,
        stage: Optional[int] = None,
        happiness: Optional[int] = None,
        custom_name: Optional[str] = None,
        clear_name: bool = False,
    ) -> dict[str, Any]:
        updates: list[str] = []
        params: list[Any] = []
        if stage is not None:
            updates.append("stage = %s")
            params.append(max(1, min(5, int(stage))))
            updates.append("stage_started_at = CURRENT_TIMESTAMP")
        if happiness is not None:
            updates.append("happiness = %s")
            params.append(max(0, min(100, int(happiness))))
        if clear_name:
            updates.append("custom_name = NULL")
        elif custom_name is not None:
            updates.append("custom_name = %s")
            params.append(str(custom_name).strip()[:80] or None)
        if not updates:
            return {"ok": False, "reason": "no_changes"}

        params.extend([guild_id, user_id, pet_id])
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        f"""
                        UPDATE user_pets
                        SET {', '.join(updates)}
                        WHERE guild_id = %s AND user_id = %s AND id = %s
                        RETURNING id, guild_id, user_id, pet_key, stage, custom_name,
                                  is_fused, fusion_data, fusion_parents, purchased_at,
                                  stage_started_at, last_collected, last_cared_at,
                                  last_played_at, happiness, total_earned
                        """,
                        tuple(params),
                    )
                    row = cursor.fetchone()
                    if not row:
                        return {"ok": False, "reason": "not_found"}
                    return {"ok": True, "pet": dict(row)}
        finally:
            self._pool.putconn(conn)

    def reset_member_role_perks(self, guild_id: int, user_id: int) -> int:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "DELETE FROM member_purchases WHERE guild_id = %s AND user_id = %s",
                        (guild_id, user_id),
                    )
                    return max(0, cursor.rowcount)
        finally:
            self._pool.putconn(conn)

    def get_guild_active_casino_games(
        self, guild_id: int, user_id: Optional[int] = None
    ) -> list[dict[str, Any]]:
        games = [
            row
            for row in self.get_active_casino_games()
            if int(row.get("guild_id") or 0) == int(guild_id)
        ]
        if user_id is not None:
            games = [
                row
                for row in games
                if int(row.get("user_id") or 0) == int(user_id)
            ]
        return games

    def clear_active_casino_games_admin(
        self,
        guild_id: int,
        *,
        user_id: Optional[int] = None,
        message_id: Optional[int] = None,
    ) -> int:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    if message_id is not None:
                        cursor.execute(
                            """
                            DELETE FROM active_casino_games
                            WHERE guild_id = %s AND message_id = %s
                            """,
                            (guild_id, message_id),
                        )
                    elif user_id is not None:
                        cursor.execute(
                            """
                            DELETE FROM active_casino_games
                            WHERE guild_id = %s AND user_id = %s
                            """,
                            (guild_id, user_id),
                        )
                    else:
                        cursor.execute(
                            "DELETE FROM active_casino_games WHERE guild_id = %s",
                            (guild_id,),
                        )
                    return max(0, cursor.rowcount)
        finally:
            self._pool.putconn(conn)

    def set_wanted_level(self, guild_id: int, user_id: int, level: int) -> int:
        level = max(0, min(10, int(level)))
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "INSERT INTO member_security (guild_id, user_id, wanted_level) VALUES (%s, %s, %s) ON CONFLICT (guild_id, user_id) DO UPDATE SET wanted_level = EXCLUDED.wanted_level RETURNING wanted_level",
                        (guild_id, user_id, level)
                    )
                    return int(cursor.fetchone()[0])
        finally:
            self._pool.putconn(conn)

    def set_jail_time(self, guild_id: int, user_id: int, minutes: int) -> Optional[datetime]:
        minutes = max(0, int(minutes))
        jail_until = datetime.now(timezone.utc) + timedelta(minutes=minutes) if minutes > 0 else None
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "INSERT INTO member_security (guild_id, user_id, jail_until) VALUES (%s, %s, %s) ON CONFLICT (guild_id, user_id) DO UPDATE SET jail_until = EXCLUDED.jail_until RETURNING jail_until",
                        (guild_id, user_id, jail_until)
                    )
                    row = cursor.fetchone()
                    return row[0] if row else None
        finally:
            self._pool.putconn(conn)

    def set_lock_level(self, guild_id: int, user_id: int, level: int) -> int:
        level = max(0, min(3, int(level)))
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "INSERT INTO member_security (guild_id, user_id, lock_level) VALUES (%s, %s, %s) ON CONFLICT (guild_id, user_id) DO UPDATE SET lock_level = EXCLUDED.lock_level RETURNING lock_level",
                        (guild_id, user_id, level)
                    )
                    return int(cursor.fetchone()[0])
        finally:
            self._pool.putconn(conn)

    def reset_guild_economy(self, guild_id: int) -> dict[str, int]:
        per_user_tables = (
            "active_casino_games",
            "member_investments",
            "pet_fusion_history",
            "user_pets",
            "pet_items",
            "economy_items",
            "member_security",
            "member_purchases",
            "heist_history",
            "member_card_presets",
            "level_card_settings",
            "member_levels",
            "member_economy",
        )
        counts: dict[str, int] = {}
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    for table in per_user_tables:
                        cursor.execute(
                            f"DELETE FROM {table} WHERE guild_id = %s", (guild_id,)
                        )
                        counts[table] = max(0, cursor.rowcount)

                    cursor.execute(
                        """
                        UPDATE member_perks
                        SET prestige_level = 0,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE guild_id = %s
                          AND prestige_level <> 0
                        """,
                        (guild_id,),
                    )
                    counts["member_perks"] = max(0, cursor.rowcount)
            return counts
        finally:
            self._pool.putconn(conn)

    def log_admin_activity(
        self,
        guild_id: int,
        admin_id: int,
        command_name: str,
        summary: str,
        *,
        target_user_id: Optional[int] = None,
        channel_id: Optional[int] = None,
    ) -> None:
        cleaned_summary = " ".join(str(summary or "").split())[:480]
        if not cleaned_summary:
            cleaned_summary = command_name
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO economy_admin_activity (
                            guild_id, admin_id, target_user_id, channel_id, command_name, summary
                        )
                        VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (
                            guild_id,
                            admin_id,
                            target_user_id,
                            channel_id,
                            command_name,
                            cleaned_summary,
                        ),
                    )
        finally:
            self._pool.putconn(conn)

    def get_admin_activity(
        self, guild_id: int, limit: int = 15, target_user_id: Optional[int] = None
    ) -> list[dict[str, Any]]:
        limit = max(1, min(50, int(limit)))
        conn = self._connect()
        try:
            with conn.cursor() as cursor:
                params: list[Any] = [guild_id]
                target_clause = ""
                if target_user_id is not None:
                    target_clause = "AND target_user_id = %s"
                    params.append(int(target_user_id))
                params.append(limit)
                cursor.execute(
                    f"""
                    SELECT id, guild_id, admin_id, target_user_id, channel_id,
                           command_name, summary, created_at
                    FROM economy_admin_activity
                    WHERE guild_id = %s
                    {target_clause}
                    ORDER BY created_at DESC, id DESC
                    LIMIT %s
                    """,
                    tuple(params),
                )
                return [dict(row) for row in cursor.fetchall()]
        finally:
            self._pool.putconn(conn)

    def _ensure_member_perks(self, cursor, guild_id: int, user_id: int) -> None:
        cursor.execute(
            """
            INSERT INTO member_perks (guild_id, user_id)
            VALUES (%s, %s)
            ON CONFLICT (guild_id, user_id) DO NOTHING
            """,
            (guild_id, user_id),
        )

    def get_member_perks(self, guild_id: int, user_id: int) -> dict[str, Any]:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    self._ensure_member_perks(cursor, guild_id, user_id)
                    cursor.execute(
                        """
                        SELECT guild_id, user_id, prestige_level, is_booster, last_boost_thanked_at
                        FROM member_perks
                        WHERE guild_id = %s AND user_id = %s
                        """,
                        (guild_id, user_id),
                    )
                    return dict(cursor.fetchone())
        finally:
            self._pool.putconn(conn)

    def set_member_booster(
        self, guild_id: int, user_id: int, is_booster: bool
    ) -> dict[str, Any]:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO member_perks (guild_id, user_id, is_booster, updated_at)
                        VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
                        ON CONFLICT (guild_id, user_id) DO UPDATE SET
                            is_booster = EXCLUDED.is_booster,
                            updated_at = CURRENT_TIMESTAMP
                        RETURNING guild_id, user_id, prestige_level, is_booster, last_boost_thanked_at
                        """,
                        (guild_id, user_id, bool(is_booster)),
                    )
                    return dict(cursor.fetchone())
        finally:
            self._pool.putconn(conn)

    def set_booster_panel(
        self, guild_id: int, channel_id: int, message_id: Optional[int]
    ) -> None:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO booster_panels (guild_id, channel_id, message_id, updated_at)
                        VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
                        ON CONFLICT (guild_id) DO UPDATE SET
                            channel_id = EXCLUDED.channel_id,
                            message_id = EXCLUDED.message_id,
                            updated_at = CURRENT_TIMESTAMP
                        """,
                        (guild_id, channel_id, message_id),
                    )
        finally:
            self._pool.putconn(conn)

    def get_booster_panel(self, guild_id: int) -> Optional[dict[str, Any]]:
        conn = self._connect()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT guild_id, channel_id, message_id FROM booster_panels WHERE guild_id = %s",
                    (guild_id,),
                )
                row = cursor.fetchone()
                return dict(row) if row else None
        finally:
            self._pool.putconn(conn)

    def prestige_status(self, guild_id: int, user_id: int) -> dict[str, Any]:
        perks = self.get_member_perks(guild_id, user_id)
        wallet, bank = self.get_balances(guild_id, user_id)
        level = int(perks.get("prestige_level") or 0)
        maxed = level >= PRESTIGE_MAX_LEVEL
        return {
            "level": level,
            "max_level": PRESTIGE_MAX_LEVEL,
            "next_cost": None if maxed else _prestige_cost(level),
            "wallet": int(wallet),
            "bank": int(bank),
            "total": int(wallet) + int(bank),
            "bonus_rate": min(
                level * PRESTIGE_BONUS_PER_LEVEL,
                PRESTIGE_MAX_LEVEL * PRESTIGE_BONUS_PER_LEVEL,
            ),
            "maxed": maxed,
        }

    def set_prestige_level(
        self, guild_id: int, user_id: int, level: int
    ) -> dict[str, Any]:
        level = max(0, min(PRESTIGE_MAX_LEVEL, int(level)))
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO member_perks (guild_id, user_id, prestige_level, updated_at)
                        VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
                        ON CONFLICT (guild_id, user_id) DO UPDATE SET
                            prestige_level = EXCLUDED.prestige_level,
                            updated_at = CURRENT_TIMESTAMP
                        RETURNING guild_id, user_id, prestige_level, is_booster
                        """,
                        (guild_id, user_id, level),
                    )
                    row = dict(cursor.fetchone())
                    row["bonus_rate"] = (
                        int(row["prestige_level"]) * PRESTIGE_BONUS_PER_LEVEL
                    )
                    return row
        finally:
            self._pool.putconn(conn)

    def set_role_perk(
        self, guild_id: int, user_id: int, role_name: str, enabled: bool
    ) -> dict[str, Any]:
        if role_name not in SHOP_ITEMS:
            return {"ok": False, "reason": "invalid_role"}
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    if enabled:
                        cursor.execute(
                            """
                            INSERT INTO member_purchases (guild_id, user_id, role_name, purchased_at)
                            VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
                            ON CONFLICT (guild_id, user_id, role_name) DO NOTHING
                            """,
                            (guild_id, user_id, role_name),
                        )
                    else:
                        cursor.execute(
                            "DELETE FROM member_purchases WHERE guild_id = %s AND user_id = %s AND role_name = %s",
                            (guild_id, user_id, role_name),
                        )
                    cursor.execute(
                        "SELECT role_name FROM member_purchases WHERE guild_id = %s AND user_id = %s ORDER BY purchased_at ASC",
                        (guild_id, user_id),
                    )
                    return {
                        "ok": True,
                        "role_name": role_name,
                        "enabled": enabled,
                        "roles": [str(row["role_name"]) for row in cursor.fetchall()],
                    }
        finally:
            self._pool.putconn(conn)

class AdminCog(commands.Cog):
    ADMIN_ACTIVITY_COMMANDS = {
        "the-big-reset",
        "resetcd",
        "ecoadmin add",
        "ecoadmin remove",
        "ecoadmin set",
        "ecoadmin bank",
        "ecoadmin bankadd",
        "ecoadmin bankremove",
        "ecoadmin reset",
        "ecoadmin resetcd",
        "ecoadmin setcd",
        "ecoadmin settle",
        "ecoadmin cancelinvest",
        "ecoadmin restock",
        "ecoadmin shop restock",
        "ecoadmin shop stock",
        "ecoadmin stock",
        "ecoadmin prestige",
        "ecoadmin booster",
        "ecoadmin roleperk",
        "ecoadmin rolereset",
        "ecoadmin bmrestock",
        "ecoadmin bmshop restock",
        "ecoadmin bmshop stock",
        "ecoadmin bmshop discount",
        "ecoadmin petshop restock",
        "ecoadmin petshop stock",
        "ecoadmin utilshop restock",
        "ecoadmin bmstock",
        "ecoadmin bmdiscount",
        "ecoadmin itemgrant",
        "ecoadmin itemset",
        "ecoadmin itemreset",
        "ecoadmin petgrant",
        "ecoadmin petremove",
        "ecoadmin pet give",
        "ecoadmin pet remove",
        "ecoadmin pet duplicate",
        "ecoadmin petitem",
        "ecoadmin petitemset",
        "ecoadmin petitem give",
        "ecoadmin petitem remove",
        "ecoadmin petitem set",
        "ecoadmin petstage",
        "ecoadmin pethappy",
        "ecoadmin petrename",
        "ecoadmin petreset",
        "ecoadmin wanted",
        "ecoadmin jail",
        "ecoadmin unjail",
        "ecoadmin lock",
        "ecoadmin gamesclear",
        "ecoadmin config channel",
        "ecoadmin config bypass",
        "ecoadmin wipe",
    }

    async def cog_after_invoke(self, ctx: commands.Context) -> None:
        await self._log_admin_activity(ctx)

    async def _log_admin_activity(self, ctx: commands.Context) -> None:
        if ctx.guild is None or ctx.command is None:
            return
        command_name = ctx.command.qualified_name
        if command_name not in self.ADMIN_ACTIVITY_COMMANDS:
            return
        try:
            await asyncio.to_thread(
                self.store.log_admin_activity,
                ctx.guild.id,
                ctx.author.id,
                command_name,
                getattr(ctx.message, "content", command_name),
                target_user_id=self._audit_target_user_id(ctx),
                channel_id=ctx.channel.id,
            )
        except Exception as exc:
            LOGGER.warning("Failed to log economy admin activity: %s", exc)

    async def cog_command_error(
        self, ctx: commands.Context, error: commands.CommandError
    ) -> None:
        if isinstance(error, commands.CheckFailure):
            return
        if isinstance(error, commands.UserInputError):
            usage = getattr(ctx.command, "usage", None) or ctx.command.signature
            if isinstance(error, commands.MissingRequiredArgument):
                msg = f"Missing `{error.param.name}`."
            elif isinstance(error, commands.BadArgument):
                msg = "I could not read one of those arguments."
            else:
                msg = str(error)
            return await ctx.send(
                f"{msg}\nUsage: `{ctx.clean_prefix}{ctx.command.qualified_name} {usage}`"
            )
        raise error

    def _admin_usage(self, command: commands.Command) -> str:
        usage = getattr(command, "usage", None) or command.signature
        return f".{command.qualified_name} {usage}".strip()

    async def _send_member_snapshot(
        self, ctx: commands.Context, member: discord.Member
    ) -> None:
        snapshot = await asyncio.to_thread(
            self.store.get_member_admin_snapshot, ctx.guild.id, member.id
        )
        wallet = int(snapshot["wallet"])
        bank = int(snapshot["bank"])
        security = snapshot["security"]
        perks = snapshot["perks"]
        embed = discord.Embed(
            title=f"Economy Control - {member.display_name}",
            description=(
                f"{member.mention}\n"
                f"Wallet: **{wallet:,} cr**\n"
                f"Bank: **{bank:,} cr**\n"
                f"Total: **{wallet + bank:,} cr**"
            ),
            color=0xF1C40F,
        )
        embed.add_field(
            name="Security",
            value=(
                f"Wanted: **{int(security.get('wanted_level') or 0)}**\n"
                f"Lock: **{int(security.get('lock_level') or 0)}**\n"
                f"Jail: **{_fmt_dt(security.get('jail_until'))}**"
            ),
            inline=True,
        )
        embed.add_field(
            name="Perks",
            value=(
                f"Prestige: **{int(perks.get('prestige_level') or 0)}/{PRESTIGE_MAX_LEVEL}**\n"
                f"Booster: **{'On' if perks.get('is_booster') else 'Off'}**\n"
                f"Shop perks: **{len(snapshot['purchased_roles'])}**"
            ),
            inline=True,
        )
        embed.add_field(
            name="Cooldowns/Games",
            value=(
                f"Cooldown rows: **{len(snapshot['cooldowns'])}**\n"
                f"Active casino rows: **{len(snapshot['active_games'])}**"
            ),
            inline=True,
        )

        eco_items = _format_quantity_map(
            snapshot["economy_items"], ECONOMY_ITEM_DEFS
        )
        pet_items = _format_quantity_map(snapshot["pet_items"], PET_ITEM_DEFS)
        embed.add_field(name="Economy Items", value=eco_items[:1024], inline=False)
        embed.add_field(name="Pet Materials", value=pet_items[:1024], inline=False)

        pet_lines = []
        for pet in snapshot["pets"][:10]:
            pet_lines.append(
                f"`#{int(pet['id'])}` {_pet_display_name(pet)} "
                f"(stage {int(pet['stage'])}, happy {int(pet['happiness'])})"
            )
        if len(snapshot["pets"]) > 10:
            pet_lines.append(f"...and {len(snapshot['pets']) - 10} more")
        embed.add_field(
            name="Pets",
            value="\n".join(pet_lines) if pet_lines else "None",
            inline=False,
        )

        active_investments = [
            row for row in snapshot["investments"] if str(row.get("status")) == "active"
        ]
        embed.add_field(
            name="Investments",
            value=(
                f"Active: **{len(active_investments)}**\n"
                f"Recent rows loaded: **{len(snapshot['investments'])}**"
            ),
            inline=True,
        )
        embed.add_field(
            name="Purchased Roles",
            value=", ".join(snapshot["purchased_roles"])[:1024]
            if snapshot["purchased_roles"]
            else "None",
            inline=True,
        )
        embed.set_footer(
            text="Use .eco items, .eco pets, .eco cooldowns, .eco investments, and .eco games for detailed views."
        )
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    async def _send_investments(
        self, ctx: commands.Context, member: discord.Member
    ) -> None:
        rows = await asyncio.to_thread(
            self.store.get_user_investments, ctx.guild.id, member.id, 12
        )
        embed = discord.Embed(
            title=f"Investments - {member.display_name}",
            color=0x3498DB,
        )
        if not rows:
            embed.description = "No investment history."
            return await ctx.send(
                embed=embed, allowed_mentions=discord.AllowedMentions.none()
            )

        active_lines: list[str] = []
        history_lines: list[str] = []
        for row in rows:
            amount = int(row["amount"])
            status = str(row["status"])
            if status == "active":
                active_lines.append(
                    f"#{row['id']} - **{amount:,} cr** - matures {_fmt_dt(row['matures_at'])}"
                )
            else:
                returned = int(row.get("return_amount") or 0)
                history_lines.append(
                    f"#{row['id']} - **{status.title()}** - returned **{returned:,} cr** "
                    f"({returned - amount:+,} cr) - {_fmt_dt(row.get('settled_at'))}"
                )
        embed.add_field(
            name="Active",
            value="\n".join(active_lines) if active_lines else "No active investments.",
            inline=False,
        )
        embed.add_field(
            name="Recent",
            value="\n".join(history_lines) if history_lines else "No completed investments.",
            inline=False,
        )
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    async def _send_shop_inventory(self, ctx: commands.Context) -> None:
        inventory = await asyncio.to_thread(self.store.get_inventory, ctx.guild.id)
        lines = []
        for name, data in SHOP_ITEMS.items():
            current = int(inventory.get(name, 0))
            max_stock = int(data["max_stock"])
            lines.append(
                f"**{name}**: **{current}/{max_stock}** stock - {int(data['price']):,} cr"
            )
        embed = discord.Embed(title="Shop Inventory", color=0xF1C40F)
        for index, chunk in enumerate(_chunk_text(lines), start=1):
            embed.add_field(
                name="Roles" if index == 1 else f"Roles {index}",
                value=chunk,
                inline=False,
            )
        await ctx.send(embed=embed)

    async def _send_blackmarket_inventory(self, ctx: commands.Context) -> None:
        inventory = await asyncio.to_thread(
            self.store.get_blackmarket_inventory, ctx.guild.id
        )
        if not inventory:
            return await ctx.send("Blackmarket is empty.")
        lines = []
        for key, item in sorted(inventory.items()):
            name = ECONOMY_ITEM_DEFS.get(key, {}).get("name")
            if name is None and key.startswith("lock_"):
                lock_level = int(key.rsplit("_", 1)[-1])
                name = LOCK_LEVELS.get(lock_level, {}).get("name", key)
            lines.append(
                f"`{key}` {name or key}: **{int(item['stock']):,}** stock, "
                f"**{int(item['discount'])}%** discount"
            )
        embed = discord.Embed(title="Blackmarket Inventory", color=0x2B2D31)
        for index, chunk in enumerate(_chunk_text(lines), start=1):
            embed.add_field(
                name="Items" if index == 1 else f"Items {index}",
                value=chunk,
                inline=False,
            )
        await ctx.send(embed=embed)

    async def _send_cooldowns(
        self, ctx: commands.Context, member: discord.Member
    ) -> None:
        rows = await asyncio.to_thread(
            self.store.get_member_cooldowns, ctx.guild.id, member.id
        )
        security = await asyncio.to_thread(
            self.store.get_member_security, ctx.guild.id, member.id
        )
        embed = discord.Embed(
            title=f"Cooldowns - {member.display_name}", color=0x5865F2
        )
        command_lines = [
            f"`{row['command_name']}` expires {_fmt_dt(row['expires_at'])}"
            for row in rows
        ]
        embed.add_field(
            name="Command Cooldowns",
            value="\n".join(command_lines) if command_lines else "None",
            inline=False,
        )
        embed.add_field(
            name="Robbery/Heist Timers",
            value=(
                f"Rob: {_fmt_dt(security.get('last_rob_at'))}\n"
                f"Jewelry: {_fmt_dt(security.get('last_jewelry_heist_at'))}\n"
                f"Bank: {_fmt_dt(security.get('last_bank_heist_at'))}\n"
                f"Team bank: {_fmt_dt(security.get('last_team_heist_at'))}\n"
                f"Jail: {_fmt_dt(security.get('jail_until'))}"
            ),
            inline=False,
        )
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @commands.command(
        name="the-big-reset",
        aliases=["bigreset", "big-reset", "thebigreset", "thegreatreset"],
        help="Administrator only. Reset all economy progression for this server.",
    )
    async def the_big_reset_cmd(self, ctx: commands.Context, confirmation: str = ""):
        if not (
            isinstance(ctx.author, discord.Member)
            and (
                ctx.author.guild_permissions.administrator
                or ctx.author.id in ECONOMY_ADMIN_USER_IDS
            )
        ):
            return await ctx.send(
                "❌ You must be an administrator to use this command."
            )
        if ctx.guild is None:
            return await ctx.send("This command can only be used in a server.")

        if confirmation.strip().lower() != "confirm":
            return await ctx.send(
                "This will wipe all user economy progression for this server: wallet, bank, investments, "
                "purchased shop roles, purchases, pets, pet materials, robbery tools, locks, wanted levels, "
                "heist history, active casino games, prestige levels, level XP, level cards, and level reward roles.\n"
                "Run `.the-big-reset confirm` to continue."
            )

        await ctx.send("⏳ Wiping economy database...")

        # --- Step 1: Wipe the database FIRST (this is the critical part) ---
        try:
            counts = await asyncio.to_thread(
                self.store.reset_guild_economy, ctx.guild.id
            )
        except Exception:
            LOGGER.exception(
                "Failed to run the big economy reset for guild=%s", ctx.guild.id
            )
            return await ctx.send(
                "❌ Database wipe failed. The database connection may have dropped. Try again."
            )

        cleared_rows = sum(counts.values())
        await ctx.send(
            f"✅ Database wiped! **{cleared_rows:,}** rows cleared. Now cleaning up roles..."
        )

        # --- Step 2: Remove shop roles (best-effort, 30s timeout) ---
        role_stats = {
            "roles_removed": 0,
            "members_scanned": 0,
            "members_missing": 0,
            "roles_missing": 0,
        }
        role_note = ""
        try:
            role_stats = await asyncio.wait_for(
                self._remove_big_reset_shop_roles(ctx.guild), timeout=30
            )
        except asyncio.TimeoutError:
            role_note = "⚠️ Shop role cleanup timed out after 30s. Some users may still have shop roles.\n"
        except Exception:
            LOGGER.exception("Shop role cleanup failed for guild=%s", ctx.guild.id)
            role_note = "⚠️ Shop role cleanup failed (connection error). Some users may still have shop roles.\n"

        # --- Step 3: Remove level reward roles (best-effort, 30s timeout) ---
        level_sys = getattr(self.bot, "level_system", None)
        level_role_stats: dict[str, int] = {}
        level_note = ""
        if level_sys is not None:
            try:
                level_role_stats = await asyncio.wait_for(
                    level_sys.remove_guild_reward_roles(ctx.guild), timeout=30
                )
            except asyncio.TimeoutError:
                level_note = "⚠️ Level role cleanup timed out after 30s. Some users may still have level roles.\n"
            except Exception:
                LOGGER.exception("Level role cleanup failed for guild=%s", ctx.guild.id)
                level_note = "⚠️ Level role cleanup failed (connection error). Some users may still have level roles.\n"

        # --- Step 4: Report ---
        level_rows = (
            counts.get("member_levels", 0)
            + counts.get("level_card_settings", 0)
            + counts.get("member_card_presets", 0)
        )
        embed = discord.Embed(
            title="The Big Reset Complete",
            description=(
                f"Reset economy progression for **{ctx.guild.name}**.\n"
                f"Rows cleared or reset: **{cleared_rows:,}**\n"
                f"Level rows cleared: **{level_rows:,}**\n"
                f"{role_note}{level_note}"
            ),
            color=0xED4245,
        )
        embed.add_field(
            name="Balances",
            value=f"{counts.get('member_economy', 0):,} member row(s)",
            inline=True,
        )
        embed.add_field(
            name="Investments",
            value=f"{counts.get('member_investments', 0):,} row(s)",
            inline=True,
        )
        embed.add_field(
            name="Pets", value=f"{counts.get('user_pets', 0):,} pet row(s)", inline=True
        )
        embed.add_field(
            name="Materials",
            value=f"{counts.get('pet_items', 0):,} pet item row(s)",
            inline=True,
        )
        embed.add_field(
            name="Tools/Locks",
            value=f"{counts.get('economy_items', 0) + counts.get('member_security', 0):,} row(s)",
            inline=True,
        )
        embed.add_field(
            name="Perks",
            value=f"{counts.get('member_perks', 0):,} prestige row(s)",
            inline=True,
        )
        embed.add_field(
            name="Shop Roles",
            value=f"{role_stats.get('roles_removed', 0):,} removed from members",
            inline=True,
        )
        if level_sys is not None:
            embed.add_field(
                name="Level Roles",
                value=f"{level_role_stats.get('roles_removed', 0):,} removed from members",
                inline=True,
            )
        embed.set_footer(
            text="Shop stock, black market stock, booster status, panels, and level reward config were preserved."
        )
        await send_v2(ctx, embed)

    @commands.command(
        name="resetcd",
        aliases=["resetcooldowns", "clearcd", "clearcooldowns"],
        help="Administrator only. Reset all cooldowns for a member.",
    )
    @economy_admin_only()
    async def resetcd_cmd(
        self, ctx: commands.Context, member: Optional[discord.Member] = None
    ):
        if ctx.guild is None:
            return await ctx.send("This command can only be used in a server.")
        if member is None:
            return await ctx.send("Usage: `.resetcd @user`")

        result = await asyncio.to_thread(
            self.store.reset_member_cooldowns, ctx.guild.id, member.id
        )
        command_rows = int(result.get("command_cooldowns") or 0)
        robbery_rows = int(result.get("robbery_cooldowns") or 0)
        jail_cleared = int(result.get("jail_cleared") or 0)
        embed = discord.Embed(
            title="Cooldowns Reset",
            description=(
                f"Reset cooldowns for {member.mention}.\n"
                f"Command cooldown rows cleared: **{command_rows:,}**\n"
                f"Robbery/heist cooldown profile reset: **{'Yes' if robbery_rows else 'No active profile'}**\n"
                f"Jail/wanted status cleared: **{'Yes' if jail_cleared else 'No active profile'}**"
            ),
            color=0x2ECC71,
        )
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @commands.group(
        name="ecoadmin",
        aliases=["eco", "economyadmin"],
        invoke_without_command=True,
        help="Administrator economy control panel.",
    )
    @commands.guild_only()
    @economy_admin_only()
    async def ecoadmin(self, ctx: commands.Context):
        embed = discord.Embed(
            title="Economy Admin",
            description=(
                "Manage the server economy. Commands below show the action they perform.\n"
                "Use `.help eco <command>` when you need every argument and alias."
            ),
            color=0xF1C40F,
        )
        embed.add_field(
            name="Account",
            value=(
                "`.eco user @user` - Full economy snapshot.\n"
                "`.eco bal @user` - Wallet, bank, and total.\n"
                "`.eco add/remove/set @user <amount>` - Change wallet credits.\n"
                "`.eco bank/bankadd/bankremove @user <amount>` - Change bank credits."
            ),
            inline=True,
        )
        embed.add_field(
            name="Investments",
            value=(
                "`.eco investments @user` - Active and recent investments.\n"
                "`.eco settle @user` - Mature and settle all active investments.\n"
                "`.eco cancelinvest @user [refund]` - Cancel active investments."
            ),
            inline=True,
        )
        embed.add_field(
            name="Shop Management",
            value=(
                "`.eco shop inv/restock/stock` - Role shop inventory.\n"
                "`.eco bmshop inv/restock/stock/discount` - Blackmarket inventory.\n"
                "`.eco petshop restock/stock` - Fusion material inventory.\n"
                "`.eco restock` - Refill every limited shop."
            ),
            inline=True,
        )
        embed.add_field(
            name="Pets And Items",
            value=(
                "`.eco items @user` - View a member's regular items.\n"
                "`.eco itemgrant/itemset @user ...` - Give or set regular items.\n"
                "`.eco pets @user` - View owned pets.\n"
                "`.eco pet give/remove/duplicate @user ...` - Manage pets by ID.\n"
                "`.eco petitem give/remove/set @user ...` - Manage pet materials."
            ),
            inline=False,
        )
        embed.add_field(
            name="Security, Games, And Server",
            value=(
                "`.eco wanted/jail/unjail/lock @user ...` - Crime and security state.\n"
                "`.eco cooldowns @user`, `.eco resetcd @user [command]` - Cooldown controls.\n"
                "`.eco games [@user]`, `.eco gamesclear ...` - Active casino games.\n"
                "`.eco config ...`, `.eco history [@user] [limit]` - Server config and audit log.\n"
                "`.eco reset @user`, `.eco wipe @user`, `.the-big-reset confirm` - Destructive resets."
            ),
            inline=False,
        )
        embed.set_footer(text="Destructive actions are immediate. Check the target before sending.")
        return await send_v2(ctx, embed)

        embed = discord.Embed(
            title="Economy Admin Control Panel",
            description="Complete administrator control over the economy, players, items, and blackmarket.",
            color=0xF1C40F,
        )
        
        embed.add_field(
            name="👥 Players (Balances & Resets)",
            value=(
                "`.eco bal @user` - Check wallet, bank, and total credits.\n"
                "`.eco add @user <amount>` - Add credits to wallet.\n"
                "`.eco remove @user <amount>` - Remove credits from wallet.\n"
                "`.eco set @user <amount>` - Set exact wallet balance.\n"
                "`.eco bank @user <amount>` - Set exact bank balance.\n"
                "`.eco bankadd @user <amount>` - Add credits to bank.\n"
                "`.eco bankremove @user <amount>` - Remove credits from bank.\n"
                "`.eco reset @user` - Reset wallet and bank to 0.\n"
                "`.eco wipe @user` - 🚨 **WIPE ALL** user data (credits, pets, items)."
            ),
            inline=False,
        )
        
        embed.add_field(
            name="📈 Investments",
            value=(
                "`.eco investments @user` - Show active/recent investments.\n"
                "`.eco settle @user` - Force active investments to mature now and settle.\n"
                "`.eco cancelinvest @user [refund]` - Cancel active investments."
            ),
            inline=False,
        )
        
        embed.add_field(
            name="🏪 Shop & Perks",
            value=(
                "`.eco restock` - Force full shop restock.\n"
                "`.eco inv` - Show current shop stock.\n"
                "`.eco stock <amount> <role>` - Set stock for one shop role.\n"
                "`.eco prestige @user <0-10>` - Set prestige level.\n"
                "`.eco booster @user <on|off>` - Toggle booster perks.\n"
                "`.eco roleperk @user <grant|remove> <role>` - Grant/remove shop perk."
            ),
            inline=False,
        )
        
        embed.add_field(
            name="🕵️ Blackmarket",
            value=(
                "`.eco bmrestock` - Force blackmarket restock.\n"
                "`.eco bminv` - Show blackmarket stock.\n"
                "`.eco bmstock <amount> <item>` - Set blackmarket stock.\n"
                "`.eco bmdiscount <0-100> <item>` - Set item discount %."
            ),
            inline=False,
        )
        
        embed.add_field(
            name="🎒 Items & Pets",
            value=(
                "`.eco itemgrant @user <item> <amt>` - Grant/remove items (negative to remove).\n"
                "`.eco itemreset @user` - Wipe all economy items.\n"
                "`.eco petgrant @user <pet> [stage]` - Grant a pet.\n"
                "`.eco petremove @user <pet_id>` - Remove specific pet by ID.\n"
                "`.eco petitem @user <item> <amt>` - Grant/remove pet materials.\n"
                "`.eco petreset @user` - Wipe all pets and pet materials."
            ),
            inline=False,
        )
        
        embed.add_field(
            name="🚔 Security & Jail",
            value=(
                "`.eco wanted @user <level>` - Set wanted level.\n"
                "`.eco jail @user <duration>` - Jail member, e.g. `30m`, `12h`, `7d`.\n"
                "`.eco unjail @user` - Unjail member.\n"
                "`.eco lock @user <level>` - Set security lock (0-2).\n"
                "`.resetcd @user` - Reset all robbery/heist cooldowns."
            ),
            inline=False,
        )
        
        embed.add_field(
            name="📜 Admin",
            value="`.eco history [@user] [limit]` - Show recent economy admin activity, optionally for one target.",
            inline=False,
        )
        
        await send_v2(ctx, embed)

    async def _send_admin_history(
        self,
        ctx: commands.Context,
        limit: Optional[int] = None,
        member: Optional[discord.Member] = None,
    ) -> None:
        parsed_limit = max(1, min(50, int(limit or 15)))
        target_user_id = member.id if member is not None else None
        rows = await asyncio.to_thread(
            self.store.get_admin_activity, ctx.guild.id, parsed_limit, target_user_id
        )
        target_label = f" for {member.display_name}" if member else ""
        embed = discord.Embed(
            title=f"Economy Admin History{target_label}",
            description=(
                "Recent successful economy-admin mutations."
                if rows
                else (
                    f"No logged economy-admin activity targeting {member.mention} yet."
                    if member
                    else "No logged economy-admin activity yet. Logging starts from this update forward."
                )
            ),
            color=0xF1C40F,
        )
        if rows:
            lines: list[str] = []
            for row in rows:
                admin_id = int(row["admin_id"])
                target_id = row.get("target_user_id")
                target_text = f" -> <@{int(target_id)}>" if target_id else ""
                channel_id = row.get("channel_id")
                channel_text = f" in <#{int(channel_id)}>" if channel_id else ""
                created_at = row.get("created_at")
                lines.append(
                    f"`#{int(row['id'])}` <t:{_discord_timestamp(created_at)}:R> "
                    f"<@{admin_id}>{target_text}{channel_text}\n"
                    f"**{row['command_name']}** - `{str(row['summary'])[:180]}`"
                )
            chunks: list[list[str]] = []
            current: list[str] = []
            current_len = 0
            for line in lines:
                line_len = len(line) + 2
                if current and current_len + line_len > 980:
                    chunks.append(current)
                    current = []
                    current_len = 0
                current.append(line)
                current_len += line_len
            if current:
                chunks.append(current)
            for index, chunk in enumerate(chunks, start=1):
                embed.add_field(
                    name="Recent Activity" if index == 1 else f"Recent Activity {index}",
                    value="\n\n".join(chunk),
                    inline=False,
                )
        if member:
            embed.set_footer(
                text=f"Filtered to actions targeting {member.display_name}. Use .eco history @user 25 for more rows. Max 50."
            )
        else:
            embed.set_footer(text="Use .eco history 25 for more rows. Max 50.")
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @ecoadmin.command(
        name="history",
        aliases=["adminhistory", "audit", "logs"],
        help="Show recent economy admin activity, optionally for one target member.",
        usage="[@user] [limit]",
    )
    @economy_admin_only()
    async def ecoadmin_history(
        self,
        ctx: commands.Context,
        member: Optional[discord.Member] = None,
        limit: Optional[int] = None,
    ):
        await self._send_admin_history(ctx, limit, member)

    @ecoadmin.group(
        name="admin",
        invoke_without_command=True,
        help="Show recent economy admin activity, optionally for one target member.",
    )
    @economy_admin_only()
    async def ecoadmin_admin(
        self,
        ctx: commands.Context,
        member: Optional[discord.Member] = None,
        limit: Optional[int] = None,
    ):
        await self._send_admin_history(ctx, limit, member)

    @ecoadmin_admin.command(
        name="history",
        aliases=["audit", "logs"],
        help="Show recent economy admin activity, optionally for one target member.",
        usage="[@user] [limit]",
    )
    @economy_admin_only()
    async def ecoadmin_admin_history(
        self,
        ctx: commands.Context,
        member: Optional[discord.Member] = None,
        limit: Optional[int] = None,
    ):
        await self._send_admin_history(ctx, limit, member)

    @ecoadmin.command(
        name="user",
        aliases=["profile", "inspect", "view"],
        help="Show a full economy-admin snapshot for one member.",
        usage="@user",
    )
    @economy_admin_only()
    async def ecoadmin_user(self, ctx: commands.Context, member: discord.Member):
        await self._send_member_snapshot(ctx, member)

    @ecoadmin.command(
        name="items",
        aliases=["inventory", "itemlist"],
        help="Show a member's economy items and pet materials.",
        usage="@user",
    )
    @economy_admin_only()
    async def ecoadmin_items(self, ctx: commands.Context, member: discord.Member):
        economy_items, pet_items = await asyncio.gather(
            asyncio.to_thread(self.store.get_economy_items, ctx.guild.id, member.id),
            asyncio.to_thread(self.store.get_pet_items, ctx.guild.id, member.id),
        )
        embed = discord.Embed(
            title=f"Items - {member.display_name}",
            color=0xF1C40F,
        )
        embed.add_field(
            name="Economy Items",
            value=_format_quantity_map(economy_items, ECONOMY_ITEM_DEFS)[:1024],
            inline=False,
        )
        embed.add_field(
            name="Pet Materials",
            value=_format_quantity_map(pet_items, PET_ITEM_DEFS)[:1024],
            inline=False,
        )
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @ecoadmin.command(
        name="pets",
        aliases=["petlist"],
        help="Show a member's pets with IDs for admin edits.",
        usage="@user",
    )
    @economy_admin_only()
    async def ecoadmin_pets(self, ctx: commands.Context, member: discord.Member):
        pets = await asyncio.to_thread(self.store.get_user_pets, ctx.guild.id, member.id)
        embed = discord.Embed(title=f"Pets - {member.display_name}", color=0x9B59B6)
        if not pets:
            embed.description = "No pets."
            return await ctx.send(
                embed=embed, allowed_mentions=discord.AllowedMentions.none()
            )
        lines = [
            f"`#{int(pet['id'])}` {_pet_display_name(pet)} - "
            f"`{pet['pet_key']}` stage **{int(pet['stage'])}**, "
            f"happiness **{int(pet['happiness'])}**, earned **{int(pet['total_earned']):,} cr**"
            for pet in pets
        ]
        for index, chunk in enumerate(_chunk_text(lines), start=1):
            embed.add_field(
                name="Pets" if index == 1 else f"Pets {index}",
                value=chunk,
                inline=False,
            )
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @ecoadmin.command(
        name="investments",
        aliases=["invests", "investstatus"],
        help="Show a member's active and recent investments.",
        usage="@user",
    )
    @economy_admin_only()
    async def ecoadmin_investments(self, ctx: commands.Context, member: discord.Member):
        await self._send_investments(ctx, member)

    @ecoadmin.command(
        name="cancelinvest",
        aliases=["cancel_invest", "cinvest"],
        help="Cancel active investments, optionally refunding principal.",
        usage="@user [refund]",
    )
    @economy_admin_only()
    async def ecoadmin_cancelinvest(
        self, ctx: commands.Context, member: discord.Member, refund: str = ""
    ):
        should_refund = refund.strip().lower() in {"refund", "yes", "true", "1"}
        cancelled = await asyncio.to_thread(
            self.store.cancel_active_investments,
            ctx.guild.id,
            member.id,
            should_refund,
        )
        if not cancelled:
            return await ctx.send(
                f"{member.mention} has no active investments to cancel.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        returned = sum(int(row.get("return_amount") or 0) for row in cancelled)
        refund_text = (
            f" Refunded **{returned:,} cr**."
            if should_refund
            else " No credits were refunded."
        )
        await ctx.send(
            f"Cancelled **{len(cancelled)}** active investment(s) for {member.mention}.{refund_text}",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @ecoadmin.command(
        name="cooldowns",
        aliases=["cds", "cooldown"],
        help="Show economy cooldowns and robbery timers for a member.",
        usage="@user",
    )
    @economy_admin_only()
    async def ecoadmin_cooldowns(self, ctx: commands.Context, member: discord.Member):
        await self._send_cooldowns(ctx, member)

    @ecoadmin.command(
        name="resetcd",
        aliases=["clearcd", "clearcooldown"],
        help="Clear all cooldowns or one command cooldown for a member.",
        usage="@user [command]",
    )
    @economy_admin_only()
    async def ecoadmin_resetcd(
        self, ctx: commands.Context, member: discord.Member, *, command_name: str = ""
    ):
        command_name = " ".join(command_name.strip().lower().split()) or None
        if command_name is None:
            result = await asyncio.to_thread(
                self.store.reset_member_cooldowns, ctx.guild.id, member.id
            )
            rows = int(result.get("command_cooldowns") or 0)
            return await ctx.send(
                f"Cleared **{rows:,}** command cooldown row(s), jail, wanted, and robbery timers for {member.mention}.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        rows = await asyncio.to_thread(
            self.store.clear_member_cooldowns, ctx.guild.id, member.id, command_name
        )
        await ctx.send(
            f"Cleared **{rows:,}** `{command_name}` cooldown row(s) for {member.mention}.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @ecoadmin.command(
        name="setcd",
        aliases=["setcooldown"],
        help="Set or clear one command cooldown for a member.",
        usage="@user <command> <minutes>",
    )
    @economy_admin_only()
    async def ecoadmin_setcd(
        self, ctx: commands.Context, member: discord.Member, *, raw: str
    ):
        parts = raw.rsplit(maxsplit=1)
        if len(parts) != 2:
            return await ctx.send("Usage: `.eco setcd @user <command> <minutes>`")
        command_name, minute_text = parts
        try:
            minutes = int(minute_text.replace(",", ""))
        except ValueError:
            return await ctx.send("Minutes must be a whole number.")
        expires_at = await asyncio.to_thread(
            self.store.set_member_cooldown_admin,
            ctx.guild.id,
            member.id,
            command_name,
            minutes,
        )
        if expires_at is None:
            return await ctx.send(
                f"Cleared `{command_name}` cooldown for {member.mention}.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        await ctx.send(
            f"Set `{command_name}` cooldown for {member.mention} until {_fmt_dt(expires_at)}.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @ecoadmin.command(
        name="inv",
        aliases=["stocklist", "shopinv"],
        help="Show current shop role stock.",
    )
    @economy_admin_only()
    async def ecoadmin_inv(self, ctx: commands.Context):
        await self._send_shop_inventory(ctx)

    @ecoadmin.command(
        name="games",
        aliases=["activegames", "casinoactive"],
        help="Show active persisted casino games.",
        usage="[@user]",
    )
    @economy_admin_only()
    async def ecoadmin_games(
        self, ctx: commands.Context, member: Optional[discord.Member] = None
    ):
        games = await asyncio.to_thread(
            self.store.get_guild_active_casino_games,
            ctx.guild.id,
            member.id if member else None,
        )
        embed = discord.Embed(title="Active Casino Games", color=0x5865F2)
        if not games:
            embed.description = "No active persisted casino games."
            return await ctx.send(embed=embed)
        lines = [
            f"`{row['message_id']}` {row['game_type']} - <@{int(row['user_id'])}> "
            f"bet **{int(row['bet']):,} cr** in <#{int(row['channel_id'])}>"
            for row in games[:25]
        ]
        embed.description = "\n".join(lines)
        if len(games) > 25:
            embed.set_footer(text=f"Showing 25 of {len(games)} rows.")
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @ecoadmin.command(
        name="gamesclear",
        aliases=["cleargames", "clearcasino"],
        help="Clear active casino rows by all, member, or message ID.",
        usage="<all|@user|message_id>",
    )
    @economy_admin_only()
    async def ecoadmin_gamesclear(
        self, ctx: commands.Context, target: str
    ):
        target = (target or "").strip()
        if target.lower() == "all":
            rows = await asyncio.to_thread(
                self.store.clear_active_casino_games_admin, ctx.guild.id
            )
            return await ctx.send(f"Cleared **{rows:,}** active casino row(s).")

        member = None
        try:
            member = await commands.MemberConverter().convert(ctx, target)
        except commands.BadArgument:
            member = None
        if member is not None:
            rows = await asyncio.to_thread(
                self.store.clear_active_casino_games_admin,
                ctx.guild.id,
                user_id=member.id,
            )
            return await ctx.send(
                f"Cleared **{rows:,}** active casino row(s) for {member.mention}.",
                allowed_mentions=discord.AllowedMentions.none(),
            )

        message_id = _parse_credit_amount(target)
        if message_id is None:
            return await ctx.send("Use `.eco gamesclear all`, `.eco gamesclear @user`, or `.eco gamesclear <message_id>`.")
        rows = await asyncio.to_thread(
            self.store.clear_active_casino_games_admin,
            ctx.guild.id,
            message_id=message_id,
        )
        await ctx.send(f"Cleared **{rows:,}** active casino row(s) for message `{message_id}`.")

    @ecoadmin.group(
        name="config",
        invoke_without_command=True,
        help="View or edit economy guild configuration.",
    )
    @economy_admin_only()
    async def ecoadmin_config(self, ctx: commands.Context):
        config = await asyncio.to_thread(self.store.get_guild_config, ctx.guild.id)
        shop_channel_id = config.get("shop_channel_id")
        bypass_role_id = config.get("shop_bypass_role_id")
        embed = discord.Embed(title="Economy Config", color=0x2B2D31)
        embed.add_field(
            name="Shop Channel",
            value=f"<#{int(shop_channel_id)}>" if shop_channel_id else "Any channel",
            inline=True,
        )
        embed.add_field(
            name="Shop Bypass Role",
            value=f"<@&{int(bypass_role_id)}>" if bypass_role_id else "None",
            inline=True,
        )
        embed.set_footer(text="Use .eco config channel and .eco config bypass.")
        await ctx.send(embed=embed)

    @ecoadmin_config.command(
        name="channel",
        aliases=["shopchannel"],
        help="Set or clear the required shop channel.",
        usage="<#channel|id|clear>",
    )
    @economy_admin_only()
    async def ecoadmin_config_channel(self, ctx: commands.Context, target: str):
        if target.strip().lower() in {"clear", "none", "any", "0"}:
            await asyncio.to_thread(
                self.store.set_guild_config, ctx.guild.id, "shop_channel_id", None
            )
            return await ctx.send("Shop channel restriction cleared.")
        channel = await commands.TextChannelConverter().convert(ctx, target)
        await asyncio.to_thread(
            self.store.set_guild_config,
            ctx.guild.id,
            "shop_channel_id",
            channel.id,
        )
        await ctx.send(f"Shop channel set to {channel.mention}.")

    @ecoadmin_config.command(
        name="bypass",
        aliases=["bypassrole", "shopbypass"],
        help="Set or clear the shop bypass role.",
        usage="<@role|id|clear>",
    )
    @economy_admin_only()
    async def ecoadmin_config_bypass(self, ctx: commands.Context, target: str):
        if target.strip().lower() in {"clear", "none", "0"}:
            await asyncio.to_thread(
                self.store.set_guild_config, ctx.guild.id, "shop_bypass_role_id", None
            )
            return await ctx.send("Shop bypass role cleared.")
        role = await commands.RoleConverter().convert(ctx, target)
        await asyncio.to_thread(
            self.store.set_guild_config,
            ctx.guild.id,
            "shop_bypass_role_id",
            role.id,
        )
        await ctx.send(f"Shop bypass role set to {role.mention}.")

    @ecoadmin.command(
        name="bal",
        aliases=["balance", "credits"],
        help="Check a member's wallet, bank, and total credits.",
        usage="@user",
    )
    @economy_admin_only()
    async def ecoadmin_balance(self, ctx: commands.Context, member: discord.Member):
        if hasattr(self.store, "get_balances"):
            wallet, bank = await asyncio.to_thread(
                self.store.get_balances, ctx.guild.id, member.id
            )
            total = wallet + bank
            await ctx.send(
                f"💰 {member.mention} has:\n**Wallet:** {wallet:,} cr\n**Bank:** {bank:,} cr\n**Total:** {total:,} cr",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            balance = await asyncio.to_thread(
                self.store.get_balance, ctx.guild.id, member.id
            )
            await ctx.send(
                f"💰 {member.mention} has **{balance:,} cr**.",
                allowed_mentions=discord.AllowedMentions.none(),
            )

    @ecoadmin.command(
        name="add", aliases=["give"], help="Add credits to a member's wallet."
    )
    @economy_admin_only()
    async def ecoadmin_add(
        self, ctx: commands.Context, member: discord.Member, amount: str
    ):
        parsed = _parse_credit_amount(amount)
        if parsed is None or parsed <= 0:
            return await ctx.send("❌ Amount must be a positive whole number.")

        try:
            new_balance = await asyncio.to_thread(
                self.store.add_credits, ctx.guild.id, member.id, parsed
            )
        except CreditAmountOutOfRange:
            return await ctx.send(
                f"Amount is too high. Wallet balances can be at most **{MAX_BIGINT:,} cr**."
            )
        await ctx.send(
            f"✅ Added **{parsed:,} cr** to {member.mention}. New balance: **{new_balance:,} cr**.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @ecoadmin.command(
        name="remove",
        aliases=["take"],
        help="Remove credits from a member's wallet without going below zero.",
    )
    @economy_admin_only()
    async def ecoadmin_remove(
        self, ctx: commands.Context, member: discord.Member, amount: str
    ):
        parsed = _parse_credit_amount(amount)
        if parsed is None or parsed <= 0:
            return await ctx.send("❌ Amount must be a positive whole number.")

        current_balance = await asyncio.to_thread(
            self.store.get_balance, ctx.guild.id, member.id
        )
        new_balance = max(0, current_balance - parsed)
        removed = current_balance - new_balance
        await asyncio.to_thread(
            self.store.set_credits, ctx.guild.id, member.id, new_balance
        )
        await ctx.send(
            f"✅ Removed **{removed:,} cr** from {member.mention}. New balance: **{new_balance:,} cr**.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @ecoadmin.command(
        name="set",
        aliases=["setbal", "setcredits"],
        help="Set a member's wallet balance exactly.",
    )
    @economy_admin_only()
    async def ecoadmin_set(
        self, ctx: commands.Context, member: discord.Member, amount: str
    ):
        parsed = _parse_credit_amount(amount)
        if parsed is None:
            return await ctx.send("❌ Amount must be 0 or a positive whole number.")

        try:
            new_balance = await asyncio.to_thread(
                self.store.set_credits, ctx.guild.id, member.id, parsed
            )
        except CreditAmountOutOfRange:
            return await ctx.send(
                f"Amount is too high. Wallet balances can be at most **{MAX_BIGINT:,} cr**."
            )
        await ctx.send(
            f"✅ Set {member.mention}'s balance to **{new_balance:,} cr**.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @ecoadmin.command(
        name="bank", aliases=["setbank"], help="Set a member's bank balance exactly."
    )
    @economy_admin_only()
    async def ecoadmin_bank(
        self, ctx: commands.Context, member: discord.Member, amount: str
    ):
        parsed = _parse_credit_amount(amount)
        if parsed is None:
            return await ctx.send("Bank amount must be 0 or a positive whole number.")

        new_bank = await asyncio.to_thread(
            self.store.set_bank, ctx.guild.id, member.id, parsed
        )
        wallet, bank = await asyncio.to_thread(
            self.store.get_balances, ctx.guild.id, member.id
        )
        await ctx.send(
            f"Set {member.mention}'s bank to **{new_bank:,} cr**.\n"
            f"Wallet: **{wallet:,} cr** | Bank: **{bank:,} cr** | Total: **{wallet + bank:,} cr**",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @ecoadmin.command(
        name="bankadd",
        aliases=["addbank", "bankgive"],
        help="Add credits to a member's bank.",
    )
    @economy_admin_only()
    async def ecoadmin_bank_add(
        self, ctx: commands.Context, member: discord.Member, amount: str
    ):
        parsed = _parse_credit_amount(amount)
        if parsed is None or parsed <= 0:
            return await ctx.send("Amount must be a positive whole number.")

        new_bank = await asyncio.to_thread(
            self.store.add_bank, ctx.guild.id, member.id, parsed
        )
        await ctx.send(
            f"Added **{parsed:,} cr** to {member.mention}'s bank. New bank balance: **{new_bank:,} cr**.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @ecoadmin.command(
        name="bankremove",
        aliases=["removebank", "banktake"],
        help="Remove credits from a member's bank without going below zero.",
    )
    @economy_admin_only()
    async def ecoadmin_bank_remove(
        self, ctx: commands.Context, member: discord.Member, amount: str
    ):
        parsed = _parse_credit_amount(amount)
        if parsed is None or parsed <= 0:
            return await ctx.send("Amount must be a positive whole number.")

        current_wallet, current_bank = await asyncio.to_thread(
            self.store.get_balances, ctx.guild.id, member.id
        )
        new_bank = await asyncio.to_thread(
            self.store.remove_bank, ctx.guild.id, member.id, parsed
        )
        removed = max(0, current_bank - new_bank)
        await ctx.send(
            f"Removed **{removed:,} cr** from {member.mention}'s bank.\n"
            f"Wallet: **{current_wallet:,} cr** | Bank: **{new_bank:,} cr** | Total: **{current_wallet + new_bank:,} cr**",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @ecoadmin.command(
        name="reset",
        aliases=["resetbal"],
        help="Reset a member's wallet and bank balances to zero.",
    )
    @economy_admin_only()
    async def ecoadmin_reset(self, ctx: commands.Context, member: discord.Member):
        wallet, bank = await asyncio.to_thread(
            self.store.reset_member_economy, ctx.guild.id, member.id
        )
        await ctx.send(
            f"Reset {member.mention}'s wallet and bank.\nWallet: **{wallet:,} cr** | Bank: **{bank:,} cr**",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @ecoadmin.command(
        name="settle",
        aliases=["settleinvs", "settleinvest", "investsettle", "investmentsettle"],
        help="Force active investments to mature now and settle for a member.",
        usage="@user",
    )
    @economy_admin_only()
    async def ecoadmin_settle(self, ctx: commands.Context, member: discord.Member):
        settled = await asyncio.to_thread(
            self.store.settle_due_investments, ctx.guild.id, member.id, None, True
        )
        if not settled:
            return await ctx.send(
                f"{member.mention} has no active investments to settle.",
                allowed_mentions=discord.AllowedMentions.none(),
            )

        returned = sum(int(row.get("return_amount") or 0) for row in settled)
        await ctx.send(
            f"Forced **{len(settled)}** investment(s) to mature for {member.mention} and settled them. Returned **{returned:,} cr**.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @ecoadmin.group(
        name="shop",
        aliases=["roleshop"],
        invoke_without_command=True,
        help="Manage role shop stock.",
    )
    @economy_admin_only()
    async def ecoadmin_shop(self, ctx: commands.Context):
        await self._send_shop_inventory(ctx)

    @ecoadmin_shop.command(name="restock", help="Restock the role shop.")
    @economy_admin_only()
    async def ecoadmin_shop_restock(self, ctx: commands.Context):
        await asyncio.to_thread(self.store.refresh_inventory, ctx.guild.id, SHOP_ITEMS)
        await ctx.send("Role shop restocked.")

    @ecoadmin_shop.command(name="inv", aliases=["inventory"], help="Show role shop stock.")
    @economy_admin_only()
    async def ecoadmin_shop_inventory(self, ctx: commands.Context):
        await self._send_shop_inventory(ctx)

    @ecoadmin_shop.command(
        name="stock", aliases=["setstock"], help="Set stock for one role shop item."
    )
    @economy_admin_only()
    async def ecoadmin_shop_stock(
        self, ctx: commands.Context, amount: str, *, role_name: str
    ):
        parsed = _parse_credit_amount(amount)
        if parsed is None:
            return await ctx.send("Stock must be 0 or a positive whole number.")
        matched_name = _match_shop_item_name(role_name)
        if matched_name is None:
            return await ctx.send(f"`{role_name}` is not a valid shop role.")
        new_stock = await asyncio.to_thread(
            self.store.set_inventory_stock, ctx.guild.id, matched_name, parsed
        )
        await ctx.send(f"`{matched_name}` stock set to **{new_stock:,}**.")

    @ecoadmin.group(
        name="bmshop",
        aliases=["blackmarketshop", "blackmarket"],
        invoke_without_command=True,
        help="Manage blackmarket stock.",
    )
    @economy_admin_only()
    async def ecoadmin_bmshop(self, ctx: commands.Context):
        await self._send_blackmarket_inventory(ctx)

    @ecoadmin_bmshop.command(name="restock", help="Restock the blackmarket.")
    @economy_admin_only()
    async def ecoadmin_bmshop_restock(self, ctx: commands.Context):
        await asyncio.to_thread(
            self.store.refresh_blackmarket_inventory,
            ctx.guild.id,
            ECONOMY_ITEM_DEFS,
            LOCK_LEVELS,
        )
        await ctx.send("Blackmarket restocked.")

    @ecoadmin_bmshop.command(name="inv", aliases=["inventory"], help="Show blackmarket stock.")
    @economy_admin_only()
    async def ecoadmin_bmshop_inventory(self, ctx: commands.Context):
        await self._send_blackmarket_inventory(ctx)

    @ecoadmin_bmshop.command(
        name="stock", aliases=["setstock"], help="Set stock for a blackmarket item."
    )
    @economy_admin_only()
    async def ecoadmin_bmshop_stock(self, ctx: commands.Context, *, raw: str):
        item_key, amount, error = _parse_item_amount(raw, _resolve_blackmarket_item)
        if error or item_key is None:
            return await ctx.send("Usage: `.eco bmshop stock <item> <amount>`")
        result = await asyncio.to_thread(
            self.store.set_blackmarket_admin_entry,
            ctx.guild.id,
            item_key,
            stock=amount,
        )
        await ctx.send(
            f"Blackmarket `{item_key}` stock set to **{int(result['stock']):,}**."
        )

    @ecoadmin_bmshop.command(
        name="discount", aliases=["setdiscount"], help="Set a blackmarket item discount."
    )
    @economy_admin_only()
    async def ecoadmin_bmshop_discount(self, ctx: commands.Context, *, raw: str):
        item_key, amount, error = _parse_item_amount(raw, _resolve_blackmarket_item)
        if error or item_key is None or amount is None or not 0 <= int(amount) <= 100:
            return await ctx.send("Usage: `.eco bmshop discount <item> <0-100>`")
        result = await asyncio.to_thread(
            self.store.set_blackmarket_admin_entry,
            ctx.guild.id,
            item_key,
            discount=amount,
        )
        await ctx.send(
            f"Blackmarket `{item_key}` discount set to **{int(result['discount'])}%**."
        )

    @ecoadmin.group(
        name="petshop",
        aliases=["cores", "coreshop"],
        invoke_without_command=True,
        help="Manage fusion-core shop stock.",
    )
    @economy_admin_only()
    async def ecoadmin_petshop(self, ctx: commands.Context):
        rows = await asyncio.to_thread(self.store.ensure_pet_core_shop, ctx.guild.id)
        lines = [
            f"**{PET_ITEM_DEFS[row['item_key']]['name']}**: {int(row['stock']):,} in stock"
            for row in rows
        ]
        await ctx.send("\n".join(lines))

    @ecoadmin_petshop.command(name="restock", help="Restock all fusion-core materials.")
    @economy_admin_only()
    async def ecoadmin_petshop_restock(self, ctx: commands.Context):
        await asyncio.to_thread(self.store.refresh_pet_core_shop, ctx.guild.id)
        await ctx.send("Fusion-core shop restocked.")

    @ecoadmin_petshop.command(
        name="stock", aliases=["setstock"], help="Set stock for one fusion-core material."
    )
    @economy_admin_only()
    async def ecoadmin_petshop_stock(
        self, ctx: commands.Context, item: str, amount: str
    ):
        item_key = _match_pet_item(item)
        parsed = _parse_credit_amount(amount)
        if item_key not in PET_CORE_SHOP or parsed is None:
            return await ctx.send("Usage: `.eco petshop stock <item> <amount>`")
        stock = await asyncio.to_thread(
            self.store.set_pet_core_stock, ctx.guild.id, item_key, parsed
        )
        await ctx.send(
            f"`{PET_ITEM_DEFS[item_key]['name']}` stock set to **{int(stock):,}**."
        )

    @ecoadmin.group(
        name="utilshop",
        aliases=["utilityshop"],
        invoke_without_command=True,
        help="View utility shop inventory settings.",
    )
    @economy_admin_only()
    async def ecoadmin_utilshop(self, ctx: commands.Context):
        await ctx.send(
            "The utility shop has unlimited shared stock, so there is nothing to restock."
        )

    @ecoadmin_utilshop.command(name="restock", help="Show utility shop restock status.")
    @economy_admin_only()
    async def ecoadmin_utilshop_restock(self, ctx: commands.Context):
        await ctx.send(
            "The utility shop has unlimited shared stock, so it is always fully stocked."
        )

    @ecoadmin.command(
        name="restock",
        aliases=["refreshshop"],
        help="Force a full restock of every limited shop inventory.",
    )
    @economy_admin_only()
    async def ecoadmin_restock(self, ctx: commands.Context):
        await asyncio.gather(
            asyncio.to_thread(self.store.refresh_inventory, ctx.guild.id, SHOP_ITEMS),
            asyncio.to_thread(
                self.store.refresh_blackmarket_inventory,
                ctx.guild.id,
                ECONOMY_ITEM_DEFS,
                LOCK_LEVELS,
            ),
            asyncio.to_thread(self.store.refresh_pet_core_shop, ctx.guild.id),
        )
        inventory = await asyncio.to_thread(self.store.get_inventory, ctx.guild.id)
        total_stock = sum(max(0, int(stock)) for stock in inventory.values())
        await ctx.send(
            f"✅ Shop restocked. **{total_stock:,}** total items are now in stock. Next automatic restock {_format_restock_countdown(None)}."
        )

    @ecoadmin.command(
        name="stock", aliases=["setstock"], help="Set stock for one shop role."
    )
    @economy_admin_only()
    async def ecoadmin_stock(
        self, ctx: commands.Context, amount: str, *, role_name: str
    ):
        parsed = _parse_credit_amount(amount)
        if parsed is None:
            return await ctx.send("❌ Stock must be 0 or a positive whole number.")

        matched_name = _match_shop_item_name(role_name)
        if matched_name is None:
            return await ctx.send(f"❌ `{role_name}` is not a valid shop role.")

        max_stock = int(SHOP_ITEMS[matched_name]["max_stock"])
        new_stock = await asyncio.to_thread(
            self.store.set_inventory_stock, ctx.guild.id, matched_name, parsed
        )
        override_note = (
            f" Default max is **{max_stock}**; admin override is active."
            if parsed > max_stock
            else ""
        )
        await ctx.send(
            f"✅ `{matched_name}` stock set to **{new_stock:,}**.{override_note}"
        )

    @ecoadmin.command(
        name="prestige",
        aliases=["setprestige"],
        help="Set a member's economy prestige level.",
    )
    @economy_admin_only()
    async def ecoadmin_prestige(
        self, ctx: commands.Context, member: discord.Member, level: str
    ):
        parsed = _parse_credit_amount(level)
        if parsed is None:
            return await ctx.send(
                f"Prestige must be a whole number from 0 to {PRESTIGE_MAX_LEVEL}."
            )
        result = await asyncio.to_thread(
            self.store.set_prestige_level, ctx.guild.id, member.id, parsed
        )
        await ctx.send(
            f"Set {member.mention}'s prestige to **{int(result['prestige_level'])}/{PRESTIGE_MAX_LEVEL}** "
            f"(+{float(result['bonus_rate']) * 100:.0f}% perk bonus).",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @ecoadmin.command(
        name="booster",
        aliases=["boost", "setbooster"],
        help="Manually toggle a member's booster perk.",
    )
    @economy_admin_only()
    async def ecoadmin_booster(
        self, ctx: commands.Context, member: discord.Member, state: str
    ):
        normalized = state.strip().lower()
        if normalized not in {"on", "off", "true", "false", "yes", "no", "1", "0"}:
            return await ctx.send(
                "Use `.eco booster @user on` or `.eco booster @user off`."
            )
        enabled = normalized in {"on", "true", "yes", "1"}
        result = await asyncio.to_thread(
            self.store.set_member_booster, ctx.guild.id, member.id, enabled
        )
        await ctx.send(
            f"Booster perks for {member.mention}: **{'on' if result.get('is_booster') else 'off'}**.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @ecoadmin.command(
        name="roleperk",
        aliases=["roleperks", "perkrole"],
        help="Grant or remove a persisted shop-role perk.",
    )
    @economy_admin_only()
    async def ecoadmin_roleperk(
        self,
        ctx: commands.Context,
        member: discord.Member,
        action: str,
        *,
        role_name: str,
    ):
        normalized = action.strip().lower()
        if normalized not in {"grant", "add", "on", "remove", "delete", "off"}:
            return await ctx.send(
                "Use `.eco roleperk @user grant <shop role>` or `.eco roleperk @user remove <shop role>`."
            )
        matched_name = _match_shop_item_name(role_name)
        if matched_name is None:
            return await ctx.send(f"`{role_name}` is not a valid shop role.")
        enabled = normalized in {"grant", "add", "on"}
        result = await asyncio.to_thread(
            self.store.set_role_perk, ctx.guild.id, member.id, matched_name, enabled
        )
        if not result.get("ok"):
            return await ctx.send("I could not update that role perk.")
        perk = SHOP_ROLE_PERKS.get(matched_name, {})
        await ctx.send(
            f"{'Granted' if enabled else 'Removed'} **{matched_name}** perk for {member.mention}."
            f"\nPerk: {perk.get('label', 'No bonus configured')}",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @ecoadmin.command(
        name="rolereset",
        aliases=["roleperkreset", "clearroles", "clearperks"],
        help="Remove all persisted shop-role perks from a member.",
        usage="@user",
    )
    @economy_admin_only()
    async def ecoadmin_rolereset(self, ctx: commands.Context, member: discord.Member):
        count = await asyncio.to_thread(
            self.store.reset_member_role_perks, ctx.guild.id, member.id
        )
        await ctx.send(
            f"Removed **{count:,}** persisted shop-role perk(s) from {member.mention}.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    def _booster_description(self) -> str:
        return (
            "**Server Booster Perks**\n"
            f"- **+{BOOSTER_BONUS_RATE * 100:.0f}%** bonus on perked economy payouts\n"
            "- Stacks with pets, prestige, and shop-role bonuses\n"
            "- Applies to daily, work, fish, hunt, crime, casino profit, and passive pet income\n"
            "- Boost thank-you posts appear in this panel channel\n\n"
            "**Stacking Rules**\n"
            f"- Prestige adds **+{PRESTIGE_BONUS_PER_LEVEL * 100:.0f}%** per prestige level\n"
            "- Shop roles add their listed perk bonuses when purchased from `.shop`\n"
            "- Total bonuses are capped per payout type so rewards stay balanced"
        )

    def _booster_panel_view(self, guild: discord.Guild) -> discord.ui.LayoutView:
        view = discord.ui.LayoutView(timeout=None)
        header = (
            "Boosting activates a permanent server-booster economy flag while the boost is active.\n"
            f"Booster bonus **+{BOOSTER_BONUS_RATE * 100:.0f}%** | "
            f"Prestige scaling **+{PRESTIGE_BONUS_PER_LEVEL * 100:.0f}%/level** | "
            "Pets and shop roles stack with caps."
        )
        header_accessory = discord.ui.Thumbnail(guild.icon.url) if guild.icon else None
        children: list[discord.ui.Item[Any]] = [
            discord.ui.Section(
                discord.ui.TextDisplay(f"**Soul Booster Perks**\n{header}"),
                accessory=header_accessory,
            )
            if header_accessory
            else discord.ui.TextDisplay(f"**Soul Booster Perks**\n{header}"),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.large),
            discord.ui.TextDisplay(
                "**Booster Economy Bonus**\n"
                f"**+{BOOSTER_BONUS_RATE * 100:.0f}%** bonus on daily, work, fish, hunt, crime, casino profit, and passive pet income."
            ),
            discord.ui.TextDisplay(
                "**Stacks With**\n"
                f"Prestige up to **{PRESTIGE_MAX_LEVEL}** levels, evolved pet bonuses, fused pet bonuses, and purchased shop-role perks."
            ),
            discord.ui.TextDisplay(
                "**Payout Caps**\n"
                f"General payouts cap at **{TOTAL_BONUS_CAPS['default'] * 100:.0f}%**, "
                f"casino profit at **{TOTAL_BONUS_CAPS['gambling'] * 100:.0f}%**, "
                f"passive income at **{TOTAL_BONUS_CAPS['passive'] * 100:.0f}%**."
            ),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.large),
            discord.ui.TextDisplay(
                "**Best Pairings**\n"
                "**Royalty**: casino profit | **Divinity**: passive pet income | "
                "**The One Who Remains**: all perked payouts"
            ),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
            discord.ui.TextDisplay(
                "Use `.perks` to see your active stack. This channel will also receive boost activation posts."
            ),
        ]
        view.add_item(discord.ui.Container(*children, accent_color=0xF47FFF))
        return ensure_layout_view_action_rows(view)

    @ecoadmin.command(name="bmrestock")
    @economy_admin_only()
    async def ecoadmin_bmrestock(self, ctx: commands.Context):
        await asyncio.to_thread(
            self.store.refresh_blackmarket_inventory,
            ctx.guild.id,
            ECONOMY_ITEM_DEFS,
            LOCK_LEVELS,
        )
        return await ctx.send("Blackmarket restocked.")

        result = await asyncio.to_thread(self.store.refresh_blackmarket_inventory, ctx.guild.id, force=True)
        if result["ok"]:
            await ctx.send(f"✅ Blackmarket restocked. Next refresh in {result.get('next_refresh_minutes', '?')}m.")
        else:
            await ctx.send("❌ Failed to restock blackmarket.")

    @ecoadmin.command(name="bminv")
    @economy_admin_only()
    async def ecoadmin_bminv(self, ctx: commands.Context):
        return await self._send_blackmarket_inventory(ctx)

        items = await asyncio.to_thread(self.store.get_blackmarket_inventory, ctx.guild.id)
        if not items:
            return await ctx.send("Blackmarket is empty.")
        lines = [f"**{i['item_key']}**: {i['current_stock']} left (-{i['sale_discount']}%)" for i in items]
        await ctx.send("\n".join(lines))

    @ecoadmin.command(
        name="bmstock",
        aliases=["setbmstock"],
        help="Set blackmarket stock for a tool or lock.",
        usage="<item> <amount>",
    )
    @economy_admin_only()
    async def ecoadmin_bmstock(self, ctx: commands.Context, *, raw: str):
        item_key, amount, error = _parse_item_amount(raw, _resolve_blackmarket_item)
        if error:
            return await ctx.send(f"{error} Usage: `.eco bmstock <item> <amount>`")
        if item_key is None:
            return await ctx.send(f"`{raw}` is not a valid blackmarket item.")
        result = await asyncio.to_thread(
            self.store.set_blackmarket_admin_entry,
            ctx.guild.id,
            item_key,
            stock=amount,
        )
        stock = int(result["stock"])
        item = item_key
        return await ctx.send(
            f"Blackmarket `{item_key}` stock set to **{stock:,}** "
            f"with **{int(result['discount'])}%** discount."
        )

        result = await asyncio.to_thread(self.store.set_blackmarket_inventory_stock, ctx.guild.id, item, stock)
        if result.get("ok"):
            await ctx.send(f"✅ Blackmarket stock for `{item}` set to **{stock}**.")
        else:
            await ctx.send("❌ Failed to set stock.")

    @ecoadmin.command(
        name="bmdiscount",
        aliases=["setbmdiscount"],
        help="Set blackmarket discount for a tool or lock without changing stock.",
        usage="<item> <0-100>",
    )
    @economy_admin_only()
    async def ecoadmin_bmdiscount(self, ctx: commands.Context, *, raw: str):
        item_key, amount, error = _parse_item_amount(raw, _resolve_blackmarket_item)
        if error:
            return await ctx.send(f"{error} Usage: `.eco bmdiscount <item> <0-100>`")
        if item_key is None:
            return await ctx.send(f"`{raw}` is not a valid blackmarket item.")
        if amount is None or not 0 <= int(amount) <= 100:
            return await ctx.send("Discount must be between 0 and 100.")
        result = await asyncio.to_thread(
            self.store.set_blackmarket_admin_entry,
            ctx.guild.id,
            item_key,
            discount=amount,
        )
        discount = int(result["discount"])
        item = item_key
        return await ctx.send(
            f"Blackmarket `{item_key}` discount set to **{discount}%** "
            f"with **{int(result['stock']):,}** stock preserved."
        )

        result = await asyncio.to_thread(self.store.set_blackmarket_inventory_stock, ctx.guild.id, item, 0, discount)
        if result.get("ok"):
            await ctx.send(f"✅ Blackmarket discount for `{item}` set to **{discount}%** (Stock unchanged, defaulting to 0 if not exist).")
        else:
            await ctx.send("❌ Failed to set discount.")

    @ecoadmin.command(
        name="itemgrant",
        aliases=["giveitem"],
        help="Add or remove economy item quantity for a member.",
        usage="@user <item> <amount>",
    )
    @economy_admin_only()
    async def ecoadmin_itemgrant(self, ctx: commands.Context, member: discord.Member, *, raw: str):
        item_key, amount, error = _parse_item_amount(raw, _match_economy_item)
        if error:
            return await ctx.send(f"{error} Usage: `.eco itemgrant @user <item> <amount>`")
        if item_key is None or amount is None:
            return await ctx.send(f"`{raw}` is not a valid economy item plus amount.")
        result = await asyncio.to_thread(
            self.store.grant_economy_item, ctx.guild.id, member.id, item_key, amount
        )
        item = item_key
        if result.get("ok"):
            return await ctx.send(
                f"Updated `{item_key}` by **{amount:+,}** for {member.mention}. "
                f"New total: **{int(result['quantity']):,}**.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        return await ctx.send(f"Failed to update `{item_key}`: {result.get('reason', 'unknown')}.")

        result = await asyncio.to_thread(self.store.grant_economy_item, ctx.guild.id, member.id, item, amount)
        if result.get("ok"):
            await ctx.send(f"✅ Granted {amount}x `{item}` to {member.mention}. New total: **{result['quantity']}**.")
        else:
            await ctx.send("❌ Failed to grant item.")

    @ecoadmin.command(
        name="itemset",
        aliases=["setitem"],
        help="Set an economy item quantity exactly.",
        usage="@user <item> <amount>",
    )
    @economy_admin_only()
    async def ecoadmin_itemset(self, ctx: commands.Context, member: discord.Member, *, raw: str):
        item_key, amount, error = _parse_item_amount(raw, _match_economy_item)
        if error:
            return await ctx.send(f"{error} Usage: `.eco itemset @user <item> <amount>`")
        if item_key is None or amount is None:
            return await ctx.send(f"`{raw}` is not a valid economy item plus amount.")
        result = await asyncio.to_thread(
            self.store.set_economy_item_quantity,
            ctx.guild.id,
            member.id,
            item_key,
            amount,
        )
        await ctx.send(
            f"Set `{item_key}` for {member.mention} to **{int(result['quantity']):,}**.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @ecoadmin.command(name="itemreset")
    @economy_admin_only()
    async def ecoadmin_itemreset(self, ctx: commands.Context, member: discord.Member):
        count = await asyncio.to_thread(self.store.reset_member_economy_items, ctx.guild.id, member.id)
        return await ctx.send(
            f"Reset **{count:,}** economy item row(s) for {member.mention}.",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await ctx.send(f"✅ Reset {count} economy items for {member.mention}.")

    @ecoadmin.group(
        name="pet",
        invoke_without_command=True,
        help="Manage a member's pets.",
    )
    @economy_admin_only()
    async def ecoadmin_pet(self, ctx: commands.Context):
        await ctx.send(
            "Use `.eco pet give @user <pet> [stage]`, `.eco pet remove @user <pet_id>`, or `.eco pet duplicate @user <pet_id>`."
        )

    @ecoadmin_pet.command(name="give", help="Give a pet line to a member.")
    @economy_admin_only()
    async def ecoadmin_pet_give(
        self, ctx: commands.Context, member: discord.Member, pet: str, stage: int = 1
    ):
        pet_key = _match_pet_line(pet)
        if pet_key is None:
            return await ctx.send(f"`{pet}` is not a valid pet line.")
        result = await asyncio.to_thread(
            self.store.grant_pet, ctx.guild.id, member.id, pet_key, stage
        )
        if not result.get("ok"):
            return await ctx.send(f"Failed to give pet: {result.get('reason', 'unknown')}.")
        created = result["pet"]
        await ctx.send(
            f"Gave `{pet_key}` stage **{int(created['stage'])}** to {member.mention} as pet ID `{created['id']}`.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @ecoadmin_pet.command(name="remove", help="Remove a member's pet by ID.")
    @economy_admin_only()
    async def ecoadmin_pet_remove(
        self, ctx: commands.Context, member: discord.Member, pet_id: int
    ):
        result = await asyncio.to_thread(
            self.store.remove_pet, ctx.guild.id, member.id, pet_id
        )
        if not result.get("ok"):
            return await ctx.send("That pet ID does not belong to that member.")
        await ctx.send(
            f"Removed pet ID `{pet_id}` from {member.mention}.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @ecoadmin_pet.command(name="duplicate", aliases=["clone"], help="Duplicate one owned pet by ID.")
    @economy_admin_only()
    async def ecoadmin_pet_duplicate(
        self, ctx: commands.Context, member: discord.Member, pet_id: int
    ):
        result = await asyncio.to_thread(
            self.store.duplicate_pet, ctx.guild.id, member.id, pet_id
        )
        if not result.get("ok"):
            return await ctx.send("That pet ID does not belong to that member.")
        duplicate = result["pet"]
        await ctx.send(
            f"Duplicated pet ID `{pet_id}` for {member.mention} as new pet ID `{duplicate['id']}`.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @ecoadmin.command(
        name="petgrant",
        aliases=["givepet"],
        help="Grant a pet line to a member.",
        usage="@user <pet> [stage]",
    )
    @economy_admin_only()
    async def ecoadmin_petgrant(
        self, ctx: commands.Context, member: discord.Member, pet: str, stage: int = 1
    ):
        pet_key = _match_pet_line(pet)
        if pet_key is None:
            return await ctx.send(f"`{pet}` is not a valid pet line.")
        result = await asyncio.to_thread(
            self.store.grant_pet, ctx.guild.id, member.id, pet_key, stage
        )
        pet = pet_key
        if result.get("ok"):
            created = result.get("pet") or {}
            return await ctx.send(
                f"Granted `{pet_key}` stage **{int(created.get('stage') or stage)}** "
                f"to {member.mention} as pet ID `{created.get('id', '?')}`.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        return await ctx.send(f"Failed to grant pet: {result.get('reason', 'unknown')}.")

        result = await asyncio.to_thread(self.store.grant_pet, ctx.guild.id, member.id, pet, stage)
        if result.get("ok"):
            await ctx.send(f"✅ Granted `{pet}` (stage {stage}) to {member.mention}.")
        else:
            await ctx.send(f"❌ Failed to grant pet: {result.get('reason')}")

    @ecoadmin.command(name="petremove")
    @economy_admin_only()
    async def ecoadmin_petremove(self, ctx: commands.Context, member: discord.Member, pet_id: int):
        result = await asyncio.to_thread(self.store.remove_pet, ctx.guild.id, member.id, pet_id)
        if result.get("ok"):
            await ctx.send(f"✅ Removed pet ID `{pet_id}` from {member.mention}.")
        else:
            await ctx.send("❌ Failed to remove pet (Not found).")

    @ecoadmin.group(
        name="petitem",
        invoke_without_command=True,
        help="Manage a member's pet materials.",
    )
    @economy_admin_only()
    async def ecoadmin_petitem_group(self, ctx: commands.Context):
        await ctx.send(
            "Use `.eco petitem give @user <item> <amount>`, `.eco petitem remove @user <item> <amount>`, or `.eco petitem set @user <item> <amount>`."
        )

    @ecoadmin_petitem_group.command(name="give", help="Give pet materials to a member.")
    @economy_admin_only()
    async def ecoadmin_petitem_give(
        self, ctx: commands.Context, member: discord.Member, *, raw: str
    ):
        item_key, amount, error = _parse_item_amount(raw, _match_pet_item)
        if error or item_key is None or amount is None or amount <= 0:
            return await ctx.send("Usage: `.eco petitem give @user <item> <positive amount>`")
        result = await asyncio.to_thread(
            self.store.grant_pet_item, ctx.guild.id, member.id, item_key, amount
        )
        await ctx.send(
            f"Gave **{amount:,}x** `{item_key}` to {member.mention}. New total: **{int(result['quantity']):,}**.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @ecoadmin_petitem_group.command(name="remove", help="Remove pet materials from a member.")
    @economy_admin_only()
    async def ecoadmin_petitem_remove(
        self, ctx: commands.Context, member: discord.Member, *, raw: str
    ):
        item_key, amount, error = _parse_item_amount(raw, _match_pet_item)
        if error or item_key is None or amount is None or amount <= 0:
            return await ctx.send("Usage: `.eco petitem remove @user <item> <positive amount>`")
        result = await asyncio.to_thread(
            self.store.grant_pet_item, ctx.guild.id, member.id, item_key, -amount
        )
        await ctx.send(
            f"Removed up to **{amount:,}x** `{item_key}` from {member.mention}. New total: **{int(result['quantity']):,}**.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @ecoadmin_petitem_group.command(name="set", help="Set a pet material quantity exactly.")
    @economy_admin_only()
    async def ecoadmin_petitem_set(
        self, ctx: commands.Context, member: discord.Member, *, raw: str
    ):
        item_key, amount, error = _parse_item_amount(raw, _match_pet_item)
        if error or item_key is None or amount is None or amount < 0:
            return await ctx.send("Usage: `.eco petitem set @user <item> <amount>`")
        result = await asyncio.to_thread(
            self.store.set_pet_item_quantity,
            ctx.guild.id,
            member.id,
            item_key,
            amount,
        )
        await ctx.send(
            f"Set `{item_key}` for {member.mention} to **{int(result['quantity']):,}**.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @ecoadmin.command(
        name="petitemlegacy",
        aliases=["givepetitem"],
        help="Add or remove pet material quantity for a member.",
        usage="@user <item> <amount>",
    )
    @economy_admin_only()
    async def ecoadmin_petitem(self, ctx: commands.Context, member: discord.Member, *, raw: str):
        item_key, amount, error = _parse_item_amount(raw, _match_pet_item)
        if error:
            return await ctx.send(f"{error} Usage: `.eco petitem @user <item> <amount>`")
        if item_key is None or amount is None:
            return await ctx.send(f"`{raw}` is not a valid pet material plus amount.")
        result = await asyncio.to_thread(
            self.store.grant_pet_item, ctx.guild.id, member.id, item_key, amount
        )
        item = item_key
        if result.get("ok"):
            return await ctx.send(
                f"Updated `{item_key}` by **{amount:+,}** for {member.mention}. "
                f"New total: **{int(result['quantity']):,}**.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        return await ctx.send(f"Failed to update `{item_key}`: {result.get('reason', 'unknown')}.")

        result = await asyncio.to_thread(self.store.grant_pet_item, ctx.guild.id, member.id, item, amount)
        if result.get("ok"):
            await ctx.send(f"✅ Granted {amount}x `{item}` to {member.mention}. New total: **{result['quantity']}**.")
        else:
            await ctx.send("❌ Failed to grant pet item.")

    @ecoadmin.command(
        name="petitemset",
        aliases=["setpetitem"],
        help="Set a pet material quantity exactly.",
        usage="@user <item> <amount>",
    )
    @economy_admin_only()
    async def ecoadmin_petitemset(self, ctx: commands.Context, member: discord.Member, *, raw: str):
        item_key, amount, error = _parse_item_amount(raw, _match_pet_item)
        if error:
            return await ctx.send(f"{error} Usage: `.eco petitemset @user <item> <amount>`")
        if item_key is None or amount is None:
            return await ctx.send(f"`{raw}` is not a valid pet material plus amount.")
        result = await asyncio.to_thread(
            self.store.set_pet_item_quantity,
            ctx.guild.id,
            member.id,
            item_key,
            amount,
        )
        await ctx.send(
            f"Set `{item_key}` for {member.mention} to **{int(result['quantity']):,}**.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @ecoadmin.command(
        name="petstage",
        aliases=["setpetstage"],
        help="Set a specific owned pet's stage.",
        usage="@user <pet_id> <1-5>",
    )
    @economy_admin_only()
    async def ecoadmin_petstage(
        self, ctx: commands.Context, member: discord.Member, pet_id: int, stage: int
    ):
        result = await asyncio.to_thread(
            self.store.update_pet_admin,
            ctx.guild.id,
            member.id,
            pet_id,
            stage=stage,
        )
        if not result.get("ok"):
            return await ctx.send(f"Could not update pet `{pet_id}`: {result.get('reason')}.")
        pet = result["pet"]
        await ctx.send(
            f"Set pet `#{pet_id}` for {member.mention} to stage **{int(pet['stage'])}**.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @ecoadmin.command(
        name="pethappy",
        aliases=["pethappiness"],
        help="Set a specific owned pet's happiness.",
        usage="@user <pet_id> <0-100>",
    )
    @economy_admin_only()
    async def ecoadmin_pethappy(
        self, ctx: commands.Context, member: discord.Member, pet_id: int, happiness: int
    ):
        result = await asyncio.to_thread(
            self.store.update_pet_admin,
            ctx.guild.id,
            member.id,
            pet_id,
            happiness=happiness,
        )
        if not result.get("ok"):
            return await ctx.send(f"Could not update pet `{pet_id}`: {result.get('reason')}.")
        pet = result["pet"]
        await ctx.send(
            f"Set pet `#{pet_id}` for {member.mention} happiness to **{int(pet['happiness'])}/100**.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @ecoadmin.command(
        name="petrename",
        aliases=["renamepet"],
        help="Set or clear a specific owned pet's custom name.",
        usage="@user <pet_id> <name|clear>",
    )
    @economy_admin_only()
    async def ecoadmin_petrename(
        self, ctx: commands.Context, member: discord.Member, pet_id: int, *, name: str
    ):
        clear_name = name.strip().lower() in {"clear", "none", "reset"}
        result = await asyncio.to_thread(
            self.store.update_pet_admin,
            ctx.guild.id,
            member.id,
            pet_id,
            custom_name=None if clear_name else name,
            clear_name=clear_name,
        )
        if not result.get("ok"):
            return await ctx.send(f"Could not update pet `{pet_id}`: {result.get('reason')}.")
        pet = result["pet"]
        display = pet.get("custom_name") or _pet_display_name(pet)
        await ctx.send(
            f"Pet `#{pet_id}` for {member.mention} is now named **{display}**.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @ecoadmin.command(name="petreset")
    @economy_admin_only()
    async def ecoadmin_petreset(self, ctx: commands.Context, member: discord.Member):
        result = await asyncio.to_thread(self.store.reset_member_pets, ctx.guild.id, member.id)
        return await ctx.send(
            f"Wiped **{result.get('pets', 0):,}** pets and **{result.get('items', 0):,}** pet item rows from {member.mention}.",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await ctx.send(f"✅ Wiped {result.get('deleted_pets', 0)} pets and {result.get('deleted_items', 0)} pet items from {member.mention}.")

    @ecoadmin.command(name="wanted")
    @economy_admin_only()
    async def ecoadmin_wanted(self, ctx: commands.Context, member: discord.Member, level: int):
        new_level = await asyncio.to_thread(self.store.set_wanted_level, ctx.guild.id, member.id, level)
        return await ctx.send(
            f"Set wanted level for {member.mention} to **{new_level}**.",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await ctx.send(f"✅ Set wanted level for {member.mention} to **{new_level}**.")

    @ecoadmin.command(
        name="jail",
        help="Jail a member for a human duration.",
        usage="@user <duration>",
    )
    @economy_admin_only()
    async def ecoadmin_jail(self, ctx: commands.Context, member: discord.Member, duration: str):
        minutes = _parse_duration_minutes(duration)
        if minutes is None:
            return await ctx.send(
                "Use a duration like `30m`, `12h`, `7d`, `1w`, or plain minutes."
            )
        jail_time = await asyncio.to_thread(self.store.set_jail_time, ctx.guild.id, member.id, minutes)
        if jail_time is None:
            return await ctx.send(
                f"Jail cleared for {member.mention}.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        return await ctx.send(
            f"Jailed {member.mention} for **{_format_duration_minutes(minutes)}**, until {_fmt_dt(jail_time)}.",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await ctx.send(f"✅ Jailed {member.mention} for **{minutes}** minutes (Until {jail_time}).")

    @ecoadmin.command(name="unjail")
    @economy_admin_only()
    async def ecoadmin_unjail(self, ctx: commands.Context, member: discord.Member):
        await asyncio.to_thread(self.store.set_jail_time, ctx.guild.id, member.id, 0)
        return await ctx.send(
            f"Unjailed {member.mention}.",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await ctx.send(f"✅ Unjailed {member.mention}.")

    @ecoadmin.command(name="lock")
    @economy_admin_only()
    async def ecoadmin_lock(self, ctx: commands.Context, member: discord.Member, level: int):
        new_level = await asyncio.to_thread(self.store.set_lock_level, ctx.guild.id, member.id, level)
        return await ctx.send(
            f"Set lock level for {member.mention} to **{new_level}**.",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await ctx.send(f"✅ Set lock level for {member.mention} to **{new_level}**.")

    @ecoadmin.command(name="wipe", aliases=["wipeall"])
    @economy_admin_only()
    async def ecoadmin_wipeall(self, ctx: commands.Context, member: discord.Member):
        counts = await asyncio.to_thread(self.store.wipe_member_completely, ctx.guild.id, member.id)
        rows = sum(int(value) for value in counts.values())

        # Strip shop roles from the Discord member so .bal reflects the wipe
        removed_roles = []
        shop_role_names = set(SHOP_ITEMS.keys())
        for role in member.roles:
            if role.name in shop_role_names:
                try:
                    await member.remove_roles(role, reason=f"Economy wipe by {ctx.author}")
                    removed_roles.append(role.name)
                except Exception:
                    pass

        role_note = f"\nRemoved Discord roles: **{', '.join(removed_roles)}**" if removed_roles else ""
        return await ctx.send(
            f"🚨 Wiped all economy state for {member.mention}. Rows touched: **{rows:,}**.{role_note}",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.command(
        name="booster",
        aliases=["boosters", "boost"],
        help="Admin: post the booster perks panel and set the boost thank-you channel.",
    )
    @economy_admin_only()
    async def booster_cmd(self, ctx: commands.Context):
        if ctx.guild is None:
            return await ctx.send("Use this in a server.")
        view = self._booster_panel_view(ctx.guild)
        msg = await ctx.send(view=view, allowed_mentions=discord.AllowedMentions.none())
        await asyncio.to_thread(
            self.store.set_booster_panel, ctx.guild.id, ctx.channel.id, msg.id
        )

    @commands.command(
        name="prestige",
        aliases=["prestiges"],
        help="View or buy economy prestige levels. Use `.prestige confirm` to buy.",
    )
    async def prestige_cmd(self, ctx: commands.Context, action: str = ""):
        if ctx.guild is None:
            return await ctx.send("Use this in a server.")
        if action.strip().lower() in {"confirm", "buy", "upgrade"}:
            result = await asyncio.to_thread(
                self.store.buy_prestige, ctx.guild.id, ctx.author.id
            )
            if not result.get("ok"):
                if result.get("reason") == "maxed":
                    text = f"You are already at max prestige **{int(result['level'])}/{PRESTIGE_MAX_LEVEL}**."
                else:
                    text = (
                        f"Next prestige costs **{int(result['cost']):,} cr**.\n"
                        f"Your wallet + bank total is **{int(result['total']):,} cr**."
                    )
                view, file = self._simple_panel_view(
                    title="Prestige", description=text, accent_color=0xED4245
                )
                return await ctx.send(
                    view=view,
                    file=file,
                    allowed_mentions=discord.AllowedMentions.none(),
                )

            next_cost = result.get("next_cost")
            text = (
                f"{ctx.author.mention} reached **Prestige {int(result['level'])}**.\n"
                f"Permanent bonus: **+{float(result['bonus_rate']) * 100:.0f}%** on perked payouts.\n"
                f"Pets removed: **{int(result.get('pets_removed') or 0)}**.\n"
            )
            if next_cost:
                text += f"Next prestige cost: **{int(next_cost):,} cr**."
            else:
                text += "You are now at max prestige."
            view, file = self._simple_panel_view(
                title="Prestige Upgraded", description=text, accent_color=0xF1C40F
            )
            return await ctx.send(
                view=view, file=file, allowed_mentions=discord.AllowedMentions.none()
            )

        status = await asyncio.to_thread(
            self.store.prestige_status, ctx.guild.id, ctx.author.id
        )
        if status["maxed"]:
            next_line = "You are at max prestige."
        else:
            next_line = (
                f"Next cost: **{int(status['next_cost']):,} cr**\n"
                f"Wallet + bank: **{int(status['total']):,} cr**\n"
                "Run `.prestige confirm` to buy the next level."
            )
        description = (
            f"Level: **{int(status['level'])}/{PRESTIGE_MAX_LEVEL}**\n"
            f"Current prestige bonus: **+{float(status['bonus_rate']) * 100:.0f}%**\n\n"
            f"{next_line}"
        )
        view, file = self._simple_panel_view(
            title=f"{ctx.author.display_name}'s Prestige",
            description=description,
            accent_color=0xF1C40F,
        )
        await ctx.send(
            view=view, file=file, allowed_mentions=discord.AllowedMentions.none()
        )

    @commands.command(
        name="perks",
        aliases=["perk", "bonuses", "bonus"],
        help="Show your active prestige, booster, pet, and shop-role bonuses.",
    )
    async def perks_cmd(
        self, ctx: commands.Context, member: Optional[discord.Member] = None
    ):
        if ctx.guild is None:
            return await ctx.send("Use this in a server.")
        target = member or ctx.author
        
        buffs = await asyncio.to_thread(
            self.store.get_all_active_buffs, ctx.guild.id, target.id
        )
        
        summary_lines = ["**Overall Payout Bonuses**"]
        for stat in buffs["payout_stats"]:
            sources = [
                f"pets +{float(stat['pet_rate']) * 100:.0f}%" if float(stat["pet_rate"]) > 0 else "",
                f"roles +{float(stat['role_rate']) * 100:.0f}%" if float(stat["role_rate"]) > 0 else "",
                f"prestige +{float(stat['prestige_rate']) * 100:.0f}%" if float(stat["prestige_rate"]) > 0 else "",
                f"booster +{float(stat['booster_rate']) * 100:.0f}%" if float(stat["booster_rate"]) > 0 else "",
            ]
            source_text = ", ".join(source for source in sources if source) or "no active sources"
            summary_lines.append(
                f"- **{stat['label']}:** +{float(stat['total_rate']) * 100:.0f}% ({source_text})"
            )

        full_lines = [
            "**Global Modifiers**",
            f"- Prestige {buffs['prestige_level']}/{PRESTIGE_MAX_LEVEL} (+{float(buffs['prestige_rate']) * 100:.0f}% payout bonus)",
            f"- Server Booster (+{float(buffs['booster_rate']) * 100:.0f}% payout bonus)"
            if buffs["is_booster"] else "- Server Booster (Inactive)",
            "\n**Shop Role Perks**",
        ]
        if buffs["roles"]:
            full_lines.extend(
                f"- **{role['name']}**: {role['label']}" for role in buffs["roles"]
            )
        else:
            full_lines.append("- No active shop roles.")
        full_lines.append("\n**Pet Perks**")
        if buffs["pets"]:
            full_lines.extend(
                f"- **{pet['name']}**: {' | '.join(pet['perks'])}"
                for pet in buffs["pets"]
            )
        else:
            full_lines.append("- No active pet perks.")
        full_lines.append("\n**Items & Effects**")
        full_lines.extend(
            f"- **{gear['name']}** ({gear['task']}): Luck +{float(gear['luck']) * 100:.0f}%, cooldown -{float(gear['cooldown']) * 100:.0f}%"
            for gear in buffs["gear"]
        )
        full_lines.extend(
            f"- **{str(effect['effect_key']).replace('_', ' ').title()}** (+{float(effect['rate']) * 100:.0f}%)"
            for effect in buffs["effects"]
        )
        if not buffs["gear"] and not buffs["effects"]:
            full_lines.append("- No active item buffs.")

        def make_panel(description: str) -> discord.ui.LayoutView:
            view = discord.ui.LayoutView(timeout=180)
            view.add_item(
                branded_panel_container(
                    title=f"{target.display_name}'s Active Perks",
                    description=description,
                    accent_color=0x9B59B6,
                    min_width_chars=None,
                )
            )
            return ensure_layout_view_action_rows(view)

        full_view = make_panel("\n".join(full_lines))
        summary_view = make_panel("\n".join(summary_lines))
        button = discord.ui.Button(label="View Full Stats", style=discord.ButtonStyle.secondary)

        async def show_full_stats(interaction: discord.Interaction) -> None:
            await interaction.response.edit_message(view=full_view)

        button.callback = show_full_stats
        summary_view.children[0].add_item(
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small)
        )
        summary_view.children[0].add_item(discord.ui.ActionRow(button))
        await ctx.send(
            view=ensure_layout_view_action_rows(summary_view),
            allowed_mentions=discord.AllowedMentions.none(),
        )

