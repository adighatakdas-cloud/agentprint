"""AP002: a secret sits in an agent settings / allowlist / MCP config file."""

from ..models import Kind, Severity
from .base import SecretCheck

CHECK = SecretCheck(
    id="AP002",
    severity=Severity.CRITICAL,
    title="Secret in agent settings",
    fix="Rotate this key, remove it from the settings file, and load it from an environment "
    "variable instead.",
    kind=Kind.SETTINGS,
)
