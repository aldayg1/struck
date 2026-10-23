"""Canonical layouts: the ``LayoutManager`` class — the geometry counterpart
of ``struck.ui.theme`` (operator instruction 2026-10-22: "a layout factory
like the thememanager", as a CLASS).

Theme owns colors and type; ``LayoutManager`` owns EVERYTHING geometric —
spacing, margins, card padding, button sizing, stacking and the
never-truncate scroll. ONE instance is canonical app-wide: ``LAYOUT`` (import
it, use its methods and tokens). No caller invents its own margins or spacing
numbers. The grid is 4px: every default below is a multiple of 4.
"""

from __future__ import annotations

from typing import Final

from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from . import theme


class LayoutManager:
    """THE canonical layout authority. One instance: ``LAYOUT``."""

    # 4px grid tokens
    GAP: Final = 8  # standard spacing between siblings
    GAP_TIGHT: Final = 4  # inside a row (label + field)
    CARD_GAP: Final = 12  # between cards on a page
    MARGIN: Final = 0  # page-level margin: cards carry their own padding

    MIN_DIALOG_WIDTH: Final = 720
    MAX_SCREEN_FRACTION: Final = 0.88  # a dialogue never covers the whole screen

    def vbox(self, spacing: int = GAP, margin: int = MARGIN) -> QVBoxLayout:
        box = QVBoxLayout()
        box.setSpacing(spacing)
        box.setContentsMargins(margin, margin, margin, margin)
        return box

    def hbox(self, spacing: int = GAP, margin: int = MARGIN) -> QHBoxLayout:
        box = QHBoxLayout()
        box.setSpacing(spacing)
        box.setContentsMargins(margin, margin, margin, margin)
        return box

    def form(self, spacing: int = GAP, margin: int = MARGIN) -> QFormLayout:
        box = QFormLayout()
        box.setSpacing(spacing)
        box.setContentsMargins(margin, margin, margin, margin)
        return box

    def card(self, title: str, spacing: int = GAP) -> tuple[QGroupBox, QVBoxLayout]:
        """A themed card (QGroupBox) with its canonical inner layout."""
        box = QGroupBox(title)
        inner = self.vbox(spacing)
        box.setLayout(inner)
        return box, inner

    def button(
        self,
        text: str,
        variant: str = "default",
        *,
        object_name: str | None = None,
        compact: bool = True,
    ) -> QPushButton:
        """A themed button; COMPACT is the professional dialog size (the
        default here) — panels keep the larger ``panels._button``."""
        b = QPushButton(text)
        if object_name is not None:
            b.setObjectName(object_name)
        b.setStyleSheet(theme.button_qss(variant, compact=compact))
        return b

    def header(self, widgets: list[QWidget]) -> QHBoxLayout:
        """The canonical brand/identity row: tight gaps, stretched to the right."""
        row = self.hbox(self.GAP_TIGHT)
        for w in widgets:
            row.addWidget(w)
        row.addStretch(1)
        return row

    def page(self, cards: list[QWidget], spacing: int = CARD_GAP) -> tuple[QWidget, QVBoxLayout]:
        """A standard page: the given cards top-to-bottom, canonical gaps.
        Returns the page widget and its column (for trailing rows)."""
        holder = QWidget()
        column = self.vbox(spacing)
        for c in cards:
            column.addWidget(c)
        holder.setLayout(column)
        return holder, column

    def scroll(self, content: QWidget) -> QScrollArea:
        """Content that must never truncate: scrolls when it does not fit."""
        area = QScrollArea()
        area.setWidget(content)
        area.setWidgetResizable(True)
        area.setFrameShape(QScrollArea.Shape.NoFrame)
        area.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        return area

    def dialog_row(self, accept: QPushButton, cancel: QPushButton) -> QHBoxLayout:
        """Standard dialog button order: [cancel] [accept] right-aligned."""
        row = self.hbox(self.GAP_TIGHT)
        row.addStretch(1)
        row.addWidget(cancel)
        row.addWidget(accept)
        return row

    def fit_dialog(self, dlg: QDialog, width: int = MIN_DIALOG_WIDTH) -> None:
        """Size a dialogue to its content, capped at MAX_SCREEN_FRACTION of the
        primary screen (with the scroll wrapper, tall content scrolls instead
        of being cut off)."""
        screen = QGuiApplication.primaryScreen()
        available = screen.availableGeometry() if screen is not None else dlg.frameGeometry()
        hint = dlg.sizeHint()
        height = min(hint.height(), int(available.height() * self.MAX_SCREEN_FRACTION))
        dlg.resize(
            max(width, min(hint.width(), int(available.width() * self.MAX_SCREEN_FRACTION))),
            height,
        )


LAYOUT = LayoutManager()
