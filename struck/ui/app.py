"""Struck desktop app (spec D1/§7): Refractic Labs branding, three panels.

uv run python -m struck.ui.app
"""

from __future__ import annotations

import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDockWidget,
    QLabel,
    QMainWindow,
    QMessageBox,
    QTabWidget,
    QWidget,
)

from ..config import RECEIPTS_DIR, load_config, network_help, pop_network_flag, wants_help
from ..triggers.engine import TriggerStore
from ..plan.models import PlanProposal
from ..strike.deposit import DepositPlanProposal
from ..strike.models import StrikePlanProposal
from ..triggers.models import TriggerSpec
from ..wallets import WalletInfo
from . import theme
from .layout import LAYOUT
from .chat_panel import ChatPanel
from .deposit_form import DepositForm
from .deposit_approval import DepositApproval
from .liquidity import LiquidityPanel
from .registration import RegistrationDialog, registration_complete
from .reputation import ReputationPanel
from .strategies import StrategiesPanel
from .wallets import WalletsPanel
from .panels import (
    AccountPanel,
    ApprovalPanel,
    BotsPanel,
    MarketPanel,
    SettingsPanel,
    network_badge,
)


class StruckWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        net = load_config().profile
        self.setWindowTitle(f"Struck · Refractic Labs · {net.label}")
        self.resize(1400, 900)
        logo = QLabel()
        logo.setPixmap(QPixmap(str(theme.LOGO_PATH)).scaledToHeight(36))
        brand = QLabel("STRUCK")
        brand.setObjectName("brand")
        tag = QLabel("by Refractic Labs · human approval on every plan")
        tag.setObjectName("muted")
        header = LAYOUT.header([logo, brand, tag, network_badge(net)])
        self.market = MarketPanel(self.review_strategy)
        self.account = AccountPanel()
        self.bots = BotsPanel(TriggerStore(), self.account)
        self.approval = ApprovalPanel()
        self.strategies = StrategiesPanel(self.review_strategy)
        self.deposit_form = DepositForm(self.review_deposit)
        self.deposit_approval = DepositApproval()
        self.tabs = QTabWidget()
        self.tabs.addTab(self.market, "Market")
        self.tabs.addTab(self.bots, "Bots")
        self.tabs.addTab(self.strategies, "Strategies")
        self.tabs.addTab(self.approval, "Approval")
        self.tabs.addTab(self.deposit_form, "Deposit")
        self.tabs.addTab(self.deposit_approval, "Deposit approval")
        self.tabs.addTab(self.account, "Account")
        self.wallets = WalletsPanel(self.inspect_wallet)
        self.tabs.addTab(self.wallets, "Wallets")
        self.reputation = ReputationPanel()
        self.tabs.addTab(self.reputation, "Reputation")
        self.liquidity = LiquidityPanel()
        self.tabs.addTab(self.liquidity, "Liquidity")
        self.settings = SettingsPanel()
        self.tabs.addTab(self.settings, "Settings")
        root = QWidget()
        layout = LAYOUT.vbox()
        root.setLayout(layout)
        layout.addLayout(header)
        layout.addWidget(self.tabs, 1)
        self.setCentralWidget(root)
        # The chat dock is visible from every tab: THE plan-making interface.
        self.chat = ChatPanel(self.review_strike, self.review_trigger, self.review_onchain)
        self.chat_dock = QDockWidget("Chat", self)
        self.chat_dock.setObjectName("chat-dock")
        self.chat_dock.setWidget(self.chat)
        self.chat_dock.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QDockWidget.DockWidgetFeature.DockWidgetFloatable
        )
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.chat_dock)
        self.resizeDocks([self.chat_dock], [440], Qt.Orientation.Horizontal)
        self.settings.saved.connect(self.chat.refresh_state)
        self.settings.saved.connect(self.account.start)
        self.settings.saved.connect(self.wallets.refresh)
        self.settings.saved.connect(self.deposit_form.load)
        self.settings.saved.connect(self.strategies.load)
        self.wallets.wallets_changed.connect(self.deposit_form.load)

    def inspect_wallet(self, wallet: WalletInfo) -> None:
        panel = self.approval.onchain
        panel.from_address.setText(wallet.address)
        panel.refresh_wallets()
        panel.wallet_pick.setCurrentIndex(panel._wallet_names.index(wallet.name))
        self.approval.plan_tabs.setCurrentWidget(panel)
        self.tabs.setCurrentWidget(self.approval)
        panel.load_wallet()

    # Review = hand the proposal to the EXISTING path; the human decides there.
    def review_strike(self, plan: StrikePlanProposal) -> None:
        self.approval.use_chat_plan(plan)
        self.approval.plan_tabs.setCurrentIndex(0)
        self.tabs.setCurrentWidget(self.approval)

    def review_strategy(self, plan: StrikePlanProposal) -> None:
        """A native TWAP/grid proposal from the Strategies tab rides the SAME
        approval path as the order ticket (K01 wiring; the human approves)."""
        self.approval.use_ticket_plan(plan)
        self.approval.plan_tabs.setCurrentIndex(0)
        self.tabs.setCurrentWidget(self.approval)

    def review_trigger(self, spec: TriggerSpec) -> None:
        self.bots.load_proposed(spec)
        self.tabs.setCurrentWidget(self.bots)

    def review_onchain(self, plan: PlanProposal) -> None:
        self.approval.onchain.load_proposal(plan)
        self.approval.plan_tabs.setCurrentWidget(self.approval.onchain)
        self.tabs.setCurrentWidget(self.approval)

    def review_deposit(self, proposal: DepositPlanProposal) -> None:
        """A deposit proposal from the Deposit tab (K03 wiring). It is saved as
        the strict JSON the deposit CLI consumes and shown with the EXACT
        human-gated command — the quote/sign/confirm flow lives at the
        terminal (``struck.strike.deposit_cli``), never auto-run here."""
        out = RECEIPTS_DIR / "deposit-proposals" / f"{proposal.plan_hash[:16]}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(proposal.model_dump_json() + "\n", encoding="utf-8")
        self.deposit_approval.load_proposal(proposal)
        self.tabs.setCurrentWidget(self.deposit_approval)


def _config_problem() -> str | None:
    """Why ``load_config()`` refuses right now, or None when it does not (the
    first-run test: the UI's refusal is the registration dialogue, not an exit)."""
    try:
        load_config()
    except (ValueError, OSError) as exc:
        return str(exc)
    return None


def main() -> int:
    """``--network <name>`` overrides the saved setting for this run.

    First run (no endpoints file / no network selected): the REGISTRATION
    DIALOGUE asks for the network, the master password and the Strike API
    wallet IN the app — a person never needs a terminal. Cancelling it exits
    loudly: there is still no default network and no stored secret."""
    if wants_help(sys.argv[1:]):
        print(
            "usage: python -m struck.ui.app [--network <name>]\n" + network_help(), file=sys.stderr
        )
        return 0
    try:
        rest = pop_network_flag(sys.argv[1:], check_config=False)
    except SystemExit as exc:  # a bad --network value is a usage error, not first run
        print(
            f"{exc}\nusage: python -m struck.ui.app [--network <name>]\n{network_help()}",
            file=sys.stderr,
        )
        return 2
    if rest:
        print(
            f"unknown argument(s) {rest}\nusage: python -m struck.ui.app [--network <name>]\n{network_help()}",
            file=sys.stderr,
        )
        return 2
    argv = sys.argv[:1]
    app = QApplication.instance()
    if not isinstance(app, QApplication):
        app = QApplication(argv)
    app.setStyleSheet(theme.app_qss())
    problem = _config_problem()
    if problem is not None or not registration_complete():
        # A HALF-finished registration (a saved network but no API wallet)
        # re-opens the dialogue: only the private marker counts as done.
        dialog = RegistrationDialog(problem or "")
        if dialog.exec() != QDialog.DialogCode.Accepted:
            print(f"STRUCK REGISTRATION CANCELLED: {problem or 'registration not completed'}",
                  file=sys.stderr)
            return 2
        problem = _config_problem()  # the dialogue's writes must satisfy the config
        if problem is not None:
            QMessageBox.critical(None, "Struck registration incomplete", problem)
            print(f"STRUCK CONFIG REFUSED: {problem}", file=sys.stderr)
            return 2
    window = StruckWindow()
    window.show()
    window.market.start()
    window.account.start()
    window.strategies.load()
    window.deposit_form.load()
    window.liquidity.load()
    window.account.order_update.connect(window.approval.on_order_update)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
