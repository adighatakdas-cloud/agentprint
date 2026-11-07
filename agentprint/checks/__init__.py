"""Registry of checks: SCAN_CHECKS run by `agentprint scan`, package checks run by `pack`."""

from ..models import Finding
from ..pack import PackageListing, Wants
from . import ap006_package_leak, ap009_source_exposure
from .ap001_history_secret import CHECK as AP001
from .ap002_settings_secret import CHECK as AP002
from .ap003_overbroad_permission import CHECK as AP003
from .ap004_network_hook import CHECK as AP004
from .ap005_mcp_inline_secret import CHECK as AP005
from .ap007_local_not_ignored import CHECK as AP007
from .ap008_readable_by_others import CHECK as AP008
from .base import Check

SCAN_CHECKS: tuple[Check, ...] = (AP001, AP002, AP003, AP004, AP005, AP007, AP008)

# What `pack`, `fix --pack` and the publish guard read from a package: agent files first, with
# the whole content budget exactly as before (AP006, and keys inside them), then what AP009
# needs from the room left.
PACKAGE_WANT = Wants(ap006_package_leak.is_agent_file, ap009_source_exposure.wants)
PACKAGE_CHECK_IDS = (ap006_package_leak.ID, ap009_source_exposure.ID)


def check_package(listing: PackageListing) -> list[Finding]:
    """Every package check for one listing: AP006 (with AP002 for keys inside), then AP009.

    Not for a git push: pushing your own source to your own repository is normal."""
    return ap006_package_leak.check_listing(listing) + ap009_source_exposure.check_listing(listing)
