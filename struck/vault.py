"""The native Strike VAULT surface (matrix VAULT01, coordination E04).

Every row is SERVED and passed through verbatim: pinned to
``skills/strike-vaults/SKILL.md`` (the official contract, quoted in each
docstring). Money values stay strings — the Python layer never computes on
them (law 3). The deposit/withdraw share math (minting, leader fee, payout)
is the venue's engine; Struck only validates request shapes and posts the
prepared write, and nothing here is idempotent ("a blind retry deposits or
withdraws twice", SKILL :12) — so a write always needs the human's own
typed approval at a real terminal (struck.vault_cli).

Public routes (SKILL :24-31): ``	list / detail / portfolio / history /
depositors / liquidations / announcements``. Depositor FlexibleAuth reads
(SKILL :262-277) go through a signed client; the selector is resolved
server-side in this order: ``account_id``, ``blockchain`` +
``blockchain_address``, ``blockchain_address``, then the authenticated
caller (SKILL :263-264).

Writes (SKILL :279-303, :333-397): ``POST /v2/vault/deposit``,
``POST /v2/vault/withdraw``, cancel, pause, resume — async: ``200`` only
means the command was queued; the outcome arrives as a ``VAULT_*``
user-stream event (owned by the userstream rail, E02's streams.py). The
sync-vs-async boundary is explicit here: ``prepare_*`` builds and validates
the request and the CLI gates it on the human.
"""

from __future__ import annotations

import json
from typing import Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr

from .callhistory import RecordedHttp, Served
from .network import checked_strike_url

DepositorSelection = Literal["account_id", "vault_id", "blockchain_address", "caller"]


class _Signed(Protocol):
    """The signed-request surface (struck/strike/history.py's protocol shape)."""

    def request(self, method: str, path_with_query: str, body: bytes | None = None) -> Served: ...

VaultStatus = Literal["active", "paused", "closed"]
VaultType = Literal["user", "protocol"]
HistoryType = Literal["deposit", "withdrawal"]
HistoryStatus = Literal["completed", "pending", "cancelled", "failed"]
VaultPeriod = Literal["24h", "7d", "30d", "6m", "1y", "all"]
LiquidationPeriod = Literal["24h", "7d", "30d", "6m", "1y"]


class VaultError(RuntimeError):
    """A served contract violation (never a silent fallback)."""


class _Served(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow", frozen=True)


class VaultSummary(_Served):
    """One row of ``GET /v2/vaults`` (skill :96-118). Money stays served strings."""

    vault_id: StrictStr
    account_id: StrictStr
    name: StrictStr
    total_shares: StrictStr
    share_price: StrictStr
    equity: StrictStr
    wallet_balance: StrictStr
    min_deposit: StrictStr
    lockup_days: StrictInt
    leader_commission_bps: StrictInt
    status: VaultStatus
    type: VaultType


class VaultDepositHistory(_Served):
    """A row of ``GET /v2/vault/:id/history``; a withdrawal row's ``id`` IS the
    ``withdrawal_id`` (skill :157-176) — the cancel route reads it."""

    id: StrictStr
    type: HistoryType
    status: HistoryStatus
    timestamp: StrictInt


class VaultHistoryPage(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    vault_id: StrictStr | None
    rows: list[VaultDepositHistory]
    next_cursor: StrictStr | None
    limit: StrictInt


class VaultListPage(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    vaults: tuple[VaultSummary, ...]
    count: StrictInt
    limit: StrictInt
    offset: StrictInt


class StrikeVaultClient:
    """The vault routes as served, on the network table's pinned Strike host."""

    def __init__(self, http: RecordedHttp, base_url: str) -> None:
        self._http = http
        self._base = checked_strike_url(base_url)

    @property
    def base_url(self) -> str:
        return self._base

    def _get(
        self,
        path: str,
        params: dict[str, str] | None = None,
    ) -> Served:
        return self._http.request(
            source="vault",
            method="GET",
            url=self._base + path,
            params=params,
        )

    # -- public reads ------------------------------------------------------
    def list_vaults(
        self,
        *,
        status: VaultStatus | None = None,
        type_filter: VaultType | None = None,
        period: VaultPeriod | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> tuple[Served, VaultListPage]:
        """``GET /v2/vaults`` (skill :33-118). Filters validated client-side
        exactly per the served error contract."""
        params: dict[str, str] = {}
        if status is not None:
            if status not in ("active", "paused", "closed"):
                raise VaultError("status filter must be active, paused, or closed")
            params["status"] = status
        if type_filter is not None:
            if type_filter not in ("user", "protocol"):
                raise VaultError("type filter must be user or protocol")
            params["type"] = type_filter
        if period is not None:
            if period not in ("24h", "7d", "30d", "6m", "1y", "all"):
                raise VaultError("invalid period filter")
            params["period"] = period
        if limit is not None:
            if limit <= 0:
                params["limit"] = "100"  # skill :45: values <= 0 fall back to 100
            else:
                params["limit"] = "500" if limit > 500 else str(limit)
        if offset is not None:
            params["offset"] = str(offset) if offset >= 0 else "0"
        served = self._get("/v2/vaults", params or None)
        page = VaultListPage.model_validate_json(served.content)
        return served, page

    def detail(self, vault_id: str) -> tuple[Served, _Served]:
        """``GET /v2/vault/:id`` (skill :120-128). Unknown id: ``404 vault not
        found`` — surfaced, never guessed around."""
        if not vault_id or "/" in vault_id or "?" in vault_id or " " in vault_id:
            raise VaultError(f"not a vault id: {vault_id!r}")
        served = self._get(f"/v2/vault/{vault_id}")
        return served, _Served.model_validate_json(served.content)

    def portfolio(self, vault_id: str, *, period: VaultPeriod | None = None) -> tuple[Served, _Served]:
        """``GET /v2/vault/:id/portfolio``; an invalid period is ``404``
        (skill :137) so the client-side refusal keeps the served shape."""
        if period is not None and period not in ("24h", "7d", "30d", "6m", "1y", "all"):
            raise VaultError("invalid portfolio period")
        params = None if period is None else {"period": str(period)}
        served = self._get(f"/v2/vault/{vault_id}/portfolio", params)
        return served, _Served.model_validate_json(served.content)

    def history(
        self,
        vault_id: str,
        *,
        type_filter: HistoryType | None = None,
        status: HistoryStatus | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> tuple[Served, VaultHistoryPage]:
        """``GET /v2/vault/:id/history`` (skill :130-176). Deposits are only
        ever ``completed`` (skill :169)."""
        params: dict[str, str] = {}
        if type_filter is not None:
            if type_filter not in ("deposit", "withdrawal"):
                raise VaultError("invalid type filter, must be deposit or withdrawal")
            params["type"] = type_filter
        if status is not None:
            if status not in ("completed", "pending", "cancelled", "failed"):
                raise VaultError("invalid status filter")
            params["status"] = status
        if limit is not None:
            if not 1 <= limit <= 500:
                raise VaultError("limit must be an integer 1..500")
            params["limit"] = str(limit)
        if cursor is not None:
            params["pagination"] = cursor
        served = self._get(f"/v2/vault/{vault_id}/history", params or None)
        page = VaultHistoryPage.model_validate(
            _vault_history_page_json(served.content, vault_id)
        )
        return served, page

    def depositors(self, vault_id: str, *, limit: int | None = None, cursor: str | None = None) -> Served:
        """``GET /v2/vault/:id/depositors`` (skill :178-188), rows as served."""
        params: dict[str, str] = {}
        if limit is not None:
            if not 1 <= limit <= 500:
                raise VaultError("limit must be an integer 1..500")
            params["limit"] = str(limit)
        if cursor is not None:
            params["pagination"] = cursor
        return self._get(f"/v2/vault/{vault_id}/depositors", params or None)

    def liquidations(
        self,
        vault_id: str,
        *,
        limit: int | None = None,
        cursor: str | None = None,
        start_ms: int | None = None,
        end_ms: int | None = None,
    ) -> Served:
        """``GET /v2/vault/:id/liquidations`` (skill :190-203)."""
        params: dict[str, str] = {}
        if limit is not None:
            if not 1 <= limit <= 100:
                raise VaultError("liquidation limit must be an integer 1..100")
            params["limit"] = str(limit)
        if cursor is not None:
            params["pagination"] = cursor
        if start_ms is not None:
            if start_ms < 0:
                raise VaultError("invalid startTime parameter")
            params["startTime"] = str(start_ms)
        if end_ms is not None:
            if end_ms < 0 or (start_ms is not None and start_ms > end_ms):
                raise VaultError("invalid endTime parameter")
            params["endTime"] = str(end_ms)
        return self._get(f"/v2/vault/{vault_id}/liquidations", params or None)

    def announcements(self, vault_id: str, *, limit: int | None = None, cursor: str | None = None) -> Served:
        """``GET /v2/vault/:id/announcements`` (skill :205-217)."""
        params: dict[str, str] = {}
        if limit is not None:
            if not 1 <= limit <= 100:
                raise VaultError("limit must be an integer 1..100")
            params["limit"] = str(limit)
        if cursor is not None:
            params["pagination"] = cursor
        return self._get(f"/v2/vault/{vault_id}/announcements", params or None)

    # -- depositor FlexibleAuth reads --------------------------------------
    def vault_position(
        self,
        signed: _Signed,
        *,
        vault_id: str,
        account_id: str | None = None,
        blockchain_address: str | None = None,
    ) -> Served:
        """``GET /v2/vault/position`` through a signed client (skill :279-299).
        ``vault_id`` selects the vault; the depositor is `account_id`, then
        `blockchain_address`, then the authenticated caller (skill :263-264)."""
        params = {"vault_id": vault_id}
        if account_id is not None:
            params["account_id"] = account_id
        elif blockchain_address is not None:
            params["blockchain_address"] = blockchain_address
        path = "/v2/vault/position?" + "&".join(
            f"{k}={v}" for k, v in params.items()
        )
        return signed.request("GET", path, None)

    # -- writes (PREPARATION ONLY; execution is the human's gated CLI) -----
    def prepare_deposit(self, vault_id: str, usd_amount: str) -> dict[str, "str | bool"]:
        """The ``POST /v2/vault/deposit`` body (skill :280-283): amounts are
        JSON strings; no arithmetic; an unknown ``vault_id`` still returns 200
        and fails async (skill :289-291) — the caller is told that HERE."""
        validate_vault_id(vault_id)
        validate_positive_string("usd_amount", usd_amount)
        return {"vault_id": vault_id, "usd_amount": usd_amount}

    def prepare_withdraw(self, vault_id: str, *, shares: str | None = None) -> dict[str, "str | bool"]:
        """The ``POST /v2/vault/withdraw`` body (skill :333-341). Partial
        shares: positive, at most 8 decimals; ``withdraw_all`` takes no shares."""
        validate_vault_id(vault_id)
        if shares is None:
            return {"vault_id": vault_id, "withdraw_all": True}
        validate_withdraw_shares(shares)
        return {"vault_id": vault_id, "shares": shares}


def validate_vault_id(vault_id: str) -> None:
    if not vault_id:
        raise VaultError("vault_id is required")
    if any(c in vault_id for c in ' /?#"\\'):
        raise VaultError(f"not a vault id: {vault_id!r}")
    if len(vault_id) != 36 or vault_id.count("-") != 4:
        raise VaultError(
            "vault_id must be the vault UUID (36 chars, 4 dashes); vault_<8> fails the UUID check"
        )


def validate_positive_string(name: str, value: str) -> None:
    """Strict served-shape check WITHOUT arithmetic: characters only."""
    if not value.replace(".", "", 1).isdigit() or (
        value.count(".") > 1 or value.startswith(".") or value.endswith(".")
    ):
        raise VaultError(f"invalid {name}")
    if value in ("", "0", "0.0"):
        raise VaultError(f"{name} must be positive")


def validate_withdraw_shares(shares: str) -> None:
    validate_positive_string("shares", shares)
    if "." in shares and len(shares.split(".")[1]) > 8:
        raise VaultError(
            "partial withdrawal shares must have at most eight decimals; "
            "use withdraw_all for an exact full balance"
        )


def _vault_history_page_json(content: bytes, vault_id: str) -> dict[str, object]:
    """The public history page wraps rows; a depositor-scoped history adds
    ``count`` (skill :270-272). Roofs decoded strictly."""
    raw = cast("object", json.loads(content))
    served = raw
    if not isinstance(served, dict):
        raise VaultError("vault history: not a JSON object")
    rows = served.get("history")
    if not isinstance(rows, list):
        raise VaultError(f"vault {vault_id} history: no history list served")
    return {
        "vault_id": served.get("vault_id", vault_id),
        "rows": rows,
        "next_cursor": served.get("next_cursor"),
        "limit": served.get("limit", 0),
    }
