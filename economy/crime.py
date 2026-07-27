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




class HeistRunView(discord.ui.LayoutView):
    STEPS = {
        "jewelry": [
            ("Scout", "Case the display room."),
            ("Pick Lock", "Open the jewelry cases."),
            ("Grab Loot", "Take the cleanest stones."),
            ("Escape", "Run the exit plan."),
        ],
        "bank": [
            ("Scout", "Map the guards."),
            ("Bypass Lock", "Open the staff entrance."),
            ("Crack Vault", "Burn through the vault door."),
            ("Escape", "Get out before sirens close in."),
        ],
        "team_bank": [
            ("Scout", "Assign the crew."),
            ("Bypass Lock", "Open the side entrance."),
            ("Crack Vault", "Drill the vault."),
            ("Escape", "Split up and leave."),
        ],
    }

    def __init__(
        self,
        cog: Any,
        guild: discord.Guild,
        leader_id: int,
        participant_ids: list[int],
        heist_key: str,
    ):
        super().__init__(timeout=180)
        self.cog = cog
        self.guild = guild
        self.leader_id = leader_id
        self.participant_ids = participant_ids
        self.heist_key = heist_key
        self.step_index = 0
        self.heat = 0
        self.step_log: list[str] = []
        self.finished = False
        self.result: Optional[dict[str, Any]] = None
        self.error_text: Optional[str] = None
        self.render()

    def crew_ids(self) -> list[int]:
        seen: list[int] = []
        for user_id in [self.leader_id, *self.participant_ids]:
            if user_id not in seen:
                seen.append(user_id)
        return seen

    def _crew_text(self) -> str:
        crew = []
        for user_id in self.crew_ids():
            member = self.guild.get_member(user_id)
            crew.append(member.mention if member else f"`{user_id}`")
        return ", ".join(crew)

    def _format_required_items(self, items: dict[str, int]) -> str:
        if not items:
            return "None"
        lines = []
        for item_key, qty in items.items():
            item_name = ECONOMY_ITEM_DEFS.get(item_key, {}).get(
                "name", item_key.replace("_", " ").title()
            )
            lines.append(f"{int(qty)}x {item_name}")
        return ", ".join(lines)

    def _requirements_text(self) -> str:
        heist = HEIST_DEFS[self.heist_key]
        cooldown_hours = int(ROBBERY_COOLDOWNS[self.heist_key].total_seconds() // 3600)
        base_items = self._format_required_items(dict(heist.get("items", {})))
        if self.heist_key == "team_bank":
            leader_items = self._format_required_items(
                dict(heist.get("leader_items", {}))
            )
            return (
                f"Each member: **{base_items}**\n"
                f"Leader extra: **{leader_items}**\n"
                f"Cooldown: **{cooldown_hours}h** per crew member\n"
                "Heat lowers final odds by **7%** per level."
            )
        return (
            f"Required: **{base_items}**\n"
            f"Cooldown: **{cooldown_hours}h**\n"
            "Heat lowers final odds by **7%** per level."
        )

    def _description(self) -> str:
        total_steps = len(self.STEPS[self.heist_key])
        parts = [
            f"Crew: {self._crew_text()}",
            f"Progress: **{self.step_index}/{total_steps}**",
            f"Heat: **{self.heat}/5**",
            f"\n**Requirements**\n{self._requirements_text()}",
        ]
        if self.error_text:
            parts.append(f"\n**Heist blocked**\n{self.error_text}")
        elif self.result:
            if self.result.get("success"):
                parts.append(
                    "\n**Success**\n"
                    f"Total payout **{int(self.result['payout']):,} cr**\n"
                    f"Each member got **{int(self.result['split']):,} cr**."
                )
            else:
                parts.append(
                    "\n**Failed**\n"
                    f"Fine **{int(self.result['fine']):,} cr** total\n"
                    f"Each member paid up to **{int(self.result['fine_each']):,} cr**\n"
                    f"Jail time: **{int(self.result['jail_minutes'])} min**."
                )
            parts.append(
                f"*Success chance was {float(self.result['success_chance']) * 100:.0f}%.*"
            )
        else:
            current = self.STEPS[self.heist_key][
                min(self.step_index, len(self.STEPS[self.heist_key]) - 1)
            ]
            step_risk = HEIST_STEP_RISKS[self.heist_key][
                min(self.step_index, len(HEIST_STEP_RISKS[self.heist_key]) - 1)
            ]
            parts.append(
                f"\n**{current[0]}**\n{current[1]}\nRisk check: **{step_risk * 100:.0f}%** chance to raise heat."
            )
        if self.step_log:
            parts.append("\n**Trail**\n" + "\n".join(self.step_log[-4:]))
        return "\n".join(parts)

    def _accent_color(self) -> int:
        if self.error_text:
            return 0xED4245
        if self.result:
            return 0x2ECC71 if self.result.get("success") else 0xED4245
        return 0xE67E22

    def render(self) -> None:
        self.clear_items()
        heist = HEIST_DEFS[self.heist_key]
        container = branded_panel_container(
            title=heist["title"],
            description=self._description(),
            accent_color=self._accent_color(),
            min_width_chars=None,
        )
        container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
        buttons: list[discord.ui.Button] = []
        for index, (label, _) in enumerate(self.STEPS[self.heist_key]):
            button = discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.success
                if index < self.step_index
                else discord.ButtonStyle.primary
                if index == self.step_index
                else discord.ButtonStyle.secondary,
                disabled=self.finished or index != self.step_index,
            )

            async def step_callback(
                interaction: discord.Interaction, step: int = index
            ) -> None:
                await self.advance(interaction, step)

            button.callback = step_callback
            buttons.append(button)

        container.add_item(discord.ui.ActionRow(*buttons))
        self.add_item(container)
        ensure_layout_view_action_rows(self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id not in self.crew_ids():
            await interaction.response.send_message(
                "You are not in this crew.", ephemeral=True
            )
            return False
        return True

    async def advance(self, interaction: discord.Interaction, step: int) -> None:
        if self.finished:
            return await interaction.response.send_message(
                "This heist is already finished.", ephemeral=True
            )
        if step != self.step_index:
            return await interaction.response.send_message(
                "Follow the heist steps in order.", ephemeral=True
            )
        label = self.STEPS[self.heist_key][step][0]
        step_risk = HEIST_STEP_RISKS[self.heist_key][
            min(step, len(HEIST_STEP_RISKS[self.heist_key]) - 1)
        ]
        if random.random() < min(0.75, step_risk + self.heat * 0.04):
            self.heat = min(5, self.heat + 1)
            self.step_log.append(f"{label}: heat increased to **{self.heat}**.")
        else:
            self.step_log.append(f"{label}: clean.")
        self.step_index += 1
        if self.step_index < len(self.STEPS[self.heist_key]):
            self.render()
            return await interaction.response.edit_message(view=self)

        result = await asyncio.to_thread(
            self.cog.store.execute_heist,
            self.guild.id,
            self.leader_id,
            self.participant_ids,
            self.heist_key,
            self.heat,
        )
        self.finished = True
        if not result.get("ok"):
            reason = result.get("reason")
            if reason == "items":
                lines = []
                for missing in result.get("missing", []):
                    member = self.guild.get_member(int(missing["user_id"]))
                    name = member.mention if member else f"`{missing['user_id']}`"
                    item_name = ECONOMY_ITEM_DEFS[missing["item_key"]]["name"]
                    lines.append(
                        f"{name}: needs **{int(missing['required'])}x {item_name}**, owns **{int(missing['owned'])}**"
                    )
                self.error_text = "\n".join(lines)
            elif reason == "jailed":
                member = self.guild.get_member(int(result["user_id"]))
                name = member.mention if member else f"`{result['user_id']}`"
                self.error_text = f"{name} is jailed until <t:{_discord_timestamp(result['jail_until'])}:R>."
            elif reason == "team_size":
                self.error_text = "Team bank robbery needs at least two crew members."
            elif reason == "cooldown":
                member = self.guild.get_member(int(result["user_id"]))
                name = member.mention if member else f"`{result['user_id']}`"
                self.error_text = f"{name} can run this heist again <t:{_discord_timestamp(result['ready_at'])}:R>."
            else:
                self.error_text = "The heist could not start."
            self.render()
            return await interaction.response.edit_message(view=self)
        self.result = result
        self.render()
        await interaction.response.edit_message(view=self)

class TeamHeistLobbyView(discord.ui.LayoutView):
    def __init__(self, cog: Any, ctx: commands.Context):
        super().__init__(timeout=120)
        self.cog = cog
        self.guild = ctx.guild
        self.leader_id = ctx.author.id
        self.participants: set[int] = {ctx.author.id}
        self.render()

    def _description(self) -> str:
        crew = []
        for user_id in sorted(self.participants):
            member = self.guild.get_member(user_id)
            crew.append(member.mention if member else f"`{user_id}`")
        return (
            f"Crew size: **{len(self.participants)}/4**\n"
            f"{', '.join(crew)}\n\n"
            "**Required**\n"
            "Each member: **2 Lockpicks**\n"
            "Leader: **1 Vault Drill**\n\n"
            f"Cooldown: **{int(ROBBERY_COOLDOWNS['team_bank'].total_seconds() // 3600)}h** per crew member\n\n"
            "*Join the crew, then the leader starts the interactive heist.*"
        )

    def render(self) -> None:
        self.clear_items()
        join_button = discord.ui.Button(
            label="Join Crew", style=discord.ButtonStyle.success
        )
        start_button = discord.ui.Button(
            label="Start Heist", style=discord.ButtonStyle.danger
        )
        kick_button = discord.ui.Button(
            label="Kick Member",
            style=discord.ButtonStyle.secondary,
            disabled=len(self.participants) <= 1,
        )
        join_button.callback = self.join_button
        start_button.callback = self.start_button
        kick_button.callback = self.kick_button
        container = branded_panel_container(
            title="Team Bank Robbery",
            description=self._description(),
            accent_color=0xE67E22,
            min_width_chars=None,
        )
        container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
        container.add_item(discord.ui.ActionRow(join_button, start_button, kick_button))
        self.add_item(container)
        ensure_layout_view_action_rows(self)

    async def join_button(self, interaction: discord.Interaction) -> None:
        if len(self.participants) >= 4 and interaction.user.id not in self.participants:
            return await interaction.response.send_message(
                "This crew is full.", ephemeral=True
            )
        result = await asyncio.to_thread(
            self.cog.store.check_heist_ready,
            self.guild.id,
            interaction.user.id,
            "team_bank",
        )
        if not result.get("ok"):
            if result.get("reason") == "jailed":
                return await interaction.response.send_message(
                    f"You are jailed until <t:{_discord_timestamp(result['jail_until'])}:R>.",
                    ephemeral=True,
                )
            if result.get("reason") == "cooldown":
                return await interaction.response.send_message(
                    f"You can join a team bank robbery again <t:{_discord_timestamp(result['ready_at'])}:R>.",
                    ephemeral=True,
                )
            return await interaction.response.send_message(
                "You cannot join that heist right now.", ephemeral=True
            )
        self.participants.add(interaction.user.id)
        self.render()
        await interaction.response.edit_message(view=self)

    async def kick_button(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.leader_id:
            return await interaction.response.send_message(
                "Only the crew leader can kick members.", ephemeral=True
            )
        kickable = [user_id for user_id in sorted(self.participants) if user_id != self.leader_id]
        if not kickable:
            return await interaction.response.send_message(
                "There are no crew members to kick.", ephemeral=True
            )

        view = discord.ui.View(timeout=45)
        options = []
        for user_id in kickable:
            member = self.guild.get_member(user_id)
            options.append(
                discord.SelectOption(
                    label=(member.display_name if member else str(user_id))[:100],
                    value=str(user_id),
                )
            )
        select = discord.ui.Select(
            placeholder="Choose a crew member to kick",
            min_values=1,
            max_values=1,
            options=options,
        )

        async def select_callback(select_interaction: discord.Interaction) -> None:
            if select_interaction.user.id != self.leader_id:
                return await select_interaction.response.send_message(
                    "Only the crew leader can use this menu.", ephemeral=True
                )
            removed_id = int(select.values[0])
            if removed_id == self.leader_id or removed_id not in self.participants:
                return await select_interaction.response.send_message(
                    "That member is no longer in the crew.", ephemeral=True
                )
            self.participants.remove(removed_id)
            self.render()
            await select_interaction.response.edit_message(view=self)

        select.callback = select_callback
        view.add_item(select)
        await interaction.response.send_message(view=view, ephemeral=True)

    async def start_button(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.leader_id:
            return await interaction.response.send_message(
                "Only the crew leader can start this heist.", ephemeral=True
            )
        if len(self.participants) < 2:
            return await interaction.response.send_message(
                "You need at least two crew members.", ephemeral=True
            )
        run_view = HeistRunView(
            self.cog,
            self.guild,
            self.leader_id,
            [uid for uid in self.participants if uid != self.leader_id],
            "team_bank",
        )
        await interaction.response.edit_message(view=run_view)

class CrimeStoreMixin:
    def get_member_security(self, guild_id: int, user_id: int) -> dict[str, Any]:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO member_security (guild_id, user_id)
                        VALUES (%s, %s)
                        ON CONFLICT (guild_id, user_id) DO NOTHING
                        """,
                        (guild_id, user_id),
                    )
                    cursor.execute(
                        """
                        SELECT guild_id, user_id, lock_level, wanted_level, jail_until,
                               last_rob_at, last_jewelry_heist_at, last_bank_heist_at, last_team_heist_at
                        FROM member_security
                        WHERE guild_id = %s AND user_id = %s
                        """,
                        (guild_id, user_id),
                    )
                    row = cursor.fetchone()
                    return (
                        dict(row)
                        if row
                        else {
                            "guild_id": guild_id,
                            "user_id": user_id,
                            "lock_level": 0,
                            "wanted_level": 0,
                            "jail_until": None,
                            "last_rob_at": None,
                            "last_jewelry_heist_at": None,
                            "last_bank_heist_at": None,
                            "last_team_heist_at": None,
                        }
                    )
        finally:
            self._pool.putconn(conn)

    def buy_security_lock(
        self, guild_id: int, user_id: int, lock_level: int
    ) -> dict[str, Any]:
        lock_level = max(1, min(3, int(lock_level)))
        data = LOCK_LEVELS[lock_level]
        base_price = int(data["price"])
        item_key = f"lock_{lock_level}"

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

                    if stock < 1:
                        return {"ok": False, "reason": "stock", "stock": stock}

                    price = int(base_price * (1 - discount / 100))

                    cursor.execute(
                        """
                        INSERT INTO member_security (guild_id, user_id)
                        VALUES (%s, %s)
                        ON CONFLICT (guild_id, user_id) DO NOTHING
                        """,
                        (guild_id, user_id),
                    )
                    cursor.execute(
                        "SELECT lock_level FROM member_security WHERE guild_id = %s AND user_id = %s FOR UPDATE",
                        (guild_id, user_id),
                    )
                    current_level = int(cursor.fetchone()["lock_level"])
                    if current_level >= lock_level:
                        return {
                            "ok": False,
                            "reason": "owned",
                            "lock_level": current_level,
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

                    # deduct balance
                    cursor.execute(
                        "UPDATE member_economy SET credits = credits - %s, updated_at = CURRENT_TIMESTAMP WHERE guild_id = %s AND user_id = %s",
                        (price, guild_id, user_id),
                    )
                    # deduct stock
                    cursor.execute(
                        "UPDATE blackmarket_inventory SET current_stock = current_stock - 1, updated_at = CURRENT_TIMESTAMP WHERE guild_id = %s AND item_key = %s",
                        (guild_id, item_key),
                    )

                    cursor.execute(
                        "UPDATE member_security SET lock_level = %s, updated_at = CURRENT_TIMESTAMP WHERE guild_id = %s AND user_id = %s",
                        (lock_level, guild_id, user_id),
                    )
                    return {
                        "ok": True,
                        "lock_level": lock_level,
                        "price": price,
                        "balance": balance - price,
                    }
        finally:
            self._pool.putconn(conn)

    def _ensure_security_for_update(
        self, cursor, guild_id: int, user_id: int
    ) -> dict[str, Any]:
        cursor.execute(
            """
            INSERT INTO member_security (guild_id, user_id)
            VALUES (%s, %s)
            ON CONFLICT (guild_id, user_id) DO NOTHING
            """,
            (guild_id, user_id),
        )
        cursor.execute(
            """
            SELECT guild_id, user_id, lock_level, wanted_level, jail_until,
                   last_rob_at, last_jewelry_heist_at, last_bank_heist_at, last_team_heist_at
            FROM member_security
            WHERE guild_id = %s AND user_id = %s
            FOR UPDATE
            """,
            (guild_id, user_id),
        )
        return dict(cursor.fetchone())

    def check_heist_ready(
        self, guild_id: int, user_id: int, heist_key: str
    ) -> dict[str, Any]:
        if heist_key not in HEIST_DEFS:
            return {"ok": False, "reason": "invalid_heist"}
        now = datetime.now(timezone.utc)
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    security = self._ensure_security_for_update(
                        cursor, guild_id, user_id
                    )
                    jail_until = _as_utc(security.get("jail_until"))
                    if jail_until and jail_until > now:
                        return {
                            "ok": False,
                            "reason": "jailed",
                            "user_id": user_id,
                            "jail_until": jail_until,
                        }
                    blocked = self._cooldown_block(security, heist_key, now)
                    if blocked:
                        blocked["user_id"] = user_id
                        return blocked
                    return {"ok": True}
        finally:
            self._pool.putconn(conn)

    def attempt_member_rob(
        self, guild_id: int, robber_id: int, target_id: int
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    robber_security = self._ensure_security_for_update(
                        cursor, guild_id, robber_id
                    )
                    self._ensure_security_for_update(cursor, guild_id, target_id)
                    jail_until = _as_utc(robber_security.get("jail_until"))
                    if jail_until and jail_until > now:
                        return {
                            "ok": False,
                            "reason": "jailed",
                            "jail_until": jail_until,
                        }

                    blocked = self._cooldown_block(robber_security, "rob", now)
                    if blocked:
                        return blocked

                    cursor.execute(
                        "SELECT lock_level FROM member_security WHERE guild_id = %s AND user_id = %s FOR UPDATE",
                        (guild_id, target_id),
                    )
                    target_lock = int(cursor.fetchone()["lock_level"])
                    robber_items = self._get_item_quantities_for_update(
                        cursor, guild_id, robber_id
                    )

                    cursor.execute(
                        "SELECT credits FROM member_economy WHERE guild_id = %s AND user_id = %s FOR UPDATE",
                        (guild_id, target_id),
                    )
                    target_row = cursor.fetchone()
                    target_balance = int(target_row["credits"]) if target_row else 0

                    if target_balance < 10000:
                        return {
                            "ok": False,
                            "reason": "poor"
                        }

                    used_item = None
                    wanted_level = int(robber_security.get("wanted_level") or 0)
                    success_chance = (
                        0.26
                        - float(LOCK_LEVELS.get(target_lock, LOCK_LEVELS[0])["penalty"])
                        - min(0.16, wanted_level * 0.025)
                    )
                    defense_rate = 0.0
                    if hasattr(self, "get_effect_rate"):
                        defense_rate = float(
                            self.get_effect_rate(guild_id, target_id, "defense")
                        )
                    success_chance -= min(0.12, max(0.0, defense_rate))
                    if robber_items.get("advanced_lockpick", 0) > 0:
                        success_chance += 0.09
                        used_item = "advanced_lockpick"
                    elif robber_items.get("lockpick", 0) > 0:
                        success_chance += 0.04
                        used_item = "lockpick"
                    success_chance = max(0.03, min(0.48, success_chance))
                    if used_item:
                        cursor.execute(
                            "UPDATE economy_items SET quantity = quantity - 1, updated_at = CURRENT_TIMESTAMP WHERE guild_id = %s AND user_id = %s AND item_key = %s",
                            (guild_id, robber_id, used_item),
                        )
                    cursor.execute(
                        "UPDATE member_security SET last_rob_at = %s, updated_at = CURRENT_TIMESTAMP WHERE guild_id = %s AND user_id = %s",
                        (now, guild_id, robber_id),
                    )

                    success = random.random() <= success_chance
                    if success:
                        stolen = max(
                            500, int(target_balance * random.uniform(0.04, 0.12))
                        )
                        cursor.execute(
                            """
                            WITH deduct AS (
                                UPDATE member_economy 
                                SET debt = debt + GREATEST(0, %s - credits),
                                    debt_deadline = CASE WHEN debt + GREATEST(0, %s - credits) > 0 THEN CURRENT_TIMESTAMP + INTERVAL '1 hour' ELSE debt_deadline END,
                                    credits = GREATEST(0, credits - %s),
                                    updated_at = CURRENT_TIMESTAMP
                                WHERE guild_id = %s AND user_id = %s
                                RETURNING credits
                            )
                            INSERT INTO member_economy (guild_id, user_id, credits, updated_at)
                            VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
                            ON CONFLICT (guild_id, user_id) DO UPDATE SET
                                credits = member_economy.credits + EXCLUDED.credits,
                                updated_at = CURRENT_TIMESTAMP
                            WHERE EXISTS (SELECT 1 FROM deduct)
                            """,
                            (
                                stolen,
                                stolen,
                                stolen,
                                guild_id,
                                target_id,
                                guild_id,
                                robber_id,
                                stolen,
                            ),
                        )
                        cursor.execute(
                            "UPDATE member_security SET wanted_level = LEAST(10, wanted_level + 1), updated_at = CURRENT_TIMESTAMP WHERE guild_id = %s AND user_id = %s",
                            (guild_id, robber_id),
                        )
                        return {
                            "ok": True,
                            "success": True,
                            "stolen": stolen,
                            "success_chance": success_chance,
                            "target_lock": target_lock,
                            "used_item": used_item,
                        }

                    fine = max(1_000, int(target_balance * random.uniform(0.03, 0.08)))
                    jail_until = None
                    if wanted_level >= 3 or random.random() < 0.35:
                        jail_until = now + timedelta(minutes=20 + wanted_level * 8)
                    cursor.execute(
                        """
                        UPDATE member_economy 
                        SET debt = debt + GREATEST(0, %s - credits),
                            debt_deadline = CASE WHEN debt + GREATEST(0, %s - credits) > 0 THEN CURRENT_TIMESTAMP + INTERVAL '1 hour' ELSE debt_deadline END,
                            credits = GREATEST(0, credits - %s), 
                            updated_at = CURRENT_TIMESTAMP 
                        WHERE guild_id = %s AND user_id = %s
                        """,
                        (fine, fine, fine, guild_id, robber_id),
                    )
                    cursor.execute(
                        """
                        UPDATE member_security
                        SET wanted_level = CASE WHEN %s::timestamp IS NOT NULL THEN 0 ELSE LEAST(10, wanted_level + 2) END,
                            jail_until = COALESCE(%s, jail_until),
                            updated_at = CURRENT_TIMESTAMP
                        WHERE guild_id = %s AND user_id = %s
                        """,
                        (jail_until, jail_until, guild_id, robber_id),
                    )
                    return {
                        "ok": True,
                        "success": False,
                        "fine": fine,
                        "success_chance": success_chance,
                        "target_lock": target_lock,
                        "used_item": used_item,
                        "jail_until": jail_until,
                    }
        finally:
            self._pool.putconn(conn)

    def execute_heist(
        self,
        guild_id: int,
        leader_id: int,
        participant_ids: list[int],
        heist_key: str,
        heat: int = 0,
    ) -> dict[str, Any]:
        if heist_key not in HEIST_DEFS:
            return {"ok": False, "reason": "invalid_heist"}
        participants = []
        for user_id in [leader_id, *participant_ids]:
            user_id = int(user_id)
            if user_id not in participants:
                participants.append(user_id)
        if heist_key == "team_bank" and len(participants) < 2:
            return {"ok": False, "reason": "team_size"}
        if len(participants) > 4:
            participants = participants[:4]

        heist = HEIST_DEFS[heist_key]
        heat = max(0, min(5, int(heat)))
        now = datetime.now(timezone.utc)
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    securities: dict[int, dict[str, Any]] = {}
                    inventories: dict[int, dict[str, int]] = {}
                    for user_id in participants:
                        cursor.execute(
                            """
                            INSERT INTO member_security (guild_id, user_id)
                            VALUES (%s, %s)
                            ON CONFLICT (guild_id, user_id) DO NOTHING
                            """,
                            (guild_id, user_id),
                        )
                        cursor.execute(
                            """
                            SELECT lock_level, wanted_level, jail_until,
                                   last_rob_at, last_jewelry_heist_at, last_bank_heist_at, last_team_heist_at
                            FROM member_security
                            WHERE guild_id = %s AND user_id = %s
                            FOR UPDATE
                            """,
                            (guild_id, user_id),
                        )
                        security = dict(cursor.fetchone())
                        jail_until = _as_utc(security.get("jail_until"))
                        if jail_until and jail_until > now:
                            return {
                                "ok": False,
                                "reason": "jailed",
                                "user_id": user_id,
                                "jail_until": jail_until,
                            }
                        blocked = self._cooldown_block(security, heist_key, now)
                        if blocked:
                            blocked["user_id"] = user_id
                            return blocked
                        securities[user_id] = security
                        inventories[user_id] = self._get_item_quantities_for_update(
                            cursor, guild_id, user_id
                        )

                    missing: list[dict[str, Any]] = []
                    for user_id in participants:
                        required = dict(heist.get("items", {}))
                        if user_id == leader_id:
                            for item_key, qty in heist.get("leader_items", {}).items():
                                required[item_key] = required.get(item_key, 0) + qty
                        for item_key, qty in required.items():
                            owned = int(inventories[user_id].get(item_key, 0))
                            if owned < int(qty):
                                missing.append(
                                    {
                                        "user_id": user_id,
                                        "item_key": item_key,
                                        "owned": owned,
                                        "required": int(qty),
                                    }
                                )
                    if missing:
                        return {"ok": False, "reason": "items", "missing": missing}

                    for user_id in participants:
                        required = dict(heist.get("items", {}))
                        if user_id == leader_id:
                            for item_key, qty in heist.get("leader_items", {}).items():
                                required[item_key] = required.get(item_key, 0) + qty
                        for item_key, qty in required.items():
                            cursor.execute(
                                """
                                UPDATE economy_items
                                SET quantity = quantity - %s, updated_at = CURRENT_TIMESTAMP
                                WHERE guild_id = %s AND user_id = %s AND item_key = %s
                                """,
                                (int(qty), guild_id, user_id, item_key),
                            )

                    avg_wanted = sum(
                        int(securities[user_id]["wanted_level"])
                        for user_id in participants
                    ) / len(participants)
                    fake_id_user = next(
                        (
                            user_id
                            for user_id in participants
                            if inventories[user_id].get("fake_id", 0) > 0
                        ),
                        None,
                    )
                    getaway_user = next(
                        (
                            user_id
                            for user_id in participants
                            if inventories[user_id].get("getaway_car", 0) > 0
                        ),
                        None,
                    )
                    success_chance = (
                        float(heist["base_success"])
                        + min(0.08, 0.02 * (len(participants) - 1))
                        - min(0.26, avg_wanted * 0.045)
                        - (heat * 0.07)
                    )
                    if fake_id_user is not None:
                        success_chance += 0.05
                    if getaway_user is not None:
                        success_chance += 0.07
                    success_chance = max(0.06, min(0.58, success_chance))
                    success = random.random() <= success_chance

                    payout = (
                        random.randint(
                            int(heist["min_payout"]), int(heist["max_payout"])
                        )
                        if success
                        else 0
                    )
                    fine = (
                        0
                        if success
                        else int(
                            random.randint(
                                int(heist["fail_min"]), int(heist["fail_max"])
                            )
                            * (1 + heat * 0.12)
                        )
                    )
                    split = payout // len(participants) if success else 0
                    fine_each = max(1, fine // len(participants)) if fine else 0

                    for optional_user, optional_item in (
                        (fake_id_user, "fake_id"),
                        (getaway_user, "getaway_car"),
                    ):
                        if optional_user is None:
                            continue
                        cursor.execute(
                            """
                            UPDATE economy_items
                            SET quantity = quantity - 1, updated_at = CURRENT_TIMESTAMP
                            WHERE guild_id = %s AND user_id = %s AND item_key = %s
                            """,
                            (guild_id, optional_user, optional_item),
                        )

                    if success and split > 0:
                        for user_id in participants:
                            cursor.execute(
                                """
                                INSERT INTO member_economy (guild_id, user_id, credits, updated_at)
                                VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
                                ON CONFLICT (guild_id, user_id) DO UPDATE SET
                                    credits = member_economy.credits + EXCLUDED.credits,
                                    updated_at = CURRENT_TIMESTAMP
                                """,
                                (guild_id, user_id, split),
                            )
                    elif fine_each > 0:
                        for user_id in participants:
                            cursor.execute(
                                """
                                UPDATE member_economy
                                SET debt = debt + GREATEST(0, %s - credits),
                                    debt_deadline = CASE WHEN debt + GREATEST(0, %s - credits) > 0 THEN CURRENT_TIMESTAMP + INTERVAL '1 hour' ELSE debt_deadline END,
                                    credits = GREATEST(0, credits - %s), updated_at = CURRENT_TIMESTAMP
                                WHERE guild_id = %s AND user_id = %s
                                """,
                                (fine_each, fine_each, fine_each, guild_id, user_id),
                            )

                    for user_id in participants:
                        jail_until = None
                        if not success:
                            jail_until = now + timedelta(
                                minutes=int(heist["jail_minutes"])
                                + heat * 10
                                + int(securities[user_id]["wanted_level"]) * 7
                            )
                        cooldown_column = ROBBERY_COOLDOWN_COLUMNS[heist_key]
                        cursor.execute(
                            f"""
                            UPDATE member_security
                            SET wanted_level = CASE WHEN %s::timestamp IS NOT NULL THEN 0 ELSE LEAST(10, wanted_level + %s) END,
                                jail_until = COALESCE(%s, jail_until),
                                {cooldown_column} = %s,
                                updated_at = CURRENT_TIMESTAMP
                            WHERE guild_id = %s AND user_id = %s
                            """,
                            (
                                jail_until,
                                int(heist["wanted"]) + (1 if heat >= 2 else 0),
                                jail_until,
                                now,
                                guild_id,
                                user_id,
                            ),
                        )

                    cursor.execute(
                        """
                        INSERT INTO heist_history (guild_id, leader_id, participant_ids, heist_key, outcome, payout, fine)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            guild_id,
                            leader_id,
                            psycopg2.extras.Json(participants),
                            heist_key,
                            "success" if success else "failed",
                            payout,
                            fine,
                        ),
                    )
                    return {
                        "ok": True,
                        "heist_key": heist_key,
                        "title": heist["title"],
                        "success": success,
                        "success_chance": success_chance,
                        "heat": heat,
                        "participants": participants,
                        "payout": payout,
                        "split": split,
                        "fine": fine,
                        "fine_each": fine_each,
                        "jail_minutes": 0
                        if success
                        else int(heist["jail_minutes"]) + heat * 10,
                    }
        finally:
            self._pool.putconn(conn)

class CrimeCog(commands.Cog):
    async def _can_start_heist(self, ctx: commands.Context, heist_key: str) -> bool:
        result = await asyncio.to_thread(
            self.store.check_heist_ready, ctx.guild.id, ctx.author.id, heist_key
        )
        if result.get("ok"):
            return True
        if result.get("reason") == "jailed":
            await ctx.send(
                f"You are jailed until <t:{_discord_timestamp(result['jail_until'])}:R>."
            )
            return False
        if result.get("reason") == "cooldown":
            await ctx.send(
                f"You can run **{HEIST_DEFS[heist_key]['title']}** again <t:{_discord_timestamp(result['ready_at'])}:R>."
            )
            return False
        await ctx.send("You cannot start that heist right now.")
        return False

    @commands.command(
        name="jewelryheist",
        aliases=["jewelry", "jewelrob"],
        help="Start an interactive jewelry theft.",
    )
    async def jewelryheist_cmd(self, ctx: commands.Context):
        if not await self._can_start_heist(ctx, "jewelry"):
            return
        view = HeistRunView(self, ctx.guild, ctx.author.id, [], "jewelry")
        await ctx.send(view=view, allowed_mentions=discord.AllowedMentions.none())

    @commands.command(
        name="bankrob",
        aliases=["bankheist"],
        help="Start an interactive solo bank robbery.",
    )
    async def bankrob_cmd(self, ctx: commands.Context):
        if not await self._can_start_heist(ctx, "bank"):
            return
        view = HeistRunView(self, ctx.guild, ctx.author.id, [], "bank")
        await ctx.send(view=view, allowed_mentions=discord.AllowedMentions.none())

    @commands.command(
        name="teamrob",
        aliases=["teamheist", "crewrob"],
        help="Open a team bank robbery lobby.",
    )
    async def teamrob_cmd(self, ctx: commands.Context):
        if not await self._can_start_heist(ctx, "team_bank"):
            return
        view = TeamHeistLobbyView(self, ctx)
        await ctx.send(view=view, allowed_mentions=discord.AllowedMentions.none())

    @commands.command(
        name="rob", help="Attempt to rob another member's wallet (30 minute cooldown)."
    )
    async def rob(self, ctx: commands.Context, member: Optional[discord.Member] = None):
        if member is None:
            embed = self.create_embed(
                "Robbery",
                "Usage: `.rob @user`\nPick a member with at least **10,000 cr** in their wallet.",
                color=0xED4245,
            )
            await self._send(ctx, embed)
            return
        await self._check_cooldown(ctx, 1800)
        if member == ctx.author:
            embed = self.create_embed(
                "🦹 Robbery", "You can't rob yourself!", color=0xED4245
            )
            await self._send(ctx, embed)
            await self._reset_cooldown(ctx)
            return

        if hasattr(self.store, "attempt_member_rob"):
            result = await asyncio.to_thread(
                self.store.attempt_member_rob, ctx.guild.id, ctx.author.id, member.id
            )
            if not result.get("ok"):
                if result.get("reason") == "poor":
                    embed = self.create_embed(
                        "🦹 Robbery",
                        f"{member.mention} needs at least **10,000 cr** in wallet before they can be robbed.",
                        color=0xED4245,
                    )
                    await self._send(ctx, embed)
                    await self._reset_cooldown(ctx)
                    return
                if result.get("reason") == "jailed":
                    embed = self.create_embed(
                        "🦹 Robbery",
                        f"You are jailed until <t:{int(result['jail_until'].timestamp())}:R>.",
                        color=0xED4245,
                    )
                    await self._send(ctx, embed)
                    await self._reset_cooldown(ctx)
                    return
                if result.get("reason") == "cooldown":
                    embed = self.create_embed(
                        "🦹 Robbery",
                        f"You can rob again <t:{int(result['ready_at'].timestamp())}:R>.",
                        color=0xED4245,
                    )
                    await self._send(ctx, embed)
                    return
            lock_note = f" Target lock level: **{int(result.get('target_lock', 0))}**."
            tool_note = (
                f" Used **{str(result.get('used_item')).replace('_', ' ').title()}**."
                if result.get("used_item")
                else ""
            )
            if result.get("success"):
                embed = self.create_embed(
                    "🦹 Robbery Successful!",
                    f"You stole **{int(result['stolen']):,} cr** from {member.mention}!{lock_note}{tool_note}",
                    color=0x2ECC71,
                )
            else:
                jail_note = (
                    f" You were jailed until <t:{int(result['jail_until'].timestamp())}:R>."
                    if result.get("jail_until")
                    else ""
                )
                embed = self.create_embed(
                    "🦹 Robbery Failed!",
                    f"You were caught trying to rob {member.mention} and paid a fine of **{int(result['fine']):,} cr**.{jail_note}{lock_note}{tool_note}",
                    color=0xED4245,
                )
        else:
            target_bal = await asyncio.to_thread(
                self.store.get_balance, ctx.guild.id, member.id
            )
            if target_bal < 10_000:
                embed = self.create_embed(
                    "🦹 Robbery",
                    f"{member.mention} needs at least **10,000 cr** in wallet before they can be robbed.",
                    color=0xED4245,
                )
                await self._send(ctx, embed)
                await self._reset_cooldown(ctx)
                return
            fine = random.randint(100, 300)
            await asyncio.to_thread(
                self.store.remove_credits, ctx.guild.id, ctx.author.id, fine
            )
            embed = self.create_embed(
                "🦹 Robbery Failed!",
                f"You were caught trying to rob {member.mention} and had to pay a fine of **{fine:,} cr**!",
                color=0xED4245,
            )

        await self._send(ctx, embed)

