"""The witanime download flow (core/site_flows.py) and moving existing profiles to it
(utils/config.update_profile_flow, the "witanime_template_v2" migration).

The flow is the exported profile in fixtures/witanime_template.json. A profile is
moved only while it still holds a flow the app shipped (fixtures/
witanime_retired_flows.json, from git history) -- compared ignoring stray spaces
and 2 vs 2.0, but not path order: anything else is the user's edit and stays.

Run with: py -m unittest discover -s tests
"""

import copy
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
__import__("tests")

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return json.load(f)


RETIRED = fixture("witanime_retired_flows.json")["flows"]
EXPORT = fixture("witanime_template.json")


def profile(step_paths, **extra):
    """A witanime profile as sites_config.json holds it."""
    p = {"url": "https://witanime.site/watch/one-piece/{x}", "next_btn_xpath": "الحلقة التالية",
         "step_paths": copy.deepcopy(step_paths), "episode_bounds": [1, 1180], "skip_filler": False,
         "filler_episodes": [54, 55], "filler_source": "witanime", "last_episodes": "617"}
    p.update(extra)
    return p


def spaced(step_paths, where="after"):
    """The same paths with spaces typed around xpaths, scripts and names."""
    pad = (lambda s: s + "  ") if where == "after" else (lambda s: " " + s + "\t")
    out = {}
    for name, steps in step_paths.items():
        out[pad(name)] = [{k: (pad(v) if isinstance(v, str) else v) for k, v in s.items()} for s in steps]
    return out


class TemplateTests(unittest.TestCase):
    def setUp(self):
        from core.site_flows import DEFAULT_SITE_FLOWS
        self.flow = DEFAULT_SITE_FLOWS["witanime.site"]

    def test_the_flow_is_exactly_the_exported_profile(self):
        self.assertEqual(list(EXPORT["step_paths"].items()), list(self.flow["step_paths"].items()))
        self.assertEqual(EXPORT["next_btn_xpath"], self.flow["next_btn_xpath"])
        self.assertEqual("FHD - Google", next(iter(self.flow["step_paths"])))   # tried first

    def test_the_google_path_keeps_its_host_check(self):
        """Renamed from "FHD - Google Drive": the engine's check that a click really
        reached Google (and not an ad page) goes by the path's name."""
        from core.selenium_engine import tab_matches_path
        self.assertTrue(tab_matches_path("https://drive.google.com/uc?export=download&id=1", "FHD - Google"))
        self.assertTrue(tab_matches_path("https://drive.usercontent.google.com/download?id=1", "FHD - Google"))
        self.assertFalse(tab_matches_path("https://fast.io/google-drive-alternatives", "FHD - Google"))
        self.assertFalse(tab_matches_path("https://drive.google.com.evil.example/x", "FHD - Google"))
        self.assertTrue(tab_matches_path("https://drive.google.com/x", "FHD - Google Drive"))   # old name too

    def test_new_profiles_get_the_flow(self):
        from ui.search_tab import resolve_site_flow
        paths, nxt = resolve_site_flow("witanime.site")
        self.assertEqual(list(self.flow["step_paths"]), list(paths))
        self.assertEqual(self.flow["next_btn_xpath"], nxt)

    def test_every_retired_flow_is_known_and_the_current_one_is_not_retired(self):
        from core.site_flows import RETIRED_FLOWS, flow_fingerprint
        retired = RETIRED_FLOWS["witanime.site"]
        for commit, old in RETIRED.items():
            self.assertIn(flow_fingerprint(old["step_paths"]), retired, commit)
        self.assertNotIn(flow_fingerprint(self.flow["step_paths"]), retired)


class FingerprintTests(unittest.TestCase):
    def setUp(self):
        from core.site_flows import flow_fingerprint
        self.fp = flow_fingerprint
        self.paths = RETIRED["ba06e51"]["step_paths"]

    def test_what_is_ignored(self):
        base = self.fp(self.paths)
        self.assertEqual(base, self.fp(spaced(self.paths)))
        self.assertEqual(base, self.fp(spaced(self.paths, "around")))
        ints = {n: [dict(s, delay=int(s["delay"])) for s in steps] for n, steps in self.paths.items()}
        self.assertEqual(base, self.fp(ints))

    def test_what_counts(self):
        base = self.fp(self.paths)
        names = list(self.paths)
        swapped = {n: self.paths[n] for n in [names[1], names[0]] + names[2:]}
        self.assertNotEqual(base, self.fp(swapped))                             # order
        self.assertNotEqual(base, self.fp({n.upper(): s for n, s in self.paths.items()}))   # case
        delay = copy.deepcopy(self.paths)
        delay["FHD - Mediafire"][0]["delay"] = 1.5
        self.assertNotEqual(base, self.fp(delay))
        inner = copy.deepcopy(self.paths)
        inner["FHD - Mediafire"][2]["xpath"] = '//*[@id="download Button"]'     # a space inside
        self.assertNotEqual(base, self.fp(inner))
        extra = copy.deepcopy(self.paths)
        extra["FHD - Mediafire"][0]["optional"] = True
        self.assertNotEqual(base, self.fp(extra))
        self.assertNotEqual(base, self.fp({}))


class UpdateProfileTests(unittest.TestCase):
    def setUp(self):
        from core.site_flows import DEFAULT_SITE_FLOWS
        from utils.config import update_profile_flow
        self.update = lambda p: update_profile_flow(p, "witanime.site")
        self.flow = DEFAULT_SITE_FLOWS["witanime.site"]

    def assertCurrent(self, p):
        self.assertEqual(list(self.flow["step_paths"].items()), list(p["step_paths"].items()))
        self.assertIsNot(self.flow["step_paths"], p["step_paths"])               # a copy
        self.assertIsNot(self.flow["step_paths"]["FHD - Google"], p["step_paths"]["FHD - Google"])

    def test_the_last_shipped_flow_is_moved_and_nothing_else_of_the_profile_changes(self):
        p = profile(RETIRED["ba06e51"]["step_paths"])
        before = {k: copy.deepcopy(v) for k, v in p.items() if k != "step_paths"}
        self.assertTrue(self.update(p))
        self.assertCurrent(p)
        self.assertEqual(before, {k: v for k, v in p.items() if k != "step_paths"})

    def test_trailing_and_leading_spaces_in_the_fields(self):
        for where in ("after", "around"):
            p = profile(spaced(RETIRED["ba06e51"]["step_paths"], where))
            self.assertTrue(self.update(p), where)
            self.assertCurrent(p)
            self.assertNotIn(" ", "".join(n[-1] + n[0] for n in p["step_paths"]))

    def test_delays_saved_as_whole_numbers(self):
        ints = {n: [dict(s, delay=int(s["delay"])) for s in steps]
                for n, steps in RETIRED["ba06e51"]["step_paths"].items()}
        p = profile(ints)
        self.assertTrue(self.update(p))
        self.assertCurrent(p)

    def test_older_flows_and_the_paths_migrations_appended(self):
        from utils import config
        for commit in ("cb5273b", "88f84a6"):
            p = profile(RETIRED[commit]["step_paths"])
            with mock.patch.object(config, "sites_data", {"x": p}):
                config._add_witanime_mp4upload()                       # as an old install ran them
                config._add_witanime_wahmi()
            self.assertTrue(self.update(p), commit)
            self.assertCurrent(p)
            alone = profile(RETIRED[commit]["step_paths"])             # without them, too
            self.assertTrue(self.update(alone), commit)

    def test_the_current_flow_is_left_as_it_is(self):
        p = profile(self.flow["step_paths"])
        self.assertFalse(self.update(p))
        self.assertCurrent(p)

    def test_the_current_flow_with_stray_spaces_is_cleaned(self):
        p = profile(spaced(self.flow["step_paths"]), next_btn_xpath="الحلقة التالية ")
        self.assertTrue(self.update(p))
        self.assertCurrent(p)
        self.assertEqual("الحلقة التالية", p["next_btn_xpath"])

    def test_a_next_button_the_user_changed_is_kept(self):
        p = profile(RETIRED["ba06e51"]["step_paths"], next_btn_xpath="//a[@rel='next']")
        self.assertTrue(self.update(p))
        self.assertEqual("//a[@rel='next']", p["next_btn_xpath"])

    def test_edited_profiles_are_left_alone(self):
        old = RETIRED["ba06e51"]["step_paths"]
        names = list(old)
        edits = {
            "a delay": lambda s: s["FHD - Mediafire"][0].update(delay=9.0),
            "an xpath": lambda s: s["FHD - wtsrv"][1].update(xpath="//button[3]"),
            "a deleted path": lambda s: s.pop("FHD - gofile"),
            "an added path": lambda s: s.update({"My host": [{"xpath": "//a", "delay": 1.0}]}),
            "a renamed path": lambda s: s.update({"Mediafire": s.pop("FHD - Mediafire")}),
            "a removed step": lambda s: s["FHD - Workupload"].pop(),
        }
        for what, edit in edits.items():
            steps = copy.deepcopy(old)
            edit(steps)
            p = profile(steps)
            kept = copy.deepcopy(p)
            self.assertFalse(self.update(p), what)
            self.assertEqual(kept, p, what)
        reordered = profile({n: old[n] for n in [names[1], names[0]] + names[2:]})   # Google first by hand
        kept = copy.deepcopy(reordered)
        self.assertFalse(self.update(reordered))
        self.assertEqual(kept, reordered)

    def test_profiles_that_are_not_a_flow(self):
        from utils.config import update_profile_flow
        for p in ({}, {"url": "x"}, {"step_paths": None}, {"step_paths": {}}, {"step_paths": []},
                  {"step_paths": {"FHD - x": "not a list"}}, "not a profile", None):
            before = copy.deepcopy(p)
            self.assertFalse(update_profile_flow(p, "witanime.site"))
            self.assertEqual(before, p)
        self.assertFalse(update_profile_flow(profile(RETIRED["ba06e51"]["step_paths"]), "nowhere.invalid"))
        animerco = profile(RETIRED["ba06e51"]["step_paths"])
        self.assertFalse(update_profile_flow(animerco, "eta.animerco.org"))   # not its site's flow


class MigrationTests(unittest.TestCase):
    """Through load_config, as an update meets a real sites_config.json."""

    def setUp(self):
        import utils.config as cfg
        self.cfg = cfg
        self.tmp = tempfile.mkdtemp(prefix="aed_template_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.file = os.path.join(self.tmp, "sites_config.json")
        for name, value in (("APP_DIR", self.tmp), ("CONFIG_FILE", self.file)):
            p = mock.patch.object(cfg, name, value)
            p.start()
            self.addCleanup(p.stop)
        saved_sites, saved_settings = dict(cfg.sites_data), copy.deepcopy(cfg.app_settings)
        self.addCleanup(lambda: (cfg.sites_data.clear(), cfg.sites_data.update(saved_sites),
                                 cfg.app_settings.clear(), cfg.app_settings.update(saved_settings)))
        cfg.sites_data.clear()

    def write(self, sites, done):
        with open(self.file, "w", encoding="utf-8") as f:
            json.dump({"sites": sites, "settings": {"migrations_done": done}}, f, ensure_ascii=False)

    def saved(self):
        with open(self.file, encoding="utf-8") as f:
            return json.load(f)

    def test_an_update_moves_unedited_profiles_and_records_it(self):
        from core.site_flows import DEFAULT_SITE_FLOWS
        flow = DEFAULT_SITE_FLOWS["witanime.site"]["step_paths"]
        edited = copy.deepcopy(RETIRED["ba06e51"]["step_paths"])
        edited["FHD - Mediafire"][0]["delay"] = 4.0
        sites = {
            "One Piece": profile(spaced(RETIRED["ba06e51"]["step_paths"])),
            "Old install": profile(RETIRED["cb5273b"]["step_paths"]),
            "Edited": profile(edited),
            "Bleach": {"url": "https://eta.animerco.org/episodes/bleach-{x}",
                       "step_paths": copy.deepcopy(RETIRED["ba06e51"]["step_paths"])},
            "Built-in": {"url": "https://witanime.site/watch/x/{x}"},
            "Old host": profile(RETIRED["ba06e51"]["step_paths"], url="https://witanime.life/watch/y/{x}"),
        }
        self.write(sites, ["witanime_mp4upload_v1", "witanime_wahmi_v1"])
        self.cfg.load_config()
        got = self.saved()
        for name in ("One Piece", "Old host"):
            self.assertEqual(list(flow), list(got["sites"][name]["step_paths"]), name)
            self.assertEqual(flow, got["sites"][name]["step_paths"], name)
        self.assertEqual("https://witanime.site/watch/y/{x}", got["sites"]["Old host"]["url"])
        # A profile from before mp4upload and wahmi existed gets them with the rest.
        self.assertEqual(list(flow.items()), list(got["sites"]["Old install"]["step_paths"].items()))
        self.assertEqual(edited, got["sites"]["Edited"]["step_paths"])
        self.assertEqual(RETIRED["ba06e51"]["step_paths"], got["sites"]["Bleach"]["step_paths"])
        self.assertNotIn("step_paths", got["sites"]["Built-in"])
        self.assertIn("witanime_template_v2", got["settings"]["migrations_done"])
        self.assertEqual([54, 55], got["sites"]["One Piece"]["filler_episodes"])   # the rest kept

    def test_it_runs_once(self):
        self.write({"One Piece": profile(RETIRED["ba06e51"]["step_paths"])},
                   ["witanime_mp4upload_v1", "witanime_wahmi_v1", "witanime_template_v2"])
        self.cfg.load_config()
        self.assertEqual(RETIRED["ba06e51"]["step_paths"], self.cfg.sites_data["One Piece"]["step_paths"])

    def test_a_very_old_install_gets_every_step_in_order(self):
        self.write({"Old": profile(RETIRED["cb5273b"]["step_paths"])}, [])
        self.cfg.load_config()
        from core.site_flows import DEFAULT_SITE_FLOWS
        self.assertEqual(list(DEFAULT_SITE_FLOWS["witanime.site"]["step_paths"].items()),
                         list(self.cfg.sites_data["Old"]["step_paths"].items()))
        self.assertEqual(["witanime_mp4upload_v1", "witanime_template_v2", "witanime_wahmi_v1"],
                         sorted(self.saved()["settings"]["migrations_done"]))

    def test_a_fresh_install_counts_it_as_done(self):
        self.cfg.load_config()                                     # no file yet
        self.assertIn("witanime_template_v2", self.saved()["settings"]["migrations_done"])

    def test_a_failing_migration_is_retried_next_launch(self):
        self.write({"One Piece": profile(RETIRED["ba06e51"]["step_paths"])},
                   ["witanime_mp4upload_v1", "witanime_wahmi_v1"])
        with mock.patch.object(self.cfg, "_PROFILE_MIGRATIONS",
                               (("witanime_template_v2", mock.Mock(side_effect=OSError("disk"))),)):
            self.cfg.load_config()
        with open(self.file, encoding="utf-8") as f:
            self.assertNotIn("witanime_template_v2", json.load(f)["settings"]["migrations_done"])


if __name__ == "__main__":
    unittest.main()
