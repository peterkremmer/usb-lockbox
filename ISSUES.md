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
- [ ] **LB-134 Unattended-batch timing needs tuning on real batches.** Estimates, stall/slow/overdue warnings, the batch strip, taskbar flash and beep are covered by unit tests and the headless smoke test. Defaults to review: stall 15 min, slow = 3x usual, overdue = 2x expected.
- [ ] **LB-135 Shell Hardware Detection pause needs more real runs.** If the app is killed mid-run the service stays stopped until reboot or `Start-Service ShellHWDetection`.

## Resolved

- [x] **LB-132 "Could not determine the new drive letter" on a flash drive.** A cleared drive can report MBR with one whole-disk partition, so `Initialize-Disk` and `New-Partition` fail. Partitioning now uses diskpart (clean, convert, create), formatting stops on errors, and the drive letter is assigned last. Confirmed with a full run on real hardware.
- [x] **LB-136 Old-name leftovers removed** (launcher files, wording, PDF title). The self-update check no longer depends on a launcher file name. 0.1.0 and 0.1.1 look for the old launcher, so they need a manual download for the next release.
- [x] **LB-137 Docs refresh:** README rewritten, CHANGELOG cut down to what changed per release.
- [x] **LB-131 Update check showed "HTTP Error 404"** when no release existed; it now says so plainly.
- [x] **LB-002 Published:** repo and releases are live; the release workflow builds the zip and `SHA256SUMS` from a pushed tag.
- [x] **LB-001 to LB-130 (earlier work):** rename to USB Lockbox, update check and self-update, hardened settings handling, safer record defaults, settings overhaul, live port discovery with hide/name, simulator service mode, dry-run banner, PIN and typed erase phrase removed, tooltips, file-count breakdown, repo files (LICENSE, SECURITY, CHANGELOG, RELEASING, CI). See the git history for details.
