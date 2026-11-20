# SPDX-License-Identifier: LicenseRef-Boardwright-Source-Available
# Copyright (c) Ryan Hicks

"""Choose where a preview or release runs: local Docker or GitHub Actions.

Policy (``docs/LOCAL_FIRST.md`` section 3):

- Every preview variant (``DRAFT``, ``CHECKED``, ``PRELIMINARY``,
  ``RELEASED``) builds locally by default, even when GitHub is usable. A
  preview is never release evidence.
- Releases use GitHub when the project has a usable GitHub remote, and run
  locally when there is no remote.
- An explicit override (``--backend``) beats the project setting
  (``backends:`` in ``project.yaml``), which beats the default.
- An unsupported remote (not GitHub) is never treated as GitHub; a release
  then needs an explicit local override.
- Selection happens once, before anything runs. A failure afterwards is
  reported with the override that would use the other backend; nothing
  switches backend by itself.

Inspection (``inspect_remote``, ``inspect_docker``) only reads state; the
command runner is injectable for tests.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Sequence

Runner = Callable[[Sequence[str], Path], "CommandResult"]

PREVIEW_VARIANTS = ("DRAFT", "CHECKED", "PRELIMINARY", "RELEASED")
SETTINGS = ("auto", "local", "github")
WORKFLOWS = ("prepare-release.yaml", "release.yaml", "dev-preview.yaml")
_GITHUB_URL = re.compile(
    r"^(?:https://github\.com/|ssh://git@github\.com/|git@github\.com:)"
    r"(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)
# Credentials in a remote URL: any userinfo in http(s) (a token alone is
# enough), a password in any scheme (``ssh://user:pw@``), or a query or
# fragment (``?token=``). An ssh login name (``git@``) is not a secret.
_HTTP_USERINFO = re.compile(r"^https?://[^/?#]*@", re.IGNORECASE)
_PASSWORD = re.compile(r"^[a-z][a-z0-9+.-]*://[^/@?#]*:[^/@?#]*@", re.IGNORECASE)
_SCHEME_AUTHORITY = re.compile(r"^(?P<scheme>[a-z][a-z0-9+.-]*://)(?:[^/@?#]*@)?(?P<rest>[^?#]*)", re.IGNORECASE)


def has_credentials(url: str) -> bool:
    url = url.strip()
    return bool(_HTTP_USERINFO.match(url) or _PASSWORD.match(url)
                or ("://" in url and ("?" in url or "#" in url)))


def redact_url(url: str) -> str:
    """The URL without userinfo, query or fragment: safe to print."""

    url = url.strip()
    match = _SCHEME_AUTHORITY.match(url)
    if match:
        return match["scheme"] + match["rest"]
    return url.split("?", 1)[0].split("#", 1)[0]


class Operation(Enum):
    PREVIEW = "preview"
    RELEASE = "release"


class Backend(Enum):
    LOCAL = "local"
    GITHUB = "github"


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


def run_command(argv: Sequence[str], cwd: Path) -> CommandResult:
    try:
        done = subprocess.run(list(argv), cwd=cwd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CommandResult(127, "", str(exc))
    return CommandResult(done.returncode, done.stdout, done.stderr)


@dataclass(frozen=True)
class RemoteInfo:
    kind: str  # "none" | "github" | "unsupported"
    url: str = ""
    repository: str = ""
    reachable: bool | None = None
    gh_authenticated: bool | None = None
    workflows_present: bool = False
    problems: tuple[str, ...] = field(default_factory=tuple)

    @property
    def usable_github(self) -> bool:
        return self.kind == "github" and not self.problems


@dataclass(frozen=True)
class DockerInfo:
    available: bool
    detail: str = ""


@dataclass(frozen=True)
class Selection:
    operation: Operation
    backend: Backend | None
    source: str  # "override" | "project" | "default"
    reason: str
    problems: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.backend is not None and not self.problems


def classify_remote_url(url: str) -> tuple[str, str]:
    """("github", "owner/name") | ("unsupported", "") | ("none", "")."""

    url = url.strip()
    if not url:
        return "none", ""
    if has_credentials(url):
        return "unsupported", ""
    match = _GITHUB_URL.match(url)
    if match:
        return "github", match["repo"]
    return "unsupported", ""


def inspect_remote(root: Path, *, run: Runner = run_command, check_access: bool = True) -> RemoteInfo:
    """Read-only inspection of ``origin``: kind, ``gh`` login, access, workflows."""

    got = run(["git", "remote", "get-url", "origin"], root)
    url = got.stdout.strip() if got.returncode == 0 else ""
    if has_credentials(url):
        # Checked before classification so the URL is never stored or echoed.
        return RemoteInfo("unsupported", "(credentials removed)",
                          problems=("the origin URL embeds credentials; remove them from the remote",))
    kind, repository = classify_remote_url(url)
    if kind == "none":
        return RemoteInfo("none")
    if kind == "unsupported":
        shown = redact_url(url)
        return RemoteInfo("unsupported", shown, problems=(f"origin {shown} is not a GitHub repository",))
    workflows = all((root / ".github" / "workflows" / name).is_file() for name in WORKFLOWS)
    problems: list[str] = []
    if not workflows:
        problems.append("the Boardwright GitHub workflows are not in .github/workflows")
    authenticated: bool | None = None
    reachable: bool | None = None
    if check_access:
        if shutil.which("gh") is None and run is run_command:
            problems.append("the GitHub CLI (gh) is not installed")
            authenticated = False
        else:
            status = run(["gh", "auth", "status", "--hostname", "github.com"], root)
            authenticated = status.returncode == 0
            if not authenticated:
                problems.append("the GitHub CLI is not authenticated (gh auth login)")
            else:
                view = run(["gh", "repo", "view", repository, "--json", "name"], root)
                reachable = view.returncode == 0
                if not reachable:
                    detail = (view.stderr or view.stdout).strip().splitlines()
                    problems.append(f"repository {repository} is not reachable" + (f": {detail[0]}" if detail else ""))
    return RemoteInfo("github", url, repository, reachable, authenticated, workflows, tuple(problems))


def inspect_docker(*, run: Runner = run_command, root: Path = Path(".")) -> DockerInfo:
    if shutil.which("docker") is None and run is run_command:
        return DockerInfo(False, "Docker is not installed or not on PATH")
    info = run(["docker", "info", "--format", "{{.ServerVersion}}"], root)
    if info.returncode != 0:
        detail = (info.stderr or info.stdout).strip().splitlines()
        return DockerInfo(False, "the Docker engine is not running" + (f": {detail[0]}" if detail else ""))
    return DockerInfo(True, f"Docker {info.stdout.strip()}")


def _requested(project_setting: str, override: str) -> tuple[str, str, list[str]]:
    problems = []
    for name, value in (("--backend", override), ("project backends setting", project_setting)):
        if value and value not in SETTINGS:
            problems.append(f"{name} {value!r} is not one of {', '.join(SETTINGS)}")
    if override and override != "auto":
        return override, "override", problems
    if project_setting and project_setting != "auto":
        return project_setting, "project", problems
    return "auto", "default", problems


def select_backend(
    operation: Operation,
    *,
    remote: RemoteInfo,
    docker: DockerInfo,
    variant: str = "",
    project_setting: str = "auto",
    override: str = "",
) -> Selection:
    """Decide once, before running, which backend handles ``operation``."""

    wanted, source, problems = _requested(project_setting, override)
    if problems:
        return Selection(operation, None, source, "invalid backend setting", tuple(problems))
    def local(reason: str, warnings: tuple[str, ...] = ()) -> Selection:
        if not docker.available:
            # Name the override, but never take it: no silent switch.
            other = "; use --backend github to run on GitHub" if remote.usable_github else ""
            return Selection(operation, None, source, reason,
                             (f"local Docker is unavailable ({docker.detail}){other}",))
        return Selection(operation, Backend.LOCAL, source, reason, (), warnings)

    def github(reason: str) -> Selection:
        if remote.kind == "none":
            return Selection(operation, None, source, reason, ("the project has no remote; GitHub cannot be used",))
        if remote.kind == "unsupported":
            return Selection(operation, None, source, reason,
                             (f"origin is not a GitHub repository ({remote.url}); only GitHub is supported as a remote backend",))
        if remote.problems:
            return Selection(operation, None, source, reason, tuple(remote.problems))
        return Selection(operation, Backend.GITHUB, source, reason)

    if wanted == "local":
        warnings: tuple[str, ...] = ()
        if operation is Operation.RELEASE and remote.kind == "github":
            warnings = ("this is a local release: its approval is not server-enforced and its tag is not pushed to GitHub",)
        return local(f"explicit {source} setting: local", warnings)
    if wanted == "github":
        return github(f"explicit {source} setting: github")

    if operation is Operation.PREVIEW:
        name = variant.upper()
        if name and name not in PREVIEW_VARIANTS:
            return Selection(operation, None, source, "invalid variant",
                             (f"variant {variant!r} is not one of {', '.join(PREVIEW_VARIANTS)}",))
        return local(f"{name or 'all'} previews build locally by default (never release evidence)")

    if remote.kind == "none":
        return local("the project has no remote: releases are local")
    if remote.kind == "unsupported":
        return Selection(operation, None, source, "unsupported remote",
                         (f"origin is not a GitHub repository ({remote.url}); "
                          "release locally with an explicit --backend local",))
    if remote.problems:
        return Selection(operation, None, source, "GitHub remote is not usable",
                         tuple(remote.problems) + ("fix the problem, or release locally with an explicit --backend local",))
    return github("the project has a usable GitHub remote: releases use GitHub")


def format_selection(selection: Selection) -> str:
    lines = [
        f"{selection.operation.value}: "
        + (selection.backend.value if selection.backend else "refused")
        + f" ({selection.source}) - {selection.reason}"
    ]
    lines += [f"  problem: {p}" for p in selection.problems]
    lines += [f"  warning: {w}" for w in selection.warnings]
    return "\n".join(lines)
