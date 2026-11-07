"""winacl: who can read a file on Windows, and the icacls lock.

The rules (classify, allows, parse_acl, sid_text, the recipe) are plain Python and run on every
OS with made-up access lists. The ctypes layer runs here against a fake advapi32/kernel32 that
hands back real memory, and against real Windows in the `windows_only` tests (windows-latest).
"""

from __future__ import annotations

import ctypes
import os
import struct
import subprocess
import sys

import pytest

from agentprint import platform, winacl
from tests.fakewin import FULL, INHERITED, ME, READ, READ_EXECUTE, SOMEONE, allow, deny

INHERIT_ONLY = winacl.INHERIT_ONLY
MODIFY = 0x1301BF
TRUSTED_INSTALLER = "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"


# ---- binary SIDs and ACLs, built the way Windows lays them out --------------------------------


def sid_bytes(sid: str) -> bytes:
    _, revision, authority, *subs = sid.split("-")
    return (
        bytes([int(revision), len(subs)])
        + int(authority, 0).to_bytes(6, "big")
        + struct.pack(f"<{len(subs)}I", *map(int, subs))
    )


def ace_bytes(kind: int, flags: int, mask: int, sid: str, extra: bytes = b"") -> bytes:
    body = struct.pack("<I", mask) + sid_bytes(sid) + extra
    return struct.pack("<BBH", kind, flags, 4 + len(body)) + body


def acl_bytes(*aces: bytes) -> bytes:
    body = b"".join(aces)
    return struct.pack("<BBHHH", 2, 0, 8 + len(body), len(aces), 0) + body


@pytest.mark.parametrize(
    "sid",
    [winacl.EVERYONE, winacl.SYSTEM, winacl.ADMINISTRATORS, ME, TRUSTED_INSTALLER, "S-1-15-2-1"],
)
def test_sid_text_reads_binary_sids(sid):
    assert winacl.sid_text(sid_bytes(sid)) == sid


def test_sid_text_writes_a_large_authority_in_hex_and_refuses_a_short_sid():
    raw = bytes([1, 1]) + (2**40).to_bytes(6, "big") + struct.pack("<I", 7)
    assert winacl.sid_text(raw) == "S-1-0x010000000000-7"
    with pytest.raises(ValueError):
        winacl.sid_text(sid_bytes(ME)[:-2])
    with pytest.raises(ValueError):
        winacl.sid_text(b"\x01")


def test_parse_acl_reads_every_entry_in_order():
    raw = acl_bytes(
        ace_bytes(winacl.ALLOW, INHERITED, FULL, winacl.SYSTEM),
        ace_bytes(winacl.DENY, 0, READ, winacl.EVERYONE),
        ace_bytes(0x9, 0, READ, winacl.USERS, extra=b"artx\x00\x00\x00\x00"),  # callback + data
        struct.pack("<BBHI", 0x5, 0, 12, READ) + b"\x00" * 4,  # object entry: no SID read
    )
    assert winacl.parse_acl(raw) == (
        (winacl.ALLOW, INHERITED, FULL, winacl.SYSTEM),
        (winacl.DENY, 0, READ, winacl.EVERYONE),
        (0x9, 0, READ, winacl.USERS),
        (0x5, 0, READ, ""),
    )
    assert winacl.parse_acl(acl_bytes()) == ()


@pytest.mark.parametrize(
    "raw",
    [
        b"\x02\x00",  # shorter than the header
        struct.pack("<BBHHH", 2, 0, 8, 1, 0),  # says one entry, holds none
        acl_bytes(struct.pack("<BBHI", 0, 0, 4, READ)),  # entry smaller than its header
        acl_bytes(struct.pack("<BBHI", 0, 0, 64, READ)),  # entry runs past the end
    ],
)
def test_parse_acl_refuses_a_malformed_list(raw):
    with pytest.raises(ValueError):
        winacl.parse_acl(raw)


# ---- who counts as "other users" -------------------------------------------------------------

MINE = {ME}


@pytest.mark.parametrize(
    "sid",
    [
        winacl.EVERYONE,
        winacl.USERS,
        winacl.AUTHENTICATED_USERS,
        "S-1-5-32-546",  # Guests
        "S-1-5-4",  # INTERACTIVE
        "S-1-5-7",  # ANONYMOUS LOGON
        "S-1-5-2",  # NETWORK
        "S-1-5-32-555",  # Remote Desktop Users
        SOMEONE,  # another local account
        "S-1-5-21-1-2-3-513",  # Domain Users
        "S-1-12-1-111-222-333-444",  # a Microsoft Entra account
        # Not on any list: every SID outside the trusted few counts (fails closed).
        "S-1-2-0",  # LOCAL
        "S-1-2-1",  # CONSOLE LOGON
        "S-1-5-14",  # REMOTE INTERACTIVE LOGON
        "S-1-5-113",  # Local account
        "S-1-5-15",  # This Organization
        "S-1-5-1000",  # Other Organization
        "S-1-5-3",  # BATCH
        "S-1-5-6",  # SERVICE
        "S-1-5-32-547",  # Power Users
        "S-1-5-32-558",  # Performance Monitor Users
        "S-1-5-32-568",  # IIS_IUSRS
        "S-1-5-83-0",  # NT VIRTUAL MACHINE\Virtual Machines
        "S-1-9-1",  # a resource-manager authority nobody listed
        "",  # an entry whose SID could not be named
    ],
)
def test_a_read_grant_to_someone_else_is_an_exposure(sid):
    access = winacl.classify([allow(sid, READ_EXECUTE, INHERITED)], MINE)
    assert access.readers == (sid,) and access.explicit == () and not access.unknown


@pytest.mark.parametrize(
    "sid",
    [
        ME,
        winacl.SYSTEM,
        winacl.ADMINISTRATORS,
        "S-1-3-0",  # CREATOR OWNER
        "S-1-3-1",  # CREATOR GROUP
        "S-1-3-4",  # OWNER RIGHTS
        "S-1-15-2-1",  # ALL APPLICATION PACKAGES
        "S-1-15-3-1024-1-2-3",  # an app capability
        TRUSTED_INSTALLER,
        "S-1-5-80-0",  # NT SERVICE\ALL SERVICES
        "S-1-5-114",  # Local account and member of Administrators
        "S-1-5-19",  # LOCAL SERVICE
        "S-1-5-20",  # NETWORK SERVICE
        "S-1-5-32-551",  # Backup Operators
        "S-1-5-21-1-2-3-500",  # the built-in Administrator
        "S-1-5-21-1-2-3-512",  # Domain Admins
    ],
)
def test_the_owner_system_admins_and_system_principals_never_count(sid):
    assert winacl.classify([allow(sid, FULL)], MINE) == winacl.Access()


@pytest.mark.parametrize(
    ("mask", "reads"),
    [
        (winacl.FILE_READ_DATA, True),
        (READ, True),
        (READ_EXECUTE, True),
        (MODIFY, True),
        (FULL, True),
        (winacl.GENERIC_READ, True),
        (winacl.GENERIC_ALL, True),
        (0x120080, False),  # read attributes and permissions: not the contents
        (winacl.FILE_WRITE_DATA, False),
        (winacl.GENERIC_WRITE, False),
    ],
)
def test_only_rights_that_read_the_contents_count(mask, reads):
    assert bool(winacl.classify([allow(winacl.EVERYONE, mask)], MINE).readers) is reads


def test_inherit_only_entries_do_not_apply_to_the_file():
    aces = [allow(winacl.EVERYONE, FULL, INHERIT_ONLY), (0x9, INHERIT_ONLY, READ, "")]
    assert winacl.classify(aces, MINE) == winacl.Access()


def test_an_earlier_deny_wins_and_a_later_one_does_not():
    users = allow(winacl.USERS, READ_EXECUTE, INHERITED)
    assert winacl.classify([deny(winacl.USERS), users], MINE).readers == ()
    assert winacl.classify([deny(winacl.EVERYONE), users], MINE).readers == ()  # covers all
    assert winacl.classify([users, deny(winacl.USERS)], MINE).readers == (winacl.USERS,)
    assert winacl.classify([deny("S-1-5-32-546"), users], MINE).readers == (winacl.USERS,)
    assert winacl.classify([deny(winacl.USERS, winacl.FILE_WRITE_DATA), users], MINE).readers


@pytest.mark.parametrize("kind", [0x4, 0x5, 0x9, 0xA, 0xB])
def test_conditional_callback_and_object_entries_mean_could_not_check(kind):
    access = winacl.classify([allow(winacl.SYSTEM, FULL), (kind, 0, READ, "")], MINE)
    assert access == winacl.Access(unknown=winacl.UNUSUAL)


def test_no_access_list_lets_everyone_in_and_an_empty_one_lets_nobody_in():
    assert winacl.classify(None, MINE).readers == (winacl.EVERYONE,)
    assert winacl.classify([], MINE) == winacl.Access()


def test_explicit_readers_are_the_ones_the_file_names_itself():
    aces = [
        allow(winacl.EVERYONE),
        allow(winacl.USERS, READ_EXECUTE, INHERITED),
        allow(winacl.EVERYONE, READ, INHERITED),
    ]
    access = winacl.classify(aces, MINE)
    assert access.readers == (winacl.EVERYONE, winacl.USERS)  # each SID once, in list order
    assert access.explicit == (winacl.EVERYONE,)


def test_allows_reads_the_owner_s_own_entries_in_order():
    read, write = winacl.FILE_READ_DATA, winacl.FILE_WRITE_DATA
    mine = [allow(ME, FULL)]
    assert winacl.allows(mine, {ME}, read) and winacl.allows(mine, {ME}, write)
    assert not winacl.allows([deny(ME, write), *mine], {ME}, write)
    assert winacl.allows([deny(ME, write), *mine], {ME}, read)
    assert not winacl.allows([allow(ME, FULL, INHERIT_ONLY)], {ME}, read)
    assert not winacl.allows([allow(SOMEONE, FULL)], {ME}, read)
    assert winacl.allows(None, {ME}, write)


def test_owned_by_me_accepts_administrators_only_when_elevated():
    assert winacl.owned_by_me(ME, ME, False)
    assert winacl.owned_by_me(winacl.ADMINISTRATORS, ME, True)
    assert not winacl.owned_by_me(winacl.ADMINISTRATORS, ME, False)
    assert not winacl.owned_by_me(SOMEONE, ME, True)


def test_names_are_plain_words():
    assert winacl.name(winacl.EVERYONE) == "Everyone"
    assert winacl.name(winacl.USERS) == "Users"
    assert winacl.name("S-1-5-21-1-2-3-513") == "Domain Users"
    assert winacl.name(SOMEONE) == f"another account or group ({SOMEONE})"
    assert winacl.name("S-1-2-0") == "LOCAL"


def test_the_recipe_is_the_brief_s_icacls_call_with_sid_literals():
    path = r"C:\Users\ana\.hermes\.env"
    assert winacl.recipe(path, ME) == (
        f'icacls "{path}" /inheritance:r /grant:r *{ME}:F *S-1-5-18:F *S-1-5-32-544:F'
    )
    assert winacl.lock_args(path, ME, (winacl.EVERYONE, SOMEONE)) == [
        path,
        "/inheritance:r",
        "/grant:r",
        f"*{ME}:F",
        "*S-1-5-18:F",
        "*S-1-5-32-544:F",
        "/remove:g",
        "*S-1-1-0",
        f"*{SOMEONE}",
    ]
    assert "chmod" not in winacl.recipe(path, ME, (winacl.EVERYONE,))


# ---- icacls --------------------------------------------------------------------------------


def test_lock_runs_icacls_in_list_form_without_a_shell_or_a_window(monkeypatch):
    exe = r"C:\Windows\System32\icacls.exe"
    monkeypatch.setattr(winacl, "icacls", lambda: exe)
    seen = []

    class Done:
        returncode = 0

    def run(args, **kwargs):
        seen.append((args, kwargs))
        return Done()

    monkeypatch.setattr(platform, "is_windows", lambda: True)
    monkeypatch.setattr(winacl.subprocess, "run", run)
    assert winacl.lock(r"C:\x\f.json", ME, (winacl.EVERYONE,)) == 0
    [(args, kwargs)] = seen
    assert args == [exe, *winacl.lock_args(r"C:\x\f.json", ME, (winacl.EVERYONE,))]
    assert kwargs["creationflags"] == 0x08000000 and kwargs["stdin"] is subprocess.DEVNULL
    assert "shell" not in kwargs and kwargs["timeout"] > 0


@pytest.mark.parametrize(
    "error", [FileNotFoundError(2, "no icacls"), subprocess.TimeoutExpired("x", 1)]
)
def test_lock_says_minus_one_when_icacls_cannot_run(monkeypatch, error):
    def run(*args, **kwargs):
        raise error

    monkeypatch.setattr(winacl, "icacls", lambda: r"C:\Windows\System32\icacls.exe")
    monkeypatch.setattr(winacl.subprocess, "run", run)
    assert winacl.lock("f", ME) == -1


def test_icacls_is_only_ever_the_one_in_system32(monkeypatch, tmp_path):
    exe = tmp_path / "System32" / "icacls.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"")
    monkeypatch.setenv("SYSTEMROOT", str(tmp_path))
    assert winacl.icacls() == str(exe)
    # A planted icacls in the current folder or on PATH is never used: missing means no lock.
    planted = tmp_path / "project"
    planted.mkdir()
    (planted / "icacls.exe").write_bytes(b"")
    (planted / "icacls.bat").write_text("echo pwned\n")
    monkeypatch.chdir(planted)
    monkeypatch.setenv("PATH", str(planted))
    monkeypatch.setenv("SYSTEMROOT", str(tmp_path / "nowhere"))
    assert winacl.icacls() is None
    monkeypatch.setenv("SYSTEMROOT", "relative")
    assert winacl.icacls() is None

    def run(*args, **kwargs):
        raise AssertionError("icacls must not run")

    monkeypatch.setattr(winacl.subprocess, "run", run)
    assert winacl.lock(str(planted / "f"), ME) == -1


def test_a_powershell_recipe_keeps_dollar_backtick_percent_and_bang_literal():
    path = "C:\\Users\\ana\\it's $HOME `x` %P% !\\f.json"
    text = winacl.recipe(path, ME, powershell=True)
    assert text == (
        "In PowerShell: icacls 'C:\\Users\\ana\\it''s $HOME `x` %P% !\\f.json' /inheritance:r "
        f"/grant:r *{ME}:F *S-1-5-18:F *S-1-5-32-544:F"
    )


# ---- the ctypes layer against a fake advapi32/kernel32 that returns real memory ---------------


class FakeApi:
    """GetNamedSecurityInfoW and friends, writing into the out-arguments the way Windows does."""

    def __init__(self, aces=(), owner=ME, protected=False, acls=True, elevated=False):
        self.keep: list = []  # memory handed out stays alive
        self.owner = self._buffer(sid_bytes(owner)) if owner else None
        self.dacl = None if aces is None else self._buffer(acl_bytes(*aces))
        self.me = self._buffer(sid_bytes(ME))
        self.protected, self.acls, self.elevated = protected, acls, elevated
        self.sddl = "D:PAI(A;;FA;;;SY)" if protected else "D:AI(A;ID;FA;;;SY)"
        self.code = 0  # what GetNamedSecurityInfoW / SetNamedSecurityInfoW return
        self.fail: set[str] = set()  # BOOL functions that return FALSE
        self.freed: list = []
        self.set_calls: list = []
        self.present = 1

    def _buffer(self, data: bytes) -> int:
        buf = ctypes.create_string_buffer(data)
        self.keep.append(buf)
        return ctypes.addressof(buf)

    def get_named(self, path, kind, info, owner, group, dacl, sacl, descriptor):
        assert (kind, info, group, sacl) == (1, 0x5, None, None)
        owner.value, dacl.value, descriptor.value = self.owner, self.dacl, 0xD5
        return self.code

    def sd_control(self, descriptor, control, revision):
        control.value = 0x1000 if self.protected else 0x8000
        return "sd_control" not in self.fail

    def sd_to_text(self, descriptor, revision, info, text, size):
        assert (revision, info) == (1, 0x4)
        buf = ctypes.create_unicode_buffer(self.sddl)
        self.keep.append(buf)
        text.value = ctypes.addressof(buf)
        return "sd_to_text" not in self.fail

    def text_to_sd(self, text, revision, descriptor, size):
        self.parsed = text
        descriptor.value = 0x5D
        return "text_to_sd" not in self.fail

    def sd_dacl(self, descriptor, present, dacl, defaulted):
        present.value, dacl.value = self.present, 0xDAC1
        return "sd_dacl" not in self.fail

    def set_named(self, path, kind, info, owner, group, dacl, sacl):
        self.set_calls.append((path, kind, info, owner, group, dacl.value, sacl))
        return self.code

    def local_free(self, handle):
        self.freed.append(getattr(handle, "value", handle))

    def current_process(self):
        return -1

    def open_token(self, process, access, token):
        assert (process, access) == (-1, winacl.TOKEN_QUERY)
        token.value = 0x70
        return "open_token" not in self.fail

    def token_info(self, token, kind, buffer, size, needed):
        if kind == winacl.TOKEN_USER and buffer is None:
            needed.value = 16
            return 0  # ERROR_INSUFFICIENT_BUFFER, with the size it needs
        if "token_info" in self.fail:
            return 0
        if kind == winacl.TOKEN_USER:
            ctypes.c_void_p.from_address(buffer).value = self.me  # TOKEN_USER.User.Sid
        else:
            ctypes.c_uint32.from_address(buffer).value = int(self.elevated)
        return 1

    def close(self, handle):
        self.closed = handle.value

    def volume_path(self, path, root, size):
        root.value = "C:\\"
        return "volume_path" not in self.fail

    def volume_info(self, root, name, name_size, serial, length, flags, fs, fs_size):
        flags.value = 0x7 | (winacl.FILE_PERSISTENT_ACLS if self.acls else 0)
        return "volume_info" not in self.fail


@pytest.fixture
def api(monkeypatch):
    def install(**kwargs) -> FakeApi:
        fake = FakeApi(**kwargs)
        monkeypatch.setattr(winacl, "_api", lambda: fake)
        winacl._volume_has_acls.cache_clear()
        return fake

    yield install
    winacl._volume_has_acls.cache_clear()


def test_read_returns_the_owner_entries_protection_and_sddl(api):
    fake = api(
        aces=[
            ace_bytes(winacl.ALLOW, 0, READ, winacl.EVERYONE),
            ace_bytes(winacl.ALLOW, INHERITED, FULL, winacl.SYSTEM),
        ],
        protected=True,
    )
    acl = winacl.read(r"C:\x\f")
    assert acl.owner == ME and acl.protected and acl.sddl == "D:PAI(A;;FA;;;SY)"
    assert acl.aces == ((0, 0, READ, winacl.EVERYONE), (0, INHERITED, FULL, winacl.SYSTEM))
    assert 0xD5 in fake.freed  # the security descriptor is given back


def test_read_reports_a_null_dacl_as_no_list(api):
    api(aces=None)
    assert winacl.read("f").aces is None


def test_read_raises_on_windows_errors(api):
    fake = api()
    fake.code = 5
    with pytest.raises(OSError):
        winacl.read("f")
    for failing in ("sd_control", "sd_to_text"):
        fake = api()
        fake.fail.add(failing)
        with pytest.raises(OSError):
            winacl.read("f")
        assert 0xD5 in fake.freed
    api(owner=None)
    with pytest.raises(ValueError):
        winacl.read("f")


@pytest.mark.parametrize("elevated", [False, True])
def test_current_user_reads_the_token(api, elevated):
    fake = api(elevated=elevated)
    assert winacl.current_user() == (ME, elevated)
    assert fake.closed == 0x70  # the token handle is closed


@pytest.mark.parametrize("failing", ["open_token", "token_info"])
def test_current_user_raises_when_the_token_cannot_be_read(api, failing):
    api().fail.add(failing)
    with pytest.raises(OSError):
        winacl.current_user()


def test_has_acls_reads_the_drive_s_flags(api):
    api()
    assert winacl.has_acls(r"C:\x\f")
    api(acls=False)
    assert not winacl.has_acls(r"C:\x\f")
    for failing in ("volume_path", "volume_info"):
        api().fail.add(failing)
        with pytest.raises(OSError):
            winacl.has_acls(r"C:\x\f")


@pytest.mark.parametrize(
    ("protected", "flag"),
    [(True, winacl.PROTECTED_DACL_SECURITY_INFORMATION), (False, 0x20000000)],
)
def test_restore_puts_the_list_back_protected_or_inheriting(api, protected, flag):
    fake = api(protected=protected)
    winacl.restore(r"C:\x\f", "D:AI(A;;FR;;;WD)")
    assert fake.parsed == "D:AI(A;;FR;;;WD)"
    [(path, kind, info, owner, group, dacl, sacl)] = fake.set_calls
    assert (path, kind, info, dacl) == (
        r"C:\x\f",
        1,
        winacl.DACL_SECURITY_INFORMATION | flag,
        0xDAC1,
    )
    assert (owner, group, sacl) == (None, None, None)  # the owner is never changed
    assert 0x5D in fake.freed


def test_restore_raises_when_the_list_cannot_be_put_back(api):
    for failing in ("text_to_sd", "sd_dacl"):
        api().fail.add(failing)
        with pytest.raises(OSError):
            winacl.restore("f", "D:")
    fake = api()
    fake.present = 0
    with pytest.raises(ValueError):
        winacl.restore("f", "D:")
    fake = api()
    fake.code = 5
    with pytest.raises(OSError):
        winacl.restore("f", "D:")


def test_exposure_puts_it_together(api):
    api(aces=[ace_bytes(winacl.ALLOW, INHERITED, READ_EXECUTE, winacl.USERS)])
    assert winacl.exposure("f", ME, False).readers == (winacl.USERS,)
    api(aces=[], owner=SOMEONE)
    assert winacl.exposure("f", ME, True) is None  # someone else's file is theirs to fix
    api(acls=False)
    assert winacl.exposure("f", ME, False) == winacl.Access(unknown=winacl.NO_ACLS)
    api().code = 5
    assert winacl.exposure("f", ME, False) == winacl.Access(unknown=winacl.UNREADABLE)


def test_a_file_owned_by_administrators_is_not_checked_unless_elevated(api):
    api(aces=[ace_bytes(winacl.ALLOW, 0, READ, winacl.EVERYONE)], owner=winacl.ADMINISTRATORS)
    assert winacl.exposure("f", ME, False) == winacl.Access(unknown=winacl.ADMIN_OWNED)
    assert winacl.exposure("f", ME, True).readers == (winacl.EVERYONE,)


def test_agentprint_imports_on_a_python_without_ctypes():
    code = (
        "import sys\n"
        "sys.modules['ctypes'] = None\n"
        "sys.modules['_ctypes'] = None\n"
        "import agentprint.checks.ap008_readable_by_others, agentprint.fix\n"
        "from agentprint import winacl\n"
        "assert 'ctypes' not in sys.modules or sys.modules['ctypes'] is None\n"
        "try:\n"
        "    winacl.current_user()\n"
        "except OSError:\n"
        "    pass\n"
        "else:\n"
        "    raise SystemExit('no OSError')\n"
        "print(winacl.exposure('f', 'S-1-5-21-1-2-3-1001', False).unknown)\n"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == winacl.UNREADABLE


def test_errors_are_oserrors_on_any_os():
    error = winacl._error(5)
    assert isinstance(error, OSError)
    assert isinstance(winacl._error(), OSError)


def test_every_declared_function_has_known_argument_types():
    for dll, symbol, spec in winacl._CALLS.values():
        assert dll in ("advapi32", "kernel32") and symbol
        assert set(spec.split()) <= winacl._LETTERS, symbol


@pytest.mark.posix_only
def test_the_windows_api_is_not_there_elsewhere():
    with pytest.raises(OSError, match="only be read on Windows"):
        winacl._api()


# ---- real Windows ----------------------------------------------------------------------------


@pytest.mark.windows_only
def test_real_reader_on_a_new_file(tmp_path):
    target = tmp_path / "f.json"
    target.write_text("{}")
    me, elevated = winacl.current_user()
    assert me.startswith(("S-1-5-21-", "S-1-12-1-")), me
    assert winacl.has_acls(str(target))
    acl = winacl.read(str(target))
    assert winacl.owned_by_me(acl.owner, me, elevated), (acl.owner, me, elevated)
    assert acl.aces and acl.sddl.startswith("D:")
    # A new file under the user's profile is private: nobody else is named in its list.
    profile = os.environ.get("USERPROFILE", "").lower()
    if profile and str(target).lower().startswith(profile):
        assert winacl.exposure(str(target), me, elevated) == winacl.Access(), acl.sddl


@pytest.mark.windows_only
def test_real_icacls_lock_removes_everyone_keeps_the_owner_and_restores(tmp_path):
    target = tmp_path / "history.jsonl"
    target.write_text("x\n")
    where = str(target)
    exe = winacl.icacls()
    assert exe, "icacls.exe is not in %SYSTEMROOT%\\System32"
    grant = [exe, where, "/grant", "*S-1-1-0:R"]
    subprocess.run(grant, check=True, capture_output=True, **platform.subprocess_kwargs())
    me, elevated = winacl.current_user()
    before = winacl.read(where)
    exposed = winacl.classify(before.aces, {me, before.owner})
    assert exposed.readers == exposed.explicit == (winacl.EVERYONE,), before.sddl

    assert winacl.lock(where, me, exposed.explicit) == 0
    after = winacl.read(where)
    assert winacl.classify(after.aces, {me, after.owner}) == winacl.Access(), after.sddl
    for right in (winacl.FILE_READ_DATA, winacl.FILE_WRITE_DATA):
        assert winacl.allows(after.aces, {me}, right), after.sddl
    assert after.protected and target.read_text() == "x\n"

    winacl.restore(where, before.sddl)
    assert winacl.read(where).sddl == before.sddl
    assert os.access(where, os.R_OK | os.W_OK)


@pytest.mark.windows_only
def test_real_restore_of_a_protected_list(tmp_path):
    target = tmp_path / "f"
    target.write_text("x")
    where = str(target)
    me, _ = winacl.current_user()
    assert winacl.lock(where, me) == 0
    locked = winacl.read(where)
    winacl.restore(where, "D:AI(A;;FA;;;SY)")  # inheriting again: SYSTEM plus the parent's
    assert not winacl.read(where).protected
    winacl.restore(where, locked.sddl)
    again = winacl.read(where)
    assert (again.aces, again.protected) == (locked.aces, True), again.sddl
