"""Anime news search edge cases: the tab's search bar and the AniList picker
(ui/news_tab.MatchDialog, NewsWidget.follow_search / follow) beyond the everyday
path (tests/test_news_follow.py). No test reaches AniList.

Run with: py -m unittest discover -s tests
"""

import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
__import__("tests")

from tests.test_news import m  # noqa: E402
from tests.test_news_edges import TabBase  # noqa: E402


class PickerBase(TabBase):
    def picker(self, pages, query="fate", follow=True, current_id=None):
        """A picker over these AniList pages ([media], or an exception, per page)."""
        from ui.news_tab import MatchDialog
        from PyQt6.QtWidgets import QWidget
        host = QWidget()
        host.resize(900, 2000)
        self.addCleanup(host.deleteLater)
        self.asked = []

        def search_page(q, page):
            self.asked.append((q, page))
            result = pages[page - 1] if page <= len(pages) else []
            if isinstance(result, Exception):
                raise result
            return result, page < len(pages)
        p = mock.patch.object(self.news, "search_page", side_effect=search_page)
        p.start()
        self.addCleanup(p.stop)
        dlg = MatchDialog(query, current_id, host, follow=follow)
        self.wait(dlg)
        return dlg

    def wait(self, dlg):
        for t in list(dlg._threads):
            t.wait(5000)
        self._app.processEvents()

    def ids(self, dlg):
        return [c.media["id"] for c in dlg._cards]


class NamesTests(PickerBase):
    def test_an_entry_with_only_a_native_name(self):
        # It showed as "?" in the list and in "Following ?".
        native = {"id": 7, "format": "TV", "title": {"romaji": None, "english": None, "native": "まかでみ"}}
        self.assertEqual("まかでみ", self.news.display_title(native))
        self.assertEqual("?", self.news.display_title({"id": 8, "title": {}}))
        self.assertEqual("?", self.news.display_title({"id": 8}))
        self.assertEqual("?", self.news.display_title(None))
        dlg = self.picker([[native]])
        self.assertEqual("まかでみ", dlg._cards[0].title.full_text)

    def test_names_that_look_like_markup_are_shown_as_written(self):
        # "<Infinite Dendrogram>" read as a tag and vanished from the label.
        name = "<Infinite Dendrogram>"
        dlg = self.picker([[m(1, name, english=name)]], query="<b>dendro</b>")
        from PyQt6.QtCore import Qt as Q
        title = dlg._cards[0].title
        self.assertEqual(Q.TextFormat.PlainText, title.textFormat())
        self.assertEqual(name, title.text())
        self.assertEqual(Q.TextFormat.PlainText, dlg.status.textFormat())
        self.assertEqual([("<b>dendro</b>", 1)], self.asked)           # asked as typed

    def test_entries_missing_fields_still_make_cards(self):
        bare = [{"id": 1, "format": "TV", "title": {"romaji": "Bare"}},       # no dates, no cover
                {"id": 2, "format": "TV", "title": {"romaji": "Nulls"}, "startDate": None,
                 "episodes": None, "status": None, "coverImage": None, "synonyms": None}]
        dlg = self.picker([bare], query="bare")
        self.assertEqual([1, 2], sorted(self.ids(dlg)))
        self.assertEqual("TV", dlg._cards[0].findChildren(type(dlg._cards[0].title))[-1].full_text)

    def test_a_japanese_query_is_asked_as_typed(self):
        self.picker([[m(1, "Frieren")]], query=" 葬送のフリーレン ")
        self.assertEqual([("葬送のフリーレン", 1)], self.asked)


class ResultListTests(PickerBase):
    def test_the_same_entry_twice_in_one_answer_shows_once(self):
        dlg = self.picker([[m(1, "Fate"), m(2, "Fate 2"), m(1, "Fate")]])
        self.assertEqual([1, 2], sorted(self.ids(dlg)))
        self.assertEqual("2 results. Click one to pick it, or double-click to use it right away.",
                         dlg.status.text())

    def test_a_name_with_films_only_keeps_showing_films_on_later_pages(self):
        films = [m(i, f"Fate Film {i}", fmt="MOVIE") for i in range(1, 4)]
        later = [m(i, f"Fate Film {i}", fmt="MOVIE") for i in range(4, 6)]
        dlg = self.picker([films, later])
        self.assertEqual(3, len(dlg._cards))
        dlg.btn_more.click()
        self.wait(dlg)
        self.assertEqual(5, len(dlg._cards))                           # the next films too

    def test_series_first_page_leaves_later_films_out(self):
        dlg = self.picker([[m(1, "Fate")], [m(2, "Fate Film", fmt="MOVIE"), m(3, "Fate 3")]])
        dlg.btn_more.click()
        self.wait(dlg)
        self.assertEqual([1, 3], sorted(self.ids(dlg)))

    def test_anilist_says_more_then_has_none(self):
        dlg = self.picker([[m(1, "Fate")], []])
        self.assertFalse(dlg.btn_more.isHidden())
        dlg.btn_more.click()
        self.wait(dlg)
        self.assertTrue(dlg.btn_more.isHidden())
        self.assertIn("AniList has nothing more for “fate”", dlg.status.text())
        self.assertEqual(1, len(dlg._cards))

    def test_the_first_search_failing_offers_no_more(self):
        dlg = self.picker([self.news.NewsError("AniList answered HTTP 500")])
        self.assertEqual([], dlg._cards)
        self.assertTrue(dlg.btn_more.isHidden())
        self.assertIn("HTTP 500", dlg.status.text())
        self.assertFalse(dlg.yesButton.isEnabled())

    def test_nothing_found_on_a_later_search_clears_the_list(self):
        dlg = self.picker([[m(1, "Fate")], [m(2, "Fate 2")]])
        dlg.btn_more.click()
        self.wait(dlg)
        dlg._pick(dlg._cards[0])
        with mock.patch.object(self.news, "search_page", return_value=([], False)):
            dlg.search_box.setText("zzz")
            dlg.search_box.returnPressed.emit()
            self.wait(dlg)
        self.assertEqual([], dlg._cards)
        self.assertIsNone(dlg.picked)
        self.assertFalse(dlg.yesButton.isEnabled())
        self.assertTrue(dlg.btn_more.isHidden())
        self.assertIn("Nothing on AniList is called “zzz”", dlg.status.text())

    def test_the_current_match_is_marked_on_a_later_page_too(self):
        dlg = self.picker([[m(1, "Fate")], [m(2, "Fate 2")]], follow=False, current_id=2)
        dlg.btn_more.click()
        self.wait(dlg)
        card = next(c for c in dlg._cards if c.media["id"] == 2)
        self.assertIn("Current match", [lbl.text() for lbl in card.findChildren(type(dlg.status))])

    def test_double_click_on_a_later_page_picks_and_closes(self):
        dlg = self.picker([[m(1, "Fate")], [m(2, "Fate 2")]])
        dlg.btn_more.click()
        self.wait(dlg)
        card = next(c for c in dlg._cards if c.media["id"] == 2)
        with mock.patch.object(dlg, "accept") as accept:
            card.chosen.emit()
        accept.assert_called_once()
        self.assertEqual(2, dlg.picked["id"])

    def test_show_more_while_covers_load_keeps_them_coming(self):
        dlg = self.picker([[m(1, "Fate")], [m(2, "Fate 2")]])
        first = mock.Mock()
        dlg._threads.append(first)                                     # page 1's covers, still loading
        with mock.patch("ui.news_tab.SearchThread") as thread:
            dlg._load_more()
        first.requestInterruption.assert_not_called()
        thread.assert_called_once_with("fate", dlg._generation, 2)
        with mock.patch("ui.news_tab.SearchThread"):
            dlg._search("other")                                       # a new search does stop them
        first.requestInterruption.assert_called_once()

    def test_the_more_button_stays_last_and_the_list_scrolls(self):
        dlg = self.picker([[m(i, f"Fate {i}") for i in range(1, 51)], [m(i, f"Fate {i}") for i in range(51, 60)]])
        self.assertEqual(dlg.list_height(), dlg.scroll.height())        # full: it scrolls
        dlg.btn_more.click()
        self.wait(dlg)
        self.assertEqual(dlg.list_col.count() - 1, dlg.list_col.indexOf(dlg.btn_more))
        self.assertEqual(59, len(dlg._cards))


class TabSearchBarTests(TabBase):
    def tab(self, library=(), records=None, followed=()):
        self.write_file({"shows": records or {}, "checked_at": time.time(), "followed": list(followed)})
        p = mock.patch("ui.news_tab.library_shows", return_value=list(library))
        p.start()
        self.addCleanup(p.stop)
        from ui.news_tab import NewsWidget
        w = NewsWidget()
        self.addCleanup(w.deleteLater)
        w.refresh()
        return w

    def test_a_picker_closed_any_way_is_deleted(self):
        w = self.tab()
        w.search_box.setText("fate")
        for result in (True, False):
            with mock.patch("ui.news_tab.MatchDialog") as dialog, mock.patch.object(w, "follow"):
                dialog.return_value.exec.return_value = result
                dialog.return_value.picked = m(1, "Fate") if result else None
                w.follow_search()
            dialog.return_value.deleteLater.assert_called_once()

    def test_follow_accepted_without_a_pick_does_nothing(self):
        w = self.tab()
        w.search_box.setText("fate")
        with mock.patch("ui.news_tab.MatchDialog") as dialog, mock.patch.object(w, "follow") as follow:
            dialog.return_value.exec.return_value = True
            dialog.return_value.picked = None
            w.follow_search()
        follow.assert_not_called()
        self.assertEqual("fate", w.search_box.text())                  # kept to try again

    def test_a_removed_library_anime_can_be_followed(self):
        # Its old record is still in the file; it isn't on the page any more.
        w = self.tab(records={"https://site/gone": {"anilist_id": 1, "state": {"kind": "finished",
                                                                            "anilist_id": 1}}})
        with mock.patch("ui.news_tab.InfoBar") as bar, mock.patch.object(w, "check", return_value=True):
            entry = w.follow(m(1, "Gone Show"))
        self.assertIsNotNone(entry)
        bar.success.assert_called_once()

    def test_following_the_same_anime_from_two_pickers(self):
        # Two clicks race: the second sees it in the file and adds nothing.
        w = self.tab()
        with mock.patch("ui.news_tab.InfoBar") as bar, mock.patch.object(w, "check", return_value=True), \
                mock.patch.object(w, "tracked_ids", return_value=set()):
            first = w.follow(m(5, "Twice"))
            second = w.follow(m(5, "Twice"))
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(1, len(self.news.followed()))
        bar.success.assert_called_once()

    def test_a_followed_cover_that_cant_be_saved(self):
        from PyQt6.QtGui import QImage
        w = self.tab()
        with mock.patch("ui.news_tab.InfoBar"), mock.patch.object(w, "check", return_value=True), \
                mock.patch("ui.watchlist_tab._persist_cover", return_value=""):
            entry = w.follow(m(6, "No Cover"), QImage(4, 4, QImage.Format.Format_RGB32))
        self.assertEqual("", entry["cover"])                           # a placeholder, not a failure

    def test_the_followed_card_shows_its_name_as_written(self):
        name = "<Infinite Dendrogram>"
        w = self.tab(followed=[{"url": "anilist:9", "title": name}],
                     records={"anilist:9": {"anilist_id": 9, "state": {
                         "kind": "airing", "key": "airing", "headline": f"{name} is airing.", "detail": "",
                         "when": "", "season_title": name, "site_url": "", "anilist_id": 9}}})
        from PyQt6.QtWidgets import QLabel
        texts = [lbl.text() for lbl in w._rows[0].findChildren(QLabel)
                 if lbl.textFormat().name == "PlainText"]
        self.assertIn(name, texts)
        self.assertIn(f"{name} is airing.", texts)


if __name__ == "__main__":
    unittest.main()
