# Releasing Auto Episodes Downloader

Use the [Release workflow](../.github/workflows/release.yml) to publish the
Windows installer and portable ZIP. Its checks cover the desktop app; service
changes require separate service tests.

## GitHub Actions release

1. Add user-facing changes under **Unreleased** in [CHANGELOG.md](../CHANGELOG.md)
   and commit them to `main`.
2. Open **Actions → Release → Run workflow** on GitHub.
3. Enter a new, unused `X.Y.Z` version. Optionally select **prerelease**.
4. Run the workflow and inspect its result.

The dispatched workflow checks out `main`, validates the version, runs lint
and desktop tests, bumps `APP_VERSION`, and stamps the changelog. It builds
before pushing the version commit and tag, then publishes the release artifacts.

## Manual tag release

Start from an up-to-date `main` with a clean working tree. Install development
dependencies as described in [CONTRIBUTING.md](../CONTRIBUTING.md).
These commands use PowerShell; replace the example with a new, unused version:

```powershell
$releaseVersion = "5.0.1"
.\.venv\Scripts\python.exe tools/bump_version.py $releaseVersion
.\.venv\Scripts\python.exe tools/release_notes.py --stamp $releaseVersion
.\.venv\Scripts\python.exe tools/lint.py
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe tools/build_release.py --expect-version $releaseVersion
git add -- utils/config.py CHANGELOG.md
git commit -m "chore: release $releaseVersion"
git tag "v$releaseVersion"
git push origin main "v$releaseVersion"
```

The tag workflow builds the tagged commit. It does **not** bump the version or
stamp the changelog for you; prepare both before tagging. The build fails if
`APP_VERSION` and the requested version differ.

## Maintainer-local helper

Some maintainer checkouts include `publish.py`. It is ignored by Git and is
**not included in a clone**. The supported paths available to contributors are
the GitHub workflow and tracked release scripts above.

## Validation and artifacts

| Configuration | Trigger | Checks and outputs |
| --- | --- | --- |
| [CI](../.github/workflows/ci.yml) | Push/PR to `main`, or manual run | Lint, offscreen imports, desktop tests, app/installer build, installer artifact |
| [Release](../.github/workflows/release.yml) | Manual dispatch or `v*` tag | Lint, desktop tests, version validation, build, installer/portable ZIP publication |
| [Cloud checker](../.github/workflows/cloud-checker.yml) | Every 10 minutes or manual run | Calls the configured service; reports cycle statistics |
| [Dependabot](../.github/dependabot.yml) | Monthly | Desktop Python and GitHub Actions dependency update PRs |

Run the [service tests](../service/README.md#tests) separately when a change
affects the cloud service. They require service dependencies, a dedicated
PostgreSQL test database, `DATABASE_URL`, and `AED_NOTIFY_SCHEMA=aed_test`.

The local build script produces `dist/AutoDownloader/` and
`dist/AutoDownloader_Setup.exe`. The Release workflow additionally packages
`AutoDownloader_Portable.zip`.

## Version and updater contract

- Keep `APP_VERSION` in `utils/config.py` aligned with the `vX.Y.Z` tag.
- Publish `AutoDownloader_Setup.exe` and `AutoDownloader_Portable.zip`.
  The updater looks for a Setup `.exe` asset; preserve the established name.
- The updater offers only a newer stable release to the installed app.
  Source runs and alternate `AED_APP_DIR` runs do not install updates.
- Release notes come from the version's changelog section, with Unreleased notes
  or GitHub-generated notes as fallbacks when appropriate.
