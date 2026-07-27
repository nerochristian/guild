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
from .cache import ttl_cache
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
from .core import _discord_timestamp, _parse_credit_amount, _progress_bar


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
PET_LIST_PAGE_SIZE = 5
PET_MAX_LEGENDARY_OR_FUSED = 3
PET_FEED_COSTS = {1: 4_000, 2: 6_000, 3: 8_000, 4: 10_500, 5: 12_500}
PET_ACTIVE_BONUS_CAP = 0.25
PET_GAMBLING_BONUS_CAP = 0.22
PET_PASSIVE_BONUS_CAP = 0.15
PET_LUCK_BONUS_CAP = 0.80
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




def _pet_stage(pet_key: str, stage: int) -> dict[str, Any]:
    if pet_key not in PET_LINES:
        return {}
    stage = int(stage)
    if stage < 1:
        raise ValueError("Stage must be >= 1")
    return PET_LINES[pet_key]["stages"][min(5, stage) - 1]

def _pet_display_name(row: dict[str, Any]) -> str:
    if row.get("is_fused") and row.get("custom_name"):
        return str(row["custom_name"])
    if row.get("is_fused"):
        data = row.get("fusion_data")
        if isinstance(data, dict) and data.get("name"):
            return str(data["name"])
        parents = row.get("fusion_parents")
        if isinstance(parents, dict):
            primary = parents.get("primary_key")
            secondary = parents.get("secondary_key")
            if primary and secondary:
                key = frozenset((primary, secondary))
                if key in PET_HYBRIDS:
                    return PET_HYBRIDS[key]["name"]
    return _pet_stage(str(row["pet_key"]), int(row["stage"]))["name"]

def _pet_rarity(row: dict[str, Any]) -> str:
    if row.get("is_fused"):
        return "Hybrid"
    return _pet_stage(str(row["pet_key"]), int(row["stage"]))["rarity"]

def _pet_hourly_income(row: dict[str, Any]) -> int:
    if row.get("is_fused") and row.get("fusion_data"):
        data = row["fusion_data"]
        if isinstance(data, dict):
            return int(data.get("daily_income", 0))
    return int(_pet_stage(str(row["pet_key"]), int(row["stage"]))["daily_income"])

def _pet_bonuses(row: dict[str, Any]) -> list[tuple[str, float]]:
    bonuses = []
    if row.get("is_fused") and row.get("fusion_data"):
        data = row["fusion_data"]
        if isinstance(data, dict):
            for b in data.get("bonuses", []):
                if b.get("type") and float(b.get("value") or 0) > 0:
                    bonuses.append((str(b["type"]), float(b["value"])))
            if not bonuses:
                btype = data.get("bonus_type")
                bval = float(data.get("bonus_value") or 0)
                if btype and bval > 0:
                    bonuses.append((str(btype), bval))
            return bonuses
    data = _pet_stage(str(row["pet_key"]), int(row["stage"]))
    btype = data.get("bonus_type")
    bval = float(data.get("bonus_value") or 0)
    if btype and bval > 0:
        bonuses.append((str(btype), bval))
    return bonuses

def _format_pet_percent(value: float) -> str:
    percent = float(value) * 100
    if abs(percent - round(percent)) < 0.05:
        return f"{int(round(percent))}%"
    return f"{percent:.1f}%"

def _pet_bonus_type_label(bonus_type: Optional[str]) -> str:
    labels = {
        "daily": "Daily rewards",
        "work": "Work rewards",
        "work_fish": "Work and fishing rewards",
        "fish_hunt": "Fishing and hunting rewards",
        "crime": "Crime rewards",
        "gambling": "Casino profits",
        "gambling_luck": "Casino luck",
        "gambling_crime": "Casino and crime rewards",
        "active": "Task rewards",
        "passive": "Passive pet income",
        "all": "All perked payouts",
    }
    return labels.get(str(bonus_type or ""), str(bonus_type or "Bonus").replace("_", " ").title())

def _pet_perk_lines_from_data(data: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    
    for b in data.get("bonuses", []):
        if b.get("type") and float(b.get("value") or 0) > 0:
            lines.append(f"{_pet_bonus_type_label(str(b['type']))} +{_format_pet_percent(float(b['value']))}")
            
    if not data.get("bonuses"):
        bonus_type = data.get("bonus_type")
        bonus_value = float(data.get("bonus_value") or 0)
        if bonus_type and bonus_value > 0:
            lines.append(f"{_pet_bonus_type_label(str(bonus_type))} +{_format_pet_percent(bonus_value)}")

    luck_bonus = float(data.get("luck_bonus") or 0)
    if luck_bonus > 0:
        lines.append(f"Luck Boost +{_format_pet_percent(luck_bonus)}")

    cooldown_reduction = float(data.get("cooldown_reduction") or 0)
    if cooldown_reduction > 0:
        lines.append(f"Task cooldowns -{_format_pet_percent(cooldown_reduction)}")

    gamble_multiplier = float(data.get("gamble_multiplier") or 0)
    if gamble_multiplier > 0:
        lines.append(f"Winning casino payouts x{1.0 + gamble_multiplier:.2f}")

    return lines

def _pet_perk_summary_from_data(data: dict[str, Any]) -> str:
    return " | ".join(_pet_perk_lines_from_data(data)) or "No special perk"

def _pet_perk_summary(row: dict[str, Any]) -> str:
    if row.get("is_fused") and row.get("fusion_data"):
        data = row["fusion_data"]
        if isinstance(data, dict):
            return _pet_perk_summary_from_data(data)
    return _pet_perk_summary_from_data(
        _pet_stage(str(row["pet_key"]), int(row["stage"]))
    )

def _pet_image_key(row_or_key: Any) -> str:
    if isinstance(row_or_key, dict):
        if row_or_key.get("is_fused") and row_or_key.get("fusion_parents"):
            data = row_or_key.get("fusion_data")
            if isinstance(data, dict) and data.get("fusion_image_key"):
                return str(data["fusion_image_key"])
            parents = row_or_key.get("fusion_parents")
            if isinstance(parents, dict):
                primary_key = str(parents.get("primary_key") or "")
                secondary_key = str(parents.get("secondary_key") or "")
                if primary_key and secondary_key:
                    tier = str(parents.get("fusion_tier") or "perfect")
                    first_key, second_key = sorted((primary_key, secondary_key))
                    return f"fusion_{first_key}_{second_key}_{tier}"
                pet_key = str(parents.get("primary_key") or row_or_key.get("pet_key") or "catalog")
                stage = int(row_or_key.get("stage", 1))
                return f"{pet_key}_{stage}" if pet_key != "catalog" else "catalog"
        pet_key = str(row_or_key.get("pet_key") or "catalog")
        stage = int(row_or_key.get("stage", 1))
        return f"{pet_key}_{stage}" if pet_key != "catalog" else "catalog"
    return str(row_or_key or "catalog")

def _pet_image_path(image_key: str) -> Optional[Path]:
    normalized = str(image_key or "catalog").strip().lower()
    if normalized.startswith("fusion_"):
        for tier in ("lesser", "greater", "perfect"):
            if normalized.endswith(f"_{tier}"):
                tier_path = PET_ASSET_DIR / "fusions" / tier / f"{normalized}.png"
                if tier_path.exists():
                    return tier_path
        fusion_path = PET_ASSET_DIR / "fusions" / f"{normalized}.png"
        if fusion_path.exists():
            return fusion_path

    pet_key = normalized
    stage = 1
    if "_" in normalized:
        maybe_key, maybe_stage = normalized.rsplit("_", 1)
        if maybe_key in PET_SPRITE_PREFIXES and maybe_stage.isdigit():
            pet_key = maybe_key
            stage = max(1, min(5, int(maybe_stage)))
    elif normalized in PET_SPRITE_PREFIXES:
        stage = 5

    sprite_prefix = PET_SPRITE_PREFIXES.get(pet_key)
    if sprite_prefix:
        sprite_path = PET_SPRITE_DIR / f"{sprite_prefix}_stage{stage}.png"
        if sprite_path.exists():
            return sprite_path

    legacy_filename = PET_LEGACY_IMAGE_FILES.get(normalized)
    if legacy_filename:
        legacy_path = PET_ASSET_DIR / legacy_filename
        if legacy_path.exists():
            return legacy_path

    catalog_path = PET_ASSET_DIR / PET_LEGACY_IMAGE_FILES["catalog"]
    return catalog_path if catalog_path.exists() else None

def _pet_image_attachment(
    row_or_key: Any,
) -> tuple[Optional[discord.File], Optional[str]]:
    image_key = _pet_image_key(row_or_key)
    path = _pet_image_path(image_key)
    if path is None:
        return None, None
    filename = f"pet-{image_key}-{path.name}".replace("/", "-").replace("\\", "-")
    return discord.File(path, filename=filename), f"attachment://{filename}"

def _pet_happiness_modifier(happiness: int) -> float:
    happiness = max(0, min(100, int(happiness)))
    if happiness >= 90:
        return 0.15
    if happiness >= 70:
        return 0.05
    if happiness >= 50:
        return 0.0
    if happiness >= 30:
        return -0.15
    return -0.35

def _pet_mood(happiness: int) -> str:
    happiness = max(0, min(100, int(happiness)))
    if happiness >= 90:
        return "Glowing"
    if happiness >= 70:
        return "Content"
    if happiness >= 50:
        return "Neutral"
    if happiness >= 30:
        return "Unhappy"
    return "Miserable"

def _normalize_pet_lookup(raw_name: str) -> str:
    return " ".join((raw_name or "").strip().casefold().replace("#", "").split())

def _match_pet_line(raw_name: str) -> Optional[str]:
    cleaned = _normalize_pet_lookup(raw_name)
    if not cleaned:
        return None
    for key, line in PET_LINES.items():
        names = {key, line["family"].casefold()}
        names.update(stage["name"].casefold() for stage in line["stages"])
        if cleaned in names:
            return key
    return None

def _match_pet_item(raw_item: str) -> Optional[str]:
    cleaned = _normalize_pet_lookup(raw_item)
    for key, data in PET_ITEM_DEFS.items():
        if cleaned == data["name"].casefold() or cleaned in data["aliases"]:
            return key
    return None

def _format_pet_core_stock(stock: int, daily_stock: int) -> str:
    if int(daily_stock) >= PET_INFINITE_STOCK_THRESHOLD:
        return "∞/∞"
    return f"{int(stock):,}/{int(daily_stock):,}"

def _pet_hour_start(reference: Optional[datetime] = None) -> datetime:
    now = _as_utc(reference) or datetime.now(timezone.utc)
    return now.replace(minute=0, second=0, microsecond=0)

class PetShopView(discord.ui.LayoutView):
    def __init__(
        self,
        cog: Any,
        ctx: commands.Context,
        *,
        mode: str = "pets",
        femboy_mode: bool = False,
    ):
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx
        self.guild_id = int(ctx.guild.id)
        self.user_id = int(ctx.author.id)
        self.mode = mode if mode in {"pets", "mats"} else "pets"
        self.femboy_mode = femboy_mode
        self.files: list[discord.File] = []
        self._render_pets()

    def _toggle_button(self) -> discord.ui.Button:
        if self.mode == "pets":
            button = discord.ui.Button(
                label="Buy Mats UwU" if self.femboy_mode else "Buy Mats",
                style=discord.ButtonStyle.primary,
            )
        else:
            button = discord.ui.Button(
                label="Buy Pets UwU" if self.femboy_mode else "Buy Pets",
                style=discord.ButtonStyle.secondary,
            )
        button.callback = self._toggle_callback
        return button

    def _header_section(self, title: str, description: str) -> discord.ui.Section:
        return discord.ui.Section(
            discord.ui.TextDisplay(f"**{title}**\n{description}"),
            accessory=self._toggle_button(),
        )

    def _replace_container(
        self, children: list[discord.ui.Item[Any]], accent_color: int
    ) -> None:
        self.clear_items()
        self.add_item(discord.ui.Container(*children, accent_color=accent_color))

    def _render_pets(self) -> None:
        self.mode = "pets"
        files: list[discord.File] = []
        attached_filenames: set[str] = set()
        children: list[discord.ui.Item[Any]] = [
            self._header_section(
                "Da Soul Pet Shop UwU" if self.femboy_mode else "The Soul Pet Shop",
                (
                    "Adopt a wittwe stawter pet with `.buypet <line>`. Pets earn passive cwedities and unwock cute bonuses as they evowve~"
                    if self.femboy_mode
                    else "Buy a starter pet with `.buypet <line>`. Pets earn passive credits and unlock bonuses as they evolve."
                ),
            ),
            discord.ui.Separator(),
        ]

        for pet_key, line in PET_LINES.items():
            if not line.get("available_in_shop", True):
                continue
            text = f"**{self.cog._pet_line_field_name(pet_key)}**\n{self.cog._pet_line_summary(pet_key)}"
            self.cog._add_pet_thumbnail_section(
                children, files, attached_filenames, text, f"{pet_key}_1"
            )

        children.append(discord.ui.Separator())
        children.append(
            discord.ui.TextDisplay(
                (
                    "*Use .pet evolution <line> to peek at aww da cute stages~*"
                    if self.femboy_mode
                    else "*Use .pet evolution <line> to preview all stages.*"
                )
            )
        )
        self.files = files
        self._replace_container(children, 0x9B59B6)

    async def _render_mats(self) -> None:
        self.mode = "mats"
        shop, items = await asyncio.gather(
            asyncio.to_thread(self.cog.store.ensure_pet_core_shop, self.guild_id),
            asyncio.to_thread(
                self.cog.store.get_pet_items, self.guild_id, self.user_id
            ),
        )
        children: list[discord.ui.Item[Any]] = [
            self._header_section(
                "Soul Pet Matewiaws UwU" if self.femboy_mode else "Soul Pet Materials",
                (
                    "Buy shiny fusion matewiaws for safe fusion and hybwid fusion, cutie~"
                    if self.femboy_mode
                    else "Buy fusion materials for safe fusion and hybrid fusion."
                ),
            ),
            discord.ui.Separator(),
        ]

        for row in shop:
            item_name = PET_ITEM_DEFS[row["item_key"]]["name"]
            stock = int(row["stock"])
            daily_stock = int(row["daily_stock"])
            price = int(row["price"])
            item_key = str(row["item_key"])
            stock_label = _format_pet_core_stock(stock, daily_stock)
            children.append(
                discord.ui.TextDisplay(
                    f"**{item_name}**\n"
                    f"{'Pwice' if self.femboy_mode else 'Price'} **{price:,} cr** | "
                    f"{'Stockies' if self.femboy_mode else 'Stock'} **{stock_label}**\n"
                    f"{'Buyy' if self.femboy_mode else 'Buy'}: `.pet cores buy {item_key} 1`"
                )
            )

        owned = ", ".join(
            f"{PET_ITEM_DEFS[key]['name']}: **{qty:,}**"
            for key, qty in sorted(items.items())
            if qty > 0 and key in PET_ITEM_DEFS
        )
        children.append(discord.ui.Separator())
        children.append(
            discord.ui.TextDisplay(
                (
                    f"**Youw Matewiaws**\n{owned or 'No pet matewiaws yet, cutie~'}"
                    if self.femboy_mode
                    else f"**Your Materials**\n{owned or 'No pet materials yet.'}"
                )
            )
        )
        children.append(discord.ui.Separator())
        children.append(
            discord.ui.TextDisplay(
                (
                    "*Use .pet cores buy <fusion_core|greater_fusion_core> [amount] to buy some, UwU~*"
                    if self.femboy_mode
                    else "*Use .pet cores buy <fusion_core|greater_fusion_core> [amount] to purchase.*"
                )
            )
        )
        self._replace_container(children, 0xE67E22)

    async def _toggle_callback(self, interaction: discord.Interaction) -> None:
        if self.mode == "pets":
            await self._render_mats()
        else:
            self._render_pets()
        await interaction.response.edit_message(view=self)


class PetListView(discord.ui.LayoutView):
    def __init__(
        self,
        cog: Any,
        ctx: commands.Context,
        rows: list[dict[str, Any]],
        items: dict[str, int],
        *,
        page: int = 1,
    ):
        super().__init__(timeout=180)
        self.cog = cog
        self.user_id = int(ctx.author.id)
        self.display_name = ctx.author.display_name
        self.rows = rows
        self.items = items
        self.page = max(1, min(self.total_pages, int(page or 1)))
        self.files: list[discord.File] = []
        self.render()

    @property
    def total_pages(self) -> int:
        return max(1, math.ceil(len(self.rows) / PET_LIST_PAGE_SIZE))

    def _page_controls(self) -> discord.ui.ActionRow:
        prev_button = discord.ui.Button(
            label="<",
            style=discord.ButtonStyle.secondary,
            disabled=self.page <= 1,
        )
        page_button = discord.ui.Button(
            label=f"Page {self.page}/{self.total_pages}",
            style=discord.ButtonStyle.secondary,
            disabled=True,
        )
        next_button = discord.ui.Button(
            label=">",
            style=discord.ButtonStyle.secondary,
            disabled=self.page >= self.total_pages,
        )

        async def prev_callback(interaction: discord.Interaction) -> None:
            self.page = max(1, self.page - 1)
            self.render()
            await interaction.response.edit_message(view=self, attachments=self.files)

        async def next_callback(interaction: discord.Interaction) -> None:
            self.page = min(self.total_pages, self.page + 1)
            self.render()
            await interaction.response.edit_message(view=self, attachments=self.files)

        prev_button.callback = prev_callback
        next_button.callback = next_callback
        return discord.ui.ActionRow(prev_button, page_button, next_button)

    def render(self) -> None:
        self.clear_items()
        self.files = []
        children: list[discord.ui.Item[Any]] = []
        header = f"**{self.display_name}'s Pets**"
        if not self.rows:
            header += "\n\nYou do not own any pets yet. Use `.petshop` to browse starter lines."
            children.append(discord.ui.TextDisplay(header))
        else:
            start = (self.page - 1) * PET_LIST_PAGE_SIZE
            page_rows = self.rows[start : start + PET_LIST_PAGE_SIZE]
            header += (
                f"\nPage **{self.page}/{self.total_pages}** | "
                f"Showing **{start + 1}-{start + len(page_rows)}** of **{len(self.rows)}**"
            )
            children.append(self._page_controls())
            children.append(discord.ui.TextDisplay(header))
            children.append(discord.ui.Separator())
            page_files: list[discord.File] = []
            attached_filenames: set[str] = set()
            for pet in page_rows:
                text = self.cog._pet_brief(pet)
                self.cog._add_pet_thumbnail_section(
                    children, page_files, attached_filenames, text, pet
                )
            self.files = page_files

        children.append(discord.ui.Separator())
        item_line = ", ".join(
            f"{PET_ITEM_DEFS[key]['name']}: **{qty:,}**"
            for key, qty in sorted(self.items.items())
            if qty > 0 and key in PET_ITEM_DEFS
        )
        children.append(
            discord.ui.TextDisplay(
                f"**Materials**\n{item_line or 'No pet materials yet.'}"
            )
        )
        children.append(discord.ui.Separator())
        children.append(
            discord.ui.TextDisplay(
                "*Use pet IDs like #12 for feed, evolve, and fusion commands.*"
            )
        )

        self.add_item(discord.ui.Container(*children, accent_color=0x9B59B6))
        ensure_layout_view_action_rows(self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "This pet list is not yours.", ephemeral=True
            )
            return False
        return True

class PetsStoreMixin:
    def _pet_rows(
        self, cursor, guild_id: int, user_id: int, *, for_update: bool = False
    ) -> list[dict[str, Any]]:
        suffix = " FOR UPDATE" if for_update else ""
        cursor.execute(
            f"""
            SELECT id, guild_id, user_id, pet_key, stage, custom_name, is_fused, fusion_data, fusion_parents,
                   purchased_at, stage_started_at, last_collected, last_cared_at, last_played_at,
                   happiness, total_earned
            FROM user_pets
            WHERE guild_id = %s AND user_id = %s
            ORDER BY id ASC
            {suffix}
            """,
            (guild_id, user_id),
        )
        rows = [dict(row) for row in cursor.fetchall()]
        for row in rows:
            fusion_data = row.get("fusion_data")
            fusion_parents = row.get("fusion_parents")
            if (
                not row.get("is_fused")
                or not isinstance(fusion_data, dict)
                or fusion_data.get("perk_model") == "combination_v3"
                or not isinstance(fusion_parents, dict)
            ):
                continue

            first_key = str(fusion_parents.get("primary_key") or "")
            second_key = str(fusion_parents.get("secondary_key") or "")
            parent_stages = fusion_data.get("parent_stages")
            if not isinstance(parent_stages, list) or len(parent_stages) != 2:
                parent_stages = [
                    int(fusion_data.get("fusion_stage") or row["stage"]),
                    int(fusion_data.get("fusion_stage") or row["stage"]),
                ]
            try:
                upgraded_data = self._hybrid_fusion_data(
                    first_key,
                    second_key,
                    int(parent_stages[0]),
                    int(parent_stages[1]),
                )
            except (KeyError, TypeError, ValueError):
                continue

            cursor.execute(
                "UPDATE user_pets SET fusion_data = %s WHERE id = %s",
                (psycopg2.extras.Json(upgraded_data), row["id"]),
            )
            row["fusion_data"] = upgraded_data
        return rows

    def _apply_pet_decay(
        self, cursor, rows: list[dict[str, Any]], now: Optional[datetime] = None
    ) -> list[dict[str, Any]]:
        now = _as_utc(now) or datetime.now(timezone.utc)
        for row in rows:
            cared_at = _as_utc(row.get("last_cared_at")) or now
            full_days = max(0, int((now - cared_at).total_seconds() // 86400))
            if full_days <= 0:
                continue
            happiness = max(
                0,
                int(row.get("happiness") or 0)
                - full_days * PET_HAPPINESS_DECAY_PER_DAY,
            )
            cursor.execute(
                "UPDATE user_pets SET happiness = %s, last_cared_at = %s WHERE id = %s",
                (happiness, now, row["id"]),
            )
            row["happiness"] = happiness
            row["last_cared_at"] = now
        return rows

    def _pet_bonus_rate_from_rows(
        self, rows: list[dict[str, Any]], action: str
    ) -> float:
        total = 0.0
        for row in rows:
            for bonus_type, value in _pet_bonuses(row):
                if value > 0 and _bonus_type_applies(bonus_type, action):
                    total += value

        if action == "gambling":
            return min(total, PET_GAMBLING_BONUS_CAP)
        if action == "passive":
            return min(total, PET_PASSIVE_BONUS_CAP)
        return min(total, PET_ACTIVE_BONUS_CAP)

    @ttl_cache(ttl=60)
    def get_pet_bonus_rate(self, guild_id: int, user_id: int, action: str) -> float:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    rows = self._pet_rows(cursor, guild_id, user_id, for_update=True)
                    rows = self._apply_pet_decay(cursor, rows)
                    return self._pet_bonus_rate_from_rows(rows, action)
        finally:
            self._pool.putconn(conn)

    @ttl_cache(ttl=60)
    def get_pet_gamble_multiplier(self, guild_id: int, user_id: int) -> float:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    rows = self._pet_rows(cursor, guild_id, user_id)
                    rows = self._apply_pet_decay(cursor, rows)
                    total = 0.0
                    for row in rows:
                        if row.get("is_fused") and row.get("fusion_data"):
                            data = row["fusion_data"]
                            if isinstance(data, dict):
                                total += float(data.get("gamble_multiplier", 0))
                        else:
                            stage_data = _pet_stage(
                                str(row["pet_key"]), int(row["stage"])
                            )
                            total += float(stage_data.get("gamble_multiplier", 0))
                    return min(total, 0.50)
        finally:
            self._pool.putconn(conn)

    @ttl_cache(ttl=60)
    def get_pet_luck_bonus(self, guild_id: int, user_id: int) -> float:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    rows = self._pet_rows(cursor, guild_id, user_id)
                    rows = self._apply_pet_decay(cursor, rows)
                    total = 0.0
                    for row in rows:
                        if row.get("is_fused") and row.get("fusion_data"):
                            data = row["fusion_data"]
                            if isinstance(data, dict):
                                total += float(data.get("luck_bonus", 0))
                        else:
                            stage_data = _pet_stage(
                                str(row["pet_key"]), int(row["stage"])
                            )
                            total += float(stage_data.get("luck_bonus", 0))
                    return min(total, PET_LUCK_BONUS_CAP)
        finally:
            self._pool.putconn(conn)

    @ttl_cache(ttl=60)
    def get_pet_cooldown_reduction(self, guild_id: int, user_id: int) -> float:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    rows = self._pet_rows(cursor, guild_id, user_id)
                    rows = self._apply_pet_decay(cursor, rows)
                    total = 0.0
                    for row in rows:
                        if row.get("is_fused") and row.get("fusion_data"):
                            data = row["fusion_data"]
                            if isinstance(data, dict):
                                total += float(data.get("cooldown_reduction", 0))
                        else:
                            stage_data = _pet_stage(
                                str(row["pet_key"]), int(row["stage"])
                            )
                            total += float(stage_data.get("cooldown_reduction", 0))
                    return min(total, 0.40)
        finally:
            self._pool.putconn(conn)

    def apply_pet_bonus(
        self, guild_id: int, user_id: int, amount: int, action: str
    ) -> dict[str, Any]:
        return self.apply_total_bonus(guild_id, user_id, amount, action)

    def get_user_pets(self, guild_id: int, user_id: int) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    rows = self._pet_rows(cursor, guild_id, user_id, for_update=True)
                    return self._apply_pet_decay(cursor, rows)
        finally:
            self._pool.putconn(conn)

    def get_pet_items(self, guild_id: int, user_id: int) -> dict[str, int]:
        conn = self._connect()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT item_key, quantity FROM pet_items WHERE guild_id = %s AND user_id = %s",
                    (guild_id, user_id),
                )
                return {
                    row["item_key"]: int(row["quantity"]) for row in cursor.fetchall()
                }
        finally:
            self._pool.putconn(conn)

    def grant_pet_item(
        self, guild_id: int, user_id: int, item_key: str, amount: int
    ) -> dict[str, Any]:
        amount = int(amount)
        if item_key not in PET_ITEM_DEFS:
            return {"ok": False, "reason": "invalid_item"}
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO pet_items (guild_id, user_id, item_key, quantity, updated_at)
                        VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
                        ON CONFLICT (guild_id, user_id, item_key) DO UPDATE SET
                            quantity = GREATEST(0, pet_items.quantity + EXCLUDED.quantity),
                            updated_at = CURRENT_TIMESTAMP
                        RETURNING quantity
                        """,
                        (guild_id, user_id, item_key, amount),
                    )
                    return {
                        "ok": True,
                        "item_key": item_key,
                        "quantity": int(cursor.fetchone()[0]),
                    }
        finally:
            self._pool.putconn(conn)

    def reset_member_pets(self, guild_id: int, user_id: int) -> dict[str, Any]:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "DELETE FROM user_pets WHERE guild_id = %s AND user_id = %s",
                        (guild_id, user_id),
                    )
                    pet_count = cursor.rowcount
                    cursor.execute(
                        "DELETE FROM pet_items WHERE guild_id = %s AND user_id = %s",
                        (guild_id, user_id),
                    )
                    item_count = cursor.rowcount
                    return {"ok": True, "pets": pet_count, "items": item_count}
        finally:
            self._pool.putconn(conn)

    def buy_pet(self, guild_id: int, user_id: int, pet_key: str) -> dict[str, Any]:
        return self.buy_pets(guild_id, user_id, pet_key, 1)

    def buy_pets(
        self, guild_id: int, user_id: int, pet_key: str, amount: int
    ) -> dict[str, Any]:
        if pet_key not in PET_LINES:
            return {"ok": False, "reason": "invalid_pet"}
        if not PET_LINES[pet_key].get("available_in_shop", True):
            return {"ok": False, "reason": "not_for_sale"}
        amount = int(amount)
        if amount < 1:
            return {"ok": False, "reason": "invalid_amount"}
        price_each = int(PET_LINES[pet_key]["stages"][0]["price"])
        price = price_each * amount
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT COUNT(*) FROM user_pets WHERE guild_id = %s AND user_id = %s",
                        (guild_id, user_id),
                    )
                    owned_count = int(cursor.fetchone()[0])
                    if owned_count + amount > PET_MAX_OWNED:
                        return {
                            "ok": False,
                            "reason": "pet_limit",
                            "limit": PET_MAX_OWNED,
                            "available": max(0, PET_MAX_OWNED - owned_count),
                        }

                    cursor.execute(
                        "SELECT credits FROM member_economy WHERE guild_id = %s AND user_id = %s FOR UPDATE",
                        (guild_id, user_id),
                    )
                    row = cursor.fetchone()
                    balance = int(row["credits"]) if row else 0
                    if balance < price:
                        return {
                            "ok": False,
                            "reason": "insufficient",
                            "balance": balance,
                            "price": price,
                        }

                    cursor.execute(
                        """
                        UPDATE member_economy
                        SET credits = credits - %s, updated_at = CURRENT_TIMESTAMP
                        WHERE guild_id = %s AND user_id = %s
                        """,
                        (price, guild_id, user_id),
                    )
                    pets = []
                    for _ in range(amount):
                        cursor.execute(
                            """
                            INSERT INTO user_pets (guild_id, user_id, pet_key, stage, happiness)
                            VALUES (%s, %s, %s, 1, 80)
                            RETURNING id, guild_id, user_id, pet_key, stage, custom_name, is_fused, fusion_data,
                                      fusion_parents, purchased_at, stage_started_at, last_collected,
                                      last_cared_at, last_played_at, happiness, total_earned
                            """,
                            (guild_id, user_id, pet_key),
                        )
                        pets.append(dict(cursor.fetchone()))
                    return {
                        "ok": True,
                        "pet": pets[0],
                        "pets": pets,
                        "amount": amount,
                        "price": price,
                        "price_each": price_each,
                        "balance": balance - price,
                    }
        finally:
            self._pool.putconn(conn)

    def grant_pet(
        self, guild_id: int, user_id: int, pet_key: str, stage: int = 1
    ) -> dict[str, Any]:
        if pet_key not in PET_LINES:
            return {"ok": False, "reason": "invalid_pet"}
        stage = max(1, min(5, int(stage)))
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO user_pets (guild_id, user_id, pet_key, stage, happiness)
                        VALUES (%s, %s, %s, %s, 90)
                        RETURNING id, guild_id, user_id, pet_key, stage, custom_name, is_fused, fusion_data,
                                  fusion_parents, purchased_at, stage_started_at, last_collected,
                                  last_cared_at, last_played_at, happiness, total_earned
                        """,
                        (guild_id, user_id, pet_key, stage),
                    )
                    return {"ok": True, "pet": dict(cursor.fetchone())}
        finally:
            self._pool.putconn(conn)

    def resolve_user_pet(
        self, guild_id: int, user_id: int, raw_ref: str
    ) -> dict[str, Any]:
        ref = (raw_ref or "").strip()
        cleaned = _normalize_pet_lookup(ref)
        pets = self.get_user_pets(guild_id, user_id)
        if cleaned.isdigit():
            pet_id = int(cleaned)
            match = next((pet for pet in pets if int(pet["id"]) == pet_id), None)
            return {
                "ok": bool(match),
                "pet": match,
                "reason": None if match else "not_found",
            }

        matches = [
            pet
            for pet in pets
            if _normalize_pet_lookup(_pet_display_name(pet)) == cleaned
        ]
        if len(matches) == 1:
            return {"ok": True, "pet": matches[0]}
        if len(matches) > 1:
            return {"ok": False, "reason": "ambiguous", "matches": matches}
        return {"ok": False, "reason": "not_found"}

    def feed_pet(self, guild_id: int, user_id: int, pet_id: int) -> dict[str, Any]:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT * FROM user_pets WHERE id = %s AND guild_id = %s AND user_id = %s FOR UPDATE",
                        (pet_id, guild_id, user_id),
                    )
                    row = cursor.fetchone()
                    if not row:
                        return {"ok": False, "reason": "not_found"}
                    pet = self._apply_pet_decay(cursor, [dict(row)])[0]
                    cost = PET_FEED_COSTS.get(int(pet["stage"]), 25_000)
                    cursor.execute(
                        "SELECT credits FROM member_economy WHERE guild_id = %s AND user_id = %s FOR UPDATE",
                        (guild_id, user_id),
                    )
                    balance_row = cursor.fetchone()
                    balance = int(balance_row["credits"]) if balance_row else 0
                    if balance < cost:
                        return {
                            "ok": False,
                            "reason": "insufficient",
                            "cost": cost,
                            "balance": balance,
                        }
                    happiness = min(100, int(pet["happiness"]) + 25)
                    cursor.execute(
                        "UPDATE member_economy SET credits = credits - %s, updated_at = CURRENT_TIMESTAMP WHERE guild_id = %s AND user_id = %s",
                        (cost, guild_id, user_id),
                    )
                    cursor.execute(
                        "UPDATE user_pets SET happiness = %s, last_cared_at = CURRENT_TIMESTAMP WHERE id = %s RETURNING *",
                        (happiness, pet_id),
                    )
                    return {
                        "ok": True,
                        "pet": dict(cursor.fetchone()),
                        "cost": cost,
                        "happiness": happiness,
                    }
        finally:
            self._pool.putconn(conn)

    def play_with_pet(self, guild_id: int, user_id: int, pet_id: int) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT * FROM user_pets WHERE id = %s AND guild_id = %s AND user_id = %s FOR UPDATE",
                        (pet_id, guild_id, user_id),
                    )
                    row = cursor.fetchone()
                    if not row:
                        return {"ok": False, "reason": "not_found"}
                    pet = self._apply_pet_decay(cursor, [dict(row)], now)[0]
                    last_played = _as_utc(pet.get("last_played_at"))
                    if last_played and now - last_played < PET_PLAY_COOLDOWN:
                        ready_at = last_played + PET_PLAY_COOLDOWN
                        return {"ok": False, "reason": "cooldown", "ready_at": ready_at}
                    happiness = min(100, int(pet["happiness"]) + 10)
                    cursor.execute(
                        """
                        UPDATE user_pets
                        SET happiness = %s, last_played_at = %s, last_cared_at = %s
                        WHERE id = %s
                        RETURNING *
                        """,
                        (happiness, now, now, pet_id),
                    )
                    return {
                        "ok": True,
                        "pet": dict(cursor.fetchone()),
                        "happiness": happiness,
                    }
        finally:
            self._pool.putconn(conn)

    def collect_pet_income(self, guild_id: int, user_id: int) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    rows = self._pet_rows(cursor, guild_id, user_id, for_update=True)
                    if not rows:
                        return {"ok": False, "reason": "no_pets"}
                    rows = self._apply_pet_decay(cursor, rows, now)
                    passive_bonus = self._pet_bonus_rate_from_rows(rows, "passive")
                    self._ensure_member_perks(cursor, guild_id, user_id)
                    cursor.execute(
                        "SELECT prestige_level, is_booster FROM member_perks WHERE guild_id = %s AND user_id = %s",
                        (guild_id, user_id),
                    )
                    perks = dict(cursor.fetchone())
                    passive_bonus += (
                        min(int(perks.get("prestige_level") or 0), PRESTIGE_MAX_LEVEL)
                        * PRESTIGE_BONUS_PER_LEVEL
                    )
                    if bool(perks.get("is_booster")):
                        passive_bonus += BOOSTER_BONUS_RATE
                    cursor.execute(
                        "SELECT role_name FROM member_purchases WHERE guild_id = %s AND user_id = %s",
                        (guild_id, user_id),
                    )
                    role_bonus = 0.0
                    for role_row in cursor.fetchall():
                        role_perk = SHOP_ROLE_PERKS.get(str(role_row["role_name"]))
                        if role_perk and _bonus_type_applies(
                            str(role_perk.get("bonus_type")), "passive"
                        ):
                            role_bonus += float(role_perk.get("bonus_value") or 0.0)
                    passive_bonus = min(
                        passive_bonus + min(role_bonus, ROLE_BONUS_CAP),
                        TOTAL_BONUS_CAPS["passive"],
                    )
                    collected: list[dict[str, Any]] = []
                    total = 0
                    for pet in rows:
                        last_collected = _as_utc(pet.get("last_collected")) or now
                        hours = min(
                            PET_MAX_ACCRUAL_HOURS,
                            max(0, int((now - last_collected).total_seconds() // 3600)),
                        )
                        if hours <= 0:
                            collected.append({"pet": pet, "amount": 0, "hours": 0})
                            continue
                        hourly_income = _pet_hourly_income(pet)
                        modifier = (
                            1
                            + _pet_happiness_modifier(int(pet["happiness"]))
                            + passive_bonus
                        )
                        amount = max(0, int(hourly_income * hours * modifier))
                        total += amount
                        cursor.execute(
                            """
                            UPDATE user_pets
                            SET last_collected = %s,
                                total_earned = total_earned + %s
                            WHERE id = %s
                            """,
                            (now, amount, pet["id"]),
                        )
                        collected.append({"pet": pet, "amount": amount, "hours": hours})

                    if total > 0:
                        cursor.execute(
                            """
                            INSERT INTO member_economy (guild_id, user_id, credits, updated_at)
                            VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
                            ON CONFLICT (guild_id, user_id) DO UPDATE SET
                                credits = member_economy.credits + EXCLUDED.credits,
                                updated_at = CURRENT_TIMESTAMP
                            RETURNING credits
                            """,
                            (guild_id, user_id, total),
                        )
                        balance = int(cursor.fetchone()[0])
                    else:
                        cursor.execute(
                            "SELECT credits FROM member_economy WHERE guild_id = %s AND user_id = %s",
                            (guild_id, user_id),
                        )
                        row = cursor.fetchone()
                        balance = int(row["credits"]) if row else 0
                    return {
                        "ok": True,
                        "total": total,
                        "balance": balance,
                        "passive_bonus": passive_bonus,
                        "collected": collected,
                    }
        finally:
            self._pool.putconn(conn)

    def ensure_pet_core_shop(self, guild_id: int) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc)
        hour_start = _pet_hour_start(now)
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    rows: list[dict[str, Any]] = []
                    for item_key, data in PET_CORE_SHOP.items():
                        cursor.execute(
                            "SELECT current_stock, last_refresh FROM pet_shop_state WHERE guild_id = %s AND item_key = %s FOR UPDATE",
                            (guild_id, item_key),
                        )
                        row = cursor.fetchone()
                        if not row or _as_utc(row["last_refresh"]) < hour_start:
                            stock = int(data["daily_stock"])
                            cursor.execute(
                                """
                                INSERT INTO pet_shop_state (guild_id, item_key, current_stock, last_refresh)
                                VALUES (%s, %s, %s, %s)
                                ON CONFLICT (guild_id, item_key) DO UPDATE SET
                                    current_stock = EXCLUDED.current_stock,
                                    last_refresh = EXCLUDED.last_refresh
                                """,
                                (guild_id, item_key, stock, hour_start),
                            )
                        else:
                            stock = int(row["current_stock"])
                        rows.append(
                            {
                                "item_key": item_key,
                                "stock": stock,
                                "price": int(data["price"]),
                                "daily_stock": int(data["daily_stock"]),
                            }
                        )
                    return rows
        finally:
            self._pool.putconn(conn)

    def refresh_pet_core_shop(self, guild_id: int) -> list[dict[str, Any]]:
        """Immediately refill every limited item in the fusion-core shop."""
        refreshed_at = datetime.now(timezone.utc)
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    rows: list[dict[str, Any]] = []
                    for item_key, data in PET_CORE_SHOP.items():
                        stock = int(data["daily_stock"])
                        cursor.execute(
                            """
                            INSERT INTO pet_shop_state (guild_id, item_key, current_stock, last_refresh)
                            VALUES (%s, %s, %s, %s)
                            ON CONFLICT (guild_id, item_key) DO UPDATE SET
                                current_stock = EXCLUDED.current_stock,
                                last_refresh = EXCLUDED.last_refresh
                            """,
                            (guild_id, item_key, stock, refreshed_at),
                        )
                        rows.append(
                            {
                                "item_key": item_key,
                                "stock": stock,
                                "price": int(data["price"]),
                                "daily_stock": stock,
                            }
                        )
                    return rows
        finally:
            self._pool.putconn(conn)

    def set_pet_core_stock(
        self, guild_id: int, item_key: str, stock: int
    ) -> Optional[int]:
        if item_key not in PET_CORE_SHOP:
            return None
        stock = max(0, int(stock))
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO pet_shop_state (guild_id, item_key, current_stock, last_refresh)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (guild_id, item_key) DO UPDATE SET
                            current_stock = EXCLUDED.current_stock,
                            last_refresh = EXCLUDED.last_refresh
                        RETURNING current_stock
                        """,
                        (guild_id, item_key, stock, datetime.now(timezone.utc)),
                    )
                    row = cursor.fetchone()
                    return int(row["current_stock"]) if row else stock
        finally:
            self._pool.putconn(conn)

    def buy_pet_core(
        self, guild_id: int, user_id: int, item_key: str, amount: int
    ) -> dict[str, Any]:
        if item_key not in PET_CORE_SHOP:
            return {"ok": False, "reason": "invalid_item"}
        amount = max(1, int(amount))
        self.ensure_pet_core_shop(guild_id)
        price_each = int(PET_CORE_SHOP[item_key]["price"])
        total_price = price_each * amount
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT current_stock FROM pet_shop_state WHERE guild_id = %s AND item_key = %s FOR UPDATE",
                        (guild_id, item_key),
                    )
                    stock_row = cursor.fetchone()
                    stock = int(stock_row["current_stock"]) if stock_row else 0
                    if stock < amount:
                        return {"ok": False, "reason": "stock", "stock": stock}
                    cursor.execute(
                        "SELECT credits FROM member_economy WHERE guild_id = %s AND user_id = %s FOR UPDATE",
                        (guild_id, user_id),
                    )
                    balance_row = cursor.fetchone()
                    balance = int(balance_row["credits"]) if balance_row else 0
                    if balance < total_price:
                        return {
                            "ok": False,
                            "reason": "insufficient",
                            "balance": balance,
                            "price": total_price,
                        }
                    cursor.execute(
                        "UPDATE pet_shop_state SET current_stock = current_stock - %s WHERE guild_id = %s AND item_key = %s",
                        (amount, guild_id, item_key),
                    )
                    cursor.execute(
                        "UPDATE member_economy SET credits = credits - %s, updated_at = CURRENT_TIMESTAMP WHERE guild_id = %s AND user_id = %s",
                        (total_price, guild_id, user_id),
                    )
                    cursor.execute(
                        """
                        INSERT INTO pet_items (guild_id, user_id, item_key, quantity, updated_at)
                        VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
                        ON CONFLICT (guild_id, user_id, item_key) DO UPDATE SET
                            quantity = pet_items.quantity + EXCLUDED.quantity,
                            updated_at = CURRENT_TIMESTAMP
                        RETURNING quantity
                        """,
                        (guild_id, user_id, item_key, amount),
                    )
                    return {
                        "ok": True,
                        "item_key": item_key,
                        "amount": amount,
                        "price": total_price,
                        "quantity": int(cursor.fetchone()[0]),
                        "stock": stock - amount,
                    }
        finally:
            self._pool.putconn(conn)

    def evolve_pet(self, guild_id: int, user_id: int, pet_id: int) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT * FROM user_pets WHERE id = %s AND guild_id = %s AND user_id = %s FOR UPDATE",
                        (pet_id, guild_id, user_id),
                    )
                    row = cursor.fetchone()
                    if not row:
                        return {"ok": False, "reason": "not_found"}
                    pet = self._apply_pet_decay(cursor, [dict(row)], now)[0]
                    if pet.get("is_fused"):
                        return {"ok": False, "reason": "fused"}
                    stage = int(pet["stage"])
                    if stage >= 5:
                        return {"ok": False, "reason": "max_stage"}
                    target = _pet_stage(str(pet["pet_key"]), stage + 1)
                    stage_started = (
                        _as_utc(pet.get("stage_started_at"))
                        or _as_utc(pet.get("purchased_at"))
                        or now
                    )
                    required_at = stage_started + timedelta(
                        days=int(target["time_days"])
                    )
                    used_crystal = False
                    if now < required_at:
                        cursor.execute(
                            "SELECT quantity FROM pet_items WHERE guild_id = %s AND user_id = %s AND item_key = 'evolve_crystal' FOR UPDATE",
                            (guild_id, user_id),
                        )
                        crystal_row = cursor.fetchone()
                        owned_crystals = int(crystal_row["quantity"]) if crystal_row else 0
                        if owned_crystals > 0:
                            used_crystal = True
                        else:
                            return {"ok": False, "reason": "time", "ready_at": required_at}

                    credits = int(target["evolve_credits"])
                    shards = int(target["evolve_shards"])
                    cursor.execute(
                        "SELECT credits FROM member_economy WHERE guild_id = %s AND user_id = %s FOR UPDATE",
                        (guild_id, user_id),
                    )
                    balance_row = cursor.fetchone()
                    balance = int(balance_row["credits"]) if balance_row else 0
                    if balance < credits:
                        return {
                            "ok": False,
                            "reason": "insufficient",
                            "balance": balance,
                            "price": credits,
                        }
                    cursor.execute(
                        "SELECT quantity FROM pet_items WHERE guild_id = %s AND user_id = %s AND item_key = 'soul_shard' FOR UPDATE",
                        (guild_id, user_id),
                    )
                    shard_row = cursor.fetchone()
                    owned_shards = int(shard_row["quantity"]) if shard_row else 0
                    if owned_shards < shards:
                        return {
                            "ok": False,
                            "reason": "shards",
                            "owned": owned_shards,
                            "required": shards,
                        }
                    if stage + 1 >= 5:
                        cursor.execute(
                            "SELECT COUNT(*) FROM user_pets WHERE guild_id = %s AND user_id = %s AND (is_fused = TRUE OR stage >= 5)",
                            (guild_id, user_id),
                        )
                        if int(cursor.fetchone()[0]) >= PET_MAX_LEGENDARY_OR_FUSED:
                            return {
                                "ok": False,
                                "reason": "legendary_limit",
                                "limit": PET_MAX_LEGENDARY_OR_FUSED,
                            }

                    cursor.execute(
                        "UPDATE member_economy SET credits = credits - %s, updated_at = CURRENT_TIMESTAMP WHERE guild_id = %s AND user_id = %s",
                        (credits, guild_id, user_id),
                    )
                    cursor.execute(
                        "UPDATE pet_items SET quantity = quantity - %s, updated_at = CURRENT_TIMESTAMP WHERE guild_id = %s AND user_id = %s AND item_key = 'soul_shard'",
                        (shards, guild_id, user_id),
                    )
                    if used_crystal:
                        cursor.execute(
                            "UPDATE pet_items SET quantity = quantity - 1, updated_at = CURRENT_TIMESTAMP WHERE guild_id = %s AND user_id = %s AND item_key = 'evolve_crystal'",
                            (guild_id, user_id),
                        )
                    cursor.execute(
                        """
                        UPDATE user_pets
                        SET stage = stage + 1,
                            stage_started_at = %s,
                            happiness = LEAST(100, happiness + 5)
                        WHERE id = %s
                        RETURNING *
                        """,
                        (now, pet_id),
                    )
                    return {
                        "ok": True,
                        "pet": dict(cursor.fetchone()),
                        "credits": credits,
                        "shards": shards,
                        "used_crystal": used_crystal,
                    }
        finally:
            self._pool.putconn(conn)

    def preview_pet_fusion(
        self, guild_id: int, user_id: int, first_id: int, second_id: int
    ) -> dict[str, Any]:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    rows = self._pet_rows(cursor, guild_id, user_id, for_update=True)
                    rows = self._apply_pet_decay(cursor, rows)
                    first = next(
                        (pet for pet in rows if int(pet["id"]) == int(first_id)), None
                    )
                    second = next(
                        (pet for pet in rows if int(pet["id"]) == int(second_id)), None
                    )
                    if not first or not second:
                        return {"ok": False, "reason": "not_found"}
                    req = self._fusion_requirements(first, second)
                    req.update({"first": first, "second": second})
                    return req
        finally:
            self._pool.putconn(conn)

    def fuse_pets(
        self, guild_id: int, user_id: int, first_id: int, second_id: int
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    rows = self._pet_rows(cursor, guild_id, user_id, for_update=True)
                    rows = self._apply_pet_decay(cursor, rows, now)
                    first = next(
                        (pet for pet in rows if int(pet["id"]) == int(first_id)), None
                    )
                    second = next(
                        (pet for pet in rows if int(pet["id"]) == int(second_id)), None
                    )
                    if not first or not second:
                        return {"ok": False, "reason": "not_found"}
                    req = self._fusion_requirements(first, second)
                    if not req.get("ok"):
                        return req
                    req.update({"first": first, "second": second})
                    
                    cursor.execute(
                        "SELECT COUNT(*) FROM user_pets WHERE guild_id = %s AND user_id = %s AND (is_fused = TRUE OR stage >= 5)",
                        (guild_id, user_id),
                    )
                    current_count = int(cursor.fetchone()[0])
                    first_counts = 1 if (first.get("is_fused") or int(first.get("stage", 1)) >= 5) else 0
                    second_counts = 1 if (second.get("is_fused") or int(second.get("stage", 1)) >= 5) else 0
                    
                    if req["type"] == "hybrid":
                        new_count = current_count - first_counts - second_counts + 1
                    else:
                        becomes_legendary = 1 if (int(first.get("stage", 1)) == 4) else 0
                        new_count = current_count - second_counts + becomes_legendary
                        
                    if new_count > PET_MAX_LEGENDARY_OR_FUSED:
                        return {
                            "ok": False,
                            "reason": "legendary_limit",
                            "limit": PET_MAX_LEGENDARY_OR_FUSED,
                        }

                    cursor.execute(
                        "SELECT credits FROM member_economy WHERE guild_id = %s AND user_id = %s FOR UPDATE",
                        (guild_id, user_id),
                    )
                    balance_row = cursor.fetchone()
                    balance = int(balance_row["credits"]) if balance_row else 0
                    if balance < int(req["credits"]):
                        return {
                            "ok": False,
                            "reason": "insufficient",
                            "balance": balance,
                            "price": int(req["credits"]),
                        }

                    item_requirements = {
                        "fusion_core": int(req.get("fusion_core", 0)),
                        "greater_fusion_core": int(req.get("greater_fusion_core", 0)),
                    }
                    for item_key, needed in item_requirements.items():
                        if needed <= 0:
                            continue
                        cursor.execute(
                            "SELECT quantity FROM pet_items WHERE guild_id = %s AND user_id = %s AND item_key = %s FOR UPDATE",
                            (guild_id, user_id, item_key),
                        )
                        item_row = cursor.fetchone()
                        owned = int(item_row["quantity"]) if item_row else 0
                        if owned < needed:
                            return {
                                "ok": False,
                                "reason": "items",
                                "item_key": item_key,
                                "owned": owned,
                                "required": needed,
                            }

                    cursor.execute(
                        "UPDATE member_economy SET credits = credits - %s, updated_at = CURRENT_TIMESTAMP WHERE guild_id = %s AND user_id = %s",
                        (int(req["credits"]), guild_id, user_id),
                    )
                    for item_key, needed in item_requirements.items():
                        if needed > 0:
                            cursor.execute(
                                "UPDATE pet_items SET quantity = quantity - %s, updated_at = CURRENT_TIMESTAMP WHERE guild_id = %s AND user_id = %s AND item_key = %s",
                                (needed, guild_id, user_id, item_key),
                            )

                    if req["type"] == "safe":
                        cursor.execute(
                            "DELETE FROM user_pets WHERE id = %s", (second["id"],)
                        )
                        cursor.execute(
                            """
                            UPDATE user_pets
                            SET stage = stage + 1,
                                stage_started_at = %s,
                                happiness = LEAST(100, happiness + 10)
                            WHERE id = %s
                            RETURNING *
                            """,
                            (now, first["id"]),
                        )
                        result_pet = dict(cursor.fetchone())
                        outcome = "success"
                    else:
                        success = random.random() <= float(req["chance"])
                        if success:
                            hybrid_data = dict(req["hybrid_data"])
                            parents = {
                                "primary": int(first["id"]),
                                "secondary": int(second["id"]),
                                "primary_key": str(first["pet_key"]),
                                "secondary_key": str(second["pet_key"]),
                                "fusion_tier": str(hybrid_data.get("fusion_tier") or "perfect"),
                                "fusion_image_key": str(hybrid_data.get("fusion_image_key") or ""),
                                "created_at": now.isoformat(),
                            }
                            cursor.execute(
                                "DELETE FROM user_pets WHERE id = %s", (second["id"],)
                            )
                            cursor.execute(
                                """
                                UPDATE user_pets
                                SET is_fused = TRUE,
                                    custom_name = %s,
                                    fusion_data = %s,
                                    fusion_parents = %s,
                                    stage = %s,
                                    stage_started_at = %s,
                                    happiness = LEAST(100, happiness + 10)
                                WHERE id = %s
                                RETURNING *
                                """,
                                (
                                    hybrid_data["name"],
                                    psycopg2.extras.Json(hybrid_data),
                                    psycopg2.extras.Json(parents),
                                    int(hybrid_data.get("fusion_stage") or 5),
                                    now,
                                    first["id"],
                                ),
                            )
                            result_pet = dict(cursor.fetchone())
                            outcome = "success"
                        else:
                            result_pet = first
                            outcome = "failed"
                            cursor.execute(
                                "UPDATE user_pets SET happiness = GREATEST(0, happiness - 15) WHERE id IN (%s, %s)",
                                (first["id"], second["id"]),
                            )
                            for item_key, needed in item_requirements.items():
                                refund = needed // 2
                                if refund > 0:
                                    cursor.execute(
                                        """
                                        INSERT INTO pet_items (guild_id, user_id, item_key, quantity, updated_at)
                                        VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
                                        ON CONFLICT (guild_id, user_id, item_key) DO UPDATE SET
                                            quantity = pet_items.quantity + EXCLUDED.quantity,
                                            updated_at = CURRENT_TIMESTAMP
                                        """,
                                        (guild_id, user_id, item_key, refund),
                                    )

                    cursor.execute(
                        """
                        INSERT INTO pet_fusion_history (guild_id, user_id, primary_pet_id, secondary_pet_id, fusion_type, outcome, details)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            guild_id,
                            user_id,
                            first["id"],
                            second["id"],
                            req["type"],
                            outcome,
                            psycopg2.extras.Json(
                                {
                                    "target": req["target_name"],
                                    "chance": req["chance"],
                                    "credits": req["credits"],
                                }
                            ),
                        ),
                    )
                    return {
                        "ok": True,
                        "type": req["type"],
                        "outcome": outcome,
                        "pet": result_pet,
                        "requirements": req,
                    }
        finally:
            self._pool.putconn(conn)

    def get_pet_leaderboard(
        self, guild_id: int, limit: int = 10
    ) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT user_id, COUNT(*) AS pet_count, SUM(total_earned) AS total_earned, MAX(total_earned) AS best_pet_earned
                    FROM user_pets
                    WHERE guild_id = %s
                    GROUP BY user_id
                    ORDER BY SUM(total_earned) DESC
                    LIMIT %s
                    """,
                    (guild_id, limit),
                )
                return [dict(row) for row in cursor.fetchall()]
        finally:
            self._pool.putconn(conn)

class PetsCog(commands.Cog):
    async def _send_pet_embed(
        self,
        ctx: commands.Context,
        embed: discord.Embed,
        file: Optional[discord.File] = None,
    ) -> None:
        try:
            if file is not None:
                await send_v2(ctx, embed, file=file)
            else:
                await send_v2(ctx, embed)
        except Exception:
            await ctx.send(
                embed=embed, file=file, allowed_mentions=discord.AllowedMentions.none()
            )

    async def _send_fusion_animation(
        self, ctx: commands.Context, result: dict[str, Any]
    ) -> None:
        req = result.get("requirements") or {}
        first = req.get("first") or {}
        second = req.get("second") or {}
        target = str(req.get("target_name") or _pet_display_name(result.get("pet") or {}))
        outcome = str(result.get("outcome") or "success")
        frames = [
            (
                "Fusion Reactor",
                f"`#{first.get('id', '?')}` **{_pet_display_name(first)}** and "
                f"`#{second.get('id', '?')}` **{_pet_display_name(second)}** enter the core chamber.\n\n"
                "`[■□□□□□□□□□]` Soul current stabilizing...",
            ),
            (
                "Fusion Reactor",
                f"The Fusion Cores ignite around the Soul Shard infinity loop.\n\n"
                "`[■■■■□□□□□□]` ∞ energy folding...",
            ),
            (
                "Fusion Reactor",
                f"The two pet souls spiral into one abyss-bright silhouette.\n\n"
                "`[■■■■■■■□□□]` Target: **{target}**",
            ),
        ]
        if outcome == "failed":
            frames.append(
                (
                    "Fusion Collapse",
                    "The hybrid reaction cracked, but both pets survived.\n\n"
                    "`[■■■■■■□□□□]` Cores refunded, happiness shaken.",
                )
            )
        else:
            frames.append(
                (
                    "Fusion Complete",
                    f"The chamber opens with a flash.\n\n"
                    "`[■■■■■■■■■■]` **{target}** awakened.",
                )
            )

        message: Optional[discord.Message] = None
        for index, (title, description) in enumerate(frames):
            embed = discord.Embed(title=title, description=description, color=0xE67E22)
            if message is None:
                message = await ctx.send(embed=embed)
            else:
                await message.edit(embed=embed)
            if index < len(frames) - 1:
                await asyncio.sleep(0.9)

    def _add_pet_thumbnail_section(
        self,
        children: list[discord.ui.Item[Any]],
        files: list[discord.File],
        attached_filenames: set[str],
        text: str,
        row_or_key: Any,
    ) -> None:
        pet_file, pet_url = _pet_image_attachment(row_or_key)
        if pet_file and pet_url:
            if pet_file.filename not in attached_filenames:
                attached_filenames.add(pet_file.filename)
                files.append(pet_file)
            children.append(thumbnail_text_section(text, pet_url))
            return
        children.append(discord.ui.TextDisplay(text))

    def _pet_line_summary(self, pet_key: str) -> str:
        line = PET_LINES[pet_key]
        first = line["stages"][0]
        final = line["stages"][-1]
        return (
            f"Starts as **{first['name']}** for **{int(first['price']):,} cr**.\n"
            f"Final form: **{final['name']}** - **{int(final['daily_income']):,} cr/hour**.\n"
            f"Starter perk: **{_pet_perk_summary_from_data(first)}**\n"
            f"Theme: {line['theme']}"
        )

    def _pet_line_field_name(self, pet_key: str) -> str:
        line = PET_LINES[pet_key]
        stage = line["stages"][0]
        return f"{RARITY_BADGES.get(stage['rarity'], '*')} {line['family']}"

    def _pet_brief(self, pet: dict[str, Any]) -> str:
        name = _pet_display_name(pet)
        rarity = _pet_rarity(pet)
        happiness = int(pet.get("happiness") or 0)
        return (
            f"`#{pet['id']}` **{name}** [{rarity}]\n"
            f"Stage **{int(pet['stage'])}/5** | Income **{_pet_hourly_income(pet):,} cr/hour** | "
            f"Happiness `{_progress_bar(happiness)}` **{happiness}%** ({_pet_mood(happiness)})\n"
            f"Perk: **{_pet_perk_summary(pet)}**"
        )

    async def _resolve_owned_pet(
        self, ctx: commands.Context, raw_ref: str
    ) -> Optional[dict[str, Any]]:
        result = await asyncio.to_thread(
            self.store.resolve_user_pet, ctx.guild.id, ctx.author.id, raw_ref
        )
        if result.get("ok"):
            return result["pet"]
        if result.get("reason") == "ambiguous":
            matches = ", ".join(
                f"`#{pet['id']}`" for pet in result.get("matches", [])[:8]
            )
            await ctx.send(
                f"That pet name matches multiple pets. Use one of these IDs: {matches}"
            )
        else:
            await ctx.send(
                "I could not find that pet in your collection. Use `.mypets` to see pet IDs."
            )
        return None

    def _parse_two_pet_refs(self, raw_refs: str) -> Optional[tuple[str, str]]:
        cleaned = (raw_refs or "").strip()
        if "+" in cleaned:
            first, second = cleaned.split("+", 1)
            return first.strip(), second.strip()
        parts = cleaned.split()
        if len(parts) == 2:
            return parts[0], parts[1]
        return None

    async def _pet_adjusted_gambling_winnings(
        self, guild_id: int, user_id: int, bet: int, winnings: int
    ) -> tuple[int, int, float]:
        if winnings <= bet:
            return winnings, 0, 0.0
        multiplier = await asyncio.to_thread(
            self.store.get_pet_gamble_multiplier, guild_id, user_id
        )
        if multiplier > 0:
            winnings = int(winnings * (1.0 + multiplier))
        profit = winnings - bet
        result = await asyncio.to_thread(
            self.store.apply_total_bonus, guild_id, user_id, profit, "gambling"
        )
        bonus = int(result["bonus"])
        return winnings + bonus, bonus, float(result["rate"])

    async def _pet_luck_bonus(self, guild_id: int, user_id: int) -> float:
        return await asyncio.to_thread(
            self.store.get_pet_luck_bonus, guild_id, user_id
        )

    async def _shop_femboy_mode(self, guild: Optional[discord.Guild]) -> bool:
        if guild is None or not hasattr(self.bot, "guild_settings"):
            return True
        try:
            settings = await asyncio.to_thread(
                self.bot.guild_settings.get_settings, guild.id
            )
            return settings.get("femboy_mode") is not False
        except Exception:
            return True

    @staticmethod
    def _pet_luck_triggers(luck_bonus: float) -> bool:
        return luck_bonus > 0 and random.random() < min(luck_bonus, PET_LUCK_BONUS_CAP)

    async def _send_pet_shop(self, ctx: commands.Context) -> None:
        view = PetShopView(
            self, ctx, femboy_mode=await self._shop_femboy_mode(ctx.guild)
        )
        await send_v2(ctx, embed=None, view=view, files=view.files)

    async def _send_my_pets(self, ctx: commands.Context, page: int = 1) -> None:
        rows, items = await asyncio.gather(
            asyncio.to_thread(self.store.get_user_pets, ctx.guild.id, ctx.author.id),
            asyncio.to_thread(self.store.get_pet_items, ctx.guild.id, ctx.author.id),
        )
        view = PetListView(self, ctx, rows, items, page=page)
        await send_v2(ctx, embed=None, view=view, files=view.files)

    @commands.command(
        name="petshop", aliases=["petstore"], help="Browse passive income pets."
    )
    async def petshop_cmd(self, ctx: commands.Context):
        await self._send_pet_shop(ctx)

    @commands.command(
        name="buypet",
        aliases=["buy_pet"],
        help="Buy a starter pet. Usage: .buypet <kitsune|oni|seraphim|abyssal|leviathan>",
    )
    async def buypet_cmd(self, ctx: commands.Context, *, pet_name: str):
        return await self._purchase_pets(ctx, pet_name, 1)

    async def _purchase_pets(
        self, ctx: commands.Context, pet_name: str, amount: int
    ) -> None:
        femboy = await self._shop_femboy_mode(ctx.guild)
        pet_key = _match_pet_line(pet_name)
        if pet_key is None:
            return await ctx.send(
                (
                    "I dunno dat pet wine, cutie~ Use `.petshop` to see da adoptabwe pets."
                    if femboy
                    else "Unknown pet line. Use `.petshop` to see available pets."
                )
            )
        result = await asyncio.to_thread(
            self.store.buy_pets, ctx.guild.id, ctx.author.id, pet_key, amount
        )
        if not result.get("ok"):
            reason = result.get("reason")
            if reason == "insufficient":
                return await ctx.send(
                    (
                        f"Awww, chu need **{int(result['price']):,} cr** but onwy have **{int(result['balance']):,} cr**."
                        if femboy
                        else f"You need **{int(result['price']):,} cr** but only have **{int(result['balance']):,} cr**."
                    )
                )
            if reason == "pet_limit":
                return await ctx.send(
                    (
                        f"Chu awweady have da max of **{result['limit']}** pets, sweetie~"
                        if femboy
                        else f"You already have the max of **{result['limit']}** pets."
                    )
                )
            if reason == "not_for_sale":
                return await ctx.send(
                    (
                        "Dat pet can't be bought, cutie~ Kwaken pets awe found fwom fishing."
                        if femboy
                        else "That pet cannot be bought. Kraken pets are found from fishing."
                    )
                )
            return await ctx.send(
                "I couwdn't buy dat pet wight now, sowwy~"
                if femboy
                else "I could not buy that pet right now."
            )
        pet = result["pet"]
        purchased_amount = int(result.get("amount") or 1)
        embed = discord.Embed(
            title="Pet Adopted UwU" if femboy else "Pet Purchased",
            description=(
                f"Chu adopted **{purchased_amount}x {_pet_display_name(pet)}** for **{int(result['price']):,} cr**!\nUse `.mypets` to see youw cute cowwection~"
                if femboy
                else f"You bought **{purchased_amount}x {_pet_display_name(pet)}** for **{int(result['price']):,} cr**.\nUse `.mypets` to view your collection."
            ),
            color=PET_RARITY_COLORS.get(_pet_rarity(pet), 0x9B59B6),
        )
        file, url = _pet_image_attachment(pet)
        if url:
            embed.set_image(url=url)
        await self._send_pet_embed(ctx, embed, file=file)

    @commands.command(
        name="mypets", aliases=["petlist"], help="Show your pet collection."
    )
    async def mypets_cmd(self, ctx: commands.Context, page: str = "1"):
        parsed_page = _parse_credit_amount(page) or 1
        await self._send_my_pets(ctx, int(parsed_page))

    @commands.command(
        name="collectpets",
        aliases=["petcollect"],
        help="Collect passive income from all pets.",
    )
    async def collectpets_cmd(self, ctx: commands.Context):
        result = await asyncio.to_thread(
            self.store.collect_pet_income, ctx.guild.id, ctx.author.id
        )
        if not result.get("ok"):
            return await ctx.send(
                "You do not own any pets yet. Use `.petshop` to buy one."
            )
        top_lines = []
        for item in result["collected"][:8]:
            if int(item["amount"]) > 0:
                top_lines.append(
                    f"`#{item['pet']['id']}` {_pet_display_name(item['pet'])}: **{int(item['amount']):,} cr** ({int(item['hours'])}h)"
                )
        embed = discord.Embed(
            title="Pet Income Collected",
            description="\n".join(top_lines)
            if top_lines
            else "Your pets have not built up a full hour of income yet.",
            color=0x2ECC71 if int(result["total"]) > 0 else 0x95A5A6,
        )
        embed.add_field(
            name="Total", value=f"**{int(result['total']):,} cr**", inline=True
        )
        embed.add_field(
            name="Balance", value=f"**{int(result['balance']):,} cr**", inline=True
        )
        if float(result.get("passive_bonus") or 0) > 0:
            embed.add_field(
                name="Passive Bonus",
                value=f"+{float(result['passive_bonus']) * 100:.1f}%",
                inline=True,
            )
        featured = next(
            (item["pet"] for item in result["collected"] if int(item["amount"]) > 0),
            None,
        )
        file, url = _pet_image_attachment(featured) if featured else (None, None)
        if url:
            embed.set_image(url=url)
        await self._send_pet_embed(ctx, embed, file=file)

    @commands.group(
        name="pet",
        aliases=["pets"],
        invoke_without_command=True,
        help="Manage passive income pets.",
    )
    async def pet_group(self, ctx: commands.Context, page: str = "1"):
        parsed_page = _parse_credit_amount(page) or 1
        await self._send_my_pets(ctx, int(parsed_page))

    @pet_group.command(
        name="shop", help="Browse starter pets and fusion core materials."
    )
    async def pet_shop_subcommand(self, ctx: commands.Context):
        await self._send_pet_shop(ctx)

    @pet_group.command(
        name="collect", help="Collect passive income from all owned pets."
    )
    async def pet_collect_subcommand(self, ctx: commands.Context):
        await self.collectpets_cmd.callback(self, ctx)

    @pet_group.command(
        name="info",
        help="Show stats, income, happiness, and bonuses for one owned pet.",
    )
    async def pet_info_subcommand(self, ctx: commands.Context, *, pet_ref: str):
        pet = await self._resolve_owned_pet(ctx, pet_ref)
        if not pet:
            return
        embed = discord.Embed(
            title=f"#{pet['id']} - {_pet_display_name(pet)}",
            color=PET_RARITY_COLORS.get(_pet_rarity(pet), 0x9B59B6),
        )
        embed.add_field(name="Rarity", value=_pet_rarity(pet), inline=True)
        embed.add_field(name="Stage", value=f"{int(pet['stage'])}/5", inline=True)
        embed.add_field(
            name="Income", value=f"{_pet_hourly_income(pet):,} cr/hour", inline=True
        )
        embed.add_field(
            name="Happiness",
            value=f"`{_progress_bar(int(pet['happiness']))}` **{int(pet['happiness'])}%** ({_pet_mood(int(pet['happiness']))})",
            inline=False,
        )
        embed.add_field(
            name="Bonus",
            value=_pet_perk_summary(pet),
            inline=True,
        )
        embed.add_field(
            name="Total Earned",
            value=f"{int(pet.get('total_earned') or 0):,} cr",
            inline=True,
        )
        embed.set_footer(
            text="Use .pet feed, .pet play, .pet evolve, or .pet fusion preview."
        )
        file, url = _pet_image_attachment(pet)
        if url:
            embed.set_image(url=url)
        await self._send_pet_embed(ctx, embed, file=file)

    @pet_group.command(
        name="feed", help="Feed one owned pet to increase its happiness."
    )
    async def pet_feed_subcommand(self, ctx: commands.Context, *, pet_ref: str):
        pet = await self._resolve_owned_pet(ctx, pet_ref)
        if not pet:
            return
        result = await asyncio.to_thread(
            self.store.feed_pet, ctx.guild.id, ctx.author.id, int(pet["id"])
        )
        if not result.get("ok"):
            if result.get("reason") == "insufficient":
                return await ctx.send(
                    f"Feeding costs **{int(result['cost']):,} cr** and you have **{int(result['balance']):,} cr**."
                )
            return await ctx.send("I could not feed that pet.")
        fed_pet = result["pet"]
        embed = discord.Embed(
            title="Pet Fed",
            description=f"Fed **{_pet_display_name(fed_pet)}** for **{int(result['cost']):,} cr**.\nHappiness is now **{int(result['happiness'])}%**.",
            color=PET_RARITY_COLORS.get(_pet_rarity(fed_pet), 0x9B59B6),
        )
        file, url = _pet_image_attachment(fed_pet)
        if url:
            embed.set_image(url=url)
        await self._send_pet_embed(ctx, embed, file=file)

    @pet_group.command(
        name="play", help="Play with one owned pet to increase its happiness."
    )
    async def pet_play_subcommand(self, ctx: commands.Context, *, pet_ref: str):
        pet = await self._resolve_owned_pet(ctx, pet_ref)
        if not pet:
            return
        result = await asyncio.to_thread(
            self.store.play_with_pet, ctx.guild.id, ctx.author.id, int(pet["id"])
        )
        if not result.get("ok"):
            if result.get("reason") == "cooldown":
                return await ctx.send(
                    f"That pet can play again <t:{_discord_timestamp(result['ready_at'])}:R>."
                )
            return await ctx.send("I could not play with that pet.")
        played_pet = result["pet"]
        embed = discord.Embed(
            title="Pet Playtime",
            description=f"You played with **{_pet_display_name(played_pet)}**.\nHappiness is now **{int(result['happiness'])}%**.",
            color=PET_RARITY_COLORS.get(_pet_rarity(played_pet), 0x9B59B6),
        )
        file, url = _pet_image_attachment(played_pet)
        if url:
            embed.set_image(url=url)
        await self._send_pet_embed(ctx, embed, file=file)

    @pet_group.command(
        name="evolution",
        help="Preview a pet line's evolution path or an owned pet's current stage.",
    )
    async def pet_evolution_subcommand(
        self, ctx: commands.Context, *, pet_or_line: str
    ):
        pet_key = _match_pet_line(pet_or_line)
        pet = None
        if pet_key is None:
            pet = await self._resolve_owned_pet(ctx, pet_or_line)
            if not pet:
                return
            pet_key = str(pet["pet_key"])
        view = discord.ui.LayoutView(timeout=None)
        files: list[discord.File] = []
        attached_filenames: set[str] = set()
        children: list[discord.ui.Item[Any]] = [
            discord.ui.TextDisplay(
                f"**{PET_LINES[pet_key]['family']} Evolution**\n{PET_LINES[pet_key]['theme']}"
            )
        ]
        children.append(discord.ui.Separator())
        for index, stage in enumerate(PET_LINES[pet_key]["stages"], start=1):
            req = ""
            if index > 1:
                req = f" - {int(stage['evolve_credits']):,} cr, {int(stage['evolve_shards']):,} shards, {int(stage['time_days'])}d"
            marker = " <- current" if pet and int(pet["stage"]) == index else ""
            text = (
                f"**{index}. {stage['name']}** [{stage['rarity']}]{marker}\n"
                f"Income **{int(stage['daily_income']):,} cr/hour**{req}\n"
                f"Perk: **{_pet_perk_summary_from_data(stage)}**"
            )
            self._add_pet_thumbnail_section(
                children, files, attached_filenames, text, f"{pet_key}_{index}"
            )
        view.add_item(discord.ui.Container(*children, accent_color=0x9B59B6))
        await send_v2(ctx, embed=None, view=view, files=files)

    @pet_group.command(
        name="evolve",
        help="Evolve an eligible owned pet using credits and Soul Shards.",
    )
    async def pet_evolve_subcommand(self, ctx: commands.Context, *, pet_ref: str):
        pet = await self._resolve_owned_pet(ctx, pet_ref)
        if not pet:
            return
        result = await asyncio.to_thread(
            self.store.evolve_pet, ctx.guild.id, ctx.author.id, int(pet["id"])
        )
        if not result.get("ok"):
            reason = result.get("reason")
            if reason == "time":
                return await ctx.send(
                    f"This pet is not ready to evolve yet. Ready <t:{_discord_timestamp(result['ready_at'])}:R>.\n*(You can bypass this wait by buying an `evolve_crystal`)*"
                )
            if reason == "insufficient":
                return await ctx.send(
                    f"Evolution costs **{int(result['price']):,} cr** and you have **{int(result['balance']):,} cr**."
                )
            if reason == "shards":
                return await ctx.send(
                    f"Evolution needs **{int(result['required']):,} Soul Shards**. You have **{int(result['owned']):,}**."
                )
            if reason == "legendary_limit":
                return await ctx.send(
                    f"You already have the max of **{result['limit']}** Legendary/Fused pets."
                )
            if reason == "max_stage":
                return await ctx.send("That pet is already at max evolution.")
            if reason == "fused":
                return await ctx.send("Fused pets cannot evolve further.")
            return await ctx.send("I could not evolve that pet.")
        new_pet = result["pet"]
        desc = f"Your pet awakened into **{_pet_display_name(new_pet)}**.\nSpent **{int(result['credits']):,} cr** and **{int(result['shards']):,} Soul Shards**."
        if result.get("used_crystal"):
            desc += "\n*(Consumed 1x Evolve Crystal to skip wait!)*"
            
        embed = discord.Embed(
            title="Pet Evolved",
            description=desc,
            color=PET_RARITY_COLORS.get(_pet_rarity(new_pet), 0x9B59B6),
        )
        file, url = _pet_image_attachment(new_pet)
        if url:
            embed.set_image(url=url)
        await self._send_pet_embed(ctx, embed, file=file)

    @pet_group.command(
        name="cores", help="View or buy fusion cores from the hourly core shop."
    )
    async def pet_cores_subcommand(
        self, ctx: commands.Context, action: str = "", item: str = "", amount: str = "1"
    ):
        if not action:
            femboy = await self._shop_femboy_mode(ctx.guild)
            shop, items = await asyncio.gather(
                asyncio.to_thread(self.store.ensure_pet_core_shop, ctx.guild.id),
                asyncio.to_thread(
                    self.store.get_pet_items, ctx.guild.id, ctx.author.id
                ),
            )
            embed = discord.Embed(
                title="Fusion Cowe Shop UwU" if femboy else "Fusion Core Shop",
                color=0xE67E22,
            )
            for row in shop:
                item_name = PET_ITEM_DEFS[row["item_key"]]["name"]
                stock_label = _format_pet_core_stock(row["stock"], row["daily_stock"])
                embed.add_field(
                    name=item_name,
                    value=(
                        f"{'Pwice' if femboy else 'Price'} **{int(row['price']):,} cr** | "
                        f"{'Stockies' if femboy else 'Stock'} **{stock_label}**\n"
                        f"{'Buyy' if femboy else 'Buy'}: `.pet cores buy {row['item_key']} 1`"
                    ),
                    inline=False,
                )
            owned = ", ".join(
                f"{PET_ITEM_DEFS[key]['name']}: **{qty:,}**"
                for key, qty in items.items()
                if qty > 0 and key in PET_ITEM_DEFS
            )
            embed.add_field(
                name="Youw Matewiaws" if femboy else "Your Materials",
                value=owned or ("No matewiaws yet, cutie~" if femboy else "No materials yet."),
                inline=False,
            )
            return await self._send_pet_embed(ctx, embed)

        femboy = await self._shop_femboy_mode(ctx.guild)
        if action.lower() != "buy":
            return await ctx.send(
                (
                    "Use `.pet cores` to view stockies or `.pet cores buy <shard|core|greater|crystal> [amount]`, cutie~"
                    if femboy
                    else "Use `.pet cores` to view stock or `.pet cores buy <shard|core|greater|crystal> [amount]`."
                )
            )
        item_key = _match_pet_item(item)
        if item_key not in PET_CORE_SHOP:
            return await ctx.send(
                "Chu can buy `soul_shard`, `fusion_core`, `greater_fusion_core`, or `evolve_crystal`, UwU~"
                if femboy
                else "You can buy `soul_shard`, `fusion_core`, `greater_fusion_core`, or `evolve_crystal`."
            )
        parsed = _parse_credit_amount(amount) or 1
        result = await asyncio.to_thread(
            self.store.buy_pet_core, ctx.guild.id, ctx.author.id, item_key, parsed
        )
        if not result.get("ok"):
            if result.get("reason") == "stock":
                return await ctx.send(
                    (
                        f"Onwy **{int(result['stock'])}** awe left in daiwy stockies~"
                        if femboy
                        else f"Only **{int(result['stock'])}** are left in daily stock."
                    )
                )
            if result.get("reason") == "insufficient":
                return await ctx.send(
                    (
                        f"Dat costs **{int(result['price']):,} cr** and chu have **{int(result['balance']):,} cr**."
                        if femboy
                        else f"That costs **{int(result['price']):,} cr** and you have **{int(result['balance']):,} cr**."
                    )
                )
            return await ctx.send(
                "I couwdn't buy dat cowe, sowwy cutie~"
                if femboy
                else "I could not buy that core."
            )
        await ctx.send(
            (
                f"Bought **{int(result['amount'])}x {PET_ITEM_DEFS[item_key]['name']}** for **{int(result['price']):,} cr**! Chu now have **{int(result['quantity'])}** UwU~"
                if femboy
                else f"Bought **{int(result['amount'])}x {PET_ITEM_DEFS[item_key]['name']}** for **{int(result['price']):,} cr**. You now have **{int(result['quantity'])}**."
            )
        )

    @pet_group.group(
        name="fusion",
        invoke_without_command=True,
        help="Preview fusion outcomes before combining pets.",
    )
    async def pet_fusion_group(self, ctx: commands.Context):
        await ctx.send(
            "Use `.pet fusion preview <pet id> + <pet id>` or `.pet fuse <pet id> + <pet id>`."
        )

    @pet_fusion_group.command(
        name="preview", help="Preview the cost, chance, and result for fusing two pets."
    )
    async def pet_fusion_preview_subcommand(
        self, ctx: commands.Context, *, pet_refs: str
    ):
        parsed = self._parse_two_pet_refs(pet_refs)
        if not parsed:
            return await ctx.send("Use `.pet fusion preview #12 + #15`.")
        first = await self._resolve_owned_pet(ctx, parsed[0])
        second = await self._resolve_owned_pet(ctx, parsed[1])
        if not first or not second:
            return
        result = await asyncio.to_thread(
            self.store.preview_pet_fusion,
            ctx.guild.id,
            ctx.author.id,
            int(first["id"]),
            int(second["id"]),
        )
        if not result.get("ok"):
            return await ctx.send(
                "Those pets cannot be fused. Safe fusion needs two same-line Rare+ pets at the same stage; mixed fusion needs two different pet lines at stage 3 or higher."
            )
        embed = discord.Embed(title="Fusion Preview", color=0xE67E22)
        type_label = str(result["type"]).title()
        if result["type"] == "hybrid" and result.get("tier_label"):
            type_label = f"{result['tier_label']} Hybrid"
        embed.description = (
            f"**{_pet_display_name(result['first'])}** + **{_pet_display_name(result['second'])}**\n"
            f"Outcome: **{result['target_name']}**\n"
            f"Type: **{type_label}** | Success chance: **{float(result['chance']) * 100:.0f}%**"
        )
        embed.add_field(
            name="Cost",
            value=f"{int(result['credits']):,} cr\nFusion Cores: {int(result['fusion_core'])}\nGreater Cores: {int(result['greater_fusion_core'])}",
            inline=False,
        )
        view = discord.ui.LayoutView(timeout=None)
        files: list[discord.File] = []
        attached_filenames: set[str] = set()
        children: list[discord.ui.Item[Any]] = [
            discord.ui.TextDisplay(f"**Fusion Preview**\n{embed.description}")
        ]
        children.append(discord.ui.Separator())
        self._add_pet_thumbnail_section(
            children,
            files,
            attached_filenames,
            f"**Primary**\n`#{result['first']['id']}` {_pet_display_name(result['first'])}",
            result["first"],
        )
        self._add_pet_thumbnail_section(
            children,
            files,
            attached_filenames,
            f"**Secondary**\n`#{result['second']['id']}` {_pet_display_name(result['second'])}",
            result["second"],
        )
        children.append(discord.ui.Separator())
        children.append(
            discord.ui.TextDisplay(
                f"**Cost**\n{int(result['credits']):,} cr\nFusion Cores: {int(result['fusion_core'])}\nGreater Cores: {int(result['greater_fusion_core'])}"
            )
        )
        target_image = None
        if result["type"] == "hybrid":
            target_image = (result.get("hybrid_data") or {}).get("fusion_image_key")
        else:
            target_image = f"{result['first']['pet_key']}_{int(result['first']['stage']) + 1}"
        if target_image:
            self._add_pet_thumbnail_section(
                children,
                files,
                attached_filenames,
                f"**Fusion Result**\n{result['target_name']}",
                str(target_image),
            )
        view.add_item(discord.ui.Container(*children, accent_color=0xE67E22))
        await send_v2(ctx, embed=None, view=view, files=files)

    @pet_group.command(
        name="fuse", help="Fuse two compatible owned pets into a stronger pet."
    )
    async def pet_fuse_subcommand(self, ctx: commands.Context, *, pet_refs: str = None):
        if pet_refs is None:
            return await ctx.send("Use `.pet fuse #12 + #15`.")
        parsed = self._parse_two_pet_refs(pet_refs)
        if not parsed:
            return await ctx.send("Use `.pet fuse #12 + #15`.")
        first = await self._resolve_owned_pet(ctx, parsed[0])
        second = await self._resolve_owned_pet(ctx, parsed[1])
        if not first or not second:
            return
        result = await asyncio.to_thread(
            self.store.fuse_pets,
            ctx.guild.id,
            ctx.author.id,
            int(first["id"]),
            int(second["id"]),
        )
        if not result.get("ok"):
            reason = result.get("reason")
            if reason == "insufficient":
                return await ctx.send(
                    f"Fusion costs **{int(result['price']):,} cr** and you have **{int(result['balance']):,} cr**."
                )
            if reason == "items":
                return await ctx.send(
                    f"Fusion needs **{int(result['required'])}x {PET_ITEM_DEFS[result['item_key']]['name']}**. You have **{int(result['owned'])}**."
                )
            if reason == "legendary_limit":
                return await ctx.send(
                    f"You already have the max of **{result['limit']}** Legendary/Fused pets."
                )
            return await ctx.send(
                "Those pets cannot be fused. Use `.pet fusion preview #id + #id` first."
            )
        await self._send_fusion_animation(ctx, result)
        if result["outcome"] == "failed":
            return await ctx.send(
                "Hybrid fusion failed. Your pets survived, 50% of eligible cores were refunded, and both pets lost happiness."
            )
        pet = result["pet"]
        
        if pet.get("is_fused") and pet.get("fusion_data"):
            data = pet["fusion_data"]
            if isinstance(data, str):
                import json
                try:
                    data = json.loads(data)
                except Exception:
                    data = {}
            perks = _pet_perk_lines_from_data(data) if isinstance(data, dict) else []
        else:
            perks = _pet_perk_lines_from_data(_pet_stage(str(pet["pet_key"]), int(pet["stage"])))
            
        perk_text = "\n".join(f"• {p}" for p in perks) if perks else "No bonuses."
        
        embed = discord.Embed(
            title="Pet Fused!",
            description=f"You have fused **{_pet_display_name(first)}** and **{_pet_display_name(second)}**!\n\n**New Perks:**\n{perk_text}",
            color=PET_RARITY_COLORS.get(_pet_rarity(pet), 0xE67E22),
        )
        file, url = _pet_image_attachment(pet)
        if url:
            embed.set_image(url=url)
        await self._send_pet_embed(ctx, embed, file=file)

    @pet_group.command(
        name="leaderboard", aliases=["lb"], help="Show the pet income leaderboard."
    )
    async def pet_leaderboard_subcommand(self, ctx: commands.Context):
        rows = await asyncio.to_thread(self.store.get_pet_leaderboard, ctx.guild.id, 10)
        embed = discord.Embed(title="Pet Income Leaderboard", color=0xF1C40F)
        if not rows:
            embed.description = "No pet income has been collected yet."
        else:
            lines = []
            for index, row in enumerate(rows, start=1):
                member = ctx.guild.get_member(int(row["user_id"]))
                name = member.display_name if member else f"User {row['user_id']}"
                lines.append(
                    f"**{index}. {name}** - **{int(row['total_earned'] or 0):,} cr** from **{int(row['pet_count'])}** pet(s)"
                )
            embed.description = "\n".join(lines)
        await self._send_pet_embed(ctx, embed)

