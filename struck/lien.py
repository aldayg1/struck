"""The LIEN runtime (coordination I04): flexible collateral-lien contracts
with the money rules in Rust (``struck_py.lien_transition``, rule
``struck-lien-1``) and chain evidence read as SERVED.

Division of labor (the money law):
* Rust decides EVERY value check (coverage, exact amounts, due windows,
  contract paths) — a transition goes through ``struck_py.lien_transition``
  and Python moves only what the Rust verdict says, nothing else.
* Python owns: the durable store (same shape as the TriggerStore: atomic
  file writes, strict ``TypeAdapter`` JSON), strict Pydantic wire models,
  chain evidence reads (Koios tx presence via the production client), and
  the receipts under ``receipts/lien/<id>/``.

Approach to the chain (honest, no invented Plutus work in this slice): the
COLLATERAL lock is a real Cardano payment whose observed tx id is handed to
the contract; the runtime then audits LIVE, by tx id, that the payment still
sits on the selected network. Upgrading the lock to an inline-datum Plutus
validator is the next contract slice (the Aiken CAUCE validators in the
).

Nothing here approves, signs or sends: a lock/repay payment always goes
through the human approval pipeline, and a transition is stored ONLY once
the human hands in an OBSERVED tx id.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr, TypeAdapter
from typing_extensions import TypeAlias

from .config import REPO_ROOT

PHASE: TypeAlias = Literal[
    "open", "secured", "active", "repaid", "foreclosed", "cancelled"
]

STORE_PATH: Final[Path] = REPO_ROOT / "data" / "liens.json"
RECEIPTS_ROOT: Final[Path] = REPO_ROOT / "receipts" / "lien"


class LienTerms(BaseModel):
    """Exact served strings; the Rust contract parses and checks them."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    principal: StrictStr
    fee: StrictStr
    collateral: StrictStr
    min_coverage_bps: StrictInt
    due_ms: StrictInt
    borrower: StrictStr
    lender: StrictStr


class LienState(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    terms: LienTerms
    phase: PHASE
    lock_tx_id: StrictStr | None
    release_tx_id: StrictStr | None
    last_transition_ms: StrictInt


class LienRecord(BaseModel):
    """One persisted lien: the id is `lien-<hex nonce>`, like a trigger id."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    id: StrictStr
    label: StrictStr
    state: LienState


class TransitionRefused(RuntimeError):
    """The Rust contract refused the transition; reasons ride the receipt."""


class ChainEvidenceMissing(RuntimeError):
    """The served chain does not show the evidence the contract needs."""


_RECORDS: TypeAdapter[tuple[LienRecord, ...]] = TypeAdapter(tuple[LienRecord, ...])


def lien_receipt_root(lien_id: str, override: Path | None = None) -> Path:
    return (override or RECEIPTS_ROOT) / lien_id


class LienStore:
    def __init__(self, path: Path = STORE_PATH) -> None:
        self.path = path

    def list(self) -> tuple[LienRecord, ...]:
        if not self.path.exists():
            return ()
        return _RECORDS.validate_json(self.path.read_bytes())

    def _write(self, rows: tuple[LienRecord, ...]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_bytes(_RECORDS.dump_json(rows, indent=2, by_alias=True))
        tmp.replace(self.path)

    def get(self, lien_id: str) -> LienRecord:
        for r in self.list():
            if r.id == lien_id:
                return r
        raise LienError(f"no lien {lien_id}")

    def replace(self, record: LienRecord) -> None:
        rows = self.list()
        if not any(r.id == record.id for r in rows):
            raise LienError(f"no lien {record.id} to replace")
        self._write(tuple(record if r.id == record.id else r for r in rows))

    def create(self, label: str, terms: LienTerms, now_ms: int) -> LienRecord:
        record = LienRecord(
            id=f"lien-{time.time_ns():x}",
            label=label,
            state=LienState(
                terms=terms,
                phase="open",
                lock_tx_id=None,
                release_tx_id=None,
                last_transition_ms=now_ms,
            ),
        )
        self._write((*self.list(), record))
        return record


class LienError(RuntimeError):
    pass


class TransitionRequest(BaseModel):
    """The exact wire the Rust contract reads (strict; nothing inferred)."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    current: LienState
    proposed: PHASE
    now_ms: StrictInt
    lock_amount: StrictStr | None
    repay_amount: StrictStr | None
    tx_id: StrictStr | None


def transition_receipt(
    current: LienState,
    proposed: PHASE,
    *,
    now_ms: int,
    lock_amount: str | None = None,
    repay_amount: str | None = None,
    tx_id: str | None = None,
    receipt_dir: Path | None = None,
) -> LienState:
    """Run the Rust contract ONCE and persist its verdict. The store is
    updated ONLY on `ok`; refusals raise `TransitionRefused` with the full
    reasons and both verdicts are written as receipts."""
    import struck_py

    request = TransitionRequest(
        current=current,
        proposed=proposed,
        now_ms=now_ms,
        lock_amount=lock_amount,
        repay_amount=repay_amount,
        tx_id=tx_id,
    )
    raw = struck_py.lien_transition(request.model_dump_json())
    verdict: object = json.loads(raw)
    if not isinstance(verdict, dict) or not isinstance(verdict.get("ok"), bool):
        raise TransitionRefused(
            f"struck-lien-1 verdict is not the strict receipt shape: {raw[:200]!r}"
        )
    if receipt_dir is None:
        from .config import RECEIPTS_DIR as receipts

        receipt_dir = Path(receipts) / "lien"
    receipt_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now_ms // 1000)).replace(
        "Z", f"{now_ms % 1000:03d}Z"
    )
    path = receipt_dir / f"transition-{stamp}-{proposed}.json"
    for ordinal in range(2, 100):  # distinct verdicts NEVER overwrite each other
        if not path.exists():
            break
        path = receipt_dir / f"transition-{stamp}-{proposed}-{ordinal}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(
        (request.model_dump_json() + "\n-- verdict --\n" + raw + "\n").encode()
    )
    tmp.replace(path)
    if not verdict["ok"]:
        raw_reasons = verdict.get("reasons")
        reasons = (
            "; ".join(reason for reason in raw_reasons if isinstance(reason, str))
            if isinstance(raw_reasons, list)
            else "the refusal carried no reason list"
        )
        raise TransitionRefused(
            f"struck-lien-1 refused {current.phase} -> {proposed}: {reasons} "
            f"(verdict receipt: {path})"
        )
    state = verdict.get("state")
    if not isinstance(state, dict):
        raise TransitionRefused("struck-lien-1 ok verdict carries no state object")
    return LienState.model_validate(state)


def observe_lock(
    store: LienStore,
    lien_id: str,
    *,
    tx_id: str,
    served_amount: str,
    now_ms: int,
    receipt_dir: Path | None = None,
) -> LienRecord:
    """Record an OBSERVED collateral lock (the human supplied the real tx id
    and the served amount — agents never submit)."""
    record = store.get(lien_id)
    if record.state.phase != "open":
        raise LienError(f"lien {lien_id} is {record.state.phase}, not open: the lock comes first")
    new_state = transition_receipt(
        record.state,
        "secured",
        now_ms=now_ms,
        lock_amount=served_amount,
        tx_id=tx_id,
        receipt_dir=receipt_dir,
    )
    updated = LienRecord(id=record.id, label=record.label, state=new_state)
    store.replace(updated)
    return updated


def observe_activate(
    store: LienStore,
    lien_id: str,
    *,
    now_ms: int,
    receipt_dir: Path | None = None,
) -> LienRecord:
    """Record that a secured lien was activated (lender disbursed principal)
    inside the window. The Rust contract refuses on/after due_ms."""
    record = store.get(lien_id)
    if record.state.phase != "secured":
        raise LienError(f"lien {lien_id} is {record.state.phase}, not secured: activation needs the lock first")
    new_state = transition_receipt(
        record.state,
        "active",
        now_ms=now_ms,
        receipt_dir=receipt_dir,
    )
    updated = LienRecord(id=record.id, label=record.label, state=new_state)
    store.replace(updated)
    return updated


def observe_foreclose(
    store: LienStore,
    lien_id: str,
    *,
    now_ms: int,
    receipt_dir: Path | None = None,
) -> LienRecord:
    """Record foreclosure after the window closed without full repayment."""
    record = store.get(lien_id)
    if record.state.phase not in ("secured", "active"):
        raise LienError(f"lien {lien_id} is {record.state.phase}: only secured/active liens can foreclose")
    new_state = transition_receipt(
        record.state,
        "foreclosed",
        now_ms=now_ms,
        tx_id=None,
        receipt_dir=receipt_dir,
    )
    updated = LienRecord(id=record.id, label=record.label, state=new_state)
    store.replace(updated)
    return updated


def observe_repay(
    store: LienStore,
    lien_id: str,
    *,
    served_delivered: str,
    tx_id: str,
    now_ms: int,
    receipt_dir: Path | None = None,
) -> LienRecord:
    """Record an OBSERVED repayment; the Rust contract checks it against
    principal + fee EXACTLY — partial repayments never release."""
    record = store.get(lien_id)
    if record.state.phase not in ("secured", "active"):
        raise LienError(
            f"lien {lien_id} is {record.state.phase}: only a secured/active lien can repay"
        )
    new_state = transition_receipt(
        record.state,
        "repaid",
        now_ms=now_ms,
        repay_amount=served_delivered,
        tx_id=tx_id,
        receipt_dir=receipt_dir,
    )
    updated = LienRecord(id=record.id, label=record.label, state=new_state)
    store.replace(updated)
    return updated


def audit(store: LienStore, lien_id: str) -> str:
    """LIVE audit of one lien: the contract REQUIRES the recorded lock tx to
    still be visible on the selected network. Fail-closed: an unreadable or
    unfound evidence source refuses the audit by name (never a guess)."""
    from .callhistory import RecordedHttp
    from .chain.koios import KoiosClient
    from .config import load_config

    record = store.get(lien_id)
    state = record.state
    if state.phase != "secured":
        raise ChainEvidenceMissing(
            f"lien {lien_id} is {state.phase}: the live collateral audit needs the secured phase"
        )
    assert state.lock_tx_id is not None
    cfg = load_config()
    http = RecordedHttp(cfg.timeout_s)
    try:
        confirmed, _served = KoiosClient(http, cfg.koios_url).get_tx_info(state.lock_tx_id)
    finally:
        http.close()
    if not confirmed:
        raise ChainEvidenceMissing(
            f"lien {lien_id} audit FAILED: lock tx {state.lock_tx_id} is not confirmed on "
            f"{cfg.network} (not yet in a block or dropped — DO NOT assume the collateral moved)"
        )
    return (
        f"lien {lien_id} [{record.label}]: phase {state.phase}, principal {state.terms.principal}"
        f" + fee {state.terms.fee}, collateral {state.terms.collateral}; lock tx "
        f"{state.lock_tx_id} CONFIRMED on {cfg.network}"
    )


def summary(store: LienStore) -> str:
    lines: list[str] = []
    rows = store.list()
    if not rows:
        return "no liens stored"
    for r in rows:
        t = r.state.terms
        lines.append(
            f"{r.id} [{r.state.phase}] {r.label}: principal {t.principal} + fee {t.fee} "
            f"| collateral {t.collateral} (min {t.min_coverage_bps} bps) | due {t.due_ms} "
            f"| lock {r.state.lock_tx_id or '-'}"
        )
    return "\n".join(lines)
