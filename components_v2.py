from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

import discord


LOGGER = logging.getLogger("enzo-bot.components-v2")
_V2_TOP_LEVEL_TYPES = {1, 9, 10, 12, 13, 14, 17}
BRAND_BANNER_ASSET_NAME = "banner.png"
BRAND_LOGO_ASSET_NAME = "logo.png"
BRAND_BANNER_ATTACHMENT_NAME = "soul-banner.png"
BRAND_LOGO_ATTACHMENT_NAME = "soul-logo.png"
BRAND_CONTAINER_MIN_WIDTH_CHARS = 0
_WIDTH_SPACER_CHAR = "\u2800"
_ENSURED_EMOJIS: dict[tuple[int, str], discord.Emoji] = {}
_EMOJIS_BY_NAME: dict[str, discord.Emoji] = {}


def get_valk_emoji(guild: Optional[discord.Guild], name: str) -> str:
    emoji: Optional[discord.Emoji] = None
    if guild:
        emoji = discord.utils.get(guild.emojis, name=name)
        if emoji is None:
            emoji = _ENSURED_EMOJIS.get((guild.id, name))
        if emoji is not None:
            _EMOJIS_BY_NAME[name] = emoji
    if emoji is None:
        emoji = _EMOJIS_BY_NAME.get(name)
    return f"{emoji} " if emoji else ""


async def ensure_valk_emojis(
    guild: Optional[discord.Guild],
    emoji_dir: Path,
    *,
    legacy_names: Optional[set[str]] = None,
) -> dict[str, discord.Emoji]:
    """Create missing custom emojis from PNG files in assets/emojis."""
    if guild is None or not emoji_dir.exists():
        return {}

    emojis: dict[str, discord.Emoji] = {emoji.name: emoji for emoji in guild.emojis}
    for name, emoji in emojis.items():
        _ENSURED_EMOJIS[(guild.id, name)] = emoji
        _EMOJIS_BY_NAME[name] = emoji

    me = guild.me
    if me is None:
        return emojis
    if not me.guild_permissions.manage_emojis_and_stickers:
        return emojis

    for legacy_name in legacy_names or set():
        legacy = emojis.get(legacy_name)
        if legacy is None:
            continue
        try:
            await legacy.delete(reason="Replace legacy Soul emoji name")
            emojis.pop(legacy_name, None)
        except (discord.Forbidden, discord.HTTPException):
            pass

    for path in sorted(emoji_dir.glob("*.png")):
        name = path.stem
        if name in emojis:
            continue
        try:
            emoji = await guild.create_custom_emoji(
                name=name,
                image=path.read_bytes(),
                reason="Upload missing Soul bot emoji",
            )
        except (OSError, discord.Forbidden, discord.HTTPException):
            continue
        emojis[name] = emoji
        _ENSURED_EMOJIS[(guild.id, name)] = emoji
        _EMOJIS_BY_NAME[name] = emoji

    return emojis


def _component_type(item: discord.ui.Item[Any]) -> Optional[int]:
    try:
        data = item.to_component_dict()
        value = data.get("type")
        return int(value) if value is not None else None
    except Exception:
        return None


def ensure_layout_view_action_rows(
    view: discord.ui.LayoutView,
) -> discord.ui.LayoutView:
    children = list(getattr(view, "children", []))
    if not children:
        return view

    needs_fix = any(
        (_component_type(child) not in _V2_TOP_LEVEL_TYPES) for child in children
    )
    if not needs_fix:
        return view

    action_rows: dict[int, list[discord.ui.Item[Any]]] = {}
    layout_items: list[discord.ui.Item[Any]] = []

    for child in children:
        component_type = _component_type(child)
        if component_type in _V2_TOP_LEVEL_TYPES:
            layout_items.append(child)
            continue

        row = getattr(child, "row", None)
        row_index = row if isinstance(row, int) and row >= 0 else 0
        action_rows.setdefault(row_index, []).append(child)

    view.clear_items()
    for item in layout_items:
        view.add_item(item)
    for row_index in sorted(action_rows):
        view.add_item(discord.ui.ActionRow(*action_rows[row_index]))

    try:
        children = list(getattr(view, "children", []))
        containers = [
            child for child in children if isinstance(child, discord.ui.Container)
        ]
        action_row_items = [
            child for child in children if isinstance(child, discord.ui.ActionRow)
        ]
        if containers and action_row_items:
            last_container = containers[-1]
            view.clear_items()
            for item in children:
                if not isinstance(item, discord.ui.ActionRow):
                    view.add_item(item)
            for row in action_row_items:
                last_container.add_item(row)
    except Exception as exc:
        LOGGER.debug("V2 layout reorganization skipped: %s", exc)

    return view


def branded_asset_url(kind: str) -> Optional[str]:
    if kind == "banner":
        return f"attachment://{BRAND_BANNER_ATTACHMENT_NAME}"
    if kind == "logo":
        return f"attachment://{BRAND_LOGO_ATTACHMENT_NAME}"
    return None


def branded_asset_files(icon_dir: Path) -> list[discord.File]:
    files: list[discord.File] = []

    asset_dir = icon_dir.parent / "assets"

    banner_path = asset_dir / BRAND_BANNER_ASSET_NAME
    if banner_path.exists():
        files.append(discord.File(banner_path, filename=BRAND_BANNER_ATTACHMENT_NAME))

    logo_path = asset_dir / BRAND_LOGO_ASSET_NAME
    if logo_path.exists():
        files.append(discord.File(logo_path, filename=BRAND_LOGO_ATTACHMENT_NAME))

    return files


def _with_min_width_spacer(text: str, min_width_chars: Optional[int]) -> str:
    if not text or not min_width_chars or min_width_chars <= 0:
        return text

    spacer = _WIDTH_SPACER_CHAR * min_width_chars
    return f"{text}{spacer}"


def branded_panel_container(
    *,
    title: str,
    description: str,
    banner_url: Optional[str] = None,
    logo_url: Optional[str] = None,
    accent_color: Optional[int] = None,
    banner_separated: bool = False,
    min_width_chars: Optional[int] = BRAND_CONTAINER_MIN_WIDTH_CHARS,
    header_accessory: Optional[discord.ui.Item[Any]] = None,
    actions: Optional[list[discord.ui.Item[Any]]] = None,
) -> discord.ui.Container:
    children: list[discord.ui.Item[Any]] = []

    title = (title or "").strip()
    description = (description or "").strip()
    header = "\n".join(
        part for part in [f"**{title}**" if title else "", description] if part
    ).strip()
    header = _with_min_width_spacer(header, min_width_chars)

    has_banner = False
    if banner_url:
        normalized_banner = banner_url.strip()
        if normalized_banner:
            children.append(
                discord.ui.MediaGallery(discord.MediaGalleryItem(normalized_banner))
            )
            has_banner = True

    if has_banner and banner_separated and header:
        children.append(discord.ui.Separator(spacing=discord.SeparatorSpacing.large))

    if header:
        normalized_logo = logo_url.strip() if logo_url else None
        if header_accessory is not None:
            children.append(
                discord.ui.Section(
                    discord.ui.TextDisplay(header),
                    accessory=header_accessory,
                )
            )
        elif normalized_logo:
            children.append(
                discord.ui.Section(
                    discord.ui.TextDisplay(header),
                    accessory=discord.ui.Thumbnail(normalized_logo),
                )
            )
        else:
            children.append(discord.ui.TextDisplay(header))

    if actions:
        children.append(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
        children.append(discord.ui.ActionRow(*actions))

    if accent_color is not None:
        return discord.ui.Container(*children, accent_color=accent_color)
    return discord.ui.Container(*children)


def thumbnail_text_section(
    text: str, thumbnail_url: Optional[str]
) -> discord.ui.Item[Any]:
    """Return V2 text with a thumbnail accessory when an image URL is available."""
    text_display = discord.ui.TextDisplay(text)
    normalized_url = thumbnail_url.strip() if thumbnail_url else None
    if normalized_url:
        return discord.ui.Section(
            text_display, accessory=discord.ui.Thumbnail(normalized_url)
        )
    return text_display


def branded_notice_view(
    *,
    title: str,
    description: str,
    accent_color: Optional[int] = None,
    banner_url: Optional[str] = None,
    logo_url: Optional[str] = None,
    actions: Optional[list[discord.ui.Item[Any]]] = None,
) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    container = branded_panel_container(
        title=title,
        description=description,
        banner_url=banner_url,
        logo_url=logo_url,
        accent_color=accent_color,
        actions=actions,
    )
    view.add_item(container)
    return ensure_layout_view_action_rows(view)
