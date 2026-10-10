# Changelog

What changed in each release. The section for a version is what appears on its
GitHub release page.

Add new entries under **Unreleased**; `publish.py` (or the Release workflow)
turns that heading into the version number when the release is made.

## Unreleased

## 5.0.0 — 2026-10-11

### 📚 New: the Library tab (replaces History)
- **Watching / Watch later / Completed** lists for every anime you download or
  save from Search, with a **Continue watching** strip that plays the next
  episode in one click.
- **Tracks what you watch automatically.** Episodes you play in mpv.net are
  ticked off (90% in counts as watched) — even while the app is closed. Progress
  is saved every 30 seconds, so a player crash loses at most half a minute.
- **Brings in what you already have.** The first time you open it, every anime
  in your download folder is added, with what you had already watched in
  mpv.net, and their posters are looked up.
- **Per-anime history and sources.** Each anime keeps its own download history,
  and each episode shows the website it was downloaded from.
- **Continue downloading** gets the episodes the site has that aren't on disk yet.
- **Episodes and history** window: tick episodes and change the status, then
  press **Done** to save or **Close** to discard.
- Every download now shows up in the Library, including ones from a profile you
  never added there.

### ⚡ Opens faster
- The window appears about a third sooner: it no longer waits for the cloud
  check before opening, the other tabs are built right after the window shows,
  and libraries the app never uses are no longer loaded at launch.

### ⬇️ Downloads
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
- **Windows notifications** when downloads finish and when new episodes are
  found (skipped while you are using the app).
- **Discord: each new episode is posted once.** No more repeats every 30
  minutes, and no second copy next to the cloud service's message.
- Downloading an episode from the Discord button now marks it as seen in your
  Watchlist.

### ✨ Look and feel
- Every hint now uses the app's own Fluent tooltip, and dropdowns open smoothly
  instead of stuttering.
- mpv.net: **Ctrl+1** (shaders off/on) works again on newer mpv.net versions.
