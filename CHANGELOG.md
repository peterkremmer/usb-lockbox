# Changelog

All notable changes. Release notes on GitHub should repeat the matching section.

## Unreleased

## 0.1.1

- Renamed the project to **USB Lockbox** (package `usblockbox`).
- Settings, records and PDFs now default to a `data/` folder next to the app instead of the home folder and
  `%APPDATA%`. Every location can still be changed in Settings; blank/"Default" means "next to the app".
- Update check against GitHub Releases at start-up, with an optional, confirmed, checksum-verified self-update.
- Settings file is validated on load (values end up in PowerShell commands) and written atomically.
- Fixed password is encrypted for the current Windows account only (was: any account on the machine). Old blobs
  are still readable. The Settings dialog no longer shows the saved password.
- CSV records no longer include passwords/recovery keys by default (PDF still does; both are switchable).
- Settings dialog: size-limit validation, mode-aware password fields, records warnings, new Updates tab, info
  bubbles and live descriptions. Removed the capacity-test checkbox (it was never wired to anything). There is no
  Settings PIN.
- Removed organisation-specific wording from code and defaults.
- The app now starts on the real hardware: it reads the computer's USB ports (hubs, port counts) and shows one tile per port, adding or removing tiles live when a hub is plugged in or out. No more "simulation mode" at launch, no port-count setting, no calibration step.
- Starts in dry-run (scan and plan only). Turning dry-run off (to erase) is a live Settings switch (Windows, administrator), no restart. Windows asks for administrator rights at launch (`--no-elevate` to skip).
- Drives on USB ports the app does not list are ignored.
- Simulator mode is a toolbar toggle: orange window, orange banner and a pinned side panel with its own virtual-port count, per-port insert/remove, fill and policy scenarios. Exit restores the normal look and the real ports.
- Settings > Ports: hide ports you don't use (Windows lists internal connectors as user-connectable) and give ports names.
- The file count on a tile now says where the files are (top level vs. folders, hidden ones marked), because hidden folders are counted too.
- No "Real mode" wording: the window says "DRY RUN" or, once dry-run is off, a red warning that processed drives will be erased.
- Operator name is the signed-in Windows user, shown in the status bar; the Station tab is gone.
- Settings: info bubbles, a live plain-language description of the encryption/filesystem/partition choices (compatibility, 2 TB / 4 GB limits, FIPS notes), cipher names as XTS-AES-256 etc., labels on the left with their controls beside them, the fixed password is shown, and password options that do not apply to the chosen mode are hidden.
- Windows system folders on a drive get plain names in the file count (disk-check FOUND.nnn, Recycle Bin, Mac Spotlight/Trashes/fseventsd, Linux Trash, lost+found, Mac ._ files). Tile findings scroll instead of being cut off.
- Removed the settings PIN and the typed `ERASE DRIVES` confirmation: turning dry-run off is just the Settings checkbox (Windows + administrator), and the banner turns red.
- Hover help is drawn with explicit colours and every (i) is also a button that opens the help in a window; before, hover could show an empty box.
- Settings files from older builds start in dry-run once.
- New `--ports-dump` diagnostic. Removed "Calibrate ports".
- The "Policy report" button is replaced by a status-bar indicator (tooltip + click for details); it refreshes at start-up, after Settings changes and in the simulator.

## 0.1.0

- First internal version: simulator, scan, wipe, BitLocker, hash-chained CSV + PDF records.
- Update check: "No release has been published yet" instead of "Could not reach GitHub: HTTP Error 404"; other HTTP errors show their status.
- Partitioning uses diskpart (clean, convert) then PowerShell (create partition, format, then assign the letter) and stops at the first error with Windows' own message. Fixes "Could not determine the new drive letter" on USB flash drives that Windows reports as one whole-disk MBR partition after a wipe.
- Windows' Shell Hardware Detection service is paused while a drive is partitioned and formatted (and always restarted), so Explorer's "You need to format the disk" prompt cannot appear mid-run.
- For batches nobody is watching: each working tile shows when it started, how long it has run, about how long is left and when it should finish; a strip under the banner shows how many drives are working and when the whole batch should finish (and, afterwards, when it finished and how long it took). A drive with no progress for a set time (Settings > Safety, default 15 min), far slower than usual, or far past its expected time turns amber and is named in the strip; nothing is stopped. The taskbar button flashes and a beep sounds when the batch finishes or a drive needs a look. Estimates start as rough guesses and use rates measured on this computer (`data/timings.json`).
