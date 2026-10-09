# Changelog

What changed for people using the app. The notes on each GitHub release repeat the matching section.

## 0.2.2

- Fix: when several drives were started together, one or two could fail at "Partition and format" with "The specified disk is not convertible". That step now runs one drive at a time and retries once after refreshing Windows' disk list.

## 0.2.1

- Fixed: on a PC where Windows answers drive queries slowly, every drive could fail. The safety re-check before each step now reads only that one drive, one at a time, instead of listing all USB drives (about 90 seconds with 9 drives). A step that still times out now says so plainly and tells you the drive may be left wiped but not formatted, so process it again.
- Fixed: the drive listing could interfere with a drive that was being encrypted. To find out whether an encrypted drive opens with the fixed password, the app locks the volume and unlocks it again, and it did this while re-reading drives every few minutes, including drives being processed. Reading a drive is now strictly read-only (Windows' BitLocker status only). The password test runs once, when a drive is first checked, and only on a drive Windows reports as fully encrypted and that the app is not working on. A drive that is still encrypting is never locked. Drives are re-read every 30 minutes instead of every 5, or on a replug or **Rescan**. The log records the encryption percentage and Windows' status every two minutes.
- Fixed: drives running normally could be flagged "N times slower than usual". That warning now needs the usual speed to have been measured at least three times and a gap of more than five times.
- Faster start on PCs with many USB drives and hubs. Hub and drive locations are read straight from Windows (PowerShell is the fallback), the port tiles appear first marked "READING DRIVES" with the time shown in the status bar, and each drive's details are read once and remembered instead of on every scan. One drive that will not answer no longer hides the others: its tile says "NOT RESPONDING" and it is tried again after 30 seconds. **Rescan** reads everything again.
- New: a note on the tile when a drive is connected at USB 2.0 speed (a USB 2.0 port or hub) and the job would take over an hour, with a rough time estimate. It never blocks anything.
- New: time left and "running slow" warnings now take into account drives that share one hub or port, and adjust as other drives finish. A tile shows an "ⓘ" line when drives are sharing a link. If anything cannot be determined, the simple estimate is used.
- Drive serial numbers are cleaned to printable characters, so a drive that reports a strange serial no longer shows a box or stray symbol.
- The app now has an icon: the padlock from the README banner.
- Diagnostics: the log now records each drive's progress through every step, as it happens: the step's percentage whenever it changes (for example "PORT 3: Encrypt 51%"), how long the step has run, its average speed, and, if the percentage stops moving, how long it has been unchanged (a line is also written every two minutes while a step is running).
- Diagnostics: while drives are being processed, the log records once a minute how fast each drive really reads and writes (Windows disk counters) and which USB links they share, with the speed of each hub uplink, so a slow batch can be told apart as a slow drive or drives sharing one link. Nothing changes in how the app behaves.
- Diagnostics: the log shows how long each query took per drive, and `--compare-native` prints the PowerShell and direct answers side by side (locations, partitions, volumes, port speed). Reading partitions and volumes directly is available as an experiment (`USBLOCKBOX_NATIVE_DISKS=1`) and stays off until that comparison agrees on real hardware.

## 0.2.0

- Fixed: switching to Simulator mode (or back) while the first real scan was still running made the new view wait for the old scan, so the simulator showed no ports until that scan finished. The old scan is now ignored.
- README: a new "Get started" section at the top with the three steps (install 64-bit Python for all users, download, double-click `run_usblockbox.bat`), a picture of the steps, and a note on what to expect on first start.
- Docs: the README lists `data\logs` in "Where things are stored" and warns that a diagnostics file holds drive serial numbers and your Windows user and PC name, so send it privately rather than posting it in a public issue. SECURITY.md describes what the logs contain. RELEASING.md now says to tag the commit that holds the version bump and how to recover from a failed release run.

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
