"""Proof renders of the Struck UI (offscreen Qt), with LIVE Strike (selected network)
public data and the live trigger store. The Approval tab is run to the N3
approval interrupt and captured there: NOTHING is clicked or approved.

  QT_QPA_PLATFORM=offscreen uv run python -m struck.ui.screenshot_cli <out_dir> <strike-plan.json> [live_seconds=20] [symbol] [--network <name>]

The Market tab runs LIVE on the public price stream (/ws/price) for
live_seconds before the shots; per-event message counts go to call_history
(WS-COUNTS) and stdout.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from ..config import network_help, pop_network_flag, wants_help
from . import theme
from .app import StruckWindow


def main(argv: list[str]) -> int:
    if wants_help(argv):
        print(f"{__doc__}\n{network_help()}", file=sys.stderr)
        return 0
    argv = pop_network_flag(argv)
    if len(argv) not in (2, 3, 4):
        print(f"{__doc__}\n{network_help()}", file=sys.stderr)
        return 2
    out, plan = Path(argv[0]), Path(argv[1])
    live_s = int(argv[2]) if len(argv) >= 3 else 20
    out.mkdir(parents=True, exist_ok=False)
    app = QApplication(sys.argv[:1])
    app.setStyleSheet(theme.app_qss())
    w = StruckWindow()
    w.show()
    w.market.preferred_symbol = argv[3] if len(argv) == 4 else ""
    w.market.start()
    loop = QEventLoop()
    QTimer.singleShot(live_s * 1000, loop.quit)
    loop.exec()
    print(f"market badge: {w.market.badge.text()}  status: {w.market.status.text()}")
    print(f"public stream counts after {live_s}s: {w.market.stream.log_counts()}")
    print(
        f"depth rows {w.market.depth_model.rowCount()}  trade rows {w.market.trades_model.rowCount()}  candles {len(w.market._candles)}"
    )
    w.approval.load_plan(plan)
    w.approval._start()
    w.approval._show_approval("approval")
    print(f"chat dock: {w.chat.state.text()}")
    w.account.start()
    for i, name in enumerate(("market", "bots", "approval", "account", "settings")):
        w.tabs.setCurrentIndex(i)
        app.processEvents()
        path = out / f"{i}-{name}.png"
        if name == "approval":
            w.approval.plan_tabs.setCurrentIndex(1)
            app.processEvents()
            w.grab().save(str(out / "2b-approval-ticket.png"))
            w.approval.plan_tabs.setCurrentWidget(w.approval.onchain)
            app.processEvents()
            w.grab().save(str(out / "2c-approval-onchain-builder.png"))
            w.approval.plan_tabs.setCurrentIndex(0)
            app.processEvents()
        if not w.grab().save(str(path)):
            raise RuntimeError(f"could not save {path}")
        print(f"wrote {path}")
    print(f"approval tab: {w.approval.hash_label.text()}")
    print(
        f"approve button enabled for the HUMAN: {w.approval.approve_btn.isEnabled()} (not clicked)"
    )
    w.market.stream.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
