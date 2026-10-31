"""Anime news (utils/anime_news.py, ui/news_tab.py): matching a Library anime to
AniList, following its sequels to the newest season, the wording of each state,
change notices, the once-a-day cache and the tab itself. No test reaches AniList:
every request goes to a fake built from the fixtures below.

Run with: py -m unittest discover -s tests
"""

import datetime
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
__import__("tests")


def m(id, title, status="FINISHED", fmt="TV", start=(2020, 1, 1), end=None, source="MANGA",
      relations=(), english=None, synonyms=(), next_ep=None):
    """An AniList media dict as the API returns it."""
    def date(d):
        return dict(zip(("year", "month", "day"), d)) if d else {"year": None, "month": None, "day": None}
    out = {"id": id, "siteUrl": f"https://anilist.co/anime/{id}", "format": fmt, "status": status,
           "source": source, "episodes": 12, "synonyms": list(synonyms),
           "title": {"romaji": title, "english": english}, "startDate": date(start),
           "endDate": date(end), "nextAiringEpisode": next_ep,
           "relations": {"edges": [{"relationType": rel, "node": dict(node, type=node.get("type", "ANIME"))}
                                   for rel, node in relations]}}
    return out


def manga(status):
    return {"id": 9000, "type": "MANGA", "format": "MANGA", "status": status,
            "title": {"romaji": "The manga", "english": None}}


class FakeAniList:
    """Answers search and media queries from fixtures, and counts them."""

    def __init__(self, media_by_id=None, search_results=None, fail=()):
        self.media = dict(media_by_id or {})
        self.results = dict(search_results or {})
        self.fail = set(fail)
        self.calls = []

    def __call__(self, query, variables):
        from utils import anime_news as news
        self.calls.append(dict(variables))
        if "search" in variables:
            if variables["search"] in self.fail:
                raise news.NewsError("AniList couldn't be reached (URLError)")
            return {"Page": {"media": self.results.get(variables["search"], [])}}
        if variables["id"] in self.fail:
            raise news.NewsError("AniList answered HTTP 500")
        return {"Media": self.media.get(variables["id"])}


# Frieren-like: season 1 -> season 2 (finished) -> season 3 announced for October 2027.
S3 = m(3, "Show 3rd Season", status="NOT_YET_RELEASED", start=(2027, 10, None))
S2 = m(2, "Show 2nd Season", start=(2026, 1, 16), end=(2026, 3, 27),
       relations=[("PREQUEL", {"id": 1, "format": "TV", "status": "FINISHED"}),
                  ("SEQUEL", S3), ("SOURCE", manga("RELEASING"))])
S1 = m(1, "Show", english="Show: The Long Road", start=(2023, 9, 29), end=(2024, 3, 22),
       relations=[("SEQUEL", S2), ("SEQUEL", m(50, "Show Movie", fmt="MOVIE")),
                  ("SOURCE", manga("RELEASING"))])


class MatchingTests(unittest.TestCase):
    def setUp(self):
        from utils import anime_news
        self.news = anime_news

    def test_short_name_before_a_subtitle_matches(self):
        """The sites say "Frieren" for "Frieren: Beyond Journey's End"."""
        self.assertIs(S1, self.news.best_match("Show", [S1]))
        self.assertIs(S1, self.news.best_match("Show 2nd Season", [S1]))

    def test_series_beat_films_and_the_first_season_wins(self):
        film = m(50, "Show", fmt="MOVIE", start=(2019, 1, 1))
        later = m(2, "Show", start=(2026, 1, 1))
        self.assertIs(S1, self.news.best_match("Show", [film, later, S1]))

    def test_nothing_close_enough_is_no_match(self):
        self.assertIsNone(self.news.best_match("Sample Show", [m(7, "Completely Different")]))
        self.assertIsNone(self.news.best_match("", [S1]))

    def test_candidate_line_and_other_title(self):
        self.assertEqual("TV · 2023 · 12 episodes · Finished", self.news.candidate_line(S1))
        self.assertEqual("Film · 2020 · 12 episodes · Not out yet",
                         self.news.candidate_line(m(5, "X", fmt="MOVIE", status="NOT_YET_RELEASED")))
        self.assertEqual("Show", self.news.other_title(S1))           # romaji under English
        self.assertEqual("", self.news.other_title(m(5, "X")))

    def test_run_together_names_are_split_for_the_search(self):
        """AniList finds nothing for "fatezero"; "fate zero" finds Fate/Zero."""
        self.assertEqual("fate zero", self.news.search_text("FateZero"))
        self.assertEqual("black clover", self.news.search_text("Black Clover 2nd Season"))
        fate_zero = m(11, "Fate/Zero")
        self.assertIs(fate_zero, self.news.best_match("FateZero", [fate_zero]))


class NewestSeasonTests(unittest.TestCase):
    def setUp(self):
        from utils import anime_news
        self.news = anime_news

    def test_follows_sequels_and_stops_at_an_announced_season(self):
        fake = FakeAniList({1: S1, 2: S2})
        newest, source = self.news.newest_season(1, fake, gap=0)
        self.assertEqual(3, newest["id"])
        self.assertEqual("RELEASING", source)
        # Films are skipped; the announced season needs no request of its own.
        self.assertEqual([{"id": 1}, {"id": 2}], fake.calls)

    def test_a_sequel_loop_ends(self):
        a = m(1, "A", relations=[("SEQUEL", {"id": 2, "format": "TV", "status": "FINISHED"})])
        b = m(2, "B", relations=[("SEQUEL", {"id": 1, "format": "TV", "status": "FINISHED"})])
        newest, _src = self.news.newest_season(1, FakeAniList({1: a, 2: b}), gap=0)
        self.assertEqual(2, newest["id"])

    def test_an_unknown_id_is_an_error(self):
        with self.assertRaises(self.news.NewsError):
            self.news.newest_season(404, FakeAniList({}), gap=0)


class DescribeTests(unittest.TestCase):
    def setUp(self):
        from utils import anime_news
        self.news = anime_news

    def test_airing_with_the_next_episode(self):
        at = time.mktime(datetime.datetime(2026, 11, 7, 18, 0).timetuple())
        s = self.news.describe(m(4, "Clover S2", status="RELEASING", start=(2026, 10, 3),
                                 next_ep={"episode": 6, "airingAt": at}), "RELEASING")
        self.assertEqual(self.news.AIRING, s["kind"])
        self.assertEqual("Clover S2 is airing. Episode 6 comes out next.", s["headline"])
        self.assertEqual("Sat 7 Nov", s["when"])
        self.assertEqual("Started 3 October 2026", s["detail"])

    def test_coming_with_a_full_partial_or_no_date(self):
        d = self.news.describe
        self.assertEqual(("X is coming on 3 April 2027.", "3 April 2027"),
                         (lambda s: (s["headline"], s["when"]))(
                             d(m(5, "X", status="NOT_YET_RELEASED", start=(2027, 4, 3)), None)))
        self.assertEqual("X is coming in April 2027.",
                         d(m(5, "X", status="NOT_YET_RELEASED", start=(2027, 4, None)), None)["headline"])
        self.assertEqual("X is coming in 2027.",
                         d(m(5, "X", status="NOT_YET_RELEASED", start=(2027, None, None)), None)["headline"])
        s = d(m(5, "X", status="NOT_YET_RELEASED", start=None), None)
        self.assertEqual(("X is coming.", "No date yet", self.news.COMING), (s["headline"], s["when"], s["kind"]))

    def test_finished_waiting_and_paused(self):
        d, done = self.news.describe, m(6, "Y", end=(2022, 12, 22))
        self.assertEqual(self.news.FINISHED, d(done, "FINISHED")["kind"])
        self.assertEqual(self.news.FINISHED, d(done, "ORIGINAL")["kind"])
        waiting = d(done, "RELEASING")
        self.assertEqual(self.news.WAITING, waiting["kind"])
        self.assertIn("still going", waiting["detail"])
        self.assertEqual(self.news.WAITING, d(done, None)["kind"])     # source unknown
        self.assertEqual(self.news.PAUSED, d(m(7, "Z", status="HIATUS"), None)["kind"])

    def test_an_original_anime_says_so(self):
        self.assertEqual("ORIGINAL", self.news._source_status(m(8, "O", source="ORIGINAL")))


class ChangeNoticeTests(unittest.TestCase):
    def setUp(self):
        from utils import anime_news
        self.news = anime_news

    def state(self, media, source=None):
        return self.news.describe(media, source)

    def test_the_notices(self):
        msg = self.news.change_message
        waiting = self.state(m(2, "Show S2", end=(2026, 3, 27)), "RELEASING")
        announced = self.state(m(3, "Show S3", status="NOT_YET_RELEASED", start=None))
        dated = self.state(m(3, "Show S3", status="NOT_YET_RELEASED", start=(2027, 10, None)))
        airing = self.state(m(3, "Show S3", status="RELEASING", start=(2027, 10, 2)))
        self.assertEqual("Show: Show S3 has been announced.", msg("Show", waiting, announced))
        self.assertEqual("Show: Show S3 has been announced for October 2027.",
                         msg("Show", waiting, dated))
        self.assertEqual("Show: Show S3 now has a date, October 2027.", msg("Show", announced, dated))
        self.assertEqual("Show: Show S3 has started airing.", msg("Show", dated, airing))

    def test_no_notice_for_the_first_check_or_weekly_episodes(self):
        msg = self.news.change_message
        airing = self.state(m(3, "S", status="RELEASING", next_ep={"episode": 4, "airingAt": 1.8e9}))
        later = self.state(m(3, "S", status="RELEASING", next_ep={"episode": 5, "airingAt": 1.81e9}))
        self.assertIsNone(msg("S", None, airing))
        self.assertIsNone(msg("S", airing, later))
        finished = self.state(m(3, "S", end=(2027, 1, 1)), "FINISHED")
        self.assertIsNone(msg("S", airing, finished))          # ending isn't news


class CheckAndStoreTests(unittest.TestCase):
    def setUp(self):
        from utils import anime_news
        self.news = anime_news
        self.tmp = tempfile.mkdtemp(prefix="aed_news_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        p = mock.patch.object(anime_news, "FILE", os.path.join(self.tmp, "anime_news.json"))
        p.start()
        self.addCleanup(p.stop)
        self.entries = [{"url": "https://witanime.site/anime/show/", "title": "Show"},
                        {"url": "https://witanime.site/anime/other/", "title": "Sample Show"}]

    def fake(self, **kw):
        return FakeAniList({1: S1, 2: S2}, {"show": [S1], "sample show": []}, **kw)

    def test_first_check_matches_saves_and_says_nothing(self):
        shows, changes, errors = self.news.check(self.entries, fetch=self.fake(), gap=0, now=1000)
        self.assertEqual(([], []), (changes, errors))
        show = shows["https://witanime.site/anime/show/"]
        self.assertEqual((1, "Show: The Long Road", False), (show["anilist_id"], show["match_title"], show["manual"]))
        self.assertEqual(self.news.COMING, show["state"]["kind"])
        self.assertEqual(self.news.UNKNOWN, shows["https://witanime.site/anime/other/"]["state"]["kind"])
        with open(self.news.FILE, encoding="utf-8") as f:
            self.assertIn("https://witanime.site/anime/show/", json.load(f)["shows"])

    def test_checked_once_a_day_unless_forced(self):
        self.news.check(self.entries, fetch=self.fake(), gap=0, now=1000)
        again = self.fake()
        self.news.check(self.entries, fetch=again, gap=0, now=1000 + 3600)
        self.assertEqual([], again.calls)
        self.news.check(self.entries, fetch=again, gap=0, now=1000 + 3600, force=True)
        self.assertTrue(again.calls)
        later = self.fake()
        self.news.check(self.entries, fetch=later, gap=0, now=1000 + self.news.MAX_AGE + 3600)
        self.assertTrue(later.calls)

    def test_a_change_is_reported_once_and_marked_new(self):
        before = m(2, "Show 2nd Season", end=(2026, 3, 27),
                   relations=[("SOURCE", manga("RELEASING"))])
        s1 = m(1, "Show", english="Show: The Long Road", relations=[("SEQUEL", before)])
        self.news.check(self.entries[:1], fetch=FakeAniList({1: s1, 2: before}, {"show": [s1]}), gap=0, now=1000)
        shows, changes, _e = self.news.check(self.entries[:1], fetch=self.fake(), gap=0, force=True, now=2000)
        self.assertEqual([("Show", "Show: Show 3rd Season has been announced for October 2027.")], changes)
        record = shows["https://witanime.site/anime/show/"]
        self.assertTrue(self.news.is_fresh(record, now=2000 + 3600))
        self.assertFalse(self.news.is_fresh(record, now=2000 + 8 * 24 * 3600))
        _s, changes, _e = self.news.check(self.entries[:1], fetch=self.fake(), gap=0, force=True, now=3000)
        self.assertEqual([], changes)

    def test_a_failure_keeps_the_last_state_and_the_rest_go_on(self):
        self.news.check(self.entries, fetch=self.fake(), gap=0, now=1000)
        shows, _c, errors = self.news.check(self.entries, fetch=self.fake(fail={1}), gap=0,
                                            force=True, now=2000)
        self.assertEqual([("Show", "AniList answered HTTP 500")], errors)
        self.assertEqual(self.news.COMING, shows["https://witanime.site/anime/show/"]["state"]["kind"])
        self.assertEqual(1000, self.news.load()["shows"]["https://witanime.site/anime/show/"]["checked_at"])

    def test_the_users_pick_is_kept_and_never_searched_again(self):
        self.news.set_match("https://witanime.site/anime/other/", 1, "Show: The Long Road")
        fake = self.fake()
        shows, _c, _e = self.news.check(self.entries[1:], fetch=fake, gap=0, now=1000)
        rec = shows["https://witanime.site/anime/other/"]
        self.assertTrue(rec["manual"])
        self.assertEqual(self.news.COMING, rec["state"]["kind"])
        self.assertNotIn({"search": "sample show"}, fake.calls)

    def test_stop_ends_between_shows(self):
        fake = self.fake()
        self.news.check(self.entries, fetch=fake, gap=0, now=1000, stop=lambda: True)
        self.assertEqual([], fake.calls)

    def test_a_broken_file_is_an_empty_store(self):
        with open(self.news.FILE, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertEqual({"shows": {}, "checked_at": 0}, self.news.load())


class HttpTests(unittest.TestCase):
    def setUp(self):
        from utils import anime_news
        self.news = anime_news

    def response(self, payload):
        resp = io.BytesIO(json.dumps(payload).encode("utf-8"))
        resp.__enter__ = lambda s=resp: s
        resp.__exit__ = lambda *a: False
        return resp

    def test_waits_once_when_asked_to_slow_down(self):
        busy = urllib.error.HTTPError(self.news.API_URL, 429, "Too Many Requests",
                                      {"Retry-After": "2"}, None)
        ok = self.response({"data": {"Page": {"media": []}}})
        with mock.patch("urllib.request.urlopen", side_effect=[busy, ok]) as urlopen, \
                mock.patch.object(self.news.time, "sleep") as sleep:
            self.assertEqual([], self.news.search("x"))
        self.assertEqual(2, urlopen.call_count)
        sleep.assert_called_once_with(2)

    def test_errors_become_news_errors(self):
        for exc in (urllib.error.HTTPError(self.news.API_URL, 500, "x", {}, None),
                    urllib.error.URLError("offline"), TimeoutError()):
            with mock.patch("urllib.request.urlopen", side_effect=exc):
                with self.assertRaises(self.news.NewsError):
                    self.news.search("x")
        with mock.patch("urllib.request.urlopen",
                        return_value=self.response({"errors": [{"message": "Not Found."}], "data": None})):
            with self.assertRaisesRegex(self.news.NewsError, "Not Found"):
                self.news.media(1)


class NewsTabTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        from utils import anime_news
        self.news = anime_news
        self.tmp = tempfile.mkdtemp(prefix="aed_news_ui_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        p = mock.patch.object(anime_news, "FILE", os.path.join(self.tmp, "anime_news.json"))
        p.start()
        self.addCleanup(p.stop)
        entries = [{"url": "u1", "title": "Show"}, {"url": "u2", "title": "Sample Show"}]
        self.news.check(entries, fetch=FakeAniList({1: S1, 2: S2}, {"show": [S1], "sample show": []}),
                        gap=0, now=time.time())
        q = mock.patch("ui.news_tab.library_shows", return_value=entries)
        q.start()
        self.addCleanup(q.stop)

    def test_rows_counts_and_filters(self):
        from ui.news_tab import NewsWidget
        w = NewsWidget()
        w.refresh()
        self.assertEqual(2, len(w._rows))
        self.assertEqual("All  2", w.seg.widget("all").text())
        self.assertEqual("Coming  1", w.seg.widget("coming").text())
        self.assertTrue(w.lead.text().startswith("2 shows from your Library · checked today at"))
        w._switch("coming")
        self.assertEqual(["u1"], [r.url for r in w._rows])
        w._switch("airing")
        self.assertEqual([], w._rows)
        self.assertEqual("No shows here right now.", w.empty.text())

    def test_only_an_airing_season_offers_find_in_search(self):
        from ui.news_tab import NewsWidget
        airing = m(10, "Clover", status="RELEASING", start=(2026, 10, 3),
                   next_ep={"episode": 6, "airingAt": time.time() + 86400})
        entries = [{"url": "u1", "title": "Show"},
                   {"url": "u3", "title": "Clover 2nd Season", "domain": "witanime.site"}]
        self.news.check(entries, fetch=FakeAniList({1: S1, 2: S2, 10: airing},
                                                   {"show": [S1], "clover": [airing]}),
                        gap=0, force=True, now=time.time())
        with mock.patch("ui.news_tab.library_shows", return_value=entries):
            w = NewsWidget()
            w.refresh()
            rows = {r.url: r for r in w._rows}
            self.assertIsNone(rows["u1"].btn_find)                    # coming: not on the sites yet
            self.assertEqual("Find in Search", rows["u3"].btn_find.text())
            asked = []
            w.search_requested.connect(lambda q, d: asked.append((q, d)))
            rows["u3"].btn_find.click()
        self.assertEqual([("clover", "witanime.site")], asked)

    def test_find_in_search_opens_search_already_searching(self):
        import types
        from ui.app_window import AppWindow
        from ui.search_tab import AnimeSearchWidget
        window = types.SimpleNamespace(open_tab=mock.Mock(), search_interface=mock.Mock())
        AppWindow._on_news_search(window, "clover", "witanime.site")
        window.open_tab.assert_called_once_with("search_interface")
        window.search_interface.search_for.assert_called_once_with("clover", "witanime.site")
        tab = types.SimpleNamespace(refresh_domains=mock.Mock(), txt_query=mock.Mock(),
                                    perform_search=mock.Mock())
        AnimeSearchWidget.search_for(tab, "clover", "witanime.site")
        tab.refresh_domains.assert_called_once_with(select="witanime.site")
        tab.txt_query.setText.assert_called_once_with("clover")
        tab.perform_search.assert_called_once_with()
        tab.refresh_domains.reset_mock()
        AnimeSearchWidget.search_for(tab, "clover", "")          # a local-folder show
        tab.refresh_domains.assert_not_called()

    def dialog(self, found, current_id=None):
        """A pick dialog that has searched. Never closed with done(): that starts a
        fade-out whose callback outlives the deleted dialog and crashes a later test."""
        from ui.news_tab import MatchDialog
        from PyQt6.QtWidgets import QWidget
        host = QWidget()
        self.addCleanup(host.deleteLater)
        with mock.patch.object(self.news, "search", return_value=found):
            dlg = MatchDialog("FateZero", current_id, host)
            for t in list(dlg._threads):
                t.wait(5000)
            self._app.processEvents()
        return dlg

    def test_pick_dialog_ranks_marks_and_picks(self):
        found = [m(1, "Fate/stay night: Unlimited Blade Works"), m(2, "Fate/Zero"),
                 m(3, "Fate/Zero", fmt="MOVIE"), m(4, "Hua Jianghu")]
        dlg = self.dialog(found, current_id=1)
        self.assertEqual("fate zero", dlg.search_box.text())
        titles = [self.news.display_title(c.media) for c in dlg._cards]
        self.assertEqual("Fate/Zero", titles[0])                  # closest name first
        self.assertEqual(3, len(titles))                          # the film is left out
        self.assertEqual("3 results. Click one to pick it, or double-click to use it right away.",
                         dlg.status.text())
        self.assertFalse(dlg.yesButton.isEnabled())
        dlg._pick(dlg._cards[1])
        self.assertTrue(dlg._cards[1].selected and not dlg._cards[0].selected)
        self.assertEqual(1, dlg.picked["id"])
        self.assertTrue(dlg.yesButton.isEnabled())

    def test_pick_dialog_ignores_an_older_search_and_says_when_nothing_is_found(self):
        dlg = self.dialog([m(2, "Fate/Zero")])
        dlg._generation += 1                                       # typed again
        dlg._show(dlg._generation - 1, [m(9, "Old Result")], "")
        self.assertEqual(["Fate/Zero"], [self.news.display_title(c.media) for c in dlg._cards])
        dlg.search_box.setText("zzz")
        dlg._show(dlg._generation, [], "")
        self.assertEqual([], dlg._cards)
        self.assertFalse(dlg.scroll.isVisibleTo(dlg))
        self.assertIn("Nothing on AniList is called “zzz”", dlg.status.text())
        dlg._show(dlg._generation, [], "AniList couldn't be reached (URLError)")
        self.assertIn("press Enter to try again", dlg.status.text())

    def test_notification_switch_is_saved(self):
        from ui.news_tab import NewsWidget
        from utils.config import app_settings
        w = NewsWidget()
        with mock.patch("ui.news_tab.save_config") as save:
            w.switch.setChecked(False)
        self.assertFalse(app_settings["news_notify"])
        save.assert_called()
        app_settings["news_notify"] = True

    def test_checked_text(self):
        from ui.news_tab import checked_text
        now = time.mktime(datetime.datetime(2026, 10, 31, 12, 0).timetuple())
        self.assertEqual("not checked yet", checked_text(0, now))
        self.assertEqual("checked today at 09:12",
                         checked_text(time.mktime(datetime.datetime(2026, 10, 31, 9, 12).timetuple()), now))
        self.assertEqual("checked 30 Oct at 21:05",
                         checked_text(time.mktime(datetime.datetime(2026, 10, 30, 21, 5).timetuple()), now))


class NewsNotificationTests(unittest.TestCase):
    def call(self, changes, enabled=True):
        import types
        from ui.app_window import AppWindow
        from utils.config import app_settings
        window = types.SimpleNamespace(notifier=mock.Mock())
        with mock.patch.dict(app_settings, {"news_notify": enabled}):
            AppWindow._on_news_changes(window, changes)
        return window.notifier.notify.call_args_list

    def test_one_notice_per_show_or_a_summary(self):
        calls = self.call([("A", "A: S2 has been announced."), ("B", "B: S3 has started airing.")])
        self.assertEqual([mock.call("Anime news", "A: S2 has been announced."),
                          mock.call("Anime news", "B: S3 has started airing.")], calls)
        many = self.call([(str(i), f"{i}: news") for i in range(5)])
        self.assertEqual(1, len(many))
        self.assertIn("5 shows", many[0].args[1])

    def test_off_means_off(self):
        self.assertEqual([], self.call([("A", "A: news")], enabled=False))


if __name__ == "__main__":
    unittest.main()
