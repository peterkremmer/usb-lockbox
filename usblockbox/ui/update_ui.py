"""Qt side of the update feature: a background checker and the confirmation dialog."""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit, QPushButton,
                               QVBoxLayout)

from .. import __version__, updater
from ..appinfo import APP_NAME
from ..config import APP_ROOT


class UpdateWatcher(QObject):
    """Asks GitHub for the latest release on a worker thread. Never installs anything by itself."""
    found = Signal(object)            # UpdateInfo
    up_to_date = Signal()
    failed = Signal(str)

    def check(self) -> None:
        def work():
            try:
                info = updater.check_for_update()
            except updater.UpdateError as e:
                self.failed.emit(str(e))
                return
            except Exception as e:   # noqa: BLE001 - a failed check must never break the app
                self.failed.emit(f"Update check failed: {e}")
                return
            self.found.emit(info) if info else self.up_to_date.emit()

        threading.Thread(target=work, daemon=True).start()


class UpdateDialog(QDialog):
    _done = Signal(object, str)       # (written files | None, error text)

    def __init__(self, info: updater.UpdateInfo, busy: Callable[[], bool], parent=None):
        super().__init__(parent)
        self.info, self._busy = info, busy
        self.installed = False
        self.setWindowTitle(f"{APP_NAME} update")
        self.resize(560, 420)
        v = QVBoxLayout(self)
        v.addWidget(QLabel(f"<b>Version {info.version} is available</b> (you have {__version__})."))
        notes = QPlainTextEdit(info.notes or "(no release notes)")
        notes.setReadOnly(True)
        v.addWidget(notes, 1)
        self.msg = QLabel(""); self.msg.setWordWrap(True)
        v.addWidget(self.msg)
        row = QHBoxLayout()
        self.go = QPushButton("Update now")
        self.go.clicked.connect(self._update)
        page = QPushButton("View release page")
        page.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(info.page_url)))
        close = QPushButton("Later"); close.clicked.connect(self.reject)
        row.addWidget(self.go); row.addWidget(page); row.addStretch(1); row.addWidget(close)
        v.addLayout(row)
        self._done.connect(self._finished)

        if updater.is_git_checkout(APP_ROOT):
            self._block("This copy is a git checkout. Update it with `git pull`, then restart.")
        elif not info.can_self_update:
            self._block("This release has no checksummed zip, so it can't be installed automatically. "
                        "Use 'View release page' to update by hand.")
        elif busy():
            self._block("Drives are being processed. Finish or stop them before updating.")
        else:
            self.msg.setText("Your settings and records (the data folder) are not touched. "
                             "Replaced files are backed up first.")

    def _block(self, text: str) -> None:
        self.go.setEnabled(False)
        self.msg.setText(text)

    def _update(self) -> None:
        if self._busy():
            QMessageBox.warning(self, "Busy", "Drives are being processed. Try again when they finish.")
            return
        self.go.setEnabled(False)
        self.msg.setText("Downloading and verifying...")

        def work():
            try:
                data = updater.download_zip(self.info)
                written = updater.apply_update(data, Path(APP_ROOT))
                self._done.emit(written, "")
            except Exception as e:   # noqa: BLE001
                self._done.emit(None, str(e))

        threading.Thread(target=work, daemon=True).start()

    def _finished(self, written, err: str) -> None:
        if err:
            self.msg.setText(f"Update NOT installed: {err}")
            self.go.setEnabled(True)
            return
        self.installed = True
        self.msg.setText(f"Updated {len(written)} file(s). Close and restart {APP_NAME} to use the new version.")
        QMessageBox.information(self, "Update installed", f"Restart {APP_NAME} to finish updating.")
