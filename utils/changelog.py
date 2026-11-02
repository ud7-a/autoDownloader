"""CHANGELOG.md, read: its version sections, and each section's features for the
"What's new" window (ui/whats_new.py).

The changelog is bundled with the app (tools/build_release.py), so what the window
says after an update is the same text as the GitHub release page: the Release
workflow stamps the "Unreleased" section with the version before it builds.

    ## 5.2.0 — 2026-11-03            a version section
    ### 📰 New: Anime news tab        a feature: its emoji, its title
    - **Bold** and `code` lines,      its points (continuation lines are indented)
      wrapped onto more lines.

Pure text handling, no Qt; tools/release_notes.py uses the same section reading.
"""
import html
import os
import re
import sys

UNRELEASED = "Unreleased"
_HEADING = re.compile(r"^## +(.+?)[ \t]*$", re.M)
_FEATURE = re.compile(r"^### +(.+?)[ \t]*$", re.M)
_VERSION = re.compile(r"^v?(\d+(?:\.\d+)*)")


def sections(text):
    """[(title, body)] for every '## ' section, in file order."""
    marks = list(_HEADING.finditer(text or ""))
    out = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        out.append((m.group(1), text[m.end():end].strip("\n")))
    return out


def is_version(title, version):
    return re.match(rf"^v?{re.escape(version)}(\s|$)", title) is not None


def version_key(version):
    """(5, 2, 0) from "5.2.0" or "v5.2.0 — 2026-11-03"; None when it isn't one."""
    m = _VERSION.match((version or "").strip())
    return tuple(int(p) for p in m.group(1).split(".")) if m else None


def section_date(title):
    """"2026-11-03" from "5.2.0 — 2026-11-03", else ""."""
    m = re.search(r"(\d{4}-\d{2}-\d{2})\s*$", title or "")
    return m.group(1) if m else ""


def _inline(text):
    """A changelog line as Qt rich text: escaped, with **bold**, `code` and links."""
    # "⋯" (the More button) has no glyph in Segoe UI and shows as a dash.
    out = html.escape(text.replace("⋯", "···"), quote=False)
    out = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    return re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', out)


def _split_emoji(title):
    """("📰", "New: Anime news tab") -- the leading symbol(s), if any, apart."""
    m = re.match(r"^([^\w\s(\"'“]+)\s+(.+)$", title)
    return (m.group(1), m.group(2)) if m else ("", title)


def features(body):
    """A version section's features: [{"emoji", "title", "points": [rich text]}].
    Points before the first '###' become one untitled feature."""
    out = []

    def add(emoji, title, lines):
        points, current = [], None
        for line in lines:
            stripped = line.strip()
            if not stripped:
                current = None
                continue
            if re.match(r"^[-*] ", stripped):
                current = [stripped[2:].strip()]
                points.append(current)
            elif current is not None:
                current.append(stripped)          # a wrapped line of the same point
            else:
                current = [stripped]
                points.append(current)
        if title or points:
            out.append({"emoji": emoji, "title": title,
                        "points": [_inline(" ".join(p)) for p in points]})

    marks = list(_FEATURE.finditer(body or ""))
    head = (body or "")[:marks[0].start()] if marks else (body or "")
    if head.strip():
        add("", "", head.splitlines())
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(body)
        emoji, title = _split_emoji(m.group(1))
        add(emoji, title, body[m.end():end].splitlines())
    return out


def changes_since(text, seen, current):
    """[(version, date, features)] newest first, for every version section after
    `seen` up to `current`. When `current` has no section of its own yet (a build
    from source), its Unreleased section stands for it."""
    now, before = version_key(current), version_key(seen)
    if now is None:
        return []
    out, has_current = [], False
    for title, body in sections(text):
        key = version_key(title)
        if key is None or key > now or (before is not None and key <= before):
            continue
        has_current = has_current or key == now
        feats = features(body)
        if feats:
            out.append((".".join(map(str, key)), section_date(title), feats))
    if not has_current:
        unreleased = next((b for t, b in sections(text) if t.lower() == UNRELEASED.lower()), "")
        feats = features(unreleased)
        if feats:
            out.insert(0, (current, "", feats))
    out.sort(key=lambda v: version_key(v[0]), reverse=True)
    return out


MAX_VERSIONS = 3          # updates shown at most, after skipping several


def whats_new(text, seen, current):
    """What the "What's new" window shows on this launch: changes_since() of the
    version last shown, newest first, at most MAX_VERSIONS. Nothing when this
    version was shown already or the app was downgraded. `seen` empty (an install
    from before the window existed) shows this version's changes only."""
    now = version_key(current)
    if now is None or (seen or "").strip() == current:
        return []
    before = version_key(seen)
    if before is not None and before >= now:
        return []
    found = changes_since(text, seen if before is not None else None, current)
    if before is None:
        found = [v for v in found if version_key(v[0]) == now]
    return found[:MAX_VERSIONS]


def bundled_path():
    """CHANGELOG.md beside the app: in the frozen build's bundle, else the repo."""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "CHANGELOG.md")


def read_bundled():
    try:
        with open(bundled_path(), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""
