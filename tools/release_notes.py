"""Release notes from CHANGELOG.md.

    py tools/release_notes.py 4.10.0            # print 4.10.0's notes (else Unreleased's)
    py tools/release_notes.py --stamp 4.10.0    # "## Unreleased" -> "## 4.10.0 — <date>"

The Release workflow prints a version's section into the GitHub release body
(--notes-file), so what users see under the new version is CHANGELOG.md's own
text. publish.py and the workflow's version bump stamp the Unreleased section
with the version first; a fresh, empty Unreleased heading is left above it for
the next release's entries.

Printing exits 1 when there is nothing to print, so the workflow can fall back
to GitHub's generated notes.
"""

import os
import re
import sys
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHANGELOG = os.path.join(ROOT, "CHANGELOG.md")
sys.path.insert(0, ROOT)
# The app reads its bundled changelog the same way for "What's new".
from utils.changelog import UNRELEASED, is_version as _is_version, sections as _sections  # noqa: E402


def notes_for(text, version):
    """The section body for `version`, else the Unreleased one; "" if neither has text."""
    sections = _sections(text)
    for title, body in sections:
        if _is_version(title, version) and body.strip():
            return body.strip() + "\n"
    for title, body in sections:
        if title.lower() == UNRELEASED.lower() and body.strip():
            return body.strip() + "\n"
    return ""


def stamp(text, version, today=None):
    """Retitle the Unreleased section as `version`, with a new empty Unreleased
    above it. Unchanged when `version` already has a section or Unreleased is empty."""
    if any(_is_version(t, version) for t, _b in _sections(text)):
        return text
    m = re.search(rf"^## +{UNRELEASED}[ \t]*$", text, re.M | re.I)
    if m is None:
        return text
    body = dict((t.lower(), b) for t, b in _sections(text)).get(UNRELEASED.lower(), "")
    if not body.strip():
        return text
    when = (today or date.today()).isoformat()
    return text[:m.start()] + f"## {UNRELEASED}\n\n## {version} — {when}" + text[m.end():]


def main(argv):
    args = [a for a in argv if a != "--stamp"]
    if len(args) != 1:
        sys.exit(__doc__.strip().splitlines()[2].strip())
    version = args[0].lstrip("v").strip()
    try:
        with open(CHANGELOG, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return 1
    if "--stamp" in argv:
        new = stamp(text, version)
        if new != text:
            with open(CHANGELOG, "w", encoding="utf-8", newline="\n") as f:
                f.write(new)
            print(f"CHANGELOG.md: Unreleased -> {version}")
        return 0
    notes = notes_for(text, version)
    if not notes:
        return 1
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdout.write(notes)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
