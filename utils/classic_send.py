from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional

import discord


_MessageableSend = Callable[..., Awaitable[Any]]
_ORIGINAL_MESSAGEABLE_SEND: Optional[_MessageableSend] = discord.abc.Messageable.send


def register_original_messageable_send(send: _MessageableSend) -> None:
    """Register the unpatched discord.py Messageable.send implementation."""
    global _ORIGINAL_MESSAGEABLE_SEND
    _ORIGINAL_MESSAGEABLE_SEND = send


def _component_type(item: discord.ui.Item[Any]) -> Optional[int]:
    try:
        data = item.to_component_dict()
        value = data.get("type")
        return int(value) if value is not None else None
    except Exception:
        return None


def _strip_v2_view_items(view: Optional[discord.ui.BaseView]) -> Optional[discord.ui.BaseView]:
    """Remove Components V2-only payloads from views sent with classic embeds."""
    if view is None:
        return None

    if isinstance(view, discord.ui.LayoutView):
        # LayoutView is itself a Components V2 payload, so do not attach it to
        # messages that are explicitly being sent as classic embed logs.
        return None

    children = list(getattr(view, "children", []) or [])
    for child in children:
        if _component_type(child) in {9, 10, 12, 13, 14, 17}:
            try:
                view.remove_item(child)
            except Exception:
                pass

    if not list(getattr(view, "children", []) or []):
        return None
    return view


def _strip_v2_only_kwargs(kwargs: dict[str, Any]) -> None:
    """Remove v2-specific kwargs/items that classic discord.py does not accept."""
    kwargs.pop("use_v2", None)
    kwargs.setdefault("allowed_mentions", discord.AllowedMentions.none())
    kwargs["view"] = _strip_v2_view_items(kwargs.get("view"))
    if kwargs.get("view", discord.utils.MISSING) is None:
        kwargs.pop("view", None)


async def send_classic_message(target: discord.abc.Messageable, *args: Any, **kwargs: Any) -> Any:
    """
    Send a message using classic Discord embeds.

    If a Components V2 shim has monkeypatched ``Messageable.send``, this uses the
    registered original send method so log embeds cannot be converted to V2.
    """
    kwargs = dict(kwargs)
    _strip_v2_only_kwargs(kwargs)
    send = _ORIGINAL_MESSAGEABLE_SEND
    if send is not None:
        return await send(target, *args, **kwargs)
    return await target.send(*args, **kwargs)
