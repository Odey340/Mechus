"""Whole-file exclusive lock that works on Linux/macOS (fcntl) and Windows (msvcrt).

``fcntl`` does not exist on Windows, so importing it at module level made the API
server unimportable there. Use ``lock_exclusive(fh)`` / ``unlock(fh)`` instead of
``fcntl.flock(fh.fileno(), fcntl.LOCK_EX / LOCK_UN)``.
"""
from __future__ import annotations

import os
from typing import IO

if os.name == "nt":
    import msvcrt

    def lock_exclusive(handle: IO) -> None:
        """Block until this process holds the lock (byte 0 of the file)."""
        pos = handle.tell()
        handle.seek(0)
        try:
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)  # retries for ~10 s
                    return
                except OSError:
                    continue
        finally:
            handle.seek(pos)

    def unlock(handle: IO) -> None:
        pos = handle.tell()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        finally:
            handle.seek(pos)

else:
    import fcntl

    def lock_exclusive(handle: IO) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)

    def unlock(handle: IO) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
