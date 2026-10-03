# Publishing a release

GitHub builds and publishes the release when you push a version tag (`.github/workflows/release.yml`). The in-app updater only self-installs a release that has **a zip and a SHA-256 for it**, and the workflow attaches both.

1. Edit `__version__` in `usblockbox/__init__.py` (for example `"0.1.2"`).
2. In `CHANGELOG.md`, put the notes you want shown under a heading `## 0.1.2` (just the number). Keep an empty `## Unreleased` above it.
3. Commit and push to `master`. Wait for the **tests** run on that commit to go green (repo > Actions).
4. Tag that commit `v0.1.2` and push the tag:

       git tag v0.1.2
       git push origin v0.1.2

   (GitHub Desktop: History tab, right-click the commit, Create Tag, then Push origin.)
5. The **release** run (repo > Actions) checks that the tag matches `__version__`, runs the tests, builds `usblockbox-0.1.2.zip` and `SHA256SUMS`, and publishes the release with the changelog section as its notes. A tag with a dash (for example `v0.2.0-rc1`) is published as a pre-release, which the updater ignores.
6. Check it: run an older copy and use Settings > Updates > Check now.

If the release run fails, nothing is published. Fix the cause, delete the tag (`git tag -d v0.1.2; git push origin :refs/tags/v0.1.2`), and tag again.

## Doing it by hand (fallback)

    git archive --format=zip --prefix=usblockbox-X.Y.Z/ -o usblockbox-X.Y.Z.zip vX.Y.Z
    $h = (Get-FileHash usblockbox-X.Y.Z.zip -Algorithm SHA256).Hash.ToLower()
    "$h  usblockbox-X.Y.Z.zip" | Out-File SHA256SUMS -Encoding ascii

Then on GitHub: Releases > Draft a new release, create the tag, attach both files, leave "Pre-release" off, paste the changelog section.

`UPDATE_REPO` in `usblockbox/appinfo.py` names the repository the app checks. While it still says `OWNER/REPO` the update check is off. Releases without the zip or the checksum still show up as "update available", but the app only offers the release page, never an automatic install.
