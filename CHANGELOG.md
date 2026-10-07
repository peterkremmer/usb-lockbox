# Changelog

What changed for people using the app. The notes on each GitHub release repeat the matching section.

## 0.1.2

- The launcher files are now `run_usblockbox.bat` and `run_usblockbox.py` (they were `run_station.*`). **Because of this, 0.1.0 and 0.1.1 cannot update themselves to this version: download the zip from the release page instead.**
- Wording: the old "station" terms are gone. Messages say "the fixed password" and "this computer", and the PDF record is titled "USB Lockbox Drive Record".
- Times in the batch view say "just now" instead of "under 1 min ago".
- README rewritten with current screenshots, install steps and a troubleshooting section.
- `run_usblockbox.bat` now checks the setup before starting. It finds a suitable Python (3.10 to 3.14, 64-bit, installed for all users), checks that the packages in `requirements.txt` are installed system-wide and recent enough, and offers to install or update them with administrator approval. When something is missing it explains what to do and keeps its window open.
- New read-only diagnostic `--startup-timing` times each step of the first scan, to find out why the ports can take long to appear on some PCs.
- The empty window now says "Scanning USB ports..." (with the seconds elapsed, and a note after 45 seconds that this PC is slow to answer) instead of "No USB ports detected yet...". "No USB ports found." appears only after a scan has finished.
- Logging for troubleshooting in `data\logs`: a rotating log (1 MB, 5 files, 30 days), crash capture and a launcher log. It records what the app did, how long each PowerShell call and the first scan took, and any error; it never records passwords or recovery keys. **Diagnostics > Save diagnostics for support...** bundles the logs and the settings (without secrets) into one zip to send.

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
