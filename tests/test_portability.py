"""Linux / macOS / Windows branches, unit-tested on the Linux host by selecting
each branch explicitly (platform argument) or monkeypatching sys.platform and
the OS primitive (the struck.win_acl ctypes seam, GetConsoleMode). Real Windows
and macOS runs are NOT claimed (README, PORTABILITY)."""

from __future__ import annotations

import io
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from struck import config, paths, private_file, tty, win_acl
from struck.private_file import checked_executable, checked_node_socket

# SIDs only: names are never consulted (Sec Auditer rounds 6-7).
ME = "S-1-5-21-1111111111-2222222222-3333333333-1001"  # this PC's dd
EVIL_DOMAIN_DD = (
    "S-1-5-21-9999999999-8888888888-7777777777-1001"  # EVIL-DOMAIN\\dd: same name, other SID
)
OTHERPC_MALLORY = "S-1-5-21-4444444444-5555555555-6666666666-1002"  # OTHERPC\\mallory
EVERYONE = "S-1-1-0"  # "Everyone", "Jeder" (de), "Tout le monde" (fr): one SID
USERS = "S-1-5-32-545"
TRUSTED_INSTALLER = win_acl.TRUSTED_INSTALLER_SID
CREATOR_OWNER = "S-1-3-0"
FULL = 0x001F01FF  # icacls (F)
MODIFY = 0x001301BF  # icacls (M)
READ_EXECUTE = 0x001200A9  # icacls (RX)
READ = 0x00120089  # icacls (R)


def _ace(sid: str, mask: int, kind: int = win_acl.ACCESS_ALLOWED_ACE_TYPE) -> win_acl.Ace:
    return win_acl.Ace(kind, 0, mask, sid)


def _private(owner: str = ME) -> win_acl.Security:
    return win_acl.Security(owner, (_ace(owner, FULL),))


class _FakeWin:
    """Stands in for the ctypes Win32 calls (struck.win_acl.WinApi). A handle
    snapshots the file's attributes, ACL and bytes AT OPEN, as a real handle
    pins the opened object; later path swaps do not change what it reads."""

    def __init__(self) -> None:
        self.security: dict[str, win_acl.Security] = {}
        self.reparse: set[str] = set()
        self.links: dict[str, int] = {}
        self.calls: list[tuple[str, object]] = []
        self.after_open: list[str] = []  # paths to swap to a reparse point right after open
        self._handles: dict[int, tuple[str, win_acl.HandleInfo, win_acl.Security, bytes]] = {}

    def token_user_sid(self) -> str:
        return ME

    def open_no_follow(self, path: str, data: bool) -> int:
        p = Path(path)
        if not os.path.lexists(p):
            raise FileNotFoundError(2, "not found", path)
        attrs = win_acl.FILE_ATTRIBUTE_DIRECTORY if p.is_dir() else 0
        if path in self.reparse:
            attrs |= win_acl.FILE_ATTRIBUTE_REPARSE_POINT
        body = p.read_bytes() if data and p.is_file() else b""
        info = win_acl.HandleInfo(attrs, self.links.get(path, 1))
        handle = 100 + len(self._handles)
        self._handles[handle] = (path, info, self.security.get(path, _private()), body)
        self.calls.append(("open", path))
        if path in self.after_open:  # the attacker swaps the path for a link + new bytes
            p.write_bytes(b"EVIL")
            self.reparse.add(path)
        return handle

    def handle_info(self, handle: int) -> win_acl.HandleInfo:
        self.calls.append(("info", handle))
        return self._handles[handle][1]

    def security_of_handle(self, handle: int) -> win_acl.Security:
        self.calls.append(("security", handle))
        return self._handles[handle][2]

    def security_of_path(self, path: str) -> win_acl.Security:
        raise AssertionError("policy code must check the opened handle, not the path")

    def set_owner_only(self, path: str, owner_sid: str, directory: bool) -> None:
        self.calls.append(("set_owner_only", (path, owner_sid, directory)))
        self.security[path] = _private(owner_sid)

    def create_owner_only(self, path: str, owner_sid: str) -> int:
        with open(path, "xb"):  # CREATE_NEW: an existing path fails
            pass
        self.security[path] = _private(owner_sid)
        self.calls.append(("create_owner_only", (path, owner_sid)))
        handle = 100 + len(self._handles)
        info = win_acl.HandleInfo(0, 1)
        self._handles[handle] = (path, info, self.security[path], b"")
        return handle

    def write_handle(self, handle: int, data: bytes) -> None:
        path = self._handles[handle][0]
        assert self.security.get(path) == _private(ME), "data written before the owner-only DACL"
        Path(path).write_bytes(data)
        self.calls.append(("write", handle))

    def read_handle(self, handle: int) -> bytes:
        self.calls.append(("read", handle))
        return self._handles[handle][3]

    def close(self, handle: int) -> None:
        self.calls.append(("close", handle))


def _win(monkeypatch: pytest.MonkeyPatch) -> _FakeWin:
    fake = _FakeWin()

    def api() -> win_acl.WinApi:
        return fake

    monkeypatch.setattr(private_file, "_api", api)
    return fake


def test_windows_private_write_sets_and_verifies_owner_only_dacl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _win(monkeypatch)
    monkeypatch.setattr(sys, "platform", "win32")
    path = tmp_path / "Struck" / "llm.json"
    private_file.write_private(path, b"{}")
    assert path.read_bytes() == b"{}"
    sets = [c[1] for c in fake.calls if c[0] == "set_owner_only"]
    assert sets == [(str(path.parent), ME, True)]  # new folder: owner-only, inheritable
    kinds = [c[0] for c in fake.calls]
    # the temp file is CREATED owner-only, checked on its handle, THEN written
    created = kinds.index("create_owner_only")
    assert kinds[created + 1 : created + 3] == ["security", "write"]
    assert ("open", str(path)) in fake.calls  # read back after os.replace
    assert private_file.read_private(path) == b"{}"


@pytest.mark.parametrize(
    "ace",
    [
        _ace(USERS, READ),
        _ace(EVERYONE, READ),
        _ace(EVIL_DOMAIN_DD, READ),  # EVIL-DOMAIN\dd: same NAME as you, other SID
        _ace(OTHERPC_MALLORY, MODIFY),
    ],
)
def test_windows_private_read_refuses_any_sid_outside_the_allow_list(
    ace: win_acl.Ace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "llm.json"
    path.write_bytes(b"{}")
    fake = _win(monkeypatch)
    fake.security[str(path)] = win_acl.Security(ME, (_ace(ME, FULL), ace))
    with pytest.raises(ValueError, match="not private"):
        private_file._win_read(path)
    assert not any(c[0] == "read" for c in fake.calls)


def test_windows_private_read_accepts_system_and_administrators(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "llm.json"
    path.write_bytes(b"{}")
    fake = _win(monkeypatch)
    fake.security[str(path)] = win_acl.Security(
        ME, (_ace(ME, FULL), _ace(win_acl.SYSTEM_SID, FULL), _ace(win_acl.ADMINISTRATORS_SID, FULL))
    )
    assert private_file._win_read(path) == b"{}"


def test_probe_logname_everyone_names_are_never_trusted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Probe LOGNAME=Everyone: the old getpass name match accepted an Everyone ACE
    when the login name was "Everyone". Identity is now the token SID only."""
    for var in ("LOGNAME", "USER", "USERNAME", "LNAME"):
        monkeypatch.setenv(var, "Everyone")
    path = tmp_path / "llm.json"
    path.write_bytes(b"{}")
    fake = _win(monkeypatch)
    fake.security[str(path)] = win_acl.Security(ME, (_ace(EVERYONE, FULL),))
    with pytest.raises(ValueError, match="not private"):
        private_file._win_read(path)
    src = Path(private_file.__file__).read_text(encoding="utf-8")
    src += Path(win_acl.__file__).read_text(encoding="utf-8")
    for banned in (
        "import getpass",
        "import subprocess",
        "icacls.exe",
        "os.environ",
        '"SystemRoot"',
    ):
        assert banned not in src  # identity = token SID; no name, no env, no icacls


def test_probe_evil_domain_dd_owner_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Probe EVIL-DOMAIN\\dd: a file OWNED by another domain's "dd" is not yours."""
    path = tmp_path / "network.json"
    path.write_bytes(b"{}")
    fake = _win(monkeypatch)
    fake.security[str(path)] = _private(EVIL_DOMAIN_DD)
    with pytest.raises(ValueError, match=f"owned by {EVIL_DOMAIN_DD}"):
        private_file._win_read(path)
    exe = tmp_path / "cardano-cli.exe"
    exe.write_bytes(b"")
    fake.security[str(exe)] = _private(EVIL_DOMAIN_DD)
    with pytest.raises(ValueError, match=f"owned by {EVIL_DOMAIN_DD}"):
        checked_executable(str(exe), "win32")


def test_probe_jeder_full_is_refused_on_exe_and_its_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Probe Jeder:(F): German "Everyone" is the same SID S-1-1-0; names are not read."""
    d = tmp_path / "bin"
    d.mkdir()
    exe = d / "cardano-cli.exe"
    exe.write_bytes(b"")
    fake = _win(monkeypatch)
    fake.security[str(exe)] = win_acl.Security(ME, (_ace(ME, FULL), _ace(EVERYONE, FULL)))
    with pytest.raises(ValueError, match=f"writable by a SID outside.*{EVERYONE}"):
        checked_executable(str(exe), "win32")
    fake.security[str(exe)] = _private()
    fake.security[str(d)] = win_acl.Security(ME, (_ace(ME, FULL), _ace(EVERYONE, FULL)))
    with pytest.raises(ValueError, match=f"{d}.*writable by a SID outside"):
        checked_executable(str(exe), "win32")


def test_probe_otherpc_mallory_modify_on_the_exe_folder_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Probe OTHERPC\\mallory:(M) on the folder: she could swap the binary."""
    d = tmp_path / "bin"
    d.mkdir()
    exe = d / "cardano-cli.exe"
    exe.write_bytes(b"")
    fake = _win(monkeypatch)
    fake.security[str(d)] = win_acl.Security(ME, (_ace(ME, FULL), _ace(OTHERPC_MALLORY, MODIFY)))
    with pytest.raises(ValueError, match=OTHERPC_MALLORY):
        checked_executable(str(exe), "win32")
    with pytest.raises(ValueError, match=OTHERPC_MALLORY):
        private_file.not_group_writable(d, "win32")  # the config folder: same check


def test_windows_exe_allow_list_accepts_owner_system_admins_and_read_only_users(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exe = tmp_path / "cardano-cli.exe"
    exe.write_bytes(b"")
    fake = _win(monkeypatch)
    fake.security[str(exe)] = win_acl.Security(
        win_acl.ADMINISTRATORS_SID,
        (
            _ace(win_acl.SYSTEM_SID, FULL),
            _ace(win_acl.ADMINISTRATORS_SID, FULL),
            _ace(USERS, READ_EXECUTE),
            _ace(EVERYONE, FULL, win_acl.ACCESS_DENIED_ACE_TYPE),  # a deny removes rights
        ),
    )
    assert checked_executable(str(exe), "win32") == str(exe)
    fake.security[str(exe)] = win_acl.Security(ME, (_ace(OTHERPC_MALLORY, MODIFY),))
    with pytest.raises(ValueError, match="writable by a SID outside"):
        checked_executable(str(exe), "win32")
    fake.security[str(exe)] = win_acl.Security(ME, None)
    with pytest.raises(ValueError, match="NULL DACL"):
        checked_executable(str(exe), "win32")
    fake.security[str(exe)] = win_acl.Security(ME, (win_acl.Ace(0x5, 0, 0, ""),))
    with pytest.raises(ValueError, match="not parsed"):
        checked_executable(str(exe), "win32")


def test_windows_reparse_points_refused_on_file_and_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "d" / "network.json"
    path.parent.mkdir()
    path.write_bytes(b"{}")
    fake = _win(monkeypatch)
    fake.reparse = {str(path)}
    with pytest.raises(ValueError, match="reparse point"):
        private_file._win_read(path)
    with pytest.raises(ValueError, match="reparse point"):
        private_file._win_write(path, b"x")
    fake.reparse = {str(path.parent)}
    with pytest.raises(ValueError, match="reparse point"):
        private_file._win_read(path)
    with pytest.raises(ValueError, match="reparse point"):
        checked_executable(str(path), "win32")
    assert path.read_bytes() == b"{}"  # never written over


def test_probe_reparse_swap_after_the_check_reads_the_checked_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Probe reparse swap: the path is swapped for a link (with other bytes) right
    after CreateFileW. Attributes, ACL and bytes all come from the ONE handle that
    was opened with OPEN_REPARSE_POINT, so the swap cannot inject."""
    path = tmp_path / "network.json"
    path.write_bytes(b"GOOD")
    fake = _win(monkeypatch)
    fake.after_open = [str(path)]
    assert private_file._win_read(path) == b"GOOD"
    handle = next(h for op, h in fake.calls if op == "info")
    assert [op for op, h in fake.calls if h == handle] == ["info", "security", "read", "close"]
    assert fake.calls.count(("open", str(path))) == 1  # never reopened by path
    fake.after_open = []
    with pytest.raises(ValueError, match="reparse point"):  # the swapped path now: refused
        private_file._win_read(path)


def test_windows_hard_link_and_directory_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "llm.json"
    path.write_bytes(b"{}")
    fake = _win(monkeypatch)
    fake.links[str(path)] = 2
    with pytest.raises(ValueError, match="hard links"):
        private_file._win_read(path)
    (tmp_path / "dir.json").mkdir()
    with pytest.raises(ValueError, match="regular file"):
        private_file._win_read(tmp_path / "dir.json")


def test_posix_private_files_stay_strict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    path = tmp_path / "s" / "llm.json"
    private_file.write_private(path, b"{}")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.chmod(0o644)
    with pytest.raises(ValueError, match="must be 600"):
        private_file.read_private(path)


def test_tty_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(sys, "stdin", Tty(""))
    opened: list[str] = []

    def fake_open(name: str, encoding: str = "") -> io.StringIO:
        opened.append(name)
        assert encoding == "utf-8"
        return io.StringIO("approve x\n")

    monkeypatch.setattr(tty, "open", fake_open, raising=False)
    assert tty.read_human_line("linux") == "approve x" and opened[-1] == "/dev/tty"
    assert tty.read_human_line("darwin") == "approve x" and opened[-1] == "/dev/tty"

    # Windows: isatty() alone is NOT trusted; GetConsoleMode must succeed too.
    def no_console(stream: object) -> bool:
        return False

    def console(stream: object) -> bool:
        return True

    monkeypatch.setattr(tty, "_windows_console_mode_ok", no_console)
    with pytest.raises(EOFError, match="GetConsoleMode failed"):
        tty.read_human_line("win32")
    monkeypatch.setattr(tty, "_windows_console_mode_ok", console)
    assert tty.read_human_line("win32") == "approve x" and opened[-1] == "CONIN$"
    monkeypatch.setattr(sys, "stdin", io.StringIO("approve x\n"))  # piped
    for platform in ("linux", "darwin", "win32"):
        with pytest.raises(EOFError, match="not a terminal"):
            tty.read_human_line(platform)


def test_node_socket_per_os() -> None:
    assert checked_node_socket("/run/cardano/node.socket", "linux")
    assert checked_node_socket("/Users/x/node.socket", "darwin")
    assert checked_node_socket("\\\\.\\pipe\\cardano-node", "win32")
    for bad in (
        "\\\\server\\pipe\\cardano-node",  # remote / UNC
        "\\\\.\\pipe\\a\\b",
        "//./pipe/cardano-node",
        "\\\\.\\pipe\\",
        "C:\\node.socket",
    ):
        with pytest.raises(ValueError, match="LOCAL named pipe"):
            checked_node_socket(bad, "win32")
    for bad in ("\\\\.\\pipe\\cardano-node", "relative/node.socket"):
        with pytest.raises(ValueError, match="absolute Unix socket"):
            checked_node_socket(bad, "linux")


def test_executables_only_from_the_configured_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exe = tmp_path / "cardano-cli"
    exe.write_bytes(b"")
    exe.chmod(0o755)
    assert checked_executable(str(exe), "linux") == str(exe)
    with pytest.raises(ValueError, match="absolute"):
        checked_executable("cardano-cli", "linux")  # never a PATH lookup
    with pytest.raises(ValueError, match="regular file"):
        checked_executable(str(tmp_path), "linux")
    exe.chmod(0o777)
    with pytest.raises(ValueError, match="group/other-writable"):
        checked_executable(str(exe), "darwin")
    exe.chmod(0o755)
    fake = _win(monkeypatch)
    assert checked_executable(str(exe), "win32") == str(exe)
    fake.security[str(exe)] = win_acl.Security(ME, (_ace(EVERYONE, MODIFY),))
    with pytest.raises(ValueError, match="writable by a SID outside"):
        checked_executable(str(exe), "win32")


def test_user_config_dir_per_os() -> None:
    home = Path("H")
    assert paths.user_config_dir("linux", home) == home / ".config" / "struck"
    assert paths.user_config_dir("darwin", home) == (
        home / "Library" / "Application Support" / "Struck"
    )
    assert paths.user_config_dir("win32", home) == home / "AppData" / "Roaming" / "Struck"


def test_nothing_resolves_against_the_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    before = (config.CONFIG_PATH, config.RECEIPTS_DIR, config.CALL_HISTORY_PATH)
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    assert all(p.is_absolute() for p in before)
    assert paths.REPO_ROOT == Path(config.__file__).resolve().parents[1]
    assert config.CONFIG_PATH.parent.parent == paths.REPO_ROOT
    out = subprocess.run(
        [sys.executable, "-c", "from struck import config; print(config.CONFIG_PATH)"],
        cwd=cwd,
        capture_output=True,
        check=True,
        env={**os.environ, "PYTHONPATH": str(paths.REPO_ROOT)},
    ).stdout.decode("utf-8")
    assert out.strip() == str(before[0])  # a process started elsewhere: same files
    assert list(cwd.iterdir()) == []  # nothing was created in the cwd


def test_executable_folder_must_not_be_group_writable(tmp_path: Path) -> None:
    d = tmp_path / "bin"
    d.mkdir(mode=0o755)
    exe = d / "cardano-cli"
    exe.write_bytes(b"")
    exe.chmod(0o755)
    assert checked_executable(str(exe), "linux") == str(exe)
    d.chmod(0o775)
    with pytest.raises(ValueError, match="group/other-writable"):
        checked_executable(str(exe), "linux")


def test_config_file_and_folder_permissions_are_checked(tmp_path: Path) -> None:
    src = next(p for p in tmp_path.iterdir() if p.name == "conftest-struck-config.json")
    d = tmp_path / "cfgdir"
    d.mkdir(mode=0o700)
    path = d / "struck.json"
    path.write_bytes(src.read_bytes())
    path.chmod(0o600)
    assert config.load_config(path).network == "preprod"
    path.chmod(0o620)
    with pytest.raises(config.ConfigError, match="group/other-writable"):
        config.load_config(path)
    path.chmod(0o600)
    d.chmod(0o770)
    with pytest.raises(config.ConfigError, match="group/other-writable"):
        config.load_config(path)


def test_program_files_cardano_cli_passes_trustedinstaller_and_inherit_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 8 (1)+(2): the stock C:\\Program Files ACL: TrustedInstaller owner with
    full control, SYSTEM / Administrators full, Users read+execute, and the
    INHERIT_ONLY ``CREATOR OWNER`` / SYSTEM / Administrators ACEs for children."""
    d = tmp_path / "Program Files" / "Cardano"
    d.mkdir(parents=True)
    exe = d / "cardano-cli.exe"
    exe.write_bytes(b"")
    io_ = win_acl.INHERIT_ONLY_ACE | 0x3  # OI|CI|IO
    base: tuple[win_acl.Ace, ...] = (
        _ace(TRUSTED_INSTALLER, FULL),
        _ace(win_acl.SYSTEM_SID, FULL),
        _ace(win_acl.ADMINISTRATORS_SID, FULL),
        _ace(USERS, READ_EXECUTE),
    )
    inherit_only: tuple[win_acl.Ace, ...] = (
        win_acl.Ace(win_acl.ACCESS_ALLOWED_ACE_TYPE, io_, FULL, CREATOR_OWNER),
        win_acl.Ace(win_acl.ACCESS_ALLOWED_ACE_TYPE, io_, 0x10000000, USERS),  # GA, inherit-only
    )
    fake = _win(monkeypatch)
    fake.security[str(d)] = win_acl.Security(TRUSTED_INSTALLER, base + inherit_only)
    fake.security[str(exe)] = win_acl.Security(TRUSTED_INSTALLER, base)
    assert checked_executable(str(exe), "win32") == str(exe)
    # the SAME ACE without INHERIT_ONLY applies to the folder itself: refused
    fake.security[str(d)] = win_acl.Security(TRUSTED_INSTALLER, (*base, _ace(CREATOR_OWNER, FULL)))
    with pytest.raises(ValueError, match=CREATOR_OWNER):
        checked_executable(str(exe), "win32")
    # a private file still refuses a non-inherit-only foreign ACE
    path = tmp_path / "llm.json"
    path.write_bytes(b"{}")
    fake.security[str(path)] = win_acl.Security(ME, (_ace(ME, FULL), _ace(USERS, READ)))
    with pytest.raises(ValueError, match="not private"):
        private_file._win_read(path)
    fake.security[str(path)] = win_acl.Security(ME, (_ace(ME, FULL), _ace(TRUSTED_INSTALLER, READ)))
    assert private_file._win_read(path) == b"{}"


def test_private_folder_with_an_inherit_only_everyone_ace_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 9 (1): an INHERIT_ONLY grant on a PRIVATE folder reaches every file
    created in it later, so _owner_only counts it (only the executable/config
    not-writable-by-others check skips inherit-only ACEs)."""
    folder = tmp_path / "Struck"
    folder.mkdir()
    fake = _win(monkeypatch)
    inherit_only = win_acl.Ace(
        win_acl.ACCESS_ALLOWED_ACE_TYPE, win_acl.INHERIT_ONLY_ACE | 0x3, FULL, EVERYONE
    )
    fake.security[str(folder)] = win_acl.Security(ME, (_ace(ME, FULL), inherit_only))
    with pytest.raises(ValueError, match="not private"):
        private_file._win_private_dir(folder)
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(ValueError, match="not private"):
        private_file.write_private(folder / "secrets.cfg", b"x")
    assert not (folder / "secrets.cfg").exists()
    assert not any(c[0] in ("create_owner_only", "write") for c in fake.calls)


def test_windows_write_failure_leaves_no_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _win(monkeypatch)
    monkeypatch.setattr(sys, "platform", "win32")

    def boom(handle: int, data: bytes) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(fake, "write_handle", boom)
    path = tmp_path / "Struck" / "secrets.cfg"
    with pytest.raises(OSError, match="disk full"):
        private_file.write_private(path, b"x")
    assert sorted(p.name for p in path.parent.iterdir()) == []
