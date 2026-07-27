"""
Discord Components v2 migration utilities.

Provides automatic conversion from embeds to Components v2 layouts with opt-in/opt-out
controls. Supports both global and per-message configuration.

Features:
- Convert discord.Embed → Components v2 Container
- Optional monkeypatch for transparent v2 adoption
- Granular control: global settings, per-message flags, or manual conversion
- Preserves interactive components (buttons, selects) in ActionRows
"""

from __future__ import annotations

from datetime import datetime
import re
import types
from typing import Any, Optional, Literal

import discord


# Component types that can appear at the top level of a LayoutView
_V2_TOP_LEVEL_TYPES = {1, 9, 10, 12, 13, 14, 17}
# Regex to detect timestamp-only footer text (excluded from v2 cards)
_TS_ONLY_RE = re.compile(
    r"^(?:"
    r"<t:\d+(?::[tTdDfFR])?>"
    r"|"
    r"\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}:\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
    r")$"
)


def _is_missing_like(value: Any) -> bool:
    return value is None or value is ... or value is discord.utils.MISSING


def _has_embed_payload(value: Any) -> bool:
    """Safe truthiness check that never calls discord.Embed.__bool__."""
    if _is_missing_like(value):
        return False
    if isinstance(value, discord.Embed):
        return True
    if isinstance(value, (list, tuple, set, frozenset, dict)):
        return len(value) > 0
    return True


def _embed_candidates(value: Any) -> list[Any]:
    if _is_missing_like(value):
        return []
    if isinstance(value, discord.Embed):
        return [value]
    if isinstance(value, (list, tuple, set, frozenset)):
        return list(value)
    try:
        return list(value)
    except TypeError:
        return [value]


def _resolve_guild_for_target(target: object) -> Optional[discord.Guild]:
    """Best-effort guild lookup for send/edit targets across discord.py wrappers."""
    guild = getattr(target, "guild", None)
    if isinstance(guild, discord.Guild):
        return guild

    parent = getattr(target, "_parent", None)
    parent_guild = getattr(parent, "guild", None)
    if isinstance(parent_guild, discord.Guild):
        return parent_guild

    guild_id = getattr(target, "guild_id", None) or getattr(parent, "guild_id", None)
    if guild_id is None:
        return None

    state = getattr(target, "_state", None) or getattr(parent, "_state", None)
    getter = getattr(state, "_get_guild", None) if state is not None else None
    if callable(getter):
        try:
            resolved = getter(int(guild_id))
            if isinstance(resolved, discord.Guild):
                return resolved
        except Exception:
            return None
    return None


async def _apply_status_emojis_for_target(
    target: object,
    *,
    embed: Any,
    embeds: Any,
) -> tuple[Any, Any]:
    """Apply status emoji overrides (auto-create custom emojis when needed)."""
    if not _has_embed_payload(embed) and not _has_embed_payload(embeds):
        return embed, embeds

    guild = _resolve_guild_for_target(target)
    if guild is None:
        return embed, embeds

    try:
        from utils.status_emojis import apply_status_emoji_overrides
    except Exception:
        return embed, embeds

    updated_embed = embed
    updated_embeds = embeds

    if isinstance(embed, discord.Embed):
        try:
            updated_embed = await apply_status_emoji_overrides(embed, guild)
        except Exception:
            updated_embed = embed

    if _has_embed_payload(embeds):
        try:
            converted: list[Any] = []
            for candidate in _embed_candidates(embeds):
                if isinstance(candidate, discord.Embed):
                    try:
                        candidate = await apply_status_emoji_overrides(candidate, guild)
                    except Exception:
                        pass
                converted.append(candidate)
            updated_embeds = converted
        except Exception:
            updated_embeds = embeds

    return updated_embed, updated_embeds


class ComponentsV2Config:
    """Global configuration for Components v2 behavior."""
    
    enabled: bool = False
    """Whether to convert embeds to v2 layouts by default (opt-in)."""
    
    @classmethod
    def enable(cls) -> None:
        """Enable automatic v2 conversion globally."""
        cls.enabled = True
    
    @classmethod
    def disable(cls) -> None:
        """Disable automatic v2 conversion globally."""
        cls.enabled = False


def _component_type(item: discord.ui.Item[Any]) -> Optional[int]:
    """Extract the component type from a UI item."""
    try:
        data = item.to_component_dict()
        t = data.get("type")
        return int(t) if t is not None else None
    except Exception:
        return None


def ensure_layout_view_action_rows(view: discord.ui.LayoutView) -> discord.ui.LayoutView:
    """
    Ensure a Components v2 LayoutView has valid top-level structure.

    Discord Components v2 requires interactive components (Button, Select) to be wrapped
    in ActionRows. This function automatically wraps any loose interactive components
    and places ActionRows inside the last Container for better visual cohesion.

    Args:
        view: The LayoutView to normalize

    Returns:
        The same view with properly structured children
    """
    children = list(getattr(view, "children", []))
    if not children:
        return view

    needs_fix = any((_component_type(c) not in _V2_TOP_LEVEL_TYPES) for c in children)
    if not needs_fix:
        return view

    action_rows: dict[int, list[discord.ui.Item[Any]]] = {}
    layout_items: list[discord.ui.Item[Any]] = []

    # Separate layout components from interactive components
    for child in children:
        t = _component_type(child)
        if t in _V2_TOP_LEVEL_TYPES:
            layout_items.append(child)
            continue

        row = getattr(child, "row", None)
        row_index = row if isinstance(row, int) and row >= 0 else 0
        action_rows.setdefault(row_index, []).append(child)

    # Rebuild view with layout items first, then ActionRows
    view.clear_items()
    for item in layout_items:
        view.add_item(item)
    for row_index in sorted(action_rows.keys()):
        view.add_item(discord.ui.ActionRow(*action_rows[row_index]))

    # Move ActionRows inside the last container for better appearance
    try:
        children = list(getattr(view, "children", []))
        containers = [c for c in children if isinstance(c, discord.ui.Container)]
        action_row_items = [c for c in children if isinstance(c, discord.ui.ActionRow)]
        
        if containers and action_row_items:
            last_container = containers[-1]
            view.clear_items()
            
            for item in children:
                if not isinstance(item, discord.ui.ActionRow):
                    view.add_item(item)
            
            for row in action_row_items:
                last_container.add_item(row)
    except Exception:
        pass

    return view


def branded_panel_container(
    *,
    title: str,
    description: str,
    banner_url: Optional[str] = None,
    logo_url: Optional[str] = None,
    accent_color: Optional[int] = None,
    banner_separated: bool = False,
) -> discord.ui.Container:
    """
    Create a branded panel with optional banner and logo.

    Args:
        title: Panel title (bolded automatically)
        description: Panel description text
        banner_url: URL for banner image (displayed at top)
        logo_url: URL for thumbnail logo (displayed alongside header)
        accent_color: Sidebar accent color (RGB integer)
        banner_separated: Add separator between banner and content

    Returns:
        A Container with the branded panel layout
    """
    children: list[discord.ui.Item[Any]] = []

    title = (title or "").strip()
    description = (description or "").strip()
    header = "\n".join([x for x in [f"**{title}**" if title else "", description] if x]).strip()

    # Add banner if provided
    has_banner = False
    if banner_url:
        banner_url = (banner_url or "").strip()
        if banner_url:
            children.append(discord.ui.MediaGallery(discord.MediaGalleryItem(banner_url)))
            has_banner = True

    # Add separator after banner if requested
    if has_banner and banner_separated and header:
        children.append(discord.ui.Separator(spacing=discord.SeparatorSpacing.large))

    # Add header with optional logo thumbnail
    if header:
        logo_url = (logo_url or "").strip() if logo_url else None
        
        if logo_url:
            children.append(
                discord.ui.Section(
                    discord.ui.TextDisplay(header),
                    accessory=discord.ui.Thumbnail(logo_url),
                )
            )
        else:
            children.append(discord.ui.TextDisplay(header))

    if accent_color is not None:
        return discord.ui.Container(*children, accent_color=accent_color)
    return discord.ui.Container(*children)


def _get_embed_thumbnail_url(embed: discord.Embed) -> Optional[str]:
    """Extract thumbnail URL from embed."""
    try:
        url = getattr(embed.thumbnail, "url", None)
        return url or None
    except Exception:
        return None


def _get_embed_image_url(embed: discord.Embed) -> Optional[str]:
    """Extract image URL from embed."""
    try:
        url = getattr(embed.image, "url", None)
        return url or None
    except Exception:
        return None


def _get_embed_footer_text(embed: discord.Embed) -> Optional[str]:
    """Extract footer text from embed, excluding timestamp-only footers."""
    try:
        text = getattr(embed.footer, "text", None)
        text = (text or "").strip()
        if text and _TS_ONLY_RE.fullmatch(text):
            return None
        return text or None
    except Exception:
        return None


def _accent_color_from_embed(embed: discord.Embed) -> Optional[int]:
    """Extract accent color from embed."""
    try:
        color = embed.color
        if isinstance(color, discord.Colour):
            value = int(color.value)
            return value or None
        if isinstance(color, int):
            return color or None
    except Exception:
        pass
    return None


def container_from_embed(embed: discord.Embed) -> discord.ui.Container:
    """
    Convert a discord.Embed to a Components v2 Container.

    Mapping:
    - embed.author.name → Bold header text
    - embed.title → Bold header text
    - embed.description → Header text
    - embed.thumbnail → Section accessory
    - embed.fields → Separated TextDisplay items
    - embed.image → MediaGallery at bottom
    - embed.footer → Italicized metadata (if not timestamp-only)
    - embed.color → Container accent color

    Args:
        embed: The embed to convert

    Returns:
        A Container representing the embed
    """
    accent = _accent_color_from_embed(embed)
    children: list[discord.ui.Item[Any]] = []

    # Build header from author, title, and description
    title = (embed.title or "").strip()
    desc = (embed.description or "").strip()
    author_name = ""
    
    try:
        author_name = (getattr(embed.author, "name", "") or "").strip()
    except Exception:
        author_name = ""

    header_lines: list[str] = []
    if author_name:
        header_lines.append(f"**{author_name}**")
    if title:
        header_lines.append(f"**{title}**")
    if desc:
        header_lines.append(desc)

    # Add header with optional thumbnail
    header_text = "\n".join(header_lines).strip()
    thumb_url = _get_embed_thumbnail_url(embed)
    
    if header_text:
        if thumb_url:
            children.append(
                discord.ui.Section(
                    discord.ui.TextDisplay(header_text),
                    accessory=discord.ui.Thumbnail(thumb_url),
                )
            )
        else:
            children.append(discord.ui.TextDisplay(header_text))

    # Add fields with separators
    for field in embed.fields:
        name = (field.name or "").strip()
        value = (field.value or "").strip()
        if not name and not value:
            continue
            
        children.append(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
        
        if name and value:
            children.append(discord.ui.TextDisplay(f"**{name}**\n{value}"))
        elif name:
            children.append(discord.ui.TextDisplay(f"**{name}**"))
        else:
            children.append(discord.ui.TextDisplay(value))

    # Add image gallery if present
    image_url = _get_embed_image_url(embed)
    if image_url:
        children.append(discord.ui.Separator(spacing=discord.SeparatorSpacing.large))
        children.append(discord.ui.MediaGallery(discord.MediaGalleryItem(image_url)))

    # Add footer metadata (excluding timestamp-only footers)
    footer_text = _get_embed_footer_text(embed)
    if footer_text:
        children.append(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
        children.append(discord.ui.TextDisplay(f"*{footer_text}*"))

    if accent is not None:
        return discord.ui.Container(*children, accent_color=accent)
    return discord.ui.Container(*children)


def _normalize_embeds(
    *,
    embed: Any = None,
    embeds: Any = None,
) -> list[discord.Embed]:
    """Normalize embed/embeds arguments into a list of Embeds."""
    out: list[discord.Embed] = []
    candidates: list[Any] = []
    if isinstance(embed, discord.Embed):
        candidates.append(embed)
    candidates.extend(_embed_candidates(embeds))

    clone_embed = None
    try:
        from utils.logging import clone_embed as _clone_embed
        clone_embed = _clone_embed
    except Exception:
        clone_embed = None

    for e in candidates:
        if not isinstance(e, discord.Embed):
            continue
        if clone_embed is None:
            out.append(e)
            continue
        try:
            out.append(clone_embed(e))
        except Exception:
            out.append(e)
    return out


async def layout_view_from_embeds(
    *,
    content: Any = None,
    embed: Any = None,
    embeds: Any = None,
    existing_view: Optional[discord.ui.BaseView] = None,
) -> discord.ui.LayoutView:
    """
    Create a LayoutView from embeds and optional content.

    Args:
        content: Message content (moved into a Container)
        embed: Single embed to convert
        embeds: List of embeds to convert
        existing_view: Existing view whose items should be preserved

    Returns:
        A LayoutView with Containers for each embed and preserved view items
    """
    source_view_for_hooks: Optional[discord.ui.BaseView] = None
    if existing_view is None:
        view = discord.ui.LayoutView()
        existing_children: list[discord.ui.Item[Any]] = []
    else:
        existing_children = list(getattr(existing_view, "children", []))
        if isinstance(existing_view, discord.ui.LayoutView):
            view = existing_view
        else:
            timeout = getattr(existing_view, "timeout", 180.0)
            view = discord.ui.LayoutView(timeout=timeout)
            source_view_for_hooks = existing_view

    if source_view_for_hooks is not None:
        async def _delegated_interaction_check(self: discord.ui.LayoutView, interaction: discord.Interaction) -> bool:
            try:
                checker = getattr(source_view_for_hooks, "interaction_check", None)
                if checker is None:
                    return True
                return bool(await checker(interaction))
            except Exception:
                return False

        async def _delegated_on_timeout(self: discord.ui.LayoutView) -> None:
            handler = getattr(source_view_for_hooks, "on_timeout", None)
            if handler is None:
                return
            try:
                await handler()
            except Exception:
                pass

        async def _delegated_on_error(
            self: discord.ui.LayoutView,
            interaction: discord.Interaction,
            error: Exception,
            item: discord.ui.Item[Any],
            /,
        ) -> None:
            handler = getattr(source_view_for_hooks, "on_error", None)
            if handler is None:
                return
            try:
                await handler(interaction, error, item)
            except Exception:
                pass

        view.interaction_check = types.MethodType(_delegated_interaction_check, view)
        view.on_timeout = types.MethodType(_delegated_on_timeout, view)
        view.on_error = types.MethodType(_delegated_on_error, view)

    if existing_children:
        view.clear_items()

    # Add content as a TextDisplay if provided
    if content not in (None, discord.utils.MISSING, ...):
        try:
            content_text = content if isinstance(content, str) else str(content)
        except Exception:
            content_text = None
        if content_text:
            content_text = content_text.strip()
        if content_text:
            view.add_item(discord.ui.Container(discord.ui.TextDisplay(content_text)))

    # Convert each embed to a Container
    for e in _normalize_embeds(embed=embed, embeds=embeds):
        try:
            container = container_from_embed(e)
        except Exception:
            # Recover from malformed embeds without hard-failing message sends.
            try:
                from utils.logging import clone_embed
                container = container_from_embed(clone_embed(e))
            except Exception:
                continue
        if container.children:
            view.add_item(container)

    # Preserve existing view items
    for child in existing_children:
        view.add_item(child)
        
    return ensure_layout_view_action_rows(view)


def patch_components_v2() -> None:
    """
    Monkeypatch discord.py to auto-convert embeds to Components v2.

    This allows existing embed-based code to work with Components v2 without changes.
    Respects ComponentsV2Config.enabled and per-message use_v2 flags.
    Defaults to classic embed behavior (v1) unless explicitly enabled.

    Control behavior:
    - Global: ComponentsV2Config.enable() / .disable()
    - Per-message: pass use_v2=False to any send/edit method

    Example:
        # Enable globally
        patch_components_v2()
        ComponentsV2Config.enable()

        # This sends v2
        await ctx.send(embed=my_embed)

        # This sends v1
        await ctx.send(embed=my_embed, use_v2=False)

        # This sends plain message
        await ctx.send("Hello")
    """
    if getattr(patch_components_v2, "_patched", False):
        return

    from discord.utils import MISSING

    # Store original methods
    original_interaction_send = discord.InteractionResponse.send_message
    original_webhook_send = discord.Webhook.send
    original_messageable_send = discord.abc.Messageable.send
    original_interaction_edit_original = discord.Interaction.edit_original_response
    original_interaction_response_edit = discord.InteractionResponse.edit_message
    original_message_edit = discord.Message.edit
    original_webhook_edit_message = discord.Webhook.edit_message
    original_webhook_message_edit = discord.WebhookMessage.edit

    try:
        from utils.classic_send import register_original_messageable_send

        register_original_messageable_send(original_messageable_send)
    except Exception:
        pass

    def _parse_use_v2(value: Any) -> Optional[bool]:
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                return True
            if normalized in {"0", "false", "no", "off"}:
                return False
        return bool(value)

    def _should_use_v2(kwargs: dict[str, Any]) -> bool:
        """Determine if v2 conversion should be applied based on config and flags."""
        use_v2 = _parse_use_v2(kwargs.pop("use_v2", None))
        
        # Explicit per-message control
        if use_v2 is True:
            return True
        if use_v2 is False:
            return False
        
        # Fall back to global config
        return ComponentsV2Config.enabled

    def _strip_none_view_for_send(kwargs: dict[str, Any]) -> None:
        """
        Discord send APIs reject an explicit `view=None`.

        Keep `view=None` semantics for edit operations, but drop it for send paths
        where omitting the parameter is equivalent and avoids type errors.
        """
        if kwargs.get("view", MISSING) is None:
            kwargs.pop("view", None)

    async def patched_interaction_send_message(self, *args, **kwargs):
        _strip_none_view_for_send(kwargs)
        existing_embed = kwargs.get("embed", MISSING)
        existing_embeds = kwargs.get("embeds", MISSING)
        if not _is_missing_like(existing_embed) or _has_embed_payload(existing_embeds):
            patched_embed, patched_embeds = await _apply_status_emojis_for_target(
                self,
                embed=existing_embed,
                embeds=existing_embeds,
            )
            if not _is_missing_like(existing_embed):
                kwargs["embed"] = patched_embed
            if not _is_missing_like(existing_embeds):
                kwargs["embeds"] = patched_embeds

        if not _should_use_v2(kwargs):
            content = args[0] if args else kwargs.get("content", MISSING)
            if args:
                return await original_interaction_send(self, *args, **kwargs)
            return await original_interaction_send(self, **kwargs)

        content = args[0] if args else kwargs.pop("content", MISSING)
        kwargs.pop("content", None)
        embed = kwargs.get("embed", MISSING)
        embeds = kwargs.get("embeds", MISSING)
        view = kwargs.get("view", MISSING)

        has_embed = embed is not MISSING and embed is not None
        has_embeds = embeds is not MISSING and _has_embed_payload(embeds)
        has_content = content is not MISSING and content not in (None, "", ...)
        has_visual = has_embed or has_embeds

        if has_visual and (
            view in (MISSING, None) or hasattr(view, "children")
        ):
            layout = await layout_view_from_embeds(
                content=content,
                embed=None if embed is MISSING else embed,
                embeds=None if embeds is MISSING else embeds,
                existing_view=None if view is MISSING else view,
            )
            layout = ensure_layout_view_action_rows(layout)
            kwargs["view"] = layout
            kwargs.pop("embed", None)
            kwargs.pop("embeds", None)
            content = MISSING
        elif isinstance(view, discord.ui.LayoutView) and content is not MISSING:
            layout = await layout_view_from_embeds(
                content=content,
                embed=None,
                embeds=None,
                existing_view=view,
            )
            layout = ensure_layout_view_action_rows(layout)
            kwargs["view"] = layout
            content = MISSING
        elif isinstance(view, discord.ui.LayoutView):
            kwargs["view"] = ensure_layout_view_action_rows(view)

        if content is MISSING:
            return await original_interaction_send(self, **kwargs)
        return await original_interaction_send(self, content=content, **kwargs)

    async def _coerce_to_v2_view(
        *, content: Any, embed: Any, embeds: Any, view: Any, use_v2: bool
    ) -> tuple[Any, Any, Any, Any]:
        """Convert embed(s)/content to LayoutView if v2 is enabled."""
        if not use_v2:
            return (content, embed, embeds, view)

        has_embed = embed is not MISSING and embed is not None
        has_embeds = embeds is not MISSING and _has_embed_payload(embeds)
        has_content = content is not MISSING and content not in (None, "", ...)
        has_visual = has_embed or has_embeds

        if has_visual and (
            view in (MISSING, None) or hasattr(view, "children")
        ):
            layout = await layout_view_from_embeds(
                content=content,
                embed=None if embed is MISSING else embed,
                embeds=None if embeds is MISSING else embeds,
                existing_view=None if view is MISSING else view,
            )
            layout = ensure_layout_view_action_rows(layout)
            return (MISSING, None, None, layout)

        if isinstance(view, discord.ui.LayoutView) and content is not MISSING:
            layout = await layout_view_from_embeds(
                content=content,
                embed=None,
                embeds=None,
                existing_view=view,
            )
            layout = ensure_layout_view_action_rows(layout)
            return (
                MISSING,
                None,
                None,
                layout,
            )

        if isinstance(view, discord.ui.LayoutView):
            return (
                content,
                embed if embed is not MISSING else None,
                embeds if embeds is not MISSING else None,
                ensure_layout_view_action_rows(view),
            )

        return (
            content,
            embed if embed is not MISSING else None,
            embeds if embeds is not MISSING else None,
            view,
        )

    async def patched_webhook_send(self, *args, **kwargs):
        _strip_none_view_for_send(kwargs)
        existing_embed = kwargs.get("embed", MISSING)
        existing_embeds = kwargs.get("embeds", MISSING)
        if not _is_missing_like(existing_embed) or _has_embed_payload(existing_embeds):
            patched_embed, patched_embeds = await _apply_status_emojis_for_target(
                self,
                embed=existing_embed,
                embeds=existing_embeds,
            )
            if not _is_missing_like(existing_embed):
                kwargs["embed"] = patched_embed
            if not _is_missing_like(existing_embeds):
                kwargs["embeds"] = patched_embeds

        if not _should_use_v2(kwargs):
            content = args[0] if args else kwargs.get("content", MISSING)
            if args:
                return await original_webhook_send(self, *args, **kwargs)
            return await original_webhook_send(self, **kwargs)

        content = args[0] if args else kwargs.pop("content", MISSING)
        kwargs.pop("content", None)
        embed = kwargs.get("embed", MISSING)
        embeds = kwargs.get("embeds", MISSING)
        view = kwargs.get("view", MISSING)

        has_embed = embed is not MISSING and embed is not None
        has_embeds = embeds is not MISSING and _has_embed_payload(embeds)
        has_content = content is not MISSING and content not in (None, "", ...)
        has_visual = has_embed or has_embeds

        if has_visual and (
            view in (MISSING, None) or hasattr(view, "children")
        ):
            layout = await layout_view_from_embeds(
                content=content,
                embed=None if embed is MISSING else embed,
                embeds=None if embeds is MISSING else embeds,
                existing_view=None if view is MISSING else view,
            )
            layout = ensure_layout_view_action_rows(layout)
            kwargs["view"] = layout
            kwargs.pop("embed", None)
            kwargs.pop("embeds", None)
            content = MISSING
        elif isinstance(view, discord.ui.LayoutView) and content is not MISSING:
            layout = await layout_view_from_embeds(
                content=content,
                embed=None,
                embeds=None,
                existing_view=view,
            )
            layout = ensure_layout_view_action_rows(layout)
            kwargs["view"] = layout
            content = MISSING
        elif isinstance(view, discord.ui.LayoutView):
            kwargs["view"] = ensure_layout_view_action_rows(view)

        if content is MISSING:
            return await original_webhook_send(self, **kwargs)
        return await original_webhook_send(self, content=content, **kwargs)

    async def patched_messageable_send(self, *args, **kwargs):
        _strip_none_view_for_send(kwargs)
        has_embed_kw = "embed" in kwargs
        has_embeds_kw = "embeds" in kwargs
        maybe_embed = kwargs.get("embed", None)
        maybe_embeds = kwargs.get("embeds", None)
        if maybe_embed is not None or _has_embed_payload(maybe_embeds):
            maybe_embed, maybe_embeds = await _apply_status_emojis_for_target(
                self,
                embed=maybe_embed,
                embeds=maybe_embeds,
            )
            if has_embed_kw:
                kwargs["embed"] = maybe_embed
            if has_embeds_kw:
                kwargs["embeds"] = maybe_embeds

        if not _should_use_v2(kwargs):
            return await original_messageable_send(self, *args, **kwargs)

        content = args[0] if args else kwargs.pop("content", MISSING)
        kwargs.pop("content", None)
        embed = kwargs.get("embed", None)
        embeds = kwargs.get("embeds", None)
        view = kwargs.get("view", None)

        has_embed = embed is not None
        has_embeds = _has_embed_payload(embeds)
        has_content = content is not MISSING and content not in (None, "", ...)
        has_visual = has_embed or has_embeds

        if has_visual and (view is None or hasattr(view, "children")):
            layout = await layout_view_from_embeds(
                content=content,
                embed=embed,
                embeds=embeds,
                existing_view=view,
            )
            layout = ensure_layout_view_action_rows(layout)
            kwargs["view"] = layout
            kwargs.pop("embed", None)
            kwargs.pop("embeds", None)
            content = MISSING
        elif isinstance(view, discord.ui.LayoutView) and content is not MISSING:
            layout = await layout_view_from_embeds(
                content=content,
                embed=None,
                embeds=None,
                existing_view=view,
            )
            layout = ensure_layout_view_action_rows(layout)
            kwargs["view"] = layout
            content = MISSING
        elif isinstance(view, discord.ui.LayoutView):
            kwargs["view"] = ensure_layout_view_action_rows(view)

        if content is MISSING:
            return await original_messageable_send(self, **kwargs)
        return await original_messageable_send(self, content=content, **kwargs)

    async def patched_interaction_edit_original_response(self, *args, **kwargs):
        use_v2 = _should_use_v2(kwargs)
        
        content = kwargs.get("content", MISSING)
        kwargs.pop("content", None)
        embed = kwargs.get("embed", MISSING)
        embeds = kwargs.get("embeds", MISSING)
        view = kwargs.get("view", MISSING)

        if not _is_missing_like(embed) or _has_embed_payload(embeds):
            embed, embeds = await _apply_status_emojis_for_target(
                self,
                embed=embed,
                embeds=embeds,
            )

        content, embed, embeds, view = await _coerce_to_v2_view(
            content=content,
            embed=embed,
            embeds=embeds,
            view=view,
            use_v2=use_v2,
        )

        if view is not MISSING:
            kwargs["view"] = view
        if embed is None:
            kwargs.pop("embed", None)
        if embeds is None:
            kwargs.pop("embeds", None)

        if content is MISSING:
            return await original_interaction_edit_original(self, **kwargs)
        return await original_interaction_edit_original(self, content=content, **kwargs)

    async def patched_interaction_response_edit_message(self, *args, **kwargs):
        use_v2 = _should_use_v2(kwargs)
        
        content = kwargs.get("content", MISSING)
        kwargs.pop("content", None)
        embed = kwargs.get("embed", MISSING)
        embeds = kwargs.get("embeds", MISSING)
        view = kwargs.get("view", MISSING)

        if not _is_missing_like(embed) or _has_embed_payload(embeds):
            embed, embeds = await _apply_status_emojis_for_target(
                self,
                embed=embed,
                embeds=embeds,
            )

        content, embed, embeds, view = await _coerce_to_v2_view(
            content=content,
            embed=embed,
            embeds=embeds,
            view=view,
            use_v2=use_v2,
        )

        if view is not MISSING:
            kwargs["view"] = view
        if embed is None:
            kwargs.pop("embed", None)
        if embeds is None:
            kwargs.pop("embeds", None)

        if content is MISSING:
            return await original_interaction_response_edit(self, **kwargs)
        return await original_interaction_response_edit(self, content=content, **kwargs)

    async def patched_message_edit(self, *args, **kwargs):
        use_v2 = _should_use_v2(kwargs)
        
        content = kwargs.get("content", MISSING)
        kwargs.pop("content", None)
        embed = kwargs.get("embed", MISSING)
        embeds = kwargs.get("embeds", MISSING)
        view = kwargs.get("view", MISSING)

        if not _is_missing_like(embed) or _has_embed_payload(embeds):
            embed, embeds = await _apply_status_emojis_for_target(
                self,
                embed=embed,
                embeds=embeds,
            )

        content, embed, embeds, view = await _coerce_to_v2_view(
            content=content,
            embed=embed,
            embeds=embeds,
            view=view,
            use_v2=use_v2,
        )

        if view is not MISSING:
            kwargs["view"] = view
        if embed is None:
            kwargs.pop("embed", None)
        if embeds is None:
            kwargs.pop("embeds", None)

        if content is MISSING:
            return await original_message_edit(self, **kwargs)
        return await original_message_edit(self, content=content, **kwargs)

    async def patched_webhook_edit_message(self, *args, **kwargs):
        use_v2 = _should_use_v2(kwargs)
        
        content = kwargs.get("content", MISSING)
        kwargs.pop("content", None)
        embed = kwargs.get("embed", MISSING)
        embeds = kwargs.get("embeds", MISSING)
        view = kwargs.get("view", MISSING)

        if not _is_missing_like(embed) or _has_embed_payload(embeds):
            embed, embeds = await _apply_status_emojis_for_target(
                self,
                embed=embed,
                embeds=embeds,
            )

        content, embed, embeds, view = await _coerce_to_v2_view(
            content=content,
            embed=embed,
            embeds=embeds,
            view=view,
            use_v2=use_v2,
        )

        if view is not MISSING:
            kwargs["view"] = view
        if embed is None:
            kwargs.pop("embed", None)
        if embeds is None:
            kwargs.pop("embeds", None)

        if content is MISSING:
            return await original_webhook_edit_message(self, *args, **kwargs)
        return await original_webhook_edit_message(self, *args, content=content, **kwargs)

    async def patched_webhook_message_edit(self, *args, **kwargs):
        use_v2 = _should_use_v2(kwargs)
        
        content = kwargs.get("content", MISSING)
        kwargs.pop("content", None)
        embed = kwargs.get("embed", MISSING)
        embeds = kwargs.get("embeds", MISSING)
        view = kwargs.get("view", MISSING)

        if not _is_missing_like(embed) or _has_embed_payload(embeds):
            embed, embeds = await _apply_status_emojis_for_target(
                self,
                embed=embed,
                embeds=embeds,
            )

        content, embed, embeds, view = await _coerce_to_v2_view(
            content=content,
            embed=embed,
            embeds=embeds,
            view=view,
            use_v2=use_v2,
        )

        if view is not MISSING:
            kwargs["view"] = view
        if embed is None:
            kwargs.pop("embed", None)
        if embeds is None:
            kwargs.pop("embeds", None)

        if content is MISSING:
            return await original_webhook_message_edit(self, *args, **kwargs)
        return await original_webhook_message_edit(self, *args, content=content, **kwargs)

    # Apply patches
    discord.InteractionResponse.send_message = patched_interaction_send_message  # type: ignore
    discord.Webhook.send = patched_webhook_send  # type: ignore
    discord.abc.Messageable.send = patched_messageable_send  # type: ignore
    discord.Interaction.edit_original_response = patched_interaction_edit_original_response  # type: ignore
    discord.InteractionResponse.edit_message = patched_interaction_response_edit_message  # type: ignore
    discord.Message.edit = patched_message_edit  # type: ignore
    discord.Webhook.edit_message = patched_webhook_edit_message  # type: ignore
    discord.WebhookMessage.edit = patched_webhook_message_edit  # type: ignore

    patch_components_v2._patched = True  # type: ignore
