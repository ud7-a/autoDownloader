# Contributing to Auto Episodes Downloader

Thank you for your interest in improving **Auto Episodes Downloader**! Contributions from the community are warmly welcomed.

---

## 🧭 Code of Conduct & Guidelines

- **Clean & Typed Code**: Ensure all Python code is formatted, easy to read, and well-typed.
- **Maintain Lint Cleanliness**: Never introduce linter errors. Always run `python tools/lint.py` before submitting a PR.
- **Unit Tests Required**: Any new site adapter, download feature, or data model change must include comprehensive unit tests under `tests/` or `service/tests/`.

---

## 🛠️ Development Setup

1. Fork and clone the repository:
   ```bash
   git clone https://github.com/<your-username>/autoDownloader.git
   cd autoDownloader
   ```
2. Create and activate a virtual environment:
   ```bash
   python -m venv .venv
   .venv\Scripts\activate
   ```
3. Install development dependencies:
   ```bash
   pip install -r requirements-dev.txt
   ```
4. Run the desktop application locally:
   ```bash
   python main.py
   ```

---

## 🧪 Verification & Testing

Before opening a pull request, run the complete verification suite:

```bash
# 1. Run desktop test suite
python -m unittest discover -s tests -v

# 2. Run cloud service test suite
python -m unittest discover -s service/tests -v

# 3. Check for linting issues
python tools/lint.py
```

---

## 🚀 Submitting a Pull Request

1. Create a feature branch (`git checkout -b feat/your-feature-name`).
2. Commit your changes with meaningful commit messages (`git commit -m "feat: add support for site XYZ"`).
3. Push to your fork (`git push origin feat/your-feature-name`).
4. Open a Pull Request on GitHub against the `main` branch.
