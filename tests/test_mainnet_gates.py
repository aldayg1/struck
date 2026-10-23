"""Mainnet is supported, and every gate holds on it. One case per
gate: (b) TTY-only approval, (c) MAINNET banner before
every approval prompt, (d) the network is inside the hashed/approved plan,
(e) a --network / Settings change never re-targets an approved plan.
Strike traffic here is a MOCK transport; nothing reaches any network."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import io
import json
from pathlib import Path
from typing import cast

import httpx
import pytest
import struck_py
from langgraph.types import Command
from PySide6.QtWidgets import QApplication

from struck import tty as tty_mod
from struck import config, send_cli
from struck.callhistory import RecordedHttp
from struck.network import PROFILES, load_network_setting
from struck.plan.models import ApprovedOnchainPlan, CheckReport, PlanDecision
from struck.strike import order_cli
from struck.strike.account import StrikeAuthedClient
from struck.strike.auth import wallet_from_hex
from struck.strike.market import StrikeMarketClient
from struck.strike.models import ApprovedStrikePlan, StrikeConfirm, StrikeLeg, StrikePlanProposal
from struck.strike.order_cli import _Graph, _ThreadConfig
from struck.strike.pipeline import FixedStrikePlanner, StrikePipeline, build_strike_graph, plan_of
from struck.triggers import policy_cli
from struck.ui import panels
from tests.test_security_fixes import _FakePolicy, _PausedGraph
from tests.test_strike_execute import PUB, SEED, _Mock
from tests.test_tty_approvals import STRIKE_FILE, _Stdin, _strike_graph

APP = QApplication.instance() or QApplication([])
ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "rust" / "struck_core" / "tests" / "golden"
PRE, MAIN = PROFILES["preprod"], PROFILES["mainnet"]
BANNER = "NETWORK: mainnet"


def _graph_on(
    tmp_path: Path, mock: _Mock, base: str
) -> tuple[_Graph, _ThreadConfig, StrikePipeline]:
    http = RecordedHttp(5, tmp_path / "calls.jsonl")
    http._client = httpx.Client(transport=httpx.MockTransport(mock))
    pipeline = StrikePipeline(
        FixedStrikePlanner(STRIKE_FILE.read_text(encoding="utf-8")),
        StrikeMarketClient(http, base),
        receipts_root=tmp_path,
        sender=StrikeAuthedClient(http, base, wallet_from_hex(PUB, SEED)),
    )
    graph = cast("_Graph", build_strike_graph(pipeline))
    cfg: _ThreadConfig = {"configurable": {"thread_id": "m"}}
    graph.invoke({"request": "test"}, cfg)
    plan = plan_of(graph.get_state(cfg).values)
    d = PlanDecision(decision="approve", plan_hash=plan.plan_hash, approver="unit-test", ts_ms=1)
    graph.invoke(Command(resume=cast("dict[str, object]", json.loads(d.model_dump_json()))), cfg)
    return graph, cfg, pipeline


def _send(graph: _Graph, cfg: _ThreadConfig) -> dict[str, str]:
    plan = plan_of(graph.get_state(cfg).values)
    c = StrikeConfirm(decision="send", plan_hash=plan.plan_hash, approver="unit-test", ts_ms=2)
    graph.invoke(Command(resume=cast("dict[str, object]", json.loads(c.model_dump_json()))), cfg)
    return cast("dict[str, str]", graph.get_state(cfg).values)


# (b) + (c) ----------------------------------------------------------------
def test_bc_order_cli_mainnet_tty_only_and_banner_first(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    graph, plan_hash = _strike_graph(monkeypatch)
    monkeypatch.setattr("sys.stdin", _Stdin(f"approve {plan_hash}\n", [False]))  # piped
    assert order_cli.main([str(STRIKE_FILE), "--approver", "t", "--network", "mainnet"]) == 3
    out = capsys.readouterr().out
    assert "stdin is not a terminal" in out and graph.resumes == []
    assert out.index(BANNER) < out.index("Type exactly 'approve")


def test_bc_order_cli_mainnet_send_prompt_tty_only_and_banner_first(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    graph, plan_hash = _strike_graph(monkeypatch)

    def tty_open(path: str, encoding: str = "utf-8") -> io.StringIO:
        return io.StringIO(f"approve {plan_hash}\n")

    monkeypatch.setattr(tty_mod, "open", tty_open, raising=False)
    monkeypatch.setattr("sys.stdin", _Stdin(f"send {plan_hash}\n", [True, False]))
    assert order_cli.main([str(STRIKE_FILE), "--approver", "t", "--network", "mainnet"]) == 3
    out = capsys.readouterr().out
    send_prompt = out.index("Type exactly 'send")
    assert out.rindex(BANNER, 0, send_prompt) > out.index("=== SEND CONFIRM")
    assert "STOPPED at the send confirm" in out and len(graph.resumes) == 1


def test_bc_send_cli_mainnet_tty_only_and_banner_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    plan = ApprovedOnchainPlan(
        network="mainnet", from_address="addr1x", outputs=(), max_fee_lovelace=1, valid_until_slot=1
    )

    class G:
        values: dict[str, object] = {"receipts_dir": "/tmp/none"}
        resumes = 0

        def get_state(self, c: object) -> G:
            return self

        def invoke(self, v: object, c: object) -> None:
            if not isinstance(v, dict):
                G.resumes += 1

    def none(*a: object, **k: object) -> None:
        return None

    for name in ("Pipeline", "make_backend", "FixedPlanner"):
        monkeypatch.setattr(send_cli, name, none)

    def build(p: object) -> G:
        return G()

    def plan_of_(s: object) -> ApprovedOnchainPlan:
        return plan

    def check_of(s: object) -> CheckReport:
        return CheckReport(ok=True, checks=())

    monkeypatch.setattr(send_cli, "build_graph", build)
    monkeypatch.setattr(send_cli, "plan_of", plan_of_)
    monkeypatch.setattr(send_cli, "plan_check_of", check_of)
    monkeypatch.setattr("sys.stdin", _Stdin(f"approve {plan.plan_hash}\n", [False]))
    (tmp_path / "p.json").write_text("{}", encoding="utf-8")
    argv = [
        str(tmp_path / "p.json"),
        "--approver",
        "t",
        "--backend",
        "koios",
        "--network",
        "mainnet",
    ]
    assert send_cli.main(argv) == 3
    out = capsys.readouterr().out
    assert out.index(BANNER) < out.index("Type exactly 'approve") and G.resumes == 0


def test_bc_policy_cli_mainnet_tty_only_and_banner_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    f = tmp_path / "p.json"
    f.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(policy_cli, "Policy", _FakePolicy)
    monkeypatch.setattr("sys.stdin", io.StringIO("approve h\n"))
    assert policy_cli.main(["approve", str(f), "--approver", "x", "--network", "mainnet"]) == 3
    out = capsys.readouterr().out
    assert out.index(BANNER) < out.index("Type exactly 'approve") and "not a terminal" in out


def test_c_ui_approval_tab_names_the_mainnet_network() -> None:
    config.select_network("mainnet")
    p = panels.ApprovalPanel()
    assert p.network_badge.text() == f"NETWORK: {MAIN.label}" and p.network_badge.styleSheet() == ""


# (d) ---------------------------------------------------------------------
def _legs() -> tuple[StrikeLeg, ...]:
    return StrikePlanProposal.model_validate_json(STRIKE_FILE.read_text("utf-8")).legs


def test_d_network_is_inside_the_approved_hash() -> None:
    legs = _legs()
    a = ApprovedStrikePlan(network=PRE.strike_venue, legs=legs, markets_snapshot_sha256="s")
    b = ApprovedStrikePlan(network=MAIN.strike_venue, legs=legs, markets_snapshot_sha256="s")
    assert a.plan_hash != b.plan_hash

    def onchain(net: str) -> str:
        return ApprovedOnchainPlan(
            network=net,
            from_address="addr_test1x",
            outputs=(),
            max_fee_lovelace=1,
            valid_until_slot=1,
        ).plan_hash

    assert onchain(PRE.name) != onchain(MAIN.name)


def test_d_preprod_approved_tx_fails_congruence_on_mainnet() -> None:
    cbor = (GOLDEN / "send_2ada.cbor.hex").read_text(encoding="utf-8").strip()
    plan = ApprovedOnchainPlan.model_validate_json((GOLDEN / "send_2ada.plan.json").read_bytes())
    inputs = (GOLDEN / "send_2ada.inputs.json").read_text(encoding="utf-8")
    report = CheckReport.model_validate_json(
        struck_py.congruence_check(cbor, plan.canonical_json, inputs, MAIN.rust_json())
    )
    assert not report.ok and "plan.network" in {c.id for c in report.checks if not c.ok}


def test_d_preprod_approved_strike_plan_refused_by_a_mainnet_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock = _Mock()
    graph, cfg, pipeline = _graph_on(tmp_path, mock, PRE.strike_api_url)
    http = RecordedHttp(5, tmp_path / "calls.jsonl")
    http._client = httpx.Client(transport=httpx.MockTransport(mock))
    pipeline._market = StrikeMarketClient(http, MAIN.strike_api_url)  # re-targeted after approval
    state = _send(graph, cfg)
    assert mock.writes == [] and state["rejected"].startswith("NETWORK MISMATCH")


# (e) ---------------------------------------------------------------------
def test_e_order_cli_refuses_an_approved_plan_of_another_network(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    graph, plan_hash = _strike_graph(monkeypatch)  # plan approved for Strike testnet
    answers = [f"approve {plan_hash}\n", f"send {plan_hash}\n"]

    def tty_open(path: str, encoding: str = "utf-8") -> io.StringIO:
        return io.StringIO(answers.pop(0))

    monkeypatch.setattr(tty_mod, "open", tty_open, raising=False)
    monkeypatch.setattr("sys.stdin", _Stdin("", [True, True]))
    assert order_cli.main([str(STRIKE_FILE), "--approver", "t", "--network", "mainnet"]) == 5
    out = capsys.readouterr().out
    assert "NOT SENT: NETWORK MISMATCH" in out and len(graph.resumes) == 1


def test_e_ui_refuses_an_approved_plan_of_another_network(monkeypatch: pytest.MonkeyPatch) -> None:
    config.select_network("mainnet")
    p = panels.ApprovalPanel()
    plan = ApprovedStrikePlan(network="testnet", legs=_legs(), markets_snapshot_sha256="a")
    p._graph = cast("_Graph", _PausedGraph(plan))
    p._shown_hash = plan.plan_hash
    p._wallet = wallet_from_hex(PUB, SEED)
    p.confirm_send("send")
    assert "NOT SENT: NETWORK MISMATCH" in p.raw_view.toPlainText()


def test_e_settings_change_does_not_switch_a_running_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    net_file = tmp_path / "cfg" / "network.json"
    monkeypatch.setattr(config, "load_network_setting", lambda: load_network_setting(net_file))
    assert config.load_config().network == "preprod"  # resolved and pinned for this process
    s = panels.SettingsPanel(tmp_path / "llm.json", net_file)
    s.network.setCurrentText("mainnet")
    s.save_network()
    assert load_network_setting(net_file) == "mainnet"  # saved for the NEXT start
    assert config.load_config().network == "preprod"  # this session never switches
    assert "restart" in s.network_state.text()


def test_d_signing_client_on_another_network_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """execute() checks the SIGNING client too, not only the market client."""
    mock = _Mock()
    graph, cfg, pipeline = _graph_on(tmp_path, mock, PRE.strike_api_url)
    http = RecordedHttp(5, tmp_path / "calls.jsonl")
    http._client = httpx.Client(transport=httpx.MockTransport(mock))
    pipeline._sender = StrikeAuthedClient(http, MAIN.strike_api_url, wallet_from_hex(PUB, SEED))
    state = _send(graph, cfg)
    assert mock.writes == []
    assert (
        state["rejected"].startswith("NETWORK MISMATCH") and "SIGNING client" in state["rejected"]
    )


def test_d_signing_client_base_url_must_be_the_approved_networks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock = _Mock()
    graph, cfg, pipeline = _graph_on(tmp_path, mock, PRE.strike_api_url)

    class _Drifted:
        network = PRE
        base_url = MAIN.strike_api_url  # right network label, wrong host

        def request(self, method: str, path: str, body: bytes | None = None) -> object:
            raise AssertionError("must not send")

    pipeline._sender = cast("StrikeAuthedClient", _Drifted())
    state = _send(graph, cfg)
    assert mock.writes == [] and "SIGNING client" in state["rejected"]
