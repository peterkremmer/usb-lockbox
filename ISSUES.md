# USB Lockbox: open issues

Known gaps and things still to verify. IDs (LB-###) are permanent so commits and discussions can refer to them.

## Open

- [ ] **LB-126 Verify the FIPS-policy registry check** (`config.fips_mode_enabled`, `HKLM\SYSTEM\CurrentControlSet\Control\Lsa\FipsAlgorithmPolicy`) and that recovery-password creation really fails on a FIPS-policy PC. Decide whether to refuse early instead of warning.
- [ ] **LB-127 Drives that hold only system junk** (FOUND.nnn, Spotlight, Recycle Bin) could count as "no user files". Today they are still counted and warned about.
- [ ] **LB-016 Test USB port discovery on more hardware**, with and without a hub: run `py -m usblockbox --ports-dump` and compare with the tiles. Also verify the UAC relaunch.
- [ ] **LB-003 Real-hardware testing of the Windows backend.** A full run on one drive works; more drive models and hubs are still to be tried.
- [ ] **LB-004 Verify GPO/Intune registry names** in `policy.py` (marked VERIFY) against a real `gpresult` / Intune report.
- [ ] **LB-005 Verify DPAPI user scope**, including when the elevated process runs as a different account than the one that saved the password (the app then asks you to re-enter it).
- [ ] **LB-006 Capacity (counterfeit-drive) test is not implemented.** `capacity_mismatch` exists in the model and safety check, but nothing sets it.
- [ ] **LB-007 Keyed or signed CSV log**, if real tamper-resistance is wanted (today: an unkeyed hash chain, which only catches accidental edits).
- [ ] **LB-008 Verify the self-update flow against a real GitHub release:** update an unzipped older copy via Settings > Updates and confirm the files are replaced.
- [ ] **LB-010 Per-organisation password profiles** (`password_profile_id` is reserved, not implemented).
- [ ] **LB-012 Optional: Windows CI job** that runs the GUI smoke test (the current workflow runs the non-Qt unit tests only).
- [ ] **LB-013 Verify Lock-BitLocker behaviour on removable volumes** and USB location-path stability (VERIFY comments in `windows.py`).
- [ ] **LB-133 Drive health (SMART):** not implemented. Plain flash drives normally expose no SMART data; some USB-to-SATA/NVMe enclosures do. Next step: a read-only `--health-dump` to see what Windows reports, then decide.
- [ ] **LB-134 Unattended-batch timing needs tuning on real batches.** Estimates, stall/slow/overdue warnings, the batch strip, taskbar flash and beep are covered by unit tests and the headless smoke test. Defaults to review: stall 15 min, slow = more than 5x a usual speed measured at least 3 times, overdue = 2x expected.
- [ ] **LB-135 Shell Hardware Detection pause needs more real runs.** If the app is killed mid-run the service stays stopped until reboot or `Start-Service ShellHWDetection`.

- [ ] **LB-142 First scan can take minutes on a PC with many USB drives and hubs.** A log from a PC with 9 drives and 16 hubs showed the drive listing at 89.6 s and the hub-location lookup timing out at 60 s. 0.2.1 reads locations natively, shows port tiles first, reads each drive once and isolates a drive that will not answer. Still to verify on that PC: first-scan time and `--compare-native`.
- [ ] **LB-144 Direct partition and volume reads are off by default** (`USBLOCKBOX_NATIVE_DISKS=1` turns them on). Turn them on by default only if `--compare-native` shows PowerShell's answers match on real hardware and the per-query timings show they are needed.
- [ ] **LB-149 Shared USB links: forecasts need real-hardware confirmation.** 0.2.1 forecasts time left and judges slowness using each drive's share of its hub link (capacities are rules of thumb by USB speed; measured rates take over when available). Verify against `Throughput ...` log lines with 3-4 drives on one hub.
- [ ] **LB-145 Limit on drives processed at once:** only up to 4 drives at once have been tested. Decide on a cap after testing more.
- [ ] **LB-138 Port tiles can take about a minute to appear on a PC with endpoint-security software.** Cause not measured. Next step: the diagnostics bundle from that PC, then fix the slowest step.
- [ ] **LB-139 The launcher check is untested on a clean Windows PC.** `run_usblockbox.bat` and `tools/preflight.py` are unit-tested for their logic only. Try it on a PC with no Python, with a per-user Python, and with a system Python missing the packages.

## Resolved

- [x] **LB-148 Encryption could stall or run very slowly while the drive listing re-read the same drive** (locks and unlocks it to test the password). Processing drives are now left alone (0.2.1). To confirm on hardware: two drives encrypting together should show steadily rising percentages in the log.
- [x] **LB-146 False "N times slower than usual" warnings** from a speed measured once; and **strange characters in a drive serial number**. Fixed in 0.2.1.
- [x] **LB-147 Large drive on a USB 2.0 port or hub** now gets a note with a time estimate (0.2.1). **A drive that will not answer** now shows "NOT RESPONDING" on its tile (0.2.1).
- [x] **LB-143 Every wipe failed on a PC with 9 USB drives.** The per-step safety re-check listed all USB drives each time and timed out. It now reads only the one drive, one at a time (0.2.1).
- [x] **LB-140 Simulator showed no ports while the first real scan was still running.** Switching modes now abandons the old scan instead of waiting for it. Covered by the GUI smoke test.
- [x] **LB-141 Install friction:** launcher checks Python and packages, README Get started section, diagnostics log and support file, "Scanning..." message.
- [x] **LB-132 "Could not determine the new drive letter" on a flash drive.** A cleared drive can report MBR with one whole-disk partition, so `Initialize-Disk` and `New-Partition` fail. Partitioning now uses diskpart (clean, convert, create), formatting stops on errors, and the drive letter is assigned last. Confirmed with a full run on real hardware.
- [x] **LB-136 Old-name leftovers removed** (launcher files, wording, PDF title). The self-update check no longer depends on a launcher file name. 0.1.0 and 0.1.1 look for the old launcher, so they need a manual download for the next release.
- [x] **LB-137 Docs refresh:** README rewritten, CHANGELOG cut down to what changed per release.
- [x] **LB-131 Update check showed "HTTP Error 404"** when no release existed; it now says so plainly.
- [x] **LB-002 Published:** repo and releases are live; the release workflow builds the zip and `SHA256SUMS` from a pushed tag.
- [x] **LB-001 to LB-130 (earlier work):** rename to USB Lockbox, update check and self-update, hardened settings handling, safer record defaults, settings overhaul, live port discovery with hide/name, simulator service mode, dry-run banner, PIN and typed erase phrase removed, tooltips, file-count breakdown, repo files (LICENSE, SECURITY, CHANGELOG, RELEASING, CI). See the git history for details.
