"""Read BitLocker-related policy (GPO and Intune) and report conflicts instead of just erroring.

The app DIAGNOSES; it never overrides policy. Registry value names below are recalled from
Microsoft's BitLocker policy documentation and MUST BE VERIFIED on a real Entra-joined/Intune
box (run `python -m usblockbox --policy-dump` and compare). They are data, not logic: fix the
tables here if a name is wrong.
"""
from __future__ import annotations

import re
from typing import Optional

from .config import IS_WINDOWS, Settings, method_label
from .models import PolicyItem, PolicyReport
from .passwords import has_complexity

GPO_KEY = r"SOFTWARE\Policies\Microsoft\FVE"
MDM_KEY = r"SOFTWARE\Microsoft\PolicyManager\current\device\BitLocker"

# VERIFY: numeric codes used by "Choose drive encryption method and cipher strength".
XTS_CODES = {3: "Aes128", 4: "Aes256", 6: "XtsAes128", 7: "XtsAes256"}

# VERIFY each name against Microsoft docs / a gpresult of a real device.
GPO_REMOVABLE_METHOD = "EncryptionMethodWithXtsRdv"
GPO_REMOVABLE_CONFIGURE = "RDVConfigureBDE"        # 0 = BitLocker on removable drives disabled
GPO_REMOVABLE_DENY_WRITE = "RDVDenyWriteAccess"    # 1 = unencrypted removable drives read-only
GPO_PASS_LENGTH = "RDVPassphraseLength"
GPO_PASS_COMPLEXITY = "RDVPassphraseComplexity"


def read_raw() -> dict[str, dict]:
    """{'GPO': {...}, 'Intune/MDM': {...}} straight from the registry. Empty off Windows."""
    raw: dict[str, dict] = {"GPO": {}, "Intune/MDM": {}}
    if not IS_WINDOWS:
        return raw
    import winreg

    for source, key in (("GPO", GPO_KEY), ("Intune/MDM", MDM_KEY)):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as h:
                i = 0
                while True:
                    try:
                        name, value, _ = winreg.EnumValue(h, i)
                    except OSError:
                        break
                    raw[source][name] = value
                    i += 1
        except OSError:
            pass
    return raw


def evaluate(raw: dict[str, dict], settings: Settings,
             password: Optional[str] = None) -> PolicyReport:
    rep = PolicyReport(available=True)
    requested = settings.encryption_method
    rep.effective_method = requested
    gpo = raw.get("GPO", {})
    mdm = raw.get("Intune/MDM", {})

    if not gpo and not mdm:
        rep.items.append(PolicyItem("BitLocker policy", "none found", "derived", "ok",
                                    "No GPO or Intune BitLocker settings detected on this computer."))

    # 1) BitLocker on removable drives disabled
    if gpo.get(GPO_REMOVABLE_CONFIGURE) == 0:
        rep.items.append(PolicyItem(GPO_REMOVABLE_CONFIGURE, "0", "GPO", "block",
                                    "Group Policy disables BitLocker on removable drives. "
                                    "This computer cannot encrypt drives until the policy is scoped out."))

    # 2) Encryption method forced by policy
    if GPO_REMOVABLE_METHOD in gpo:
        forced = XTS_CODES.get(gpo[GPO_REMOVABLE_METHOD], f"code {gpo[GPO_REMOVABLE_METHOD]}")
        if forced == requested:
            rep.items.append(PolicyItem(GPO_REMOVABLE_METHOD, forced, "GPO", "ok",
                                        "Policy method matches the requested method."))
        elif settings.on_policy_method_conflict == "use_policy":
            rep.effective_method = forced
            rep.items.append(PolicyItem(GPO_REMOVABLE_METHOD, forced, "GPO", "warn",
                                        f"Requested {method_label(requested)} but policy sets {method_label(forced)}. "
                                        f"Settings say to follow policy, so {method_label(forced)} will be used."))
        else:
            rep.items.append(PolicyItem(GPO_REMOVABLE_METHOD, forced, "GPO", "block",
                                        f"Requested {method_label(requested)} but Group Policy sets {method_label(forced)} for "
                                        f"removable drives. Change the policy scope, or set "
                                        f"'follow policy' in Settings > Encryption."))
    for name, val in mdm.items():
        if re.search(r"removable", name, re.I) and re.search(r"method|encrypt", name, re.I):
            rep.items.append(PolicyItem(name, str(val), "Intune/MDM", "warn",
                                        "Intune sets a removable-drive encryption value. Compare it with "
                                        "the requested method; this app cannot interpret MDM values "
                                        "automatically until names are verified."))

    # 3) Deny write to unencrypted removable
    if gpo.get(GPO_REMOVABLE_DENY_WRITE) == 1:
        rep.items.append(PolicyItem(GPO_REMOVABLE_DENY_WRITE, "1", "GPO", "warn",
                                    "Policy denies write access to unencrypted removable drives. "
                                    "Formatting may fail until encryption starts."))

    # 4) Password rules
    rep.min_password_length = int(gpo.get(GPO_PASS_LENGTH, 0) or 0)
    rep.password_complexity_required = bool(gpo.get(GPO_PASS_COMPLEXITY, 0) == 1)
    if password is not None:
        if rep.min_password_length and len(password) < rep.min_password_length:
            rep.items.append(PolicyItem(GPO_PASS_LENGTH, str(rep.min_password_length), "GPO", "block",
                                        f"Configured password is {len(password)} chars; policy requires "
                                        f"{rep.min_password_length}."))
        if rep.password_complexity_required and not has_complexity(password):
            rep.items.append(PolicyItem(GPO_PASS_COMPLEXITY, "1", "GPO", "block",
                                        "Policy requires a complex password and the configured one is not."))
    return rep


SIM_RAW: Optional[dict] = None     # Simulator mode hook: pretend these policy values exist (the controller passes it in)


def probe(settings: Settings, password: Optional[str] = None, raw: Optional[dict] = None) -> PolicyReport:
    if raw is None:
        if not IS_WINDOWS:
            r = PolicyReport(available=False,
                             note="Policy probe needs Windows; no policy information in this mode.")
            r.effective_method = settings.encryption_method
            return r
        raw = read_raw()
    return evaluate(raw, settings, password)


_ERROR_HINTS = [
    (r"0x80070005|access (is )?denied|unauthorized", "Access denied. Could be missing elevation, "
     "Group Policy/Intune restricting removable storage, or an endpoint-security / device-control product blocking raw disk access."),
    (r"0x8031004e|0x80310|FVE_E_|group policy|policy", "BitLocker reported a policy conflict "
     "(method, protector or passphrase rule)."),
    (r"write[- ]protect|read[- ]only", "Drive or policy is making the device read-only."),
    (r"timed out", "Windows (or security software on this PC) took too long to answer a drive query. "
     "Nothing was lost by the timeout itself, but a drive may be left wiped and unformatted: process it again. "
     "If it keeps happening, use Diagnostics > Save diagnostics for support and send the file."),
    (r"not supported|0x80070032", "The operation is not supported on this device or edition."),
]


def explain_error(message: str, report: Optional[PolicyReport]) -> str:
    """Best-effort human explanation of a failure. Empty string if nothing useful to add."""
    parts = [hint for pat, hint in _ERROR_HINTS if re.search(pat, message or "", re.I)]
    if report and report.blocks:
        parts.append("Active policy findings: " + "; ".join(i.message for i in report.blocks))
    elif report and report.warnings:
        parts.append("Policy notes: " + "; ".join(i.message for i in report.warnings))
    return " ".join(parts)
