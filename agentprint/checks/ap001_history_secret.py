"""AP001: a secret sits in an agent's history, transcript, paste cache or edit backups."""

from ..models import Kind, Severity
from .base import SecretCheck

CHECK = SecretCheck(
    id="AP001",
    severity=Severity.HIGH,
    title="Secret in agent history",
    fix="Rotate this key with its provider, then delete or redact the history file.",
    kind=Kind.HISTORY,
)
