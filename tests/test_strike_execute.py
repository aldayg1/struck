"""N8s execute against a MOCK transport only (never the live API). The
approvals below are given programmatically INSIDE this test, to a mock;
the CLI only ever takes them from a human on stdin. Dummy key = RFC 8032
TEST 1 (throwaway)."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import cast

import httpx
from langgraph.types import Command
from nacl.signing import VerifyKey

from struck.callhistory import RecordedHttp
from struck.plan.models import PlanDecision
from struck.strike.account import ServedStrikeAccount, StrikeAuthedClient
from struck.strike.auth import wallet_from_hex
from struck.strike.market import StrikeMarketClient
from struck.strike.models import ApiRequest, StrikeConfirm
from struck.strike.order_cli import _Graph, _ThreadConfig
from struck.strike import pipeline as pipeline_mod
from struck.strike.pipeline import (
    SENT,
    FixedStrikePlanner,
    StrikePipeline,
    build_strike_graph,
    plan_of,
)

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "rust/struck_core/tests/golden"
SEED = "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
PUB = "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
BASE = "https://api-v2-testnet.strikefinance.org"
CREATED = b'{"strategy_id":"s","primary_client_order_id":"p","account_id":"a","symbol":"ETH-USD","sequence_id":1,"message_id":"m"}'


class _Mock:
    def __init__(self, write_status: int = 201) -> None:
        self.writes: list[httpx.Request] = []
        self._status = write_status

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/v2/markets":
            return httpx.Response(
                200, content=(GOLDEN / "strike_testnet_markets.json").read_bytes()
            )
        if path == "/v2/fee-tiers":
            return httpx.Response(
                200, content=(GOLDEN / "strike_testnet_fee_tiers.json").read_bytes()
            )
        if path == "/v2/time":
            return httpx.Response(
                200, content=f'{{"serverTime":{time.time_ns() // 1_000_000}}}'.encode()
            )
        if path == "/v2/positions":
            return httpx.Response(
                200, content=(ROOT / "tests/golden/strike_spec_positions_example.json").read_bytes()
            )
        self.writes.append(req)
        return httpx.Response(
            self._status, content=CREATED if self._status < 400 else b'{"error":"boom"}'
        )


def _run(
    tmp_path: Path, mock: _Mock, send_word: str, hash_ok: bool = True
) -> tuple[dict[str, str], Path]:
    log = tmp_path / "call_history.jsonl"
    http = RecordedHttp(5, log)
    http._client = httpx.Client(transport=httpx.MockTransport(mock))
    pipeline = StrikePipeline(
        FixedStrikePlanner(
            (ROOT / "plans/strike-eth-bracket-testnet.json").read_text(encoding="utf-8")
        ),
        StrikeMarketClient(http, BASE),
        receipts_root=tmp_path,
        sender=StrikeAuthedClient(http, BASE, wallet_from_hex(PUB, SEED)),
    )
    graph = cast("_Graph", build_strike_graph(pipeline))
    cfg: _ThreadConfig = {"configurable": {"thread_id": "t"}}
    graph.invoke({"request": "test"}, cfg)
    plan = plan_of(graph.get_state(cfg).values)
    d = PlanDecision(decision="approve", plan_hash=plan.plan_hash, approver="unit-test", ts_ms=1)
    graph.invoke(Command(resume=cast("dict[str, object]", json.loads(d.model_dump_json()))), cfg)
    h = plan.plan_hash if hash_ok else "0" * 64
    c = StrikeConfirm(
        decision="send" if send_word == "send" else "abort",
        plan_hash=h,
        approver="unit-test",
        ts_ms=2,
    )
    graph.invoke(Command(resume=cast("dict[str, object]", json.loads(c.model_dump_json()))), cfg)
    state = cast("dict[str, str]", graph.get_state(cfg).values)
    return state, log


def test_send_posts_the_exact_approved_bytes_signed(tmp_path: Path) -> None:
    mock = _Mock()
    state, log = _run(tmp_path, mock, "send")
    assert "rejected" not in state
    [req] = mock.writes
    assert (req.method, req.url.host, req.url.path) == (
        "POST",
        "api-v2-testnet.strikefinance.org",
        "/v2/order/strategy",
    )
    receipts = Path(state["receipts_dir"])
    approved = ApiRequest.model_validate_json((receipts / "leg0.request.json").read_bytes())
    assert req.content == approved.body.encode("utf-8")  # the approved bytes, verbatim
    h = req.headers
    msg = f"POST:/v2/order/strategy:{h['X-API-Wallet-Timestamp']}:{h['X-API-Wallet-Nonce']}:{hashlib.sha256(req.content).hexdigest()}"
    VerifyKey(bytes.fromhex(PUB)).verify(msg.encode(), bytes.fromhex(h["X-API-Wallet-Signature"]))
    assert (receipts / "leg0.response.json").read_bytes() == CREATED
    assert SENT.validate_json(state["executed_json"])[0].status == 201
    history = log.read_text()
    assert (
        h["X-API-Wallet-Signature"] not in history
        and SEED not in history
        and "X-API-Wallet" not in history
    )


def test_abort_sends_nothing(tmp_path: Path) -> None:
    mock = _Mock()
    state, _ = _run(tmp_path, mock, "abort")
    assert mock.writes == [] and "aborted" in state["rejected"]


def test_confirm_for_another_plan_hash_sends_nothing(tmp_path: Path) -> None:
    mock = _Mock()
    state, _ = _run(tmp_path, mock, "send", hash_ok=False)
    assert mock.writes == [] and "different plan hash" in state["rejected"]


def test_http_error_is_saved_and_stops(tmp_path: Path) -> None:
    mock = _Mock(write_status=400)
    state, _ = _run(tmp_path, mock, "send")
    assert len(mock.writes) == 1 and "leg 0 failed" in state["rejected"]
    assert "boom" in (Path(state["receipts_dir"]) / "leg0.response.error.txt").read_text()


def test_trigger_account_reads_served_positions(tmp_path: Path) -> None:
    http = RecordedHttp(5, tmp_path / "h.jsonl")
    http._client = httpx.Client(transport=httpx.MockTransport(_Mock()))
    acct = ServedStrikeAccount(StrikeAuthedClient(http, BASE, wallet_from_hex(PUB, SEED)))
    assert acct.position_field("BTC-USD", "upnl") == "-8.48714642"
    assert acct.position_field("BTC-USD", "bankruptcy_price") == "157758.0419616276541"


class _Drop(_Mock):
    """The write leaves, then the connection drops: outcome UNKNOWN."""

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            self.writes.append(req)
            raise httpx.ReadTimeout("read timed out", request=req)
        return super().__call__(req)


def test_transport_error_is_unknown_and_the_client_id_is_refused_until_resync(
    tmp_path: Path,
) -> None:
    mock = _Drop()
    state, _ = _run(tmp_path, mock, "send")
    assert len(mock.writes) == 1, "later legs must not be sent"
    rejected = state["rejected"]
    assert "outcome UNKNOWN" in rejected and "Reconcile" in rejected
    assert "unresolved ledger" in rejected
    executed = pipeline_mod.SENT.validate_json(
        (Path(state["receipts_dir"]) / "executed.json").read_bytes()
    )
    assert executed[-1].outcome == "unknown" and executed[-1].status is None
    ledger = pipeline_mod.unresolved_client_ids(tmp_path)
    assert executed[-1].client_order_id in ledger
    assert ((tmp_path / pipeline_mod.UNRESOLVED_LEDGER).stat().st_mode & 0o777) == 0o600
    # Re-run of the SAME plan (same hash -> same client order ids): refused, nothing sent.
    time.sleep(1.1)  # receipts dirs are per-second stamps (exist_ok=False)
    again = _Mock()
    state2, _ = _run(tmp_path, again, "send")
    assert again.writes == []
    assert state2["rejected"].startswith("REFUSED: client order id(s)")
    assert "Resync first" in state2["rejected"]
    # The human resyncs and clears the ledger: the send is possible again.
    (tmp_path / pipeline_mod.UNRESOLVED_LEDGER).write_text("", encoding="utf-8")
    time.sleep(1.1)
    third = _Mock()
    state3, _ = _run(tmp_path, third, "send")
    assert "rejected" not in state3 and len(third.writes) >= 1


def test_malformed_leg_is_a_loud_rejection_not_a_crash(tmp_path: Path) -> None:
    """The live vLLM run proposed a kind=order leg carrying tp/sl and its own
    client_order_id; the Rust check raises ValueError. That is a rejection
    before approval (nothing built, nothing sent), not a traceback."""
    plan = cast(
        "dict[str, object]",
        json.loads((ROOT / "plans/strike-eth-bracket-testnet.json").read_text(encoding="utf-8")),
    )
    leg = cast("list[dict[str, object]]", plan["legs"])[0]
    plan["legs"] = [{**leg, "kind": "order", "client_order_id": "llm-chosen"}]
    mock = _Mock()
    http = RecordedHttp(5, tmp_path / "call_history.jsonl")
    http._client = httpx.Client(transport=httpx.MockTransport(mock))
    pipeline = StrikePipeline(
        FixedStrikePlanner(json.dumps(plan)),
        StrikeMarketClient(http, BASE),
        receipts_root=tmp_path,
        sender=None,
    )
    graph = cast("_Graph", build_strike_graph(pipeline))
    cfg: _ThreadConfig = {"configurable": {"thread_id": "bad"}}
    graph.invoke({"request": "test"}, cfg)
    state = cast("dict[str, str]", graph.get_state(cfg).values)
    assert state["rejected"].startswith("plan validation refused leg 0: ")
    assert "plan_json" not in state and mock.writes == []
    assert graph.get_state(cfg).next == ()
