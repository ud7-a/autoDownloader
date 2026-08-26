# Data-Driven Site Registry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collapse the per-site configuration that is currently scattered across four files into one `core/sites.py` registry, so adding a supported site is a single self-contained block instead of coordinated edits in unrelated modules.

**Architecture:** A frozen `Site` dataclass holds everything one site needs — search URL, download click-flow, weekly-schedule page and its scraper JS, and the hostnames worth pinning through DNS. A module-level `SITES` dict keyed by domain holds one `Site` per supported site. The four existing constants (`SUPPORTED_SITES`, `DEFAULT_SITE_FLOWS`, `PATH_HOSTS`, `SCHEDULE_URLS`/`SCHEDULE_MATCH`) become thin derivations of that registry and keep their current names and shapes, so no caller or test has to change. A snapshot fixture captured *before* any code moves proves the refactor changed no behaviour for the two shipped sites.

**Tech Stack:** Python 3.13, stdlib `unittest` (pytest is NOT installed), `dataclasses`, PyQt6 (only as an import side-effect of the modules under test), Selenium (not exercised by these tests).

**Spec:** No separate spec document exists. The requirements are captured in the Goal above and in Global Constraints below; this plan is the contract.

## Global Constraints

- Windows, Python 3.13. Run everything with `py` from the repo root: `C:\Users\Admin\Desktop\pyQt backUp`.
- Tests use **stdlib `unittest` only**. `pytest` is not installed — never write `pytest` test signatures or run `pytest`.
- Full suite: `py -m unittest discover -s tests`. Currently **121 tests, all passing**. The count only ever goes up in this plan.
- Lint gate: `py tools/lint.py` must exit 0. It is pyflakes with this repo's known-benign findings filtered out, so a non-zero exit is a real problem.
- `tests/__init__.py` redirects `AED_APP_DIR` to a temp folder before any app module is imported. Never call `save_config()` in a test, and never remove that redirect.
- Set `QT_QPA_PLATFORM=offscreen` when running anything that imports UI modules.
- **No behaviour change for `witanime.life` or `eta.animerco.org`.** The parity fixture from Task 1 is the gate for this and must pass unmodified at the end of every later task.
- Arabic strings (`الحلقة التالية`, schedule URLs, day names) must be preserved **byte-exact**. Move them by cut-and-paste; never retype them. All files are UTF-8.
- Selector strings contain nested quotes and backslashes (e.g. `'//*[@id=\\"file\\"]/div[3]/div/a'`). Cut-and-paste these too — a re-typed escape is a silent download failure.
- Do not modify `exeCompile.py` (untracked, maintainer-local). `tools/build_release.py` is the build source of truth.
- `utils/` must not import from `core/` — that inverts the layering. Task 6 depends on this.

---

## File Structure

**Created:**

- `core/sites.py` — the registry. Owns the `Site` dataclass, the `SITES` dict, the shared `DOWNLOAD_HOSTS` table, the two schedule scraper scripts, and read accessors. Imports stdlib only (`copy`, `dataclasses`), so every other module can import it without a cycle.
- `tools/snapshot_sites.py` — dumps the live site configuration to the parity fixture. Run deliberately, not on every build.
- `tests/fixtures/sites_snapshot.json` — the frozen "before" picture of all four constants. The refactor's contract.
- `docs/adding-a-site.md` — how to add a site to the registry.

**Modified:**

- `ui/search_tab.py:23-74` — `SUPPORTED_SITES` and `DEFAULT_SITE_FLOWS` become derivations of `core.sites`; the literals move out. `resolve_site_flow` (`:137`) reads the registry.
- `core/selenium_engine.py:212` — `PATH_HOSTS` becomes an alias of `core.sites.DOWNLOAD_HOSTS`. `tab_matches_path` (`:223`) is untouched.
- `core/schedule.py:38,52,71,92,105` — `SCHEDULE_URLS`, `SCHEDULE_MATCH`, `_WITANIME_JS`, `_ANIMERCO_JS` and `_SITE_SCRIPTS` become derivations of the registry.
- `ui/search_tab.py:209` and `core/selenium_engine.py:308,335` — the three `apply_dns_flags` call sites pass the registry's host list.
- `tests/test_logic.py` — new test classes appended; existing tests untouched.

Why one file: these values are read by the search tab, the download engine, the schedule scraper and the browser launcher. Splitting them by consumer is exactly the problem being fixed. They change together — when a site alters its markup, its selectors, schedule page and search URL are revised in one sitting — so they live together.

---

### Task 1: Freeze current behaviour in a parity fixture

Nothing moves yet. This task only builds the safety net that every later task is graded against.

**Files:**
- Create: `tools/snapshot_sites.py`
- Create: `tests/fixtures/sites_snapshot.json` (generated, then committed)
- Modify: `tests/test_logic.py` (append one test class + imports)

**Interfaces:**
- Consumes: nothing.
- Produces: `tests/fixtures/sites_snapshot.json`, read by `SiteRegistryParityTests` in every later task. Its top-level keys are exactly `supported_sites`, `site_flows`, `path_hosts`, `schedule_urls`, `schedule_match`, `dns_hosts`.

- [ ] **Step 1: Write the snapshot generator**

Create `tools/snapshot_sites.py`:

```python
"""Dump the live site configuration to the parity fixture.

The site registry refactor moves these values between modules. This fixture is the
"before" picture: if the registry ever stops producing exactly these values, a real
download flow has changed, not just an import path.

Run deliberately (not part of the build):

    py tools/snapshot_sites.py
"""

import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

# Import app modules only after the data dir is redirected, so generating the
# fixture can never read or write the real config.
os.environ.setdefault("AED_APP_DIR", os.path.join(REPO, "build", "snapshot-appdir"))
os.makedirs(os.environ["AED_APP_DIR"], exist_ok=True)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from ui.search_tab import SUPPORTED_SITES, DEFAULT_SITE_FLOWS       # noqa: E402
from core.selenium_engine import PATH_HOSTS                          # noqa: E402
from core.schedule import SCHEDULE_URLS, SCHEDULE_MATCH              # noqa: E402
from utils.browser_flags import DEFAULT_HOSTS                        # noqa: E402


def main():
    payload = {
        "supported_sites": SUPPORTED_SITES,
        "site_flows": DEFAULT_SITE_FLOWS,
        "path_hosts": {k: list(v) for k, v in PATH_HOSTS.items()},
        "schedule_urls": SCHEDULE_URLS,
        "schedule_match": SCHEDULE_MATCH,
        "dns_hosts": list(DEFAULT_HOSTS),
    }
    dest = os.path.join(REPO, "tests", "fixtures", "sites_snapshot.json")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "w", encoding="utf-8", newline="") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False, sort_keys=True)
    print(f"wrote {dest}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Generate the fixture**

Run: `py tools/snapshot_sites.py`
Expected: prints `wrote ...\tests\fixtures\sites_snapshot.json`

Open the file and confirm the Arabic reads correctly (`الحلقة التالية` under `site_flows`, not `\u0627\u0644...`). `ensure_ascii=False` is what keeps it readable; if you see escapes, the file was written by a different code path — fix before continuing.

- [ ] **Step 3: Write the parity test**

Append to `tests/test_logic.py`:

```python
class SiteRegistryParityTests(unittest.TestCase):
    """The site registry refactor must not change what any supported site does.

    These values drive real downloads: a changed selector or delay silently breaks
    every episode for that site. The fixture is the pre-refactor snapshot; only
    regenerate it when a site's behaviour is deliberately being changed."""

    @classmethod
    def setUpClass(cls):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "fixtures", "sites_snapshot.json")
        with open(path, encoding="utf-8") as f:
            cls.snap = json.load(f)

    def test_search_urls_unchanged(self):
        self.assertEqual(SUPPORTED_SITES, self.snap["supported_sites"])

    def test_site_flows_unchanged(self):
        self.assertEqual(DEFAULT_SITE_FLOWS, self.snap["site_flows"])

    def test_path_hosts_unchanged(self):
        self.assertEqual({k: list(v) for k, v in PATH_HOSTS.items()},
                         self.snap["path_hosts"])

    def test_schedule_config_unchanged(self):
        self.assertEqual(SCHEDULE_URLS, self.snap["schedule_urls"])
        self.assertEqual(SCHEDULE_MATCH, self.snap["schedule_match"])

    def test_dns_hosts_unchanged(self):
        self.assertEqual(list(DEFAULT_HOSTS), self.snap["dns_hosts"])
```

Add `import json` to the stdlib imports at the top of `tests/test_logic.py` (beside `import os`), and extend the existing import block:

```python
from core.schedule import SCHEDULE_URLS, SCHEDULE_MATCH              # noqa: E402
from utils.browser_flags import DEFAULT_HOSTS                        # noqa: E402
```

- [ ] **Step 4: Run the new tests**

Run: `py -m unittest tests.test_logic.SiteRegistryParityTests -v`
Expected: 5 tests, all PASS. They compare the current code against a fixture generated from that same code, so a failure here means the fixture generation is wrong — fix it now, because every later task trusts this.

- [ ] **Step 5: Run the full suite and lint**

Run: `py -m unittest discover -s tests`
Expected: `Ran 126 tests` … `OK`

Run: `py tools/lint.py`
Expected: `lint: clean`

- [ ] **Step 6: Commit**

```bash
git add tools/snapshot_sites.py tests/fixtures/sites_snapshot.json tests/test_logic.py
git commit -m "test: pin current site configuration before the registry refactor"
```

---

### Task 2: Create the registry module

**Files:**
- Create: `core/sites.py`
- Modify: `tests/test_logic.py` (append one test class)

**Interfaces:**
- Consumes: `tests/fixtures/sites_snapshot.json` (Task 1) for its own tests.
- Produces, all imported by Tasks 3-6:
  - `Site` — frozen dataclass, fields `domain: str`, `search_url: str`, `next_btn_xpath: str`, `step_paths: dict`, `schedule_url: str = ""`, `schedule_match: str = ""`, `schedule_js: str = ""`, `dns_hosts: tuple = ()`
  - `SITES: dict[str, Site]` — keyed by domain
  - `DOWNLOAD_HOSTS: dict[str, tuple[str, ...]]` — download path name → allowed hostnames
  - `search_url(domain) -> str` (returns `""` when unknown)
  - `flow(domain) -> tuple[dict, str]` — `(step_paths_deepcopy, next_btn_xpath)`; `({}, "")` when unknown
  - `is_supported(domain) -> bool`
  - `dns_hosts() -> tuple[str, ...]`
  - `schedule_sites() -> dict[str, Site]` — only entries with a `schedule_url`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_logic.py`:

```python
class SiteRegistryTests(unittest.TestCase):
    """The registry is the single definition of a supported site."""

    def test_both_shipped_sites_are_registered(self):
        self.assertIn("witanime.life", sites.SITES)
        self.assertIn("eta.animerco.org", sites.SITES)

    def test_search_url_carries_the_query_placeholder(self):
        for domain in sites.SITES:
            self.assertIn("{query}", sites.search_url(domain), domain)

    def test_flow_returns_steps_and_next_button(self):
        paths, nxt = sites.flow("witanime.life")
        self.assertIn("google drive", paths)
        self.assertEqual(nxt, "الحلقة التالية")

    def test_flow_returns_a_copy_each_time(self):
        """Callers write this into a user profile; mutating it must not poison the
        registry for every later download in the session."""
        first, _ = sites.flow("witanime.life")
        first["google drive"][0]["delay"] = 99.0
        second, _ = sites.flow("witanime.life")
        self.assertNotEqual(second["google drive"][0]["delay"], 99.0)

    def test_unknown_domain_is_empty_not_an_error(self):
        self.assertEqual(sites.search_url("nope.invalid"), "")
        self.assertEqual(sites.flow("nope.invalid"), ({}, ""))
        self.assertFalse(sites.is_supported("nope.invalid"))

    def test_schedule_sites_only_lists_sites_with_a_schedule_page(self):
        for domain, site in sites.schedule_sites().items():
            self.assertTrue(site.schedule_url, domain)
            self.assertIn(site.schedule_match, ("url", "title"), domain)
            self.assertTrue(site.schedule_js.strip(), domain)

    def test_dns_hosts_are_bare_hostnames(self):
        for host in sites.dns_hosts():
            self.assertNotIn("/", host)
            self.assertEqual(host, host.lower())
```

Add to the import block in `tests/test_logic.py`:

```python
from core import sites                                               # noqa: E402
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `py -m unittest tests.test_logic.SiteRegistryTests -v`
Expected: FAIL at import — `ImportError: cannot import name 'sites' from 'core'`

- [ ] **Step 3: Create the registry**

Create `core/sites.py`. Write the scaffolding below, then fill the four data blocks by **cutting and pasting** the existing literals — do not retype any selector or Arabic string:

- `step_paths` for each site ← `DEFAULT_SITE_FLOWS` at `ui/search_tab.py:31-74`
- `search_url` for each site ← `SUPPORTED_SITES` at `ui/search_tab.py:23-26`
- `schedule_url` / `schedule_match` ← `SCHEDULE_URLS` at `core/schedule.py:38-41` and `SCHEDULE_MATCH` at `:52-55`
- `schedule_js` ← `_WITANIME_JS` at `core/schedule.py:71` and `_ANIMERCO_JS` at `:92` (whole `r"""..."""` strings)
- `DOWNLOAD_HOSTS` ← `PATH_HOSTS` at `core/selenium_engine.py:212-221`
- per-site `dns_hosts` ← split `DEFAULT_HOSTS` at `utils/browser_flags.py:31-40`: the site's own hostnames go on that site, and the shared file hosts (mediafire, drive, workupload, mp4upload, yourupload, mega) go in `SHARED_DNS_HOSTS`

```python
"""Everything the app knows about a supported site, in one place.

A site's search URL, download click-flow, weekly-schedule page and the hostnames
worth pinning through DNS used to live in four different modules, so adding a site
meant coordinated edits in files that otherwise have nothing to do with each other,
and it was easy to add three of the four and not notice the fourth until a user hit
it. They are all per-site data and they change together -- when a site reworks its
markup, its selectors and its schedule page get revised in the same sitting.

Imports stdlib only, so every layer of the app can read it without an import cycle.
See docs/adding-a-site.md.
"""

import copy
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Site:
    """One supported site.

    step_paths maps a download host name ("google drive") to the ordered clicks that
    reach the file, each {"xpath": str, "delay": float}. The xpath accepts the smart
    forms parse_smart_xpath understands ("mediafire #last") as well as raw XPath.

    schedule_* are optional: a site with no weekly schedule page simply never
    contributes airing days to the watchlist.
    """
    domain: str
    search_url: str
    next_btn_xpath: str
    step_paths: dict
    schedule_url: str = ""
    schedule_match: str = ""      # "url" | "title" -- see docs/adding-a-site.md
    schedule_js: str = ""
    dns_hosts: tuple = field(default_factory=tuple)


# Hosts every site's downloads can land on, pinned for all sites rather than
# repeated on each one.
SHARED_DNS_HOSTS = (
    # <- paste the file-host entries from utils/browser_flags.py DEFAULT_HOSTS here
)

_WITANIME_SCHEDULE_JS = r"""
"""  # <- paste the whole body of _WITANIME_JS (core/schedule.py:71)

_ANIMERCO_SCHEDULE_JS = r"""
"""  # <- paste the whole body of _ANIMERCO_JS (core/schedule.py:92)

SITES = {
    "witanime.life": Site(
        domain="witanime.life",
        search_url="",             # <- paste from SUPPORTED_SITES
        next_btn_xpath="",         # <- paste from DEFAULT_SITE_FLOWS
        step_paths={},             # <- paste from DEFAULT_SITE_FLOWS
        schedule_url="",           # <- paste from SCHEDULE_URLS
        schedule_match="url",
        schedule_js=_WITANIME_SCHEDULE_JS,
        dns_hosts=("witanime.life", "www.witanime.life"),
    ),
    "eta.animerco.org": Site(
        domain="eta.animerco.org",
        search_url="",             # <- paste from SUPPORTED_SITES
        next_btn_xpath="",         # <- paste from DEFAULT_SITE_FLOWS
        step_paths={},             # <- paste from DEFAULT_SITE_FLOWS
        schedule_url="",           # <- paste from SCHEDULE_URLS
        schedule_match="title",
        schedule_js=_ANIMERCO_SCHEDULE_JS,
        dns_hosts=("eta.animerco.org", "animerco.org", "www.animerco.org"),
    ),
}

# Where each download path is allowed to end up. Shared across sites: "google drive"
# means the same thing everywhere. Consumed by selenium_engine.tab_matches_path,
# which drops a path that lands on an ad interstitial instead of the file.
DOWNLOAD_HOSTS = {
    # <- paste PATH_HOSTS from core/selenium_engine.py:212-221
}


def is_supported(domain):
    return domain in SITES


def search_url(domain):
    site = SITES.get(domain)
    return site.search_url if site else ""


def flow(domain):
    """(step_paths, next_btn_xpath) for `domain`, or ({}, "") if unregistered.

    The step_paths are deep-copied: callers write them straight into a user profile,
    and a mutation there must not rewrite the registry for the rest of the session.
    """
    site = SITES.get(domain)
    if not site:
        return {}, ""
    return copy.deepcopy(site.step_paths), site.next_btn_xpath


def dns_hosts():
    """Every hostname worth pinning, site-specific plus shared file hosts."""
    out = []
    for site in SITES.values():
        out.extend(site.dns_hosts)
    out.extend(SHARED_DNS_HOSTS)
    seen, unique = set(), []
    for host in out:
        if host not in seen:
            seen.add(host)
            unique.append(host)
    return tuple(unique)


def schedule_sites():
    """Only the sites that publish a weekly schedule page."""
    return {d: s for d, s in SITES.items() if s.schedule_url}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `py -m unittest tests.test_logic.SiteRegistryTests -v`
Expected: 7 tests PASS.

If `test_flow_returns_steps_and_next_button` fails on the Arabic comparison, the `next_btn_xpath` was retyped rather than pasted — re-copy it from `ui/search_tab.py`.

- [ ] **Step 5: Prove the registry reproduces the snapshot exactly**

This is the moment the paste-work is verified. Run:

```bash
py -c "import os,sys,json; sys.path.insert(0,'.'); os.environ.setdefault('AED_APP_DIR', os.path.join('build','snapshot-appdir')); os.makedirs(os.environ['AED_APP_DIR'], exist_ok=True); os.environ.setdefault('QT_QPA_PLATFORM','offscreen'); from core import sites; snap=json.load(open('tests/fixtures/sites_snapshot.json',encoding='utf-8')); flows={d:{'next_btn_xpath':s.next_btn_xpath,'step_paths':s.step_paths} for d,s in sites.SITES.items()}; print('flows match :', flows==snap['site_flows']); print('search match:', {d:s.search_url for d,s in sites.SITES.items()}==snap['supported_sites']); print('hosts match :', {k:list(v) for k,v in sites.DOWNLOAD_HOSTS.items()}==snap['path_hosts'])"
```

Expected: three lines, all `True`. Any `False` means a literal was mistyped during the move — diff that block against the original file before continuing. Do not proceed on a `False`.

- [ ] **Step 6: Run the full suite and lint**

Run: `py -m unittest discover -s tests`
Expected: `Ran 133 tests` … `OK` (the old constants still exist and still pass parity; nothing has been rewired yet)

Run: `py tools/lint.py`
Expected: `lint: clean`

- [ ] **Step 7: Commit**

```bash
git add core/sites.py tests/test_logic.py
git commit -m "feat: add core.sites registry describing each supported site"
```

---

### Task 3: Point the search tab at the registry

**Files:**
- Modify: `ui/search_tab.py:23-74` (delete the two literal dicts), `ui/search_tab.py:137` (`resolve_site_flow`)
- Test: `tests/test_logic.py` (existing `SiteRegistryParityTests`, `SiteFlowPrecedenceTests`, `WitanimeTemplateTests` — all must still pass unmodified)

**Interfaces:**
- Consumes: `core.sites.SITES`, `sites.search_url`, `sites.flow`, `sites.is_supported`.
- Produces: `SUPPORTED_SITES` and `DEFAULT_SITE_FLOWS` keep their exact names, shapes and import paths (`from ui.search_tab import SUPPORTED_SITES, DEFAULT_SITE_FLOWS`), so `tests/test_logic.py` and any other caller are unaffected.

- [ ] **Step 1: Confirm the tests that must not change are currently green**

Run: `py -m unittest tests.test_logic.SiteRegistryParityTests tests.test_logic.SiteFlowPrecedenceTests tests.test_logic.WitanimeTemplateTests -v`
Expected: all PASS. These are the behavioural gate for this task; note the count.

- [ ] **Step 2: Replace the literals with derivations**

In `ui/search_tab.py`, delete lines 23-74 (the whole `SUPPORTED_SITES` and `DEFAULT_SITE_FLOWS` literal blocks) and put in their place:

```python
from core import sites

# Kept as module-level names because the downloader, the watchlist and the tests all
# import them from here. The definitions now live in core/sites.py -- see
# docs/adding-a-site.md to add a site.
SUPPORTED_SITES = {domain: site.search_url for domain, site in sites.SITES.items()}

DEFAULT_SITE_FLOWS = {
    domain: {"next_btn_xpath": site.next_btn_xpath, "step_paths": site.step_paths}
    for domain, site in sites.SITES.items()
}
```

Put the `from core import sites` line with the other imports at the top of the file, not mid-module.

- [ ] **Step 3: Point `resolve_site_flow` at the registry**

Replace the built-in branch of `resolve_site_flow` (`ui/search_tab.py:137`) so it asks the registry rather than the derived dict. The rest of the function — the inheritance fallback for unregistered domains — stays exactly as it is:

```python
    if sites.is_supported(domain):
        return sites.flow(domain)
```

This replaces these two lines:

```python
    if domain in DEFAULT_SITE_FLOWS:
        flow = DEFAULT_SITE_FLOWS[domain]
        return copy.deepcopy(flow["step_paths"]), flow.get("next_btn_xpath", "")
```

`sites.flow()` already deep-copies, so the behaviour is identical.

- [ ] **Step 4: Run the gate tests**

Run: `py -m unittest tests.test_logic.SiteRegistryParityTests tests.test_logic.SiteFlowPrecedenceTests tests.test_logic.WitanimeTemplateTests -v`
Expected: same count as Step 1, all PASS.

A failure in `test_site_flows_unchanged` means the derivation produced a different shape — check that each value is a dict with exactly the keys `next_btn_xpath` and `step_paths`.

- [ ] **Step 5: Run the full suite and lint**

Run: `py -m unittest discover -s tests`
Expected: `Ran 133 tests` … `OK`

Run: `py tools/lint.py`
Expected: `lint: clean`. If it reports `'copy' imported but unused` in `search_tab.py`, check whether `copy` is still used elsewhere in the file before removing the import.

- [ ] **Step 6: Commit**

```bash
git add ui/search_tab.py
git commit -m "refactor: derive search tab site config from core.sites"
```

---

### Task 4: Point the download engine at the registry

**Files:**
- Modify: `core/selenium_engine.py:212-221`
- Test: `tests/test_logic.py` (existing `DownloadDestinationTests`, `SiteRegistryParityTests` — must still pass unmodified)

**Interfaces:**
- Consumes: `core.sites.DOWNLOAD_HOSTS`.
- Produces: `PATH_HOSTS` keeps its name and shape; `tab_matches_path` is untouched and keeps its behaviour.

- [ ] **Step 1: Confirm the gate tests are green**

Run: `py -m unittest tests.test_logic.DownloadDestinationTests -v`
Expected: all PASS. Note the count.

- [ ] **Step 2: Replace the literal with the registry table**

In `core/selenium_engine.py`, delete the `PATH_HOSTS` literal at lines 212-221 and replace it with:

```python
# Where each download path is allowed to end up; defined per download host in
# core/sites.py because it is shared across sites. Kept under this name because
# tab_matches_path and the tests both read it.
PATH_HOSTS = sites.DOWNLOAD_HOSTS
```

Add `from core import sites` to the imports at the top of `core/selenium_engine.py`, beside `from core.signals import signals`.

Leave the explanatory comment block above `PATH_HOSTS` (the one describing why the check exists) where it is — it documents `tab_matches_path`, which is not moving.

- [ ] **Step 3: Run the gate tests**

Run: `py -m unittest tests.test_logic.DownloadDestinationTests tests.test_logic.SiteRegistryParityTests -v`
Expected: same counts as before, all PASS.

- [ ] **Step 4: Check for an import cycle**

`core/sites.py` imports stdlib only, so this must not cycle. Prove it:

Run: `py -c "import os; os.environ['QT_QPA_PLATFORM']='offscreen'; import core.selenium_engine; import core.sites; print('no cycle')"`
Expected: `no cycle`

- [ ] **Step 5: Run the full suite and lint**

Run: `py -m unittest discover -s tests`
Expected: `Ran 133 tests` … `OK`

Run: `py tools/lint.py`
Expected: `lint: clean`

- [ ] **Step 6: Commit**

```bash
git add core/selenium_engine.py
git commit -m "refactor: read download host table from core.sites"
```

---

### Task 5: Point the schedule scraper at the registry

**Files:**
- Modify: `core/schedule.py:38-55` (`SCHEDULE_URLS`, `SCHEDULE_MATCH`), `:71-108` (`_WITANIME_JS`, `_ANIMERCO_JS`, `_SITE_SCRIPTS`)
- Test: `tests/test_logic.py` (existing `ScheduleMatchingTests`, `SiteRegistryParityTests` — must still pass unmodified)

**Interfaces:**
- Consumes: `core.sites.schedule_sites()`.
- Produces: `SCHEDULE_URLS`, `SCHEDULE_MATCH` and `_SITE_SCRIPTS` keep their names and shapes. `fetch_schedule(driver, domain, settle=2.5)` at `:136` is untouched.

- [ ] **Step 1: Confirm the gate tests are green**

Run: `py -m unittest tests.test_logic.ScheduleMatchingTests -v`
Expected: all PASS. Note the count.

- [ ] **Step 2: Replace the four literals with derivations**

In `core/schedule.py`, delete the `SCHEDULE_URLS` dict (`:38-41`), the `SCHEDULE_MATCH` dict (`:52-55`), the `_WITANIME_JS` string (`:71-90`), the `_ANIMERCO_JS` string (`:92-103`) and the `_SITE_SCRIPTS` dict (`:105-108`). Replace them with:

```python
from core import sites

# Derived from the site registry; the scraper scripts and schedule pages are defined
# per site in core/sites.py. Names kept because fetch_schedule and load_all read them.
SCHEDULE_URLS = {d: s.schedule_url for d, s in sites.schedule_sites().items()}
SCHEDULE_MATCH = {d: s.schedule_match for d, s in sites.schedule_sites().items()}
_SITE_SCRIPTS = {d: s.schedule_js for d, s in sites.schedule_sites().items()}
```

**Keep the long comment above `SCHEDULE_MATCH`** explaining why witanime matches on URL and animerco on title — that reasoning is not obvious from the values and is easy to get wrong when adding a site. Move it verbatim to sit above the new `SCHEDULE_MATCH` line.

Put `from core import sites` with the imports at the top of the file.

- [ ] **Step 3: Run the gate tests**

Run: `py -m unittest tests.test_logic.ScheduleMatchingTests tests.test_logic.SiteRegistryParityTests -v`
Expected: same counts as before, all PASS.

- [ ] **Step 4: Verify the scraper scripts survived the move intact**

The schedule JS is only exercised against a live browser, so the tests cannot catch a truncated paste. Check it structurally:

```bash
py -c "import os; os.environ['QT_QPA_PLATFORM']='offscreen'; from core import schedule; [print(f'{d}: {len(js)} chars, returns={\"return out\" in js}') for d, js in schedule._SITE_SCRIPTS.items()]"
```

Expected: two lines, each with a non-trivial character count (hundreds, not tens) and `returns=True`. A script that doesn't end in `return out` will silently yield an empty schedule and quietly ungroup the whole watchlist.

- [ ] **Step 5: Run the full suite and lint**

Run: `py -m unittest discover -s tests`
Expected: `Ran 133 tests` … `OK`

Run: `py tools/lint.py`
Expected: `lint: clean`

- [ ] **Step 6: Commit**

```bash
git add core/schedule.py
git commit -m "refactor: derive schedule config from core.sites"
```

---

### Task 6: Feed the DNS pin list from the registry

`utils/` must not import `core/`, so the registry does not reach into `browser_flags`. The call sites pass the host list in instead — `apply_dns_flags` already takes a `hosts` argument.

**Files:**
- Modify: `core/selenium_engine.py:308`, `core/selenium_engine.py:335`, `ui/search_tab.py:209`
- Modify: `utils/browser_flags.py:31-40` (comment only)
- Test: `tests/test_logic.py` (append two tests)

**Interfaces:**
- Consumes: `core.sites.dns_hosts()`.
- Produces: nothing new. `apply_dns_flags(options, hosts=None, only_broken=True)` keeps its signature; `DEFAULT_HOSTS` remains its fallback for callers that pass nothing.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_logic.py`:

```python
class DnsHostCoverageTests(unittest.TestCase):
    """Chrome gets --host-resolver-rules for these hosts because ISP resolvers
    routinely fail to resolve the anime sites. A site missing from this list is a
    site that silently stops loading on those networks."""

    def test_every_registered_site_domain_is_pinned(self):
        pinned = sites.dns_hosts()
        for domain in sites.SITES:
            self.assertIn(domain, pinned, f"{domain} would not be DNS-pinned")

    def test_shared_file_hosts_are_still_covered(self):
        """These are where the episodes actually download from."""
        pinned = sites.dns_hosts()
        for host in ("www.mediafire.com", "drive.google.com"):
            self.assertIn(host, pinned)

    def test_no_duplicate_hosts(self):
        pinned = sites.dns_hosts()
        self.assertEqual(len(pinned), len(set(pinned)))
```

- [ ] **Step 2: Run it to see where it stands**

Run: `py -m unittest tests.test_logic.DnsHostCoverageTests -v`
Expected: PASS if Task 2's paste-work was complete. If `test_shared_file_hosts_are_still_covered` fails, `SHARED_DNS_HOSTS` in `core/sites.py` is missing entries — copy the remaining file hosts from `utils/browser_flags.py:31-40` before continuing.

- [ ] **Step 3: Pass the registry's hosts at all three call sites**

At `core/selenium_engine.py:308` and `core/selenium_engine.py:335`, change:

```python
        apply_dns_flags(options)   # resolve via Google Public DNS, not the machine's
```

to:

```python
        # Hosts come from the site registry so a newly added site is pinned too.
        apply_dns_flags(options, hosts=sites.dns_hosts())
```

At `ui/search_tab.py:209`, change:

```python
    apply_dns_flags(options)
```

to:

```python
    apply_dns_flags(options, hosts=sites.dns_hosts())
```

`sites` is already imported in both modules from Tasks 3 and 4.

- [ ] **Step 4: Update the fallback comment**

In `utils/browser_flags.py`, above `DEFAULT_HOSTS` at line 31, replace the existing comment with:

```python
# Fallback list for callers that pass no hosts. The app's three browsers pass
# core.sites.dns_hosts() instead, so a site added to the registry is pinned without
# touching this file. utils/ deliberately does not import core/, hence the duplication.
```

- [ ] **Step 5: Run the tests**

Run: `py -m unittest tests.test_logic.DnsHostCoverageTests -v`
Expected: 3 tests PASS.

Run: `py -m unittest discover -s tests`
Expected: `Ran 136 tests` … `OK`

Run: `py tools/lint.py`
Expected: `lint: clean`

- [ ] **Step 6: Commit**

```bash
git add core/selenium_engine.py ui/search_tab.py utils/browser_flags.py tests/test_logic.py
git commit -m "refactor: pin DNS for hosts named by the site registry"
```

---

### Task 7: Validate registry entries and document adding a site

The payoff task: a malformed entry fails a test with a useful message instead of failing a user's download, and the procedure is written down.

**Files:**
- Create: `docs/adding-a-site.md`
- Modify: `tests/test_logic.py` (append one test class)

**Interfaces:**
- Consumes: `core.sites.SITES`, `core.sites.DOWNLOAD_HOSTS`.
- Produces: nothing consumed by other tasks. This is the final task.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_logic.py`:

```python
class SiteRegistryValidationTests(unittest.TestCase):
    """A malformed registry entry should fail here, with a message naming the site,
    rather than surfacing as a download that mysteriously does nothing."""

    def test_domain_key_matches_the_entry(self):
        for domain, site in sites.SITES.items():
            self.assertEqual(domain, site.domain,
                             f"key {domain!r} does not match Site.domain {site.domain!r}")

    def test_search_url_has_exactly_one_query_placeholder(self):
        for domain, site in sites.SITES.items():
            self.assertEqual(site.search_url.count("{query}"), 1, domain)
            self.assertTrue(site.search_url.startswith("https://"), domain)

    def test_every_site_has_at_least_one_download_path(self):
        for domain, site in sites.SITES.items():
            self.assertTrue(site.step_paths, f"{domain} has no download paths")
            for name, steps in site.step_paths.items():
                self.assertTrue(steps, f"{domain}: path {name!r} has no steps")

    def test_every_step_has_an_xpath_and_a_numeric_delay(self):
        for domain, site in sites.SITES.items():
            for name, steps in site.step_paths.items():
                for i, step in enumerate(steps):
                    where = f"{domain}/{name}[{i}]"
                    self.assertTrue(step.get("xpath", "").strip(), f"{where}: empty xpath")
                    self.assertIsInstance(float(step.get("delay", 0)), float, where)
                    self.assertGreaterEqual(float(step["delay"]), 0.0, where)

    def test_next_button_is_set(self):
        for domain, site in sites.SITES.items():
            self.assertTrue(site.next_btn_xpath.strip(), f"{domain} has no next button")

    def test_schedule_entries_are_complete_or_absent(self):
        """Half a schedule config is worse than none: the day grouping silently
        degrades instead of failing."""
        for domain, site in sites.SITES.items():
            provided = [bool(site.schedule_url), bool(site.schedule_match),
                        bool(site.schedule_js.strip())]
            self.assertIn(len(set(provided)), (1,),
                          f"{domain}: schedule_url/match/js must be all set or all empty")

    def test_download_paths_have_known_hosts_where_possible(self):
        """Not every path name has to be in DOWNLOAD_HOSTS -- an unmapped one passes
        the destination check rather than being blocked -- but the common ones should
        be, or the ad-interstitial guard does nothing for them."""
        for domain, site in sites.SITES.items():
            for name in site.step_paths:
                low = name.strip().lower()
                if low in ("google drive", "mediafire", "workupload"):
                    self.assertIn(low, sites.DOWNLOAD_HOSTS, f"{domain}: {name}")
```

- [ ] **Step 2: Run it to verify it passes against the real registry**

Run: `py -m unittest tests.test_logic.SiteRegistryValidationTests -v`
Expected: 7 tests PASS.

If `test_schedule_entries_are_complete_or_absent` fails, one site has a schedule URL but no scraper JS (or vice versa) — finish that entry.

- [ ] **Step 3: Prove the validation actually catches a bad entry**

Temporarily break one entry to confirm the tests aren't vacuous. In a Python shell:

```bash
py -c "import os; os.environ['QT_QPA_PLATFORM']='offscreen'; from core import sites; s=sites.SITES['witanime.life']; import dataclasses; bad=dataclasses.replace(s, search_url='https://x/no-placeholder'); print('placeholder count:', bad.search_url.count('{query}'), '-> validation would fail:', bad.search_url.count('{query}')!=1)"
```

Expected: `placeholder count: 0 -> validation would fail: True`

Do not commit any change to the registry from this step — it is a check, not an edit.

- [ ] **Step 4: Write the documentation**

Create `docs/adding-a-site.md`:

````markdown
# Adding a supported site

Everything a site needs lives in one entry in [`core/sites.py`](../core/sites.py).
Nothing else has to change: the search tab, download engine, watchlist schedule and
DNS pinning all read from there.

## What the app expects from a site

The download engine works by **clicking through to a file host**. `step_paths` is a
sequence of clicks that ends on a real file at MediaFire, Google Drive, Workupload or
similar. A site that only streams through an embedded player has nothing for those
steps to click and cannot be added this way.

## The entry

```python
"example.com": Site(
    domain="example.com",
    search_url="https://example.com/?s={query}",   # exactly one {query}
    next_btn_xpath="Next Episode",                 # text or raw XPath
    step_paths={
        "google drive": [
            {"xpath": "google drive #last", "delay": 3.0},
            {"xpath": "Download anyway", "delay": 2.0},
        ],
    },
    dns_hosts=("example.com", "www.example.com"),
),
```

Optional, only if the site publishes a weekly schedule page:

```python
    schedule_url="https://example.com/schedule/",
    schedule_match="url",        # or "title" -- see below
    schedule_js=_EXAMPLE_SCHEDULE_JS,
```

## Writing step_paths

Each step is one click. `xpath` accepts three forms, resolved by
`parse_smart_xpath` in [`core/selenium_engine.py`](../core/selenium_engine.py):

| Form | Meaning |
|---|---|
| `mediafire` | first element whose text contains "mediafire", case-insensitive |
| `mediafire #last` | the **last** such element |
| `mediafire #2` | the 2nd such element |
| `//*[@id="downloadButton"]` | raw XPath, passed through untouched |

`delay` is the seconds to wait after that click, before the next step. Interstitials
and countdown timers are why these are per-step rather than fixed.

Add the path's host to `DOWNLOAD_HOSTS` in the same file. That is what lets the engine
notice a click landed on an ad page instead of the file and move to the next mirror
rather than burning the 35-second interception window.

## schedule_match: url or title

- `"url"` — the schedule page links the same page a search result points at. Match on
  the URL. Use this whenever possible.
- `"title"` — the schedule page only links something else (a season page, say), so the
  title is all there is to compare. Weaker: seasons of one show are hard to tell apart
  by title alone.

Getting this wrong marks every season of a show as airing whenever any one of them is.

## Checking your entry

```bash
py -m unittest tests.test_logic.SiteRegistryValidationTests -v
py -m unittest discover -s tests
py tools/lint.py
```

The validation tests name the offending site in the failure message.

## Changing an existing site's behaviour on purpose

`tests/fixtures/sites_snapshot.json` pins the shipped sites so a refactor cannot
change them by accident. When you *intend* to change a selector or delay, regenerate
it and say so in the commit:

```bash
py tools/snapshot_sites.py
```
````

- [ ] **Step 5: Run the full suite and lint**

Run: `py -m unittest discover -s tests`
Expected: `Ran 143 tests` … `OK`

Run: `py tools/lint.py`
Expected: `lint: clean`

- [ ] **Step 6: Commit**

```bash
git add docs/adding-a-site.md tests/test_logic.py
git commit -m "feat: validate site registry entries and document adding a site"
```

---

## Done when

- `core/sites.py` is the only file holding per-site values; `SUPPORTED_SITES`, `DEFAULT_SITE_FLOWS`, `PATH_HOSTS`, `SCHEDULE_URLS`, `SCHEDULE_MATCH` and `_SITE_SCRIPTS` are all derivations that kept their names.
- `SiteRegistryParityTests` passes against the fixture generated before any code moved, proving witanime and animerco behave identically.
- Adding a site is one `Site(...)` block plus, usually, one `DOWNLOAD_HOSTS` entry.
- Full suite green (143 tests), `py tools/lint.py` clean.
