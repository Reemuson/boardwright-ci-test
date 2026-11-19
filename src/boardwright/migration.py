# SPDX-License-Identifier: LicenseRef-Boardwright-Source-Available
# Copyright (c) Ryan Hicks

"""Migration from Nguyen Vincent's hierarchical KiCad/KiBot template."""

from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any, Callable
import uuid

import yaml

from .changelog import parse_releases
from .config import DEFAULT_CONFIG_FILES, load_config
from .errors import BoardwrightError
from .git_ops import current_branch, remote_url
from .kicad_tables import _sync_kibot_project_metadata
from .managed_project import (
    PLAN_VERSION,
    TEMPLATE_VERSION,
    collect_payload,
    install_initial_payload,
    read_plan,
    write_plan,
    _file_hash,
)
from .validation import validate_project


MIGRATION_KIND = "nguyen-migration"
_LEGACY_DIRS = (Path("kibot_yaml"), Path("kibot_resources"))
_PLACEHOLDERS = {
    "",
    "project name",
    "board name",
    "company",
    "company name",
    "author",
}


def looks_like_nguyen_project(root: Path) -> bool:
    project_root = root.resolve()
    projects = list(project_root.glob("*.kicad_pro"))
    schematics = list(project_root.glob("*.kicad_sch"))
    pcbs = list(project_root.glob("*.kicad_pcb"))
    main = project_root / "kibot_yaml/kibot_main.yaml"
    workflow = project_root / ".github/workflows/ci.yaml"
    worksheets = list((project_root / "Templates").glob("KDT_*.kicad_wks"))
    if not (projects and schematics and pcbs and main.is_file() and workflow.is_file() and worksheets):
        return False
    text = main.read_text(encoding="utf-8", errors="replace")
    return (
        "KDT_Hierarchical" in text
        or "kibot_resources" in text
        or "KDT_Template" in text
    ) and "kibot_out_pdf_schematic.yaml" in text


def build_migration_plan(root: Path, project: Path | None = None) -> dict[str, Any]:
    project_root = root.resolve()
    if (project_root / ".boardwright").exists():
        raise BoardwrightError(
            "This is already a Boardwright project. Use `boardwright update plan`."
        )
    if not looks_like_nguyen_project(project_root):
        raise BoardwrightError(
            "Repository does not match the Nguyen hierarchical template family."
        )

    projects = sorted(project_root.glob("*.kicad_pro"))
    selected = _select_project(project_root, projects, project)
    project_data = _read_project_json(selected)
    kicad_values = _project_text_variables(project_data)
    kibot_values = _legacy_kibot_definitions(
        project_root / "kibot_yaml/kibot_main.yaml"
    )
    candidates = _metadata_candidates(project_root, kicad_values, kibot_values)
    resolutions, metadata_blockers = _initial_resolutions(candidates)

    logo_source = _candidate_value(candidates.get("logo", []))
    logo_target = ""
    blockers: list[str] = []
    if logo_source:
        source = project_root / logo_source
        if source.is_file():
            logo_target = (Path("assets/logos") / source.name).as_posix()
            target = project_root / logo_target
            if target.exists() and _file_hash(target) != _file_hash(source):
                blockers.append(f"logo target already exists with different content: {logo_target}")

    payload = collect_payload(workflows=True)
    legacy_paths = _legacy_paths(project_root)
    protected_existing = []
    for name, item in payload.items():
        target = project_root / name
        if target.exists() and target not in legacy_paths and target.resolve() != item.source.resolve():
            protected_existing.append(name)
    blockers.extend(f"Boardwright payload target already exists: {name}" for name in protected_existing)

    hashed_paths = set(legacy_paths)
    hashed_paths.add(selected)
    hashed_paths.update(project_root.glob("*.kicad_sch"))
    for preserved in (project_root / "CHANGELOG.md", project_root / "LICENSE"):
        if preserved.is_file():
            hashed_paths.add(preserved)
    if logo_source and (project_root / logo_source).is_file():
        hashed_paths.add(project_root / logo_source)

    return {
        "kind": MIGRATION_KIND,
        "plan_version": PLAN_VERSION,
        "root": str(project_root),
        "project": selected.relative_to(project_root).as_posix(),
        "source_hashes": {
            path.relative_to(project_root).as_posix(): _file_hash(path)
            for path in sorted(hashed_paths)
            if path.is_file()
        },
        "metadata_candidates": candidates,
        "resolutions": resolutions,
        "logo": {
            "source": logo_source,
            "target": logo_target,
        },
        "worksheet_rewrites": {
            # ${KIPRJMOD} is required: KiCad PCB exports do not resolve a bare
            # relative worksheet path (verified with kicad-cli 9.0.6 and 10.0.1).
            "schematic": "${KIPRJMOD}/Templates/Boardwright_Template_GIT.kicad_wks",
            "pcbnew": "${KIPRJMOD}/Templates/Boardwright_Template_PCB_GIT_A4.kicad_wks",
        },
        "reference_rewrites": {
            "${REVISION}": "${DRAWING_REVISION}",
        },
        "deprecated_references": [
            {"location": f"{ref.path}:{ref.line}", "variable": ref.variable, "recommendation": ref.recommendation}
            for ref in _deprecated_design_references(project_root)
        ],
        "removals": [
            path.relative_to(project_root).as_posix()
            for path in sorted(legacy_paths)
        ],
        "payload_version": TEMPLATE_VERSION,
        "metadata_blockers": metadata_blockers,
        "blockers": sorted(set(blockers)),
    }


def apply_migration_plan(plan_path: Path) -> list[Path]:
    plan = read_plan(plan_path, MIGRATION_KIND)
    root = Path(str(plan["root"])).resolve()
    selected = root / str(plan["project"])
    _require_clean_migration_branch(root, plan_path)
    _validate_source_hashes(root, plan.get("source_hashes", {}))
    resolutions = plan.get("resolutions", {})
    unresolved = [
        name
        for name in ("project_name", "pcba_name", "pcb_name", "company", "designer")
        if not str(resolutions.get(name) or "").strip()
    ]
    blockers = list(plan.get("blockers", []))
    for disagreement in plan.get("metadata_blockers", []):
        field = str(disagreement).split(":")[-1].strip()
        if not str(resolutions.get(field) or "").strip():
            blockers.append(disagreement)
    blockers.extend(f"unresolved metadata: {name}" for name in unresolved)
    if blockers:
        raise BoardwrightError("Migration plan has blockers: " + "; ".join(blockers))

    backup_paths = _migration_backup_paths(root, plan, selected)
    written: list[Path] = []

    def apply() -> None:
        logo = plan.get("logo", {})
        logo_source = str(logo.get("source") or "")
        logo_target = str(logo.get("target") or "")
        if logo_source and logo_target:
            target = root / logo_target
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / logo_source, target)
            written.append(target)

        for relative in plan.get("removals", []):
            path = root / relative
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()

        written.extend(
            install_initial_payload(
                root,
                workflows=True,
                force=True,
                origin="nguyen-hierarchical",
            )
        )
        _write_migrated_config(root, resolutions, logo_target)
        _rewrite_project_worksheets(selected, plan["worksheet_rewrites"])
        written.extend(
            _rewrite_schematic_text_variables(
                root,
                plan.get("reference_rewrites", {}),
            )
        )
        written.extend(_rewrite_pcb_title_revision(root))
        # Unambiguous deprecated variables are rewritten; ambiguous ones (for
        # example ${BOARD_NAME}: PCB or PCBA?) stay and are reported for review.
        rewritten, _manual = _rewrite_deprecated_design_references(root)
        written.extend(rewritten)
        _ensure_unreleased_changelog(root / "CHANGELOG.md")
        _sync_kibot_project_metadata(root, load_config(root, local=False))
        written.extend(_embed_migrated_logo(root))

        errors = [
            issue.message
            for issue in validate_project(load_config(root))
            if issue.level == "error"
        ]
        if errors:
            raise BoardwrightError("Migrated project validation failed: " + "; ".join(errors))

    _with_rollback(root, backup_paths, apply)
    return sorted(set(written))


def migration_summary(plan: dict[str, Any]) -> str:
    candidates = plan.get("metadata_candidates", {})
    lines = [
        f"Project: {plan.get('project')}",
        f"Detected metadata fields: {len(candidates)}",
        f"Legacy paths to remove: {len(plan.get('removals', []))}",
        f"Blockers: {len(plan.get('blockers', []))}",
    ]
    unresolved = [
        key for key, value in plan.get("resolutions", {}).items() if value in {None, ""}
    ]
    if unresolved:
        lines.append("Review resolutions: " + ", ".join(unresolved))
    references = plan.get("deprecated_references", [])
    manual = [ref for ref in references if not ref["recommendation"].startswith("replace with")]
    if len(references) > len(manual):
        lines.append(f"Deprecated text variables rewritten automatically: {len(references) - len(manual)}")
    for ref in manual:
        lines.append(f"Manual review: {ref['location']} ${{{ref['variable']}}} - {ref['recommendation']}")
    lines.append(
        "Manual review: project.yaml manufacturing requirements (standard, materials, RoHS, "
        "coating, tenting) are initialised from Boardwright defaults. Check them against the "
        "last released fabrication notes before the next release."
    )
    return "\n".join(lines)


def _select_project(root: Path, projects: list[Path], selected: Path | None) -> Path:
    if selected is not None:
        path = selected if selected.is_absolute() else root / selected
        path = path.resolve()
        if path not in [candidate.resolve() for candidate in projects]:
            raise BoardwrightError(f"Selected KiCad project does not exist at the repository root: {selected}")
        return path
    if len(projects) != 1:
        choices = ", ".join(path.name for path in projects)
        raise BoardwrightError(
            "Multiple KiCad projects found; select one with --project: " + choices
        )
    return projects[0].resolve()


def _read_project_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BoardwrightError(f"Could not read KiCad project {path.name}: {exc}") from exc
    if not isinstance(data, dict):
        raise BoardwrightError(f"KiCad project must contain a JSON object: {path.name}")
    return data


def _project_text_variables(data: dict[str, Any]) -> dict[str, str]:
    values = data.get("text_variables", {})
    if not isinstance(values, dict):
        return {}
    return {str(key): str(value).strip() for key, value in values.items()}


def _legacy_kibot_definitions(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    marker = text.rfind("\ndefinitions:")
    section = text[marker:] if marker >= 0 else text
    values: dict[str, str] = {}
    for match in re.finditer(r"(?m)^  ([A-Z][A-Z0-9_]*):\s*(.*?)\s*(?:#.*)?$", section):
        value = match.group(2).strip().strip("'\"")
        values[match.group(1)] = value
    return values


def _metadata_candidates(
    root: Path,
    kicad: dict[str, str],
    kibot: dict[str, str],
) -> dict[str, list[dict[str, str]]]:
    mapping = {
        "project_name": "PROJECT_NAME",
        "pcba_name": "BOARD_NAME",
        "pcb_name": "BOARD_NAME",
        "company": "COMPANY",
        "designer": "DESIGNER",
        "git_url": "GIT_URL",
        "logo": "LOGO",
        "fabrication_notes": "FABRICATION_NOTES",
        "assembly_notes": "ASSEMBLY_NOTES",
    }
    result: dict[str, list[dict[str, str]]] = {}
    for field, legacy in mapping.items():
        entries = []
        for source, values in (("kicad", kicad), ("kibot", kibot)):
            value = str(values.get(legacy, "")).strip()
            if value and value.lower() not in _PLACEHOLDERS:
                entries.append({"source": source, "value": value})
        if field == "git_url":
            origin = remote_url(root)
            if origin:
                entries.append({"source": "git-origin", "value": origin})
        result[field] = _deduplicate_candidates(entries)
    return result


def _deduplicate_candidates(entries: list[dict[str, str]]) -> list[dict[str, str]]:
    seen = set()
    result = []
    for entry in entries:
        key = entry["value"]
        if key in seen:
            continue
        seen.add(key)
        result.append(entry)
    return result


def _initial_resolutions(
    candidates: dict[str, list[dict[str, str]]],
) -> tuple[dict[str, str | None], list[str]]:
    resolutions: dict[str, str | None] = {}
    blockers = []
    for field, entries in candidates.items():
        values = [entry["value"] for entry in entries]
        if field in {"pcba_name", "pcb_name"}:
            resolutions[field] = None
        elif len(values) == 1:
            resolutions[field] = values[0]
        elif len(values) > 1:
            resolutions[field] = None
            blockers.append(f"metadata disagreement: {field}")
        else:
            resolutions[field] = None if field in {"project_name", "company", "designer"} else ""
    resolutions["board_revision"] = "A"
    resolutions["drawing_revision"] = "A"
    return resolutions, blockers


def _candidate_value(entries: list[dict[str, str]]) -> str:
    values = [entry["value"] for entry in entries]
    return values[0] if len(values) == 1 else ""


def _legacy_paths(root: Path) -> set[Path]:
    paths: set[Path] = set()
    for relative in _LEGACY_DIRS:
        directory = root / relative
        if directory.exists():
            paths.add(directory)
            paths.update(path for path in directory.rglob("*") if path.is_file())
    paths.update((root / "Templates").glob("KDT_*.kicad_wks"))
    for relative in (
        Path(".github/workflows/ci.yaml"),
        Path("kibot_launch.sh"),
    ):
        path = root / relative
        if path.exists():
            paths.add(path)
    return paths


def _write_migrated_config(root: Path, values: dict[str, Any], logo: str) -> None:
    config_root = root / ".boardwright"
    config_root.mkdir(exist_ok=True)
    project = yaml.safe_load(DEFAULT_CONFIG_FILES["project.yaml"])
    metadata = project["project"]
    metadata.update(
        {
            "name": str(values["project_name"]).strip(),
            "pcba_name": str(values["pcba_name"]).strip(),
            "pcb_name": str(values["pcb_name"]).strip(),
            "company": str(values["company"]).strip(),
            "designer": str(values["designer"]).strip(),
            "git_url": str(values.get("git_url") or "").strip(),
            "board_revision": str(values.get("board_revision") or "A").strip(),
        }
    )
    project["documents"]["schematic"]["revision"] = str(
        values.get("drawing_revision") or "A"
    ).strip()
    project["manufacturing"]["fabrication_notes"] = str(
        values.get("fabrication_notes") or ""
    )
    project["manufacturing"]["assembly_notes"] = str(
        values.get("assembly_notes") or ""
    )
    if logo:
        project["assets"]["logo"] = logo
        # Only a PNG can be embedded in a KiCad worksheet; any other format
        # stays the README logo and the worksheets keep their own image.
        if _is_png(config_root.parent / logo):
            project["template"]["sheet_image"] = logo
    _write_yaml(config_root / "project.yaml", project)

    for filename in (
        "branches.yaml",
        "revision_history.yaml",
        "document_revisions.yaml",
    ):
        path = config_root / filename
        if not path.exists():
            path.write_text(DEFAULT_CONFIG_FILES[filename], encoding="utf-8", newline="\n")

    legal = yaml.safe_load(DEFAULT_CONFIG_FILES["legal.yaml"])
    defaults = legal["downstream_project_defaults"]
    legal["legal_profiles"]["migrated-project"] = {
        **legal["legal_profiles"]["public-hardware"],
        "hardware_design_license": "MIT",
        "copyright_holder": str(values["company"]).strip(),
    }
    defaults["profile"] = "migrated-project"
    defaults["hardware_design_license"] = "MIT"
    defaults["copyright_holder"] = str(values["company"]).strip()
    _write_yaml(config_root / "legal.yaml", legal)


def _is_png(path: Path) -> bool:
    from .worksheet_logo import PNG_SIGNATURE

    try:
        with path.open("rb") as handle:
            return handle.read(8) == PNG_SIGNATURE
    except OSError:
        return False


def _embed_migrated_logo(root: Path) -> list[Path]:
    """Carry the project's logo into the Boardwright worksheets it now renders."""
    from .worksheet_logo import embed_logo_in_worksheets, select_logo_targets

    from .drawing_sheets import configured_logo

    config = load_config(root, local=False)
    image = configured_logo(config) if str(config.template.get("sheet_image") or "").strip() else ""
    if not image:
        return []
    logo = root / image
    return [result.worksheet for result in embed_logo_in_worksheets(select_logo_targets(config, logo), logo)]


def _rewrite_project_worksheets(path: Path, rewrites: dict[str, str]) -> None:
    data = _read_project_json(path)
    data = _replace_legacy_project_identifier(data, path.stem)
    for section in ("schematic", "pcbnew"):
        settings = data.setdefault(section, {})
        if not isinstance(settings, dict):
            raise BoardwrightError(f"KiCad project section `{section}` must be an object.")
        settings["page_layout_descr_file"] = str(rewrites[section])
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _rewrite_schematic_text_variables(
    root: Path,
    rewrites: dict[str, str],
) -> list[Path]:
    written: list[Path] = []
    for path in sorted(root.glob("*.kicad_sch")):
        text = path.read_text(encoding="utf-8")
        updated = text
        for source, target in rewrites.items():
            updated = updated.replace(str(source), str(target))
        if updated == text:
            continue
        path.write_text(updated, encoding="utf-8", newline="\n")
        written.append(path)
    return written


def _design_files(root: Path) -> list[Path]:
    # Worksheets are replaced by Boardwright worksheets during migration, so
    # only schematics and PCBs are scanned and rewritten here.
    return sorted(root.glob("*.kicad_sch")) + sorted(root.glob("*.kicad_pcb"))


def _deprecated_design_references(root: Path):
    from .deprecated_variables import find_deprecated_references

    return find_deprecated_references(root, _design_files(root))


def _rewrite_deprecated_design_references(root: Path):
    from .deprecated_variables import rewrite_unambiguous

    return rewrite_unambiguous(root, _design_files(root))


PCB_TITLE_REVISION_REWRITES = {
    b'(rev "${REVISION}")': b'(rev "${PCB_REVISION}")',
    b'(rev "${DRAWING_REVISION}")': b'(rev "${PCB_REVISION}")',
}


def _rewrite_pcb_title_revision(root: Path) -> list[Path]:
    """Point PCB title-block revisions at the physical PCB revision.

    Only the title-block ``rev`` token is replaced, byte-for-byte, so line
    endings and the rest of the authored board file are untouched.
    """

    written: list[Path] = []
    for path in sorted(root.glob("*.kicad_pcb")):
        data = path.read_bytes()
        updated = data
        for source, target in PCB_TITLE_REVISION_REWRITES.items():
            updated = updated.replace(source, target, 1)
        if updated != data:
            path.write_bytes(updated)
            written.append(path)
    return written


def _replace_legacy_project_identifier(value: Any, project_stem: str) -> Any:
    if isinstance(value, dict):
        return {
            key: _replace_legacy_project_identifier(item, project_stem)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _replace_legacy_project_identifier(item, project_stem)
            for item in value
        ]
    if isinstance(value, str):
        return value.replace("KDT_Hierarchical_KiBot", project_stem)
    return value


def _ensure_unreleased_changelog(path: Path) -> None:
    if not path.exists():
        path.write_text("# Changelog\n\n## [Unreleased]\n", encoding="utf-8", newline="\n")
        return
    text = path.read_text(encoding="utf-8")
    if any(release.name == "Unreleased" for release in parse_releases(text)):
        return
    heading_end = text.find("\n")
    if heading_end < 0:
        heading_end = len(text)
    updated = text[:heading_end].rstrip() + "\n\n## [Unreleased]\n\n" + text[heading_end:].lstrip()
    path.write_text(updated, encoding="utf-8", newline="\n")


def _require_clean_migration_branch(root: Path, plan_path: Path) -> None:
    branch = current_branch(root)
    if branch in {"main", "master", "detached"}:
        raise BoardwrightError(
            f"Migration must run on a non-release branch, not {branch}."
        )
    completed = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    if completed.returncode != 0:
        raise BoardwrightError("Could not inspect Git working tree.")
    ignored = ""
    try:
        ignored = plan_path.resolve().relative_to(root).as_posix()
    except ValueError:
        pass
    dirty = []
    for line in completed.stdout.splitlines():
        name = line[3:].strip().replace("\\", "/")
        if name != ignored:
            dirty.append(line)
    if dirty:
        raise BoardwrightError("Migration requires a clean working tree.")


def _validate_source_hashes(root: Path, hashes: dict[str, str]) -> None:
    for relative, expected in hashes.items():
        path = root / relative
        if not path.is_file() or _file_hash(path) != expected:
            raise BoardwrightError(f"Source changed after migration planning: {relative}")


def _migration_backup_paths(root: Path, plan: dict[str, Any], project: Path) -> list[Path]:
    paths = {
        root / ".boardwright",
        root / ".github/workflows",
        root / "src",
        root / "boardwright_resources",
        root / "Templates",
        root / "scripts",
        root / "assets",
        root / "pyproject.toml",
        root / "THIRD_PARTY_NOTICES.md",
        root / "CHANGELOG.md",
        project,
    }
    paths.update(root.glob("*.kicad_sch"))
    paths.update(root.glob("*.kicad_pcb"))
    paths.update(root / relative for relative in plan.get("removals", []))
    return _top_level_paths(paths)


def _top_level_paths(paths: set[Path]) -> list[Path]:
    result = []
    for path in sorted(paths, key=lambda item: len(item.parts)):
        if any(parent == path or parent in path.parents for parent in result):
            continue
        result.append(path)
    return result


def _with_rollback(root: Path, paths: list[Path], action: Callable[[], None]) -> None:
    temporary = root / f".boardwright-migration-rollback-{uuid.uuid4().hex}"
    temporary.mkdir()
    records: list[tuple[Path, Path | None]] = []
    try:
        for index, path in enumerate(paths):
            backup = None
            if path.exists():
                backup = temporary / str(index)
                if path.is_dir():
                    shutil.copytree(path, backup)
                else:
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, backup)
            records.append((path, backup))
        action()
    except Exception:
        for path, backup in reversed(records):
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            elif path.exists():
                path.unlink()
            if backup:
                path.parent.mkdir(parents=True, exist_ok=True)
                if backup.is_dir():
                    shutil.copytree(backup, path)
                else:
                    shutil.copy2(backup, path)
        raise
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
        newline="\n",
    )
