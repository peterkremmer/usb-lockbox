"""Simulator panel: a pinned side panel with everything needed to stage virtual drives."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QButtonGroup, QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton,
                               QRadioButton, QScrollArea, QSpinBox, QVBoxLayout, QWidget)

from .. import policy as policy_mod
from ..backends.simulated import SCENARIOS, SimulatedBackend

SIM_COLOR = "#f59e0b"              # orange: banner, frame and panel accent while Simulator mode is on
SIM_BG = "#2b1a06"

SCENARIO_LABELS = {
    "blank": ("Blank drive", "Raw drive with nothing on it."),
    "used_files": ("Drive with files", "MBR/NTFS with files on it: needs wiping, asks for confirmation."),
    "compliant_empty": ("Compliant and empty", "Already BitLocker XTS-AES 256, empty. Re-provisioned unless 'skip' is enabled."),
    "wrong_method": ("Wrong encryption method", "Encrypted with XTS-AES 128 and files on it."),
    "odd_boot": ("Odd boot record", "Linux/EFI partitions, active flag and boot code (installer media)."),
    "hw_encrypted": ("Hardware-encrypted", "Looks like a hardware-encrypted key: must be refused."),
    "system_disk": ("System disk (must be refused)", "Pretends to be the boot disk: must be refused."),
    "write_protected": ("Write-protected", "Read-only flag set: must be refused."),
    "too_small": ("Too small", "512 MB, below the minimum size."),
    "locked_unknown": ("Locked, unknown contents", "BitLocker-locked with someone else's password."),
}
POLICIES = [
    ("No policy", None),
    ("Policy forces XTS-AES 128", {"GPO": {policy_mod.GPO_REMOVABLE_METHOD: 6}, "Intune/MDM": {}}),
    ("BitLocker disabled on removable", {"GPO": {policy_mod.GPO_REMOVABLE_CONFIGURE: 0}, "Intune/MDM": {}}),
]


class SimulatorPanel(QFrame):
    """Built for one SimulatedBackend; discarded when Simulator mode is exited."""

    def __init__(self, ctl, backend: SimulatedBackend, settings, exit_cb, parent=None):
        super().__init__(parent)
        self.ctl, self.be, self.settings, self._exit = ctl, backend, settings, exit_cb
        self.setObjectName("simPanel")
        self.setMinimumWidth(400)
        self.setStyleSheet(
            f"#simPanel,#simBody,QScrollArea{{background:{SIM_BG};border:0;}} QLabel{{color:#ffe9c2;}} "
            f"QLabel#simTitle{{color:{SIM_COLOR};font-size:16px;font-weight:bold;}} "
            f"QLabel#simHead{{color:{SIM_COLOR};font-weight:bold;margin-top:8px;}} "
            f"QLabel#simPort{{color:{SIM_COLOR};font-weight:bold;margin-top:6px;}} "
            "QComboBox,QPushButton,QSpinBox{min-height:26px;} "
            f"QRadioButton{{color:#ffe9c2;}} QRadioButton::indicator{{width:12px;height:12px;border-radius:8px;"
            f"border:2px solid {SIM_COLOR};background:transparent;}} "
            f"QRadioButton::indicator:checked{{background:{SIM_COLOR};}} QPushButton#simExit{{background:{SIM_COLOR};"
            "color:#2b1a06;font-weight:bold;border-radius:6px;min-height:34px;}")
        outer = QVBoxLayout(self); outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        outer.addWidget(scroll)
        body = QWidget(); body.setObjectName("simBody"); scroll.setWidget(body)
        v = QVBoxLayout(body)

        title = QLabel("SIMULATOR"); title.setObjectName("simTitle")
        sub = QLabel("Virtual drives only. Nothing here can touch a real disk. Exit to go back to your real USB ports.")
        sub.setWordWrap(True)
        v.addWidget(title); v.addWidget(sub)

        head = QLabel("VIRTUAL PORTS"); head.setObjectName("simHead"); v.addWidget(head)
        row = QHBoxLayout()
        row.addWidget(QLabel("Number of ports"))
        self.count = QSpinBox(); self.count.setRange(1, 16); self.count.setValue(self.be.port_count)
        self.count.valueChanged.connect(self._count_changed)
        row.addWidget(self.count); row.addStretch(1)
        v.addLayout(row)

        head = QLabel("DRIVES"); head.setObjectName("simHead"); v.addWidget(head)
        self.rows_host = QWidget(); v.addWidget(self.rows_host)
        self.rows: list[tuple[QLabel, QComboBox]] = []
        bulk = QHBoxLayout()
        fill = QPushButton("Fill empty ports"); fill.setToolTip("Insert a different scenario in every empty port.")
        fill.clicked.connect(self._fill)
        clear = QPushButton("Remove all"); clear.clicked.connect(self._remove_all)
        bulk.addWidget(fill); bulk.addWidget(clear)
        v.addLayout(bulk)

        head = QLabel("GROUP POLICY"); head.setObjectName("simHead"); v.addWidget(head)
        self.group = QButtonGroup(self)
        current = getattr(policy_mod, "SIM_RAW", None)
        for idx, (label, raw) in enumerate(POLICIES):
            rb = QRadioButton(label); rb.setChecked(raw == current)
            rb.toggled.connect(lambda on, raw=raw: on and self._set_policy(raw))
            self.group.addButton(rb, idx); v.addWidget(rb)
        note = QLabel("Changing the policy re-scans the drives in the ports."); note.setWordWrap(True); v.addWidget(note)

        v.addStretch(1)
        ex = QPushButton("Exit simulator mode"); ex.setObjectName("simExit"); ex.clicked.connect(self._exit)
        v.addWidget(ex)
        self.rebuild()
        self.ctl.slot_changed.connect(lambda _i: self._refresh_labels())

    def rebuild(self) -> None:
        """(Re)build the per-port rows for the backend's current port count."""
        old = self.rows_host.layout()
        if old is not None:                         # drop the previous layout and its widgets
            QWidget().setLayout(old)
        grid = QGridLayout(self.rows_host); grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(6); grid.setVerticalSpacing(2); grid.setColumnStretch(0, 1)
        self.rows = []
        for i in range(self.be.port_count):
            port = i + 1
            lab = QLabel(f"Port {port}: empty"); lab.setObjectName("simPort")
            combo = QComboBox()
            for key in SCENARIOS:
                label, tip = SCENARIO_LABELS.get(key, (key, ""))
                combo.addItem(label, key); combo.setItemData(combo.count() - 1, tip, Qt.ToolTipRole)
            ins = QPushButton("Insert"); ins.clicked.connect(lambda _=False, p=port, c=combo: self._insert(p, c))
            rem = QPushButton("Remove"); rem.clicked.connect(lambda _=False, p=port: self.be.remove_port(p))
            grid.addWidget(lab, 2 * i, 0, 1, 3)
            grid.addWidget(combo, 2 * i + 1, 0); grid.addWidget(ins, 2 * i + 1, 1); grid.addWidget(rem, 2 * i + 1, 2)
            self.rows.append((lab, combo))
        self._refresh_labels()

    # ------------------------------------------------------------ actions
    def _count_changed(self, n: int) -> None:
        self.be.set_port_count(n)
        self.settings.simulator_ports = n
        try:
            self.settings.save()
        except OSError:
            pass
        self.rebuild()                              # the tiles follow on the controller's next poll

    def _insert(self, port: int, combo: QComboBox) -> None:
        self.be.remove_port(port)                   # replacing a drive = pull the old one first
        self.be.add_scenario(combo.currentData(), port)

    def _fill(self) -> None:
        busy = {d.location_path for d in self.be.list_usb_disks()}
        free = [p for p in range(1, self.be.port_count + 1) if f"sim-port-{p}" not in busy]
        keys = [k for k in SCENARIOS if k != "system_disk"] or SCENARIOS
        for n, port in enumerate(free):
            self.be.add_scenario(keys[n % len(keys)], port)

    def _remove_all(self) -> None:
        for p in range(1, self.be.port_count + 1):
            self.be.remove_port(p)

    def _set_policy(self, raw) -> None:
        policy_mod.SIM_RAW = raw
        self.ctl.refresh_policy()
        self.ctl.rescan_all()

    def _refresh_labels(self) -> None:
        seen = {d.location_path: d.serial for d in self.be.list_usb_disks()}
        for i, (lab, _c) in enumerate(self.rows):
            serial = seen.get(f"sim-port-{i + 1}")
            lab.setText(f"Port {i + 1}: {serial}" if serial else f"Port {i + 1}: empty")
