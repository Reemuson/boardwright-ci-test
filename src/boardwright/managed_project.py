# SPDX-License-Identifier: LicenseRef-Boardwright-Source-Available
# Copyright (c) Ryan Hicks

"""Versioned Boardwright project payload installation and updates."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sysconfig
from typing import Any
from collections.abc import Callable
import uuid

import yaml

from . import __version__
from .errors import BoardwrightError


STATE_PATH = Path(".boardwright/state.yaml")
BASE_PATH = Path(".boardwright/managed-base")
SCHEMA_VERSION = 1
TEMPLATE_VERSION = __version__
PLAN_VERSION = 1

_MANAGED_FILES = (
    Path("pyproject.toml"),
    Path("scripts/install_boardwright.ps1"),
    Path("scripts/kibot_launch.sh"),
    Path("LICENSES/Nguyen-MIT.txt"),
    Path("THIRD_PARTY_NOTICES.md"),
)
_MANAGED_TREES = (
    Path("src/boardwright"),
    Path("boardwright_resources"),
)
_TEXT_SUFFIXES = {
    ".json",
    ".kicad_wks",
    ".md",
    ".ps1",
    ".py",
    ".sh",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}


@dataclass(frozen=True)
class PayloadFile:
    path: Path
    source: Path
    sha256: str
    strategy: str


@dataclass(frozen=True)
class UpdateOperation:
    path: Path
    action: str
    reason: str
    merged_text: str | None = None


def payload_root() -> Path:
    override = os.environ.get("BOARDWRIGHT_PAYLOAD_ROOT", "").strip()
    candidates = []
    if override:
        candidates.append(Path(override))
    candidates.append(Path(__file__).resolve().parents[2])
    candidates.append(Path(sysconfig.get_path("data")) / "share" / "boardwright")
    for candidate in candidates:
        if (
            (candidate / "boardwright_resources").is_dir()
            and (candidate / ".github/workflows").is_dir()
            and (candidate / "Templates").is_dir()
        ):
            return candidate.resolve()
    raise BoardwrightError(
        "Could not locate the Boardwright project payload. "
        "Set BOARDWRIGHT_PAYLOAD_ROOT to a Boardwright source or installed payload tree."
    )


def collect_payload(*, workflows: bool = True) -> dict[str, PayloadFile]:
    root = payload_root()
    paths = list(_MANAGED_FILES)
    paths.extend(
        path.relative_to(root)
        for tree in _MANAGED_TREES
        for path in (root / tree).rglob("*")
        if _payload_file(path)
    )
    package_source = root / "src/boardwright"
    if not package_source.is_dir():
        installed_package = Path(__file__).resolve().parent
        paths.extend(
            Path("src/boardwright") / path.relative_to(installed_package)
            for path in installed_package.rglob("*")
            if _payload_file(path)
        )
    paths.extend(
        path.relative_to(root)
        for path in (root / "Templates").glob("Boardwright_*.kicad_wks")
        if path.is_file()
    )
    if workflows:
        paths.extend(
            path.relative_to(root)
            for path in (root / ".github/workflows").glob("*.yaml")
            if path.is_file()
        )

    payload: dict[str, PayloadFile] = {}
    for relative in sorted(set(paths), key=lambda item: item.as_posix()):
        source = root / relative
        if (
            not source.is_file()
            and relative.parts[:2] == ("src", "boardwright")
        ):
            source = Path(__file__).resolve().parent.joinpath(*relative.parts[2:])
        if not source.is_file():
            continue
        normalized = relative.as_posix()
        payload[normalized] = PayloadFile(
            path=relative,
            source=source,
            sha256=_file_hash(source),
            strategy="merge" if _is_text_path(relative) else "binary",
        )
    return payload


def install_initial_payload(
    root: Path,
    *,
    workflows: bool = True,
    force: bool = False,
    origin: str = "boardwright",
) -> list[Path]:
    project_root = root.resolve()
    state_path = project_root / STATE_PATH
    if state_path.is_file() and not force:
        return []
    payload = collect_payload(workflows=workflows)
    written: list[Path] = []
    managed: dict[str, dict[str, str]] = {}

    for name, item in payload.items():
        target = project_root / item.path
        if target.resolve() != item.source.resolve():
            if not target.exists() or force:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item.source, target)
                written.append(target)
        if target.is_file():
            entry = {"sha256": item.sha256, "strategy": item.strategy}
            if item.strategy == "merge":
                base = _write_base(project_root, item.source.read_bytes())
                entry["base"] = base.as_posix()
            managed[name] = entry

    state = {
        "schema_version": SCHEMA_VERSION,
        "template_version": TEMPLATE_VERSION,
        "origin": origin,
        "managed_files": managed,
    }
    state_path.parent.mkdir(parents=True, exist_ok=True)
    _write_yaml(state_path, state)
    written.append(state_path)
    return written


def load_state(root: Path) -> dict[str, Any]:
    path = root.resolve() / STATE_PATH
    if not path.is_file():
        raise BoardwrightError(
            "Missing .boardwright/state.yaml. Run `boardwright update plan` "
            "from an initialized project or migrate the project first."
        )
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise BoardwrightError(".boardwright/state.yaml must contain a mapping.")
    return data


def update_status(root: Path) -> dict[str, Any]:
    project_root = root.resolve()
    state = load_state(project_root)
    managed = state.get("managed_files", {})
    modified = []
    missing = []
    for name, entry in managed.items():
        path = project_root / name
        if not path.is_file():
            missing.append(name)
        elif _file_hash(path) != str(entry.get("sha256", "")):
            modified.append(name)
    blockers = build_update_plan(project_root).get("blockers", [])
    return {
        "schema_version": state.get("schema_version"),
        "installed_version": state.get("template_version"),
        "available_version": TEMPLATE_VERSION,
        "modified": sorted(modified),
        "missing": sorted(missing),
        "blockers": blockers,
    }


def build_update_plan(root: Path, *, to_version: str = "") -> dict[str, Any]:
    project_root = root.resolve()
    state = load_state(project_root)
    if to_version and to_version != TEMPLATE_VERSION:
        raise BoardwrightError(
            f"Installed tooling only provides payload {TEMPLATE_VERSION}; "
            f"cannot plan update to {to_version}."
        )
    payload = collect_payload(workflows=True)
    managed = state.get("managed_files", {})
    if not isinstance(managed, dict):
        raise BoardwrightError("state.yaml managed_files must be a mapping.")

    operations: list[UpdateOperation] = []
    source_hashes: dict[str, str] = {}
    for name in sorted(set(managed) | set(payload)):
        old = managed.get(name)
        new = payload.get(name)
        target = project_root / name
        if new:
            source_hashes[name] = new.sha256
        if old is None and new is not None:
            if target.exists():
                operations.append(UpdateOperation(Path(name), "conflict", "unmanaged target exists"))
            else:
                operations.append(UpdateOperation(Path(name), "add", "new managed file"))
            continue
        if old is not None and new is None:
            if not target.exists():
                operations.append(UpdateOperation(Path(name), "remove", "obsolete file already absent"))
            elif _file_hash(target) == str(old.get("sha256", "")):
                operations.append(UpdateOperation(Path(name), "remove", "obsolete managed file"))
            else:
                operations.append(UpdateOperation(Path(name), "conflict", "obsolete file is customized"))
            continue
        if old is None or new is None:
            continue

        old_hash = str(old.get("sha256", ""))
        local_hash = _file_hash(target) if target.is_file() else ""
        if new.sha256 == old_hash:
            action = "preserve" if local_hash != old_hash else "unchanged"
            operations.append(UpdateOperation(Path(name), action, "payload is unchanged"))
        elif local_hash == old_hash:
            operations.append(UpdateOperation(Path(name), "replace", "managed file is unmodified"))
        elif not target.exists():
            operations.append(UpdateOperation(Path(name), "conflict", "locally removed file changed upstream"))
        elif new.strategy != "merge" or str(old.get("strategy")) != "merge":
            operations.append(UpdateOperation(Path(name), "conflict", "customized binary file changed upstream"))
        else:
            merged = _merge_text(project_root, target, new.source, old)
            if merged is None:
                operations.append(UpdateOperation(Path(name), "conflict", "overlapping text changes"))
            else:
                operations.append(UpdateOperation(Path(name), "merge", "non-overlapping text changes", merged))

    config_updates, user_hashes = _plan_config_migrations(project_root)
    return {
        "kind": "boardwright-update",
        "plan_version": PLAN_VERSION,
        "root": str(project_root),
        "from_version": state.get("template_version"),
        "to_version": TEMPLATE_VERSION,
        "state_sha256": _file_hash(project_root / STATE_PATH),
        "source_hashes": source_hashes,
        "user_hashes": user_hashes,
        "config_updates": config_updates,
        "operations": [
            {
                "path": operation.path.as_posix(),
                "action": operation.action,
                "reason": operation.reason,
            }
            for operation in operations
        ],
        "blockers": [
            operation.path.as_posix()
            for operation in operations
            if operation.action == "conflict"
        ],
    }


def write_plan(path: Path, plan: dict[str, Any]) -> Path:
    target = path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    _write_yaml(target, plan)
    return target


def read_plan(path: Path, expected_kind: str) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict) or data.get("kind") != expected_kind:
        raise BoardwrightError(f"Plan must have kind: {expected_kind}")
    if int(data.get("plan_version", 0)) != PLAN_VERSION:
        raise BoardwrightError("Unsupported plan version.")
    return data


def apply_update_plan(plan_path: Path) -> list[Path]:
    plan = read_plan(plan_path, "boardwright-update")
    root = Path(str(plan["root"])).resolve()
    if _file_hash(root / STATE_PATH) != str(plan.get("state_sha256", "")):
        raise BoardwrightError("Project state changed after the update plan was created.")
    if plan.get("blockers"):
        raise BoardwrightError(
            "Update plan has unresolved conflicts: " + ", ".join(plan["blockers"])
        )
    payload = collect_payload(workflows=True)
    for name, digest in plan.get("source_hashes", {}).items():
        if name not in payload or payload[name].sha256 != digest:
            raise BoardwrightError(f"Payload changed after planning: {name}")
    for name, digest in plan.get("user_hashes", {}).items():
        path = root / name
        actual = _file_hash(path) if path.is_file() else ""
        if actual != digest:
            raise BoardwrightError(f"User configuration changed after planning: {name}")

    state = load_state(root)
    old_managed = state.get("managed_files", {})
    operations = {item["path"]: item for item in plan.get("operations", [])}
    changes: dict[Path, bytes | None] = {}
    for name, operation in operations.items():
        action = operation["action"]
        target = root / name
        if action in {"add", "replace"}:
            changes[target] = payload[name].source.read_bytes()
        elif action == "merge":
            old = old_managed[name]
            merged = _merge_text(root, target, payload[name].source, old)
            if merged is None:
                raise BoardwrightError(f"File no longer merges cleanly: {name}")
            changes[target] = merged.encode("utf-8")
        elif action == "remove":
            changes[target] = None
    for name in plan.get("config_updates", []):
        changes[root / name] = _migrated_config_bytes(root, name)

    state_data, base_changes = _current_state_data(
        root,
        payload,
        origin=str(state.get("origin", "boardwright")),
    )
    changes.update(base_changes)
    changes[root / STATE_PATH] = _yaml_bytes(state_data)

    def verify() -> None:
        from .config import load_config
        from .validation import validate_project

        errors = [
            issue.message
            for issue in validate_project(load_config(root))
            if issue.level == "error"
        ]
        if errors:
            raise BoardwrightError(
                "Updated project validation failed: " + "; ".join(errors)
            )

    written = _apply_transaction(root, changes, verify=verify)
    return written


def _current_state_data(
    root: Path,
    payload: dict[str, PayloadFile],
    *,
    origin: str,
) -> tuple[dict[str, Any], dict[Path, bytes]]:
    managed: dict[str, dict[str, str]] = {}
    base_changes: dict[Path, bytes] = {}
    for name, item in payload.items():
        target = root / name
        if not target.is_file() and not item.source.is_file():
            continue
        entry = {"sha256": item.sha256, "strategy": item.strategy}
        if item.strategy == "merge":
            content = item.source.read_bytes()
            relative = BASE_PATH / hashlib.sha256(content).hexdigest()
            entry["base"] = relative.as_posix()
            if not (root / relative).exists():
                base_changes[root / relative] = content
        managed[name] = entry
    return (
        {
            "schema_version": SCHEMA_VERSION,
            "template_version": TEMPLATE_VERSION,
            "origin": origin,
            "managed_files": managed,
        },
        base_changes,
    )


def _merge_text(
    root: Path,
    local: Path,
    incoming: Path,
    old_entry: dict[str, Any],
) -> str | None:
    base_name = str(old_entry.get("base", ""))
    base = root / base_name if base_name else None
    if base is None or not base.is_file():
        return None
    temporary = root / f".boardwright-merge-{uuid.uuid4().hex}"
    temporary.mkdir()
    try:
        merged = temporary / "local"
        shutil.copy2(local, merged)
        completed = subprocess.run(
            ["git", "merge-file", "-p", str(merged), str(base), str(incoming)],
            text=True,
            capture_output=True,
            check=False,
            timeout=20,
        )
        return completed.stdout if completed.returncode == 0 else None
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def _plan_config_migrations(root: Path) -> tuple[list[str], dict[str, str]]:
    from .config import DEFAULT_CONFIG_FILES

    updates = []
    hashes = {}
    for filename in DEFAULT_CONFIG_FILES:
        path = root / ".boardwright" / filename
        relative = path.relative_to(root).as_posix()
        hashes[relative] = _file_hash(path) if path.is_file() else ""
        if not path.is_file():
            updates.append(relative)
            continue
        current = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        default = yaml.safe_load(DEFAULT_CONFIG_FILES[filename]) or {}
        migrated = _merge_missing(current, default)
        if filename == "project.yaml":
            project = migrated.get("project", {})
            if (
                isinstance(project, dict)
                and not str(project.get("git_url") or "").strip()
                and str(project.get("github_repo") or "").strip()
            ):
                project["git_url"] = str(project.pop("github_repo")).strip()
        if migrated != current:
            updates.append(relative)
    return updates, hashes


def _migrated_config_bytes(root: Path, relative: str) -> bytes:
    from .config import DEFAULT_CONFIG_FILES

    path = root / relative
    filename = path.name
    default = yaml.safe_load(DEFAULT_CONFIG_FILES[filename]) or {}
    current = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}
    migrated = _merge_missing(current or {}, default)
    if filename == "project.yaml":
        project = migrated.get("project", {})
        if (
            isinstance(project, dict)
            and not str(project.get("git_url") or "").strip()
            and str(project.get("github_repo") or "").strip()
        ):
            project["git_url"] = str(project.pop("github_repo")).strip()
    return _yaml_bytes(migrated)


def _merge_missing(current: Any, default: Any) -> Any:
    if not isinstance(current, dict) or not isinstance(default, dict):
        return current
    result = dict(current)
    for key, value in default.items():
        if key not in result:
            result[key] = value
        elif isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _merge_missing(result[key], value)
    return result


def _apply_transaction(
    root: Path,
    changes: dict[Path, bytes | None],
    *,
    verify: Callable[[], None] | None = None,
) -> list[Path]:
    backup_root = root / f".boardwright-update-rollback-{uuid.uuid4().hex}"
    backup_root.mkdir()
    records: list[tuple[Path, Path | None]] = []
    written: list[Path] = []
    try:
        for index, (target, content) in enumerate(changes.items()):
            resolved = target.resolve()
            if root not in resolved.parents:
                raise BoardwrightError(f"Managed path escapes project root: {target}")
            backup = None
            if target.exists():
                backup = backup_root / str(index)
                backup.parent.mkdir(parents=True, exist_ok=True)
                if target.is_dir():
                    shutil.copytree(target, backup)
                else:
                    shutil.copy2(target, backup)
            records.append((target, backup))
            if content is None:
                if target.is_dir():
                    shutil.rmtree(target)
                elif target.exists():
                    target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_name(target.name + ".boardwright-new")
                temporary.write_bytes(content)
                os.replace(temporary, target)
            written.append(target)
        if verify is not None:
            verify()
        return written
    except Exception:
        for target, backup in reversed(records):
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            elif target.exists():
                target.unlink()
            if backup:
                target.parent.mkdir(parents=True, exist_ok=True)
                if backup.is_dir():
                    shutil.copytree(backup, target)
                else:
                    shutil.copy2(backup, target)
        raise
    finally:
        shutil.rmtree(backup_root, ignore_errors=True)


def _write_base(root: Path, content: bytes) -> Path:
    digest = hashlib.sha256(content).hexdigest()
    relative = BASE_PATH / digest
    target = root / relative
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return relative


def _payload_file(path: Path) -> bool:
    return (
        path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix not in {".pyc", ".pyo"}
        and ".generated." not in path.name
    )


def _is_text_path(path: Path) -> bool:
    return path.suffix.lower() in _TEXT_SUFFIXES or path.name == ".gitignore"


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
        newline="\n",
    )


def _yaml_bytes(data: dict[str, Any]) -> bytes:
    return yaml.safe_dump(
        data,
        sort_keys=False,
        allow_unicode=True,
    ).encode("utf-8")
