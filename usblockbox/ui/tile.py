"""One big status tile per hub port."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QProgressBar, QPushButton, QScrollArea,
                               QSizePolicy, QVBoxLayout)

from ..models import Severity, SlotState

BIG = {
    SlotState.EMPTY: ("◌", "EMPTY"),
    SlotState.SCANNING: ("⌛", "CHECKING"),
    SlotState.NEEDS_WORK: ("⚠", "CLICK TO PROCESS"),
    SlotState.PROCESSING: ("⟳", "WORKING – DO NOT REMOVE"),
    SlotState.DONE: ("✔", "DONE – REMOVE"),
    SlotState.ALREADY_OK: ("✔", "ALREADY OK – REMOVE"),
    SlotState.REJECTED: ("✖", "STOP – SET ASIDE"),
    SlotState.FAILED: ("✖", "FAILED – SET ASIDE"),
}


def _text_color(bg: str) -> str:
    c = QColor(bg)
    lum = 0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()
    return "#111111" if lum > 160 else "#ffffff"


def _gb(n: int) -> str:
    return f"{n / 1_000_000_000:.1f} GB"


class Tile(QFrame):
    process_clicked = Signal(int)

    def __init__(self, index: int, parent=None):
        super().__init__(parent)
        self.index = index
        self.setMinimumSize(250, 340)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        v = QVBoxLayout(self)
        v.setContentsMargins(14, 12, 14, 12)
        self.port = QLabel(f"PORT {index + 1}")
        self.port.setFont(QFont("Segoe UI", 11, QFont.Bold))
        self.icon = QLabel("◌")
        self.icon.setAlignment(Qt.AlignCenter)
        self.icon.setFont(QFont("Segoe UI Symbol", 54))
        self.big = QLabel("EMPTY")
        self.big.setAlignment(Qt.AlignCenter)
        self.big.setWordWrap(True)
        self.big.setFont(QFont("Segoe UI", 18, QFont.Bold))
        self.info = QLabel("")
        self.info.setWordWrap(True)
        self.info.setFont(QFont("Segoe UI", 9))
        self.reasons = QLabel("")
        self.reasons.setWordWrap(True)
        self.reasons.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.reasons.setFont(QFont("Segoe UI", 9))
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(True)
        self.bar.setFormat("")
        self.btn = QPushButton("PROCESS")
        self.btn.setMinimumHeight(48)
        self.btn.setFont(QFont("Segoe UI", 14, QFont.Bold))
        self.btn.setStyleSheet("QPushButton{background:#111;color:white;border-radius:6px;} QPushButton:hover{background:#333;}")
        self.btn.clicked.connect(lambda: self.process_clicked.emit(self.index))
        for w in (self.port, self.icon, self.big, self.info):
            v.addWidget(w)
        self.scroll = QScrollArea()                      # many findings scroll instead of running under the button
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setWidget(self.reasons)
        v.addWidget(self.scroll, 1)
        self.step = QLabel("")
        self.step.setFont(QFont("Segoe UI", 10, QFont.Bold))
        v.addWidget(self.step)
        v.addWidget(self.bar)
        v.addWidget(self.btn)
        self.timing = QLabel("")                          # clock times: started, time left, finished
        self.timing.setFont(QFont("Segoe UI", 9))
        self.timing.setWordWrap(True)
        v.insertWidget(v.indexOf(self.btn), self.timing)
        self.bar.hide()
        self.btn.hide()
        self.timing.hide()

    def refresh(self, slot, colors: dict) -> None:
        st: SlotState = slot.state
        bg = colors.get(st.value, "#6b7280")
        if st == SlotState.PROCESSING and slot.attention:
            bg = "#b45309"                                   # amber: this working drive needs a look
        fg = _text_color(bg)
        self.setStyleSheet(f"QFrame{{background:{bg};border-radius:12px;}} QLabel{{background:transparent;color:{fg};}}"
                           f"QScrollArea{{background:transparent;border:0;}} QScrollArea > QWidget > QWidget{{background:transparent;}}"
                           f"QProgressBar{{background:rgba(255,255,255,.35);border:0;border-radius:4px;color:{fg};height:18px;}}"
                           f"QProgressBar::chunk{{background:{fg};border-radius:4px;}}")
        self.port.setText(f"{slot.title}  ·  {slot.port.where}" if slot.port.where else slot.title)
        icon, word = BIG[st]
        self.icon.setText(icon)
        self.big.setText(word)
        d = slot.drive
        self.info.setText(f"{d.model}\nS/N {d.serial}  •  {_gb(d.size_bytes)}" if d else "Insert a drive")
        lines: list[str] = []
        if st == SlotState.PROCESSING and slot.attention:
            lines.append("⚠ " + slot.attention)
        if st in (SlotState.NEEDS_WORK, SlotState.REJECTED, SlotState.DONE, SlotState.ALREADY_OK, SlotState.FAILED):
            if slot.message:
                lines.append(slot.message)
        elif slot.message:
            lines.append(slot.message)
        if slot.scan and st in (SlotState.NEEDS_WORK, SlotState.REJECTED):
            sev = Severity.BLOCK if st == SlotState.REJECTED else Severity.NEEDS_WORK
            lines += ["• " + f.message for f in slot.scan.findings if f.severity == sev][:5]
        if slot.warnings:
            lines += ["! " + w for w in slot.warnings]
        self.reasons.setText("\n".join(lines))
        self.bar.setVisible(st == SlotState.PROCESSING)
        self.step.setVisible(st == SlotState.PROCESSING)
        if st == SlotState.PROCESSING:
            self.bar.setValue(int(slot.fraction * 1000))
            self.step.setText(f"{slot.step_label}  –  {slot.fraction * 100:.0f}%")
        self.timing.setText(slot.timing_text)
        self.timing.setVisible(bool(slot.timing_text))
        self.btn.setVisible(st == SlotState.NEEDS_WORK)
