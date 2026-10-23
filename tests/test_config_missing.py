"""config/struck.json is local and untracked, with no default/example/template.
When it is missing, every CLI fails closed with an error naming the path before
doing anything. The UI fails closed differently (2026-10-22): it opens the
first-run REGISTRATION DIALOGUE with that error as its reason line; only a
cancel exits (loudly, naming the path) — nothing runs without the human."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from PySide6.QtWidgets import QDialog

from struck import config, congruence_cli, send_cli
from struck.chat import cli as chat_cli
from struck.strike import account_cli, logon_cli, market_cli, order_cli
from struck.testonly import preview_cli, strike_preview_cli
from struck.triggers import cli as triggers_cli
from struck.triggers import policy_cli
from struck.ui import app, screenshot_cli

MAINS: dict[str, Callable[[list[str]], int]] = {
    "send_cli": send_cli.main,
    "congruence_cli": congruence_cli.main,
    "chat.cli": chat_cli.main,
    "strike.order_cli": order_cli.main,
    "strike.market_cli": market_cli.main,
    "strike.account_cli": account_cli.main,
    "strike.logon_cli": logon_cli.main,
    "triggers.cli": triggers_cli.main,
    "triggers.policy_cli": policy_cli.main,
    "testonly.preview_cli": preview_cli.main,
    "testonly.strike_preview_cli": strike_preview_cli.main,
    "ui.screenshot_cli": screenshot_cli.main,
}


def _ui_main(argv: list[str]) -> int:
    sys.argv = ["struck.ui.app", *argv]
    return app.main()


@pytest.mark.parametrize("name", MAINS)
@pytest.mark.parametrize("argv", [[], ["--network", "mainnet"]])
def test_missing_config_fails_closed_naming_the_path(
    name: str, argv: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    absent = tmp_path / "config" / "struck.json"
    monkeypatch.setattr(config.load_config, "__defaults__", (absent,))
    monkeypatch.setattr(sys, "argv", list(sys.argv))
    main = MAINS[name]
    with pytest.raises(SystemExit) as exc:
        main(list(argv))
    msg = str(exc.value.code)
    assert msg.startswith("STRUCK CONFIG REFUSED") and f"{absent} is missing" in msg
    assert "Nothing was run" in msg


@pytest.mark.parametrize("argv", [[], ["--network", "mainnet"]])
def test_ui_missing_config_opens_registration_and_cancel_exits_loudly(
    argv: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The UI never dies on a missing config: the dialogue opens with the refusal
    as its reason; a cancel (the stub's Rejected) exits 2 naming the path."""
    absent = tmp_path / "config" / "struck.json"
    monkeypatch.setattr(config.load_config, "__defaults__", (absent,))
    monkeypatch.setattr(sys, "argv", ["struck.ui.app"])
    reasons: list[str] = []

    class _Cancelled:
        def __init__(self, reason: str, **_kwargs: object) -> None:
            reasons.append(reason)

        def exec(self) -> int:
            return int(QDialog.DialogCode.Rejected)

    monkeypatch.setattr(app, "RegistrationDialog", _Cancelled)
    code = _ui_main(list(argv))
    assert code == 2
    err = capsys.readouterr().err
    assert err.startswith("STRUCK REGISTRATION CANCELLED") and f"{absent} is missing" in err
    assert reasons and "is missing" in reasons[0]  # the refusal IS the dialogue's reason line


def test_load_config_names_the_path_and_the_keys(tmp_path: Path) -> None:
    absent = tmp_path / "struck.json"
    with pytest.raises(config.ConfigError, match="is missing") as exc:
        config.load_config(absent)
    assert str(absent) in str(exc.value)
    for key in ("chain_backend", "node_socket", "cardano_cli_path", "kupo_url"):
        assert key in str(exc.value)
