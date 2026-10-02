# Security notes

USB Lockbox handles drive passwords and BitLocker recovery keys and runs elevated, so it is worth knowing what it does and does not protect. This file is the result of a review done before first publication (2026-10-02); fixed items are listed in [ISSUES.md](ISSUES.md).

## Reporting a problem

Please use GitHub's **private vulnerability reporting** on the repository (Security tab > Report a vulnerability), not a public issue. Do not post real passwords, recovery keys, serial numbers or records in issues.

## What is stored where

* `data/settings.json`: settings. The fixed password is stored encrypted with Windows DPAPI for the **current Windows account** (a different account, or a different PC, cannot read it). On non-Windows development machines the "encryption" is only base64: the simulator is the only mode there.
* `data/records/`: the CSV and per-drive PDFs. The PDF includes the password and recovery key by default; the CSV does not. Treat both folders as sensitive either way (serial numbers, operator and computer names).
* Nothing is sent anywhere except the optional update check (an HTTPS request to GitHub).

## Design points

* Passwords never go on a command line: they reach PowerShell through the child process environment (`USBLOCKBOX_PW`).
* `settings.json` is treated as untrusted input: every value is checked against an allow-list or clamped on load, because several values end up inside PowerShell commands and the app runs elevated. Anything unrecognised falls back to a safe default; a non-boolean can never switch on real mode.
* Updates: HTTPS only, GitHub hosts only (also across redirects), SHA-256 verified before anything is written, zip-slip and symlink checks, `data/` never overwritten, backup of replaced files, always user-confirmed. Source-archive releases cannot be checksummed in advance, so they are never auto-installed.
* The unit tests include hostile settings, zip-slip, symlink, checksum-mismatch and redirect-host cases.

## Known limits (not bugs)

* **Anyone who can edit `settings.json` or the code can change settings** (including turning dry-run off). Use Windows file permissions on the app folder.
* **Run real mode from a folder that only administrators can write.** The app runs elevated and imports its own code; if a standard user can modify the app folder they can run code as admin. The same applies to `data/settings.json` (it is validated, but you should not rely on that alone).
* **The CSV hash chain is tamper-evident for accidents, not tamper-proof**: it is unkeyed, so someone who deliberately edits and recomputes it is not detected. Ship the log somewhere append-only if you need that.
* The fixed-password mode means one leaked password opens every drive that used it. Prefer `generated`.
* Endpoint-security software may flag raw disk writes. That is expected.
* The Windows backend has not been tested on real hardware yet.

* The fixed password ("same password for every drive") is shown in clear text in Settings, by design: every recipient is told it. It is still stored encrypted (DPAPI, current account) in `data/settings.json`.
