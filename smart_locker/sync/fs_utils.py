"""
File: fs_utils.py
Description: Filesystem helpers for the sync layer. Provides is_network_path(),
             which reports whether a path lives on a network filesystem
             (CIFS/SMB/NFS). The watchdog file watchers use it to decide
             whether a live inotify watch is reliable for a given path.
Project: smart_locker/sync
Notes: inotify (watchdog's default Observer on Linux) does NOT receive events
       for changes made by *other* hosts on a network share. On the Raspberry Pi
       the source Excel and photos live on the mounted M: (CIFS) share, so the
       watchers fall back to the startup import + daily cron instead of a watch
       that would silently never fire. Detection is Linux-specific via
       /proc/mounts; on other platforms it returns False (assume local).
"""

import sys
from pathlib import Path

# Filesystem types that are served over the network — inotify is unreliable on these.
_NETWORK_FSTYPES = {
    "cifs", "smbfs", "smb3", "nfs", "nfs4", "ncpfs", "9p", "fuse.sshfs",
}


def is_network_path(path: str | Path) -> bool:
    """Return True if ``path`` resides on a network filesystem (CIFS/SMB/NFS/...).

    Resolves ``path`` to an absolute location, then finds the longest mount
    point in ``/proc/mounts`` that is a prefix of it and inspects that mount's
    filesystem type. Used by the file watchers to skip a live inotify watch on
    network shares (where it would never fire for remote writes) and rely on
    scheduled re-imports instead.

    Args:
        path: A file or directory path to classify.

    Returns:
        True if the path is on a recognised network filesystem. False on
        non-Linux platforms, on local filesystems, or when the mount table
        cannot be read (safe default: assume local).
    """
    if not sys.platform.startswith("linux"):
        return False

    try:
        target = str(Path(path).resolve())
    except OSError:
        return False

    try:
        with open("/proc/mounts", "r", encoding="utf-8") as handle:
            mounts = handle.readlines()
    except OSError:
        return False

    best_len = -1
    best_fstype = ""
    for line in mounts:
        parts = line.split()
        if len(parts) < 3:
            continue
        mount_point, fstype = parts[1], parts[2]
        # A mount matches if the target is the mount point itself or sits under it.
        if target == mount_point or target.startswith(mount_point.rstrip("/") + "/"):
            if len(mount_point) > best_len:
                best_len = len(mount_point)
                best_fstype = fstype

    return best_fstype in _NETWORK_FSTYPES
