"""AutoMod extension package."""

from .commands import AutoMod, setup
from .config import AUTOMOD_SETTINGS, EXAMPLE_CONFIG, MODULES, MODULE_SETTING_KEYS
from .engine import AutoModEngine
from .models import Action, Category, RuleMatch, Severity, ViolationRecord
from .panel import AutoModPanel, PANEL_PAGES, _compact_duration, _parse_duration, _parse_threshold_pair
from .utils import domain_matches, normalize_domain

__all__ = (
    "Action",
    "AutoMod",
    "AutoModEngine",
    "AutoModPanel",
    "AUTOMOD_SETTINGS",
    "Category",
    "EXAMPLE_CONFIG",
    "MODULES",
    "MODULE_SETTING_KEYS",
    "PANEL_PAGES",
    "RuleMatch",
    "Severity",
    "ViolationRecord",
    "_compact_duration",
    "_parse_duration",
    "_parse_threshold_pair",
    "domain_matches",
    "normalize_domain",
    "setup",
)
