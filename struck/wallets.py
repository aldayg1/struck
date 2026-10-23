"""Struck's Cardano wallets: a BIP-39 mnemonic per wallet, kept ONLY in Struck's
encrypted secret store (``struck.secrets``, service ``"struck"``).

* Derivation (CIP-1852 over CIP-3 Icarus, stock ``pycardano.HDWallet``):
  payment ``m/1852'/1815'/0'/0/0``; stake ``m/1852'/1815'/0'/2/0`` for a
  ``standard`` (base) address; ``enterprise`` has no stake part. The address
  network comes from the selected ``struck.network`` row (no default).
* Storage: entry ``wallet_<name>`` = JSON {name, network, wallet_type,
  derivation_path, address, mnemonic}; entry ``wallets_index`` = the JSON list
  of names. Nothing about a wallet is written anywhere else.
* The mnemonic and keys are never printed, logged or written outside the
  store; ``WalletRecord.mnemonic`` is a ``SecretStr`` (masked in every repr).
* A loaded ``WalletSigner`` re-derives the address and refuses if it differs
  from the stored one.
"""

from __future__ import annotations

import getpass
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, TextIO, cast

from pycardano import (
    Address,
    ExtendedSigningKey,
    HDWallet,
    PaymentVerificationKey,
    StakeVerificationKey,
)
from pydantic import BaseModel, ConfigDict, SecretStr, StrictStr, ValidationError

from .network import NetworkProfile, profile
from .secrets import (
    SecretError,
    delete_secret,
    get_secret,
    has_secret,
    set_secret,
    unlock_from_terminal,
    use_store,
)
from .tty import read_human_line

WalletType = Literal["standard", "enterprise"]
# CIP-1852: purpose 1852', coin type 1815' (ADA), account 0'; role 0 = external
# payment, role 2 = staking; index 0.
PAYMENT_PATH: Final = "m/1852'/1815'/0'/0/0"
STAKE_PATH: Final = "m/1852'/1815'/0'/2/0"
INDEX_ENTRY: Final = "wallets_index"
NAME_RE: Final = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")


class WalletError(RuntimeError):
    """A wallet is missing, malformed, on another network, or does not re-derive."""


class WalletRecord(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    name: StrictStr
    network: StrictStr
    wallet_type: WalletType
    derivation_path: StrictStr
    address: StrictStr
    mnemonic: SecretStr


@dataclass(frozen=True)
class WalletInfo:
    """What ``list`` shows: never the mnemonic or a key."""

    name: str
    network: str
    address: str


def wallet_entry(name: str) -> str:
    return f"wallet_{checked_name(name)}"


def checked_name(name: str) -> str:
    if not NAME_RE.fullmatch(name):
        raise WalletError(
            f"wallet name {name!r} must match {NAME_RE.pattern} (lowercase letters, digits, - _)"
        )
    return name


def _normalized(mnemonic: str) -> str:
    return " ".join(mnemonic.split())


def derive(
    mnemonic: str, net: NetworkProfile, wallet_type: WalletType
) -> tuple[ExtendedSigningKey, str]:
    """(payment signing key, bech32 address) for ``mnemonic`` on ``net``."""
    words = _normalized(mnemonic)
    if not HDWallet.is_mnemonic(words):
        raise WalletError("the mnemonic is not a valid BIP-39 English phrase (checksum/words)")
    root = HDWallet.from_mnemonic(words)
    payment = ExtendedSigningKey.from_hdwallet(root.derive_from_path(PAYMENT_PATH))
    pay_vkey = PaymentVerificationKey.from_primitive(
        payment.to_verification_key().to_non_extended().payload
    )
    stake_hash = None
    if wallet_type == "standard":
        stake = ExtendedSigningKey.from_hdwallet(root.derive_from_path(STAKE_PATH))
        stake_vkey = StakeVerificationKey.from_primitive(
            stake.to_verification_key().to_non_extended().payload
        )
        stake_hash = stake_vkey.hash()
    address = Address(pay_vkey.hash(), stake_hash, network=net.pycardano_network)
    return payment, str(address)


def _index() -> list[str]:
    if not has_secret(INDEX_ENTRY):
        return []
    raw: object = json.loads(get_secret(INDEX_ENTRY))
    if not isinstance(raw, list) or not all(isinstance(n, str) for n in raw):
        raise WalletError(f"secret '{INDEX_ENTRY}' is not a JSON list of names")
    return [str(n) for n in raw]


def _save_index(names: list[str]) -> None:
    if names:
        ordered: list[str] = sorted(set(names))
        set_secret(INDEX_ENTRY, json.dumps(ordered))
    elif has_secret(INDEX_ENTRY):
        delete_secret(INDEX_ENTRY)


def import_mnemonic(
    name: str,
    mnemonic: str,
    network: str,
    wallet_type: WalletType = "standard",
    *,
    expect_address: str | None = None,
) -> WalletInfo:
    """Store ``mnemonic`` as wallet ``name`` (refuses to overwrite)."""
    entry = wallet_entry(name)
    net = profile(network)
    if has_secret(entry):
        raise WalletError(f"wallet '{name}' already exists; delete it first")
    _key, address = derive(mnemonic, net, wallet_type)
    if expect_address is not None and address != expect_address:
        raise WalletError(
            f"derived address {address} differs from the expected {expect_address}; "
            "nothing was stored"
        )
    record = WalletRecord(
        name=name,
        network=net.name,
        wallet_type=wallet_type,
        derivation_path=PAYMENT_PATH,
        address=address,
        mnemonic=SecretStr(_normalized(mnemonic)),
    )
    stored: dict[str, str] = {
        "name": record.name,
        "network": record.network,
        "wallet_type": record.wallet_type,
        "derivation_path": record.derivation_path,
        "address": record.address,
        "mnemonic": record.mnemonic.get_secret_value(),
    }
    set_secret(entry, json.dumps(stored))
    _save_index([*_index(), name])
    return WalletInfo(name=name, network=net.name, address=address)


def load_record(name: str) -> WalletRecord:
    try:
        return WalletRecord.model_validate_json(get_secret(wallet_entry(name)))
    except SecretError as exc:
        raise WalletError(
            f"wallet '{name}' is not in the secret store: run "
            f"`uv run python -m struck.wallets import {name} --network <name>`"
        ) from exc
    except ValidationError as exc:
        raise WalletError(
            f"wallet '{name}' entry is malformed ({exc.error_count()} error(s))"
        ) from None


def list_wallets() -> tuple[WalletInfo, ...]:
    out: list[WalletInfo] = []
    for name in _index():
        rec = load_record(name)
        out.append(WalletInfo(name=rec.name, network=rec.network, address=rec.address))
    return tuple(out)


def delete_wallet(name: str) -> None:
    entry = wallet_entry(name)
    if not has_secret(entry):
        raise WalletError(f"wallet '{name}' does not exist")
    delete_secret(entry)
    _save_index([n for n in _index() if n != name])


class WalletSigner:
    """The payment key of one stored wallet, in memory only. ``sign`` returns
    (32-byte vkey, 64-byte ed25519 signature) over the given message."""

    def __init__(self, name: str, network: str, address: str, key: ExtendedSigningKey) -> None:
        self.name = name
        self.network = network
        self.address = address
        self.__key = key

    def __repr__(self) -> str:
        return (
            f"WalletSigner(name={self.name!r}, network={self.network!r}, address={self.address!r})"
        )

    def sign(self, message: bytes) -> tuple[bytes, bytes]:
        vkey = self.__key.to_verification_key().to_non_extended().payload
        return vkey, self.__key.sign(message)


def load_signer(name: str, network: str) -> WalletSigner:
    """The stored wallet as a signer for ``network``; refuses another network."""
    rec = load_record(name)
    net = profile(network)
    if rec.network != net.name:
        raise WalletError(
            f"NETWORK MISMATCH: wallet '{name}' is a {rec.network} wallet; the selected "
            f"network is {net.name}. Nothing is signed."
        )
    key, address = derive(rec.mnemonic.get_secret_value(), net, rec.wallet_type)
    if address != rec.address:
        raise WalletError(
            f"wallet '{name}' re-derives to {address}, not the stored {rec.address}; refused"
        )
    return WalletSigner(rec.name, rec.network, rec.address, key)


USAGE: Final = """usage:
  uv run python -m struck.wallets [--secrets-file <path>] import <name> --network <name> [--enterprise]
      the BIP-39 mnemonic is read from a hidden prompt (terminal) or ONE line of stdin (pipe);
      never from arguments
  uv run python -m struck.wallets [--secrets-file <path>] list
      name, network and address only
  uv run python -m struck.wallets [--secrets-file <path>] delete <name>
      at a terminal; asks y/N
The Struck master password is always read at a real terminal (struck.secrets)."""


def read_mnemonic(platform: str = sys.platform) -> str:
    """Hidden prompt at a terminal, else exactly one line from the stdin pipe."""
    stdin = cast("TextIO", sys.stdin)
    if stdin.isatty():
        return str(getpass.getpass("BIP-39 mnemonic (hidden): "))
    line = stdin.readline()
    if not line.strip():
        raise WalletError("no mnemonic on stdin")
    return line


def pop_secrets_file(argv: list[str]) -> list[str]:
    """``--secrets-file <path>`` selects the store file for this process."""
    if "--secrets-file" not in argv:
        return argv
    i = argv.index("--secrets-file")
    if i + 1 >= len(argv):
        raise WalletError("--secrets-file needs a path")
    use_store(Path(argv[i + 1]))
    return argv[:i] + argv[i + 2 :]


def main(argv: list[str], platform: str = sys.platform) -> int:
    try:
        argv = pop_secrets_file(argv)
        if not argv or argv[0] in ("-h", "--help"):
            print(USAGE, file=sys.stderr)
            return 0 if argv else 2
        cmd, rest = argv[0], argv[1:]
        if cmd == "import":
            if len(rest) < 3 or rest[1] != "--network" or rest[3:] not in ([], ["--enterprise"]):
                print(USAGE, file=sys.stderr)
                return 2
            name, network = checked_name(rest[0]), rest[2]
            wtype: WalletType = "enterprise" if rest[3:] else "standard"
            profile(network)  # unknown network: loud, before any prompt
            mnemonic = read_mnemonic(platform)
            unlock_from_terminal(platform)
            info = import_mnemonic(name, mnemonic, network, wtype)
            del mnemonic
            print(f"imported {info.name} ({info.network}) {info.address}")
            return 0
        if cmd == "list" and not rest:
            unlock_from_terminal(platform)
            for i in list_wallets():
                print(f"{i.name}\t{i.network}\t{i.address}")
            return 0
        if cmd == "delete" and len(rest) == 1:
            name = checked_name(rest[0])
            unlock_from_terminal(platform)
            print(
                f"Delete wallet '{name}' from the secret store? Type y to confirm: ",
                end="",
                flush=True,
            )
            if read_human_line(platform) != "y":
                print("not deleted")
                return 1
            delete_wallet(name)
            print(f"deleted {name}")
            return 0
        print(USAGE, file=sys.stderr)
        return 2
    except (WalletError, SecretError, EOFError) as exc:
        print(f"WALLET ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
