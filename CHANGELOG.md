# Changelog

What changed in each release. The section for a version is what appears on its
GitHub release page.

Add new entries under **Unreleased**. Under each `###` heading, an
`<!-- app: ... -->` line is the short version the app's "What's new" window shows
(hidden on the release page). The manually dispatched Release workflow
stamps that section with the release version. For a manual tag release, run
`python tools/release_notes.py --stamp X.Y.Z` before committing and tagging.
See [the release guide](docs/RELEASING.md).

## Unreleased

### ✨ New: What's new after an update
<!-- app: After each update, this window shows what's new. To see it again, use Check for updates. -->
- The first launch after an update shows what changed, one page per new
  feature: **Next**, the dots or the arrow keys move through them, and a button
  on a page opens the tab it's about. Skipped several updates? It covers them
  all, up to the last three.
- To see it again, use **Check for updates**: when you're up to date, the notice
  has a **What's new** button.

### 🔁 witanime: Google Drive first
<!-- app: witanime downloads now try Google Drive first. Your profiles switch over by themselves, unless you changed them. -->
- New witanime profiles try **Google Drive first**, then Mediafire, wtsrv,
  Workupload, mp4upload, gofile and wahmi. The Google path is now called
  **"FHD - Google"**.
- Existing witanime profiles move to this order by themselves on the first launch
  after the update, if they still hold a flow the app shipped. Profiles you
  edited (a delay, an xpath, the order, a path added or removed) are left as they
  are. Spaces typed by mistake at the start or end of an xpath field don't count
  as an edit, and are cleaned up. This happens once: a profile imported after
  that keeps exactly what its file holds.

### 📰 New: Anime news tab
<!-- app: A new tab shows what's next for every anime in your Library: airing now, a new season coming, or finished. Search it to follow any other anime too. -->
- A new **Anime news** tab says what's next for every anime in your Library:
  **Airing now** (with the next episode's day), **New season coming** (with its
  start date when there is one), **Waiting for news** (the anime has ended but
  the manga or novel it adapts hasn't), or **Finished**.
- **Follow any anime**, even one that isn't in your Library: search for it at
  the top of the tab and pick it from AniList's results. It gets its news and
  notifications like the rest, marked **Following**; **Stop following** is in
  its **⋯** menu. Once it airs, **Find to download** searches the anime sites
  for it.
- The news comes from AniList, needs no account, and is checked once a day in
  the background. **Check now** asks straight away.
- A wrong match is fixed from the **⋯** menu: **Not the right anime?** lists
  AniList's entries to pick from, and the show keeps that pick.
- A Windows notification says when a season is announced, gets a date or starts
  airing; clicking it opens the tab on **New**, the news of the last 7 days. It
  can be turned off at the bottom of the tab. Airing shows are listed by their
  next episode, soonest first.
- **Finished** means the anime and its source have both ended. AniList can't
  tell whether the whole story was animated.

## 5.1.0 — 2026-10-30

### ⏭️ New: skip openings and endings
<!-- app: The app finds each episode's opening and ending. Press K in the player to skip them. -->
- Once an anime has **three or more episodes** downloaded, the app finds each
  episode's opening and ending by itself, from the audio: the songs repeat from
  one episode to the next, wherever the episode puts them. It runs in the
  background after each download (about a second per episode), works offline
  and needs nothing extra installed.
- In mpv.net, **"Skip opening ▸ K"** shows for 10 seconds; **K** jumps to the
  end of the opening any time until it ends. Outside it, K does nothing.
  Endings skip to the end of the song, so a scene after the credits still plays.
- Video Player tab: openings and endings can each be **Off**, **Ask** (default
  for openings) or **Skip automatically** (K within 5 seconds goes back).
  Endings start Off.
- Library: the **⋯** menu shows how many openings were found and can look again.

### 🧹 New: free up space
<!-- app: Watched episodes can be deleted for you to save space. Turn it on in the Library. -->
- Library → **Free up space**: delete episodes after you watch them, keeping the
  last 1, 2, 3 or 5. Off until you choose a number. Deleted episodes go to the
  **Recycle Bin** or are **deleted permanently**, your choice.
- Only episodes you watched to the end in mpv.net are deleted on their own --
  never ones ticked by hand or skipped over, never the one you're on, and always
  at least 3 per anime. "Download missing" doesn't bring them back.
- In an anime's details: **Keep every episode** for the shows you rewatch, and
  **Delete watched** to clear one anime now (it asks first).

### 🎬 Video Player
<!-- app: Shortcuts work on any keyboard language, playback is smoother, and both picture profiles look better. -->
- **Shortcuts work on Arabic, Russian and Hebrew keyboard layouts**: with one
  of those active, mpv.net ignored letter shortcuts (K, P, Ctrl+S...). They now
  do the same as on an English layout. Installed automatically.
- **Check for updates** for mpv.net: the button now also looks for a newer
  mpv.net release and offers **Update** (through winget). If winget doesn't
  have the new version yet, or mpv.net wasn't installed with it, you get a link
  to the download page instead.
- **Smoother playback on high-refresh screens**: every frame is now shown for
  the same length of time (no uneven 12/13-refresh stepping on a 300 Hz
  monitor), without inventing in-between frames. If a profile is too heavy for
  your GPU and frames start dropping, the player switches that profile to the
  old timing by itself (a message says so), and back for lighter profiles. F12
  switches by hand.
- **Better series and movie profile (F2)**: natural skin, foliage and faces
  instead of the over-sharpened "oil painting" look, with detail at the
  original's level and about a quarter less halo. Also lighter on the GPU.
- **Best-quality anime profile (F1)**: Anime4K's largest models, tuned against
  a clean release of the same episode: sharper and cleaner than before, no
  over-sharpened outlines and no coloured fringes beside black lines. Needs a
  high-end GPU; on a weaker one, playback drops to the old frame timing by
  itself. The first use takes a few seconds to prepare (up to about 30 s on some
  systems). Two profiles now: F1 anime, F2 series and movies.

## 5.0.0 — 2026-10-12

### 📚 New: the Library tab (replaces History)
<!-- app: Everything you watch, in one place: what you're watching, what's next, and one click to continue. -->
- **Watching / Watch later / Completed** lists for every anime you download or
  save from Search, with a **Continue watching** strip that plays the next
  episode in one click.
- **Tracks what you watch automatically.** Episodes you play in mpv.net are
  ticked off (90% in counts as watched) — even while the app is closed. Progress
  is saved every 30 seconds, so a player crash loses at most half a minute.
- **Brings in what you already have.** The first time you open it, every anime
  in your download folder is added, with what you had already watched in
  mpv.net, and their posters are looked up. Newer mpv.net versions (7.1 and
  later) keep where you stopped in a different folder; both are read.
- **Per-anime history and sources.** Each anime keeps its own download history,
  and each episode shows the website it was downloaded from.
- **Continue downloading** gets the episodes the site has that aren't on disk yet.
- **Episodes and history** window: tick episodes and change the status, then
  press **Done** to save or **Close** to discard.
- Every download now shows up in the Library, including ones from a profile you
  never added there.
- **Keyboard shortcuts** (button next to *Scan anime folder*), working from
  anywhere in Windows:
  - **Open the Library** — jumps there instantly when the app is open, starts
    on it when it isn't.
  - **Continue watching** — continues the anime you watched last in mpv.net,
    without opening the app at all: an episode you stopped part-way resumes
    where you left off, otherwise the next one starts. If it isn't downloaded
    yet, the app opens on the Downloader with that anime and its missing
    episodes already selected.
  - Any key with Ctrl, Alt or Shift; the app checks it against Windows, other
    shortcuts and running apps, lets you test it live, and repairs a shortcut
    that stopped working when it starts.

### ⚡ Opens faster
<!-- app: The app opens about a third faster. -->
- The window appears about a third sooner: it no longer waits for the cloud
  check before opening, the other tabs are built right after the window shows,
  and libraries the app never uses are no longer loaded at launch.

### ⬇️ Downloads
<!-- app: Downloads try another server when a link is dead, and check for free space before they start. -->
- A download link that turns out to be dead now makes the episode try its next
  server, instead of counting it as downloaded.
- Episodes with no working FHD mirror fall back to HD.
- Before starting, the app checks there is enough free space on the download
  drive and the temp drive.
- When witanime shows the hidden browser its "session expired" screen, the
  download carries on in a visible browser instead of failing every server.
- Hidden download browsers no longer steal focus or pop up windows (ads
  included), and stay out of the taskbar and Alt+Tab.
- A season's last episode is saved with **(Final)** in its file name.
- Loading an anime that already has a profile opens that profile instead of
  making a copy, and its episode range grows as new episodes come out.

### 🔔 Notifications
<!-- app: Windows notifications when downloads finish or new episodes come out, and no more repeated Discord posts. -->
- **Windows notifications** when downloads finish and when new episodes are
  found (skipped while you are using the app).
- **Discord: each new episode is posted once.** No more repeats every 30
  minutes, and no second copy next to the cloud service's message.
- Downloading an episode from the Discord button now marks it as seen in your
  Watchlist.

### ✨ Look and feel
<!-- app: Smoother menus and tooltips that match the rest of the app. -->
- Every hint now uses the app's own Fluent tooltip, and dropdowns open smoothly
  instead of stuttering.
- mpv.net: **Ctrl+1** (shaders off/on) works again on newer mpv.net versions.

### 🔄 Updates
<!-- app: Updates can be checked for any time, cancelled, and retried if they fail. -->
- **Check for updates** button at the bottom of the sidebar.
- The update download can be cancelled, and a stalled connection no longer
  freezes the app.
- The downloaded setup is checked against the release before it runs, and the
  app closes normally to install it (your settings are saved first). You're
  warned if a download is still running.
- If an update fails, you can try again right away, and the reason is shown
  and written to `updater.log`.
- Updating works for Windows user names with an apostrophe or non-English
  letters.
- An update now replaces the app's files cleanly instead of copying the new
  ones over the old (old files were left behind), and leftover update
  downloads are deleted.
