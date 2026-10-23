"""The VAULT CLI (matrix VAULT01): the native Strike vault surface as SERVED.

Reads (public + depositor, live):
    python -m struck.vault_cli [--network <name>] list
    python -m struck.vault_cli detail|portfolio|history <vault_uuid>
    python -m struck.vault_cli depositors|liquidations|announcements <vault_uuid>
    python -m struck.vault_cli position <vault_uuid> [--account <uuid>]
Writes (HUMAN-GATED, async per skill strike-vaults):
    python -m struck.vault_cli deposit <vault_uuid> <usd_amount> --approver <name>
    python -m struck.vault_cli withdraw <vault_uuid> [--shares <shares>] --approver <name>
Additionally, deposit/withdraw/cancel bodies can be PREPARED without sending:
    python -m struck.vault_cli prepare <deposit|withdraw> <vault_uuid> [<amount_or_shares>]

Writes are queued commands ("200 means the command was queued for the engine;
the outcome arrives as a VAULT_* user-stream event") and NOT idempotent ("a
blind retry deposits or withdraws twice"). So the CLI refuses a piped stdin,
prints the exact route + network + canonical body, and the human types exactly
``approve <hash>`` = the sha256 of ``network|method|route|body``. Nothing is
sent before that line. The served response is printed verbatim and receipted
under receipts/vault/. Terminal venue outcomes carry VAULT_* events (the
user-stream rail's correlation).
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from .callhistory import RecordedHttp
from .config import load_config, network_banner, network_help, pop_network_flag, wants_help
from .strike.account import StrikeAuthedClient
from .strike.auth import StrikeCredentialsError
from .strike.venue_auth import wallet_from_secrets
from .tty import read_human_line
from .vault import StrikeVaultClient, VaultError

RECEIPTS_DIR = Path("receipts") / "vault"


def _served_sha256(data: bytes) -> str:
    import hashlib as _hashlib

    return _hashlib.sha256(data).hexdigest()


def _receipt(name: str, lines: list[str]) -> Path:
    RECEIPTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RECEIPTS_DIR / name
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
    tmp.replace(path)
    return path


def _fork(case: str, reason: str) -> int:
    print(f"{case}: FAILED: {reason}", file=sys.stderr)
    return 1


def _single_read(client: StrikeVaultClient, cmd: str, vault_id: str) -> int:
    """One public/depositor-free read, served verbatim."""
    if cmd in ("detail", "portfolio"):
        served_r, _row = client.detail(vault_id) if cmd == "detail" else client.portfolio(vault_id)
        print(served_r.content.decode("utf-8"))
        return 0
    if cmd == "history":
        served_h, hist = client.history(vault_id)
        print(f"# rows served: {len(hist.rows)}; next_cursor: {hist.next_cursor}")
        print(served_h.content.decode("utf-8"))
        return 0
    served_s = (
        client.depositors(vault_id)
        if cmd == "depositors"
        else client.liquidations(vault_id)
        if cmd == "liquidations"
        else client.announcements(vault_id)
    )
    print(served_s.content.decode("utf-8"))
    return 0


def _depositor_position(argv: list[str], http: RecordedHttp) -> int:
    """THE signed depositor read (GET /v2/vault/position), through the
    production signed client: unlock (masked) -> signed GET."""
    try:
        wallet = wallet_from_secrets(unlock=True)
    except StrikeCredentialsError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 2
    rest: list[str] = [a for a in argv if not a.startswith("--")]
    vault_id = rest[0] if rest else ""
    account_id: str | None = None
    if "--account" in argv:
        account_id = argv[argv.index("--account") + 1]
    cfg = load_config()
    try:
        authed = StrikeAuthedClient(http, cfg.strike_api_url, wallet)
        path = f"/v2/vault/position?vault_id={vault_id}"
        if account_id is not None:
            path += f"&account_id={account_id}"
        served = authed.request("GET", path, None)
        print(served.content.decode("utf-8"))
        return 0
    except (VaultError, ValidationError) as exc:
        return _fork("position", str(exc))


def _write(
    word: str,
    argv: list[str],
    http: RecordedHttp,
) -> int:
    """HUMAN-GATED write: prepare -> show -> typed approve <hash> -> signed POST."""
    try:
        wallet = wallet_from_secrets(unlock=True)
    except StrikeCredentialsError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 2
    cfg_loaded = load_config()
    authed = StrikeAuthedClient(http, cfg_loaded.strike_api_url, wallet)
    route: str
    body: dict[str, str | bool]
    if word == "deposit":
        vault_id, amount = argv[0], argv[1]
        client_prep = StrikeVaultClient(http, cfg_loaded.strike_api_url)
        body = client_prep.prepare_deposit(vault_id, amount)
        route = "/v2/vault/deposit"
    else:
        vault_id = argv[0]
        shares: str | None = None
        if "--shares" in argv:
            shares = argv[argv.index("--shares") + 1]
        client_prep = StrikeVaultClient(http, cfg_loaded.strike_api_url)
        body = client_prep.prepare_withdraw(vault_id, shares=shares)
        route = "/v2/vault/withdraw"
    body_bytes = json.dumps(body, separators=(",", ":")).encode("utf-8")
    approve_hash = _write_hash(cfg_loaded.network, "POST", route, body_bytes)
    print("=== VAULT WRITE APPROVAL (nothing is sent yet) ===")
    print(network_banner(cfg_loaded))
    print(f"route: POST {authed.base_url}{route}")
    print(f"body (verbatim request): {body_bytes.decode('utf-8')}")
    print("This is an ASYNC venue command: the served 200/4xx is the queue verdict;")
    print("the durable outcome arrives as VAULT_* events. NOT idempotent: a retry double-sends.")
    print(f"Type exactly 'approve {approve_hash}' or 'reject':", flush=True)
    try:
        answer = read_human_line()
    except (EOFError, OSError) as exc:
        print(f"NOT READ: {exc}; nothing sent.")
        return 3
    if answer != f"approve {approve_hash}":
        if answer == "reject":
            print("REJECTED by the human: nothing sent.")
            return 1
        print(f"REJECTED: unrecognised approval input {answer!r}: nothing sent.")
        return 1
    served = authed.request("POST", route, body_bytes)
    served_text = served.content.decode("utf-8")
    receipt_path = _receipt(
        f"{word}-{approve_hash}.txt",
        [
            f"# served {served.status} sha256 {served.sha256}",
            network_banner(cfg_loaded),
            f"route: POST {route}",
            f"request: {body_bytes.decode('utf-8')}",
            f"# request body sha256 {approve_hash}",
            f"response: {served_text}",
        ],
    )
    print(f"served HTTP {served.status}: {served_text}")
    print(f"receipt: {receipt_path}")
    return 0 if 200 <= served.status < 300 else 1


def _write_hash(network: str, method: str, route: str, body: bytes) -> str:
    return hashlib.sha256(f"{network}|{method}|{route}|{body.decode('utf-8')}".encode("utf-8")).hexdigest()


def main(argv: list[str]) -> int:
    if wants_help(argv):
        print(f"{__doc__}\n{network_help()}", file=sys.stderr)
        return 0
    argv = pop_network_flag(argv)
    cfg = load_config()
    http = RecordedHttp(cfg.timeout_s)
    try:
        try:
            if argv and argv[0] == "prepare" and len(argv) in (3, 4):
                prepped = _prepare(argv[1], argv[2], None if len(argv) == 3 else argv[3])
                print(prepped)
                return 0
            client = StrikeVaultClient(http, cfg.strike_api_url)
            cmd = argv[0] if argv else ""
            if cmd == "list":
                served_l, page_l = client.list_vaults()
                print(served_l.content.decode("utf-8"))
                print(f"# vaults served: {len(page_l.vaults)} / count {page_l.count}")
                return 0
            if cmd in ("detail", "portfolio", "history", "depositors", "liquidations", "announcements"):
                if len(argv) != 2:
                    print(f"usage: {cmd} <vault_uuid>", file=sys.stderr)
                    return 2
                return _single_read(client, cmd, argv[1])
            if cmd == "position":
                return _depositor_position(argv, http)
            if cmd == "deposit" and len(argv) == 4 and argv[2] == "--approver":
                return _write("deposit", argv[:2], http)
            if cmd == "withdraw" and len(argv) == 3 and argv[2] == "--approver":
                return _write("withdraw", argv[:-2], http)
            if cmd == "withdraw" and len(argv) == 6 and argv[2] == "--shares" and argv[4] == "--approver":
                return _write("withdraw", argv[-6:-2], http)
            print(f"{__doc__}\n{network_help()}", file=sys.stderr)
            return 2
        except (VaultError, ValidationError) as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 2
    finally:
        http.close()


def _prepare(word: str, vault_id: str, amount: str | None) -> str:
    """Preparation-only helper: validates the write body, sends NOTHING."""
    import json as _json

    prepper = StrikeVaultClient.__new__(StrikeVaultClient)
    body: dict[str, str | bool]
    route: str
    if word == "deposit":
        if amount is None:
            raise VaultError("prepare deposit needs <usd_amount>")
        body = prepper.prepare_deposit(vault_id, amount)
        route = "/v2/vault/deposit"
    elif word == "withdraw":
        body = prepper.prepare_withdraw(vault_id, shares=amount)
        route = "/v2/vault/withdraw"
    else:
        raise VaultError(f"unknown prepare kind {word!r}: use deposit or withdraw")
    payload = _json.dumps(body, separators=(",", ":"))
    return (
        f"PREPARED (NOT sent): POST {route} body {payload}\n"
        f"approval hash at execution time is sha256('network|POST|{route}|{payload}')\n"
        f"send by: python -m struck.vault_cli {word} {vault_id}"
        + ("" if amount is None or word == "deposit" else f" --shares {amount}")
        + " --approver <name>"
    )


if __name__ == "__main__":  # pragma: no cover - manual CLI
    raise SystemExit(main(sys.argv[1:]))
