"""Glue between backend, scan/pipeline and the Qt widgets. Long work runs in Python threads."""
from __future__ import annotations

import dataclasses
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QTimer, Signal

from ..backends.base import Backend
from ..models import (DriveInfo, PolicyReport, Port, RunResult, ScanResult, SlotState, Verdict)
from ..pipeline import Processor
from .. import policy as policy_mod
from .. import records
from ..config import Settings
from ..safety import protected_paths


@dataclass
class Slot:
    index: int
    port: Port = field(default_factory=lambda: Port("", (), ""))
    state: SlotState = SlotState.EMPTY
    drive: Optional[DriveInfo] = None
    scan: Optional[ScanResult] = None
    run: Optional[RunResult] = None
    step_label: str = ""
    fraction: float = 0.0
    message: str = ""
    warnings: list[str] = field(default_factory=list)
    processor: Optional[Processor] = None
    pdf_path: str = ""
    scanning_serial: str = ""

    @property
    def title(self) -> str:
        return self.port.name.upper() if self.port.name else f"PORT {self.index + 1}"

    @property
    def paths(self) -> set[str]:
        return {p.lower() for p in self.port.paths}


class Controller(QObject):
    slot_changed = Signal(int)
    layout_changed = Signal()         # the list of ports changed (hub plugged in / removed, backend switched)
    policy_changed = Signal(object)   # PolicyReport
    notice = Signal(str)              # status-bar message
    unassigned = Signal(int)          # count of drives not in a listed port
    _listed = Signal(object)          # internal: (backend, ports, drives) | Exception
    _scanned = Signal(int, object, object)
    _progress = Signal(int, str, float)
    _finished = Signal(int, object, object, object, object)

    def __init__(self, backend: Backend, settings: Settings, simulator: bool = False, parent=None):
        super().__init__(parent)
        self.backend = backend
        self.simulator = simulator
        self.settings = settings
        self.ports: list[Port] = []
        self._raw_ports: list[Port] = []          # every port the computer reports, before hiding/naming
        self.slots: list[Slot] = []
        self.last_policy: Optional[PolicyReport] = None
        self._polling = False
        self._deferred_notice = False
        self._lock = threading.Lock()
        self._listed.connect(self._on_listed)
        self._scanned.connect(self._on_scanned)
        self._progress.connect(self._on_progress)
        self._finished.connect(self._on_finished)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)

    # ------------------------------------------------------------ lifecycle
    def _interval(self) -> int:
        return 3000 if self.backend.real else 1500

    def start(self, interval_ms: Optional[int] = None) -> None:
        self.timer.start(interval_ms or self._interval())
        self.poll()

    def stop(self) -> None:
        self.timer.stop()

    def busy(self) -> bool:
        return any(s.state == SlotState.PROCESSING for s in self.slots)

    def set_backend(self, backend: Backend, simulator: bool) -> bool:
        """Switch between the computer's real USB ports and the simulator. Refused while drives are processing."""
        if self.busy():
            return False
        self.backend, self.simulator = backend, simulator
        self.ports, self.slots, self._raw_ports = [], [], []
        self.layout_changed.emit()
        self.timer.start(self._interval())
        self.refresh_policy()
        self.poll()
        return True

    def apply_settings(self, settings: Settings) -> None:
        self.settings = settings
        self._sync_ports(self._raw_ports)         # hidden ports and port names may have changed
        self.refresh_policy()

    def effective_settings(self) -> Settings:
        """Simulator mode never erases anything real, so it ignores the dry-run switch, and it keeps its
        records in a 'simulator' sub-folder so they never mix with real ones."""
        if not self.simulator:
            return self.settings
        s = self.settings
        return dataclasses.replace(
            s, dry_run=False,
            csv_dir=str(Path(s.resolved_csv_dir()) / "simulator"),
            pdf_dir=str(Path(s.resolved_csv_dir()) / "simulator" / "pdf"))

    # ------------------------------------------------------------ policy
    def _policy_raw(self):
        return policy_mod.SIM_RAW if self.simulator else None

    def refresh_policy(self) -> None:
        """Re-read BitLocker policy (GPO/Intune) on a worker thread and publish the result."""
        s, raw, backend = self.settings, self._policy_raw(), self.backend
        pw = s.get_fixed_password() if s.password_mode == "fixed" and s.fixed_password_enc else None

        def work():
            try:
                rep = policy_mod.probe(s, pw, raw)
            except Exception as e:   # noqa: BLE001
                rep = PolicyReport(available=False, note=f"Policy check failed: {e}")
            if backend is self.backend:
                self.last_policy = rep
                self.policy_changed.emit(rep)

        threading.Thread(target=work, daemon=True).start()

    def rescan_all(self) -> None:
        """Forget what was scanned so every present drive is checked again on the next poll."""
        for slot in self.slots:
            if slot.state not in (SlotState.EMPTY, SlotState.PROCESSING):
                self._reset(slot)
        self.poll()

    # ------------------------------------------------------------ ports and slots
    def bound_locations(self) -> set[str]:
        return {p.lower() for port in self.ports for p in port.paths}

    def _slot_for(self, drive: DriveInfo) -> Optional[int]:
        loc = (drive.location_path or "").lower()
        if loc:
            for slot in self.slots:
                if loc in slot.paths:
                    return slot.index
        return None

    def _extra_protected(self) -> list[str]:
        return protected_paths(self.effective_settings())

    def available_ports(self) -> list[Port]:
        """Every port the computer reports (for Settings > Ports). Empty in Simulator mode."""
        return [] if self.simulator else list(self._raw_ports)

    def _visible(self, raw: list[Port]) -> list[Port]:
        if self.simulator:
            return list(raw)
        hidden = set(self.settings.hidden_ports)
        names = self.settings.port_names
        return [dataclasses.replace(p, name=names.get(p.key, "")) for p in raw if p.key not in hidden]

    def _sync_ports(self, raw: list[Port]) -> None:
        self._raw_ports = list(raw)
        ports = self._visible(raw)
        if [(p.key, p.name) for p in ports] == [(p.key, p.name) for p in self.ports]:
            return
        if self.busy():                                   # slot numbers must not shift under a running job
            if not self._deferred_notice:
                self._deferred_notice = True
                self.notice.emit("The USB port list changed; it will update when processing finishes.")
            return
        self._deferred_notice = False
        old = {s.port.key: s for s in self.slots}
        self.ports = list(ports)
        self.slots = []
        for i, port in enumerate(self.ports):
            slot = old.get(port.key) or Slot(i, port)
            slot.index, slot.port = i, port
            self.slots.append(slot)
        self.layout_changed.emit()

    # ------------------------------------------------------------ polling
    def poll(self) -> None:
        if self._polling:
            return
        self._polling = True
        backend = self.backend

        def work():
            try:
                drives = backend.list_usb_disks()
                occupied = {d.location_path.lower() for d in drives if d.location_path}
                self._listed.emit((backend, backend.list_ports(occupied), drives))
            except Exception as e:   # noqa: BLE001
                self._listed.emit(e)

        threading.Thread(target=work, daemon=True).start()

    def _on_listed(self, result) -> None:
        self._polling = False
        if isinstance(result, Exception):
            self.notice.emit(f"Could not list drives: {result}")
            return
        backend, ports, drives = result
        if backend is not self.backend:                   # a stale answer from before a mode switch
            return
        self._sync_ports(ports)
        present: dict[int, DriveInfo] = {}
        unassigned = 0
        for d in drives:
            idx = self._slot_for(d)
            if idx is None:
                unassigned += 1
            else:
                present[idx] = d
        self.unassigned.emit(unassigned)

        for slot in self.slots:
            d = present.get(slot.index)
            if d is None:
                if slot.state in (SlotState.PROCESSING,):
                    continue          # worker will report the failure itself
                if slot.state != SlotState.EMPTY:
                    self._reset(slot)
                continue
            if slot.state == SlotState.PROCESSING:
                continue
            same = slot.drive is not None and slot.drive.serial == d.serial
            if same and slot.state != SlotState.EMPTY:
                continue
            slot.drive = d
            slot.state = SlotState.SCANNING
            slot.message = "Checking drive..."
            slot.scan = slot.run = None
            slot.scanning_serial = d.serial
            self.slot_changed.emit(slot.index)
            self._scan_async(slot.index, d)

    def _reset(self, slot: Slot) -> None:
        slot.state = SlotState.EMPTY
        slot.drive = slot.scan = slot.run = None
        slot.message = ""; slot.fraction = 0.0; slot.step_label = ""; slot.warnings = []
        slot.pdf_path = ""
        self.slot_changed.emit(slot.index)

    # ------------------------------------------------------------ scanning
    def _scan_async(self, idx: int, drive: DriveInfo) -> None:
        from ..scan import scan_drive
        s = self.effective_settings()
        raw, backend = self._policy_raw(), self.backend
        pw = s.get_fixed_password() if s.password_mode == "fixed" else None
        bound, extra = self.bound_locations(), self._extra_protected()

        def work():
            pol = policy_mod.probe(s, pw, raw)
            hist = records.read_history(s, drive.serial)
            res = scan_drive(drive, s, backend.system_disk_numbers(), pol, bound, hist, extra)
            self._scanned.emit(idx, res, pol)

        threading.Thread(target=work, daemon=True).start()

    def _on_scanned(self, idx: int, res: ScanResult, pol: PolicyReport) -> None:
        if idx >= len(self.slots):
            return
        slot = self.slots[idx]
        if slot.state != SlotState.SCANNING or slot.drive is None or slot.drive.serial != res.drive.serial:
            return
        self.last_policy = pol
        slot.scan = res
        slot.drive = res.drive
        if res.verdict == Verdict.REJECTED:
            slot.state = SlotState.REJECTED
            slot.message = "Not processed."
        elif res.verdict == Verdict.ALREADY_OK:
            slot.state = SlotState.ALREADY_OK
            slot.message = "Already compliant."
        else:
            slot.state = SlotState.NEEDS_WORK
            slot.message = "Ready to process."
        self.slot_changed.emit(idx)

    # ------------------------------------------------------------ processing
    def process(self, idx: int, password: Optional[str] = None) -> bool:
        slot = self.slots[idx]
        if slot.state != SlotState.NEEDS_WORK or slot.scan is None:
            return False
        s = self.effective_settings()
        proc = Processor(self.backend, s, self.bound_locations(), self._extra_protected())
        slot.processor = proc
        slot.state = SlotState.PROCESSING
        slot.fraction = 0.0; slot.step_label = "Starting"; slot.message = "Working - do not remove."
        self.slot_changed.emit(idx)
        scan, pol, backend = slot.scan, self.last_policy, self.backend

        def prog(label: str, frac: float) -> None:
            self._progress.emit(idx, label, frac)

        def work():
            run = proc.run(scan, pol, prog, (lambda: password))
            cpath = ppath = None
            warns: list[str] = []
            try:
                cpath, ppath, warns = records.record_run(s, scan, run, backend.name)
            except Exception as e:   # noqa: BLE001
                warns.append(f"RECORD NOT WRITTEN: {e}")
            self._finished.emit(idx, run, cpath, ppath, warns)

        threading.Thread(target=work, daemon=True).start()
        return True

    def process_all(self, password: Optional[str] = None) -> int:
        n = 0
        for slot in self.slots:
            if slot.state == SlotState.NEEDS_WORK:
                n += int(self.process(slot.index, password))
        return n

    def emergency_stop(self) -> None:
        for slot in self.slots:
            if slot.processor:
                slot.processor.cancel()
        self.notice.emit("Emergency stop requested. Drives being processed will be marked FAILED.")

    def _on_progress(self, idx: int, label: str, frac: float) -> None:
        if idx >= len(self.slots):
            return
        slot = self.slots[idx]
        slot.step_label, slot.fraction = label, frac
        self.slot_changed.emit(idx)

    def _on_finished(self, idx: int, run: RunResult, cpath, ppath, warns) -> None:
        if idx >= len(self.slots):
            return
        slot = self.slots[idx]
        slot.run = run
        slot.processor = None
        slot.warnings = list(warns)
        slot.pdf_path = str(ppath or "")
        record_failed = any(w.startswith("RECORD NOT WRITTEN") for w in warns)
        if run.ok and not record_failed:
            slot.state = SlotState.DONE if run.outcome != "ALREADY_COMPLIANT" else SlotState.ALREADY_OK
            slot.message = "Verified and recorded. Safe to remove." if run.outcome != "DRY_RUN" else \
                "Dry run only: nothing was erased."
        else:
            slot.state = SlotState.FAILED
            slot.message = ("Record could not be written - drive NOT cleared for use. " if record_failed and run.ok else "") \
                + (run.error or "Failed.")
            if run.possible_cause:
                slot.message += "  Possible cause: " + run.possible_cause
        slot.fraction = 1.0 if run.ok else slot.fraction
        self.slot_changed.emit(idx)
        for w in warns:
            self.notice.emit(w)
