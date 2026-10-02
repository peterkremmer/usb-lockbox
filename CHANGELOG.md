# Changelog

All notable changes. Release notes on GitHub should repeat the matching section.

## Unreleased

- Renamed the project to **USB Lockbox** (package `usblockbox`).
- Settings, records and PDFs now default to a `data/` folder next to the app instead of the home folder and
  `%APPDATA%`. Every location can still be changed in Settings; blank/"Default" means "next to the app".
- Update check against GitHub Releases at start-up, with an optional, confirmed, checksum-verified self-update.
- Settings file is validated on load (values end up in PowerShell commands) and written atomically.
- Fixed password is encrypted for the current Windows account only (was: any account on the machine). Old blobs
  are still readable. The Settings dialog no longer shows the saved password.
- CSV records no longer include passwords/recovery keys by default (PDF still does; both are switchable).
- Settings dialog: PIN confirmation and removal, size-limit validation, mode-aware password fields, records
  warnings, new Updates tab. Removed the capacity-test checkbox (it was never wired to anything).
- Removed organisation-specific wording from code and defaults.
- The app now starts on the real hardware: it reads the computer's USB ports (hubs, port counts) and shows one tile per port, adding or removing tiles live when a hub is plugged in or out. No more "simulation mode" at launch, no port-count setting, no calibration step.
- Starts in dry-run (scan and plan only). Turning dry-run off (to erase) is a live Settings switch (Windows, administrator, typed phrase), no restart. Windows asks for administrator rights at launch (`--no-elevate` to skip).
- Drives on USB ports the app does not list are ignored.
- Simulator mode is a toolbar toggle: orange window, orange banner and a pinned side panel with its own virtual-port count, per-port insert/remove, fill and policy scenarios. Exit restores the normal look and the real ports.
- Settings > Ports: hide ports you don't use (Windows lists internal connectors as user-connectable) and give ports names.
- The file count on a tile now says where the files are (top level vs. folders, hidden ones marked), because hidden folders are counted too.
- No "Real mode" wording: the window says "DRY RUN" or, once dry-run is off, a red warning that processed drives will be erased. The Settings confirmation phrase is now `ERASE DRIVES`.
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
