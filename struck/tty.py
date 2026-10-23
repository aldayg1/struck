"""Human-only answers at a REAL terminal, on Linux, macOS and Windows.

A piped or scripted stdin is refused. ``sys.stdin.isatty()`` is necessary but
not trusted alone: on Windows a pipe/NUL can pass a character-device test, so
the console handle must also answer ``GetConsoleMode`` (kernel32, via ctypes),
which succeeds only for a real console. The answer is then read from the
controlling terminal device itself (POSIX ``/dev/tty``; Windows ``CONIN$``),
never from stdin.
"""

from __future__ import annotations

import sys
from typing import TextIO, cast

POSIX_TTY = "/dev/tty"  # the controlling terminal (POSIX standard device name)
WINDOWS_CONSOLE_IN = "CONIN$"  # the console input buffer (Win32 standard name)


def _windows_console_mode_ok(stream: TextIO) -> bool:
    """True only if ``stream``'s handle is a real console (GetConsoleMode succeeds)."""
    if sys.platform == "win32":
        import ctypes
        import msvcrt
        from ctypes import wintypes

        handle = msvcrt.get_osfhandle(stream.fileno())
        mode = wintypes.DWORD()
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        return (
            cast("int", kernel32.GetConsoleMode(wintypes.HANDLE(handle), ctypes.byref(mode))) != 0
        )
    raise OSError("GetConsoleMode exists only on Windows")


def _terminal_device(platform: str) -> str:
    return WINDOWS_CONSOLE_IN if platform == "win32" else POSIX_TTY


def require_terminal(platform: str = sys.platform) -> None:
    """Raises EOFError (refusal) unless stdin is a real interactive terminal."""
    stdin = cast("TextIO", sys.stdin)
    if not stdin.isatty():
        raise EOFError("stdin is not a terminal: piped or scripted approvals are refused")
    if platform == "win32" and not _windows_console_mode_ok(stdin):
        raise EOFError(
            "stdin is not a real console (GetConsoleMode failed): piped or scripted "
            "approvals are refused"
        )


def read_human_line(platform: str = sys.platform) -> str:
    """One line typed at the terminal, after ``require_terminal``."""
    require_terminal(platform)
    with open(_terminal_device(platform), encoding="utf-8") as tty:
        return tty.readline().strip()
