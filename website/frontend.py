from __future__ import annotations

import html
from typing import Optional

import discord


def settings_int(settings: dict[str, object], key: str) -> Optional[int]:
    value = settings.get(key)
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def settings_text(settings: dict[str, object], key: str) -> str:
    value = settings.get(key)
    return value.strip() if isinstance(value, str) else ""


def _channel_label(channel: discord.abc.GuildChannel) -> str:
    prefix = "#" if isinstance(channel, discord.TextChannel) else ""
    return f"{prefix}{channel.name}"


def _select_options(
    items: list[tuple[str, str]],
    selected: Optional[str],
    *,
    blank_label: str,
) -> str:
    parts = [
        f'<option value=""{" selected" if not selected else ""}>{html.escape(blank_label)}</option>'
    ]
    for value, label in items:
        is_selected = selected == value
        parts.append(
            '<option value="{value}"{selected}>{label}</option>'.format(
                value=html.escape(value, quote=True),
                selected=" selected" if is_selected else "",
                label=html.escape(label),
            )
        )
    return "\n".join(parts)


def _category_select_options(
    guild: discord.Guild,
    selected_id: Optional[int],
    *,
    blank_label: str,
) -> str:
    items = [(str(category.id), category.name) for category in guild.categories]
    selected = str(selected_id) if selected_id else None
    return _select_options(items, selected, blank_label=blank_label)


def _text_channel_select_options(
    guild: discord.Guild,
    selected_id: Optional[int],
    *,
    blank_label: str,
    create_value: Optional[str] = None,
    create_label: Optional[str] = None,
) -> str:
    items: list[tuple[str, str]] = []
    if create_value and create_label:
        items.append((create_value, create_label))
    items.extend(
        (str(channel.id), _channel_label(channel)) for channel in guild.text_channels
    )
    selected = str(selected_id) if selected_id else create_value
    return _select_options(items, selected, blank_label=blank_label)


def _role_select_options(
    guild: discord.Guild,
    selected_id: Optional[int],
    *,
    blank_label: str,
) -> str:
    roles = [role for role in guild.roles if not role.is_default()]
    roles.sort(key=lambda role: role.position, reverse=True)
    items = [(str(role.id), f"@{role.name}") for role in roles]
    selected = str(selected_id) if selected_id else None
    return _select_options(items, selected, blank_label=blank_label)


def _guild_icon_url(guild: discord.Guild) -> str:
    icon = getattr(guild, "icon", None)
    if icon:
        return str(icon.replace(size=128, static_format="png").url)
    return ""


def _initials(value: str) -> str:
    words = [part for part in value.replace("-", " ").split() if part]
    if not words:
        return "S"
    return "".join(word[0] for word in words[:2]).upper()


def render_setup_page(
    *,
    token: str,
    guild: discord.Guild,
    settings: dict[str, object],
    ticket_settings: dict[str, object],
    economy_config: dict[str, object],
    server_name_fallback: str,
    message: str = "",
    success: bool = False,
) -> str:
    server_name = (
        settings_text(settings, "server_name") or guild.name or server_name_fallback
    )
    support_category_id = settings_int(ticket_settings, "category_id")
    ticket_log_id = settings_int(ticket_settings, "log_channel_id")
    eco_channel_id = (
        settings_int(economy_config, "shop_channel_id") if economy_config else None
    )
    eco_role_id = (
        settings_int(economy_config, "shop_bypass_role_id") if economy_config else None
    )
    icon_url = _guild_icon_url(guild)
    avatar_html = (
        f'<img src="{html.escape(icon_url, quote=True)}" alt="">'
        if icon_url
        else html.escape(_initials(server_name))
    )
    notice = ""
    if message:
        notice_class = "notice success" if success else "notice error"
        notice = f'<div class="{notice_class}" role="status">{html.escape(message)}</div>'

    wallpaper_name = (
        settings_text(settings, "welcome_wallpaper_path") or "Default welcome image"
    )
    nsfw_checked = "checked" if settings.get("nsfw_enabled") else ""
    setup_action = f"/setup/{html.escape(token, quote=True)}"
    escaped_guild = html.escape(guild.name)

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escaped_guild} setup</title>
  <link rel="stylesheet" href="/frontend.css">
</head>
<body>
  <main class="app-shell">
    <aside class="sidebar" aria-label="Setup navigation">
      <div class="brand-block">
        <div class="server-avatar">{avatar_html}</div>
        <div>
          <p class="eyebrow">Bot Setup</p>
          <h1>{escaped_guild}</h1>
        </div>
      </div>
      <div class="setup-meta">
        <span>Secure setup link</span>
        <strong>Live configuration</strong>
      </div>
      <nav class="steps">
        <button class="step active" type="button" data-step="0">
          <span class="step-index">01</span>
          <span><strong>Branding</strong><small>Name and welcome image</small></span>
        </button>
        <button class="step" type="button" data-step="1">
          <span class="step-index">02</span>
          <span><strong>Channels</strong><small>Core server destinations</small></span>
        </button>
        <button class="step" type="button" data-step="2">
          <span class="step-index">03</span>
          <span><strong>Tickets</strong><small>Support and transcripts</small></span>
        </button>
        <button class="step" type="button" data-step="3">
          <span class="step-index">04</span>
          <span><strong>Systems</strong><small>XP, economy, features</small></span>
        </button>
      </nav>
    </aside>

    <form class="workspace" method="post" enctype="multipart/form-data" action="{setup_action}">
      <header class="topbar">
        <div>
          <p class="eyebrow">Configuration Console</p>
          <h2>Server setup</h2>
        </div>
        <div class="status-grid" aria-label="Current setup coverage">
          <div><span>Channels</span><strong>{len(guild.text_channels)}</strong></div>
          <div><span>Roles</span><strong>{max(0, len(guild.roles) - 1)}</strong></div>
          <div><span>Categories</span><strong>{len(guild.categories)}</strong></div>
        </div>
      </header>

      <div class="content-panel">
        {notice}

        <section class="setup-section active" data-panel="0">
          <div class="section-heading">
            <p class="eyebrow">Identity</p>
            <h3>Branding</h3>
            <p>Set the display name used by bot messages and choose the welcome card image.</p>
          </div>
          <div class="field-grid">
            <label class="field wide">
              <span>Server display name</span>
              <input name="server_name" maxlength="80" required value="{html.escape(server_name, quote=True)}">
            </label>
            <label class="field wide">
              <span>Welcome wallpaper upload</span>
              <input type="file" name="wallpaper_file" accept="image/png,image/jpeg,image/gif,image/webp">
              <small>Current: {html.escape(wallpaper_name)}</small>
            </label>
            <label class="field wide">
              <span>Direct image URL</span>
              <input type="url" name="wallpaper_url" placeholder="https://example.com/welcome.png">
            </label>
          </div>
        </section>

        <section class="setup-section" data-panel="1">
          <div class="section-heading">
            <p class="eyebrow">Routing</p>
            <h3>Channels</h3>
            <p>Pick the destinations for public bot output, staff alerts, level-ups, and inactivity notices.</p>
          </div>
          <div class="field-grid">
            <label class="field">
              <span>Announcements</span>
              <select name="announcement_channel_id">{_text_channel_select_options(guild, settings_int(settings, "announcement_channel_id"), blank_label="Leave unchanged")}</select>
            </label>
            <label class="field">
              <span>Welcome</span>
              <select name="welcome_channel_id">{_text_channel_select_options(guild, settings_int(settings, "welcome_channel_id"), blank_label="Create #welcome if empty", create_value="create:welcome", create_label="Create or reuse #welcome")}</select>
            </label>
            <label class="field">
              <span>Rules</span>
              <select name="rules_channel_id">{_text_channel_select_options(guild, settings_int(settings, "rules_channel_id"), blank_label="Leave unchanged")}</select>
            </label>
            <label class="field">
              <span>Ticket agent alerts</span>
              <select name="agent_channel_id">{_text_channel_select_options(guild, settings_int(settings, "agent_channel_id"), blank_label="Leave unchanged")}</select>
            </label>
            <label class="field">
              <span>Level-up announcements</span>
              <select name="level_channel_id">{_text_channel_select_options(guild, settings_int(settings, "level_channel_id"), blank_label="Create #level-ups", create_value="create:level-ups", create_label="Create or reuse #level-ups")}</select>
            </label>
            <label class="field">
              <span>Inactive notices</span>
              <select name="inactive_channel_id">{_text_channel_select_options(guild, settings_int(settings, "inactive_channel_id"), blank_label="Create #inactive-notices", create_value="create:inactive-notices", create_label="Create or reuse #inactive-notices")}</select>
            </label>
          </div>
        </section>

        <section class="setup-section" data-panel="2">
          <div class="section-heading">
            <p class="eyebrow">Support</p>
            <h3>Tickets and transcripts</h3>
            <p>Configure support categories, staff access, logs, and transcript retention.</p>
          </div>
          <div class="field-grid">
            <label class="field">
              <span>Support ticket category</span>
              <select name="support_category_id">{_category_select_options(guild, support_category_id, blank_label="Create or reuse Support Tickets")}</select>
            </label>
            <label class="field">
              <span>Request ticket category</span>
              <select name="req_category_id">{_category_select_options(guild, None, blank_label="Create or reuse Req Tickets")}</select>
            </label>
            <label class="field">
              <span>Ticket support role</span>
              <select name="ticket_support_role_id">{_role_select_options(guild, settings_int(settings, "ticket_support_role_id"), blank_label="Create or reuse Support role")}</select>
            </label>
            <label class="field">
              <span>Ticket logs</span>
              <select name="ticket_log_channel_id">{_text_channel_select_options(guild, ticket_log_id, blank_label="Create private #ticket-logs", create_value="create:ticket-logs", create_label="Create or reuse #ticket-logs")}</select>
            </label>
            <label class="field">
              <span>Max transcript messages</span>
              <input type="number" name="max_transcript_messages" min="1" max="50000" value="{settings_int(settings, "max_transcript_messages") or 5000}">
            </label>
          </div>
        </section>

        <section class="setup-section" data-panel="3">
          <div class="section-heading">
            <p class="eyebrow">Automation</p>
            <h3>Progression, economy, and features</h3>
            <p>Set XP rates, invite rewards, economy routing, and gated feature access.</p>
          </div>
          <div class="field-grid">
            <label class="field">
              <span>Level min XP per message</span>
              <input type="number" name="level_min_xp" min="1" max="1000" value="{settings_int(settings, "level_min_xp") or 15}">
            </label>
            <label class="field">
              <span>Level max XP per message</span>
              <input type="number" name="level_max_xp" min="1" max="1000" value="{settings_int(settings, "level_max_xp") or 25}">
            </label>
            <label class="field">
              <span>Invite XP reward</span>
              <input type="number" name="invite_xp" min="0" max="100000" value="{settings_int(settings, "invite_xp") or 500}">
            </label>
            <label class="field">
              <span>Economy channel</span>
              <select name="eco_channel_id">{_text_channel_select_options(guild, eco_channel_id, blank_label="Leave unchanged")}</select>
            </label>
            <label class="field">
              <span>Economy bypass role</span>
              <select name="bypass_role_id">{_role_select_options(guild, settings_int(settings, "bypass_role_id") or eco_role_id, blank_label="Leave unchanged")}</select>
            </label>
            <label class="toggle-field wide">
              <input type="checkbox" name="nsfw_enabled" value="true" {nsfw_checked}>
              <span>
                <strong>Enable explicit commands</strong>
                <small>Allows gated explicit bot features for this server.</small>
              </span>
            </label>
          </div>
        </section>
      </div>

      <div class="action-bar">
        <button class="secondary" type="button" id="back">Back</button>
        <div class="action-copy">
          <strong id="step-title">Branding</strong>
          <span id="step-count">Step 1 of 4</span>
        </div>
        <button class="primary" type="button" id="next">Next</button>
        <button class="primary" type="submit" id="save" hidden>Save setup</button>
      </div>
    </form>
  </main>
  <script>
    const steps = [...document.querySelectorAll(".step")];
    const panels = [...document.querySelectorAll(".setup-section")];
    const back = document.getElementById("back");
    const next = document.getElementById("next");
    const save = document.getElementById("save");
    const stepTitle = document.getElementById("step-title");
    const stepCount = document.getElementById("step-count");
    let index = 0;

    function show(nextIndex) {{
      index = Math.max(0, Math.min(panels.length - 1, nextIndex));
      steps.forEach((step, i) => step.classList.toggle("active", i === index));
      panels.forEach((panel, i) => panel.classList.toggle("active", i === index));
      back.disabled = index === 0;
      next.hidden = index === panels.length - 1;
      save.hidden = index !== panels.length - 1;
      stepTitle.textContent = steps[index].querySelector("strong").textContent;
      stepCount.textContent = `Step ${{index + 1}} of ${{panels.length}}`;
    }}

    steps.forEach((step, i) => step.addEventListener("click", () => show(i)));
    back.addEventListener("click", () => show(index - 1));
    next.addEventListener("click", () => show(index + 1));
    show(0);
  </script>
</body>
</html>"""
