"""Anime news edge cases: what AniList, the cache file and the user can throw at
utils/anime_news.py and ui/news_tab.py beyond the everyday path (tests/test_news.py).
No test reaches AniList.

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
import types
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
__import__("tests")

from tests.test_news import FakeAniList, S1, S2, m, manga  # noqa: E402


def response(payload):
    """A urlopen() result holding `payload` (bytes, or JSON-able)."""
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
    resp = io.BytesIO(body)
    resp.__enter__ = lambda s=resp: s
    resp.__exit__ = lambda *a: False
    return resp


class Base(unittest.TestCase):
    def setUp(self):
        from utils import anime_news
        self.news = anime_news
        self.tmp = tempfile.mkdtemp(prefix="aed_news_edge_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        p = mock.patch.object(anime_news, "FILE", os.path.join(self.tmp, "anime_news.json"))
        p.start()
        self.addCleanup(p.stop)

    def write_file(self, data):
        with open(self.news.FILE, "w", encoding="utf-8") as f:
            f.write(data if isinstance(data, str) else json.dumps(data))


# ------------------------------------------------------------------ network

class PostEdgeTests(Base):
    def busy(self, retry_after=None):
        headers = {"Retry-After": retry_after} if retry_after is not None else {}
        return urllib.error.HTTPError(self.news.API_URL, 429, "Too Many Requests", headers, None)

    def test_the_request_itself(self):
        with mock.patch("urllib.request.urlopen", return_value=response({"data": {"Media": None}})) as u:
            self.assertIsNone(self.news.media("7"))
        req, = u.call_args.args
        self.assertEqual((self.news.API_URL, "POST"), (req.full_url, req.get_method()))
        body = json.loads(req.data.decode("utf-8"))
        self.assertEqual({"id": 7}, body["variables"])              # "7" sent as a number
        self.assertIn("relations", body["query"])
        self.assertEqual(self.news.TIMEOUT, u.call_args.kwargs["timeout"])
        from utils.config import APP_VERSION
        self.assertIn(APP_VERSION, req.get_header("User-agent"))

    def test_busy_twice_gives_up_after_one_wait(self):
        with mock.patch("urllib.request.urlopen", side_effect=[self.busy("1"), self.busy("1")]), \
                mock.patch.object(self.news.time, "sleep") as sleep:
            with self.assertRaisesRegex(self.news.NewsError, "429"):
                self.news.search("x")
        sleep.assert_called_once_with(1)

    def test_the_wait_is_30_s_without_a_hint_and_never_over_a_minute(self):
        for hint, waited in ((None, 30), ("600", 60)):
            with mock.patch("urllib.request.urlopen",
                            side_effect=[self.busy(hint), response({"data": {"Page": {"media": []}}})]), \
                    mock.patch.object(self.news.time, "sleep") as sleep:
                self.news.search("x")
            sleep.assert_called_once_with(waited)

    def test_errors_beside_data_still_use_the_data(self):
        payload = {"errors": [{"message": "a field failed"}], "data": {"Page": {"media": [S1]}}}
        with mock.patch("urllib.request.urlopen", return_value=response(payload)):
            self.assertEqual([S1], self.news.search("x"))

    def test_an_answer_that_isnt_json_is_a_news_error(self):
        with mock.patch("urllib.request.urlopen", return_value=response(b"<html>down</html>")):
            with self.assertRaisesRegex(self.news.NewsError, "couldn't be reached"):
                self.news.search("x")

    def test_empty_answers(self):
        fake = lambda q, v: {}                                      # noqa: E731
        self.assertEqual([], self.news.search("x", fetch=fake))
        self.assertEqual([], self.news.search("x", fetch=lambda q, v: {"Page": None}))
        self.assertIsNone(self.news.media(1, fetch=fake))


# ------------------------------------------------------------------ matching

class MatchingEdgeTests(Base):
    def test_titles_that_cant_be_searched_as_they_are(self):
        st = self.news.search_text
        self.assertEqual("", st(None))
        self.assertEqual("", st("   "))
        self.assertEqual("Season 2", st("Season 2"))                 # all season words: kept
        self.assertEqual("ون بيس", st("ون بيس الموسم 2"))             # Arabic season words go too
        self.assertEqual("re zero", st("Re:Zero"))
        self.assertEqual("one piece", st("ONE PIECE"))               # caps aren't run-together

    def test_no_match(self):
        bm = self.news.best_match
        self.assertIsNone(bm("Show", []))
        self.assertIsNone(bm("Show", [m(1, "Show", fmt="MOVIE"), m(2, "Show", fmt="SPECIAL")]))
        self.assertIsNone(bm("ون بيس", [m(3, "One Piece")]))
        self.assertIsNone(bm(None, [S1]))
        self.assertEqual(0.0, self.news.similarity("", S1))

    def test_synonyms_and_case_and_punctuation_count(self):
        x = m(5, "Kimetsu no Yaiba", english="Demon Slayer: Kimetsu no Yaiba", synonyms=["KnY"])
        self.assertIs(x, self.news.best_match("Demon Slayer", [x]))
        self.assertIs(x, self.news.best_match("KIMETSU NO YAIBA!!", [x]))
        self.assertIs(x, self.news.best_match("kny", [x]))

    def test_equally_close_entries_take_the_earliest(self):
        later = m(2, "Show", start=(2026, 1, 1))
        no_date = m(3, "Show", start=None)
        first = m(1, "Show", start=(2023, 9, 1))
        self.assertIs(first, self.news.best_match("Show", [no_date, later, first]))

    def test_titles_missing_from_an_entry(self):
        bare = {"id": 9, "format": "TV", "title": {}, "synonyms": None}
        self.assertEqual("?", self.news.display_title(bare))
        self.assertEqual("", self.news.other_title(bare))
        self.assertEqual("?", self.news.display_title(None))
        self.assertEqual(0.0, self.news.similarity("Show", bare))
        native = m(4, "Shingeki no Kyojin", english="Attack on Titan")
        native["title"]["native"] = "進撃の巨人"
        self.assertEqual("Shingeki no Kyojin", self.news.other_title(native))
        native["title"]["romaji"] = None
        self.assertEqual("進撃の巨人", self.news.other_title(native))

    def test_candidate_line_with_little_known(self):
        self.assertEqual("", self.news.candidate_line({"startDate": None}))
        one = m(6, "One", fmt="TV_SHORT", start=(2024, 1, 1))
        one["episodes"], one["status"] = 1, "RELEASING"
        self.assertEqual("TV short · 2024 · 1 episode · Airing", self.news.candidate_line(one))


# ------------------------------------------------------------------ newest season

class NewestSeasonEdgeTests(Base):
    def test_films_specials_and_ovas_are_not_seasons(self):
        s = m(1, "Show", relations=[("SEQUEL", {"id": 2, "format": "MOVIE", "status": "FINISHED"}),
                                    ("SEQUEL", {"id": 3, "format": "OVA", "status": "FINISHED"}),
                                    ("SEQUEL", {"id": 4, "format": "SPECIAL", "status": "FINISHED"}),
                                    ("SEQUEL", {"id": 5, "type": "MANGA", "format": "TV", "status": "FINISHED"}),
                                    ("SIDE_STORY", {"id": 6, "format": "TV", "status": "FINISHED"})])
        fake = FakeAniList({1: s})
        newest, _src = self.news.newest_season(1, fake, gap=0)
        self.assertEqual(1, newest["id"])
        self.assertEqual(1, len(fake.calls))

    def test_of_two_sequels_the_earlier_one_comes_next(self):
        a = m(1, "A", relations=[("SEQUEL", {"id": 3, "format": "TV", "status": "FINISHED", "startDate": {"year": 2025}}),
                                 ("SEQUEL", {"id": 2, "format": "TV", "status": "FINISHED", "startDate": {"year": 2024}})])
        fake = FakeAniList({1: a, 2: m(2, "B"), 3: m(3, "C")})
        newest, _src = self.news.newest_season(1, fake, gap=0)
        self.assertEqual([{"id": 1}, {"id": 2}], fake.calls)
        self.assertEqual(2, newest["id"])

    def test_a_very_long_chain_stops_after_max_hops(self):
        chain = {i: m(i, f"S{i}", relations=[("SEQUEL", {"id": i + 1, "format": "TV", "status": "FINISHED"})])
                 for i in range(1, 30)}
        fake = FakeAniList(chain)
        newest, _src = self.news.newest_season(1, fake, gap=0)
        self.assertEqual(1 + self.news.MAX_HOPS, newest["id"])
        self.assertEqual(1 + self.news.MAX_HOPS, len(fake.calls))

    def test_an_airing_sequel_is_fetched_for_its_next_episode(self):
        airing = m(2, "S2", status="RELEASING", next_ep={"episode": 3, "airingAt": 1.9e9})
        s1 = m(1, "S1", relations=[("SEQUEL", {"id": 2, "format": "TV", "status": "RELEASING"})])
        newest, _src = self.news.newest_season(1, FakeAniList({1: s1, 2: airing}), gap=0)
        self.assertEqual(3, newest["nextAiringEpisode"]["episode"])

    def test_a_sequel_anilist_wont_return_is_used_as_listed(self):
        node = {"id": 2, "format": "TV", "status": "FINISHED", "title": {"romaji": "S2"}}
        s1 = m(1, "S1", relations=[("SEQUEL", node)])
        newest, _src = self.news.newest_season(1, FakeAniList({1: s1}), gap=0)
        self.assertEqual("S2", self.news.display_title(newest))

    def test_the_source_comes_from_the_newest_season_that_names_one(self):
        s2 = m(2, "S2", relations=[("SOURCE", manga("FINISHED"))])
        s1 = m(1, "S1", relations=[("SEQUEL", {"id": 2, "format": "TV", "status": "FINISHED"}),
                                   ("SOURCE", manga("RELEASING"))])
        self.assertEqual("FINISHED", self.news.newest_season(1, FakeAniList({1: s1, 2: s2}), gap=0)[1])
        s2_silent = m(2, "S2")
        self.assertEqual("RELEASING", self.news.newest_season(1, FakeAniList({1: s1, 2: s2_silent}), gap=0)[1])
        no_relations = m(7, "Alone")
        no_relations["relations"] = None
        self.assertEqual((7, None), (lambda r: (r[0]["id"], r[1]))(
            self.news.newest_season(7, FakeAniList({7: no_relations}), gap=0)))

    def test_a_manga_adaptation_counts_as_the_source(self):
        s = m(1, "S", relations=[("ADAPTATION", manga("RELEASING"))])
        self.assertEqual("RELEASING", self.news._source_status(s))
        self.assertIsNone(self.news._source_status(m(2, "T", source="MANGA")))


# ------------------------------------------------------------------ wording

class DescribeEdgeTests(Base):
    def test_dates_anilist_holds_wrongly_fall_back(self):
        dt = self.news._date_text
        self.assertEqual("February 2027", dt({"year": 2027, "month": 2, "day": 30}))
        self.assertEqual("2027", dt({"year": 2027, "month": 13, "day": 1}))
        self.assertEqual("2027", dt({"year": 2027, "month": 13}))
        self.assertEqual("", dt({"year": None, "month": 4, "day": 1}))
        self.assertEqual("", dt(None))
        self.assertEqual("1 January 2027", dt({"year": 2027, "month": 1, "day": 1}))   # no leading 0
        s = self.news.describe(m(5, "X", status="NOT_YET_RELEASED", start=(2027, 2, 30)), None)
        self.assertEqual(("X is coming in February 2027.", "February 2027"), (s["headline"], s["when"]))

    def test_airing_without_a_next_episode(self):
        s = self.news.describe(m(4, "X", status="RELEASING", start=None), None)
        self.assertEqual(("X is airing.", "", "Airing now"), (s["headline"], s["when"], s["detail"]))

    def test_cancelled_and_hiatus(self):
        c = self.news.describe(m(4, "X", status="CANCELLED"), "FINISHED")
        self.assertEqual((self.news.PAUSED, "X is cancelled."), (c["kind"], c["headline"]))
        h = self.news.describe(m(4, "X", end=(2024, 1, 1)), "HIATUS")
        self.assertEqual(self.news.WAITING, h["kind"])
        self.assertIn("on hiatus", h["detail"])

    def test_finished_without_an_end_date(self):
        w = self.news.describe(m(4, "X", end=None), "RELEASING")
        self.assertTrue(w["detail"].startswith("X has ended."))
        f = self.news.describe(m(4, "X", end=None), "FINISHED")
        self.assertEqual("The story it adapts has ended too.", f["detail"])

    def test_names_and_links_when_anilist_leaves_them_out(self):
        bare = {"id": 77, "status": "FINISHED", "title": {"romaji": "Romaji Only"}}
        s = self.news.describe(bare, None)
        self.assertEqual("Romaji Only", s["season_title"])
        self.assertEqual("https://anilist.co/anime/77", s["site_url"])
        self.assertEqual(self.news.WAITING, self.news.describe({"id": 1, "title": {}}, None)["kind"])

    def test_what_a_change_is_compared_on(self):
        d = self.news.describe
        ep4 = d(m(3, "S", status="RELEASING", next_ep={"episode": 4, "airingAt": 1.8e9}), None)
        ep5 = d(m(3, "S", status="RELEASING", next_ep={"episode": 5, "airingAt": 1.81e9}), None)
        self.assertEqual(ep4["key"], ep5["key"])                    # episodes aren't news
        oct_ = d(m(3, "S", status="NOT_YET_RELEASED", start=(2027, 10, None)), None)
        nov = d(m(3, "S", status="NOT_YET_RELEASED", start=(2027, 11, None)), None)
        self.assertNotEqual(oct_["key"], nov["key"])                # a date is


class ChangeNoticeEdgeTests(Base):
    def state(self, mid, status="NOT_YET_RELEASED", start=None, source=None, end=None):
        return self.news.describe(m(mid, f"S{mid}", status=status, start=start, end=end), source)

    def test_dates_appearing_moving_and_going(self):
        msg = self.news.change_message
        none = self.state(3)
        year = self.state(3, start=(2027, None, None))
        month = self.state(3, start=(2027, 10, None))
        day = self.state(3, start=(2027, 10, 2))
        later = self.state(3, start=(2027, 11, None))
        self.assertEqual("X: S3 now has a date, 2027.", msg("X", none, year))
        self.assertEqual("X: S3 now has a date, October 2027.", msg("X", year, month))
        self.assertEqual("X: S3 now has a date, 2 October 2027.", msg("X", month, day))
        self.assertEqual("X: S3 moved to November 2027.", msg("X", month, later))
        self.assertIsNone(msg("X", month, none))                     # a date taken back

    def test_announcements_after_other_states(self):
        msg = self.news.change_message
        coming = self.state(4)
        self.assertEqual("X: S4 has been announced.", msg("X", self.state(3, status="HIATUS"), coming))
        self.assertEqual("X: S4 has been announced.", msg("X", self.state(3), coming))   # another season
        self.assertEqual("X: S4 has been announced.",
                         msg("X", self.state(3, status="FINISHED", source="FINISHED"), coming))

    def test_not_news(self):
        msg = self.news.change_message
        unknown = {"kind": self.news.UNKNOWN, "key": self.news.UNKNOWN}
        self.assertIsNone(msg("X", unknown, self.state(4)))          # just found on AniList
        self.assertIsNone(msg("X", self.state(3, status="RELEASING"), self.state(4, status="RELEASING")))
        self.assertIsNone(msg("X", self.state(3), None))
        self.assertIsNone(msg("X", {}, self.state(3)))
        self.assertIsNone(msg("X", self.state(3, status="RELEASING"), self.state(3, status="HIATUS")))


# ------------------------------------------------------------------ checking and the file

class CheckEdgeTests(Base):
    def fake(self, **kw):
        return FakeAniList({1: S1, 2: S2}, {"show": [S1]}, **kw)

    def test_entries_without_a_link_are_skipped_and_without_a_title_still_checked(self):
        entries = [{"title": "No link"}, {"url": ""}, {"url": "u1"}]
        shows, _c, errors = self.news.check(entries, fetch=lambda q, v: {"Page": {"media": []}}, gap=0, now=1000)
        self.assertEqual(["u1"], list(shows))
        self.assertEqual(self.news.UNKNOWN, shows["u1"]["state"]["kind"])
        self.assertEqual([], errors)

    def test_a_failed_title_is_named_in_the_errors(self):
        _s, _c, errors = self.news.check([{"url": "u1"}], fetch=self.fake(fail={"anime"}), gap=0, now=1000)
        self.assertEqual("Anime", errors[0][0])

    def test_progress_and_the_last_full_check(self):
        seen = []
        entries = [{"url": "u1", "title": "Show"}, {"url": "u2", "title": "Show"}]
        self.news.check(entries, fetch=self.fake(), gap=0, now=1000, progress=lambda i, n: seen.append((i, n)))
        self.assertEqual([(1, 2), (2, 2)], seen)
        self.assertEqual(1000, self.news.load()["checked_at"])
        self.news.check(entries, fetch=self.fake(fail={1}), gap=0, force=True, now=2000)
        self.assertEqual(1000, self.news.load()["checked_at"])       # not a full check

    def test_a_pick_made_while_a_check_runs_is_kept(self):
        def fetch(query, variables):
            if "search" in variables:                                # the user picks meanwhile
                self.news.set_match("u1", 2, "Show 2nd Season")
            return self.fake()(query, variables)
        shows, _c, _e = self.news.check([{"url": "u1", "title": "Show"}], fetch=fetch, gap=0, now=1000)
        stored = self.news.load()["shows"]["u1"]
        self.assertEqual((2, True, None), (stored["anilist_id"], stored["manual"], stored["state"]))

    def test_a_new_pick_replaces_the_old_match_and_is_due_at_once(self):
        self.news.check([{"url": "u1", "title": "Show"}], fetch=self.fake(), gap=0, now=1000)
        self.news.set_match("u1", 2, "Show 2nd Season")
        rec = self.news.load()["shows"]["u1"]
        self.assertEqual((2, True, None, 0), (rec["anilist_id"], rec["manual"], rec["state"], rec["checked_at"]))
        fake = self.fake()
        self.news.check([{"url": "u1", "title": "Show"}], fetch=fake, gap=0, now=1001)
        self.assertEqual([{"id": 2}], fake.calls)                    # no search, straight to it

    def test_non_ascii_titles_survive_the_file(self):
        title = "ون بيس — 進撃の巨人"
        self.news.check([{"url": "u1", "title": title}], fetch=lambda q, v: {"Page": {"media": []}}, gap=0, now=1000)
        with open(self.news.FILE, encoding="utf-8") as f:
            self.assertIn("“Not the right anime?”", f.read())          # stored as text, not \\u escapes
        self.assertEqual(self.news.UNKNOWN, self.news.load()["shows"]["u1"]["state"]["kind"])
        self.assertFalse(os.path.exists(self.news.FILE + ".tmp"))

    def test_damaged_files_and_records(self):
        for text in ("", "[]", "null", '{"shows": []}', '{"shows": "x"}', "{broken"):
            self.write_file(text)
            self.assertEqual({"shows": {}, "checked_at": 0}, self.news.load(), text)
        self.write_file({"shows": {"a": "garbage", "b": {"state": "bad", "checked_at": "soon"},
                                   "c": {"state": {"kind": "airing"}, "checked_at": "123"}}})
        shows = self.news.load()["shows"]
        self.assertEqual(["b", "c"], sorted(shows))
        self.assertEqual((None, 0.0), (shows["b"]["state"], shows["b"]["checked_at"]))
        self.assertEqual(123.0, shows["c"]["checked_at"])
        out, _c, _e = self.news.check([{"url": "a", "title": "Show"}, {"url": "b", "title": "Show"}],
                                      fetch=self.fake(), gap=0, now=1000)
        self.assertEqual({self.news.COMING}, {out["a"]["state"]["kind"], out["b"]["state"]["kind"]})

    def test_fresh_marks(self):
        fresh = self.news.is_fresh
        self.assertFalse(fresh(None))
        self.assertFalse(fresh({}))
        self.assertFalse(fresh({"changed_at": "yesterday"}))
        self.assertFalse(fresh({"changed_at": 5000}, now=1000))      # a clock set back
        self.assertTrue(fresh({"changed_at": 1000}, now=1000))
        self.assertFalse(fresh({"changed_at": 1000}, now=1000 + 7 * 24 * 3600))

    def test_the_same_link_twice_is_harmless(self):
        entries = [{"url": "u1", "title": "Show"}, {"url": "u1", "title": "Show"}]
        shows, changes, errors = self.news.check(entries, fetch=self.fake(), gap=0, now=1000)
        self.assertEqual((["u1"], [], []), (list(shows), changes, errors))


# ------------------------------------------------------------------ the tab

class TabBase(Base):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def state(self, kind, headline="h", when="", site="https://anilist.co/anime/1"):
        return {"kind": kind, "key": kind, "headline": headline, "detail": "", "when": when,
                "season_title": "S", "site_url": site, "anilist_id": 1}

    def shows(self, entries, records):
        self.write_file({"shows": records, "checked_at": time.time()})
        p = mock.patch("ui.news_tab.library_shows", return_value=entries)
        p.start()
        self.addCleanup(p.stop)
        from ui.news_tab import NewsWidget
        w = NewsWidget()
        self.addCleanup(w.deleteLater)
        w.refresh()
        return w


class NewsTabEdgeTests(TabBase):
    def test_library_shows_needs_a_link_and_a_title(self):
        from ui import news_tab
        with mock.patch.object(news_tab.wl, "entries", return_value=[
                {"url": "a", "title": "A"}, {"url": "", "title": "B"}, {"url": "c"}, {"title": "D"}]):
            self.assertEqual([{"url": "a", "title": "A"}], news_tab.library_shows())

    def test_checked_text_across_a_new_year(self):
        from ui.news_tab import checked_text
        now = time.mktime(datetime.datetime(2027, 1, 1, 0, 30).timetuple())
        then = time.mktime(datetime.datetime(2026, 12, 31, 23, 50).timetuple())
        self.assertEqual("checked 31 Dec at 23:50", checked_text(then, now))
        self.assertEqual("not checked yet", checked_text(None, now))

    def test_an_empty_library(self):
        w = self.shows([], {})
        self.assertEqual("Nothing in your Library yet", w.lead.text())
        self.assertEqual([], w._rows)
        self.assertIn("Add anime to your Library", w.empty.text())
        self.assertEqual("All  0", w.seg.widget("all").text())
        self.assertFalse(w.check())                                  # nothing to ask about

    def test_a_show_not_checked_yet(self):
        w = self.shows([{"url": "u1", "title": "One"}], {})
        row = w._rows[0]
        texts = [lbl.text() for lbl in row.findChildren(type(w.lead))]
        self.assertIn("Not checked yet", texts)
        self.assertIn("Waiting for the first check.", texts)
        self.assertEqual("1 show from your Library · not checked yet", w.lead.text())

    def test_order_filters_and_what_each_row_shows(self):
        from utils import anime_news as n
        entries = [{"url": u, "title": t} for u, t in (("f", "Finished one"), ("u", "Unknown one"),
                                                        ("p", "Paused one"), ("w", "Waiting one"),
                                                        ("c2", "b Coming"), ("c1", "A Coming"), ("a", "Airing one"))]
        recs = {"f": {"state": self.state(n.FINISHED)}, "u": {"state": self.state(n.UNKNOWN, site="")},
                "p": {"state": self.state(n.PAUSED)}, "w": {"state": self.state(n.WAITING)},
                "c2": {"state": self.state(n.COMING, when="2027")},
                "c1": {"state": self.state(n.COMING, when="No date yet"), "changed_at": time.time()},
                "a": {"state": self.state(n.AIRING, when="Sat 7 Nov")}}
        w = self.shows(entries, recs)
        self.assertEqual(["a", "c1", "c2", "w", "p", "f", "u"], [r.url for r in w._rows])
        counts = {k: w.seg.widget(k).text() for k in ("all", "airing", "coming", "waiting", "finished")}
        self.assertEqual({"all": "All  7", "airing": "Airing  1", "coming": "Coming  2",
                          "waiting": "Waiting  2", "finished": "Finished  1"}, counts)
        w._switch("waiting")
        self.assertEqual(["w", "p"], [r.url for r in w._rows])       # on hold counts as waiting
        w._switch("all")
        rows = {r.url: r for r in w._rows}
        label_texts = lambda r: [lbl.text() for lbl in r.findChildren(type(w.lead))]  # noqa: E731
        self.assertIn("New since last check", label_texts(rows["c1"]))
        self.assertNotIn("New since last check", label_texts(rows["c2"]))
        self.assertIn("Sat 7 Nov", label_texts(rows["a"]))
        from qfluentwidgets import PushButton
        site_button = lambda r: next(b for b in r.findChildren(PushButton) if b.text() == "AniList")  # noqa: E731
        self.assertFalse(site_button(rows["u"]).isEnabled())         # nothing to open
        self.assertTrue(site_button(rows["a"]).isEnabled())

    def test_filters_with_nothing_in_them(self):
        w = self.shows([{"url": "u1", "title": "One"}], {"u1": {"state": self.state("airing")}})
        w._switch("finished")
        self.assertEqual("No shows here right now.", w.empty.text())
        self.assertTrue(w.empty.isVisibleTo(w))


class FakeCheckThread:
    """Stands in for CheckThread: start() answers at once."""
    made = []
    answer = ([], [])

    def __init__(self, entries, force):
        self.entries, self.force = entries, force
        self._done, self._finished = [], []
        self.done = types.SimpleNamespace(connect=self._done.append)
        self.finished = types.SimpleNamespace(connect=self._finished.append)
        FakeCheckThread.made.append(self)

    def start(self):
        for cb in self._done:
            cb(*self.answer)
        for cb in self._finished:
            cb()

    def isRunning(self):
        return False

    def requestInterruption(self):
        pass


class NewsTabCheckTests(TabBase):
    def setUp(self):
        super().setUp()
        FakeCheckThread.made = []
        FakeCheckThread.answer = ([], [])
        p = mock.patch("ui.news_tab.CheckThread", FakeCheckThread)
        p.start()
        self.addCleanup(p.stop)
        self.entries = [{"url": "u1", "title": "One", "domain": "witanime.site"},
                        {"url": "u2", "title": "Two"}]
        self.w = self.shows(self.entries, {})
        self.bars = mock.patch("ui.news_tab.InfoBar")
        self.infobar = self.bars.start()
        self.addCleanup(self.bars.stop)

    def test_check_now_asks_about_every_show_and_says_how_it_went(self):
        self.w.btn_check.click()
        t, = FakeCheckThread.made
        self.assertEqual((self.entries, True), (t.entries, t.force))
        self.infobar.info.assert_called_once()
        self.assertTrue(self.w.btn_check.isEnabled())
        self.assertFalse(self.w.spinner.isVisibleTo(self.w))

    def test_check_now_reports_errors_then_changes(self):
        FakeCheckThread.answer = ([("One", "One: S2 has been announced.")], [("Two", "AniList answered HTTP 500")])
        got = []
        self.w.changes_found.connect(got.append)
        self.w.check(force=True, manual=True)
        self.infobar.warning.assert_called_once()
        self.assertIn("1 couldn't be checked: AniList answered HTTP 500", self.infobar.warning.call_args.args[1])
        self.assertEqual([[("One", "One: S2 has been announced.")]], got)
        FakeCheckThread.answer = ([("One", "One: S2 has been announced.")], [])
        self.w.check(force=True, manual=True)
        self.infobar.success.assert_called_once()

    def test_a_background_check_is_quiet_but_reports_changes(self):
        FakeCheckThread.answer = ([("One", "news")], [("Two", "error")])
        got = []
        self.w.changes_found.connect(got.append)
        self.assertTrue(self.w.check())
        self.assertFalse(FakeCheckThread.made[0].force)              # only shows that are due
        self.assertEqual([[("One", "news")]], got)
        self.infobar.warning.assert_not_called()
        self.infobar.info.assert_not_called()

    def test_one_check_at_a_time(self):
        class Running(FakeCheckThread):
            def __init__(self):
                self.requestInterruption = mock.Mock()

            def isRunning(self):
                return True
        running = Running()
        self.w._threads.append(running)
        self.assertFalse(self.w.check(force=True, manual=True))
        self.assertEqual([], FakeCheckThread.made)
        self.assertEqual([running], self.w.stop_threads())
        running.requestInterruption.assert_called_once()

    def test_check_this_show_asks_about_that_one_only(self):
        self.w.recheck("u2")
        t, = FakeCheckThread.made
        self.assertEqual(([self.entries[1]], True), (t.entries, t.force))
        self.w.recheck("not in the library")
        self.assertEqual(1, len(FakeCheckThread.made))               # nothing to check: no thread

    def test_find_in_search(self):
        got = []
        self.w.search_requested.connect(lambda q, d: got.append((q, d)))
        self.w.find_in_search("u1")
        self.w.find_in_search("u2")                                  # from the download folder
        self.w.find_in_search("gone")
        self.assertEqual([("one", "witanime.site"), ("two", "")], got)

    def picker(self, picked=None, accepted=True):
        dialog = mock.Mock()
        dialog.return_value.exec.return_value = accepted
        dialog.return_value.picked = picked
        return mock.patch("ui.news_tab.MatchDialog", dialog), dialog

    def test_a_pick_is_saved_and_checked_at_once(self):
        self.news.set_match("u1", 9, "Old pick")
        patch, dialog = self.picker(picked=m(2, "Show 2nd Season"))
        with patch:
            self.w.pick_match("u1")
        args = dialog.call_args.args
        self.assertEqual(("One", 9), args[:2])                       # title, the current pick
        rec = self.news.load()["shows"]["u1"]
        self.assertEqual((2, True, "Show 2nd Season"), (rec["anilist_id"], rec["manual"], rec["match_title"]))
        t, = FakeCheckThread.made
        self.assertEqual(([self.entries[0]], True), (t.entries, t.force))

    def test_cancelling_the_pick_changes_nothing(self):
        for picked, accepted in ((m(2, "X"), False), (None, True)):
            patch, _d = self.picker(picked=picked, accepted=accepted)
            with patch:
                self.w.pick_match("u1")
        self.assertNotIn("u1", self.news.load()["shows"])
        self.assertEqual([], FakeCheckThread.made)

    def test_a_pick_for_a_show_no_longer_in_the_library(self):
        patch, dialog = self.picker(picked=m(2, "X"))
        with patch:
            self.w.pick_match("gone")
        dialog.assert_not_called()


class CheckThreadTests(TabBase):
    def test_a_crash_inside_the_check_is_reported_not_raised(self):
        from ui.news_tab import CheckThread
        t = CheckThread([{"url": "u1", "title": "One"}], force=True)
        got = []
        t.done.connect(lambda c, e: got.append((c, e)))
        with mock.patch.object(self.news, "check", side_effect=KeyError("boom")):
            t.run()                                                  # in this thread
        self.assertEqual([([], [("AniList", "KeyError: 'boom'")])], got)

    def test_it_stops_when_the_app_closes(self):
        from ui.news_tab import CheckThread
        t = CheckThread([{"url": "u1", "title": "One"}], force=False)
        with mock.patch.object(self.news, "check", return_value=({}, [], [])) as check:
            t.run()
        self.assertEqual(t.isInterruptionRequested, check.call_args.kwargs["stop"])


class PickDialogEdgeTests(TabBase):
    def dialog(self, found, title="FateZero", current_id=None, error=None):
        """A pick dialog that has searched (never closed: see tests/test_news.py)."""
        from ui.news_tab import MatchDialog
        from PyQt6.QtWidgets import QWidget
        host = QWidget()
        self.addCleanup(host.deleteLater)
        kw = {"side_effect": self.news.NewsError(error)} if error else {"return_value": found}
        with mock.patch.object(self.news, "search", **kw):
            dlg = MatchDialog(title, current_id, host)
            for t in list(dlg._threads):
                t.wait(5000)
            self._app.processEvents()
        return dlg

    def test_a_title_with_nothing_to_search_starts_empty(self):
        dlg = self.dialog([], title="")
        self.assertEqual([], dlg._threads)
        self.assertEqual([], dlg._cards)
        self.assertEqual("", dlg.status.text())

    def test_only_films_are_still_shown(self):
        dlg = self.dialog([m(1, "Fate Film", fmt="MOVIE"), m(2, "Fate Special", fmt="SPECIAL")])
        self.assertEqual(2, len(dlg._cards))

    def test_no_connection(self):
        dlg = self.dialog(None, error="AniList couldn't be reached (URLError)")
        self.assertEqual([], dlg._cards)
        self.assertIn("press Enter to try again", dlg.status.text())
        self.assertFalse(dlg.yesButton.isEnabled())

    def test_covers_land_on_their_own_card_after_ranking(self):
        found = [m(1, "Something Else"), m(2, "Fate/Zero")]
        dlg = self.dialog(found)
        self.assertEqual([2, 1], [c.media["id"] for c in dlg._cards])
        with mock.patch.object(type(dlg._cards[0]), "set_cover") as set_cover:
            dlg._cover(dlg._generation, 1, b"img")                   # Fate/Zero's, AniList's 2nd
            dlg._cover(dlg._generation - 1, 0, b"old")               # an older search's
            dlg._cover(dlg._generation, 5, b"none")                  # no such result
        self.assertEqual(1, set_cover.call_count)
        from PyQt6.QtGui import QColor, QImage
        img = QImage(10, 10, QImage.Format.Format_RGB32)
        img.fill(QColor("red"))
        from PyQt6.QtCore import QBuffer, QByteArray
        data = QByteArray()
        buf = QBuffer(data)
        buf.open(QBuffer.OpenModeFlag.WriteOnly)
        img.save(buf, "PNG")
        card = dlg._cards[0]
        card.set_cover(bytes(data))
        self.assertFalse(card.cover.pixmap().isNull())
        card.set_cover(b"not an image")                              # kept as it was
        self.assertFalse(card.cover.pixmap().isNull())

    def test_typing_waits_then_searches(self):
        dlg = self.dialog([m(2, "Fate/Zero")])
        with mock.patch.object(dlg, "_search") as search:
            dlg.search_box.setText("fate stay")
            dlg.search_box.textEdited.emit("fate stay")
            self.assertTrue(dlg._typing.isActive())
            search.assert_not_called()
            dlg._typing.timeout.emit()
        search.assert_called_once_with("fate stay")
        with mock.patch.object(dlg, "_search") as search:
            dlg.search_box.setText("   ")
            dlg.btn_search.click()
        search.assert_called_once_with("   ")
        dlg._search("   ")                                           # blank: nothing happens
        self.assertTrue(all(not t.isRunning() for t in dlg._threads))

    def test_double_click_picks_and_closes(self):
        dlg = self.dialog([m(2, "Fate/Zero"), m(3, "Fate/Zero Season 2")])
        with mock.patch.object(dlg, "accept") as accept:
            dlg._cards[1].chosen.emit()
        accept.assert_called_once()
        self.assertEqual(3, dlg.picked["id"])
        self.assertTrue(dlg._cards[1].selected)

    def test_the_current_pick_is_marked_only_on_its_card(self):
        dlg = self.dialog([m(2, "Fate/Zero"), m(3, "Fate/Zero Season 2")], current_id=3)
        marked = [any(lbl.text() == "Current match" for lbl in c.findChildren(type(dlg.status)))
                  for c in dlg._cards]
        self.assertEqual([False, True], marked)

    def test_a_new_search_clears_the_pick(self):
        dlg = self.dialog([m(2, "Fate/Zero")])
        dlg._pick(dlg._cards[0])
        dlg._show(dlg._generation, [m(4, "Other")], "")
        self.assertIsNone(dlg.picked)
        self.assertFalse(dlg.yesButton.isEnabled())


class NewsTimersTests(unittest.TestCase):
    def test_background_checks_are_spaced(self):
        from ui.app_window import AppWindow
        from utils import anime_news as n
        self.assertEqual(20 * 1000, AppWindow.NEWS_CHECK_DELAY_MS)
        self.assertEqual(6 * 3600 * 1000, AppWindow.NEWS_CHECK_EVERY_MS)
        self.assertLess(AppWindow.NEWS_CHECK_EVERY_MS / 1000, n.MAX_AGE)   # a day is never skipped
        keys = [k for k, _i, _t in AppWindow.DEFERRED_TABS]
        self.assertEqual(keys.index("watch_later_interface") + 1, keys.index("news_interface"))

    def test_exactly_three_changes_are_three_notices(self):
        from ui.app_window import AppWindow
        from utils.config import app_settings
        window = types.SimpleNamespace(notifier=mock.Mock())
        with mock.patch.dict(app_settings, {"news_notify": True}):
            AppWindow._on_news_changes(window, [(str(i), f"{i}") for i in range(3)])
            AppWindow._on_news_changes(window, [])
        self.assertEqual(3, window.notifier.notify.call_count)


if __name__ == "__main__":
    unittest.main()
