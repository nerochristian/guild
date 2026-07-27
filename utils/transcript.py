import html
import io
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import discord

# Uses the same placeholder shape requested by the user template:
# {channel_name} {channel_id} {guild_name} {guild_id} {message_count}
# {generated_at} {messages} {guild_icon} {created_at} {participant_count} {user_popouts}
_DEFAULT_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
    <title>Ass mod - {channel_name}</title>
    <meta http-equiv="Content-Type" content="text/html; charset=utf-8">
    <meta name="viewport" content="width=device-width" />
    <meta name="title" content="Ass mod - {channel_name}">
    <meta name="description" content="Transcript of channel {channel_name} ({channel_id}) from Ass mod ({guild_id}) with {message_count} messages. This transcript was generated on {generated_at} (UTC).">
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
        .chatlog__embed { margin-top:6px; border-left:4px solid #4f545c; background:rgba(46,48,54,.4); padding:6px 8px; border-radius:3px; }
        .chatlog__embed-title { font-weight:600; margin-bottom:2px; }
        .chatlog__embed-description { color: rgba(255, 255, 255, 0.7); white-space: pre-wrap; }
        .chatlog__divider { margin: 14px 12px; border-top:1px solid rgba(255,255,255,.18); text-align:center; }
        .chatlog__divider span { position:relative; top:-11px; background:#36393f; color:#ed4245; font-weight:700; padding:0 10px; }
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

TRANSCRIPT_STORAGE_DIR = Path(
    os.getenv("TRANSCRIPT_STORAGE_DIR", str(Path.cwd() / "data" / "transcripts"))
)


def _fmt_utc_footer(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%d %B %Y at %H:%M:%S")


def _fmt_utc_meta(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%b %d, %Y (%H:%M:%S)")


def _escape_content(content: str) -> str:
    return html.escape(content).replace("\n", "<br>")


def _asset_url(asset: object) -> str:
    return html.escape(str(getattr(asset, "url", "") or getattr(asset, "proxy_url", "") or ""), quote=True)


def _safe_href(url: str) -> str:
    url = str(url or "").strip()
    if not url.startswith(("http://", "https://")):
        return ""
    return html.escape(url, quote=True)


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]+", "-", name or "transcript.html").strip(".-")
    if not cleaned:
        cleaned = "transcript.html"
    if not cleaned.lower().endswith(".html"):
        cleaned += ".html"
    return cleaned[:90]


def _public_transcript_base_url() -> str:
    base = (
        os.getenv("TRANSCRIPT_PUBLIC_BASE_URL")
        or os.getenv("DASHBOARD_PUBLIC_URL")
        or os.getenv("RENDER_EXTERNAL_URL")
        or ""
    ).strip().rstrip("/")
    return base


def publish_transcript(transcript_data: bytes, filename: str) -> Optional[str]:
    base_url = _public_transcript_base_url()
    if not base_url:
        return None

    TRANSCRIPT_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = _safe_filename(filename)
    stem = safe_name[:-5]
    public_name = f"{stem}-{secrets.token_urlsafe(12)}.html"
    path = TRANSCRIPT_STORAGE_DIR / public_name
    path.write_bytes(transcript_data)
    return f"{base_url}/transcripts/{public_name}"


def _render_embed(embed: discord.Embed) -> str:
    if not (
        embed.title
        or embed.description
        or embed.fields
        or embed.image
        or embed.thumbnail
        or embed.author
        or embed.footer
    ):
        return ""
    title = html.escape(embed.title) if embed.title else ""
    title_url = _safe_href(embed.url) if embed.url else ""
    description = _escape_content(embed.description) if embed.description else ""
    author_name = html.escape(str(embed.author.name)) if embed.author and embed.author.name else ""
    author_icon = _asset_url(embed.author.icon_url) if embed.author and embed.author.icon_url else ""
    thumbnail_url = _asset_url(embed.thumbnail)
    image_url = _asset_url(embed.image)
    footer_text = html.escape(str(embed.footer.text)) if embed.footer and embed.footer.text else ""
    footer_icon = _asset_url(embed.footer.icon_url) if embed.footer and embed.footer.icon_url else ""
    color = str(embed.color) if getattr(embed, "color", None) else "#4f545c"
    fields = []
    for field in embed.fields:
        inline = " chatlog__embed-field--inline" if field.inline else ""
        fields.append(
            f'<div class="chatlog__embed-field{inline}">'
            f'<div class="chatlog__embed-field-name">{html.escape(str(field.name))}</div>'
            f'<div class="chatlog__embed-field-value">{_escape_content(str(field.value))}</div>'
            "</div>"
        )
    author_html = ""
    if author_name:
        icon_html = f'<img class="chatlog__embed-author-icon" src="{author_icon}" alt="">' if author_icon else ""
        author_html = f'<div class="chatlog__embed-author">{icon_html}<span class="chatlog__embed-author-name">{author_name}</span></div>'
    title_html = ""
    if title:
        if title_url:
            title_html = f'<a class="chatlog__embed-title" href="{title_url}" target="_blank" rel="noopener noreferrer">{title}</a>'
        else:
            title_html = f'<div class="chatlog__embed-title">{title}</div>'
    thumbnail_html = (
        f'<a href="{thumbnail_url}" target="_blank" rel="noopener noreferrer">'
        f'<img class="chatlog__embed-thumbnail" src="{thumbnail_url}" alt="Embed thumbnail"></a>'
        if thumbnail_url else ""
    )
    image_html = (
        f'<div class="chatlog__embed-image-container"><a href="{image_url}" target="_blank" rel="noopener noreferrer">'
        f'<img class="chatlog__embed-image" src="{image_url}" alt="Embed image"></a></div>'
        if image_url else ""
    )
    footer_html = ""
    if footer_text:
        icon_html = f'<img class="chatlog__embed-footer-icon" src="{footer_icon}" alt="">' if footer_icon else ""
        footer_html = f'<div class="chatlog__embed-footer">{icon_html}<span class="chatlog__embed-footer-text">{footer_text}</span></div>'
    return (
        '<div class="chatlog__embed">'
        f'<div class="chatlog__embed-color-pill" style="background-color:{color};"></div>'
        f'<div class="chatlog__embed-content-container" style="border-color:{color};">'
        '<div class="chatlog__embed-content">'
        '<div class="chatlog__embed-text">'
        f"{author_html}"
        f"{title_html}"
        f'<div class="chatlog__embed-description">{description}</div>'
        f'<div class="chatlog__embed-fields">{"".join(fields)}</div>'
        "</div>"
        f"{thumbnail_html}"
        "</div>"
        f"{image_html}"
        f"{footer_html}"
        "</div>"
        "</div>"
    )


def _render_attachments(msg: discord.Message) -> str:
    chunks: list[str] = []
    for attachment in msg.attachments:
        safe_name = html.escape(attachment.filename)
        is_image = bool(attachment.content_type and attachment.content_type.startswith("image/"))
        url = _safe_href(getattr(attachment, "url", "") or getattr(attachment, "proxy_url", ""))
        if not url:
            continue
        if is_image:
            chunks.append(
                '<div class="chatlog__attachment">'
                f'<a href="{url}" target="_blank" rel="noopener noreferrer">'
                f'<img class="chatlog__attachment-thumbnail" src="{url}" alt="{safe_name}">'
                "</a>"
                "</div>"
            )
        else:
            chunks.append(
                '<div class="chatlog__attachment">'
                f'<a href="{url}" target="_blank" rel="noopener noreferrer">{safe_name}</a>'
                "</div>"
            )
    return "".join(chunks)


def _render_stickers(msg: discord.Message) -> str:
    chunks: list[str] = []
    for sticker in getattr(msg, "stickers", []) or []:
        url = _safe_href(getattr(sticker, "url", ""))
        name = html.escape(getattr(sticker, "name", "sticker"))
        if url:
            chunks.append(
                '<div class="chatlog__attachment">'
                f'<a href="{url}" target="_blank" rel="noopener noreferrer">'
                f'<img class="chatlog__attachment-thumbnail" src="{url}" alt="{name}">'
                "</a>"
                "</div>"
            )
        else:
            chunks.append(f'<div class="chatlog__attachment">Sticker: {name}</div>')
    return "".join(chunks)


def _render_message(msg: discord.Message) -> str:
    author = msg.author
    avatar_url = getattr(getattr(author, "display_avatar", None), "url", "https://cdn.discordapp.com/embed/avatars/0.png")
    display_name = html.escape(getattr(author, "display_name", str(author)))
    author_tag = html.escape(str(author))
    user_id = getattr(author, "id", 0)

    author_color = "#ffffff"
    try:
        if isinstance(author, discord.Member) and author.color != discord.Color.default():
            author_color = str(author.color)
    except Exception:
        pass

    content = _escape_content(msg.content) if msg.content else "<i>*No content*</i>"
    timestamp_full = msg.created_at.astimezone(timezone.utc).strftime("%A, %d %B %Y %H:%M")
    header_timestamp = msg.created_at.astimezone(timezone.utc).strftime("%d-%m-%Y %H:%M")

    embeds_html = "".join(_render_embed(embed) for embed in msg.embeds)
    attachments_html = _render_attachments(msg)
    stickers_html = _render_stickers(msg)

    return f"""
<div class="chatlog__message-group">
    <div class="chatlog__message-container" id="chatlog__message-container-{msg.id}" data-message-id="{msg.id}">
        <div class="chatlog__message">
            <div class="chatlog__message-aside">
                <img class="chatlog__avatar" src="{avatar_url}" alt="Avatar" data-user-id="{user_id}">
            </div>
            <div class="chatlog__message-primary">
                <div class="chatlog__header">
                    <span class="chatlog__author-name" data-user-id="{user_id}" title="{author_tag}" style="color:{author_color};">{display_name}</span>
                    <span class="chatlog__timestamp" data-timestamp="{timestamp_full}">{header_timestamp}</span>
                </div>
                <div class="chatlog__content chatlog__markdown" data-message-id="{msg.id}" id="message-{msg.id}">
                    <span class="chatlog__markdown-preserve">{content}</span>
                    {attachments_html}
                    {stickers_html}
                    {embeds_html}
                </div>
            </div>
        </div>
    </div>
</div>
"""


def _render_user_popouts(participants: dict[int, discord.abc.User]) -> str:
    popouts: list[str] = []
    for user_id, user in participants.items():
        avatar_url = getattr(getattr(user, "display_avatar", None), "url", "https://cdn.discordapp.com/embed/avatars/0.png")
        display_name = html.escape(getattr(user, "display_name", str(user)))
        username = html.escape(str(user))
        popouts.append(
            f"""
<div id="meta-popout-{user_id}" class="meta-popout">
    <div class="meta__header">
        <img src="{avatar_url}" alt="Avatar">
        <div class="meta__details">
            <div class="meta__display-name">{display_name}</div>
            <div class="meta__user">{username}</div>
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
    """
    Generate an HTML transcript.
    When purged_messages is provided, the output includes a Purged Messages section
    and renders those deleted messages as part of the transcript payload.
    """

    sorted_context = sorted(messages, key=lambda m: m.created_at)
    sorted_purged = sorted(purged_messages or [], key=lambda m: m.created_at)
    all_messages = [*sorted_context, *sorted_purged]

    participants: dict[int, discord.abc.User] = {}
    for msg in all_messages:
        participants[msg.author.id] = msg.author

    rendered: list[str] = []
    for msg in sorted_context:
        rendered.append(_render_message(msg))

    if sorted_context and sorted_purged:
        rendered.append(
            """
<div class="chatlog__divider">
    <span>Purged Messages</span>
</div>
"""
        )

    for msg in sorted_purged:
        rendered.append(_render_message(msg))

    now = datetime.now(timezone.utc)
    generated_at = _fmt_utc_footer(now)
    channel_created = _fmt_utc_meta(channel.created_at)
    guild_icon = (
        guild.icon.url
        if getattr(guild, "icon", None)
        else "https://cdn.discordapp.com/embed/avatars/0.png"
    )

    html_out = (
        HTML_TEMPLATE
        .replace("{channel_name}", html.escape(channel.name))
        .replace("{channel_id}", str(channel.id))
        .replace("{guild_name}", html.escape(guild.name))
        .replace("{guild_id}", str(guild.id))
        .replace("{message_count}", str(len(all_messages)))
        .replace("{generated_at}", generated_at)
        .replace("{guild_icon}", guild_icon)
        .replace("{created_at}", channel_created)
        .replace("{participant_count}", str(len(participants)))
        .replace("{messages}", "".join(rendered))
        .replace("{user_popouts}", _render_user_popouts(participants))
    )

    return io.BytesIO(html_out.encode("utf-8"))


class EphemeralTranscriptView(discord.ui.View):
    """Provides a log button that opens a hosted transcript when available."""

    def __init__(self, transcript_data: io.BytesIO, filename: str = "transcript.html"):
        super().__init__(timeout=3600)
        self.data_bytes = transcript_data.getvalue()
        self.filename = filename
        self.public_url = publish_transcript(self.data_bytes, filename)
        if self.public_url:
            for item in list(self.children):
                if isinstance(item, discord.ui.Button) and item.label == "Download Transcript":
                    self.remove_item(item)
            self.add_item(
                discord.ui.Button(
                    label="View Transcript",
                    style=discord.ButtonStyle.link,
                    url=self.public_url,
                )
            )

    @discord.ui.button(label="Download Transcript", style=discord.ButtonStyle.secondary)
    async def download(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.public_url:
            await interaction.response.send_message(self.public_url, ephemeral=True)
            return
        file_buffer = io.BytesIO(self.data_bytes)
        file_buffer.seek(0)
        await interaction.response.send_message(
            file=discord.File(file_buffer, filename=self.filename),
            ephemeral=True,
        )
