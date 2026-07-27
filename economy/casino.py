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
    CARD_SUITS,
    CASINO_GAME_LIST,
    UNO_COLORS,
    UNO_COLOR_RGB,
    UNO_STARTING_HAND_SIZE,
    UNO_WIN_MULTIPLIER,
    _card_rank,
    _create_deck,
    _create_uno_deck,
    _discord_timestamp,
    _normalize_game_name,
    _poker_hand_name,
    _uno_card_file_exists,
    _uno_card_label,
    _uno_hand_file,
    _uno_is_playable_card,
    _uno_load_card_image,
)


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
CASINO_COLORS = {
    "slots": 0xF59E0B,
    "coinflip": 0xFBBF24,
    "dice": 0x38BDF8,
    "roulette": 0xDC2626,
    "blackjack": 0x10B981,
    "mines": 0xF97316,
    "poker": 0xA855F7,
    "win": 0x2ECC71,
    "lose": 0xED4245,
    "neutral": 0x2B2D31,
}
CASINO_ANIMATION_SECONDS = {
    "slots": 2.2,
    "coinflip": 1.8,
    "dice": 1.6,
    "roulette": 2.4,
}
SLOT_PAYOUTS = {
    "7\ufe0f\u20e37\ufe0f\u20e37\ufe0f\u20e3": 50,
    "\U0001f48e\U0001f48e\U0001f48e": 10,
    "\U0001f347\U0001f347\U0001f347": 5,
    "\U0001f34a\U0001f34a\U0001f34a": 4,
    "\U0001f34b\U0001f34b\U0001f34b": 3,
    "\U0001f352\U0001f352\U0001f352": 2,
}
POKER_PAYOUTS = {
    "Royal Flush": 800,
    "Straight Flush": 50,
    "Four of a Kind": 25,
    "Full House": 9,
    "Flush": 6,
    "Straight": 4,
    "Three of a Kind": 3,
    "Two Pair": 2,
    "Jacks or Better": 1,
}
CASINO_ANIMATION_DIR = Path(__file__).resolve().parent.parent / "assets" / "casino"
CASINO_GAMBLING_BG_PATH = CASINO_ANIMATION_DIR / "gambling bg.png"
CASINO_ANIMATIONS = {
    "slots": "slots.gif",
    "coinflip": "coinflip.gif",
    "dice": "dice.gif",
    "roulette": "roulette.gif",
}
_CASINO_BG_CACHE: dict[tuple[int, int], Image.Image] = {}
BLACKJACK_PAYOUT = 2.0
ALL_OR_NOTHING_MIN_BALANCE = 10_000
ALL_OR_NOTHING_WIN_CHANCE = 0.10
ALL_OR_NOTHING_WIN_MULTIPLIER = 5
ALL_OR_NOTHING_COOLDOWN = timedelta(days=7)




def _casino_limits(game_key: str) -> dict[str, int]:
    return CASINO_BET_LIMITS.get(game_key, CASINO_BET_LIMITS["default"])

def _casino_font(size: int, *, bold: bool = True) -> ImageFont.ImageFont:
    assets_dir = Path(__file__).resolve().parent.parent / "assets"
    preferred = "Montserrat-ExtraBold.ttf" if bold else "Montserrat-Bold.ttf"
    candidates = [
        os.path.join(assets_dir, preferred),
        os.path.join(assets_dir, "Montserrat-Bold.ttf"),
        "C:\\Windows\\Fonts\\arialbd.ttf",
        "C:\\Windows\\Fonts\\arial.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _draw_centered_text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    font: ImageFont.ImageFont,
    fill: tuple[int, int, int, int],
    *,
    stroke_width: int = 0,
) -> None:
    draw.text(
        xy,
        text,
        font=font,
        fill=fill,
        anchor="mm",
        stroke_width=stroke_width,
        stroke_fill=(0, 0, 0, 180),
    )


def _casino_background(width: int, height: int) -> Image.Image:
    cache_key = (width, height)
    cached = _CASINO_BG_CACHE.get(cache_key)
    if cached is not None:
        return cached.copy()

    if CASINO_GAMBLING_BG_PATH.exists():
        try:
            with Image.open(CASINO_GAMBLING_BG_PATH) as image:
                frame = ImageOps.fit(
                    image.convert("RGBA"),
                    (width, height),
                    method=Image.Resampling.LANCZOS,
                    centering=(0.5, 0.5),
                )
        except Exception as exc:
            LOGGER.warning("Failed to load gambling GIF background: %s", exc)
            frame = Image.new("RGBA", (width, height), (8, 75, 40, 255))
    else:
        LOGGER.warning("Missing gambling GIF background: %s", CASINO_GAMBLING_BG_PATH)
        frame = Image.new("RGBA", (width, height), (8, 75, 40, 255))

    _CASINO_BG_CACHE[cache_key] = frame.copy()
    return frame


def _casino_base_frame(title: str, accent: tuple[int, int, int]) -> Image.Image:
    width, height = 640, 360
    frame = _casino_background(width, height)
    accent_soft = tuple(max(0, min(255, int(channel))) for channel in accent)

    vignette = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    vignette_draw = ImageDraw.Draw(vignette, "RGBA")
    vignette_draw.rectangle((0, 0, width, height), outline=(0, 0, 0, 130), width=28)
    vignette_draw.rectangle((0, 0, width, height), fill=(0, 0, 0, 34))
    frame.alpha_composite(vignette.filter(ImageFilter.GaussianBlur(18)))

    header = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    header_draw = ImageDraw.Draw(header, "RGBA")
    header_draw.rounded_rectangle(
        (172, 18, 468, 70),
        radius=14,
        fill=(0, 0, 0, 150),
        outline=(*accent_soft, 150),
        width=2,
    )
    frame.alpha_composite(header)
    draw = ImageDraw.Draw(frame, "RGBA")
    _draw_centered_text(
        draw,
        (width // 2, 44),
        title,
        _casino_font(26),
        (255, 255, 255, 238),
        stroke_width=1,
    )
    return frame

def _save_casino_animation(
    frames: list[Image.Image], filename: str
) -> tuple[discord.File, discord.File]:
    gif_buf = io.BytesIO()
    still_buf = io.BytesIO()
    final_frame = frames[-1]
    gif_frames = frames + [final_frame.copy() for _ in range(12)]
    gif_frames[0].convert("P", palette=Image.Palette.ADAPTIVE).save(
        gif_buf,
        format="GIF",
        save_all=True,
        append_images=[
            frame.convert("P", palette=Image.Palette.ADAPTIVE)
            for frame in gif_frames[1:]
        ],
        duration=70,
        optimize=False,
    )
    final_frame.convert("RGB").save(still_buf, format="PNG", optimize=True)
    gif_buf.seek(0)
    still_buf.seek(0)
    stem = filename.rsplit(".", 1)[0]
    return discord.File(gif_buf, filename=filename), discord.File(
        still_buf, filename=f"{stem}_final.png"
    )

def _build_casino_animation_files(
    game_key: str, outcome: dict[str, object] | None = None
) -> tuple[discord.File, discord.File | None] | None:
    from .core import _draw_coin, _draw_dice, _draw_roulette, _draw_slots

    if not outcome:
        file = _casino_animation_file(game_key)
        return (file, None) if file is not None else None

    frame_count = 30
    accent_map = {
        "slots": (255, 107, 157),
        "coinflip": (251, 191, 36),
        "dice": (139, 92, 246),
        "roulette": (220, 38, 38),
    }
    title_map = {
        "slots": "SLOT MACHINE",
        "coinflip": "COIN FLIP",
        "dice": "DICE ROLL",
        "roulette": "ROULETTE",
    }
    if game_key not in accent_map:
        return None

    frames: list[Image.Image] = []
    for idx in range(frame_count):
        frame = _casino_base_frame(title_map[game_key], accent_map[game_key])
        if game_key == "coinflip":
            _draw_coin(frame, str(outcome["result"]), idx, frame_count)
        elif game_key == "dice":
            _draw_dice(
                frame,
                int(outcome["player_roll"]),
                int(outcome["dealer_roll"]),
                idx,
                frame_count,
            )
        elif game_key == "slots":
            _draw_slots(frame, list(outcome["result"]), idx, frame_count)
        elif game_key == "roulette":
            _draw_roulette(
                frame, int(outcome["number"]), str(outcome["color"]), idx, frame_count
            )
        frames.append(frame)
    return _save_casino_animation(frames, CASINO_ANIMATIONS[game_key])

def _casino_animation_file(game_key: str) -> discord.File | None:
    filename = CASINO_ANIMATIONS.get(game_key)
    if not filename:
        return None

    path = os.path.join(CASINO_ANIMATION_DIR, filename)
    if not os.path.exists(path):
        LOGGER.warning("Missing casino animation asset: %s", path)
        return None
    return discord.File(path, filename=filename)

def _casino_embed_view(
    embed: discord.Embed, *, banner_url: str | None = None
) -> discord.ui.LayoutView:
    parts: list[str] = []
    if embed.description:
        parts.append(embed.description)

    for field in embed.fields:
        field_name = str(field.name).strip()
        field_value = str(field.value).strip()
        if field_name and field_value:
            parts.append(f"**{field_name}**\n{field_value}")

    footer = getattr(embed.footer, "text", None)
    if footer:
        parts.append(f"*{footer}*")

    color = embed.color.value if embed.color else CASINO_COLORS["neutral"]
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(
        branded_panel_container(
            title=embed.title or "Soul Casino",
            description="\n\n".join(parts),
            banner_url=banner_url,
            accent_color=color,
            banner_separated=True,
            min_width_chars=None,
        )
    )
    return ensure_layout_view_action_rows(view)

async def _send_casino_animation(
    ctx: commands.Context,
    game_key: str,
    embed: discord.Embed,
    *,
    outcome: dict[str, object] | None = None,
) -> tuple[discord.Message, discord.File | None]:
    files = await asyncio.to_thread(_build_casino_animation_files, game_key, outcome)
    if files is None:
        return await ctx.send(view=_casino_embed_view(embed)), None

    file, final_file = files
    return await ctx.send(
        view=_casino_embed_view(embed, banner_url=f"attachment://{file.filename}"),
        file=file,
    ), final_file

async def _edit_casino_result(
    message: discord.Message,
    embed: discord.Embed,
    *,
    final_file: discord.File | None = None,
) -> discord.Message:
    if final_file is not None:
        return await message.edit(
            view=_casino_embed_view(
                embed, banner_url=f"attachment://{final_file.filename}"
            ),
            attachments=[final_file],
        )
    return await message.edit(view=_casino_embed_view(embed))

class MultiplayerDiceView(discord.ui.LayoutView):
    def __init__(self, cog: Any, ctx: commands.Context, bet: int):
        super().__init__(timeout=120)
        self.cog = cog
        self.guild = ctx.guild
        self.channel_id = ctx.channel.id
        self.leader_id = ctx.author.id
        self.bet = int(bet)
        self.participants: dict[int, int] = {ctx.author.id: self.bet}
        self.message: Optional[discord.Message] = None
        self.started = False
        self.cancelled = False
        self.result_text: Optional[str] = None
        self.render()

    def _member_text(self, user_id: int) -> str:
        member = self.guild.get_member(user_id) if self.guild else None
        return member.mention if member else f"`{user_id}`"

    def _participants_text(self) -> str:
        return "\n".join(
            f"* {self._member_text(user_id)} - **{self.bet:,} cr**"
            for user_id in self.participants
        )

    def _description(self) -> str:
        if self.result_text:
            return self.result_text
        if self.cancelled:
            return "Lobby cancelled. All buy-ins were refunded."
        pot = self.bet * len(self.participants)
        return (
            f"Buy-in: **{self.bet:,} cr**\n"
            f"Players: **{len(self.participants)}/4**\n"
            f"Current pot: **{pot:,} cr**\n\n"
            f"{self._participants_text()}\n\n"
            "Highest roll wins the pot. With four players, the winner gets **4x** the buy-in."
        )

    def render(self) -> None:
        self.clear_items()
        container = branded_panel_container(
            title="Multiplayer Dice",
            description=self._description(),
            accent_color=0x8B5CF6
            if not self.result_text and not self.cancelled
            else 0x22C55E
            if self.result_text
            else 0xF43F5E,
            min_width_chars=None,
        )
        if not self.started and not self.cancelled:
            join_button = discord.ui.Button(
                label="Join", style=discord.ButtonStyle.success
            )
            start_button = discord.ui.Button(
                label="Roll", style=discord.ButtonStyle.primary
            )
            cancel_button = discord.ui.Button(
                label="Cancel", style=discord.ButtonStyle.danger
            )
            join_button.callback = self.join_button
            start_button.callback = self.start_button
            cancel_button.callback = self.cancel_button
            container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
            container.add_item(discord.ui.ActionRow(join_button, start_button, cancel_button))
        self.add_item(container)
        ensure_layout_view_action_rows(self)

    async def _refund_all(self) -> None:
        for user_id, amount in list(self.participants.items()):
            if amount > 0:
                await asyncio.to_thread(
                    self.cog.store.add_credits, self.guild.id, user_id, amount
                )
        self.participants.clear()

    async def join_button(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or interaction.guild.id != self.guild.id:
            return await interaction.response.send_message(
                "This dice lobby is not for this server.", ephemeral=True
            )
        if self.started or self.cancelled:
            return await interaction.response.send_message(
                "This dice lobby is already closed.", ephemeral=True
            )
        if interaction.user.id in self.participants:
            return await interaction.response.send_message(
                "You are already in this dice lobby.", ephemeral=True
            )
        if len(self.participants) >= 4:
            return await interaction.response.send_message(
                "This dice lobby is full.", ephemeral=True
            )
        removed = await asyncio.to_thread(
            self.cog.store.remove_credits,
            self.guild.id,
            interaction.user.id,
            self.bet,
        )
        if not removed:
            balance = await asyncio.to_thread(
                self.cog.store.get_balance, self.guild.id, interaction.user.id
            )
            return await interaction.response.send_message(
                f"You need **{self.bet:,} cr** to join. Wallet: **{balance:,} cr**.",
                ephemeral=True,
            )
        self.participants[interaction.user.id] = self.bet
        self.render()
        await interaction.response.edit_message(view=self)

    async def start_button(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.leader_id:
            return await interaction.response.send_message(
                "Only the lobby leader can roll.", ephemeral=True
            )
        if len(self.participants) < 2:
            return await interaction.response.send_message(
                "You need at least two players.", ephemeral=True
            )
        self.started = True
        rolls = {user_id: random.randint(1, 6) for user_id in self.participants}
        reroll_lines: list[str] = []
        winner_id: int | None = None
        for _ in range(20):
            highest = max(rolls.values())
            tied = [user_id for user_id, roll in rolls.items() if roll == highest]
            if len(tied) == 1:
                winner_id = tied[0]
                break
            reroll_lines.append(
                "Tie reroll: "
                + ", ".join(f"{self._member_text(user_id)} rolled **{highest}**" for user_id in tied)
            )
            for user_id in tied:
                rolls[user_id] = random.randint(1, 6)
        if winner_id is None:
            highest = max(rolls.values())
            tied = [user_id for user_id, roll in rolls.items() if roll == highest]
            winner_id = random.choice(tied)
            reroll_lines.append(
                "Final tie breaker: "
                + ", ".join(self._member_text(user_id) for user_id in tied)
            )

        pot = self.bet * len(self.participants)
        await asyncio.to_thread(self.cog.store.add_credits, self.guild.id, winner_id, pot)
        roll_lines = [
            f"* {self._member_text(user_id)} rolled **{roll}**"
            for user_id, roll in rolls.items()
        ]
        self.result_text = (
            f"Winner: {self._member_text(winner_id)}\n"
            f"Payout: **{pot:,} cr** (**{len(self.participants)}x** buy-in)\n\n"
            "**Rolls**\n"
            + "\n".join(roll_lines)
        )
        if reroll_lines:
            self.result_text += "\n\n**Ties**\n" + "\n".join(reroll_lines[-3:])
        self.render()
        self.stop()
        await interaction.response.edit_message(view=self)

    async def cancel_button(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.leader_id:
            return await interaction.response.send_message(
                "Only the lobby leader can cancel.", ephemeral=True
            )
        self.cancelled = True
        await self._refund_all()
        self.render()
        self.stop()
        await interaction.response.edit_message(view=self)

    async def on_timeout(self) -> None:
        if self.started or self.cancelled:
            return
        self.cancelled = True
        await self._refund_all()
        self.render()
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass

class UnoPlayCardSelectView(discord.ui.View):
    def __init__(
        self,
        game_view: "UnoGameView",
        user_id: int,
        playable_indexes: list[int],
    ):
        super().__init__(timeout=60)
        self.game_view = game_view
        self.user_id = user_id
        options: list[discord.SelectOption] = []
        hand = game_view.hands.get(user_id, [])
        for hand_index in playable_indexes[:25]:
            if hand_index >= len(hand):
                continue
            card = hand[hand_index]
            options.append(
                discord.SelectOption(
                    label=_uno_card_label(card)[:100],
                    value=str(hand_index),
                    description=f"Card {hand_index + 1} in your hand",
                )
            )
        select = discord.ui.Select(
            placeholder="Choose a card to play",
            min_values=1,
            max_values=1,
            options=options,
        )
        select.callback = self.select_card
        self.add_item(select)

    async def select_card(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message(
                "This card picker is not yours.", ephemeral=True
            )
        hand_index = int(self.children[0].values[0])
        await self.game_view.play_card(interaction, hand_index)

class UnoWildColorChoiceView(discord.ui.View):
    def __init__(self, game_view: "UnoGameView", user_id: int, hand_index: int):
        super().__init__(timeout=45)
        self.game_view = game_view
        self.user_id = user_id
        self.hand_index = hand_index
        for color in UNO_COLORS:
            button = discord.ui.Button(
                label=color,
                style=discord.ButtonStyle.primary
                if color == "Blue"
                else discord.ButtonStyle.success
                if color == "Green"
                else discord.ButtonStyle.danger
                if color == "Red"
                else discord.ButtonStyle.secondary,
            )
            button.callback = self._make_callback(color)
            self.add_item(button)

    def _make_callback(self, color: str):
        async def callback(interaction: discord.Interaction) -> None:
            if interaction.user.id != self.user_id:
                return await interaction.response.send_message(
                    "This color picker is not yours.", ephemeral=True
                )
            await self.game_view.play_card(
                interaction, self.hand_index, chosen_color=color
            )

        return callback

class UnoGameView(discord.ui.LayoutView):
    def __init__(self, cog: Any, ctx: commands.Context, bet: int):
        super().__init__(timeout=900)
        self.cog = cog
        self.guild = ctx.guild
        self.channel_id = ctx.channel.id
        self.leader_id = ctx.author.id
        self.bet = int(bet)
        self.participants: list[int] = [ctx.author.id]
        self.hands: dict[int, list[tuple[str, str, str]]] = {}
        self.deck: list[tuple[str, str, str]] = []
        self.discard: list[tuple[str, str, str]] = []
        self.top_card: Optional[tuple[str, str, str]] = None
        self.active_color: Optional[str] = None
        self.direction = 1
        self.current_index = 0
        self.started = False
        self.finished = False
        self.cancelled = False
        self.last_action = "Waiting for 2-4 players."
        self.message: Optional[discord.Message] = None
        self.render()

    def _register(self, user_id: int) -> None:
        self.cog._active_uno_games[user_id] = self

    def _release_all(self) -> None:
        for user_id in list(self.participants):
            current = self.cog._active_uno_games.get(user_id)
            if current is self:
                self.cog._active_uno_games.pop(user_id, None)

    def _member_text(self, user_id: int) -> str:
        member = self.guild.get_member(user_id) if self.guild else None
        return member.mention if member else f"`{user_id}`"

    def _member_name(self, user_id: int) -> str:
        member = self.guild.get_member(user_id) if self.guild else None
        return member.display_name if member else str(user_id)

    def _short_member_name(self, user_id: int, limit: int = 18) -> str:
        name = self._member_name(user_id).strip() or str(user_id)
        return name if len(name) <= limit else f"{name[: limit - 1]}."

    def _current_player_id(self) -> int:
        return self.participants[self.current_index % len(self.participants)]

    def _player_lines(self) -> str:
        lines: list[str] = []
        for user_id in self.participants:
            marker = "ACTIVE" if self.started and user_id == self._current_player_id() else "SEAT"
            card_count = len(self.hands.get(user_id, []))
            detail = f" - **{card_count}** cards" if self.started else ""
            lines.append(f"`{marker}` {self._member_text(user_id)}{detail}")
        return "\n".join(lines)

    def _top_card_text(self) -> str:
        if not self.top_card:
            return "No card yet"
        color_text = self.active_color or self.top_card[0]
        return f"{_uno_card_label(self.top_card)} | Active color: **{color_text}**"

    def _description(self) -> str:
        if self.finished or self.cancelled:
            return self.last_action
        if not self.started:
            payout = self.bet * UNO_WIN_MULTIPLIER
            return (
                f"**Buy-in:** `{self.bet:,} cr`  **Winner takes:** `{payout:,} cr`\n"
                f"**Seats:** `{len(self.participants)}/4`  **Deal:** `{UNO_STARTING_HAND_SIZE}` cards\n\n"
                f"{self._player_lines()}\n\n"
                "The table opens when the leader starts. Hands stay private in DM."
            )
        return (
            f"**Top:** {self._top_card_text()}\n"
            f"**Turn:** {self._member_text(self._current_player_id())}  "
            f"**Flow:** `{'Clockwise' if self.direction > 0 else 'Counter-clockwise'}`\n\n"
            f"{self._player_lines()}\n\n"
            f"**Table Log**\n{self.last_action}"
        )

    def table_file(self) -> discord.File:
        width, height = 900, 520
        bg = Image.new("RGBA", (width, height), (8, 13, 23, 255))
        draw = ImageDraw.Draw(bg)
        draw.rectangle((0, 0, width, height), fill=(8, 13, 23, 255))
        for radius, alpha in ((360, 58), (260, 78), (160, 100)):
            color = (225, 29, 72, alpha)
            draw.ellipse(
                (450 - radius, 260 - radius, 450 + radius, 260 + radius),
                outline=color,
                width=6,
            )
        draw.rounded_rectangle((44, 34, 856, 486), radius=38, fill=(15, 23, 42, 245), outline=(225, 29, 72, 210), width=4)
        draw.rounded_rectangle((72, 62, 828, 458), radius=30, fill=(18, 83, 75, 220), outline=(45, 212, 191, 90), width=2)

        title_font = _casino_font(36)
        label_font = _casino_font(18)
        small_font = _casino_font(14)
        draw.text((92, 82), "UNO GAMBLE", fill=(255, 255, 255, 255), font=title_font)
        draw.text(
            (92, 126),
            f"Buy-in {self.bet:,} cr  |  Winner {self.bet * UNO_WIN_MULTIPLIER:,} cr",
            fill=(203, 213, 225, 255),
            font=small_font,
        )

        active_rgb = UNO_COLOR_RGB.get(self.active_color or "Wild", (255, 255, 255))
        draw.rounded_rectangle((674, 82, 806, 132), radius=18, fill=(*active_rgb, 255))
        draw.text((694, 98), (self.active_color or "LOBBY").upper(), fill=(255, 255, 255, 255), font=label_font)

        back_card = _uno_load_card_image(("Back", "Back", "Back"), (116, 172))
        top_card = _uno_load_card_image(self.top_card, (128, 188)) if self.top_card else None
        if back_card:
            bg.alpha_composite(back_card, (330, 174))
            draw.text((352, 358), f"Deck {len(self.deck)}", fill=(226, 232, 240, 255), font=small_font)
        if top_card:
            bg.alpha_composite(top_card, (464, 164))
            draw.text((464, 358), _uno_card_label(self.top_card), fill=(255, 255, 255, 255), font=small_font)
        else:
            draw.rounded_rectangle((464, 164, 592, 352), radius=20, fill=(30, 41, 59, 255), outline=(148, 163, 184, 160), width=2)
            draw.text((498, 244), "START", fill=(226, 232, 240, 255), font=label_font)

        seat_positions = [(532, 88), (748, 268), (450, 430), (152, 268)]
        for index, user_id in enumerate(self.participants[:4]):
            x, y = seat_positions[index]
            current = self.started and user_id == self._current_player_id()
            cards = len(self.hands.get(user_id, []))
            seat_fill = (250, 204, 21, 255) if current else (15, 23, 42, 255)
            text_fill = (15, 23, 42, 255) if current else (248, 250, 252, 255)
            draw.rounded_rectangle((x - 112, y - 34, x + 112, y + 34), radius=18, fill=seat_fill, outline=(255, 255, 255, 120), width=2)
            name = self._short_member_name(user_id)
            draw.text((x - 96, y - 22), name, fill=text_fill, font=label_font)
            status = f"{cards} cards" if self.started else "ready"
            draw.text((x - 96, y + 4), status, fill=text_fill, font=small_font)

        buffer = io.BytesIO()
        bg.convert("RGB").save(buffer, format="PNG", optimize=True)
        buffer.seek(0)
        return discord.File(buffer, filename="uno-table.png")

    def render(self) -> None:
        self.clear_items()
        container = branded_panel_container(
            title="UNO Gamble",
            description=self._description(),
            banner_url="attachment://uno-table.png",
            banner_separated=True,
            accent_color=0xE11D48
            if not self.finished and not self.cancelled
            else 0x22C55E
            if self.finished
            else 0x6B7280,
            min_width_chars=None,
        )
        container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
        if not self.started and not self.cancelled:
            join_button = discord.ui.Button(
                label="Join", style=discord.ButtonStyle.success
            )
            start_button = discord.ui.Button(
                label="Start", style=discord.ButtonStyle.primary
            )
            cancel_button = discord.ui.Button(
                label="Cancel", style=discord.ButtonStyle.danger
            )
            join_button.callback = self.join_button
            start_button.callback = self.start_button
            cancel_button.callback = self.cancel_button
            container.add_item(discord.ui.ActionRow(join_button, start_button, cancel_button))
        elif self.started and not self.finished and not self.cancelled:
            play_button = discord.ui.Button(
                label="Play Card", style=discord.ButtonStyle.primary
            )
            draw_button = discord.ui.Button(
                label="Draw", style=discord.ButtonStyle.secondary
            )
            hand_button = discord.ui.Button(
                label="Hand", style=discord.ButtonStyle.secondary
            )
            play_button.callback = self.play_button
            draw_button.callback = self.draw_button
            hand_button.callback = self.hand_button
            container.add_item(discord.ui.ActionRow(play_button, draw_button, hand_button))
        self.add_item(container)
        ensure_layout_view_action_rows(self)

    async def _refund_all(self) -> None:
        for user_id in list(self.participants):
            await asyncio.to_thread(
                self.cog.store.add_credits, self.guild.id, user_id, self.bet
            )

    def _draw_card(self) -> tuple[str, str, str]:
        if not self.deck:
            if self.discard:
                self.deck = list(self.discard)
                self.discard.clear()
                random.shuffle(self.deck)
            else:
                self.deck = _create_uno_deck()
        return self.deck.pop()

    def _is_playable(self, card: tuple[str, str, str]) -> bool:
        return _uno_is_playable_card(card, self.top_card, self.active_color)

    def _playable_indexes(self, user_id: int) -> list[int]:
        return [
            index
            for index, card in enumerate(self.hands.get(user_id, []))
            if self._is_playable(card)
        ]

    def _choose_wild_color(self, user_id: int) -> str:
        counts = {color: 0 for color in UNO_COLORS}
        for card in self.hands.get(user_id, []):
            if card[0] in counts:
                counts[card[0]] += 1
        return max(counts, key=counts.get)

    def _advance_turn(self, steps: int = 1) -> None:
        self.current_index = (
            self.current_index + (steps * self.direction)
        ) % len(self.participants)

    async def _send_hand_dm(self, user_id: int) -> bool:
        member = self.guild.get_member(user_id) if self.guild else None
        if member is None:
            return False
        hand = self.hands.get(user_id, [])
        file = _uno_hand_file(
            user_id, hand, top_card=self.top_card, active_color=self.active_color
        )
        content = (
            f"**UNO Table - {self.guild.name}**\n"
            f"Top: **{_uno_card_label(self.top_card) if self.top_card else 'None'}**\n"
            f"Color: **{self.active_color or 'None'}**\n"
            f"Turn: **{'You' if user_id == self._current_player_id() else self._member_name(self._current_player_id())}**\n"
            "Green outlines are playable right now."
        )
        try:
            if file:
                await member.send(content, file=file)
            else:
                cards = ", ".join(_uno_card_label(card) for card in hand) or "No cards"
                await member.send(f"{content}\nCards: {cards}")
            return True
        except discord.HTTPException:
            return False

    async def _send_all_hands(self) -> None:
        for user_id in self.participants:
            await self._send_hand_dm(user_id)

    async def join_button(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or interaction.guild.id != self.guild.id:
            return await interaction.response.send_message(
                "This UNO lobby is not for this server.", ephemeral=True
            )
        if self.started or self.cancelled:
            return await interaction.response.send_message(
                "This UNO lobby is already closed.", ephemeral=True
            )
        if interaction.user.id in self.participants:
            return await interaction.response.send_message(
                "You are already in this UNO lobby.", ephemeral=True
            )
        if interaction.user.id in self.cog._active_uno_games:
            return await interaction.response.send_message(
                "You are already in another UNO lobby or game.", ephemeral=True
            )
        if len(self.participants) >= 4:
            return await interaction.response.send_message(
                "This UNO lobby is full.", ephemeral=True
            )
        removed = await asyncio.to_thread(
            self.cog.store.remove_credits,
            self.guild.id,
            interaction.user.id,
            self.bet,
        )
        if not removed:
            balance = await asyncio.to_thread(
                self.cog.store.get_balance, self.guild.id, interaction.user.id
            )
            return await interaction.response.send_message(
                f"You need **{self.bet:,} cr** to join. Wallet: **{balance:,} cr**.",
                ephemeral=True,
            )
        self.participants.append(interaction.user.id)
        self._register(interaction.user.id)
        self.last_action = f"{interaction.user.mention} bought in. Table is heating up."
        self.render()
        await interaction.response.edit_message(
            attachments=[self.table_file()], view=self
        )

    async def start_button(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.leader_id:
            return await interaction.response.send_message(
                "Only the lobby leader can start this UNO game.", ephemeral=True
            )
        if len(self.participants) < 2:
            return await interaction.response.send_message(
                "You need at least two players.", ephemeral=True
            )
        missing_assets = [
            _uno_card_label(card)
            for card in _create_uno_deck()
            if not _uno_card_file_exists(card)
        ]
        if missing_assets:
            return await interaction.response.send_message(
                "UNO card assets are missing. Upload the full card pack first.",
                ephemeral=True,
            )
        self.started = True
        self.deck = _create_uno_deck()
        self.hands = {
            user_id: [self._draw_card() for _ in range(UNO_STARTING_HAND_SIZE)]
            for user_id in self.participants
        }
        self.top_card = self._draw_card()
        while self.top_card[0] == "Wild":
            self.deck.insert(0, self.top_card)
            random.shuffle(self.deck)
            self.top_card = self._draw_card()
        self.active_color = self.top_card[0]
        self.current_index = 0
        self.last_action = "Cards are out. Check DMs, then play fast."
        self.render()
        await interaction.response.edit_message(
            attachments=[self.table_file()], view=self
        )
        await self._send_all_hands()

    async def cancel_button(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.leader_id:
            return await interaction.response.send_message(
                "Only the lobby leader can cancel this UNO lobby.", ephemeral=True
            )
        self.cancelled = True
        await self._refund_all()
        self._release_all()
        self.last_action = "Lobby closed. Buy-ins were refunded."
        self.render()
        self.stop()
        await interaction.response.edit_message(
            attachments=[self.table_file()], view=self
        )

    async def hand_button(self, interaction: discord.Interaction) -> None:
        if interaction.user.id not in self.participants:
            return await interaction.response.send_message(
                "You are not in this UNO game.", ephemeral=True
            )
        sent = await self._send_hand_dm(interaction.user.id)
        if sent:
            return await interaction.response.send_message(
                "Hand sent. Playable cards are outlined in green.", ephemeral=True
            )
        return await interaction.response.send_message(
            "I could not DM your hand. Enable DMs from server members and try again.",
            ephemeral=True,
        )

    async def play_button(self, interaction: discord.Interaction) -> None:
        if not self.started or self.finished:
            return await interaction.response.send_message(
                "This UNO game is not active.", ephemeral=True
            )
        if interaction.user.id != self._current_player_id():
            return await interaction.response.send_message(
                "It is not your turn.", ephemeral=True
            )
        playable = self._playable_indexes(interaction.user.id)
        if not playable:
            return await interaction.response.send_message(
                "No legal card in hand. Draw and keep it moving.", ephemeral=True
            )
        suffix = (
            "\nShowing the first 25 playable cards."
            if len(playable) > 25
            else ""
        )
        await interaction.response.send_message(
            f"Pick your move.{suffix}",
            view=UnoPlayCardSelectView(self, interaction.user.id, playable),
            ephemeral=True,
        )

    async def play_card(
        self,
        interaction: discord.Interaction,
        hand_index: int,
        *,
        chosen_color: Optional[str] = None,
    ) -> None:
        user_id = interaction.user.id
        if user_id != self._current_player_id():
            return await interaction.response.send_message(
                "It is no longer your turn.", ephemeral=True
            )
        hand = self.hands.get(user_id, [])
        if hand_index < 0 or hand_index >= len(hand):
            return await interaction.response.send_message(
                "That card is no longer in your hand.", ephemeral=True
            )
        card = hand[hand_index]
        if not self._is_playable(card):
            return await interaction.response.send_message(
                "That card cannot be played on the current color/value.",
                ephemeral=True,
            )
        if card[0] == "Wild" and chosen_color not in UNO_COLORS:
            return await interaction.response.edit_message(
                content="Choose the color before you throw the Wild.",
                view=UnoWildColorChoiceView(self, user_id, hand_index),
            )

        hand.pop(hand_index)
        if self.top_card:
            self.discard.append(self.top_card)
        self.top_card = card
        color, value, _ = card
        action_lines = [f"{interaction.user.mention} dropped **{_uno_card_label(card)}**."]
        steps = 1
        if color == "Wild":
            self.active_color = chosen_color or self._choose_wild_color(user_id)
            action_lines.append(f"Color locked to **{self.active_color}**.")
        else:
            self.active_color = color

        if value == "Reverse":
            self.direction *= -1
            if len(self.participants) == 2:
                steps = 2
                action_lines.append("Reverse bounced the turn right back.")
            else:
                action_lines.append("Table direction flipped.")
        elif value == "Skip":
            skipped_id = self.participants[
                (self.current_index + self.direction) % len(self.participants)
            ]
            steps = 2
            action_lines.append(f"{self._member_text(skipped_id)} got skipped.")
        elif value == "Draw2":
            target_id = self.participants[
                (self.current_index + self.direction) % len(self.participants)
            ]
            self.hands[target_id].extend(self._draw_card() for _ in range(2))
            steps = 2
            action_lines.append(f"{self._member_text(target_id)} drew 2 and lost turn.")
            await self._send_hand_dm(target_id)
        elif value == "Draw4":
            target_id = self.participants[
                (self.current_index + self.direction) % len(self.participants)
            ]
            self.hands[target_id].extend(self._draw_card() for _ in range(4))
            steps = 2
            action_lines.append(f"{self._member_text(target_id)} drew 4 and lost turn.")
            await self._send_hand_dm(target_id)

        if not hand:
            payout = self.bet * UNO_WIN_MULTIPLIER
            await asyncio.to_thread(
                self.cog.store.add_credits, self.guild.id, user_id, payout
            )
            self.finished = True
            self._release_all()
            action_lines.append(
                f"{interaction.user.mention} won **{payout:,} cr** (**4x** buy-in)."
            )
            self.last_action = "\n".join(action_lines)
            self.render()
            self.stop()
            if self.message:
                await self.message.edit(attachments=[self.table_file()], view=self)
            return await interaction.response.edit_message(
                content=f"Played {_uno_card_label(card)} and won.", view=None
            )

        self._advance_turn(steps)
        if len(hand) == 1:
            action_lines.append(f"{interaction.user.mention} has **UNO**.")
        self.last_action = "\n".join(action_lines)
        self.render()
        if self.message:
            await self.message.edit(attachments=[self.table_file()], view=self)
        await self._send_hand_dm(user_id)
        return await interaction.response.edit_message(
            content=f"Played {_uno_card_label(card)}.", view=None
        )

    async def draw_button(self, interaction: discord.Interaction) -> None:
        if not self.started or self.finished:
            return await interaction.response.send_message(
                "This UNO game is not active.", ephemeral=True
            )
        if interaction.user.id != self._current_player_id():
            return await interaction.response.send_message(
                "It is not your turn.", ephemeral=True
            )
        card = self._draw_card()
        self.hands[interaction.user.id].append(card)
        self.last_action = f"{interaction.user.mention} drew from the deck. Next seat."
        self._advance_turn()
        self.render()
        await interaction.response.edit_message(
            attachments=[self.table_file()], view=self
        )
        await self._send_hand_dm(interaction.user.id)

    async def on_timeout(self) -> None:
        if self.finished or self.cancelled:
            return
        self.cancelled = True
        await self._refund_all()
        self._release_all()
        self.last_action = "Table timed out. Buy-ins were refunded."
        self.render()
        self.stop()
        if self.message:
            try:
                await self.message.edit(attachments=[self.table_file()], view=self)
            except Exception:
                pass

class MinesTileButton(discord.ui.Button):
    def __init__(self, view_ref: "MinesView", tile_index: int, row_index: int):
        self.view_ref = view_ref
        self.tile_index = tile_index
        super().__init__(
            label=view_ref.tile_label(tile_index),
            emoji=view_ref.tile_emoji(tile_index),
            style=view_ref.tile_style(tile_index),
            disabled=view_ref.tile_disabled(tile_index),
            row=row_index,
            custom_id=f"mines:{view_ref.user_id}:{tile_index}",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.view_ref.pick_tile(interaction, self.tile_index)

class MinesView(discord.ui.LayoutView):
    def __init__(
        self,
        cog: Any,
        guild_id: int,
        user_id: int,
        bet: int,
        difficulty: str,
        config: dict[str, Any],
        state: dict = None,
    ):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = guild_id
        self.user_id = user_id
        self.bet = bet
        self.difficulty = difficulty
        self.size = int(config["size"])
        self.mine_count = int(config["mines"])
        self.target_picks = int(config["picks"])
        self.multiplier = float(config["mult"])
        self.row_len = int(self.size**0.5)
        self.message: Optional[discord.Message] = None

        if state:
            self.mine_tiles = set(state["mine_tiles"])
            self.revealed = set(state["revealed"])
            self.hit_mine = state["hit_mine"]
            self.game_over = state["game_over"]
            self.result_text = state["result_text"]
            self.result_color = state["result_color"]
        else:
            self.mine_tiles = set(random.sample(range(self.size), self.mine_count))
            self.revealed: set[int] = set()
            self.hit_mine: Optional[int] = None
            self.game_over = False
            self.result_text = "Pick safe tiles. Hit a mine and the bet is gone."
            self.result_color = CASINO_COLORS["mines"]

    def _save_state(self):
        if getattr(self, "message", None):
            state = {
                "difficulty": self.difficulty,
                "config": {
                    "size": self.size,
                    "mines": self.mine_count,
                    "picks": self.target_picks,
                    "mult": self.multiplier,
                },
                "mine_tiles": list(self.mine_tiles),
                "revealed": list(self.revealed),
                "hit_mine": self.hit_mine,
                "game_over": self.game_over,
                "result_text": self.result_text,
                "result_color": self.result_color,
            }
            asyncio.create_task(
                asyncio.to_thread(
                    self.cog.store.save_casino_game,
                    self.message.id,
                    self.message.channel.id,
                    self.guild_id,
                    self.user_id,
                    "mines",
                    self.bet,
                    state,
                )
            )

    def tile_label(self, tile_index: int) -> str:
        if self.game_over and tile_index in self.mine_tiles:
            return ""
        if tile_index in self.revealed:
            return ""
        return str(tile_index + 1)

    def tile_emoji(self, tile_index: int) -> Optional[str]:
        if tile_index == self.hit_mine:
            return "💥"
        if self.game_over and tile_index in self.mine_tiles:
            return "💣"
        if tile_index in self.revealed:
            return "✅"
        return None

    def tile_style(self, tile_index: int) -> discord.ButtonStyle:
        if tile_index == self.hit_mine:
            return discord.ButtonStyle.danger
        if self.game_over and tile_index in self.mine_tiles:
            return discord.ButtonStyle.danger
        if tile_index in self.revealed:
            return discord.ButtonStyle.success
        return discord.ButtonStyle.secondary

    def tile_disabled(self, tile_index: int) -> bool:
        return self.game_over or tile_index in self.revealed

    def _board_text(self) -> str:
        cells: list[str] = []
        for idx in range(self.size):
            if idx == self.hit_mine:
                cells.append("💥")
            elif self.game_over and idx in self.mine_tiles:
                cells.append("💣")
            elif idx in self.revealed:
                cells.append("✅")
            else:
                cells.append("⬛")
        return "\n".join(
            " ".join(cells[i : i + self.row_len])
            for i in range(0, len(cells), self.row_len)
        )

    def _description(self) -> str:
        payout = int(self.bet * self.multiplier)
        profit = payout - self.bet
        progress = f"{len(self.revealed)}/{self.target_picks}"
        return (
            f"{self._board_text()}\n\n"
            f"**Safe picks:** `{progress}`  **Mines:** `{self.mine_count}`\n"
            f"**Bet:** `{self.bet:,} cr`  **Clear payout:** `{payout:,} cr` (`+{profit:,}`)\n\n"
            f"{self.result_text}"
        )

    def render(self) -> None:
        self.clear_items()
        container = branded_panel_container(
            title=f"💣 Mines - {self.difficulty.title()}",
            description=self._description(),
            accent_color=self.result_color,
            min_width_chars=None,
        )
        container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))

        for row_start in range(0, self.size, self.row_len):
            buttons = [
                MinesTileButton(self, tile_index, row_start // self.row_len)
                for tile_index in range(
                    row_start, min(row_start + self.row_len, self.size)
                )
            ]
            container.add_item(discord.ui.ActionRow(*buttons))

        self.add_item(container)
        ensure_layout_view_action_rows(self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "This mines board is not yours.", ephemeral=True
            )
            return False
        return True

    async def pick_tile(
        self, interaction: discord.Interaction, tile_index: int
    ) -> None:
        if self.game_over:
            await interaction.response.send_message(
                "This mines game is already over.", ephemeral=True
            )
            return
        if tile_index in self.revealed:
            await interaction.response.send_message(
                "That tile is already revealed.", ephemeral=True
            )
            return

        if tile_index in self.mine_tiles:
            self.hit_mine = tile_index
            self.game_over = True
            self.result_color = CASINO_COLORS["lose"]
            self.result_text = f"💥 **BOOM!**\n\n💸 **Lost:** `{self.bet:,} cr`"
        else:
            self.revealed.add(tile_index)
            if len(self.revealed) >= self.target_picks:
                self.game_over = True
                winnings = int(self.bet * self.multiplier)
                winnings, pet_bonus, _ = await self.cog._pet_adjusted_gambling_winnings(
                    self.guild_id, self.user_id, self.bet, winnings
                )
                await asyncio.to_thread(
                    self.cog.store.add_credits, self.guild_id, self.user_id, winnings
                )
                balance = await asyncio.to_thread(
                    self.cog.store.get_balance, self.guild_id, self.user_id
                )
                self.result_color = CASINO_COLORS["win"]
                self.result_text = (
                    f"✅ **CLEARED!**\n\n"
                    f"💰 **Won:** `{winnings:,} cr`\n"
                    f"📈 **Profit:** `+{winnings - self.bet:,} cr`\n"
                    f"{f'Perk Bonus: **+{pet_bonus:,} cr**' + chr(10) if pet_bonus > 0 else ''}"
                    f"**Balance:** `{balance:,} cr`"
                )
            else:
                remaining = self.target_picks - len(self.revealed)
                self.result_text = (
                    f"✅ Safe tile. Pick **{remaining}** more to clear the board."
                )

        self.render()
        await interaction.response.edit_message(view=self)
        if self.game_over:
            if getattr(self, "message", None):
                asyncio.create_task(
                    asyncio.to_thread(
                        self.cog.store.delete_casino_game, self.message.id
                    )
                )
            self.stop()
        else:
            self._save_state()

    async def on_timeout(self) -> None:
        if self.game_over:
            return
        self.game_over = True
        self.result_color = CASINO_COLORS["neutral"]
        self.result_text = "⏱️ **Timed out.** This mines board is locked."
        self.render()
        if self.message is not None:
            try:
                await self.message.edit(embeds=[], view=self)
            except discord.HTTPException:
                pass

class BlackjackView(discord.ui.LayoutView):
    def __init__(
        self,
        cog: Any,
        guild_id: int,
        user_id: int,
        bet: int,
        state: Optional[dict[str, Any]] = None,
    ):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = int(guild_id)
        self.user_id = int(user_id)
        self.bet = int(bet)
        self.message: Optional[discord.Message] = None
        self.settling = False

        if state:
            self.deck = list(state.get("deck") or _create_deck())
            self.player_hand = list(state.get("player_hand") or [])
            self.dealer_hand = list(state.get("dealer_hand") or [])
            self.game_over = bool(state.get("game_over", False))
            self.reveal_dealer = bool(
                state.get("reveal_dealer", state.get("player_stood", False))
            )
            self.doubled = bool(state.get("doubled", False))
            self.result_color = int(
                state.get("result_color") or CASINO_COLORS["blackjack"]
            )
            self.result_text = str(
                state.get("result_text")
                or "Get 21 or closer than the dealer without going over."
            )
        else:
            self.deck = _create_deck()
            self.player_hand = [self._draw_card(), self._draw_card()]
            self.dealer_hand = [self._draw_card(), self._draw_card()]
            self.game_over = False
            self.reveal_dealer = False
            self.doubled = False
            self.result_color = CASINO_COLORS["blackjack"]
            self.result_text = "Get 21 or closer than the dealer without going over."
        while len(self.player_hand) < 2:
            self.player_hand.append(self._draw_card())
        while len(self.dealer_hand) < 2:
            self.dealer_hand.append(self._draw_card())
        self.render()

    def _draw_card(self, preferred_ranks: Optional[set[str]] = None) -> str:
        if not self.deck:
            self.deck = _create_deck()
        if preferred_ranks:
            matching_indexes = [
                index
                for index, card in enumerate(self.deck)
                if self._rank(card) in preferred_ranks
            ]
            if matching_indexes:
                return self.deck.pop(random.choice(matching_indexes))
        return self.deck.pop()

    def _state(self) -> dict[str, Any]:
        return {
            "deck": self.deck,
            "player_hand": self.player_hand,
            "dealer_hand": self.dealer_hand,
            "game_over": self.game_over,
            "player_stood": self.reveal_dealer,
            "reveal_dealer": self.reveal_dealer,
            "doubled": self.doubled,
            "result_text": self.result_text,
            "result_color": self.result_color,
        }

    def _save_state(self) -> None:
        if self.message is None or self.game_over:
            return
        asyncio.create_task(
            asyncio.to_thread(
                self.cog.store.save_casino_game,
                self.message.id,
                self.message.channel.id,
                self.guild_id,
                self.user_id,
                "blackjack",
                self.bet,
                self._state(),
            )
        )

    def _delete_state(self) -> None:
        if self.message is None:
            return
        asyncio.create_task(
            asyncio.to_thread(self.cog.store.delete_casino_game, self.message.id)
        )

    async def _send_database_error(
        self, interaction: discord.Interaction, message: str
    ) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    def _rank(self, card: str) -> str:
        match = re.match(r"^(10|[2-9JQKA])", str(card).strip())
        if match:
            return match.group(1)
        return _card_rank(str(card)).strip()

    def _card_value(self, card: str) -> int:
        rank = self._rank(card)
        if rank in {"J", "Q", "K"}:
            return 10
        if rank == "A":
            return 11
        return int(rank)

    def _score(self, hand: list[str]) -> tuple[int, bool]:
        total = 0
        aces = 0
        for card in hand:
            total += self._card_value(card)
            if self._rank(card) == "A":
                aces += 1

        soft = aces > 0
        while total > 21 and aces:
            total -= 10
            aces -= 1
        soft = aces > 0
        return total, soft

    def _hand_value(self, hand: list[str]) -> int:
        total, _soft = self._score(hand)
        return total

    def _is_soft_17(self, hand: list[str]) -> bool:
        total, soft = self._score(hand)
        return total == 17 and soft

    def _is_natural_blackjack(self, hand: list[str]) -> bool:
        return len(hand) == 2 and self._hand_value(hand) == 21

    def has_natural_blackjack(self) -> bool:
        return self._is_natural_blackjack(
            self.player_hand
        ) or self._is_natural_blackjack(self.dealer_hand)

    def _display_hands(self) -> tuple[str, str, str, str]:
        player_value = str(self._hand_value(self.player_hand))
        player_display = " ".join(self.player_hand)
        if self.reveal_dealer or self.game_over:
            dealer_display = " ".join(self.dealer_hand)
            dealer_value = str(self._hand_value(self.dealer_hand))
        else:
            dealer_display = f"{self.dealer_hand[0]} [hidden]"
            dealer_value = "?"
        return dealer_display, dealer_value, player_display, player_value

    def _description(self) -> str:
        dealer_display, dealer_value, player_display, player_value = (
            self._display_hands()
        )
        return (
            f"**Dealer:** `{dealer_display}`  **Value:** `{dealer_value}`\n"
            f"**You:** `{player_display}`  **Value:** `{player_value}`\n\n"
            f"**Bet:** `{self.bet:,} cr`\n\n"
            f"{self.result_text}"
        )

    def render(self) -> None:
        self.clear_items()
        actions: list[discord.ui.Button] = []
        if not self.game_over and not self.settling:
            hit_btn = discord.ui.Button(
                label="Hit",
                style=discord.ButtonStyle.primary,
                emoji="👆",
                custom_id=f"bj:{self.user_id}:hit",
            )
            stand_btn = discord.ui.Button(
                label="Stand",
                style=discord.ButtonStyle.success,
                emoji="✋",
                custom_id=f"bj:{self.user_id}:stand",
            )
            double_btn = discord.ui.Button(
                label="Double",
                style=discord.ButtonStyle.secondary,
                emoji="2️⃣",
                disabled=self.doubled or len(self.player_hand) != 2,
                custom_id=f"bj:{self.user_id}:double",
            )

            hit_btn.callback = self._handle_hit
            stand_btn.callback = self._handle_stand
            double_btn.callback = self._handle_double
            actions = [hit_btn, stand_btn, double_btn]

        self.add_item(
            branded_panel_container(
                title="🃏 Blackjack",
                description=self._description(),
                accent_color=self.result_color,
                actions=actions,
                min_width_chars=None,
            )
        )
        ensure_layout_view_action_rows(self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "Not your blackjack game.", ephemeral=True
            )
            return False
        return True

    async def _handle_hit(self, interaction: discord.Interaction) -> None:
        if self.game_over or self.settling:
            return await interaction.response.send_message(
                "This blackjack hand is already over.", ephemeral=True
            )
        self.message = (
            interaction.message
            if isinstance(interaction.message, discord.Message)
            else self.message
        )
        self.player_hand.append(self._draw_card())
        player_value = self._hand_value(self.player_hand)
        if player_value >= 21:
            await self._finish(interaction, natural=False)
            return

        self.result_text = "Card drawn. Hit, stand, or double if still available."
        self.render()
        await interaction.response.edit_message(view=self)
        self._save_state()

    async def _handle_stand(self, interaction: discord.Interaction) -> None:
        if self.game_over or self.settling:
            return await interaction.response.send_message(
                "This blackjack hand is already over.", ephemeral=True
            )
        self.message = (
            interaction.message
            if isinstance(interaction.message, discord.Message)
            else self.message
        )
        await self._finish(interaction, natural=False)

    async def _handle_double(self, interaction: discord.Interaction) -> None:
        if self.game_over or self.settling:
            return await interaction.response.send_message(
                "This blackjack hand is already over.", ephemeral=True
            )
        if self.doubled or len(self.player_hand) != 2:
            return await interaction.response.send_message(
                "You can only double before taking another card.", ephemeral=True
            )

        self.message = (
            interaction.message
            if isinstance(interaction.message, discord.Message)
            else self.message
        )
        try:
            removed = await asyncio.to_thread(
                self.cog.store.remove_credits, self.guild_id, self.user_id, self.bet
            )
        except psycopg2.Error:
            LOGGER.exception(
                "Blackjack double down database failure for guild %s user %s",
                self.guild_id,
                self.user_id,
            )
            return await self._send_database_error(
                interaction,
                "Database is temporarily unavailable, so double down could not be placed. Try again in a moment.",
            )
        if not removed:
            return await interaction.response.send_message(
                f"Need **{self.bet:,} cr** to double down.", ephemeral=True
            )

        self.bet *= 2
        self.doubled = True
        self.player_hand.append(self._draw_card())
        await self._finish(interaction, natural=False)

    async def finish_initial_hand(self) -> None:
        await self._settle(natural=True)

    async def _finish(self, interaction: discord.Interaction, *, natural: bool) -> None:
        if self.game_over or self.settling:
            return
        self.settling = True
        try:
            await self._settle(natural=natural)
        except psycopg2.Error:
            self.settling = False
            LOGGER.exception(
                "Blackjack settlement database failure for guild %s user %s",
                self.guild_id,
                self.user_id,
            )
            await self._send_database_error(
                interaction,
                "Database is temporarily unavailable, so blackjack could not finish. Try again in a moment.",
            )
            return

        self.render()
        await interaction.response.edit_message(view=self)
        self._delete_state()
        self.stop()

    async def _settle(self, *, natural: bool) -> None:
        self.reveal_dealer = True
        player_value = self._hand_value(self.player_hand)
        dealer_value = self._hand_value(self.dealer_hand)
        player_blackjack = natural and self._is_natural_blackjack(self.player_hand)
        dealer_blackjack = natural and self._is_natural_blackjack(self.dealer_hand)

        if player_value <= 21 and not player_blackjack and not dealer_blackjack:
            while dealer_value < 17 or self._is_soft_17(self.dealer_hand):
                self.dealer_hand.append(self._draw_card())
                dealer_value = self._hand_value(self.dealer_hand)

        payout = 0
        pet_bonus = 0
        if player_blackjack and dealer_blackjack:
            title = "🤝 PUSH"
            detail = f"Both sides have blackjack. Bet returned: **{self.bet:,} cr**"
            payout = self.bet
            color = CASINO_COLORS["neutral"]
        elif player_blackjack:
            title = "🎉 BLACKJACK"
            payout = int(self.bet * BLACKJACK_PAYOUT)
            payout, pet_bonus, _ = await self.cog._pet_adjusted_gambling_winnings(
                self.guild_id, self.user_id, self.bet, payout
            )
            detail = (
                f"💰 Won **{payout:,} cr**\n📈 Profit: **+{payout - self.bet:,} cr**"
            )
            color = CASINO_COLORS["win"]
        elif dealer_blackjack:
            title = "🎩 DEALER BLACKJACK"
            detail = f"💸 Lost **{self.bet:,} cr**"
            color = CASINO_COLORS["lose"]
        elif player_value > 21:
            title = "💥 BUST"
            detail = f"💸 Lost **{self.bet:,} cr**"
            color = CASINO_COLORS["lose"]
        elif dealer_value > 21:
            title = "🎉 DEALER BUST"
            payout = self.bet * 2
            payout, pet_bonus, _ = await self.cog._pet_adjusted_gambling_winnings(
                self.guild_id, self.user_id, self.bet, payout
            )
            detail = (
                f"💰 Won **{payout:,} cr**\n📈 Profit: **+{payout - self.bet:,} cr**"
            )
            color = CASINO_COLORS["win"]
        elif player_value > dealer_value:
            title = "✅ YOU WIN"
            payout = self.bet * 2
            payout, pet_bonus, _ = await self.cog._pet_adjusted_gambling_winnings(
                self.guild_id, self.user_id, self.bet, payout
            )
            detail = (
                f"💰 Won **{payout:,} cr**\n📈 Profit: **+{payout - self.bet:,} cr**"
            )
            color = CASINO_COLORS["win"]
        elif player_value < dealer_value:
            title = "❌ DEALER WINS"
            detail = f"💸 Lost **{self.bet:,} cr**"
            color = CASINO_COLORS["lose"]
        else:
            title = "🤝 PUSH"
            detail = f"Bet returned: **{self.bet:,} cr**"
            payout = self.bet
            color = CASINO_COLORS["neutral"]

        if pet_bonus > 0:
            detail += f"\nPerk Bonus: **+{pet_bonus:,} cr**"
        if payout > 0:
            balance = await asyncio.to_thread(
                self.cog.store.add_credits, self.guild_id, self.user_id, payout
            )
        else:
            balance = await asyncio.to_thread(
                self.cog.store.get_balance, self.guild_id, self.user_id
            )

        self.game_over = True
        self.settling = False
        self.result_color = color
        self.result_text = f"**{title}**\n\n{detail}\n**Balance:** `{balance:,} cr`"

class PokerView(discord.ui.LayoutView):
    def __init__(
        self,
        cog,
        guild_id: int,
        user_id: int,
        bet: int,
        deck: list,
        hand: list,
        state: dict = None,
    ):
        super().__init__(timeout=None)
        self.cog = cog
        self.bet = bet
        self.user_id = user_id
        self.guild_id = guild_id
        self.message: Optional[discord.Message] = None

        if state:
            self.deck = state["deck"]
            self.hand = state["hand"]
            self.held = state["held"]
            self.game_over = state["game_over"]
            self.result_color = state["result_color"]
            self.result_text = state["result_text"]
        else:
            self.deck = deck
            self.hand = hand
            self.held = [False] * 5
            self.game_over = False
            self.result_color = CASINO_COLORS["poker"]
            self.result_text = (
                "Select cards to hold, then draw once to finish the hand."
            )
        return

    def _save_state(self):
        if getattr(self, "message", None):
            state = {
                "deck": self.deck,
                "hand": self.hand,
                "held": self.held,
                "game_over": self.game_over,
                "result_text": self.result_text,
                "result_color": self.result_color,
            }
            asyncio.create_task(
                asyncio.to_thread(
                    self.cog.store.save_casino_game,
                    self.message.id,
                    self.message.channel.id,
                    self.guild_id,
                    self.user_id,
                    "poker",
                    self.bet,
                    state,
                )
            )

        # Add 5 card buttons
        for i in range(5):
            btn = discord.ui.Button(
                label=self.hand[i],
                style=discord.ButtonStyle.secondary,
                custom_id=f"card_{i}",
                row=0,
            )
            btn.callback = self.make_toggle_callback(i, btn)
            self.add_item(btn)

        # Add Draw button
        self.draw_btn = discord.ui.Button(
            label="Draw",
            style=discord.ButtonStyle.primary,
            custom_id="draw",
            row=1,
            emoji="🎴",
        )
        self.draw_btn.callback = self.draw_callback
        self.add_item(self.draw_btn)

    def _description(self) -> str:
        hand_display = "  ".join(
            f"**[{card}]**" if held else f"`{card}`"
            for card, held in zip(self.hand, self.held)
        )
        return (
            f"**Hand:** {hand_display}\n"
            f"**Bet:** `{self.bet:,} cr`\n\n"
            f"{self.result_text}\n\n"
            "**Best payouts:** Royal Flush 800x | Straight Flush 50x | Four Kind 25x | Full House 9x | Flush 6x"
        )

    def render(self) -> None:
        self.clear_items()
        actions: list[discord.ui.Item[Any]] = []
        if not self.game_over:
            for index, card in enumerate(self.hand):
                button = discord.ui.Button(
                    label=card,
                    style=discord.ButtonStyle.success
                    if self.held[index]
                    else discord.ButtonStyle.secondary,
                    custom_id=f"poker:{self.user_id}:card:{index}",
                )

                async def toggle_callback(
                    interaction: discord.Interaction, card_index: int = index
                ) -> None:
                    await self._toggle_card(interaction, card_index)

                button.callback = toggle_callback
                actions.append(button)

            draw_button = discord.ui.Button(
                label="Draw",
                style=discord.ButtonStyle.primary,
                emoji="🎴",
                custom_id=f"poker:{self.user_id}:draw",
            )
            draw_button.callback = self._draw_cards
            actions.append(draw_button)

        container = branded_panel_container(
            title="🂡 Video Poker",
            description=self._description(),
            accent_color=self.result_color,
            min_width_chars=None,
        )
        if actions:
            container.add_item(
                discord.ui.Separator(spacing=discord.SeparatorSpacing.small)
            )
            container.add_item(discord.ui.ActionRow(*actions[:5]))
            if len(actions) > 5:
                container.add_item(discord.ui.ActionRow(*actions[5:]))
        self.add_item(container)
        ensure_layout_view_action_rows(self)

    async def _toggle_card(self, interaction: discord.Interaction, index: int) -> None:
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message(
                "Not your game.", ephemeral=True
            )
        if self.game_over:
            return await interaction.response.send_message(
                "This poker hand is already over.", ephemeral=True
            )

        self.held[index] = not self.held[index]
        held_count = sum(1 for held in self.held if held)
        self.result_text = f"Holding **{held_count}** card{'s' if held_count != 1 else ''}. Draw when ready."
        self.render()
        await interaction.response.edit_message(view=self)
        self._save_state()

    async def _draw_cards(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message(
                "Not your game.", ephemeral=True
            )
        if self.game_over:
            return await interaction.response.send_message(
                "This poker hand is already over.", ephemeral=True
            )

        self.game_over = True
        for index in range(5):
            if not self.held[index]:
                self.hand[index] = self.deck.pop()

        hand_name = _poker_hand_name(self.hand)
        payout_mult = POKER_PAYOUTS.get(hand_name, 0)
        winnings = self.bet * payout_mult if payout_mult > 0 else 0

        if winnings:
            winnings, pet_bonus, _ = await self.cog._pet_adjusted_gambling_winnings(
                self.guild_id, self.user_id, self.bet, winnings
            )
            await asyncio.to_thread(
                self.cog.store.add_credits, self.guild_id, self.user_id, winnings
            )
            self.result_color = CASINO_COLORS["win"]
            self.result_text = (
                f"✅ **{hand_name}!**\n\n"
                f"💰 **Won:** `{winnings:,} cr`\n"
                f"📈 **Profit:** `+{winnings - self.bet:,} cr`\n"
                f"{f'Perk Bonus: **+{pet_bonus:,} cr**' + chr(10) if pet_bonus > 0 else ''}"
                f"**Payout:** `{payout_mult}x`"
            )
        else:
            self.result_color = CASINO_COLORS["lose"]
            self.result_text = f"❌ **{hand_name}**\n\n💸 **Lost:** `{self.bet:,} cr`"

        balance = await asyncio.to_thread(
            self.cog.store.get_balance, self.guild_id, self.user_id
        )
        self.result_text += f"\n**Balance:** `{balance:,} cr`"
        self.render()
        await interaction.response.edit_message(view=self)
        if getattr(self, "message", None):
            asyncio.create_task(
                asyncio.to_thread(self.cog.store.delete_casino_game, self.message.id)
            )
        self.stop()

    def make_toggle_callback(self, index: int, button: discord.ui.Button):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.user_id:
                return await interaction.response.send_message(
                    "❌ Not your game!", ephemeral=True
                )
            self.held[index] = not self.held[index]
            button.style = (
                discord.ButtonStyle.success
                if self.held[index]
                else discord.ButtonStyle.secondary
            )
            await interaction.response.edit_message(view=self)

        return callback

    async def draw_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message(
                "❌ Not your game!", ephemeral=True
            )

        # Disable all buttons and replace unheld cards
        self.game_over = True
        for i in range(5):
            if not self.held[i]:
                self.hand[i] = self.deck.pop()

            # Update button labels and disable them
            child = self.children[i]
            child.label = self.hand[i]
            child.disabled = True
            child.style = (
                discord.ButtonStyle.success
                if self.held[i]
                else discord.ButtonStyle.secondary
            )

        self.draw_btn.disabled = True

        # Evaluate hand
        from economy_system import _poker_hand_name, CASINO_COLORS, POKER_PAYOUTS

        hand_name = _poker_hand_name(self.hand)
        payout_mult = POKER_PAYOUTS.get(hand_name, 0)
        winnings = self.bet * payout_mult if payout_mult > 0 else 0

        if winnings:
            winnings, pet_bonus, _ = await self.cog._pet_adjusted_gambling_winnings(
                self.guild_id, self.user_id, self.bet, winnings
            )
            await asyncio.to_thread(
                self.cog.store.add_credits, self.guild_id, self.user_id, winnings
            )
            result_text = (
                f"✅ **{hand_name}!**\n\n💰 **Won:** {winnings:,} cr\n📈 **Profit:** +{winnings - self.bet:,} cr\n**Payout:** {payout_mult}x"
                + (f"\nPerk Bonus: **+{pet_bonus:,} cr**" if pet_bonus > 0 else "")
            )
            color = CASINO_COLORS["win"]
        else:
            result_text = f"❌ **{hand_name}**\n\n💸 **Lost:** {self.bet:,} cr"
            color = CASINO_COLORS["lose"]

        balance = await asyncio.to_thread(
            self.cog.store.get_balance, self.guild_id, self.user_id
        )

        embed = discord.Embed(
            title="🂡 Video Poker",
            description=f"**Final Hand:** `{'  '.join(self.hand)}`",
            color=color,
        )
        embed.add_field(name="Result", value=result_text, inline=False)
        embed.set_footer(text=f"Balance: {balance:,} cr | Bet: {self.bet:,} cr")

        await interaction.response.edit_message(embed=embed, view=self)
        self.stop()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return True

    async def on_timeout(self):
        if not self.game_over:
            for child in self.children:
                child.disabled = True
            if self.message:
                try:
                    await self.message.edit(embeds=[], view=self)
                except discord.HTTPException:
                    pass

class CasinoStoreMixin:
    def save_casino_game(
        self,
        message_id: int,
        channel_id: int,
        guild_id: int,
        user_id: int,
        game_type: str,
        bet: int,
        game_state: dict,
    ) -> None:
        conn = self._connect()
        try:
            import json

            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO active_casino_games (message_id, channel_id, guild_id, user_id, game_type, bet, game_state)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (message_id) DO UPDATE SET
                            game_state = EXCLUDED.game_state
                        """,
                        (
                            message_id,
                            channel_id,
                            guild_id,
                            user_id,
                            game_type,
                            bet,
                            json.dumps(game_state),
                        ),
                    )
        finally:
            self._pool.putconn(conn)

    def delete_casino_game(self, message_id: int) -> None:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "DELETE FROM active_casino_games WHERE message_id = %s",
                        (message_id,),
                    )
        finally:
            self._pool.putconn(conn)

    def get_active_casino_games(self) -> list:
        conn = self._connect()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT message_id, channel_id, guild_id, user_id, game_type, bet, game_state FROM active_casino_games"
                )
                return [dict(row) for row in cursor.fetchall()]
        except Exception:
            return []
        finally:
            self._pool.putconn(conn)

    def execute_all_or_nothing(self, guild_id: int, user_id: int) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        ready_at = now + ALL_OR_NOTHING_COOLDOWN
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT expires_at
                        FROM member_cooldowns
                        WHERE guild_id = %s AND user_id = %s AND command_name = %s
                        FOR UPDATE
                        """,
                        (guild_id, user_id, "aln"),
                    )
                    cooldown_row = cursor.fetchone()
                    if cooldown_row:
                        expires_at = _as_utc(cooldown_row["expires_at"])
                        if expires_at and expires_at > now:
                            return {
                                "ok": False,
                                "reason": "cooldown",
                                "ready_at": expires_at,
                            }

                    cursor.execute(
                        """
                        INSERT INTO member_economy (guild_id, user_id, credits, updated_at)
                        VALUES (%s, %s, 0, CURRENT_TIMESTAMP)
                        ON CONFLICT (guild_id, user_id) DO NOTHING
                        """,
                        (guild_id, user_id),
                    )
                    cursor.execute(
                        """
                        SELECT credits
                        FROM member_economy
                        WHERE guild_id = %s AND user_id = %s
                        FOR UPDATE
                        """,
                        (guild_id, user_id),
                    )
                    balance = int(cursor.fetchone()["credits"])
                    if balance < ALL_OR_NOTHING_MIN_BALANCE:
                        return {
                            "ok": False,
                            "reason": "minimum",
                            "balance": balance,
                            "minimum": ALL_OR_NOTHING_MIN_BALANCE,
                        }

                    won = random.random() < ALL_OR_NOTHING_WIN_CHANCE
                    new_balance = balance * ALL_OR_NOTHING_WIN_MULTIPLIER if won else 0
                    cursor.execute(
                        """
                        UPDATE member_economy
                        SET credits = %s, updated_at = CURRENT_TIMESTAMP
                        WHERE guild_id = %s AND user_id = %s
                        """,
                        (new_balance, guild_id, user_id),
                    )
                    cursor.execute(
                        """
                        INSERT INTO member_cooldowns (guild_id, user_id, command_name, expires_at)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (guild_id, user_id, command_name) DO UPDATE SET
                            expires_at = EXCLUDED.expires_at
                        """,
                        (guild_id, user_id, "aln", ready_at),
                    )
                    return {
                        "ok": True,
                        "won": won,
                        "wager": balance,
                        "new_balance": new_balance,
                        "payout": new_balance if won else 0,
                        "ready_at": ready_at,
                    }
        finally:
            self._pool.putconn(conn)

    def apply_scratch_result(
        self, guild_id: int, user_id: int, cost: int, prize: int
    ) -> dict[str, Any]:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE member_economy
                        SET credits = credits - %s + %s,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE guild_id = %s AND user_id = %s AND credits >= %s
                        RETURNING credits
                        """,
                        (cost, prize, guild_id, user_id, cost),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        return {"ok": False, "reason": "balance"}
                    return {"ok": True, "balance": int(row[0])}
        finally:
            self._pool.putconn(conn)

class CasinoCog(commands.Cog):
    @commands.command(name="casino", help="View available casino games")
    async def casino_cmd(self, ctx: commands.Context):
        balance = await asyncio.to_thread(
            self.store.get_balance, ctx.guild.id, ctx.author.id
        )
        embed = discord.Embed(
            title="🎰 Soul Casino",
            description=(
                f"**Balance:** {balance:,} cr\n"
                "Use `.gamble <amount> <game>` or run a game directly."
            ),
            color=0xF1C40F,
        )
        for game in CASINO_GAME_LIST:
            game_key = (
                _normalize_game_name(game["usage"].split()[0].lstrip(".")) or "default"
            )
            if game_key == "aln":
                details = (
                    f"{game['description']}\n"
                    f"Requires wallet **{ALL_OR_NOTHING_MIN_BALANCE:,}+ cr** | Cooldown **7 days**"
                )
            else:
                limits = _casino_limits(game_key)
                details = (
                    f"{game['description']}\n"
                    f"Limits: **{int(limits['min']):,}-{int(limits['max']):,} cr**"
                )
            embed.add_field(
                name=f"{game['emoji']} `{game['usage']}`",
                value=details,
                inline=False,
            )
        embed.set_footer(
            text="Each casino game has its own min/max. You can use all/max."
        )
        await send_v2(ctx, embed)

    @commands.command(
        name="aln",
        aliases=["allornothing"],
        help="All or Nothing: 10% chance to turn your wallet into 5x. Luck does not apply. 7 day cooldown.",
    )
    async def all_or_nothing_cmd(self, ctx: commands.Context):
        result = await asyncio.to_thread(
            self.store.execute_all_or_nothing,
            ctx.guild.id,
            ctx.author.id,
        )
        if not result.get("ok"):
            reason = result.get("reason")
            if reason == "cooldown":
                return await ctx.send(
                    f"You can play `.aln` again <t:{_discord_timestamp(result['ready_at'])}:R>."
                )
            if reason == "minimum":
                return await ctx.send(
                    f"You need at least **{int(result['minimum']):,} cr** in your wallet to play `.aln`. "
                    f"Wallet: **{int(result['balance']):,} cr**."
                )
            return await ctx.send("All or Nothing could not start. Try again later.")

        wager = int(result["wager"])
        new_balance = int(result["new_balance"])
        chance_text = f"{ALL_OR_NOTHING_WIN_CHANCE * 100:.1f}".rstrip("0").rstrip(".")
        if result.get("won"):
            embed = discord.Embed(
                title="All or Nothing - WIN",
                description=(
                    f"You risked **{wager:,} cr** and hit the **{chance_text}%** chance.\n"
                    f"Your wallet is now **{new_balance:,} cr**."
                ),
                color=CASINO_COLORS["win"],
            )
        else:
            embed = discord.Embed(
                title="All or Nothing - LOSS",
                description=(
                    f"You risked **{wager:,} cr** with a **{chance_text}%** win chance and lost it all.\n"
                    "Your wallet is now **0 cr**."
                ),
                color=CASINO_COLORS["lose"],
            )
        embed.set_footer(text="Chance: 10% | Luck does not apply | 7 day cooldown")
        await send_v2(ctx, embed)

    @commands.command(
        name="scratch",
        help="Scratch a lotto card for a random prize (15 minute cooldown).",
    )
    async def scratch(self, ctx: commands.Context):
        await self._check_cooldown(ctx, 900)
        cost = 50
        winnings = (
            random.choice([200, 300, 500, 1000, 5000]) if random.random() < 0.3 else 0
        )
        try:
            result = await asyncio.to_thread(
                self.store.apply_scratch_result,
                ctx.guild.id,
                ctx.author.id,
                cost,
                winnings,
            )
        except Exception:
            LOGGER.exception(
                "Failed to apply scratch result for guild=%s user=%s",
                ctx.guild.id,
                ctx.author.id,
            )
            await self._reset_cooldown(ctx)
            embed = self.create_embed(
                "🎫 Scratch Card",
                "I could not process that scratch card, so your cooldown was reset.",
                color=0xED4245,
            )
            return await self._send(ctx, embed)

        if not result.get("ok"):
            embed = self.create_embed(
                "🎫 Scratch Card",
                f"You need **{cost:,} cr** to buy a scratch card!",
                color=0xED4245,
            )
            await self._send(ctx, embed)
            await self._reset_cooldown(ctx)
            return

        if winnings > 0:
            embed = self.create_embed(
                "🎫 Scratch Card",
                f"🎉 YOU WON! 🎉\nYou scratched the card and won **{winnings:,} cr**!",
                color=0xFFD700,
            )
        else:
            embed = self.create_embed(
                "🎫 Scratch Card",
                f"You scratched the card but it was a dud. You lost **{cost:,} cr**.",
                color=0x95A5A6,
            )

        await self._send(ctx, embed)

