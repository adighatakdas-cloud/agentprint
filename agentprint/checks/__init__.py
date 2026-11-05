"""Registry of checks run by `agentprint scan`."""

from .ap001_history_secret import CHECK as AP001
from .ap002_settings_secret import CHECK as AP002
from .ap003_overbroad_permission import CHECK as AP003
from .ap004_network_hook import CHECK as AP004
from .ap005_mcp_inline_secret import CHECK as AP005
from .ap007_local_not_ignored import CHECK as AP007
from .ap008_readable_by_others import CHECK as AP008
from .base import Check

SCAN_CHECKS: tuple[Check, ...] = (AP001, AP002, AP003, AP004, AP005, AP007, AP008)
