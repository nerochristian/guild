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
from .pets import _normalize_pet_lookup


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
SHOP_REFRESH_HOURS = 1
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
}
PET_LEGACY_IMAGE_FILES = {
    "catalog": "pet_catalog.png",
    "kitsune": "kitsune_evolution.png",
    "oni": "oni_evolution.png",
    "seraphim": "seraphim_evolution.png",
    "abyssal": "abyssal_evolution.png",
}
for _name in ["kitsune", "oni", "seraphim", "abyssal"]:
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
            "desc": "Robbery defense +10% for 10 minutes.",
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

def _match_economy_item(raw_item: str) -> Optional[str]:
    cleaned = _normalize_pet_lookup(raw_item).replace("_", " ")
    for key, data in ECONOMY_ITEM_DEFS.items():
        aliases = {str(alias).casefold().replace("_", " ") for alias in data["aliases"]}
        if (
            cleaned == data["name"].casefold()
            or cleaned in aliases
            or cleaned == key.replace("_", " ")
        ):
            return key
    return None

def _match_shop_item_name(raw_name: str) -> Optional[str]:
    cleaned = " ".join((raw_name or "").split()).casefold()
    return next((name for name in SHOP_ITEMS if name.casefold() == cleaned), None)


def _parse_purchase_request(raw_purchase: str) -> tuple[str, int]:
    cleaned = " ".join((raw_purchase or "").split())
    suffix_amount_match = re.fullmatch(r"(.+?)\s+(\d+)", cleaned)
    prefix_amount_match = re.fullmatch(r"(\d+)\s+(.+)", cleaned)
    if prefix_amount_match:
        return prefix_amount_match.group(2), int(prefix_amount_match.group(1))
    if suffix_amount_match:
        return suffix_amount_match.group(1), int(suffix_amount_match.group(2))
    return cleaned, 1


def _split_purchase_requests(raw_purchase: str) -> list[str]:
    return [
        part.strip()
        for part in re.split(r"\s*(?:,|\band\b|\bor\b)\s*", raw_purchase, flags=re.IGNORECASE)
        if part.strip()
    ]


def _purchase_name_variants(item_name: str) -> list[str]:
    cleaned = " ".join((item_name or "").replace("(s)", "").split())
    if not cleaned:
        return []
    variants = [cleaned]
    lowered = cleaned.casefold()
    if lowered.endswith("ies") and len(cleaned) > 3:
        variants.append(cleaned[:-3] + "y")
    elif lowered.endswith("s") and len(cleaned) > 1:
        variants.append(cleaned[:-1])
    return variants


class BlackMarketView(discord.ui.LayoutView):
    TOOL_ORDER = ("lockpick", "advanced_lockpick", "drill", "fake_id", "getaway_car")

    def __init__(
        self,
        inventory: Dict[str, Dict[str, int]],
        balance: int,
        security: dict[str, Any],
        last_refresh: Optional[datetime],
        femboy_mode: bool = False,
    ):
        super().__init__(timeout=None)
        self.inventory = inventory
        self.balance = int(balance)
        self.security = security
        self.last_refresh = last_refresh
        self.femboy_mode = femboy_mode
        self.render()

    @staticmethod
    def _sale_price(price: int, discount: int) -> int:
        return int(price * (1 - max(0, int(discount)) / 100))

    @staticmethod
    def _price_line(price: int, discount: int) -> str:
        sale_price = BlackMarketView._sale_price(price, discount)
        if discount > 0:
            return f"Price: ~~{price:,}~~ **{sale_price:,} cr** `-{discount}%`"
        return f"Price: **{price:,} cr**"

    @staticmethod
    def _stock_line(stock: int, max_stock: int) -> str:
        if stock <= 0:
            return "**OUT OF STOCK**"
        return f"`{_format_stock_bar(stock, max_stock)}` **{stock}/{max_stock}** left"

    def _stock_for(self, item_key: str) -> int:
        inv = self.inventory.get(item_key, {"stock": 0})
        return int(inv.get("stock") or 0)

    def _status_text(self) -> str:
        lock_level = int(self.security.get("lock_level") or 0)
        wanted = int(self.security.get("wanted_level") or 0)
        jail_until = _as_utc(self.security.get("jail_until"))
        jail_text = (
            f"<t:{_discord_timestamp(jail_until)}:R>"
            if jail_until and jail_until > datetime.now(timezone.utc)
            else "Free"
        )
        if self.femboy_mode:
            return (
                f"Wawwet **{self.balance:,} cr** | "
                f"Wock **{LOCK_LEVELS.get(lock_level, LOCK_LEVELS[0])['name']}** | "
                f"Wanted **{wanted}/10** | Status **{jail_text}**"
            )
        return (
            f"Wallet **{self.balance:,} cr** | "
            f"Lock **{LOCK_LEVELS.get(lock_level, LOCK_LEVELS[0])['name']}** | "
            f"Wanted **{wanted}/10** | Status **{jail_text}**"
        )

    def _tool_card(self, item_key: str) -> str:
        data = ECONOMY_ITEM_DEFS[item_key]
        inv = self.inventory.get(item_key, {"stock": 0, "discount": 0})
        stock = int(inv.get("stock") or 0)
        discount = int(inv.get("discount") or 0)
        max_stock = int(data.get("max_stock", 1))
        rarity = str(data.get("rarity", "Common"))
        badge = RARITY_BADGES.get(rarity, "◆")
        price_line = self._price_line(int(data["price"]), discount)
        if self.femboy_mode:
            price_line = price_line.replace("Price:", "Pwice:")
        stock_label = "Stock" if not self.femboy_mode else "Stockies"
        return (
            f"{badge} **{data['name']}** [{rarity}]\n"
            f"{price_line}\n"
            f"{stock_label}: {self._stock_line(stock, max_stock)}\n"
            f"*{data.get('desc', '')}*"
        )

    def _lock_card(self, level: int) -> str:
        data = LOCK_LEVELS[level]
        inv = self.inventory.get(f"lock_{level}", {"stock": 0, "discount": 0})
        stock = int(inv.get("stock") or 0)
        discount = int(inv.get("discount") or 0)
        max_stock = int(data.get("max_stock", 1))
        rarity = str(data.get("rarity", "Common"))
        badge = RARITY_BADGES.get(rarity, "◆")
        penalty = int(float(data.get("penalty") or 0) * 100)
        price_line = self._price_line(int(data["price"]), discount)
        if self.femboy_mode:
            price_line = price_line.replace("Price:", "Pwice:")
        protection_label = "Protection" if not self.femboy_mode else "Pwotection"
        stock_label = "Stock" if not self.femboy_mode else "Stockies"
        return (
            f"{badge} **{data['name']}** [{rarity}]\n"
            f"{price_line}\n"
            f"{protection_label}: **-{penalty}% rob success**\n"
            f"{stock_label}: {self._stock_line(stock, max_stock)}"
        )

    def render(self) -> None:
        self.clear_items()
        available_tools = [
            item_key for item_key in self.TOOL_ORDER if self._stock_for(item_key) > 0
        ]
        available_locks = [
            level for level in (1, 2, 3) if self._stock_for(f"lock_{level}") > 0
        ]
        header_text = (
            "**Bwack Mawket UwU**\n"
            "Sneaky toows, heist geaw, and cute wocks wotate on timed stock. Sawe tags awe applied at checkout~\n"
            if self.femboy_mode
            else (
                "**Black Market**\n"
                "Robbery tools, heist gear, and wallet locks rotate on timed stock. Sale tags are reflected in the listed prices.\n"
            )
        )
        children: list[discord.ui.Item[Any]] = [
            discord.ui.TextDisplay(
                header_text
                + (
                f"{self._status_text()}\n"
                f"{'Westock' if self.femboy_mode else 'Restock'}: {_format_restock_countdown(self.last_refresh)}"
                )
            ),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.large),
            discord.ui.TextDisplay("**Toowsies**" if self.femboy_mode else "**Tools**"),
        ]
        for item_key in available_tools:
            children.append(discord.ui.TextDisplay(self._tool_card(item_key)))

        if not available_tools:
            children.append(
                discord.ui.TextDisplay(
                    (
                        "No wobbery toows awe in stock wight now, cutie. Check da westock timew above~"
                        if self.femboy_mode
                        else "No robbery tools are in stock right now. Check the restock timer above."
                    )
                )
            )
        children.append(discord.ui.Separator(spacing=discord.SeparatorSpacing.large))
        children.append(discord.ui.TextDisplay("**Wocks**" if self.femboy_mode else "**Locks**"))
        for level in available_locks:
            children.append(discord.ui.TextDisplay(self._lock_card(level)))

        if not available_locks:
            children.append(
                discord.ui.TextDisplay(
                    (
                        "No wock upgwades awe in stock wight now. Check da westock timew above~"
                        if self.femboy_mode
                        else "No lock upgrades are in stock right now. Check the restock timer above."
                    )
                )
            )
        children.append(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
        children.append(
            discord.ui.TextDisplay(
                (
                    "*How to buy: `.buyblackmarket <item> [amount]` (awias: `.buybm`). Wocks can onwy be bought one at a time~*"
                    if self.femboy_mode
                    else "*How to buy: `.buyblackmarket <item> [amount]` (alias: `.buybm`). Locks can only be purchased one at a time.*"
                )
            )
        )

        self.add_item(discord.ui.Container(*children, accent_color=0x101820))
        ensure_layout_view_action_rows(self)

class ShopStoreMixin:
    def get_shop_background(self, guild_id: int) -> Optional[str]:
        conn = self._connect()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT background_url FROM shop_settings WHERE guild_id = %s",
                    (guild_id,),
                )
                row = cursor.fetchone()
                return row[0] if row else None
        finally:
            self._pool.putconn(conn)

    def set_shop_background(self, guild_id: int, url: Optional[str]):
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    if url is None:
                        cursor.execute(
                            "DELETE FROM shop_settings WHERE guild_id = %s", (guild_id,)
                        )
                    else:
                        cursor.execute(
                            """
                            INSERT INTO shop_settings (guild_id, background_url)
                            VALUES (%s, %s)
                            ON CONFLICT (guild_id) DO UPDATE SET background_url = EXCLUDED.background_url
                            """,
                            (guild_id, url),
                        )
        finally:
            self._pool.putconn(conn)

    def buy_prestige(self, guild_id: int, user_id: int) -> dict[str, Any]:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    self._ensure_member_perks(cursor, guild_id, user_id)
                    cursor.execute(
                        "SELECT prestige_level FROM member_perks WHERE guild_id = %s AND user_id = %s FOR UPDATE",
                        (guild_id, user_id),
                    )
                    current_level = int(cursor.fetchone()["prestige_level"])
                    if current_level >= PRESTIGE_MAX_LEVEL:
                        return {"ok": False, "reason": "maxed", "level": current_level}

                    cost = _prestige_cost(current_level)
                    cursor.execute(
                        """
                        INSERT INTO member_economy (guild_id, user_id, credits, bank, updated_at)
                        VALUES (%s, %s, 0, 0, CURRENT_TIMESTAMP)
                        ON CONFLICT (guild_id, user_id) DO NOTHING
                        """,
                        (guild_id, user_id),
                    )
                    cursor.execute(
                        "SELECT credits, bank FROM member_economy WHERE guild_id = %s AND user_id = %s FOR UPDATE",
                        (guild_id, user_id),
                    )
                    row = cursor.fetchone()
                    wallet = int(row["credits"])
                    bank = int(row["bank"])
                    total = wallet + bank
                    if total < cost:
                        return {
                            "ok": False,
                            "reason": "insufficient",
                            "cost": cost,
                            "total": total,
                            "level": current_level,
                        }

                    wallet_take = min(wallet, cost)
                    bank_take = cost - wallet_take
                    cursor.execute(
                        """
                        UPDATE member_economy
                        SET credits = credits - %s,
                            bank = bank - %s,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE guild_id = %s AND user_id = %s
                        """,
                        (wallet_take, bank_take, guild_id, user_id),
                    )
                    cursor.execute(
                        """
                        UPDATE member_perks
                        SET prestige_level = prestige_level + 1,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE guild_id = %s AND user_id = %s
                        RETURNING prestige_level
                        """,
                        (guild_id, user_id),
                    )
                    new_level = int(cursor.fetchone()["prestige_level"])
                    cursor.execute(
                        "DELETE FROM user_pets WHERE guild_id = %s AND user_id = %s",
                        (guild_id, user_id),
                    )
                    pets_removed = cursor.rowcount
                    cursor.execute(
                        "DELETE FROM pet_items WHERE guild_id = %s AND user_id = %s",
                        (guild_id, user_id),
                    )
                    pet_items_removed = cursor.rowcount
                    return {
                        "ok": True,
                        "level": new_level,
                        "cost": cost,
                        "next_cost": None
                        if new_level >= PRESTIGE_MAX_LEVEL
                        else _prestige_cost(new_level),
                        "bonus_rate": new_level * PRESTIGE_BONUS_PER_LEVEL,
                        "pets_removed": max(0, pets_removed),
                        "pet_items_removed": max(0, pet_items_removed),
                    }
        finally:
            self._pool.putconn(conn)

    def get_inventory(self, guild_id: int) -> Dict[str, int]:
        conn = self._connect()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT role_name, current_stock FROM shop_inventory WHERE guild_id = %s",
                    (guild_id,),
                )
                return {
                    row["role_name"]: row["current_stock"] for row in cursor.fetchall()
                }
        finally:
            self._pool.putconn(conn)

    def get_last_shop_refresh(self, guild_id: int) -> Optional[datetime]:
        conn = self._connect()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT last_refresh FROM shop_state WHERE guild_id = %s",
                    (guild_id,),
                )
                row = cursor.fetchone()
                return row["last_refresh"] if row else None
        finally:
            self._pool.putconn(conn)

    def refresh_inventory(
        self,
        guild_id: int,
        items: Dict[str, Dict],
        refreshed_at: Optional[datetime] = None,
    ):
        refreshed_at = _as_utc(refreshed_at) or datetime.now(timezone.utc)
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    for name, data in items.items():
                        current_stock = _roll_shop_stock(data)
                        cursor.execute(
                            """
                            INSERT INTO shop_inventory (guild_id, role_name, current_stock, updated_at)
                            VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
                            ON CONFLICT (guild_id, role_name) DO UPDATE SET
                                current_stock = EXCLUDED.current_stock,
                                updated_at = CURRENT_TIMESTAMP
                            """,
                            (guild_id, name, current_stock),
                        )
                    cursor.execute(
                        """
                        INSERT INTO shop_state (guild_id, last_refresh)
                        VALUES (%s, %s)
                        ON CONFLICT (guild_id) DO UPDATE SET last_refresh = EXCLUDED.last_refresh
                        """,
                        (guild_id, refreshed_at),
                    )
        finally:
            self._pool.putconn(conn)

    def get_blackmarket_inventory(self, guild_id: int) -> Dict[str, Dict[str, int]]:
        conn = self._connect()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT item_key, current_stock, sale_discount FROM blackmarket_inventory WHERE guild_id = %s",
                    (guild_id,),
                )
                return {
                    row["item_key"]: {
                        "stock": row["current_stock"],
                        "discount": row["sale_discount"],
                    }
                    for row in cursor.fetchall()
                }
        finally:
            self._pool.putconn(conn)

    def get_last_blackmarket_refresh(self, guild_id: int) -> Optional[datetime]:
        conn = self._connect()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT last_refresh FROM blackmarket_state WHERE guild_id = %s",
                    (guild_id,),
                )
                row = cursor.fetchone()
                return row["last_refresh"] if row else None
        finally:
            self._pool.putconn(conn)

    def refresh_blackmarket_inventory(
        self,
        guild_id: int,
        tools: Dict[str, Dict],
        locks: Dict[int, Dict],
        refreshed_at: Optional[datetime] = None,
    ):
        refreshed_at = _as_utc(refreshed_at) or datetime.now(timezone.utc)
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    for key, data in tools.items():
                        current_stock = _roll_shop_stock(data)
                        discount = (
                            random.randint(10, 40) if random.random() < 0.20 else 0
                        )
                        cursor.execute(
                            """
                            INSERT INTO blackmarket_inventory (guild_id, item_key, current_stock, sale_discount, updated_at)
                            VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
                            ON CONFLICT (guild_id, item_key) DO UPDATE SET
                                current_stock = EXCLUDED.current_stock,
                                sale_discount = EXCLUDED.sale_discount,
                                updated_at = CURRENT_TIMESTAMP
                            """,
                            (guild_id, key, current_stock, discount),
                        )
                    for level, data in locks.items():
                        if level == 0:
                            continue
                        current_stock = _roll_shop_stock(data)
                        discount = (
                            random.randint(10, 40) if random.random() < 0.20 else 0
                        )
                        cursor.execute(
                            """
                            INSERT INTO blackmarket_inventory (guild_id, item_key, current_stock, sale_discount, updated_at)
                            VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
                            ON CONFLICT (guild_id, item_key) DO UPDATE SET
                                current_stock = EXCLUDED.current_stock,
                                sale_discount = EXCLUDED.sale_discount,
                                updated_at = CURRENT_TIMESTAMP
                            """,
                            (guild_id, f"lock_{level}", current_stock, discount),
                        )
                    cursor.execute(
                        """
                        INSERT INTO blackmarket_state (guild_id, last_refresh)
                        VALUES (%s, %s)
                        ON CONFLICT (guild_id) DO UPDATE SET last_refresh = EXCLUDED.last_refresh
                        """,
                        (guild_id, refreshed_at),
                    )
        finally:
            self._pool.putconn(conn)

    def set_blackmarket_inventory_stock(
        self, guild_id: int, item_key: str, stock: int, discount: Optional[int] = None
    ) -> dict[str, Any]:
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
                                current_stock = EXCLUDED.current_stock,
                                sale_discount = EXCLUDED.sale_discount,
                                updated_at = CURRENT_TIMESTAMP
                            """,
                            (guild_id, item_key, stock, discount),
                        )
                    else:
                        cursor.execute(
                            """
                            INSERT INTO blackmarket_inventory (guild_id, item_key, current_stock, sale_discount, updated_at)
                            VALUES (%s, %s, %s, 0, CURRENT_TIMESTAMP)
                            ON CONFLICT (guild_id, item_key) DO UPDATE SET
                                current_stock = EXCLUDED.current_stock,
                                updated_at = CURRENT_TIMESTAMP
                            """,
                            (guild_id, item_key, stock),
                        )
                    return {"ok": True, "item_key": item_key, "stock": stock, "discount": discount}
        finally:
            self._pool.putconn(conn)

    def set_inventory_stock(self, guild_id: int, role_name: str, stock: int) -> int:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO shop_inventory (guild_id, role_name, current_stock, updated_at)
                        VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
                        ON CONFLICT (guild_id, role_name) DO UPDATE SET
                            current_stock = EXCLUDED.current_stock,
                            updated_at = CURRENT_TIMESTAMP
                        RETURNING current_stock
                        """,
                        (guild_id, role_name, stock),
                    )
                    return cursor.fetchone()[0]
        finally:
            self._pool.putconn(conn)

    def buy_item(self, guild_id: int, user_id: int, role_name: str, price: int) -> bool:
        """Attempts to purchase an item, reducing stock and credits if successful. Returns True if purchased."""
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT credits FROM member_economy WHERE guild_id = %s AND user_id = %s FOR UPDATE",
                        (guild_id, user_id),
                    )
                    bal_row = cursor.fetchone()
                    if not bal_row or bal_row[0] < price:
                        return False

                    cursor.execute(
                        "SELECT current_stock FROM shop_inventory WHERE guild_id = %s AND role_name = %s FOR UPDATE",
                        (guild_id, role_name),
                    )
                    stock_row = cursor.fetchone()
                    if not stock_row or stock_row[0] <= 0:
                        return False

                    cursor.execute(
                        """
                        INSERT INTO member_purchases (guild_id, user_id, role_name, purchased_at)
                        VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
                        ON CONFLICT DO NOTHING
                        RETURNING role_name
                        """,
                        (guild_id, user_id, role_name),
                    )
                    if cursor.fetchone() is None:
                        return False
                    cursor.execute(
                        """
                        UPDATE member_economy
                        SET credits = credits - %s,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE guild_id = %s AND user_id = %s
                        """,
                        (price, guild_id, user_id),
                    )
                    cursor.execute(
                        """
                        UPDATE shop_inventory
                        SET current_stock = current_stock - 1,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE guild_id = %s AND role_name = %s
                        """,
                        (guild_id, role_name),
                    )
                    return True
        finally:
            self._pool.putconn(conn)

    def get_purchased_roles(self, guild_id: int, user_id: int) -> list[str]:
        conn = self._connect()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT role_name FROM member_purchases WHERE guild_id = %s AND user_id = %s",
                    (guild_id, user_id),
                )
                return [row[0] for row in cursor.fetchall()]
        finally:
            self._pool.putconn(conn)

    def get_guild_purchased_roles(self, guild_id: int) -> dict[int, list[str]]:
        conn = self._connect()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT user_id, role_name
                    FROM member_purchases
                    WHERE guild_id = %s
                    ORDER BY user_id, purchased_at ASC
                    """,
                    (guild_id,),
                )
                purchased: dict[int, list[str]] = {}
                for row in cursor.fetchall():
                    purchased.setdefault(int(row["user_id"]), []).append(
                        str(row["role_name"])
                    )
                return purchased
        finally:
            self._pool.putconn(conn)

    def buy_economy_item(
        self, guild_id: int, user_id: int, item_key: str, amount: int = 1
    ) -> dict[str, Any]:
        if item_key not in ECONOMY_ITEM_DEFS:
            return {"ok": False, "reason": "invalid_item"}
        amount = max(1, int(amount))
        base_price = int(ECONOMY_ITEM_DEFS[item_key]["price"])

        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    # check stock and discount
                    cursor.execute(
                        "SELECT current_stock, sale_discount FROM blackmarket_inventory WHERE guild_id = %s AND item_key = %s FOR UPDATE",
                        (guild_id, item_key),
                    )
                    inv_row = cursor.fetchone()
                    stock = inv_row["current_stock"] if inv_row else 0
                    discount = inv_row["sale_discount"] if inv_row else 0

                    if stock < amount:
                        return {"ok": False, "reason": "stock", "stock": stock}

                    price_per_item = int(base_price * (1 - discount / 100))
                    total_price = price_per_item * amount

                    cursor.execute(
                        "SELECT credits FROM member_economy WHERE guild_id = %s AND user_id = %s FOR UPDATE",
                        (guild_id, user_id),
                    )
                    row = cursor.fetchone()
                    balance = int(row["credits"]) if row else 0
                    if balance < total_price:
                        return {
                            "ok": False,
                            "reason": "insufficient",
                            "balance": balance,
                            "price": total_price,
                        }

                    # deduct balance
                    cursor.execute(
                        "UPDATE member_economy SET credits = credits - %s, updated_at = CURRENT_TIMESTAMP WHERE guild_id = %s AND user_id = %s",
                        (total_price, guild_id, user_id),
                    )
                    # deduct stock
                    cursor.execute(
                        "UPDATE blackmarket_inventory SET current_stock = current_stock - %s, updated_at = CURRENT_TIMESTAMP WHERE guild_id = %s AND item_key = %s",
                        (amount, guild_id, item_key),
                    )

                    cursor.execute(
                        """
                        INSERT INTO economy_items (guild_id, user_id, item_key, quantity, updated_at)
                        VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
                        ON CONFLICT (guild_id, user_id, item_key) DO UPDATE SET
                            quantity = economy_items.quantity + EXCLUDED.quantity,
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
                        "balance": balance - total_price,
                    }
        finally:
            self._pool.putconn(conn)

class ShopCog(commands.Cog):
    async def cog_load(self):
        self._shop_channel_notice_times = {}
        self.refresh_shop_task.start()

    def cog_unload(self):
        self.refresh_shop_task.cancel()

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

    async def _ensure_shop_role(
        self, guild: discord.Guild, item_name: str
    ) -> Optional[discord.Role]:
        role = discord.utils.get(guild.roles, name=item_name)
        if role is not None:
            return role

        bot_member = guild.me
        if bot_member is None and self.bot.user is not None:
            bot_member = guild.get_member(self.bot.user.id)
        if bot_member is None or not bot_member.guild_permissions.manage_roles:
            LOGGER.warning(
                "Missing Manage Roles permission to create shop role %s in %s",
                item_name,
                guild.id,
            )
            return None

        item = SHOP_ITEMS.get(item_name, {})
        color = int(item.get("color", DEFAULT_ECONOMY_ACCENT))
        try:
            return await guild.create_role(
                name=item_name,
                colour=discord.Colour(color),
                mentionable=False,
                reason="Create missing economy shop role",
            )
        except discord.Forbidden:
            LOGGER.warning("Forbidden creating shop role %s in %s", item_name, guild.id)
        except discord.HTTPException as exc:
            LOGGER.warning(
                "Failed to create shop role %s in %s: %s", item_name, guild.id, exc
            )
        return None

    async def _ensure_shop_roles(
        self,
        guild: discord.Guild,
        item_names: Iterable[str],
    ) -> dict[str, Optional[discord.Role]]:
        roles: dict[str, Optional[discord.Role]] = {}
        for item_name in item_names:
            roles[item_name] = await self._ensure_shop_role(guild, item_name)
        return roles

    async def _require_shop_channel(self, ctx: commands.Context) -> bool:
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return False

        config = await asyncio.to_thread(self.store.get_guild_config, ctx.guild.id)
        shop_channel_id = _coerce_optional_int(config.get("shop_channel_id"))
        bypass_role_id = _coerce_optional_int(config.get("shop_bypass_role_id"))
        if shop_channel_id is None:
            return True
        if (
            bypass_role_id is not None
            and isinstance(ctx.author, discord.Member)
            and any(role.id == bypass_role_id for role in ctx.author.roles)
        ):
            return True

        channel_ids = {ctx.channel.id}
        parent_id = getattr(ctx.channel, "parent_id", None)
        if isinstance(parent_id, int):
            channel_ids.add(parent_id)

        if shop_channel_id in channel_ids:
            return True

        delete_later = getattr(self.bot, "_delete_later", None)
        if callable(delete_later):
            asyncio.create_task(delete_later(ctx.message, 30))

        now = monotonic()
        notice_key = (ctx.guild.id, ctx.channel.id, shop_channel_id)
        last_notice = self._shop_channel_notice_times.get(notice_key, 0.0)
        if now - last_notice >= SHOP_CHANNEL_NOTICE_SECONDS:
            self._shop_channel_notice_times[notice_key] = now
            reply = await ctx.send(f"Use this command in <#{shop_channel_id}>.")
            if callable(delete_later):
                asyncio.create_task(delete_later(reply, SHOP_CHANNEL_NOTICE_SECONDS))

        if len(self._shop_channel_notice_times) > 512:
            cutoff = now - (SHOP_CHANNEL_NOTICE_SECONDS * 4)
            self._shop_channel_notice_times = {
                key: timestamp
                for key, timestamp in self._shop_channel_notice_times.items()
                if timestamp >= cutoff
            }
        return False

    @tasks.loop(time=SHOP_REFRESH_TIMES)
    async def refresh_shop_task(self):
        refreshed_at = _shop_refresh_period_start()
        for guild in self.bot.guilds:
            try:
                await asyncio.to_thread(
                    self.store.refresh_inventory, guild.id, SHOP_ITEMS, refreshed_at
                )
                LOGGER.info("Refreshed shop inventory for guild %s", guild.id)
            except Exception as exc:
                LOGGER.error("Failed to refresh shop for guild %s: %s", guild.id, exc)

    @refresh_shop_task.before_loop
    async def before_refresh_shop(self):
        try:
            await self.bot.wait_until_ready()
        except RuntimeError:
            self.refresh_shop_task.cancel()

    async def _remove_big_reset_shop_roles(
        self, guild: discord.Guild
    ) -> dict[str, int]:
        purchased = await asyncio.to_thread(
            self.store.get_guild_purchased_roles, guild.id
        )
        stats = {
            "purchase_records": sum(len(roles) for roles in purchased.values()),
            "members_scanned": 0,
            "member_scan_failed": 0,
            "members_missing": 0,
            "members_failed": 0,
            "roles_missing": 0,
            "roles_unmanageable": 0,
            "roles_removed": 0,
            "roles_failed": 0,
            "permission_blocked": 0,
        }
        bot_member = guild.me
        if bot_member is None and self.bot.user is not None:
            bot_member = guild.get_member(self.bot.user.id)
        if bot_member is None or not bot_member.guild_permissions.manage_roles:
            stats["permission_blocked"] = 1
            return stats

        role_names_by_user: dict[int, set[str]] = {
            user_id: set(role_names) for user_id, role_names in purchased.items()
        }
        shop_roles = {
            role_name: discord.utils.get(guild.roles, name=role_name)
            for role_name in SHOP_ITEMS
        }

        try:
            await guild.chunk(cache=True)
        except (asyncio.TimeoutError, discord.Forbidden, discord.HTTPException):
            stats["member_scan_failed"] = 1

        stats["members_scanned"] = len(guild.members)
        for member in guild.members:
            if member.bot:
                continue
            for role_name, role in shop_roles.items():
                if role is not None and role in member.roles:
                    role_names_by_user.setdefault(member.id, set()).add(role_name)

        for user_id, role_names in role_names_by_user.items():
            member = guild.get_member(user_id)
            if member is None:
                try:
                    member = await guild.fetch_member(user_id)
                except discord.NotFound:
                    stats["members_missing"] += 1
                    continue
                except (discord.Forbidden, discord.HTTPException):
                    stats["members_failed"] += 1
                    continue

            roles_to_remove: list[discord.Role] = []
            for role_name in sorted(role_names):
                role = shop_roles.get(role_name)
                if role is None:
                    stats["roles_missing"] += 1
                    continue
                if role not in member.roles:
                    continue
                if role >= bot_member.top_role:
                    stats["roles_unmanageable"] += 1
                    continue
                roles_to_remove.append(role)

            if not roles_to_remove:
                continue

            try:
                await member.remove_roles(*roles_to_remove, reason="The Big Reset")
            except discord.Forbidden:
                stats["roles_failed"] += len(roles_to_remove)
            except discord.HTTPException:
                stats["roles_failed"] += len(roles_to_remove)
            else:
                stats["roles_removed"] += len(roles_to_remove)

        return stats

    @commands.command(name="shop", help="View the role shop")
    async def shop(self, ctx: commands.Context):
        femboy = await self._shop_femboy_mode(ctx.guild)

        inventory, last_refresh = await asyncio.gather(
            asyncio.to_thread(self.store.get_inventory, ctx.guild.id),
            asyncio.to_thread(self.store.get_last_shop_refresh, ctx.guild.id),
        )
        now = datetime.now(timezone.utc)
        if not inventory or _shop_refresh_due(last_refresh, now):
            await asyncio.to_thread(
                self.store.refresh_inventory,
                ctx.guild.id,
                SHOP_ITEMS,
                _shop_refresh_period_start(now),
            )
            inventory, last_refresh = await asyncio.gather(
                asyncio.to_thread(self.store.get_inventory, ctx.guild.id),
                asyncio.to_thread(self.store.get_last_shop_refresh, ctx.guild.id),
            )
        shop_roles = await self._ensure_shop_roles(ctx.guild, SHOP_ITEMS.keys())

        balance, config = await asyncio.gather(
            asyncio.to_thread(self.store.get_balance, ctx.guild.id, ctx.author.id),
            asyncio.to_thread(self.store.get_guild_config, ctx.guild.id),
        )
        shop_channel_id = _coerce_optional_int(config.get("shop_channel_id"))
        command_note = (
            (
                f"\nEconomy commands stay in <#{shop_channel_id}>, cutie~"
                if femboy
                else f"\nEconomy commands stay in <#{shop_channel_id}>."
            )
            if shop_channel_id
            else ""
        )
        embed = discord.Embed(
            title=(
                f"{SHOP_ICON} SOUL WOWE SHOP UwU"
                if femboy
                else f"{SHOP_ICON} SOUL ROLE SHOP"
            ),
            description=(
                (
                    f"**{ctx.author.display_name}**, chu have **{balance:,} cwedities**.\n"
                    "Chat to earn cwedities, then buy a cute wowe with `.buy <role name>`.\n"
                    f"Onwy wowes in stockies awe listed.{command_note}"
                )
                if femboy
                else (
                    f"**{ctx.author.display_name}**, you have **{balance:,} credits**.\n"
                    "Chat to earn credits, then buy access with `.buy <role name>`.\n"
                    f"Only roles currently in stock are listed.{command_note}"
                )
            ),
            color=0xFF4D8D,
            timestamp=datetime.now(timezone.utc),
        )
        if ctx.guild.icon:
            embed.set_author(name=ctx.guild.name, icon_url=ctx.guild.icon.url)
            embed.set_thumbnail(url=ctx.guild.icon.url)
        else:
            embed.set_author(name=ctx.guild.name)

        sorted_items = [
            (name, data)
            for name, data in sorted(SHOP_ITEMS.items(), key=lambda x: x[1]["price"])
            if inventory.get(name, 0) > 0
        ]

        if not sorted_items:
            empty_value = (
                f"Da shop is empty wight now. Westock: {_format_restock_countdown(last_refresh)}."
                if femboy
                else f"The shop is empty right now. Restock: {_format_restock_countdown(last_refresh)}."
            )
            embed.add_field(
                name="No wowes in stockies" if femboy else "No roles in stock",
                value=empty_value,
                inline=False,
            )

        for name, data in sorted_items:
            stock = inventory.get(name, 0)
            max_stock = data["max_stock"]
            rarity = data["rarity"]
            badge = RARITY_BADGES.get(rarity, "\U0001f539")
            role = shop_roles.get(name) or discord.utils.get(ctx.guild.roles, name=name)
            role_label = role.mention if role else f"`{name}`"
            stock_line = f"{AVAILABLE_ICON} `{_format_stock_bar(stock, max_stock)}` **{stock}/{max_stock}** left"
            perk = SHOP_ROLE_PERKS.get(name)
            perk_line = (
                f"{'Pewk' if femboy else 'Perk'}: **{perk['label']}**\n"
                if perk
                else ""
            )

            embed.add_field(
                name=f"{badge} {name}",
                value=(
                    f"{'Wowe' if femboy else 'Role'}: {role_label}\n"
                    f"{'Wawity' if femboy else 'Rarity'}: **{rarity.upper()}**\n"
                    f"{perk_line}"
                    f"{'Pwice' if femboy else 'Price'}: **{data['price']:,}** {COIN_ICON} credits\n"
                    f"{STOCK_ICON} {'Stockies' if femboy else 'Stock'}: {stock_line}\n"
                    f"{'Buyy' if femboy else 'Buy'}: `.buy {name}`"
                ),
                inline=False,
            )

        bg_url = await asyncio.to_thread(self.store.get_shop_background, ctx.guild.id)
        if bg_url:
            embed.set_image(url=bg_url)

        embed.description = (
            embed.description or ""
        ) + f"\n\n{'Westock' if femboy else 'Restock'}: {_format_restock_countdown(last_refresh)}"
        embed.set_footer(
            text=(
                "Use .bal to check youw cwedities, cutie~"
                if femboy
                else "Use .bal to check your balance"
            )
        )
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @commands.command(
        name="shopedit",
        help="Admin only. Customize the shop thumbnail image. Usage: .shopedit [url/reset] or attach an image",
    )
    @economy_admin_only()
    async def shopedit(self, ctx: commands.Context, *, arg: str = ""):
        if ctx.guild is None:
            return

        reset = arg.lower() == "reset"
        background_url = None

        if ctx.message.attachments:
            background_url = ctx.message.attachments[0].url
        elif arg and not reset:
            background_url = arg

        if reset:
            await asyncio.to_thread(self.store.set_shop_background, ctx.guild.id, None)
            await ctx.send("Shop background reset to default.")
            return

        if not background_url:
            await ctx.send(
                "Add a background attachment, a background URL, or type `reset`."
            )
            return

        try:
            saved_background_url = _validate_image_url(background_url)
            msg = await ctx.send("Validating image...")
            image_bytes = await asyncio.to_thread(
                _download_image_bytes, saved_background_url
            )
            await asyncio.to_thread(_verify_image_bytes, image_bytes)
            await msg.delete()

            await asyncio.to_thread(
                self.store.set_shop_background, ctx.guild.id, saved_background_url
            )
            await ctx.send(
                "Shop background updated successfully! Type `.shop` to see it."
            )
        except Exception as exc:
            await ctx.send(str(exc))

    @commands.command(
        name="ecoadmin_inv",
        aliases=["ecoadmin_inventory", "ecoadmin_stocklist"],
        help="Show current stock for every shop role.",
    )
    @economy_admin_only()
    async def ecoadmin_inventory(self, ctx: commands.Context):
        inventory = await asyncio.to_thread(self.store.get_inventory, ctx.guild.id)
        if not inventory:
            await asyncio.to_thread(
                self.store.refresh_inventory, ctx.guild.id, SHOP_ITEMS
            )
            inventory = await asyncio.to_thread(self.store.get_inventory, ctx.guild.id)

        embed = discord.Embed(title="Shop Inventory", color=0xFF4D8D)
        for name, data in sorted(SHOP_ITEMS.items(), key=lambda item: item[1]["price"]):
            stock = max(0, int(inventory.get(name, 0)))
            max_stock = int(data["max_stock"])
            embed.add_field(
                name=f"{RARITY_BADGES.get(data['rarity'], '🔹')} {name}",
                value=f"`{_format_stock_bar(stock, max_stock)}` **{stock}/{max_stock}**",
                inline=False,
            )
        embed.set_footer(text="Use .eco stock <amount> <role name> to edit one role.")
        await send_v2(ctx, embed)

    @commands.command(name="buy", help="Buy a role from the shop")
    async def buy(self, ctx: commands.Context, *, role_name: str):
        femboy = await self._shop_femboy_mode(ctx.guild)
        # Case insensitive match
        matched_name = _match_shop_item_name(role_name)

        if not matched_name:
            return await self._buy_from_any_shop(ctx, role_name)

        data = SHOP_ITEMS[matched_name]
        price = data["price"]

        # Check if they already have the role
        role = await self._ensure_shop_role(ctx.guild, matched_name)
        if not role:
            await ctx.send(
                (
                    f"I need **Manage Roles** pewmission to make `{matched_name}` befowe chu can buy it~"
                    if femboy
                    else f"I need the **Manage Roles** permission to create `{matched_name}` before it can be bought."
                )
            )
            return

        if role in ctx.author.roles:
            await asyncio.to_thread(
                self.store.set_role_perk,
                ctx.guild.id,
                ctx.author.id,
                matched_name,
                True,
            )
            perk = SHOP_ROLE_PERKS.get(matched_name)
            perk_note = (
                f"\n{'Pewk' if femboy else 'Perk'} active: **{perk['label']}**"
                if perk
                else ""
            )
            await ctx.send(
                (
                    f"Chu awweady own {role.mention}. I synced its economy pewk UwU~{perk_note}"
                    if femboy
                    else f"You already own the {role.mention} role. I synced its economy perk.{perk_note}"
                )
            )
            return

        # Attempt purchase
        msg = await ctx.send(
            "Pwocessing twansaction... UwU~" if femboy else "Processing transaction..."
        )
        success = await asyncio.to_thread(
            self.store.buy_item, ctx.guild.id, ctx.author.id, matched_name, price
        )

        if not success:
            await msg.edit(
                content=(
                    f"Twansaction failed. Chu need **{price:,}** credits or da item is out of stockies~"
                    if femboy
                    else f"Transaction failed. You either don't have enough credits (**{price:,}** needed) or the item is out of stock!"
                )
            )
            return

        # Add role
        bot_member = ctx.guild.me
        if role >= bot_member.top_role:
            # Refund
            await asyncio.to_thread(
                self.store.add_credits, ctx.guild.id, ctx.author.id, price
            )
            await asyncio.to_thread(
                self.store.set_role_perk,
                ctx.guild.id,
                ctx.author.id,
                matched_name,
                False,
            )
            await msg.edit(
                content=(
                    "I can't assign dis wowe because it's highew than my top wowe. I refunded youw cwedities~"
                    if femboy
                    else "I cannot assign this role because it is higher than my top role. I have refunded your credits."
                )
            )
            return

        try:
            await ctx.author.add_roles(role, reason="Purchased from shop")
            perk = SHOP_ROLE_PERKS.get(matched_name)
            perk_note = (
                f"\n{'Pewk' if femboy else 'Perk'} active: **{perk['label']}**"
                if perk
                else ""
            )
            await msg.edit(
                content=(
                    f"Purchase successfuw! Chu bought {role.mention} for **{price:,}** credits UwU~{perk_note}"
                    if femboy
                    else f"Purchase successful! You bought {role.mention} for **{price:,}** credits.{perk_note}"
                )
            )
        except discord.Forbidden:
            # Refund
            await asyncio.to_thread(
                self.store.add_credits, ctx.guild.id, ctx.author.id, price
            )
            await asyncio.to_thread(
                self.store.set_role_perk,
                ctx.guild.id,
                ctx.author.id,
                matched_name,
                False,
            )
            await msg.edit(
                content=(
                    "I don't have pewmissions to assign dis wowe. I refunded youw cwedities~"
                    if femboy
                    else "I lack permissions to assign this role. I have refunded your credits."
                )
            )

    def _blackmarket_description(self, inventory: Dict[str, Dict[str, int]]) -> str:
        tool_lines = []
        for item_key, data in ECONOMY_ITEM_DEFS.items():
            inv = inventory.get(item_key, {"stock": 0, "discount": 0})
            stock = inv["stock"]
            discount = inv["discount"]
            price = int(data["price"])
            sale_price = int(price * (1 - discount / 100))

            price_text = (
                f"~~{price:,}~~ **{sale_price:,} cr** (-{discount}%)"
                if discount > 0
                else f"`{price:,} cr`"
            )
            stock_text = f" - {stock}/{data.get('max_stock', 100)} left"
            if stock == 0:
                stock_text = " - **OUT OF STOCK**"

            tool_lines.append(
                f"**{data['name']}** - {price_text}{stock_text}\n*{data.get('desc', '')}*"
            )

        lock_lines = []
        for level, data in LOCK_LEVELS.items():
            if level == 0:
                continue
            item_key = f"lock_{level}"
            inv = inventory.get(item_key, {"stock": 0, "discount": 0})
            stock = inv["stock"]
            discount = inv["discount"]
            price = int(data["price"])
            sale_price = int(price * (1 - discount / 100))

            price_text = (
                f"~~{price:,}~~ **{sale_price:,} cr** (-{discount}%)"
                if discount > 0
                else f"`{price:,} cr`"
            )
            stock_text = f" - {stock}/{data.get('max_stock', 10)} left"
            if stock == 0:
                stock_text = " - **OUT OF STOCK**"

            lock_lines.append(
                f"**{data['name']}** - {price_text}{stock_text}\n*{data.get('desc', '')}*"
            )

        return (
            "Buy robbery tools for heists and install wallet locks that lower `.rob` success odds.\n\n"
            f"**Tools**\n{chr(10).join(tool_lines)}\n\n"
            f"**Locks**\n{chr(10).join(lock_lines)}\n\n"
            "*How to buy: `.buyblackmarket <item> [amount]` (alias: `.buybm`). Locks can only be purchased one at a time.*"
        )

    @commands.command(
        name="buyblackmarket",
        aliases=["buybm", "bmbuy"],
        help="Buy a tool or lock from the black market.",
    )
    async def buyblackmarket_cmd(
        self, ctx: commands.Context, *, purchase: str = ""
    ):
        return await self._buy_blackmarket_item(
            ctx,
            purchase,
            usage=".buyblackmarket <item> [amount]",
        )

    async def _buy_blackmarket_item(
        self, ctx: commands.Context, purchase: str, *, usage: str
    ) -> None:
        femboy = await self._shop_femboy_mode(ctx.guild)
        raw_purchase = " ".join(purchase.split())
        if not raw_purchase:
            return await ctx.send(
                f"Use `{usage}`, cutie~" if femboy else f"Usage: `{usage}`"
            )

        amount = 1
        item_name = raw_purchase
        suffix_amount_match = re.fullmatch(r"(.+?)\s+(\d+)", raw_purchase)
        prefix_amount_match = re.fullmatch(r"(\d+)\s+(.+)", raw_purchase)
        amount_match = suffix_amount_match or prefix_amount_match
        if amount_match:
            if prefix_amount_match:
                amount_text = prefix_amount_match.group(1)
                item_name = prefix_amount_match.group(2)
            else:
                item_name = suffix_amount_match.group(1)
                amount_text = suffix_amount_match.group(2)
            if len(amount_text) > 3:
                return await ctx.send("You can buy at most **100** items at once.")
            amount = int(amount_text)
        if amount < 1 or amount > 100:
            return await ctx.send("You can buy between **1** and **100** items at once.")

        normalized_name = " ".join(item_name.split()).casefold().replace("_", " ")
        lock_level = next(
            (
                level
                for level, data in LOCK_LEVELS.items()
                if level > 0 and normalized_name == data["name"].casefold()
            ),
            None,
        )
        tool_key = _match_economy_item(item_name)
        if tool_key not in BlackMarketView.TOOL_ORDER:
            tool_key = None

        if lock_level is None and tool_key is None:
            return await ctx.send(
                "That item is not sold in the black market. Use `.blackmarket` to view the current inventory."
            )

        if lock_level is not None:
            if amount != 1:
                return await ctx.send("Locks can only be purchased one at a time.")
            result = await asyncio.to_thread(
                self.store.buy_security_lock,
                ctx.guild.id,
                ctx.author.id,
                lock_level,
            )
            item_label = LOCK_LEVELS[lock_level]["name"]
        else:
            result = await asyncio.to_thread(
                self.store.buy_economy_item,
                ctx.guild.id,
                ctx.author.id,
                tool_key,
                amount,
            )
            item_label = ECONOMY_ITEM_DEFS[tool_key]["name"]

        if not result.get("ok"):
            reason = result.get("reason")
            if reason == "stock":
                return await ctx.send(
                    f"Not enough **{item_label}** stock. Available: **{int(result.get('stock', 0)):,}**."
                )
            if reason == "insufficient":
                return await ctx.send(
                    f"You need **{int(result.get('price', 0)):,} cr** but only have **{int(result.get('balance', 0)):,} cr**."
                )
            if reason == "owned":
                current_level = int(result.get("lock_level", 0))
                current_lock = LOCK_LEVELS.get(current_level, LOCK_LEVELS[0])["name"]
                return await ctx.send(
                    f"You already own **{current_lock}** or better."
                )
            return await ctx.send("The black market purchase could not be completed.")

        purchased_amount = 1 if lock_level is not None else amount
        await ctx.send(
            f"Bought **{purchased_amount:,}x {item_label}** for **{int(result['price']):,} cr**. "
            f"Wallet: **{int(result['balance']):,} cr**."
        )

    async def _buy_from_any_shop(
        self, ctx: commands.Context, purchase: str
    ) -> None:
        requests = _split_purchase_requests(purchase)
        if not requests:
            return await ctx.send("Usage: .buy <amount> <item> [, <amount> <item>].")
        for request in requests:
            await self._buy_single_from_any_shop(ctx, request)

    async def _buy_single_from_any_shop(
        self, ctx: commands.Context, purchase: str
    ) -> None:
        from .core import (
            DEFENSE_EFFECTS,
            GEAR_SHOP_ITEMS,
            LUCK_EFFECTS,
            UTILITY_SHOP_ITEMS,
        )
        from .pets import PET_CORE_SHOP, _match_pet_item, _match_pet_line

        item_name, amount = _parse_purchase_request(purchase)
        if amount < 1 or amount > 100:
            return await ctx.send("You can buy between **1** and **100** items at once.")

        item_name_variants = _purchase_name_variants(item_name)
        if not item_name_variants:
            return await ctx.send("Specify an item to buy.")
        normalized_names = [
            name.casefold().replace("_", " ") for name in item_name_variants
        ]
        lock_level = next(
            (
                level
                for level, data in LOCK_LEVELS.items()
                if level > 0 and data["name"].casefold() in normalized_names
            ),
            None,
        )
        tool_key = next(
            (
                key
                for candidate in item_name_variants
                if (key := _match_economy_item(candidate)) is not None
            ),
            None,
        )
        if lock_level is not None or tool_key in BlackMarketView.TOOL_ORDER:
            return await self._buy_blackmarket_item(
                ctx,
                f"{item_name_variants[-1]} {amount}",
                usage=".buy <item> [amount]",
            )

        economy_system = getattr(self.bot, "economy_system", None)
        core_cog = getattr(economy_system, "core_cog", None)
        pets_cog = getattr(economy_system, "pets_cog", None)
        if core_cog is None or pets_cog is None:
            return await ctx.send("The economy system is still starting. Try again in a moment.")

        pet_item_key = next(
            (
                key
                for candidate in item_name_variants
                if (key := _match_pet_item(candidate)) in PET_CORE_SHOP
            ),
            None,
        )
        if pet_item_key:
            return await pets_cog.pet_cores_subcommand.callback(
                pets_cog,
                ctx,
                action="buy",
                item=pet_item_key,
                amount=str(amount),
            )

        pet_key = next(
            (
                key
                for candidate in item_name_variants
                if (key := _match_pet_line(candidate)) is not None
            ),
            None,
        )
        if pet_key:
            return await pets_cog._purchase_pets(ctx, pet_key, amount)

        gear_key = next(
            (
                key
                for candidate in item_name_variants
                if (key := core_cog._match_item_key(candidate, GEAR_SHOP_ITEMS.keys()))
                is not None
            ),
            None,
        )
        if gear_key:
            if amount != 1:
                return await ctx.send("Gear can only be purchased one item at a time.")
            return await core_cog.buygear_cmd.callback(core_cog, ctx, item=gear_key)

        potion_keys = (*LUCK_EFFECTS.keys(), *DEFENSE_EFFECTS.keys())
        potion_key = next(
            (
                key
                for candidate in item_name_variants
                if (key := core_cog._match_item_key(candidate, potion_keys)) is not None
            ),
            None,
        )
        if potion_key:
            if amount != 1:
                return await ctx.send("Potions can only be purchased one at a time.")
            return await core_cog.buypotion_cmd.callback(core_cog, ctx, item=potion_key)

        utility_key = next(
            (
                key
                for candidate in item_name_variants
                if (key := core_cog._match_item_key(candidate, UTILITY_SHOP_ITEMS.keys()))
                is not None
            ),
            None,
        )
        if utility_key:
            if amount != 1:
                return await ctx.send("Utility items can only be purchased one at a time.")
            return await core_cog.buyutility_cmd.callback(core_cog, ctx, item=utility_key)

        await ctx.send(
            "That item is not sold in any shop. Use .shop, .blackmarket, .gearshop, .utilityshop, .petshop, or .pet cores."
        )

    @commands.command(
        name="blackmarket",
        aliases=["bm", "toolshop"],
        help="View the black market inventory.",
    )
    async def blackmarket_cmd(self, ctx: commands.Context):
        inventory, last_refresh = await asyncio.gather(
            asyncio.to_thread(self.store.get_blackmarket_inventory, ctx.guild.id),
            asyncio.to_thread(self.store.get_last_blackmarket_refresh, ctx.guild.id),
        )
        now = datetime.now(timezone.utc)
        if not inventory or _shop_refresh_due(last_refresh, now):
            await asyncio.to_thread(
                self.store.refresh_blackmarket_inventory,
                ctx.guild.id,
                ECONOMY_ITEM_DEFS,
                LOCK_LEVELS,
                _shop_refresh_period_start(now),
            )
            inventory, last_refresh = await asyncio.gather(
                asyncio.to_thread(self.store.get_blackmarket_inventory, ctx.guild.id),
                asyncio.to_thread(
                    self.store.get_last_blackmarket_refresh, ctx.guild.id
                ),
            )

        balance, security = await asyncio.gather(
            asyncio.to_thread(self.store.get_balance, ctx.guild.id, ctx.author.id),
            asyncio.to_thread(
                self.store.get_member_security, ctx.guild.id, ctx.author.id
            ),
        )
        femboy = await self._shop_femboy_mode(ctx.guild)
        view = BlackMarketView(
            inventory, balance, security, last_refresh, femboy
        )
        await ctx.send(view=view, allowed_mentions=discord.AllowedMentions.none())

