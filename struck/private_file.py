"""Per-user private settings files (``llm.json``, ``network.json`` in
``struck.paths.user_config_dir()``): ONE implementation, equally strict on
every OS, with the OS-appropriate mechanism. Any violation raises
``ValueError`` naming the fix; nothing is ever silently skipped.

POSIX (Linux, macOS):
* read: ``O_RDONLY | O_NOFOLLOW`` + ``fstat``: a regular file owned by you,
  mode exactly 0600; its parent a real directory owned by you, not
  group/other-writable. Missing file = None.
* write: refuse a symlinked path or parent; ``mkstemp`` in the parent,
  ``fchmod 0600``, full write, ``fsync``, ``os.replace``, parent ``fsync``.

Windows (no O_NOFOLLOW, no POSIX modes; ``struck.win_acl``, ctypes advapi32 /
kernel32, no icacls, no SystemRoot, no user names):
* "you" = the process token's user SID (OpenProcessToken + TokenUser);
* every object is opened with ``CreateFileW`` + ``FILE_FLAG_OPEN_REPARSE_POINT``
  (+ BACKUP_SEMANTICS); a reparse point (symlink, junction, mount point) is
  refused from ``GetFileInformationByHandle`` on THAT handle, and the file is
  read from THAT same handle (no check-then-reopen window);
* private file / folder: owner SID == you, a DACL present (no NULL DACL), and
  every ALLOW ACE that applies to the object (INHERIT_ONLY ones are skipped)
  for a SID in the allow-list {you, SYSTEM S-1-5-18, Administrators
  S-1-5-32-544, TrustedInstaller S-1-5-80-956008885-...}; any other SID (Everyone
  in any language, another domain's or PC's user of the same NAME) = refused;
  an ACE type Struck does not parse = refused; file link count must be 1;
* write: ``mkstemp`` in the checked folder, full write, ``fsync``, then an
  owner-only PROTECTED DACL (one ACE: you, full) via ``SetNamedSecurityInfoW``,
  check, ``os.replace``, check again.
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile
from pathlib import Path

from . import win_acl
from .win_acl import (
    ACCESS_ALLOWED_ACE_TYPE,
    ACCESS_DENIED_ACE_TYPE,
    ADMINISTRATORS_SID,
    INHERIT_ONLY_ACE,
    FILE_ATTRIBUTE_DIRECTORY,
    FILE_ATTRIBUTE_REPARSE_POINT,
    SYSTEM_SID,
    TRUSTED_INSTALLER_SID,
    WRITE_MASK,
    Security,
    WinApi,
)

# ── POSIX ─────────────────────────────────────────────────────────────────


if sys.platform != "win32":
    # O_NOFOLLOW / getuid / fchmod / O_DIRECTORY exist only here.

    def _posix_private_dir(parent: Path) -> None:
        st = parent.lstat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
            raise ValueError(f"{parent} must be a real directory (not a symlink) owned by you")
        if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise ValueError(f"{parent} is group/other-writable: chmod go-w {parent}")

    def _posix_read(path: Path) -> bytes | None:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            return None
        except OSError as exc:  # ELOOP: a symlink
            raise ValueError(
                f"{path} refused ({exc.strerror}): must be a regular file, not a symlink"
            ) from exc
        with os.fdopen(fd, "rb") as fh:
            st = os.fstat(fh.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
                raise ValueError(f"{path} must be a regular file owned by you")
            if stat.S_IMODE(st.st_mode) != 0o600:
                raise ValueError(
                    f"{path} has mode {stat.S_IMODE(st.st_mode):o}, must be 600: chmod 600 {path}"
                )
            _posix_private_dir(path.parent)
            return fh.read()

    def _posix_write(path: Path, data: bytes) -> None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _posix_private_dir(path.parent)
        try:
            if stat.S_ISLNK(path.lstat().st_mode):
                raise ValueError(f"{path} is a symlink: refusing to write through or over it")
        except FileNotFoundError:
            pass
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp")
        try:
            os.fchmod(fd, 0o600)
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view) :]
            os.fsync(fd)
        except BaseException:
            os.close(fd)
            os.unlink(tmp)
            raise
        os.close(fd)
        try:
            os.replace(tmp, path)
        except BaseException:
            os.unlink(tmp)
            raise
        dfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)

else:

    def _posix_unavailable(*_: object) -> None:
        raise ValueError("POSIX private-file path called on Windows")  # dispatch never does

    _posix_private_dir = _posix_unavailable
    _posix_write = _posix_unavailable

    def _posix_read(path: Path) -> bytes | None:
        raise ValueError("POSIX private-file path called on Windows")


# ── Windows ───────────────────────────────────────────────────────────────


def _api() -> WinApi:
    return win_acl.win_api()  # tests replace this seam (ctypes mocked)


def _allowed(me: str) -> frozenset[str]:
    """The ONLY SIDs that may hold any right (private) or any write right (others)."""
    return frozenset({me, SYSTEM_SID, ADMINISTRATORS_SID, TRUSTED_INSTALLER_SID})


def _acl_violations(sec: Security, me: str, rights: int, *, skip_inherit_only: bool) -> list[str]:
    """ALLOW ACEs granting ``rights`` to a SID outside the allow-list; unparsed ACE
    types and a NULL DACL are violations.

    ``skip_inherit_only``: True ONLY for the not-writable-by-others check of an
    executable/config and its folder, where an INHERIT_ONLY ACE (e.g. CREATOR
    OWNER S-1-3-0 on Program Files) grants nothing on the object itself. A
    PRIVATE file or folder (``_owner_only``) counts them: an inheritable grant to
    another SID would reach every file later created in a private folder."""
    if sec.dacl is None:
        return ["NULL DACL (everyone: full access)"]
    bad: list[str] = []
    allowed = _allowed(me)
    for ace in sec.dacl:
        if ace.kind == ACCESS_DENIED_ACE_TYPE:
            continue  # a deny only removes rights
        if skip_inherit_only and ace.flags & INHERIT_ONLY_ACE:
            continue  # applies only to children created later, not to this object
        if ace.kind != ACCESS_ALLOWED_ACE_TYPE:
            bad.append(f"ACE type {ace.kind:#x} (not parsed: refused)")
        elif ace.mask & rights and ace.sid not in allowed:
            bad.append(f"{ace.sid} mask {ace.mask:#010x}")
    return bad


def _open_checked(api: WinApi, path: Path, data: bool, directory: bool | None) -> int:
    """CreateFileW(OPEN_REPARSE_POINT) handle, checked ON that handle: not a reparse
    point; a directory iff ``directory`` (None = either); a file has one link."""
    handle = api.open_no_follow(str(path), data)
    try:
        info = api.handle_info(handle)
        if info.attributes & FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError(f"{path} is a reparse point (symlink/junction/mount): refusing")
        is_dir = bool(info.attributes & FILE_ATTRIBUTE_DIRECTORY)
        if directory is not None and is_dir != directory:
            raise ValueError(f"{path} must be a {'directory' if directory else 'regular file'}")
        if not is_dir and info.links != 1:
            raise ValueError(f"{path} has {info.links} hard links: refusing")
    except BaseException:
        api.close(handle)
        raise
    return handle


def _owner_only(path: Path, sec: Security, me: str) -> None:
    if sec.owner != me:
        raise ValueError(f"{path} is owned by {sec.owner}, not by you ({me}): refusing")
    bad = _acl_violations(sec, me, ~0, skip_inherit_only=False)
    if bad:
        raise ValueError(
            f"{path} ACL is not private (only {sorted(_allowed(me))} may have access; "
            f"found {bad}): refusing"
        )


def _win_private_dir(parent: Path) -> None:
    api = _api()
    me = api.token_user_sid()
    try:
        handle = _open_checked(api, parent, data=False, directory=True)
    except FileNotFoundError:
        parent.mkdir(parents=True, exist_ok=True)
        api.set_owner_only(str(parent), me, directory=True)
        handle = _open_checked(api, parent, data=False, directory=True)
    try:
        _owner_only(parent, api.security_of_handle(handle), me)
    finally:
        api.close(handle)


def _win_read(path: Path) -> bytes | None:
    api = _api()
    me = api.token_user_sid()
    try:
        handle = _open_checked(api, path, data=True, directory=False)
    except FileNotFoundError:
        return None
    try:
        _owner_only(path, api.security_of_handle(handle), me)
        _win_private_dir(path.parent)
        return api.read_handle(handle)  # the SAME handle that was checked
    finally:
        api.close(handle)


def _win_check_file(api: WinApi, path: Path, me: str) -> None:
    handle = _open_checked(api, path, data=False, directory=False)
    try:
        _owner_only(path, api.security_of_handle(handle), me)
    finally:
        api.close(handle)


def _win_write(path: Path, data: bytes) -> None:
    _win_private_dir(path.parent)
    api = _api()
    me = api.token_user_sid()
    try:
        api.close(_open_checked(api, path, data=False, directory=False))
    except FileNotFoundError:
        pass  # new file; an existing reparse point / directory / hard link was refused
    # The temp file is CREATED with an owner-only protected DACL (CreateFileW
    # CREATE_NEW + SECURITY_ATTRIBUTES): no byte is written before the ACL holds,
    # and it never exists with an inherited ACL.
    tmp = str(path.parent / f".{path.stem}.{os.urandom(8).hex()}.tmp")
    handle = api.create_owner_only(tmp, me)
    try:
        try:
            _owner_only(Path(tmp), api.security_of_handle(handle), me)
            api.write_handle(handle, data)  # WriteFile + FlushFileBuffers
        finally:
            api.close(handle)
        _win_check_file(api, Path(tmp), me)
        os.replace(tmp, path)
    except BaseException:
        if os.path.lexists(tmp):
            os.unlink(tmp)
        raise
    _win_check_file(api, path, me)  # read back after the move


# ── dispatch ──────────────────────────────────────────────────────────────


def read_private(path: Path) -> bytes | None:
    if sys.platform == "win32":
        return _win_read(path)
    else:
        return _posix_read(path)


def write_private(path: Path, data: bytes) -> None:
    if sys.platform == "win32":
        _win_write(path, data)
    else:
        _posix_write(path, data)


def private_dir(parent: Path) -> None:
    if sys.platform == "win32":
        _win_private_dir(parent)
    else:
        _posix_private_dir(parent)


# ── executables Struck runs (cardano-cli) and the config: no one else may write ──


def _win_not_writable_by_others(path: Path) -> None:
    """Owner and every ALLOW ACE with a write right must be in the SID allow-list."""
    api = _api()
    me = api.token_user_sid()
    handle = _open_checked(api, path, data=False, directory=None)
    try:
        sec = api.security_of_handle(handle)
    finally:
        api.close(handle)
    allowed = _allowed(me)
    if sec.owner not in allowed:
        raise ValueError(
            f"{path} is owned by {sec.owner} (the owner can rewrite its ACL); only "
            f"{sorted(allowed)} are accepted: refusing"
        )
    bad = _acl_violations(sec, me, WRITE_MASK, skip_inherit_only=True)
    if bad:
        raise ValueError(f"{path} is writable by a SID outside {sorted(allowed)} ({bad}): refusing")


def checked_executable(value: str, platform: str = sys.platform) -> str:
    """An absolute path to an existing REGULAR file that only its owner/admins can
    modify, in a folder only they can modify (a writable folder lets anyone swap
    the binary). POSIX: neither group/other-writable. Windows: owner and every
    write ACE in the SID allow-list, file AND folder, opened without following
    reparse points. No PATH lookup."""
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"{value!r} must be an absolute path (no PATH lookup)")
    try:
        st = path.stat()
    except OSError as exc:
        raise ValueError(f"{value!r} is not readable: {exc.strerror}") from exc
    if not stat.S_ISREG(st.st_mode):
        raise ValueError(f"{value!r} is not a regular file")
    if platform == "win32":
        _win_not_writable_by_others(path)
    elif st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ValueError(f"{value!r} is group/other-writable: refusing to run it")
    not_group_writable(path.parent, platform)
    return value


def not_group_writable(path: Path, platform: str = sys.platform) -> None:
    """POSIX: refuse a file/dir that is group/other-writable (``chmod go-w``).
    Windows: refuse unless its owner and every write ACE are in the SID
    allow-list (you, SYSTEM, Administrators, TrustedInstaller), opened without following reparse
    points."""
    if platform == "win32":
        _win_not_writable_by_others(path)
        return
    mode = path.stat().st_mode
    if mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ValueError(f"{path} is group/other-writable: run `chmod go-w {path}`")


_PIPE_NAME = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
LOCAL_PIPE_PREFIX = "\\\\.\\pipe\\"  # \\.\pipe\  (local named pipe namespace)


def checked_node_socket(value: str, platform: str = sys.platform) -> str:
    """The node's local IPC endpoint: a Unix socket path (absolute) on Linux and
    macOS; on Windows ONLY a local named pipe ``\\\\.\\pipe\\<name>`` (a UNC or
    remote ``\\\\server\\pipe\\...`` is refused). Passed to cardano-cli the same
    way on every OS (``--socket-path`` / CARDANO_NODE_SOCKET_PATH)."""
    if platform == "win32":
        name = value[len(LOCAL_PIPE_PREFIX) :] if value.startswith(LOCAL_PIPE_PREFIX) else ""
        if not name or not set(name) <= _PIPE_NAME:
            raise ValueError(
                f"node socket {value!r}: on Windows only a LOCAL named pipe "
                f"{LOCAL_PIPE_PREFIX}<name> is accepted (no UNC/remote pipe)"
            )
        return value
    if not Path(value).is_absolute() or value.startswith("\\\\"):
        raise ValueError(f"node socket {value!r} must be an absolute Unix socket path")
    return value
