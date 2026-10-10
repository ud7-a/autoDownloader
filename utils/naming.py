"""Names shared by the engine, the Downloader and the disk check, so they always
agree on where an anime's episodes live and what counts as a video."""

VIDEO_EXTENSIONS = (".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".ts")


def safe_folder_name(name):
    """A profile/anime name as a Windows folder name (characters Windows forbids
    removed). This is the per-anime folder inside the download folder."""
    return "".join(c for c in (name or "") if c not in r'\/:*?"<>|').strip()
