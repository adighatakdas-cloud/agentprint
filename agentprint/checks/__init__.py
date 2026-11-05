"""Registry of checks run by `agentprint scan`."""

from .ap001_history_secret import CHECK as AP001
from .ap002_settings_secret import CHECK as AP002
from .ap003_overbroad_permission import CHECK as AP003
from .base import Check

SCAN_CHECKS: tuple[Check, ...] = (AP001, AP002, AP003)
