"""Struck theme. QSS structure ported from the 
``ThemeManager`` @ 2d47b95dd; palette, typography and radii RE-BASED
2026-10-22 onto the extracted Struck brand pack (``design/brand/BRAND.md``,
exact tokens from ``design/dashboard.html``) by operator instruction: VOID
``#050505`` ground, CARD ``#0a0a0a`` surfaces, warm-white hairlines, SILVER
text ramp, CHAMPAGNE ``#d4b36b`` the only accent, Space Grotesk display /
Manrope body / IBM Plex Mono. Dropped from the port: service accents,
ripple/animation routers, icon loader. No blue anywhere; ``tests/test_ui_theme.py``
asserts it on every colour the theme emits.
"""

from __future__ import annotations

from pathlib import Path

LOGO_PATH = Path(__file__).resolve().parent / "assets" / "brand_logo.png"

BASE_COLORS: dict[str, str] = {
    "primary_bg": "#050505",  # VOID
    "secondary_bg": "#0a0a0a",  # CARD surface
    "card_bg": "#0a0a0a",  # CARD surface
    "accent_bg": "rgba(212, 179, 107, 0.14)",  # champagne-dim
    "text_primary": "#f0efec",  # hi
    "text_secondary": "#c0c3c8",  # silver
    "text_muted": "#9b9b9b",  # muted
    "text_disabled": "#656565",  # faint
    "border": "rgba(255, 252, 245, 0.085)",  # line
    "border_light": "#c0c3c8",  # silver
    "hover": "rgba(255, 252, 245, 0.06)",
    "selected": "#f0efec",
    "success": "rgba(88, 201, 138, 0.12)",
    "warning": "rgba(212, 179, 107, 0.14)",  # warning is champagne, never amber
    "error": "rgba(227, 123, 123, 0.12)",
    "info": "rgba(192, 195, 200, 0.08)",
    "accent_gold": "rgba(212, 179, 107, 0.14)",
    "accent_gold_line": "rgba(212, 179, 107, 0.55)",
}

FONT_DISPLAY = "'Space Grotesk', 'Manrope', system-ui, sans-serif"
FONT_PRIMARY = "'Manrope', 'Space Grotesk', system-ui, sans-serif"
FONT_MONO = "'IBM Plex Mono', 'SF Mono', ui-monospace, monospace"
FONT_XS, FONT_SM, FONT_BASE, FONT_MD, FONT_LG, FONT_XL, FONT_TITLE = 10, 11, 12, 13, 15, 18, 22

SEMANTIC: dict[str, dict[str, str]] = {
    "success": {
        "fg": "#58c98a",
        "bg": "rgba(88, 201, 138, 0.12)",
        "bg_hover": "rgba(88, 201, 138, 0.22)",
        "border": "rgba(88, 201, 138, 0.38)",
    },
    "danger": {
        "fg": "#e37b7b",
        "bg": "rgba(227, 123, 123, 0.12)",
        "bg_hover": "rgba(227, 123, 123, 0.22)",
        "border": "rgba(227, 123, 123, 0.38)",
    },
    "warning": {
        "fg": "#ecd29a",
        "bg": "rgba(212, 179, 107, 0.14)",
        "bg_hover": "rgba(212, 179, 107, 0.24)",
        "border": "rgba(212, 179, 107, 0.38)",
    },
    "info": {
        "fg": "#c0c3c8",
        "bg": "rgba(192, 195, 200, 0.08)",
        "bg_hover": "rgba(192, 195, 200, 0.16)",
        "border": "rgba(192, 195, 200, 0.28)",
    },
    "neutral": {
        "fg": "#9b9b9b",
        "bg": "rgba(255, 252, 245, 0.05)",
        "bg_hover": "rgba(255, 252, 245, 0.09)",
        "border": "rgba(255, 252, 245, 0.12)",
    },
    "accent": {
        "fg": "#ecd29a",
        "bg": "rgba(212, 179, 107, 0.20)",
        "bg_hover": "rgba(212, 179, 107, 0.32)",
        "border": "rgba(212, 179, 107, 0.55)",
    },
}


def palette() -> dict[str, str]:
    b, s = BASE_COLORS, SEMANTIC
    return {
        "white": b["text_primary"],
        "pearl": b["text_secondary"],
        "silver": b["border_light"],
        "platinum": b["text_muted"],
        "charcoal": b["text_disabled"],
        "onyx": b["card_bg"],
        "black": b["primary_bg"],
        "deep": b["primary_bg"],
        "border": b["border"],
        "hover": b["hover"],
        "green": s["success"]["fg"],
        "red": s["danger"]["fg"],
        "amber": s["warning"]["fg"],
        "cyan": s["info"]["fg"],
        "gold": "#d4b36b",
        "success": s["success"]["fg"],
        "danger": s["danger"]["fg"],
        "warning": s["warning"]["fg"],
        "info": s["info"]["fg"],
    }


def _btn_common(compact: bool) -> str:
    """Buttons: default = panel actions; compact = dialogs/professional rows."""
    pad = "5px 12px; font-size: 12px;" if compact else "9px 16px;"
    return (
        f"border-radius: 7px; padding: {pad} font-family: {FONT_DISPLAY};"
        "font-weight: 600; letter-spacing: 0.06em;"
    )


def button_qss(variant: str = "default", *, compact: bool = False) -> str:
    base = BASE_COLORS
    if variant in ("default", "neutral", "ghost"):
        return f"""
            QPushButton {{ background: rgba(5,5,5,0.55); color: {base["text_secondary"]};
                border: 1px solid {base["border"]}; {_btn_common(compact)} }}
            QPushButton:hover {{ background: {base["hover"]}; border-color: rgba(255,252,245,0.28); color: {base["text_primary"]}; }}
            QPushButton:pressed {{ background: rgba(255,252,245,0.04); }}
            QPushButton:disabled {{ color: {base["text_disabled"]}; border-color: {base["text_disabled"]}; background: transparent; }}
        """
    if variant in ("primary", "accent"):
        # The dashboard CTA: champagne metal gradient, near-black text, glow.
        return f"""
            QPushButton {{ background: qlineargradient(x1:0, y1:0, x2:0.25, y2:1,
                    stop:0 #ecd29a, stop:0.55 #d4b36b, stop:1 #b9974f);
                color: #171106; border: 1px solid rgba(212,179,107,0.7); {_btn_common(compact)} }}
            QPushButton:hover {{ border-color: #ecd29a; background: qlineargradient(x1:0, y1:0, x2:0.25, y2:1,
                    stop:0 #f2ddb0, stop:0.55 #dcbf7d, stop:1 #c2a058); }}
            QPushButton:pressed {{ background: #d4b36b; }}
            QPushButton:disabled {{ color: {base["text_disabled"]}; border-color: {base["text_disabled"]}; background: transparent; }}
        """
    sem = SEMANTIC[variant]
    return f"""
        QPushButton {{ background: {sem["bg"]}; color: {sem["fg"]}; border: 1px solid {sem["border"]}; {_btn_common(compact)} }}
        QPushButton:hover {{ background: {sem["bg_hover"]}; border-color: {sem["fg"]}; }}
        QPushButton:pressed {{ background: {sem["bg"]}; }}
        QPushButton:disabled {{ color: {base["text_disabled"]}; border-color: {base["text_disabled"]}; background: transparent; }}
    """


def card_qss() -> str:
    base = BASE_COLORS
    return f"""
        QGroupBox {{
            background: {base["card_bg"]};
            border: 1px solid {base["border"]}; border-radius: 10px; margin-top: 18px; padding: 16px 17px;
            font-family: {FONT_PRIMARY}; font-size: 13px; font-weight: 500;
            color: {base["text_secondary"]};
        }}
        QGroupBox::title {{ subcontrol-origin: margin; left: 14px; padding: 2px 10px; color: {base["text_primary"]};
            font-family: {FONT_DISPLAY}; font-weight: 600; letter-spacing: 0.14em; }}
    """


def table_qss() -> str:
    base, sem = BASE_COLORS, SEMANTIC["info"]
    return f"""
        QTableWidget, QTableView {{ background: {base["secondary_bg"]}; alternate-background-color: rgba(255,252,245,0.02);
            color: {base["text_secondary"]}; border: 1px solid {base["border"]}; border-radius: 8px;
            gridline-color: rgba(255,252,245,0.05); font-size: 12px; }}
        QTableWidget::item, QTableView::item {{ padding: 10px 12px; border: none; }}
        QTableWidget::item:selected, QTableView::item:selected {{ background: {sem["bg"]}; color: {base["text_primary"]}; }}
        QHeaderView::section {{ background: {base["card_bg"]}; color: {base["text_muted"]}; border: none;
            border-bottom: 1px solid {base["border"]}; padding: 10px 12px; font-family: {FONT_DISPLAY};
            font-weight: 600; letter-spacing: 0.12em; font-size: 11px; }}
        QTableCornerButton::section {{ background: {base["card_bg"]}; border: none; }}
    """


def input_qss() -> str:
    base = BASE_COLORS
    return f"""
        QLineEdit, QComboBox, QPlainTextEdit, QTextEdit {{ background: rgba(5,5,5,0.55); color: {base["text_secondary"]};
            border: 1px solid rgba(255,252,245,0.16); border-radius: 7px; padding: 8px 10px; font-size: 13px; }}
        QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus, QTextEdit:focus {{
            border-color: rgba(212,179,107,0.5); background: rgba(212,179,107,0.05); }}
    """


def app_qss() -> str:
    """Application stylesheet: window, tabs, labels, plus the widget QSS."""
    base = BASE_COLORS
    return (
        f"""
        QWidget {{ background: {base["primary_bg"]}; color: {base["text_secondary"]};
            font-family: {FONT_PRIMARY}; font-size: {FONT_BASE}px; }}
        QTabWidget::pane {{ border: 1px solid {base["border"]}; border-radius: 8px; }}
        QTabBar::tab {{ background: transparent; color: {base["text_muted"]}; padding: 10px 18px;
            font-family: {FONT_DISPLAY}; font-weight: 600;
            border-bottom: 2px solid transparent; letter-spacing: 0.12em; }}
        QTabBar::tab:selected {{ color: {base["text_primary"]}; border-bottom: 2px solid {base["accent_gold_line"]}; }}
        QLabel#brand {{ font-family: {FONT_DISPLAY}; font-size: {FONT_TITLE}px; font-weight: 700;
            color: {base["text_primary"]}; letter-spacing: 0.1em; }}
        QLabel#muted {{ color: {base["text_muted"]}; font-size: {FONT_SM}px; }}
        QLabel#wait {{ color: {SEMANTIC["danger"]["fg"]}; font-size: {FONT_LG}px; font-weight: 700; }}
        QLabel#cardlabel {{ font-family: {FONT_DISPLAY}; font-size: 10px; font-weight: 600;
            letter-spacing: 0.22em; color: {base["text_muted"]}; }}
        QPlainTextEdit#mono {{ font-family: {FONT_MONO}; font-size: {FONT_SM}px; }}
        """
        + button_qss()
        + card_qss()
        + table_qss()
        + input_qss()
    )
