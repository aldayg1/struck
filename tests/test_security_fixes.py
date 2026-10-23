"""Security review fixes (2026-10-21): stale-graph approval, TTY-only approvals,
full LLM-field visibility, allowlisted cardano-cli env, bech32 before URLs,
credential-free base_url."""

from __future__ import annotations

import io
import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import cast

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from tests.conftest import config_path_of
from struck.network import PROFILES
from struck.chain import cli as chain_cli
from struck.chain.reads import ChainReadError, KupoClient
from struck.callhistory import RecordedHttp
from struck.config import load_config
from struck.strike.models import ApprovedStrikePlan, StrikePlanProposal
from struck.strike.order_cli import _Graph
from struck.strike.pipeline import StrikeState, summarize
from struck.triggers import policy_cli
from struck.ui import panels
from struck.ui.panels import LEG_COLUMNS, ApprovalPanel, SettingsPanel, strike_leg_rows

APP = QApplication.instance() or QApplication([])
ROOT = Path(__file__).parents[1]
PLAN = StrikePlanProposal.model_validate_json(
    (ROOT / "plans" / "strike-eth-bracket-testnet.json").read_text(encoding="utf-8")
)


class _PausedGraph:
    def __init__(self, plan: ApprovedStrikePlan) -> None:
        self.values: StrikeState = {"plan_json": plan.model_dump_json()}

    def get_state(self, config: object) -> _PausedGraph:
        return self

    def invoke(self, value: object, config: object) -> object:
        raise AssertionError("must not resume a stale plan")


def _graph_of(p: ApprovalPanel) -> tuple[object, object]:
    return p._graph, p._shown_hash


def test_new_plan_drops_the_paused_graph_and_hash_mismatch_is_loud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(panels, "UI_LOG", tmp_path / "ui.log")
    p = ApprovalPanel()
    old = ApprovedStrikePlan(network="testnet", legs=PLAN.legs, markets_snapshot_sha256="a")

    def load_file(plan: StrikePlanProposal) -> None:
        p.load_plan(ROOT / "plans" / "strike-eth-limit-testnet.json")

    loads: tuple[Callable[[StrikePlanProposal], None], ...] = (
        p.use_chat_plan,
        p.use_ticket_plan,
        load_file,
    )
    for load in loads:
        p._graph = cast("_Graph", _PausedGraph(old))
        p._shown_hash = old.plan_hash
        for b in (p.approve_btn, p.reject_btn, p.send_btn, p.abort_btn):
            b.setEnabled(True)
        load(PLAN)
        assert _graph_of(p) == (None, None)
        assert not any(
            b.isEnabled() for b in (p.approve_btn, p.reject_btn, p.send_btn, p.abort_btn)
        )
        p.decide("approve")
        assert "NOT DONE" in p.raw_view.toPlainText()
    p._graph = cast("_Graph", _PausedGraph(old))
    p._shown_hash = "someotherhash"
    p.decide("approve")
    assert "!= paused graph plan_hash" in p.raw_view.toPlainText()
    p.confirm_send("abort")
    assert "NOT SENT" in p.raw_view.toPlainText()


class _FakePolicy:
    policy_hash = "h"

    @classmethod
    def model_validate_json(cls, raw: bytes) -> _FakePolicy:
        return cls()

    def model_dump_json(self, indent: int = 0) -> str:
        return "{}"


def test_piped_approval_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    f = tmp_path / "p.json"
    f.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(policy_cli, "Policy", _FakePolicy)
    monkeypatch.setattr("sys.stdin", io.StringIO("approve h\n"))  # piped, not a TTY
    assert policy_cli.main(["approve", str(f), "--approver", "x"]) == 3
    assert "stdin is not a terminal" in capsys.readouterr().out


def test_approval_view_shows_every_llm_field() -> None:
    for col in (
        "TIF",
        "reduce_only",
        "post_only",
        "callback_rate",
        "activation",
        "client_order_id",
    ):
        assert col in LEG_COLUMNS
    row = strike_leg_rows(PLAN.legs)[0]
    leg = PLAN.legs[0]
    assert len(row) == len(LEG_COLUMNS)
    assert str(leg.reduce_only).lower() in row and (leg.time_in_force or "") in row
    if leg.tp is not None:
        assert any(f"stop {leg.tp.stop_price} price" in c for c in row)
    text = "\n".join(
        summarize(
            ApprovedStrikePlan(network="testnet", legs=PLAN.legs, markets_snapshot_sha256="a")
        )
    )
    for word in (
        "time_in_force",
        "reduce_only",
        "post_only",
        "callback_rate",
        "activation_price",
        "client_order_id",
    ):
        assert word in text


def _noop(*args: object) -> None:
    return None


def test_cardano_cli_gets_an_allowlisted_env(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, str] = {}

    def fake_run(
        argv: list[str], capture_output: bool, env: dict[str, str], check: bool
    ) -> subprocess.CompletedProcess[bytes]:
        seen.update(env)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setenv("OPENAI_API_KEY", "sk-must-not-leak")
    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(chain_cli, "record_command", _noop)
    chain_cli.CardanoCli(load_config())._run(["version"])
    assert set(seen) <= {*chain_cli.CLI_ENV_ALLOWLIST, "CARDANO_NODE_SOCKET_PATH"}
    assert "OPENAI_API_KEY" not in seen
    assert "CARDANO_NODE_SOCKET_PATH" in seen


@pytest.mark.parametrize(
    "bad",
    [
        "../../admin",
        "addr_test1xyz?unspent&x",
        "addr1qx2fxv2umyhttkxyxp8x0dlpdt3k6cwng5pxj3jhsydzer3n0d3vllmyqwsx5wktcd8cc3sq835lu7drv2xwl2wywfgse35a3x",
    ],
)
def test_kupo_and_tool_reject_non_preprod_addresses(bad: str) -> None:
    http = RecordedHttp(5)
    try:
        with pytest.raises(ChainReadError, match="address"):
            KupoClient(http, "http://127.0.0.1:1442", PROFILES["preprod"]).unspent(bad)
    finally:
        http.close()
    from struck.chat.tools import wallet_utxos

    with pytest.raises(ValueError, match="address"):
        wallet_utxos.invoke(bad)


def test_settings_rejects_credentials_or_query_in_base_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config_path_of(tmp_path)  # conftest's temp config (no /mnt/cardano-node)
    cfg_before = cfg.read_bytes()
    llm = tmp_path / "llm" / "llm.json"
    s = SettingsPanel(llm, tmp_path / "net" / "network.json")
    s.provider.setCurrentText("ollama")
    s.model_pick.setCurrentText("m")
    for url in ("http://user:pw@127.0.0.1:11434", "http://127.0.0.1:11434/?api_key=x"):
        s.base_url.setText(url)
        s.save()
        assert "NOT SAVED" in s.state.text()
        assert not llm.exists() and cfg.read_bytes() == cfg_before


class _Reached(Exception):
    pass


def test_valid_preprod_address_passes_into_the_kupo_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    good = "addr_test1vz2fxv2umyhttkxyxp8x0dlpdt3k6cwng5pxj3jhsydzerspjrlsz"
    urls: list[str] = []

    def fake_request(self: RecordedHttp, source: str, method: str, url: str) -> object:
        urls.append(url)
        raise _Reached

    monkeypatch.setattr(RecordedHttp, "request", fake_request)
    http = RecordedHttp(5)
    try:
        with pytest.raises(_Reached):
            KupoClient(http, "http://127.0.0.1:1442", PROFILES["preprod"]).unspent(good)
    finally:
        http.close()
    assert urls == [f"http://127.0.0.1:1442/matches/{good}?unspent"]
