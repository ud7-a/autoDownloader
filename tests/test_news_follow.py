"""Anime news: following an anime that isn't in the Library, from the tab's search
bar (utils/anime_news.follow, ui/news_tab.NewsWidget.follow). No test reaches
AniList.

Run with: py -m unittest discover -s tests
"""

import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
__import__("tests")

from tests.test_news import FakeAniList, S1, S2, S3, m  # noqa: E402
from tests.test_news_edges import Base, TabBase  # noqa: E402


class FollowStoreTests(Base):
    def test_follow_then_unfollow(self):
        entry = self.news.follow(S1, cover=r"C:\covers\1.img")
        self.assertEqual({"url": "anilist:1", "title": "Show: The Long Road",
                          "cover": r"C:\covers\1.img"}, {k: entry[k] for k in ("url", "title", "cover")})
        self.assertEqual([entry], self.news.followed())
        rec = self.news.load()["shows"]["anilist:1"]
        # Picked on AniList itself: matched already, and never searched by title.
        self.assertEqual((1, True, None, 0), (rec["anilist_id"], rec["manual"], rec["state"],
                                              rec["checked_at"]))
        self.assertIsNone(self.news.follow(S1))                  # followed already
        self.assertEqual(1, len(self.news.followed()))

        self.assertEqual(entry, self.news.unfollow("anilist:1"))
        self.assertEqual([], self.news.followed())
        self.assertNotIn("anilist:1", self.news.load()["shows"])
        self.assertIsNone(self.news.unfollow("anilist:1"))       # gone already

    def test_a_damaged_followed_list_is_cleaned(self):
        self.write_file({"shows": {}, "checked_at": 0, "followed": [
            {"url": "anilist:1", "title": "Kept"}, {"url": "https://site/x", "title": "Not ours"},
            {"url": "anilist:2"}, "junk", None, {"title": "No url"}]})
        self.assertEqual([{"url": "anilist:1", "title": "Kept"}], self.news.followed())
        for broken in ({"shows": {}, "followed": "nope"}, {"shows": {}, "followed": None}, {"shows": {}}):
            self.write_file(broken)
            self.assertEqual([], self.news.followed(), broken)

    def test_is_followed(self):
        self.assertTrue(self.news.is_followed("anilist:5"))
        for url in ("https://witanime/anime/x", "", None):
            self.assertFalse(self.news.is_followed(url))

    def test_a_followed_show_is_checked_by_id_without_a_search(self):
        fake = FakeAniList({1: S1, 2: S2})
        self.news.follow(S1)
        shows, changes, errors = self.news.check(self.news.followed(), fetch=fake, gap=0)
        self.assertEqual(([], []), (changes, errors))
        self.assertFalse(any("search" in c for c in fake.calls))
        state = shows["anilist:1"]["state"]
        self.assertEqual(("coming", 3), (state["kind"], state["anilist_id"]))

    def test_unfollowed_during_a_check_stays_gone(self):
        self.news.follow(S1)
        fake = FakeAniList({1: S1, 2: S2})

        def fetch(query, variables):
            self.news.unfollow("anilist:1")                      # the user, meanwhile
            return fake(query, variables)
        self.news.check(self.news.followed(), fetch=fetch, gap=0)
        self.assertNotIn("anilist:1", self.news.load()["shows"])
        self.assertEqual([], self.news.followed())


class FollowTabTests(TabBase):
    def state(self, kind, anilist_id=None, headline="h"):
        return {"kind": kind, "key": kind, "headline": headline, "detail": "", "when": "",
                "season_title": "S", "site_url": "https://anilist.co/anime/1",
                "anilist_id": anilist_id}

    def follow_file(self, followed, records, library=()):
        """The tab over this Library and file (TabBase.shows writes no followed list)."""
        self.write_file({"shows": records, "checked_at": time.time(), "followed": followed})
        p = mock.patch("ui.news_tab.library_shows", return_value=list(library))
        p.start()
        self.addCleanup(p.stop)
        from ui.news_tab import NewsWidget
        w = NewsWidget()
        self.addCleanup(w.deleteLater)
        w.refresh()
        return w

    def test_a_followed_anime_gets_its_own_row(self):
        lib = [{"url": "https://site/a", "title": "Library Show"}]
        w = self.follow_file([{"url": "anilist:9", "title": "Followed Show", "cover": ""}],
                             {"https://site/a": {"state": self.state("finished", 1)},
                              "anilist:9": {"anilist_id": 9, "manual": True,
                                            "state": self.state("coming", 9)}}, lib)
        self.assertEqual(["anilist:9", "https://site/a"], [r.url for r in w._rows])
        followed_row = w._rows[0]
        labels = [lbl.text() for lbl in followed_row.findChildren(type(w.lead))]
        self.assertIn("Following", labels)
        self.assertEqual(["Check this show", "Stop following"], [t for _i, t, _s in followed_row.actions()])
        self.assertEqual(["Not the right anime?", "Check this show"],
                         [t for _i, t, _s in w._rows[1].actions()])
        self.assertTrue(w.lead.text().startswith("1 show from your Library, 1 followed · "))

    def test_only_followed_anime(self):
        w = self.follow_file([{"url": "anilist:9", "title": "A"}, {"url": "anilist:8", "title": "B"}],
                             {"anilist:9": {"anilist_id": 9, "state": None},
                              "anilist:8": {"anilist_id": 8, "state": None}})
        self.assertTrue(w.lead.text().startswith("2 followed shows · "))
        self.assertEqual(2, len(w._rows))
        self.assertTrue(w.empty.isHidden())

    def test_a_library_season_leads_over_a_followed_copy(self):
        # The same anime in the Library and followed (its newest season is one id):
        # one row, the Library's, that can still stop following the copy.
        lib = [{"url": "https://site/a", "title": "Show 2nd Season"}]
        w = self.follow_file([{"url": "anilist:1", "title": "Show 3rd Season", "added": time.time()}],
                             {"https://site/a": {"anilist_id": 2, "state": self.state("coming", 3)},
                              "anilist:1": {"anilist_id": 1, "state": self.state("coming", 3)}}, lib)
        self.assertEqual(["https://site/a"], [r.url for r in w._rows])
        row = w._rows[0]
        labels = [lbl.text() for lbl in row.findChildren(type(w.lead))]
        self.assertFalse(any(t.startswith("Also in your Library") for t in labels))
        self.assertNotIn("Following", labels)
        self.assertIn("Stop following", [t for _i, t, _s in row.actions()])
        with mock.patch("ui.news_tab.InfoBar"):
            w.unfollow(row.url)
        self.assertEqual([], self.news.followed())
        self.assertIn("https://site/a", self.news.load()["shows"])     # the Library's stays
        self.assertNotIn("Stop following", [t for _i, t, _s in w._rows[0].actions()])

    def test_follow_adds_saves_the_cover_and_checks_quietly(self):
        w = self.follow_file([], {})
        w.search_box.setText("show")
        from PyQt6.QtGui import QColor, QImage
        img = QImage(8, 8, QImage.Format.Format_RGB32)
        img.fill(QColor("red"))
        with mock.patch("ui.news_tab.InfoBar") as bar, \
                mock.patch("ui.watchlist_tab._persist_cover", return_value=r"C:\c.img") as persist, \
                mock.patch.object(w, "check", return_value=True) as check:
            entry = w.follow(S1, img)
        persist.assert_called_once_with("anilist:1", img)
        self.assertEqual(r"C:\c.img", entry["cover"])
        check.assert_called_once_with(force=True, entries=[entry])     # not manual: no "nothing new"
        bar.success.assert_called_once()
        self.assertEqual("", w.search_box.text())
        self.assertEqual(["anilist:1"], [r.url for r in w._rows])

    def test_following_without_a_cover(self):
        w = self.follow_file([], {})
        with mock.patch("ui.news_tab.InfoBar"), mock.patch("ui.watchlist_tab._persist_cover") as persist, \
                mock.patch.object(w, "check", return_value=True):
            entry = w.follow(S1, None)
        persist.assert_not_called()
        self.assertEqual("", entry["cover"])

    def test_an_anime_already_here_isnt_followed_twice(self):
        lib = [{"url": "https://site/a", "title": "Show"}]
        # Matched to season 1, its newest season is 3: either id is "already here".
        w = self.follow_file([], {"https://site/a": {"anilist_id": 1, "state": self.state("coming", 3)}}, lib)
        for picked in (S1, S3):
            with mock.patch("ui.news_tab.InfoBar") as bar, mock.patch.object(w, "check") as check:
                self.assertIsNone(w.follow(picked))
            bar.info.assert_called_once()
            check.assert_not_called()
        self.assertEqual([], self.news.followed())

    def test_followed_while_a_check_runs_is_checked_after_it(self):
        w = self.follow_file([], {})
        with mock.patch("ui.news_tab.InfoBar"), mock.patch.object(w, "check", return_value=False):
            entry = w.follow(S1)
        self.assertEqual([entry], w._pending)
        with mock.patch.object(w, "check", return_value=False):
            w._check_pending()                                   # still busy: kept
        self.assertEqual([entry], w._pending)
        with mock.patch.object(w, "check", return_value=True) as check:
            w._check_pending()
        check.assert_called_once_with(force=True, entries=[entry])
        self.assertEqual([], w._pending)

    def test_the_search_bar_opens_the_picker(self):
        w = self.follow_file([], {})
        with mock.patch("ui.news_tab.MatchDialog") as dialog:
            w.search_box.setText("   ")
            w.btn_search.click()
            dialog.assert_not_called()                           # nothing typed
            dialog.return_value.exec.return_value = True
            dialog.return_value.picked = S1
            dialog.return_value.picked_image = None
            w.search_box.setText(" frieren ")
            with mock.patch.object(w, "follow") as follow:
                w.search_box.returnPressed.emit()
        dialog.assert_called_once_with("frieren", None, w.window(), follow=True)
        follow.assert_called_once_with(S1, None)

    def test_cancelling_the_picker_follows_nothing(self):
        w = self.follow_file([], {})
        with mock.patch("ui.news_tab.MatchDialog") as dialog, mock.patch.object(w, "follow") as follow:
            dialog.return_value.exec.return_value = False
            w.search_box.setText("frieren")
            w.follow_search()
        follow.assert_not_called()

    def test_followed_rows_find_in_search_but_have_no_match_to_fix(self):
        w = self.follow_file([{"url": "anilist:9", "title": "Show 2nd Season"}],
                             {"anilist:9": {"anilist_id": 9, "state": self.state("airing", 9)}})
        got = []
        w.search_requested.connect(lambda q, d: got.append((q, d)))
        w.find_in_search("anilist:9")
        self.assertEqual([("show", "")], got)
        with mock.patch("ui.news_tab.MatchDialog") as dialog:
            w.pick_match("anilist:9")
        dialog.assert_not_called()

    def test_check_now_and_check_this_show_include_followed(self):
        w = self.follow_file([{"url": "anilist:9", "title": "F"}],
                             {"anilist:9": {"anilist_id": 9, "state": None}},
                             [{"url": "https://site/a", "title": "L"}])
        with mock.patch("ui.news_tab.CheckThread") as thread:
            w.check(force=True, manual=True)
        self.assertEqual(["https://site/a", "anilist:9"], [e["url"] for e in thread.call_args[0][0]])
        with mock.patch.object(w, "check") as check:
            w.recheck("anilist:9")
        self.assertEqual(["anilist:9"], [e["url"] for e in check.call_args.kwargs["entries"]])


class FollowDialogTests(TabBase):
    def dialog(self, found, query, more=False):
        from ui.news_tab import MatchDialog
        from PyQt6.QtWidgets import QWidget
        host = QWidget()
        self.addCleanup(host.deleteLater)
        with mock.patch.object(self.news, "search_page", return_value=(found, more)) as search:
            dlg = MatchDialog(query, None, host, follow=True)
            for t in list(dlg._threads):
                t.wait(5000)
            self._app.processEvents()
        return dlg, search

    def test_follow_mode_searches_what_was_typed(self):
        dlg, search = self.dialog([m(2, "Show 2nd Season")], " Show 2nd Season ")
        # Not cut down to the anime's name, as a Library title is: the user may
        # want exactly that season.
        search.assert_called_once_with("Show 2nd Season", 1)
        self.assertEqual("Follow", dlg.yesButton.text())
        from qfluentwidgets import SubtitleLabel
        self.assertEqual(["Follow an anime"], [lbl.text() for lbl in dlg.findChildren(SubtitleLabel)])

    def test_a_long_name_ends_in_an_ellipsis_inside_the_dialog(self):
        long = ("MASHLE: MAGIC AND MUSCLES The Tri-Magicathlon Divine Visionary Final "
                "Exam Arc, Part Two")
        dlg, _s = self.dialog([m(2, long, english=long, synonyms=[long + " (romaji)"])], "mashle")
        card = dlg._cards[0]
        # Nothing in the card asks for more width than the dialog has.
        self.assertLess(card.minimumSizeHint().width(), dlg.widget.minimumWidth())
        from PyQt6.QtCore import QSize
        from PyQt6.QtGui import QResizeEvent

        def resize(width):
            # A hidden widget gets its resize event when shown: hand it over now.
            old = card.title.size()
            card.title.resize(width, old.height())
            self._app.sendEvent(card.title, QResizeEvent(QSize(width, old.height()), old))
        resize(200)
        self.assertTrue(card.title.text().endswith("…"))
        self.assertEqual(long, card.title.toolTip())
        resize(4000)
        self.assertEqual(long, card.title.text())
        self.assertEqual("", card.title.toolTip())                   # whole: no hint needed

    def test_the_list_shows_more_rows_and_scrolls_past_them(self):
        found = [m(i, f"Fate {i}") for i in range(1, 18)]
        dlg, _s = self.dialog(found, "fate")
        self.assertEqual(17, len(dlg._cards))
        host = dlg.parentWidget()
        # Six rows when there's room; in an 800 px window (the app at 125% on a
        # 1440p screen) 4.7 rows, the last cut to show there's more; never under 2.
        for height, list_h in ((2000, 540), (800, 430), (300, 180)):
            host.resize(800, height)
            self.assertEqual(list_h, dlg.list_height(), height)
        host.resize(800, 2000)
        dlg._show(dlg._generation, found, "")
        self.assertEqual(540, dlg.scroll.height())                   # then it scrolls
        self.assertGreater(dlg.scroll.widget().sizeHint().height(), dlg.scroll.height())
        dlg._show(dlg._generation, found[:2], "")                    # fewer than fit
        self.assertEqual(2 * dlg.ROW_STEP, dlg.scroll.height())

    def test_accept_keeps_the_picked_cover(self):
        from PyQt6.QtGui import QColor, QImage
        dlg, _s = self.dialog([m(2, "Show"), m(3, "Show Two")], "show")
        img = QImage(8, 8, QImage.Format.Format_RGB32)
        img.fill(QColor("red"))
        dlg._cards[1].image = img
        dlg._pick(dlg._cards[1])
        from ui import news_tab
        with mock.patch.object(news_tab.MessageBoxBase, "accept"):
            dlg.accept()
        self.assertEqual(3, dlg.picked["id"])
        self.assertIs(img, dlg.picked_image)


class PickListSearchTests(Base):
    def test_matching_asks_for_10_and_the_pick_list_for_all_anilist_gives(self):
        fake = FakeAniList(search_results={"show": [S1]})
        self.news.search("show", fake)
        self.assertEqual(10, fake.calls[-1]["perPage"])
        self.news.search("show", fake, per_page=self.news.PICK_RESULTS)
        self.assertEqual(50, fake.calls[-1]["perPage"])          # AniList's cap per request

    def test_the_search_thread_gets_every_cover_in_list_order(self):
        import io
        from ui.news_tab import SearchThread
        found = [dict(m(i, f"Fate {i}"), coverImage={"medium": f"https://img/{i}"} if i != 3 else {})
                 for i in range(1, 9)]

        def urlopen(req, timeout):
            if req.full_url.endswith("/5"):
                raise OSError("one cover fails")
            resp = io.BytesIO(req.full_url.encode())
            resp.__enter__ = lambda s=resp: s
            resp.__exit__ = lambda *a: False
            return resp
        for page, offset in ((1, 0), (3, 100)):
            t = SearchThread("fate", 7, page)
            lists, covers = [], []
            t.results.connect(lambda g, f, e, more: lists.append((g, len(f), e, more)))
            t.cover.connect(lambda g, i, d: covers.append((g, i, d)))
            with mock.patch.object(self.news, "search_page", return_value=(found, True)) as search, \
                    mock.patch("urllib.request.urlopen", side_effect=urlopen):
                t.run()                                          # here, not in a thread
            search.assert_called_once_with("fate", page)
            self.assertEqual([(7, 8, "", True)], lists)
            # No cover for 3 (none on AniList) or 5 (failed); the rest in order, by
            # their place in AniList's whole answer (page 3 starts at 100).
            self.assertEqual([(7, offset + i - 1, f"https://img/{i}".encode())
                              for i in (1, 2, 4, 6, 7, 8)], covers)

    def test_search_page_asks_for_a_page_and_reads_whether_more_follow(self):
        calls = []

        def fetch(query, variables):
            calls.append(variables)
            return {"Page": {"pageInfo": {"hasNextPage": variables["page"] < 3},
                             "media": [S1] if variables["page"] < 3 else []}}
        self.assertEqual(([S1], True), self.news.search_page("show", 2, fetch))
        self.assertEqual({"search": "show", "perPage": 50, "page": 2}, calls[-1])
        self.assertEqual(([], False), self.news.search_page("show", 3, fetch))
        # An answer without pageInfo (or no Page at all) has no next page.
        self.assertEqual(([S1], False), self.news.search_page("show", 1, lambda q, v: {"Page": {"media": [S1]}}))
        self.assertEqual(([], False), self.news.search_page("show", 1, lambda q, v: {}))


class ShowMoreTests(TabBase):
    """"Show more results": AniList answers 50 a page; the next page goes under the
    cards already shown."""

    def dialog(self, pages):
        """A follow picker over these AniList pages ([media] per page), searched."""
        from ui.news_tab import MatchDialog
        from PyQt6.QtWidgets import QWidget
        host = QWidget()
        host.resize(900, 2000)
        self.addCleanup(host.deleteLater)
        self.asked = []

        def search_page(query, page):
            self.asked.append(page)
            if isinstance(pages[page - 1], Exception):
                raise pages[page - 1]
            return pages[page - 1], page < len(pages)
        p = mock.patch.object(self.news, "search_page", side_effect=search_page)
        p.start()
        self.addCleanup(p.stop)
        dlg = MatchDialog("fate", None, host, follow=True)
        self.wait(dlg)
        return dlg

    def wait(self, dlg):
        for t in list(dlg._threads):
            t.wait(5000)
        self._app.processEvents()

    def page(self, first, n=50, fmt="TV"):
        return [m(i, f"Fate {i}", fmt=fmt) for i in range(first, first + n)]

    def ids(self, dlg):
        return [c.media["id"] for c in dlg._cards]

    def test_more_pages_are_added_under_the_first(self):
        dlg = self.dialog([self.page(1), self.page(51), self.page(101, 7)])
        self.assertEqual(50, len(dlg._cards))
        self.assertFalse(dlg.btn_more.isHidden())
        dlg._pick(dlg._cards[3])
        dlg.btn_more.click()
        self.assertFalse(dlg.btn_more.isEnabled())                   # while it loads
        self.wait(dlg)
        self.assertEqual([1, 2], self.asked)
        self.assertEqual(100, len(dlg._cards))
        self.assertEqual(list(range(1, 51)), sorted(self.ids(dlg)[:50]))   # the first stay first
        self.assertTrue(dlg._cards[3].selected)                      # and the pick stays
        self.assertTrue(dlg.btn_more.isEnabled())
        self.assertEqual("Show more results", dlg.btn_more.text())
        dlg.btn_more.click()
        self.wait(dlg)
        self.assertEqual(107, len(dlg._cards))
        self.assertTrue(dlg.btn_more.isHidden())                     # AniList has no more
        self.assertIn("107 results", dlg.status.text())
        # The button sits under the last card.
        self.assertEqual(dlg.list_col.count() - 1, dlg.list_col.indexOf(dlg.btn_more))

    def test_one_page_only_has_no_button(self):
        dlg = self.dialog([self.page(1, 12)])
        self.assertEqual(12, len(dlg._cards))
        self.assertTrue(dlg.btn_more.isHidden())

    def test_covers_of_a_later_page_land_on_its_cards(self):
        dlg = self.dialog([self.page(1), self.page(51)])
        dlg.btn_more.click()
        self.wait(dlg)
        card = next(c for c in dlg._cards if c.media["id"] == 60)    # AniList's 60th: index 59
        with mock.patch.object(card, "set_cover") as set_cover:
            dlg._cover(dlg._generation, 59, b"img")
        set_cover.assert_called_once_with(b"img")

    def test_films_and_repeats_on_a_later_page_are_left_out(self):
        later = self.page(51, 3, fmt="MOVIE") + [m(2, "Fate 2")] + self.page(60, 2)
        dlg = self.dialog([self.page(1), later, self.page(200, 1)])
        dlg.btn_more.click()
        self.wait(dlg)
        self.assertEqual(52, len(dlg._cards))                        # only 60 and 61 are new
        self.assertEqual(1, self.ids(dlg).count(2))
        # A page of nothing new says so, and the button stays for the next one.
        dlg2 = self.dialog([self.page(1), self.page(51, 4, fmt="MOVIE"), self.page(300, 1)])
        dlg2.btn_more.click()
        self.wait(dlg2)
        self.assertEqual(50, len(dlg2._cards))
        self.assertIn("films, specials or ones shown already", dlg2.status.text())
        self.assertFalse(dlg2.btn_more.isHidden())

    def test_a_failed_page_can_be_tried_again(self):
        error = self.news.NewsError("AniList couldn't be reached (URLError)")
        pages = [self.page(1), error]
        dlg = self.dialog(pages)
        dlg.btn_more.click()
        self.wait(dlg)
        self.assertEqual(50, len(dlg._cards))
        self.assertIn("Show more results", dlg.status.text())
        self.assertTrue(dlg.btn_more.isEnabled() and not dlg.btn_more.isHidden())
        pages[1] = self.page(51, 5)
        dlg.btn_more.click()
        self.wait(dlg)
        self.assertEqual([1, 2, 2], self.asked)                      # the same page again
        self.assertEqual(55, len(dlg._cards))

    def test_a_new_search_starts_again_from_page_one(self):
        dlg = self.dialog([self.page(1), self.page(51)])
        dlg.btn_more.click()
        self.wait(dlg)
        dlg.search_box.setText("fate zero")
        dlg.btn_search.click()
        self.wait(dlg)
        self.assertEqual([1, 2, 1], self.asked)
        self.assertEqual(50, len(dlg._cards))
        self.assertEqual("fate zero", dlg._query)

    def test_a_late_page_of_an_old_search_is_ignored(self):
        dlg = self.dialog([self.page(1), self.page(51)])
        old = dlg._generation
        dlg._generation += 1                                         # searched again meanwhile
        dlg._more(old, self.page(51), "", True)
        self.assertEqual(50, len(dlg._cards))


if __name__ == "__main__":
    unittest.main()
