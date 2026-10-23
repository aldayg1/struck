"""TEST-ONLY proof of the first-run registration dialogue (offscreen Qt).

  QT_QPA_PLATFORM=offscreen uv run python -m struck.testonly.registration_preview_cli <out_dir>

A THROWAWAY HOME is created and set BEFORE any struck import, so per-user paths
(the network setting, the encrypted secret store, the registration marker)
resolve inside it, and the dialogue is given a throwaway endpoints path:
nothing touches the real per-user config, the real secret store, the real
marker or the repo's config/struck.json (all byte-compared before/after and
asserted unchanged). The dialogue is driven the way a person drives it — ONE
page, every field visible: pick preprod in the combo, type the master
password, GENERATE a Strike testnet API wallet in-dialogue, Register. Also
proves the gate: a saved network alone is NOT completion (no marker -> the
dialogue is still the first thing). One PNG per state lands in <out_dir>. No
venue call is made here (Verify with venue is the dialogue's own live check);
no plan, order, send or deposit exists in this path.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


def _state(paths: dict[Path, bool]) -> dict[Path, bytes]:
    out: dict[Path, bytes] = {}
    for path, recursive in paths.items():
        files = sorted(path.rglob("*")) if recursive and path.is_dir() else [path]
        for f in files:
            if f.is_file():
                out[f] = f.read_bytes()
    return out


def main() -> int:
    if len(sys.argv) != 2 or "-h" in sys.argv or "--help" in sys.argv:
        print(__doc__, file=sys.stderr)
        return 2
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    repo_config = Path(__file__).resolve().parents[2] / "config" / "struck.json"
    real_user = Path.home() / ".config" / "struck"
    before = _state({repo_config: False, real_user: True})
    home = Path(tempfile.mkdtemp(prefix="struck-reg-proof-"))
    os.environ["HOME"] = str(home)  # BEFORE the struck imports: per-user paths resolve here

    from PySide6.QtWidgets import QApplication, QDialog, QLineEdit, QPushButton

    from struck.config import load_config
    from struck.network import load_network_setting, profile, save_network_setting
    from struck.secrets import SECRETS_PATH, has_secret, is_unlocked
    from struck.ui import theme
    from struck.ui.registration import REGISTRATION_PATH, RegistrationDialog, registration_complete

    tmp_cfg = home / "repo-config" / "struck.json"
    try:
        load_config(tmp_cfg)
        print("PROOF FAILED: expected the fresh-machine config refusal", file=sys.stderr)
        return 1
    except (ValueError, OSError) as exc:
        reason = str(exc)
    print(f"startup reason (shown muted at the bottom of the dialogue): {reason[:90]}…")

    app = QApplication(sys.argv[:1])
    app.setStyleSheet(theme.app_qss())
    dlg = RegistrationDialog(reason, config_path=tmp_cfg)
    if registration_complete():
        print("PROOF FAILED: fresh machine claims registration complete", file=sys.stderr)
        return 1

    def shot(name: str) -> None:
        app.processEvents()
        path = out / f"{name}.png"
        if not dlg.grab().save(str(path)):
            raise RuntimeError(f"could not save {path}")
        print(f"wrote {path}")

    def button(object_name: str) -> QPushButton:
        b = dlg.findChild(QPushButton, object_name)
        assert b is not None, object_name
        return b

    def field(object_name: str) -> QLineEdit:
        f = dlg.findChild(QLineEdit, object_name)
        assert f is not None, object_name
        return f

    # ONE page: config card (file missing), network combo, password, API card.
    shot("1-fresh-page")
    if dlg.findChild(QPushButton, "generate_strike") is not None:
        print("PROOF FAILED: the API form must not exist before a network is picked", file=sys.stderr)
        return 1
    if field("reg_pw").isHidden() or field("reg_pw2").isHidden():
        print("PROOF FAILED: password (and repeat, new store) must be on the single page", file=sys.stderr)
        return 1
    print("single page: password fields and API card are on screen from the start")

    # The saved-network-alone state is NOT completion: gate re-check.
    save_network_setting("preprod")
    if registration_complete():
        print("PROOF FAILED: a saved network must not count as completed registration", file=sys.stderr)
        return 1
    print("gate: saved network alone is not completion (no marker)")

    # Pick preprod in the combo -> saved at once, API card builds.
    dlg.network.setCurrentIndex(dlg.network.findText(profile("preprod").label))
    app.processEvents()
    if load_network_setting() != "preprod":
        print("PROOF FAILED: the combo pick did not save the network", file=sys.stderr)
        return 1
    if dlg._form is None:
        print("PROOF FAILED: the API form did not build on the network pick", file=sys.stderr)
        return 1
    print(f"network picked + saved: {load_network_setting()}; API fields are on screen")
    shot("2-network-picked")

    # Master password + Generate the key pair IN the dialogue (one page).
    field("reg_pw").setText("proof-master-password")
    field("reg_pw2").setText("proof-master-password")
    if not button("reg_register").isEnabled():
        print("PROOF FAILED: Register must be clickable (validation happens on click)", file=sys.stderr)
        return 1
    shot("3-fields-filled")
    button("reg_unlock").click()
    if not is_unlocked():
        print(f"PROOF FAILED: store not unlocked ({dlg._master_status.text()})", file=sys.stderr)
        return 1
    print(f"store created via the page's own button: {SECRETS_PATH}")
    dlg._form.label.setText("proof-account")
    button("generate_strike").click()
    if not has_secret("strike_api_testnet"):
        print("PROOF FAILED: generated key pair not stored", file=sys.stderr)
        return 1
    print(f"Strike testnet API wallet generated IN the dialogue: {dlg._form.stored.text()}")
    shot("4-generated")

    # Register -> store unlocked, endpoints written, marker present, accepted.
    button("reg_register").click()
    if dlg.result() != QDialog.DialogCode.Accepted:
        print(f"PROOF FAILED: Register did not finish ({dlg.status.text()})", file=sys.stderr)
        return 1
    if not is_unlocked():
        print("PROOF FAILED: the store is not unlocked after Register", file=sys.stderr)
        return 1
    if not tmp_cfg.is_file():
        print("PROOF FAILED: Register did not write the missing endpoints file", file=sys.stderr)
        return 1
    if not SECRETS_PATH.is_file() or SECRETS_PATH.stat().st_mode & 0o777 != 0o600:
        print(f"PROOF FAILED: store {SECRETS_PATH} missing or not 0600", file=sys.stderr)
        return 1
    if not REGISTRATION_PATH.is_file() or not registration_complete():
        print("PROOF FAILED: the completion marker is missing", file=sys.stderr)
        return 1
    print(f"store {SECRETS_PATH} (0600), marker {REGISTRATION_PATH}")
    cfg = load_config(tmp_cfg)
    print(f"load_config after registration: network={cfg.network} backend={cfg.chain_backend}")
    if cfg.network != "preprod" or cfg.chain_backend != "koios":
        print("PROOF FAILED: the registered config does not resolve", file=sys.stderr)
        return 1

    # START OVER (the retry path): confirm stubbed Yes -> store deleted, card
    # flips to create mode, API card hides; then a FULL re-registration works.
    import struck.ui.registration as reg_mod
    from PySide6.QtWidgets import QMessageBox, QWidget

    def _yes(parent: QWidget) -> QMessageBox.StandardButton:
        return QMessageBox.StandardButton.Yes

    reg_mod._confirm_delete_store = _yes
    button("reg_start_over").click()
    app.processEvents()
    if SECRETS_PATH.exists() or field("reg_pw2").isHidden():
        print("PROOF FAILED: start over did not delete the store / flip to create mode", file=sys.stderr)
        return 1
    if not dlg._api_host.isHidden():
        print("PROOF FAILED: the API card must hide with the deleted store", file=sys.stderr)
        return 1
    print("start over: store deleted, create mode, API card waits for the store")
    shot("5-started-over")
    field("reg_pw").setText("proof-master-password-2")
    field("reg_pw2").setText("proof-master-password-2")
    button("reg_unlock").click()
    if not is_unlocked() or dlg.findChild(QPushButton, "generate_strike") is None:
        print(f"PROOF FAILED: re-create store failed ({dlg._master_status.text()})", file=sys.stderr)
        return 1
    assert dlg._form is not None
    dlg._form.label.setText("proof-account-2")
    button("generate_strike").click()
    if not has_secret("strike_api_testnet"):
        print("PROOF FAILED: re-generation after start over failed", file=sys.stderr)
        return 1
    button("reg_register").click()
    if dlg.result() != QDialog.DialogCode.Accepted or not registration_complete():
        print(f"PROOF FAILED: re-registration failed ({dlg.status.text()})", file=sys.stderr)
        return 1
    print("re-registered after start over: new store, new API wallet, marker rewritten")

    after = _state({repo_config: False, real_user: True})
    if after != before:
        print("PROOF FAILED: real files were touched", file=sys.stderr)
        return 1
    print("real repo config and real per-user files untouched")
    print(f"dialogue size: {dlg.width()}x{dlg.height()}")
    print(f"PROOF OK (throwaway HOME {home}; screenshots in {out})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
