"""The ONE network setting: every branch, from the frozen table struck.network.PROFILES."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
import subprocess
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError
from PySide6.QtWidgets import QApplication

import struck_py
from tests.conftest import config_path_of
from struck import config
from struck.chain.backends import BackendError, KoiosBackend, NodeBackend
from struck.chain.cli import CardanoCli
from struck.chain.koios_context import KoiosChainContext
from struck.chain.reads import OgmiosClient
from struck.plan.models import ApprovedOnchainPlan, CheckReport
from struck.network import (
    KOIOS_HOSTS,
    NETWORKS,
    PROFILES,
    STRIKE_HOSTS,
    NetworkError,
    NetworkProfile,
    validate_table,
    canonical_address,
    checked_strike_url,
    load_network_setting,
    profile,
    profile_for_strike_url,
    profile_for_strike_venue,
    save_network_setting,
)
from struck.strike.auth import wallet_from_hex
from struck.strike.streams import StrikeStream
from struck.ui import panels
from tests.test_strike_execute import PUB, SEED

APP = QApplication.instance() or QApplication([])
ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "rust" / "struck_core" / "tests" / "golden"
PRE, MAIN = PROFILES["preprod"], PROFILES["mainnet"]
PRE_ADDR = "addr_test1vz2fxv2umyhttkxyxp8x0dlpdt3k6cwng5pxj3jhsydzerspjrlsz"
MAIN_ADDR = "addr1qx2fxv2umyhttkxyxp8x0dlpdt3k6cwng5pxj3jhsydzer3n0d3vllmyqwsx5wktcd8cc3sq835lu7drv2xwl2wywfgse35a3x"


def test_table_rows_and_derived_values() -> None:
    assert NETWORKS == ("preprod", "mainnet")
    assert (PRE.cardano_magic, MAIN.cardano_magic) == (1, 764824073)
    assert (PRE.network_id, MAIN.network_id) == (0, 1)
    assert (PRE.address_prefix, MAIN.address_prefix) == ("addr_test1", "addr1")
    assert MAIN.label == "mainnet (Cardano mainnet + Strike MAINNET)"
    assert MAIN.strike_label == "Strike MAINNET" and PRE.strike_label == "Strike TESTNET"
    assert STRIKE_HOSTS == {"api-v2-testnet.strikefinance.org", "api.strikefinance.org"}
    assert cast("dict[str, object]", json.loads(MAIN.rust_json())) == {
        "name": "mainnet",
        "network_id": 1,
        "address_hrp": "addr",
        "label": MAIN.label,
        "strike_label": MAIN.strike_label,
    }


def test_table_is_frozen_and_validated_loudly() -> None:
    with pytest.raises(TypeError):
        cast("dict[str, NetworkProfile]", PROFILES)["evil"] = PRE  # read-only mapping
    with pytest.raises(ValidationError):
        PRE.__setattr__("cardano_magic", 2)  # frozen row
    clash = NetworkProfile.model_validate_json(
        MAIN.model_dump_json().replace('"cardano_magic":764824073', '"cardano_magic":1')
    )
    assert clash.cardano_magic == PRE.cardano_magic
    with pytest.raises(ValueError, match="cardano_magic must differ"):
        validate_table((PRE, clash))
    with pytest.raises(ValueError, match="empty"):
        validate_table(())
    assert KOIOS_HOSTS == {"preprod.koios.rest", "api.koios.rest"}
    import struck.network as network_mod

    assert not hasattr(network_mod, "DEFAULT_NETWORK")  # no code default network


def test_rust_fixture_matches_the_python_table() -> None:
    fixture = (ROOT / "rust" / "struck_core" / "tests" / "common" / "mod.rs").read_text("utf-8")
    for p in PROFILES.values():
        assert p.rust_json() in fixture, p.name


def test_unknown_network_has_no_fallback() -> None:
    for name in ("testnet", "Mainnet", "", "preview"):
        with pytest.raises(NetworkError, match="unknown network"):
            profile(name)
    with pytest.raises(NetworkError):
        profile_for_strike_venue("preprod")


@pytest.mark.parametrize(
    "url",
    [
        "https://api.strikefinance.org.evil.com",
        "https://evil.api.strikefinance.org",
        "https://api.strikefinance.org:8443",
        "http://api.strikefinance.org",
        "https://user@api.strikefinance.org",
        "https://api.strikefinance.org/?x=1",
        "wss://api.strikefinance.org.",
    ],
)
def test_strike_url_exact_host_allowlist(url: str) -> None:
    with pytest.raises(NetworkError):
        checked_strike_url(url)


def test_strike_url_maps_to_its_network() -> None:
    for p in PROFILES.values():
        assert profile_for_strike_url(p.strike_api_url) is p
        assert profile_for_strike_venue(p.strike_venue) is p
        for ws in (p.strike_price_ws, p.strike_user_ws):
            assert checked_strike_url(ws) == ws
    with pytest.raises(NetworkError):
        StrikeStream("wss://evil.example/ws/price", "x", list)


def test_addresses_only_for_the_selected_network() -> None:
    assert canonical_address(PRE_ADDR, PRE) == PRE_ADDR
    assert canonical_address(MAIN_ADDR, MAIN) == MAIN_ADDR
    with pytest.raises(NetworkError):
        canonical_address(PRE_ADDR, MAIN)
    with pytest.raises(NetworkError):
        canonical_address(MAIN_ADDR, PRE)


def test_selection_order_and_no_silent_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    assert config.load_config().network == "preprod"  # rung 3: the local config field
    monkeypatch.setattr(config, "load_network_setting", lambda: "mainnet")
    assert config.load_config().network == "preprod"  # pinned: no switch mid-process
    config._SELECTED.clear()  # = a new process
    assert config.load_config().network == "mainnet"  # the user's Settings choice
    assert config.pop_network_flag(["a", "--network", "preprod", "b"]) == ["a", "b"]
    assert config.load_config().network == "preprod"  # the flag wins
    assert config.pop_network_flag(["--network=mainnet"]) == []
    cfg = config.load_config()
    assert cfg.strike_api_url == MAIN.strike_api_url and cfg.cardano_magic == MAIN.cardano_magic
    assert config.network_banner(cfg) == f"NETWORK: {MAIN.label}"
    with pytest.raises(SystemExit, match="unknown network 'testnet'"):
        config.pop_network_flag(["--network", "testnet"])
    with pytest.raises(SystemExit):
        config.pop_network_flag(["--network"])


def test_network_setting_file_is_private(tmp_path: Path) -> None:
    path = tmp_path / "struck" / "network.json"
    assert load_network_setting(path) is None
    save_network_setting("mainnet", path)
    assert (path.stat().st_mode & 0o777) == 0o600
    assert load_network_setting(path) == "mainnet"
    for mode in (0o666, 0o640, 0o644, 0o400):
        path.chmod(mode)
        with pytest.raises(NetworkError, match="must be 600"):
            load_network_setting(path)
    path.chmod(0o600)
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(NetworkError, match="regular file"):
        load_network_setting(link)
    parent = path.parent
    parent.chmod(0o770)
    with pytest.raises(NetworkError, match="group/other-writable"):
        load_network_setting(path)
    with pytest.raises(NetworkError, match="group/other-writable"):
        save_network_setting("preprod", path)
    parent.chmod(0o700)
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    (tmp_path / "linkdir").symlink_to(real)
    with pytest.raises(NetworkError, match="not a symlink"):
        save_network_setting("preprod", tmp_path / "linkdir" / "network.json")
    with pytest.raises(NetworkError, match="symlink"):
        save_network_setting("preprod", link)
    with pytest.raises(NetworkError, match="unknown network"):
        save_network_setting("testnet", path)
    path.write_text('{"network": "testnet"}', encoding="utf-8")
    with pytest.raises(NetworkError, match="unknown network"):
        load_network_setting(path)


def test_cardano_cli_args_come_from_the_table(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, args in (("preprod", ["--testnet-magic", "1"]), ("mainnet", ["--mainnet"])):
        config.select_network(name)
        assert CardanoCli(config.load_config()).network_args() == args


class _Ogmios:
    def __init__(self, magic: int, kind: str) -> None:
        self.answer = (magic, kind)

    def network_magic(self) -> tuple[int, str]:
        return self.answer


class _Genesis:
    def __init__(self, magic: int, slot_length: int = 1) -> None:
        self.network_magic = magic
        self.slot_length = slot_length


class _Ctx:
    def __init__(self, magic: int) -> None:
        self.genesis_param = _Genesis(magic)


class _Cli:
    def socket_tip_slot(self) -> int:
        return 42


@pytest.mark.parametrize("selected", ["preprod", "mainnet"])
def test_backends_refuse_a_node_or_koios_of_another_network(selected: str) -> None:
    net = PROFILES[selected]
    other = MAIN if net is PRE else PRE
    node = object.__new__(NodeBackend)
    node._net = net
    node._cli = cast("CardanoCli", _Cli())
    node._ogmios = cast("OgmiosClient", _Ogmios(net.cardano_magic, net.ogmios_genesis_network))
    assert selected in node.check_network()
    node._ogmios = cast("OgmiosClient", _Ogmios(other.cardano_magic, other.ogmios_genesis_network))
    with pytest.raises(BackendError, match="NETWORK MISMATCH"):
        node.check_network()
    koios = object.__new__(KoiosBackend)
    koios._net = net
    koios._ctx = cast("KoiosChainContext", _Ctx(net.cardano_magic))
    assert selected in koios.check_network()
    koios._ctx = cast("KoiosChainContext", _Ctx(other.cardano_magic))
    with pytest.raises(BackendError, match="NETWORK MISMATCH"):
        koios.check_network()


def _fake_run(code: int, out: bytes, err: bytes, seen: list[list[str]]) -> object:
    def run(argv: list[str], **kw: object) -> subprocess.CompletedProcess[bytes]:
        seen.append(list(argv))
        return subprocess.CompletedProcess(argv, code, out, err)

    return run


def test_node_socket_handshake_is_checked_on_the_selected_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from struck.chain.cli import CliError

    seen: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", _fake_run(0, b'{"slot": 7}', b"", seen))
    cli = CardanoCli(config.load_config())
    assert cli.socket_tip_slot() == 7
    assert seen[-1][1:] == [
        "conway", "query", "tip", "--testnet-magic", "1",
        "--socket-path", "/nonexistent/test/node.socket",
    ]  # fmt: skip
    refused = b'HandshakeError (Refused NodeToClientV_16 "version data mismatch: magic")'
    monkeypatch.setattr(subprocess, "run", _fake_run(1, b"", refused, seen))
    with pytest.raises(CliError, match="HandshakeError"):
        cli.socket_tip_slot()  # a socket on another network fails loudly
    monkeypatch.setattr(subprocess, "run", _fake_run(0, b"not json", b"", seen))
    with pytest.raises(CliError, match="unexpected output"):
        cli.socket_tip_slot()


def test_rust_checks_take_the_selected_network() -> None:
    cbor = (GOLDEN / "send_2ada.cbor.hex").read_text(encoding="utf-8").strip()
    plan = ApprovedOnchainPlan.model_validate_json((GOLDEN / "send_2ada.plan.json").read_bytes())
    inputs = (GOLDEN / "send_2ada.inputs.json").read_text(encoding="utf-8")
    ok = CheckReport.model_validate_json(
        struck_py.congruence_check(cbor, plan.canonical_json, inputs, PRE.rust_json())
    )
    assert ok.ok
    bad = CheckReport.model_validate_json(
        struck_py.congruence_check(cbor, plan.canonical_json, inputs, MAIN.rust_json())
    )
    assert "plan.network" in {c.id for c in bad.checks if not c.ok}
    with pytest.raises(ValueError):
        struck_py.congruence_check(cbor, plan.canonical_json, inputs, '{"name": "preprod"}')


def test_ui_badge_settings_toggle_and_logon_offset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(panels, "UI_LOG", tmp_path / "ui.log")
    assert panels.network_badge(PRE).text() == f"NETWORK: {PRE.label}"
    badge = panels.network_badge(MAIN)
    assert badge.text() == f"NETWORK: {MAIN.label}" and badge.styleSheet() == ""
    s = panels.SettingsPanel(tmp_path / "llm.json", tmp_path / "net" / "network.json")
    s.network.setCurrentText("mainnet")
    s.save_network()
    assert load_network_setting(tmp_path / "net" / "network.json") == "mainnet"
    assert "restart" in s.network_state.text() and "Running on: preprod" in s.network_state.text()
    acct = panels.AccountPanel()
    with pytest.raises(Exception, match="not logging on"):
        acct.logon_request()  # no wallet / no served time: loud
    acct._wallet = wallet_from_hex(PUB, SEED)
    with pytest.raises(Exception, match="not logging on"):
        acct.logon_request()  # wallet but server time not read yet: loud
    acct._offset_ms = 5_000
    monkeypatch.setattr("struck.ui.panels.time.time_ns", lambda: 1_000_000_000_000)
    params = cast("dict[str, object]", acct.logon_request()[0]["params"])
    assert params["timestamp"] == 1_000_000 + 5_000


class _TipCtx:
    def __init__(self, block_time: int, slot_length: int = 1) -> None:
        from struck.chain.koios import KoiosTip

        self.tip = KoiosTip(slot=1000, height=1, hash="00", epoch=1, block_time=block_time)
        self.genesis_param = _Genesis(PRE.cardano_magic, slot_length)

    def utxo_rows(self, address: str) -> list[object]:
        return []


def _koios(ctx: _TipCtx) -> KoiosBackend:
    k = object.__new__(KoiosBackend)
    k._net = PRE
    k._ctx = cast("KoiosChainContext", ctx)
    return k


@pytest.mark.parametrize(
    ("offset_s", "ok"),
    [(0, True), (590, True), (-110, True), (601, False), (-121, False), (86_400, False)],
)
def test_koios_tip_vs_local_clock_bounds(
    offset_s: int, ok: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("struck.chain.backends.time.time", lambda: 2_000_000_000.0)
    k = _koios(_TipCtx(2_000_000_000 - offset_s))
    if ok:
        view = k.wallet_view(PRE_ADDR, 30)
        assert view.valid_until_slot == 1000 + 30 * 60
    else:
        with pytest.raises(BackendError, match="from local time"):
            k.wallet_view(PRE_ADDR, 30)


def test_koios_validity_window_is_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("struck.chain.backends.time.time", lambda: 2_000_000_000.0)
    with pytest.raises(BackendError, match="must be > 0"):
        _koios(_TipCtx(2_000_000_000)).wallet_view(PRE_ADDR, 0)
    with pytest.raises(BackendError, match="whole number"):
        _koios(_TipCtx(2_000_000_000, slot_length=7)).wallet_view(PRE_ADDR, 1)


def _write_cfg(tmp_path: Path, **over: object) -> Path:
    raw = cast("dict[str, object]", json.loads(config_path_of(tmp_path).read_text("utf-8")))
    raw.update(over)
    for k in [k for k, v in over.items() if v is _DROP]:
        del raw[k]
    path = tmp_path / "variant.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    path.chmod(0o600)
    return path


_DROP = object()


def test_no_network_anywhere_fails_loudly_naming_all_three_ways(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write_cfg(tmp_path, network=_DROP)
    monkeypatch.setattr(config.load_config, "__defaults__", (path,))
    with pytest.raises(config.ConfigError) as exc:
        config.load_config()
    msg = str(exc.value)
    for way in ("--network", "Settings", "network.json", '"network"', "config/struck.json"):
        assert way in msg, way
    assert config._SELECTED == []  # nothing was silently chosen
    with pytest.raises(SystemExit, match="NO NETWORK SELECTED"):
        config.pop_network_flag([])  # every CLI and the UI call this first
    assert config.pop_network_flag(["--network", "mainnet"]) == []  # rung 1 alone suffices
    config._SELECTED.clear()
    monkeypatch.setattr(config, "load_network_setting", lambda: "mainnet")
    assert config.load_config().network == "mainnet"  # rung 2 alone suffices


def test_config_network_field_is_validated(tmp_path: Path) -> None:
    with pytest.raises(config.ConfigError, match="unknown network 'testnet'"):
        config.load_config(_write_cfg(tmp_path, network="testnet"))
    with pytest.raises(config.ConfigError, match="must be a string"):
        config.load_config(_write_cfg(tmp_path, network=1))


def test_node_socket_follows_the_network(tmp_path: Path) -> None:
    cfg = config.load_config()
    assert cfg.node_socket_path == "/nonexistent/test/node.socket"  # preprod row
    config._SELECTED[:] = ["mainnet"]
    main = config.load_config()
    with pytest.raises(config.ConfigError, match="node_socket.mainnet is null"):
        main.node_socket_path
    from struck.callhistory import RecordedHttp
    from struck.chain.backends import make_backend

    http = RecordedHttp(5, tmp_path / "calls.jsonl")
    with pytest.raises(config.ConfigError, match="no node is configured for"):
        make_backend(main, "node", http)  # refused before any client is built
    assert make_backend(main, "koios", http).network == MAIN  # koios needs no socket
    http.close()
    config._SELECTED.clear()
    for bad in ({"preprod": "/s"}, {"preprod": "/s", "mainnet": None, "x": None}):
        with pytest.raises(ValidationError, match="every network"):
            config.load_config(_write_cfg(tmp_path, node_socket=bad))
    with pytest.raises(ValidationError, match="absolute"):
        config.load_config(_write_cfg(tmp_path, node_socket={"preprod": "rel", "mainnet": None}))
