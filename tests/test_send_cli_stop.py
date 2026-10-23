"""send_cli stops loudly (exit 4, one line, no traceback) on a backend failure."""

from __future__ import annotations

import pytest

from struck import send_cli
from struck.callhistory import CallFailed


def test_backend_failure_is_a_loud_exit_4(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(argv: list[str]) -> int:
        raise CallFailed("koios GET https://preprod.koios.rest/api/v1/genesis -> HTTP 429")

    monkeypatch.setattr(send_cli, "_main", boom)
    assert send_cli.main(["plan.json", "--approver", "x"]) == 4
    err = capsys.readouterr().err
    assert "STOPPED (CallFailed)" in err and "HTTP 429" in err
    assert "Traceback" not in err
