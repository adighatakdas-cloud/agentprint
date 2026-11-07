"""A pretend Windows for AP008 and the fix lock: access lists kept in a dict, icacls applied the
way its docs describe, so the Windows code paths run on every OS. Real ACLs are tested by the
`windows_only` tests in tests/test_windows_acl.py on windows-latest.
"""

from __future__ import annotations

import json
from pathlib import Path

from agentprint import platform, winacl

ME = "S-1-5-21-1111111111-2222222222-3333333333-1001"
SOMEONE = "S-1-5-21-1111111111-2222222222-3333333333-1002"
FULL = 0x1F01FF  # F
READ = 0x120089  # R
READ_EXECUTE = 0x1200A9  # RX
INHERITED = winacl.INHERITED


def allow(sid: str, mask: int = READ, flags: int = 0) -> winacl.Ace:
    return (winacl.ALLOW, flags, mask, sid)


def deny(sid: str, mask: int = READ, flags: int = 0) -> winacl.Ace:
    return (winacl.DENY, flags, mask, sid)


# What a file under the user's profile gets: the user, SYSTEM and Administrators, inherited.
PRIVATE = (
    allow(winacl.SYSTEM, FULL, INHERITED),
    allow(winacl.ADMINISTRATORS, FULL, INHERITED),
    allow(ME, FULL, INHERITED),
)


def _sddl(aces: tuple[winacl.Ace, ...] | None, protected: bool) -> str:
    return "FAKE:" + json.dumps({"protected": protected, "aces": aces})


class FakeWindows:
    def __init__(self, me: str = ME, elevated: bool = False) -> None:
        self.me, self.elevated = me, elevated
        self.acls: dict[str, winacl.Acl] = {}
        self.no_acls = False  # the drive keeps no permissions
        self.exit_code = 0  # what icacls returns
        self.icacls_works = True  # False: icacls exits non-zero and changes nothing
        self.calls: list[list[str]] = []
        self.restored: list[str] = []

    def set(
        self,
        path: Path,
        aces: tuple[winacl.Ace, ...] | None,
        owner: str | None = None,
        protected: bool = False,
    ) -> None:
        self.acls[str(Path(path).resolve())] = winacl.Acl(
            owner or self.me, aces, protected, _sddl(aces, protected)
        )

    def get(self, path: Path) -> winacl.Acl:
        return self.read(str(Path(path).resolve()))

    # ---- the winacl functions it stands in for ----

    def read(self, path: str) -> winacl.Acl:
        if path not in self.acls:
            self.set(Path(path), PRIVATE)
        return self.acls[path]

    def current_user(self) -> tuple[str, bool]:
        return self.me, self.elevated

    def has_acls(self, path: str) -> bool:
        return not self.no_acls

    def lock(self, path: str, me: str, remove=()) -> int:
        args = winacl.lock_args(path, me, remove)
        self.calls.append(args)
        if not self.icacls_works:
            return self.exit_code or 1
        old = self.read(path)
        aces = [a for a in old.aces or () if not a[1] & INHERITED]  # /inheritance:r
        grants = [a.split(":")[0].lstrip("*") for a in args[3:6]]
        removed = [a.lstrip("*") for a in args[7:]]
        aces = [  # /grant:r replaces their explicit grants; /remove:g drops allow entries
            a for a in aces if not (a[0] == winacl.ALLOW and (a[3] in grants or a[3] in removed))
        ]
        aces += [allow(sid, FULL) for sid in grants]
        self.set(Path(path), tuple(aces), owner=old.owner, protected=True)
        return self.exit_code

    def restore(self, path: str, sddl: str) -> None:
        self.restored.append(path)
        data = json.loads(sddl.removeprefix("FAKE:"))
        aces = None if data["aces"] is None else tuple(tuple(a) for a in data["aces"])
        self.set(Path(path), aces, owner=self.read(path).owner, protected=data["protected"])

    def install(self, monkeypatch) -> FakeWindows:
        monkeypatch.setattr(platform, "is_windows", lambda: True)
        monkeypatch.setattr(platform, "_RUN_NOTES", {})
        for name in ("read", "current_user", "has_acls", "lock", "restore"):
            monkeypatch.setattr(winacl, name, getattr(self, name))
        return self
