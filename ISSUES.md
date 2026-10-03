# USB Lockbox: running list

One place for what we are working on and what has been fixed. Newest items first inside each section.
IDs are permanent (LB-###) so commits and chat can refer to them. Move an item to **Resolved** with the date when done.

## Next up (agreed order)

- [ ] **LB-001 More UI changes.** First batch is done (LB-112 to LB-115). Add the next ones here as LB-0xx items.
- [ ] **LB-126 Verify the FIPS-policy registry check** (`config.fips_mode_enabled`, HKLM\\SYSTEM\\CurrentControlSet\\Control\\Lsa\\FipsAlgorithmPolicy) and that recovery-password creation really fails on a FIPS-policy PC; decide whether to refuse early instead of warning. Source: Microsoft KB "BitLocker recovery password not FIPS compliant".
- [ ] **LB-127 Decide: drives that hold only system junk** (FOUND.nnn, Spotlight, Recycle Bin) could count as "no user files". Currently still counted and warned.
- [ ] **LB-016 Test USB port discovery on real hardware** (HP Omen, with and without a hub): run `py -m usblockbox --ports-dump`, compare with the tiles. The SetupAPI/IOCTL code, companion-port merging and location-path matching are unit-tested only on synthetic data. Also verify the UAC relaunch.
- [ ] **LB-002 Publish:** `UPDATE_REPO` is set to `peterkremmer/usb-lockbox`; create that repo on GitHub, push, then publish v0.1.0 following RELEASING.md and check Settings > Updates > Check now against it (LB-008).
- [ ] **LB-003 Real-hardware testing of the Windows backend** (README "Before real mode"). Nothing destructive has run on real disks yet.

## Open

- [ ] **LB-004 Verify GPO/Intune registry names** in `policy.py` (marked VERIFY) against a real `gpresult` / Intune report.
- [ ] **LB-005 Verify DPAPI user scope** on Windows, including when the elevated process runs as a different account than the one that saved the password (the app then asks you to re-enter it).
- [ ] **LB-006 Capacity (counterfeit-drive) test is not implemented.** `capacity_mismatch` exists in the model and safety check, but nothing sets it. UI checkbox removed until it exists.
- [ ] **LB-007 Keyed or signed CSV log** if real tamper-resistance is wanted (today: unkeyed hash chain, accidental edits only).
- [ ] **LB-008 Verify the update flow against a real GitHub release** (checked here only with mocked responses and zip fixtures).
- [ ] **LB-010 Per-organisation password profiles** (`password_profile_id` is reserved, not implemented).
- [ ] **LB-011 Remove stale `__pycache__` folders** from the working copy (ignored by git, but they contain paths from the old machine; delete before zipping or sharing the folder by any other means).
- [ ] **LB-012 Optional: Windows CI job that runs the GUI smoke test** (the current workflow runs the non-Qt unit tests only).
- [ ] **LB-013 Verify Lock-BitLocker behaviour on removable volumes and USB location-path stability** (VERIFY comments in `windows.py`).

- [ ] **LB-132 "Could not determine the new drive letter" on a flash drive (real hardware, 2026-10-02):** after Clear-Disk the drive reports MBR with one whole-disk partition of type FAT16, so Initialize-Disk says "already initialized" and New-Partition says "Not enough available capacity". Likely Windows treating a blank removable drive as a "superfloppy" (unconfirmed). The partition/format step also swallows PowerShell errors (only the last command's result is checked): **Changed 2026-10-02, needs a retest on the same drive:** partitioning now uses diskpart (clean, convert, create partition primary), which worked by hand on this drive, then PowerShell formats with errors set to stop and reads the drive letter back. Move to Resolved once a full run succeeds.

## Resolved

### 2026-10-02 (UI batch 2)

- [x] **LB-116 App launches on real hardware.** Reads the computer's USB hubs and ports and lists them; hub ports appear and disappear live. Drives on unlisted ports are ignored and counted as unassigned. (Needs LB-016 to be confirmed on Windows.)
- [x] **LB-117 No simulator wording at launch.** Launch shows a blue "Dry run" banner (or red "Real mode"). Dry run is the default; real mode is a live Settings switch behind the typed phrase, Windows and administrator only.
- [x] **LB-118 Simulator mode is orange** (window, banner, flyout), with its own virtual-port count, and the word "service" is gone from the app.
- [x] **LB-120 File count breakdown.** A drive with 13 visible files showed 6388: hidden folders are counted (correctly, since erase removes them) but the tile did not say so. It now shows top-level count and the biggest folders, with hidden ones marked. Open: confirm on real hardware what the 6375 extra files are (LB-121).
- [x] **LB-014 Settings PIN removed** (code, UI, tests, docs). Old `settings_pin_hash` values in a settings file are ignored and dropped on next save.
- [x] **LB-009 Working folder renamed** to `USB Lockbox`.
- [x] **LB-130 Publishing prep:** `UPDATE_REPO` set to `peterkremmer/usb-lockbox`, LICENSE holder is Peter Kremmer, a dry-run `git add` showed 46 files and nothing from `data/`, and a scan of them found no personal paths or addresses.
- [x] **LB-128 Typed erase phrase removed.** Turning dry-run off is the checkbox only (still needs Windows and administrator rights); the red banner is the reminder.
- [x] **LB-129 Hover help showed only a ? cursor.** Tooltips now use explicit colours app-wide, the (i) is a button (hover shows help, click opens it in a window) and the question-mark cursor is gone.
- [x] **LB-121 Tile text clipped** with many findings: the findings area now scrolls.
- [x] **LB-122 Banner wording.** "Real mode" removed from the window; dry-run is blue, erase-enabled is a red warning. Confirmation phrase is `ERASE DRIVES`. Old settings files start in dry-run once.
- [x] **LB-123 Station tab removed**; operator = signed-in user, shown in the status bar.
- [x] **LB-124 Settings overhaul:** info bubbles, descriptions with compatibility/FIPS notes, XTS-AES-256 style names (Microsoft's Enable-BitLocker docs write them that way), controls beside their labels, fixed password shown, unused password options hidden.
- [x] **LB-125 Junk-folder names** in the file count (FOUND.nnn disk-check folders, Recycle Bin, Mac Spotlight/Trashes/fseventsd, Linux Trash/lost+found, Mac ._ files).
- [x] **LB-119 Settings > Ports.** First `--ports-dump` on the HP Omen (26-port root hub + one 5-port hub) showed Windows marks almost every port user-connectable, so 13 tiles appeared. Ports can now be hidden and named; hidden ports are ignored like unlisted ones. Port discovery itself matched Windows' data (companion merging, hub ports, drive-to-port match all worked on real hardware).
- [x] **LB-015 Calibrate ports removed** (obsolete: ports are detected automatically). Port-count setting removed too.

### 2026-10-02 (UI batch 1)

- [x] **LB-112 Port count is a live change.** Tiles and simulator rows rebuild on save; busy ports are never removed. Only simulation <-> real mode still needs a restart (the backend and elevation change).
- [x] **LB-113 Simulator service mode:** toggle recolors the window (cyan frame, banner and title say so) and opens a pinned side panel with per-port scenario/insert/remove, fill, remove all, policy scenarios and an exit button. Replaces the context menu.
- [x] **LB-114 Policy report button replaced** by a status-bar indicator with tooltip and click-through details; refreshed at start-up, after Settings changes and when the simulated policy changes (which now also re-scans the ports).
- [x] **LB-115 Window title shows the mode** (Simulation, Dry run, REAL MODE, Simulator service mode).

### 2026-10-02 (pre-publication pass)

- [x] **LB-100 Renamed** to USB Lockbox / `usblockbox`; name and update repo live in `usblockbox/appinfo.py`.
- [x] **LB-101 Default storage under the running folder.** Settings, CSV and PDF default to `<app>/data/...`; blank/"Default" follows the app if it moves; read-only app folder falls back to the profile folder; `USBLOCKBOX_HOME` override.
- [x] **LB-102 Update check + optional self-update** (GitHub Releases, checksum required, confirmation required, git clones untouched, `data/` preserved, backups). 32 new tests.
- [x] **LB-103 Settings file treated as untrusted** (allow-lists and clamps; PowerShell-injection and real-mode-flag cases tested). It is read by an elevated process and now lives in the app folder.
- [x] **LB-104 Fixed password no longer shown again** in the Settings dialog (it used to be decrypted into the field, revealable with "Show").
- [x] **LB-105 DPAPI scope** changed from whole-machine to current account (old blobs still readable); decrypt failure now gives a clear "re-enter it" message instead of a crash.
- [x] **LB-106 Safer record defaults:** CSV no longer holds passwords/recovery keys by default; Settings warns about what each choice means.
- [x] **LB-107 README overstated the hash chain** ("edits are detectable"); reworded to what it really gives.
- [x] **LB-108 Organisation-specific text removed** (endpoint-product names in an error hint, compliance-standard reference, default volume label).
- [x] **LB-109 Repo files:** LICENSE (MIT), `.gitignore` (data, settings, records, caches), SECURITY.md, CHANGELOG.md, RELEASING.md, CI workflow, `requirements-dev.txt`.
- [x] **LB-110 Settings UI review:** PIN confirmation + remove-PIN (a typo could lock you out), min/max size validation, password fields follow the selected mode, "Default" buttons for folders, wrapped notes, estimate computed from the pass count, removed read-only profile field and the dead capacity checkbox, new Updates tab.
- [x] **LB-111 Secrets / personal-info scan** of source, docs, tests and tools: no keys, tokens, addresses or personal paths found. Only dummy simulator password in `backends/simulated.py` (labelled).
- [x] **LB-131 Update check said "Could not reach GitHub: HTTP Error 404":** GitHub answers 404 for "latest release" when none is published. The app now says "No release has been published yet", and other HTTP errors show their status. Tests added (80 pass).
