"""Strike API-wallet accounts: ONE per Strike environment, in Struck's encrypted store.

PORT (re-typed, not imported) of 
2d47b95dd, sha256 f2dc7419…e232604; see the golden provenance record). Kept: an account binds
an Ed25519 API wallet (docs.strikefinance.org api/getting-started: public key
64 hex chars, raw Ed25519; private key 64 or 128 hex chars, seed or full key)
to a venue environment and an optional Struck wallet name; the key pair is
validated before it is stored; registration happens on Strike's API-keys page
(``NetworkProfile.strike_api_keys_url``); a signed ``GET /v2/account`` on the
selected network's host verifies it (200 only for a registered key).
Changed: the store is Struck's cryptfile (``struck.secrets``, entry
``strike_api_<testnet|mainnet>``, one JSON record with the metadata and the
private key; nothing in plain files); exactly one account per environment, so
there is no "first matching account" choice; the derived public key must equal
the stored one; ``generate`` makes the key pair here (stock PyNaCl) and prints
only the public key.

    python -m struck.strike.venue_auth [--secrets-file P] [--network N] generate <label> [--wallet W] [--replace]
    python -m struck.strike.venue_auth [--secrets-file P] [--network N] import <label> <public_hex> [--wallet W] [--replace]
    python -m struck.strike.venue_auth [--secrets-file P] [--network N] verify
    python -m struck.strike.venue_auth [--secrets-file P] list
    python -m struck.strike.venue_auth [--secrets-file P] [--network N] delete
    python -m struck.strike.venue_auth [--secrets-file P] [--network N] import-legacy <label>
    python -m struck.strike.venue_auth [--secrets-file P] [--network N] connect <label> --wallet <name>
        --code <builder_code>

``connect`` is the builder-connect flow (strike-auth skill): the stored
Struck wallet's payment key CIP-30-signs Strike's served consent message, at
a real-terminal gate; Strike creates/uses the account keyed by that wallet
and registers a FRESH API-wallet key pair (the previous stored pair is kept
under ``strike_api_<env>_previous`` for rollback). fee_share_bps is 0.

``import`` reads the PRIVATE key from a hidden prompt (or one stdin line),
never from argv.
"""

from __future__ import annotations

import getpass
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, TextIO, cast

from nacl.signing import SigningKey
from pydantic import BaseModel, ConfigDict, SecretStr, StrictStr

from ..callhistory import CallFailed, RecordedHttp, sha256_hex
from ..config import RECEIPTS_DIR, load_config, pop_network_flag
from ..network import NetworkProfile
from ..secrets import (
    STRIKE_PRIVATE,
    STRIKE_PUBLIC,
    SecretError,
    delete_secret,
    get_secret,
    has_secret,
    set_secret,
    unlock_from_terminal,
)
from ..tty import read_human_line
from ..wallets import WalletError, load_signer, pop_secrets_file
from .account import StrikeAuthedClient
from .auth import ApiWallet, StrikeCredentialsError, wallet_from_hex
from .builder_auth import FEE_SHARE_BPS, BuilderConnectClient, cip30_sign_data

Environment = Literal["testnet", "mainnet"]
ENVIRONMENTS: tuple[Environment, ...] = ("testnet", "mainnet")
_LABEL = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_HEX128 = re.compile(r"^[0-9a-f]{128}$")


class VenueAccount(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    label: StrictStr
    environment: Environment
    wallet: StrictStr  # the Struck Cardano wallet it belongs to ("" = none)
    public_key_hex: StrictStr
    private_key_hex: SecretStr  # 64 hex (seed); never printed
    created_utc: StrictStr


def entry_for(environment: Environment) -> str:
    return f"strike_api_{environment}"


def environment_of(profile: NetworkProfile) -> Environment:
    venue = profile.strike_venue
    if venue == "testnet":
        return "testnet"
    if venue == "mainnet":
        return "mainnet"
    raise StrikeCredentialsError(f"network {profile.name} has no Strike environment ({venue!r})")


def validate_keypair(public_hex: str, private_hex: str) -> tuple[str, str]:
    """(public hex, 64-hex seed). Private: 64 hex (seed) or 128 hex (seed + public);
    the derived public key must equal ``public_hex``. SSH-format keys are refused."""
    pub = public_hex.strip().lower()
    priv = private_hex.strip().lower()
    if not _HEX64.fullmatch(pub):
        raise StrikeCredentialsError(
            "Strike API wallet public key must be 64 hex chars (raw Ed25519); "
            "SSH-format keys are incompatible"
        )
    if _HEX128.fullmatch(priv):
        priv = priv[:64]
    elif not _HEX64.fullmatch(priv):
        raise StrikeCredentialsError(
            "Strike API wallet private key must be 64 or 128 hex chars (seed or full key); "
            "SSH-format keys are incompatible"
        )
    wallet_from_hex(pub, priv)  # raises unless the seed derives this public key
    return pub, priv


def _label(label: str) -> str:
    if not _LABEL.fullmatch(label):
        raise StrikeCredentialsError(
            f"account label {label!r}: lowercase letters, digits, '_' or '-', 1-48 chars"
        )
    return label


def save_account(
    environment: Environment,
    label: str,
    public_hex: str,
    private_hex: str,
    wallet: str = "",
    *,
    replace: bool = False,
) -> VenueAccount:
    pub, seed = validate_keypair(public_hex, private_hex)
    entry = entry_for(environment)
    if has_secret(entry) and not replace:
        raise StrikeCredentialsError(
            f"A Strike {environment} account is already stored. To create a NEW one, tick "
            "'replace the stored account' in the dialogue, then press Generate again. "
            "Pasted a key pair? Press 'Save account' after ticking. Creating new? Press Generate. To keep the stored one, press 'Verify wallet on Strike'."
        )
    account = VenueAccount(
        label=_label(label),
        environment=environment,
        wallet=wallet,
        public_key_hex=pub,
        private_key_hex=SecretStr(seed),
        created_utc=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    record: dict[str, str] = {
        "label": account.label,
        "environment": account.environment,
        "wallet": account.wallet,
        "public_key_hex": account.public_key_hex,
        "private_key_hex": seed,
        "created_utc": account.created_utc,
    }
    set_secret(entry, json.dumps(record, separators=(",", ":")))
    if load_account(environment).public_key_hex != pub:
        raise SecretError(f"read-back of {entry} differs")
    return account


def generate_account(
    environment: Environment, label: str, wallet: str = "", *, replace: bool = False
) -> VenueAccount:
    """A NEW Ed25519 key pair (stock PyNaCl, OS randomness), stored at once; only the
    public key leaves this function in clear (``VenueAccount.public_key_hex``)."""
    key = SigningKey.generate()
    return save_account(
        environment,
        label,
        key.verify_key.encode().hex(),
        key.encode().hex(),
        wallet,
        replace=replace,
    )


def load_account(environment: Environment) -> VenueAccount:
    try:
        raw = get_secret(entry_for(environment))
    except SecretError as exc:
        legacy = ""
        try:
            if has_secret(STRIKE_PUBLIC) or has_secret(STRIKE_PRIVATE):
                legacy = (
                    f"; network-less entries {STRIKE_PUBLIC}/{STRIKE_PRIVATE} exist: bind them "
                    "to their network with `python -m struck.strike.venue_auth --network "
                    "<network> import-legacy <label>`"
                )
        except SecretError:
            pass
        raise StrikeCredentialsError(
            f"no Strike {environment} API wallet (entry {entry_for(environment)}) in the secret "
            f"store ({exc}){legacy}. Register "
            "a public key on Strike's API-keys page and store the pair with `python -m "
            "struck.strike.venue_auth generate|import` (env vars are never read; no fallback)"
        ) from exc
    try:
        return VenueAccount.model_validate_json(raw)
    except ValueError as exc:
        raise StrikeCredentialsError(f"{entry_for(environment)} is not a valid record") from exc


def wallet_for(environment: Environment) -> ApiWallet:
    account = load_account(environment)
    return wallet_from_hex(account.public_key_hex, account.private_key_hex.get_secret_value())


def wallet_from_secrets(unlock: bool = False) -> ApiWallet:
    """The API wallet of the SELECTED network's Strike environment (``load_config``).
    ``unlock=True`` (CLIs) first asks for the master password at a real terminal."""
    if unlock:
        try:
            unlock_from_terminal()
        except SecretError as exc:
            raise StrikeCredentialsError(f"Strike API wallet not available: {exc}") from exc
    try:
        return wallet_for(environment_of(load_config().profile))
    except SecretError as exc:
        raise StrikeCredentialsError(f"Strike API wallet not available: {exc}") from exc


def list_accounts() -> list[VenueAccount]:
    return [load_account(e) for e in ENVIRONMENTS if has_secret(entry_for(e))]


def delete_account(environment: Environment) -> None:
    delete_secret(entry_for(environment))


def import_legacy(environment: Environment, label: str, *, replace: bool = False) -> VenueAccount:
    """Bind the old network-less pair to ONE environment, then delete the old entries."""
    account = save_account(
        environment, label, get_secret(STRIKE_PUBLIC), get_secret(STRIKE_PRIVATE), replace=replace
    )
    delete_secret(STRIKE_PUBLIC)
    delete_secret(STRIKE_PRIVATE)
    return account


def verify(wallet: ApiWallet, profile: NetworkProfile, http: RecordedHttp) -> str:
    """Signed GET /v2/account on the network's own Strike host; Strike answers 200
    only for a registered key. Returns the served body; raises ``CallFailed`` otherwise."""
    client = StrikeAuthedClient(http, profile.strike_api_url, wallet)
    served = client.request("GET", "/v2/account")
    return served.content.decode("utf-8", "replace")


def _write(out: Path, name: str, text: str) -> None:
    (out / name).write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")


def _connect(
    environment: Environment, label: str, code: str, wallet_name: str, profile: NetworkProfile
) -> int:
    """Builder connect (strike-auth skill): the stored wallet CIP-30-signs the
    served consent at a real-terminal gate; Strike binds that wallet to a Strike
    account and registers a FRESH API-wallet pair, which is then stored here
    (the previous pair is preserved under ``strike_api_<env>_previous``)."""
    signer = load_signer(wallet_name, profile.name)
    key = SigningKey.generate()  # a fresh pair every connect: public keys are unique
    public_hex = key.verify_key.encode().hex()
    cfg = load_config()
    consent_hash = sha256_hex(
        f"connect|{environment}|{code}|{signer.address}|{public_hex}".encode("utf-8")
    )
    http = RecordedHttp(cfg.timeout_s)
    try:
        client = BuilderConnectClient(http, profile.strike_api_url)
        challenge_served, challenge = client.request_challenge(
            signer.address, "cardano", code, FEE_SHARE_BPS, public_hex
        )
        print("=== BUILDER CONNECT: the served consent message, verbatim ===")
        print(challenge.message_to_sign)
        print(
            f"account label: {label}\n"
            f"wallet: {signer.name} ({signer.network}) {signer.address}\n"
            f"builder code: {code} | fee cap {FEE_SHARE_BPS} bps\n"
            f"fresh API-wallet public key: {public_hex}\n"
            f"consent_hash: {consent_hash}"
        )
        print(f"Type exactly 'connect {consent_hash}' or 'abort':", flush=True)
        answer = read_human_line()
        if answer == "abort":
            print("ABORTED by the human; nothing was signed or stored.")
            return 1
        if answer != f"connect {consent_hash}":
            print(f"REJECTED: unrecognised input {answer!r}")
            return 1
        wallet_signature = cip30_sign_data(signer, challenge.message_to_sign)
        verify_served, result = client.verify(challenge.nonce, wallet_signature)
    finally:
        http.close()
    if result.api_wallet_public_key != public_hex:
        raise StrikeCredentialsError(
            f"Strike registered {result.api_wallet_public_key}, not the fresh pair's "
            f"{public_hex}; nothing was stored"
        )
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = RECEIPTS_DIR / f"{stamp}-builder-connect-{consent_hash[:12]}"
    out.mkdir(parents=True, exist_ok=False)
    _write(out, "challenge.json", challenge_served.content.decode("utf-8", "replace"))
    _write(out, "verify.json", verify_served.content.decode("utf-8", "replace"))
    _write(
        out,
        "MANIFEST.txt",
        "\n".join(
            [
                f"network: {profile.name} (Strike {profile.strike_venue})",
                f"builder code: {code} (fee cap {FEE_SHARE_BPS} bps)",
                f"wallet: {signer.name} {signer.address}",
                f"account_id: {result.account_id}",
                f"api_wallet_public_key: {result.api_wallet_public_key}",
                "bodies as served; no private key or signature is ever written",
            ]
        ),
    )
    entry = entry_for(environment)
    if has_secret(entry):
        set_secret(entry + "_previous", get_secret(entry))  # encrypted rollback copy
    save_account(environment, label, public_hex, key.encode().hex(), wallet_name, replace=True)
    print(
        f"CONNECTED: Strike account {result.account_id} now has wallet {signer.address}\n"
        f"API wallet {result.api_wallet_id} {result.api_wallet_public_key} "
        f"(expires {result.api_wallet_expired_at})\n"
        f"stored as '{label}' (previous pair kept at {entry}_previous)\n"
        f"receipts: {out}"
    )
    return 0


def _read_private(platform: str) -> str:
    stdin = cast("TextIO", sys.stdin)
    if stdin.isatty():
        return str(getpass.getpass("Strike API wallet PRIVATE key, hex (hidden): "))
    line = stdin.readline()
    if not line.strip():
        raise StrikeCredentialsError("no private key on stdin")
    return line


def _pop(args: list[str], flag: str) -> tuple[str | None, list[str]]:
    if flag not in args:
        return None, args
    i = args.index(flag)
    if i + 1 >= len(args):
        raise StrikeCredentialsError(f"{flag} needs a value")
    return args[i + 1], args[:i] + args[i + 2 :]


USAGE = __doc__.split("\n\n")[-2] if __doc__ else ""


def main(argv: list[str], platform: str = sys.platform) -> int:
    try:
        args = pop_network_flag(pop_secrets_file(argv))
        wallet_name, args = _pop(args, "--wallet")
        replace = "--replace" in args
        args = [a for a in args if a != "--replace"]
        if not args:
            print(USAGE, file=sys.stderr)
            return 2
        cmd, rest = args[0], args[1:]
        profile = load_config().profile
        environment = environment_of(profile)
        unlock_from_terminal(platform)
        if cmd == "generate" and len(rest) == 1:
            account = generate_account(environment, rest[0], wallet_name or "", replace=replace)
            print(
                f"generated Strike {environment} API wallet '{account.label}' (private key stored, not shown)"
            )
            print(f"register this PUBLIC key at {profile.strike_api_keys_url}:")
            print(account.public_key_hex)
            return 0
        if cmd == "import" and len(rest) == 2:
            account = save_account(
                environment,
                rest[0],
                rest[1],
                _read_private(platform),
                wallet_name or "",
                replace=replace,
            )
            print(
                f"stored Strike {environment} API wallet '{account.label}' {account.public_key_hex}"
            )
            return 0
        if cmd == "import-legacy" and len(rest) == 1:
            account = import_legacy(environment, rest[0], replace=replace)
            print(
                f"bound legacy pair to Strike {environment} as '{account.label}' {account.public_key_hex}"
            )
            return 0
        if cmd == "list" and not rest:
            for a in list_accounts():
                print(
                    f"{a.environment:<8} {a.label:<24} {a.wallet or '-':<20} {a.public_key_hex} {a.created_utc}"
                )
            return 0
        if cmd == "delete" and not rest:
            delete_account(environment)
            print(f"deleted {entry_for(environment)}")
            return 0
        if cmd == "verify" and not rest:
            cfg = load_config()
            http = RecordedHttp(cfg.timeout_s)
            try:
                body = verify(wallet_for(environment), profile, http)
            except CallFailed as exc:
                print(f"NOT REGISTERED / REFUSED on {profile.strike_api_url}: {exc}")
                return 1
            finally:
                http.close()
            print(f"VERIFIED: signed GET /v2/account on {profile.strike_api_url} -> 200")
            print(body[:2000])
            return 0
        if cmd == "connect" and len(rest) == 3 and rest[1] == "--code":
            if not wallet_name:
                print("connect needs --wallet <name>: the Struck wallet that signs", file=sys.stderr)
                return 2
            return _connect(environment, rest[0], rest[2], wallet_name, profile)
        print(USAGE, file=sys.stderr)
        return 2
    except (StrikeCredentialsError, SecretError, WalletError, CallFailed, ValueError) as exc:
        print(f"STRIKE KEY ERROR: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
