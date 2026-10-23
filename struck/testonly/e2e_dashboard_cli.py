"""TEST-ONLY end-to-end proof: register AS A USER, then the LIVE dashboard.

  QT_QPA_PLATFORM=offscreen uv run python -m struck.testonly.e2e_dashboard_cli \
      <out_dir> --secrets <0600 file: master password / public key / private key>

The whole point is the operator's E2E demand: a person starts the app, enters
a password (preprod), registers the Strike API wallet, and THE DASHBOARD WORKS
— everything serving live data. The secrets file is typed/pasted by the human
running the proof (never a repo file, never echoed, never logged); this driver
only TYPES those values into the real widgets, exactly like a person. No key
literal exists in source. No plan, order, send or deposit exists in this path;
the only authenticated calls are the read-only ones the dashboard itself makes
(user websocket logon + REST resync), explicitly ordered by the operator for
this validation.

Default mode creates a THROWAWAY HOME before any struck import (the real
per-user config/store/marker are byte-compared before/after and asserted
untouched) and a throwaway endpoints file for the registration dialogue.
``--home real`` runs against the REAL per-user config instead (the operator's
own machine flow): no throwaway, no untouched assertion; the driver may unlock
the real store with the supplied master password and, if registration is
incomplete, complete it through the same dialogue.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

USAGE = "usage: e2e_dashboard_cli <out_dir> --secrets <file> [--home real] [--timeout 90]"


def _state(paths: dict[Path, bool]) -> dict[Path, bytes]:
    out: dict[Path, bytes] = {}
    for path, recursive in paths.items():
        files = sorted(path.rglob("*")) if recursive and path.is_dir() else [path]
        for f in files:
            if f.is_file():
                out[f] = f.read_bytes()
    return out


def _read_secrets(path: Path) -> tuple[str, str, str]:
    """(master password, public key, private key). Keys may be empty ONLY when
    the store already holds the account. Values are never printed."""
    if not path.is_file():
        raise ValueError(f"secrets file {path} is missing")
    if path.stat().st_mode & 0o777 != 0o600:
        raise ValueError(f"secrets file {path} must be mode 0600")
    lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]
    if len(lines) < 3:
        raise ValueError(f"secrets file {path} needs 3 lines: master / public / private")
    master, pub, priv = lines[0], lines[1], lines[2]
    if not master:
        raise ValueError("master password line is empty")
    if bool(pub) != bool(priv):
        raise ValueError("public and private key lines must be given together")
    return master, pub, priv


def main() -> int:
    out_dir: Path | None = None
    secrets_path: Path | None = None
    real_home = False
    timeout_s = 90.0
    it = iter(sys.argv[1:])
    for arg in it:
        if arg == "--secrets":
            value = next(it, None)
            if value is None:
                print(USAGE, file=sys.stderr)
                return 2
            secrets_path = Path(value)
        elif arg == "--home":
            value = next(it, None)
            if value != "real" or value is None:
                print(USAGE, file=sys.stderr)
                return 2
            real_home = True
        elif arg == "--timeout":
            value = next(it, None)
            if value is None or not value.isdigit():
                print(USAGE, file=sys.stderr)
                return 2
            timeout_s = float(value)
        elif out_dir is None:
            out_dir = Path(arg)
        else:
            print(USAGE, file=sys.stderr)
            return 2
    if out_dir is None or secrets_path is None:
        print(USAGE, file=sys.stderr)
        return 2
    try:
        master, pub, priv = _read_secrets(secrets_path)
    except ValueError as exc:
        print(f"PROOF FAILED: {exc}", file=sys.stderr)
        return 1
    out_dir.mkdir(parents=True, exist_ok=True)

    home: Path | None = None
    repo_config = Path(__file__).resolve().parents[2] / "config" / "struck.json"
    real_user = Path.home() / ".config" / "struck"
    before: dict[Path, bytes] = {}
    if not real_home:
        before = _state({repo_config: False, real_user: True})
        home = Path(tempfile.mkdtemp(prefix="struck-e2e-proof-"))
        os.environ["HOME"] = str(home)  # BEFORE struck imports: per-user paths move

    from PySide6.QtWidgets import QApplication, QDialog, QLineEdit, QPushButton, QWidget

    from struck.config import load_config
    from struck.network import profile
    from struck.secrets import has_secret, is_unlocked
    from struck.strike.venue_auth import entry_for
    from struck.ui import theme
    from struck.ui.app import StruckWindow, _config_problem
    from struck.ui.registration import RegistrationDialog, registration_complete

    app = QApplication(sys.argv[:1])
    app.setStyleSheet(theme.app_qss())

    def shot(widget: QWidget, name: str) -> None:
        app.processEvents()
        path = out_dir / f"{name}.png"
        if not widget.grab().save(str(path)):
            raise RuntimeError(f"could not save {path}")
        print(f"wrote {path}")

    def button(parent: QWidget, object_name: str) -> QPushButton:
        b = parent.findChild(QPushButton, object_name)
        if b is None:
            raise RuntimeError(f"button {object_name} not found")
        return b

    def field(parent: QWidget, object_name: str) -> QLineEdit:
        f = parent.findChild(QLineEdit, object_name)
        if f is None:
            raise RuntimeError(f"field {object_name} not found")
        return f

    def wait_live(deadline_s: float) -> tuple[str, str]:
        """Pump Qt until market AND account report live (or the deadline)."""
        deadline = time.monotonic() + deadline_s
        while time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.05)
            market = window.market.badge.text()
            account = window.account.badge.text()
            market_live = market == "[LIVE]"
            account_live = account == "[LIVE]"
            if market_live and account_live and "account" in window.account.account.keys():
                return market, account
        return window.market.badge.text(), window.account.badge.text()

    # ---- phase 1: the registration gate, driven exactly like a person ----------
    problem = _config_problem()
    if problem is not None or not registration_complete():
        config_path = (home / "repo-config" / "struck.json") if home is not None else repo_config
        dialog = RegistrationDialog(problem or "", config_path=config_path)
        shot(dialog, "1-registration-page")
        dialog.network.setCurrentIndex(dialog.network.findText(profile("preprod").label))
        app.processEvents()
        field(dialog, "reg_pw").setText(master)
        if not field(dialog, "reg_pw2").isHidden():  # fresh store: the repeat field
            field(dialog, "reg_pw2").setText(master)
        button(dialog, "reg_unlock").click()
        app.processEvents()
        if not is_unlocked():
            print(f"PROOF FAILED: store not unlocked ({dialog._master_status.text()})",
                  file=sys.stderr)
            return 1
        form = dialog._form
        if form is None:
            print("PROOF FAILED: the API form did not build on the preprod pick", file=sys.stderr)
            return 1
        if not has_secret(entry_for(form.environment)):
            if not pub or not priv:
                print("PROOF FAILED: no stored API wallet and no keys supplied", file=sys.stderr)
                return 1
            form.public_key.setText(pub)  # the human's paste, typed into the widget
            form.private_key.setText(priv)
            form.label.setText("operator-preprod")
        shot(dialog, "2-registration-filled")
        button(dialog, "reg_register").click()
        app.processEvents()
        if dialog.result() != QDialog.DialogCode.Accepted:
            print(f"PROOF FAILED: Register did not finish ({dialog.status.text()})", file=sys.stderr)
            return 1
        if not registration_complete() or not is_unlocked():
            print("PROOF FAILED: registration marker/store state wrong after Register",
                  file=sys.stderr)
            return 1
        print("registered through the dialogue: network saved, store open, API wallet stored")
    elif not is_unlocked():
        # A returning registered user unlocks in the Settings tab, driven below.
        print("already registered; the locked store unlocks through the Settings tab below")
    cfg = load_config()
    print(f"config resolves: network={cfg.network} backend={cfg.chain_backend}")

    # ---- phase 2: the dashboard, started exactly like struck.ui.app.main() ----
    window = StruckWindow()
    window.show()
    if not is_unlocked():
        # main() can open with a locked store; the returning user unlocks in
        # Settings. Drive THAT path too (the masked field + button).
        window.tabs.setCurrentWidget(window.settings)
        field(window.settings, "master_password").setText(master)
        button(window.settings, "unlock_store").click()
        app.processEvents()
        if not is_unlocked():
            print("PROOF FAILED: Settings unlock did not open the store", file=sys.stderr)
            return 1
        window.account.start()
    window.market.start()
    window.account.start()
    window.strategies.load()
    window.liquidity.load()
    window.account.order_update.connect(window.approval.on_order_update)

    market_badge, account_badge = wait_live(timeout_s)
    time.sleep(2.0)  # let ticks/trades accumulate for the screenshots
    app.processEvents()

    # ---- assertions on LIVE data ----------------------------------------------
    failures: list[str] = []
    if market_badge != "[LIVE]":
        failures.append(f"market websocket not LIVE: {market_badge!r}")
    if account_badge != "[LIVE]":
        failures.append(f"account user-stream not live: {account_badge!r}")
    if "account" not in window.account.account.keys():
        failures.append("account REST snapshot has not completed")
    if window.market.symbol.count() < 1:
        failures.append("symbol selector has no served markets")
    if len(window.market._candles) < 2:
        failures.append("chart has no candles")
    if window.market.chart._t != [row[0] / 1000 for row in window.market._candles]:
        failures.append("rendered chart does not match the current candle history")
    if window.market.markets_table.rowCount() < 1:
        failures.append("markets table is empty")
    print(f"market badge: {market_badge}")
    print(f"account badge: {account_badge}")
    print(f"served symbols in the selector: {window.market.symbol.count()}")
    print(f"candles drawn: {len(window.market._candles)}")
    print(f"account rows: {window.account.account.keys()}")
    print(f"position rows: {window.account.positions.keys()}")
    print(f"order rows: {window.account.orders.keys()}")

    for i in range(window.tabs.count()):
        name = window.tabs.tabText(i).lower()
        tab = window.tabs.widget(i)
        if tab is None:
            failures.append(f"tab {name} has no widget")
            continue
        window.tabs.setCurrentWidget(tab)
        app.processEvents()
        time.sleep(0.3)
        app.processEvents()
        shot(window, f"3-tab-{name}")

    if not real_home:
        after = _state({repo_config: False, real_user: True})
        if after != before:
            failures.append("real repo config or real per-user files were touched")
        else:
            print("real repo config and real per-user files untouched")

    # ---- teardown: stop live streams/timers so no QThread outlives the proof --
    window.market._resync_timer.stop()
    window.market.stream.close()
    if window.account._stream is not None:
        window.account._stream.close()
    for worker in window.market._workers:
        worker.wait(10_000)
    if window.account._clock_worker is not None:
        window.account._clock_worker.wait(10_000)
    window.close()
    stop = time.monotonic() + 3.0
    while time.monotonic() < stop:
        app.processEvents()
        time.sleep(0.05)

    if failures:
        for f in failures:
            print(f"PROOF FAILED: {f}", file=sys.stderr)
        return 1
    mode = "real HOME" if real_home else f"throwaway HOME {home}"
    print(f"PROOF OK ({mode}; screenshots in {out_dir})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
