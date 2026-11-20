# SPDX-License-Identifier: LicenseRef-Boardwright-Source-Available
# Copyright (c) Ryan Hicks

"""Push a release tag with a bounded, verified retry (audit R1).

Release 0.1.7's approved run pushed its release commit, then GitHub answered
the tag push with ``! [remote rejected] 0.1.7 -> 0.1.7 (failed)`` and the
tag had to be created by hand. This module pushes an annotated tag that
``prepare-release.yaml`` has just created, and on every attempt:

1. verifies the local tag is annotated and points at the expected release
   commit, and that the release commit is on the remote release branch;
2. reads the remote tag. If it already points at the release commit, the tag
   is in place (an earlier attempt may have succeeded despite an error):
   success. If it points anywhere else: conflict, stop;
3. pushes the tag without force. A failure is classified: authentication or
   permission, conflict and unknown errors stop at once; only recognised
   transient failures are retried, after re-reading the remote.

It never force-updates or deletes a tag.

Exit status: 0 tag in place; 2 local state wrong; 3 conflicting remote tag;
4 authentication/permission; 5 transient failures exhausted or unknown error.

Usage::

    python -m boardwright.tag_push --tag 0.1.7 --commit SHA [--remote origin]
        [--branch main] [--attempts 4] [--delay 5]
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Callable

Runner = Callable[[list[str]], "GitResult"]

AUTH_PATTERNS = (
    r"authentication failed",
    r"permission to .* denied",
    r"permission denied",
    r"\b403\b",
    r"\b401\b",
    r"could not read username",
    r"invalid username or password",
    r"protected (ref|tag)",
    r"refusing to allow",
    r"push declined",
    r"rule violations",
)
CONFLICT_PATTERNS = (
    r"already exists",
    r"would clobber existing tag",
    r"\(stale info\)",
    r"non-fast-forward",
    r"\(fetch first\)",
)
TRANSIENT_PATTERNS = (
    r"\[remote rejected\] .*\(failed\)",
    r"\b50[0-4]\b",
    r"internal server error",
    r"rpc failed",
    r"early eof",
    r"connection (reset|refused|timed out)",
    r"operation timed out",
    r"timed out",
    r"could not resolve host",
    r"failed to connect",
    r"the remote end hung up unexpectedly",
    r"temporary failure",
    r"unexpected disconnect",
)

EXIT_OK, EXIT_LOCAL, EXIT_CONFLICT, EXIT_AUTH, EXIT_EXHAUSTED = 0, 2, 3, 4, 5


@dataclass(frozen=True)
class GitResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


def run_git(argv: list[str]) -> GitResult:
    try:
        done = subprocess.run(["git", *argv], capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return GitResult(124, "", "git timed out")
    return GitResult(done.returncode, done.stdout, done.stderr)


def classify(stderr: str) -> str:
    """'auth', 'conflict', 'transient' or 'unknown' for a failed git command."""

    text = stderr.lower()
    for kind, patterns in (("auth", AUTH_PATTERNS), ("conflict", CONFLICT_PATTERNS), ("transient", TRANSIENT_PATTERNS)):
        if any(re.search(pattern, text) for pattern in patterns):
            return kind
    return "unknown"


@dataclass(frozen=True)
class RemoteTag:
    state: str  # 'absent', 'present' or 'error'
    object_sha: str = ""
    commit_sha: str = ""
    error: str = ""


def remote_tag(git: Runner, remote: str, tag: str) -> RemoteTag:
    done = git(["ls-remote", "--tags", remote, f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}"])
    if done.returncode != 0:
        return RemoteTag("error", error=done.stderr.strip() or f"exit {done.returncode}")
    refs = {}
    for line in done.stdout.splitlines():
        sha, _, ref = line.partition("\t")
        refs[ref.strip()] = sha.strip()
    if f"refs/tags/{tag}" not in refs:
        return RemoteTag("absent")
    obj = refs[f"refs/tags/{tag}"]
    return RemoteTag("present", obj, refs.get(f"refs/tags/{tag}^{{}}", obj))


def local_problems(git: Runner, tag: str, commit: str) -> list[str]:
    problems = []
    kind = git(["cat-file", "-t", f"refs/tags/{tag}"])
    if kind.returncode != 0:
        return [f"local tag {tag} does not exist"]
    if kind.stdout.strip() != "tag":
        problems.append(f"local tag {tag} is not annotated")
    peeled = git(["rev-parse", f"refs/tags/{tag}^{{commit}}"]).stdout.strip()
    if peeled != commit:
        problems.append(f"local tag {tag} points at {peeled or '(nothing)'}, not the release commit {commit}")
    return problems


def release_commit_on_remote(git: Runner, remote: str, branch: str, commit: str) -> tuple[bool, str]:
    """(True, '') when ``commit`` is the remote branch head or an ancestor of it."""

    head = git(["ls-remote", "--heads", remote, f"refs/heads/{branch}"])
    if head.returncode != 0:
        return False, head.stderr.strip() or "cannot read the remote branch"
    sha = head.stdout.split("\t", 1)[0].strip()
    if not sha:
        return False, f"remote branch {branch} does not exist"
    if sha == commit:
        return True, ""
    fetched = git(["fetch", "--quiet", "--no-tags", remote, f"refs/heads/{branch}"])
    if fetched.returncode != 0:
        return False, fetched.stderr.strip() or "cannot fetch the remote branch"
    ancestor = git(["merge-base", "--is-ancestor", commit, sha])
    if ancestor.returncode == 0:
        return True, ""
    return False, f"release commit {commit} is not on {remote}/{branch} ({sha})"


def push_tag(
    tag: str,
    commit: str,
    *,
    remote: str = "origin",
    branch: str = "main",
    attempts: int = 4,
    delay: float = 5.0,
    git: Runner = run_git,
    sleep: Callable[[float], None] = time.sleep,
    echo: Callable[[str], None] = print,
) -> int:
    attempts = max(1, attempts)
    for attempt in range(1, attempts + 1):
        prefix = f"[tag {tag} attempt {attempt}/{attempts}]"
        problems = local_problems(git, tag, commit)
        if problems:
            echo(f"{prefix} refusing: " + "; ".join(problems))
            return EXIT_LOCAL
        on_remote, detail = release_commit_on_remote(git, remote, branch, commit)
        if not on_remote:
            kind = classify(detail)
            if kind == "transient" and attempt < attempts:
                echo(f"{prefix} cannot confirm the release commit on {remote}/{branch} (transient): {detail}")
                sleep(delay * 3 ** (attempt - 1))
                continue
            echo(f"{prefix} refusing: {detail}")
            return EXIT_AUTH if kind == "auth" else (EXIT_EXHAUSTED if kind == "transient" else EXIT_LOCAL)
        state = remote_tag(git, remote, tag)
        if state.state == "present":
            if state.commit_sha == commit:
                echo(f"{prefix} remote tag already points at the release commit {commit}; nothing to push.")
                return EXIT_OK
            echo(f"{prefix} conflict: remote tag points at {state.commit_sha}, not {commit}. Not forcing.")
            return EXIT_CONFLICT
        if state.state == "error":
            kind = classify(state.error)
            if kind == "auth":
                echo(f"{prefix} authentication/permission failure reading the remote: {state.error}")
                return EXIT_AUTH
            if kind != "transient" or attempt == attempts:
                echo(f"{prefix} cannot read the remote tag ({kind}): {state.error}")
                return EXIT_EXHAUSTED
            echo(f"{prefix} transient failure reading the remote: {state.error}")
            sleep(delay * 3 ** (attempt - 1))
            continue
        pushed = git(["push", remote, f"refs/tags/{tag}:refs/tags/{tag}"])
        if pushed.returncode == 0:
            after = remote_tag(git, remote, tag)
            if after.state == "present" and after.commit_sha == commit:
                echo(f"{prefix} pushed; remote tag points at {commit}.")
                return EXIT_OK
            if after.state == "present":
                echo(f"{prefix} conflict after push: remote tag points at {after.commit_sha}.")
                return EXIT_CONFLICT
            echo(f"{prefix} push reported success but the remote tag is not visible; re-checking.")
            sleep(delay * 3 ** (attempt - 1))
            continue
        kind = classify(pushed.stderr)
        message = pushed.stderr.strip()
        if kind == "auth":
            echo(f"{prefix} authentication/permission failure: {message}")
            return EXIT_AUTH
        # Whatever the error, the remote is authoritative: an attempt that
        # failed in transit may still have created the tag.
        after = remote_tag(git, remote, tag)
        if after.state == "present":
            if after.commit_sha == commit:
                echo(f"{prefix} push reported '{message}', but the remote tag points at the release commit: in place.")
                return EXIT_OK
            echo(f"{prefix} conflict: remote tag points at {after.commit_sha}, not {commit}. Not forcing.")
            return EXIT_CONFLICT
        if kind == "conflict":
            echo(f"{prefix} push rejected as a conflict: {message}")
            return EXIT_CONFLICT
        if kind != "transient":
            echo(f"{prefix} unrecognised push failure, not retried: {message}")
            return EXIT_EXHAUSTED
        if attempt == attempts:
            echo(f"{prefix} transient failure, attempts exhausted: {message}")
            return EXIT_EXHAUSTED
        wait = delay * 3 ** (attempt - 1)
        echo(f"{prefix} transient failure, retrying in {wait:g}s: {message}")
        sleep(wait)
    return EXIT_EXHAUSTED


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m boardwright.tag_push", description=__doc__.split("\n\n")[0])
    parser.add_argument("--tag", required=True)
    parser.add_argument("--commit", required=True, help="Expected release commit (full SHA).")
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--branch", default="main", help="Remote branch that must contain the release commit.")
    parser.add_argument("--attempts", type=int, default=4)
    parser.add_argument("--delay", type=float, default=5.0, help="First retry delay in seconds; tripled each retry.")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[0-9a-f]{40}", args.commit):
        print("--commit must be a full 40-character SHA.", file=sys.stderr)
        return EXIT_LOCAL
    return push_tag(args.tag, args.commit, remote=args.remote, branch=args.branch, attempts=args.attempts, delay=args.delay)


if __name__ == "__main__":
    raise SystemExit(main())
