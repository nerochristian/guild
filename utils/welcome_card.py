from __future__ import annotations

import io
import logging
import os
import urllib.request
from dataclasses import dataclass
from typing import Optional

import aiohttp
import discord

log = logging.getLogger(__name__)

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from config import Config

# ---------------------------------------------------------------------------
# Font loading
# ---------------------------------------------------------------------------

_FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")

_FONT_URLS: dict[str, list[str]] = {
    "DejaVuSans.ttf": [
        "https://github.com/dejavu-fonts/dejavu-fonts/raw/master/ttf/DejaVuSans.ttf",
        "https://cdn.jsdelivr.net/gh/dejavu-fonts/dejavu-fonts/ttf/DejaVuSans.ttf",
    ],
    "DejaVuSans-Bold.ttf": [
        "https://github.com/dejavu-fonts/dejavu-fonts/raw/master/ttf/DejaVuSans-Bold.ttf",
        "https://cdn.jsdelivr.net/gh/dejavu-fonts/dejavu-fonts/ttf/DejaVuSans-Bold.ttf",
    ],
}

_SYSTEM_DIRS = [
    _FONT_DIR,
    "/usr/share/fonts/truetype/dejavu",
    "/usr/share/fonts/truetype/liberation",
    "/usr/share/fonts/truetype/freefont",
    "/usr/share/fonts/truetype/msttcorefonts",
    "/usr/share/fonts",
    "/usr/local/share/fonts",
    "/System/Library/Fonts",
    "/Library/Fonts",
    os.path.join(os.environ.get("WINDIR", "C:/Windows"), "Fonts"),
    ".",
]

_FONT_ALIASES = {
    "DejaVuSans.ttf":      ["DejaVuSans.ttf", "arial.ttf", "segoeui.ttf", "LiberationSans-Regular.ttf", "FreeSans.ttf"],
    "DejaVuSans-Bold.ttf": ["DejaVuSans-Bold.ttf", "arialbd.ttf", "segoeuib.ttf", "LiberationSans-Bold.ttf", "FreeSansBold.ttf"],
}

_font_cache: dict[tuple[str, int], ImageFont.FreeTypeFont | ImageFont.ImageFont] = {}


def _ensure_font(filename: str) -> Optional[str]:
    for alias in _FONT_ALIASES.get(filename, [filename]):
        for d in _SYSTEM_DIRS:
            p = os.path.join(d, alias)
            if os.path.isfile(p):
                return p
    os.makedirs(_FONT_DIR, exist_ok=True)
    dest = os.path.join(_FONT_DIR, filename)
    if os.path.isfile(dest) and os.path.getsize(dest) > 1000:
        return dest
    for url in _FONT_URLS.get(filename, []):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = resp.read()
            if len(data) > 1000:
                with open(dest, "wb") as f:
                    f.write(data)
                return dest
        except Exception:
            continue
    return None


def load_font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    filename = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    key = (filename, size)
    if key in _font_cache:
        return _font_cache[key]
    path = _ensure_font(filename)
    if path:
        try:
            font = ImageFont.truetype(path, size=size)
            _font_cache[key] = font
            return font
        except Exception:
            pass
    fallback = ImageFont.load_default()
    _font_cache[key] = fallback
    return fallback


def _prefetch_fonts() -> None:
    for filename in _FONT_URLS:
        _ensure_font(filename)


_prefetch_fonts()


# ---------------------------------------------------------------------------
# Discord badge icons
# Official badge images downloaded from Discord's CDN at startup.
# Mapping: public_flags attribute → (cache filename, CDN url)
# ---------------------------------------------------------------------------

_BADGE_ICON_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "badge_icons")

# flag attr → (local filename, primary CDN url, fallback CDN url)
_DISCORD_BADGE_ICONS: dict[str, tuple[str, str, str]] = {
    "staff": (
        "staff.png",
        "https://cdn.discordapp.com/badge-icons/5e74e9b61934fc1f67c65515d1f7e60d.png",
        "https://discord.com/assets/5e74e9b61934fc1f67c65515d1f7e60d.png",
    ),
    "partner": (
        "partner.png",
        "https://cdn.discordapp.com/badge-icons/3f9748e53446a137a052f3454e2de41e.png",
        "https://discord.com/assets/3f9748e53446a137a052f3454e2de41e.png",
    ),
    "hypesquad": (
        "hypesquad.png",
        "https://cdn.discordapp.com/badge-icons/bf01d1073931f921909045f3a39fd264.png",
        "https://discord.com/assets/bf01d1073931f921909045f3a39fd264.png",
    ),
    "hypesquad_bravery": (
        "hypesquad_bravery.png",
        "https://cdn.discordapp.com/badge-icons/8a88d63823d8a71cd5e390baa45afa0d.png",
        "https://discord.com/assets/8a88d63823d8a71cd5e390baa45afa0d.png",
    ),
    "hypesquad_brilliance": (
        "hypesquad_brilliance.png",
        "https://cdn.discordapp.com/badge-icons/011940fd013082a75b1fde53a8500bbf.png",
        "https://discord.com/assets/011940fd013082a75b1fde53a8500bbf.png",
    ),
    "hypesquad_balance": (
        "hypesquad_balance.png",
        "https://cdn.discordapp.com/badge-icons/3aa41de486fa12454c3761e8e223442e.png",
        "https://discord.com/assets/3aa41de486fa12454c3761e8e223442e.png",
    ),
    "bug_hunter": (
        "bug_hunter.png",
        "https://cdn.discordapp.com/badge-icons/2717692e7220792d9d1c3cc5b2f50f10.png",
        "https://discord.com/assets/2717692e7220792d9d1c3cc5b2f50f10.png",
    ),
    "bug_hunter_level_1": (
        "bug_hunter.png",
        "https://cdn.discordapp.com/badge-icons/2717692e7220792d9d1c3cc5b2f50f10.png",
        "https://discord.com/assets/2717692e7220792d9d1c3cc5b2f50f10.png",
    ),
    "bug_hunter_level_2": (
        "bug_hunter_2.png",
        "https://cdn.discordapp.com/badge-icons/848f79194d4be5ff5f81505cbd0ce1e6.png",
        "https://discord.com/assets/848f79194d4be5ff5f81505cbd0ce1e6.png",
    ),
    "early_supporter": (
        "early_supporter.png",
        "https://cdn.discordapp.com/badge-icons/7060786766c9c840eb3019e725d2b358.png",
        "https://discord.com/assets/7060786766c9c840eb3019e725d2b358.png",
    ),
    "early_verified_bot_developer": (
        "verified_dev.png",
        "https://cdn.discordapp.com/badge-icons/6df5892e0f35b051d8b61d8330539633.png",
        "https://discord.com/assets/6df5892e0f35b051d8b61d8330539633.png",
    ),
    "verified_bot_developer": (
        "verified_dev.png",
        "https://cdn.discordapp.com/badge-icons/6df5892e0f35b051d8b61d8330539633.png",
        "https://discord.com/assets/6df5892e0f35b051d8b61d8330539633.png",
    ),
    "discord_certified_moderator": (
        "certified_mod.png",
        "https://cdn.discordapp.com/badge-icons/fee1624003e2fee35cb398e125dc479b.png",
        "https://discord.com/assets/fee1624003e2fee35cb398e125dc479b.png",
    ),
    "active_developer": (
        "active_developer.png",
        "https://cdn.discordapp.com/badge-icons/6bdc42827a38498929a4920da12695d9.png",
        "https://discord.com/assets/6bdc42827a38498929a4920da12695d9.png",
    ),
}

# Loaded PIL images, keyed by flag attr. Populated at startup.
_badge_icon_cache: dict[str, Image.Image] = {}


def _download_badge_icon(flag_attr: str, filename: str, urls: tuple[str, str]) -> Optional[str]:
    """Download a badge icon to the local cache dir. Returns local path or None."""
    os.makedirs(_BADGE_ICON_DIR, exist_ok=True)
    dest = os.path.join(_BADGE_ICON_DIR, filename)
    if os.path.isfile(dest) and os.path.getsize(dest) > 100:
        return dest
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 DiscordBot"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = resp.read()
            if len(data) > 100:
                with open(dest, "wb") as f:
                    f.write(data)
                log.debug("Downloaded badge icon %s from %s", filename, url)
                return dest
        except Exception as exc:
            log.debug("Failed to download badge icon %s from %s: %s", filename, url, exc)
    return None


def _prefetch_badge_icons() -> None:
    """Download all known Discord badge icons at startup."""
    seen_files: set[str] = set()
    for flag_attr, (filename, primary, fallback) in _DISCORD_BADGE_ICONS.items():
        if filename in seen_files:
            # Reuse already-loaded image for duplicate filenames (e.g. bug_hunter / bug_hunter_level_1)
            src_attr = next(
                (a for a, (f, *_) in _DISCORD_BADGE_ICONS.items() if f == filename and a in _badge_icon_cache),
                None,
            )
            if src_attr:
                _badge_icon_cache[flag_attr] = _badge_icon_cache[src_attr]
            continue
        seen_files.add(filename)
        path = _download_badge_icon(flag_attr, filename, (primary, fallback))
        if path:
            try:
                img = Image.open(path).convert("RGBA")
                _badge_icon_cache[flag_attr] = img
                # Also populate duplicate-filename attrs right away
                for other_attr, (other_file, *_) in _DISCORD_BADGE_ICONS.items():
                    if other_file == filename and other_attr not in _badge_icon_cache:
                        _badge_icon_cache[other_attr] = img
            except Exception as exc:
                log.warning("Could not load badge icon %s: %s", path, exc)


_prefetch_badge_icons()


# ---------------------------------------------------------------------------
# Drawing helpers
# ---------------------------------------------------------------------------

def _int_to_rgb(c: int) -> tuple[int, int, int]:
    return ((c >> 16) & 0xFF, (c >> 8) & 0xFF, c & 0xFF)


def _cover_resize(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    tw, th = size
    sw, sh = img.size
    if sw <= 0 or sh <= 0:
        return Image.new("RGBA", size, (0, 0, 0, 255))
    scale = max(tw / sw, th / sh)
    rw, rh = max(1, int(sw * scale)), max(1, int(sh * scale))
    resized = img.resize((rw, rh), Image.Resampling.LANCZOS)
    l, t = (rw - tw) // 2, (rh - th) // 2
    return resized.crop((l, t, l + tw, t + th))


def _circle_crop(img: Image.Image, size: int) -> Image.Image:
    img = img.resize((size, size), Image.Resampling.LANCZOS).convert("RGBA")
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img, mask=mask)
    return out


def _get_textbbox(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> tuple[int, int, int, int]:
    """Safe textbbox wrapper."""
    try:
        return draw.textbbox((0, 0), text, font=font)
    except AttributeError:
        try:
            tw, th = draw.textsize(text, font=font)  # type: ignore[attr-defined]
            return (0, 0, tw, th)
        except Exception:
            return (0, 0, len(text) * 8, 16)


def _text_fit(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_w: int) -> str:
    def w(s: str) -> int:
        bb = _get_textbbox(draw, s, font)
        return bb[2] - bb[0]
    if w(text) <= max_w:
        return text
    lo, hi, best = 0, len(text), "..."
    while lo <= hi:
        mid = (lo + hi) // 2
        cand = text[:mid].rstrip() + "..."
        if w(cand) <= max_w:
            best = cand
            lo = mid + 1
        else:
            hi = mid - 1
    return best


async def _fetch(session: aiohttp.ClientSession, url: str) -> Optional[Image.Image]:
    try:
        async with session.get(url) as r:
            if r.status != 200:
                return None
            return Image.open(io.BytesIO(await r.read())).convert("RGBA")
    except Exception:
        return None


def _asset_url(asset: object, *, size: int = 512, fmt: str = "png") -> Optional[str]:
    if asset is None:
        return None
    try:
        return asset.replace(size=size, static_format=fmt).url  # type: ignore
    except Exception:
        try:
            return str(asset.url)  # type: ignore
        except Exception:
            return None


# ---------------------------------------------------------------------------
# Badge helpers
# ---------------------------------------------------------------------------

# Ordered list of flag attributes to check (highest priority first).
# Only attrs present in _DISCORD_BADGE_ICONS will render — as real images.
_FLAG_BADGE_ATTRS: list[str] = [
    "staff",
    "partner",
    "hypesquad_bravery",
    "hypesquad_brilliance",
    "hypesquad_balance",
    "hypesquad",
    "bug_hunter_level_2",
    "bug_hunter_level_1",
    "bug_hunter",
    "early_supporter",
    "early_verified_bot_developer",
    "verified_bot_developer",
    "discord_certified_moderator",
    "active_developer",
]


@dataclass
class _Badge:
    label: Optional[str] = None
    icon:  Optional[Image.Image] = None


def _parse_username(user: discord.abc.User) -> str:
    disc = getattr(user, "discriminator", "0")
    return f"{user.name}#{disc}" if disc and disc != "0" else user.name


def _top_role_color(member: discord.Member) -> Optional[tuple[int, int, int]]:
    roles = sorted(
        getattr(member, "roles", []) or [],
        key=lambda r: getattr(r, "position", 0), reverse=True,
    )
    guild_id = getattr(getattr(member, "guild", None), "id", None)
    for role in roles:
        if guild_id and getattr(role, "id", None) == guild_id:
            continue
        val = getattr(getattr(role, "color", None), "value", 0)
        if val:
            return _int_to_rgb(val)
    return None


def _collect_flag_badges(user: discord.abc.User) -> list["_Badge"]:
    """Return _Badge objects (with real Discord icon images) for all active profile flags."""
    result: list[_Badge] = []
    seen_icons: set[str] = set()   # deduplicate by filename
    flags = getattr(user, "public_flags", None) or getattr(user, "flags", None)
    if not flags:
        return result
    for attr in _FLAG_BADGE_ATTRS:
        try:
            if not getattr(flags, attr, False):
                continue
        except Exception:
            continue
        # Use real icon if available
        icon_img = _badge_icon_cache.get(attr)
        if icon_img is not None:
            # Deduplicate by canonical filename so bug_hunter/bug_hunter_level_1 don't both appear
            fname = _DISCORD_BADGE_ICONS.get(attr, ("",))[0]
            if fname in seen_icons:
                continue
            seen_icons.add(fname)
            result.append(_Badge(icon=icon_img.copy()))
        # If icon failed to download we silently skip — no text fallback for profile flags
    return result


def _role_badge_urls(member: discord.Member, limit: int = 6) -> list[str]:
    roles = sorted(
        getattr(member, "roles", []) or [],
        key=lambda r: getattr(r, "position", 0), reverse=True,
    )
    guild_id = getattr(getattr(member, "guild", None), "id", None)
    urls: list[str] = []
    for role in roles:
        if guild_id and getattr(role, "id", None) == guild_id:
            continue
        icon = getattr(role, "display_icon", None)
        if icon is None:
            continue
        url = _asset_url(icon, size=64) or ""
        if url:
            urls.append(url)
        if len(urls) >= limit:
            break
    return urls


def _primary_guild_badge_url(user: discord.abc.User) -> Optional[str]:
    try:
        pg = getattr(user, "primary_guild", None)
        return _asset_url(getattr(pg, "badge", None), size=64) if pg else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Per-guild welcome text
# ---------------------------------------------------------------------------

GUILD_WELCOME_LABELS: dict[int, str] = {
    # guild_id: "Custom welcome text",
}
DEFAULT_WELCOME_LABEL = "WELCOME!"


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WelcomeCardOptions:
    width: int        = 800
    height: int       = 400
    radius: int       = 0
    avatar_size: int  = 180
    margin: int       = 0
    accent_color: int = getattr(Config, "EMBED_ACCENT_COLOR", Config.COLOR_EMBED)
    use_role_color: bool      = False
    welcome_label: str        = ""
    role_badge_fallback: bool = False
    custom_bg_url: Optional[str] = None


# ---------------------------------------------------------------------------
# Card builder
# ---------------------------------------------------------------------------

async def build_welcome_card_png(
    bot: discord.Client,
    member: discord.Member,
    *,
    options: WelcomeCardOptions = WelcomeCardOptions(),
) -> bytes:
    try:
        return await _build_welcome_card_png_inner(bot, member, options=options)
    except Exception as exc:
        log.exception("welcome_card build failed for %s: %s", getattr(member, "id", "?"), exc)
        raise


async def _build_welcome_card_png_inner(
    bot: discord.Client,
    member: discord.Member,
    *,
    options: WelcomeCardOptions = WelcomeCardOptions(),
) -> bytes:
    import asyncio

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:

        # Full user object
        try:
            full_user: discord.abc.User = await bot.fetch_user(member.id)
        except Exception:
            full_user = member  # type: ignore

        # Avatar
        avatar_img = await _fetch(session, _asset_url(member.display_avatar, size=512) or "")

        # Background: custom, or user banner, or blurred avatar
        bg_img: Optional[Image.Image] = None
        if options.custom_bg_url:
            bg_img = await _fetch(session, options.custom_bg_url)
            
        if bg_img is None:
            banner_url = _asset_url(getattr(full_user, "banner", None), size=1024)
            if banner_url:
                bg_img = await _fetch(session, banner_url)
        if bg_img is None and avatar_img is not None:
            bg_img = avatar_img.copy()

        # Avatar decoration
        deco_url = _asset_url(getattr(full_user, "avatar_decoration", None), size=256)
        deco_img = await _fetch(session, deco_url) if deco_url else None

        # ── Badges ──────────────────────────────────────────────────────────
        badges: list[_Badge] = []

        # 1. Clan / primary guild badge
        pgb_url = _primary_guild_badge_url(full_user)
        if pgb_url:
            pgb = await _fetch(session, pgb_url)
            if pgb:
                badges.append(_Badge(icon=pgb))

        # 2. Discord flag badges (real images)
        for badge in _collect_flag_badges(full_user):
            badges.append(badge)
            if len(badges) >= 6:
                break

        # 3. Role icon badges
        if options.role_badge_fallback and len(badges) < 6:
            for url in _role_badge_urls(member, limit=6 - len(badges)):
                img = await _fetch(session, url)
                if img:
                    badges.append(_Badge(icon=img))
                if len(badges) >= 6:
                    break

    badges = badges[:6]

    # ── Accent colour ─────────────────────────────────────────────────────
    if options.use_role_color:
        accent = _top_role_color(member) or _int_to_rgb(options.accent_color)
    else:
        accent = _int_to_rgb(options.accent_color)

    # ── Guild / pill text ──────────────────────────────────────────────────
    guild    = getattr(member, "guild", None)
    guild_id = getattr(guild, "id", None)

    if options.welcome_label:
        pill_text = options.welcome_label
    elif guild_id and guild_id in GUILD_WELCOME_LABELS:
        pill_text = GUILD_WELCOME_LABELS[guild_id]
    else:
        pill_text = DEFAULT_WELCOME_LABEL

    display_name = getattr(member, "display_name", member.name) or member.name
    username     = _parse_username(full_user)  # type: ignore

    return await asyncio.to_thread(
        _render_welcome_card_sync,
        bg_img, avatar_img, deco_img, badges, accent, pill_text, display_name, username, options
    )

def _render_welcome_card_sync(
    bg_img: Optional[Image.Image],
    avatar_img: Optional[Image.Image],
    deco_img: Optional[Image.Image],
    badges: list[_Badge],
    accent: tuple[int, int, int],
    pill_text: str,
    display_name: str,
    username: str,
    options: WelcomeCardOptions
) -> bytes:
    W, H = options.width, options.height
    
    # ── Background ─────────────────────────────────────────────────────────
    if bg_img is None:
        bg_img = Image.new("RGBA", (W, H), (18, 18, 18, 255))
        
    card = _cover_resize(bg_img, (W, H)).convert("RGBA")
    
    # Very subtle overlay if background is too bright, otherwise leave it mostly untouched
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 40))
    card = Image.alpha_composite(card, overlay)

    draw = ImageDraw.Draw(card)

    # ── Avatar ─────────────────────────────────────────────────────────────
    AV = options.avatar_size
    av_x = (W - AV) // 2
    av_y = (H - AV) // 2 - 40
    RING = 6

    # Draw solid white border
    draw.ellipse(
        (av_x - RING, av_y - RING, av_x + AV + RING, av_y + AV + RING),
        fill=(255, 255, 255, 255)
    )

    if avatar_img is not None:
        av = _circle_crop(avatar_img, AV)
        card.alpha_composite(av, (av_x, av_y))

    # ── Fonts ──────────────────────────────────────────────────────────────
    font_welcome = load_font(76, bold=True)
    font_user    = load_font(34, bold=True)
    
    draw = ImageDraw.Draw(card)
    
    display_name = (username.upper() if username else display_name.upper())

    # ── Text WELCOME ───────────────────────────────────────────────────────
    w_bb = _get_textbbox(draw, "WELCOME", font_welcome)
    w_w, w_h = w_bb[2] - w_bb[0], w_bb[3] - w_bb[1]
    
    wx = (W - w_w) // 2
    wy = av_y + AV - (w_h // 2) + 10
    
    # Subtle drop shadow
    draw.text((wx + 3, wy + 3), "WELCOME", font=font_welcome, fill=(0, 0, 0, 200))
    # Main text
    draw.text((wx, wy), "WELCOME", font=font_welcome, fill=(255, 255, 255, 255))
    
    # ── Text Username ──────────────────────────────────────────────────────
    display_name = _text_fit(draw, display_name, font_user, W - 40)
    u_bb = _get_textbbox(draw, display_name, font_user)
    u_w, u_h = u_bb[2] - u_bb[0], u_bb[3] - u_bb[1]
    
    ux = (W - u_w) // 2
    uy = wy + w_h + 10
    
    draw.text((ux + 2, uy + 2), display_name, font=font_user, fill=(0, 0, 0, 200))
    draw.text((ux, uy), display_name, font=font_user, fill=(255, 255, 255, 255))

    # ── Export ─────────────────────────────────────────────────────────────
    out = io.BytesIO()
    card.save(out, format="PNG", optimize=True)
    return out.getvalue()


async def build_welcome_card_file(
    bot: discord.Client,
    member: discord.Member,
    *,
    filename: str = "welcome.png",
    options: WelcomeCardOptions = WelcomeCardOptions(),
) -> discord.File:
    png = await build_welcome_card_png(bot, member, options=options)
    return discord.File(io.BytesIO(png), filename=filename)