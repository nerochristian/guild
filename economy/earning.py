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
from .core import _discord_timestamp


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
KRAKEN_FISHING_DROP_RATE = 0.00002
INVESTMENT_MARKET_WINDOW_SECONDS = 300
INVESTMENT_LUCK_RETURN_WEIGHT = 0.45
INVESTMENT_MARKET_RETURN_WEIGHT = 0.35
INVESTMENT_MIN_MULTIPLIER = 0.35
INVESTMENT_MAX_MULTIPLIER = 2.50
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




def _investment_timestamp(value) -> int:
    if value is None:
        return int(datetime.now(timezone.utc).timestamp())
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp())

def _investment_amount(raw_amount: str) -> int | None:
    cleaned = (raw_amount or "").strip().lower().replace(",", "").replace("_", "")
    if not cleaned:
        return None
    multipliers = {
        "k": 1_000,
        "m": 1_000_000,
        "b": 1_000_000_000,
    }
    multiplier = 1
    suffix = cleaned[-1]
    if suffix in multipliers:
        multiplier = multipliers[suffix]
        cleaned = cleaned[:-1]
    if not cleaned:
        return None
    try:
        amount = int(round(float(cleaned) * multiplier))
    except (OverflowError, ValueError):
        return None
    return amount if amount >= 0 else None

def _investment_market_score(guild_id: int, slot: int) -> float:
    phase = (int(guild_id) % 997) / 997 * math.tau
    wave = math.sin(slot * 0.91 + phase) * 0.58
    pulse = math.sin(slot * 0.23 + phase * 1.7) * 0.30
    chop = math.sin(slot * 2.17 + phase * 0.4) * 0.12
    return max(-1.0, min(1.0, wave + pulse + chop))

def _investment_market_snapshot(
    guild_id: int, reference: Optional[datetime] = None
) -> dict[str, Any]:
    now = _as_utc(reference) or datetime.now(timezone.utc)
    slot = int(now.timestamp() // INVESTMENT_MARKET_WINDOW_SECONDS)
    scores = [_investment_market_score(guild_id, slot - offset) for offset in range(11, -1, -1)]
    current = scores[-1]
    previous = scores[-2] if len(scores) > 1 else current
    delta = current - previous
    if current >= 0.45:
        label = "Surging"
        direction = "up hard"
        color = 0x2ECC71
    elif current >= 0.12:
        label = "Climbing"
        direction = "up"
        color = 0x57F287
    elif current <= -0.45:
        label = "Crashing"
        direction = "down hard"
        color = 0xED4245
    elif current <= -0.12:
        label = "Dipping"
        direction = "down"
        color = 0xFEE75C
    else:
        label = "Choppy"
        direction = "sideways"
        color = 0x3498DB
    glyphs = "▁▂▃▄▅▆▇█"
    sparkline = "".join(
        glyphs[min(len(glyphs) - 1, max(0, int(round((score + 1) / 2 * (len(glyphs) - 1)))))]
        for score in scores
    )
    next_shift = datetime.fromtimestamp(
        (slot + 1) * INVESTMENT_MARKET_WINDOW_SECONDS, tz=timezone.utc
    )
    return {
        "score": current,
        "delta": delta,
        "label": label,
        "direction": direction,
        "sparkline": sparkline,
        "next_shift": next_shift,
        "color": color,
    }

class EarningStoreMixin:
    def get_investment_market(self, guild_id: int) -> dict[str, Any]:
        return _investment_market_snapshot(guild_id)

    def _investment_luck_rate(self, guild_id: int, user_id: int) -> float:
        total = 0.0
        if hasattr(self, "get_pet_luck_bonus"):
            total += float(self.get_pet_luck_bonus(guild_id, user_id) or 0)
        if hasattr(self, "get_effect_rate"):
            total += float(self.get_effect_rate(guild_id, user_id, "luck") or 0)
        return max(0.0, min(0.80, total))

    def _investment_multiplier(self, guild_id: int, user_id: int) -> tuple[float, dict[str, Any], float]:
        market = self.get_investment_market(guild_id)
        luck_rate = self._investment_luck_rate(guild_id, user_id)
        base = random.uniform(0.5, 2.0)
        market_bonus = float(market["score"]) * INVESTMENT_MARKET_RETURN_WEIGHT
        luck_bonus = random.random() * luck_rate * INVESTMENT_LUCK_RETURN_WEIGHT
        multiplier = max(
            INVESTMENT_MIN_MULTIPLIER,
            min(INVESTMENT_MAX_MULTIPLIER, base + market_bonus + luck_bonus),
        )
        return multiplier, market, luck_rate

    def create_investment(
        self, guild_id: int, user_id: int, amount: int, duration_minutes: int = 20
    ) -> dict[str, Any]:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT id, amount, matures_at
                        FROM member_investments
                        WHERE guild_id = %s AND user_id = %s AND status = 'active'
                        ORDER BY created_at DESC
                        LIMIT 1
                        """,
                        (guild_id, user_id),
                    )
                    active = cursor.fetchone()
                    if active:
                        return {
                            "ok": False,
                            "reason": "active",
                            "investment": dict(active),
                        }

                    cursor.execute(
                        "SELECT credits FROM member_economy WHERE guild_id = %s AND user_id = %s FOR UPDATE",
                        (guild_id, user_id),
                    )
                    balance_row = cursor.fetchone()
                    balance = int(balance_row["credits"]) if balance_row else 0
                    if balance < amount:
                        return {
                            "ok": False,
                            "reason": "insufficient",
                            "balance": balance,
                        }

                    cursor.execute(
                        """
                        UPDATE member_economy
                        SET credits = credits - %s,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE guild_id = %s AND user_id = %s
                        """,
                        (amount, guild_id, user_id),
                    )
                    cursor.execute(
                        """
                        INSERT INTO member_investments (guild_id, user_id, amount, matures_at)
                        VALUES (%s, %s, %s, CURRENT_TIMESTAMP + (%s * INTERVAL '1 minute'))
                        RETURNING id, guild_id, user_id, amount, return_amount, multiplier, status, created_at, matures_at, settled_at
                        """,
                        (guild_id, user_id, amount, duration_minutes),
                    )
                    return {"ok": True, "investment": dict(cursor.fetchone())}
        finally:
            self._pool.putconn(conn)

    def settle_due_investments(
        self,
        guild_id: int,
        user_id: Optional[int] = None,
        investment_id: Optional[int] = None,
        force: bool = False,
    ) -> list[dict[str, Any]]:
        conn = self._connect()
        settled: list[dict[str, Any]] = []
        try:
            with conn:
                with conn.cursor() as cursor:
                    filters = [
                        "guild_id = %s",
                        "status = 'active'",
                    ]
                    if not force:
                        filters.append("matures_at <= CURRENT_TIMESTAMP")
                    params: list[Any] = [guild_id]
                    if user_id is not None:
                        filters.append("user_id = %s")
                        params.append(user_id)
                    if investment_id is not None:
                        filters.append("id = %s")
                        params.append(investment_id)

                    cursor.execute(
                        f"""
                        SELECT id, guild_id, user_id, amount, created_at, matures_at
                        FROM member_investments
                        WHERE {" AND ".join(filters)}
                        ORDER BY matures_at ASC
                        FOR UPDATE
                        """,
                        tuple(params),
                    )
                    rows = [dict(row) for row in cursor.fetchall()]
                    for row in rows:
                        amount = int(row["amount"])
                        multiplier, market, luck_rate = self._investment_multiplier(
                            guild_id, int(row["user_id"])
                        )
                        return_amount = max(0, int(amount * multiplier))
                        status = "won" if return_amount >= amount else "lost"
                        cursor.execute(
                            """
                            UPDATE member_investments
                            SET status = %s,
                                multiplier = %s,
                                return_amount = %s,
                                matures_at = CASE
                                    WHEN %s THEN CURRENT_TIMESTAMP
                                    ELSE matures_at
                                END,
                                settled_at = CURRENT_TIMESTAMP
                            WHERE id = %s
                            RETURNING id, guild_id, user_id, amount, return_amount, multiplier, status, created_at, matures_at, settled_at
                            """,
                            (status, multiplier, return_amount, bool(force), row["id"]),
                        )
                        settled_row = dict(cursor.fetchone())
                        settled_row["market"] = market
                        settled_row["luck_rate"] = luck_rate
                        cursor.execute(
                            """
                            INSERT INTO member_economy (guild_id, user_id, credits, updated_at)
                            VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
                            ON CONFLICT (guild_id, user_id) DO UPDATE SET
                                credits = member_economy.credits + EXCLUDED.credits,
                                updated_at = CURRENT_TIMESTAMP
                            """,
                            (guild_id, int(row["user_id"]), return_amount),
                        )
                        settled.append(settled_row)
            return settled
        finally:
            self._pool.putconn(conn)

    def settle_all_due_investments(self) -> list[dict]:
        conn = self._connect()
        settled = []
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT id, guild_id, user_id, amount, created_at, matures_at
                        FROM member_investments
                        WHERE status = 'active' AND matures_at <= CURRENT_TIMESTAMP
                        FOR UPDATE
                        """
                    )
                    rows = [dict(row) for row in cursor.fetchall()]
                    for row in rows:
                        amount = int(row["amount"])
                        multiplier, market, luck_rate = self._investment_multiplier(
                            int(row["guild_id"]), int(row["user_id"])
                        )
                        return_amount = max(0, int(amount * multiplier))
                        status = "won" if return_amount >= amount else "lost"
                        cursor.execute(
                            """
                            UPDATE member_investments
                            SET status = %s, multiplier = %s, return_amount = %s, settled_at = CURRENT_TIMESTAMP
                            WHERE id = %s
                            RETURNING id, guild_id, user_id, amount, return_amount, multiplier, status, created_at, matures_at, settled_at
                            """,
                            (status, multiplier, return_amount, row["id"]),
                        )
                        settled_row = dict(cursor.fetchone())
                        settled_row["market"] = market
                        settled_row["luck_rate"] = luck_rate
                        cursor.execute(
                            """
                            INSERT INTO member_economy (guild_id, user_id, credits, updated_at)
                            VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
                            ON CONFLICT (guild_id, user_id) DO UPDATE SET
                                credits = member_economy.credits + EXCLUDED.credits,
                                updated_at = CURRENT_TIMESTAMP
                            """,
                            (int(row["guild_id"]), int(row["user_id"]), return_amount),
                        )
                        settled.append(settled_row)
            return settled
        finally:
            self._pool.putconn(conn)

    def get_user_investments(
        self, guild_id: int, user_id: int, limit: int = 5
    ) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, guild_id, user_id, amount, return_amount, multiplier, status, created_at, matures_at, settled_at
                    FROM member_investments
                    WHERE guild_id = %s AND user_id = %s
                    ORDER BY
                        CASE WHEN status = 'active' THEN 0 ELSE 1 END,
                        COALESCE(settled_at, matures_at, created_at) DESC
                    LIMIT %s
                    """,
                    (guild_id, user_id, limit),
                )
                return [dict(row) for row in cursor.fetchall()]
        finally:
            self._pool.putconn(conn)

    def cancel_active_investments(
        self, guild_id: int, user_id: int, refund: bool = False
    ) -> list[dict[str, Any]]:
        conn = self._connect()
        cancelled: list[dict[str, Any]] = []
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT id, guild_id, user_id, amount, created_at, matures_at
                        FROM member_investments
                        WHERE guild_id = %s AND user_id = %s AND status = 'active'
                        ORDER BY created_at ASC
                        FOR UPDATE
                        """,
                        (guild_id, user_id),
                    )
                    rows = [dict(row) for row in cursor.fetchall()]
                    for row in rows:
                        return_amount = int(row["amount"]) if refund else 0
                        cursor.execute(
                            """
                            UPDATE member_investments
                            SET status = 'cancelled',
                                multiplier = 0,
                                return_amount = %s,
                                settled_at = CURRENT_TIMESTAMP
                            WHERE id = %s
                            RETURNING id, guild_id, user_id, amount, return_amount, multiplier, status, created_at, matures_at, settled_at
                            """,
                            (return_amount, row["id"]),
                        )
                        cancelled_row = dict(cursor.fetchone())
                        if refund and return_amount > 0:
                            cursor.execute(
                                """
                                INSERT INTO member_economy (guild_id, user_id, credits, updated_at)
                                VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
                                ON CONFLICT (guild_id, user_id) DO UPDATE SET
                                    credits = member_economy.credits + EXCLUDED.credits,
                                    updated_at = CURRENT_TIMESTAMP
                                """,
                                (guild_id, user_id, return_amount),
                            )
                        cancelled.append(cancelled_row)
            return cancelled
        finally:
            self._pool.putconn(conn)

class EarningCog(commands.Cog):
    async def cog_load(self):
        self._investments_task = asyncio.create_task(self.poll_investments())

    def cog_unload(self):
        if hasattr(self, "_investments_task"):
            self._investments_task.cancel()

    async def _investment_market_embed(self, ctx: commands.Context) -> discord.Embed:
        market = await asyncio.to_thread(self.store.get_investment_market, ctx.guild.id)
        score = float(market["score"])
        bias = score * INVESTMENT_MARKET_RETURN_WEIGHT
        embed = self.create_embed(
            "Investment Market",
            (
                f"Market is **{market['label']}** ({market['direction']}).\n"
                f"`{market['sparkline']}`\n"
                f"Return pressure: **{bias:+.0%}** before personal luck.\n"
                f"Next market shift: <t:{_discord_timestamp(market['next_shift'])}:R>"
            ),
            color=int(market["color"]),
        )
        return embed

    @commands.command(
        name="stocks",
        aliases=["stock", "market", "stonks", "investmarket"],
        help="Show whether the investment market is trending up or down.",
    )
    async def stocks(self, ctx: commands.Context):
        await self._send(ctx, await self._investment_market_embed(ctx))

    @commands.command(
        name="ecoadmin_investments",
        aliases=["ecoadmin_invests", "ecoadmin_investstatus"],
        help="Show a member's active and recent investments.",
    )
    @economy_admin_only()
    async def ecoadmin_investments(self, ctx: commands.Context, member: discord.Member):
        rows = await asyncio.to_thread(
            self.store.get_user_investments, ctx.guild.id, member.id, 10
        )
        if not rows:
            return await ctx.send(
                f"{member.mention} has no investment history.",
                allowed_mentions=discord.AllowedMentions.none(),
            )

        active_lines: list[str] = []
        history_lines: list[str] = []
        for row in rows:
            amount = int(row["amount"])
            status = str(row["status"])
            if status == "active":
                active_lines.append(
                    f"#{row['id']} - **{amount:,} cr** - matures <t:{_discord_timestamp(row['matures_at'])}:R>"
                )
                continue

            return_amount = int(row.get("return_amount") or 0)
            delta = return_amount - amount
            history_lines.append(
                f"#{row['id']} - **{status.title()}** - returned **{return_amount:,} cr** "
                f"({delta:+,} cr) - <t:{_discord_timestamp(row.get('settled_at'))}:R>"
            )

        embed = discord.Embed(
            title=f"Investments - {member.display_name}", color=0x3498DB
        )
        embed.add_field(
            name="Active",
            value="\n".join(active_lines) if active_lines else "No active investments.",
            inline=False,
        )
        embed.add_field(
            name="Recent",
            value="\n".join(history_lines)
            if history_lines
            else "No completed investments.",
            inline=False,
        )
        await send_v2(ctx, embed)

    @commands.command(
        name="ecoadmin_cancel_invest",
        aliases=["ecoadmin_cancelinvest", "ecoadmin_cinvest"],
        help="Cancel a member's active investment with optional principal refund.",
    )
    @economy_admin_only()
    async def ecoadmin_cancel_invest(
        self, ctx: commands.Context, member: discord.Member, refund: str = ""
    ):
        should_refund = refund.strip().lower() in {"refund", "yes", "true", "1"}
        cancelled = await asyncio.to_thread(
            self.store.cancel_active_investments, ctx.guild.id, member.id, should_refund
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

    @commands.command(name="mines", aliases=["minesweeper"], help="💣 Play mines")
    async def mines_cmd(
        self, ctx: commands.Context, amount: str = "", *, difficulty: str = "easy"
    ):
        if not amount:
            return await ctx.send("Usage: `.mines <amount> [easy|medium|hard]`")
        difficulty = (difficulty or "easy").strip().lower()
        configs = {
            "easy": {"size": 9, "mines": 2, "picks": 3, "mult": 1.55},
            "medium": {"size": 16, "mines": 5, "picks": 4, "mult": 2.25},
            "hard": {"size": 25, "mines": 10, "picks": 5, "mult": 3.60},
        }
        if difficulty not in configs:
            return await ctx.send("❌ Difficulty must be `easy`, `medium`, or `hard`.")

        bet = await self._validate_bet(ctx, amount, "mines")
        if bet is None:
            return

        from .casino import MinesView

        view = MinesView(
            self, ctx.guild.id, ctx.author.id, bet, difficulty, configs[difficulty]
        )
        view.render()
        msg = await ctx.send(view=view)
        view.message = msg
        view._save_state()

    async def poll_investments(self):
        try:
            await self.bot.wait_until_ready()
        except RuntimeError:
            return
        while not self.bot.is_closed():
            await asyncio.sleep(60)
            try:
                if not self.store:
                    continue
                settled = await asyncio.to_thread(self.store.settle_all_due_investments)
                for investment in settled:
                    try:
                        user = self.bot.get_user(investment["user_id"])
                        if not user:
                            continue
                        amount = investment["amount"]
                        return_amt = investment["return_amount"]
                        multiplier = investment["multiplier"]
                        if multiplier >= 1.0:
                            profit = return_amt - amount
                            msg = f"📈 Your investment of **{amount:,} cr** matured! You earned a profit of **{profit:,} cr** (Total: **{return_amt:,} cr**)."
                            color = 0x2ECC71
                        else:
                            loss = amount - return_amt
                            msg = f"📉 Your investment of **{amount:,} cr** tanked. You lost **{loss:,} cr** (Total: **{return_amt:,} cr**)."
                            color = 0xED4245
                        embed = self.create_embed(
                            "Investment Matured", msg, color=color
                        )
                        await user.send(embed=embed)
                    except Exception as dm_error:
                        LOGGER.warning(
                            "Failed to DM investment result to %s: %s",
                            investment.get("user_id"),
                            dm_error,
                        )
            except Exception as e:
                LOGGER.error(f"Error polling investments: {e}")

    @commands.command(
        name="daily",
        help="Claim your daily coin reward (resets every 24 hours). Scales with level!",
    )
    async def daily(self, ctx: commands.Context):
        await self._check_cooldown(ctx, 86400)
        level = await asyncio.to_thread(
            self.store.get_user_level, ctx.guild.id, ctx.author.id
        )
        base_reward = random.randint(500, 1000)
        bonus = (level - 1) * 50
        pet_result = await self._pet_bonus_result(
            ctx.guild.id, ctx.author.id, base_reward + bonus, "daily"
        )
        reward = int(pet_result["total"])
        if not await self._add_credits_or_reset_cooldown(
            ctx, ctx.guild.id, ctx.author.id, reward, "Daily Reward"
        ):
            return
        embed = self.create_embed(
            "📅 Daily Reward",
            f"You claimed your daily reward of **{reward:,} cr**! (Level Bonus: **+{bonus} cr**)",
            color=0x2ECC71,
        )
        await self._send(ctx, embed)

    @commands.command(
        name="work",
        help="Work a job for a random coin payout (1 hour cooldown). Scales with level!",
    )
    async def work(self, ctx: commands.Context):
        await self._check_cooldown(ctx, 3600)
        level = await asyncio.to_thread(
            self.store.get_user_level, ctx.guild.id, ctx.author.id
        )
        jobs = [
            "developer",
            "janitor",
            "chef",
            "hacker",
            "youtuber",
            "streamer",
            "pizza delivery driver",
            "clown",
        ]
        base_reward = random.randint(100, 300)
        bonus = int(base_reward * (level * 0.02))  # 2% extra per level
        pet_result = await self._pet_bonus_result(
            ctx.guild.id, ctx.author.id, base_reward + bonus, "work"
        )
        reward = int(pet_result["total"])
        if not await self._add_credits_or_reset_cooldown(
            ctx, ctx.guild.id, ctx.author.id, reward, "Work"
        ):
            return
        embed = self.create_embed(
            "💼 Work",
            f"You worked as a **{random.choice(jobs)}** and earned **{reward:,} cr**! (Level Bonus: **+{bonus} cr**)",
            color=0x3498DB,
        )
        await self._send(ctx, embed)

    @commands.command(
        name="beg",
        help="Beg for coins — random chance of getting a small amount (3 minute cooldown).",
    )
    async def beg(self, ctx: commands.Context):
        await self._check_cooldown(ctx, 180)
        luck_rate = await self._active_luck_rate(ctx.guild.id, ctx.author.id, "beg")
        success_chance = min(0.85, 0.6 + luck_rate * 0.25)
        luck_helped = success_chance > 0.6
        if random.random() < success_chance:
            reward = random.randint(10, 50)
            if not await self._add_credits_or_reset_cooldown(
                ctx, ctx.guild.id, ctx.author.id, reward, "Begging"
            ):
                return
            embed = self.create_embed(
                "🙏 Begging",
                f"Someone felt bad and gave you **{reward:,} cr**.",
                color=0x2ECC71,
            )
            if luck_helped:
                embed.description = (embed.description or "") + "\nLuck made begging more likely to work."
        else:
            embed = self.create_embed(
                "🙏 Begging", "Nobody gave you anything. Get a job!", color=0xED4245
            )
        await self._send(ctx, embed)

    @commands.command(name="fish", help="Go fishing for coins (5 minute cooldown).")
    async def fish(self, ctx: commands.Context):
        await self._check_cooldown(ctx, 300)
        luck_rate = await self._active_luck_rate(ctx.guild.id, ctx.author.id, "fish")
        luck_triggered = await self._luck_triggers(ctx.guild.id, ctx.author.id, "fish")
        catch = random.choice(
            ["🐟", "🐠", "🐡", "🦈", "🐙", "🦑", "🦐", "🦀", "🦞", "👢"]
        )
        if catch == "👢" and luck_triggered:
            catch = random.choice(["🐟", "🐠", "🐡", "🦐", "🦀"])
        loot_key = None
        loot_roll = random.random()
        if loot_roll < 0.01 + luck_rate * 0.04:
            loot_key = "drill"
        elif loot_roll < 0.04 + luck_rate * 0.08:
            loot_key = "advanced_lockpick"
        elif loot_roll < 0.12 + luck_rate * 0.12:
            loot_key = "lockpick"
        if catch == "👢":
            embed = self.create_embed(
                "🎣 Fishing",
                "You went fishing and caught an old boot. Worth 0 cr.",
                color=0x95A5A6,
            )
        else:
            reward = random.randint(20, 80)
            if catch == "🦈":
                reward += 100
            pet_result = await self._pet_bonus_result(
                ctx.guild.id, ctx.author.id, reward, "fish"
            )
            reward = int(pet_result["total"])
            if not await self._add_credits_or_reset_cooldown(
                ctx, ctx.guild.id, ctx.author.id, reward, "Fishing"
            ):
                return
            embed = self.create_embed(
                "🎣 Fishing",
                f"You went fishing and caught a {catch}! You sold it for **{reward:,} cr**.",
                color=0x3498DB,
            )
        if loot_key:
            result = await asyncio.to_thread(
                self.store.grant_economy_item,
                ctx.guild.id,
                ctx.author.id,
                loot_key,
                1,
            )
            if result.get("ok"):
                item_name = ECONOMY_ITEM_DEFS[loot_key]["name"]
                embed.description = (embed.description or "") + f"\nYou also fished up **1x {item_name}**."
        kraken_drop_rate = KRAKEN_FISHING_DROP_RATE * (1 + luck_rate * 5)
        if random.random() < kraken_drop_rate:
            result = await asyncio.to_thread(
                self.store.grant_pet,
                ctx.guild.id,
                ctx.author.id,
                "kraken",
                1,
            )
            if result.get("ok"):
                pet_id = result.get("pet", {}).get("id", "?")
                embed.color = 0x00D5FF
                embed.description = (
                    (embed.description or "")
                    + f"\n\n**ABYSSAL JACKPOT!** You hooked a **Kraken Spawn** pet. "
                    f"Base odds: **0.002%**"
                    + (f" | Luck-adjusted: **{kraken_drop_rate * 100:.4f}%**" if luck_rate > 0 else "")
                    + f". Pet ID: `#{pet_id}`."
                )
        await self._send(ctx, embed)

    @commands.command(name="hunt", help="Go hunting for coins (10 minute cooldown).")
    async def hunt(self, ctx: commands.Context):
        await self._check_cooldown(ctx, 600)
        luck_triggered = await self._luck_triggers(ctx.guild.id, ctx.author.id, "hunt")
        catch = random.choice(["🐰", "🦊", "🐻", "🐗", "🦌", "🦆", "🪨"])
        if catch == "🪨" and luck_triggered:
            catch = random.choice(["🐰", "🦊", "🐗", "🦆"])
        if catch == "🪨":
            embed = self.create_embed(
                "🏹 Hunting",
                "You went hunting and only found a rock. Worth 0 cr.",
                color=0x95A5A6,
            )
        else:
            reward = random.randint(30, 100)
            if catch in ["🐻", "🦌"]:
                reward += 50
            if luck_triggered:
                reward = int(reward * 1.15)
            pet_result = await self._pet_bonus_result(
                ctx.guild.id, ctx.author.id, reward, "hunt"
            )
            reward = int(pet_result["total"])
            if not await self._add_credits_or_reset_cooldown(
                ctx, ctx.guild.id, ctx.author.id, reward, "Hunting"
            ):
                return
            embed = self.create_embed(
                "🏹 Hunting",
                f"You went hunting and caught a {catch}! You sold it for **{reward:,} cr**.",
                color=0xE67E22,
            )
            if luck_triggered:
                embed.description = (embed.description or "") + "\nLuck improved the hunt."
        await self._send(ctx, embed)

    @commands.command(name="mine", help="Mine for coins (8 minute cooldown).")
    async def mine(self, ctx: commands.Context):
        await self._check_cooldown(ctx, 480)
        luck_triggered = await self._luck_triggers(ctx.guild.id, ctx.author.id, "mine")
        ore = random.choice(["🪨", "⛏️", "💎", "🪙", "🌑"])
        if ore in ["🪨", "🌑", "⛏️"] and luck_triggered:
            ore = random.choice(["🪙", "💎"])
        if ore in ["🪨", "🌑", "⛏️"]:
            embed = self.create_embed(
                "⛏️ Mining",
                "You went mining and found nothing but dirt.",
                color=0x95A5A6,
            )
        else:
            reward = random.randint(40, 120)
            if ore == "💎":
                reward += 150
            if luck_triggered:
                reward = int(reward * 1.15)
            if not await self._add_credits_or_reset_cooldown(
                ctx, ctx.guild.id, ctx.author.id, reward, "Mining"
            ):
                return
            embed = self.create_embed(
                "⛏️ Mining",
                f"You went mining and found {ore}! You sold it for **{reward:,} cr**.",
                color=0xF1C40F,
            )
            if luck_triggered:
                embed.description = (embed.description or "") + "\nLuck improved the mine."
        await self._send(ctx, embed)

    @commands.command(name="invest", help="Invest coins for a return after 20 minutes.")
    async def invest(self, ctx: commands.Context, amount: str = ""):
        store = self.store
        if store is None:
            embed = self.create_embed(
                "Investment",
                "Economy storage is not ready yet. Try again in a moment.",
                color=0xED4245,
            )
            return await self._send(ctx, embed)

        if not amount:
            embed = self.create_embed(
                "Invest", "You need to specify an amount to invest.", color=0xED4245
            )
            return await self._send(ctx, embed)

        if amount.lower() in ["all", "max"]:
            parsed_amount = await asyncio.to_thread(
                store.get_balance, ctx.guild.id, ctx.author.id
            )
        else:
            parsed_amount = _investment_amount(amount)
            if parsed_amount is None:
                embed = self.create_embed(
                    "Invest", "Please provide a valid number.", color=0xED4245
                )
                return await self._send(ctx, embed)

        if parsed_amount < 50:
            embed = self.create_embed(
                "Invest", "You must invest at least 50 cr.", color=0xED4245
            )
            return await self._send(ctx, embed)

        await self._ensure_cooldown_ready(ctx, 10800)

        try:
            await asyncio.to_thread(
                store.settle_due_investments, ctx.guild.id, ctx.author.id
            )
            result = await asyncio.to_thread(
                store.create_investment, ctx.guild.id, ctx.author.id, parsed_amount, 20
            )
        except Exception:
            LOGGER.exception(
                "Failed to create investment for guild=%s user=%s",
                ctx.guild.id,
                ctx.author.id,
            )
            embed = self.create_embed(
                "Investment",
                "I could not save that investment. No credits were invested.",
                color=0xED4245,
            )
            return await self._send(ctx, embed)

        if not result.get("ok"):
            if result.get("reason") == "active":
                active = result["investment"]
                ready_at = _investment_timestamp(active["matures_at"])
                embed = self.create_embed(
                    "Investment Active",
                    f"You already have an active investment of **{int(active['amount']):,} cr**.\n"
                    f"Status: **Growing**\n"
                    f"Matures: <t:{ready_at}:R>",
                    color=0xED4245,
                )
                return await self._send(ctx, embed)

            balance = int(result.get("balance", 0))
            embed = self.create_embed(
                "Investment", f"You only have **{balance:,} cr**.", color=0xED4245
            )
            return await self._send(ctx, embed)

        investment = result["investment"]
        investment_id = int(investment["id"])
        try:
            saved_rows = await asyncio.to_thread(
                store.get_user_investments, ctx.guild.id, ctx.author.id, 10
            )
        except Exception:
            LOGGER.exception(
                "Investment was inserted but could not be verified for guild=%s user=%s id=%s",
                ctx.guild.id,
                ctx.author.id,
                investment_id,
            )
            saved_rows = []

        if not any(int(row["id"]) == investment_id for row in saved_rows):
            LOGGER.error(
                "Investment insert was not visible after create: guild=%s user=%s id=%s",
                ctx.guild.id,
                ctx.author.id,
                investment_id,
            )
            embed = self.create_embed(
                "Investment",
                "I could not verify that investment saved. Check `.investment` before trying again.",
                color=0xED4245,
            )
            return await self._send(ctx, embed)

        await self._start_cooldown(ctx, 10800)
        ready_at = _investment_timestamp(investment["matures_at"])
        market = await asyncio.to_thread(store.get_investment_market, ctx.guild.id)
        embed = self.create_embed(
            "Investment Placed",
            f"You invested **{parsed_amount:,} cr**.\n"
            f"Status: **Growing**\n"
            f"Return ready: <t:{ready_at}:R>\n"
            f"Market: **{market['label']}** ({market['direction']}) `{market['sparkline']}`",
            color=0x2ECC71,
        )
        await self._send(ctx, embed)

    @commands.command(
        name="investment",
        aliases=["investments", "investstatus"],
        help="View your active and recent investments.",
    )
    async def investment(self, ctx: commands.Context):
        store = self.store
        if store is None:
            embed = self.create_embed(
                "Investments",
                "Economy storage is not ready yet. Try again in a moment.",
                color=0xED4245,
            )
            return await self._send(ctx, embed)

        try:
            await asyncio.to_thread(
                store.settle_due_investments, ctx.guild.id, ctx.author.id
            )
            rows = await asyncio.to_thread(
                store.get_user_investments, ctx.guild.id, ctx.author.id, 6
            )
        except Exception:
            LOGGER.exception(
                "Failed to load investments for guild=%s user=%s",
                ctx.guild.id,
                ctx.author.id,
            )
            embed = self.create_embed(
                "Investments",
                "I could not load your investments right now.",
                color=0xED4245,
            )
            return await self._send(ctx, embed)

        if not rows:
            embed = self.create_embed(
                "Investments",
                "You do not have any investments yet. Start one with `.invest <amount>`.",
                color=0x95A5A6,
            )
            return await self._send(ctx, embed)

        active_lines = []
        history_lines = []
        now = datetime.now(timezone.utc).timestamp()
        for row in rows:
            amount = int(row["amount"])
            status = str(row["status"])
            matures_at = _investment_timestamp(row["matures_at"])
            if status == "active":
                label = (
                    "Ready to settle"
                    if matures_at <= now
                    else f"Matures <t:{matures_at}:R>"
                )
                active_lines.append(f"**#{row['id']}** - **{amount:,} cr** - {label}")
                continue

            return_amount = int(row.get("return_amount") or 0)
            delta = return_amount - amount
            outcome = "Profit" if delta >= 0 else "Loss"
            settled_at = _investment_timestamp(row.get("settled_at"))
            history_lines.append(
                f"**#{row['id']}** - {status.title()} - **{return_amount:,} cr** returned "
                f"({outcome}: **{delta:+,} cr**) - <t:{settled_at}:R>"
            )

        embed = self.create_embed(
            "Investments", "Your active and recent investment status.", color=0x3498DB
        )
        market = await asyncio.to_thread(store.get_investment_market, ctx.guild.id)
        embed.add_field(
            name="Market",
            value=(
                f"**{market['label']}** ({market['direction']})\n"
                f"`{market['sparkline']}`\n"
                f"Next shift <t:{_discord_timestamp(market['next_shift'])}:R>"
            ),
            inline=False,
        )
        embed.add_field(
            name="Active",
            value="\n".join(active_lines) if active_lines else "No active investment.",
            inline=False,
        )
        embed.add_field(
            name="Recent",
            value="\n".join(history_lines)
            if history_lines
            else "No completed investments yet.",
            inline=False,
        )
        await self._send(ctx, embed)

