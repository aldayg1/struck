"""``python -m struck.keys list | set <name> | delete <name> | import-strike-env``:
manage the entries of Struck's encrypted secret store (``struck.secrets``).

Every command runs only at a REAL terminal (``struck.tty.require_terminal``:
isatty, and GetConsoleMode on Windows) and first asks for the master password
(``getpass``, no echo). Values are read with ``getpass`` and never printed,
logged or written anywhere but the encrypted store.

* ``list``: every known entry name with present / missing only.
* ``set <name>`` / ``delete <name>``: one entry; only names in ``ENTRIES``.
* ``import-strike-env [path]``: one-time migration of the Strike API wallet from
  the old ``strike.env`` (default ``<user config dir>/strike.env``): the two
  ``STRUCK_STRIKE_API_WALLET_*`` values are parsed (stock ``shlex``), the derived
  public key must start with ``EXPECTED_STRIKE_PUBKEY_PREFIX`` (else nothing is
  stored), both are stored and read back, and only then a y/N question offers
  to shred the file (overwrite, fsync, delete). Default is N: the file stays.
"""

from __future__ import annotations

import argparse
import getpass
import os
import shlex
import sys
from pathlib import Path

from .paths import user_config_dir
from .plan.providers import PROVIDERS
from .secrets import (
    LLM_CUSTOM,
    STRIKE_PRIVATE,
    STRIKE_PUBLIC,
    SecretError,
    delete_secret,
    has_secret,
    llm_entry,
    set_secret,
    unlock_from_terminal,
)
from .config import pop_network_flag, load_config
from .strike.auth import StrikeCredentialsError, wallet_from_hex
from .strike.venue_auth import ENVIRONMENTS, Environment, entry_for, environment_of, save_account
from .tty import read_human_line, require_terminal

ENTRIES: tuple[str, ...] = (
    *(llm_entry(p) for p, info in PROVIDERS.items() if info.keyed),
    LLM_CUSTOM,
)
# The registered Strike API wallet (public key prefix; not a secret).
EXPECTED_STRIKE_PUBKEY_PREFIX = "e926f4ad"
STRIKE_ENV_PATH = user_config_dir() / "strike.env"
_ENV_NAMES = {
    "STRUCK_STRIKE_API_WALLET_PUBLIC_KEY": STRIKE_PUBLIC,
    "STRUCK_STRIKE_API_WALLET_PRIVATE_KEY": STRIKE_PRIVATE,
}


def _known(name: str) -> str:
    if name not in ENTRIES:
        raise SecretError(f"unknown secret entry {name!r}; known: {', '.join(ENTRIES)}")
    return name


def entry_states() -> list[tuple[str, str]]:
    """(name, 'present' | 'missing') for every entry; raises SecretError while locked."""
    names = (*ENTRIES, *(entry_for(e) for e in ENVIRONMENTS))
    return [(n, "present" if has_secret(n) else "missing") for n in names]


def parse_strike_env(text: str) -> dict[str, str]:
    """``[export ]NAME=value`` lines (shell quoting, via stock shlex) -> entry values."""
    found: dict[str, str] = {}
    for line in text.splitlines():
        words = shlex.split(line, comments=True)
        if words[:1] == ["export"]:
            words = words[1:]
        for word in words:
            name, sep, value = word.partition("=")
            if sep and name in _ENV_NAMES:
                found[_ENV_NAMES[name]] = value
    return found


def _shred(path: Path) -> None:
    size = path.stat().st_size
    with path.open("r+b") as fh:
        fh.write(os.urandom(size))
        fh.flush()
        os.fsync(fh.fileno())
    path.unlink()


def import_strike_env(
    path: Path, platform: str, expect_prefix: str, environment: Environment
) -> int:
    values = parse_strike_env(path.read_text(encoding="utf-8"))
    missing = [n for n in (STRIKE_PUBLIC, STRIKE_PRIVATE) if not values.get(n)]
    if missing:
        print(f"REFUSED: {path} lacks {', '.join(missing)}; nothing stored", file=sys.stderr)
        return 4
    try:
        wallet = wallet_from_hex(values[STRIKE_PUBLIC], values[STRIKE_PRIVATE])
    except StrikeCredentialsError as exc:
        print(f"REFUSED: {exc}; nothing stored", file=sys.stderr)
        return 4
    if not wallet.public_key_hex.startswith(expect_prefix):
        print(
            f"REFUSED: the derived public key starts {wallet.public_key_hex[:8]}, expected "
            f"{expect_prefix}; nothing stored",
            file=sys.stderr,
        )
        return 4
    print(
        f"derived public key {wallet.public_key_hex[:8]}… matches; storing it for Strike {environment}"
    )
    try:
        save_account(environment, "strike-env", values[STRIKE_PUBLIC], values[STRIKE_PRIVATE])
    except StrikeCredentialsError as exc:
        print(f"REFUSED: {exc}; {path} kept", file=sys.stderr)
        return 4
    print(f"stored {entry_for(environment)} (values not shown)")
    print(f"Shred {path} now (overwrite + delete)? [y/N] ", end="", flush=True)
    if read_human_line(platform).strip().lower() == "y":
        _shred(path)
        print(f"shredded {path}")
    else:
        print(f"kept {path}")
    return 0


class _Args(argparse.Namespace):
    cmd: str = ""
    name: str = ""
    path: str = ""


def main(argv: list[str] | None = None, platform: str = sys.platform) -> int:
    parser = argparse.ArgumentParser(prog="python -m struck.keys", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="entry names with present/missing only")
    s = sub.add_parser("set", help="store one entry (terminal only, no echo)")
    s.add_argument("name")
    d = sub.add_parser("delete", help="delete one entry")
    d.add_argument("name")
    i = sub.add_parser("import-strike-env", help="one-time strike.env migration")
    i.add_argument("path", nargs="?", default=str(STRIKE_ENV_PATH))
    raw = sys.argv[1:] if argv is None else argv
    explicit_network = any(a == "--network" or a.startswith("--network=") for a in raw)
    args = parser.parse_args(pop_network_flag(raw), namespace=_Args())
    try:
        if args.cmd in ("set", "delete"):
            _known(args.name)
        try:
            require_terminal(platform)
        except EOFError as exc:
            print(f"REFUSED: {exc}; struck.keys runs only at a real terminal", file=sys.stderr)
            return 2
        unlock_from_terminal(platform)
        if args.cmd == "list":
            for name, state in entry_states():
                print(f"{name:<34} {state}")
            return 0
        if args.cmd == "delete":
            delete_secret(args.name)
            print(f"deleted secret {args.name}")
            return 0
        if args.cmd == "import-strike-env":
            if not explicit_network:
                print(
                    "REFUSED: import-strike-env needs --network <name> (the key's Strike environment)",
                    file=sys.stderr,
                )
                return 2
            return import_strike_env(
                Path(args.path),
                platform,
                EXPECTED_STRIKE_PUBKEY_PREFIX,
                environment_of(load_config().profile),
            )
        value = getpass.getpass(f"{args.name} (input hidden): ")
        set_secret(args.name, value)
        print(f"stored secret {args.name}; value not shown")
        return 0
    except SecretError as exc:
        print(f"SECRET STORE ERROR: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
