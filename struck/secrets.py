"""Struck's ONLY secret source: ONE encrypted file, the same on Linux, macOS and
Windows (pure Python; no desktop or OS keyring is ever consulted). Source,
verbatim (Rag-Time, 2026-10-21 16:54 CDMX, Grok Bot chat widget answer): "Sí,
reanuda: archivo cifrado propio de Struck (keyring + cryptfile, servicio
struck), igual en los 3 OS, sin tocar el vault del 

* backend: the stock ``keyrings.cryptfile`` ``CryptFileKeyring`` (Argon2id KDF +
  AES-GCM, both from its pinned deps), set EXPLICITLY with
  ``keyring.set_keyring(...)``; keyringrc / auto-detection / any OS keyring is
  never consulted. Service name ``"struck"``.
* file: ``<struck.paths.user_config_dir()>/secrets.cfg``, checked with
  ``struck.private_file`` (POSIX: regular file, yours, 0600, folder not
  group/other-writable, no symlink; Windows: owner-only DACL by SID, no reparse
  point) before every use. Every WRITE of the file is atomic: the stock
  backend's three file writes are routed through ``private_file.write_private``
  (temp file in the same folder created 0600 / owner-only DACL, fsync,
  ``os.replace``, folder fsync on POSIX). A corrupt file raises ``SecretError``.
* ``lock()`` drops the master password from the backend (stock ``_lock``) and
  every reference Struck holds. CPython ``str`` objects cannot be zeroed in
  place; dropping the references is what is possible without hand-rolled code.
* master password: given to ``unlock()`` only, by a real terminal
  (``unlock_from_terminal``: struck.tty + getpass) or a masked UI field (the
  first-run registration dialogue ``struck.ui.registration`` or the Settings
  tab). Never read from env, arguments or files; never logged. A wrong one
  fails loudly ("did not decrypt"); there is no fallback.
* no environment variable is ever read for a secret. A missing entry raises
  ``SecretError`` naming the entry and the exact command to set it.

Entries (the full list is ``struck.keys.ENTRIES``):

    strike_api_wallet_public_key   Strike API wallet Ed25519 public key (64 hex)
    strike_api_wallet_private_key  Strike API wallet Ed25519 seed (64 hex)
    llm_<provider>                 that provider's key, official host only
                                   (llm_openai, llm_anthropic, llm_openrouter, llm_vllm, ...)
    llm_custom                     the only key a user-opted-in custom LLM host gets
"""

from __future__ import annotations

import configparser
import getpass
import io
import os
import sys
from pathlib import Path
from typing import Final

import keyring
from keyring.core import load_keyring
from keyring.errors import KeyringError, PasswordDeleteError
from keyrings.cryptfile.cryptfile import CryptFileKeyring
from keyrings.cryptfile.escape import escape as escape_for_ini

from .paths import user_config_dir
from .private_file import private_dir, read_private, write_private
from .tty import require_terminal

SERVICE: Final = "struck"
STRIKE_PUBLIC: Final = "strike_api_wallet_public_key"
STRIKE_PRIVATE: Final = "strike_api_wallet_private_key"
LLM_CUSTOM: Final = "llm_custom"  # the ONLY key a user-opted-in custom LLM host receives
SECRETS_PATH: Path = user_config_dir() / "secrets.cfg"


class SecretError(RuntimeError):
    """A secret is missing, the store is locked, or the master password is wrong."""


class _AtomicCryptFile(CryptFileKeyring):
    """The stock ``CryptFileKeyring``, unchanged except WHERE its file is written:
    its three ``open(file_path, 'w')`` writes (file_base.py ``_write_config_value``,
    ``_ensure_file_path``, ``delete_password``) go through ``write_private``
    (atomic, private from creation). Encryption, KDF and format are stock."""

    def _read_config(self) -> configparser.RawConfigParser:
        config = configparser.RawConfigParser()
        if os.path.exists(self.file_path):
            config.read(self.file_path, encoding="utf-8")
        return config

    def _save(self, config: configparser.RawConfigParser) -> None:
        text = io.StringIO()
        config.write(text)
        write_private(Path(self.file_path), text.getvalue().encode("utf-8"))

    def _ensure_file_path(self) -> None:
        if not os.path.isfile(self.file_path):
            write_private(Path(self.file_path), b"")

    def _write_config_value(self, service: str, key: str, value: str) -> None:
        self._ensure_file_path()
        config = self._read_config()
        section, option = escape_for_ini(service), escape_for_ini(key)
        if not config.has_section(section):
            config.add_section(section)
        config.set(section, option, value)
        self._save(config)

    def delete_password(self, service: str, username: str) -> None:
        config = self._read_config()
        section, option = escape_for_ini(service), escape_for_ini(username)
        try:
            if not config.remove_option(section, option):
                raise PasswordDeleteError("Password not found")
        except configparser.NoSectionError:
            raise PasswordDeleteError("Password not found") from None
        self._save(config)


class _State:
    backend: CryptFileKeyring | None = None


_CORRUPT = (configparser.Error, UnicodeDecodeError, KeyError)


def llm_entry(provider: str) -> str:
    return f"llm_{provider}"


def set_command(name: str) -> str:
    return f"uv run python -m struck.keys set {name}"


def _checked_path() -> Path:
    """The store file, private-file checked (a missing file is fine: unlock creates it;
    a missing per-user config dir is fine too: the first write creates it 0700 and
    checks it — absence is not a violation, an existing bad dir/file is)."""
    path = SECRETS_PATH
    if not path.parent.exists():
        return path
    try:
        private_dir(path.parent)
        read_private(path)  # raises on a symlink / wrong owner / mode / ACL
    except (ValueError, OSError) as exc:
        raise SecretError(f"secret store {path} refused: {exc}") from exc
    return path


def store_exists() -> bool:
    return _checked_path().is_file()


def is_unlocked() -> bool:
    return _State.backend is not None and keyring.get_keyring() is _State.backend


def unlock(master: str) -> None:
    """Open (or, if absent, create) the encrypted store with ``master``."""
    if not master.strip():
        raise SecretError("refusing an empty master password")
    path = _checked_path()
    backend = _AtomicCryptFile()
    backend.file_path = str(path)
    try:
        # A non-empty file without the stock password reference is NOT a new
        # store: the stock backend would silently re-initialize (overwrite) it.
        if path.is_file() and path.stat().st_size > 0 and not backend._check_file():
            raise SecretError(
                f"secret store {path} is corrupt: no password reference; it was NOT "
                "re-initialized (restore it from a backup, or move it away to start over)"
            )
        backend.keyring_key = master  # stock: verifies the file's password reference
    except _CORRUPT as exc:
        raise SecretError(f"secret store {path} is corrupt: {type(exc).__name__}: {exc}") from exc
    except ValueError as exc:  # stock: "Incorrect Password" / AES-GCM "MAC check failed"
        raise SecretError(
            f"wrong master password: the secret store {path} did not decrypt ({exc})"
        ) from exc
    keyring.set_keyring(backend)  # explicit: no keyringrc, no auto-detect, no OS keyring
    _State.backend = backend
    _checked_path()  # a newly created file is checked too


def lock() -> None:
    """Drop the master password: the backend's stock ``_lock`` (its only copy),
    then Struck's reference, then keyring's global (set to the stock fail backend)."""
    backend, _State.backend = _State.backend, None
    if backend is not None:
        backend._lock()
        if keyring.get_keyring() is backend:
            keyring.set_keyring(load_keyring("keyring.backends.fail.Keyring"))


def use_store(path: Path) -> None:
    """Use another store file for this process (``--secrets-file <path>``): a
    path only, never a secret. Locks first; the file gets the same checks."""
    global SECRETS_PATH
    lock()
    SECRETS_PATH = path.expanduser().absolute()


def unlock_from_terminal(platform: str = sys.platform) -> None:
    """Prompt for the master password at a REAL terminal (struck.tty; getpass, no echo).
    A new store asks twice. Piped/scripted stdin is refused."""
    if is_unlocked():
        return
    try:
        require_terminal(platform)
    except EOFError as exc:
        raise SecretError(f"{exc}; the master password is read only from a real terminal") from exc
    new = not store_exists()
    master = getpass.getpass(
        "New Struck secret store, choose a master password: " if new else "Struck master password: "
    )
    if new and getpass.getpass("Repeat the master password: ") != master:
        raise SecretError("the two master passwords differ; nothing was created")
    unlock(master)


def _backend() -> CryptFileKeyring:
    backend = _State.backend
    if backend is None or keyring.get_keyring() is not backend:
        raise SecretError(
            f"secret store {SECRETS_PATH} is locked: unlock it in the Settings tab or run "
            "`uv run python -m struck.keys list` at a terminal"
        )
    _checked_path()
    return backend


def get_secret(name: str) -> str:
    backend = _backend()
    try:
        value = backend.get_password(SERVICE, name)
    except (KeyringError, ValueError, *_CORRUPT) as exc:
        raise SecretError(f"secret '{name}' could not be read: {exc}") from exc
    if not value:
        raise SecretError(
            f"secret '{name}' (service '{SERVICE}') is not set: run `{set_command(name)}`"
        )
    return value


def has_secret(name: str) -> bool:
    """Presence only (status displays); raises SecretError while locked."""
    try:
        return bool(_backend().get_password(SERVICE, name))
    except (KeyringError, ValueError, *_CORRUPT) as exc:
        raise SecretError(f"secret '{name}' could not be read: {exc}") from exc


def set_secret(name: str, value: str) -> None:
    if not value:
        raise SecretError(f"refusing to store an empty value for '{name}'")
    backend = _backend()
    try:
        backend.set_password(SERVICE, name, value)
    except (KeyringError, ValueError, *_CORRUPT, OSError) as exc:
        raise SecretError(f"secret '{name}' could not be written: {exc}") from exc
    _checked_path()


def delete_secret(name: str) -> None:
    backend = _backend()
    try:
        backend.delete_password(SERVICE, name)
    except PasswordDeleteError as exc:
        raise SecretError(f"secret '{name}' is not set (nothing to delete)") from exc
    except (KeyringError, ValueError, *_CORRUPT, OSError) as exc:
        raise SecretError(f"secret '{name}' could not be deleted: {exc}") from exc
