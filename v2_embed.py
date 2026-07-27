from __future__ import annotations

import discord
from collections import defaultdict
from typing import Callable, Iterable, Optional, Sequence


V2_TEXT_LIMIT = 4000  # Discord v2 TextDisplay limit (best-effort)


def _safe_separator(
    *, visible: bool = True, spacing: Optional[object] = None
) -> discord.ui.Separator:
    """
    discord.py 2.7.0a has occasionally produced a Separator whose underlying spacing ends up as a plain string.
    This helper forces a valid SeparatorSpacing enum to avoid `.spacing.value` AttributeErrors on send.
    """
    if spacing is None:
        spacing = getattr(discord, "SeparatorSpacing", None)
        spacing = spacing.small if spacing is not None else None

    try:
        sep = (
            discord.ui.Separator(visible=visible, spacing=spacing)
            if spacing is not None
            else discord.ui.Separator(visible=visible)
        )
    except Exception:
        sep = discord.ui.Separator()

    try:
        enum_type = getattr(discord, "SeparatorSpacing", None)
        if enum_type is not None:
            if isinstance(getattr(sep, "spacing", None), str):
                sep.spacing = (
                    enum_type.small
                    if str(getattr(sep, "spacing", "")).lower() != "large"
                    else enum_type.large
                )

            underlying = getattr(sep, "_underlying", None)
            if underlying is not None and isinstance(
                getattr(underlying, "spacing", None), str
            ):
                underlying.spacing = (
                    enum_type.small
                    if str(getattr(underlying, "spacing", "")).lower() != "large"
                    else enum_type.large
                )
    except Exception:
        pass

    return sep


def iter_all_items(view: object) -> Iterable[discord.ui.Item]:
    walk = getattr(view, "walk_children", None)
    if callable(walk):
        yield from walk()
        return

    children = getattr(view, "children", None)
    if children is not None:
        yield from children


def _is_interactive_control(item: object) -> bool:
    return isinstance(item, (discord.ui.Button, discord.ui.Select))


def _control_signature(item: discord.ui.Item) -> tuple[object, ...]:
    return (
        type(item),
        getattr(item, "label", None),
        getattr(item, "placeholder", None),
        str(getattr(item, "emoji", None)),
        getattr(item, "row", None),
        getattr(item, "url", None),
    )


def _declared_view_item_factories(
    view: discord.ui.LayoutView,
) -> list[tuple[str, Callable[..., object]]]:
    """
    Return methods decorated with discord.ui model decorators.

    In some discord.py alpha builds, LayoutView does not auto-register these.
    """
    factories: list[tuple[str, Callable[..., object]]] = []
    seen_names: set[str] = set()

    # Base classes first, then subclasses.
    for cls in reversed(view.__class__.mro()):
        if cls is object:
            continue
        for name, value in cls.__dict__.items():
            if name in seen_names:
                continue
            if callable(value) and hasattr(value, "__discord_ui_model_type__"):
                factories.append((name, value))
                seen_names.add(name)

    return factories


def ensure_layoutview_controls(view: discord.ui.LayoutView) -> None:
    """
    Ensure decorator-declared controls exist on LayoutView instances.
    """
    try:
        existing_declared_names = {
            getattr(item, "_declared_control_name")
            for item in iter_all_items(view)
            if getattr(item, "_declared_control_name", None)
        }
        existing_signatures = {
            _control_signature(item)
            for item in iter_all_items(view)
            if _is_interactive_control(item)
        }
    except Exception:
        existing_declared_names = set()
        existing_signatures = set()

    for name, method in _declared_view_item_factories(view):
        if name in existing_declared_names:
            continue

        model_type = getattr(method, "__discord_ui_model_type__", None)
        model_kwargs = getattr(method, "__discord_ui_model_kwargs__", None)
        if model_type is None:
            continue

        kwargs = dict(model_kwargs or {})
        try:
            item = model_type(**kwargs)
        except Exception:
            continue

        sig = _control_signature(item)
        if sig in existing_signatures:
            continue

        bound = getattr(view, name, None)
        if bound is None:
            continue

        async def _callback(
            interaction: discord.Interaction,
            *,
            _bound=bound,
            _item=item,
        ):
            return await _bound(interaction, _item)

        item.callback = _callback
        setattr(item, "_declared_control_name", name)
        view.add_item(item)
        existing_declared_names.add(name)
        existing_signatures.add(sig)


def disable_all_interactive(view: object) -> None:
    for item in iter_all_items(view):
        if hasattr(item, "disabled"):
            try:
                item.disabled = True
            except Exception:
                pass


def _chunk_text(text: str, limit: int = V2_TEXT_LIMIT) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []

    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        split_at = remaining.rfind("\n", 0, limit)
        if split_at < 1:
            split_at = limit
        chunk = remaining[:split_at].rstrip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[split_at:].lstrip("\n").lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def embed_to_v2_items(
    embed: discord.Embed, files: Optional[Sequence[discord.File]] = None
) -> list[discord.ui.Item]:
    items: list[discord.ui.Item] = []

    header_lines: list[str] = []
    if embed.title:
        header_lines.append(f"# {embed.title}")
    if embed.description:
        header_lines.append(embed.description)

    header = "\n\n".join([line for line in header_lines if line]).strip()
    thumbnail_url = getattr(getattr(embed, "thumbnail", None), "url", None)

    if header:
        if thumbnail_url:
            items.append(
                discord.ui.Section(
                    discord.ui.TextDisplay(content=header),
                    accessory=discord.ui.Thumbnail(thumbnail_url),
                )
            )
        else:
            for chunk in _chunk_text(header):
                items.append(discord.ui.TextDisplay(content=chunk))

    for field in getattr(embed, "fields", []):
        name = (getattr(field, "name", "") or "").strip()
        value = (getattr(field, "value", "") or "").strip()
        if not (name or value):
            continue

        block = "## " + name if name else ""
        if value:
            block = f"{block}\n{value}".strip() if block else value

        for chunk in _chunk_text(block):
            items.append(discord.ui.TextDisplay(content=chunk))

    image_url = getattr(getattr(embed, "image", None), "url", None)
    if image_url:
        from discord.ui.media_gallery import MediaGalleryItem

        media_src = image_url
        if image_url.startswith("attachment://") and files:
            filename = image_url[len("attachment://") :]
            for f in files:
                if getattr(f, "filename", None) == filename:
                    media_src = f
                    break

        items.append(discord.ui.MediaGallery(MediaGalleryItem(media_src)))

    footer_text = getattr(getattr(embed, "footer", None), "text", None)
    if footer_text:
        items.append(_safe_separator())
        for chunk in _chunk_text(f"*{footer_text}*"):
            items.append(discord.ui.TextDisplay(content=chunk))

    return items


def controls_to_action_rows(
    controls: Sequence[discord.ui.Item],
) -> list[discord.ui.ActionRow]:
    """
    Build ActionRows while respecting Discord component row width limits.

    Select-like components typically consume full row width (5), so they
    cannot share an ActionRow with buttons.
    """
    hinted_rows: dict[int, list[discord.ui.Item]] = defaultdict(list)
    for item in controls:
        row = getattr(item, "row", None)
        hinted_rows[int(row) if row is not None else 0].append(item)

    packed_rows: list[list[discord.ui.Item]] = []
    for row_idx in sorted(hinted_rows):
        bucket = hinted_rows[row_idx]
        current: list[discord.ui.Item] = []
        used_width = 0

        for item in bucket:
            width = int(getattr(item, "width", 1) or 1)
            if width >= 5:
                # Full-width controls (e.g. Select) must be on their own row.
                if current:
                    packed_rows.append(current)
                    current = []
                    used_width = 0
                packed_rows.append([item])
                continue

            if used_width + width > 5:
                if current:
                    packed_rows.append(current)
                current = [item]
                used_width = width
            else:
                current.append(item)
                used_width += width

        if current:
            packed_rows.append(current)

    return [discord.ui.ActionRow(*row_items) for row_items in packed_rows]


def build_v2_container(
    *,
    body_items: Sequence[discord.ui.Item],
    controls: Sequence[discord.ui.Item] = (),
    accent_color: Optional[discord.Colour | int] = None,
) -> discord.ui.Container:
    children: list[discord.ui.Item] = list(body_items)
    if controls:
        if children:
            children.append(_safe_separator())
        children.extend(controls_to_action_rows(controls))

    return discord.ui.Container(*children, accent_colour=accent_color)


def apply_v2_embed_layout(
    view: discord.ui.LayoutView,
    *,
    embed: Optional[discord.Embed] = None,
    body_items: Optional[Sequence[discord.ui.Item]] = None,
    accent_color: Optional[discord.Colour | int] = None,
    files: Optional[Sequence[discord.File]] = None,
) -> None:
    ensure_layoutview_controls(view)

    controls: list[discord.ui.Item] = []
    for item in iter_all_items(view):
        if _is_interactive_control(item):
            controls.append(item)

    view.clear_items()

    if body_items is None:
        body_items = embed_to_v2_items(embed or discord.Embed(), files=files)

    if accent_color is None and embed is not None and embed.color is not None:
        accent_color = embed.color

    view.add_item(
        build_v2_container(
            body_items=body_items, controls=controls, accent_color=accent_color
        )
    )


_LAYOUTVIEW_PATCHED = False
_INTERACTION_EDIT_PATCHED = False


def patch_layoutview_declarative_items() -> None:
    """
    Patch discord.ui.LayoutView.__init__ to restore decorator controls globally.
    """
    global _LAYOUTVIEW_PATCHED
    if _LAYOUTVIEW_PATCHED:
        return

    original_init = discord.ui.LayoutView.__init__

    def _patched_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        try:
            ensure_layoutview_controls(self)
        except Exception:
            pass

    discord.ui.LayoutView.__init__ = _patched_init
    _LAYOUTVIEW_PATCHED = True


def _normalize_embeds_for_retry(value: object) -> list[discord.Embed]:
    if value is None:
        return []
    if isinstance(value, discord.Embed):
        return [value]
    if isinstance(value, (list, tuple)):
        return [x for x in value if isinstance(x, discord.Embed)]
    return []


def _is_components_v2_embed_error(exc: discord.HTTPException) -> bool:
    text = str(exc).lower()
    code = getattr(exc, "code", None)
    if "messageflags.is_components_v2" in text and "embed" in text:
        return True
    if code == 50035 and "embed" in text and "cannot be used" in text:
        return True
    return False


def _build_retry_kwargs_for_v2(
    kwargs: dict[object, object],
) -> Optional[dict[object, object]]:
    embeds: list[discord.Embed] = []
    embeds.extend(_normalize_embeds_for_retry(kwargs.get("embed")))
    embeds.extend(_normalize_embeds_for_retry(kwargs.get("embeds")))
    if not embeds:
        return None

    current_view = kwargs.get("view")
    if isinstance(current_view, discord.ui.LayoutView):
        retry_view = current_view
    elif current_view is None:
        retry_view = discord.ui.LayoutView(timeout=None)
    else:
        retry_view = discord.ui.LayoutView(
            timeout=getattr(current_view, "timeout", None)
        )
        # Best-effort migration of classic View controls into a LayoutView.
        try:
            for item in iter_all_items(current_view):
                if _is_interactive_control(item):
                    retry_view.add_item(item)
        except Exception:
            pass

    body_items: list[discord.ui.Item] = []
    for idx, emb in enumerate(embeds):
        body_items.extend(embed_to_v2_items(emb))
        if idx < len(embeds) - 1:
            body_items.append(_safe_separator())

    accent = None
    if embeds and embeds[0].color is not None:
        accent = embeds[0].color

    apply_v2_embed_layout(retry_view, body_items=body_items, accent_color=accent)

    retry_kwargs = dict(kwargs)
    retry_kwargs.pop("embed", None)
    retry_kwargs.pop("embeds", None)
    retry_kwargs["view"] = retry_view
    return retry_kwargs


def patch_interaction_response_v2_embed_fallback() -> None:
    """
    Patch InteractionResponse.edit_message to gracefully handle editing V2 messages
    with classic embeds (which Discord rejects with 50035).
    """
    global _INTERACTION_EDIT_PATCHED
    if _INTERACTION_EDIT_PATCHED:
        return

    original_response_edit_message = discord.InteractionResponse.edit_message
    original_edit_original_response = discord.Interaction.edit_original_response

    async def _patched_response_edit_message(self, *args, **kwargs):
        try:
            return await original_response_edit_message(self, *args, **kwargs)
        except discord.HTTPException as exc:
            if not _is_components_v2_embed_error(exc):
                raise
            retry_kwargs = _build_retry_kwargs_for_v2(kwargs)
            if retry_kwargs is None:
                raise
            return await original_response_edit_message(self, *args, **retry_kwargs)

    async def _patched_edit_original_response(self, *args, **kwargs):
        try:
            return await original_edit_original_response(self, *args, **kwargs)
        except discord.HTTPException as exc:
            if not _is_components_v2_embed_error(exc):
                raise
            retry_kwargs = _build_retry_kwargs_for_v2(kwargs)
            if retry_kwargs is None:
                raise
            return await original_edit_original_response(self, *args, **retry_kwargs)

    discord.InteractionResponse.edit_message = _patched_response_edit_message
    discord.Interaction.edit_original_response = _patched_edit_original_response
    _INTERACTION_EDIT_PATCHED = True


patch_layoutview_declarative_items()
patch_interaction_response_v2_embed_fallback()
