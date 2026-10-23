"""User-stream payloads as served (skills/strike-userstream/SKILL.md :92-196).
Only the fields Struck shows; extra fields are kept (extra="allow")."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt, StrictStr


class _Served(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow", frozen=True)


class OrderTrade(_Served):
    s: StrictStr
    c: StrictStr
    S: StrictStr
    o: StrictStr
    q: StrictStr
    p: StrictStr
    x: StrictStr
    X: StrictStr
    i: StrictInt
    cr: StrictStr
    z: StrictStr
    l: StrictStr  # noqa: E741 (served field name)
    L: StrictStr
    n: StrictStr
    t: StrictInt
    m: StrictBool
    rp: StrictStr
    # Trailing stop and trigger fields (userstream ORDER_TRADE_UPDATE). Absent when
    # the event is not that order type.
    f: StrictStr | None = None
    sp: StrictStr | None = None
    wt: StrictStr | None = None
    cp: StrictBool | None = None
    AP: StrictStr | None = None
    CR: StrictStr | None = None
    N: StrictStr | None = None


class OrderTradeUpdate(_Served):
    e: StrictStr
    E: StrictInt
    data: OrderTrade


class Balance(_Served):
    a: StrictStr
    wb: StrictStr


class StreamPosition(_Served):
    s: StrictStr
    pa: StrictStr
    ep: StrictStr
    mt: StrictStr
    leverage: StrictInt
    position_id: StrictStr
    position_seq: StrictInt


class AccountData(_Served):
    e: StrictStr
    B: list[Balance]
    P: list[StreamPosition]
    r: StrictStr  # engine reason (userstream skill: FILL, DEPOSIT, ...)


class AccountUpdate(_Served):
    e: StrictStr
    E: StrictInt
    data: AccountData


def deposit_credit(update: AccountUpdate) -> bool:
    """A deposit that moved the balance (userstream skill: ``data.e`` and ``r`` are ``DEPOSIT``).

    ``DEPOSIT_PENDING`` / ``DEPOSIT_FAILED`` are transfer statuses. They carry
    no balance, so they are not a landed deposit.
    """
    return update.e == "ACCOUNT_UPDATE" and update.data.e == "DEPOSIT" and update.data.r == "DEPOSIT"


# Public price stream payloads (skills/strike-price-feeds/SKILL.md "Payloads").
class DepthUpdate(_Served):
    e: StrictStr
    E: StrictInt
    s: StrictStr
    U: StrictInt
    u: StrictInt
    b: list[list[StrictStr]] | None
    a: list[list[StrictStr]] | None


class PublicTrade(_Served):
    e: StrictStr
    E: StrictInt
    s: StrictStr
    t: StrictInt
    p: StrictStr
    q: StrictStr
    T: StrictInt
    m: StrictBool
    o: StrictInt | None = None


class MarkPriceUpdate(_Served):
    e: StrictStr
    E: StrictInt
    s: StrictStr
    p: StrictStr
    i: StrictStr
    r: StrictStr
    T: StrictInt
    P: StrictStr | None = None


class Candle(_Served):
    t: StrictInt
    T: StrictInt
    s: StrictStr
    i: StrictStr
    o: StrictStr
    c: StrictStr
    h: StrictStr
    l: StrictStr  # noqa: E741 (served field name)
    v: StrictStr
    n: StrictInt
    x: StrictBool


class KlineEvent(_Served):
    e: StrictStr
    E: StrictInt
    s: StrictStr
    k: Candle


class RfqCounts(_Served):
    acked: StrictInt
    quoted: StrictInt
    uninterested: StrictInt


class RfqObject(_Served):
    """Requester RFQ object. ``server_time_ms`` is REST-only and stays an extra field."""

    rfq_id: StrictStr
    client_rfq_id: StrictStr
    symbol: StrictStr
    requested_size: StrictStr
    status: StrictStr
    revision: StrictInt
    total_quoter_count: StrictInt
    response_count: StrictInt
    response_counts: RfqCounts
    created_at_ms: StrictInt
    updated_at_ms: StrictInt
    expires_at_ms: StrictInt
    side: StrictStr | None = None
    closed_at_ms: StrictInt | None = None


class RfqUpdate(_Served):
    """Requester ``rfqUpdate`` on the user stream. ``E`` is ``updated_at_ms``."""

    e: StrictStr
    E: StrictInt
    data: RfqObject


class QuoterRfqBody(_Served):
    symbol: StrictStr
    size: StrictStr
    created_at_ms: StrictInt
    expires_at_ms: StrictInt
    side: StrictStr | None = None


class QuoterRfqEvent(_Served):
    """Quoter feed frame ``type: rfq``. ``rfq`` is present on ``op: set``."""

    type: StrictStr
    op: StrictStr
    rfq_id: StrictStr
    rfq: QuoterRfqBody | None = None


class ProspectiveCohort(_Served):
    side: StrictStr
    size: StrictStr
    bound_price: StrictStr
    working_type: StrictStr | None = None
    trigger_price: StrictStr | None = None


class ProspectiveBody(_Served):
    resolution_bps: StrictInt
    cohorts: list[ProspectiveCohort]


class ProspectiveRfqEvent(_Served):
    """Quoter feed frame ``type: prosp_rfq``. One of the bodies is set on ``op: set``."""

    type: StrictStr
    op: StrictStr
    symbol: StrictStr
    kind: StrictStr
    source_seq: StrictInt
    source_time_ms: StrictInt
    expires_at_ms: StrictInt
    conditional: ProspectiveBody | None = None
    liquidation: ProspectiveBody | None = None


class StrategyData(_Served):
    """``strategyUpdate`` payload (TWAP, bracket, grid). Ids stay the served strings."""

    status: StrictStr
    strategy_id: StrictStr | None = None
    bot_id: StrictStr | None = None
    event_type: StrictStr | None = None
    market: StrictStr | None = None
    symbol: StrictStr | None = None
    side: StrictStr | None = None
    filled_size: StrictStr | None = None
    total_size: StrictStr | None = None
    duration_sec: StrictInt | None = None
    realized_pnl: StrictStr | None = None
    error: StrictStr | None = None


class StrategyUpdate(_Served):
    e: StrictStr
    E: StrictInt
    s: StrictStr | None = None
    data: StrategyData


class MiniTicker(_Served):
    """``!miniticker@arr`` / ``miniticker`` event (strike-market-data)."""

    e: StrictStr
    E: StrictInt
    s: StrictStr
    c: StrictStr
    o: StrictStr
    h: StrictStr
    l: StrictStr  # noqa: E741 (served field name)
    v: StrictStr
    q: StrictStr
