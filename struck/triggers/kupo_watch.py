"""Incoming-payment facts from the LOCAL Kupo index (port of 
``crypto/bots/triggers.py`` ``_kupo_matches`` :845, ``_incoming_payments``
:875-952, ``_payment_from_payer`` :954-972; 
tree sha256 a4bc1d1f…ad33ea).

Changes from the 
history), matches are parsed by a strict model (a wrong shape raises instead
of being skipped), and nothing is defaulted to 0: a missing field fails
loudly (rules 15/16).
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr

from ..callhistory import RecordedHttp

SOURCE = "kupo"


class _Loose(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow", frozen=True)


class _Point(_Loose):
    slot_no: StrictInt


class _Spent(_Loose):
    slot_no: StrictInt
    transaction_id: StrictStr


class _Value(_Loose):
    coins: StrictInt
    assets: dict[str, StrictInt]


class KupoMatch(_Loose):
    transaction_id: StrictStr
    output_index: StrictInt
    address: StrictStr
    value: _Value
    created_at: _Point
    spent_at: _Spent | None


class _Matches(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True)
    rows: tuple[KupoMatch, ...]


@dataclass(frozen=True)
class IncomingPayment:
    tx: str
    slot: int
    lovelace: int
    assets: dict[str, int]


def _slot_of(p: IncomingPayment) -> int:
    return p.slot


class KupoWatch:
    def __init__(self, http: RecordedHttp, base_url: str) -> None:
        self._http = http
        self._base = base_url.rstrip("/")

    def matches(self, address: str, flag: str | None) -> tuple[KupoMatch, ...]:
        """Raw matches (created AND spent; ``flag`` ``spent`` for spent only)."""
        url = f"{self._base}/matches/{address}" + (f"?{flag}" if flag else "")
        served = self._http.request(SOURCE, "GET", url)
        return _Matches.model_validate_json(b'{"rows":' + served.content + b"}").rows

    def incoming_payments(self, address: str, after_slot: int) -> tuple[IncomingPayment, ...]:
        """EXTERNAL payments created at ``address`` after ``after_slot``, one
        per funding tx. A tx that also spent this address's own UTxOs is
        self/change traffic and is excluded ()."""
        rows = self.matches(address, None)
        own_spent = {r.spent_at.transaction_id for r in rows if r.spent_at is not None}
        by_tx: dict[str, IncomingPayment] = {}
        for r in rows:
            if r.transaction_id in own_spent or r.created_at.slot_no <= after_slot:
                continue
            prev = by_tx.get(r.transaction_id)
            assets = dict(prev.assets) if prev else {}
            for unit, qty in r.value.assets.items():
                assets[unit] = assets.get(unit, 0) + qty
            by_tx[r.transaction_id] = IncomingPayment(
                tx=r.transaction_id,
                slot=r.created_at.slot_no,
                lovelace=(prev.lovelace if prev else 0) + r.value.coins,
                assets=assets,
            )
        return tuple(sorted(by_tx.values(), key=_slot_of))

    def payment_from_payer(self, payer: str, slot: int, tx: str) -> bool:
        """True when ``payer``'s UTxOs were spent by exactly ``tx`` at ``slot``:
        proven on the local index, never a remote API ()."""
        return any(
            r.spent_at is not None
            and r.spent_at.transaction_id == tx
            and r.spent_at.slot_no == slot
            for r in self.matches(payer, "spent")
        )
