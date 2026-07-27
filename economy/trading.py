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
from .earning import _investment_amount
from .pets import _pet_display_name


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
TRADE_REQUEST_TIMEOUT_SECONDS = 120
TRADE_EXECUTION_COUNTDOWN_SECONDS = 5




class TradeState:
    """In-memory trade offer between two guild users."""

    def __init__(self, guild_id: int, user_a_id: int, user_b_id: int):
        self.guild_id = guild_id
        self.user_a_id = user_a_id
        self.user_b_id = user_b_id
        self.items_a: dict = {
            "credits": 0,
            "pets": [],
            "economy_items": {},
            "pet_items": {},
        }
        self.items_b: dict = {
            "credits": 0,
            "pets": [],
            "economy_items": {},
            "pet_items": {},
        }
        self.confirmed_a = False
        self.confirmed_b = False
        self.cancelled = False
        self.executed = False

    def _side(self, user_id: int) -> dict:
        if user_id == self.user_a_id:
            return self.items_a
        if user_id == self.user_b_id:
            return self.items_b
        raise ValueError("Unknown user in trade")

    def other_id(self, user_id: int) -> int:
        if user_id == self.user_a_id:
            return self.user_b_id
        if user_id == self.user_b_id:
            return self.user_a_id
        raise ValueError("Unknown user in trade")

    def _unconfirm(self):
        self.confirmed_a = False
        self.confirmed_b = False

    def add_credits(self, user_id: int, amount: int):
        side = self._side(user_id)
        side["credits"] += amount
        self._unconfirm()

    def add_pet(self, user_id: int, pet_id: int, pet_name: str):
        side = self._side(user_id)
        if not any(p[0] == pet_id for p in side["pets"]):
            side["pets"].append((pet_id, pet_name))
            self._unconfirm()

    def add_economy_item(self, user_id: int, item_key: str, qty: int):
        side = self._side(user_id)
        side["economy_items"][item_key] = side["economy_items"].get(item_key, 0) + qty
        self._unconfirm()

    def add_pet_item(self, user_id: int, item_key: str, qty: int):
        side = self._side(user_id)
        side["pet_items"][item_key] = side["pet_items"].get(item_key, 0) + qty
        self._unconfirm()

    def remove_item(self, user_id: int, category: str, ref: str):
        side = self._side(user_id)
        if category == "credits":
            side["credits"] = 0
        elif category == "pet":
            pet_id = int(ref)
            side["pets"] = [p for p in side["pets"] if p[0] != pet_id]
        elif category == "economy_item":
            current = side["economy_items"].get(ref, 0)
            if current <= 1:
                side["economy_items"].pop(ref, None)
            else:
                side["economy_items"][ref] = current - 1
        elif category == "pet_item":
            current = side["pet_items"].get(ref, 0)
            if current <= 1:
                side["pet_items"].pop(ref, None)
            else:
                side["pet_items"][ref] = current - 1
        self._unconfirm()

    def has_anything(self, user_id: int) -> bool:
        side = self._side(user_id)
        return bool(
            side["credits"] > 0
            or side["pets"]
            or side["economy_items"]
            or side["pet_items"]
        )

    def is_confirmed(self, user_id: int) -> bool:
        if user_id == self.user_a_id:
            return self.confirmed_a
        if user_id == self.user_b_id:
            return self.confirmed_b
        return False

    def confirm(self, user_id: int) -> bool:
        if user_id == self.user_a_id:
            self.confirmed_a = True
        elif user_id == self.user_b_id:
            self.confirmed_b = True
        return self.confirmed_a and self.confirmed_b

class CreditTradeModal(discord.ui.Modal, title="Offer Credits"):
    amount = discord.ui.TextInput(
        label="How many credits?",
        placeholder="Enter amount to offer...",
        required=True,
        max_length=12,
    )

    def __init__(self, trade_view: discord.ui.View, user_id: int):
        super().__init__()
        self.trade_view = trade_view
        self.user_id = user_id

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.amount.value.strip().replace(",", "")
        try:
            amt = int(raw)
        except (ValueError, TypeError):
            return await interaction.response.send_message(
                "Invalid number.", ephemeral=True
            )
        if amt <= 0:
            return await interaction.response.send_message(
                "Amount must be positive.", ephemeral=True
            )
        balance = int(
            await asyncio.to_thread(
                self.trade_view.cog.store.get_balance,
                self.trade_view.state.guild_id,
                self.user_id,
            )
        )
        if amt > balance:
            return await interaction.response.send_message(
                f"You only have **{balance:,} cr** in your wallet.", ephemeral=True
            )
        existing = self.trade_view.state._side(self.user_id)["credits"]
        if existing + amt > balance:
            return await interaction.response.send_message(
                f"You can only offer **{balance - existing:,}** more credits (you have **{existing:,}** already offered).",
                ephemeral=True,
            )
        self.trade_view.state.add_credits(self.user_id, amt)
        self.trade_view.render()
        try:
            await self.trade_view._edit_message()
        except Exception:
            pass
        await interaction.response.send_message(
            f"Added **{amt:,} cr** to your offer.", ephemeral=True
        )


class ItemQuantityTradeModal(discord.ui.Modal, title="Offer Item Quantity"):
    amount = discord.ui.TextInput(
        label="How many items?",
        placeholder="Enter quantity to offer...",
        required=True,
        max_length=12,
    )

    def __init__(
        self,
        trade_view: discord.ui.View,
        user_id: int,
        category: str,
        item_key: str,
        item_name: str,
        available: int,
    ):
        super().__init__()
        self.trade_view = trade_view
        self.user_id = user_id
        self.category = category
        self.item_key = item_key
        self.item_name = item_name
        self.available = max(1, int(available))
        self.amount.placeholder = f"Up to {self.available:,} available"

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.amount.value.strip().replace(",", "")
        try:
            quantity = int(raw)
        except (TypeError, ValueError):
            return await interaction.response.send_message(
                "Quantity must be a whole number.", ephemeral=True
            )
        if quantity < 1 or quantity > self.available:
            return await interaction.response.send_message(
                f"Choose an amount from **1** to **{self.available:,}**.",
                ephemeral=True,
            )

        if self.category == "economy":
            self.trade_view.state.add_economy_item(
                self.user_id, self.item_key, quantity
            )
        else:
            self.trade_view.state.add_pet_item(
                self.user_id, self.item_key, quantity
            )
        self.trade_view.render()
        await self.trade_view._edit_message()
        await interaction.response.send_message(
            f"Added **{quantity:,}x {self.item_name}** to your offer.",
            ephemeral=True,
        )


class TradeRequestView(discord.ui.LayoutView):
    def __init__(
        self,
        cog: Any,
        guild_id: int,
        requester: discord.Member,
        target: discord.Member,
    ):
        super().__init__(timeout=TRADE_REQUEST_TIMEOUT_SECONDS)
        self.cog = cog
        self.guild_id = guild_id
        self.requester = requester
        self.target = target
        self.message: Optional[discord.Message] = None
        self.status = "pending"
        self.render()

    def _description(self) -> str:
        if self.status == "accepted":
            return f"{self.target.mention} accepted {self.requester.mention}'s trade request."
        if self.status == "declined":
            return f"{self.target.mention} declined {self.requester.mention}'s trade request."
        if self.status == "cancelled":
            return f"{self.requester.mention}'s trade request was cancelled."
        if self.status == "expired":
            return f"{self.requester.mention}'s trade request expired."
        return (
            f"{self.requester.mention} wants to trade with {self.target.mention}.\n"
            "The trade will only begin if the request is accepted. This request expires in 2 minutes."
        )

    def _cleanup_pending(self) -> None:
        current = self.cog._pending_trades.get(self.requester.id)
        if current is self:
            self.cog._pending_trades.pop(self.requester.id, None)
        current = self.cog._pending_trades.get(self.target.id)
        if current is self:
            self.cog._pending_trades.pop(self.target.id, None)

    def render(self) -> None:
        self.clear_items()
        container = branded_panel_container(
            title="Trade Request",
            description=self._description(),
            accent_color=0xF1C40F if self.status == "pending" else 0x6B7280,
            min_width_chars=None,
        )
        if self.status == "pending":
            accept_button = discord.ui.Button(
                label="Accept", style=discord.ButtonStyle.success
            )
            decline_button = discord.ui.Button(
                label="Decline", style=discord.ButtonStyle.danger
            )
            accept_button.callback = self.accept_button
            decline_button.callback = self.decline_button
            container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
            container.add_item(discord.ui.ActionRow(accept_button, decline_button))
        self.add_item(container)
        ensure_layout_view_action_rows(self)

    async def accept_button(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.target.id:
            return await interaction.response.send_message(
                "Only the requested member can accept this trade.", ephemeral=True
            )
        if (
            self.requester.id in self.cog._active_trades
            or self.target.id in self.cog._active_trades
        ):
            self.status = "cancelled"
            self._cleanup_pending()
            self.render()
            return await interaction.response.edit_message(view=self)
        trade_view = TradeView(self.cog, self.guild_id, self.requester, self.target)
        self.cog._active_trades[self.requester.id] = trade_view
        self.cog._active_trades[self.target.id] = trade_view
        self.status = "accepted"
        self._cleanup_pending()
        self.stop()
        trade_view.message = interaction.message
        await interaction.response.edit_message(view=trade_view)

    async def decline_button(self, interaction: discord.Interaction) -> None:
        if interaction.user.id not in (self.requester.id, self.target.id):
            return await interaction.response.send_message(
                "This trade request is not for you.", ephemeral=True
            )
        self.status = "cancelled" if interaction.user.id == self.requester.id else "declined"
        self._cleanup_pending()
        self.render()
        self.stop()
        await interaction.response.edit_message(view=self)

    async def cancel_from_command(self) -> None:
        self.status = "cancelled"
        self._cleanup_pending()
        self.render()
        self.stop()
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass

    async def on_timeout(self) -> None:
        if self.status != "pending":
            return
        self.status = "expired"
        self._cleanup_pending()
        self.render()
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass

class TradeView(discord.ui.LayoutView):
    def __init__(
        self,
        cog: Any,
        guild_id: int,
        user_a: discord.Member,
        user_b: discord.Member,
    ):
        super().__init__(timeout=300)
        self.cog = cog
        self.state = TradeState(guild_id, user_a.id, user_b.id)
        self.user_a = user_a
        self.user_b = user_b
        self.message: Optional[discord.Message] = None
        self.execution_deadline: Optional[datetime] = None
        self.execution_task: Optional[asyncio.Task] = None
        self.render()

    def _user_mention(self, user_id: int) -> str:
        if user_id == self.user_a.id:
            return self.user_a.mention
        return self.user_b.mention

    def _user_by_id(self, user_id: int) -> discord.Member:
        if user_id == self.user_a.id:
            return self.user_a
        return self.user_b

    def _guild(self) -> Optional[discord.Guild]:
        return self.cog.bot.get_guild(self.state.guild_id)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user is None:
            return False
        if interaction.user.id not in (self.user_a.id, self.user_b.id):
            await interaction.response.send_message(
                "This is not your trade.", ephemeral=True
            )
            return False
        return True

    def _format_items(self, user_id: int) -> str:
        side = self.state._side(user_id)
        guild = self._guild()
        lines = []
        if side["credits"] > 0:
            lines.append(f"*   **{side['credits']:,}** credits")
        for pet_id, pet_name in side["pets"]:
            lines.append(f"*   {get_valk_emoji(guild, 'pet')}**{pet_name}** (#{pet_id})")
        for ikey, qty in sorted(side.get("economy_items", {}).items()):
            name = ECONOMY_ITEM_DEFS.get(ikey, {}).get("name", ikey)
            lines.append(f"*   **{qty}x** {name}")
        for pkey, qty in sorted(side.get("pet_items", {}).items()):
            name = PET_ITEM_DEFS.get(pkey, {}).get("name", pkey)
            lines.append(f"*   **{qty}x** {name}")
        return "\n".join(lines) if lines else "*(Nothing offered yet)*"

    def _build_summary_container(self) -> discord.ui.Container:
        if self.state.executed:
            title = "Trade Complete"
            description = (
                f"Trade completed between {self.user_a.mention} and {self.user_b.mention}."
            )
            accent_color = 0x2ECC71
        elif self.state.cancelled:
            title = "Trade Cancelled"
            description = (
                f"The trade between {self.user_a.mention} and {self.user_b.mention} was cancelled."
            )
            accent_color = 0xE74C3C
        else:
            title = "Trade Pact"
            description = (
                f"Trade between {self.user_a.mention} and {self.user_b.mention}."
            )
            accent_color = 0xF1C40F

        children: list[discord.ui.Item[Any]] = [
            discord.ui.TextDisplay(f"**{title}**\n{description}"),
        ]
        if self.execution_deadline is not None and not self.state.executed and not self.state.cancelled:
            remaining = max(
                0,
                int(
                    (self.execution_deadline - datetime.now(timezone.utc)).total_seconds()
                    + 0.999
                ),
            )
            children.append(
                discord.ui.TextDisplay(
                    f"**Completion Countdown**\n⏳ Completing in **{remaining}s**"
                )
            )
        children.extend(
            [
                discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
            discord.ui.TextDisplay(
                f"**{self.user_a.display_name} offers**\n{self._format_items(self.user_a.id)}"
            ),
            discord.ui.TextDisplay(
                f"**{self.user_b.display_name} offers**\n{self._format_items(self.user_b.id)}"
            ),
            ]
        )

        if not self.state.executed and not self.state.cancelled:
            a_status = "Ready" if self.state.confirmed_a else "Pending"
            b_status = "Ready" if self.state.confirmed_b else "Pending"
            children.extend(
                [
                    discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
                    discord.ui.TextDisplay(
                        "**Status**\n"
                        f"{self.user_a.display_name}: {a_status}\n"
                        f"{self.user_b.display_name}: {b_status}\n"
                        "*Both parties must confirm to complete.*"
                    ),
                ]
            )

        return discord.ui.Container(*children, accent_color=accent_color)

    async def _edit_message(self) -> None:
        if self.message is not None:
            await self.message.edit(view=self)

    def _owner_label(self, user_id: int) -> str:
        return self._user_by_id(user_id).display_name[:80]

    def _owned_remove_option(
        self,
        *,
        user_id: int,
        label: str,
        value: str,
        description: Optional[str] = None,
    ) -> discord.SelectOption:
        return discord.SelectOption(
            label=label[:100],
            value=f"{user_id}:{value}"[:100],
            description=description[:100] if description else self._owner_label(user_id),
        )

    def _remove_select_options(self) -> list[discord.SelectOption]:
        options: list[discord.SelectOption] = []
        options.extend(self._remove_options(self.user_a.id))
        options.extend(self._remove_options(self.user_b.id))
        actionable = [option for option in options if option.value != "none"]
        if actionable:
            return actionable[:25]
        return [
            discord.SelectOption(
                label="Nothing to remove",
                value="none",
                default=True,
            )
        ]

    def render(self):
        self.clear_items()
        container = self._build_summary_container()
        if self.state.executed or self.state.cancelled:
            self.add_item(container)
            return
        countdown_active = self.execution_deadline is not None

        add_credits_btn = discord.ui.Button(
            label="Add Credits",
            style=discord.ButtonStyle.primary,
            disabled=countdown_active,
        )

        async def add_credits_cb(interaction: discord.Interaction):
            await interaction.response.send_modal(
                CreditTradeModal(self, interaction.user.id)
            )

        add_credits_btn.callback = add_credits_cb

        add_pet_btn = discord.ui.Button(
            label="Add Pet",
            style=discord.ButtonStyle.secondary,
            disabled=countdown_active,
        )

        async def add_pet_cb(interaction: discord.Interaction):
            await self._add_pet_select(interaction)

        add_pet_btn.callback = add_pet_cb

        add_item_btn = discord.ui.Button(
            label="Add Item",
            style=discord.ButtonStyle.secondary,
            disabled=countdown_active,
        )

        async def add_item_cb(interaction: discord.Interaction):
            await self._add_item_select(interaction)

        add_item_btn.callback = add_item_cb

        remove_options = self._remove_select_options()
        remove_sel = discord.ui.Select(
            placeholder="Remove an offered item...",
            options=remove_options,
            custom_id=f"trade_remove:{self.user_a.id}:{self.user_b.id}",
            disabled=countdown_active or not any(option.value != "none" for option in remove_options),
        )

        async def remove_cb(interaction: discord.Interaction):
            val = remove_sel.values[0] if remove_sel.values else ""
            if val == "none" or ":" not in val:
                return await interaction.response.send_message(
                    "You have nothing to remove.", ephemeral=True
                )
            owner_raw, remainder = val.split(":", 1)
            try:
                owner_id = int(owner_raw)
            except ValueError:
                return await interaction.response.send_message(
                    "That trade option is no longer valid.", ephemeral=True
                )
            if owner_id != interaction.user.id:
                return await interaction.response.send_message(
                    "You can only remove your own offers.", ephemeral=True
                )
            cat, ref = remainder.split(":", 1)
            self.state.remove_item(interaction.user.id, cat, ref)
            self.render()
            await self._edit_message()
            await interaction.response.send_message(
                "Removed from your offer.", ephemeral=True
            )

        remove_sel.callback = remove_cb

        confirm_btn = discord.ui.Button(
            label="Confirm",
            style=discord.ButtonStyle.success,
            disabled=countdown_active,
        )

        async def confirm_cb(interaction: discord.Interaction):
            uid = interaction.user.id
            if not self.state.has_anything(uid):
                return await interaction.response.send_message(
                    "You must offer something first.", ephemeral=True
                )
            if self.execution_deadline is not None:
                return await interaction.response.send_message(
                    "Both traders already confirmed. The execution countdown is active.",
                    ephemeral=True,
                )
            both_ready = self.state.confirm(uid)
            if both_ready:
                self._start_execution_countdown()
                self.render()
                await self._edit_message()
                await interaction.response.send_message(
                    "Both traders confirmed. The trade completes in 5 seconds unless cancelled.",
                    ephemeral=True,
                )
            else:
                self.render()
                await self._edit_message()
                await interaction.response.send_message(
                    "Confirmed! Waiting for the other party.", ephemeral=True
                )

        confirm_btn.callback = confirm_cb

        cancel_btn = discord.ui.Button(
            label="Cancel", style=discord.ButtonStyle.danger
        )

        async def cancel_cb(interaction: discord.Interaction):
            self._cancel_execution_countdown()
            self.state.cancelled = True
            self.state.confirmed_a = False
            self.state.confirmed_b = False
            self.render()
            self.stop()
            self.cog._active_trades.pop(self.user_a.id, None)
            self.cog._active_trades.pop(self.user_b.id, None)
            await self._edit_message()
            await interaction.response.send_message("Trade cancelled.", ephemeral=True)

        cancel_btn.callback = cancel_cb

        container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
        container.add_item(discord.ui.ActionRow(add_credits_btn, add_pet_btn, add_item_btn))
        container.add_item(discord.ui.ActionRow(remove_sel))
        container.add_item(discord.ui.ActionRow(confirm_btn, cancel_btn))
        self.add_item(container)

    def _start_execution_countdown(self) -> None:
        self.execution_deadline = datetime.now(timezone.utc) + timedelta(
            seconds=TRADE_EXECUTION_COUNTDOWN_SECONDS
        )
        self.execution_task = asyncio.create_task(self._run_execution_countdown())

    def _cancel_execution_countdown(self) -> None:
        task = self.execution_task
        self.execution_task = None
        self.execution_deadline = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    async def _run_execution_countdown(self) -> None:
        try:
            while (
                self.execution_deadline is not None
                and not self.state.cancelled
                and not self.state.executed
            ):
                remaining = (self.execution_deadline - datetime.now(timezone.utc)).total_seconds()
                if remaining <= 0:
                    break
                self.render()
                await self._edit_message()
                await asyncio.sleep(min(1.0, remaining))

            if not self.state.cancelled and not self.state.executed:
                self.execution_deadline = None
                await self._execute_trade()
        except asyncio.CancelledError:
            raise
        finally:
            if self.execution_task is asyncio.current_task():
                self.execution_task = None

    def _get_all_controls(self):
        controls = []

        def collect(item):
            if isinstance(item, (discord.ui.Button, discord.ui.Select)):
                controls.append(item)
            for child in getattr(item, "children", []):
                collect(child)

        for child in self.children:
            collect(child)
        return controls

    def _remove_options(self, user_id: int) -> list[discord.SelectOption]:
        side = self.state._side(user_id)
        options: list[discord.SelectOption] = []
        if side["credits"] > 0:
            options.append(
                self._owned_remove_option(
                    user_id=user_id,
                    label=f"{side['credits']:,} credits",
                    value="credits:credits",
                )
            )
        for pet_id, pet_name in side["pets"]:
            options.append(
                self._owned_remove_option(
                    user_id=user_id,
                    label=pet_name,
                    value=f"pet:{pet_id}",
                )
            )
        for ikey, qty in sorted(side.get("economy_items", {}).items()):
            name = ECONOMY_ITEM_DEFS.get(ikey, {}).get("name", ikey)
            options.append(
                self._owned_remove_option(
                    user_id=user_id,
                    label=f"{name} x{qty}",
                    value=f"economy_item:{ikey}",
                )
            )
        for pkey, qty in sorted(side.get("pet_items", {}).items()):
            name = PET_ITEM_DEFS.get(pkey, {}).get("name", pkey)
            options.append(
                self._owned_remove_option(
                    user_id=user_id,
                    label=f"{name} x{qty}",
                    value=f"pet_item:{pkey}",
                )
            )
        if not options:
            options.append(
                discord.SelectOption(
                    label="Nothing to remove", value="none", default=True
                )
            )
        return options

    async def _add_pet_select(self, interaction: discord.Interaction):
        pets = await asyncio.to_thread(
            self.cog.store.get_user_pets, self.state.guild_id, interaction.user.id
        )
        offered_ids = {p[0] for p in self.state._side(interaction.user.id)["pets"]}
        options = []
        for pet in pets:
            pid = int(pet["id"])
            if pid in offered_ids:
                continue
            pet_info = _pet_display_name(pet)
            pet_line = PET_LINES.get(pet.get("pet_key", ""), {})
            stage = int(pet.get("stage") or 1)
            stage_info = (
                pet_line.get("stages", [{}])[
                    min(stage - 1, len(pet_line.get("stages", [{}])) - 1)
                ]
                if pet_line
                else {}
            )
            rarity = stage_info.get("rarity", pet.get("rarity", ""))
            options.append(
                discord.SelectOption(
                    label=f"{pet_info[:80]}",
                    value=str(pid),
                    description=f"{rarity} * Income {int(stage_info.get('daily_income', 0)):,}/day",
                )
            )
        if not options:
            return await interaction.response.send_message(
                "You have no available pets to offer.", ephemeral=True
            )
        select = discord.ui.Select(
            placeholder="Pick a pet to add...", options=options[:25]
        )

        async def pet_select_cb(sel_interaction):
            pid = int(select.values[0])
            pet_info = _pet_display_name(
                next(
                    (p for p in pets if int(p["id"]) == pid),
                    {"id": pid, "pet_key": "", "stage": 1},
                )
            )
            self.state.add_pet(interaction.user.id, pid, pet_info)
            self.render()
            await self._edit_message()
            await sel_interaction.response.send_message(
                f"Added **{pet_info}** (#{pid}) to your offer.", ephemeral=True
            )

        select.callback = pet_select_cb
        view = discord.ui.View(timeout=60)
        view.add_item(select)
        await interaction.response.send_message(
            "Select a pet to add:", view=view, ephemeral=True
        )

    async def _add_item_select(self, interaction: discord.Interaction):
        eco_items = await asyncio.to_thread(
            self.cog.store.get_economy_items, self.state.guild_id, interaction.user.id
        )
        pet_items = await asyncio.to_thread(
            self.cog.store.get_pet_items, self.state.guild_id, interaction.user.id
        )
        current_eco = self.state._side(interaction.user.id).get("economy_items", {})
        current_pet = self.state._side(interaction.user.id).get("pet_items", {})
        options = []
        available_by_value: dict[str, int] = {}
        for ikey, qty in sorted(eco_items.items()):
            available = max(0, int(qty) - current_eco.get(ikey, 0))
            if available > 0:
                name = ECONOMY_ITEM_DEFS.get(ikey, {}).get("name", ikey)
                value = f"economy:{ikey}"
                options.append(
                    discord.SelectOption(
                        label=f"{name} ({available} available)", value=value
                    )
                )
                available_by_value[value] = available
        for pkey, qty in sorted(pet_items.items()):
            available = max(0, int(qty) - current_pet.get(pkey, 0))
            if available > 0:
                name = PET_ITEM_DEFS.get(pkey, {}).get("name", pkey)
                value = f"pet_mat:{pkey}"
                options.append(
                    discord.SelectOption(
                        label=f"{name} ({available} available)", value=value
                    )
                )
                available_by_value[value] = available
        if not options:
            return await interaction.response.send_message(
                "You have no items to offer.", ephemeral=True
            )
        select = discord.ui.Select(
            placeholder="Pick an item to add...", options=options[:25]
        )

        async def item_select_cb(sel_interaction):
            val = select.values[0]
            if val.startswith("economy:"):
                ikey = val.split(":", 1)[1]
                item_name = ECONOMY_ITEM_DEFS.get(ikey, {}).get("name", ikey)
                category = "economy"
                item_key = ikey
            elif val.startswith("pet_mat:"):
                pkey = val.split(":", 1)[1]
                item_name = PET_ITEM_DEFS.get(pkey, {}).get("name", pkey)
                category = "pet_mat"
                item_key = pkey
            else:
                return await sel_interaction.response.send_message(
                    "That trade item is no longer valid.", ephemeral=True
                )
            await sel_interaction.response.send_modal(
                ItemQuantityTradeModal(
                    self,
                    interaction.user.id,
                    category,
                    item_key,
                    item_name,
                    available_by_value.get(val, 1),
                )
            )

        select.callback = item_select_cb
        view = discord.ui.View(timeout=60)
        view.add_item(select)
        await interaction.response.send_message(
            "Select an item to add:", view=view, ephemeral=True
        )

    async def _execute_trade(self, interaction: Optional[discord.Interaction] = None):
        items_a = {
            "user_id": self.user_a.id,
            "credits": self.state.items_a["credits"],
            "pets": self.state.items_a["pets"],
            "economy_items": self.state.items_a["economy_items"],
            "pet_items": self.state.items_a["pet_items"],
        }
        items_b = {
            "user_id": self.user_b.id,
            "credits": self.state.items_b["credits"],
            "pets": self.state.items_b["pets"],
            "economy_items": self.state.items_b["economy_items"],
            "pet_items": self.state.items_b["pet_items"],
        }
        result = await asyncio.to_thread(
            self.cog.store.execute_trade, self.state.guild_id, items_a, items_b
        )
        if result.get("ok"):
            self.state.executed = True
            self.render()
            self.stop()
            self.cog._active_trades.pop(self.user_a.id, None)
            self.cog._active_trades.pop(self.user_b.id, None)
            await self._edit_message()
            if interaction is not None:
                await interaction.response.send_message(
                    "Trade completed successfully!", ephemeral=True
                )
        else:
            reason = result.get("reason", "unknown")
            detail = result.get("detail", "")
            self.state.confirmed_a = False
            self.state.confirmed_b = False
            self.render()
            await self._edit_message()
            if interaction is not None:
                await interaction.response.send_message(
                    f"Trade failed: **{reason}** {detail}".strip(), ephemeral=True
                )

    async def on_timeout(self):
        self._cancel_execution_countdown()
        self.state.cancelled = True
        self.render()
        for child in self._get_all_controls():
            if hasattr(child, "disabled"):
                child.disabled = True
        self.stop()
        self.cog._active_trades.pop(self.user_a.id, None)
        self.cog._active_trades.pop(self.user_b.id, None)
        if self.message:
            try:
                await self._edit_message()
            except Exception:
                pass

class TradingStoreMixin:
    def transfer_credits(
        self, guild_id: int, from_user_id: int, to_user_id: int, amount: int
    ) -> bool:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE member_economy
                        SET credits = credits - %s,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE guild_id = %s AND user_id = %s AND credits >= %s
                        RETURNING credits
                        """,
                        (amount, guild_id, from_user_id, amount),
                    )
                    if cursor.fetchone() is None:
                        return False
                    cursor.execute(
                        """
                        INSERT INTO member_economy (guild_id, user_id, credits, updated_at)
                        VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
                        ON CONFLICT (guild_id, user_id) DO UPDATE SET
                            credits = member_economy.credits + EXCLUDED.credits,
                            updated_at = CURRENT_TIMESTAMP
                        """,
                        (guild_id, to_user_id, amount),
                    )
                    return True
        finally:
            self._pool.putconn(conn)

    def execute_trade(
        self, guild_id: int, items_a: dict, items_b: dict
    ) -> dict[str, Any]:
        user_a_id = items_a["user_id"]
        user_b_id = items_b["user_id"]
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT credits FROM member_economy WHERE guild_id = %s AND user_id = %s FOR UPDATE",
                        (guild_id, user_a_id),
                    )
                    row_a = cursor.fetchone()
                    balance_a = int(row_a["credits"]) if row_a else 0

                    cursor.execute(
                        "SELECT credits FROM member_economy WHERE guild_id = %s AND user_id = %s FOR UPDATE",
                        (guild_id, user_b_id),
                    )
                    row_b = cursor.fetchone()
                    balance_b = int(row_b["credits"]) if row_b else 0

                    credits_a = int(items_a.get("credits") or 0)
                    credits_b = int(items_b.get("credits") or 0)

                    if credits_a > balance_a:
                        return {
                            "ok": False,
                            "reason": "insufficient_credits",
                            "user": user_a_id,
                            "detail": f"Needs {credits_a:,}, has {balance_a:,}",
                        }
                    if credits_b > balance_b:
                        return {
                            "ok": False,
                            "reason": "insufficient_credits",
                            "user": user_b_id,
                            "detail": f"Needs {credits_b:,}, has {balance_b:,}",
                        }

                    pet_ids_a = [p[0] for p in (items_a.get("pets") or [])]
                    pet_ids_b = [p[0] for p in (items_b.get("pets") or [])]

                    all_pet_ids = pet_ids_a + pet_ids_b
                    if all_pet_ids:
                        placeholders = ",".join(["%s"] * len(all_pet_ids))
                        cursor.execute(
                            f"SELECT id, guild_id, user_id FROM user_pets WHERE id IN ({placeholders}) FOR UPDATE",
                            all_pet_ids,
                        )
                        owned = {
                            int(row["id"]): int(row["user_id"])
                            for row in cursor.fetchall()
                        }
                        for pid in pet_ids_a:
                            if pid not in owned:
                                return {
                                    "ok": False,
                                    "reason": "pet_not_found",
                                    "user": user_a_id,
                                    "pet_id": pid,
                                }
                            if owned[pid] != user_a_id:
                                return {
                                    "ok": False,
                                    "reason": "pet_not_owned",
                                    "user": user_a_id,
                                    "pet_id": pid,
                                }
                        for pid in pet_ids_b:
                            if pid not in owned:
                                return {
                                    "ok": False,
                                    "reason": "pet_not_found",
                                    "user": user_b_id,
                                    "pet_id": pid,
                                }
                            if owned[pid] != user_b_id:
                                return {
                                    "ok": False,
                                    "reason": "pet_not_owned",
                                    "user": user_b_id,
                                    "pet_id": pid,
                                }

                    item_keys_a = list((items_a.get("economy_items") or {}).keys())
                    item_keys_b = list((items_b.get("economy_items") or {}).keys())
                    all_item_keys = sorted(set(item_keys_a + item_keys_b))

                    if all_item_keys:
                        placeholders = ",".join(["%s"] * len(all_item_keys))
                        cursor.execute(
                            f"SELECT item_key, user_id, quantity FROM economy_items WHERE guild_id = %s AND user_id = %s AND item_key IN ({placeholders}) FOR UPDATE",
                            [guild_id, user_a_id] + all_item_keys,
                        )
                        eco_a = {
                            row["item_key"]: int(row["quantity"])
                            for row in cursor.fetchall()
                        }
                        cursor.execute(
                            f"SELECT item_key, user_id, quantity FROM economy_items WHERE guild_id = %s AND user_id = %s AND item_key IN ({placeholders}) FOR UPDATE",
                            [guild_id, user_b_id] + all_item_keys,
                        )
                        eco_b = {
                            row["item_key"]: int(row["quantity"])
                            for row in cursor.fetchall()
                        }

                        for ikey, qty in (items_a.get("economy_items") or {}).items():
                            if eco_a.get(ikey, 0) < qty:
                                return {
                                    "ok": False,
                                    "reason": "insufficient_items",
                                    "user": user_a_id,
                                    "item": ikey,
                                }
                        for ikey, qty in (items_b.get("economy_items") or {}).items():
                            if eco_b.get(ikey, 0) < qty:
                                return {
                                    "ok": False,
                                    "reason": "insufficient_items",
                                    "user": user_b_id,
                                    "item": ikey,
                                }

                    pet_item_keys_a = list((items_a.get("pet_items") or {}).keys())
                    pet_item_keys_b = list((items_b.get("pet_items") or {}).keys())
                    all_pet_keys = sorted(set(pet_item_keys_a + pet_item_keys_b))

                    if all_pet_keys:
                        placeholders = ",".join(["%s"] * len(all_pet_keys))
                        cursor.execute(
                            f"SELECT item_key, user_id, quantity FROM pet_items WHERE guild_id = %s AND user_id = %s AND item_key IN ({placeholders}) FOR UPDATE",
                            [guild_id, user_a_id] + all_pet_keys,
                        )
                        mat_a = {
                            row["item_key"]: int(row["quantity"])
                            for row in cursor.fetchall()
                        }
                        cursor.execute(
                            f"SELECT item_key, user_id, quantity FROM pet_items WHERE guild_id = %s AND user_id = %s AND item_key IN ({placeholders}) FOR UPDATE",
                            [guild_id, user_b_id] + all_pet_keys,
                        )
                        mat_b = {
                            row["item_key"]: int(row["quantity"])
                            for row in cursor.fetchall()
                        }

                        for pkey, qty in (items_a.get("pet_items") or {}).items():
                            if mat_a.get(pkey, 0) < qty:
                                return {
                                    "ok": False,
                                    "reason": "insufficient_materials",
                                    "user": user_a_id,
                                    "item": pkey,
                                }
                        for pkey, qty in (items_b.get("pet_items") or {}).items():
                            if mat_b.get(pkey, 0) < qty:
                                return {
                                    "ok": False,
                                    "reason": "insufficient_materials",
                                    "user": user_b_id,
                                    "item": pkey,
                                }

                    if credits_a > 0:
                        cursor.execute(
                            "UPDATE member_economy SET credits = credits - %s, updated_at = CURRENT_TIMESTAMP WHERE guild_id = %s AND user_id = %s",
                            (credits_a, guild_id, user_a_id),
                        )
                        cursor.execute(
                            "INSERT INTO member_economy (guild_id, user_id, credits, updated_at) VALUES (%s, %s, %s, CURRENT_TIMESTAMP) ON CONFLICT (guild_id, user_id) DO UPDATE SET credits = member_economy.credits + EXCLUDED.credits, updated_at = CURRENT_TIMESTAMP",
                            (guild_id, user_b_id, credits_a),
                        )
                    if credits_b > 0:
                        cursor.execute(
                            "UPDATE member_economy SET credits = credits - %s, updated_at = CURRENT_TIMESTAMP WHERE guild_id = %s AND user_id = %s",
                            (credits_b, guild_id, user_b_id),
                        )
                        cursor.execute(
                            "INSERT INTO member_economy (guild_id, user_id, credits, updated_at) VALUES (%s, %s, %s, CURRENT_TIMESTAMP) ON CONFLICT (guild_id, user_id) DO UPDATE SET credits = member_economy.credits + EXCLUDED.credits, updated_at = CURRENT_TIMESTAMP",
                            (guild_id, user_a_id, credits_b),
                        )

                    if pet_ids_a:
                        placeholders = ",".join(["%s"] * len(pet_ids_a))
                        cursor.execute(
                            f"UPDATE user_pets SET user_id = %s WHERE id IN ({placeholders})",
                            [user_b_id] + pet_ids_a,
                        )
                    if pet_ids_b:
                        placeholders = ",".join(["%s"] * len(pet_ids_b))
                        cursor.execute(
                            f"UPDATE user_pets SET user_id = %s WHERE id IN ({placeholders})",
                            [user_a_id] + pet_ids_b,
                        )

                    for ikey, qty in (items_a.get("economy_items") or {}).items():
                        cursor.execute(
                            "UPDATE economy_items SET quantity = GREATEST(0, quantity - %s), updated_at = CURRENT_TIMESTAMP WHERE guild_id = %s AND user_id = %s AND item_key = %s",
                            (qty, guild_id, user_a_id, ikey),
                        )
                        cursor.execute(
                            "INSERT INTO economy_items (guild_id, user_id, item_key, quantity, updated_at) VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP) ON CONFLICT (guild_id, user_id, item_key) DO UPDATE SET quantity = GREATEST(0, economy_items.quantity + EXCLUDED.quantity), updated_at = CURRENT_TIMESTAMP",
                            (guild_id, user_b_id, ikey, qty),
                        )
                    for ikey, qty in (items_b.get("economy_items") or {}).items():
                        cursor.execute(
                            "UPDATE economy_items SET quantity = GREATEST(0, quantity - %s), updated_at = CURRENT_TIMESTAMP WHERE guild_id = %s AND user_id = %s AND item_key = %s",
                            (qty, guild_id, user_b_id, ikey),
                        )
                        cursor.execute(
                            "INSERT INTO economy_items (guild_id, user_id, item_key, quantity, updated_at) VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP) ON CONFLICT (guild_id, user_id, item_key) DO UPDATE SET quantity = GREATEST(0, economy_items.quantity + EXCLUDED.quantity), updated_at = CURRENT_TIMESTAMP",
                            (guild_id, user_a_id, ikey, qty),
                        )

                    for pkey, qty in (items_a.get("pet_items") or {}).items():
                        cursor.execute(
                            "UPDATE pet_items SET quantity = GREATEST(0, quantity - %s), updated_at = CURRENT_TIMESTAMP WHERE guild_id = %s AND user_id = %s AND item_key = %s",
                            (qty, guild_id, user_a_id, pkey),
                        )
                        cursor.execute(
                            "INSERT INTO pet_items (guild_id, user_id, item_key, quantity, updated_at) VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP) ON CONFLICT (guild_id, user_id, item_key) DO UPDATE SET quantity = GREATEST(0, pet_items.quantity + EXCLUDED.quantity), updated_at = CURRENT_TIMESTAMP",
                            (guild_id, user_b_id, pkey, qty),
                        )
                    for pkey, qty in (items_b.get("pet_items") or {}).items():
                        cursor.execute(
                            "UPDATE pet_items SET quantity = GREATEST(0, quantity - %s), updated_at = CURRENT_TIMESTAMP WHERE guild_id = %s AND user_id = %s AND item_key = %s",
                            (qty, guild_id, user_b_id, pkey),
                        )
                        cursor.execute(
                            "INSERT INTO pet_items (guild_id, user_id, item_key, quantity, updated_at) VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP) ON CONFLICT (guild_id, user_id, item_key) DO UPDATE SET quantity = GREATEST(0, pet_items.quantity + EXCLUDED.quantity), updated_at = CURRENT_TIMESTAMP",
                            (guild_id, user_a_id, pkey, qty),
                        )

                    return {"ok": True}
        finally:
            self._pool.putconn(conn)

class TradingCog(commands.Cog):
    def __init__(self, bot: Optional[commands.Bot] = None):
        self.bot = bot
        self.color = 0x2B2D31
        self._active_trades: dict[int, TradeView] = {}
        self._pending_trades: dict[int, TradeRequestView] = {}

    @commands.command(
        name="trade",
        help="Trade credits, pets, and items with another player. Usage: .trade @user",
    )
    async def trade_cmd(
        self, ctx: commands.Context, *, target: str = ""
    ):
        if ctx.guild is None:
            return await ctx.send("This command can only be used in a server.")

        target = target.strip()
        if not target:
            return await ctx.send("Usage: `.trade @user` or `.trade cancel`")

        if target.lower() == "cancel":
            view = self._active_trades.pop(ctx.author.id, None)
            if view:
                self._active_trades.pop(view.user_a.id, None)
                self._active_trades.pop(view.user_b.id, None)
                view.stop()
                return await ctx.send("✅ Your active trade has been forcefully cancelled.")
            pending = self._pending_trades.get(ctx.author.id)
            if pending:
                await pending.cancel_from_command()
                return await ctx.send("✅ Your pending trade request was cancelled.")
            return await ctx.send("You do not have an active trade to cancel.")

        try:
            member = await commands.MemberConverter().convert(ctx, target)
        except commands.BadArgument:
            return await ctx.send("Could not find that user. Usage: `.trade @user`")

        if member.bot:
            return await ctx.send("You cannot trade with a bot.")
        if member.id == ctx.author.id:
            return await ctx.send("You cannot trade with yourself.")
        if ctx.author.id in self._active_trades:
            return await ctx.send(
                "You are already in a trade. Finish or cancel it first (`.trade cancel`)."
            )
        if member.id in self._active_trades:
            return await ctx.send(f"{member.display_name} is already in a trade.")
        if ctx.author.id in self._pending_trades:
            return await ctx.send(
                "You already have a pending trade request. Cancel it first (`.trade cancel`)."
            )
        if member.id in self._pending_trades:
            return await ctx.send(
                f"{member.display_name} already has a pending trade request."
            )

        view = TradeRequestView(self, ctx.guild.id, ctx.author, member)
        self._pending_trades[ctx.author.id] = view
        self._pending_trades[member.id] = view

        try:
            msg = await ctx.send(
                view=view,
                allowed_mentions=discord.AllowedMentions(users=True),
            )
            view.message = msg
        except Exception:
            self._pending_trades.pop(ctx.author.id, None)
            self._pending_trades.pop(member.id, None)
            raise

    @commands.command(
        name="transfer",
        aliases=["give", "pay"],
        help="Transfer coins from your wallet to another member.",
    )
    async def transfer(
        self,
        ctx: commands.Context,
        member_or_amount: str = "",
        amount_or_member: str = "",
    ):
        if not member_or_amount or not amount_or_member:
            return await self._send(
                ctx,
                self.create_embed(
                    "💸 Transfer",
                    "Usage: `.transfer @user <amount>` or `.transfer <amount> @user`.",
                    color=0xED4245,
                ),
            )

        member: Optional[discord.Member] = None
        raw_amount = ""
        converter = commands.MemberConverter()
        try:
            member = await converter.convert(ctx, member_or_amount)
            raw_amount = amount_or_member
        except commands.BadArgument:
            raw_amount = member_or_amount
            try:
                member = await converter.convert(ctx, amount_or_member)
            except commands.BadArgument:
                return await self._send(
                    ctx,
                    self.create_embed(
                        "💸 Transfer",
                        "I could not find that member. Use `.transfer @user <amount>`.",
                        color=0xED4245,
                    ),
                )

        if member == ctx.author:
            return await self._send(
                ctx,
                self.create_embed(
                    "💸 Transfer",
                    "You can't transfer money to yourself!",
                    color=0xED4245,
                ),
            )

        if not raw_amount:
            return await self._send(
                ctx,
                self.create_embed(
                    "💸 Transfer",
                    "You need to specify an amount to transfer!",
                    color=0xED4245,
                ),
            )

        amount_key = raw_amount.strip().lower()
        if amount_key in ["all", "max"]:
            amount = await asyncio.to_thread(
                self.store.get_balance, ctx.guild.id, ctx.author.id
            )
        else:
            amount = _investment_amount(raw_amount)
            if amount is None:
                return await self._send(
                    ctx,
                    self.create_embed(
                        "💸 Transfer", "Please provide a valid number.", color=0xED4245
                    ),
                )

        if amount <= 0:
            return await self._send(
                ctx,
                self.create_embed(
                    "💸 Transfer",
                    "You must transfer a positive amount.",
                    color=0xED4245,
                ),
            )

        balance = await asyncio.to_thread(
            self.store.get_balance, ctx.guild.id, ctx.author.id
        )
        if balance < amount:
            return await self._send(
                ctx,
                self.create_embed(
                    "💸 Transfer", f"You only have **{balance:,} cr**!", color=0xED4245
                ),
            )

        success = await asyncio.to_thread(
            self.store.transfer_credits, ctx.guild.id, ctx.author.id, member.id, amount
        )
        if not success:
            return await self._send(
                ctx,
                self.create_embed(
                    "💸 Transfer",
                    "Your balance changed before the transfer could be placed. Try again.",
                    color=0xED4245,
                ),
            )

        embed = self.create_embed(
            "💸 Transfer",
            f"Successfully transferred **{amount:,} cr** to {member.mention}.",
            color=0x2ECC71,
        )
        await self._send(ctx, embed)

