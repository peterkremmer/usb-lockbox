![USB Lockbox](docs/images/banner.png)

# USB Lockbox

Scan, wipe and BitLocker-encrypt USB drives on a Windows 11 (Pro/Enterprise) workstation, with a big status tile per hub port and an audit trail: one CSV row and one PDF per drive. No database. Python + PySide6.

> **Status: v0.1, early.** The simulator is tested (unit tests + a headless GUI smoke test). **The Windows backend, including USB port detection, has NOT been tested on real hardware.** The app starts in **dry-run** and cannot erase a drive until you turn that off. Once you turn dry-run off the app **permanently erases the drives you process**. Use sacrificial drives on a spare machine first. No warranty (see [LICENSE](LICENSE)).

Running list of planned work and fixed issues: [ISSUES.md](ISSUES.md). Security notes: [SECURITY.md](SECURITY.md).

## What it looks like

![How it works](docs/images/how-it-works.png)

![Main window in dry-run](docs/images/screenshot-main.png)

*Main window in dry-run, one tile per USB port. Screenshots use the built-in simulator with virtual drives.*

| Settings > Ports | Settings > Encryption |
|---|---|
| ![Ports](docs/images/screenshot-settings-ports.png) | ![Encryption](docs/images/screenshot-settings-encryption.png) |

![Simulator mode](docs/images/screenshot-simulator.png)

## Run

    py -m pip install -r requirements.txt
    py -m usblockbox

or double-click `run_station.bat` / run `py run_station.py`. On Windows the app asks for administrator rights at start (UAC prompt); `--no-elevate` skips that, in which case drives can be scanned but never erased.

**On launch** the app reads the USB ports Windows reports and shows one tile per port. Plug in a hub and its ports are added; unplug it and they go away. Windows also reports connectors that are inside the machine, so use **Settings > Ports** to untick the ones you do not use and to name the rest ("Front left"). Drives on ports the app does not list or that you hid are ignored. It starts in **dry-run** (blue banner): drives are scanned and planned, nothing is erased. To erase drives, untick **Dry run** in Settings > Safety (needs Windows and administrator rights). The banner then turns red and says that any drive you process will be permanently erased. No restart. The operator name in records is the signed-in Windows user (shown at the bottom of the window).

**Simulator mode:** click **Simulator mode** in the toolbar. The window turns orange with an orange banner, and a pinned panel opens on the right where you choose how many virtual ports there are and insert or remove a virtual drive in any port (blank, used, wrong method, odd boot records, hardware-encrypted, a fake "system disk", write-protected, ...), fill all empty ports, and simulate Group Policy conflicts. **Exit simulator mode** puts everything back to normal.

## Where things are stored

Everything defaults to a `data` folder **next to the app** (the folder holding `run_station.py`):

| What | Default location |
|---|---|
| Settings | `data/settings.json` |
| CSV log | `data/records/usblockbox_log.csv` |
| PDFs | `data/records/pdf/` |
| Update backups | `.update_backup/` |

Change the CSV and PDF folders in **Settings > Records** (a "Default" button resets them). If the app folder is read-only, the per-user profile folder is used instead and Settings says so. Set the environment variable `USBLOCKBOX_HOME` to use a different base folder.

`data/` is in `.gitignore`: it holds your encrypted fixed password and records. Never commit it.

## Updates

On start-up the app asks GitHub for the latest release (one HTTPS request, nothing else sent; switch it off in Settings > Updates). If there is a newer version a green bar appears; click it to read the notes and choose **Update now**. The update is only installed when you confirm, only if the release has a zip plus a published SHA-256, and only when no drive is being processed. Your `data/` folder is never touched and replaced files are backed up. A git clone is never modified: the app tells you to `git pull`. See [RELEASING.md](RELEASING.md).

## Policy status

The status bar shows a **Policy** indicator: green = no BitLocker policy conflicts, amber = notes, red = a policy blocks processing, grey = cannot be read here (non-Windows). Hover for the summary, click for the full table of GPO / Intune settings it found. The same findings also appear on the drive tiles.

## Tiles

| Color | Text | Meaning |
|---|---|---|
| Grey | EMPTY | Nothing in the port |
| Blue | CHECKING | Read-only scan running |
| Amber | CLICK TO PROCESS | Needs work; reasons listed on the tile |
| Purple | WORKING - DO NOT REMOVE | Wiping / encrypting (progress bar) |
| Green | DONE - REMOVE / ALREADY OK - REMOVE | Verified and recorded |
| Red | STOP / FAILED - SET ASIDE | Rejected by a safety check or policy, or a step failed. Reason on the tile |

Colors are remappable in `settings.json` (`colors`, `#rrggbb` only). Every state also has an icon and words.

## What the scan checks (read-only)

USB bus / removable / size limits / a listed USB port; system disk; write-protect; hardware-encrypted or multi-LUN devices; BitLocker state (method, % encrypted, locked, station password opens it); file content; partition style, extra/odd partitions, active flag, boot code in the first sector; BitLocker policy from GPO/Intune (conflicts are reported with the setting and source, never overridden).

## Encryption choices (Settings > Encryption)

Methods are shown the way Microsoft writes them: XTS-AES-256 and XTS-AES-128 (Windows 10 version 1511 and later) and the older AES-CBC-256 / AES-CBC-128 (only for drives that must open on Windows 8.1 / Server 2012 R2 or earlier). Each choice has a hover explanation and a plain-language note under the form, including exFAT/NTFS/FAT32 and GPT/MBR trade-offs. XTS-AES is approved by NIST for storage devices (SP 800-38E); that is about the algorithm, not a FIPS 140 validation of the product. If Windows' own FIPS policy ("Use FIPS compliant algorithms") is on, Windows refuses to create a BitLocker recovery password, which this app adds to every drive, so encryption can fail on such a PC.

## Processing (on click)

Clear partitions and boot records, zero first/last MB, overwrite passes (default 3, 0 = skip), GPT + one partition, format exFAT, BitLocker XTS-AES 256 (full volume) with password + recovery protectors, wait for 100%, verify (method, protectors, unlock test), lock. Only then does the record get written and the tile go green. If the record cannot be written, the tile is NOT green.

## Safety nets

Never touches boot/system/pagefile disks, non-USB buses, fixed disks (unless allowed), drives on USB ports the app does not list, or a drive holding the app / records folders. Identity (serial + unique id + size) and eligibility are re-checked before every destructive step. Emergency stop; mid-run removal = FAILED; dry-run by default; turning dry-run off requires Windows and an elevated session.

## Records

* `usblockbox_log.csv`: one appended row per run, with `prev_hash`/`row_hash` chaining so accidental or casual edits are detectable (`records.verify_chain`). The chain is **not keyed**, so it is not proof against someone who deliberately recomputes it. Cells starting with `= + - @` get a leading `'` (Excel formula-injection guard). If Excel has the file open, writes retry for ~3 s and then the run is marked failed rather than losing the record.
* One PDF per drive, named `timestamp_serial.pdf`.
* **Passwords and recovery keys: PDF on, CSV off by default.** Switch each in Settings > Records. With the CSV on, it is a clear-text master key list: restrict access to its folder.
* The CSV doubles as the drive history (looked up by serial).
* The operator name (Settings, or the Windows user name if blank) and the computer name are written into every record.

## Honest limits

* An overwrite on flash media is NIST 800-88 **Clear**, not Purge. The record says so. Drives with unknown history are flagged for Destroy or documented risk acceptance if they held regulated or sensitive data.
* "Already compliant" drives are re-provisioned by default (empty is not proof of clean). Opt in under Settings > Safety.
* Per-organisation password profiles are reserved (`password_profile_id`) but not implemented.
* The capacity (counterfeit-drive) test is not implemented yet.

## Before you turn dry-run off (do in this order, on a spare machine, with sacrificial drives)

1. `py -m usblockbox --policy-dump` on a managed device. Compare registry names in `usblockbox/policy.py` with a real `gpresult` / Intune report (they are marked VERIFY).
2. `py -m usblockbox --selftest-windows` (read-only, elevated). Check: system disk numbers, USB disks, serials, location paths, `is_removable`, partitions, BitLocker state.
3. `py -m usblockbox --ports-dump` (read-only, elevated). Check that the hubs, port counts and port-to-drive matches are right, with and without a hub plugged in.
4. Start the app (dry-run). Confirm the tiles match the ports and the scan results on real drives. Nothing is written.
5. Settings > Safety: untick Dry run, one sacrificial drive, overwrite passes 0, then 1, then 3.
6. Endpoint-security products will likely flag raw disk writes: allow-list the install folder by path before testing.

Things not yet verified on real systems (marked VERIFY in code comments): GPO/MDM registry value names, USB location-path stability, `MediaType` for your drive models, behavior of Lock-BitLocker on removable volumes, whether Intune escrows removable-drive recovery keys, DPAPI user-scope behavior when the elevated process runs as a different account, and the wording of whatever media-sanitization or password standard applies to you.

## Tests

    py -m pip install -r requirements-dev.txt
    py -m pytest -q
    py tools\ui_smoke.py out_dir       (headless GUI; set QT_QPA_PLATFORM=offscreen on non-Windows)

## License

MIT, see [LICENSE](LICENSE).
