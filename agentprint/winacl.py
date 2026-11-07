"""Who can read a file on Windows, and making it private, with nothing but the standard library.

Windows has no mode bits: a file's access list (its DACL) says who may do what with it. AP008
asks it one question, "can anyone besides the owner read this file?", and `agentprint fix`
answers with the built-in icacls. Sources (learn.microsoft.com, checked 2026-11-07): the
well-known SIDs list, the icacls command reference, and GetNamedSecurityInfoW.

Two halves:
- Plain Python over bytes and (ace type, flags, mask, SID) tuples: `parse_acl`, `sid_text`,
  `classify`, `allows`, `owned_by_me`, `lock_args`/`recipe`. Tested on every OS with made-up
  access lists.
- Windows calls through ctypes (`read`, `current_user`, `has_acls`, `restore`) and one icacls
  run (`lock`). Anywhere else `_api()` raises OSError, which callers report as "could not
  check", never as "private".

When a file counts as readable by others: an Allow entry that applies to the file itself (not
inherit-only) gives a right to read its contents (FILE_READ_DATA, GENERIC_READ or GENERIC_ALL)
to Everyone, Users, Authenticated Users, Guests, INTERACTIVE, ANONYMOUS LOGON, NETWORK, Remote
Desktop Users or another account, and no earlier Deny entry for that SID, or for Everyone, takes
the read right away (Windows reads the entries in order). The owner, SYSTEM, Administrators,
CREATOR OWNER / OWNER RIGHTS, service and app-package SIDs never count. An entry of any other
type that applies to the file (conditional, callback, object, compound) makes the answer
"could not check". No access list at all (a NULL DACL) lets everyone in.

Folders above the file are not looked at: by default every account may pass through folders
("bypass traverse checking"), so a file's own list decides.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import struct
import subprocess
from collections.abc import Container, Iterable, Sequence
from dataclasses import dataclass
from functools import lru_cache
from types import SimpleNamespace
from typing import Any

from . import platform

EVERYONE = "S-1-1-0"
USERS = "S-1-5-32-545"
AUTHENTICATED_USERS = "S-1-5-11"
SYSTEM = "S-1-5-18"
ADMINISTRATORS = "S-1-5-32-544"

# Groups that mean "someone other than the owner" (learn.microsoft.com well-known SIDs).
OTHERS = {
    EVERYONE: "Everyone",
    USERS: "Users",
    AUTHENTICATED_USERS: "Authenticated Users",
    "S-1-5-32-546": "Guests",
    "S-1-5-4": "INTERACTIVE",
    "S-1-5-7": "ANONYMOUS LOGON",
    "S-1-5-2": "NETWORK",
    "S-1-5-32-555": "Remote Desktop Users",
}
# Accounts and groups of this PC, a domain or Microsoft Entra ID: another one of them is
# another user, except the administrator ones (built-in Administrator, Domain, Schema and
# Enterprise Admins), who can read every file anyway.
_ACCOUNTS = ("S-1-5-21-", "S-1-12-1-")
_ADMIN_RIDS = frozenset({"500", "512", "518", "519"})

ALLOW, DENY = 0x0, 0x1
_SID_AT_8 = frozenset({ALLOW, DENY, 0x9, 0xA})  # plain and callback allow/deny: the same layout
INHERIT_ONLY = 0x08
INHERITED = 0x10
FILE_READ_DATA = 0x1
FILE_WRITE_DATA = 0x2
GENERIC_ALL = 0x10000000
GENERIC_WRITE = 0x40000000
GENERIC_READ = 0x80000000

# Why a file could not be checked (completes "who can read it could not be checked: ...").
NO_ACLS = "its drive keeps no Windows permissions"
UNREADABLE = "agentprint could not read its access list"
UNUSUAL = "its access list has conditional or other entries agentprint does not read"
NO_USER = "agentprint could not tell which account it runs as"

Ace = tuple[int, int, int, str]  # (ACE type, ACE flags, access mask, SID as S-1-...)


# ---- plain Python: bytes, SIDs and access lists ----------------------------------------------


def sid_text(raw: bytes) -> str:
    """A binary SID in ConvertSidToStringSidW's format: S-<revision>-<authority>-<sub>-<sub>..."""
    if len(raw) < 8 or len(raw) < 8 + 4 * raw[1]:
        raise ValueError("SID ends early")
    revision, count = raw[0], raw[1]
    authority = int.from_bytes(raw[2:8], "big")
    head = f"S-{revision}-{authority}" if authority < 2**32 else f"S-{revision}-0x{authority:012X}"
    return "-".join([head, *map(str, struct.unpack_from(f"<{count}I", raw, 8))])


def parse_acl(raw: bytes) -> tuple[Ace, ...]:
    """The entries of a binary ACL (an 8-byte header, then each ACE) as (type, flags, mask, SID)
    tuples. The SID is read for allow and deny entries and their callback forms; other types get
    "" (classify() reports them as unknown). Raises ValueError on a malformed list."""
    if len(raw) < 8:
        raise ValueError("ACL ends early")
    count = struct.unpack_from("<H", raw, 4)[0]
    aces: list[Ace] = []
    offset = 8
    for _ in range(count):
        if offset + 8 > len(raw):
            raise ValueError("ACL ends early")
        kind, flags, size = struct.unpack_from("<BBH", raw, offset)
        if size < 8 or offset + size > len(raw):
            raise ValueError("ACE has a bad size")
        mask = struct.unpack_from("<I", raw, offset + 4)[0]
        sid = sid_text(raw[offset + 8 : offset + size]) if kind in _SID_AT_8 else ""
        aces.append((kind, flags, mask, sid))
        offset += size
    return tuple(aces)


def is_other(sid: str, mine: Container[str]) -> bool:
    """True if `sid` is someone other than this user (and not SYSTEM or an administrator)."""
    if sid in mine:
        return False
    if sid in OTHERS:
        return True
    return sid.startswith(_ACCOUNTS) and sid.rsplit("-", 1)[-1] not in _ADMIN_RIDS


def name(sid: str) -> str:
    if sid in OTHERS:
        return OTHERS[sid]
    if sid.startswith("S-1-5-21-") and sid.endswith("-513"):
        return "Domain Users"
    return f"another account ({sid})"


def _grants(mask: int, right: int) -> bool:
    generic = {FILE_READ_DATA: GENERIC_READ, FILE_WRITE_DATA: GENERIC_WRITE}.get(right, 0)
    return bool(mask & (right | generic | GENERIC_ALL))


@dataclass(frozen=True)
class Access:
    """Who besides the owner can read a file."""

    readers: tuple[str, ...] = ()  # their SIDs, in access-list order
    explicit: tuple[str, ...] = ()  # those granted by the file's own (not inherited) entries
    unknown: str = ""  # set when it could not be worked out (one of the reasons above)


def classify(aces: Sequence[Ace] | None, mine: Iterable[str]) -> Access:
    """Who can read a file with these access-list entries, besides the SIDs in `mine` (the user
    and the owner). `aces` None is a NULL DACL: everyone may read it."""
    if aces is None:
        return Access(readers=(EVERYONE,))
    mine = frozenset(mine)
    denied: set[str] = set()
    readers: list[str] = []
    explicit: set[str] = set()
    for kind, flags, mask, sid in aces:
        if flags & INHERIT_ONLY:
            continue  # applies to files created later in a folder, not to this file
        if kind not in (ALLOW, DENY):
            return Access(unknown=UNUSUAL)
        if not _grants(mask, FILE_READ_DATA) or not is_other(sid, mine):
            continue
        if kind == DENY:
            denied.add(sid)
        elif sid not in denied and EVERYONE not in denied:
            if sid not in readers:
                readers.append(sid)
            if not flags & INHERITED:
                explicit.add(sid)
    return Access(tuple(readers), tuple(s for s in readers if s in explicit))


def allows(aces: Sequence[Ace] | None, sids: Iterable[str], right: int) -> bool:
    """True if the first entry for one of `sids` that mentions `right` allows it (the order
    Windows reads them in). Used to check the owner kept access after a lock."""
    if aces is None:
        return True
    sids = frozenset(sids)
    for kind, flags, mask, sid in aces:
        if flags & INHERIT_ONLY or sid not in sids or not _grants(mask, right):
            continue
        return kind == ALLOW
    return False


def owned_by_me(owner: str, me: str, elevated: bool) -> bool:
    """The file is this user's: they own it, or an elevated (Run as administrator) process made
    it, which Windows records as owned by Administrators."""
    return owner == me or (elevated and owner == ADMINISTRATORS)


def lock_args(path: str, me: str, remove: Sequence[str] = ()) -> list[str]:
    """icacls arguments that leave `path` to this user, SYSTEM and Administrators: inherited
    entries go (/inheritance:r) and the three get full control (/grant:r), in one call so the
    file is never left with an empty list. `remove`: other SIDs the file names in its own
    entries, whose allow entries go too (/remove:g)."""
    args = [path, "/inheritance:r", "/grant:r", f"*{me}:F", f"*{SYSTEM}:F", f"*{ADMINISTRATORS}:F"]
    if remove:
        args += ["/remove:g", *(f"*{sid}" for sid in remove)]
    return args


def recipe(path: str, me: str, remove: Sequence[str] = ()) -> str:
    """The same icacls call as a command for a person or an AI agent (PowerShell and cmd read it
    alike). The caller makes sure the path holds no quote or variable characters."""
    return " ".join(["icacls", f'"{path}"', *lock_args(path, me, remove)[1:]])


# ---- Windows: ctypes and icacls ---------------------------------------------------------------

SE_FILE_OBJECT = 1
OWNER_SECURITY_INFORMATION = 0x1
DACL_SECURITY_INFORMATION = 0x4
PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
UNPROTECTED_DACL_SECURITY_INFORMATION = 0x20000000
SE_DACL_PROTECTED = 0x1000
SDDL_REVISION_1 = 1
TOKEN_QUERY = 0x8
TOKEN_USER = 1
TOKEN_ELEVATION = 20
FILE_PERSISTENT_ACLS = 0x8
ERROR_ACCESS_DENIED = 5
ERROR_NOT_SUPPORTED = 50


@dataclass(frozen=True)
class Acl:
    owner: str  # SID
    aces: tuple[Ace, ...] | None  # None: no access list at all (everyone may do anything)
    protected: bool  # inheritance from the parent folder is off
    sddl: str  # the access list as SDDL text, which `restore` puts back exactly


def _error(code: int | None = None) -> OSError:
    """The Windows error `code` (default: the thread's last error) as an OSError."""
    last, win_error = getattr(ctypes, "get_last_error", None), getattr(ctypes, "WinError", None)
    if code is None:
        code = last() if last else 0
    return win_error(code) if win_error else OSError(code, f"Windows error {code}")


# Each function: (DLL, name, "result argument..."). p pointer, P pointer to a pointer, d DWORD,
# D DWORD*, W WORD*, i int (BOOL and enums), I int*, s wide string. A c_void_p, c_uint32, ...
# passed where a pointer to one is declared is passed by reference (ctypes does that itself).
_CALLS = {
    "get_named": ("advapi32", "GetNamedSecurityInfoW", "d s i d P P P P P"),
    "set_named": ("advapi32", "SetNamedSecurityInfoW", "d s i d p p p p"),
    "sd_control": ("advapi32", "GetSecurityDescriptorControl", "i p W D"),
    "sd_dacl": ("advapi32", "GetSecurityDescriptorDacl", "i p I P I"),
    "sd_to_text": (
        "advapi32",
        "ConvertSecurityDescriptorToStringSecurityDescriptorW",
        "i p d d P D",
    ),
    "text_to_sd": ("advapi32", "ConvertStringSecurityDescriptorToSecurityDescriptorW", "i s d P D"),
    "open_token": ("advapi32", "OpenProcessToken", "i p d P"),
    "token_info": ("advapi32", "GetTokenInformation", "i p i p d D"),
    "current_process": ("kernel32", "GetCurrentProcess", "p"),
    "close": ("kernel32", "CloseHandle", "i p"),
    "local_free": ("kernel32", "LocalFree", "p p"),
    "volume_path": ("kernel32", "GetVolumePathNameW", "i s s d"),
    "volume_info": ("kernel32", "GetVolumeInformationW", "i s s d D D D s d"),
}
_TYPES: dict[str, Any] = {
    "p": ctypes.c_void_p,
    "P": ctypes.POINTER(ctypes.c_void_p),
    "d": ctypes.c_uint32,
    "D": ctypes.POINTER(ctypes.c_uint32),
    "W": ctypes.POINTER(ctypes.c_uint16),
    "i": ctypes.c_int,
    "I": ctypes.POINTER(ctypes.c_int),
    "s": ctypes.c_wchar_p,
}


def _api() -> Any:
    """The advapi32 and kernel32 functions of _CALLS, typed; loaded on first use."""
    return _load()


@lru_cache(maxsize=1)
def _load() -> SimpleNamespace:
    windll = getattr(ctypes, "WinDLL", None)
    if windll is None:
        raise OSError("Windows access lists can only be read on Windows")
    dlls = {lib: windll(lib, use_last_error=True) for lib in ("advapi32", "kernel32")}
    calls = {}
    for key, (dll, symbol, spec) in _CALLS.items():
        result, *args = (_TYPES[letter] for letter in spec.split())
        function = getattr(dlls[dll], symbol)
        function.restype, function.argtypes = result, args
        calls[key] = function
    return SimpleNamespace(**calls)


def _sid_at(address: int) -> str:
    head = ctypes.string_at(address, 8)
    return sid_text(ctypes.string_at(address, 8 + 4 * head[1]))


def _acl_at(address: int) -> tuple[Ace, ...]:
    size = struct.unpack_from("<H", ctypes.string_at(address, 8), 2)[0]
    return parse_acl(ctypes.string_at(address, size))


def _sddl(api: Any, descriptor: int) -> str:
    text = ctypes.c_void_p()
    if not api.sd_to_text(descriptor, SDDL_REVISION_1, DACL_SECURITY_INFORMATION, text, None):
        raise _error()
    if not text.value:
        raise ValueError("no SDDL text")
    try:
        return ctypes.wstring_at(text.value)
    finally:
        api.local_free(text)


def _protected(api: Any, descriptor: int) -> bool:
    control, revision = ctypes.c_uint16(), ctypes.c_uint32()
    if not api.sd_control(descriptor, control, revision):
        raise _error()
    return bool(control.value & SE_DACL_PROTECTED)


def read(path: str) -> Acl:
    """The owner and access list of a file (GetNamedSecurityInfoW). Raises OSError or
    ValueError when they can't be read."""
    api = _api()
    owner, dacl, descriptor = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    info = OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION
    code = api.get_named(path, SE_FILE_OBJECT, info, owner, None, dacl, None, descriptor)
    if code:
        raise _error(code)
    try:
        if not owner.value:
            raise ValueError("the file has no owner")
        return Acl(
            owner=_sid_at(owner.value),
            aces=_acl_at(dacl.value) if dacl.value else None,
            protected=_protected(api, descriptor.value or 0),
            sddl=_sddl(api, descriptor.value or 0),
        )
    finally:
        api.local_free(descriptor)


def current_user() -> tuple[str, bool]:
    """This process's user SID, and whether it runs elevated (as administrator)."""
    api = _api()
    token = ctypes.c_void_p()
    if not api.open_token(api.current_process(), TOKEN_QUERY, token):
        raise _error()
    try:
        size = ctypes.c_uint32()
        api.token_info(token, TOKEN_USER, None, 0, size)  # fails, and says how much room it needs
        if not size.value:
            raise _error()
        buffer = ctypes.create_string_buffer(size.value)
        if not api.token_info(token, TOKEN_USER, ctypes.addressof(buffer), size.value, size):
            raise _error()
        me = _sid_at(ctypes.c_void_p.from_buffer(buffer).value or 0)  # TOKEN_USER.User.Sid
        elevation = ctypes.c_uint32()
        elevated = bool(
            api.token_info(token, TOKEN_ELEVATION, ctypes.addressof(elevation), 4, size)
            and elevation.value
        )
        return me, elevated
    finally:
        api.close(token)


def has_acls(path: str) -> bool:
    """False for a drive that keeps no permissions (FAT, exFAT, some network and WSL drives)."""
    api = _api()
    root = ctypes.create_unicode_buffer(1024)
    if not api.volume_path(path, root, len(root)):
        raise _error()
    return _volume_has_acls(root.value)


@lru_cache(maxsize=64)
def _volume_has_acls(root: str) -> bool:
    flags = ctypes.c_uint32()
    if not _api().volume_info(root, None, 0, None, None, flags, None, 0):
        raise _error()
    return bool(flags.value & FILE_PERSISTENT_ACLS)


def restore(path: str, sddl: str) -> None:
    """Put back an access list saved by `read` (its SDDL text), protected or inheriting as it
    was (SetNamedSecurityInfoW)."""
    api = _api()
    descriptor = ctypes.c_void_p()
    if not api.text_to_sd(sddl, SDDL_REVISION_1, descriptor, None):
        raise _error()
    try:
        present, defaulted, dacl = ctypes.c_int(), ctypes.c_int(), ctypes.c_void_p()
        if not api.sd_dacl(descriptor, present, dacl, defaulted):
            raise _error()
        if not present.value:
            raise ValueError("the saved permissions hold no access list")
        info = DACL_SECURITY_INFORMATION | (
            PROTECTED_DACL_SECURITY_INFORMATION
            if _protected(api, descriptor.value or 0)
            else UNPROTECTED_DACL_SECURITY_INFORMATION
        )
        code = api.set_named(path, SE_FILE_OBJECT, info, None, None, dacl, None)
        if code:
            raise _error(code)
    finally:
        api.local_free(descriptor)


def icacls() -> str:
    """Windows' own icacls.exe, by full path so a look-alike on PATH is never run."""
    root = os.environ.get("SYSTEMROOT") or os.environ.get("WINDIR") or "C:\\Windows"
    exe = os.path.join(root, "System32", "icacls.exe")
    return exe if os.path.isfile(exe) else (shutil.which("icacls") or "icacls")


def lock(path: str, me: str, remove: Sequence[str] = ()) -> int:
    """Run the `lock_args` icacls call (list form, no shell, no console window). Returns its
    exit code, or -1 when icacls could not be run at all."""
    try:
        done = subprocess.run(
            [icacls(), *lock_args(path, me, remove)],
            stdin=subprocess.DEVNULL,  # never the MCP server's or a hook's own stdin
            capture_output=True,
            timeout=60,
            check=False,
            **platform.subprocess_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return -1
    return done.returncode


def exposure(path: str, me: str, elevated: bool) -> Access | None:
    """Who besides this user can read `path`; None when the file is another user's (theirs to
    fix). Never raises: what can't be read comes back as `Access.unknown`."""
    try:
        if not has_acls(path):
            return Access(unknown=NO_ACLS)
        acl = read(path)
    except (OSError, ValueError):
        return Access(unknown=UNREADABLE)
    if not owned_by_me(acl.owner, me, elevated):
        return None
    return classify(acl.aces, {me, acl.owner})
