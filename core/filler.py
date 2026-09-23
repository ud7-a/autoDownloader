"""Which episodes each site marks as filler, and how to leave them out.

Both sites say so in the episode list, in different shapes:

    witanime   <span ...>الحلقة 57</span> <span class="... bg-amber-500 ...">فيلر</span>
    animerco   <li data-number="33"><a ...><span>الحلقة 33 - فلر</span>

so rather than matching either layout, this finds each marker word and takes the
episode number written just before it. Layout changes (classes, tags, spacing) do
not break that; only renaming the word would.

How complete the data is differs a lot, which the UI has to be honest about:
Bleach is marked on witanime as 163 filler episodes (33, 50, 64-108, 128-137,
168-189, 228-266, 311-341, 355 ...), matching the known filler arcs, while
animerco marks only 33 and 50 of the same 366 episodes.
"""

import re

# Distinct words, not substrings of one another: فيلر has a ي that فلر does not.
_MARKERS = ("فيلر", "فلر")
_EPISODE_NUMBER = re.compile(r"الحلقة\s*[:\-]?\s*(\d{1,4})")

# How far back from the marker the episode number may sit. The gap is one closing
# tag plus an opening tag on witanime, and a few characters on animerco; a short
# window keeps the word "filler" in a synopsis from stealing a nearby number.
_LOOKBACK = 220


def parse_filler_episodes(html):
    """Episode numbers the page marks as filler, as a sorted list.

    Never raises: a page that has no markers (or is not an episode list at all)
    simply yields nothing, which the caller reports as "none found".
    """
    found = set()
    if not html:
        return []
    for marker in _MARKERS:
        start = 0
        while True:
            at = html.find(marker, start)
            if at == -1:
                break
            start = at + len(marker)
            # The number immediately before the marker -- the last one in the window.
            window = html[max(0, at - _LOOKBACK):at]
            last = None
            for last in _EPISODE_NUMBER.finditer(window):
                pass
            if last is not None:
                found.add(int(last.group(1)))
    return sorted(found)


def strip_filler(episodes, filler):
    """`episodes` minus anything in `filler`, order preserved."""
    skip = set(filler or ())
    return [n for n in (episodes or []) if n not in skip]


def wants_other_site(url, found):
    """Should the other site be asked as well, given what this one returned?

    Not simply "when this site found nothing": animerco marks 2 of Bleach's 366
    episodes where witanime marks 163, so a non-empty animerco answer is still
    nearly useless. An animerco profile therefore always asks witanime -- one fetch,
    about a second, much better data -- while a witanime profile only asks when it
    found nothing at all, because querying animerco needs a browser (~5 s) and
    rarely adds anything.
    """
    return (not found) or ("witanime" not in (url or ""))


def pick_cross_site_match(title, candidates):
    """The candidate that is certainly the same show, or None.

    Filler numbering belongs to the anime, not to the site, so when one site marks
    nothing (animerco flags 2 of Bleach's 366 episodes where witanime flags 163) the
    other site's list is still correct. Finding the show elsewhere is a title match,
    and it uses the same rule as the schedule's cross-site day lookup: the normalized
    titles must be equal -- no containment -- and the season number must match,
    stated on neither or the same on both.

    The strictness is deliberate. Skipping episodes the user wanted is worse than
    skipping nothing: a wrong match could delete Bleach's filler numbers from a
    different show's download and the user would only notice the gaps afterwards.
    """
    from core.schedule import season_number
    target = _comparable(title)
    if not target:
        return None
    season = season_number(title)
    for candidate in candidates or ():
        other = candidate.get("title") if isinstance(candidate, dict) else candidate
        if _comparable(other) == target and season_number(other) == season:
            return candidate
    return None


def _comparable(title):
    """normalize_title(), plus long romanized vowels collapsed.

    The sites romanize the same name differently -- animerco writes "Naruto:
    Shippuuden" where witanime writes "Naruto Shippuden" -- and a doubled vowel is
    the usual way a long Japanese vowel is spelled out, so "uu" and "u" are the same
    show. Only identical doubled vowels are collapsed; "ou" is left alone, since it
    is a real distinction in names often enough to be worth keeping. This lives here
    rather than in normalize_title() so the schedule's matching is unchanged.
    """
    from core.schedule import normalize_title
    return re.sub(r"([aeiou])\1+", r"\1", normalize_title(title))
