"""Registry of checks run by `agentprint scan`."""

from .ap001_history_secret import CHECK as AP001
from .ap002_settings_secret import CHECK as AP002
from .base import SecretCheck

SCAN_CHECKS: tuple[SecretCheck, ...] = (AP001, AP002)
