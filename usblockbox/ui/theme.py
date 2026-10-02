"""Small app-wide look fixes."""
from __future__ import annotations

from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QToolTip


def apply_tooltip_style() -> None:
    """Hover help is drawn with explicit colours: with a dark system theme the default tooltip can end up with
    text that cannot be read, so every tooltip showed as an empty box."""
    pal = QToolTip.palette()
    for role, colour in ((QPalette.ToolTipBase, "#fffbe6"), (QPalette.ToolTipText, "#111111"),
                         (QPalette.Window, "#fffbe6"), (QPalette.WindowText, "#111111"),
                         (QPalette.Base, "#fffbe6"), (QPalette.Text, "#111111")):
        pal.setColor(role, QColor(colour))
    QToolTip.setPalette(pal)
    QToolTip.setFont(QFont("Segoe UI", 9))
