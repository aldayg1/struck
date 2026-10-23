"""Trigger CRUD + one evaluation tick (the 
in a tmux daemon; here a tick is an explicit command).

  uv run python -m struck.triggers.cli add <trigger-spec.json>
  uv run python -m struck.triggers.cli list
  uv run python -m struck.triggers.cli update <id> <trigger-spec.json>
  uv run python -m struck.triggers.cli pause|resume|remove <id>
  uv run python -m struck.triggers.cli tick
  (every form takes [--network <name>])

A tick that fires writes PROPOSALS only (``receipts/proposals/``); each
needs a fresh human approval through the normal pipeline.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

from ..callhistory import RecordedHttp
from ..strike.account import ServedStrikeAccount, StrikeAuthedClient
from ..strike.history import FillRow, PositionLife, fills, position_history, transactions
from ..strike.auth import StrikeCredentialsError
from ..strike.venue_auth import wallet_from_secrets
from ..config import load_config, pop_network_flag, network_help, wants_help
from ..strike.market import StrikeMarketClient
from .engine import Engine, TriggerError, TriggerStore
from .kupo_watch import KupoWatch
from .models import TriggerSpec
from .policy import ApprovedPolicies

USAGE = (
    "usage: python -m struck.triggers.cli add <spec.json> | list | update <id> <spec.json> "
    "| pause|resume|remove <id> | tick  [--network <name>]"
)


def _now_ms(now: datetime) -> int:
    return int(now.timestamp() * 1000)


def main(argv: list[str]) -> int:
    if wants_help(argv):
        print(USAGE + "\n" + network_help(), file=sys.stderr)
        return 0
    argv = pop_network_flag(argv)
    store = TriggerStore()
    now = datetime.now(UTC)
    match argv:
        case ["add", path]:
            record = store.add(TriggerSpec.model_validate_json(Path(path).read_bytes()), now)
            print(f"added {record.id} ({record.spec.label})")
        case ["list"]:
            for r in store.list():
                print(
                    f"{r.id} [{r.status}] {r.spec.label}: last {r.last_evaluated} {r.last_result}"
                )
        case ["update", trigger_id, path]:
            store.update(trigger_id, TriggerSpec.model_validate_json(Path(path).read_bytes()), now)
            print(f"updated {trigger_id}")
        case ["pause", trigger_id]:
            store.set_status(trigger_id, "paused", now)
            print(f"paused {trigger_id}")
        case ["resume", trigger_id]:
            store.set_status(trigger_id, "active", now)
            print(f"resumed {trigger_id}")
        case ["remove", trigger_id]:
            store.remove(trigger_id)
            print(f"removed {trigger_id}")
        case ["tick"]:
            cfg = load_config()
            http = RecordedHttp(cfg.timeout_s)
            try:
                # strike_position reads the served /v2/positions with YOUR API
                # wallet; without it those conditions fail closed (not fired).
                try:
                    client = StrikeAuthedClient(
                        http, cfg.strike_api_url, wallet_from_secrets(unlock=True)
                    )
                    account: ServedStrikeAccount | None = ServedStrikeAccount(client)
                except StrikeCredentialsError as e:
                    print(f"strike_position conditions disabled: {e}", file=sys.stderr)
                    client = None
                    account = None
                deposits = None
                balances = None
                fills_rows: tuple[FillRow, ...] | None = None
                position_rows: tuple[PositionLife, ...] | None = None
                if client is not None:
                    # strike-history: status 2 is completed. type name "deposit".
                    _, history = transactions(client, tx_type="deposit", status=2, limit=100)
                    deposits = tuple(history.transactions)
                    # Signed reads for strike_balance / strike_realized_pnl.
                    balances = client.balances()[1].root
                    _, fill_hist = fills(client, start_ms=_now_ms(now) - 24 * 60 * 60_000, limit=200)
                    fills_rows = tuple(fill_hist.fills)
                    _, pos_hist = position_history(
                        client, start_ms=_now_ms(now) - 24 * 60 * 60_000, limit=200
                    )
                    position_rows = tuple(pos_hist.positions)
                engine = Engine(
                    store,
                    KupoWatch(http, cfg.kupo_url),
                    StrikeMarketClient(http, cfg.strike_api_url),
                    account,
                    ApprovedPolicies(),
                    balances=balances,
                    fills=fills_rows,
                    position_lives=position_rows,
                )
                for line in engine.tick(now, deposits):
                    print(line)
            finally:
                http.close()
        case _:
            print(USAGE + "\n" + network_help(), file=sys.stderr)
            return 2
    return 0


def _main_loud(argv: list[str]) -> int:
    try:
        return main(argv)
    except (TriggerError, ValueError, OSError) as exc:  # pydantic errors are ValueError
        print(f"FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(_main_loud(sys.argv[1:]))
