"""Main window."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QDockWidget, QGridLayout, QHBoxLayout, QInputDialog, QLabel,
                               QLineEdit, QMainWindow, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from .. import __version__, updater
from ..appinfo import APP_NAME
from .. import policy as policy_mod
from ..backends.simulated import SimulatedBackend
from ..config import Settings, current_user, method_label
from ..models import SlotState
from .controller import Controller
from .settings_dialog import SettingsDialog
from .sim_panel import SIM_BG, SIM_COLOR, SimulatorPanel
from .tile import Tile
from .update_ui import UpdateDialog, UpdateWatcher


class PolicyDialog(QDialog):
    def __init__(self, report, parent=None):
        super().__init__(parent)
        self.setWindowTitle("BitLocker policy details")
        self.resize(820, 400)
        v = QVBoxLayout(self)
        if not report.available:
            v.addWidget(QLabel(report.note))
        t = QTableWidget(len(report.items), 4)
        t.setHorizontalHeaderLabels(["Setting", "Value", "Source", "Status / meaning"])
        for r, i in enumerate(report.items):
            for c, txt in enumerate([i.name, i.value, i.source, f"{i.status.upper()}: {i.message}"]):
                t.setItem(r, c, QTableWidgetItem(txt))
        t.horizontalHeader().setStretchLastSection(True)
        t.resizeColumnsToContents()
        v.addWidget(t)
        v.addWidget(QLabel(f"Effective method: {method_label(report.effective_method) if report.effective_method else '(requested)'}   |   "
                           f"Policy min password length: {report.min_password_length or 'none'}"))
        bb = QDialogButtonBox(QDialogButtonBox.Close); bb.rejected.connect(self.reject); bb.accepted.connect(self.accept)
        bb.button(QDialogButtonBox.Close).clicked.connect(self.accept)
        v.addWidget(bb)


class MainWindow(QMainWindow):
    def __init__(self, controller: Controller, settings: Settings):
        super().__init__()
        self.ctl = controller
        self.settings = settings
        self.hw_backend = controller.backend           # the computer's real USB ports (or nothing, off Windows)
        self.sim_backend = None
        self.simulator_mode = False
        self.resize(1280, 760)
        self.root = QWidget(); self.root.setObjectName("centralRoot"); self.setCentralWidget(self.root)
        v = QVBoxLayout(self.root)

        self.update_bar = QPushButton(); self.update_bar.hide(); self.update_bar.setMinimumHeight(30)
        self.update_bar.setStyleSheet("background:#065f46;color:white;font-weight:bold;border-radius:6px;")
        self.update_bar.clicked.connect(self._show_update)
        v.addWidget(self.update_bar)
        self._update_info = None
        self.banner = QLabel(); self.banner.setAlignment(Qt.AlignCenter)
        self.banner.setFont(QFont("Segoe UI", 12, QFont.Bold)); self.banner.setMinimumHeight(34)
        v.addWidget(self.banner)
        self.warn_bar = QLabel(); self.warn_bar.setWordWrap(True); self.warn_bar.hide()
        self.warn_bar.setStyleSheet("background:#fef3c7;color:#78350f;border-radius:6px;padding:6px 10px;")
        v.addWidget(self.warn_bar)

        bar = QHBoxLayout()
        def btn(text, fn, style=""):
            b = QPushButton(text); b.setMinimumHeight(36); b.clicked.connect(fn)
            if style:
                b.setStyleSheet(style)
            bar.addWidget(b); return b
        btn("Process all ready", self.on_process_all)
        btn("EMERGENCY STOP", self.ctl.emergency_stop, "background:#991b1b;color:white;font-weight:bold;")
        btn("Open records folder", self.on_open_records)
        btn("Settings", self.on_settings)
        self.sim_btn = btn("Simulator mode", self.toggle_simulator_mode)
        self.sim_btn.setCheckable(True)
        v.addLayout(bar)

        self.grid = QGridLayout()
        self.tiles: list[Tile] = []
        v.addLayout(self.grid, 1)
        self._build_tiles()

        # status bar: policy chip (click for details, hover for the summary) + version
        self.status = self.statusBar()
        self.policy_chip = QPushButton("Policy: checking...")
        self.policy_chip.setFlat(True)
        self.policy_chip.setCursor(Qt.PointingHandCursor)
        self.policy_chip.clicked.connect(self.on_policy)
        self.operator_label = QLabel()
        self.status.addPermanentWidget(self.operator_label)
        self.status.addPermanentWidget(self.policy_chip)
        self.status.addPermanentWidget(QLabel(f"v{__version__}"))

        self.ctl.slot_changed.connect(self.on_slot_changed)
        self.ctl.layout_changed.connect(self._on_layout_changed)
        self.ctl.policy_changed.connect(self._on_policy)
        self.ctl.notice.connect(lambda m: self.status.showMessage(m, 15000))
        self.ctl.unassigned.connect(self._on_unassigned)

        # simulator panel: pinned on the right, only while Simulator mode is on
        self.sim_panel = None
        self.dock = QDockWidget("Simulator", self)
        self.dock.setFeatures(QDockWidget.NoDockWidgetFeatures)          # pinned: no float, no close
        self.dock.setTitleBarWidget(QWidget())                           # no title bar: the panel has its own header
        self.addDockWidget(Qt.RightDockWidgetArea, self.dock)
        self.dock.hide()

        self._update_banner()
        self._update_operator()
        self.watcher = UpdateWatcher(self)
        self.watcher.found.connect(self._on_update_found)
        self._manual_check = False
        self.watcher.up_to_date.connect(lambda: self._update_result("You are up to date."))
        self.watcher.failed.connect(lambda m: self._update_result(m))
        if settings.check_updates and updater.enabled():
            QTimer.singleShot(2000, self.watcher.check)
        self.ctl.start()

    # ------------------------------------------------------------ simulator mode
    def toggle_simulator_mode(self, on: bool | None = None) -> None:
        on = (not self.simulator_mode) if on is None else bool(on)
        if on == self.simulator_mode:
            self.sim_btn.setChecked(self.simulator_mode)
            return
        if self.ctl.busy():
            self.sim_btn.setChecked(self.simulator_mode)
            self.status.showMessage("Drives are being processed. Finish or stop them before switching modes.", 8000)
            return
        if on:
            self.sim_backend = SimulatedBackend(port_count=self.settings.simulator_ports)
            self.ctl.set_backend(self.sim_backend, simulator=True)
            self.sim_panel = SimulatorPanel(self.ctl, self.sim_backend, self.settings,
                                            lambda: self.toggle_simulator_mode(False))
            self.dock.setWidget(self.sim_panel)
        else:
            policy_mod.SIM_RAW = None
            self.ctl.set_backend(self.hw_backend, simulator=False)
            old = self.dock.widget()
            self.dock.setWidget(QWidget())
            if old is not None:
                old.deleteLater()
            self.sim_panel = self.sim_backend = None
        self.simulator_mode = on
        self.sim_btn.setChecked(on)
        self.sim_btn.setText("Exit simulator mode" if on else "Simulator mode")
        self.dock.setVisible(on)
        self.setStyleSheet(
            f"#centralRoot{{background:{SIM_BG};border:4px solid {SIM_COLOR};}} "
            f"QMainWindow{{background:{SIM_BG};}} QStatusBar{{background:{SIM_BG};color:#ffe0a3;}} "
            f"QStatusBar QLabel{{color:#ffe0a3;}} QPushButton{{min-height:24px;}}" if on else "")
        self._update_banner()

    # ------------------------------------------------------------ policy chip
    def _on_policy(self, report) -> None:
        self.ctl.last_policy = report
        if not report.available:
            text, color, tip = "Policy: not available", "#9ca3af", report.note or "BitLocker policy cannot be read here."
        elif report.blocks:
            text, color = f"Policy: {len(report.blocks)} blocking", "#ef4444"
            tip = "\n".join(i.message for i in report.blocks)
        elif report.warnings:
            text, color = f"Policy: {len(report.warnings)} note(s)", "#f59e0b"
            tip = "\n".join(i.message for i in report.warnings)
        else:
            text, color = "Policy: no conflicts", "#22c55e"
            tip = "No BitLocker policy conflicts with the current settings."
        self.policy_chip.setText(f"● {text}")
        self.policy_chip.setStyleSheet(f"QPushButton{{border:0;color:{color};font-weight:bold;padding:0 8px;}}")
        self.policy_chip.setToolTip(tip + "\n\nClick for details.")
        if report.blocks:
            self.status.showMessage("BitLocker policy blocks processing: " + report.blocks[0].message, 15000)

    def on_policy(self) -> None:
        rep = self.ctl.last_policy
        if rep is not None:
            PolicyDialog(rep, self).exec()

    # ------------------------------------------------------------ updates
    def check_updates_now(self) -> None:
        """Manual check (Settings > Updates). Reports the result in the status bar / a dialog."""
        self._manual_check = True
        if not updater.enabled():
            self._update_result("Update source is not configured yet (UPDATE_REPO in appinfo.py).")
            return
        self.status.showMessage("Checking for updates...", 5000)
        self.watcher.check()

    def _update_result(self, text: str) -> None:
        if self._manual_check:
            QMessageBox.information(self, "Updates", text)
        self._manual_check = False
        self.status.showMessage(text, 8000)

    def _on_update_found(self, info) -> None:
        self._update_info = info
        self.update_bar.setText(f"Version {info.version} is available - click for details")
        self.update_bar.show()
        if self._manual_check:
            self._manual_check = False
            self._show_update()

    def _show_update(self) -> None:
        if self._update_info is None:
            return
        busy = lambda: any(x.state == SlotState.PROCESSING for x in self.ctl.slots)  # noqa: E731
        dlg = UpdateDialog(self._update_info, busy, self)
        dlg.exec()
        if dlg.installed:
            self.update_bar.setText("Update installed - restart to use it")
            self.update_bar.setEnabled(False)

    # ------------------------------------------------------------ layout
    def _build_tiles(self) -> None:
        for t in self.tiles:
            self.grid.removeWidget(t); t.setParent(None); t.deleteLater()
        self.tiles = []
        if getattr(self, "empty_label", None) is not None:
            self.grid.removeWidget(self.empty_label); self.empty_label.hide(); self.empty_label.deleteLater(); self.empty_label = None
        n = len(self.ctl.slots)
        if n == 0:
            self.empty_label = QLabel("No USB ports detected yet...")
            self.empty_label.setAlignment(Qt.AlignCenter)
            self.empty_label.setFont(QFont("Segoe UI", 14))
            self.grid.addWidget(self.empty_label, 0, 0)
            self.empty_label.show()
            return
        cols = 4 if n <= 8 else 6
        for i in range(n):
            t = Tile(i); t.process_clicked.connect(self.on_process)
            self.tiles.append(t); self.grid.addWidget(t, i // cols, i % cols)
            t.show()                      # widgets added to an already-visible window start hidden
            t.refresh(self.ctl.slots[i], self.settings.colors)

    def _on_layout_changed(self) -> None:
        """The USB port list changed (hub plugged in or removed, or the mode switched): rebuild the tiles."""
        self._build_tiles()
        if self.sim_panel is not None:
            self.sim_panel.rebuild()
        self._update_banner()
        n = len(self.ctl.slots)
        self.status.showMessage(f"{n} USB port{'s' if n != 1 else ''} detected.", 6000)

    def _update_operator(self) -> None:
        self.operator_label.setText(f"Operator: {self.settings.operator or current_user()}   ")
        self.operator_label.setToolTip("Written into every record. It is the Windows account that is signed in.")

    def _update_banner(self) -> None:
        s = self.settings
        if self.simulator_mode:
            text, bg = "SIMULATOR MODE – virtual drives only; nothing touches a real disk.", SIM_COLOR
        elif s.dry_run:
            text, bg = "DRY RUN – drives are scanned and planned, but nothing is erased.", "#2563eb"
        else:
            text, bg = "WARNING – any drive you process will be permanently erased.", "#991b1b"
        fg = "#111" if bg == SIM_COLOR else "#fff"
        self.banner.setText(text); self.banner.setStyleSheet(f"background:{bg};color:{fg};border-radius:6px;")
        suffix = ("Simulator mode" if self.simulator_mode else "Dry run" if s.dry_run else "")
        self.setWindowTitle(f"{APP_NAME} – {suffix}" if suffix else APP_NAME)
        notes = []
        if not self.simulator_mode:
            hw = self.hw_backend
            if hw.real and not hw.elevated:
                notes.append("Not running as administrator: BitLocker and boot-sector checks are limited and "
                             "erasing is unavailable. Restart the app and accept the administrator prompt.")
            if hw.ports_note:
                notes.append(hw.ports_note)
        self.warn_bar.setText("  ".join(notes))
        self.warn_bar.setVisible(bool(notes))

    def _on_unassigned(self, n: int) -> None:
        self._update_banner()                    # the backend's port note may have changed
        if n:
            self.status.showMessage(f"{n} USB drive(s) are on ports this app does not list and are being ignored.")

    def on_slot_changed(self, idx: int) -> None:
        if idx < len(self.tiles) and idx < len(self.ctl.slots):
            self.tiles[idx].refresh(self.ctl.slots[idx], self.settings.colors)

    # ------------------------------------------------------------ actions
    def _password_if_needed(self):
        if self.settings.password_mode != "prompt":
            return True, None
        pw, ok = QInputDialog.getText(self, "Drive password", "Password to apply to this batch:", QLineEdit.Password)
        return (ok and bool(pw)), pw

    def on_process(self, idx: int) -> None:
        slot = self.ctl.slots[idx]
        if slot.state != SlotState.NEEDS_WORK or not slot.scan:
            return
        s = self.settings
        risky = slot.scan.content_found or (slot.drive and slot.drive.bitlocker.locked)
        if s.require_confirm_if_content and risky and not s.dry_run:
            d = slot.drive
            n = d.total_files
            what = f"{n} file(s)" if n else "unknown contents (locked or unreadable)"
            r = QMessageBox.warning(self, "Erase this drive?",
                                    f"Port {idx + 1}: {d.model}\nS/N {d.serial}  ({d.size_gb:.1f} GB)\n\n"
                                    f"This drive holds {what}.\nEverything on it will be permanently erased.",
                                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if r != QMessageBox.Yes:
                return
        ok, pw = self._password_if_needed()
        if ok:
            self.ctl.process(idx, pw)

    def on_process_all(self) -> None:
        ready = [x for x in self.ctl.slots if x.state == SlotState.NEEDS_WORK]
        if not ready:
            self.status.showMessage("Nothing is ready to process.", 5000); return
        risky = [x for x in ready if x.scan and (x.scan.content_found or x.drive.bitlocker.locked)]
        if self.settings.require_confirm_if_content and risky and not self.settings.dry_run:
            r = QMessageBox.warning(self, "Erase drives?",
                                    f"{len(ready)} drive(s) will be processed; {len(risky)} contain data or unknown "
                                    f"contents that will be permanently erased.", QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if r != QMessageBox.Yes:
                return
        ok, pw = self._password_if_needed()
        if ok:
            self.ctl.process_all(pw)

    def on_open_records(self) -> None:
        p = Path(self.settings.resolved_csv_dir()); p.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(str(p))   # noqa: S606
        else:
            self.status.showMessage(f"Records folder: {p}", 10000)

    def on_settings(self) -> None:
        s = self.settings
        hw = self.hw_backend
        why = ("" if hw.real and hw.elevated else
               "Erasing needs Windows." if not hw.real else
               "Erasing needs administrator rights. Restart the app and accept the administrator prompt.")
        dlg = SettingsDialog(s, self, erase_blocked_reason=why, ports=self.ctl.available_ports())
        if dlg.exec():
            self.ctl.apply_settings(s)
            self._update_banner()
