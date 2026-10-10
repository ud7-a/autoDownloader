"""Will this download run fit on the disk?

An episode's size is only known once its download starts, so the estimate uses
what this anime's episodes have actually weighed so far (the videos already in
its folder), or a typical FHD episode when there are none yet.
"""
import os
import shutil

from utils.naming import VIDEO_EXTENSIONS as VIDEO_EXTS

TYPICAL_EPISODE_BYTES = 450 * 1024 ** 2     # a witanime FHD episode is ~400-450 MB
KEEP_FREE_BYTES = 1024 ** 3                 # leave 1 GB for Windows and temp files
_MIN_REAL_VIDEO = 20 * 1024 ** 2            # smaller files are partials or samples


def typical_episode_bytes(folder):
    """Average size of the episodes already downloaded into `folder`, else the
    built-in typical size."""
    sizes = []
    try:
        for name in os.listdir(folder):
            if name.lower().endswith(VIDEO_EXTS):
                try:
                    size = os.path.getsize(os.path.join(folder, name))
                except OSError:
                    continue
                if size >= _MIN_REAL_VIDEO:
                    sizes.append(size)
    except OSError:
        pass
    return int(sum(sizes) / len(sizes)) if sizes else TYPICAL_EPISODE_BYTES


def free_bytes(path):
    """Free space on the drive holding `path` (which may not exist yet)."""
    probe = os.path.abspath(path or ".")
    while probe and not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return None


def _drive(path):
    return os.path.splitdrive(os.path.abspath(path or "."))[0].upper()


def check_space(target_dir, anime_folder, episode_count, temp_dir=None, parallel=1):
    """Drives this run would overfill: (per_episode, [(drive, needed, free), ...]).
    An empty list means it fits (a drive whose free space can't be read is never
    reported -- a failed check must not block a download).

    Episodes are downloaded into `temp_dir` first and moved into the anime folder
    when done. On one drive that is just a move, so the total is all it needs. On
    two drives the temp drive must also hold the episodes downloading at the same
    time (`parallel`).
    """
    per_episode = typical_episode_bytes(anime_folder)
    count = max(0, int(episode_count))
    demands = {_drive(target_dir): per_episode * count}
    if temp_dir and _drive(temp_dir) != _drive(target_dir):
        demands[_drive(temp_dir)] = per_episode * min(count, max(1, int(parallel)))
    paths = {_drive(target_dir): target_dir}
    if temp_dir:
        paths.setdefault(_drive(temp_dir), temp_dir)
    short = []
    for drive, needed in demands.items():
        free = free_bytes(paths[drive])
        if free is not None and needed + KEEP_FREE_BYTES > free:
            short.append((drive or paths[drive], needed, free))
    return per_episode, short


def human(n):
    gb = n / 1024 ** 3
    return f"{gb:.1f} GB" if gb >= 1 else f"{n / 1024 ** 2:.0f} MB"
