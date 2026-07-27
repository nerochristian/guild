"""
Time parsing utilities
"""

import re
from datetime import datetime, timedelta
from typing import Optional, Tuple

TIME_REGEX = re.compile(r'(\d+)\s*([smhdwMy])')

TIME_UNITS = {
    's': ('seconds', 1),
    'm': ('minutes', 60),
    'h': ('hours', 3600),
    'd': ('days', 86400),
    'w': ('weeks', 604800),
    'M': ('months', 2592000),
    'y': ('years', 31536000),
}


def parse_time(time_string: str) -> Optional[Tuple[timedelta, str]]:
    """
    Parse a time string like '1h30m' or '2d' into a timedelta
    Returns (timedelta, human_readable_string) or None if invalid
    """
    matches = TIME_REGEX.findall(time_string. lower().replace('mo', 'M'))

    if not matches:
        return None

    total_seconds = 0
    parts = []

    for amount, unit in matches:
        amount = int(amount)
        if unit not in TIME_UNITS:
            continue

        unit_name, unit_seconds = TIME_UNITS[unit]
        total_seconds += amount * unit_seconds

        if amount == 1:
            unit_name = unit_name[:-1]
        parts.append(f"{amount} {unit_name}")

    if total_seconds == 0:
        return None

    return timedelta(seconds=total_seconds), ", ".join(parts)


def format_time(dt: datetime) -> str:
    """Format a datetime for Discord"""
    return f"<t:{int(dt.timestamp())}:R>"


def format_timedelta(td: timedelta) -> str:
    """Format a timedelta into human readable string"""
    total_seconds = int(td.total_seconds())

    if total_seconds < 60:
        return f"{total_seconds} seconds"

    parts = []

    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)

    if days:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    if hours:
        parts. append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    if seconds and not days:
        parts.append(f"{seconds} second{'s' if seconds != 1 else ''}")

    return ", ".join(parts)