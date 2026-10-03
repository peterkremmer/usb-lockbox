# Changelog

What changed for people using the app. The notes on each GitHub release repeat the matching section.

## 0.1.2

- The launcher files are now `run_usblockbox.bat` and `run_usblockbox.py` (they were `run_station.*`). **Because of this, 0.1.0 and 0.1.1 cannot update themselves to this version: download the zip from the release page instead.**
- Wording: the old "station" terms are gone. Messages say "the fixed password" and "this computer", and the PDF record is titled "USB Lockbox Drive Record".
- Times in the batch view say "just now" instead of "under 1 min ago".
- README rewritten with current screenshots, install steps and a troubleshooting section.

## 0.1.1

- Fixed "Could not determine the new drive letter" on USB flash drives.
- Windows' "You need to format the disk" prompt no longer appears during a run.
- Walk-away batches: every working tile shows when it started, about how long is left and when it should finish, and a strip under the banner shows when the whole batch should finish. A drive that stalls (15 minutes without progress by default, set in Settings > Safety), runs far slower than usual, or runs far past its expected time turns amber and is named in the strip. The taskbar button flashes and a beep sounds when the batch finishes or a drive needs a look.
- The update check says "No release has been published yet" instead of an HTTP 404 error.

## 0.1.0

First public release.

- One tile per USB port, read from Windows; plugging in a hub adds its ports. Hide and name ports in Settings.
- Scans each drive, wipes it (optional overwrite passes), formats it, turns on BitLocker (XTS-AES-256 by default) with a password and a recovery key, and verifies the result.
- Starts in dry-run: scans and plans, erases nothing, until you turn it off in Settings.
- A CSV row and a PDF record for every drive.
- Simulator mode for trying it without real drives.
- Optional update check against GitHub Releases.
