"""Processing pipeline: wipe -> partition/format -> BitLocker -> verify. Backend-agnostic."""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Callable, Optional

from .backends.base import Backend, BackendError, Cancelled
from .config import Settings
from .models import (DriveInfo, PolicyReport, RunResult, ScanResult, StepResult, Verdict)
from .passwords import resolve_password, validate_password
from .policy import explain_error
from .safety import SafetyError, revalidate

ProgressCb = Callable[[str, float], None]     # (step label, overall fraction 0..1)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def plan_steps(settings: Settings) -> list[str]:
    steps = ["Clear partitions and boot records", "Zero disk edges"]
    if settings.overwrite_passes > 0:
        steps.append(f"Overwrite ({settings.overwrite_passes} pass{'es' if settings.overwrite_passes != 1 else ''})")
    steps += ["Partition and format", "Enable BitLocker", "Encrypt", "Verify", "Lock and finish"]
    return steps


class Processor:
    def __init__(self, backend: Backend, settings: Settings,
                 bound_locations: Optional[set[str]] = None,
                 extra_protected: Optional[list[str]] = None):
        self.backend = backend
        self.settings = settings
        self.bound_locations = bound_locations
        self.extra_protected = extra_protected or []
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def _cancelled(self) -> bool:
        return self._cancel.is_set()

    # ------------------------------------------------------------------
    def run(self, scan: ScanResult, policy: Optional[PolicyReport], progress: ProgressCb,
            password_prompt: Optional[Callable[[], Optional[str]]] = None) -> RunResult:
        s = self.settings
        res = RunResult(ok=False, outcome="FAILED", started=_now())
        drive = scan.drive

        if scan.verdict == Verdict.REJECTED:
            res.outcome = "REJECTED"; res.error = "; ".join(scan.reasons("BLOCK")) or "Rejected by scan."
            res.finished = _now()
            return res
        if scan.verdict == Verdict.ALREADY_OK:
            res.ok = True; res.outcome = "ALREADY_COMPLIANT"
            res.encryption_method = drive.bitlocker.method
            res.sanitization_category = "None (no wipe needed)"
            res.finished = _now()
            return res

        method = (policy.effective_method if policy and policy.effective_method else s.encryption_method)
        res.encryption_method = method

        steps = plan_steps(s)
        total = len(steps)
        idx = {"i": 0}

        def step_progress(label: str):
            def cb(frac: float) -> None:
                progress(label, (idx["i"] + min(max(frac, 0.0), 1.0)) / total)
            return cb

        def do(label: str, fn: Callable[[], object]):
            if self._cancelled():
                raise Cancelled("Cancelled by operator.")
            sr = StepResult(label, False, started=_now())
            progress(label, idx["i"] / total)
            try:
                out = fn()
                sr.ok = True
            except BaseException as e:   # noqa: BLE001 - record then re-raise
                sr.detail = str(e)
                sr.finished = _now()
                res.steps.append(sr)
                raise
            sr.finished = _now()
            res.steps.append(sr)
            idx["i"] += 1
            progress(label, idx["i"] / total)
            return out

        def guard() -> DriveInfo:
            return revalidate(self.backend, drive, s, self.bound_locations, self.extra_protected)

        try:
            # password first: nothing destructive happens if it is unusable
            pw = resolve_password(s, password_prompt)
            err = validate_password(pw, s, policy.min_password_length if policy else 0,
                                    policy.password_complexity_required if policy else False)
            if err:
                raise ValueError(err)
            res.password = pw
            res.password_profile = s.password_profile_id

            if s.dry_run:
                res.ok = True; res.outcome = "DRY_RUN"
                res.steps = [StepResult(n, True, "dry run: not executed") for n in steps]
                res.finished = _now()
                return res

            if self.backend.real and not self.backend.elevated:
                raise SafetyError("Erasing needs administrator rights. Restart the app as administrator.")

            self.backend.hold(drive.disk_number)          # the drive listing must leave this disk alone from here on
            guard()
            do(steps[0], lambda: (guard(), self.backend.clear_disk(drive)))
            do(steps[1], lambda: (guard(), self.backend.zero_edges(drive, s.zero_edge_mb)))
            if s.overwrite_passes > 0:
                label = steps[2]
                do(label, lambda: (guard(), self.backend.overwrite(
                    drive, s.overwrite_passes, s.verify_readback, step_progress(label), self._cancelled)))
            letter = do("Partition and format", lambda: (guard(), self.backend.init_partition_format(
                drive, s.partition_style, s.filesystem, s.volume_label))[1])
            res.volume_letter = letter
            key, ids = do("Enable BitLocker", lambda: (guard(), self.backend.enable_bitlocker(
                drive, letter, method, pw, s.full_volume_encryption))[1])
            res.recovery_key, res.protector_ids = key, ids
            do("Encrypt", lambda: self.backend.wait_encrypted(drive, letter, step_progress("Encrypt"), self._cancelled))
            do("Verify", lambda: self.backend.verify_bitlocker(drive, letter, method, pw))
            do("Lock and finish", lambda: self.backend.finalize(drive, letter))

            res.ok = True
            res.outcome = "PROCESSED"
            res.sanitization_category = (
                f"Clear (overwrite x{s.overwrite_passes})" if s.overwrite_passes > 0
                else "Clear (partition removal only)")
            if not scan.history_known:
                res.destroy_note = ("Prior use unknown. On flash media an overwrite is NIST 800-88 Clear, "
                                    "not Purge. If this drive held regulated data, Destroy or a documented risk "
                                    "acceptance applies.")
        except (SafetyError, BackendError, ValueError) as e:
            res.error = str(e)
            res.outcome = "REJECTED" if isinstance(e, SafetyError) and not res.steps else "FAILED"
            if isinstance(e, Cancelled):
                res.error = "Cancelled by operator."
            res.possible_cause = explain_error(str(e), policy)
        except Exception as e:   # noqa: BLE001
            res.error = f"Unexpected error: {e}"
            res.outcome = "FAILED"
        finally:
            self.backend.release(drive.disk_number)
        res.finished = _now()
        return res
