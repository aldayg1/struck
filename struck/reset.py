"""Guarded ACCOUNT RESET (operator request 2026-10-22): "log out" must be
possible (re-registration must be testable), but NEVER while an asset could
be lost. The guard is a LIVE read-only audit — Strike balances, positions and
open-order holds; every imported Cardano wallet's unspent chain state; stored
triggers — and the reset refuses BY NAME on any nonzero value, any position,
any open-order hold or any trigger. Nothing destructive runs on a failed,
locked or incomplete audit. Python holds no money arithmetic: every quantity
comparison goes through the Rust ``struck_py.decimal_compare``. The reset
deletes ONLY Struck's own credential surface (secret store, Strike venue env
file, registration marker, LLM config, trigger store); ``network.json`` and
the endpoints file are NOT touched (no data is lost by keeping them)."""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

from pydantic import BaseModel, ConfigDict, StrictBool, StrictStr

from . import secrets
from .callhistory import RecordedHttp
from .config import LLM_CONFIG_PATH, StruckConfig, load_config
from .keys import STRIKE_ENV_PATH
from .strike.account import StrikeAuthedClient
from .strike.auth import StrikeCredentialsError
from .strike.venue_auth import wallet_from_secrets
from .triggers.engine import STORE_PATH, TriggerStore
from .wallets import list_wallets

TRIGGER_PATH: Path = STORE_PATH  # reset-side indirection so tests can isolate it


class AuditRow(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    source: StrictStr  # e.g. "strike balance ADA" | "wallet <name>" | "triggers"
    network: StrictStr
    clear: StrictBool
    detail: StrictStr


class AuditReport(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    rows: tuple[AuditRow, ...]
    clear: StrictBool


class ResetRefused(RuntimeError):
    """The audit did not prove 'nothing can be lost'; no file was touched."""


def _block(source: str, network: str, detail: str) -> AuditRow:
    return AuditRow(source=source, network=network, clear=False, detail=detail)


def _hold(source: str, network: str, findings: list[str]) -> AuditRow:
    """A clear row, or a loud blocking row when findings are nonempty."""
    if findings:
        return _block(source, network, "; ".join(findings))
    return AuditRow(source=source, network=network, clear=True, detail="0")


def _zero_pct(value: str, what: str) -> str | None:
    """None when the quantity is zero, else a loud detail; Rust compares."""
    from struck_py import decimal_compare  # Rust: the ONLY quantity comparator

    if not value.strip():
        return f"{what}: served empty (unparseable — audited UNCLEAR)"
    try:
        if decimal_compare(value, "eq", "0"):
            return None
    except ValueError as exc:
        return f"{what}: {value!r} does not parse as a quantity: {exc}"
    return f"{what} = {value}"


def _strike_rows(http: RecordedHttp, cfg: StruckConfig) -> tuple[AuditRow, ...]:
    """LIVE /v2/balances + /v2/positions on the SELECTED network (read-only).
    Any failure — locked store, refused request, unparseable body — BLOCKS: an
    unreadable account is not a proven-empty account."""
    net = cfg.network
    try:
        wallet = wallet_from_secrets()
    except StrikeCredentialsError as exc:
        return (_block("strike auth", net, f"cannot audit: {exc}"),)
    client = StrikeAuthedClient(http, cfg.strike_api_url, wallet)
    rows: list[AuditRow] = []
    try:
        _served, balances = client.balances()
        for b in balances.root:
            findings = list(
                f
                for f in (
                    _zero_pct(b.walletBalance, "walletBalance"),
                    _zero_pct(b.marginBalance, "marginBalance"),
                    _zero_pct(b.openOrderInitialMargin, "openOrderInitialMargin (open orders)"),
                    _zero_pct(b.stakingRewardBalance, "stakingRewardBalance"),
                    _zero_pct(b.lockedRewards, "lockedRewards"),
                )
                if f is not None
            )
            rows.append(_hold(f"strike balance {b.asset}", net, findings))
        _served, positions = client.positions()
        held = [
            f"{p.symbol} size {p.size} margin_mode {p.margin_mode} leverage {p.leverage} "
            f"(liquidation {p.liquidation_price})"
            for p in positions.positions
            if _zero_pct(p.size, "size") is not None
        ]
        rows.append(
            _hold(
                "strike positions",
                net,
                held or [],
            )
        )
    except Exception as exc:  # noqa: BLE001  refusals, timeouts, parse fails: NOT audited
        return (*rows, _block("strike account", net, f"audit request failed: {exc}"))
    return tuple(rows)


def _chain_rows(http: RecordedHttp, cfg: StruckConfig) -> tuple[AuditRow, ...]:
    """Every imported Cardano wallet's LIVE unspent state, read through the
    exact production backend for the pinned network (chainbackend.py)."""
    from .chain.backends import make_backend

    rows: list[AuditRow] = []
    try:
        wallets = list_wallets()
    except secrets.SecretError as exc:
        return (_block("cardano wallets", cfg.network, f"cannot read the wallet index: {exc}"),)
    if not wallets:
        return (AuditRow(source="cardano wallets", network=cfg.network, clear=True, detail="none imported"),)
    try:
        backend = make_backend(cfg, cfg.chain_backend, http)
    except Exception as exc:  # noqa: BLE001  no node socket, bad backend name: loud
        return (_block("cardano wallets", cfg.network, f"backend unavailable: {exc}"),)
    for w in wallets:
        if w.network != cfg.network:
            rows.append(
                _block(
                    f"wallet {w.name}",
                    w.network,
                    f"wallet is on {w.network}; the pinned network is {cfg.network} — "
                    "pin that network to audit it, or remove the wallet in its own flow first",
                )
            )
            continue
        try:
            view = backend.wallet_view(w.address, 0)
            findings = [
                f"utxo {u.reference}: {u.lovelace} lovelace"
                + (
                    f" + {', '.join(f'{a.policy_hex[:16]}…{a.name_hex}:{a.qty}' for a in u.assets)}"
                    if u.assets
                    else ""
                )
                for u in view.utxos
                if u.lovelace > 0 or u.assets
            ]
            rows.append(_hold(f"wallet {w.name}", cfg.network, findings))
        except Exception as exc:  # noqa: BLE001  unreachable reader: NOT audited
            rows.append(_block(f"wallet {w.name}", cfg.network, f"chain audit failed: {exc}"))
    return tuple(rows)


def _trigger_rows() -> tuple[AuditRow, ...]:
    """Stored triggers can fire later at funds — a reset removes the store
    only when it holds none."""
    try:
        stored = TriggerStore(TRIGGER_PATH).list()
    except Exception as exc:  # noqa: BLE001  unreadable store: NOT audited
        return (_block("triggers", "*", f"trigger store {TRIGGER_PATH} unreadable: {exc}"),)
    if stored:
        return (
            _block(
                "triggers",
                "*",
                f"{len(stored)} stored trigger(s) in {TRIGGER_PATH}: "
                + ", ".join(r.id for r in stored)
                + " — cancel them in the Bots tab first",
            ),
        )
    return (AuditRow(source="triggers", network="*", clear=True, detail="none stored"),)


def audit(cfg: StruckConfig) -> AuditReport:
    """The LIVE read-only asset audit. Rows stay in order: triggers, Strike,
    chain wallets. clear=True ONLY when every row is clear."""
    http = RecordedHttp(cfg.timeout_s)
    try:
        strike = _strike_rows(http, cfg)
        chain = _chain_rows(http, cfg)
    finally:
        http.close()
    rows = (*_trigger_rows(), *strike, *chain)
    return AuditReport(rows=rows, clear=all(r.clear for r in rows))


def report_text(report: AuditReport) -> str:
    """The human-readable audit sheet (loud, by name; never a bare boolean)."""
    lines = [f"[{'CLEAR' if r.clear else 'HOLDS ASSETS'}] {r.source} ({r.network}): {r.detail}" for r in report.rows]
    verdict = (
        "SAFE TO LOG OUT: every audited source is empty. Type LOG OUT to delete the "
        "credential surface (secret store, Strike env, registration marker, LLM config, "
        "empty trigger store). network.json and config/struck.json (endpoints) stay."
        if report.clear
        else "RESET REFUSED — ASSETS/STATE PRESENT. Nothing was deleted. cancel/act on the rows above (networks may need re-pinning), then audit again."
    )
    return "\n".join([*lines, verdict])


def reset(receipt_root: Path = Path("receipts")) -> Path:
    """Delete Struck's credential surface. RE-AUDITS LIVE and refuses unless
    THAT audit is clear (fail-closed: funds could arrive between the shown
    sheet and the keystroke). ONLY callable after BOTH confirmations around it
    (the audit sheet + the exact LOG OUT token typed by the human). Receipt at
    ``<receipt_root>/reset-<ts>/`` records exactly what was removed."""
    report = audit(load_config())
    if not report.clear:
        raise ResetRefused("RESET REFUSED: the (re-)audit holds assets/state; NOTHING was deleted.\n" + report_text(report))
    stamp = _dt.datetime.now(_dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    receipt = receipt_root / f"reset-{stamp}"
    receipt.mkdir(parents=True, exist_ok=True)
    (receipt / "audit.json").write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    try:
        secrets.lock()
    except secrets.SecretError as exc:  # locked already fine; failure is loud, listed
        (receipt / "lock.warning.txt").write_text(f"{exc}\n", encoding="utf-8")
    from .ui.registration import REGISTRATION_PATH  # lazy: no import cycle

    paths: list[Path] = [
        secrets.SECRETS_PATH,
        STRIKE_ENV_PATH,
        REGISTRATION_PATH,
        LLM_CONFIG_PATH,
    ]
    if not TriggerStore(TRIGGER_PATH).list():
        paths.append(TRIGGER_PATH)
    removed: list[str] = []
    for path in paths:
        if path.exists():
            path.unlink()
            removed.append(str(path))
    (receipt / "removed.txt").write_text("\n".join(removed) + "\n", encoding="utf-8")
    (receipt / "kept.txt").write_text(
        "\n".join(("network.json (Settings can change it)", "config/struck.json (endpoints)")) + "\n",
        encoding="utf-8",
    )
    return receipt


def cli() -> int:
    """``python -m struck.reset``: audit sheet at a real terminal, then the
    exact LOG OUT token ({"LOG OUT"}; one wrong or different word aborts)."""
    from .tty import read_human_line, require_terminal

    cfg = load_config()
    print(f"NETWORK: {cfg.network} (guarded account reset; read-only audit first)")
    if not secrets.is_unlocked():
        require_terminal()
        secrets.unlock_from_terminal()  # the audit reads wallets/balances only
    report = audit(cfg)
    print(report.model_dump_json())
    print(report_text(report))
    if not report.clear:
        return 1
    require_terminal()  # the token is typed at a real terminal only
    answer = read_human_line('type exactly "LOG OUT" to delete the credential surface: ')
    if answer.strip() != "LOG OUT":
        print("ABORTED: the confirmation token was not exactly LOG OUT; nothing was deleted.")
        return 1
    receipt = reset()
    print(f"logged out. receipts: {receipt}. Restart to register again.")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
