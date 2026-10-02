# Publishing a release

The in-app updater only self-installs a release that has **a zip asset and a SHA-256 for it**. Do this for every release.

1. Bump `__version__` in `usblockbox/__init__.py`, move the `## Unreleased` notes in `CHANGELOG.md` under the new version, commit, tag `vX.Y.Z`.
2. Build the zip from the tag (the single top-level folder is what the updater expects):

       git archive --format=zip --prefix=usblockbox-X.Y.Z/ -o usblockbox-X.Y.Z.zip vX.Y.Z

3. Write the checksum file (PowerShell):

       $h = (Get-FileHash usblockbox-X.Y.Z.zip -Algorithm SHA256).Hash.ToLower()
       "$h  usblockbox-X.Y.Z.zip" | Out-File SHA256SUMS -Encoding ascii

4. Create the GitHub release for the tag (not a draft, not a pre-release) and attach **both** `usblockbox-X.Y.Z.zip` and `SHA256SUMS`. Paste the changelog section into the notes.
5. Check it: run an older copy and use Settings > Updates > Check now.

Before the first release, set `UPDATE_REPO` in `usblockbox/appinfo.py` to `owner/repo`. While it still says `OWNER/REPO` the update check is off.

Releases without the zip or the checksum still show up as "update available", but the app only offers the release page, never an automatic install.
