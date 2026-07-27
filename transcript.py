import html
import io
import re
from collections import Counter
from datetime import datetime, timezone
from itertools import count
from pathlib import Path
from typing import Optional

import discord
from discord.components import (
    ActionRow as MessageActionRow,
    Button as MessageButton,
    Container as MessageContainer,
    MediaGalleryComponent,
    SectionComponent,
    SelectMenu as MessageSelectMenu,
    SeparatorComponent,
    TextDisplay as MessageTextDisplay,
    ThumbnailComponent,
)

_DEFAULT_AVATAR_URL = "https://cdn.discordapp.com/embed/avatars/0.png"
_BOT_TAG_HTML = """
<span class="chatlog__bot-tag">
    <svg class="chatlog__bot-tag-verified" height="16" viewBox="0 0 16 15.2">
        <path d="M7.4,11.17,4,8.62,5,7.26l2,1.53L10.64,4l1.36,1Z" fill="#ffffff"></path>
    </svg>
    <span>APP</span>
</span>
"""
_BUTTON_STYLE_COLOURS = {
    discord.ButtonStyle.primary: "#5865F2",
    discord.ButtonStyle.secondary: "#4F545C",
    discord.ButtonStyle.success: "#2D7D46",
    discord.ButtonStyle.danger: "#D83C3E",
    discord.ButtonStyle.link: "#4F545C",
}
_CODE_BLOCK_RE = re.compile(r"```(?:[^\n`]+\n)?(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_UNDERLINE_RE = re.compile(r"__(.+?)__", re.DOTALL)
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", re.DOTALL)
_CUSTOM_EMOJI_RE = re.compile(r"&lt;(a?):([A-Za-z0-9_]+):(\d+)&gt;")
_USER_MENTION_RE = re.compile(r"&lt;@!?(\d+)&gt;")
_ROLE_MENTION_RE = re.compile(r"&lt;@&(\d+)&gt;")
_CHANNEL_MENTION_RE = re.compile(r"&lt;#(\d+)&gt;")

_DEFAULT_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
    <title>{guild_name} - {channel_name}</title>
    <meta http-equiv="Content-Type" content="text/html; charset=utf-8">
    <meta name="viewport" content="width=device-width" />
    <meta name="title" content="{guild_name} - {channel_name}">
    <meta name="description" content="Transcript of channel {channel_name} ({channel_id}) from {guild_name} ({guild_id}) with {message_count} messages. This transcript was generated on {generated_at} (UTC).">
    <meta name="theme-color" content="#638dfc" />
    <style>
        body { background:#36393f; color:#fff; font-family: "gg sans", Helvetica, Arial, sans-serif; margin:0; }
        .panel { display:flex; align-items:center; gap:8px; padding:12px 16px; background:#2f3136; border-bottom:1px solid #202225; font-weight:700; }
        .main { padding: 12px 0 0 0; }
        .chatlog__message-container { display:grid; grid-template-columns: 72px 1fr; padding:6px 10px; }
        .chatlog__message-container:hover { background:#32353b; }
        .chatlog__message-aside { text-align:center; }
        .chatlog__avatar { width:40px; height:40px; border-radius:50%; }
        .chatlog__short-timestamp { color:#a3a6aa; font-size:0.72rem; margin-top:4px; }
        .chatlog__header { margin-bottom:2px; }
        .chatlog__author-name { font-weight:600; color:#fff; cursor:pointer; }
        .chatlog__timestamp { margin-left:0.3rem; color:#9599a2; font-size:0.75rem; }
        .chatlog__content { color:#dcddde; line-height:1.3; }
        .chatlog__attachment { margin-top:6px; }
        .chatlog__attachment-thumbnail { max-width:45vw; max-height:225px; border-radius:3px; }
        .chatlog__embed { display:flex; margin-top:6px; max-width:720px; }
        .chatlog__embed-color-pill { flex-shrink:0; width:0.25em; border-top-left-radius:3px; border-bottom-left-radius:3px; background:#4f545c; }
        .chatlog__embed-content-container { flex:1; padding:0.5em 0.6em; border:1px solid rgba(46, 48, 54, 0.6); border-top-right-radius:3px; border-bottom-right-radius:3px; background:rgba(46,48,54,.3); }
        .chatlog__embed-content { display:flex; width:100%; }
        .chatlog__embed-text { flex:1; min-width:0; }
        .chatlog__embed-author { display:flex; margin-bottom:0.3em; align-items:center; }
        .chatlog__embed-author-icon { margin-right:0.5em; width:20px; height:20px; border-radius:50%; }
        .chatlog__embed-author-name { font-size:0.875em; font-weight:600; color:#ffffff; }
        .chatlog__embed-title { font-weight:600; margin-bottom:2px; font-size:0.875em; color:#ffffff; }
        .chatlog__embed-description { color: rgba(255, 255, 255, 0.7); white-space: pre-wrap; font-size:0.85em; font-weight:500; }
        .chatlog__embed-fields { display:flex; flex-wrap:wrap; }
        .chatlog__embed-field { min-width:100%; max-width:506px; padding-top:0.6em; font-size:0.875em; }
        .chatlog__embed-field--inline { flex:1; flex-basis:auto; min-width:150px; }
        .chatlog__embed-field-name { margin-bottom:0.2em; font-weight:600; color:#ffffff; }
        .chatlog__embed-field-value { font-weight:500; color:rgba(255, 255, 255, 0.6); }
        .chatlog__embed-thumbnail { flex:0; margin-left:1.2em; max-width:80px; max-height:80px; border-radius:3px; }
        .chatlog__embed-image-container { margin-top:0.6em; }
        .chatlog__embed-image { max-width:500px; max-height:400px; border-radius:3px; }
        .chatlog__embed-footer { margin-top:0.6em; color:rgba(255, 255, 255, 0.6); }
        .chatlog__embed-footer-icon { margin-right:0.2em; width:20px; height:20px; border-radius:50%; vertical-align:middle; }
        .chatlog__embed-footer-text { font-size:0.75em; font-weight:500; }
        .chatlog__divider { margin: 14px 12px; border-top:1px solid rgba(255,255,255,.18); text-align:center; }
        .chatlog__divider span { position:relative; top:-11px; background:#36393f; color:#ed4245; font-weight:700; padding:0 10px; }
        .chatlog__components { display:flex; flex-wrap:wrap; }
        .chatlog__component-button { display:flex; align-items:center; margin:0.35em 0.1em 0.1em 0.1em; padding:0.2em 0.35em; border-radius:2px; }
        .chatlog__button-label { min-width:9px; margin-left:0.35em; margin-right:0.35em; font-size:0.875em; color:white; font-weight:500; }
        .chatlog__component-dropdown { width:min(400px, 100%); margin-top:5px; position:relative; display:inline-block; }
        .chatlog__container-block { margin-top:0.55em; }
        .chatlog__container-block:first-child { margin-top:0; }
        .chatlog__container-separator { margin:0.8em 0; border-top:1px solid rgba(255,255,255,0.12); }
        .chatlog__text-display { font-size:0.95em; color:#fff; }
        .dropdownButton { width:100%; color:#6E767D; padding:11.5px; font-size:15px; cursor:pointer; text-align:left; border-radius:5px; background-color:#2F3136; border:1px solid #202225; }
        .dropdownContent { z-index:1; display:none; width:99.5%; font-size:14px; position:absolute; margin-top:-0.7px; background-color:#2F3136; border:1px solid #202225; box-shadow:0px 8px 16px 0px rgba(0,0,0,0.2); }
        .chatlog__component-dropdown-show { display:block; }
        .dropdownContentTitle { color:white; font-weight:600; }
        .dropdownContentDesc { color:#999B9E; }
        .dropdownContent a { display:block; padding:12px 16px; text-decoration:none; }
        .dropdownContent a:hover { background-color:#292B2F; }
        .chatlog__component-disabled { cursor:not-allowed; opacity:0.6; }
        .emoji { width:1.25em; height:1.25em; margin:0 0.06em; vertical-align:-0.1em; }
        .emoji--small { width:1em; height:1em; }
        .mention { border-radius:3px; padding:0 2px; color:#dee0fc; background:rgba(88, 101, 242, .3); font-weight:500; }
        .pre { background:#2f3136; border-radius:3px; font-family:"Consolas", "Courier New", Courier, monospace; }
        .pre--inline { padding:2px; font-size:0.85em; }
        .pre--multiline { margin-top:0.25em; padding:0.5em; border:2px solid #282b30; color:#b9bbbe; white-space:pre-wrap; }
        .footer { margin: 14px 16px 16px; padding: 12px; background:#202225; border-radius:6px; color:#b9bbbe; }
        .meta-popout { display:none; }
    </style>
</head>
<body>
<div class="panel">
    <span>{channel_name}</span>
</div>
<div class="main">
    <div class="chatlog">
        {messages}
    </div>
</div>
<div class="footer">
    This transcript was generated on {generated_at} (UTC)
</div>
{user_popouts}
</body>
</html>
"""

_template_path = Path(__file__).with_name("transcript_template.html")
try:
    HTML_TEMPLATE = _template_path.read_text(encoding="utf-8")
except Exception:
    HTML_TEMPLATE = _DEFAULT_TEMPLATE


def _fmt_utc_footer(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%d %B %Y at %H:%M:%S")


def _fmt_utc_meta(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%b %d, %Y (%H:%M:%S)")


def _escape_attr(value: str) -> str:
    return html.escape(value, quote=True)


def _avatar_url(user: discord.abc.User) -> str:
    return str(
        getattr(getattr(user, "display_avatar", None), "url", _DEFAULT_AVATAR_URL)
    )


def _colour_to_rgba(colour: Optional[discord.Colour]) -> Optional[str]:
    if colour is None:
        return None
    return f"rgba({colour.r}, {colour.g}, {colour.b}, 1)"


def _format_file_size(size: int) -> str:
    units = ["B", "KB", "MB", "GB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{int(size)} B"


def _stash_placeholder(placeholders: dict[str, str], value: str) -> str:
    key = f"\u0000TRANSCRIPT_PLACEHOLDER_{len(placeholders)}\u0000"
    placeholders[key] = value
    return key


def _render_user_mention(guild: Optional[discord.Guild], user_id: int) -> str:
    member = guild.get_member(user_id) if guild else None
    label = f"@{member.display_name}" if member is not None else f"@{user_id}"
    return f'<span class="mention" title="{user_id}">{html.escape(label)}</span>'


def _render_role_mention(guild: Optional[discord.Guild], role_id: int) -> str:
    role = guild.get_role(role_id) if guild else None
    label = f"@{role.name}" if role is not None else f"@{role_id}"
    return f'<span class="mention" title="{role_id}">{html.escape(label)}</span>'


def _render_channel_mention(guild: Optional[discord.Guild], channel_id: int) -> str:
    channel = None
    if guild is not None:
        getter = getattr(guild, "get_channel_or_thread", None)
        channel = (
            getter(channel_id) if callable(getter) else guild.get_channel(channel_id)
        )
    label = f"#{channel.name}" if channel is not None else f"#{channel_id}"
    return f'<span class="mention" title="{channel_id}">{html.escape(label)}</span>'


def _replace_custom_emoji(match: re.Match[str]) -> str:
    animated = match.group(1) == "a"
    name = match.group(2)
    emoji_id = match.group(3)
    extension = "gif" if animated else "png"
    url = f"https://cdn.discordapp.com/emojis/{emoji_id}.{extension}"
    return f'<img class="emoji emoji--small" src="{url}" alt=":{html.escape(name)}:">'


def _render_markdownish(text: str, guild: Optional[discord.Guild] = None) -> str:
    if not text:
        return ""

    rendered = html.escape(text)
    placeholders: dict[str, str] = {}

    rendered = _CODE_BLOCK_RE.sub(
        lambda match: _stash_placeholder(
            placeholders,
            f'<div class="pre pre--multiline">{match.group(1)}</div>',
        ),
        rendered,
    )
    rendered = _INLINE_CODE_RE.sub(
        lambda match: _stash_placeholder(
            placeholders,
            f'<span class="pre pre--inline">{match.group(1)}</span>',
        ),
        rendered,
    )
    rendered = _CUSTOM_EMOJI_RE.sub(_replace_custom_emoji, rendered)
    rendered = _USER_MENTION_RE.sub(
        lambda match: _render_user_mention(guild, int(match.group(1))), rendered
    )
    rendered = _ROLE_MENTION_RE.sub(
        lambda match: _render_role_mention(guild, int(match.group(1))), rendered
    )
    rendered = _CHANNEL_MENTION_RE.sub(
        lambda match: _render_channel_mention(guild, int(match.group(1))), rendered
    )
    rendered = _UNDERLINE_RE.sub(r"<u>\1</u>", rendered)
    rendered = _BOLD_RE.sub(r"<strong>\1</strong>", rendered)
    rendered = _ITALIC_RE.sub(r"<em>\1</em>", rendered)
    rendered = rendered.replace("\n", "<br>")

    for key, value in placeholders.items():
        rendered = rendered.replace(key, value)

    return rendered


def _render_emoji(emoji: object) -> str:
    emoji_id = getattr(emoji, "id", None)
    emoji_name = getattr(emoji, "name", None) or str(emoji)
    if emoji_id:
        extension = "gif" if getattr(emoji, "animated", False) else "png"
        url = f"https://cdn.discordapp.com/emojis/{emoji_id}.{extension}"
        return f'<img class="emoji emoji--small" src="{url}" alt=":{html.escape(str(emoji_name))}:">'
    return html.escape(str(emoji_name))


def _render_embed(embed: discord.Embed, guild: Optional[discord.Guild]) -> str:
    has_content = any(
        (
            embed.title,
            embed.description,
            embed.fields,
            getattr(embed.author, "name", None),
            getattr(embed.footer, "text", None),
            getattr(embed.thumbnail, "url", None),
            getattr(embed.image, "url", None),
        )
    )
    if not has_content:
        return ""

    colour_style = _colour_to_rgba(embed.colour)
    colour_pill = (
        f'<div class="chatlog__embed-color-pill" style="background-color:{colour_style}"></div>'
        if colour_style
        else '<div class="chatlog__embed-color-pill chatlog__embed-color-pill--default"></div>'
    )

    author_name = getattr(embed.author, "name", None)
    author_icon_url = str(getattr(embed.author, "icon_url", "") or "")
    author_url = str(getattr(embed.author, "url", "") or "")
    author_html = ""
    if author_name:
        author_name_html = html.escape(author_name)
        if author_url:
            author_name_html = (
                f'<a class="chatlog__embed-author-name chatlog__embed-author-name-link" '
                f'href="{_escape_attr(author_url)}" target="_blank" rel="noopener noreferrer">{author_name_html}</a>'
            )
        else:
            author_name_html = (
                f'<span class="chatlog__embed-author-name">{author_name_html}</span>'
            )

        icon_html = ""
        if author_icon_url:
            icon_html = f'<img class="chatlog__embed-author-icon" src="{_escape_attr(author_icon_url)}" alt="Author Icon">'

        author_html = (
            f'<div class="chatlog__embed-author">{icon_html}{author_name_html}</div>'
        )

    title_html = ""
    if embed.title:
        title_text = html.escape(embed.title)
        embed_url = str(embed.url or "")
        if embed_url:
            title_html = (
                f'<a class="chatlog__embed-title" href="{_escape_attr(embed_url)}" '
                f'target="_blank" rel="noopener noreferrer">{title_text}</a>'
            )
        else:
            title_html = f'<div class="chatlog__embed-title">{title_text}</div>'

    description_html = ""
    if embed.description:
        description_html = (
            '<div class="chatlog__embed-description">'
            f'<div class="markdown preserve-whitespace">{_render_markdownish(embed.description, guild)}</div>'
            "</div>"
        )

    fields_html = ""
    if embed.fields:
        field_blocks: list[str] = []
        for field in embed.fields:
            inline_class = " chatlog__embed-field--inline" if field.inline else ""
            field_blocks.append(
                '<div class="chatlog__embed-field{inline_class}">'
                '<div class="chatlog__embed-field-name"><div class="markdown preserve-whitespace">{name}</div></div>'
                '<div class="chatlog__embed-field-value"><div class="markdown preserve-whitespace">{value}</div></div>'
                "</div>".format(
                    inline_class=inline_class,
                    name=_render_markdownish(field.name, guild),
                    value=_render_markdownish(field.value, guild),
                )
            )
        fields_html = (
            f'<div class="chatlog__embed-fields">{"".join(field_blocks)}</div>'
        )

    thumbnail_html = ""
    thumbnail_url = str(getattr(embed.thumbnail, "url", "") or "")
    if thumbnail_url:
        thumbnail_html = f'<img class="chatlog__embed-thumbnail" src="{_escape_attr(thumbnail_url)}" alt="Embed Thumbnail">'

    image_html = ""
    image_url = str(getattr(embed.image, "url", "") or "")
    if image_url:
        image_html = (
            '<div class="chatlog__embed-image-container">'
            f'<a href="{_escape_attr(image_url)}" target="_blank" rel="noopener noreferrer">'
            f'<img class="chatlog__embed-image" src="{_escape_attr(image_url)}" alt="Embed Image">'
            "</a>"
            "</div>"
        )

    footer_html = ""
    footer_text = getattr(embed.footer, "text", None)
    footer_icon_url = str(getattr(embed.footer, "icon_url", "") or "")
    if footer_text:
        footer_icon_html = ""
        if footer_icon_url:
            footer_icon_html = f'<img class="chatlog__embed-footer-icon" src="{_escape_attr(footer_icon_url)}" alt="Footer Icon">'
        footer_html = (
            '<div class="chatlog__embed-footer">'
            f'{footer_icon_html}<span class="chatlog__embed-footer-text">{html.escape(footer_text)}</span>'
            "</div>"
        )

    return (
        '<div class="chatlog__embed">'
        f"{colour_pill}"
        '<div class="chatlog__embed-content-container">'
        '<div class="chatlog__embed-content">'
        '<div class="chatlog__embed-text">'
        f"{author_html}{title_html}{description_html}{fields_html}{image_html}{footer_html}"
        "</div>"
        f"{thumbnail_html}"
        "</div>"
        "</div>"
        "</div>"
    )


def _render_attachments(msg: discord.Message) -> str:
    chunks: list[str] = []
    for attachment in msg.attachments:
        safe_name = html.escape(attachment.filename)
        safe_url = _escape_attr(attachment.url)
        content_type = attachment.content_type or ""

        if content_type.startswith("image/"):
            chunks.append(
                '<div class="chatlog__attachment">'
                f'<a href="{safe_url}" target="_blank" rel="noopener noreferrer">'
                f'<img class="chatlog__attachment-thumbnail" src="{safe_url}" alt="{safe_name}">'
                "</a>"
                "</div>"
            )
            continue

        if content_type.startswith("audio/"):
            chunks.append(
                '<div class="chatlog__attachment">'
                '<div class="chatlog__attachment-audio-container">'
                f'<div class="chatlog__attachment-filename"><a href="{safe_url}" target="_blank" rel="noopener noreferrer">{safe_name}</a></div>'
                f'<div class="chatlog__attachment-filesize">{_format_file_size(attachment.size)}</div>'
                f'<audio controls src="{safe_url}"></audio>'
                "</div>"
                "</div>"
            )
            continue

        chunks.append(
            '<div class="chatlog__attachment">'
            '<div class="chatlog__attachment-container">'
            f'<div class="chatlog__attachment-filename"><a href="{safe_url}" target="_blank" rel="noopener noreferrer">{safe_name}</a></div>'
            f'<div class="chatlog__attachment-filesize">{_format_file_size(attachment.size)}</div>'
            "</div>"
            "</div>"
        )
    return "".join(chunks)


def _render_text_display(
    display: MessageTextDisplay, guild: Optional[discord.Guild]
) -> str:
    return (
        '<div class="chatlog__container-block chatlog__text-display">'
        f'<div class="markdown preserve-whitespace">{_render_markdownish(display.content, guild)}</div>'
        "</div>"
    )


def _render_thumbnail(thumbnail: ThumbnailComponent) -> str:
    url = str(
        getattr(thumbnail.media, "url", "")
        or getattr(thumbnail.media, "proxy_url", "")
        or _DEFAULT_AVATAR_URL
    )
    description = html.escape(thumbnail.description or "Thumbnail")
    return f'<img class="chatlog__embed-thumbnail" src="{_escape_attr(url)}" alt="{description}">'


def _render_media_gallery(component: MediaGalleryComponent) -> str:
    chunks: list[str] = []
    for item in component.items:
        url = str(
            getattr(item.media, "url", "") or getattr(item.media, "proxy_url", "") or ""
        )
        if not url:
            continue

        description = html.escape(item.description or "Media")
        chunks.append(
            '<div class="chatlog__container-block">'
            '<div class="chatlog__embed-image-container">'
            f'<a href="{_escape_attr(url)}" target="_blank" rel="noopener noreferrer">'
            f'<img class="chatlog__embed-image" src="{_escape_attr(url)}" alt="{description}">'
            "</a>"
            "</div>"
            "</div>"
        )
    return "".join(chunks)


def _render_button(button: MessageButton) -> str:
    style = _BUTTON_STYLE_COLOURS.get(button.style, "#4F545C")
    disabled_class = " chatlog__component-disabled" if button.disabled else ""
    url = _escape_attr(button.url) if button.url else "javascript:;"
    label_parts: list[str] = []
    if button.emoji:
        label_parts.append(_render_emoji(button.emoji))
    if button.label:
        label_parts.append(html.escape(button.label))
    label = "".join(label_parts) or "Button"

    return (
        f'<div class="chatlog__component-button{disabled_class}" style="background-color:{style}">'
        f'<a href="{url}" style="text-decoration:none">'
        f'<span class="chatlog__button-label">{label}</span>'
        "</a>"
        "</div>"
    )


def _render_select(select: MessageSelectMenu, dropdown_counter: count) -> str:
    dropdown_id = str(select.id or next(dropdown_counter))
    disabled_attr = " disabled" if select.disabled else ""
    disabled_class = " chatlog__component-disabled" if select.disabled else ""
    placeholder = html.escape(select.placeholder or "Select an option...")

    option_blocks: list[str] = []
    for option in getattr(select, "options", []) or []:
        emoji_html = _render_emoji(option.emoji) if option.emoji else ""
        emoji_block = (
            f'<span class="dropdownContentEmoji">{emoji_html}</span>'
            if emoji_html
            else ""
        )
        description_html = (
            f'<div class="dropdownContentDesc">{html.escape(option.description)}</div>'
            if option.description
            else ""
        )
        option_blocks.append(
            '<a href="javascript:;">'
            f"{emoji_block}"
            '<div class="chatlog__dropdown-text">'
            f'<div class="dropdownContentTitle">{html.escape(option.label)}</div>'
            f"{description_html}"
            "</div>"
            "</a>"
        )

    return (
        f'<div class="chatlog__component-dropdown{disabled_class}">'
        f'<button class="dropdownButton" id="dropdownButton{dropdown_id}" type="button" onclick="showDropdown(\'{dropdown_id}\')"{disabled_attr}>'
        f'<span class="chatlog__dropdown-text">{placeholder}</span>'
        '<span class="chatlog__dropdown-icon">&#9662;</span>'
        "</button>"
        f'<div id="dropdownMenuContent{dropdown_id}" class="dropdownContent">'
        f'<div id="dropdownMenu{dropdown_id}" class="chatlog__dropdown-content">{"".join(option_blocks)}</div>'
        "</div>"
        "</div>"
    )


def _render_action_row(
    action_row: MessageActionRow,
    guild: Optional[discord.Guild],
    dropdown_counter: count,
) -> str:
    children_html = "".join(
        _render_component(child, guild, dropdown_counter)
        for child in action_row.children
    )
    if not children_html:
        return ""
    return f'<div class="chatlog__container-block"><div class="chatlog__components">{children_html}</div></div>'


def _render_section(
    section: SectionComponent, guild: Optional[discord.Guild], dropdown_counter: count
) -> str:
    children_html = "".join(
        _render_component(child, guild, dropdown_counter) for child in section.children
    )
    accessory_html = (
        _render_component(section.accessory, guild, dropdown_counter)
        if section.accessory is not None
        else ""
    )
    return (
        '<div class="chatlog__container-block">'
        '<div class="chatlog__embed-content chatlog__transcript-section">'
        f'<div class="chatlog__embed-text">{children_html}</div>'
        f"{accessory_html}"
        "</div>"
        "</div>"
    )


def _render_container(
    container: MessageContainer, guild: Optional[discord.Guild], dropdown_counter: count
) -> str:
    children_html = "".join(
        _render_component(child, guild, dropdown_counter)
        for child in container.children
    )
    if not children_html:
        return ""

    accent_style = _colour_to_rgba(getattr(container, "accent_color", None))
    colour_pill = (
        f'<div class="chatlog__embed-color-pill" style="background-color:{accent_style}"></div>'
        if accent_style
        else '<div class="chatlog__embed-color-pill chatlog__embed-color-pill--default"></div>'
    )

    return (
        '<div class="chatlog__embed chatlog__container-card">'
        f"{colour_pill}"
        '<div class="chatlog__embed-content-container">'
        f'<div class="chatlog__container-body">{children_html}</div>'
        "</div>"
        "</div>"
    )


def _render_component(
    component: object, guild: Optional[discord.Guild], dropdown_counter: count
) -> str:
    if isinstance(component, MessageContainer):
        return _render_container(component, guild, dropdown_counter)
    if isinstance(component, MessageActionRow):
        return _render_action_row(component, guild, dropdown_counter)
    if isinstance(component, MessageButton):
        return _render_button(component)
    if isinstance(component, MessageSelectMenu):
        return _render_select(component, dropdown_counter)
    if isinstance(component, SectionComponent):
        return _render_section(component, guild, dropdown_counter)
    if isinstance(component, MessageTextDisplay):
        return _render_text_display(component, guild)
    if isinstance(component, ThumbnailComponent):
        return _render_thumbnail(component)
    if isinstance(component, MediaGalleryComponent):
        return _render_media_gallery(component)
    if isinstance(component, SeparatorComponent):
        return (
            '<div class="chatlog__container-separator"></div>'
            if component.visible
            else ""
        )
    return ""


def _render_message(
    msg: discord.Message, guild: discord.Guild, dropdown_counter: count
) -> str:
    author = msg.author
    avatar_url = _avatar_url(author)
    display_name = html.escape(
        getattr(author, "display_name", getattr(author, "name", str(author)))
    )
    author_tag = html.escape(str(author))
    user_id = getattr(author, "id", 0)

    author_color = "#ffffff"
    if isinstance(author, discord.Member) and author.color != discord.Color.default():
        author_color = str(author.color)

    raw_content = msg.system_content or msg.content or ""
    content_html = _render_markdownish(raw_content, guild) if raw_content else ""
    attachments_html = _render_attachments(msg)
    embeds_html = "".join(_render_embed(embed, guild) for embed in msg.embeds)
    components_html = "".join(
        _render_component(component, guild, dropdown_counter)
        for component in msg.components
    )

    if not any((content_html, attachments_html, embeds_html, components_html)):
        content_html = "<em>*No content*</em>"

    timestamp_full = msg.created_at.astimezone(timezone.utc).strftime(
        "%A, %d %B %Y %H:%M"
    )
    header_timestamp = msg.created_at.astimezone(timezone.utc).strftime(
        "%d-%m-%Y %H:%M"
    )
    edited_html = ""
    if msg.edited_at is not None:
        edited_html = (
            f'<span class="chatlog__reference-edited-timestamp" '
            f'data-timestamp="{msg.edited_at.astimezone(timezone.utc).strftime("%A, %d %B %Y %H:%M")}">(edited)</span>'
        )

    bot_tag_html = _BOT_TAG_HTML if getattr(author, "bot", False) else ""
    content_block = f'<div class="markup">{content_html}</div>' if content_html else ""

    return f"""
<div class="chatlog__message-group">
    <div class="chatlog__message-container" id="chatlog__message-container-{msg.id}" data-message-id="{msg.id}">
        <div class="chatlog__message">
            <div class="chatlog__message-aside">
                <img class="chatlog__avatar" src="{_escape_attr(avatar_url)}" alt="Avatar" data-user-id="{user_id}">
            </div>
            <div class="chatlog__message-primary">
                <div class="chatlog__header">
                    <span class="chatlog__author-name" data-user-id="{user_id}" title="{author_tag}" style="color:{author_color};">{display_name}</span>
                    {bot_tag_html}
                    <span class="chatlog__timestamp" data-timestamp="{timestamp_full}">{header_timestamp}</span>
                    {edited_html}
                </div>
                <div class="chatlog__content chatlog__markdown" data-message-id="{msg.id}" id="message-{msg.id}">
                    {content_block}
                    {attachments_html}
                    {embeds_html}
                    {components_html}
                </div>
            </div>
        </div>
    </div>
</div>
"""


def _render_user_popouts(
    guild: discord.Guild,
    participants: dict[int, discord.abc.User],
    message_counts: Counter[int],
) -> str:
    popouts: list[str] = []
    for user_id, user in participants.items():
        avatar_url = _avatar_url(user)
        display_name = html.escape(
            getattr(user, "display_name", getattr(user, "name", str(user)))
        )
        username = html.escape(getattr(user, "name", str(user)))
        discriminator = getattr(user, "discriminator", "")
        discriminator_html = (
            f'<div class="meta__discriminator">#{html.escape(discriminator)}</div>'
            if discriminator and discriminator != "0"
            else ""
        )
        bot_tag_html = _BOT_TAG_HTML if getattr(user, "bot", False) else ""

        member_since_lines: list[str] = []
        created_at = getattr(user, "created_at", None)
        if created_at:
            member_since_lines.append(f"Discord: {_fmt_utc_meta(created_at)}")
        joined_at = getattr(user, "joined_at", None)
        if joined_at:
            member_since_lines.append(f"{guild.name}: {_fmt_utc_meta(joined_at)}")
        if not member_since_lines:
            member_since_lines.append("Unavailable")

        member_since_html = "<br>".join(
            html.escape(value) for value in member_since_lines
        )

        popouts.append(
            f"""
<div id="meta-popout-{user_id}" class="meta-popout">
    <div class="meta__header">
         <img src="{_escape_attr(avatar_url)}" alt="Avatar">
    </div>
    <div class="meta__description">
        <div class="meta__display-name">{display_name}</div>
        <div class="meta__details">
            <div class="meta__user">{username}</div>
            {discriminator_html}
            {bot_tag_html}
        </div>
        <div class="meta__divider-2"></div>
        <div class="meta__field">
            <div class="meta__title">Member Since</div>
            <div class="meta__value">{member_since_html}</div>
        </div>
        <div class="meta__field">
            <div class="meta__title">Member ID</div>
            <div class="meta__value">{user_id}</div>
        </div>
        <div class="meta__field">
            <div class="meta__title">Message Count</div>
            <div class="meta__value">{message_counts.get(user_id, 0)}</div>
        </div>
    </div>
</div>
"""
        )
    return "".join(popouts)


def generate_html_transcript(
    guild: discord.Guild,
    channel: discord.TextChannel,
    messages: list[discord.Message],
    purged_messages: Optional[list[discord.Message]] = None,
) -> io.BytesIO:
    sorted_context = sorted(messages, key=lambda message: message.created_at)
    sorted_purged = sorted(
        purged_messages or [], key=lambda message: message.created_at
    )
    all_messages = [*sorted_context, *sorted_purged]

    participants: dict[int, discord.abc.User] = {}
    message_counts: Counter[int] = Counter()
    for message in all_messages:
        participants.setdefault(message.author.id, message.author)
        message_counts[message.author.id] += 1

    dropdown_counter = count(1)
    rendered: list[str] = []
    for msg in sorted_context:
        rendered.append(_render_message(msg, guild, dropdown_counter))

    if sorted_context and sorted_purged:
        rendered.append(
            """
<div class="chatlog__divider">
    <span>Purged Messages</span>
</div>
"""
        )

    for msg in sorted_purged:
        rendered.append(_render_message(msg, guild, dropdown_counter))

    now = datetime.now(timezone.utc)
    generated_at = _fmt_utc_footer(now)
    channel_created = _fmt_utc_meta(channel.created_at)
    guild_icon = (
        str(guild.icon.url) if getattr(guild, "icon", None) else _DEFAULT_AVATAR_URL
    )

    html_out = (
        HTML_TEMPLATE.replace("{channel_name}", html.escape(channel.name))
        .replace("{channel_id}", str(channel.id))
        .replace("{guild_name}", html.escape(guild.name))
        .replace("{guild_id}", str(guild.id))
        .replace("{message_count}", str(len(all_messages)))
        .replace("{generated_at}", generated_at)
        .replace("{guild_icon}", guild_icon)
        .replace("{created_at}", channel_created)
        .replace("{participant_count}", str(len(participants)))
        .replace("{messages}", "".join(rendered))
        .replace(
            "{user_popouts}", _render_user_popouts(guild, participants, message_counts)
        )
    )

    return io.BytesIO(html_out.encode("utf-8"))


class EphemeralTranscriptView(discord.ui.View):
    def __init__(self, transcript_data: io.BytesIO, filename: str = "transcript.html"):
        super().__init__(timeout=3600)
        self.data_bytes = transcript_data.getvalue()
        self.filename = filename

    @discord.ui.button(label="Download Transcript", style=discord.ButtonStyle.secondary)
    async def download(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        file_buffer = io.BytesIO(self.data_bytes)
        file_buffer.seek(0)
        await interaction.response.send_message(
            file=discord.File(file_buffer, filename=self.filename),
            ephemeral=True,
        )
