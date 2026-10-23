"""Readiness check AFTER the operator sets his Strike API wallet (selected network).

  uv run python -m struck.keys set strike_api_wallet_public_key   # 64 hex, as registered
  uv run python -m struck.keys set strike_api_wallet_private_key  # 64 hex Ed25519 seed
  uv run python -m struck.strike.account_cli [--network <name>] [--no-values]

READ-ONLY: ``GET /v2/time`` (public), signed ``GET /v2/account`` and
``GET /v2/positions``. No orders, no writes. Every call goes to the call
history (headers never recorded); served bodies are saved as receipts.
Exit 0 = authenticated reads ok; 2 = credentials missing/invalid; 1 = HTTP error.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from ..callhistory import CallFailed, RecordedHttp
from ..config import (
    RECEIPTS_DIR,
    load_config,
    network_banner,
    pop_network_flag,
    network_help,
    wants_help,
)
from .account import StrikeAuthedClient
from .auth import StrikeCredentialsError
from .venue_auth import wallet_from_secrets


def main(argv: list[str] | None = None) -> int:
    """READ-ONLY: GET /v2/account and GET /v2/positions, signed. ``--network``
    selects the deployment; ``--no-values`` prints field names + HTTP status
    only and writes only the served bodies' sha256 (no balances anywhere)."""
    raw = list(sys.argv[1:] if argv is None else argv)
    usage = (
        "usage: python -m struck.strike.account_cli [--network <name>] [--no-values]\n"
        + network_help()
    )
    if wants_help(raw):
        print(usage, file=sys.stderr)
        return 0
    args = pop_network_flag(raw)
    if any(a != "--no-values" for a in args):
        print(f"unknown argument(s) {args}\n{usage}", file=sys.stderr)
        return 2
    no_values = "--no-values" in args
    try:
        wallet = wallet_from_secrets(unlock=True)
    except StrikeCredentialsError as e:
        print(f"FAILED: {e}", file=sys.stderr)
        return 2
    cfg = load_config()
    out = RECEIPTS_DIR / "strike" / f"account-{time.strftime('%Y%m%dT%H%M%S')}"
    http = RecordedHttp(cfg.timeout_s)
    try:
        client = StrikeAuthedClient(http, cfg.strike_api_url, wallet)
        acc_served, acc = client.account()
        pos_served, pos = client.positions()
    except CallFailed as e:
        print(f"FAILED: {e}", file=sys.stderr)
        return 1
    finally:
        http.close()
    out.mkdir(parents=True, exist_ok=True)
    print(network_banner(cfg))
    if no_values:
        print(f"API wallet (encrypted store; not printed) authenticated on {cfg.strike_api_url}")
        for name, served in (("/v2/account", acc_served), ("/v2/positions", pos_served)):
            print(
                f"GET {name} -> HTTP {served.status}, {len(served.content)} bytes, sha256 {served.sha256}"
            )
        print(f"/v2/account fields (served): {', '.join(type(acc).model_fields)}")
        print(f"/v2/positions: {len(pos.positions)} position(s) served")
        (out / "served.sha256.txt").write_text(
            f"account {acc_served.status} {acc_served.sha256}\npositions {pos_served.status} {pos_served.sha256}\n",
            encoding="utf-8",
        )
        print(f"receipts (sha256 only): {out}")
        return 0
    print(f"API wallet {wallet.public_key_hex[:8]}… authenticated on {cfg.strike_api_url}")
    Path(out / "account.json").write_bytes(acc_served.content)
    Path(out / "positions.json").write_bytes(pos_served.content)
    print(
        f"/v2/account (served): wallet_balance {acc.wallet_balance} available_balance {acc.available_balance} "
        f"unrealized_pnl {acc.unrealized_pnl} margin_balance {acc.margin_balance} maintenance_margin {acc.maintenance_margin}"
    )
    print(f"/v2/positions (served): {len(pos.positions)} open")
    for p in pos.positions:
        print(
            f"  {p.symbol} size {p.size} entry {p.entry_price} upnl {p.upnl} maintenance_margin {p.maintenance_margin} "
            f"liquidation_price {p.liquidation_price} bankruptcy_price {p.bankruptcy_price}"
        )
    print(f"receipts: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
