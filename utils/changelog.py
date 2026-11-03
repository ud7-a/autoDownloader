"""CHANGELOG.md, read: its version sections, and each section's features for the
"What's new" window (ui/whats_new.py).

The changelog is bundled with the app (tools/build_release.py), so what the window
says after an update is the same text as the GitHub release page: the Release
workflow stamps the "Unreleased" section with the version before it builds.

    ## 5.2.0 — 2026-11-03            a version section
    ### 📰 New: Anime news tab        a feature: its emoji, its title
    <!-- app: Short and plain. -->    what the window says about it (hidden on GitHub)
    - **Bold** and `code` lines,      its points, the details (continuation lines
      wrapped onto more lines.        are indented)

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


def _same(key):
    """A version key without its trailing zeros: 5.2 and 5.2.0 are one version."""
    if key is None:
        return None
    key = list(key)
    while len(key) > 1 and key[-1] == 0:
        key.pop()
    return tuple(key)


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
    """("📰", "New: Anime news tab") -- the leading symbol(s), if any, apart. A
    title of symbols only is all emoji, with no words."""
    m = re.match(r"^([^\w\s(\"'“]+)\s+(.+)$", title)
    if m:
        return m.group(1), m.group(2)
    return (title, "") if not re.search(r"\w", title) else ("", title)


_SUMMARY = re.compile(r"^<!--\s*app:\s*(.*?)\s*-->$", re.S)
# A list item: "- ", "* ", "+ ", or numbered, "1. " / "1) ".
_BULLET = re.compile(r"^(?:[-*+]|\d+[.)])\s+")


def _first_sentence(rich):
    """A point's first sentence, as plain text: the short version of a feature
    whose notes have no "app:" line (releases from before there was one)."""
    plain = html.unescape(re.sub(r"<[^>]+>", "", rich))
    m = re.match(r"(.+?[.!?])(\s|$)", plain)
    return (m.group(1) if m else plain).strip()


def features(body):
    """A version section's features: [{"emoji", "title", "summary", "points"}]
    (summary and points as rich text). Points before the first '###' become one
    untitled feature."""
    out = []

    def add(emoji, title, lines):
        points, current, summary = [], None, ""
        comment = None            # an HTML comment's lines while it is open
        for line in lines:
            stripped = line.strip()
            # Comments are hidden on GitHub, so never shown here either; an "app:"
            # one, on one line or several, is the feature's short version.
            if comment is not None or stripped.startswith("<!--"):
                comment = (comment or []) + [stripped]
                if "-->" in stripped:
                    m = _SUMMARY.match(" ".join(comment))
                    if m:
                        summary = _inline(m.group(1))
                    comment = None
                continue
            if not stripped:
                current = None
                continue
            bullet = _BULLET.match(stripped)
            if bullet:
                current = [stripped[bullet.end():].strip()]
                points.append(current)
            elif current is not None:
                current.append(stripped)          # a wrapped line of the same point
            else:
                current = [stripped]
                points.append(current)
        if title or points:
            rich = [_inline(" ".join(p)) for p in points]
            out.append({"emoji": emoji, "title": title, "points": rich,
                        "summary": summary or (html.escape(_first_sentence(rich[0]), quote=False)
                                               if rich else "")})

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
    now, before = _same(version_key(current)), _same(version_key(seen))
    if now is None:
        return []
    out, has_current = [], False
    for title, body in sections(text):
        key = version_key(title)
        same = _same(key)
        if key is None or same > now or (before is not None and same <= before):
            continue
        has_current = has_current or same == now
        feats = features(body)
        if feats:
            out.append((".".join(map(str, key)), section_date(title), feats))
    if not has_current:
        unreleased = next((b for t, b in sections(text) if t.lower() == UNRELEASED.lower()), "")
        feats = features(unreleased)
        if feats:
            out.insert(0, (current, "", feats))
    out.sort(key=lambda v: _same(version_key(v[0])), reverse=True)
    return out


MAX_VERSIONS = 3          # updates shown at most, after skipping several


def whats_new(text, seen, current):
    """What the "What's new" window shows on this launch: changes_since() of the
    version last shown, newest first, at most MAX_VERSIONS. Nothing when this
    version was shown already or the app was downgraded. `seen` empty (an install
    from before the window existed) shows this version's changes only."""
    now = _same(version_key(current))
    if now is None or (seen or "").strip() == current:
        return []
    before = _same(version_key(seen))
    if before is not None and before >= now:
        return []
    found = changes_since(text, seen if before is not None else None, current)
    if before is None:
        found = [v for v in found if _same(version_key(v[0])) == now]
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
