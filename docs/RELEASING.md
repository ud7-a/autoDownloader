# Release Process & Workflow Documentation

## 🚀 How to Ship a New Release

### Option A: Automated CLI Helper (Recommended)
Run:
```bash
python publish.py
```
This tool reads the current version, prompts for a bump (major/minor/patch), commits, pushes, and triggers GitHub Actions to compile and publish the release.

---

### Option B: GitHub Actions Web Interface
1. Go to **Actions → Release → Run workflow** on GitHub.
2. Type the target version (e.g. `4.4.0`).
3. Click **Run workflow**.

---

### Option C: Manual Git Tag
```bash
python tools/bump_version.py 4.4.0
git commit -am "🚀 Release version 4.4.0"
git tag v4.4.0
git push origin main v4.4.0
```

---

## ⚙️ Workflows Overview

| Workflow | Trigger | Action |
|---|---|---|
| `ci.yml` | Push to `main`, PRs | Linting (`tools/lint.py`) → Unit Tests (`tests/`) → Builds Artifacts for validation |
| `release.yml` | Manual trigger or `v*` tag | CI checks → PyInstaller Build (`tools/build_release.py`) → GitHub Release with Setup & Portable ZIP |
| `dependabot.yml` | Monthly | Automated dependency update PRs |

---

## 🔒 Consistency Rules

1. **Version Alignment**: `APP_VERSION` in `utils/config.py`, the git tag (`vX.Y.Z`), and the release title must match.
2. **Asset Naming**: The installer must be named `AutoDownloader_Setup.exe` so the silent in-app updater (`core/updater.py`) recognizes it.
3. **Pre-release Checks**:
   ```bash
   python tools/lint.py
   python -m unittest discover -s tests -v
   python -m unittest discover -s service/tests -v
   ```
