## 📝 Description
Briefly explain the changes made in this pull request and the problem they solve.

## 🎯 Type of Change
- [ ] 🐛 Bug fix (non-breaking change which fixes an issue)
- [ ] ✨ New feature (non-breaking change which adds functionality)
- [ ] 🎨 UI/UX improvement
- [ ] ⚡ Performance optimization
- [ ] 📚 Documentation update
- [ ] 🧹 Refactoring

## 🧪 Verification Checklist

Mark checks that apply and explain any omitted checks. Service tests require the
service dependencies and a dedicated PostgreSQL test database; see
[CONTRIBUTING.md](https://github.com/ud7-a/autoDownloader/blob/main/CONTRIBUTING.md).

- [ ] `python tools/lint.py` passes with 0 errors
- [ ] `python -m unittest discover -s tests -v` passes
- [ ] `python -m unittest discover -s service/tests -v` passes with `DATABASE_URL` and `AED_NOTIFY_SCHEMA=aed_test` configured
- [ ] Tested manually on Windows
