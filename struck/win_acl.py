"""Windows file security through the OS API itself (ctypes: advapi32 + kernel32),
no icacls, no SystemRoot, no user NAMES anywhere (Sec Auditer rounds 6-7).

* who am I: ``OpenProcessToken`` + ``GetTokenInformation(TokenUser)`` -> the
  process token's user SID (never ``getpass``/USERNAME/LOGNAME);
* who may touch a file: ``GetSecurityInfo`` (on an open handle) or
  ``GetNamedSecurityInfoW`` -> owner SID + DACL ACEs (type, flags, mask, SID);
* set owner-only: ``SetNamedSecurityInfoW`` with a PROTECTED DACL holding ONE
  ACE (the token user, FILE_ALL_ACCESS) and the token user as owner;
* open without following links: ``CreateFileW`` with
  ``FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_BACKUP_SEMANTICS`` (directories
  open too); ``GetFileInformationByHandle`` on THAT handle gives attributes
  and link count; ``ReadFile`` reads from THAT handle.

Constants and structure layouts are the documented Win32 values
(learn.microsoft.com: winnt.h / fileapi.h / securitybaseapi.h / aclapi.h;
cited in the golden provenance record). ``WinApi`` is the seam the policy code in
``struck.private_file`` uses; tests replace it (ctypes mocked).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Final, Protocol

SYSTEM_SID: Final = "S-1-5-18"  # NT AUTHORITY\SYSTEM (well-known SID)
ADMINISTRATORS_SID: Final = "S-1-5-32-544"  # BUILTIN\Administrators (well-known SID)
# NT SERVICE\TrustedInstaller (service SID; owns C:\Program Files and System32)
TRUSTED_INSTALLER_SID: Final = "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"

ACCESS_ALLOWED_ACE_TYPE: Final = 0x0
ACCESS_DENIED_ACE_TYPE: Final = 0x1
INHERIT_ONLY_ACE: Final = 0x08

FILE_ATTRIBUTE_DIRECTORY: Final = 0x10
FILE_ATTRIBUTE_REPARSE_POINT: Final = 0x400

# Rights that let a principal change a file/folder, its contents, or who may.
FILE_WRITE_DATA: Final = 0x0002  # (dirs: FILE_ADD_FILE)
FILE_APPEND_DATA: Final = 0x0004  # (dirs: FILE_ADD_SUBDIRECTORY)
FILE_WRITE_EA: Final = 0x0010
FILE_DELETE_CHILD: Final = 0x0040
FILE_WRITE_ATTRIBUTES: Final = 0x0100
DELETE: Final = 0x00010000
WRITE_DAC: Final = 0x00040000
WRITE_OWNER: Final = 0x00080000
MAXIMUM_ALLOWED: Final = 0x02000000
GENERIC_ALL: Final = 0x10000000
GENERIC_WRITE: Final = 0x40000000
WRITE_MASK: Final = (
    FILE_WRITE_DATA
    | FILE_APPEND_DATA
    | FILE_WRITE_EA
    | FILE_DELETE_CHILD
    | FILE_WRITE_ATTRIBUTES
    | DELETE
    | WRITE_DAC
    | WRITE_OWNER
    | MAXIMUM_ALLOWED
    | GENERIC_ALL
    | GENERIC_WRITE
)


@dataclass(frozen=True)
class Ace:
    kind: int  # ACE_HEADER.AceType
    flags: int  # ACE_HEADER.AceFlags
    mask: int  # ACCESS_MASK
    sid: str  # string SID ("" for an ACE type Struck does not parse: refused by policy)


@dataclass(frozen=True)
class Security:
    owner: str  # string SID
    dacl: tuple[Ace, ...] | None  # None = NULL DACL (everyone, full access)


@dataclass(frozen=True)
class HandleInfo:
    attributes: int
    links: int


class WinApi(Protocol):
    def token_user_sid(self) -> str: ...
    def open_no_follow(self, path: str, data: bool) -> int: ...
    def handle_info(self, handle: int) -> HandleInfo: ...
    def security_of_handle(self, handle: int) -> Security: ...
    def security_of_path(self, path: str) -> Security: ...
    def set_owner_only(self, path: str, owner_sid: str, directory: bool) -> None: ...
    def create_owner_only(self, path: str, owner_sid: str) -> int:
        """CreateFileW(CREATE_NEW) with an owner-only, protected DACL in the
        SECURITY_ATTRIBUTES: the file never exists with any other ACL. A write handle."""
        ...

    def write_handle(self, handle: int, data: bytes) -> None:
        """WriteFile every byte, then FlushFileBuffers."""
        ...

    def read_handle(self, handle: int) -> bytes: ...
    def close(self, handle: int) -> None: ...


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes
    from typing import cast

    _ADVAPI32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)

    TOKEN_QUERY: Final = 0x0008
    TOKEN_USER_CLASS: Final = 1  # TOKEN_INFORMATION_CLASS.TokenUser
    ERROR_INSUFFICIENT_BUFFER: Final = 122
    ERROR_FILE_NOT_FOUND: Final = 2
    ERROR_PATH_NOT_FOUND: Final = 3
    SE_FILE_OBJECT: Final = 1
    OWNER_SECURITY_INFORMATION: Final = 0x1
    DACL_SECURITY_INFORMATION: Final = 0x4
    PROTECTED_DACL_SECURITY_INFORMATION: Final = 0x80000000
    ACL_REVISION: Final = 2
    ACL_SIZE_INFORMATION_CLASS: Final = 2  # AclSizeInformation
    OBJECT_INHERIT_ACE: Final = 0x1
    CONTAINER_INHERIT_ACE: Final = 0x2
    FILE_ALL_ACCESS: Final = 0x001F01FF
    READ_CONTROL: Final = 0x00020000
    FILE_READ_ATTRIBUTES: Final = 0x0080
    GENERIC_READ: Final = 0x80000000
    CREATE_NEW: Final = 1
    FILE_ATTRIBUTE_NORMAL: Final = 0x80
    SECURITY_DESCRIPTOR_REVISION: Final = 1
    SECURITY_DESCRIPTOR_BUFFER: Final = 64  # >= SECURITY_DESCRIPTOR_MIN_LENGTH (20 x86, 40 x64)
    SE_DACL_PROTECTED: Final = 0x1000
    FILE_SHARE_READ: Final = 0x1
    OPEN_EXISTING: Final = 3
    FILE_FLAG_BACKUP_SEMANTICS: Final = 0x02000000
    FILE_FLAG_OPEN_REPARSE_POINT: Final = 0x00200000
    INVALID_HANDLE: Final = cast("int", ctypes.c_void_p(-1).value)

    # Raw documented layouts (little-endian; no ctypes.Structure, whose types are Any):
    ACE_HEADER_AND_MASK: Final = 8  # ACE_HEADER {BYTE type; BYTE flags; WORD size} + DWORD Mask
    ACCESS_ACE_SIZE: Final = 12  # ACCESS_ALLOWED_ACE: header + Mask + SidStart(DWORD)
    ACL_HEADER_SIZE: Final = 8  # sizeof(ACL)
    ACL_SIZE_INFO_SIZE: Final = 12  # ACL_SIZE_INFORMATION {AceCount, BytesInUse, BytesFree}
    BY_HANDLE_INFO_SIZE: Final = 52  # BY_HANDLE_FILE_INFORMATION
    BY_HANDLE_ATTRS_AT: Final = 0  # dwFileAttributes
    BY_HANDLE_LINKS_AT: Final = 40  # nNumberOfLinks (after 3 FILETIMEs, serial, size hi/lo)

    def _dword(raw: bytes, at: int) -> int:
        return int.from_bytes(raw[at : at + 4], "little")

    def _fail(what: str) -> OSError:
        code = ctypes.get_last_error()
        return OSError(code, f"{what} failed: {ctypes.FormatError(code).strip()}")

    def _ok(result: object, what: str) -> None:
        if not result:
            raise _fail(what)

    def _sid_string(psid: int) -> str:
        out = wintypes.LPWSTR()
        _ok(
            cast("int", _ADVAPI32.ConvertSidToStringSidW(ctypes.c_void_p(psid), ctypes.byref(out))),
            "ConvertSidToStringSidW",
        )
        try:
            value = out.value
            if value is None:
                raise OSError("ConvertSidToStringSidW returned no string")
            return value
        finally:
            _KERNEL32.LocalFree(out)

    def _parse_dacl(pdacl: int) -> tuple[Ace, ...]:
        info = ctypes.create_string_buffer(ACL_SIZE_INFO_SIZE)
        _ok(
            cast(
                "int",
                _ADVAPI32.GetAclInformation(
                    ctypes.c_void_p(pdacl), info, ACL_SIZE_INFO_SIZE, ACL_SIZE_INFORMATION_CLASS
                ),
            ),
            "GetAclInformation",
        )
        aces: list[Ace] = []
        for i in range(_dword(info.raw, 0)):
            pace = ctypes.c_void_p()
            _ok(
                cast("int", _ADVAPI32.GetAce(ctypes.c_void_p(pdacl), i, ctypes.byref(pace))),
                "GetAce",
            )
            address = pace.value
            if address is None:
                raise OSError("GetAce returned NULL")
            head = ctypes.string_at(address, ACE_HEADER_AND_MASK)
            kind, flags = head[0], head[1]
            if kind in (ACCESS_ALLOWED_ACE_TYPE, ACCESS_DENIED_ACE_TYPE):
                sid_at = address + ACE_HEADER_AND_MASK  # SidStart
                aces.append(Ace(kind, flags, _dword(head, 4), _sid_string(sid_at)))
            else:  # object / callback / audit ACE: not parsed -> refused by policy
                aces.append(Ace(kind, flags, 0, ""))
        return tuple(aces)

    def _security(
        code: int, powner: ctypes.c_void_p, pdacl: ctypes.c_void_p, psd: ctypes.c_void_p, what: str
    ) -> Security:
        if code != 0:
            raise OSError(code, f"{what} failed: {ctypes.FormatError(code).strip()}")
        try:
            if powner.value is None:
                raise OSError(f"{what}: no owner SID")
            owner = _sid_string(powner.value)
            dacl = None if pdacl.value is None else _parse_dacl(pdacl.value)
            return Security(owner, dacl)
        finally:
            _KERNEL32.LocalFree(psd)

    class CtypesWinApi:
        """The real Win32 implementation (Windows only)."""

        def token_user_sid(self) -> str:
            token = wintypes.HANDLE()
            process = cast("int", _KERNEL32.GetCurrentProcess())
            _ok(
                cast(
                    "int",
                    _ADVAPI32.OpenProcessToken(
                        wintypes.HANDLE(process), TOKEN_QUERY, ctypes.byref(token)
                    ),
                ),
                "OpenProcessToken",
            )
            try:
                need = wintypes.DWORD()
                _ADVAPI32.GetTokenInformation(token, TOKEN_USER_CLASS, None, 0, ctypes.byref(need))
                if ctypes.get_last_error() != ERROR_INSUFFICIENT_BUFFER:
                    raise _fail("GetTokenInformation(size)")
                buf = ctypes.create_string_buffer(int(need.value))
                _ok(
                    cast(
                        "int",
                        _ADVAPI32.GetTokenInformation(
                            token, TOKEN_USER_CLASS, buf, need, ctypes.byref(need)
                        ),
                    ),
                    "GetTokenInformation(TokenUser)",
                )
                # TOKEN_USER.User.Sid: the first pointer-sized field of the buffer
                psid = int.from_bytes(buf.raw[: ctypes.sizeof(ctypes.c_void_p)], sys.byteorder)
                if psid == 0:
                    raise OSError("TokenUser has no SID")
                return _sid_string(psid)
            finally:
                _KERNEL32.CloseHandle(token)

        def open_no_follow(self, path: str, data: bool) -> int:
            access = READ_CONTROL | FILE_READ_ATTRIBUTES | (GENERIC_READ if data else 0)
            create = _KERNEL32.CreateFileW
            create.restype = wintypes.HANDLE
            handle = cast(
                "int | None",
                create(
                    path,
                    access,
                    FILE_SHARE_READ,
                    None,
                    OPEN_EXISTING,
                    FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
                    None,
                ),
            )
            if handle is None or handle == INVALID_HANDLE:
                code = ctypes.get_last_error()
                if code in (ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND):
                    raise FileNotFoundError(code, ctypes.FormatError(code).strip(), path)
                raise OSError(
                    code, f"CreateFileW({path!r}) failed: {ctypes.FormatError(code).strip()}"
                )
            return handle

        def handle_info(self, handle: int) -> HandleInfo:
            info = ctypes.create_string_buffer(BY_HANDLE_INFO_SIZE)
            _ok(
                cast(
                    "int",
                    _KERNEL32.GetFileInformationByHandle(wintypes.HANDLE(handle), info),
                ),
                "GetFileInformationByHandle",
            )
            return HandleInfo(
                _dword(info.raw, BY_HANDLE_ATTRS_AT), _dword(info.raw, BY_HANDLE_LINKS_AT)
            )

        def security_of_handle(self, handle: int) -> Security:
            powner, pdacl, psd = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
            code = cast(
                "int",
                _ADVAPI32.GetSecurityInfo(
                    wintypes.HANDLE(handle),
                    SE_FILE_OBJECT,
                    OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
                    ctypes.byref(powner),
                    None,
                    ctypes.byref(pdacl),
                    None,
                    ctypes.byref(psd),
                ),
            )
            return _security(code, powner, pdacl, psd, "GetSecurityInfo")

        def security_of_path(self, path: str) -> Security:
            powner, pdacl, psd = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
            code = cast(
                "int",
                _ADVAPI32.GetNamedSecurityInfoW(
                    path,
                    SE_FILE_OBJECT,
                    OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
                    ctypes.byref(powner),
                    None,
                    ctypes.byref(pdacl),
                    None,
                    ctypes.byref(psd),
                ),
            )
            return _security(code, powner, pdacl, psd, "GetNamedSecurityInfoW")

        def create_owner_only(self, path: str, owner_sid: str) -> int:
            psid = _sid_from_string(owner_sid)
            try:
                acl = _owner_acl(psid, directory=False)
                sd = ctypes.create_string_buffer(SECURITY_DESCRIPTOR_BUFFER)
                _ok(
                    cast(
                        "int",
                        _ADVAPI32.InitializeSecurityDescriptor(sd, SECURITY_DESCRIPTOR_REVISION),
                    ),
                    "InitializeSecurityDescriptor",
                )
                _ok(
                    cast("int", _ADVAPI32.SetSecurityDescriptorOwner(sd, psid, False)),
                    "SetSecurityDescriptorOwner",
                )
                _ok(
                    cast("int", _ADVAPI32.SetSecurityDescriptorDacl(sd, True, acl, False)),
                    "SetSecurityDescriptorDacl",
                )
                _ok(
                    cast(
                        "int",
                        _ADVAPI32.SetSecurityDescriptorControl(
                            sd, SE_DACL_PROTECTED, SE_DACL_PROTECTED
                        ),
                    ),
                    "SetSecurityDescriptorControl",
                )
                # SECURITY_ATTRIBUTES {DWORD nLength; LPVOID lpSecurityDescriptor;
                # BOOL bInheritHandle}: 3 pointer-sized slots with natural alignment.
                ptr = ctypes.sizeof(ctypes.c_void_p)
                raw = bytearray(3 * ptr)  # bInheritHandle stays 0 (FALSE)
                raw[0:4] = (3 * ptr).to_bytes(4, sys.byteorder)
                raw[ptr : 2 * ptr] = ctypes.addressof(sd).to_bytes(ptr, sys.byteorder)
                sa = ctypes.create_string_buffer(bytes(raw), 3 * ptr)
                create = _KERNEL32.CreateFileW
                create.restype = wintypes.HANDLE
                handle = cast(
                    "int | None",
                    create(
                        path,
                        GENERIC_WRITE | READ_CONTROL,
                        0,  # no sharing while Struck writes it
                        sa,
                        CREATE_NEW,
                        FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT,
                        None,
                    ),
                )
                if handle is None or handle == INVALID_HANDLE:
                    code = ctypes.get_last_error()
                    raise OSError(
                        code,
                        f"CreateFileW(CREATE_NEW {path!r}) failed: {ctypes.FormatError(code).strip()}",
                    )
                return handle
            finally:
                _KERNEL32.LocalFree(psid)

        def write_handle(self, handle: int, data: bytes) -> None:
            done = wintypes.DWORD()
            view = memoryview(data)
            while view:
                chunk = bytes(view[: 1 << 20])
                _ok(
                    cast(
                        "int",
                        _KERNEL32.WriteFile(
                            wintypes.HANDLE(handle), chunk, len(chunk), ctypes.byref(done), None
                        ),
                    ),
                    "WriteFile",
                )
                if done.value == 0:
                    raise OSError("WriteFile wrote 0 bytes")
                view = view[done.value :]
            _ok(
                cast("int", _KERNEL32.FlushFileBuffers(wintypes.HANDLE(handle))),
                "FlushFileBuffers",
            )

        def set_owner_only(self, path: str, owner_sid: str, directory: bool) -> None:
            psid = _sid_from_string(owner_sid)
            try:
                acl = _owner_acl(psid, directory)
                code = cast(
                    "int",
                    _ADVAPI32.SetNamedSecurityInfoW(
                        path,
                        SE_FILE_OBJECT,
                        OWNER_SECURITY_INFORMATION
                        | DACL_SECURITY_INFORMATION
                        | PROTECTED_DACL_SECURITY_INFORMATION,
                        psid,
                        None,
                        acl,
                        None,
                    ),
                )
                if code != 0:
                    raise OSError(
                        code, f"SetNamedSecurityInfoW failed: {ctypes.FormatError(code).strip()}"
                    )
            finally:
                _KERNEL32.LocalFree(psid)

        def read_handle(self, handle: int) -> bytes:
            chunks: list[bytes] = []
            buf = ctypes.create_string_buffer(65536)
            got = wintypes.DWORD()
            while True:
                _ok(
                    cast(
                        "int",
                        _KERNEL32.ReadFile(
                            wintypes.HANDLE(handle), buf, len(buf), ctypes.byref(got), None
                        ),
                    ),
                    "ReadFile",
                )
                if got.value == 0:
                    return b"".join(chunks)
                chunks.append(buf.raw[: got.value])

        def close(self, handle: int) -> None:
            _KERNEL32.CloseHandle(wintypes.HANDLE(handle))

    def _sid_from_string(owner_sid: str) -> ctypes.c_void_p:
        psid = ctypes.c_void_p()
        _ok(
            cast("int", _ADVAPI32.ConvertStringSidToSidW(owner_sid, ctypes.byref(psid))),
            "ConvertStringSidToSidW",
        )
        return psid

    def _owner_acl(psid: ctypes.c_void_p, directory: bool) -> ctypes.Array[ctypes.c_char]:
        """ONE ALLOW ACE: the owner, full access (inheritable on a directory)."""
        sid_len = cast("int", _ADVAPI32.GetLengthSid(psid))
        # sizeof(ACL) + sizeof(ACCESS_ALLOWED_ACE) - sizeof(SidStart) + SID length
        size = ACL_HEADER_SIZE + ACCESS_ACE_SIZE - 4 + sid_len
        acl = ctypes.create_string_buffer(size)
        _ok(cast("int", _ADVAPI32.InitializeAcl(acl, size, ACL_REVISION)), "InitializeAcl")
        inherit = (OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE) if directory else 0
        _ok(
            cast(
                "int",
                _ADVAPI32.AddAccessAllowedAceEx(acl, ACL_REVISION, inherit, FILE_ALL_ACCESS, psid),
            ),
            "AddAccessAllowedAceEx",
        )
        return acl

    def win_api() -> WinApi:
        return CtypesWinApi()

else:

    def win_api() -> WinApi:
        raise OSError("the Win32 security API exists only on Windows")
