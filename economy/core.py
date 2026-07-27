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


async def _send_casino_animation(
    ctx: commands.Context,
    game_key: str,
    embed: discord.Embed,
    *,
    outcome: dict[str, object] | None = None,
) -> tuple[discord.Message, discord.File | None]:
    from .casino import _send_casino_animation as send_casino_animation

    return await send_casino_animation(ctx, game_key, embed, outcome=outcome)


async def _edit_casino_result(
    message: discord.Message,
    embed: discord.Embed,
    *,
    final_file: discord.File | None = None,
) -> discord.Message:
    from .casino import _edit_casino_result as edit_casino_result

    return await edit_casino_result(message, embed, final_file=final_file)


LOGGER = logging.getLogger("enzo-bot.economy-system")
ACCENT_COLOR = 0x000000
ECONOMY_LEADERBOARD_CARD_WIDTH = 800
ECONOMY_LEADERBOARD_CARD_HEIGHT = 680
DEFAULT_ECONOMY_ACCENT = 0xF1C40F
SHOP_CHANNEL_NOTICE_SECONDS = 20
MAX_BIGINT = 9_223_372_036_854_775_807


class CreditAmountOutOfRange(ValueError):
    def __init__(self, amount: int, *, maximum: int = MAX_BIGINT) -> None:
        self.amount = int(amount)
        self.maximum = int(maximum)
        super().__init__(
            f"Credit amount {self.amount} is outside the supported range 0-{self.maximum}."
        )


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
PET_INFINITE_STOCK_THRESHOLD = 999_999_999
PET_FUSION_EXCLUDED_KEYS = {"kraken"}
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
            {"type": "gambling", "value": 0.20},
            {"type": "all", "value": 0.20},
        ],
        "luck_bonus": 0.15,
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
LUCK_EFFECTS = {
    "luck_potion_i": {"rate": 0.10, "minutes": 5, "price": 100_000},
    "luck_potion_ii": {"rate": 0.15, "minutes": 5, "price": 300_000},
    "luck_potion_iii": {"rate": 0.20, "minutes": 5, "price": 700_000},
}
DEFENSE_EFFECTS = {
    "defense_potion_i": {"rate": 0.10, "minutes": 10, "price": 100_000},
}
TASK_GEAR = {
    "fish": (
        {"item_key": "fishing_rod_ii", "luck": 0.05, "cooldown": 0.05},
        {"item_key": "fishing_rod_iii", "luck": 0.10, "cooldown": 0.10},
    ),
    "mine": (
        {"item_key": "pickaxe_ii", "luck": 0.05, "cooldown": 0.05},
        {"item_key": "pickaxe_iii", "luck": 0.10, "cooldown": 0.10},
    ),
    "hunt": (
        {"item_key": "hunting_weapon_ii", "luck": 0.05, "cooldown": 0.05},
        {"item_key": "hunting_weapon_iii", "luck": 0.10, "cooldown": 0.10},
    ),
}
GEAR_SHOP_ITEMS = {
    key: int(data["price"])
    for key, data in ECONOMY_ITEM_DEFS.items()
    if key
    in {
        "fishing_rod_ii",
        "fishing_rod_iii",
        "pickaxe_ii",
        "pickaxe_iii",
        "hunting_weapon_ii",
        "hunting_weapon_iii",
    }
}
UTILITY_SHOP_ITEMS = {
    "luck_potion_i": 100_000,
    "luck_potion_ii": 300_000,
    "luck_potion_iii": 700_000,
    "defense_potion_i": 100_000,
    "shop_reset_token": 100_000,
}
LOTTERY_TICKET_PRICE = 5_000
LUCK_CHANCE_CAP = 0.80
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
ROULETTE_REDS = {
    1,
    3,
    5,
    7,
    9,
    12,
    14,
    16,
    18,
    19,
    21,
    23,
    25,
    27,
    30,
    32,
    34,
    36,
}
CASINO_GAME_ALIASES = {
    "slot": "slots",
    "slots": "slots",
    "coinflip": "coinflip",
    "cf": "coinflip",
    "flip": "coinflip",
    "dice": "dice",
    "roll": "dice",
    "multidice": "multidice",
    "mdice": "multidice",
    "dicebattle": "multidice",
    "partydice": "multidice",
    "uno": "uno",
    "unogamble": "uno",
    "unobet": "uno",
    "blackjack": "blackjack",
    "bj": "blackjack",
    "roulette": "roulette",
    "roul": "roulette",
    "mines": "mines",
    "minesweeper": "mines",
    "poker": "poker",
    "videopoker": "poker",
    "vp": "poker",
}
CASINO_GAME_LIST = [
    {
        "emoji": "\U0001f3b0",
        "usage": ".slots <amount>",
        "description": "Spin the slot machine for symbol payouts.",
    },
    {
        "emoji": "\U0001fa99",
        "usage": ".coinflip <amount> <heads/tails>",
        "description": "Call a 50/50 coin flip.",
    },
    {
        "emoji": "\U0001f3b2",
        "usage": ".dice <amount>",
        "description": "Roll against the dealer.",
    },
    {
        "emoji": "\U0001f465",
        "usage": ".multidice <amount>",
        "description": "Open a 2-4 player dice lobby.",
    },
    {
        "emoji": "\U0001f0cf",
        "usage": ".uno <amount>",
        "description": "Open a 2-4 player UNO betting lobby.",
    },
    {
        "emoji": "\U0001f0cf",
        "usage": ".blackjack <amount>",
        "description": "Play blackjack against the dealer.",
    },
    {
        "emoji": "\U0001f3a1",
        "usage": ".roulette <amount> <choice>",
        "description": "Bet on roulette colors, ranges, parity, or numbers.",
    },
    {
        "emoji": "\U0001f4a3",
        "usage": ".mines <amount> [easy|medium|hard]",
        "description": "Pick safe tiles before hitting a mine.",
    },
    {
        "emoji": "\U0001f0a1",
        "usage": ".poker <amount>",
        "description": "Play video poker with draw/hold controls.",
    },
    {
        "emoji": "\U0001f4a5",
        "usage": ".aln",
        "description": "All or Nothing turns your wallet into 5x or zero.",
    },
]
CARD_VALUES = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]
CARD_SUITS = ["\u2660", "\u2665", "\u2666", "\u2663"]
UNO_COLORS = ("Red", "Yellow", "Green", "Blue")
UNO_ASSET_DIR = Path(__file__).resolve().parent.parent / "assets" / "uno"
UNO_STARTING_HAND_SIZE = 7
UNO_WIN_MULTIPLIER = 4
UNO_COLOR_RGB = {
    "Red": (239, 68, 68),
    "Yellow": (234, 179, 8),
    "Green": (34, 197, 94),
    "Blue": (59, 130, 246),
    "Wild": (255, 255, 255),
}
SLOT_SYMBOLS = [
    "\U0001f352",
    "\U0001f34b",
    "\U0001f34a",
    "\U0001f347",
    "\U0001f48e",
    "7\ufe0f\u20e3",
]


def _casino_limits(game_key: str) -> dict[str, int]:
    return CASINO_BET_LIMITS.get(game_key, CASINO_BET_LIMITS["default"])


def _casino_font(size: int, *, bold: bool = True) -> ImageFont.ImageFont:
    assets_dir = Path(__file__).resolve().parent.parent / "assets"
    preferred = "Montserrat-ExtraBold.ttf" if bold else "Montserrat-Bold.ttf"
    candidates = [
        assets_dir / preferred,
        assets_dir / "Montserrat-Bold.ttf",
        Path("C:/Windows/Fonts/arialbd.ttf"),
        Path("C:/Windows/Fonts/arial.ttf"),
    ]
    for path in candidates:
        if path.exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


BLACKJACK_PAYOUT = 2.0
ALL_OR_NOTHING_MIN_BALANCE = 10_000
ALL_OR_NOTHING_WIN_CHANCE = 0.10
ALL_OR_NOTHING_WIN_MULTIPLIER = 5
ALL_OR_NOTHING_COOLDOWN = timedelta(days=7)




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

def _getenv_int(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        return int(raw_value)
    except ValueError:
        LOGGER.warning("Invalid %s value %r. Using %s.", name, raw_value, default)
        return default

def _coerce_optional_int(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

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

def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)

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

def _progress_bar(value: int, maximum: int = 100, width: int = 10) -> str:
    maximum = max(1, int(maximum))
    value = max(0, min(int(value), maximum))
    filled = round((value / maximum) * width)
    return "#" * filled + "-" * (width - filled)

def _parse_credit_amount(raw_amount: str) -> Optional[int]:
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

def _discord_timestamp(value: Optional[datetime]) -> int:
    value = _as_utc(value)
    if value is None:
        return int(datetime.now(timezone.utc).timestamp())
    return int(value.timestamp())

def _create_uno_deck() -> list[tuple[str, str, str]]:
    deck: list[tuple[str, str, str]] = []
    for color in UNO_COLORS:
        deck.append((color, "0", f"{color}_0"))
        for value in tuple(str(number) for number in range(1, 10)) + (
            "Skip",
            "Reverse",
            "Draw2",
        ):
            deck.append((color, value, f"{color}_{value}"))
            deck.append((color, value, f"{color}_{value}"))
    for index in range(1, 5):
        deck.append(("Wild", "Wild", f"Wild_{index}"))
        deck.append(("Wild", "Draw4", f"Wild_Draw4_{index}"))
    random.shuffle(deck)
    return deck

def _uno_card_label(card: tuple[str, str, str]) -> str:
    color, value, _ = card
    if color == "Wild":
        return "Wild +4" if value == "Draw4" else "Wild"
    if value == "Draw2":
        return f"{color} Draw 2"
    return f"{color} {value}"

def _uno_card_path(card: tuple[str, str, str]) -> str:
    return os.path.join(UNO_ASSET_DIR, f"{card[2]}.png")

def _uno_card_file_exists(card: tuple[str, str, str]) -> bool:
    return os.path.exists(_uno_card_path(card))

def _uno_is_playable_card(
    card: tuple[str, str, str],
    top_card: Optional[tuple[str, str, str]],
    active_color: Optional[str],
) -> bool:
    if not top_card:
        return True
    color, value, _ = card
    if color == "Wild":
        return True
    top_color, top_value, _ = top_card
    return color == active_color or color == top_color or value == top_value

def _uno_load_card_image(
    card: tuple[str, str, str], size: tuple[int, int]
) -> Optional[Image.Image]:
    path = _uno_card_path(card)
    if not os.path.exists(path):
        return None
    image = Image.open(path).convert("RGBA")
    image.thumbnail(size, Image.Resampling.LANCZOS)
    return image.copy()

def _uno_hand_file(
    user_id: int,
    hand: list[tuple[str, str, str]],
    *,
    top_card: Optional[tuple[str, str, str]] = None,
    active_color: Optional[str] = None,
) -> Optional[discord.File]:
    if not hand:
        return None
    card_images: list[Image.Image] = []
    try:
        for card in hand:
            image = _uno_load_card_image(card, (120, 180))
            if image is not None:
                card_images.append(image)
    except Exception:
        LOGGER.exception("Failed building UNO hand image for user %s", user_id)
        return None
    if not card_images:
        return None

    columns = min(7, len(card_images))
    rows = math.ceil(len(card_images) / columns)
    spacing = 12
    slot_w = 136
    slot_h = 214
    header_h = 72
    width = columns * slot_w + (columns + 1) * spacing
    height = header_h + rows * slot_h + (rows + 1) * spacing
    sheet = Image.new("RGBA", (width, height), (13, 18, 28, 255))
    draw = ImageDraw.Draw(sheet)
    draw.rounded_rectangle((12, 12, width - 12, header_h - 6), radius=18, fill=(24, 30, 43, 255))
    title = "YOUR UNO HAND"
    subtitle = f"Active color: {active_color or 'None'}"
    draw.text((28, 22), title, fill=(248, 250, 252, 255), font=_casino_font(24))
    draw.text((28, 48), subtitle, fill=(148, 163, 184, 255), font=_casino_font(14))
    for index, image in enumerate(card_images):
        row = index // columns
        column = index % columns
        card = hand[index]
        playable = _uno_is_playable_card(card, top_card, active_color)
        slot_x = spacing + column * (slot_w + spacing)
        slot_y = header_h + spacing + row * (slot_h + spacing)
        border = (34, 197, 94, 255) if playable else (51, 65, 85, 255)
        fill = (10, 15, 25, 255) if playable else (17, 24, 39, 255)
        draw.rounded_rectangle(
            (slot_x, slot_y, slot_x + slot_w, slot_y + slot_h),
            radius=16,
            fill=fill,
            outline=border,
            width=4 if playable else 2,
        )
        x = slot_x + (slot_w - image.width) // 2
        y = slot_y + 12 + (180 - image.height) // 2
        sheet.alpha_composite(image, (x, y))
        draw.text(
            (slot_x + 10, slot_y + slot_h - 24),
            f"{index + 1}. {_uno_card_label(card)}",
            fill=(226, 232, 240, 255),
            font=_casino_font(12),
        )

    buffer = io.BytesIO()
    sheet.convert("RGB").save(buffer, format="PNG", optimize=True)
    buffer.seek(0)
    return discord.File(buffer, filename=f"uno-hand-{user_id}.png")

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

def _draw_coin(
    frame: Image.Image, result: str, frame_index: int, frame_count: int
) -> None:
    draw = ImageDraw.Draw(frame, "RGBA")
    cx = 320
    is_resting = frame_index >= frame_count - 10

    if not is_resting:
        t = frame_index / max(1, frame_count - 10 - 1)
        cy = int(220 - 130 * (1 - (t * 2 - 1) ** 2))
        spin = math.cos(t * math.tau * 5)
        coin_h = max(10, int(180 * abs(spin)))
        face = "H" if spin > 0 else "T"
    else:
        cy = 188
        coin_h = 180
        face = result[0].upper()

    draw.ellipse(
        (cx - 120, cy + max(18, coin_h // 3), cx + 120, cy + max(46, coin_h // 3 + 34)),
        fill=(0, 0, 0, 62),
    )

    if not is_resting and coin_h < 170:
        edge_offset = 15
        draw.ellipse(
            (
                cx - 90,
                cy - coin_h // 2 + edge_offset,
                cx + 90,
                cy + coin_h // 2 + edge_offset,
            ),
            fill=(180, 130, 30, 255),
        )

    draw.ellipse(
        (cx - 90, cy - coin_h // 2, cx + 90, cy + coin_h // 2),
        fill=(244, 176, 44, 255),
        outline=(132, 82, 18, 190),
        width=3,
    )

    if coin_h > 40:
        inner_h = int(coin_h * 68 / 90)
        draw.ellipse(
            (cx - 68, cy - inner_h // 2, cx + 68, cy + inner_h // 2),
            outline=(95, 56, 12, 210),
            width=4,
        )
        font_size = max(10, int(coin_h * 64 / 180))
        _draw_centered_text(
            draw,
            (cx, cy - 2),
            face,
            _casino_font(font_size),
            (75, 46, 8, 255),
            stroke_width=1,
        )

    if is_resting:
        _draw_centered_text(
            draw,
            (cx, 312),
            result.upper(),
            _casino_font(32),
            (255, 242, 170, 255),
            stroke_width=1,
        )

def _draw_die(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    value: int,
    size: int,
    fill: tuple[int, int, int],
) -> None:
    draw.rounded_rectangle(
        (x, y, x + size, y + size),
        radius=18,
        fill=(*fill, 255),
        outline=(255, 255, 255, 220),
        width=4,
    )
    pip_map = {
        1: [(0, 0)],
        2: [(-1, -1), (1, 1)],
        3: [(-1, -1), (0, 0), (1, 1)],
        4: [(-1, -1), (1, -1), (-1, 1), (1, 1)],
        5: [(-1, -1), (1, -1), (0, 0), (-1, 1), (1, 1)],
        6: [(-1, -1), (1, -1), (-1, 0), (1, 0), (-1, 1), (1, 1)],
    }
    center_x = x + size // 2
    center_y = y + size // 2
    step = size // 4
    for px, py in pip_map.get(value, []):
        dot_x = center_x + px * step
        dot_y = center_y + py * step
        draw.ellipse(
            (dot_x - 8, dot_y - 8, dot_x + 8, dot_y + 8), fill=(20, 20, 28, 245)
        )

def _draw_dice(
    frame: Image.Image,
    player_roll: int,
    dealer_roll: int,
    frame_index: int,
    frame_count: int,
) -> None:
    draw = ImageDraw.Draw(frame, "RGBA")
    final = frame_index >= frame_count - 10
    player = player_roll if final else ((frame_index + 2) % 6) + 1
    dealer = dealer_roll if final else ((frame_index + 5) % 6) + 1
    bounce = 0 if final else int(abs(math.sin(frame_index / 3)) * 34)
    _draw_die(draw, 205, 130 - bounce, player, 105, (246, 242, 255))
    _draw_die(draw, 330, 130 - (bounce // 2), dealer, 105, (219, 207, 255))
    if final:
        _draw_centered_text(
            draw,
            (257, 280),
            f"YOU: {player_roll}",
            _casino_font(24),
            (255, 255, 255, 235),
            stroke_width=1,
        )
        _draw_centered_text(
            draw,
            (383, 280),
            f"DEALER: {dealer_roll}",
            _casino_font(24),
            (255, 255, 255, 235),
            stroke_width=1,
        )

def _draw_slot_symbol(draw: ImageDraw.ImageDraw, x: int, y: int, symbol: str) -> None:
    if symbol == "7️⃣":
        _draw_centered_text(
            draw, (x + 4, y + 4), "777", _casino_font(42), (0, 0, 0, 150)
        )
        _draw_centered_text(
            draw, (x, y), "777", _casino_font(42), (255, 40, 60, 255), stroke_width=3
        )
    elif symbol == "💎":
        draw.polygon(
            [(x, y - 30), (x + 34, y - 8), (x, y + 34), (x - 34, y - 8)],
            fill=(100, 200, 255, 255),
            outline=(255, 255, 255, 255),
            width=3,
        )
        draw.polygon(
            [(x - 22, y - 12), (x, y - 20), (x + 22, y - 12), (x, y + 16)],
            fill=(180, 230, 255, 255),
        )
    elif symbol == "🍒":
        draw.line((x + 4, y - 24, x - 20, y + 8), fill=(50, 150, 50, 255), width=5)
        draw.line((x + 4, y - 24, x + 24, y + 4), fill=(50, 150, 50, 255), width=5)
        draw.ellipse(
            (x - 36, y - 4, x - 8, y + 24),
            fill=(220, 20, 40, 255),
            outline=(150, 10, 20, 255),
            width=2,
        )
        draw.ellipse(
            (x + 8, y - 8, x + 36, y + 20),
            fill=(220, 20, 40, 255),
            outline=(150, 10, 20, 255),
            width=2,
        )
    elif symbol == "🍇":
        draw.line((x, y - 30, x, y - 16), fill=(100, 180, 50, 255), width=4)
        for gy, gx in [
            (-16, -12),
            (-16, 12),
            (0, -20),
            (0, 0),
            (0, 20),
            (16, -10),
            (16, 10),
            (28, 0),
        ]:
            draw.ellipse(
                (x + gx - 12, y + gy - 12, x + gx + 12, y + gy + 12),
                fill=(140, 50, 200, 255),
                outline=(80, 20, 120, 255),
                width=2,
            )
    elif symbol == "🍊":
        draw.ellipse(
            (x - 26, y - 26, x + 26, y + 26),
            fill=(255, 140, 0, 255),
            outline=(200, 100, 0, 255),
            width=3,
        )
        draw.ellipse((x + 8, y - 20, x + 20, y - 8), fill=(255, 180, 80, 255))
        draw.ellipse((x - 4, y - 34, x + 16, y - 22), fill=(80, 180, 40, 255))
    elif symbol == "🍋":
        draw.ellipse(
            (x - 30, y - 22, x + 30, y + 22),
            fill=(255, 230, 0, 255),
            outline=(200, 180, 0, 255),
            width=3,
        )
        draw.ellipse((x - 16, y - 12, x + 4, y), fill=(255, 250, 100, 255))
        draw.line((x + 18, y - 18, x + 30, y - 30), fill=(80, 180, 40, 255), width=5)
    else:
        _draw_centered_text(
            draw, (x, y), str(symbol), _casino_font(30), (255, 255, 255, 255)
        )

def _draw_slots(
    frame: Image.Image, result: list[str], frame_index: int, frame_count: int
) -> None:
    draw = ImageDraw.Draw(frame, "RGBA")
    reel_x = [210, 320, 430]

    # Main Casing Drop Shadow
    draw.rounded_rectangle((122, 96, 518, 292), radius=28, fill=(0, 0, 0, 150))

    # Outer Machine Casing (Rich Crimson Red with Thick Gold Border)
    draw.rounded_rectangle(
        (122, 86, 518, 282),
        radius=28,
        fill=(140, 15, 25, 255),
        outline=(255, 215, 0, 255),
        width=6,
    )

    # Casing Inner Panel (Darker Red to create depth)
    draw.rounded_rectangle((136, 100, 504, 268), radius=20, fill=(100, 10, 15, 255))

    # Casino Lights (Blinking bulbs around the bezel)
    bulb_on = (255, 255, 220, 255)
    bulb_off = (150, 100, 0, 255)
    for i, bx in enumerate(range(145, 505, 30)):
        color1 = bulb_on if (frame_index // 2 + i) % 2 == 0 else bulb_off
        color2 = bulb_on if (frame_index // 2 + i + 1) % 2 == 0 else bulb_off
        draw.ellipse((bx - 4, 93 - 4, bx + 4, 93 + 4), fill=color1)
        draw.ellipse((bx - 4, 275 - 4, bx + 4, 275 + 4), fill=color2)
    for i, by in enumerate(range(115, 260, 30)):
        color1 = bulb_on if (frame_index // 2 + i) % 2 == 0 else bulb_off
        color2 = bulb_on if (frame_index // 2 + i + 1) % 2 == 0 else bulb_off
        draw.ellipse((129 - 4, by - 4, 129 + 4, by + 4), fill=color1)
        draw.ellipse((511 - 4, by - 4, 511 + 4, by + 4), fill=color2)

    # Inner display area (where the reels are)
    draw.rounded_rectangle(
        (154, 116, 486, 246),
        radius=18,
        fill=(15, 15, 20, 255),
        outline=(200, 180, 120, 255),
        width=4,
    )

    # Payline Indicators (Golden triangles pointing to the center)
    draw.polygon([(143, 181), (154, 174), (154, 188)], fill=(255, 215, 0, 255))
    draw.polygon([(497, 181), (486, 174), (486, 188)], fill=(255, 215, 0, 255))

    stop_frames = [frame_count - 16, frame_count - 10, frame_count - 4]

    for idx, x in enumerate(reel_x):
        # Create a reel surface to act as a clipping mask
        reel_surf = Image.new("RGBA", (100, 122), (0, 0, 0, 255))
        r_draw = ImageDraw.Draw(reel_surf, "RGBA")

        is_stopped = frame_index >= stop_frames[idx]

        if is_stopped:
            symbol = result[idx]
            _draw_slot_symbol(r_draw, 50, 61, symbol)
        else:
            speed = 35 + idx * 5
            offset = (frame_index * speed) % 80

            for y_mult in [-1, 0, 1, 2]:
                y_pos = 61 + (y_mult * 80) + offset - 40
                sym_idx = (frame_index + idx * 3 + y_mult) % len(SLOT_SYMBOLS)
                symbol = SLOT_SYMBOLS[sym_idx]

                _draw_slot_symbol(r_draw, 50, y_pos, symbol)

            reel_surf = reel_surf.filter(ImageFilter.BoxBlur(1))

        frame.alpha_composite(reel_surf, (x - 50, 120))
        # Draw reel border/shadows
        draw.rounded_rectangle(
            (x - 50, 120, x + 50, 242), radius=13, outline=(255, 255, 255, 170), width=3
        )
        # Inner reel shadow for 3D depth
        draw.rounded_rectangle(
            (x - 50, 120, x + 50, 242), radius=13, outline=(0, 0, 0, 120), width=6
        )

    # Red winning line drawn over the reels
    draw.line((154, 181, 486, 181), fill=(255, 50, 50, 210), width=5)

    # Glass reflection over the reels (proper alpha composite to avoid solid white blocks)
    glass = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    ImageDraw.Draw(glass, "RGBA").polygon(
        [(158, 118), (280, 118), (190, 244), (158, 244)], fill=(255, 255, 255, 15)
    )
    frame.alpha_composite(glass)

def _draw_roulette(
    frame: Image.Image, number: int, color_name: str, frame_index: int, frame_count: int
) -> None:
    draw = ImageDraw.Draw(frame, "RGBA")
    cx, cy, radius = 320, 190, 128
    final = frame_index >= frame_count - 10
    start = 0 if final else frame_index * 18
    step = 360 / 37
    seq = [
        0,
        32,
        15,
        19,
        4,
        21,
        2,
        25,
        17,
        34,
        6,
        27,
        13,
        36,
        11,
        30,
        8,
        23,
        10,
        5,
        24,
        16,
        33,
        1,
        20,
        14,
        31,
        9,
        22,
        18,
        29,
        7,
        28,
        12,
        35,
        3,
        26,
    ]
    for i, pocket_num in enumerate(seq):
        pocket_color = (
            (20, 145, 88)
            if pocket_num == 0
            else (220, 38, 38)
            if pocket_num in ROULETTE_REDS
            else (18, 18, 24)
        )
        draw.pieslice(
            (cx - radius, cy - radius, cx + radius, cy + radius),
            start + i * step,
            start + (i + 1) * step,
            fill=(*pocket_color, 255),
            outline=(255, 255, 255, 52),
        )
    draw.ellipse(
        (cx - 82, cy - 82, cx + 82, cy + 82),
        fill=(28, 16, 18, 245),
        outline=(255, 226, 170, 230),
        width=5,
    )
    draw.ellipse(
        (cx - 34, cy - 34, cx + 34, cy + 34),
        fill=(255, 220, 120, 255),
        outline=(125, 82, 24, 255),
        width=4,
    )
    target_idx = seq.index(number)
    ball_angle = math.radians(start + target_idx * step + step / 2)
    bx = cx + math.cos(ball_angle) * (radius - 15)
    by = cy + math.sin(ball_angle) * (radius - 15)
    draw.ellipse(
        (bx - 8, by - 8, bx + 8, by + 8),
        fill=(255, 255, 255, 255),
        outline=(180, 180, 190, 255),
        width=2,
    )
    if final:
        result_color = {
            "red": (255, 95, 95, 255),
            "black": (235, 235, 245, 255),
            "green": (75, 255, 166, 255),
        }.get(color_name, (255, 255, 255, 255))
        _draw_centered_text(
            draw,
            (cx, 314),
            f"{number} {color_name.upper()}",
            _casino_font(31),
            result_color,
            stroke_width=1,
        )

def _parse_bet(raw: str) -> Optional[int]:
    cleaned = raw.strip().replace(",", "").lower()
    if cleaned in ("all", "max"):
        return -1  # sentinel for "all in"
    return _parse_credit_amount(cleaned)

def _normalize_game_name(raw: str) -> Optional[str]:
    cleaned = raw.strip().lower().replace("-", "").replace("_", "")
    return CASINO_GAME_ALIASES.get(cleaned)

def _create_deck() -> list[str]:
    deck = [f"{value}{suit}" for value in CARD_VALUES for suit in CARD_SUITS]
    random.shuffle(deck)
    return deck

def _card_rank(card: str) -> str:
    for suit in CARD_SUITS:
        if card.endswith(suit):
            return card[: -len(suit)]
    return card[:-1]

def _rank_value(rank: str) -> int:
    if rank == "A":
        return 14
    if rank == "K":
        return 13
    if rank == "Q":
        return 12
    if rank == "J":
        return 11
    return int(rank)

def _is_straight(values: list[int]) -> bool:
    unique = sorted(set(values))
    if unique == [2, 3, 4, 5, 14]:
        return True
    return len(unique) == 5 and unique[-1] - unique[0] == 4

def _poker_hand_name(hand: list[str]) -> str:
    ranks = [_rank_value(_card_rank(card)) for card in hand]
    suits = [
        next((suit for suit in CARD_SUITS if card.endswith(suit)), "") for card in hand
    ]
    counts = sorted((ranks.count(rank) for rank in set(ranks)), reverse=True)
    flush = len(set(suits)) == 1
    straight = _is_straight(ranks)
    royal = sorted(ranks) == [10, 11, 12, 13, 14]

    if royal and flush:
        return "Royal Flush"
    if straight and flush:
        return "Straight Flush"
    if counts == [4, 1]:
        return "Four of a Kind"
    if counts == [3, 2]:
        return "Full House"
    if flush:
        return "Flush"
    if straight:
        return "Straight"
    if counts == [3, 1, 1]:
        return "Three of a Kind"
    if counts == [2, 2, 1]:
        return "Two Pair"
    pair_ranks = {rank for rank in set(ranks) if ranks.count(rank) == 2}
    if any(rank >= 11 for rank in pair_ranks):
        return "Jacks or Better"
    return "High Card"

def _roulette_color(number: int) -> str:
    if number == 0:
        return "green"
    return "red" if number in ROULETTE_REDS else "black"

def _parse_roulette_choice(choice: str) -> tuple[str, object, int] | None:
    cleaned = choice.strip().lower().replace(" ", "")
    if cleaned in {"red", "black"}:
        return "color", cleaned, 2
    if cleaned in {"even", "odd"}:
        return "parity", cleaned, 2
    if cleaned in {"low", "1-18", "1to18"}:
        return "range", (1, 18, "low"), 2
    if cleaned in {"high", "19-36", "19to36"}:
        return "range", (19, 36, "high"), 2
    if cleaned in {"1-12", "1to12", "first", "first12"}:
        return "range", (1, 12, "1-12"), 3
    if cleaned in {"13-24", "13to24", "second", "second12"}:
        return "range", (13, 24, "13-24"), 3
    if cleaned in {"25-36", "25to36", "third", "third12"}:
        return "range", (25, 36, "25-36"), 3
    try:
        number = int(cleaned)
    except ValueError:
        return None
    if 0 <= number <= 36:
        return "number", number, 36
    return None

class CoreStoreMixin:
    def __init__(self, db_url: str):
        self.db_url = db_url
        from psycopg2 import pool

        self._pool = pool.ThreadedConnectionPool(
            2, 10, db_url, cursor_factory=psycopg2.extras.DictCursor
        )
        self._bonus_cache = {}

    def _connect(self):
        for _ in range(3):
            try:
                conn = self._pool.getconn()
                with conn.cursor() as c:
                    c.execute("SELECT 1")
                return conn
            except (psycopg2.OperationalError, psycopg2.InterfaceError):
                self._pool.putconn(conn, close=True)
        return self._pool.getconn()

    def process_debt_penalties(self):
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    # Mass optimize debt penalties into a single database query.
                    cursor.execute(
                        """
                        UPDATE member_economy 
                        SET bank = GREATEST(0, bank - LEAST(bank, CAST(debt * 1.05 AS BIGINT))),
                            credits = GREATEST(0, credits - LEAST(credits, CAST(debt * 1.05 AS BIGINT) - LEAST(bank, CAST(debt * 1.05 AS BIGINT)))),
                            debt = 0,
                            debt_deadline = NULL,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE debt > 0 AND debt_deadline < CURRENT_TIMESTAMP
                        """
                    )
        finally:
            self._pool.putconn(conn)

    def process_loan_defaults(self):
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT id, guild_id, lender_id, borrower_id, principal, interest, amount_paid FROM economy_loans WHERE status = 'active' AND due_at < CURRENT_TIMESTAMP FOR UPDATE"
                    )
                    rows = cursor.fetchall()
                    if not rows:
                        return

                    paid_loans = []
                    borrower_keys = []
                    
                    for row in rows:
                        total_owed = max(0, int(row["principal"]) + int(row["interest"]) - int(row["amount_paid"]))
                        if total_owed == 0:
                            paid_loans.append((row["id"],))
                        else:
                            borrower_keys.append((row["guild_id"], row["borrower_id"]))

                    if paid_loans:
                        psycopg2.extras.execute_batch(
                            cursor,
                            "UPDATE economy_loans SET status = 'paid' WHERE id = %s",
                            paid_loans
                        )

                    if not borrower_keys:
                        return

                    # Bulk fetch borrower balances
                    cursor.execute(
                        "SELECT guild_id, user_id, bank, credits FROM member_economy WHERE (guild_id, user_id) IN %s FOR UPDATE",
                        (tuple(borrower_keys),)
                    )
                    borrower_bals = {(r["guild_id"], r["user_id"]): r for r in cursor.fetchall()}

                    member_deductions = []
                    lender_credits = []
                    loan_updates = []

                    for row in rows:
                        total_owed = max(0, int(row["principal"]) + int(row["interest"]) - int(row["amount_paid"]))
                        if total_owed == 0:
                            continue

                        brow = borrower_bals.get((row["guild_id"], row["borrower_id"]))
                        bank = int(brow["bank"]) if brow else 0
                        credits = int(brow["credits"]) if brow else 0
                        
                        rem_pay = total_owed
                        deduct_bank = min(bank, rem_pay)
                        rem_pay -= deduct_bank
                        deduct_credits = min(credits, rem_pay)
                        recovered = deduct_bank + deduct_credits
                        
                        if recovered > 0:
                            member_deductions.append((deduct_bank, deduct_credits, row["guild_id"], row["borrower_id"]))
                            lender_credits.append((row["guild_id"], row["lender_id"], recovered))
                        
                        loan_updates.append((recovered, row["id"]))

                    if member_deductions:
                        psycopg2.extras.execute_batch(
                            cursor,
                            "UPDATE member_economy SET bank = bank - %s, credits = credits - %s, updated_at = CURRENT_TIMESTAMP WHERE guild_id = %s AND user_id = %s",
                            member_deductions
                        )
                    
                    if lender_credits:
                        psycopg2.extras.execute_batch(
                            cursor,
                            """
                            INSERT INTO member_economy (guild_id, user_id, bank, updated_at)
                            VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
                            ON CONFLICT (guild_id, user_id) DO UPDATE SET
                                bank = member_economy.bank + EXCLUDED.bank,
                                updated_at = CURRENT_TIMESTAMP
                            """,
                            lender_credits
                        )

                    if loan_updates:
                        psycopg2.extras.execute_batch(
                            cursor,
                            """
                            UPDATE economy_loans
                            SET amount_paid = LEAST(principal + interest, amount_paid + %s),
                                status = 'defaulted'
                            WHERE id = %s
                            """,
                            loan_updates
                        )
        finally:
            self._pool.putconn(conn)

    @staticmethod
    def _ensure_guild_config_table(cursor) -> None:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS guild_config (
                guild_id BIGINT PRIMARY KEY,
                shop_channel_id BIGINT,
                shop_bypass_role_id BIGINT
            )
            """
        )
        cursor.execute(
            "ALTER TABLE guild_config ADD COLUMN IF NOT EXISTS shop_channel_id BIGINT"
        )
        cursor.execute(
            "ALTER TABLE guild_config ADD COLUMN IF NOT EXISTS shop_bypass_role_id BIGINT"
        )

    def initialize(self):
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "CREATE TABLE IF NOT EXISTS schema_version (version INT PRIMARY KEY)"
                    )
                    cursor.execute("SELECT version FROM schema_version")
                    row = cursor.fetchone()
                    if not row:
                        cursor.execute(
                            "INSERT INTO schema_version (version) VALUES (1)"
                        )

                    # Balance tracking
                    self._ensure_guild_config_table(cursor)

                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS member_economy (
                            guild_id BIGINT NOT NULL,
                            user_id BIGINT NOT NULL,
                            credits BIGINT NOT NULL DEFAULT 0,
                            bank BIGINT NOT NULL DEFAULT 0,
                            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            PRIMARY KEY (guild_id, user_id)
                        )
                        """
                    )
                    cursor.execute(
                        "ALTER TABLE member_economy ADD COLUMN IF NOT EXISTS bank BIGINT NOT NULL DEFAULT 0"
                    )
                    # Shop stock tracking
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS shop_inventory (
                            guild_id BIGINT NOT NULL,
                            role_name TEXT NOT NULL,
                            current_stock INTEGER NOT NULL,
                            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            PRIMARY KEY (guild_id, role_name)
                        )
                        """
                    )
                    # Shop refresh tracking
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS shop_state (
                            guild_id BIGINT NOT NULL PRIMARY KEY,
                            last_refresh TIMESTAMP NOT NULL
                        )
                        """
                    )
                    # Black Market stock and sales tracking
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS blackmarket_inventory (
                            guild_id BIGINT NOT NULL,
                            item_key TEXT NOT NULL,
                            current_stock INTEGER NOT NULL,
                            sale_discount INTEGER NOT NULL DEFAULT 0,
                            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            PRIMARY KEY (guild_id, item_key)
                        )
                        """
                    )
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS blackmarket_state (
                            guild_id BIGINT NOT NULL PRIMARY KEY,
                            last_refresh TIMESTAMP NOT NULL
                        )
                        """
                    )
                    # Purchased roles tracking (for persistence)
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS member_purchases (
                            guild_id BIGINT NOT NULL,
                            user_id BIGINT NOT NULL,
                            role_name TEXT NOT NULL,
                            purchased_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            PRIMARY KEY (guild_id, user_id, role_name)
                        )
                        """
                    )
                    # Member investments, persisted so active investments survive bot restarts.
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS member_investments (
                            id BIGSERIAL PRIMARY KEY,
                            guild_id BIGINT NOT NULL,
                            user_id BIGINT NOT NULL,
                            amount BIGINT NOT NULL,
                            return_amount BIGINT,
                            multiplier DOUBLE PRECISION,
                            status TEXT NOT NULL DEFAULT 'active',
                            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            matures_at TIMESTAMP NOT NULL,
                            settled_at TIMESTAMP
                        )
                        """
                    )
                    cursor.execute(
                        """
                        CREATE INDEX IF NOT EXISTS idx_member_investments_user_status
                        ON member_investments (guild_id, user_id, status, matures_at)
                        """
                    )
                    # Shop custom background tracking
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS shop_settings (
                            guild_id BIGINT NOT NULL PRIMARY KEY,
                            background_url TEXT
                        )
                        """
                    )
                    # Active casino games persistence
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS active_casino_games (
                            message_id BIGINT PRIMARY KEY,
                            channel_id BIGINT NOT NULL,
                            guild_id BIGINT NOT NULL,
                            user_id BIGINT NOT NULL,
                            game_type TEXT NOT NULL,
                            bet BIGINT NOT NULL,
                            game_state JSONB NOT NULL
                        )
                        """
                    )
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS user_pets (
                            id BIGSERIAL PRIMARY KEY,
                            guild_id BIGINT NOT NULL,
                            user_id BIGINT NOT NULL,
                            pet_key TEXT NOT NULL,
                            stage INTEGER NOT NULL DEFAULT 1,
                            custom_name TEXT,
                            is_fused BOOLEAN NOT NULL DEFAULT FALSE,
                            fusion_data JSONB,
                            fusion_parents JSONB,
                            purchased_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            stage_started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            last_collected TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            last_cared_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            last_played_at TIMESTAMP,
                            happiness INTEGER NOT NULL DEFAULT 80,
                            total_earned BIGINT NOT NULL DEFAULT 0
                        )
                        """
                    )
                    cursor.execute(
                        "ALTER TABLE user_pets ADD COLUMN IF NOT EXISTS stage_started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP"
                    )
                    cursor.execute(
                        """
                        CREATE INDEX IF NOT EXISTS idx_user_pets_owner
                        ON user_pets (guild_id, user_id, id)
                        """
                    )
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS pet_items (
                            guild_id BIGINT NOT NULL,
                            user_id BIGINT NOT NULL,
                            item_key TEXT NOT NULL,
                            quantity BIGINT NOT NULL DEFAULT 0,
                            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            PRIMARY KEY (guild_id, user_id, item_key)
                        )
                        """
                    )
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS pet_fusion_history (
                            id BIGSERIAL PRIMARY KEY,
                            guild_id BIGINT NOT NULL,
                            user_id BIGINT NOT NULL,
                            primary_pet_id BIGINT NOT NULL,
                            secondary_pet_id BIGINT NOT NULL,
                            fusion_type TEXT NOT NULL,
                            outcome TEXT NOT NULL,
                            details JSONB,
                            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                        )
                        """
                    )
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS pet_shop_state (
                            guild_id BIGINT NOT NULL,
                            item_key TEXT NOT NULL,
                            current_stock INTEGER NOT NULL,
                            last_refresh TIMESTAMP NOT NULL,
                            PRIMARY KEY (guild_id, item_key)
                        )
                        """
                    )
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS economy_items (
                            guild_id BIGINT NOT NULL,
                            user_id BIGINT NOT NULL,
                            item_key TEXT NOT NULL,
                            quantity BIGINT NOT NULL DEFAULT 0,
                            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            PRIMARY KEY (guild_id, user_id, item_key)
                        )
                        """
                    )
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS member_security (
                            guild_id BIGINT NOT NULL,
                            user_id BIGINT NOT NULL,
                            lock_level INTEGER NOT NULL DEFAULT 0,
                            wanted_level INTEGER NOT NULL DEFAULT 0,
                            jail_until TIMESTAMP,
                            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            PRIMARY KEY (guild_id, user_id)
                        )
                        """
                    )
                    cursor.execute(
                        "ALTER TABLE member_security ADD COLUMN IF NOT EXISTS last_rob_at TIMESTAMP"
                    )
                    cursor.execute(
                        "ALTER TABLE member_security ADD COLUMN IF NOT EXISTS last_jewelry_heist_at TIMESTAMP"
                    )
                    cursor.execute(
                        "ALTER TABLE member_security ADD COLUMN IF NOT EXISTS last_bank_heist_at TIMESTAMP"
                    )
                    cursor.execute(
                        "ALTER TABLE member_security ADD COLUMN IF NOT EXISTS last_team_heist_at TIMESTAMP"
                    )
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS member_perks (
                            guild_id BIGINT NOT NULL,
                            user_id BIGINT NOT NULL,
                            prestige_level INTEGER NOT NULL DEFAULT 0,
                            is_booster BOOLEAN NOT NULL DEFAULT FALSE,
                            last_boost_thanked_at TIMESTAMP,
                            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            PRIMARY KEY (guild_id, user_id)
                        )
                        """
                    )
                    cursor.execute(
                        "ALTER TABLE member_perks ADD COLUMN IF NOT EXISTS prestige_level INTEGER NOT NULL DEFAULT 0"
                    )
                    cursor.execute(
                        "ALTER TABLE member_perks ADD COLUMN IF NOT EXISTS is_booster BOOLEAN NOT NULL DEFAULT FALSE"
                    )
                    cursor.execute(
                        "ALTER TABLE member_perks ADD COLUMN IF NOT EXISTS last_boost_thanked_at TIMESTAMP"
                    )
                    cursor.execute(
                        "ALTER TABLE member_perks ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP"
                    )
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS booster_panels (
                            guild_id BIGINT NOT NULL PRIMARY KEY,
                            channel_id BIGINT NOT NULL,
                            message_id BIGINT,
                            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                        )
                        """
                    )
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS heist_history (
                            id BIGSERIAL PRIMARY KEY,
                            guild_id BIGINT NOT NULL,
                            leader_id BIGINT NOT NULL,
                            participant_ids JSONB NOT NULL,
                            heist_key TEXT NOT NULL,
                            outcome TEXT NOT NULL,
                            payout BIGINT NOT NULL DEFAULT 0,
                            fine BIGINT NOT NULL DEFAULT 0,
                            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                        )
                        """
                    )

                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS member_cooldowns (
                            guild_id BIGINT NOT NULL,
                            user_id BIGINT NOT NULL,
                            command_name TEXT NOT NULL,
                            expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
                            PRIMARY KEY (guild_id, user_id, command_name)
                        )
                        """
                    )
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS member_effects (
                            guild_id BIGINT NOT NULL,
                            user_id BIGINT NOT NULL,
                            effect_key TEXT NOT NULL,
                            rate DOUBLE PRECISION NOT NULL DEFAULT 0,
                            expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
                            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            PRIMARY KEY (guild_id, user_id, effect_key)
                        )
                        """
                    )
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS economy_state (
                            guild_id BIGINT NOT NULL,
                            state_key TEXT NOT NULL,
                            amount BIGINT NOT NULL DEFAULT 0,
                            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            PRIMARY KEY (guild_id, state_key)
                        )
                        """
                    )
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS lottery_entries (
                            guild_id BIGINT NOT NULL,
                            user_id BIGINT NOT NULL,
                            entries INTEGER NOT NULL DEFAULT 0,
                            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            PRIMARY KEY (guild_id, user_id)
                        )
                        """
                    )
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS economy_admin_activity (
                            id BIGSERIAL PRIMARY KEY,
                            guild_id BIGINT NOT NULL,
                            admin_id BIGINT NOT NULL,
                            target_user_id BIGINT,
                            channel_id BIGINT,
                            command_name TEXT NOT NULL,
                            summary TEXT NOT NULL,
                            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                        )
                        """
                    )
                    cursor.execute(
                        """
                        CREATE INDEX IF NOT EXISTS idx_economy_admin_activity_recent
                        ON economy_admin_activity (guild_id, created_at DESC, id DESC)
                        """
                    )
                    cursor.execute(
                        """
                        CREATE INDEX IF NOT EXISTS idx_economy_admin_activity_target
                        ON economy_admin_activity (guild_id, target_user_id, created_at DESC, id DESC)
                        """
                    )
                    cursor.execute(
                        "ALTER TABLE member_economy ADD COLUMN IF NOT EXISTS debt BIGINT NOT NULL DEFAULT 0"
                    )
                    cursor.execute(
                        "ALTER TABLE member_economy ADD COLUMN IF NOT EXISTS debt_deadline TIMESTAMP"
                    )
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS economy_loans (
                            id BIGSERIAL PRIMARY KEY,
                            guild_id BIGINT NOT NULL,
                            lender_id BIGINT NOT NULL,
                            borrower_id BIGINT NOT NULL,
                            principal BIGINT NOT NULL,
                            interest BIGINT NOT NULL,
                            amount_paid BIGINT NOT NULL DEFAULT 0,
                            due_at TIMESTAMP,
                            status TEXT NOT NULL DEFAULT 'active'
                        )
                        """
                    )
                    cursor.execute(
                        "ALTER TABLE economy_loans ADD COLUMN IF NOT EXISTS amount_paid BIGINT NOT NULL DEFAULT 0"
                    )
        finally:
            self._pool.putconn(conn)

    @ttl_cache(ttl=300)
    @ttl_cache(ttl=300)
    def get_shop_background(self, guild_id: int) -> Optional[str]:
        conn = self._connect()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT background_url FROM shop_settings WHERE guild_id = %s",
                    (guild_id,),
                )
                row = cursor.fetchone()
                return row["background_url"] if row else None
        finally:
            self._pool.putconn(conn)

    def get_cooldown(
        self, guild_id: int, user_id: int, command_name: str
    ) -> Optional[datetime]:
        conn = self._connect()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT expires_at FROM member_cooldowns WHERE guild_id = %s AND user_id = %s AND command_name = %s",
                    (guild_id, user_id, command_name),
                )
                row = cursor.fetchone()
                return _as_utc(row["expires_at"]) if row else None
        finally:
            self._pool.putconn(conn)

    def set_cooldown(
        self, guild_id: int, user_id: int, command_name: str, expires_at: datetime
    ) -> None:
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
                        """,
                        (guild_id, user_id, command_name, expires_at),
                    )
        finally:
            self._pool.putconn(conn)

    @ttl_cache(ttl=300)
    @ttl_cache(ttl=300)
    def get_guild_config(self, guild_id: int) -> dict:
        conn = self._connect()
        try:
            try:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT shop_channel_id, shop_bypass_role_id FROM guild_config WHERE guild_id = %s",
                        (guild_id,),
                    )
                    row = cursor.fetchone()
                    return dict(row) if row else {}
            except (psycopg2.errors.UndefinedTable, psycopg2.errors.UndefinedColumn):
                conn.rollback()
                with conn:
                    with conn.cursor() as cursor:
                        self._ensure_guild_config_table(cursor)
                        cursor.execute(
                            "SELECT shop_channel_id, shop_bypass_role_id FROM guild_config WHERE guild_id = %s",
                            (guild_id,),
                        )
                        row = cursor.fetchone()
                        return dict(row) if row else {}
        finally:
            self._pool.putconn(conn)

    def set_guild_config(self, guild_id: int, key: str, value: int = None) -> None:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    self._ensure_guild_config_table(cursor)
                    cursor.execute(
                        "INSERT INTO guild_config (guild_id) VALUES (%s) ON CONFLICT (guild_id) DO NOTHING",
                        (guild_id,),
                    )
                    if key in ("shop_channel_id", "shop_bypass_role_id"):
                        cursor.execute(
                            f"UPDATE guild_config SET {key} = %s WHERE guild_id = %s",
                            (value, guild_id),
                        )
        finally:
            self._pool.putconn(conn)

    def reset_guild_config(self, guild_id: int) -> dict[str, int]:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    self._ensure_guild_config_table(cursor)
                    cursor.execute(
                        "DELETE FROM guild_config WHERE guild_id = %s",
                        (guild_id,),
                    )
                    guild_config_rows = max(0, cursor.rowcount)
                    cursor.execute(
                        "DELETE FROM shop_settings WHERE guild_id = %s",
                        (guild_id,),
                    )
                    shop_settings_rows = max(0, cursor.rowcount)
            return {
                "guild_config": guild_config_rows,
                "shop_settings": shop_settings_rows,
            }
        finally:
            self._pool.putconn(conn)

    def add_credits(self, guild_id: int, user_id: int, amount: int) -> int:
        amount = int(amount)
        if amount < 0 or amount > MAX_BIGINT:
            raise CreditAmountOutOfRange(amount)
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT credits FROM member_economy WHERE guild_id = %s AND user_id = %s FOR UPDATE",
                        (guild_id, user_id),
                    )
                    row = cursor.fetchone()
                    current = int(row[0]) if row else 0
                    if current > MAX_BIGINT - amount:
                        raise CreditAmountOutOfRange(current + amount)
                    cursor.execute(
                        """
                        INSERT INTO member_economy (guild_id, user_id, credits, updated_at)
                        VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
                        ON CONFLICT (guild_id, user_id) DO UPDATE SET
                            credits = EXCLUDED.credits,
                            updated_at = CURRENT_TIMESTAMP
                        RETURNING credits
                        """,
                        (guild_id, user_id, current + amount),
                    )
                    return cursor.fetchone()[0]
        finally:
            self._pool.putconn(conn)

    def remove_credits(self, guild_id: int, user_id: int, amount: int) -> bool:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE member_economy SET
                            credits = credits - %s,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE guild_id = %s AND user_id = %s AND credits >= %s
                        RETURNING credits
                        """,
                        (amount, guild_id, user_id, amount),
                    )
                    return cursor.fetchone() is not None
        finally:
            self._pool.putconn(conn)

    def get_balances(self, guild_id: int, user_id: int) -> tuple[int, int]:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT credits, bank FROM member_economy WHERE guild_id = %s AND user_id = %s",
                        (guild_id, user_id),
                    )
                    row = cursor.fetchone()
                    if row:
                        return row["credits"], row["bank"]
                    return 0, 0
        finally:
            self._pool.putconn(conn)

    def get_debt_info(self, guild_id: int, user_id: int) -> tuple[int, Optional[datetime]]:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT debt, debt_deadline FROM member_economy WHERE guild_id = %s AND user_id = %s",
                        (guild_id, user_id),
                    )
                    row = cursor.fetchone()
                    if row:
                        return row["debt"], _as_utc(row["debt_deadline"])
                    return 0, None
        finally:
            self._pool.putconn(conn)

    def add_debt(self, guild_id: int, user_id: int, amount: int) -> tuple[int, Optional[datetime]]:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO member_economy (guild_id, user_id, debt, debt_deadline)
                        VALUES (%s, %s, %s, CURRENT_TIMESTAMP + INTERVAL '1 hour')
                        ON CONFLICT (guild_id, user_id) DO UPDATE SET
                            debt = member_economy.debt + EXCLUDED.debt,
                            debt_deadline = COALESCE(member_economy.debt_deadline, EXCLUDED.debt_deadline)
                        RETURNING debt, debt_deadline
                        """,
                        (guild_id, user_id, amount),
                    )
                    row = cursor.fetchone()
                    return row["debt"], _as_utc(row["debt_deadline"])
        finally:
            self._pool.putconn(conn)

    def pay_debt(self, guild_id: int, user_id: int, amount: int) -> int:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE member_economy SET 
                            debt = GREATEST(0, debt - %s),
                            debt_deadline = CASE WHEN debt - %s <= 0 THEN NULL ELSE debt_deadline END
                        WHERE guild_id = %s AND user_id = %s
                        RETURNING debt
                        """,
                        (amount, amount, guild_id, user_id),
                    )
                    row = cursor.fetchone()
                    return row["debt"] if row else 0
        finally:
            self._pool.putconn(conn)

    def add_bank(self, guild_id: int, user_id: int, amount: int) -> int:
        if amount == 0:
            return self.get_balances(guild_id, user_id)[1]
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO member_economy (guild_id, user_id, bank)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (guild_id, user_id) DO UPDATE
                        SET bank = member_economy.bank + EXCLUDED.bank,
                            updated_at = CURRENT_TIMESTAMP
                        RETURNING bank
                        """,
                        (guild_id, user_id, amount),
                    )
                    return cursor.fetchone()[0]
        finally:
            self._pool.putconn(conn)

    def remove_bank(self, guild_id: int, user_id: int, amount: int) -> int:
        if amount == 0:
            return self.get_balances(guild_id, user_id)[1]
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE member_economy
                        SET bank = GREATEST(0, bank - %s),
                            updated_at = CURRENT_TIMESTAMP
                        WHERE guild_id = %s AND user_id = %s
                        RETURNING bank
                        """,
                        (amount, guild_id, user_id),
                    )
                    row = cursor.fetchone()
                    return row[0] if row else 0
        finally:
            self._pool.putconn(conn)

    def set_bank(self, guild_id: int, user_id: int, amount: int) -> int:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO member_economy (guild_id, user_id, bank, updated_at)
                        VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
                        ON CONFLICT (guild_id, user_id) DO UPDATE SET
                            bank = EXCLUDED.bank,
                            updated_at = CURRENT_TIMESTAMP
                        RETURNING bank
                        """,
                        (guild_id, user_id, amount),
                    )
                    return cursor.fetchone()[0]
        finally:
            self._pool.putconn(conn)

    def mark_boost_thanked(self, guild_id: int, user_id: int) -> None:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO member_perks (guild_id, user_id, is_booster, last_boost_thanked_at, updated_at)
                        VALUES (%s, %s, TRUE, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                        ON CONFLICT (guild_id, user_id) DO UPDATE SET
                            is_booster = TRUE,
                            last_boost_thanked_at = CURRENT_TIMESTAMP,
                            updated_at = CURRENT_TIMESTAMP
                        """,
                        (guild_id, user_id),
                    )
        finally:
            self._pool.putconn(conn)

    def transfer_to_bank(self, guild_id: int, user_id: int, amount: int) -> bool:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE member_economy
                        SET credits = credits - %s,
                            bank = bank + %s,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE guild_id = %s AND user_id = %s AND credits >= %s
                        RETURNING credits
                        """,
                        (amount, amount, guild_id, user_id, amount),
                    )
                    return cursor.fetchone() is not None
        finally:
            self._pool.putconn(conn)

    def withdraw_from_bank(self, guild_id: int, user_id: int, amount: int) -> bool:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE member_economy
                        SET credits = credits + %s,
                            bank = bank - %s,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE guild_id = %s AND user_id = %s AND bank >= %s
                        RETURNING bank
                        """,
                        (amount, amount, guild_id, user_id, amount),
                    )
                    return cursor.fetchone() is not None
        finally:
            self._pool.putconn(conn)

    def get_top_balances(self, guild_id: int, limit: int = 10) -> list[dict]:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT user_id, credits, bank, (credits + bank) as total
                        FROM member_economy
                        WHERE guild_id = %s
                        ORDER BY total DESC
                        LIMIT %s
                        """,
                        (guild_id, limit),
                    )
                    return [dict(r) for r in cursor.fetchall()]
        finally:
            self._pool.putconn(conn)

    def get_user_level(self, guild_id: int, user_id: int) -> int:
        conn = self._connect()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT level FROM member_levels WHERE guild_id = %s AND user_id = %s",
                    (guild_id, user_id),
                )
                row = cursor.fetchone()
                return row["level"] if row else 1
        except Exception:
            return 1
        finally:
            self._pool.putconn(conn)

    def get_balance(self, guild_id: int, user_id: int) -> int:
        conn = self._connect()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT credits FROM member_economy WHERE guild_id = %s AND user_id = %s",
                    (guild_id, user_id),
                )
                row = cursor.fetchone()
                return row[0] if row else 0
        finally:
            self._pool.putconn(conn)

    def set_credits(self, guild_id: int, user_id: int, amount: int) -> int:
        amount = int(amount)
        if amount < 0 or amount > MAX_BIGINT:
            raise CreditAmountOutOfRange(amount)
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO member_economy (guild_id, user_id, credits, updated_at)
                        VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
                        ON CONFLICT (guild_id, user_id) DO UPDATE SET
                            credits = EXCLUDED.credits,
                            updated_at = CURRENT_TIMESTAMP
                        RETURNING credits
                        """,
                        (guild_id, user_id, amount),
                    )
                    return cursor.fetchone()[0]
        finally:
            self._pool.putconn(conn)

    def get_bonus_breakdown(
        self, guild_id: int, user_id: int, action: str
    ) -> dict[str, Any]:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    rows = self._pet_rows(cursor, guild_id, user_id, for_update=True)
                    rows = self._apply_pet_decay(cursor, rows)
                    pet_rate = self._pet_bonus_rate_from_rows(rows, action)

                    self._ensure_member_perks(cursor, guild_id, user_id)
                    cursor.execute(
                        """
                        SELECT prestige_level, is_booster
                        FROM member_perks
                        WHERE guild_id = %s AND user_id = %s
                        """,
                        (guild_id, user_id),
                    )
                    perks = dict(cursor.fetchone())
                    prestige_level = int(perks.get("prestige_level") or 0)
                    prestige_rate = (
                        min(prestige_level, PRESTIGE_MAX_LEVEL)
                        * PRESTIGE_BONUS_PER_LEVEL
                    )
                    booster_rate = (
                        BOOSTER_BONUS_RATE if bool(perks.get("is_booster")) else 0.0
                    )

                    cursor.execute(
                        "SELECT role_name FROM member_purchases WHERE guild_id = %s AND user_id = %s",
                        (guild_id, user_id),
                    )
                    owned_roles = [str(row["role_name"]) for row in cursor.fetchall()]

        finally:
            self._pool.putconn(conn)

        role_rate = 0.0
        role_sources: list[dict[str, Any]] = []
        all_role_sources: list[dict[str, Any]] = []
        for role_name in owned_roles:
            perk = SHOP_ROLE_PERKS.get(role_name)
            if not perk:
                continue
            value = float(perk.get("bonus_value") or 0)
            if value <= 0:
                continue
            all_role_sources.append(
                {
                    "name": role_name,
                    "rate": value,
                    "label": str(perk.get("label") or ""),
                }
            )
            if not _bonus_type_applies(str(perk.get("bonus_type")), action):
                continue
            role_rate += value
            role_sources.append(
                {
                    "name": role_name,
                    "rate": value,
                    "label": str(perk.get("label") or ""),
                }
            )
        role_rate = min(role_rate, ROLE_BONUS_CAP)

        sources: list[dict[str, Any]] = []
        if pet_rate > 0:
            sources.append({"name": "Pets", "rate": pet_rate})
        if prestige_rate > 0:
            sources.append(
                {"name": f"Prestige {prestige_level}", "rate": prestige_rate}
            )
        if booster_rate > 0:
            sources.append({"name": "Server Booster", "rate": booster_rate})
        if role_rate > 0:
            sources.append(
                {"name": "Shop Roles", "rate": role_rate, "roles": role_sources}
            )

        cap = TOTAL_BONUS_CAPS.get(action, TOTAL_BONUS_CAPS["default"])
        rate = min(sum(float(source["rate"]) for source in sources), cap)
        return {
            "rate": rate,
            "cap": cap,
            "sources": sources,
            "prestige_level": prestige_level,
            "is_booster": booster_rate > 0,
            "owned_role_perks": role_sources,
            "all_owned_role_perks": all_role_sources,
        }

    @ttl_cache(ttl=60)
    @ttl_cache(ttl=60)
    def get_all_active_buffs(self, guild_id: int, user_id: int) -> dict[str, Any]:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    rows = self._pet_rows(cursor, guild_id, user_id, for_update=False)
                    rows = self._apply_pet_decay(cursor, rows)
                    
                    from .pets import (
                        _pet_display_name,
                        _pet_perk_lines_from_data,
                        _pet_stage,
                    )
                    pet_lines = []
                    pets = []
                    for row in rows:
                        if row.get("is_fused") and row.get("fusion_data"):
                            data = row["fusion_data"]
                        else:
                            data = _pet_stage(str(row["pet_key"]), int(row["stage"]))
                        if isinstance(data, dict):
                            perk_lines = _pet_perk_lines_from_data(data)
                            pet_lines.extend(perk_lines)
                            if perk_lines:
                                pets.append(
                                    {
                                        "name": _pet_display_name(row),
                                        "perks": perk_lines,
                                    }
                                )

                    self._ensure_member_perks(cursor, guild_id, user_id)
                    cursor.execute(
                        "SELECT prestige_level, is_booster FROM member_perks WHERE guild_id = %s AND user_id = %s",
                        (guild_id, user_id),
                    )
                    perks = dict(cursor.fetchone())
                    prestige_level = int(perks.get("prestige_level") or 0)
                    is_booster = bool(perks.get("is_booster"))

                    cursor.execute(
                        "SELECT role_name FROM member_purchases WHERE guild_id = %s AND user_id = %s",
                        (guild_id, user_id),
                    )
                    owned_roles = [str(r["role_name"]) for r in cursor.fetchall()]

                    cursor.execute(
                        "SELECT effect_key, rate, expires_at FROM member_effects WHERE guild_id = %s AND user_id = %s AND expires_at > CURRENT_TIMESTAMP",
                        (guild_id, user_id)
                    )
                    active_effects = [dict(r) for r in cursor.fetchall()]

                    cursor.execute(
                        "SELECT item_key, quantity FROM economy_items WHERE guild_id = %s AND user_id = %s AND quantity > 0",
                        (guild_id, user_id),
                    )
                    owned_items = {
                        str(row["item_key"]): int(row["quantity"])
                        for row in cursor.fetchall()
                    }

        finally:
            self._pool.putconn(conn)

        role_sources = []
        for role_name in owned_roles:
            perk = SHOP_ROLE_PERKS.get(role_name)
            if perk:
                role_sources.append({"name": role_name, "label": perk.get("label", "")})

        prestige_rate = min(prestige_level, PRESTIGE_MAX_LEVEL) * PRESTIGE_BONUS_PER_LEVEL
        booster_rate = BOOSTER_BONUS_RATE if is_booster else 0.0
        payout_stats = []
        for action, label in (
            ("daily", "Daily"),
            ("work", "Work"),
            ("fish", "Fishing"),
            ("hunt", "Hunting"),
            ("mine", "Mining"),
            ("crime", "Crime"),
            ("gambling", "Casino Profits"),
            ("gambling_luck", "Casino Luck"),
            ("passive", "Pet Income"),
        ):
            pet_rate = self._pet_bonus_rate_from_rows(rows, action)
            role_rate = min(
                sum(
                    float(SHOP_ROLE_PERKS[role_name].get("bonus_value") or 0)
                    for role_name in owned_roles
                    if role_name in SHOP_ROLE_PERKS
                    and _bonus_type_applies(
                        str(SHOP_ROLE_PERKS[role_name].get("bonus_type") or ""),
                        action,
                    )
                ),
                ROLE_BONUS_CAP,
            )
            total_rate = min(
                pet_rate + role_rate + prestige_rate + booster_rate,
                TOTAL_BONUS_CAPS.get(action, TOTAL_BONUS_CAPS["default"]),
            )
            payout_stats.append(
                {
                    "label": label,
                    "total_rate": total_rate,
                    "pet_rate": pet_rate,
                    "role_rate": role_rate,
                    "prestige_rate": prestige_rate,
                    "booster_rate": booster_rate,
                }
            )

        gear = []
        for task, options in TASK_GEAR.items():
            active_option = next(
                (
                    option
                    for option in reversed(options)
                    if owned_items.get(str(option["item_key"]), 0) > 0
                ),
                None,
            )
            if active_option:
                item_key = str(active_option["item_key"])
                gear.append(
                    {
                        "name": str(ECONOMY_ITEM_DEFS[item_key]["name"]),
                        "task": task.title(),
                        "luck": float(active_option["luck"]),
                        "cooldown": float(active_option["cooldown"]),
                    }
                )

        return {
            "prestige_level": prestige_level,
            "prestige_rate": prestige_rate,
            "is_booster": is_booster,
            "booster_rate": booster_rate,
            "payout_stats": payout_stats,
            "roles": role_sources,
            "pets": pets,
            "pet_lines": pet_lines,
            "effects": active_effects,
            "gear": gear,
        }

    def apply_total_bonus(
        self, guild_id: int, user_id: int, amount: int, action: str
    ) -> dict[str, Any]:
        base = max(0, int(amount))
        breakdown = self.get_bonus_breakdown(guild_id, user_id, action)
        rate = float(breakdown.get("rate") or 0.0)
        bonus = int(base * rate)
        return {
            "base": base,
            "bonus": bonus,
            "total": base + bonus,
            "rate": rate,
            "sources": breakdown.get("sources", []),
            "breakdown": breakdown,
        }

    def _hybrid_fusion_tier(self, first_stage: int, second_stage: int) -> dict[str, Any]:
        min_stage = min(int(first_stage), int(second_stage))
        if min_stage >= 5:
            return {
                "key": "perfect",
                "label": "Perfect",
                "stage": 5,
                "credits": 6_000_000,
                "fusion_core": 3,
                "greater_fusion_core": 1,
                "income_multiplier": 1.40,
                "bonus_multiplier": 1.15,
                "base_chance": 0.55,
            }
        if min_stage >= 4:
            return {
                "key": "greater",
                "label": "Greater",
                "stage": 4,
                "credits": 5_000_000,
                "fusion_core": 3,
                "greater_fusion_core": 0,
                "income_multiplier": 1.25,
                "bonus_multiplier": 1.00,
                "base_chance": 0.62,
            }
        return {
            "key": "lesser",
            "label": "Lesser",
            "stage": 3,
            "credits": 3_000_000,
            "fusion_core": 2,
            "greater_fusion_core": 0,
            "income_multiplier": 1.10,
            "bonus_multiplier": 0.85,
            "base_chance": 0.72,
        }

    def _hybrid_fusion_data(
        self, first_key: str, second_key: str, first_stage: int, second_stage: int
    ) -> dict[str, Any]:
        combo = frozenset((first_key, second_key))
        tier = self._hybrid_fusion_tier(first_stage, second_stage)
        ordered_keys = tuple(sorted((first_key, second_key)))
        family_names = {
            key: str(PET_LINES.get(key, {}).get("family") or key.replace("_", " ").title())
            for key in ordered_keys
        }
        base_data = dict(PET_HYBRIDS.get(combo) or {})
        base_name = str(
            base_data.get("name")
            or f"{family_names[ordered_keys[0]]}-{family_names[ordered_keys[1]]}"
        )
        target_name = f"{tier['label']} {base_name}"
        first_data = PET_LINES[first_key]["stages"][int(first_stage) - 1]
        second_data = PET_LINES[second_key]["stages"][int(second_stage) - 1]
        average_income = (
            int(first_data.get("daily_income") or 0)
            + int(second_data.get("daily_income") or 0)
        ) / 2

        image_key = f"fusion_{ordered_keys[0]}_{ordered_keys[1]}_{tier['key']}"
        return {
            "name": target_name,
            "daily_income": int(average_income * float(tier["income_multiplier"])),
            "bonuses": [dict(bonus) for bonus in base_data.get("bonuses", [])],
            "luck_bonus": float(base_data.get("luck_bonus") or 0),
            "cooldown_reduction": float(base_data.get("cooldown_reduction") or 0),
            "gamble_multiplier": float(base_data.get("gamble_multiplier") or 0),
            "fishing_luck": float(base_data.get("fishing_luck") or 0),
            "perk_model": "combination_v3",
            "parent_stages": [int(first_stage), int(second_stage)],
            "fusion_tier": tier["key"],
            "fusion_tier_label": tier["label"],
            "fusion_stage": tier["stage"],
            "fusion_keys": list(ordered_keys),
            "fusion_image_key": image_key,
        }

    def _fusion_requirements(
        self, first: dict[str, Any], second: dict[str, Any]
    ) -> dict[str, Any]:
        if int(first["id"]) == int(second["id"]):
            return {"ok": False, "reason": "same_pet"}
        if first.get("is_fused") or second.get("is_fused"):
            return {"ok": False, "reason": "already_fused"}

        first_key = str(first["pet_key"])
        second_key = str(second["pet_key"])
        first_stage = int(first["stage"])
        second_stage = int(second["stage"])
        if (
            first_key == second_key
            and first_stage == second_stage
            and first_stage >= 3
            and first_stage < 5
        ):
            from .pets import _pet_stage

            credits = 3_000_000 if first_stage == 3 else 7_000_000
            cores = 2
            greater = 1 if first_stage == 4 else 0
            return {
                "ok": True,
                "type": "safe",
                "credits": credits,
                "fusion_core": cores,
                "greater_fusion_core": greater,
                "target_name": _pet_stage(first_key, first_stage + 1)["name"],
                "chance": 1.0,
            }

        if (
            first_key != second_key
            and first_stage >= 3
            and second_stage >= 3
            and first_key in PET_LINES
            and second_key in PET_LINES
            and first_key not in PET_FUSION_EXCLUDED_KEYS
            and second_key not in PET_FUSION_EXCLUDED_KEYS
        ):
            avg_happiness = (int(first["happiness"]) + int(second["happiness"])) / 2
            if avg_happiness >= 90:
                mood_bonus = 0.25
            elif avg_happiness >= 70:
                mood_bonus = 0.10
            elif avg_happiness >= 50:
                mood_bonus = 0.0
            elif avg_happiness >= 30:
                mood_bonus = -0.15
            else:
                mood_bonus = -0.40
            tier = self._hybrid_fusion_tier(first_stage, second_stage)
            hybrid_data = self._hybrid_fusion_data(
                first_key, second_key, first_stage, second_stage
            )
            chance = max(0.25, min(0.95, float(tier["base_chance"]) + mood_bonus))
            return {
                "ok": True,
                "type": "hybrid",
                "tier": tier["key"],
                "tier_label": tier["label"],
                "credits": int(tier["credits"]),
                "fusion_core": int(tier["fusion_core"]),
                "greater_fusion_core": int(tier["greater_fusion_core"]),
                "target_name": hybrid_data["name"],
                "chance": chance,
                "hybrid_data": hybrid_data,
            }
        return {"ok": False, "reason": "invalid_pair"}

    def get_economy_items(self, guild_id: int, user_id: int) -> dict[str, int]:
        conn = self._connect()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT item_key, quantity FROM economy_items WHERE guild_id = %s AND user_id = %s",
                    (guild_id, user_id),
                )
                return {
                    row["item_key"]: int(row["quantity"]) for row in cursor.fetchall()
                }
        finally:
            self._pool.putconn(conn)

    def grant_economy_item(
        self, guild_id: int, user_id: int, item_key: str, amount: int
    ) -> dict[str, Any]:
        if item_key not in ECONOMY_ITEM_DEFS:
            return {"ok": False, "reason": "invalid_item"}
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO economy_items (guild_id, user_id, item_key, quantity, updated_at)
                        VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
                        ON CONFLICT (guild_id, user_id, item_key) DO UPDATE SET
                            quantity = GREATEST(0, economy_items.quantity + EXCLUDED.quantity),
                            updated_at = CURRENT_TIMESTAMP
                        RETURNING quantity
                        """,
                        (guild_id, user_id, item_key, int(amount)),
                    )
                    return {
                        "ok": True,
                        "item_key": item_key,
                        "quantity": int(cursor.fetchone()[0]),
                    }
        finally:
            self._pool.putconn(conn)

    def buy_utility_item(
        self, guild_id: int, user_id: int, item_key: str, price: Optional[int] = None
    ) -> dict[str, Any]:
        if item_key not in ECONOMY_ITEM_DEFS:
            return {"ok": False, "reason": "invalid_item"}
        price = int(price if price is not None else ECONOMY_ITEM_DEFS[item_key]["price"])
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO member_economy (guild_id, user_id, credits, updated_at)
                        VALUES (%s, %s, 0, CURRENT_TIMESTAMP)
                        ON CONFLICT (guild_id, user_id) DO NOTHING
                        """,
                        (guild_id, user_id),
                    )
                    cursor.execute(
                        "SELECT credits FROM member_economy WHERE guild_id = %s AND user_id = %s FOR UPDATE",
                        (guild_id, user_id),
                    )
                    balance = int(cursor.fetchone()["credits"])
                    if balance < price:
                        return {"ok": False, "reason": "insufficient", "balance": balance, "price": price}
                    cursor.execute(
                        "UPDATE member_economy SET credits = credits - %s, updated_at = CURRENT_TIMESTAMP WHERE guild_id = %s AND user_id = %s",
                        (price, guild_id, user_id),
                    )
                    cursor.execute(
                        """
                        INSERT INTO economy_items (guild_id, user_id, item_key, quantity, updated_at)
                        VALUES (%s, %s, %s, 1, CURRENT_TIMESTAMP)
                        ON CONFLICT (guild_id, user_id, item_key) DO UPDATE SET
                            quantity = economy_items.quantity + 1,
                            updated_at = CURRENT_TIMESTAMP
                        RETURNING quantity
                        """,
                        (guild_id, user_id, item_key),
                    )
                    return {
                        "ok": True,
                        "item_key": item_key,
                        "price": price,
                        "balance": balance - price,
                        "quantity": int(cursor.fetchone()["quantity"]),
                    }
        finally:
            self._pool.putconn(conn)

    def consume_economy_item(
        self, guild_id: int, user_id: int, item_key: str, quantity: int = 1
    ) -> dict[str, Any]:
        quantity = max(1, int(quantity))
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT quantity
                        FROM economy_items
                        WHERE guild_id = %s AND user_id = %s AND item_key = %s
                        FOR UPDATE
                        """,
                        (guild_id, user_id, item_key),
                    )
                    row = cursor.fetchone()
                    owned = int(row["quantity"]) if row else 0
                    if owned < quantity:
                        return {"ok": False, "reason": "missing", "owned": owned}
                    cursor.execute(
                        """
                        UPDATE economy_items
                        SET quantity = quantity - %s, updated_at = CURRENT_TIMESTAMP
                        WHERE guild_id = %s AND user_id = %s AND item_key = %s
                        RETURNING quantity
                        """,
                        (quantity, guild_id, user_id, item_key),
                    )
                    remaining = int(cursor.fetchone()["quantity"])
                    if remaining <= 0:
                        cursor.execute(
                            "DELETE FROM economy_items WHERE guild_id = %s AND user_id = %s AND item_key = %s AND quantity <= 0",
                            (guild_id, user_id, item_key),
                        )
                    return {"ok": True, "item_key": item_key, "remaining": max(0, remaining)}
        finally:
            self._pool.putconn(conn)

    def get_active_effects(self, guild_id: int, user_id: int) -> dict[str, dict[str, Any]]:
        now = datetime.now(timezone.utc)
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "DELETE FROM member_effects WHERE guild_id = %s AND user_id = %s AND expires_at <= %s",
                        (guild_id, user_id, now),
                    )
                    cursor.execute(
                        """
                        SELECT effect_key, rate, expires_at
                        FROM member_effects
                        WHERE guild_id = %s AND user_id = %s
                        ORDER BY expires_at DESC
                        """,
                        (guild_id, user_id),
                    )
                    return {str(row["effect_key"]): dict(row) for row in cursor.fetchall()}
        finally:
            self._pool.putconn(conn)

    def activate_effect(
        self,
        guild_id: int,
        user_id: int,
        item_key: str,
        effect_key: str,
        rate: float,
        minutes: int,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(minutes=max(1, int(minutes)))
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT effect_key, expires_at
                        FROM member_effects
                        WHERE guild_id = %s AND user_id = %s AND effect_key = %s AND expires_at > %s
                        """,
                        (guild_id, user_id, effect_key, now),
                    )
                    active = cursor.fetchone()
                    if active:
                        return {"ok": False, "reason": "active", "expires_at": active["expires_at"]}
                    cursor.execute(
                        """
                        SELECT quantity
                        FROM economy_items
                        WHERE guild_id = %s AND user_id = %s AND item_key = %s
                        FOR UPDATE
                        """,
                        (guild_id, user_id, item_key),
                    )
                    item_row = cursor.fetchone()
                    owned = int(item_row["quantity"]) if item_row else 0
                    if owned < 1:
                        return {"ok": False, "reason": "missing", "owned": owned}
                    cursor.execute(
                        """
                        UPDATE economy_items
                        SET quantity = quantity - 1, updated_at = CURRENT_TIMESTAMP
                        WHERE guild_id = %s AND user_id = %s AND item_key = %s
                        RETURNING quantity
                        """,
                        (guild_id, user_id, item_key),
                    )
                    remaining = int(cursor.fetchone()["quantity"])
                    if remaining <= 0:
                        cursor.execute(
                            "DELETE FROM economy_items WHERE guild_id = %s AND user_id = %s AND item_key = %s AND quantity <= 0",
                            (guild_id, user_id, item_key),
                        )
                    cursor.execute(
                        """
                        INSERT INTO member_effects (guild_id, user_id, effect_key, rate, expires_at, updated_at)
                        VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                        ON CONFLICT (guild_id, user_id, effect_key) DO UPDATE SET
                            rate = EXCLUDED.rate,
                            expires_at = EXCLUDED.expires_at,
                            updated_at = CURRENT_TIMESTAMP
                        """,
                        (guild_id, user_id, effect_key, float(rate), expires_at),
                    )
                    return {
                        "ok": True,
                        "effect_key": effect_key,
                        "rate": float(rate),
                        "expires_at": expires_at,
                        "remaining": max(0, remaining),
                    }
        finally:
            self._pool.putconn(conn)

    def get_effect_rate(self, guild_id: int, user_id: int, effect_key: str) -> float:
        effects = self.get_active_effects(guild_id, user_id)
        row = effects.get(effect_key)
        return float(row.get("rate") or 0) if row else 0.0

    def task_gear_modifiers(self, guild_id: int, user_id: int, task: str) -> dict[str, float]:
        owned = self.get_economy_items(guild_id, user_id)
        luck = 0.0
        cooldown = 0.0
        for gear in TASK_GEAR.get(task, ()):
            if int(owned.get(gear["item_key"], 0)) > 0:
                luck = max(luck, float(gear["luck"]))
                cooldown = max(cooldown, float(gear["cooldown"]))
        return {"luck": luck, "cooldown": cooldown}

    def add_state_amount(self, guild_id: int, state_key: str, amount: int) -> int:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO economy_state (guild_id, state_key, amount, updated_at)
                        VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
                        ON CONFLICT (guild_id, state_key) DO UPDATE SET
                            amount = GREATEST(0, economy_state.amount + EXCLUDED.amount),
                            updated_at = CURRENT_TIMESTAMP
                        RETURNING amount
                        """,
                        (guild_id, state_key, int(amount)),
                    )
                    return int(cursor.fetchone()["amount"])
        finally:
            self._pool.putconn(conn)

    def get_state_amount(self, guild_id: int, state_key: str) -> int:
        conn = self._connect()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT amount FROM economy_state WHERE guild_id = %s AND state_key = %s",
                    (guild_id, state_key),
                )
                row = cursor.fetchone()
                return int(row["amount"]) if row else 0
        finally:
            self._pool.putconn(conn)

    def take_state_amount(self, guild_id: int, state_key: str, minimum: int = 0) -> int:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO economy_state (guild_id, state_key, amount, updated_at)
                        VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
                        ON CONFLICT (guild_id, state_key) DO NOTHING
                        """,
                        (guild_id, state_key, int(minimum)),
                    )
                    cursor.execute(
                        "SELECT amount FROM economy_state WHERE guild_id = %s AND state_key = %s FOR UPDATE",
                        (guild_id, state_key),
                    )
                    row = cursor.fetchone()
                    amount = int(row["amount"]) if row else int(minimum)
                    cursor.execute(
                        """
                        UPDATE economy_state
                        SET amount = 0, updated_at = CURRENT_TIMESTAMP
                        WHERE guild_id = %s AND state_key = %s
                        """,
                        (guild_id, state_key),
                    )
                    return max(amount, int(minimum))
        finally:
            self._pool.putconn(conn)

    def buy_lottery_ticket(self, guild_id: int, user_id: int, entries: int = 1) -> dict[str, Any]:
        entries = max(1, min(20, int(entries)))
        cost = LOTTERY_TICKET_PRICE * entries
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO member_economy (guild_id, user_id, credits, updated_at)
                        VALUES (%s, %s, 0, CURRENT_TIMESTAMP)
                        ON CONFLICT (guild_id, user_id) DO NOTHING
                        """,
                        (guild_id, user_id),
                    )
                    cursor.execute(
                        "SELECT credits FROM member_economy WHERE guild_id = %s AND user_id = %s FOR UPDATE",
                        (guild_id, user_id),
                    )
                    balance = int(cursor.fetchone()["credits"])
                    if balance < cost:
                        return {"ok": False, "reason": "insufficient", "balance": balance, "cost": cost}
                    cursor.execute(
                        "UPDATE member_economy SET credits = credits - %s, updated_at = CURRENT_TIMESTAMP WHERE guild_id = %s AND user_id = %s",
                        (cost, guild_id, user_id),
                    )
                    cursor.execute(
                        """
                        INSERT INTO lottery_entries (guild_id, user_id, entries, updated_at)
                        VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
                        ON CONFLICT (guild_id, user_id) DO UPDATE SET
                            entries = lottery_entries.entries + EXCLUDED.entries,
                            updated_at = CURRENT_TIMESTAMP
                        RETURNING entries
                        """,
                        (guild_id, user_id, entries),
                    )
                    total_entries = int(cursor.fetchone()["entries"])
                    cursor.execute(
                        """
                        INSERT INTO economy_state (guild_id, state_key, amount, updated_at)
                        VALUES (%s, 'lottery_pool', %s, CURRENT_TIMESTAMP)
                        ON CONFLICT (guild_id, state_key) DO UPDATE SET
                            amount = economy_state.amount + EXCLUDED.amount,
                            updated_at = CURRENT_TIMESTAMP
                        RETURNING amount
                        """,
                        (guild_id, cost),
                    )
                    pool = int(cursor.fetchone()["amount"])
                    return {"ok": True, "entries": entries, "total_entries": total_entries, "cost": cost, "pool": pool}
        finally:
            self._pool.putconn(conn)

    def get_lottery_status(self, guild_id: int) -> dict[str, Any]:
        conn = self._connect()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT user_id, entries FROM lottery_entries WHERE guild_id = %s ORDER BY entries DESC, updated_at ASC",
                    (guild_id,),
                )
                rows = [dict(row) for row in cursor.fetchall()]
            return {
                "pool": self.get_state_amount(guild_id, "lottery_pool"),
                "entries": rows,
                "total_entries": sum(int(row["entries"]) for row in rows),
            }
        finally:
            self._pool.putconn(conn)

    def draw_lottery(self, guild_id: int) -> dict[str, Any]:
        conn = self._connect()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT user_id, entries FROM lottery_entries WHERE guild_id = %s FOR UPDATE",
                        (guild_id,),
                    )
                    rows = [dict(row) for row in cursor.fetchall()]
                    total_entries = sum(int(row["entries"]) for row in rows)
                    if not rows or total_entries <= 0:
                        return {"ok": False, "reason": "empty"}
                    ticket = random.randint(1, total_entries)
                    running = 0
                    winner_id = int(rows[0]["user_id"])
                    for row in rows:
                        running += int(row["entries"])
                        if ticket <= running:
                            winner_id = int(row["user_id"])
                            break
                    cursor.execute(
                        "SELECT amount FROM economy_state WHERE guild_id = %s AND state_key = 'lottery_pool' FOR UPDATE",
                        (guild_id,),
                    )
                    pool_row = cursor.fetchone()
                    pool = int(pool_row["amount"]) if pool_row else 0
                    if pool <= 0:
                        pool = LOTTERY_TICKET_PRICE * total_entries
                    cursor.execute(
                        """
                        INSERT INTO member_economy (guild_id, user_id, credits, updated_at)
                        VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
                        ON CONFLICT (guild_id, user_id) DO UPDATE SET
                            credits = member_economy.credits + EXCLUDED.credits,
                            updated_at = CURRENT_TIMESTAMP
                        """,
                        (guild_id, winner_id, pool),
                    )
                    cursor.execute("DELETE FROM lottery_entries WHERE guild_id = %s", (guild_id,))
                    cursor.execute(
                        """
                        INSERT INTO economy_state (guild_id, state_key, amount, updated_at)
                        VALUES (%s, 'lottery_pool', 0, CURRENT_TIMESTAMP)
                        ON CONFLICT (guild_id, state_key) DO UPDATE SET
                            amount = 0,
                            updated_at = CURRENT_TIMESTAMP
                        """,
                        (guild_id,),
                    )
                    return {"ok": True, "winner_id": winner_id, "pool": pool, "entries": total_entries}
        finally:
            self._pool.putconn(conn)

    def _get_item_quantities_for_update(
        self, cursor, guild_id: int, user_id: int
    ) -> dict[str, int]:
        cursor.execute(
            "SELECT item_key, quantity FROM economy_items WHERE guild_id = %s AND user_id = %s FOR UPDATE",
            (guild_id, user_id),
        )
        return {row["item_key"]: int(row["quantity"]) for row in cursor.fetchall()}

    def _cooldown_block(
        self, security: dict[str, Any], cooldown_key: str, now: datetime
    ) -> Optional[dict[str, Any]]:
        column = ROBBERY_COOLDOWN_COLUMNS[cooldown_key]
        last_used = _as_utc(security.get(column))
        if last_used is None:
            return None
        ready_at = last_used + ROBBERY_COOLDOWNS[cooldown_key]
        if now < ready_at:
            return {
                "ok": False,
                "reason": "cooldown",
                "ready_at": ready_at,
                "cooldown_key": cooldown_key,
            }
        return None


class ColorDuelView(discord.ui.LayoutView):
    COLORS = ("Red", "Blue", "Green", "Yellow", "Purple")

    def __init__(
        self,
        cog: Any,
        ctx: commands.Context,
        opponent: discord.Member,
        bet: int,
    ):
        super().__init__(timeout=180)
        self.cog = cog
        self.guild_id = ctx.guild.id
        self.channel = ctx.channel
        self.challenger_id = ctx.author.id
        self.opponent_id = opponent.id
        self.bet = int(bet)
        self.pot = int(bet) * 2
        self.accepted = False
        self.cancelled = False
        self.finished = False
        self.target_color = random.choice(self.COLORS)
        self.message: Optional[discord.Message] = None
        self.scores = {self.challenger_id: 0, self.opponent_id: 0}
        self.guesses: dict[int, str] = {}
        self.round_number = 1
        self._render_lobby()

    def _member_text(self, user_id: int) -> str:
        guild = self.channel.guild if hasattr(self.channel, "guild") else None
        member = guild.get_member(user_id) if guild else None
        return member.mention if member else f"<@{user_id}>"

    def _build_container(self, extra_desc: str = "") -> discord.ui.Container:
        from components_v2 import branded_panel_container
        
        if not self.accepted:
            description = (
                f"{self._member_text(self.challenger_id)} challenged "
                f"{self._member_text(self.opponent_id)} to a Color Duel.\n"
                f"Entry: **{self.bet:,} cr** each\n"
                f"Prize: **{self.pot:,} cr**"
            )
        else:
            score_line = (
                f"{self._member_text(self.challenger_id)}: **{self.scores[self.challenger_id]}/4**\n"
                f"{self._member_text(self.opponent_id)}: **{self.scores[self.opponent_id]}/4**"
            )
            guesses_text = ", ".join(
                self._member_text(uid) for uid in self.guesses
            ) or "No guesses locked yet."
            description = (
                f"Round **{self.round_number}**\n"
                f"First to **4** wins **{self.pot:,} cr**.\n\n"
                f"{score_line}\n\n"
                f"Guessed: {guesses_text}"
            )
            
        if extra_desc:
            description = f"{extra_desc}\n\n{description}"
            
        return branded_panel_container(
            title="Color Duel",
            description=description,
            accent_color=0x5865F2,
        )

    def _render_lobby(self) -> None:
        self.clear_items()
        container = self._build_container()
        
        accept = discord.ui.Button(label="Enter Duel", style=discord.ButtonStyle.success)
        decline = discord.ui.Button(label="Decline", style=discord.ButtonStyle.danger)
        accept.callback = self.accept_button
        decline.callback = self.decline_button
        
        container.add_item(discord.ui.ActionRow(accept, decline))
        self.add_item(container)

    def _render_round(self) -> None:
        self.clear_items()
        for color in self.COLORS:
            button = discord.ui.Button(label=color, style=discord.ButtonStyle.primary)

            async def callback(
                interaction: discord.Interaction, selected: str = color
            ) -> None:
                await self.guess_button(interaction, selected)

            button.callback = callback
            self.add_item(button)

    async def on_timeout(self) -> None:
        if self.finished or self.cancelled:
            return
        self.cancelled = True
        refund_ids = (
            [self.challenger_id, self.opponent_id]
            if self.accepted
            else [self.challenger_id]
        )
        for user_id in refund_ids:
            await asyncio.to_thread(
                self.cog.store.add_credits,
                self.guild_id,
                user_id,
                self.bet,
            )
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                embed = self._embed()
                embed.description = (embed.description or "") + "\n\nDuel expired."
                await self.message.edit(embed=embed, view=self)
            except discord.HTTPException:
                pass

    async def accept_button(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.opponent_id:
            return await interaction.response.send_message(
                "Only the challenged player can enter.", ephemeral=True
            )
        removed = await asyncio.to_thread(
            self.cog.store.remove_credits,
            self.guild_id,
            self.opponent_id,
            self.bet,
        )
        if not removed:
            balance = await asyncio.to_thread(
                self.cog.store.get_balance, self.guild_id, self.opponent_id
            )
            return await interaction.response.send_message(
                f"You need **{self.bet:,} cr** to enter. Wallet: **{balance:,} cr**.",
                ephemeral=True,
            )
        self.accepted = True
        self._render_round()
        await interaction.response.edit_message(embed=self._embed(), view=self)

    async def decline_button(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.opponent_id and interaction.user.id != self.challenger_id:
            return await interaction.response.send_message(
                "Only a duel player can close this.", ephemeral=True
            )
        self.cancelled = True
        self.finished = True
        await asyncio.to_thread(
            self.cog.store.add_credits, self.guild_id, self.challenger_id, self.bet
        )
        for item in self.children:
            item.disabled = True
        embed = self._embed()
        embed.description = (embed.description or "") + "\n\nDuel cancelled. Entry refunded."
        await interaction.response.edit_message(embed=embed, view=self)

    async def guess_button(self, interaction: discord.Interaction, selected: str) -> None:
        if not self.accepted or self.finished:
            return await interaction.response.send_message(
                "This duel is not active.", ephemeral=True
            )
        if interaction.user.id not in self.scores:
            return await interaction.response.send_message(
                "You are not in this duel.", ephemeral=True
            )
        if interaction.user.id in self.guesses:
            return await interaction.response.send_message(
                "You already guessed this round.", ephemeral=True
            )
        self.guesses[interaction.user.id] = selected
        if len(self.guesses) < 2:
            return await interaction.response.send_message(
                f"Locked **{selected}**.", ephemeral=True
            )

        winners = [
            user_id for user_id, guess in self.guesses.items() if guess == self.target_color
        ]
        if len(winners) == 1:
            self.scores[winners[0]] += 1
        elif len(winners) == 2:
            for user_id in winners:
                self.scores[user_id] += 1

        reveal = (
            f"The color was **{self.target_color}**.\n"
            + "\n".join(
                f"{self._member_text(user_id)} guessed **{guess}**"
                for user_id, guess in self.guesses.items()
            )
        )
        winner_id = next(
            (user_id for user_id, score in self.scores.items() if score >= 4), None
        )
        self.guesses.clear()
        self.target_color = random.choice(self.COLORS)
        if winner_id is not None:
            self.finished = True
            await asyncio.to_thread(
                self.cog.store.add_credits, self.guild_id, winner_id, self.pot
            )
            for item in self.children:
                item.disabled = True
            embed = self._embed()
            embed.title = "Color Duel - Winner"
            embed.description = (
                f"{reveal}\n\n{self._member_text(winner_id)} wins **{self.pot:,} cr**."
            )
            return await interaction.response.edit_message(embed=embed, view=self)

        self.round_number += 1
        embed = self._embed()
        embed.description = f"{reveal}\n\n{embed.description}"
        await interaction.response.edit_message(embed=embed, view=self)

class CoreCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.color = 0x2B2D31
        self._investments_task = None
        self._active_uno_games = {}
        self._shop_channel_notice_times = {}

    @tasks.loop(minutes=1)
    async def debt_penalty_loop(self):
        if hasattr(self, "store"):
            try:
                await asyncio.to_thread(self.store.process_debt_penalties)
            except Exception as e:
                LOGGER.error("Error in debt penalty loop: %s", e)

    @tasks.loop(minutes=60)
    async def loan_default_loop(self):
        if hasattr(self, "store"):
            try:
                await asyncio.to_thread(self.store.process_loan_defaults)
            except Exception as e:
                LOGGER.error("Error in loan default loop: %s", e)

    def _audit_target_user_id(self, ctx: commands.Context) -> Optional[int]:
        for value in list(getattr(ctx, "args", ()))[2:]:
            if isinstance(value, discord.Member):
                return value.id
        for value in getattr(ctx, "kwargs", {}).values():
            if isinstance(value, discord.Member):
                return value.id
        return None

    async def cog_after_invoke(self, ctx: commands.Context) -> None:
        log_admin_activity = getattr(self, "_log_admin_activity", None)
        if log_admin_activity is not None:
            await log_admin_activity(ctx)

    async def restore_active_games(self):
        try:
            await self.bot.wait_until_ready()
        except RuntimeError:
            return
        games = await asyncio.to_thread(self.store.get_active_casino_games)
        from .casino import BlackjackView, MinesView, PokerView

        for game in games:
            try:
                import json

                state = game["game_state"]
                if isinstance(state, str):
                    state = json.loads(state)
                view = None
                if game["game_type"] == "mines":
                    view = MinesView(
                        self,
                        game["guild_id"],
                        game["user_id"],
                        game["bet"],
                        state["difficulty"],
                        state["config"],
                        state,
                    )
                elif game["game_type"] == "blackjack":
                    view = BlackjackView(
                        self, game["guild_id"], game["user_id"], game["bet"], state
                    )
                elif game["game_type"] == "poker":
                    view = PokerView(
                        self,
                        game["guild_id"],
                        game["user_id"],
                        game["bet"],
                        state["deck"],
                        state["hand"],
                        state,
                    )

                if view:
                    self.bot.add_view(view, message_id=game["message_id"])
            except Exception as e:
                LOGGER.error(f"Failed to restore casino game {game['message_id']}: {e}")

    async def cog_check(self, ctx: commands.Context) -> bool:
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return False

        store = self.store
        if store is None:
            await ctx.send("Economy system is not ready yet. Try again in a moment.")
            return False

        config = await asyncio.to_thread(store.get_guild_config, ctx.guild.id)
        shop_channel_id = _coerce_optional_int(config.get("shop_channel_id"))
        bypass_role_id = _coerce_optional_int(config.get("shop_bypass_role_id"))

        if not shop_channel_id:
            return True

        if (
            bypass_role_id
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

    async def cog_load(self):
        asyncio.create_task(self.restore_active_games())
        self.debt_penalty_loop.start()
        self.loan_default_loop.start()

    def cog_unload(self):
        self.debt_penalty_loop.cancel()
        self.loan_default_loop.cancel()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        if not isinstance(message.author, discord.Member):
            return
        if not message.content.strip():
            return

        # Give standard message credits
        await asyncio.to_thread(
            self.store.add_credits,
            message.guild.id,
            message.author.id,
            CREDITS_PER_MESSAGE,
        )

        import random
        if random.random() < 0.02:
            try:
                await asyncio.to_thread(
                    self.store.grant_pet_item,
                    message.guild.id,
                    message.author.id,
                    "soul_shard",
                    1,
                )
                await message.channel.send(f"{message.author.mention} you found a **Soul Shard**! 💎")
            except Exception:
                pass

    @commands.Cog.listener()
    async def on_level_up(self, member: discord.Member, new_level: int):
        """Custom event listener for level ups to give bonus credits."""
        bonus = new_level * 100
        await asyncio.to_thread(
            self.store.add_credits, member.guild.id, member.id, bonus
        )

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return

        # Restore purchased roles
        try:
            purchased_roles = await asyncio.to_thread(
                self.store.get_purchased_roles, member.guild.id, member.id
            )
            roles_to_add = []
            for role_name in purchased_roles:
                role = discord.utils.get(member.guild.roles, name=role_name)
                if role:
                    roles_to_add.append(role)

            if roles_to_add:
                bot_member = member.guild.me
                valid_roles = [r for r in roles_to_add if r < bot_member.top_role]
                if valid_roles:
                    await member.add_roles(
                        *valid_roles, reason="Restoring purchased shop roles"
                    )
        except Exception as exc:
            LOGGER.error("Failed to restore roles for %s: %s", member.id, exc)

    async def _profile_card_settings(
        self, guild_id: int, user_id: int
    ) -> Optional[dict[str, Any]]:
        level_sys = getattr(self.bot, "level_system", None)
        store = getattr(level_sys, "store", None)
        if store is None:
            return None
        try:
            return await asyncio.to_thread(store.get_card_settings, guild_id, user_id)
        except Exception:
            return None

    async def _fetch_economy_leaderboard_backgrounds(
        self, guild_id: int, rows: list[dict[str, Any]]
    ) -> dict[int, bytes]:
        async def fetch_one(user_id: int):
            settings = await self._profile_card_settings(guild_id, user_id)
            background_data = _background_data_from_settings(settings)
            if background_data is not None:
                return user_id, background_data
            background_url = (settings or {}).get("background_url")
            if not background_url:
                return user_id, None
            try:
                data = await asyncio.to_thread(
                    _read_background_bytes,
                    str(background_url),
                    Path(__file__).resolve().parent,
                )
                return user_id, data
            except Exception:
                return user_id, None

        results = await asyncio.gather(
            *(fetch_one(int(row["user_id"])) for row in rows[:10])
        )
        return {user_id: data for user_id, data in results if data is not None}

    async def _generate_economy_leaderboard_card(
        self,
        guild: discord.Guild,
        rows: list[dict[str, Any]],
        bg_data: Optional[dict[int, bytes]] = None,
    ) -> io.BytesIO:
        if bg_data is None:
            bg_data = {}

        width = ECONOMY_LEADERBOARD_CARD_WIDTH
        height = ECONOMY_LEADERBOARD_CARD_HEIGHT
        assets_dir = Path(__file__).resolve().parent.parent / "assets"
        background_path = assets_dir / "bal_bg.png"

        if background_path.exists():
            bg = (
                Image.open(background_path)
                .convert("RGBA")
                .resize((width, height), Image.LANCZOS)
            )
            bg = bg.filter(ImageFilter.GaussianBlur(radius=2.0))
        else:
            bg = Image.new("RGBA", (width, height), (12, 14, 22, 255))

        card = Image.alpha_composite(
            bg, Image.new("RGBA", (width, height), (6, 7, 12, 226))
        )
        glow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        glow_draw = ImageDraw.Draw(glow)
        gold = _color_tuple(DEFAULT_ECONOMY_ACCENT)
        glow_draw.ellipse(
            (230, -80, 900, 260), fill=_color_tuple(DEFAULT_ECONOMY_ACCENT, 42)
        )
        glow_draw.rectangle(
            (560, 0, width, height), fill=(gold[0], gold[1], gold[2], 34)
        )
        glow = glow.filter(ImageFilter.GaussianBlur(radius=26))
        card = Image.alpha_composite(card, glow)
        draw = ImageDraw.Draw(card)

        bold_path = assets_dir / "Montserrat-Bold.ttf"
        heavy_path = assets_dir / "Montserrat-ExtraBold.ttf"
        title_font = _load_font(heavy_path, 38, bold=True)
        header_font = _load_font(bold_path, 18, bold=True)
        name_font_path = heavy_path
        stat_font = _load_font(heavy_path, 17, bold=True)
        muted_font = _load_font(bold_path, 13, bold=True)
        rank_font = _load_font(heavy_path, 24, bold=True)

        draw.text(
            (36, 30), "Credits Leaderboard", fill=(248, 249, 255), font=title_font
        )
        draw.text((39, 78), guild.name, fill=(166, 168, 190), font=header_font)

        row_x = 36
        row_y = 122
        row_w = 728
        row_h = 46
        gap = 9
        top_total = max(1, int(rows[0].get("total") or rows[0].get("credits") or 1))

        for index, row in enumerate(rows[:10], start=1):
            y = row_y + ((row_h + gap) * (index - 1))
            user_id = int(row["user_id"])
            wallet = int(row.get("credits") or 0)
            bank = int(row.get("bank") or 0)
            total = int(row.get("total") or (wallet + bank))
            member = guild.get_member(user_id)
            name = (
                _member_card_name(member) if member is not None else f"User {user_id}"
            )
            settings = await self._profile_card_settings(guild.id, user_id)
            user_accent_int = _settings_accent(settings)
            user_accent = _color_tuple(user_accent_int)

            if user_id in bg_data:
                try:
                    with Image.open(io.BytesIO(bg_data[user_id])) as row_bg_img:
                        row_bg_img.seek(0)
                        row_bg = row_bg_img.convert("RGBA")
                        row_bg = ImageOps.fit(
                            row_bg, (row_w, row_h), method=Image.LANCZOS
                        )
                        row_bg = Image.alpha_composite(
                            row_bg, Image.new("RGBA", (row_w, row_h), (0, 0, 0, 152))
                        )

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

            rank_color = user_accent if index == 1 else (203, 205, 220, 255)
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
                try:
                    avatar = _circle_avatar(await _read_avatar(member), avatar_size)
                    card.paste(avatar, (avatar_x, avatar_y), avatar)
                except (discord.HTTPException, OSError):
                    draw.ellipse(
                        (
                            avatar_x,
                            avatar_y,
                            avatar_x + avatar_size,
                            avatar_y + avatar_size,
                        ),
                        fill=user_accent,
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
                name_font_path,
                max_width=245,
                start_size=21,
                min_size=15,
                bold=True,
            )
            draw.text(
                (row_x + 104, y + 10), name, fill=(248, 249, 255), font=fitted_name_font
            )

            total_text = f"{_format_compact_number(total)} cr"
            wallet_text = f"{_format_compact_number(wallet)} wallet"
            bank_text = f"{_format_compact_number(bank)} bank"
            draw.text(
                (row_x + 372, y + 8), total_text, fill=user_accent, font=stat_font
            )
            draw.text(
                (row_x + 506, y + 8), wallet_text, fill=(248, 249, 255), font=stat_font
            )
            draw.text(
                (row_x + 634, y + 8), bank_text, fill=(166, 168, 190), font=muted_font
            )

            bar_x = row_x + 104
            bar_y = y + row_h - 7
            bar_w = 610
            bar_h = 4
            progress = max(0.0, min(total / top_total, 1.0))
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

    def _generate_bal_card(
        self,
        target: discord.Member,
        balance: int,
        avatar_bytes: bytes = None,
        card_settings: dict = None,
        owned_roles: list = None,
        bank: int = 0,
    ) -> io.BytesIO:
        W, H = 1600, 400

        # Use user's ms profile accent color, fallback to gold
        raw_accent = (card_settings or {}).get("accent_color")
        try:
            accent_int = int(raw_accent) if raw_accent is not None else 0xF1C40F
        except (TypeError, ValueError):
            accent_int = 0xF1C40F
        accent_rgb = (
            (accent_int >> 16) & 255,
            (accent_int >> 8) & 255,
            accent_int & 255,
        )
        ambient = (
            int(accent_rgb[0] * 0.7),
            int(accent_rgb[1] * 0.5),
            int(accent_rgb[2] * 0.3),
        )

        # Use user's ms profile background, fallback to bal_bg.png
        custom_bg_url = (card_settings or {}).get("background_url")
        custom_bg_data = _background_data_from_settings(card_settings)
        bg_frames = []
        duration = 100
        has_custom_bg = bool(custom_bg_url or custom_bg_data is not None)
        try:
            if custom_bg_data is not None or custom_bg_url:
                if custom_bg_data is not None:
                    img_bytes = custom_bg_data
                else:
                    img_bytes = _read_background_bytes(
                        str(custom_bg_url), Path(__file__).resolve().parent
                    )
                img = Image.open(io.BytesIO(img_bytes))
                raw_dur = img.info.get("duration")
                duration = int(raw_dur) if raw_dur is not None else 100
                if getattr(img, "is_animated", False):
                    for i in range(min(getattr(img, "n_frames", 1), 40)):
                        img.seek(i)
                        frame = img.convert("RGBA")
                        sw, sh = frame.size
                        scale = max(W / sw, H / sh)
                        frame = frame.resize((int(sw * scale), int(sh * scale)), Image.Resampling.BILINEAR)
                        left = (frame.width - W) // 2
                        top = (frame.height - H) // 2
                        bg_frames.append(frame.crop((left, top, left + W, top + H)))
                else:
                    bg = img.convert("RGBA")
                    sw, sh = bg.size
                    scale = max(W / sw, H / sh)
                    bg = bg.resize((int(sw * scale), int(sh * scale)), Image.Resampling.LANCZOS)
                    left = (bg.width - W) // 2
                    top = (bg.height - H) // 2
                    bg_frames.append(bg.crop((left, top, left + W, top + H)))
            else:
                raise ValueError("no custom bg")
        except Exception:
            try:
                base_dir = Path(__file__).resolve().parent.parent / "assets"
                bg_path = None
                for ext in (".gif", ".png", ".jpg", ".jpeg"):
                    p = base_dir / f"bal_bg{ext}"
                    if os.path.exists(p):
                        bg_path = p
                        break
                if bg_path:
                    img = Image.open(bg_path)
                    raw_dur = img.info.get("duration")
                    duration = int(raw_dur) if raw_dur is not None else 100
                    if getattr(img, "is_animated", False):
                        for i in range(min(getattr(img, "n_frames", 1), 40)):
                            img.seek(i)
                            bg = img.convert("RGBA").resize((W, H), Image.LANCZOS)
                            bg = bg.filter(ImageFilter.GaussianBlur(radius=2.0))
                            bg_frames.append(bg)
                    else:
                        bg = img.convert("RGBA").resize((W, H), Image.LANCZOS)
                        bg = bg.filter(ImageFilter.GaussianBlur(radius=2.0))
                        bg_frames.append(bg)
                else:
                    bg_frames.append(Image.new("RGBA", (W, H), (12, 14, 22, 255)))
            except Exception:
                bg_frames.append(Image.new("RGBA", (W, H), (12, 14, 22, 255)))

        # Dark overlay
        foreground = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        overlay_alpha = 100 if has_custom_bg else 200
        overlay = Image.new("RGBA", (W, H), (7, 8, 15, overlay_alpha))
        foreground = Image.alpha_composite(foreground, overlay)

        # Ambient accent glow (downscaled for performance)
        rW, rH = W // 2, H // 2
        glow = Image.new("RGBA", (rW, rH), (0, 0, 0, 0))
        glow_draw = ImageDraw.Draw(glow)
        for x in range(rW):
            alpha = int(max(0, (x - 510 / 2) / (290 / 2)) * 35)
            glow_draw.line([(x, 0), (x, rH)], fill=(*ambient, alpha))
        glow_draw.ellipse(
            (210 // 2, 128 // 2, 1420 // 2, 548 // 2), fill=(*ambient, 12)
        )
        glow = glow.filter(ImageFilter.GaussianBlur(radius=22))
        glow = glow.resize((W, H), Image.Resampling.BICUBIC)
        foreground = Image.alpha_composite(foreground, glow)
        draw = ImageDraw.Draw(foreground)

        # Avatar with accent ring
        avatar_size = 320
        avatar_x = 32
        avatar_y = 36
        if avatar_bytes:
            try:
                av = _circle_avatar(avatar_bytes, avatar_size)
                draw.ellipse(
                    (
                        avatar_x - 6,
                        avatar_y - 6,
                        avatar_x + avatar_size + 6,
                        avatar_y + avatar_size + 6,
                    ),
                    fill=accent_rgb,
                )
                foreground.paste(av, (avatar_x, avatar_y), av)
            except Exception:
                draw.ellipse(
                    (
                        avatar_x - 6,
                        avatar_y - 6,
                        avatar_x + avatar_size + 6,
                        avatar_y + avatar_size + 6,
                    ),
                    fill=accent_rgb,
                )
        else:
            draw.ellipse(
                (
                    avatar_x - 6,
                    avatar_y - 6,
                    avatar_x + avatar_size + 6,
                    avatar_y + avatar_size + 6,
                ),
                fill=accent_rgb,
            )

        # Fonts
        assets_dir = Path(__file__).resolve().parent.parent / "assets"
        bold_path = assets_dir / "Montserrat-Bold.ttf"
        heavy_path = assets_dir / "Montserrat-ExtraBold.ttf"
        try:
            meta_font = ImageFont.truetype(bold_path, 36)
            value_font = ImageFont.truetype(heavy_path, 36)
            items_font = ImageFont.truetype(bold_path, 28)
        except Exception:
            meta_font = value_font = items_font = ImageFont.load_default()

        # Name
        display_name = _member_card_name(target)
        if len(display_name) > 24:
            display_name = display_name[:22] + "..."
        name_text = f"@{display_name}"
        try:
            name_font = ImageFont.truetype(heavy_path, 80)
            test_draw = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
            bbox = test_draw.textbbox((0, 0), name_text, font=name_font)
            while (bbox[2] - bbox[0]) > 1100 and name_font.size > 48:
                name_font = ImageFont.truetype(heavy_path, name_font.size - 2)
                bbox = test_draw.textbbox((0, 0), name_text, font=name_font)
        except Exception:
            name_font = ImageFont.load_default()

        text_x = 416
        draw.text((text_x, 84), name_text, fill=(248, 249, 255), font=name_font)

        # Balance with suffix measured as one line, so "cr" stays aligned with the number.
        y = 188
        bal_text = f"{balance:,}"
        stat_text_y = y + 7
        label_x = text_x + 12
        label_text = "Balance"
        label_w = draw.textlength(label_text, font=meta_font)
        value_x = label_x + label_w + 8
        value_w = draw.textlength(bal_text, font=value_font)
        suffix_x = value_x + value_w + 5
        value_bbox = draw.textbbox((value_x, stat_text_y), bal_text, font=value_font)
        suffix_bbox = draw.textbbox((suffix_x, stat_text_y), "cr", font=meta_font)
        suffix_y = (
            stat_text_y
            + ((value_bbox[3] - value_bbox[1]) - (suffix_bbox[3] - suffix_bbox[1])) // 2
        )

        draw.text(
            (label_x, stat_text_y), label_text, fill=(168, 170, 192), font=meta_font
        )
        draw.text((value_x, stat_text_y), bal_text, fill=accent_rgb, font=value_font)
        draw.text((suffix_x, suffix_y), "cr", fill=(168, 170, 192), font=meta_font)

        # Bank Balance
        bank_y = stat_text_y + 60
        bank_text = f"{bank:,}"
        bank_label = "Bank"
        bank_value_w = draw.textlength(bank_text, font=value_font)
        bank_suffix_x = value_x + bank_value_w + 5

        draw.text((label_x, bank_y), bank_label, fill=(168, 170, 192), font=meta_font)
        draw.text((value_x, bank_y), bank_text, fill=accent_rgb, font=value_font)
        draw.text(
            (
                bank_suffix_x,
                bank_y
                + ((value_bbox[3] - value_bbox[1]) - (suffix_bbox[3] - suffix_bbox[1]))
                // 2,
            ),
            "cr",
            fill=(168, 170, 192),
            font=meta_font,
        )
        # Items section - list owned shop roles vertically
        items_x = text_x + 468
        items_label_font = meta_font
        draw.text(
            (items_x, y + 7), "Items", fill=(168, 170, 192), font=items_label_font
        )

        if owned_roles:
            item_y = y + 52
            for role_name in owned_roles[:4]:  # Max 4 to fit
                if len(role_name) > 20:
                    role_name = role_name[:18] + ".."
                draw.text(
                    (items_x, item_y),
                    f"• {role_name}",
                    fill=(248, 249, 255),
                    font=items_font,
                )
                item_y += 40
            if len(owned_roles) > 4:
                draw.text(
                    (items_x, item_y),
                    f"  +{len(owned_roles) - 4} more",
                    fill=(120, 122, 145),
                    font=items_font,
                )
        else:
            draw.text((items_x, y + 52), "None", fill=(120, 122, 145), font=items_font)

        final_frames = []
        for bg_frame in bg_frames:
            final_frames.append(
                Image.alpha_composite(bg_frame, foreground).convert("RGB")
            )

        buf = io.BytesIO()
        if len(final_frames) > 1:
            scaled_frames = [f.resize((800, 200), Image.Resampling.LANCZOS) for f in final_frames]
            scaled_frames[0].save(
                buf,
                format="GIF",
                save_all=True,
                append_images=scaled_frames[1:],
                duration=duration,
                loop=0,
                optimize=True,
            )
        else:
            final_frames[0].save(buf, format="PNG", optimize=True)
        buf.seek(0)
        return buf

    @commands.command(
        name="bal", aliases=["balance", "credits"], help="Check your credit balance"
    )
    async def bal(self, ctx: commands.Context, member: Optional[discord.Member] = None):
        target = member or ctx.author
        msg = await ctx.send("Fetching balance...")

        async def load_balances() -> tuple[int, int]:
            if hasattr(self.store, "get_balances"):
                return await asyncio.to_thread(
                    self.store.get_balances, ctx.guild.id, target.id
                )
            wallet = await asyncio.to_thread(
                self.store.get_balance, ctx.guild.id, target.id
            )
            return wallet, 0

        async def load_card_settings() -> Optional[dict[str, Any]]:
            level_sys = getattr(self.bot, "level_system", None)
            if level_sys is None:
                return None
            return await asyncio.to_thread(
                level_sys.store.get_card_settings, ctx.guild.id, target.id
            )

        async def load_avatar() -> Optional[bytes]:
            try:
                avatar = target.display_avatar.with_size(512).with_format("png")
                return await avatar.read()
            except Exception:
                return None

        (wallet, bank), card_settings, avatar_bytes = await asyncio.gather(
            load_balances(),
            load_card_settings(),
            load_avatar(),
        )
        balance = wallet

        # Fetch owned shop roles from the database to ensure sync with wipes/purchases
        owned_roles = []
        if hasattr(self.store, "get_purchased_roles"):
            owned_roles = await asyncio.to_thread(
                self.store.get_purchased_roles, ctx.guild.id, target.id
            )

        card = await asyncio.to_thread(
            self._generate_bal_card,
            target,
            balance,
            avatar_bytes,
            card_settings,
            owned_roles,
            bank,
        )
        is_gif = card.getvalue()[:4] == b"GIF8"
        ext = "gif" if is_gif else "png"
        await msg.delete()
        await ctx.send(file=discord.File(card, filename=f"balance.{ext}"))

    @commands.command(
        name="ecolb",
        aliases=["economylb", "creditslb", "moneylb"],
        help="Show the server credit leaderboard",
    )
    async def ecolb(self, ctx: commands.Context):
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return

        msg = await ctx.send("Fetching economy leaderboard...")
        rows = await asyncio.to_thread(self.store.get_top_balances, ctx.guild.id, 10)
        rows = [
            row for row in rows if int(row.get("total") or row.get("credits") or 0) > 0
        ]
        if not rows:
            await msg.delete()
            embed = discord.Embed(
                title="Credits Leaderboard",
                description="No credits have been earned yet.",
                color=DEFAULT_ECONOMY_ACCENT,
            )
            await ctx.send(embed=embed)
            return

        await msg.edit(content="Fetching backgrounds...")
        bg_data = await self._fetch_economy_leaderboard_backgrounds(ctx.guild.id, rows)
        card = await self._generate_economy_leaderboard_card(ctx.guild, rows, bg_data)
        await msg.delete()
        await ctx.send(file=_card_file(card, "economy-leaderboard"))

    async def _send_tools_status(self, ctx: commands.Context) -> None:
        items, security = await asyncio.gather(
            asyncio.to_thread(
                self.store.get_economy_items, ctx.guild.id, ctx.author.id
            ),
            asyncio.to_thread(
                self.store.get_member_security, ctx.guild.id, ctx.author.id
            ),
        )
        item_lines = [
            f"**{data['name']}**: `{int(items.get(item_key, 0)):,}`"
            for item_key, data in ECONOMY_ITEM_DEFS.items()
        ]
        lock_level = int(security.get("lock_level") or 0)
        wanted = int(security.get("wanted_level") or 0)
        jail_until = _as_utc(security.get("jail_until"))
        now = datetime.now(timezone.utc)
        jail_text = (
            f"<t:{_discord_timestamp(jail_until)}:R>"
            if jail_until and jail_until > now
            else "Free"
        )

        cooldown_labels = {
            "rob": "Rob",
            "jewelry": "Jewelry",
            "bank": "Bank",
            "team_bank": "Team Bank",
        }
        cooldown_lines = []
        for key, label in cooldown_labels.items():
            last_used = _as_utc(security.get(ROBBERY_COOLDOWN_COLUMNS[key]))
            ready_at = last_used + ROBBERY_COOLDOWNS[key] if last_used else None
            status = (
                f"<t:{_discord_timestamp(ready_at)}:R>"
                if ready_at and ready_at > now
                else "Ready"
            )
            cooldown_lines.append(f"{label}: **{status}**")

        description = (
            f"**Inventory**\n{chr(10).join(item_lines)}\n\n"
            "**Security**\n"
            f"Lock: **{LOCK_LEVELS.get(lock_level, LOCK_LEVELS[0])['name']}**\n"
            f"Wanted: **{wanted}/10**\n"
            f"Status: **{jail_text}**\n\n"
            f"**Cooldowns**\n{chr(10).join(cooldown_lines)}"
        )
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(
            branded_panel_container(
                title=f"{ctx.author.display_name}'s Tools",
                description=description,
                accent_color=0x2B2D31,
                min_width_chars=None,
            )
        )
        await ctx.send(
            view=ensure_layout_view_action_rows(view),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    def _simple_panel_view(
        self,
        *,
        title: str,
        description: str,
        accent_color: int = 0x2B2D31,
        image_path: Optional[Path] = None,
    ) -> tuple[discord.ui.LayoutView, Optional[discord.File]]:
        view = discord.ui.LayoutView(timeout=None)
        container = branded_panel_container(
            title=title,
            description=description,
            accent_color=accent_color,
            min_width_chars=None,
        )
        file = None
        if image_path and image_path.exists():
            filename = f"booster-{image_path.name}"
            file = discord.File(image_path, filename=filename)
            container.add_item(
                discord.ui.Separator(spacing=discord.SeparatorSpacing.large)
            )
            container.add_item(
                discord.ui.MediaGallery(
                    discord.MediaGalleryItem(f"attachment://{filename}")
                )
            )
        view.add_item(container)
        return ensure_layout_view_action_rows(view), file

    def _boost_thanks_view(self, member: discord.Member) -> discord.ui.LayoutView:
        view = discord.ui.LayoutView(timeout=None)
        avatar_url = member.display_avatar.with_size(128).url
        children: list[discord.ui.Item[Any]] = [
            discord.ui.Section(
                discord.ui.TextDisplay(
                    f"**Server Boost Activated**\n"
                    f"{member.mention} boosted **{member.guild.name}**.\n"
                    f"Booster economy perks are now active: **+{BOOSTER_BONUS_RATE * 100:.0f}%** on perked payouts."
                ),
                accessory=discord.ui.Thumbnail(avatar_url),
            ),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.large),
            discord.ui.TextDisplay(
                "**Active Stack**\n"
                "Booster bonus stacks with pets, prestige, and shop-role perks while payout caps keep rewards controlled."
            ),
            discord.ui.TextDisplay("Use `.perks` to check the full bonus breakdown."),
        ]
        view.add_item(discord.ui.Container(*children, accent_color=0xF47FFF))
        return ensure_layout_view_action_rows(view)

    async def _send_boost_thanks(self, member: discord.Member) -> None:
        panel = await asyncio.to_thread(self.store.get_booster_panel, member.guild.id)
        if not panel:
            return
        channel = member.guild.get_channel(int(panel["channel_id"]))
        if channel is None:
            try:
                fetched = await member.guild.fetch_channel(int(panel["channel_id"]))
            except (discord.Forbidden, discord.HTTPException):
                return
            channel = fetched if isinstance(fetched, discord.abc.Messageable) else None
        if channel is None:
            return

        view = self._boost_thanks_view(member)
        try:
            await channel.send(
                content=member.mention,
                view=view,
                allowed_mentions=discord.AllowedMentions(users=[member]),
            )
            await asyncio.to_thread(
                self.store.mark_boost_thanked, member.guild.id, member.id
            )
        except (discord.Forbidden, discord.HTTPException):
            LOGGER.warning(
                "Failed to send boost thank-you for %s in %s",
                member.id,
                member.guild.id,
            )

    @commands.Cog.listener()
    async def on_member_update(
        self, before: discord.Member, after: discord.Member
    ) -> None:
        if before.guild.id != after.guild.id:
            return
        if before.premium_since is None and after.premium_since is not None:
            await asyncio.to_thread(
                self.store.set_member_booster, after.guild.id, after.id, True
            )
            await self._send_boost_thanks(after)
            return
        if before.premium_since is not None and after.premium_since is None:
            await asyncio.to_thread(
                self.store.set_member_booster, after.guild.id, after.id, False
            )

    @commands.command(
        name="tools",
        aliases=["gear", "locks"],
        help="Show your robbery tools, locks, wanted level, and jail status.",
    )
    async def tools_cmd(self, ctx: commands.Context):
        await self._send_tools_status(ctx)

    def _match_item_key(self, query: str, allowed: Iterable[str]) -> Optional[str]:
        cleaned = (query or "").strip().lower().replace("_", " ")
        if not cleaned:
            return None
        allowed_set = set(allowed)
        for item_key in allowed_set:
            data = ECONOMY_ITEM_DEFS.get(item_key, {})
            candidates = {
                item_key,
                item_key.replace("_", " "),
                str(data.get("name", "")).lower(),
            }
            candidates.update(str(alias).lower() for alias in data.get("aliases", set()))
            if cleaned in candidates:
                return item_key
        return None

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

    def _shop_lines(self, items: dict[str, int], *, femboy: bool = False) -> str:
        lines = []
        for item_key, price in items.items():
            data = ECONOMY_ITEM_DEFS[item_key]
            price_label = "Pwice" if femboy else "Price"
            lines.append(
                f"**{data['name']}** - {price_label} **{int(price):,} cr**\n"
                f"`{item_key}` - {data.get('desc', 'No description.')}"
            )
        return "\n\n".join(lines)

    @commands.command(
        name="gearshop",
        aliases=["taskshop"],
        help="View fishing, hunting, and mining gear upgrades.",
    )
    async def gearshop_cmd(self, ctx: commands.Context):
        femboy = await self._shop_femboy_mode(ctx.guild)
        title = "Task Geaw Shop UwU" if femboy else "Task Gear Shop"
        note = (
            "Pick a cute toowie with `.buygear <item>`, cutie~"
            if femboy
            else "Buy with `.buygear <item>`."
        )
        embed = self.create_embed(
            title,
            self._shop_lines(GEAR_SHOP_ITEMS, femboy=femboy) + f"\n\n{note}",
            color=0x3498DB,
        )
        await self._send(ctx, embed)

    @commands.command(
        name="utilityshop",
        aliases=["potionshop", "buttonshop"],
        help="View potions and reset buttons.",
    )
    async def utilityshop_cmd(self, ctx: commands.Context):
        femboy = await self._shop_femboy_mode(ctx.guild)
        title = "Utiwity Shop UwU" if femboy else "Utility Shop"
        note = (
            "Sip cute potions with `.buypotion <item>` or gwab a shop weset with `.buyutility <item>`~"
            if femboy
            else "Buy potions with `.buypotion <item>` or shop reset buttons with `.buyutility <item>`."
        )
        embed = self.create_embed(
            title,
            self._shop_lines(UTILITY_SHOP_ITEMS, femboy=femboy) + f"\n\n{note}",
            color=0x9B59B6,
        )
        await self._send(ctx, embed)

    @commands.command(name="buygear", aliases=["buytool"], help="Buy task gear.")
    async def buygear_cmd(self, ctx: commands.Context, *, item: str = ""):
        femboy = await self._shop_femboy_mode(ctx.guild)
        item_key = self._match_item_key(item, GEAR_SHOP_ITEMS.keys())
        if not item_key:
            return await ctx.send(
                "Use `.buygear <gear item>`, cutie~"
                if femboy
                else "Usage: `.buygear <gear item>`"
            )
        result = await asyncio.to_thread(
            self.store.buy_utility_item,
            ctx.guild.id,
            ctx.author.id,
            item_key,
            GEAR_SHOP_ITEMS[item_key],
        )
        if not result.get("ok"):
            return await ctx.send(
                (
                    f"Awww, chu need **{int(result.get('price', GEAR_SHOP_ITEMS[item_key])):,} cr** for dat geaw, cutie~"
                    if femboy
                    else f"You need **{int(result.get('price', GEAR_SHOP_ITEMS[item_key])):,} cr** for that gear."
                )
            )
        data = ECONOMY_ITEM_DEFS[item_key]
        await ctx.send(
            (
                f"Bought **{data['name']}** for **{int(result['price']):,} cr**! Owned: **{int(result['quantity']):,}** UwU~"
                if femboy
                else f"Bought **{data['name']}** for **{int(result['price']):,} cr**. Owned: **{int(result['quantity']):,}**."
            )
        )

    @commands.command(
        name="buyutility",
        aliases=["buybutton", "buyreset"],
        help="Buy utility buttons.",
    )
    async def buyutility_cmd(self, ctx: commands.Context, *, item: str = ""):
        femboy = await self._shop_femboy_mode(ctx.guild)
        item_key = self._match_item_key(item, UTILITY_SHOP_ITEMS.keys())
        if not item_key:
            return await ctx.send(
                "Use `.buyutility <reset shop>`, sweetie~"
                if femboy
                else "Usage: `.buyutility <reset shop>`"
            )
        result = await asyncio.to_thread(
            self.store.buy_utility_item,
            ctx.guild.id,
            ctx.author.id,
            item_key,
            UTILITY_SHOP_ITEMS[item_key],
        )
        if not result.get("ok"):
            return await ctx.send(
                (
                    f"Awww, chu need **{int(result.get('price', UTILITY_SHOP_ITEMS[item_key])):,} cr** for dat item, cutie~"
                    if femboy
                    else f"You need **{int(result.get('price', UTILITY_SHOP_ITEMS[item_key])):,} cr** for that item."
                )
            )
        data = ECONOMY_ITEM_DEFS[item_key]
        await ctx.send(
            (
                f"Bought **{data['name']}** for **{int(result['price']):,} cr**! Owned: **{int(result['quantity']):,}** UwU~"
                if femboy
                else f"Bought **{data['name']}** for **{int(result['price']):,} cr**. Owned: **{int(result['quantity']):,}**."
            )
        )

    @commands.command(name="buypotion", aliases=["potionbuy"], help="Buy a potion.")
    async def buypotion_cmd(self, ctx: commands.Context, *, item: str = ""):
        femboy = await self._shop_femboy_mode(ctx.guild)
        potion_prices = {
            key: UTILITY_SHOP_ITEMS[key]
            for key in (*LUCK_EFFECTS.keys(), *DEFENSE_EFFECTS.keys())
        }
        item_key = self._match_item_key(item, potion_prices.keys())
        if not item_key:
            return await ctx.send(
                "Use `.buypotion <luck1|luck2|luck3|defense>`, cutie~"
                if femboy
                else "Usage: `.buypotion <luck1|luck2|luck3|defense>`"
            )
        result = await asyncio.to_thread(
            self.store.buy_utility_item,
            ctx.guild.id,
            ctx.author.id,
            item_key,
            potion_prices[item_key],
        )
        if not result.get("ok"):
            return await ctx.send(
                (
                    f"Awww, chu need **{int(result.get('price', potion_prices[item_key])):,} cr** for dat potion, sweetie~"
                    if femboy
                    else f"You need **{int(result.get('price', potion_prices[item_key])):,} cr** for that potion."
                )
            )
        data = ECONOMY_ITEM_DEFS[item_key]
        await ctx.send(
            (
                f"Bought **{data['name']}** for **{int(result['price']):,} cr**! Dwink it with `.usepotion {item_key}` UwU~"
                if femboy
                else f"Bought **{data['name']}** for **{int(result['price']):,} cr**. Use it with `.usepotion {item_key}`."
            )
        )

    @commands.command(name="usepotion", aliases=["drinkpotion"], help="Use a potion.")
    async def usepotion_cmd(self, ctx: commands.Context, *, item: str = ""):
        item_key = self._match_item_key(item, (*LUCK_EFFECTS.keys(), *DEFENSE_EFFECTS.keys()))
        if not item_key:
            return await ctx.send("Usage: `.usepotion <luck1|luck2|luck3|defense>`")
        effects = LUCK_EFFECTS if item_key in LUCK_EFFECTS else DEFENSE_EFFECTS
        effect_key = "luck" if item_key in LUCK_EFFECTS else "defense"
        data = effects[item_key]
        result = await asyncio.to_thread(
            self.store.activate_effect,
            ctx.guild.id,
            ctx.author.id,
            item_key,
            effect_key,
            float(data["rate"]),
            int(data["minutes"]),
        )
        if not result.get("ok"):
            reason = result.get("reason")
            if reason == "active":
                return await ctx.send(
                    f"You already have an active **{effect_key}** potion. Potions do not stack."
                )
            if reason == "missing":
                return await ctx.send("You do not own that potion.")
            return await ctx.send("That potion could not be used.")
        expires_at = _as_utc(result["expires_at"])
        await ctx.send(
            f"Activated **{ECONOMY_ITEM_DEFS[item_key]['name']}** until <t:{_discord_timestamp(expires_at)}:R>."
        )

    @commands.command(name="effects", aliases=["buffs"], help="Show active potion effects.")
    async def effects_cmd(self, ctx: commands.Context):
        effects = await asyncio.to_thread(
            self.store.get_active_effects, ctx.guild.id, ctx.author.id
        )
        if not effects:
            return await ctx.send("You have no active potion effects.")
        lines = []
        for key, row in effects.items():
            expires_at = _as_utc(row["expires_at"])
            lines.append(
                f"**{key.title()}**: +{float(row['rate']) * 100:.0f}% until <t:{_discord_timestamp(expires_at)}:R>"
            )
        await ctx.send("\n".join(lines))

    @commands.command(name="resetshop", aliases=["shopreset"], help="Use a shop reset button.")
    async def resetshop_cmd(self, ctx: commands.Context):
        consumed = await asyncio.to_thread(
            self.store.consume_economy_item,
            ctx.guild.id,
            ctx.author.id,
            "shop_reset_token",
            1,
        )
        if not consumed.get("ok"):
            return await ctx.send("You need a **Shop Reset Button** from `.utilityshop`.")
        await asyncio.gather(
            asyncio.to_thread(self.store.refresh_inventory, ctx.guild.id, SHOP_ITEMS),
            asyncio.to_thread(
                self.store.refresh_blackmarket_inventory,
                ctx.guild.id,
                ECONOMY_ITEM_DEFS,
                LOCK_LEVELS,
            ),
        )
        await ctx.send("Shop and blackmarket stock were refreshed.")

    @commands.command(
        name="resetcooldown",
        aliases=["cdreset", "cooldownreset"],
        help="Use a cooldown reset button on one economy command.",
    )
    async def resetcooldown_cmd(self, ctx: commands.Context, command_name: str = ""):
        command_name = command_name.strip().lower().lstrip(".")
        if not command_name:
            return await ctx.send("Usage: `.resetcooldown <command>`")
        command = self.bot.get_command(command_name)
        if command is None:
            return await ctx.send("I could not find that command.")
        consumed = await asyncio.to_thread(
            self.store.consume_economy_item,
            ctx.guild.id,
            ctx.author.id,
            "cooldown_reset_token",
            1,
        )
        if not consumed.get("ok"):
            return await ctx.send("You need a **Cooldown Reset Button** in your inventory.")
        if hasattr(self.store, "clear_member_cooldowns"):
            await asyncio.to_thread(
                self.store.clear_member_cooldowns,
                ctx.guild.id,
                ctx.author.id,
                command.name,
            )
        else:
            await asyncio.to_thread(
                self.store.set_cooldown,
                ctx.guild.id,
                ctx.author.id,
                command.name,
                datetime.now(timezone.utc),
            )
        await ctx.send(f"Cooldown for `.{command.name}` was reset.")

    @commands.command(name="dailyspin", aliases=["spin"], help="Spin the daily prize wheel.")
    async def dailyspin_cmd(self, ctx: commands.Context):
        await self._ensure_cooldown_ready(ctx, 86400)
        wheel = [
            ("credits", 25_000, 28),
            ("credits", 50_000, 22),
            ("credits", 100_000, 12),
            ("item", "luck_potion_i", 10),
            ("item", "defense_potion_i", 10),
            ("item", "lockpick", 9),
            ("item", "advanced_lockpick", 5),
            ("item", "drill", 3),
            ("credits", 250_000, 1),
        ]
        pick = random.choices(wheel, weights=[entry[2] for entry in wheel], k=1)[0]
        if pick[0] == "credits":
            amount = int(pick[1])
            saved = await self._add_credits_or_reset_cooldown(
                ctx, ctx.guild.id, ctx.author.id, amount, "Daily Spin"
            )
            if not saved:
                return
            result_text = f"You won **{amount:,} cr**."
        else:
            item_key = str(pick[1])
            result = await asyncio.to_thread(
                self.store.grant_economy_item, ctx.guild.id, ctx.author.id, item_key, 1
            )
            if not result.get("ok"):
                return await ctx.send("The wheel prize could not be saved. Try again later.")
            result_text = f"You won **1x {ECONOMY_ITEM_DEFS[item_key]['name']}**."
        await self._start_cooldown(ctx, 86400)
        embed = self.create_embed("Daily Spin", result_text, color=0xF1C40F)
        await self._send(ctx, embed)

    @commands.command(name="lottery", aliases=["lotto"], help="Server lottery.")
    async def lottery_cmd(
        self, ctx: commands.Context, action: str = "status", entries: str = "1"
    ):
        action = (action or "status").strip().lower()
        if action in {"enter", "buy", "ticket", "tickets"}:
            count = _parse_credit_amount(entries)
            if count is None or count <= 0 or count > 100:
                return await ctx.send("Usage: `.lottery enter [1-100]`")
            result = await asyncio.to_thread(
                self.store.buy_lottery_ticket, ctx.guild.id, ctx.author.id, count
            )
            if not result.get("ok"):
                return await ctx.send(
                    f"You need **{int(result.get('cost', LOTTERY_TICKET_PRICE * count)):,} cr** for those tickets."
                )
            return await ctx.send(
                f"Bought **{count}** lottery ticket(s). Pool: **{int(result['pool']):,} cr**."
            )
        if action == "draw":
            is_admin = (
                ctx.author.id in ECONOMY_ADMIN_USER_IDS
                or isinstance(ctx.author, discord.Member)
                and ctx.author.guild_permissions.administrator
            )
            if not is_admin:
                return await ctx.send("Only economy admins can draw the lottery.")
            result = await asyncio.to_thread(self.store.draw_lottery, ctx.guild.id)
            if not result.get("ok"):
                return await ctx.send("The lottery has no entries.")
            return await ctx.send(
                f"<@{int(result['winner_id'])}> won the lottery pool of **{int(result['pool']):,} cr**.",
                allowed_mentions=discord.AllowedMentions.none(),
            )

        status = await asyncio.to_thread(self.store.get_lottery_status, ctx.guild.id)
        lines = [
            f"Pool: **{int(status['pool']):,} cr**",
            f"Tickets: **{int(status['total_entries']):,}**",
            f"Entry price: **{LOTTERY_TICKET_PRICE:,} cr**",
            "Use `.lottery enter [tickets]` to join.",
        ]
        await ctx.send("\n".join(lines))

    @commands.command(
        name="colorduel",
        aliases=["colourduel", "cduel"],
        help="Challenge someone to a color guessing duel.",
    )
    async def colorduel_cmd(
        self, ctx: commands.Context, member: Optional[discord.Member] = None, *, amount: str = ""
    ):
        if member is None or not amount:
            return await ctx.send("Usage: `.colorduel @user <amount>`")
        if member.bot or member.id == ctx.author.id:
            return await ctx.send("Choose another real member for the duel.")
        bet = await self._validate_bet(ctx, amount, "default")
        if bet is None:
            return
        view = ColorDuelView(self, ctx, member, bet)
        try:
            msg = await ctx.send(
                content=member.mention,
                embed=view._embed(),
                view=view,
                allowed_mentions=discord.AllowedMentions(users=[member]),
            )
            view.message = msg
        except Exception:
            await asyncio.to_thread(
                self.store.add_credits, ctx.guild.id, ctx.author.id, bet
            )
            raise

    async def _validate_bet(
        self, ctx: commands.Context, raw_amount: str, game_key: str = "default"
    ) -> Optional[int]:
        """Parse and validate a bet. Returns the bet amount or None if invalid."""
        parsed = _parse_bet(raw_amount)
        if parsed is None:
            await ctx.send("❌ Enter a valid number, or `all`.")
            return None
        limits = _casino_limits(game_key)
        balance = await asyncio.to_thread(
            self.store.get_balance, ctx.guild.id, ctx.author.id
        )
        if parsed == -1:
            parsed = min(balance, int(limits["max"]))
        if parsed < int(limits["min"]):
            await ctx.send(
                f"❌ Minimum {game_key} bet is **{int(limits['min']):,}** cr."
            )
            return None
        if parsed > int(limits["max"]):
            await ctx.send(
                f"❌ Maximum {game_key} bet is **{int(limits['max']):,}** cr."
            )
            return None
        if parsed > balance:
            await ctx.send(f"❌ You only have **{balance:,}** cr!")
            return None
        removed = await asyncio.to_thread(
            self.store.remove_credits, ctx.guild.id, ctx.author.id, parsed
        )
        if not removed:
            await ctx.send(
                "❌ Your balance changed before the bet could be placed. Try again."
            )
            return None
        return parsed

    @commands.command(
        name="gamble",
        aliases=["bet"],
        help="Play a casino game. Usage: .gamble <amount> <game> [choice]",
    )
    async def gamble_cmd(
        self,
        ctx: commands.Context,
        amount: str = "",
        game: str = "",
        *,
        option: str = "",
    ):
        game_key = _normalize_game_name(game)
        if not amount or not game_key:
            return await self.casino_cmd.callback(self, ctx)

        if game_key == "slots":
            return await self.slots_cmd.callback(self, ctx, amount=amount)
        if game_key == "coinflip":
            return await self.coinflip_cmd.callback(self, ctx, amount, choice=option)
        if game_key == "dice":
            return await self.dice_cmd.callback(self, ctx, amount=amount)
        if game_key == "multidice":
            return await self.multiplayer_dice_cmd.callback(self, ctx, amount=amount)
        if game_key == "uno":
            return await self.uno_cmd.callback(self, ctx, amount=amount)
        if game_key == "blackjack":
            return await self.blackjack_cmd.callback(self, ctx, amount=amount)
        if game_key == "roulette":
            return await self.roulette_cmd.callback(self, ctx, amount, choice=option)
        if game_key == "mines":
            return await self.mines_cmd.callback(self, ctx, amount, difficulty=option)
        if game_key == "poker":
            return await self.poker_cmd.callback(self, ctx, amount=amount)

        await ctx.send("❌ Unknown game. Use `.casino` to see the list.")

    @commands.command(name="slots", help="Spin the slot machine")
    async def slots_cmd(self, ctx: commands.Context, *, amount: str = ""):
        if not amount:
            return await ctx.send("Usage: `.slots <amount>`")
        bet = await self._validate_bet(ctx, amount, "slots")
        if bet is None:
            return

        symbols = ["🍒", "🍋", "🍊", "🍇", "💎", "7️⃣"]
        weights = [40, 30, 15, 10, 4, 1]
        result = random.choices(symbols, weights=weights, k=3)
        result_str = "".join(result)
        payout_mult = SLOT_PAYOUTS.get(result_str, 0)
        winnings = int(bet * payout_mult) if payout_mult > 0 else 0
        if payout_mult == 0:
            if (
                result[0] == result[1]
                or result[1] == result[2]
                or result[0] == result[2]
            ):
                pair_sym = (
                    result[0]
                    if result[0] == result[1] or result[0] == result[2]
                    else result[1]
                )
                if pair_sym == "7️⃣":
                    payout_mult = 5.0
                elif pair_sym == "💎":
                    payout_mult = 2.0
                elif pair_sym == "🍇":
                    payout_mult = 1.5
                elif pair_sym == "🍊":
                    payout_mult = 1.0
                else:
                    payout_mult = 0.5
                winnings = int(bet * payout_mult)
        luck_triggered = False
        if winnings <= 0:
            if await self._luck_triggers(ctx.guild.id, ctx.author.id, "slots"):
                luck_triggered = True
                result = [symbols[2], symbols[2], symbols[0]]
                result_str = "".join(result)
                payout_mult = 1.0
                winnings = bet
        pet_bonus = 0
        jackpot_bonus = 0
        jackpot_key = "slots_jackpot"
        jackpot_result = "7️⃣7️⃣7️⃣"
        if winnings <= 0 and hasattr(self.store, "add_state_amount"):
            await asyncio.to_thread(
                self.store.add_state_amount, ctx.guild.id, jackpot_key, bet // 2
            )

        msg, final_animation_file = await _send_casino_animation(
            ctx,
            "slots",
            discord.Embed(
                title="🎰 Slot Machine",
                description="**[ 🔄 🔄 🔄 ]**\n\n*Spinning the reels...*",
                color=CASINO_COLORS["slots"],
            ),
            outcome={"result": result},
        )
        await asyncio.sleep(CASINO_ANIMATION_SECONDS["slots"])

        if winnings > 0:
            if result_str == jackpot_result and hasattr(self.store, "take_state_amount"):
                jackpot_bonus = await asyncio.to_thread(
                    self.store.take_state_amount, ctx.guild.id, jackpot_key, 100_000
                )
                winnings += jackpot_bonus
            winnings, pet_bonus, _ = await self._pet_adjusted_gambling_winnings(
                ctx.guild.id, ctx.author.id, bet, winnings
            )
            await asyncio.to_thread(
                self.store.add_credits, ctx.guild.id, ctx.author.id, winnings
            )

        balance = await asyncio.to_thread(
            self.store.get_balance, ctx.guild.id, ctx.author.id
        )
        is_win = winnings > 0
        embed = discord.Embed(
            title="🎰 Slot Machine"
            + (" - JACKPOT!" if payout_mult >= 10 else " - WIN!" if is_win else ""),
            color=CASINO_COLORS["win"] if is_win else CASINO_COLORS["lose"],
        )
        embed.description = f"**[ {' '.join(result)} ]**\n\n"
        if is_win:
            profit = winnings - bet
            if payout_mult >= 10:
                embed.description += f"🎉 **{payout_mult}x MEGA WIN!** 🎉\n\n"
            else:
                embed.description += f"✨ **{payout_mult}x Multiplier!** ✨\n\n"
            embed.description += (
                f"💰 **Won:** {winnings:,} cr\n📈 **Profit:** +{profit:,} cr"
            )
            if luck_triggered:
                embed.description += "\nLuck Boost saved the spin."
            if jackpot_bonus > 0:
                embed.description += f"\nSlots Jackpot: **+{jackpot_bonus:,} cr**"
            if pet_bonus > 0:
                embed.description += f"\nPerk Bonus: **+{pet_bonus:,} cr**"
        else:
            embed.description += (
                f"💸 **Lost:** {bet:,} cr\n"
                f"Jackpot pool gained **{bet // 2:,} cr**."
            )
        embed.add_field(
            name="💎 Payout Table",
            value="7️⃣7️⃣7️⃣ = 50x  |  💎💎💎 = 10x\n🍇🍇🍇 = 5x  |  🍊🍊🍊 = 4x\n🍋🍋🍋 = 3x  |  🍒🍒🍒 = 2x",
            inline=False,
        )
        embed.set_footer(text=f"Balance: {balance:,} cr | Bet: {bet:,} cr")
        await _edit_casino_result(msg, embed, final_file=final_animation_file)

    @commands.command(name="coinflip", aliases=["cf"], help="🪙 Flip a coin")
    async def coinflip_cmd(
        self, ctx: commands.Context, amount: str = "", *, choice: str = ""
    ):
        if not amount:
            return await ctx.send("Usage: `.coinflip <amount|all> <head/tail>`")
        choice = choice.strip().lower()
        heads_choices = {"head", "heads", "h"}
        tails_choices = {"tail", "tails", "t"}
        if choice not in heads_choices | tails_choices:
            return await ctx.send(
                "❌ Choose **head** or **tail**. Example: `.coinflip all head`"
            )
        choice = "Heads" if choice in heads_choices else "Tails"

        bet = await self._validate_bet(ctx, amount, "coinflip")
        if bet is None:
            return

        result = random.choice(["Heads", "Tails"])
        won = result == choice
        luck_triggered = False
        if not won:
            if await self._luck_triggers(ctx.guild.id, ctx.author.id, "coinflip"):
                luck_triggered = True
                result = random.choice(["Heads", "Tails"])
                won = result == choice

        msg, final_animation_file = await _send_casino_animation(
            ctx,
            "coinflip",
            discord.Embed(
                title="🪙 Coin Flip",
                description=f"Choice locked: **{choice}**\n\n*Flipping the coin...*",
                color=CASINO_COLORS["coinflip"],
            ),
            outcome={"result": result},
        )
        await asyncio.sleep(CASINO_ANIMATION_SECONDS["coinflip"])

        if won:
            winnings = bet * 2
            winnings, pet_bonus, _ = await self._pet_adjusted_gambling_winnings(
                ctx.guild.id, ctx.author.id, bet, winnings
            )
            await asyncio.to_thread(
                self.store.add_credits, ctx.guild.id, ctx.author.id, winnings
            )
            embed = discord.Embed(
                title=f"🪙 {result}!",
                description=f"You chose: **{choice}**\n\n✅ **YOU WIN!**\n\n💰 **Won:** {winnings:,} cr\n📈 **Profit:** +{winnings - bet:,} cr"
                + (f"\nPerk Bonus: **+{pet_bonus:,} cr**" if pet_bonus > 0 else ""),
                color=CASINO_COLORS["win"],
            )
        else:
            embed = discord.Embed(
                title=f"🪙 {result}!",
                description=f"You chose: **{choice}**\n\n❌ **YOU LOSE!**\n\n💸 **Lost:** {bet:,} cr",
                color=CASINO_COLORS["lose"],
            )
        balance = await asyncio.to_thread(
            self.store.get_balance, ctx.guild.id, ctx.author.id
        )
        embed.set_footer(text=f"Balance: {balance:,} cr | Bet: {bet:,} cr")
        await _edit_casino_result(msg, embed, final_file=final_animation_file)

    @commands.command(
        name="multidice",
        aliases=["mdice", "dicebattle", "partydice"],
        help="Start a 2-4 player dice lobby. Highest roll wins the pot.",
    )
    async def multiplayer_dice_cmd(self, ctx: commands.Context, *, amount: str = ""):
        if not amount:
            return await ctx.send("Usage: `.mdice <amount>`")
        bet = await self._validate_bet(ctx, amount, "dice")
        if bet is None:
            return

        from .casino import MultiplayerDiceView

        view = MultiplayerDiceView(self, ctx, bet)
        try:
            msg = await ctx.send(
                view=view, allowed_mentions=discord.AllowedMentions.none()
            )
            view.message = msg
        except Exception:
            await asyncio.to_thread(
                self.store.add_credits, ctx.guild.id, ctx.author.id, bet
            )
            raise

    @commands.command(
        name="uno",
        aliases=["unogamble", "unobet"],
        help="Start a 2-4 player UNO betting lobby. Winner gets 4x the buy-in.",
    )
    async def uno_cmd(self, ctx: commands.Context, *, amount: str = ""):
        if not amount:
            return await ctx.send("Usage: `.uno <amount>`")
        if ctx.author.id in self._active_uno_games:
            return await ctx.send("You are already in an UNO lobby or game.")
        bet = await self._validate_bet(ctx, amount, "uno")
        if bet is None:
            return

        from .casino import UnoGameView

        view = UnoGameView(self, ctx, bet)
        self._active_uno_games[ctx.author.id] = view
        try:
            msg = await ctx.send(
                file=view.table_file(),
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            view.message = msg
        except Exception:
            self._active_uno_games.pop(ctx.author.id, None)
            await asyncio.to_thread(
                self.store.add_credits, ctx.guild.id, ctx.author.id, bet
            )
            raise

    @commands.command(name="dice", help="🎲 Roll dice against the dealer")
    async def dice_cmd(self, ctx: commands.Context, *, amount: str = ""):
        if not amount:
            return await ctx.send("Usage: `.dice <amount>`")
        bet = await self._validate_bet(ctx, amount, "dice")
        if bet is None:
            return

        player_roll = random.randint(1, 6)
        dealer_roll = random.randint(1, 6)
        luck_triggered = False
        if player_roll <= dealer_roll:
            if await self._luck_triggers(ctx.guild.id, ctx.author.id, "dice"):
                luck_triggered = True
                player_roll = random.randint(1, 6)
                dealer_roll = random.randint(1, 6)

        msg, final_animation_file = await _send_casino_animation(
            ctx,
            "dice",
            discord.Embed(
                title="🎲 Dice Roll",
                description="*Rolling against the dealer...*",
                color=CASINO_COLORS["dice"],
            ),
            outcome={"player_roll": player_roll, "dealer_roll": dealer_roll},
        )
        await asyncio.sleep(CASINO_ANIMATION_SECONDS["dice"])

        if player_roll > dealer_roll:
            winnings = bet * 2
            winnings, pet_bonus, _ = await self._pet_adjusted_gambling_winnings(
                ctx.guild.id, ctx.author.id, bet, winnings
            )
            await asyncio.to_thread(
                self.store.add_credits, ctx.guild.id, ctx.author.id, winnings
            )
            result_text = (
                f"✅ **YOU WIN!**\n\n💰 **Won:** {winnings:,} cr\n📈 **Profit:** +{winnings - bet:,} cr"
                + (f"\nPerk Bonus: **+{pet_bonus:,} cr**" if pet_bonus > 0 else "")
            )
            color = CASINO_COLORS["win"]
        elif player_roll < dealer_roll:
            result_text = f"❌ **YOU LOSE!**\n\n💸 **Lost:** {bet:,} cr"
            color = CASINO_COLORS["lose"]
        else:
            await asyncio.to_thread(
                self.store.add_credits, ctx.guild.id, ctx.author.id, bet
            )
            result_text = f"🤝 **TIE!**\n\nBet returned: **{bet:,}** cr"
            color = CASINO_COLORS["neutral"]

        balance = await asyncio.to_thread(
            self.store.get_balance, ctx.guild.id, ctx.author.id
        )
        embed = discord.Embed(title="🎲 Dice Roll", color=color)
        embed.add_field(name="👤 Your Roll", value=f"**{player_roll}**", inline=True)
        embed.add_field(name="🎩 Dealer Roll", value=f"**{dealer_roll}**", inline=True)
        embed.add_field(name="Result", value=result_text, inline=False)
        if luck_triggered:
            embed.add_field(
                name="Luck Boost",
                value="The first losing roll was rerolled.",
                inline=False,
            )
        embed.set_footer(text=f"Balance: {balance:,} cr | Bet: {bet:,} cr")
        await _edit_casino_result(msg, embed, final_file=final_animation_file)

    @commands.command(name="blackjack", aliases=["bj"], help="🃏 Play blackjack")
    async def blackjack_cmd(self, ctx: commands.Context, *, amount: str = ""):
        if not amount:
            return await ctx.send("Usage: `.blackjack <amount>`")
        bet = await self._validate_bet(ctx, amount, "blackjack")
        if bet is None:
            return

        from .casino import BlackjackView

        view = BlackjackView(self, ctx.guild.id, ctx.author.id, bet)
        if view.has_natural_blackjack():
            try:
                await view.finish_initial_hand()
            except psycopg2.Error:
                LOGGER.exception(
                    "Blackjack initial settlement database failure for guild %s user %s",
                    ctx.guild.id,
                    ctx.author.id,
                )
                await asyncio.to_thread(
                    self.store.add_credits, ctx.guild.id, ctx.author.id, bet
                )
                return await ctx.send(
                    "Database is temporarily unavailable, so blackjack was refunded. Try again in a moment."
                )
        view.render()
        msg = await ctx.send(view=view)
        view.message = msg
        if not view.game_over:
            view._save_state()

    @commands.command(name="roulette", aliases=["roul"], help="🎡 Play roulette")
    async def roulette_cmd(
        self, ctx: commands.Context, amount: str = "", *, choice: str = ""
    ):
        if not amount or not choice:
            return await ctx.send(
                "Usage: `.roulette <amount> <red|black|even|odd|low|high|1-12|13-24|25-36|0-36>`"
            )
        parsed_choice = _parse_roulette_choice(choice)
        if parsed_choice is None:
            return await ctx.send(
                "❌ Invalid roulette choice. Try `red`, `black`, `even`, `odd`, `low`, `high`, `1-12`, or a number `0-36`."
            )

        bet = await self._validate_bet(ctx, amount, "roulette")
        if bet is None:
            return

        bet_type, bet_value, payout_mult = parsed_choice

        def evaluate_spin(spin_number: int, spin_color: str) -> tuple[bool, str]:
            if bet_type == "color":
                return spin_number != 0 and spin_color == bet_value, str(bet_value).title()
            if bet_type == "parity":
                won_parity = spin_number != 0 and (
                    spin_number % 2 == 0 if bet_value == "even" else spin_number % 2 == 1
                )
                return won_parity, str(bet_value).title()
            if bet_type == "range":
                start, end, range_label = bet_value
                return start <= spin_number <= end, str(range_label)
            if bet_type == "number":
                return spin_number == bet_value, f"Number {bet_value}"
            return False, str(bet_value)

        number = random.randint(0, 36)
        color_name = _roulette_color(number)
        won, label = evaluate_spin(number, color_name)
        luck_triggered = False
        if not won:
            if await self._luck_triggers(ctx.guild.id, ctx.author.id, "roulette"):
                luck_triggered = True
                number = random.randint(0, 36)
                color_name = _roulette_color(number)
                won, label = evaluate_spin(number, color_name)

        msg, final_animation_file = await _send_casino_animation(
            ctx,
            "roulette",
            discord.Embed(
                title="🎡 Roulette",
                description=f"Bet locked: **{choice}**\n\n*Spinning the wheel...*",
                color=CASINO_COLORS["roulette"],
            ),
            outcome={"number": number, "color": color_name},
        )
        await asyncio.sleep(CASINO_ANIMATION_SECONDS["roulette"])

        if won:
            winnings = bet * payout_mult
            winnings, pet_bonus, _ = await self._pet_adjusted_gambling_winnings(
                ctx.guild.id, ctx.author.id, bet, winnings
            )
            await asyncio.to_thread(
                self.store.add_credits, ctx.guild.id, ctx.author.id, winnings
            )
            result_text = (
                f"✅ **YOU WIN!**\n\n💰 **Won:** {winnings:,} cr\n📈 **Profit:** +{winnings - bet:,} cr\n**Payout:** {payout_mult}x"
                + (f"\nPerk Bonus: **+{pet_bonus:,} cr**" if pet_bonus > 0 else "")
            )
            color = CASINO_COLORS["win"]
        else:
            result_text = f"❌ **YOU LOSE!**\n\n💸 **Lost:** {bet:,} cr"
            color = CASINO_COLORS["lose"]

        balance = await asyncio.to_thread(
            self.store.get_balance, ctx.guild.id, ctx.author.id
        )
        embed = discord.Embed(title="🎡 Roulette", color=color)
        embed.add_field(name="Your Bet", value=f"**{label}**", inline=True)
        embed.add_field(
            name="Spin", value=f"**{number}** ({color_name.title()})", inline=True
        )
        embed.add_field(name="Result", value=result_text, inline=False)
        if luck_triggered:
            embed.add_field(
                name="Luck Boost",
                value="The first losing spin was rerolled.",
                inline=False,
            )
        embed.set_footer(text=f"Balance: {balance:,} cr | Bet: {bet:,} cr")
        await _edit_casino_result(msg, embed, final_file=final_animation_file)

    @commands.command(
        name="poker", aliases=["videopoker", "vp"], help="🂡 Play video poker"
    )
    async def poker_cmd(self, ctx: commands.Context, *, amount: str = ""):
        if not amount:
            return await ctx.send("Usage: `.poker <amount>`")
        bet = await self._validate_bet(ctx, amount, "poker")
        if bet is None:
            return

        from .casino import PokerView

        deck = _create_deck()
        hand = [deck.pop() for _ in range(5)]

        view = PokerView(self, ctx.guild.id, ctx.author.id, bet, deck, hand)
        view.render()
        msg = await ctx.send(view=view)
        view.message = msg
        view._save_state()
        return

    async def _get_cooldown_reduction(self, ctx: commands.Context) -> float:
        if self.store is None:
            return 0.0
        reduction = 0.0
        if hasattr(self.store, "get_pet_cooldown_reduction"):
            reduction += await asyncio.to_thread(
                self.store.get_pet_cooldown_reduction, ctx.guild.id, ctx.author.id
            )
        task_key = {"fish": "fish", "hunt": "hunt", "mine": "mine"}.get(
            getattr(ctx.command, "name", "")
        )
        if task_key and hasattr(self.store, "task_gear_modifiers"):
            gear = await asyncio.to_thread(
                self.store.task_gear_modifiers, ctx.guild.id, ctx.author.id, task_key
            )
            reduction += float(gear.get("cooldown") or 0)
        return max(0.0, min(0.75, float(reduction)))

    async def _active_luck_rate(
        self, guild_id: int, user_id: int, context_key: str = "default"
    ) -> float:
        pet_bonus = 0.0
        if hasattr(self, "_pet_luck_bonus"):
            pet_bonus = await self._pet_luck_bonus(guild_id, user_id)
        potion_bonus = 0.0
        if context_key not in {"coinflip", "aln"} and hasattr(
            self.store, "get_effect_rate"
        ):
            potion_bonus = await asyncio.to_thread(
                self.store.get_effect_rate, guild_id, user_id, "luck"
            )
        gear_bonus = 0.0
        task_key = {"fish": "fish", "hunt": "hunt", "mine": "mine"}.get(context_key)
        if task_key and hasattr(self.store, "task_gear_modifiers"):
            gear = await asyncio.to_thread(
                self.store.task_gear_modifiers, guild_id, user_id, task_key
            )
            gear_bonus = float(gear.get("luck") or 0)
        return min(LUCK_CHANCE_CAP, max(0.0, pet_bonus + potion_bonus + gear_bonus))

    async def _luck_triggers(
        self, guild_id: int, user_id: int, context_key: str = "default"
    ) -> bool:
        rate = await self._active_luck_rate(guild_id, user_id, context_key)
        return rate > 0 and random.random() < min(LUCK_CHANCE_CAP, rate)

    async def _check_cooldown(self, ctx: commands.Context, seconds: int):
        store = self.store
        if store is None:
            await ctx.send("Economy system is not ready yet. Try again in a moment.")
            raise commands.CheckFailure("Economy system is not ready.")
        reduction = await self._get_cooldown_reduction(ctx)
        effective_seconds = max(1, int(seconds * (1.0 - reduction)))
        expires_at = await asyncio.to_thread(
            store.get_cooldown, ctx.guild.id, ctx.author.id, ctx.command.name
        )
        now = datetime.now(timezone.utc)
        if expires_at and expires_at > now:
            retry_after = (expires_at - now).total_seconds()
            raise commands.CommandOnCooldown(
                commands.Cooldown(1, effective_seconds),
                retry_after,
                commands.BucketType.user,
            )
        new_expires = now + timedelta(seconds=effective_seconds)
        await asyncio.to_thread(
            store.set_cooldown,
            ctx.guild.id,
            ctx.author.id,
            ctx.command.name,
            new_expires,
        )

    async def _reset_cooldown(self, ctx: commands.Context):
        store = self.store
        if store is None:
            return
        await asyncio.to_thread(
            store.set_cooldown,
            ctx.guild.id,
            ctx.author.id,
            ctx.command.name,
            datetime.now(timezone.utc),
        )

    async def _ensure_cooldown_ready(self, ctx: commands.Context, seconds: int):
        store = self.store
        if store is None:
            await ctx.send("Economy system is not ready yet. Try again in a moment.")
            raise commands.CheckFailure("Economy system is not ready.")
        reduction = await self._get_cooldown_reduction(ctx)
        effective_seconds = max(1, int(seconds * (1.0 - reduction)))
        expires_at = await asyncio.to_thread(
            store.get_cooldown, ctx.guild.id, ctx.author.id, ctx.command.name
        )
        now = datetime.now(timezone.utc)
        if expires_at and expires_at > now:
            retry_after = (expires_at - now).total_seconds()
            raise commands.CommandOnCooldown(
                commands.Cooldown(1, effective_seconds),
                retry_after,
                commands.BucketType.user,
            )

    async def _start_cooldown(self, ctx: commands.Context, seconds: int):
        store = self.store
        if store is None:
            return
        reduction = await self._get_cooldown_reduction(ctx)
        effective_seconds = max(1, int(seconds * (1.0 - reduction)))
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=effective_seconds)
        await asyncio.to_thread(
            store.set_cooldown,
            ctx.guild.id,
            ctx.author.id,
            ctx.command.name,
            expires_at,
        )

    @property
    def store(self):
        get_cog = getattr(self.bot, "get_cog", None)
        if get_cog is not None:
            cog = get_cog("EconomySystem")
            if cog:
                return cog.store
        system = getattr(self.bot, "economy_system", None)
        if system is not None:
            return getattr(system, "store", None)
        return None

    def create_embed(self, title, description, color=None):
        return discord.Embed(
            title=title, description=description, color=color or self.color
        )

    async def _add_credits_or_reset_cooldown(
        self,
        ctx: commands.Context,
        guild_id: int,
        user_id: int,
        amount: int,
        action: str,
    ) -> bool:
        try:
            await asyncio.to_thread(self.store.add_credits, guild_id, user_id, amount)
        except Exception:
            LOGGER.exception(
                "Failed to add %s credits for %s in %s during %s",
                amount,
                user_id,
                guild_id,
                action,
            )
            await self._reset_cooldown(ctx)
            embed = self.create_embed(
                action,
                "I could not save that reward, so your cooldown was reset. Try again in a moment.",
                color=0xED4245,
            )
            await self._send(ctx, embed)
            return False
        return True

    async def _send(self, ctx, embed):
        # Fallback to normal send if V2 is not available/working for this
        try:
            from v2_embed import apply_v2_embed_layout

            view = discord.ui.LayoutView()
            apply_v2_embed_layout(view, embed=embed)
            await ctx.send(view=view)
        except ImportError:
            await ctx.send(embed=embed)

    async def _pet_bonus_result(
        self, guild_id: int, user_id: int, amount: int, action: str
    ) -> dict:
        store = self.store
        if store is None:
            return {"base": amount, "bonus": 0, "total": amount, "rate": 0.0}
        if hasattr(store, "apply_total_bonus"):
            return await asyncio.to_thread(
                store.apply_total_bonus, guild_id, user_id, amount, action
            )
        if not hasattr(store, "apply_pet_bonus"):
            return {"base": amount, "bonus": 0, "total": amount, "rate": 0.0}
        return await asyncio.to_thread(
            store.apply_pet_bonus, guild_id, user_id, amount, action
        )

    def _pet_bonus_note(self, result: dict) -> str:
        bonus = int(result.get("bonus") or 0)
        if bonus <= 0:
            return ""
        return f" | Perk Bonus: **+{bonus:,} cr**"

    @commands.command(
        name="crime",
        help="Commit a crime for coins — risk getting caught (45 minute cooldown).",
    )
    async def crime(self, ctx: commands.Context):
        await self._check_cooldown(ctx, 2700)
        if hasattr(self.store, "get_member_security"):
            security = await asyncio.to_thread(
                self.store.get_member_security, ctx.guild.id, ctx.author.id
            )
            jail_until = security.get("jail_until")
            if jail_until and jail_until.tzinfo is None:
                jail_until = jail_until.replace(tzinfo=timezone.utc)
            if jail_until and jail_until > datetime.now(timezone.utc):
                embed = self.create_embed(
                    "🚔 Crime",
                    f"You are jailed until <t:{int(jail_until.timestamp())}:R>.",
                    color=0xED4245,
                )
                await self._send(ctx, embed)
                await self._reset_cooldown(ctx)
                return
        luck_rate = await self._active_luck_rate(ctx.guild.id, ctx.author.id, "crime")
        success_chance = min(0.75, 0.5 + luck_rate * 0.25)
        luck_helped = success_chance > 0.5
        if random.random() < success_chance:
            reward = random.randint(300, 800)
            pet_result = await self._pet_bonus_result(
                ctx.guild.id, ctx.author.id, reward, "crime"
            )
            reward = int(pet_result["total"])
            await asyncio.to_thread(
                self.store.add_credits, ctx.guild.id, ctx.author.id, reward
            )
            embed = self.create_embed(
                "🏃 Crime Successful!",
                f"You successfully pulled off the heist and got away with **{reward:,} cr**!",
                color=0x2ECC71,
            )
            if luck_helped:
                embed.description = (embed.description or "") + "\nLuck improved the getaway odds."
        else:
            fine = random.randint(200, 500)
            await asyncio.to_thread(
                self.store.remove_credits, ctx.guild.id, ctx.author.id, fine
            )
            embed = self.create_embed(
                "🚔 Busted!",
                f"The cops caught you! You had to pay a fine of **{fine:,} cr**.",
                color=0xED4245,
            )

        await self._send(ctx, embed)

    @commands.command(
        name="deposit",
        aliases=["dep"],
        help="Deposit coins from your wallet to your bank.",
    )
    async def deposit(self, ctx: commands.Context, amount: str = ""):
        if not hasattr(self.store, "transfer_to_bank"):
            return await self._send(
                ctx,
                self.create_embed(
                    "Error", "Bank system is not initialized.", color=0xED4245
                ),
            )

        if not amount:
            return await self._send(
                ctx,
                self.create_embed(
                    "🏦 Deposit",
                    "You need to specify an amount to deposit!",
                    color=0xED4245,
                ),
            )

        amount_key = amount.strip().lower()
        if amount_key in ["all", "max"]:
            amount = await asyncio.to_thread(
                self.store.get_balance, ctx.guild.id, ctx.author.id
            )
        else:
            amount = _investment_amount(amount)
            if amount is None:
                return await self._send(
                    ctx,
                    self.create_embed(
                        "🏦 Deposit", "Please provide a valid number.", color=0xED4245
                    ),
                )

        if amount <= 0:
            return await self._send(
                ctx,
                self.create_embed(
                    "🏦 Deposit",
                    "You can't deposit zero or negative amounts.",
                    color=0xED4245,
                ),
            )

        success = await asyncio.to_thread(
            self.store.transfer_to_bank, ctx.guild.id, ctx.author.id, amount
        )
        if success:
            embed = self.create_embed(
                "🏦 Deposit",
                f"Successfully deposited **{amount:,} cr** to your bank.",
                color=0x2ECC71,
            )
        else:
            embed = self.create_embed(
                "🏦 Deposit",
                "You don't have enough coins in your wallet!",
                color=0xED4245,
            )
        await self._send(ctx, embed)

    @commands.command(
        name="withdraw",
        aliases=["with"],
        help="Withdraw coins from your bank to your wallet.",
    )
    async def withdraw(self, ctx: commands.Context, amount: str = ""):
        if not hasattr(self.store, "withdraw_from_bank"):
            return await self._send(
                ctx,
                self.create_embed(
                    "Error", "Bank system is not initialized.", color=0xED4245
                ),
            )

        if not amount:
            return await self._send(
                ctx,
                self.create_embed(
                    "🏦 Withdraw",
                    "You need to specify an amount to withdraw!",
                    color=0xED4245,
                ),
            )

        amount_key = amount.strip().lower()
        if amount_key in ["all", "max"]:
            _, amount = await asyncio.to_thread(
                self.store.get_balances, ctx.guild.id, ctx.author.id
            )
        else:
            amount = _investment_amount(amount)
            if amount is None:
                return await self._send(
                    ctx,
                    self.create_embed(
                        "🏦 Withdraw", "Please provide a valid number.", color=0xED4245
                    ),
                )

        if amount <= 0:
            return await self._send(
                ctx,
                self.create_embed(
                    "🏦 Withdraw",
                    "You can't withdraw zero or negative amounts.",
                    color=0xED4245,
                ),
            )

        success = await asyncio.to_thread(
            self.store.withdraw_from_bank, ctx.guild.id, ctx.author.id, amount
        )
        if success:
            embed = self.create_embed(
                "🏦 Withdraw",
                f"Successfully withdrew **{amount:,} cr** from your bank.",
                color=0x2ECC71,
            )
        else:
            embed = self.create_embed(
                "🏦 Withdraw",
                "You don't have enough coins in your bank!",
                color=0xED4245,
            )
        await self._send(ctx, embed)

    @commands.command(
        name="cd",
        aliases=["cooldowns"],
        help="Check all your economy command cooldowns.",
    )
    async def cd(self, ctx: commands.Context):
        cmds = [
            "daily",
            "work",
            "beg",
            "fish",
            "hunt",
            "mine",
            "scratch",
            "rob",
            "crime",
            "invest",
        ]
        desc = ""

        now = datetime.now(timezone.utc)
        for cmd_name in cmds:
            expires_at = (
                await asyncio.to_thread(
                    self.store.get_cooldown, ctx.guild.id, ctx.author.id, cmd_name
                )
                if self.store
                else None
            )
            if expires_at and expires_at > now:
                ready_time = int(expires_at.timestamp())
                desc += f"**{cmd_name.capitalize()}:** ⏳ <t:{ready_time}:R>\n"
            else:
                desc += f"**{cmd_name.capitalize()}:** ✅ Ready\n"

        embed = self.create_embed("⏱️ Economy Cooldowns", desc)
        await self._send(ctx, embed)

    @commands.command(
        name="ecohelp", help="Show all economy commands and their cooldowns."
    )
    async def ecohelp(self, ctx: commands.Context):
        embed = self.create_embed(
            "Soul Economy Help",
            "Use the `.` prefix for economy commands. Casino, pets, black market, and heists include interactive controls.",
            color=0xF1C40F,
        )
        embed.add_field(
            name="Core Earnings",
            value=(
                "`.daily` - claim daily credits\n"
                "`.work` - work a job\n"
                "`.beg` - chance at small credits\n"
                "`.fish` - fishing payout\n"
                "`.hunt` - hunting payout\n"
                "`.mine` - mining payout\n"
                "`.dailyspin` - daily prize wheel\n"
                "`.lottery enter [tickets]` - server lottery, 5,000 cr per ticket\n"
                "`.scratch` - scratch card gamble\n"
                "`.crime` - risky crime payout"
            ),
            inline=False,
        )
        embed.add_field(
            name="Wallet & Banking",
            value=(
                "`.bal [member]` - wallet and bank card\n"
                "`.deposit <amount>` - move wallet credits to bank\n"
                "`.withdraw <amount>` - move bank credits to wallet\n"
                "`.transfer @user <amount>` - pay another member\n"
                "`.invest <amount>` - 20-minute investment\n"
                "`.investment` - active/recent investments\n"
                "`.prestige` / `.prestige confirm` - view or buy permanent payout prestige\n"
                "`.perks [member]` - view active pet, prestige, booster, and role bonuses\n"
                "`.ecolb` - richest members leaderboard\n"
                "`.cd` - economy cooldowns"
            ),
            inline=False,
        )
        embed.add_field(
            name="Shop & Pets",
            value=(
                "`.shop` / `.buy <role>` - role shop with payout perks\n"
                "`.gearshop` / `.buygear <item>` - task gear for fishing, hunting, mining\n"
                "`.utilityshop` - potions and shop reset buttons\n"
                "`.buypotion <item>` / `.usepotion <item>` - luck and defense potions\n"
                "`.resetshop` / `.resetcooldown <command>` - use reset buttons\n"
                "`.petshop` / `.buypet <line>` - buy pets\n"
                "`.mypets` - pets and materials\n"
                "`.pet collect` - passive pet income\n"
                "`.pet feed #id` / `.pet play #id` - pet happiness\n"
                "`.pet evolve #id` - evolve a pet\n"
                "`.pet cores` - fusion core shop\n"
                "`.pet fuse #id + #id` - fuse pets\n"
                "`.pet leaderboard` - pet income leaderboard"
            ),
            inline=False,
        )
        embed.add_field(
            name="Boosters & Bonuses",
            value=(
                "`.booster` - admin booster perks panel\n"
                "Server boosters get payout bonuses automatically\n"
                "Prestige, boosters, shop roles, and pets stack with payout caps\n"
                "Shop-role perks apply after buying roles from `.shop`"
            ),
            inline=False,
        )
        embed.add_field(
            name="Casino",
            value=(
                "`.casino` - casino menu and limits\n"
                "`.slots <amount>` - reel spin payouts\n"
                "`.coinflip <amount> <heads/tails>` - 50/50 payout\n"
                "`.dice <amount>` - solo dice vs dealer\n"
                "`.mdice <amount>` - 2-4 player dice lobby\n"
                "`.colorduel @user <amount>` - color duel, first to 4 wins the pot\n"
                "`.uno <amount>` - 2-4 player UNO lobby, winner gets 4x\n"
                "`.aln` - All or Nothing, 10% chance, no luck bonus, 7 day cooldown\n"
                "`.roulette <amount> <choice>` - 250 to 300,000 cr\n"
                "`.blackjack <amount>` - 100 to 75,000 cr\n"
                "`.mines <amount> [easy|medium|hard]` - 250 to 100,000 cr\n"
                "`.poker <amount>` - 500 to 75,000 cr"
            ),
            inline=False,
        )
        embed.add_field(
            name="Robbery & Heists",
            value=(
                "`.blackmarket` - buy robbery tools and locks\n"
                "`.tools` - tools, locks, wanted, jail, and robbery cooldowns\n"
                "`.rob @user` - 30m cooldown, target needs 10,000+ wallet cr\n"
                "`.jewelryheist` - 3h cooldown, needs 2 Lockpicks\n"
                "`.bankrob` - 8h cooldown, needs 3 Lockpicks + 1 Vault Drill\n"
                "`.teamrob` - 12h cooldown, 2 Lockpicks each + leader Vault Drill"
            ),
            inline=False,
        )
        embed.set_footer(
            text="Admins can use .eco for balance, stock, prestige, booster, role perk, pet, and item controls."
        )
        await self._send(ctx, embed)


