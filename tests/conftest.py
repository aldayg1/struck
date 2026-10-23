"""Test isolation (autouse, every test):

* network: the per-user ``network.json`` and any ``--network`` selection are
  isolated away; every test starts on the ``network`` of ``TEST_CONFIG``
  (selection rung 3, the local config field; there is no code default);
* config: ``load_config()`` reads a temp config built from ``TEST_CONFIG``
  (test-only endpoint values; never the local ``config/struck.json``, which is
  untracked and may be absent) whose ``cardano_cli_path`` is a temp copy of
  ``/bin/true`` (no test needs ``/mnt/cardano-node``);
* logs: the call history and ``panels.UI_LOG`` go to ``tmp_path`` (no test
  writes into the repo's ``logs/``);
* secrets: ``struck.secrets.SECRETS_PATH`` -> a temp file (the real stock
  ``CryptFileKeyring`` writes it only after ``unlock``; ``put_secret`` unlocks
  with ``TEST_MASTER``); the store starts LOCKED. keyring's global backend is
  set EXPLICITLY to keyring's ``fail`` backend around every test, and backend
  auto-detection is replaced by a tripwire, so no test can reach the user's
  real secret store, an OS keyring or DBus (tests/test_secrets.py guards it).
"""

import json
import shutil
from collections.abc import Iterator
from pathlib import Path

import keyring
import pytest
from keyring.core import load_keyring

from struck import callhistory, config
from struck import secrets as struck_secrets
from struck.ui import panels


# Test-only endpoints (loopback; nothing is contacted). cardano_cli_path is filled per test.
TEST_CONFIG: dict[str, object] = {
    "network": "preprod",
    "chain_backend": "koios",
    "node_socket": {"preprod": "/nonexistent/test/node.socket", "mainnet": None},
    "kupo_url": "http://127.0.0.1:1442",
    "ogmios_url": "http://127.0.0.1:1337",
    "timeout_s": 30,
}


def config_path_of(tmp_path: Path) -> Path:
    """The temp config file ``load_config()`` reads in this test."""
    return tmp_path / "conftest-struck-config.json"


TEST_MASTER = "test-only master password"
DETECTIONS: list[str] = []  # any backend auto-detection lands here (guard test)


def unlock_test_store() -> None:
    """Unlock (creating on first use) the temp store with the test-only password."""
    if not struck_secrets.is_unlocked():
        struck_secrets.unlock(TEST_MASTER)


def put_secret(name: str, value: str) -> None:
    unlock_test_store()
    struck_secrets.set_secret(name, value)


def _tripwire(*args: object, **kwargs: object) -> None:
    DETECTIONS.append("keyring backend auto-detection")
    raise AssertionError("a test reached keyring backend auto-detection (real backends)")


@pytest.fixture(name="_isolated", autouse=True)
def _isolated_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(config, "load_network_setting", lambda: None)
    saved = list(config._SELECTED)
    config._SELECTED.clear()

    cli = tmp_path / "cardano-cli-copy"
    shutil.copy("/bin/true", cli)
    cli.chmod(0o755)
    data = {**TEST_CONFIG, "cardano_cli_path": str(cli)}
    cfg = config_path_of(tmp_path)
    cfg.write_text(json.dumps(data), encoding="utf-8")
    cfg.chmod(0o600)  # load_config refuses a group/other-writable config
    monkeypatch.setattr(config.load_config, "__defaults__", (cfg,))

    history = tmp_path / "call_history.jsonl"
    monkeypatch.setattr(callhistory.append_record, "__defaults__", (history,))
    monkeypatch.setattr(callhistory.RecordedHttp.__init__, "__defaults__", (history,))
    monkeypatch.setattr(callhistory.record_command, "__defaults__", (history,))
    monkeypatch.setattr(panels, "UI_LOG", tmp_path / "ui_events.log")
    secrets_dir = tmp_path / "struck-user-config"
    secrets_dir.mkdir(mode=0o700)
    secrets_dir.chmod(0o700)  # umask-proof
    monkeypatch.setattr(struck_secrets, "SECRETS_PATH", secrets_dir / "secrets.cfg")
    monkeypatch.setattr("keyring.core.init_backend", _tripwire)
    monkeypatch.setattr("keyring.backend.get_all_keyring", _tripwire)
    keyring.set_keyring(load_keyring("keyring.backends.fail.Keyring"))
    struck_secrets.lock()
    yield
    struck_secrets.lock()
    keyring.set_keyring(load_keyring("keyring.backends.fail.Keyring"))
    config._SELECTED[:] = saved
