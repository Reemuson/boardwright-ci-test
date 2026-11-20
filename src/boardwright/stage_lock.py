# SPDX-License-Identifier: LicenseRef-Boardwright-Source-Available
# Copyright (c) Ryan Hicks

"""Exclusive, cross-platform lock for a stage directory.

The lock is an operating-system lock on ``<stage>.lock`` (``msvcrt.locking``
on Windows, ``fcntl.flock`` elsewhere), taken without blocking. The operating
system releases it when the holder exits, including when it is killed, so an
interrupted preview never leaves a lock that must be cleared, and no lock is
ever removed because of its age. The lock file itself is kept (deleting it
would let two processes lock different files of the same name); the holder
writes its process ID, host and command after byte 0 for diagnostics.
"""

from __future__ import annotations

import json
import os
import socket
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .errors import BoardwrightError


class StageBusy(BoardwrightError):
    """Another process holds the stage."""


def lock_path(stage_dir: Path) -> Path:
    return stage_dir.with_name(stage_dir.name + ".lock")


def _try_lock(fd: int) -> bool:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # byte 0 only; the holder info is readable
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


def holder(path: Path) -> dict:
    """The diagnostic record written by the current or last holder (may be stale)."""

    try:
        # Byte 0 is the locked region; on Windows reading it fails while held.
        with path.open("rb") as handle:
            handle.seek(1)
            data = handle.read()
        return json.loads(data.decode("utf-8")) if data.strip() else {}
    except (OSError, ValueError):
        return {}


@contextmanager
def stage_lock(stage_dir: Path, purpose: str = "preview") -> Iterator[Path]:
    """Hold the stage exclusively, or raise ``StageBusy`` at once."""

    path = lock_path(stage_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        if not _try_lock(fd):
            info = holder(path)
            who = (f"process {info.get('pid')} on {info.get('host')} since {info.get('since')} ({info.get('purpose')})"
                   if info else "another process")
            raise StageBusy(
                f"{stage_dir.name} is in use by {who}. Wait for it to finish (or stop it); "
                f"the lock is released automatically when that process exits. Lock: {path}"
            )
        try:
            info = {"pid": os.getpid(), "host": socket.gethostname(), "purpose": purpose,
                    "since": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "command": " ".join(sys.argv)[:200]}
            payload = json.dumps(info).encode("utf-8")
            os.lseek(fd, 1, os.SEEK_SET)
            os.write(fd, payload)
            os.ftruncate(fd, 1 + len(payload))
            yield path
        finally:
            _unlock(fd)
    finally:
        os.close(fd)
