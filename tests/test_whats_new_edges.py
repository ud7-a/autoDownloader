""""What's new" edge cases: what a changelog, the settings and the user can throw
at utils/changelog.py and ui/whats_new.py beyond the everyday path
(tests/test_whats_new.py).

Run with: py -m unittest discover -s tests
"""

import os
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
__import__("tests")

from tests.test_whats_new import LOG, Qt  # noqa: E402


def versions(text, seen, current):
    from utils import changelog
    return [v for v, _d, _f in changelog.whats_new(text, seen, current)]


class VersionEdgeTests(unittest.TestCase):
    def setUp(self):
        from utils import changelog
        self.c = changelog

    def test_a_trailing_zero_is_the_same_version(self):
        # "## 5.2" in the changelog is the app's 5.2.0: not an older version shown
        # beside Unreleased standing in for 5.2.0.
        log = ("## Unreleased\n### Later\n- l.\n## 5.2 — 2026-11-03\n### A\n- a.\n"
               "## 5.1.0 — 2026-10-01\n### B\n- b.\n")
        got = self.c.whats_new(log, "5.1.0", "5.2.0")
        self.assertEqual([("5.2", "2026-11-03")], [(v, d) for v, d, _f in got])
        self.assertEqual("A", got[0][2][0]["title"])
        self.assertEqual([], versions(log, "5.2", "5.2.0"))             # seen already
        self.assertEqual([], versions(log, "5.2.0", "5.2"))
        self.assertEqual([], versions(log, "5.2.0.0", "5.2.0"))

    def test_odd_version_strings(self):
        vk = self.c.version_key
        self.assertEqual((5, 2, 0), vk("5.2.0-beta.1"))
        self.assertEqual((5, 2, 0), vk("  v5.2.0  "))
        self.assertEqual((10,), vk("10"))
        for bad in ("", "   ", "v", "five", ".5", "Unreleased — 2026-11-03"):
            self.assertIsNone(vk(bad), bad)
        self.assertEqual([], versions(LOG, "5.0.0", ""))              # the app's own is broken
        self.assertEqual([], versions(LOG, "v5.2.0", "5.2.0"))        # "v" written: still seen

    def test_sections_out_of_order_come_out_newest_first(self):
        log = "## 5.0.0\n### A\n- a.\n## 5.2.0\n### C\n- c.\n## 5.1.0\n### B\n- b.\n"
        self.assertEqual(["5.2.0", "5.1.0", "5.0.0"], versions(log, "4.0.0", "5.2.0"))

    def test_skipping_many_updates_shows_the_newest_three(self):
        log = "".join(f"## 5.{i}.0\n### F{i}\n- p.\n" for i in range(8, 0, -1))
        self.assertEqual(["5.8.0", "5.7.0", "5.6.0"], versions(log, "5.0.0", "5.8.0"))

    def test_a_release_without_notes_shows_nothing_not_unreleased(self):
        # 5.2.0 has a section with nothing in it; the next version's notes in
        # Unreleased aren't 5.2.0's.
        log = "## Unreleased\n### Next\n- n.\n## 5.2.0 — 2026-11-03\n\n## 5.1.0\n### B\n- b.\n"
        self.assertEqual([], versions(log, "5.1.0", "5.2.0"))

    def test_a_build_from_source_shows_unreleased_once(self):
        log = "## Unreleased\n### Next\n- n.\n## 5.2.0\n### A\n- a.\n"
        self.assertEqual(["5.3.0"], versions(log, "5.2.0", "5.3.0"))
        self.assertEqual(["5.3.0"], versions(log.replace("Unreleased", "UNRELEASED"), "5.2.0", "5.3.0"))
        self.assertEqual([], versions("## 5.2.0\n### A\n- a.\n", "5.2.0", "5.3.0"))   # no Unreleased


class ChangelogTextEdgeTests(unittest.TestCase):
    def setUp(self):
        from utils import changelog
        self.c = changelog

    def feature(self, text):
        return self.c.features(text)[0]

    def test_comments_never_show(self):
        # A note for maintainers, or an "app:" line wrapped over two lines, is
        # hidden on GitHub and was shown in the app as "<!-- ...".
        f = self.feature("### X\n<!-- a note for maintainers -->\n- Real point.\n"
                         "<!-- app: Wrapped\n  over two lines. -->\n- Second.\n")
        self.assertEqual(["Real point.", "Second."], f["points"])
        self.assertEqual("Wrapped over two lines.", f["summary"])
        f = self.feature("### X\n<!--\nopen\n- not a point\n-->\n- Kept.\n")
        self.assertEqual(["Kept."], f["points"])
        f = self.feature("### X\n- Only.\n<!-- never closed\n- swallowed\n")
        self.assertEqual(["Only."], f["points"])                        # nothing leaks out

    def test_every_kind_of_list_item(self):
        f = self.feature("### X\n1. First step.\n2) Second step.\n+ Plus item.\n* Star item.\n"
                         "- Dash item\n  wrapped on.\n")
        self.assertEqual(["First step.", "Second step.", "Plus item.", "Star item.",
                          "Dash item wrapped on."], f["points"])
        # A year or a version opening a sentence isn't a list item.
        f = self.feature("### X\n- In\n2027 it comes.\n")
        self.assertEqual(["In 2027 it comes."], f["points"])

    def test_titles(self):
        self.assertEqual(("✨", ""), (self.feature("### ✨\n- x.\n")["emoji"], self.feature("### ✨\n- x.\n")["title"]))
        self.assertEqual("New:", self.feature("### ✨ New:\n- x.\n")["title"])
        self.assertEqual(("", "\"Quoted\" title"), (self.feature('### "Quoted" title\n- x.\n')["emoji"],
                                                     self.feature('### "Quoted" title\n- x.\n')["title"]))
        self.assertEqual("Spaces", self.feature("###    Spaces   \t\n- x.\n")["title"])

    def test_markup_that_isnt(self):
        p = self.feature("### X\n- a ** b, a ` b, <script>x</script> & [t](javascript:alert(1)).\n")["points"][0]
        self.assertIn("a ** b", p)
        self.assertIn("&lt;script&gt;", p)
        self.assertNotIn("<a ", p)                                     # only http(s) links
        self.assertIn("[t](javascript:alert(1))", p)

    def test_windows_line_endings(self):
        crlf = LOG.replace("\n", "\r\n")
        path = os.path.join(tempfile.mkdtemp(prefix="aed_crlf_"), "CHANGELOG.md")
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(crlf)
        with mock.patch.object(self.c, "bundled_path", return_value=path):
            text = self.c.read_bundled()
        self.assertEqual(self.c.whats_new(LOG, "4.0.0", "5.3.0"), self.c.whats_new(text, "4.0.0", "5.3.0"))

    def test_a_section_with_only_a_summary(self):
        f = self.feature("### X\n<!-- app: Just this. -->\n")
        self.assertEqual(("Just this.", []), (f["summary"], f["points"]))
        self.assertEqual([], self.c.features("<!-- app: nothing else -->\n"))   # no title, no points

    def test_an_empty_app_line_falls_back_to_the_first_sentence(self):
        f = self.feature("### X\n<!-- app: -->\n- First. Second.\n")
        self.assertEqual("First.", f["summary"])


class PageEdgeTests(Qt):
    def page(self, feature, several=False):
        from ui.whats_new import FeaturePage
        p = FeaturePage("5.2.0", "2026-11-03", feature, several)
        self.addCleanup(p.deleteLater)
        return p

    def test_a_new_feature_without_a_name(self):
        p = self.page({"title": "New:", "summary": "s", "points": ["p"]})
        self.assertEqual("New feature", p.title.text())

    def test_a_feature_with_no_title_no_points_no_target(self):
        p = self.page({"title": "", "summary": "", "points": []})
        self.assertEqual("Other changes", p.title.text())
        self.assertIsNone(p.details_button)
        self.assertIsNone(p.details)
        self.assertIsNone(p.button)

    def test_summary_links_open_outside_and_markup_stays_text(self):
        p = self.page({"title": "X", "summary": 'See <a href="https://x.org">this</a>.', "points": []})
        self.assertTrue(p.summary.openExternalLinks())
        self.assertEqual("X", self.page({"title": "x", "summary": "", "points": []}).title.text())

    def test_a_long_date_that_isnt_one(self):
        from ui.whats_new import _long_date
        self.assertEqual("", _long_date(""))
        self.assertEqual("", _long_date("2026-02-30"))
        self.assertEqual("", _long_date(None))
        self.assertEqual("1 January 2027", _long_date("2027-01-01"))


class DialogEdgeTests(Qt):
    def many(self, n):
        feats = [{"title": f"Feature {i}", "summary": "s", "points": ["p"], "emoji": ""} for i in range(n)]
        return [("5.2.0", "2026-11-03", feats)]

    def test_many_pages(self):
        dlg = self.dialog(self.many(12))
        self.assertEqual(12, len(dlg.pages))
        self.assertEqual("1 of 12", dlg.counter.text())
        dlg.go(-5)
        self.assertEqual(0, dlg.current)
        dlg.go(500)
        self.assertEqual((11, "Got it", "12 of 12"), (dlg.current, dlg.yesButton.text(), dlg.counter.text()))
        dlg.go(11)                                                     # the same page: nothing
        self.assertEqual(11, dlg.stack.currentIndex())

    def test_other_keys_still_reach_the_dialog(self):
        from PyQt6.QtCore import QEvent, Qt as Q
        from PyQt6.QtGui import QKeyEvent
        from qfluentwidgets import MessageBoxBase
        dlg = self.dialog(self.many(3))
        with mock.patch.object(MessageBoxBase, "keyPressEvent") as base:
            dlg.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Q.Key.Key_Escape, Q.KeyboardModifier.NoModifier))
        base.assert_called_once()
        self.assertEqual(0, dlg.current)

    def test_several_versions_never_seen_before(self):
        # The heading names the version before the oldest shown, as the user knew
        # it; with no record of one, the oldest shown.
        from utils.config import app_settings
        vs = [("5.2.0", "", [{"title": "A", "summary": "a", "points": []}]),
              ("5.1.0", "", [{"title": "B", "summary": "b", "points": []}])]
        from PyQt6.QtWidgets import QLabel
        for seen, heading in (("", "What's new since version 5.1.0"),
                              ("5.0.0", "What's new since version 5.0.0")):
            with mock.patch.dict(app_settings, {"whats_new_seen": seen}):
                dlg = self.dialog(vs)
            self.assertIn(heading, [w.text() for w in dlg.findChildren(QLabel)], seen)
        self.assertNotIn("Released", " ".join(w.text() for w in dlg.findChildren(QLabel)))

    def test_a_tab_button_on_the_last_page_of_a_resumed_tour(self):
        from ui import whats_new
        from utils.config import app_settings
        from PyQt6.QtWidgets import QWidget
        host = QWidget()
        self.addCleanup(host.deleteLater)
        vs = self.versions()
        opened, offers = [], []

        def fake_exec(dlg):
            self.assertEqual(3, dlg.current)                           # resumed on the last page
            dlg.opened = "player_interface"
            return 1
        with mock.patch.dict(app_settings, {"whats_new_seen": "5.0.0"}), \
                mock.patch("ui.whats_new.save_config"), \
                mock.patch.object(whats_new.WhatsNewDialog, "exec", fake_exec), \
                mock.patch.object(whats_new, "offer_to_continue", side_effect=lambda *a: offers.append(a)):
            whats_new.show(host, vs, opened.append, start=3)
        self.assertEqual(["player_interface"], opened)
        self.assertEqual([], offers)                                   # nothing left after it

    def test_the_notice_counts_what_is_left(self):
        from ui import whats_new
        import qfluentwidgets
        from PyQt6.QtWidgets import QWidget
        host = QWidget()
        self.addCleanup(host.deleteLater)
        bar = mock.Mock()
        with mock.patch.object(qfluentwidgets.InfoBar, "info", return_value=bar) as info:
            whats_new.offer_to_continue(host, self.versions(), mock.Mock(), 3, 4)
        self.assertEqual("1 more to see.", info.call_args.args[1])


class OnDemandTests(unittest.TestCase):
    def call(self, notes):
        from ui import whats_new
        from ui.app_window import AppWindow
        import qfluentwidgets
        w = types.SimpleNamespace(open_tab=mock.Mock())
        with mock.patch("utils.changelog.read_bundled", return_value=notes), \
                mock.patch.object(whats_new, "show") as show, \
                mock.patch.object(qfluentwidgets.InfoBar, "info") as info:
            AppWindow.show_whats_new(w)
        return w, show, info

    def test_this_versions_notes(self):
        from utils.config import APP_VERSION
        w, show, info = self.call(f"## {APP_VERSION}\n### A\n- a.\n## 0.1.0\n### Old\n- o.\n")
        self.assertEqual([APP_VERSION], [v for v, _d, _f in show.call_args.args[1]])   # this one only
        info.assert_not_called()

    def test_no_notes_says_so_instead_of_nothing(self):
        # The up-to-date notice's "What's new" did nothing at all for a version
        # the changelog has nothing about.
        from utils.config import APP_VERSION
        for notes in ("", "## 0.1.0\n### Old\n- o.\n", f"## {APP_VERSION}\n"):
            _w, show, info = self.call(notes)
            show.assert_not_called()
            info.assert_called_once()
            self.assertIn(APP_VERSION, info.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
