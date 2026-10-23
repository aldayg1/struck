"""Trigger evaluation, store and fire (port of 
``crypto/bots/triggers.py`` ``TriggerStore`` :569-700, ``evaluate``
:1021-1361, ``_cooldown_active`` :1606-1618 and ``trigger_engine.py``
``_tick`` :74-85; 
and f02f70de…6ede6).

Laws kept from the 
evaluated never fires); an ``incoming_payment`` arms its watermark at the
newest payment already on chain and then fires once per NEW payment.

Changes (spec L1/§5, rules 15/16):
* A fire NEVER executes anything. It writes one ``PlanProposal``,
  ``StrikePlanProposal``, or ``DepositPlanProposal`` per action into
  ``receipts/proposals/`` with status
  ``awaiting_approval``; each goes through the normal pipeline (N2 -> HUMAN
  N3 -> ...). The 
* Optional D5 policy: when the record names an approved policy, each
  proposal is checked in Rust (``struck_core::policy``) and the verdict is
  recorded as the approval route. In this build the route is RECORDED only:
  the milestone-1 pipelines still stop at their approval interrupt.
* Thresholds on served decimals compare exactly in Rust
  (``struck_py.decimal_compare``). Lovelace and token quantities are summed
  in Rust (``struck_py.sum_quantities``) and compared there
  (``struck_py.compare_quantities``). Python only lists the served ints.
* A corrupt store raises (the ]``); there is no
  active-trigger cap and no tmux daemon (``tick`` is a CLI step).
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol

import struck_py
from pydantic import BaseModel, ConfigDict, StrictStr, TypeAdapter

from ..config import REPO_ROOT
from ..plan.models import OnchainSendLeg, PlanProposal, SendOutput
from ..strike.account import Balance
from ..strike.deposit import DepositPlanProposal
from ..strike.history import FillRow, PositionLife, TransactionRow, completed_deposit
from ..strike.market import StrikeMarketClient
from ..strike.models import StrikePlanProposal
from ..strike.stream_models import AccountUpdate, deposit_credit
from .kupo_watch import KupoWatch
from .models import (
    Action,
    CompositeCondition,
    Condition,
    FireContext,
    FireRecord,
    IncomingPaymentCondition,
    IndicatorCondition,
    IntervalCondition,
    NotCondition,
    PriceCondition,
    ScheduleCondition,
    SendTo,
    SequenceCondition,
    StrikeBalanceCondition,
    StrikeDepositAction,
    StrikeDepositCondition,
    StrikeOrderAction,
    StrikePositionCondition,
    StrikeRealizedPnlCondition,
    TimeWindowCondition,
    TriggerRecord,
    TriggerSpec,
    WalletBalanceCondition,
    WalletSendAction,
)
from .policy import ApprovedPolicies

STORE_PATH = REPO_ROOT / "data" / "triggers.json"
PROPOSALS_DIR = REPO_ROOT / "receipts" / "proposals"
_RECORDS = TypeAdapter(tuple[TriggerRecord, ...])


class _Keep:
    """Sentinel: this ``_record`` argument keeps the stored value."""


_KEEP = _Keep()


class TriggerError(RuntimeError):
    pass


class FireStatus(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    trigger: StrictStr
    status: Literal["awaiting_approval"]
    fire_context: FireContext | None
    approval_routes: tuple[StrictStr, ...]
    next: StrictStr


def _record(
    r: TriggerRecord,
    *,
    spec: TriggerSpec | None = None,
    status: Literal["active", "paused", "fired"] | None = None,
    updated_at: str | None = None,
    last_evaluated: str | None = None,
    last_result: str | None = None,
    last_payment_slot: int | None = None,
    sequence_step: int | _Keep = _KEEP,
    sequence_step_since: str | None | _Keep = _KEEP,
    fire_context: FireContext | None | _Keep = _KEEP,
    fire_history: tuple[FireRecord, ...] | None = None,
) -> TriggerRecord:
    """A changed copy, re-validated (no unvalidated ``model_copy``)."""
    step = r.sequence_step if isinstance(sequence_step, _Keep) else sequence_step
    since = r.sequence_step_since if isinstance(sequence_step_since, _Keep) else sequence_step_since
    context = r.fire_context if isinstance(fire_context, _Keep) else fire_context
    return TriggerRecord(
        id=r.id,
        spec=spec if spec is not None else r.spec,
        status=status if status is not None else r.status,
        created_at=r.created_at,
        updated_at=updated_at if updated_at is not None else r.updated_at,
        last_evaluated=last_evaluated if last_evaluated is not None else r.last_evaluated,
        last_result=last_result if last_result is not None else r.last_result,
        last_payment_slot=last_payment_slot
        if last_payment_slot is not None
        else r.last_payment_slot,
        sequence_step=step,
        sequence_step_since=since,
        fire_context=context,
        fire_history=fire_history if fire_history is not None else r.fire_history,
    )


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


# ── indicator math (trigger-condition scope: analysis floats, never money) ──


def _ema_series(values: list[float], period: int) -> list[float]:
    if len(values) < period:
        return []
    ema: list[float] = []
    k = 2.0 / (period + 1.0)
    seed = sum(values[:period]) / period
    ema.append(seed)
    for value in values[period:]:
        ema.append(value * k + ema[-1] * (1.0 - k))
    return ema


def _rsi(closes: list[float], period: int) -> float | None:
    if len(closes) < period + 1:
        return None
    gains = 0.0
    losses = 0.0
    for prev, cur in zip(closes[-(period + 1) : -1], closes[-period:], strict=True):
        delta = cur - prev
        if delta >= 0:
            gains += delta
        else:
            losses -= delta
    if losses == 0:
        return 100.0
    rs = (gains / period) / (losses / period)
    return 100.0 - (100.0 / (1.0 + rs))


def _pct_change(closes: list[float], bars: int) -> float | None:
    if len(closes) <= bars or closes[-bars - 1] == 0:
        return None
    return (closes[-1] / closes[-bars - 1] - 1.0) * 100.0


def _realized_volatility_pct(closes: list[float], bars: int) -> float | None:
    if len(closes) < bars + 1:
        return None
    returns = [
        (cur / prev - 1.0) * 100.0
        for prev, cur in zip(closes[-(bars + 1) : -1], closes[-bars:], strict=True)
        if prev != 0
    ]
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / len(returns)
    return math.sqrt(variance)


# ).
def _float_op(left: float, op: str, right: float) -> bool:
    match op:
        case "gt":
            return left > right
        case "lt":
            return left < right
        case "gte":
            return left >= right
        case "lte":
            return left <= right
        case _:
            raise TriggerError(f"unknown op {op!r}: expected gt, lt, gte or lte")


class StrikeAccount(Protocol):
    """Served ``/v2/positions`` (API wallet). No implementation exists in this
    build: the API wallet is spec B0 (operator-set)."""

    def position_field(self, symbol: str, field: str) -> str:  # the /v2/positions field, as served
        ...


@dataclass(frozen=True)
class Verdict:
    fires: bool
    data_ok: bool
    detail: str


class TriggerStore:
    def __init__(self, path: Path = STORE_PATH) -> None:
        self.path = path

    def list(self) -> tuple[TriggerRecord, ...]:
        if not self.path.exists():
            return ()
        return _RECORDS.validate_json(self.path.read_bytes())

    def _write(self, rows: tuple[TriggerRecord, ...]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_bytes(_RECORDS.dump_json(rows, indent=2, by_alias=True))
        tmp.replace(self.path)

    def get(self, trigger_id: str) -> TriggerRecord:
        for r in self.list():
            if r.id == trigger_id:
                return r
        raise TriggerError(f"no trigger {trigger_id}")

    def add(self, spec: TriggerSpec, now: datetime) -> TriggerRecord:
        stamp = iso(now)
        record = TriggerRecord(
            id=f"trg-{time.time_ns():x}",
            spec=spec,
            status="active",
            created_at=stamp,
            updated_at=stamp,
            last_evaluated=None,
            last_result=None,
            last_payment_slot=None,
            sequence_step=0,
            sequence_step_since=stamp,
            fire_context=None,
            fire_history=(),
        )
        self._write((*self.list(), record))
        return record

    def update(self, trigger_id: str, spec: TriggerSpec, now: datetime) -> TriggerRecord:
        old = self.get(trigger_id)
        record = _record(old, spec=spec, updated_at=iso(now))
        self.replace(record)
        return record

    def replace(self, record: TriggerRecord) -> None:
        rows = self.list()
        if not any(r.id == record.id for r in rows):
            raise TriggerError(f"no trigger {record.id}")
        self._write(tuple(record if r.id == record.id else r for r in rows))

    def remove(self, trigger_id: str) -> None:
        rows = self.list()
        kept = tuple(r for r in rows if r.id != trigger_id)
        if len(kept) == len(rows):
            raise TriggerError(f"no trigger {trigger_id}")
        self._write(kept)

    def set_status(
        self, trigger_id: str, status: Literal["active", "paused"], now: datetime
    ) -> TriggerRecord:
        record = _record(self.get(trigger_id), status=status, updated_at=iso(now))
        self.replace(record)
        return record


class Evaluator:
    def __init__(
        self,
        kupo: KupoWatch,
        market: StrikeMarketClient,
        account: StrikeAccount | None,
        deposits: tuple[TransactionRow, ...] | None = None,
        account_updates: tuple[AccountUpdate, ...] | None = None,
        balances: tuple[Balance, ...] | None = None,
        fills: tuple[FillRow, ...] | None = None,
        position_lives: tuple[PositionLife, ...] | None = None,
    ) -> None:
        self._kupo = kupo
        self._market = market
        self._account = account
        self._deposits = deposits
        self._account_updates = account_updates
        self._balances = balances
        self._fills = fills
        self._position_lives = position_lives
        self.context: FireContext | None = None
        self.armed_slot: int | None = None

    def evaluate(self, cond: Condition, record: TriggerRecord, now: datetime) -> Verdict:
        if isinstance(cond, PriceCondition):
            return self._price(cond)
        if isinstance(cond, TimeWindowCondition):
            start, end = (int(x[:2]) * 60 + int(x[3:]) for x in (cond.start_utc, cond.end_utc))
            minute = now.hour * 60 + now.minute
            inside = start <= minute <= end if start <= end else (minute >= start or minute <= end)
            fires = inside and now.weekday() in cond.weekdays
            return Verdict(fires, True, f"utc {now:%H:%M} weekday {now.weekday()} -> {fires}")
        if isinstance(cond, ScheduleCondition):
            at = parse_iso(cond.at_utc)
            return Verdict(now >= at, True, f"now >= {cond.at_utc} -> {now >= at}")
        if isinstance(cond, IntervalCondition):
            if cond.start_utc is not None and now < parse_iso(cond.start_utc):
                return Verdict(False, True, f"before start_utc {cond.start_utc}")
            fired = [f for f in record.fire_history if f.outcome == "proposed"]
            if not fired:
                return Verdict(
                    True, True, f"interval due (first fire, every {cond.every_minutes} min)"
                )
            due = now - parse_iso(fired[-1].ts) >= timedelta(minutes=cond.every_minutes)
            return Verdict(
                due, True, f"every {cond.every_minutes} min since {fired[-1].ts} -> {due}"
            )
        if isinstance(cond, IncomingPaymentCondition):
            return self._incoming(cond, record)
        if isinstance(cond, WalletBalanceCondition):
            return self._wallet_balance(cond)
        if isinstance(cond, StrikeDepositCondition):
            return self._strike_deposit(cond)
        if isinstance(cond, SequenceCondition):
            raise TriggerError("sequence is the trigger condition, not a nested child")
        if isinstance(cond, StrikePositionCondition):
            return self._strike_position(cond)
        if isinstance(cond, IndicatorCondition):
            return self._indicator(cond)
        if isinstance(cond, StrikeBalanceCondition):
            return self._strike_balance(cond)
        if isinstance(cond, StrikeRealizedPnlCondition):
            return self._strike_realized_pnl(cond, now)
        if isinstance(cond, CompositeCondition):
            results = [self.evaluate(c, record, now) for c in cond.children]
            if cond.logic == "and":
                fires, ok = all(r.fires for r in results), all(r.data_ok for r in results)
            elif cond.logic == "xor":
                fires, ok = sum(r.fires for r in results) == 1, all(r.data_ok for r in results)
            else:
                fires, ok = any(r.fires for r in results), any(r.data_ok for r in results)
            return Verdict(
                fires and ok,
                ok,
                f"{cond.logic.upper()}[" + "; ".join(r.detail for r in results) + "]",
            )
        if isinstance(cond, NotCondition):
            child = self.evaluate(cond.child, record, now)
            return Verdict(
                (not child.fires) and child.data_ok, child.data_ok, f"NOT[{child.detail}]"
            )
        raise TriggerError(f"unhandled condition {cond!r}")

    def _price(self, cond: PriceCondition) -> Verdict:
        market = self._market.markets().value.markets.get(cond.symbol)
        if market is None:
            return Verdict(
                False, False, f"{cond.symbol} not listed on {self._market.network.strike_label}"
            )
        value = {"mark": market.mark_price, "index": market.index_price, "last": market.last_price}[
            cond.price_type
        ]
        if value is None:
            # A served shape without this perp price is not a guessable number.
            return Verdict(
                False,
                False,
                f"{cond.symbol} {cond.price_type} is not served by /v2/markets for this market shape",
            )
        fires = struck_py.decimal_compare(value, cond.op, cond.threshold)
        return Verdict(
            fires,
            True,
            f"strike {cond.symbol} {cond.price_type}={value} {cond.op} {cond.threshold} -> {fires}",
        )

    def _incoming(self, cond: IncomingPaymentCondition, record: TriggerRecord) -> Verdict:
        if record.last_payment_slot is None:
            existing = self._kupo.incoming_payments(cond.address, 0)
            self.armed_slot = max((p.slot for p in existing), default=0)
            return Verdict(
                False,
                True,
                f"armed at slot {self.armed_slot}: watching NEW payments to {cond.address}",
            )
        for p in self._kupo.incoming_payments(cond.address, record.last_payment_slot):
            if struck_py.compare_quantities(str(p.lovelace), "lt", str(cond.min_lovelace)):
                continue
            qty = 0
            if cond.token is not None:
                qty = p.assets.get(cond.token, 0)
            elif cond.policy is not None:
                quantities = [
                    q for unit, q in p.assets.items() if unit.startswith(cond.policy + ".")
                ]
                qty = struck_py.sum_quantities(json.dumps(quantities, separators=(",", ":")))
            if cond.min_token_qty is not None and struck_py.compare_quantities(
                str(qty), "lt", str(cond.min_token_qty)
            ):
                continue
            if cond.sender is not None and not self._kupo.payment_from_payer(
                cond.sender, p.slot, p.tx
            ):
                continue
            self.context = FireContext(
                address=cond.address,
                tx=p.tx,
                slot=p.slot,
                sender=cond.sender,
                lovelace=p.lovelace,
                asset=cond.token or cond.policy or "ada",
                qty=qty,
            )
            self.armed_slot = p.slot
            return Verdict(
                True,
                True,
                f"incoming {p.lovelace} lovelace to {cond.address} (tx {p.tx}, slot {p.slot})",
            )
        return Verdict(
            False, True, f"no new matching payment after slot {record.last_payment_slot}"
        )

    def _wallet_balance(self, cond: WalletBalanceCondition) -> Verdict:
        amounts: list[int] = []
        for address in cond.addresses:
            rows = [r for r in self._kupo.matches(address, "unspent") if r.spent_at is None]
            if cond.symbol == "ada":
                coins = [r.value.coins for r in rows]
                amounts.append(struck_py.sum_quantities(json.dumps(coins, separators=(",", ":"))))
            else:
                held = [r.value.assets.get(cond.symbol, 0) for r in rows]
                amounts.append(struck_py.sum_quantities(json.dumps(held, separators=(",", ":"))))
        values = (
            [struck_py.sum_quantities(json.dumps(amounts, separators=(",", ":")))]
            if cond.aggregate == "sum"
            else amounts
        )
        fires = all(
            struck_py.compare_quantities(str(v), cond.op, str(cond.threshold)) for v in values
        )
        return Verdict(
            fires,
            True,
            f"wallet {cond.symbol} {cond.aggregate}={values} {cond.op} {cond.threshold} -> {fires}",
        )

    def _strike_deposit(self, cond: StrikeDepositCondition) -> Verdict:
        """Landed only when history says ``completed``. A stream ``DEPOSIT_PENDING`` does not."""
        if self._deposits is None:
            return Verdict(
                False,
                False,
                "strike_deposit needs GET /v2/history/transaction type=deposit status=completed; "
                "API wallet not read",
            )
        pending = 0
        credits = 0
        if self._account_updates is not None:
            for update in self._account_updates:
                if update.data.e == "DEPOSIT_PENDING":
                    pending += 1
                elif deposit_credit(update):
                    credits += 1
        matched = [
            row
            for row in self._deposits
            if completed_deposit(row, cond.asset, cond.min_amount)
        ]
        if not matched:
            return Verdict(
                False,
                True,
                f"no completed {cond.asset} deposit >= {cond.min_amount} "
                f"({credits} stream credit(s), {pending} pending)",
            )
        row = matched[0]
        return Verdict(
            True,
            True,
            f"completed deposit {row.amount} {cond.asset} (history id {row.id}, "
            f"{credits} stream credit(s))",
        )

    def _strike_position(self, cond: StrikePositionCondition) -> Verdict:
        if self._account is None:
            return Verdict(
                False,
                False,
                "strike_position needs /v2/positions: Strike API wallet not registered (B0)",
            )
        value = self._account.position_field(cond.symbol, cond.field)
        fires = struck_py.decimal_compare(value, cond.op, cond.threshold)
        return Verdict(
            fires,
            True,
            f"strike {cond.symbol} {cond.field}={value} {cond.op} {cond.threshold} -> {fires}",
        )

    def _indicator(self, cond: IndicatorCondition) -> Verdict:
        """Served klines -> one analysis float -> compare (spec: indicator
        math is analysis, not money; the comparison feeds a proposal)."""
        try:
            read = self._market.klines(cond.symbol, cond.interval, cond.limit)
        except (ValueError, OSError) as exc:
            return Verdict(False, False, f"klines for {cond.symbol} failed: {exc}")
        closes = [float(row[4]) for row in read.value.root]  # served close strings
        if len(closes) < 3:
            return Verdict(
                False,
                False,
                f"klines for {cond.symbol} ({cond.interval}, limit {cond.limit}) "
                f"returned {len(closes)} row(s): too few",
            )
        period = cond.period if cond.period is not None else 14
        fast = cond.fast if cond.fast is not None else 9
        slow = cond.slow if cond.slow is not None else 21
        bars = cond.bars if cond.bars is not None else 24
        value: float | None
        if cond.indicator == "rsi":
            value = _rsi(closes, period)
        elif cond.indicator == "ema_cross":
            fast_series = _ema_series(closes, fast)
            slow_series = _ema_series(closes, slow)
            # +1 when the fast EMA is above the slow one; compare against a
            # signed threshold (gt 0 = golden state), 
            value = (
                1.0
                if fast_series and slow_series and fast_series[-1] > slow_series[-1]
                else -1.0
            )
            if not fast_series or not slow_series:
                value = None
        elif cond.indicator == "momentum":
            value = _pct_change(closes, bars)
        else:
            value = _realized_volatility_pct(closes, bars)
        if value is None or not math.isfinite(value):
            return Verdict(
                False,
                False,
                f"indicator {cond.indicator} for {cond.symbol} needs more data "
                f"({len(closes)} closes)",
            )
        fires = _float_op(value, cond.op, cond.threshold)
        return Verdict(
            fires,
            True,
            f"indicator {cond.indicator}({cond.symbol}, {cond.interval})={value:.6f} "
            f"{cond.op} {cond.threshold} -> {fires}",
        )

    def _strike_balance(self, cond: StrikeBalanceCondition) -> Verdict:
        if self._balances is None:
            return Verdict(
                False,
                False,
                "strike_balance needs GET /v2/balances: the API wallet read was not made",
            )
        rows = [row for row in self._balances if row.asset == cond.asset]
        if not rows:
            listed = sorted({row.asset for row in self._balances})
            return Verdict(
                False,
                False,
                f"{cond.asset!r} is not in the served /v2/balances rows (served assets: {listed}); "
                "that kind fails closed",
            )
        served_fields = {
            "walletBalance": rows[0].walletBalance,
            "unrealizedPnl": rows[0].unrealizedPnl,
            "marginBalance": rows[0].marginBalance,
            "maintMargin": rows[0].maintMargin,
            "initialMargin": rows[0].initialMargin,
            "positionInitialMargin": rows[0].positionInitialMargin,
            "openOrderInitialMargin": rows[0].openOrderInitialMargin,
            "crossWalletBalance": rows[0].crossWalletBalance,
            "crossUnPnl": rows[0].crossUnPnl,
            "availableBalance": rows[0].availableBalance,
            "maxWithdrawAmount": rows[0].maxWithdrawAmount,
        }
        value = served_fields[cond.field]
        fires = struck_py.decimal_compare(value, cond.op, cond.threshold)
        return Verdict(
            fires,
            True,
            f"strike balance {cond.asset} {cond.field}={value} {cond.op} {cond.threshold} -> {fires}",
        )

    def _strike_realized_pnl(
        self, cond: StrikeRealizedPnlCondition, now: datetime
    ) -> Verdict:
        now_ms = int(now.timestamp() * 1000)
        floor_ms = now_ms - cond.window_minutes * 60_000
        if cond.source == "fills":
            if self._fills is None:
                return Verdict(
                    False,
                    False,
                    "strike_realized_pnl(fills) needs GET /v2/history/fill: the API wallet "
                    "read was not made",
                )
            fill_rows = [
                row
                for row in self._fills
                if floor_ms <= row.timestamp <= now_ms
                and (cond.symbol == "" or row.symbol == cond.symbol)
            ]
            newest_fill: FillRow | None = None
            new_fill_ts = -1
            for f_row in fill_rows:
                if f_row.timestamp > new_fill_ts:
                    newest_fill, new_fill_ts = f_row, f_row.timestamp
            if newest_fill is None:
                return Verdict(
                    False,
                    True,
                    f"no fill realized within {cond.window_minutes} min "
                    f"(served rows checked: {len(self._fills)})",
                )
            value = newest_fill.realized_pnl
            which = f"fill {newest_fill.id} ({newest_fill.symbol}) at {newest_fill.timestamp}"
        else:
            if self._position_lives is None:
                return Verdict(
                    False,
                    False,
                    "strike_realized_pnl(position_history) needs GET /v2/history/position: "
                    "the API wallet read was not made",
                )
            position_rows = [
                row
                for row in self._position_lives
                if cond.symbol == "" or row.symbol == cond.symbol
            ]
            in_window: list[PositionLife] = []
            for p_row in position_rows:
                p_stamp = (
                    p_row.close_timestamp if p_row.close_timestamp is not None else p_row.update_timestamp
                )
                if floor_ms <= p_stamp <= now_ms:
                    in_window.append(p_row)
            if not in_window:
                return Verdict(
                    False,
                    True,
                    f"no position history row updated within {cond.window_minutes} min "
                    f"(rows considered: {len(position_rows)})",
                )
            newest_position: PositionLife | None = None
            position_stamps: dict[str, int] = {}
            for p_row in in_window:
                p_stamp = (
                    p_row.close_timestamp if p_row.close_timestamp is not None else p_row.update_timestamp
                )
                position_stamps[p_row.id] = p_stamp
                if (
                    newest_position is None
                    or p_stamp > position_stamps[newest_position.id]
                ):
                    newest_position = p_row
            if newest_position is None:
                return Verdict(
                    False,
                    True,
                    f"no position history row updated within {cond.window_minutes} min "
                    f"(rows considered: {len(position_rows)})",
                )
            value = newest_position.net_realized_pnl
            which = f"position {newest_position.position_id} ({newest_position.status}) row {newest_position.id}"
        fires = struck_py.decimal_compare(value, cond.op, cond.threshold)
        return Verdict(
            fires,
            True,
            f"strike realized pnl ({cond.source}, {which})={value} "
            f"{cond.op} {cond.threshold} -> {fires}",
        )


def _resolved_lovelace(
    out: SendTo, context: FireContext | None, kupo: KupoWatch | None, from_address: str
) -> int:
    """The integer lovelace a send output proposes. Percent math is Rust.
    ``pct_of_balance`` resolves from the served unspent sum at the sending
    wallet (Kupo); a fractional whole-quantity result is a Rust error."""
    if out.pct_of_incoming is not None:
        if context is None:
            raise TriggerError("pct_of_incoming needs an incoming_payment fire context")
        return struck_py.percent_of_quantity(str(context.lovelace), out.pct_of_incoming)
    if out.pct_of_balance is not None:
        if kupo is None:
            raise TriggerError("pct_of_balance needs the chain index (no Kupo client)")
        unspent = sum(
            r.value.coins for r in kupo.matches(from_address, "unspent") if r.spent_at is None
        )
        if unspent <= 0:
            raise TriggerError(
                f"pct_of_balance has nothing to resolve against: {from_address} is empty"
            )
        return struck_py.percent_of_quantity(str(unspent), out.pct_of_balance)
    if out.lovelace == "incoming":
        if context is None:
            raise TriggerError("'incoming' amount needs an incoming_payment fire context")
        return context.lovelace
    if isinstance(out.lovelace, int):
        return out.lovelace
    raise TriggerError("send output has no amount")


def proposal_for(
    action: Action,
    label: str,
    context: FireContext | None,
    kupo: KupoWatch | None = None,
) -> PlanProposal | StrikePlanProposal | DepositPlanProposal:
    if isinstance(action, WalletSendAction):
        outputs: list[SendOutput] = []
        needs_balance = any(out.pct_of_balance is not None for out in action.outputs)
        if needs_balance and kupo is None:
            raise TriggerError("pct_of_balance needs the chain index (no Kupo client)")
        for out in action.outputs:
            outputs.append(
                SendOutput(
                    address=out.address,
                    lovelace=_resolved_lovelace(out, context, kupo, action.from_address),
                    assets=out.assets,
                )
            )
        return PlanProposal(
            lane="onchain",
            intent=f"trigger {label}: wallet_send",
            legs=(
                OnchainSendLeg(
                    kind="send",
                    from_address=action.from_address,
                    outputs=tuple(outputs),
                    max_fee_lovelace=action.max_fee_lovelace,
                    validity_minutes=action.validity_minutes,
                ),
            ),
            rationale="proposed by a fired trigger; needs a fresh human approval",
        )
    if isinstance(action, StrikeOrderAction):
        return StrikePlanProposal(
            lane="strike",
            intent=f"trigger {label}: strike_order",
            legs=(action.leg,),
            rationale="proposed by a fired trigger; needs a fresh human approval",
        )
    if isinstance(action, StrikeDepositAction):
        if action.pct_of_incoming is not None:
            if context is None:
                raise TriggerError("pct_of_incoming needs an incoming_payment fire context")
            asset_amount = str(
                struck_py.percent_of_quantity(str(context.lovelace), action.pct_of_incoming)
            )
        else:
            asset_amount = action.asset_amount or ""
        return DepositPlanProposal(
            lane="deposit",
            intent=f"trigger {label}: strike_deposit",
            blockchain=action.blockchain,
            asset_symbol=action.asset_symbol,
            asset_amount=asset_amount,
            user_address=action.user_address,
            mode=action.mode,
            rationale="proposed by a fired trigger; quote and the unsigned tx still need a human",
        )
    raise TriggerError(f"unhandled action {action!r}")


class Engine:
    def __init__(
        self,
        store: TriggerStore,
        kupo: KupoWatch,
        market: StrikeMarketClient,
        account: StrikeAccount | None,
        policies: ApprovedPolicies,
        proposals_dir: Path = PROPOSALS_DIR,
        balances: tuple[Balance, ...] | None = None,
        fills: tuple[FillRow, ...] | None = None,
        position_lives: tuple[PositionLife, ...] | None = None,
    ) -> None:
        self._store = store
        self._kupo = kupo
        self._market = market
        self._account = account
        self._policies = policies
        self._proposals_dir = proposals_dir
        self._balances = balances
        self._fills = fills
        self._position_lives = position_lives

    def _cooldown_active(self, record: TriggerRecord, now: datetime) -> bool:
        fired = [f for f in record.fire_history if f.outcome == "proposed"]
        return bool(fired) and now - parse_iso(fired[-1].ts) < timedelta(
            seconds=record.spec.cooldown_s
        )

    def tick(
        self,
        now: datetime,
        deposits: tuple[TransactionRow, ...] | None = None,
        account_updates: tuple[AccountUpdate, ...] | None = None,
    ) -> list[str]:
        outcomes: list[str] = []
        for record in self._store.list():
            if record.status != "active":
                continue
            outcomes.append(self._tick_one(record, now, deposits, account_updates))
        return outcomes

    def _tick_one(
        self,
        record: TriggerRecord,
        now: datetime,
        deposits: tuple[TransactionRow, ...] | None,
        account_updates: tuple[AccountUpdate, ...] | None,
    ) -> str:
        if isinstance(record.spec.condition, SequenceCondition):
            return self._tick_sequence(record, now, deposits, account_updates)
        incoming = _has_incoming(record.spec.condition)
        if not incoming and self._cooldown_active(record, now):
            return f"{record.id}: cooldown"
        ev = Evaluator(
            self._kupo,
            self._market,
            self._account,
            deposits,
            account_updates,
            self._balances,
            self._fills,
            self._position_lives,
        )
        verdict = ev.evaluate(record.spec.condition, record, now)
        branch_actions: tuple[Action, ...] | None = None
        if verdict.fires and verdict.data_ok and record.spec.branches:
            # First-match chain (spec §5.2): the FIRST branch that fires
            # proposes exactly its own actions. Never two branches at once.
            chosen = [
                (b, ev.evaluate(b.condition, record, now))
                for b in record.spec.branches
            ]
            for i, (branch, branch_verdict) in enumerate(chosen):
                if branch_verdict.fires and branch_verdict.data_ok:
                    branch_actions = branch.actions
                    verdict = Verdict(
                        True,
                        True,
                        f"branch [{i}] '{branch.label}': {branch_verdict.detail}",
                    )
                    break
            if branch_actions is None:
                verdict = Verdict(
                    False if not record.spec.else_actions else verdict.fires,
                    verdict.data_ok,
                    "no branch fired "
                    + "; ".join(f"'{b.label}': {v.detail}" for b, v in chosen)
                    or "no branch matched",
                )
                if record.spec.else_actions:
                    branch_actions = record.spec.else_actions
                    verdict = Verdict(True, verdict.data_ok, f"else: {verdict.detail}")
        slot = ev.armed_slot if ev.armed_slot is not None else record.last_payment_slot
        evaluated = _record(
            record, last_evaluated=iso(now), last_result=verdict.detail, last_payment_slot=slot
        )
        if not (verdict.fires and verdict.data_ok):
            self._store.replace(evaluated)
            return f"{record.id}: not fired: {verdict.detail}"
        written, route = self._fire(record, ev.context, now)
        fire = FireRecord(
            ts=iso(now),
            outcome="proposed",
            detail=verdict.detail,
            proposals=written,
            approval_route=route,
        )
        self._store.replace(
            _record(
                evaluated,
                fire_history=(*record.fire_history, fire),
                status="active" if record.spec.rearm else "fired",
            )
        )
        return f"{record.id}: FIRED -> {len(written)} proposal(s) awaiting approval ({route}): {verdict.detail}"

    def _tick_sequence(
        self,
        record: TriggerRecord,
        now: datetime,
        deposits: tuple[TransactionRow, ...] | None,
        account_updates: tuple[AccountUpdate, ...] | None,
    ) -> str:
        cond = record.spec.condition
        if not isinstance(cond, SequenceCondition):
            raise TriggerError("sequence tick on a trigger that is not a sequence")
        if record.sequence_step_since is None:
            self._store.replace(
                _record(
                    record,
                    sequence_step_since=iso(now),
                    last_evaluated=iso(now),
                    last_result=f"sequence step {record.sequence_step} armed",
                )
            )
            return f"{record.id}: sequence step {record.sequence_step} armed"
        if record.sequence_step < 0 or record.sequence_step >= len(cond.steps):
            self._store.replace(_record(record, status="paused", last_result="sequence step out of range"))
            return f"{record.id}: sequence step {record.sequence_step} out of range; paused"
        step = cond.steps[record.sequence_step]
        if record.sequence_step == 0 and self._cooldown_active(record, now):
            if not _has_incoming(step.condition):
                return f"{record.id}: cooldown"
        if now - parse_iso(record.sequence_step_since) >= timedelta(seconds=step.timeout_s):
            detail = f"sequence step {record.sequence_step} timed out after {step.timeout_s}s"
            fire = FireRecord(
                ts=iso(now),
                outcome="error",
                detail=detail,
                proposals=(),
                approval_route="not proposed",
            )
            self._store.replace(
                _record(
                    record,
                    status="paused",
                    last_evaluated=iso(now),
                    last_result=detail,
                    fire_history=(*record.fire_history, fire),
                )
            )
            return f"{record.id}: {detail}; paused; nothing proposed"
        ev = Evaluator(
            self._kupo,
            self._market,
            self._account,
            deposits,
            account_updates,
            self._balances,
            self._fills,
            self._position_lives,
        )
        verdict = ev.evaluate(step.condition, record, now)
        slot = ev.armed_slot if ev.armed_slot is not None else record.last_payment_slot
        context = ev.context if ev.context is not None else record.fire_context
        evaluated = _record(
            record,
            last_evaluated=iso(now),
            last_result=verdict.detail,
            last_payment_slot=slot,
            fire_context=context,
        )
        if not (verdict.fires and verdict.data_ok):
            self._store.replace(evaluated)
            return f"{record.id}: step {record.sequence_step} not fired: {verdict.detail}"
        written, route = self._fire(record, context, now, step.actions)
        fire = FireRecord(
            ts=iso(now),
            outcome="proposed",
            detail=verdict.detail,
            proposals=written,
            approval_route=route,
        )
        nxt = record.sequence_step + 1
        done = nxt >= len(cond.steps)
        if done and record.spec.rearm:
            nxt = 0
            status: Literal["active", "paused", "fired"] = "active"
        elif done:
            status = "fired"
        else:
            status = "active"
        self._store.replace(
            _record(
                evaluated,
                sequence_step=nxt,
                sequence_step_since=iso(now),
                fire_context=context,
                fire_history=(*record.fire_history, fire),
                status=status,
            )
        )
        where = "sequence complete" if status == "fired" else f"next step {nxt}"
        return (
            f"{record.id}: step {record.sequence_step} FIRED -> {len(written)} proposal(s) "
            f"awaiting approval ({route}); {where}: {verdict.detail}"
        )

    def _fire(
        self,
        record: TriggerRecord,
        context: FireContext | None,
        now: datetime,
        actions: tuple[Action, ...] | None = None,
    ) -> tuple[tuple[str, ...], str]:
        chosen = record.spec.actions if actions is None else actions
        out = self._proposals_dir / f"{now:%Y%m%dT%H%M%SZ}-{record.id}"
        out.mkdir(parents=True, exist_ok=False)
        written: list[str] = []
        routes: list[str] = []
        for i, action in enumerate(chosen):
            proposal = proposal_for(action, record.spec.label, context, self._kupo)
            path = out / f"action{i}.{proposal.lane}.proposal.json"
            path.write_text(proposal.model_dump_json(indent=2) + "\n", encoding="utf-8")
            written.append(str(path))
            routes.append(self._policies.route(record.spec.policy_id, proposal, self._market, now))
        status = FireStatus(
            trigger=record.id,
            status="awaiting_approval",
            fire_context=context,
            approval_routes=tuple(routes),
            next=(
                "wallet_send: python -m struck.send_cli; "
                "strike: python -m struck.strike.order_cli; "
                "deposit: python -m struck.strike.deposit_cli. "
                "A HUMAN approves. This fire does not quote, sign, or send."
            ),
        )
        (out / "STATUS.json").write_text(status.model_dump_json(indent=2) + "\n", encoding="utf-8")
        return tuple(written), "; ".join(routes)


def _has_incoming(cond: Condition) -> bool:
    if isinstance(cond, IncomingPaymentCondition):
        return True
    if isinstance(cond, CompositeCondition):
        return any(_has_incoming(c) for c in cond.children)
    if isinstance(cond, NotCondition):
        return _has_incoming(cond.child)
    if isinstance(cond, SequenceCondition):
        return any(_has_incoming(step.condition) for step in cond.steps)
    return False
