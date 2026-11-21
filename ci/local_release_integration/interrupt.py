# SPDX-License-Identifier: LicenseRef-Boardwright-Source-Available
# Copyright (c) Ryan Hicks
"""Run one ``boardwright release local`` command and kill the process at a chosen point (testbench only).

    interrupt.py checkpoint <name> -- <release local args...>
    interrupt.py event <event> -- <release local args...>

``checkpoint`` exits at a Git ref or store checkpoint (``after-branch``,
``after-store``, ...); ``event`` exits immediately before the named index
event is appended (after its entry was frozen). ``os._exit`` skips every
``finally`` and lock release path, like a killed process. Exit status 70
means the interruption happened.
"""

from __future__ import annotations

import os
import sys

INTERRUPTED = 70


def main(argv: list[str]) -> int:
    kind, point, separator, *args = argv
    assert separator == "--" and kind in ("checkpoint", "event"), argv
    from boardwright import local_finalize, local_publish, release_store
    from boardwright.local_release import main as release_main

    def die(reason: str) -> None:
        print(f"[interrupt] killed at {reason}", flush=True)
        os._exit(INTERRUPTED)

    if kind == "checkpoint":
        def checkpoint(name: str) -> None:
            if name == point:
                die(f"checkpoint {name}")

        # local_publish imported the function by name: patch both references.
        local_finalize.checkpoint = checkpoint
        local_publish.checkpoint = checkpoint
    else:
        original = release_store.ReleaseStore.append_event

        def append_event(self, event):
            if event.get("event") == point:
                die(f"before the {point} index event")
            return original(self, event)

        release_store.ReleaseStore.append_event = append_event
    return release_main(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
