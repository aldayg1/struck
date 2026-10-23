"""K02 ``struck.strike.read_cli``: argument validation + raw-bytes persistence (L2).

Oracle: ``tests/fixtures/read_cli/capture-manifest.json`` — REAL Strike TESTNET
responses captured 2026-10-22 by the shipped CLI (see ``the golden provenance record`` there for
source, date and the refresh commands). The replay adapter subclasses the real
``RecordedHttp`` interface and serves ONLY those captured bytes; any call the
CLI makes that was not captured fails loudly, so no test can touch the network
and no expectation is invented. Assertions state the RULE (exact served bytes
persisted; manifest describes them truthfully), never replayed volatile values.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import pytest
from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr

from struck.callhistory import RecordedHttp, Served, sha256_hex
from struck.strike import read_cli
from struck.strike.market import KLINE_INTERVALS

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "read_cli"


class _Captured(BaseModel):
    """One entry of the capture index (mirrors the shipped manifest entry)."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    url: StrictStr
    status: StrictInt
    response_bytes: StrictInt
    sha256: StrictStr
    file: StrictStr


class _CaptureIndex(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    network: StrictStr
    captured_utc: StrictStr
    operations: dict[str, _Captured]


def _index() -> _CaptureIndex:
    return _CaptureIndex.model_validate_json(
        (FIXTURES / "capture-manifest.json").read_text(encoding="utf-8")
    )


class _ReplayHttp(RecordedHttp):
    """L2 adapter over the REAL interface: serves the captured Strike bytes for an
    exact (path, params) pair; anything else is a loud refusal, never a network call."""

    def __init__(self) -> None:
        super().__init__(5)
        self._by_path: dict[str, tuple[tuple[tuple[str, str], ...], Served]] = {}
        for name, cap in _index().operations.items():
            body = (FIXTURES / cap.file).read_bytes()
            assert len(body) == cap.response_bytes, f"{name}: fixture byte count drifted"
            assert sha256_hex(body) == cap.sha256, f"{name}: fixture sha256 drifted"
            query = tuple(sorted(parse_qsl(urlsplit(cap.url).query)))
            path = urlsplit(cap.url).path
            assert path not in self._by_path, f"capture index: ambiguous path {path}"
            self._by_path[path] = (query, Served(url=cap.url, status=cap.status, content=body))

    def request(
        self,
        source: str,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        json_body: bytes | None = None,
        auth_headers: dict[str, str] | None = None,
        cbor_body: bytes | None = None,
    ) -> Served:
        path = urlsplit(url).path
        entry = self._by_path.get(path)
        if entry is None:
            raise AssertionError(f"replay: no captured Strike response for {path}; refusing")
        expected_query, served = entry
        requested = tuple(sorted(params.items())) if params is not None else ()
        if requested != expected_query:
            raise AssertionError(
                f"replay: {path} requested with params {requested}; captured {expected_query}"
            )
        return served


def test_help_names_network_out_and_every_operation(capsys: pytest.CaptureFixture[str]) -> None:
    assert read_cli.main(["--help"]) == 0
    out = capsys.readouterr().out
    assert "--network" in out
    assert "--out" in out
    for op in ("markets", "server-time", "fee-tiers", "ticker", "depth", "trades", "klines"):
        assert op in out


def test_unknown_operation_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert read_cli.main(["no-such-op"], http=_ReplayHttp()) == 2
    err = capsys.readouterr().err
    assert "unknown operation 'no-such-op'" in err
    assert "usage:" in err


def test_wrong_arity_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert read_cli.main(["klines", "BTC-USD"], http=_ReplayHttp()) == 2
    assert "klines takes exactly <symbol> <interval> <limit>" in capsys.readouterr().err


def test_interval_must_be_a_served_value(capsys: pytest.CaptureFixture[str]) -> None:
    # Strike's served request enum (strike-klines / market.py), not a local invention.
    assert read_cli.main(["klines", "BTC-USD", "9h", "5"], http=_ReplayHttp()) == 2
    err = capsys.readouterr().err
    assert "'9h' is not a served interval" in err
    assert ", ".join(KLINE_INTERVALS) in err


@pytest.mark.parametrize(
    ("bad", "because"),
    [("0", "limit must be 1..1000"), ("1001", "limit must be 1..1000"), ("-1", "limit must be 1..1000"), ("abc", "limit must be an integer")],
)
def test_depth_limit_is_strike_documented_1_to_1000(
    bad: str, because: str, capsys: pytest.CaptureFixture[str]
) -> None:
    # .codex/skills/strike-market-data: depth limit is an integer, max 1000 (default 20).
    assert read_cli.main(["depth", "BTC-USD", bad], http=_ReplayHttp()) == 2
    assert because in capsys.readouterr().err


@pytest.mark.parametrize("bad", ["0", "1501"])
def test_klines_limit_is_strike_documented_1_to_1500(
    bad: str, capsys: pytest.CaptureFixture[str]
) -> None:
    # .codex/skills/strike-market-data: klines limit max 1500 (default 500).
    assert read_cli.main(["klines", "BTC-USD", "1h", bad], http=_ReplayHttp()) == 2
    assert "limit must be 1..1500" in capsys.readouterr().err


def test_malformed_symbol_is_refused_before_any_call(capsys: pytest.CaptureFixture[str]) -> None:
    assert read_cli.main(["ticker", "not a symbol!"], http=_ReplayHttp()) == 2
    assert "only letters, digits" in capsys.readouterr().err


def test_unserved_symbol_is_refused_but_the_markets_call_is_still_captured(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "receipts"
    assert read_cli.main(["ticker", "ZZZ-USD", "--out", str(out)], http=_ReplayHttp()) == 2
    err = capsys.readouterr().err
    assert "ZZZ-USD' is not served by /v2/markets" in err
    assert "BTC-USD" in err  # the refusal names the served symbols
    # The /v2/markets call that WAS made is kept: raw bytes + manifest, no masking.
    captured = _index().operations["markets"]
    assert (out / "markets.json").read_bytes() == (FIXTURES / captured.file).read_bytes()
    manifest = read_cli.Manifest.model_validate_json(
        (out / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest.operations["markets"].url == captured.url


# (label, argv, expected manifest/file names) — params mirror the capture run
# exactly (the golden provenance record), so the replay can serve the recorded bytes.
_OPS: list[tuple[str, list[str], list[str]]] = [
    ("markets", ["markets"], ["markets"]),
    ("server-time", ["server-time"], ["server-time"]),
    ("fee-tiers", ["fee-tiers"], ["fee-tiers"]),
    ("ticker", ["ticker", "BTC-USD"], ["markets", "ticker_BTC-USD"]),
    ("depth", ["depth", "BTC-USD", "5"], ["markets", "depth_BTC-USD_5"]),
    ("trades", ["trades", "BTC-USD", "5"], ["markets", "trades_BTC-USD_5"]),
    ("klines", ["klines", "BTC-USD", "1h", "5"], ["markets", "klines_BTC-USD_1h_5"]),
]


@pytest.mark.parametrize(("label", "argv", "names"), _OPS, ids=[row[0] for row in _OPS])
def test_operation_persists_exact_served_bytes_and_truthful_manifest(
    label: str, argv: list[str], names: list[str], tmp_path: Path
) -> None:
    out = tmp_path / label
    assert read_cli.main([*argv, "--out", str(out)], http=_ReplayHttp()) == 0
    index = _index()
    for name in names:
        captured = index.operations[name]
        body = (out / f"{name}.json").read_bytes()
        # RULE: the receipt file is the EXACT raw body as served (byte for byte).
        assert body == (FIXTURES / captured.file).read_bytes()
        # RULE: the manifest describes that file truthfully.
        assert hashlib.sha256(body).hexdigest() == captured.sha256
        assert len(body) == captured.response_bytes
    manifest = read_cli.Manifest.model_validate_json(
        (out / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest.network == "preprod"
    assert sorted(manifest.operations) == sorted(names)
    for name in names:
        captured = index.operations[name]
        entry = manifest.operations[name]
        assert entry.url == captured.url
        assert entry.status == captured.status
        assert entry.response_bytes == captured.response_bytes
        assert entry.sha256 == captured.sha256
        assert entry.file == captured.file
        assert (out / entry.file).read_bytes() == (FIXTURES / captured.file).read_bytes()
