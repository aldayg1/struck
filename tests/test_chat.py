"""Chat harness + dock with a FAKE tool-calling chat model (tests only): routing
of every reply kind to its EXISTING path, question asks back, invalid output
REJECTED, no approval possible from the chat, history persists."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import cast

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, messages_from_dict
from langchain_core.tools import BaseTool, tool
from PySide6.QtWidgets import QApplication, QPushButton

from struck.chat.assistant import ChatAssistant, ChatRejected
from struck.config import LlmConfig
from struck.plan.models import PlanProposal
from struck.strike.models import StrikePlanProposal
from struck.triggers.engine import TriggerStore
from struck.triggers.models import TriggerSpec
from struck.ui import panels
from struck.ui.app import StruckWindow
from struck.ui.chat_panel import ChatPanel

APP = QApplication.instance() or QApplication([])
ROOT = Path(__file__).parents[1]
PLANS = ROOT / "plans"


def _json(path: Path) -> dict[str, object]:
    return cast("dict[str, object]", json.loads(path.read_text(encoding="utf-8")))


STRIKE = _json(PLANS / "strike-eth-limit-testnet.json")
ONCHAIN = _json(PLANS / "send-2ada-preprod.json")
TRIGGER = _json(PLANS / "triggers" / "eth-mark-band-strike-order.json")
CALLS: list[str] = []


@cast("Callable[[Callable[[], str]], BaseTool]", tool)
def strike_markets() -> str:
    """Fake read-only markets tool (tests only)."""
    CALLS.append("strike_markets")
    return '{"markets": ["ETH-USD"]}'


class ToolCallingFake(GenericFakeChatModel):
    """Scripted AIMessages; bind_tools is a no-op so create_agent can drive it."""

    def bind_tools(self, tools: Sequence[object], **kwargs: object) -> ToolCallingFake:
        return self


def _call(name: str, args: Mapping[str, object], call_id: str) -> AIMessage:
    data = {"content": "", "tool_calls": [{"name": name, "args": dict(args), "id": call_id}]}
    doc: list[dict[str, object]] = [{"type": "ai", "data": data}]
    return cast("AIMessage", messages_from_dict(doc)[0])


def _reply(args: Mapping[str, object], call_id: str = "r1") -> AIMessage:
    return _call("ChatReply", args, call_id)


def _model(*messages: AIMessage) -> BaseChatModel:
    it: Iterator[AIMessage] = iter(messages)
    return ToolCallingFake(messages=it)


def _assistant(*messages: AIMessage) -> ChatAssistant:
    tools: tuple[BaseTool, ...] = (strike_markets,)
    return ChatAssistant(_model(*messages), tools=tools)


EMPTY: dict[str, object] = {"onchain": None, "strike": None, "trigger": None, "question": None}


def test_strike_reply_after_a_tool_call() -> None:
    CALLS.clear()
    a = _assistant(
        _call("strike_markets", {}, "t1"),
        _reply({**EMPTY, "kind": "strike", "strike": STRIKE}),
    )
    turn = a.ask("limit buy ETH")
    assert CALLS == ["strike_markets"] and turn.tool_calls[0][0] == "strike_markets"
    assert turn.reply.strike == StrikePlanProposal.model_validate_json(json.dumps(STRIKE))


def test_question_asks_back_and_history_persists() -> None:
    a = _assistant(
        _reply({**EMPTY, "kind": "question", "question": "Which symbol?"}),
        _reply({**EMPTY, "kind": "trigger", "trigger": TRIGGER}, "r2"),
    )
    assert a.ask("make me a bot").reply.question == "Which symbol?"
    turn = a.ask("ETH-USD")
    assert turn.reply.trigger == TriggerSpec.model_validate_json(json.dumps(TRIGGER))
    humans = [t for k, t in a.history() if k == "human"]
    assert humans[:2] == ["make me a bot", "ETH-USD"]


def test_invalid_output_is_rejected() -> None:
    bad = {**EMPTY, "kind": "strike", "strike": {"market": "ETH-USD", "side": "BUY"}}
    with pytest.raises(ChatRejected, match="not a valid ChatReply"):
        _assistant(_reply(bad)).ask("buy")
    mismatched = {**EMPTY, "kind": "trigger", "strike": STRIKE}
    with pytest.raises(ChatRejected):
        _assistant(_reply(mismatched)).ask("buy")
    with pytest.raises(ChatRejected, match="empty"):
        _assistant().ask("  ")


def _panel(reviewed: list[object]) -> ChatPanel:
    p = ChatPanel(reviewed.append, reviewed.append, reviewed.append)
    return p


def test_each_kind_routes_to_its_existing_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(panels, "UI_LOG", tmp_path / "ui_events.log")
    w = StruckWindow()
    w.bots.store = TriggerStore(tmp_path / "triggers.json")
    a = _assistant(
        _reply({**EMPTY, "kind": "strike", "strike": STRIKE}),
        _reply({**EMPTY, "kind": "trigger", "trigger": TRIGGER}, "r2"),
        _reply({**EMPTY, "kind": "onchain_send", "onchain": ONCHAIN}, "r3"),
    )
    buttons = [w.chat.show_reply(a.ask(t).reply) for t in ("s", "t", "o")]
    assert all(isinstance(b, QPushButton) for b in buttons)
    assert buttons[0] is not None and buttons[1] is not None and buttons[2] is not None
    buttons[0].click()
    assert w.tabs.currentWidget() is w.approval
    assert (
        w.approval._plan_text
        == StrikePlanProposal.model_validate_json(json.dumps(STRIKE)).model_dump_json()
    )
    assert not w.approval.approve_btn.isEnabled()  # Review never approves
    buttons[1].click()
    assert w.tabs.currentWidget() is w.bots and len(w.bots.store.list()) == 0  # nothing stored
    assert w.bots.builder.spec() == TriggerSpec.model_validate_json(json.dumps(TRIGGER))
    buttons[2].click()
    assert w.approval.plan_tabs.currentWidget() is w.approval.onchain
    plan = PlanProposal.model_validate_json(json.dumps(ONCHAIN))
    assert w.approval.onchain.recipient.text() == plan.legs[0].outputs[0].address


def test_chat_has_no_approve_sign_or_send_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(panels, "UI_LOG", tmp_path / "ui_events.log")
    p = _panel([])
    texts = {b.text().lower() for b in p.findChildren(QPushButton)}
    assert texts == {"ask"}
    for word in ("approve", "sign", "send"):
        assert not any(hasattr(p, n) for n in (word, f"{word}_plan", f"confirm_{word}"))


def test_unconfigured_is_loud(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(panels, "UI_LOG", tmp_path / "ui_events.log")
    p = _panel([])
    factory: Callable[[LlmConfig], BaseChatModel] = p.model_factory
    assert factory is not None
    if "LLM READY" not in p.state.text():
        assert p.state.text().startswith("[LLM NOT CONFIGURED")
    p.input.setPlainText("")
    p.ask()
    assert "empty" in p.state.text() or "NOT CONFIGURED" in p.state.text()
