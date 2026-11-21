"""The look of the app: colour tokens, one application stylesheet, button roles.

Widgets ask for a style by role instead of hard-coding colours:

    set_role(button, "primary")     the main action of a panel (Connect, Capture, Run)
    set_role(button, "danger")      All outputs OFF
    set_role(button, "recording")   a recorder that is running
    set_role(button, "")            back to normal

The instrument colours (volts amber, amps blue, ...) stay in widgets.py.
"""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QApplication, QWidget

ICONS = Path(__file__).resolve().parent / "icons"

BG = "#0f1115"          # window
SURFACE = "#171a20"     # cards: group boxes, the top bar
RAISED = "#20242c"      # buttons
FIELD = "#0b0d11"       # inputs, tables, plots
LINE = "#2a2f38"
LINE_HI = "#3a414d"
TEXT = "#dfe3ea"
MUTED = "#8a93a0"
DIM = "#5d6570"
ACCENT = "#4c9aff"
ACCENT_DIM = "#1b2f4d"
DANGER = "#e5534b"
PLOT_BG = FIELD


def _icon(name: str) -> str:
    return (ICONS / name).as_posix()


def stylesheet() -> str:
    return f"""
    QWidget {{ color:{TEXT}; }}
    QMainWindow, QDialog {{ background:{BG}; }}
    QToolTip {{ background:{RAISED}; color:{TEXT}; border:1px solid {LINE_HI};
                border-radius:6px; padding:6px 8px; }}
    QLabel {{ background:transparent; }}

    /* cards */
    QGroupBox {{ background:{SURFACE}; border:1px solid {LINE}; border-radius:10px;
                 margin-top:20px; padding:12px 10px 10px 10px; font-weight:600; }}
    QGroupBox::title {{ subcontrol-origin:margin; subcontrol-position:top left; left:12px;
                        top:2px; padding:0 2px; color:{MUTED}; }}
    QFrame#bar {{ background:{SURFACE}; border:1px solid {LINE}; border-radius:10px; }}

    /* buttons */
    QPushButton, QToolButton {{ background:{RAISED}; border:1px solid {LINE};
                                border-radius:6px; padding:5px 12px; }}
    QPushButton:hover, QToolButton:hover {{ border-color:{LINE_HI}; background:#262b34; }}
    QPushButton:pressed, QToolButton:pressed {{ background:#181b21; }}
    QPushButton::menu-indicator {{ image:none; width:0; }}
    QPushButton:checked {{ background:{ACCENT_DIM}; border-color:{ACCENT}; color:#ffffff; }}
    QPushButton:disabled, QToolButton:disabled {{ color:{DIM}; background:#181b20;
                                                 border-color:#22262d; }}
    QPushButton[role="primary"] {{ background:{ACCENT}; border-color:{ACCENT}; color:#08111f;
                                   font-weight:700; }}
    QPushButton[role="primary"]:hover {{ background:#6aacff; }}
    QPushButton[role="primary"]:disabled {{ background:#24364f; border-color:#24364f;
                                            color:#6f86a5; }}
    QPushButton[role="danger"] {{ background:#2a1416; border-color:#6e2a2a; color:#ff8a80;
                                  font-weight:700; }}
    QPushButton[role="danger"]:hover {{ background:#45191b; border-color:{DANGER}; }}
    QPushButton[role="danger-solid"] {{ background:#b3261e; border-color:#b3261e;
                                        color:#ffffff; font-weight:700; }}
    QPushButton[role="danger-solid"]:hover {{ background:#cc3128; }}
    QPushButton[role="recording"] {{ background:#3a1517; border-color:{DANGER};
                                     color:#ff8a80; font-weight:700; }}

    /* inputs */
    QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit, QTextEdit {{
        background:{FIELD}; border:1px solid {LINE}; border-radius:6px; padding:3px 6px;
        selection-background-color:{ACCENT}; selection-color:#08111f; }}
    QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus,
    QPlainTextEdit:focus {{ border-color:{ACCENT}; }}
    QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled, QComboBox:disabled {{
        color:{DIM}; }}
    QSpinBox::up-button, QDoubleSpinBox::up-button, QSpinBox::down-button,
    QDoubleSpinBox::down-button {{ border:none; background:transparent; width:16px; }}
    QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{ image:url({_icon('up.svg')});
                                                   width:10px; height:10px; }}
    QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{ image:url({_icon('down.svg')});
                                                       width:10px; height:10px; }}
    QComboBox::drop-down {{ border:none; width:22px; }}
    QComboBox::down-arrow {{ image:url({_icon('down.svg')}); width:12px; height:12px; }}
    QComboBox QAbstractItemView {{ background:{RAISED}; border:1px solid {LINE_HI};
        selection-background-color:{ACCENT_DIM}; selection-color:#ffffff; outline:0; }}

    QCheckBox {{ spacing:8px; background:transparent; }}
    QCheckBox::indicator {{ width:16px; height:16px; border:1px solid {LINE_HI};
                            border-radius:4px; background:{FIELD}; }}
    QCheckBox::indicator:hover {{ border-color:{ACCENT}; }}
    QCheckBox::indicator:checked {{ background:{ACCENT}; border-color:{ACCENT};
                                    image:url({_icon('check.svg')}); }}
    QCheckBox::indicator:disabled {{ background:#15181d; border-color:#22262d; }}

    /* tabs: text with an accent underline, no boxes */
    QTabWidget::pane {{ border:none; border-top:1px solid {LINE}; top:-1px; }}
    QTabBar {{ qproperty-drawBase:0; background:transparent; }}
    QTabBar::tab {{ background:transparent; color:{MUTED}; border:none;
                    border-bottom:2px solid transparent; padding:7px 14px; margin-right:2px; }}
    QTabBar::tab:hover {{ color:{TEXT}; }}
    QTabBar::tab:selected {{ color:{TEXT}; border-bottom:2px solid {ACCENT}; }}
    QTabWidget#instruments > QTabBar::tab {{ font-weight:600; padding:8px 16px; }}
    QTabWidget#instruments > QTabBar::tab:selected {{ background:{SURFACE};
        border-top-left-radius:8px; border-top-right-radius:8px; }}
    QTabBar::close-button {{ image:url({_icon('close.svg')}); subcontrol-position:right; }}
    QTabBar::close-button:hover {{ image:url({_icon('close-hover.svg')}); }}

    /* tables, lists */
    QTableWidget, QTableView, QListWidget, QTreeWidget {{ background:{FIELD};
        border:1px solid {LINE}; border-radius:6px; gridline-color:#1a1e25;
        selection-background-color:{ACCENT_DIM}; selection-color:#ffffff;
        alternate-background-color:#101318; }}
    QHeaderView {{ background:transparent; }}
    QHeaderView::section {{ background:{SURFACE}; color:{MUTED}; border:none;
        border-bottom:1px solid {LINE}; border-right:1px solid #1d2128; padding:4px 8px;
        font-weight:600; }}
    QTableCornerButton::section {{ background:{SURFACE}; border:none; }}

    /* chrome */
    QScrollArea {{ background:transparent; border:none; }}
    QScrollArea > QWidget > QWidget {{ background:transparent; }}
    QScrollBar:vertical {{ background:{SURFACE}; width:12px; margin:0;
                           border-radius:6px; }}
    QScrollBar:horizontal {{ background:{SURFACE}; height:12px; margin:0;
                             border-radius:6px; }}
    QScrollBar::handle {{ background:#4a5361; border-radius:5px; min-height:32px;
                          min-width:32px; margin:2px; }}
    QScrollBar::handle:hover {{ background:{ACCENT}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ width:0; height:0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background:none; }}
    QSplitter::handle {{ background:transparent; }}
    QSplitter::handle:hover {{ background:{LINE}; }}
    QStatusBar {{ color:{MUTED}; }}
    QStatusBar::item {{ border:none; }}
    QMenu {{ background:{RAISED}; border:1px solid {LINE_HI}; padding:4px; }}
    QMenu::item {{ padding:5px 18px; border-radius:4px; }}
    QMenu::item:selected {{ background:{ACCENT_DIM}; }}
    """


def apply(app: QApplication):
    app.setStyle("Fusion")
    p = QPalette()
    for role, color in ((QPalette.ColorRole.Window, BG), (QPalette.ColorRole.WindowText, TEXT),
                        (QPalette.ColorRole.Base, FIELD),
                        (QPalette.ColorRole.AlternateBase, "#101318"),
                        (QPalette.ColorRole.Text, TEXT), (QPalette.ColorRole.Button, RAISED),
                        (QPalette.ColorRole.ButtonText, TEXT),
                        (QPalette.ColorRole.Highlight, ACCENT),
                        (QPalette.ColorRole.HighlightedText, "#08111f"),
                        (QPalette.ColorRole.ToolTipBase, RAISED),
                        (QPalette.ColorRole.ToolTipText, TEXT),
                        (QPalette.ColorRole.PlaceholderText, DIM),
                        (QPalette.ColorRole.Link, ACCENT)):
        p.setColor(role, QColor(color))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText,
                 QPalette.ColorRole.WindowText):
        p.setColor(QPalette.ColorGroup.Disabled, role, QColor(DIM))
    app.setPalette(p)
    app.setStyleSheet(stylesheet())


def set_role(w: QWidget, role: str):
    """Restyle a button by role (re-polishes so the change shows at once)."""
    if w.property("role") == role:
        return
    w.setProperty("role", role)
    st = w.style()
    st.unpolish(w)
    st.polish(w)
    w.update()


def touch_extra(hit: int) -> str:
    """Bigger check boxes / arrows for fingers (added to touch.TOUCH_STYLE)."""
    return f"""
    QCheckBox::indicator {{ width:26px; height:26px; border-radius:6px; }}
    QSpinBox::up-button, QDoubleSpinBox::up-button, QSpinBox::down-button,
    QDoubleSpinBox::down-button {{ width:0; }}
    QTabWidget#instruments > QTabBar::tab {{ min-height:{hit - 6}px; }}
    QScrollBar:vertical {{ width:18px; }}
    QScrollBar:horizontal {{ height:18px; }}
    QScrollBar::handle {{ border-radius:7px; }}
    """
