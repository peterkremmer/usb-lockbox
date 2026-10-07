"""Glue between backend, scan/pipeline and the Qt widgets. Long work runs in Python threads."""
from __future__ import annotations

import dataclasses
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QTimer, Signal

from .. import diag
from ..backends.base import Backend
from ..models import (DriveInfo, PolicyReport, Port, RunResult, ScanResult, SlotState, Verdict)
from ..pipeline import Processor, plan_steps
from .. import policy as policy_mod
from .. import records
from .. import eta
from ..config import Settings, settings_dir
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
    # timing (set while processing, kept after it ends so a returning operator can see when it finished)
    plan: list = field(default_factory=list)
    size_gb: float = 0.0
    passes: int = 0
    started_at: float = 0.0
    finished_at: float = 0.0
    step_started: float = 0.0
    step_label_seen: str = ""
    last_progress_at: float = 0.0
    last_frac: float = 0.0
    expected_total: Optional[float] = None
    remaining: Optional[float] = None
    remaining_measured: bool = False
    attention: str = ""
    timing_text: str = ""

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
    _listed = Signal(object)          # internal: (generation, (backend, ports, drives) | Exception)
    _scanned = Signal(int, object, object)
    _progress = Signal(int, str, float)
    _finished = Signal(int, object, object, object, object)
    batch_changed = Signal()          # the batch summary text may have changed
    batch_finished = Signal()         # nothing is processing any more after a batch
    attention_raised = Signal(int)    # a working drive looks stuck, slow or overdue
    scan_state_changed = Signal()     # the first scan finished or failed (the "Scanning..." text depends on it)

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
        self._poll_gen = 0                        # bumped on a backend switch so an old scan cannot block or answer the new one
        self._deferred_notice = False
        self.first_scan_done = False              # False until the first scan of USB ports and drives has finished
        self.first_scan_seconds = 0.0
        self.scan_error = ""                      # why the latest scan failed ("" if it did not)
        self._scan_started = time.monotonic()
        self._scan_warned: set[int] = set()
        self._lock = threading.Lock()
        self._listed.connect(self._on_listed)
        self._scanned.connect(self._on_scanned)
        self._progress.connect(self._on_progress)
        self._finished.connect(self._on_finished)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self._now = time.time
        self._timings_real = eta.Timings(settings_dir() / "timings.json")
        self._timings_sim = eta.Timings(None)       # the simulator's made-up speeds never pollute the real ones
        self._batch_started = 0.0
        self._batch_finished_at = 0.0
        self._batch_done = 0
        self._batch_failed = 0
        self.tick_timer = QTimer(self)
        self.tick_timer.timeout.connect(self.tick)
        self.tick_timer.start(5000)

    # ------------------------------------------------------------ lifecycle
    def _interval(self) -> int:
        return 3000 if self.backend.real else 1500

    def start(self, interval_ms: Optional[int] = None) -> None:
        self._begin_first_scan()
        self.timer.start(interval_ms or self._interval())
        self.poll()

    def _begin_first_scan(self) -> None:
        self.first_scan_done, self.scan_error = False, ""
        self._scan_started = time.monotonic()
        self._scan_warned = set()
        diag.reset_startup()
        diag.log.info("First scan started (backend: %s)", self.backend.name)

    def scan_elapsed(self) -> float:
        return 0.0 if self.first_scan_done else time.monotonic() - self._scan_started

    def stop(self) -> None:
        self.timer.stop()

    @property
    def timings(self) -> eta.Timings:
        return self._timings_sim if self.simulator else self._timings_real

    def busy(self) -> bool:
        return any(s.state == SlotState.PROCESSING for s in self.slots)

    def set_backend(self, backend: Backend, simulator: bool) -> bool:
        """Switch between the computer's real USB ports and the simulator. Refused while drives are processing."""
        if self.busy():
            return False
        self.backend, self.simulator = backend, simulator
        self.ports, self.slots, self._raw_ports = [], [], []
        self._poll_gen += 1                               # a slow scan of the old backend may still be running:
        self._polling = False                             # do not wait for it, and ignore its answer when it arrives
        self._begin_first_scan()
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
                with diag.timed("BitLocker policy check"):
                    rep = policy_mod.probe(s, pw, raw)
                diag.log.info("Policy: %d setting(s) read, effective method %s", len(rep.items), rep.effective_method or "-")
            except Exception as e:   # noqa: BLE001
                diag.log.warning("Policy check failed: %s", e)
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
        diag.log.info("USB ports changed: %d port(s) shown (%d reported by Windows)", len(ports), len(raw))
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
        backend, gen = self.backend, self._poll_gen

        def work():
            t0 = time.perf_counter()
            try:
                drives = backend.list_usb_disks()
                t1 = time.perf_counter()
                occupied = {d.location_path.lower() for d in drives if d.location_path}
                ports = backend.list_ports(occupied)
                if not diag.startup_done():
                    diag.log.info("Scan pieces: drives %.2f s, ports %.2f s", t1 - t0, time.perf_counter() - t1)
                self._listed.emit((gen, (backend, ports, drives)))
            except Exception as e:   # noqa: BLE001
                diag.log.exception("Scan failed")
                self._listed.emit((gen, e))

        threading.Thread(target=work, daemon=True).start()

    def _on_listed(self, message) -> None:
        gen, result = message
        if gen != self._poll_gen:                         # answer from before a mode switch: it owns nothing now
            return
        self._polling = False
        if isinstance(result, Exception):
            self.notice.emit(f"Could not list drives: {result}")
            self.scan_error = str(result)
            self.scan_state_changed.emit()
            return
        backend, ports, drives = result
        if backend is not self.backend:                   # a stale answer from before a mode switch
            return
        self._sync_ports(ports)
        if not self.first_scan_done or self.scan_error:
            self.scan_error = ""
            if not self.first_scan_done:
                self.first_scan_done = True
                self.first_scan_seconds = time.monotonic() - self._scan_started
                diag.log.info("First scan finished in %.1f s: %d port(s) shown, %d drive(s) present",
                              self.first_scan_seconds, len(self.ports), len(drives))
                diag.mark_startup_done()
            self.scan_state_changed.emit()
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
                    diag.log.info("Drive removed from %s", slot.title)
                    self._reset(slot)
                continue
            if slot.state == SlotState.PROCESSING:
                continue
            same = slot.drive is not None and slot.drive.serial == d.serial
            if same and slot.state != SlotState.EMPTY:
                continue
            slot.drive = d
            diag.log.info("Drive detected on %s: %s, serial %s, %.1f GB, %d volume(s)", slot.title, d.model,
                          d.serial, d.size_gb, len(d.volumes))
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
        slot.finished_at = slot.started_at = 0.0
        slot.attention = slot.timing_text = ""
        slot.remaining = None
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
        diag.log.info("Checked drive on %s: verdict %s, %d finding(s)", slot.title, res.verdict.name, len(res.findings))
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
        now = self._now()
        if not self.busy():                                   # first drive of a new batch
            self._batch_started, self._batch_finished_at = now, 0.0
            self._batch_done = self._batch_failed = 0
        slot.plan = plan_steps(s)
        slot.size_gb = (slot.scan.drive.size_bytes or 0) / eta.GB
        slot.passes = s.overwrite_passes
        slot.started_at, slot.finished_at = now, 0.0
        slot.step_started = slot.last_progress_at = now
        slot.step_label_seen, slot.last_frac, slot.attention = "", 0.0, ""
        rem = eta.remaining(slot.plan, slot.plan[0], 0.0, 0.0, slot.size_gb, slot.passes, self.timings)
        slot.expected_total = rem[0] if rem and rem[1] else None     # only judge "overdue" against measured numbers
        slot.state = SlotState.PROCESSING
        diag.log.info("Processing started on %s (serial %s, %.1f GB, steps: %s, dry run: %s)", slot.title,
                      slot.scan.drive.serial, slot.size_gb, ", ".join(slot.plan), s.dry_run)
        slot.fraction = 0.0; slot.step_label = "Starting"; slot.message = "Working - do not remove."
        self._update_timing(slot, now)
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
        now = self._now()
        if label != slot.step_label_seen:                     # a new step began: learn how long the last one took
            diag.log.info("%s: step '%s' (previous step took %.0f s)", slot.title, label,
                          now - slot.step_started if slot.step_label_seen else 0.0)
            self._learn(slot, slot.step_label_seen, now)
            slot.step_label_seen, slot.step_started, slot.last_progress_at = label, now, now
        if frac > slot.last_frac + 0.0005:
            slot.last_progress_at = now
        slot.last_frac = max(slot.last_frac, frac)
        slot.step_label, slot.fraction = label, frac
        self._update_timing(slot, now)
        self.slot_changed.emit(idx)

    def _step_frac(self, slot: Slot) -> float:
        if slot.step_label not in slot.plan or not slot.plan:
            return 0.0
        n = len(slot.plan)
        return min(max(slot.fraction * n - slot.plan.index(slot.step_label), 0.0), 1.0)

    def _learn(self, slot: Slot, label: str, now: float) -> None:
        """A step just finished: remember its duration (per GB for the long ones) for future estimates."""
        if not label or label not in slot.plan or self.settings.dry_run and not self.simulator:
            return
        key, took = eta.step_key(label), now - slot.step_started
        if key in eta.LONG_STEPS:
            if slot.size_gb <= 0:
                return
            took = took / slot.size_gb / (max(slot.passes, 1) if key == "overwrite" else 1)
        self.timings.record(key, took)

    def _update_timing(self, slot: Slot, now: float) -> None:
        """Refresh the time-left text and the 'needs a look' flag for one slot."""
        if slot.state == SlotState.PROCESSING:
            f = self._step_frac(slot)
            elapsed_step = now - slot.step_started
            rem = eta.remaining(slot.plan, slot.step_label, f, elapsed_step, slot.size_gb, slot.passes, self.timings)
            slot.remaining, slot.remaining_measured = (rem if rem else (None, False))
            old = slot.attention
            slot.attention = eta.attention(
                now=now, last_progress_at=slot.last_progress_at, stall_seconds=self.settings.stall_minutes * 60,
                key=eta.step_key(slot.step_label), size_gb=slot.size_gb, passes=slot.passes, step_frac=f,
                step_elapsed=elapsed_step, started_at=slot.started_at, expected_total=slot.expected_total,
                timings=self.timings)
            if slot.attention and not old:
                self.attention_raised.emit(slot.index)
            line1 = f"Started {eta.fmt_clock(slot.started_at)} · running {eta.fmt_duration(now - slot.started_at)}"
            if slot.remaining is None:
                line2 = "Estimating time left..."
            else:
                line2 = (f"About {eta.fmt_duration(slot.remaining)} left · done around "
                         f"{eta.fmt_clock(now + slot.remaining)}" + ("" if slot.remaining_measured else " (rough guess)"))
            slot.timing_text = line1 + "\n" + line2
        elif slot.finished_at and slot.state in (SlotState.DONE, SlotState.FAILED, SlotState.ALREADY_OK):
            slot.timing_text = (f"Finished {eta.fmt_clock(slot.finished_at)} · {eta.fmt_ago(now - slot.finished_at)}"
                                f" · took {eta.fmt_duration(slot.finished_at - slot.started_at)}")
        else:
            slot.timing_text = ""

    def tick(self) -> None:
        """Every few seconds: keep clocks, time left and warnings fresh even when nothing else changes."""
        now = self._now()
        if not self.first_scan_done:                          # leave a trail if the first scan hangs
            waited = time.monotonic() - self._scan_started
            for limit in (15, 45, 120, 300):
                if waited >= limit and limit not in self._scan_warned:
                    self._scan_warned.add(limit)
                    diag.log.warning("First scan still running after %d s", limit)
        for slot in self.slots:
            before = (slot.timing_text, slot.attention)
            self._update_timing(slot, now)
            if (slot.timing_text, slot.attention) != before:
                self.slot_changed.emit(slot.index)
        self.batch_changed.emit()

    def batch_summary(self) -> tuple[str, str]:
        """(text, kind) for the strip under the banner. kind: '' (hide), working, attention, finished."""
        now = self._now()
        working = [s for s in self.slots if s.state == SlotState.PROCESSING]
        if working:
            n = len(working)
            parts = [f"{n} drive{'s' if n != 1 else ''} working"]
            if self._batch_done or self._batch_failed:
                parts.append(f"{self._batch_done} done" + (f", {self._batch_failed} failed" if self._batch_failed else ""))
            known = [s.remaining for s in working if s.remaining is not None]
            if known and len(known) == n:
                left = max(known)
                rough = "" if all(s.remaining_measured for s in working) else " (rough guess)"
                parts.append(f"all finished about {eta.fmt_clock(now + left)}, {eta.fmt_duration(left)} from now{rough}")
            elif known:
                parts.append(f"at least {eta.fmt_duration(max(known))} more")
            else:
                parts.append("estimating time left")
            flagged = [s.title for s in working if s.attention]
            if flagged:
                parts.append("NEEDS A LOOK: " + ", ".join(flagged))
                return "   ·   ".join(parts), "attention"
            return "   ·   ".join(parts), "working"
        if self._batch_finished_at and any(s.state != SlotState.EMPTY for s in self.slots):
            parts = [f"Batch finished {eta.fmt_clock(self._batch_finished_at)} "
                     f"({eta.fmt_ago(now - self._batch_finished_at)})",
                     f"took {eta.fmt_duration(self._batch_finished_at - self._batch_started)}",
                     f"{self._batch_done} done" + (f", {self._batch_failed} FAILED: set those aside" if self._batch_failed else "")]
            return "   ·   ".join(parts), ("attention" if self._batch_failed else "finished")
        return "", ""

    def _on_finished(self, idx: int, run: RunResult, cpath, ppath, warns) -> None:
        if idx >= len(self.slots):
            return
        slot = self.slots[idx]
        now = self._now()
        if run.ok:
            self._learn(slot, slot.step_label_seen, now)          # the last step
        slot.finished_at, slot.attention, slot.remaining = now, "", None
        diag.log.info("%s finished after %.0f s: %s%s", slot.title, now - slot.started_at,
                      "OK (" + (run.outcome or "done") + ")" if run.ok else "FAILED", "" if run.ok else " - " + (run.error or ""))
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
        if slot.state in (SlotState.DONE, SlotState.ALREADY_OK):
            self._batch_done += 1
        else:
            self._batch_failed += 1
        self._update_timing(slot, now)
        self.slot_changed.emit(idx)
        for w in warns:
            self.notice.emit(w)
        if not self.busy():
            self._batch_finished_at = now
            self.batch_finished.emit()
        self.batch_changed.emit()
