# SPDX-License-Identifier: LicenseRef-Boardwright-Source-Available
# Copyright (c) Ryan Hicks

"""Boardwright-owned stage directories and their temporaries.

A stage is replaced only when it carries ``.git/boardwright-stage``;
temporaries (snapshots, new stages, retired stages) are created with
``mkdtemp`` under short names and an owner marker naming their stage, and are
only ever removed when that marker matches. Nothing is removed by path alone.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

from .errors import BoardwrightError

STAGE_MARKER = "boardwright-stage"
TEMP_OWNER = ".boardwright-temporary"


def remove_tree(path: Path) -> None:
    def make_writable(func, target, _exc):
        os.chmod(target, stat.S_IWRITE)
        func(target)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=make_writable)
    else:  # pragma: no cover
        shutil.rmtree(path, onerror=make_writable)


def owned_temporary(workdir: Path, kind: str) -> Path:
    """A new, uniquely named directory beside the stage, marked as Boardwright's.

    ``mkdtemp`` creates it atomically and never reuses an existing path, so a
    run never adopts another run's (or anyone's) directory. Short names: a
    nested stage under a name repeating the stage name doubled the path length
    and broke clones on Windows (MAX_PATH). Ownership comes from the marker's
    stage name, never from the directory name.
    """

    workdir.parent.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix=f".bw-{kind}-", dir=workdir.parent))
    (path / TEMP_OWNER).write_text(json.dumps({"stage": workdir.name, "kind": kind, "pid": os.getpid()}) + "\n",
                                   encoding="utf-8")
    return path


def is_owned_temporary(path: Path, workdir: Path) -> bool:
    marker = path / TEMP_OWNER
    if path.is_symlink() or not marker.is_file() or marker.is_symlink():
        return False
    try:
        return json.loads(marker.read_text(encoding="utf-8")).get("stage") == workdir.name
    except (OSError, ValueError):
        return False


def remove_owned(path: Path, workdir: Path) -> None:
    """Remove a temporary directory only when it carries this stage's owner marker."""

    if path.exists() and is_owned_temporary(path, workdir):
        remove_tree(path)


def remove_abandoned_temporaries(workdir: Path) -> None:
    """Temporaries left by a killed run of this stage (the caller holds the stage lock)."""

    for kind in ("snapshot", "staging", "retired"):
        for path in workdir.parent.glob(f".bw-{kind}-*"):
            if is_owned_temporary(path, workdir):
                remove_tree(path)


def swap_into_place(new_stage: Path, workdir: Path) -> None:
    """Replace the previous stage with the new one; refuse anything that is not a stage."""

    if workdir.exists() or workdir.is_symlink():
        if workdir.is_symlink() or not (workdir / ".git" / STAGE_MARKER).is_file():
            raise BoardwrightError(f"{workdir} exists and is not a Boardwright stage; refusing to replace it.")
        retired = owned_temporary(workdir, "retired")
        old = retired / "s"
        try:
            os.replace(workdir, old)
        except OSError as exc:
            remove_tree(retired)
            raise BoardwrightError(f"Cannot replace the previous stage {workdir} ({exc}); a file in it may be "
                                   "open in another program. The previous stage is unchanged.") from exc
        try:
            os.replace(new_stage, workdir)
        except OSError:
            os.replace(old, workdir)  # put the previous stage back
            raise
        remove_tree(retired)
    else:
        os.replace(new_stage, workdir)
