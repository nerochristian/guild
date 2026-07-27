"""
Centralized Error Messages and Response Templates
Provides consistent messaging across the bot
"""

from typing import Optional


class Messages:
    """Standard error and response messages"""
    
    # Permission Errors
    MISSING_PERMISSIONS = "❌ You don't have permission to use this command."
    BOT_MISSING_PERMISSIONS = "❌ I need the following permissions: {perms}"
    HIERARCHY_ERROR = "❌ You cannot perform this action on that user due to role hierarchy."
    SELF_ACTION_ERROR = "❌ You cannot perform moderation actions on yourself."
    BOT_HIERARCHY_ERROR = "❌ I cannot perform this action on that user due to role hierarchy."
    
    # User Errors
    INVALID_USER = "❌ Could not find that user."
    USER_NOT_IN_GUILD = "❌ That user is not in this server."
    USER_IS_BOT = "❌ You cannot perform this action on a bot."
    TARGET_IS_OWNER = "❌ You cannot perform this action on the server owner."
    
    # Argument Errors
    MISSING_ARGUMENT = "❌ Missing required argument: `{param}`\n\nUse `{prefix}help {command}` for more info."
    INVALID_ARGUMENT = "❌ Invalid argument provided: {error}"
    INVALID_DURATION = "❌ Invalid duration format. Examples: `1h`, `30m`, `2d`, `1w`"
    INVALID_NUMBER = "❌ Please provide a valid number."
    
    # Moderation Errors
    ALREADY_BANNED = "❌ That user is already banned."
    NOT_BANNED = "❌ That user is not banned."
    ALREADY_MUTED = "❌ That user is already timed out."
    NOT_MUTED = "❌ That user is not timed out."
    CASE_NOT_FOUND = "❌ Case #{case_number} not found."
    
    # System Errors
    DATABASE_ERROR = "❌ A database error occurred. Please try again later."
    API_ERROR = "❌ An API error occurred. Please try again later."
    RATE_LIMITED = "⏰ You're being rate limited. Please wait {seconds}s before trying again."
    TIMEOUT_ERROR = "⏰ Command timed out. Please try again."
    
    # Success Messages
    SUCCESS_WARN = "✅ Successfully warned {user}."
    SUCCESS_MUTE = "✅ Successfully timed out {user} for {duration}."
    SUCCESS_UNMUTE = "✅ Successfully removed timeout from {user}."
    SUCCESS_KICK = "✅ Successfully kicked {user}."
    SUCCESS_BAN = "✅ Successfully banned {user}."
    SUCCESS_UNBAN = "✅ Successfully unbanned {user}."
    SUCCESS_PURGE = "✅ Successfully deleted {count} messages."
    
    # Configuration Messages
    CONFIG_UPDATED = "✅ Configuration updated successfully."
    CONFIG_RESET = "✅ Configuration reset to defaults."
    FEATURE_ENABLED = "✅ {feature} has been enabled."
    FEATURE_DISABLED = "✅ {feature} has been disabled."
    
    # AI Moderation
    AI_PROCESSING = "🤖 Processing your request..."
    AI_ERROR = "❌ AI service error: {error}"
    AI_RATE_LIMIT = "⏰ AI rate limit reached. Please wait {seconds}s."
    AI_NO_API_KEY = "❌ AI features are disabled (no API key configured)."
    
    # Logging
    LOG_CHANNEL_SET = "✅ {log_type} logs will now be sent to {channel}."
    LOG_CHANNEL_DISABLED = "✅ {log_type} logging has been disabled."
    LOG_SEND_FAILED = "⚠️ Failed to send log to {channel} (check permissions)."
    
    # Tickets & Modmail
    TICKET_CREATED = "🎫 Ticket created: {channel}"
    TICKET_CLOSED = "🔒 Ticket closed successfully."
    MODMAIL_BLOCKED = "🚫 You are blocked from using modmail."
    MODMAIL_SENT = "✅ Your message has been sent to the staff team."
    
    # Court System
    COURT_SESSION_STARTED = "⚖️ Court session started in {channel}."
    COURT_SESSION_CLOSED = "⚖️ Court session closed. Verdict: {verdict}"
    EVIDENCE_SUBMITTED = "📋 Evidence submitted successfully."
    VOTE_RECORDED = "🗳️ Your vote has been recorded."
    
    @staticmethod
    def format(template: str, **kwargs) -> str:
        """Format a message template with kwargs"""
        return template.format(**kwargs)


class Confirmations:
    """Confirmation prompts for dangerous actions"""
    
    BAN_CONFIRM = "⚠️ Are you sure you want to ban {user}?\nThis action is severe."
    PURGE_CONFIRM = "⚠️ Are you sure you want to delete {count} messages?\nThis cannot be undone."
    KICK_CONFIRM = "⚠️ Are you sure you want to kick {user}?"
    RESET_CONFIRM = "⚠️ Are you sure you want to reset all settings?\nThis cannot be undone."
    
    @staticmethod
    def format(template: str, **kwargs) -> str:
        """Format a confirmation template with kwargs"""
        return template.format(**kwargs)


class InfoMessages:
    """Informational messages and help text"""
    
    BOT_STARTUP = """
🤖 **Bot Starting Up**
Version: {version}
Servers: {guilds}
Users: {users}
    """.strip()
    
    COMMAND_COOLDOWN = "⏰ This command is on cooldown. Try again in {retry_after:.1f}s."
    
    NO_PERMISSION_HINT = "💡 This command requires the `{permission}` permission."
    
    HELP_FOOTER = "Use {prefix}help <command> for more detailed information."
    
    @staticmethod
    def format(template: str, **kwargs) -> str:
        """Format an info template with kwargs"""
        return template.format(**kwargs)
