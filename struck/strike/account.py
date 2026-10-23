"""Authenticated Strike READS (selected network) (API wallet), parsed AS SERVED.

``GET /v2/account`` and ``GET /v2/positions`` (strike-finance-skills @30ba679
``openapi/user-api.yaml`` ``Position`` / ``PositionsResponse``). Signed per
``struck.strike.auth``; the timestamp comes from the served ``GET /v2/time``
minus 1 s (SKILL.md:209: never in the future). No writes live here.
"""

from __future__ import annotations

import time
from urllib.parse import urlencode

import httpx
from pydantic import BaseModel, ConfigDict, RootModel, StrictInt, StrictStr

from ..callhistory import RecordedHttp, Served
from .auth import ApiWallet, new_nonce, signed_headers
from .market import StrikeMarketClient, source_for


class _Served(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow", frozen=True)


class Position(_Served):
    symbol: StrictStr
    margin_mode: StrictStr
    leverage: StrictInt
    size: StrictStr
    entry_price: StrictStr
    upnl: StrictStr
    maintenance_margin: StrictStr
    bankruptcy_price: StrictStr
    liquidation_price: StrictStr


class PositionsResponse(_Served):
    positions: tuple[Position, ...]


class SubAccount(_Served):
    account_id: StrictStr
    name: StrictStr


class Account(_Served):
    """``GET /v2/account`` as served. Hold fields stay strings.

    ``total_margin`` is margin currently reserved. ``position_initial_margin``
    is initial margin held by positions. ``copy_reserved_balance`` is the
    copy-trading hold. No local sum.
    """

    account_id: StrictStr
    blockchain: StrictStr
    blockchain_address: StrictStr | None
    wallet_balance: StrictStr
    available_balance: StrictStr
    copy_reserved_balance: StrictStr
    copy_deployed_margin: StrictStr | None = None
    copy_profit_share_reserved: StrictStr | None = None
    unrealized_pnl: StrictStr
    margin_balance: StrictStr
    total_margin: StrictStr
    position_initial_margin: StrictStr
    maintenance_margin: StrictStr
    sub_accounts: tuple[SubAccount, ...]


class Balance(_Served):
    """One ``GET /v2/balances`` row. Open-order hold is ``openOrderInitialMargin``."""

    asset: StrictStr
    walletBalance: StrictStr
    unrealizedPnl: StrictStr
    marginBalance: StrictStr
    maintMargin: StrictStr
    initialMargin: StrictStr
    positionInitialMargin: StrictStr
    openOrderInitialMargin: StrictStr
    crossWalletBalance: StrictStr
    crossUnPnl: StrictStr
    availableBalance: StrictStr
    maxWithdrawAmount: StrictStr
    marginAvailable: bool
    updateTime: StrictInt
    lockedRewards: StrictStr
    maxWithdrawAfterRewards: StrictStr
    stakingRewardBalance: StrictStr


class Balances(RootModel[tuple[Balance, ...]]):
    model_config = ConfigDict(strict=True, frozen=True)


def server_offset_ms(market: StrikeMarketClient) -> int:
    """Strike's served clock minus ours (GET /v2/time, as served): every signed
    timestamp (REST headers and the user-stream logon) uses it."""
    served_ms = market.server_time().value.serverTime
    return served_ms - time.time_ns() // 1_000_000


class Portfolio(_Served):
    """The consumed fields of ``GET /v2/portfolio`` (as served): the account's
    effective ``fee_tier`` (the N2/N5s fee rules use it) and the served
    deposit/withdraw minimums (money strings/numbers stay as served)."""

    fee_tier: StrictInt


class StrikeAuthedClient:
    def __init__(self, http: RecordedHttp, base_url: str, wallet: ApiWallet) -> None:
        self._http = http
        market = StrikeMarketClient(http, base_url)  # exact-host allowlist
        self.network = market.network
        self._base = self.network.strike_api_url
        self._wallet = wallet
        self._offset_ms = server_offset_ms(market)

    @property
    def base_url(self) -> str:
        """The exact REST base every signed request goes to (from the network table)."""
        return self._base

    def _timestamp_s(self) -> int:
        return (time.time_ns() // 1_000_000 + self._offset_ms - 1000) // 1000

    def request(self, method: str, path_with_query: str, body: bytes | None = None) -> Served:
        # Only a path on the pinned base: never "//host", a scheme, or a relative path.
        if (
            not path_with_query.startswith("/")
            or path_with_query.startswith("//")
            or "://" in path_with_query
            or "\\" in path_with_query
            or "@" in path_with_query.split("?", 1)[0]
        ):
            raise ValueError(
                f"signed request path {path_with_query!r} refused: must be a path starting "
                "with a single '/' on the pinned Strike host (no scheme, no '//', no host)"
            )
        url = httpx.URL(self._base + path_with_query)
        signed_path = url.raw_path.decode("ascii")  # path + "?" + raw query, exactly as sent
        headers = signed_headers(
            self._wallet, method, signed_path, body, self._timestamp_s(), new_nonce()
        )
        return self._http.request(
            source_for(self.network) + "-auth",
            method,
            str(url),
            json_body=body,
            auth_headers=headers,
        )

    def account(self) -> tuple[Served, Account]:
        served = self.request("GET", "/v2/account")
        return served, Account.model_validate_json(served.content)

    def positions(self) -> tuple[Served, PositionsResponse]:
        served = self.request("GET", "/v2/positions")
        return served, PositionsResponse.model_validate_json(served.content)

    def balances(self) -> tuple[Served, Balances]:
        served = self.request("GET", "/v2/balances")
        return served, Balances.model_validate_json(served.content)

    def portfolio(self) -> tuple[Served, Portfolio]:
        served = self.request("GET", "/v2/portfolio")
        return served, Portfolio.model_validate_json(served.content)

    def order(
        self, symbol: str, *, order_id: str | None = None, client_order_id: str | None = None
    ) -> Served:
        """``GET /v2/order``. One of ``order_id`` or ``client_order_id``."""
        if not symbol or (order_id is None) == (client_order_id is None):
            raise ValueError("GET /v2/order needs symbol and exactly one of order_id, client_order_id")
        query = {"symbol": symbol}
        if order_id is not None:
            query["order_id"] = order_id
        else:
            assert client_order_id is not None
            query["client_order_id"] = client_order_id
        return self.request("GET", "/v2/order?" + urlencode(query))

    def twap_strategy(self, strategy_id: str) -> Served:
        """``GET /v2/algo/twap/{id}``. The id is one path segment, as served."""
        if not strategy_id or any(c in strategy_id for c in "/?#"):
            raise ValueError("GET /v2/algo/twap/{id} needs one path segment")
        return self.request("GET", f"/v2/algo/twap/{strategy_id}")

    def rfq(
        self,
        rfq_id: str,
        *,
        vault_id: str | None = None,
        sub_account_id: str | None = None,
    ) -> Served:
        """``GET /v2/rfq/{id}``. Optional scope is the one used at create."""
        if not rfq_id or any(c in rfq_id for c in "/?#"):
            raise ValueError("GET /v2/rfq/{id} needs one path segment")
        if vault_id is not None and sub_account_id is not None:
            raise ValueError("GET /v2/rfq/{id} takes vault_id or sub_account_id, not both")
        path = f"/v2/rfq/{rfq_id}"
        if vault_id is not None:
            path += "?" + urlencode({"vault_id": vault_id})
        elif sub_account_id is not None:
            path += "?" + urlencode({"sub_account_id": sub_account_id})
        return self.request("GET", path)


class ServedStrikeAccount:
    """``triggers.engine.StrikeAccount`` backed by the served ``/v2/positions``."""

    def __init__(self, client: StrikeAuthedClient) -> None:
        self._client = client

    def position_field(self, symbol: str, field: str) -> str:
        _, served = self._client.positions()
        for p in served.positions:
            if p.symbol == symbol:
                served_fields = {
                    "upnl": p.upnl,
                    "liquidation_price": p.liquidation_price,
                    "bankruptcy_price": p.bankruptcy_price,
                    "maintenance_margin": p.maintenance_margin,
                }
                if field not in served_fields:
                    raise ValueError(f"{field!r} is not a /v2/positions trigger field")
                return served_fields[field]
        raise LookupError(f"no open {symbol} position in /v2/positions")
