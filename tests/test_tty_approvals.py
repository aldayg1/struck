"""Piped / scripted approval input is refused at every human prompt: both
order_cli prompts (approve, send confirm) and send_cli's approve. Everything
around the prompt is a test double; no network, nothing sent."""

from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

import pytest

from struck import tty as tty_mod
from struck import send_cli
from struck.plan.models import ApprovedOnchainPlan, CheckReport
from struck.strike import order_cli
from struck.strike.models import ApprovedStrikePlan, StrikePlanProposal

ROOT = Path(__file__).parents[1]
STRIKE_FILE = ROOT / "plans" / "strike-eth-limit-testnet.json"
SEND_FILE = ROOT / "plans" / "send-2ada-preprod.json"


class _Graph:
    def __init__(self, values: dict[str, object]) -> None:
        self.values = values
        self.resumes: list[object] = []
        self.next = ("confirm_send",)

    def get_state(self, config: object) -> _Graph:
        return self

    def invoke(self, value: object, config: object) -> object:
        if not isinstance(value, dict):
            self.resumes.append(value)
        return None


class _Stdin(io.StringIO):
    """Piped stdin carrying a would-be approval; isatty() follows ``ttys``."""

    def __init__(self, text: str, ttys: list[bool]) -> None:
        super().__init__(text)
        self._ttys = ttys

    def isatty(self) -> bool:
        return self._ttys.pop(0) if self._ttys else False


def _strike_graph(monkeypatch: pytest.MonkeyPatch) -> tuple[_Graph, str]:
    proposal = StrikePlanProposal.model_validate_json(STRIKE_FILE.read_text(encoding="utf-8"))
    plan = ApprovedStrikePlan(network="testnet", legs=proposal.legs, markets_snapshot_sha256="x")
    graph = _Graph({"plan_json": plan.model_dump_json(), "receipts_dir": "/tmp/none"})

    def none(*args: object, **kwargs: object) -> None:
        return None

    def build(pipeline: object) -> _Graph:
        return graph

    def prepared(state: object) -> list[object]:
        return []

    def confirm(state: object) -> SimpleNamespace:
        return SimpleNamespace(receipts_dir="/tmp/none")

    monkeypatch.setattr(order_cli, "wallet_from_secrets", none)
    for name in (
        "StrikePipeline",
        "StrikeMarketClient",
        "StrikeAuthedClient",
        "FixedStrikePlanner",
    ):
        monkeypatch.setattr(order_cli, name, none)
    monkeypatch.setattr(order_cli, "build_strike_graph", build)
    monkeypatch.setattr(order_cli, "prepared_of", prepared)
    monkeypatch.setattr(order_cli, "confirm_request_of", confirm)
    return graph, plan.plan_hash


def test_order_cli_approve_prompt_refuses_piped_input(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    graph, plan_hash = _strike_graph(monkeypatch)
    monkeypatch.setattr("sys.stdin", _Stdin(f"approve {plan_hash}\n", [False]))
    assert order_cli.main([str(STRIKE_FILE), "--approver", "t"]) == 3
    out = capsys.readouterr().out
    assert "stdin is not a terminal" in out and "STOPPED at the approval interrupt" in out
    assert graph.resumes == []  # no decision was ever sent into the graph


def test_order_cli_send_confirm_refuses_piped_input(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    graph, plan_hash = _strike_graph(monkeypatch)

    def tty_open(path: str, encoding: str = "utf-8") -> io.StringIO:
        assert path == "/dev/tty"
        return io.StringIO(f"approve {plan_hash}\n")  # the HUMAN at the TTY (mock)

    monkeypatch.setattr(tty_mod, "open", tty_open, raising=False)
    # 1st prompt: a real TTY (mock); 2nd prompt (send confirm): piped
    monkeypatch.setattr("sys.stdin", _Stdin(f"send {plan_hash}\n", [True, False]))
    assert order_cli.main([str(STRIKE_FILE), "--approver", "t"]) == 3
    out = capsys.readouterr().out
    assert "STOPPED at the send confirm" in out and "stdin is not a terminal" in out
    assert len(graph.resumes) == 1  # only the approval; the send was never resumed


def test_send_cli_approve_prompt_refuses_piped_input(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    plan = ApprovedOnchainPlan(
        network="preprod",
        from_address="addr_test1x",
        outputs=(),
        max_fee_lovelace=1,
        valid_until_slot=1,
    )
    graph = _Graph({"receipts_dir": "/tmp/none"})

    def none(*args: object, **kwargs: object) -> None:
        return None

    def build(pipeline: object) -> _Graph:
        return graph

    def plan_of(state: object) -> ApprovedOnchainPlan:
        return plan

    def check_of(state: object) -> CheckReport:
        return CheckReport(ok=True, checks=())

    for name in ("Pipeline", "make_backend", "FixedPlanner"):
        monkeypatch.setattr(send_cli, name, none)
    monkeypatch.setattr(send_cli, "build_graph", build)
    monkeypatch.setattr(send_cli, "plan_of", plan_of)
    monkeypatch.setattr(send_cli, "plan_check_of", check_of)
    monkeypatch.setattr("sys.stdin", _Stdin(f"approve {plan.plan_hash}\n", [False]))
    assert send_cli.main([str(SEND_FILE), "--approver", "t", "--backend", "koios"]) == 3
    out = capsys.readouterr().out
    assert "stdin is not a terminal" in out and "nothing built" in out
    assert graph.resumes == []
