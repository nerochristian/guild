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
    width: int        = 940
    height: int       = 300
    radius: int       = 20
    avatar_size: int  = 200
    margin: int       = 30
    accent_color: int = getattr(Config, "EMBED_ACCENT_COLOR", Config.COLOR_EMBED)
    use_role_color: bool      = True
    welcome_label: str        = ""
    role_badge_fallback: bool = True


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

        # Background: user banner if available, else blurred avatar
        bg_img: Optional[Image.Image] = None
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
    # ── Fonts ──────────────────────────────────────────────────────────────
    font_name  = load_font(72, bold=True)
    font_user  = load_font(34, bold=False)
    font_pill  = load_font(22, bold=True)
    font_badge = load_font(13, bold=True)

    # ── Canvas ─────────────────────────────────────────────────────────────
    W, H, R, M = options.width, options.height, options.radius, options.margin

    # Background
    if bg_img is None:
        bg_img = Image.new("RGBA", (W, H), (18, 18, 18, 255))

    bg = _cover_resize(bg_img, (W, H)).convert("RGBA")
    bg = bg.filter(ImageFilter.GaussianBlur(radius=14))
    # Dark overlay for readability
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 175))
    bg = Image.alpha_composite(bg, overlay)

    # Rounded card mask
    card_mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(card_mask).rounded_rectangle((0, 0, W, H), radius=R, fill=255)
    card = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    card.paste(bg, mask=card_mask)

    draw = ImageDraw.Draw(card)

    # Subtle card border using accent color
    draw.rounded_rectangle(
        (2, 2, W - 2, H - 2),
        radius=R,
        outline=(*accent, 160),
        width=3,
    )

    # ── Avatar ─────────────────────────────────────────────────────────────
    AV    = options.avatar_size
    RING  = 8        # ring thickness
    GLOW  = 16       # extra glow blur radius
    total = AV + RING * 2
    ax    = M
    ay    = (H - total) // 2

    # Draw glowing ring behind avatar
    # 1. Soft outer glow layer
    glow_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    glow_draw  = ImageDraw.Draw(glow_layer)
    glow_draw.ellipse(
        (ax - GLOW, ay - GLOW, ax + total + GLOW, ay + total + GLOW),
        fill=(*accent, 90),
    )
    glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(radius=GLOW))
    card.alpha_composite(glow_layer)

    # 2. Solid ring
    draw = ImageDraw.Draw(card)
    draw.ellipse(
        (ax, ay, ax + total, ay + total),
        fill=(*accent, 255),
    )

    # 3. Inner dark background circle (so ring has defined edge)
    pad = RING
    draw.ellipse(
        (ax + pad, ay + pad, ax + total - pad, ay + total - pad),
        fill=(15, 15, 15, 200),
    )

    # 4. Avatar image
    if avatar_img is not None:
        av = _circle_crop(avatar_img, AV)
        card.alpha_composite(av, (ax + RING, ay + RING))

    # 5. Avatar decoration overlay
    if deco_img is not None:
        dsz  = total + 20
        deco = deco_img.resize((dsz, dsz), Image.Resampling.LANCZOS)
        card.alpha_composite(deco, (ax + (total - dsz) // 2, ay + (total - dsz) // 2))

    # ── Text ───────────────────────────────────────────────────────────────
    tx     = ax + total + 28
    max_tw = W - tx - M - 8

    draw = ImageDraw.Draw(card)
    display_name = _text_fit(draw, display_name, font_name, max_tw)
    username     = _text_fit(draw, username,     font_user, max_tw)

    # Vertically center text block in the card
    dn_bb   = _get_textbbox(draw, display_name, font_name)
    dn_h    = dn_bb[3] - dn_bb[1]
    un_bb   = _get_textbbox(draw, username, font_user)
    un_h    = un_bb[3] - un_bb[1]
    gap     = 10
    block_h = dn_h + gap + un_h
    name_y  = (H - block_h) // 2
    user_y  = name_y + dn_h + gap

    # Drop shadow on name
    draw.text((tx + 2, name_y + 3), display_name, font=font_name, fill=(0, 0, 0, 180))
    # Main name in white
    draw.text((tx,     name_y),     display_name, font=font_name, fill=(255, 255, 255, 250))

    # Username in muted white
    draw.text((tx + 1, user_y + 2), username, font=font_user, fill=(0, 0, 0, 130))
    draw.text((tx,     user_y),     username, font=font_user, fill=(190, 190, 190, 220))

    # ── WELCOME pill (bottom-right) ────────────────────────────────────────
    pbb    = _get_textbbox(draw, pill_text, font_pill)
    pw, ph = pbb[2] - pbb[0], pbb[3] - pbb[1]
    ppx, ppy = 18, 9
    pill_w = pw + ppx * 2
    pill_h = ph + ppy * 2
    px     = W - M - pill_w
    py     = H - M - pill_h

    # Pill background
    draw.rounded_rectangle(
        (px, py, px + pill_w, py + pill_h),
        radius=12,
        fill=(12, 12, 12, 160),
        outline=(*accent, 220),
        width=2,
    )
    draw.text(
        (px + ppx - pbb[0], py + ppy - pbb[1]),
        pill_text,
        font=font_pill,
        fill=(255, 255, 255, 245),
    )

    # ── Badges (top-right, flowing left) ──────────────────────────────────
    BSIZE  = 32          # chip height & width for icon-only badges
    BGAP   = 6
    right  = W - M + 2
    badge_y = 12

    for badge in badges:
        bw = BSIZE

        # All profile-flag badges are now images; text-label badges only appear
        # for role chips (if any were added as _Badge(label=…) elsewhere).
        if badge.label:
            lbb = _get_textbbox(draw, badge.label, font_badge)
            bw  = max(BSIZE, lbb[2] - lbb[0] + 16)

        bx = right - bw

        # Chip background
        draw.rounded_rectangle(
            (bx, badge_y, bx + bw, badge_y + BSIZE),
            radius=9,
            fill=(10, 10, 10, 185),
            outline=(*accent, 215),
            width=2,
        )

        if badge.icon is not None:
            pad = 4
            isz = BSIZE - pad * 2
            # Scale icon to fit, preserving transparency (no circle crop for official badges)
            icon = badge.icon.resize((isz, isz), Image.Resampling.LANCZOS)
            card.alpha_composite(icon, (bx + pad, badge_y + pad))
            draw = ImageDraw.Draw(card)
        elif badge.label:
            lbb = _get_textbbox(draw, badge.label, font_badge)
            lw, lh = lbb[2] - lbb[0], lbb[3] - lbb[1]
            draw.text(
                (bx + (bw - lw) // 2 - lbb[0], badge_y + (BSIZE - lh) // 2 - lbb[1]),
                badge.label,
                font=font_badge,
                fill=(255, 255, 255, 245),
            )

        right = bx - BGAP

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