# Releasing

## Shipping a version

Open **Actions → Release → Run workflow**, type the version (`4.2.3`), run it. That is
the whole process. The workflow bumps `APP_VERSION`, commits, tags, builds, and
publishes — installed copies pick the update up on their next launch.

If you would rather tag by hand, bump the version first or the build will refuse:

```bash
py tools/bump_version.py 4.2.3
git commit -am "🚀 Release version 4.2.3"
git tag v4.2.3 && git push origin main v4.2.3
```

## What runs when

| Workflow | Trigger | Does |
|---|---|---|
| `ci.yml` | push to `main`, PRs | lint → imports → 97 unit tests → build app + installer (uploaded as an artifact, not released) |
| `release.yml` | Run workflow, or a `v*` tag | the CI checks → bump/tag → build → GitHub Release |
| `dependabot.yml` | monthly | dependency PRs, which CI builds |

Everything runs on `windows-latest`. A Linux runner would be cheaper and would test
something no user runs: the app drives Chrome through Windows paths, ships `.exe`
helpers, and installs to `C:\`.

## The version rule

`APP_VERSION` in `utils/config.py`, the git tag, and the release must all agree.
`core/updater.py` compares `APP_VERSION` against the latest release, so if they drift
users either never see the update or reinstall the same one forever. The release build
runs `build_release.py --expect-version` and fails when they disagree.

## The asset-name rule

`core/updater.py` downloads the release asset whose name **contains `Setup`** and
**ends in `.exe`**, then runs it with `--silent`. `AutoDownloader_Setup.exe` satisfies
that. Renaming it breaks in-app updates for everyone already installed, so the release
workflow re-reads the published release and fails if nothing matches the filter.

Each release carries two assets:

- `AutoDownloader_Setup.exe` — installer, and what the updater fetches
- `AutoDownloader_Portable.zip` — the unpacked app, no install

## Building locally

```bash
py tools/build_release.py
```

Produces `dist/AutoDownloader/` and `dist/AutoDownloader_Setup.exe` — the same two
artifacts CI produces, from the same flags. Add `--app-only` to skip the installer.

`exeCompile.py` is untracked and stays that way: it is the maintainer's convenience
wrapper that also deploys to `C:\Auto Episodes Downloader\App`. Because CI cannot see
it, `tools/build_release.py` holds its own copy of the PyInstaller flags. **Change one,
change the other.**

## Checks before pushing

```bash
py tools/lint.py && py -m unittest discover -s tests
```

`tools/lint.py` is pyflakes with this repo's known-benign findings filtered out (the
deliberate function-level re-imports that keep startup fast, mostly), so a non-zero
exit means a real problem. Tests redirect `AED_APP_DIR` to a temp folder in
`tests/__init__.py`, so they never touch the real config, history DB, or browser
profile.
