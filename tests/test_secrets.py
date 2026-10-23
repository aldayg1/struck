"""struck.secrets / struck.keys / the Settings secrets box against the REAL stock
CryptFileKeyring on a temp file (tests/conftest.py: SECRETS_PATH in tmp_path)."""

from __future__ import annotations

import getpass
import os
import subprocess
import sys
from pathlib import Path

import keyring
import pytest
from keyrings.cryptfile.cryptfile import CryptFileKeyring
from PySide6.QtWidgets import QApplication, QLineEdit

from struck import keys, paths
from struck import secrets as s
from struck.strike import venue_auth
from struck.ui import panels
from tests.conftest import DETECTIONS, TEST_MASTER, put_secret, unlock_test_store

APP = QApplication.instance() or QApplication([])
SEED = "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"  # RFC 8032 test 1
PUB = "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"


def test_store_is_the_explicit_cryptfile_on_a_temp_file(tmp_path: Path) -> None:
    put_secret("llm_openai", "plain-value-xyz")
    backend = keyring.get_keyring()
    assert isinstance(backend, CryptFileKeyring)
    assert Path(backend.file_path) == s.SECRETS_PATH and s.SECRETS_PATH.is_relative_to(tmp_path)
    assert (s.SECRETS_PATH.stat().st_mode & 0o777) == 0o600
    assert b"plain-value-xyz" not in s.SECRETS_PATH.read_bytes()  # encrypted at rest
    assert s.get_secret("llm_openai") == "plain-value-xyz"


def test_guard_no_test_reached_a_real_backend() -> None:
    """Tripwire: keyring backend auto-detection (which would probe OS keyrings / DBus)
    never ran in this session, and no OS-keyring backend module was imported."""
    put_secret("llm_openai", "v")
    assert DETECTIONS == []
    for mod in ("keyring.backends.SecretService", "keyring.backends.kwallet", "secretstorage"):
        assert mod not in sys.modules, mod
    out = subprocess.run(
        [
            sys.executable,
            "-c",
            "import struck.secrets, struck.keys, keyring.core as c; print(c._keyring_backend)",
        ],
        capture_output=True,
        check=True,
        env={**os.environ, "PYTHONPATH": str(paths.REPO_ROOT)},
    ).stdout.decode()
    assert out.strip() == "None"  # importing Struck never selects/probes a backend


def test_locked_store_fails_loudly() -> None:
    with pytest.raises(s.SecretError, match="locked"):
        s.get_secret("llm_openai")


def test_missing_entry_fails_loudly_with_the_exact_command() -> None:
    s.unlock(TEST_MASTER)
    with pytest.raises(s.SecretError) as e:
        s.get_secret("llm_openai")
    msg = str(e.value)
    assert "'llm_openai'" in msg and "`uv run python -m struck.keys set llm_openai`" in msg


def test_wrong_master_password_fails_loudly() -> None:
    put_secret("llm_openai", "v")
    s.lock()
    with pytest.raises(s.SecretError, match="wrong master password.*did not decrypt"):
        s.unlock("not the password")
    assert not s.is_unlocked()
    with pytest.raises(s.SecretError, match="locked"):
        s.get_secret("llm_openai")
    with pytest.raises(s.SecretError, match="empty master"):
        s.unlock("   ")
    s.unlock(TEST_MASTER)
    assert s.get_secret("llm_openai") == "v"


def test_env_vars_are_ignored_even_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "OPENAI_API_KEY",
        "llm_openai",
        "KEYRING_PROPERTY_KEYRING_KEY",
        "KEYRING_CRYPTFILE_PASSWORD",
        "PYTHON_KEYRING_BACKEND",
        "STRUCK_MASTER_PASSWORD",
    ):
        monkeypatch.setenv(var, TEST_MASTER)
    with pytest.raises(s.SecretError, match="locked"):  # no env var unlocks or answers
        s.get_secret("llm_openai")
    s.unlock(TEST_MASTER)
    with pytest.raises(s.SecretError, match="not set"):
        s.get_secret("llm_openai")


def test_store_file_private_checks(tmp_path: Path) -> None:
    put_secret("llm_openai", "v")
    s.SECRETS_PATH.chmod(0o644)
    with pytest.raises(s.SecretError, match="must be 600"):
        s.get_secret("llm_openai")
    s.SECRETS_PATH.chmod(0o600)
    s.SECRETS_PATH.parent.chmod(0o770)
    with pytest.raises(s.SecretError, match="group/other-writable"):
        s.get_secret("llm_openai")
    s.SECRETS_PATH.parent.chmod(0o700)
    s.lock()
    real = tmp_path / "elsewhere.cfg"
    s.SECRETS_PATH.rename(real)
    s.SECRETS_PATH.symlink_to(real)
    with pytest.raises(s.SecretError, match="symlink"):
        s.unlock(TEST_MASTER)


def _terminal(platform: str = "linux") -> None:
    return None


def test_unlock_from_terminal_refuses_a_pipe_and_asks_twice_for_a_new_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(s.SecretError, match="real terminal"):
        s.unlock_from_terminal()  # pytest's stdin is not a TTY
    answers = iter(["one", "two"])

    def typed(prompt: str) -> str:
        return next(answers)

    monkeypatch.setattr(s, "require_terminal", _terminal)  # mock-only: a real TTY
    monkeypatch.setattr(getpass, "getpass", typed)
    with pytest.raises(s.SecretError, match="differ"):
        s.unlock_from_terminal()
    assert not s.SECRETS_PATH.exists()


def _tty(monkeypatch: pytest.MonkeyPatch, answers: list[str]) -> None:
    it = iter(answers)

    def typed(prompt: str) -> str:
        return next(it)

    monkeypatch.setattr(keys, "require_terminal", _terminal)  # mock-only: a real TTY
    monkeypatch.setattr(s, "require_terminal", _terminal)
    monkeypatch.setattr(getpass, "getpass", typed)


def test_keys_cli_refuses_a_non_terminal(capsys: pytest.CaptureFixture[str]) -> None:
    for argv in (["list"], ["set", "llm_openai"], ["delete", "llm_openai"]):
        assert keys.main(argv) == 2
        assert "REFUSED" in capsys.readouterr().err
    assert not s.SECRETS_PATH.exists()


def test_keys_cli_set_list_delete_never_print_values(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _tty(monkeypatch, [TEST_MASTER, TEST_MASTER, "typed-secret"])  # new store: twice + value
    assert keys.main(["set", "llm_openai"]) == 0
    assert s.get_secret("llm_openai") == "typed-secret"
    assert keys.main(["list"]) == 0
    out = capsys.readouterr()
    assert "typed-secret" not in out.out + out.err and TEST_MASTER not in out.out + out.err
    names = (*keys.ENTRIES, "strike_api_testnet", "strike_api_mainnet")
    rows = dict(ln.split() for ln in out.out.splitlines() if ln.split()[0] in names)
    assert rows["llm_openai"] == "present" and rows["strike_api_testnet"] == "missing"
    assert set(rows) == set(names) and s.STRIKE_PRIVATE not in keys.ENTRIES
    assert keys.main(["delete", "llm_openai"]) == 0 and not s.has_secret("llm_openai")
    assert keys.main(["delete", "llm_openai"]) == 3
    # delete_secret names the exact case (secrets.py): not set vs a real failure
    assert "is not set (nothing to delete)" in capsys.readouterr().err
    assert keys.main(["delete", "llm_typo"]) == 3
    assert "unknown secret entry" in capsys.readouterr().err


def test_import_strike_env_checks_prefix_stores_and_keeps_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env = tmp_path / "strike.env"
    env.write_text(
        f"export STRUCK_STRIKE_API_WALLET_PUBLIC_KEY={PUB}\n"
        f"STRUCK_STRIKE_API_WALLET_PRIVATE_KEY='{SEED}'  # comment\n",
        encoding="utf-8",
    )
    s.unlock(TEST_MASTER)
    # the registered prefix: this RFC 8032 test key does not match -> nothing stored
    assert keys.import_strike_env(env, "linux", keys.EXPECTED_STRIKE_PUBKEY_PREFIX, "testnet") == 4
    assert "expected e926f4ad; nothing stored" in capsys.readouterr().err
    assert not s.has_secret("strike_api_testnet")

    def enter(platform: str) -> str:
        return ""  # Enter = N

    monkeypatch.setattr(keys, "read_human_line", enter)
    assert keys.import_strike_env(env, "linux", PUB[:8], "testnet") == 0
    out = capsys.readouterr().out
    assert SEED not in out and PUB not in out and "kept" in out and env.exists()
    assert venue_auth.wallet_for("testnet").public_key_hex == PUB
    assert not s.has_secret("strike_api_mainnet")

    def yes(platform: str) -> str:
        return "y"

    monkeypatch.setattr(keys, "read_human_line", yes)
    assert keys.import_strike_env(env, "linux", PUB[:8], "mainnet") == 0
    assert not env.exists() and "shredded" in capsys.readouterr().out


def test_import_strike_env_refuses_a_mismatched_pair(tmp_path: Path) -> None:
    env = tmp_path / "strike.env"
    env.write_text(
        f"STRUCK_STRIKE_API_WALLET_PUBLIC_KEY={'00' * 32}\n"
        f"STRUCK_STRIKE_API_WALLET_PRIVATE_KEY={SEED}\n",
        encoding="utf-8",
    )
    s.unlock(TEST_MASTER)
    assert keys.import_strike_env(env, "linux", "00000000", "testnet") == 4
    assert not s.has_secret("strike_api_testnet") and env.exists()


def test_settings_fields_are_masked_and_cleared() -> None:
    panel = panels.SettingsPanel()
    assert panel.sources.text().startswith("SECRET STORE UNAVAILABLE")
    assert "locked" in panel.sources.text()
    for field in (panel.master, panel.secret_value):
        assert field.echoMode() == QLineEdit.EchoMode.Password and field.text() == ""
    put_secret("llm_anthropic", "x")  # creates the store with TEST_MASTER
    s.lock()
    panel.master.setText("wrong")
    panel.unlock_store()
    assert "STORE NOT UNLOCKED" in panel.secret_state.text()
    assert "wrong master" in panel.secret_state.text() and panel.master.text() == ""
    panel.master.setText(TEST_MASTER)
    panel.unlock_store()
    assert "STORE UNLOCKED" in panel.secret_state.text() and panel.master.text() == ""
    panel.secret_entry.setCurrentText("llm_openai")
    panel.secret_value.setText("ui-secret")
    panel.store_secret()
    assert panel.secret_value.text() == "" and s.get_secret("llm_openai") == "ui-secret"
    shown = panel.sources.text() + panel.secret_state.text() + panel.key_label.text()
    assert "ui-secret" not in shown and TEST_MASTER not in shown
    assert any(ln.split() == ["llm_openai", "present"] for ln in panel.sources.text().splitlines())
    panel.delete_secret()
    assert not s.has_secret("llm_openai") and "KEY DELETED" in panel.secret_state.text()
    panel.store_secret()  # empty value -> loud
    assert "KEY NOT STORED" in panel.secret_state.text()


# ── Sec Auditer round 9: (3) lock wipes, (4) corrupt store, (5) atomic writes ──


def test_lock_drops_the_master_from_the_backend_and_keyring() -> None:
    unlock_test_store()
    backend = s._State.backend
    assert backend is not None and keyring.get_keyring() is backend
    s.lock()
    assert backend._keyring_key is None  # the stock backend's only copy is gone
    assert s._State.backend is None and keyring.get_keyring() is not backend
    with pytest.raises(s.SecretError, match="locked"):
        s.get_secret("llm_openai")


@pytest.mark.parametrize(
    "garbage", [b"not an ini file\n", b"[a]\nb\n\xff\xfe", b"[keyring-setting]\n"]
)
def test_corrupt_store_raises_secret_error_not_configparser(garbage: bytes) -> None:
    unlock_test_store()
    s.set_secret("llm_openai", "sk-1")
    s.lock()
    s.SECRETS_PATH.write_bytes(garbage)
    s.SECRETS_PATH.chmod(0o600)
    with pytest.raises(s.SecretError):
        s.unlock(TEST_MASTER)


def test_corrupt_store_while_unlocked_raises_secret_error() -> None:
    unlock_test_store()
    s.set_secret("llm_openai", "sk-1")
    s.SECRETS_PATH.write_bytes(b"garbage without a section\n")
    for op in (
        lambda: s.get_secret("llm_openai"),
        lambda: s.has_secret("llm_openai"),
        lambda: s.set_secret("llm_openai", "sk-2"),
        lambda: s.delete_secret("llm_openai"),
    ):
        with pytest.raises(s.SecretError, match="could not be"):
            op()


def test_store_writes_are_atomic_and_private_from_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unlock_test_store()
    s.set_secret("llm_openai", "sk-1")
    before = s.SECRETS_PATH.read_bytes()
    replaced: list[tuple[str, int]] = []
    real_replace = os.replace

    def spy(src: str, dst: object) -> None:
        replaced.append((src, os.stat(src).st_mode & 0o777))
        real_replace(src, dst)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "replace", spy)
    s.set_secret("llm_anthropic", "sk-2")
    ((tmp, mode),) = replaced
    assert Path(tmp).parent == s.SECRETS_PATH.parent and mode == 0o600
    assert s.SECRETS_PATH.stat().st_mode & 0o777 == 0o600

    def crash(src: str, dst: object) -> None:
        raise OSError("power cut")

    monkeypatch.setattr(os, "replace", crash)
    middle = s.SECRETS_PATH.read_bytes()
    with pytest.raises(s.SecretError, match="power cut"):
        s.set_secret("llm_groq", "sk-3")
    assert s.SECRETS_PATH.read_bytes() == middle != before  # old content intact
    assert sorted(p.name for p in s.SECRETS_PATH.parent.iterdir()) == ["secrets.cfg"]
