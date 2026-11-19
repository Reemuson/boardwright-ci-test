# SPDX-License-Identifier: LicenseRef-Boardwright-Source-Available
# Copyright (c) Ryan Hicks

"""Active KiCad drawing sheets (worksheets): resolution, branding and adoption.

Boardwright treats an external ``.kicad_wks`` file, referenced from the KiCad
project as ``${KIPRJMOD}/Templates/<name>.kicad_wks``, as the authoritative
drawing sheet. Verified with kicad-cli 9.0.6 and 10.0.1: that setting is used
for schematic and PCB exports even when the design file still carries an
embedded copy; a bare relative path is *not* resolved for PCB exports.

A ``kicad-embed://`` setting makes KiCad render the copy stored inside the
``.kicad_sch``/``.kicad_pcb``. Boardwright's logo, update and fingerprint tools
act on external files, so an embedded sheet is reported and can be adopted
(switched to the external file) only when the two are provably identical.

Documents and the sheet they render with:

* ``schematic``: the ``.kicad_pro`` ``schematic`` page layout.
* ``fabrication`` / ``assembly``: the ``.kicad_pro`` ``pcbnew`` page layout,
  else ``template.worksheet`` (KiBot ``SHEET_WKS``; see ``kicad_tables``).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .errors import BoardwrightError

EMBED_PREFIX = "kicad-embed://"
KIPRJMOD_PREFIX = "${KIPRJMOD}/"
DEFAULT_PCB_SHEET = "Templates/Boardwright_Template_PCB_GIT_A4.kicad_wks"
DOCUMENTS = ("schematic", "fabrication", "assembly")
_SECTION_FOR_DOCUMENT = {"schematic": "schematic", "fabrication": "pcbnew", "assembly": "pcbnew"}

# Drawing variables whose values are part of a drawing's controlled
# presentation. Revision, release, date, Git and revision-table variables are
# excluded: they are governed by the release model itself and change with
# every issue by design.
STABLE_DRAWING_VARIABLES = (
    "COMPANY",
    "DESIGNER",
    "PROJECT_NUMBER",
    "PCBA_NAME",
    "PCB_NAME",
    "DOCUMENT_TYPE",
    "DRAWING_TITLE",
    "DRAWING_NUMBER",
    "DRAWN_BY",
    "DRAWN_DATE",
    "SHEET_LEGAL_NOTICE",
)

ReadText = Callable[[str], "str | None"]


@dataclass(frozen=True)
class SheetRef:
    """A KiCad project page-layout setting, parsed."""

    section: str  # "schematic" or "pcbnew"
    setting: str  # raw .kicad_pro value ("" when unset)
    kind: str  # "file", "embedded", "relative", "absolute" or "unset"
    path: str  # project-relative POSIX path of the external file ("" if none)
    embedded_name: str = ""

    @property
    def counterpart(self) -> str:
        """The Templates/ file an embedded sheet corresponds to."""
        return f"Templates/{self.embedded_name}" if self.embedded_name else ""


def parse_setting(section: str, value: str) -> SheetRef:
    raw = str(value or "").strip()
    normalized = raw.replace("\\", "/")
    if not normalized:
        return SheetRef(section, raw, "unset", "")
    if normalized.startswith(EMBED_PREFIX):
        name = normalized.removeprefix(EMBED_PREFIX).lstrip("/")
        return SheetRef(section, raw, "embedded", "", embedded_name=name)
    if normalized.startswith(KIPRJMOD_PREFIX):
        return SheetRef(section, raw, "file", normalized.removeprefix(KIPRJMOD_PREFIX))
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:/", normalized):
        return SheetRef(section, raw, "absolute", normalized)
    return SheetRef(section, raw, "relative", normalized.lstrip("./"))


def kicad_project_name(files: list[str]) -> str:
    projects = sorted(name for name in files if name.endswith(".kicad_pro") and "/" not in name)
    return projects[0] if projects else ""


def project_sheet_refs(read_text: ReadText, files: list[str]) -> dict[str, SheetRef]:
    """The schematic and pcbnew page-layout settings of the root KiCad project."""
    project_name = kicad_project_name(files)
    data: Any = {}
    if project_name:
        try:
            data = json.loads(read_text(project_name) or "{}")
        except json.JSONDecodeError:
            data = {}
    refs: dict[str, SheetRef] = {}
    for section in ("schematic", "pcbnew"):
        settings = data.get(section) if isinstance(data, dict) else None
        value = (settings or {}).get("page_layout_descr_file") if isinstance(settings, dict) else ""
        refs[section] = parse_setting(section, str(value or ""))
    return refs


def document_sheet(document: str, refs: dict[str, SheetRef], template_worksheet: str = "") -> SheetRef:
    """The sheet a document is rendered with (mirrors the build's SHEET_WKS)."""
    if document not in DOCUMENTS:
        raise BoardwrightError(f"Unknown drawing document {document!r}.")
    ref = refs.get(_SECTION_FOR_DOCUMENT[document])
    if ref is not None and ref.kind != "unset":
        return ref
    if document == "schematic":
        return ref or SheetRef("schematic", "", "unset", "")
    fallback = str(template_worksheet or "").strip() or DEFAULT_PCB_SHEET
    parsed = parse_setting("pcbnew", fallback if fallback.startswith(("${", EMBED_PREFIX)) else KIPRJMOD_PREFIX + fallback)
    return parsed


def design_file_for(section: str, files: list[str]) -> str:
    suffix = ".kicad_sch" if section == "schematic" else ".kicad_pcb"
    project = kicad_project_name(files)
    if project:
        candidate = project[: -len(".kicad_pro")] + suffix
        if candidate in files:
            return candidate
    roots = sorted(name for name in files if name.endswith(suffix) and "/" not in name)
    return roots[0] if roots else ""


# --- Embedded worksheets ----------------------------------------------------


def embedded_record(design_text: str, name: str) -> str | None:
    """The whitespace-normalised ``(data ...)`` record of an embedded file.

    Deterministic without decompression, so it can be fingerprinted on any
    Python version.
    """
    block = _embedded_file_block(design_text, name)
    if block is None:
        return None
    match = re.search(r"\(data\s+(.*?)\)", block, re.S)
    return "".join(match.group(1).split()) if match else None


def embedded_worksheet_bytes(design_text: str, name: str) -> bytes | None:
    """Decompressed content of an embedded worksheet.

    Returns None when the file is absent. Raises BoardwrightError when it is
    present but cannot be decoded (no zstd support), so callers fail safe.
    """
    record = embedded_record(design_text, name)
    if record is None:
        return None
    payload = base64.b64decode(record.strip("|"))
    return _zstd_decompress(payload)


def _zstd_decompress(payload: bytes) -> bytes:
    try:
        from compression import zstd  # Python 3.14+

        return zstd.decompress(payload)
    except ImportError:
        pass
    try:
        import zstandard  # type: ignore[import-not-found]

        return zstandard.ZstdDecompressor().decompress(payload, max_output_size=64 * 1024 * 1024)
    except ImportError as exc:
        raise BoardwrightError(
            "Cannot decode an embedded KiCad worksheet: zstd support is unavailable "
            "(Python 3.14+ or the `zstandard` package is required)."
        ) from exc


def _embedded_file_block(text: str, name: str) -> str | None:
    for match in re.finditer(r'\(file\s+\(name "([^"]+)"\)', text):
        if match.group(1) != name:
            continue
        start = match.start()
        depth = 0
        in_string = False
        index = start
        while index < len(text):
            char = text[index]
            if in_string:
                if char == "\\":
                    index += 1
                elif char == '"':
                    in_string = False
            elif char == '"':
                in_string = True
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    return text[start : index + 1]
            index += 1
        return None
    return None


# --- Fingerprints -----------------------------------------------------------


def normalise_sheet_text(text: str) -> str:
    return text.replace("\r\n", "\n")


def referenced_variables(sheet_text: str) -> set[str]:
    return set(re.findall(r"\$\{([A-Za-z0-9_]+)\}", sheet_text))


def sheet_content(read_text: ReadText, files: list[str], ref: SheetRef) -> tuple[str, str | None]:
    """(identity, text) of the sheet a reference renders.

    ``identity`` is what is fingerprinted; ``text`` is the readable sheet when
    available (used to restrict variables to those the sheet draws).
    """
    if ref.kind in ("file", "relative"):
        text = read_text(ref.path)
        if text is None:
            return f"missing:{ref.path}", None
        text = normalise_sheet_text(text)
        return "file:" + text, text
    if ref.kind == "embedded":
        design = design_file_for(ref.section, files)
        record = embedded_record(read_text(design) or "", ref.embedded_name) if design else None
        if record is None:
            return f"missing-embedded:{ref.embedded_name}", None
        try:
            text = normalise_sheet_text(embedded_worksheet_bytes(read_text(design) or "", ref.embedded_name).decode("utf-8"))
        except (BoardwrightError, UnicodeDecodeError, ValueError):
            text = None
        return "embedded:" + record, text
    if ref.kind == "absolute":
        return f"absolute:{ref.path}", None
    return "unset", None


def document_sheet_fingerprints(read_text: ReadText, files: list[str], project_yaml: str | None) -> dict[str, str]:
    """Hash of each drawing's effective sheet (logo included) and stable variables."""
    from .config import BoardwrightConfig
    from .kicad_variables import _render_project_text

    data: Any = {}
    if project_yaml:
        try:
            import yaml

            data = yaml.safe_load(project_yaml) or {}
        except Exception:  # pragma: no cover - malformed YAML is reported elsewhere
            data = {}
    if not isinstance(data, dict):
        data = {}
    config = BoardwrightConfig(root=Path("."), project=data, branches={}, legal={}, revision_history={})
    template = data.get("template", {}) if isinstance(data.get("template"), dict) else {}
    refs = project_sheet_refs(read_text, files)
    result: dict[str, str] = {}
    for document in DOCUMENTS:
        ref = document_sheet(document, refs, str(template.get("worksheet") or ""))
        identity, text = sheet_content(read_text, files, ref)
        variables = dict(config.drawing_variables(document))
        project_section = data.get("project", {}) if isinstance(data.get("project"), dict) else {}
        variables["SHEET_LEGAL_NOTICE"] = _render_project_text(config.sheet_legal_notice, project_section)
        keys: tuple[str, ...] = STABLE_DRAWING_VARIABLES
        if text is None and not identity.startswith("embedded:"):
            # No sheet is rendered (unset or missing): nothing is drawn.
            keys = ()
        elif text is not None:
            drawn = referenced_variables(text)
            keys = tuple(key for key in STABLE_DRAWING_VARIABLES if key in drawn)
        payload = {
            "sheet": identity,
            "variables": {key: str(variables.get(key, "")) for key in keys},
        }
        result[document] = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
    return result


# --- Working-tree views ------------------------------------------------------


def _tree_reader(root: Path) -> tuple[ReadText, list[str]]:
    files = [path.name for path in root.iterdir() if path.is_file()]

    def read_text(relative: str) -> str | None:
        path = root / relative
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    return read_text, files


def active_sheet_refs(root: Path, template_worksheet: str = "") -> dict[str, SheetRef]:
    read_text, files = _tree_reader(root)
    refs = project_sheet_refs(read_text, files)
    return {document: document_sheet(document, refs, template_worksheet) for document in DOCUMENTS}


def active_sheet_files(root: Path, template_worksheet: str = "") -> list[Path]:
    """External worksheet files that the project's drawings render with."""
    found: list[Path] = []
    for ref in active_sheet_refs(root, template_worksheet).values():
        if ref.kind in ("file", "relative") and ref.path:
            path = (root / ref.path).resolve()
            if path.is_file() and path not in found:
                found.append(path)
        elif ref.kind == "absolute":
            path = Path(ref.path)
            if path.is_file() and path not in found:
                found.append(path)
    return found


@dataclass(frozen=True)
class SheetIssue:
    level: str  # "error" or "warning"
    message: str


def sheet_issues(root: Path, template_worksheet: str = "") -> list[SheetIssue]:
    """Problems with how the project's drawing sheets are referenced."""
    read_text, files = _tree_reader(root)
    refs = project_sheet_refs(read_text, files)
    issues: list[SheetIssue] = []
    for section, ref in refs.items():
        if ref.kind == "embedded":
            issues.append(
                SheetIssue(
                    "warning",
                    f"{section} renders the worksheet embedded in the design file ({ref.setting}). "
                    "Boardwright logo, update and fingerprint tools act on external worksheets; run "
                    "`boardwright worksheet adopt` to switch to Templates/ when they are identical.",
                )
            )
        elif ref.kind == "relative":
            issues.append(
                SheetIssue(
                    "error" if section == "pcbnew" else "warning",
                    f"{section} worksheet {ref.setting!r} is a bare relative path; KiCad PCB exports do not "
                    f"resolve it. Use {KIPRJMOD_PREFIX}{ref.path}.",
                )
            )
        elif ref.kind == "file" and not (root / ref.path).is_file():
            issues.append(SheetIssue("error", f"{section} worksheet {ref.path} does not exist."))
    for document in ("fabrication", "assembly"):
        ref = document_sheet(document, refs, template_worksheet)
        if ref.section == "pcbnew" and ref.kind == "file" and not (root / ref.path).is_file() and refs["pcbnew"].kind == "unset":
            issues.append(SheetIssue("error", f"{document} drawing worksheet {ref.path} does not exist."))
            break
    return issues


# --- Branding ----------------------------------------------------------------


BOARDWRIGHT_DEFAULT_SHEET_IMAGE = "assets/logos/logo-block-black.png"


def configured_logo(config: Any) -> str:
    """The project's worksheet logo, or "" when it uses Boardwright's own.

    The shipped default (``assets/logos/logo-block-black.png``) is
    Boardwright branding, not a project logo: when it is configured and either
    absent (downstream projects do not ship it) or the Boardwright image
    itself, no project logo is configured.
    """
    from .worksheet_logo import BOARDWRIGHT_PLACEHOLDER_LOGO_SHA256

    value = str(config.template_sheet_image or "").strip()
    if value.replace("\\", "/") == BOARDWRIGHT_DEFAULT_SHEET_IMAGE:
        path = Path(config.root) / value
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() == BOARDWRIGHT_PLACEHOLDER_LOGO_SHA256:
            return ""
    return value


def branding_issues(config: Any) -> list[SheetIssue]:
    """A configured project logo must be what every active drawing renders.

    Never falls back silently: a missing or unreadable configured logo, or an
    active sheet that embeds something else, is an error.
    """
    from .worksheet_logo import BOARDWRIGHT_PLACEHOLDER_LOGO_SHA256, embedded_png, png_dimensions

    declared = configured_logo(config)
    if not declared:
        return []
    template = getattr(config, "template", {}) or {}
    # An explicit template.sheet_image must be exactly what renders. A logo
    # inherited from template.logo/assets.logo (also the README logo) may
    # differ from a dedicated worksheet image, but never be replaced by
    # Boardwright's own logo.
    explicit = bool(str(template.get("sheet_image") or "").strip())
    root: Path = config.root
    logo_path = Path(declared) if Path(declared).is_absolute() else root / declared
    source = "template.sheet_image" if explicit else "template.logo/assets.logo"
    if not logo_path.is_file():
        return [
            SheetIssue(
                "error" if explicit else "warning",
                f"Configured worksheet logo {declared} ({source}) is missing. Commit it to the project, or "
                "point template.sheet_image at an available PNG. Boardwright does not fall back to its own logo.",
            )
        ]
    logo = logo_path.read_bytes()
    try:
        png_dimensions(logo)
    except BoardwrightError as exc:
        if not explicit:
            return [
                SheetIssue(
                    "warning",
                    f"Project logo {declared} cannot be embedded in worksheets ({exc}); set "
                    "template.sheet_image to a PNG to brand the drawings.",
                )
            ]
        return [SheetIssue("error", f"Configured worksheet logo {declared} is not usable: {exc}")]

    issues: list[SheetIssue] = []
    read_text, files = _tree_reader(root)
    refs = project_sheet_refs(read_text, files)
    seen: set[str] = set()
    for document in DOCUMENTS:
        ref = document_sheet(document, refs, str(template.get("worksheet") or ""))
        label = ref.path or ref.setting
        if label in seen:
            continue
        seen.add(label)
        if ref.kind in ("file", "relative", "absolute"):
            path = Path(ref.path) if ref.kind == "absolute" else root / ref.path
            if not path.is_file():
                continue  # reported by sheet_issues
            embedded = embedded_png(path.read_text(encoding="utf-8"))
        elif ref.kind == "embedded":
            design = design_file_for(ref.section, files)
            try:
                content = embedded_worksheet_bytes(read_text(design) or "", ref.embedded_name) if design else None
            except BoardwrightError as exc:
                issues.append(SheetIssue("error", f"{ref.setting}: cannot verify its logo ({exc})"))
                continue
            if content is None:
                issues.append(SheetIssue("error", f"{ref.setting} is not embedded in {design or 'the design file'}."))
                continue
            embedded = embedded_png(content.decode("utf-8", "replace"))
        else:
            continue
        if embedded is None or embedded == logo:
            continue
        where = label if ref.kind != "embedded" else f"{ref.setting} (embedded in {design_file_for(ref.section, files)})"
        if hashlib.sha256(embedded).hexdigest() == BOARDWRIGHT_PLACEHOLDER_LOGO_SHA256:
            detail = "still renders the Boardwright logo"
        elif explicit:
            detail = "renders a different logo"
        else:
            continue
        fix = (
            "Run `boardwright worksheet adopt`, then `boardwright worksheet-logo`."
            if ref.kind == "embedded"
            else "Run `boardwright worksheet-logo` to embed the configured logo."
        )
        issues.append(
            SheetIssue("error", f"{where} {detail}, but the project configures {declared}. {fix}")
        )
    return issues


# --- Adoption -----------------------------------------------------------------


@dataclass(frozen=True)
class AdoptionResult:
    section: str
    setting: str
    action: str  # "adopted", "identical-already-file", "diverged", "missing", "unverifiable", "skipped"
    detail: str


def plan_adoption(root: Path) -> list[AdoptionResult]:
    """Which embedded sheets can switch to their Templates/ file, without writing."""
    read_text, files = _tree_reader(root)
    refs = project_sheet_refs(read_text, files)
    results: list[AdoptionResult] = []
    for section, ref in refs.items():
        if ref.kind != "embedded":
            results.append(AdoptionResult(section, ref.setting, "skipped", f"not embedded ({ref.kind})"))
            continue
        counterpart = root / ref.counterpart
        design = design_file_for(section, files)
        if not counterpart.is_file():
            results.append(AdoptionResult(section, ref.setting, "missing", f"{ref.counterpart} does not exist"))
            continue
        try:
            content = embedded_worksheet_bytes(read_text(design) or "", ref.embedded_name) if design else None
        except BoardwrightError as exc:
            results.append(AdoptionResult(section, ref.setting, "unverifiable", str(exc)))
            continue
        if content is None:
            results.append(AdoptionResult(section, ref.setting, "missing", f"not embedded in {design}"))
            continue
        embedded_text = normalise_sheet_text(content.decode("utf-8", "replace"))
        file_text = normalise_sheet_text(counterpart.read_text(encoding="utf-8"))
        if embedded_text.rstrip("\n") != file_text.rstrip("\n"):
            results.append(
                AdoptionResult(
                    section,
                    ref.setting,
                    "diverged",
                    f"the copy embedded in {design} differs from {ref.counterpart}; left unchanged. Review with "
                    "`boardwright worksheet extract`.",
                )
            )
            continue
        results.append(
            AdoptionResult(section, ref.setting, "adopted", f"{KIPRJMOD_PREFIX}{ref.counterpart} (identical to {design})")
        )
    return results


def apply_adoption(root: Path, results: list[AdoptionResult]) -> list[Path]:
    """Switch adopted sections of the .kicad_pro to the external file.

    Only the page-layout setting changes; design files are not rewritten.
    """
    adopted = {result.section: result.detail.split(" ", 1)[0] for result in results if result.action == "adopted"}
    if not adopted:
        return []
    read_text, files = _tree_reader(root)
    project_name = kicad_project_name(files)
    if not project_name:
        return []
    path = root / project_name
    text = path.read_text(encoding="utf-8")
    updated = text
    for section, value in adopted.items():
        current = project_sheet_refs(read_text, files)[section].setting
        # Replace only this section's occurrence, preserving the file's formatting.
        data = json.loads(updated)
        if data.get(section, {}).get("page_layout_descr_file") != current:
            raise BoardwrightError(f"{project_name} changed while adopting worksheets; nothing written.")
        pattern = re.compile(
            r'("' + re.escape(section) + r'"\s*:\s*\{)(.*?)("page_layout_descr_file"\s*:\s*)"' + re.escape(current) + '"',
            re.S,
        )
        updated, count = pattern.subn(lambda m: m.group(1) + m.group(2) + m.group(3) + json.dumps(value), updated, count=1)
        if count != 1 or json.loads(updated).get(section, {}).get("page_layout_descr_file") != value:
            raise BoardwrightError(f"Could not update the {section} worksheet setting in {project_name}.")
    path.write_text(updated, encoding="utf-8", newline="\n")
    return [path]


def extract_embedded(root: Path, out_dir: Path) -> list[Path]:
    """Write embedded worksheets to ``out_dir`` for review (never into Templates/)."""
    read_text, files = _tree_reader(root)
    written: list[Path] = []
    for section, ref in project_sheet_refs(read_text, files).items():
        if ref.kind != "embedded":
            continue
        design = design_file_for(section, files)
        content = embedded_worksheet_bytes(read_text(design) or "", ref.embedded_name) if design else None
        if content is None:
            continue
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / f"{section}__{ref.embedded_name}"
        target.write_bytes(content)
        written.append(target)
    return written


# --- CLI ------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m boardwright.drawing_sheets")
    parser.add_argument("command", choices=("check",), help="check: fail if sheets or branding are inconsistent")
    args = parser.parse_args(argv)
    from .config import load_config

    config = load_config(local=False)
    issues = sheet_issues(config.root, str(config.template.get("worksheet") or "")) + branding_issues(config)
    for issue in issues:
        print(f"{issue.level}: {issue.message}")
    errors = [issue for issue in issues if issue.level == "error"]
    if args.command == "check":
        if errors:
            print(f"Drawing sheet check failed ({len(errors)} error(s)).")
            return 1
        print("Drawing sheets and branding are consistent.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
