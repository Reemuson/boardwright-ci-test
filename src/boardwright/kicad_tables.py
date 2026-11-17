# SPDX-License-Identifier: LicenseRef-Boardwright-Source-Available
# Copyright (c) Ryan Hicks

"""Prepare KiCad PCB table placeholders before KiBot renders PDFs."""

from __future__ import annotations

import csv
import io
import re
import textwrap
import uuid
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .config import _worksheet_from_kicad_project, load_config
from .errors import BoardwrightError
from .git_ops import remote_url
from .kicad_variables import sync_kicad_project_text_variables


COMPONENT_COUNT_TEXT_BOX_UUID = "511273fd-c939-4feb-bf05-ae1b43c3644e"
COMPONENT_COUNT_RECT_UUID = "8cb5a7ba-335d-4917-9b0b-efa4a7d38e40"
IMPEDANCE_TABLE_TEXT_BOX_UUID = "9af73f77-a717-4896-ac91-e0684a71d0ea"
IMPEDANCE_TABLE_TEMPLATE = Path(
    "boardwright_resources/kibot/resources/templates/impedance_table.txt"
)
FABRICATION_NOTES_TEMPLATE = Path(
    "boardwright_resources/kibot/resources/templates/fabrication_notes.txt"
)
ASSEMBLY_NOTES_TEMPLATE = Path(
    "boardwright_resources/kibot/resources/templates/assembly_notes.txt"
)
KIBOT_MAIN_YAML = Path("boardwright_resources/kibot/yaml/kibot_main.yaml")
DOCUMENT_KIBOT_CONFIGS = {
    "assembly": Path("boardwright_resources/kibot/yaml/kibot_document_assembly.generated.yaml"),
    "fabrication": Path("boardwright_resources/kibot/yaml/kibot_document_fabrication.generated.yaml"),
}
GENERATED_WORKSHEETS = Path(".boardwright/generated_worksheets")
PCB_PAGE_TITLE_WORKSHEET = GENERATED_WORKSHEETS / "pcb_page_title.kicad_wks"
GENERATED_TABLE_NAMESPACE = uuid.UUID("23d77107-9438-4a74-a20c-c4df6c5126dd")


@dataclass(frozen=True)
class ComponentCountResult:
    csv_path: Path
    total: int
    rows: tuple[tuple[str, int, int, int], ...]


def prepare_pcb_tables(root: Path) -> ComponentCountResult:
    root = root.resolve()
    config = _load_project_config(root)
    _sync_kibot_project_metadata(root, config)
    counts = _collect_component_mount_counts(root)
    rows = _component_mount_rows(counts)
    total = rows[-1][-1] if rows else 0

    csv_path = _write_component_count_csv(root, rows, total)
    _fill_component_count_placeholder(root, rows, total)
    _write_impedance_table(root, config)
    _write_manufacturing_notes(root, config)
    _write_readme_summaries(root, config, rows)
    _write_readme_badges(root, config)

    return ComponentCountResult(csv_path=csv_path, total=total, rows=rows)


def _load_project_config(root: Path):
    try:
        return load_config(root)
    except BoardwrightError:
        return None


def _sync_kibot_project_metadata(root: Path, config=None) -> None:
    if config is None:
        config = _load_project_config(root)
    if config is None:
        return

    project = config.project.get("project", {})
    git_url = str(project.get("git_url") or "").strip()
    if not git_url and config.github_repo:
        git_url = f"https://github.com/{config.github_repo}"
    if not git_url:
        origin_url = remote_url(root)
        git_url = origin_url if origin_url else git_url

    replacements = {
        **config.drawing_variables("schematic"),
        "LOGO": str(config.assets.get("logo") or ""),
        "TEMPLATE_LOGO": config.template_logo,
        "SHEET_LEGAL_NOTICE": _render_project_text(config.sheet_legal_notice, project),
        "SHEET_WKS": _worksheet_definition(_pcb_worksheet(config.root, config.worksheet)),
        "GIT_URL": git_url,
    }
    sync_kicad_project_text_variables(config, "schematic", replacements)

    path = root / KIBOT_MAIN_YAML
    if not path.is_file():
        return
    text = path.read_text(encoding="utf-8")
    updated = text
    for key, value in replacements.items():
        replacement_line = f"  {key}: {_yaml_quote(value)}"
        pattern = rf"(?m)^  {re.escape(key)}:\s*.*$"
        if re.search(pattern, updated):
            updated = re.sub(pattern, replacement_line, updated, count=1)
        else:
            updated = _insert_kibot_definition(updated, key, replacement_line)
    if updated != text:
        path.write_text(updated, encoding="utf-8", newline="\n")
    _write_document_kibot_configs(root, updated, config, replacements)


def _write_document_kibot_configs(root: Path, main_text: str, config, base_replacements: dict[str, str]) -> None:
    for document_key, relative_path in DOCUMENT_KIBOT_CONFIGS.items():
        replacements = dict(base_replacements)
        replacements.update(config.drawing_variables(document_key))
        page_title_worksheet = _pcb_page_title_worksheet_definition(root, replacements.get("SHEET_WKS", ""))
        if page_title_worksheet:
            replacements["SHEET_WKS"] = page_title_worksheet
        updated = main_text
        for key, value in replacements.items():
            replacement_line = f"  {key}: {_yaml_quote(value)}"
            pattern = rf"(?m)^  {re.escape(key)}:\s*.*$"
            if re.search(pattern, updated):
                updated = re.sub(pattern, replacement_line, updated, count=1)
            else:
                updated = _insert_kibot_definition(updated, key, replacement_line)
        path = root / relative_path
        path.write_text(updated, encoding="utf-8", newline="\n")


def _pcb_page_title_worksheet_definition(root: Path, source_value: str) -> str:
    source = _resolve_worksheet_source(root, source_value)
    if not source.is_file():
        return ""
    target = root / PCB_PAGE_TITLE_WORKSHEET
    target.parent.mkdir(parents=True, exist_ok=True)
    text = source.read_text(encoding="utf-8")
    text = text.replace("${DRAWING_TITLE}", "${TITLE}")
    target.write_text(text, encoding="utf-8", newline="\n")
    return "${KIPRJMOD}/" + target.relative_to(root).as_posix()


def _resolve_worksheet_source(root: Path, value: str) -> Path:
    normalized = str(value or "").strip().strip("'").strip('"').replace("\\", "/")
    if normalized.startswith("${KIPRJMOD}/"):
        normalized = normalized.removeprefix("${KIPRJMOD}/")
    elif normalized.startswith("kicad-embed://"):
        normalized = (Path("Templates") / normalized.removeprefix("kicad-embed://").lstrip("/")).as_posix()
    path = Path(normalized)
    return path if path.is_absolute() else root / path


def _insert_kibot_definition(text: str, key: str, line: str) -> str:
    anchors = {
        "PCBA_NAME": "PROJECT_NUMBER",
        "PCB_NAME": "PCBA_NAME",
        "DOCUMENT_TYPE": "BOARD_REVISION",
        "DRAWING_TITLE": "DOCUMENT_TYPE",
        "DRAWING_NUMBER": "DRAWING_TITLE",
        "DRAWING_REVISION": "DRAWING_NUMBER",
        "RELEASE_VERSION": "DRAWING_REVISION",
        "RELEASE_DATE": "RELEASE_VERSION",
        "DRAWN_BY": "RELEASE_DATE",
        "DRAWN_DATE": "DRAWN_BY",
    }
    anchor = anchors.get(key, "BOARD_REVISION")
    match = re.search(rf"(?m)^  {re.escape(anchor)}:.*$", text)
    if not match:
        return text
    return text[: match.end()] + "\n" + line + text[match.end():]


def _collect_component_mount_counts(root: Path) -> Counter[tuple[str, str]]:
    counts: Counter[tuple[str, str]] = Counter()
    pcb = _pcb_path(root).read_text(encoding="utf-8")
    for block in _iter_sexpr_blocks(pcb, "footprint"):
        if _footprint_excluded_from_count(block):
            continue
        side = _footprint_side(block)
        mount = _footprint_mount(block)
        counts[(mount, side)] += 1
    return counts


def _component_mount_rows(
    counts: Counter[tuple[str, str]]
) -> tuple[tuple[str, int, int, int], ...]:
    rows: list[tuple[str, int, int, int]] = []
    for mount, label in (("THT", "THT"), ("SMD", "SMT")):
        front = counts[(mount, "Frontside")]
        back = counts[(mount, "Backside")]
        rows.append((label, front, back, front + back))

    total_front = sum(row[1] for row in rows)
    total_back = sum(row[2] for row in rows)
    rows.append(("Total", total_front, total_back, total_front + total_back))
    return tuple(rows)


def _footprint_excluded_from_count(block: str) -> bool:
    return "(exclude_from_bom)" in block or "(dnp)" in block


def _footprint_side(block: str) -> str:
    layer = _footprint_layer(block)
    return "Backside" if layer.startswith("B.") else "Frontside"


def _footprint_layer(block: str) -> str:
    match = re.search(r'\(layer\s+"([^"]+)"\)', block)
    return match.group(1) if match else "F.Cu"


def _footprint_mount(block: str) -> str:
    attr_match = re.search(r"\(attr\s+([^)]*)\)", block)
    attrs = attr_match.group(1).split() if attr_match else []
    if "through_hole" in attrs:
        return "THT"
    if "smd" in attrs:
        return "SMD"
    if re.search(r"\(pad\s+\"[^\"]*\"\s+thru_hole\b", block):
        return "THT"
    return "SMD"


def _collect_component_refs(root: Path) -> set[str]:
    refs: set[str] = set()
    for schematic in sorted(root.glob("*.kicad_sch")):
        text = schematic.read_text(encoding="utf-8")
        for block in _iter_sexpr_blocks(text, "symbol"):
            ref = _property_value(block, "Reference")
            if not ref or ref.startswith("#") or ref.startswith("${"):
                continue
            if not re.match(r"^[A-Za-z]+\d+", ref):
                continue
            if _symbol_excluded_from_count(block):
                continue
            refs.add(ref)
    return refs


def _symbol_excluded_from_count(block: str) -> bool:
    exclusions = (
        "(in_bom no)",
        "(on_board no)",
        "(dnp yes)",
        "(exclude_from_bom yes)",
    )
    return any(exclusion in block for exclusion in exclusions)


def _reference_prefix(reference: str) -> str:
    match = re.match(r"^([A-Za-z]+)", reference)
    return match.group(1).upper() if match else reference.upper()


def _property_value(block: str, name: str) -> str | None:
    pattern = re.compile(r'\(property\s+"' + re.escape(name) + r'"\s+"((?:\\.|[^"])*)"')
    match = pattern.search(block)
    if not match:
        return None
    return _unescape_kicad_string(match.group(1))


def _write_component_count_csv(
    root: Path, rows: tuple[tuple[str, int, int, int], ...], total: int
) -> Path:
    project_stem = _project_stem(root)
    output_dir = root / "Manufacturing" / "Assembly"
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / f"{project_stem}-components_count.csv"

    with csv_path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(("Type", "Front Side", "Back Side", "Total"))
        writer.writerows(rows)

    return csv_path


def _fill_component_count_placeholder(
    root: Path, rows: tuple[tuple[str, int, int, int], ...], total: int
) -> None:
    pcb_path = _pcb_path(root)
    text = pcb_path.read_text(encoding="utf-8")
    updated = _replace_text_box_content(text, COMPONENT_COUNT_TEXT_BOX_UUID, "")
    updated = _remove_generated_component_table(updated)
    updated = _insert_component_count_graphics(updated, rows)
    pcb_path.write_text(updated, encoding="utf-8")


def _insert_component_count_graphics(
    text: str, rows: tuple[tuple[str, int, int, int], ...]
) -> str:
    rect = _rect_bounds(text, COMPONENT_COUNT_RECT_UUID)
    layer = _block_layer(text, COMPONENT_COUNT_RECT_UUID)
    graphics = _component_count_graphics(rect, layer, rows)
    text = _set_rect_bounds(text, COMPONENT_COUNT_RECT_UUID, _component_table_bounds(rect, len(rows) + 1))
    group_index = text.find("\n\t(group ")
    insert_at = group_index if group_index != -1 else text.rfind("\n)")
    if insert_at == -1:
        raise ValueError("Could not find insertion point for component table")
    return text[:insert_at] + graphics + text[insert_at:]


def _component_count_graphics(
    rect: tuple[float, float, float, float],
    layer: str,
    rows: tuple[tuple[str, int, int, int], ...],
) -> str:
    x1, y1, x2, y2 = rect
    table_rows = [("Type", "Front Side", "Back Side", "Total")]
    table_rows.extend(tuple(str(value) for value in row) for row in rows)
    row_count = len(table_rows)
    x1, y1, x2, y2 = _component_table_bounds(rect, row_count)
    columns = (0.0, 0.32, 0.58, 0.80, 1.0)
    xs = [x1 + (x2 - x1) * value for value in columns]
    row_spacing = (y2 - y1) / max(row_count, 1)
    cell_padding = min(2.0, max(0.8, (x2 - x1) * 0.03))

    lines: list[str] = []
    for index, x in enumerate(xs[1:-1], start=1):
        lines.append(_gr_line(x, y1, x, y2, layer, f"v{index}"))
    lines.append(_gr_line(x1, y1 + row_spacing, x2, y1 + row_spacing, layer, "h1"))

    for row_index, row in enumerate(table_rows):
        y = y1 + (row_index + 0.5) * row_spacing
        for col_index, value in enumerate(row):
            left = xs[col_index]
            next_x = xs[col_index + 1] if col_index + 1 < len(xs) else x2
            is_number = col_index > 0 and row_index > 0
            centered = col_index > 0
            justify = "" if centered else "left"
            x = (left + next_x) / 2 if centered else left + cell_padding
            lines.append(
                _gr_text(
                    value,
                    x,
                    y,
                    layer,
                    justify,
                    f"r{row_index}c{col_index}",
                    bold=row_index == 0 or col_index == 0,
                )
            )

    return "\n" + "\n".join(lines) + "\n"


def _component_table_bounds(
    rect: tuple[float, float, float, float], row_count: int
) -> tuple[float, float, float, float]:
    x1, y1, x2, y2 = rect
    table_height = min(y2 - y1, max(row_count, 1) * 3.0)
    return (x1, y1, x2, y1 + table_height)


def _gr_line(x1: float, y1: float, x2: float, y2: float, layer: str, key: str) -> str:
    return (
        "\t(gr_line\n"
        f"\t\t(start {_fmt(x1)} {_fmt(y1)})\n"
        f"\t\t(end {_fmt(x2)} {_fmt(y2)})\n"
        "\t\t(stroke\n"
        "\t\t\t(width 0.2)\n"
        "\t\t\t(type default)\n"
        "\t\t)\n"
        f"\t\t(layer \"{layer}\")\n"
        f"\t\t(uuid \"{_generated_uuid(key)}\")\n"
        "\t)"
    )


def _gr_text(
    value: str,
    x: float,
    y: float,
    layer: str,
    justify: str,
    key: str,
    *,
    bold: bool = False,
) -> str:
    bold_line = "\t\t\t\t(bold yes)\n" if bold else ""
    justify_line = f"\t\t\t(justify {justify})\n" if justify else ""
    return (
        f"\t(gr_text \"{_escape_kicad_string(value)}\"\n"
        f"\t\t(at {_fmt(x)} {_fmt(y)} 0)\n"
        f"\t\t(layer \"{layer}\")\n"
        f"\t\t(uuid \"{_generated_uuid(key)}\")\n"
        "\t\t(effects\n"
        "\t\t\t(font\n"
        "\t\t\t\t(face \"Arimo\")\n"
        "\t\t\t\t(size 1 1)\n"
        "\t\t\t\t(thickness 0.15)\n"
        f"{bold_line}"
        "\t\t\t)\n"
        f"{justify_line}"
        "\t\t)\n"
        "\t)"
    )


def _generated_uuid(key: str) -> uuid.UUID:
    return uuid.uuid5(GENERATED_TABLE_NAMESPACE, f"component-count:{key}")


def _remove_generated_component_table(text: str) -> str:
    for key in [*(f"v{i}" for i in range(1, 4)), "h1"]:
        text = _remove_block_by_uuid(text, str(_generated_uuid(key)))
    for row in range(4):
        for col in range(4):
            text = _remove_block_by_uuid(text, str(_generated_uuid(f"r{row}c{col}")))
    return text


def _write_impedance_table(root: Path, config) -> None:
    project_stem = _project_stem(root)
    impedance_entries = _impedance_entries(config)
    output_dir = root / "Manufacturing" / "Fabrication"
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / f"{project_stem}-impedance_table.csv"
    pcb_path = _pcb_path(root)
    text = pcb_path.read_text(encoding="utf-8")

    if not impedance_entries:
        if csv_path.exists():
            csv_path.unlink()
        updated = _replace_text_box_content(
            text,
            IMPEDANCE_TABLE_TEXT_BOX_UUID,
            "NO IMPEDANCE CONTROLLED TRACES",
        )
        updated = _set_text_box_font_size(updated, IMPEDANCE_TABLE_TEXT_BOX_UUID, 1.27)
        if updated != text:
            pcb_path.write_text(updated, encoding="utf-8")
        return

    rendered = _render_impedance_table_csv(impedance_entries)
    csv_path.write_text(rendered, encoding="utf-8", newline="\n")
    updated = _replace_text_box_content(text, IMPEDANCE_TABLE_TEXT_BOX_UUID, rendered.rstrip())
    updated = _set_text_box_font_size(updated, IMPEDANCE_TABLE_TEXT_BOX_UUID, 1.0)
    if updated != text:
        pcb_path.write_text(updated, encoding="utf-8")


def _impedance_entries(config) -> list[dict[str, str]]:
    if config is None:
        return []
    entries = getattr(config, "impedance_entries", [])
    normalized: list[dict[str, str]] = []
    for entry in entries if isinstance(entries, list) else []:
        normalized.append(
            {
                "transmission_line": str(entry.get("transmission_line", "")).strip(),
                "impedance_ohms": str(entry.get("impedance_ohms", "")).strip(),
                "tolerance_ohms": str(entry.get("tolerance_ohms", "")).strip(),
                "layer": str(entry.get("layer", "")).strip(),
                "trace_width_mm": str(entry.get("trace_width_mm", "")).strip(),
                "gap_mm": str(entry.get("gap_mm", "")).strip(),
                "ref_layers": str(entry.get("ref_layers", "")).strip(),
            }
        )
    return [row for row in normalized if any(value.strip() for value in row.values())]


def _impedance_table_has_rows(root: Path) -> bool:
    config = _load_project_config(root)
    return bool(_impedance_entries(config))


def _write_manufacturing_notes(root: Path, config=None) -> None:
    if config is None:
        config = _load_project_config(root)
    values = _fabrication_note_values(root, config)
    project_stem = _project_stem(root)

    fabrication = _resource_text(root, FABRICATION_NOTES_TEMPLATE)
    impedance_rows = _impedance_entries(config)
    if not impedance_rows:
        fabrication = _strip_impedance_controlled_note(fabrication)
    else:
        fabrication = _strip_note_condition_markers(fabrication)
    fabrication = _render_note_template(fabrication, values)
    fabrication_dir = root / "Manufacturing" / "Fabrication"
    fabrication_dir.mkdir(parents=True, exist_ok=True)
    (fabrication_dir / f"{project_stem}-fabrication_notes.txt").write_text(
        fabrication,
        encoding="utf-8",
    )

    assembly = _resource_text(root, ASSEMBLY_NOTES_TEMPLATE)
    assembly_dir = root / "Manufacturing" / "Assembly"
    assembly_dir.mkdir(parents=True, exist_ok=True)
    (assembly_dir / f"{project_stem}-assembly_notes.txt").write_text(
        assembly,
        encoding="utf-8",
    )


def _write_readme_summaries(root: Path, config, rows: tuple[tuple[str, int, int, int], ...]) -> None:
    values = _fabrication_note_values(root, config)
    project_stem = _project_stem(root)
    impedance_rows = _impedance_entries(config)

    fabrication_summary = _fabrication_summary_text(values, impedance_rows)
    fabrication_dir = root / "Manufacturing" / "Fabrication"
    fabrication_dir.mkdir(parents=True, exist_ok=True)
    (fabrication_dir / f"{project_stem}-fabrication_summary.txt").write_text(
        fabrication_summary,
        encoding="utf-8",
    )

    component_summary = _component_summary_text(rows)
    assembly_dir = root / "Manufacturing" / "Assembly"
    assembly_dir.mkdir(parents=True, exist_ok=True)
    (assembly_dir / f"{project_stem}-component_summary.txt").write_text(
        component_summary,
        encoding="utf-8",
    )


def _write_readme_badges(root: Path, config) -> None:
    badge_path = root / ".boardwright" / "readme_badges.txt"
    badge_path.parent.mkdir(parents=True, exist_ok=True)
    badges = _readme_badges(config)
    badge_path.write_text(badges, encoding="utf-8")


def _readme_badges(config) -> str:
    if config is None:
        return ""

    repo = _github_repo_for_badges(config)
    if not repo:
        return ""

    badges = [
        _workflow_badge(repo, config.preview_workflow, config.dev_branch, "Preview CI"),
        _workflow_badge(repo, config.main_workflow, config.release_branch, "Accepted outputs"),
        _workflow_badge(repo, "prepare-release.yaml", config.release_branch, "Prepare release"),
        _workflow_badge(repo, "release.yaml", config.release_branch, "Publish release"),
    ]
    return " ".join(badges) + "\n"


def _workflow_badge(repo: str, workflow: str, branch: str, label: str) -> str:
    badge = f"https://github.com/{repo}/actions/workflows/{workflow}/badge.svg?branch={branch}"
    link = f"https://github.com/{repo}/actions/workflows/{workflow}"
    return f"[![{label}]({badge})]({link})"


def _github_repo_for_badges(config) -> str:
    repo = str(getattr(config, "github_repo", "") or "").strip()
    if repo:
        return repo

    git_url = str(config.project.get("project", {}).get("git_url") or "").strip()
    if not git_url:
        git_url = remote_url(config.root)
    return _github_repo_from_url(git_url)


def _github_repo_from_url(url: str) -> str:
    if not url:
        return ""
    patterns = (
        r"^https://github\.com/(?P<repo>[^/]+/[^/]+?)(?:\.git)?/?$",
        r"^git@github\.com:(?P<repo>[^/]+/[^/]+?)(?:\.git)?$",
        r"^ssh://git@github\.com/(?P<repo>[^/]+/[^/]+?)(?:\.git)?/?$",
    )
    for pattern in patterns:
        match = re.match(pattern, url)
        if match:
            return match.group("repo")
    return ""


def _resource_text(root: Path, relative_path: Path) -> str:
    project_path = root / relative_path
    if project_path.is_file():
        return project_path.read_text(encoding="utf-8")
    repo_path = Path(__file__).resolve().parents[2] / relative_path
    return repo_path.read_text(encoding="utf-8")


def _fabrication_note_values(root: Path, config=None) -> dict[str, str]:
    pcb = _pcb_path(root).read_text(encoding="utf-8")
    width, height = _board_size_mm(pcb)
    pth, npth = _min_drill_sizes(pcb)
    manufacturing = config.manufacturing if config is not None else {}
    company = _first_match(pcb, r'\(property\s+"([^"]*)"\s+"([^"]*)"\)', "")
    company = str(config.project.get("project", {}).get("company") or company or "COMPANY") if config is not None else company or "COMPANY"
    silkscreen_enabled = _optional_bool(manufacturing.get("silkscreen_enabled", True))
    conformal_coating = _optional_bool(manufacturing.get("conformal_coating", False))
    tented_vias = _optional_bool(manufacturing.get("tented_vias", True))
    rohs_pb_free = _optional_bool(manufacturing.get("rohs_pb_free", True))
    halogen_free = _optional_bool(manufacturing.get("halogen_free", True))
    silk_color = _stackup_layer_value(pcb, "F.SilkS", "color", "YELLOW")
    manufacturing_standard = _manufacturing_text(manufacturing, "manufacturing_standard", "IPC-6012 Class 2")
    core_material = _manufacturing_text(manufacturing, "core_material", "FR-4")
    flammability_rating = _manufacturing_text(manufacturing, "flammability_rating", "UL94V-0")
    tg_rating = _manufacturing_text(manufacturing, "tg_rating", "170 C")
    return {
        "pcb_finish_cap": _cap(_first_match(pcb, r'\(copper_finish\s+"([^"]+)"\)', "ENIG")),
        "solder_mask_color_text_cap": _cap(_stackup_layer_value(pcb, "F.Mask", "color", "GREEN")),
        "silk_screen_color_text_cap": _cap(silk_color),
        "manufacturing_standard_note": _manufacturing_standard_note(manufacturing_standard),
        "core_material_cap": _cap(core_material),
        "flammability_rating_cap": _cap(flammability_rating),
        "tg_rating_cap": _cap(tg_rating),
        "silkscreen_note": _silkscreen_note(silkscreen_enabled, silk_color),
        "tented_vias_note": _tented_vias_note(tented_vias),
        "rohs_note": _rohs_note(rohs_pb_free),
        "material_requirements_block": _material_requirements_block(
            core_material,
            flammability_rating,
            tg_rating,
            rohs_pb_free,
            halogen_free,
            company,
        ),
        "conformal_coating_note": _conformal_coating_note(conformal_coating),
        "fabrication_extra_notes": _extra_note_block(str(manufacturing.get("fabrication_notes") or "")),
        "COMPANY_cap": _cap(company),
        "bb_w_mm": _mm(width),
        "bb_h_mm": _mm(height),
        "thickness_mm": _mm(float(_first_match(pcb, r"\(thickness\s+([-0-9.]+)\)", "0"))),
        "track_mm": _mm(_min_track_width(pcb, 0.2)),
        "clearance_mm": _mm(_min_positive_float(re.findall(r"\(clearance\s+([-0-9.]+)\)", pcb), 0.2)),
        "drill_pth_real_mm": _mm(pth),
        "drill_npth_real_mm": _mm(npth),
        "oar_mm": _mm(_min_annular_ring(pcb, 0.15)),
        "c2h_mm": _mm(0.254),
        "c2e_mm": _mm(0.250),
        "h2h_mm": _mm(0.254),
    }



def _manufacturing_text(manufacturing: dict[str, object], key: str, default: str) -> str:
    if key not in manufacturing:
        return default
    return str(manufacturing.get(key) or "").strip()


def _manufacturing_standard_note(value: str) -> str:
    if not value:
        return ""
    return _fabrication_note(f"FABRICATE PER {_cap(value)}.")


def _material_requirements_block(
    core_material: str,
    flammability_rating: str,
    tg_rating: str,
    rohs_pb_free: bool | None,
    halogen_free: bool | None,
    company: str,
) -> str:
    requirements: list[str] = []
    if core_material:
        requirements.append(f"CORE MATERIAL: {_cap(core_material)}.")
    if flammability_rating:
        requirements.append(
            f"FLAMMABILITY RATING MUST MEET OR EXCEED {_cap(flammability_rating)} REQUIREMENTS."
        )
    if tg_rating:
        requirements.append(f"Tg {_cap(tg_rating)} OR EQUIVALENT.")
    compliance = _material_compliance_note(rohs_pb_free, halogen_free, company)
    if compliance:
        requirements.append(compliance)
    if not requirements:
        return ""
    lines = [f"\t{chr(ord('A') + index)}.\t{line}" for index, line in enumerate(requirements)]
    return _fabrication_note("PCB MATERIAL REQUIREMENTS:\n\n" + "\n".join(lines))


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _optional_bool(value) -> bool | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"", "none", "null", "unspecified"}:
        return None
    return _as_bool(value)


def _silkscreen_note(enabled: bool | None, color: str) -> str:
    if enabled is None:
        return ""
    if not enabled:
        return _fabrication_note(
            "NO SILKSCREEN LEGEND REQUIRED. DO NOT ADD MANUFACTURER MARKINGS UNLESS REQUIRED FOR TRACEABILITY."
        )
    return _fabrication_note(
        f"SILKSCREEN LEGEND TO BE APPLIED PER LAYER STACKUP USING {_cap(color)} NON-CONDUCTIVE EPOXY INK."
    )


def _tented_vias_note(enabled: bool | None) -> str:
    if enabled is None:
        return ""
    if enabled:
        return _fabrication_note("ALL VIAS ARE TENTED ON BOTH SIDES UNLESS SOLDER MASK IS OPENED IN THE GERBER DATA.")
    return _fabrication_note("VIAS ARE NOT REQUIRED TO BE TENTED UNLESS SPECIFIED IN THE GERBER DATA.")


def _rohs_note(enabled: bool | None) -> str:
    if enabled is None:
        return ""
    if enabled:
        return _fabrication_note("VENDOR SHALL USE A RoHS-COMPLIANT, PB-FREE MANUFACTURING PROCESS.")
    return _fabrication_note("RoHS / PB-FREE PROCESS IS NOT REQUIRED UNLESS SPECIFIED BY PURCHASE ORDER.")


def _material_compliance_note(rohs_pb_free: bool | None, halogen_free: bool | None, company: str) -> str:
    requirements = []
    if rohs_pb_free:
        requirements.append("RoHS-COMPLIANT")
    if halogen_free:
        requirements.append("HALOGEN FREE")
    if not requirements:
        return ""
    return f"EQUIVALENT MATERIAL SHALL BE {' AND '.join(requirements)} AND APPROVED BY {_cap(company)}."


def _conformal_coating_note(enabled: bool | None) -> str:
    if enabled is None:
        return ""
    if enabled:
        return _fabrication_note("CONFORMAL COATING IS REQUIRED AFTER ASSEMBLY UNLESS OTHERWISE SPECIFIED.")
    return _fabrication_note("CONFORMAL COATING IS NOT REQUIRED.")


def _extra_note_block(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return ""
    return _fabrication_note("ADDITIONAL FABRICATION NOTES:\n" + _indent_note_body(stripped))


def _indent_note_body(text: str) -> str:
    return "\n".join("\t" + line.strip() if line.strip() else "" for line in text.splitlines())


def _fabrication_note(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return ""
    return "@NOTE@\t" + stripped

def _fabrication_summary_text(
    values: dict[str, str],
    impedance_rows: list[dict[str, str]],
) -> str:
    lines = [
        "FABRICATION SNAPSHOT",
        f"- Board size: {values['bb_w_mm']} x {values['bb_h_mm']} mm",
        f"- Thickness: {values['thickness_mm']} mm",
        f"- Surface finish: {values['pcb_finish_cap']}",
        f"- Soldermask color: {values['solder_mask_color_text_cap']}",
        f"- Silkscreen color: {values['silk_screen_color_text_cap']}",
        f"- Minimum PTH drill: {values['drill_pth_real_mm']} mm",
        f"- Minimum NPTH drill: {values['drill_npth_real_mm']} mm",
    ]
    if impedance_rows:
        lines.append(f"- Impedance entries: {len(impedance_rows)}")
    else:
        lines.append("- Impedance: no impedance controlled traces")
    return "\n".join(lines) + "\n"


def _render_impedance_table_csv(rows: list[dict[str, str]]) -> str:
    buffer = io.StringIO()
    output = csv.writer(buffer)
    output.writerow(
        [
            "Transmission Line",
            "Impedance [ohms]",
            "Tolerance [ohms]",
            "Layer",
            "Trace Width [mm]",
            "Gap [mm]",
            "Ref. Layers",
        ]
    )
    for row in rows:
        output.writerow(
            [
                row["transmission_line"],
                row["impedance_ohms"],
                row["tolerance_ohms"],
                row["layer"],
                row["trace_width_mm"],
                row["gap_mm"],
                row["ref_layers"],
            ]
        )
    return buffer.getvalue()


def _component_summary_text(rows: tuple[tuple[str, int, int, int], ...]) -> str:
    lines = ["COMPONENT SNAPSHOT"]
    for label, front, back, total in rows:
        lines.append(f"- {label}: {front} front, {back} back, {total} total")
    return "\n".join(lines) + "\n"


def _render_note_template(template: str, values: dict[str, str]) -> str:
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("${" + key + "}", value)
    rendered = _strip_empty_note_markers(rendered)
    rendered = _number_fabrication_notes(rendered)
    return _wrap_fabrication_note_lines(_collapse_blank_note_lines(rendered))


def _strip_empty_note_markers(text: str) -> str:
    return "\n".join(
        "" if line.strip() == "@NOTE@" else line
        for line in text.splitlines()
    )


def _number_fabrication_notes(text: str) -> str:
    number = 1
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.lstrip()
        indent = line[: len(line) - len(stripped)]
        if stripped.startswith("@NOTE@"):
            body = stripped.removeprefix("@NOTE@").lstrip()
            if not body:
                continue
            lines.append(f"{indent}{number})\t{body}")
            number += 1
            continue
        lines.append(line)
    return "\n".join(lines)


def _collapse_blank_note_lines(text: str) -> str:
    lines = [line.rstrip() for line in text.splitlines()]
    collapsed: list[str] = []
    blank_count = 0
    for line in lines:
        if line.strip():
            collapsed.append(line)
            blank_count = 0
            continue
        blank_count += 1
        if blank_count <= 2:
            collapsed.append("")
    return "\n".join(collapsed).strip() + "\n"


def _wrap_fabrication_note_lines(text: str, width: int = 80) -> str:
    wrapped: list[str] = []
    for line in text.splitlines():
        if len(line.expandtabs(4)) <= width or not line.strip():
            wrapped.append(line.rstrip())
            continue
        numbered = re.match(r"^(\d+\)\t)(.*)$", line)
        if numbered:
            prefix, body = numbered.groups()
            wrapped.extend(_wrap_numbered_note_line(prefix, body, width))
            continue
        indent = re.match(r"^\s*", line).group(0)
        wrapped.extend(
            textwrap.wrap(
                line.strip(),
                width=width,
                initial_indent=indent,
                subsequent_indent=indent,
                break_long_words=False,
                break_on_hyphens=False,
            )
        )
    return "\n".join(wrapped).rstrip() + "\n"


def _wrap_numbered_note_line(prefix: str, body: str, width: int) -> list[str]:
    body_width = max(20, width - len(prefix.expandtabs(4)))
    parts = textwrap.wrap(
        body.strip(),
        width=body_width,
        break_long_words=False,
        break_on_hyphens=False,
    )
    if not parts:
        return [prefix.rstrip()]
    lines = [prefix + parts[0]]
    continuation_width = max(20, width - len("\t".expandtabs(4)))
    for part in parts[1:]:
        if len(part.expandtabs(4)) <= continuation_width:
            lines.append("\t" + part)
        else:
            lines.extend("\t" + chunk for chunk in textwrap.wrap(
                part,
                width=continuation_width,
                break_long_words=False,
                break_on_hyphens=False,
            ))
    return lines


def _strip_impedance_controlled_note(text: str) -> str:
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#?stackup and impedance_controlled"):
            continue
        if "REFER TO IMPEDANCE TABLE" in line:
            continue
        if "CONFIRM TRACE WIDTHS AND SPACINGS" in line:
            continue
        lines.append(line)
    return "\n".join(lines).rstrip() + "\n"


def _strip_note_condition_markers(text: str) -> str:
    lines = [
        line
        for line in text.splitlines()
        if not line.strip().startswith("#?")
    ]
    return _collapse_blank_note_lines("\n".join(lines))


def _board_size_mm(pcb: str) -> tuple[float, float]:
    points: list[tuple[float, float]] = []
    for block in _iter_sexpr_blocks(pcb, "gr_line"):
        if '(layer "Edge.Cuts")' not in block:
            continue
        for match in re.finditer(r"\((?:start|end)\s+([-0-9.]+)\s+([-0-9.]+)\)", block):
            points.append((float(match.group(1)), float(match.group(2))))
    for block in _iter_sexpr_blocks(pcb, "gr_rect"):
        if '(layer "Edge.Cuts")' not in block:
            continue
        for match in re.finditer(r"\((?:start|end)\s+([-0-9.]+)\s+([-0-9.]+)\)", block):
            points.append((float(match.group(1)), float(match.group(2))))
    if not points:
        return 0.0, 0.0
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return max(xs) - min(xs), max(ys) - min(ys)


def _min_drill_sizes(pcb: str) -> tuple[float, float]:
    pth: list[float] = []
    npth: list[float] = []
    for block in _iter_sexpr_blocks(pcb, "pad"):
        drill = _pad_drill(block)
        if drill is None:
            continue
        if "np_thru_hole" in block:
            npth.append(drill)
        elif "thru_hole" in block:
            pth.append(drill)
    return min(pth) if pth else 0.0, min(npth) if npth else 0.0


def _pad_drill(block: str) -> float | None:
    match = re.search(r"\(drill\s+(?:oval\s+)?([-0-9.]+)(?:\s+([-0-9.]+))?", block)
    if not match:
        return None
    values = [float(value) for value in match.groups() if value is not None]
    return min(values) if values else None


def _min_annular_ring(pcb: str, default: float) -> float:
    rings: list[float] = []
    for block in _iter_sexpr_blocks(pcb, "pad"):
        if "thru_hole" not in block or "np_thru_hole" in block:
            continue
        drill = _pad_drill(block)
        size = re.search(r"\(size\s+([-0-9.]+)\s+([-0-9.]+)\)", block)
        if drill is None or not size:
            continue
        rings.append((min(float(size.group(1)), float(size.group(2))) - drill) / 2)
    positives = [ring for ring in rings if ring > 0]
    return min(positives) if positives else default


def _min_track_width(pcb: str, default: float) -> float:
    widths: list[float] = []
    for head in ("segment", "arc"):
        for block in _iter_sexpr_blocks(pcb, head):
            match = re.search(r"\(width\s+([-0-9.]+)\)", block)
            if match:
                widths.append(float(match.group(1)))
    if widths:
        return min(widths)
    last_width = re.search(r"\(last_track_width\s+([-0-9.]+)\)", pcb)
    return float(last_width.group(1)) if last_width else default


def _stackup_layer_value(pcb: str, layer_name: str, field: str, default: str) -> str:
    layer_index = pcb.find(f'(layer "{layer_name}"')
    if layer_index == -1:
        return default
    block = pcb[layer_index : _find_matching_paren(pcb, layer_index)]
    return _first_match(block, rf'\({field}\s+"?([^")]+)"?\)', default)


def _first_match(text: str, pattern: str, default: str) -> str:
    match = re.search(pattern, text)
    return match.group(1) if match else default


def _min_float(values: list[str], default: float) -> float:
    numbers = [float(value) for value in values]
    return min(numbers) if numbers else default


def _min_positive_float(values: list[str], default: float) -> float:
    numbers = [float(value) for value in values if float(value) > 0]
    return min(numbers) if numbers else default


def _mm(value: float) -> str:
    return f"{value:.3f}"


def _cap(value: str) -> str:
    return value.upper()


def _remove_block_by_uuid(text: str, target_uuid: str) -> str:
    uuid_index = text.find(f'(uuid "{target_uuid}")')
    if uuid_index == -1:
        return text
    start_candidates = [
        text.rfind("(gr_line", 0, uuid_index),
        text.rfind("(gr_rect", 0, uuid_index),
        text.rfind("(gr_text", 0, uuid_index),
        text.rfind("(gr_text_box", 0, uuid_index),
    ]
    start = max(start_candidates)
    if start == -1:
        return text
    line_start = text.rfind("\n", 0, start)
    if line_start == -1:
        line_start = start
    end = _find_matching_paren(text, start)
    return text[:line_start] + text[end + 1 :]


def _rect_bounds(text: str, rect_uuid: str) -> tuple[float, float, float, float]:
    block = _block_for_uuid(text, rect_uuid)
    start = re.search(r"\(start\s+([-0-9.]+)\s+([-0-9.]+)\)", block)
    end = re.search(r"\(end\s+([-0-9.]+)\s+([-0-9.]+)\)", block)
    if not start or not end:
        raise ValueError(f"Could not parse rectangle bounds for UUID: {rect_uuid}")
    return (
        float(start.group(1)),
        float(start.group(2)),
        float(end.group(1)),
        float(end.group(2)),
    )


def _set_rect_bounds(
    text: str,
    rect_uuid: str,
    bounds: tuple[float, float, float, float],
) -> str:
    block = _block_for_uuid(text, rect_uuid)
    x1, y1, x2, y2 = bounds
    updated_block = re.sub(
        r"\(start\s+[-0-9.]+\s+[-0-9.]+\)",
        f"(start {_fmt(x1)} {_fmt(y1)})",
        block,
        count=1,
    )
    updated_block = re.sub(
        r"\(end\s+[-0-9.]+\s+[-0-9.]+\)",
        f"(end {_fmt(x2)} {_fmt(y2)})",
        updated_block,
        count=1,
    )
    if updated_block == block:
        return text
    start = text.find(block)
    return text[:start] + updated_block + text[start + len(block) :]


def _block_layer(text: str, target_uuid: str) -> str:
    block = _block_for_uuid(text, target_uuid)
    match = re.search(r'\(layer\s+"([^"]+)"\)', block)
    if not match:
        raise ValueError(f"Could not parse layer for UUID: {target_uuid}")
    return match.group(1)


def _block_for_uuid(text: str, target_uuid: str) -> str:
    uuid_index = text.find(f'(uuid "{target_uuid}")')
    if uuid_index == -1:
        raise ValueError(f"KiCad object UUID not found: {target_uuid}")
    starts = [
        text.rfind("(gr_rect", 0, uuid_index),
        text.rfind("(gr_text_box", 0, uuid_index),
        text.rfind("(gr_line", 0, uuid_index),
        text.rfind("(gr_text", 0, uuid_index),
    ]
    start = max(starts)
    if start == -1:
        raise ValueError(f"KiCad object block not found for UUID: {target_uuid}")
    end = _find_matching_paren(text, start)
    return text[start : end + 1]


def _fmt(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _replace_text_box_content(text: str, uuid: str, replacement: str) -> str:
    uuid_index = text.find(f'(uuid "{uuid}")')
    if uuid_index == -1:
        raise ValueError(f"KiCad text box UUID not found: {uuid}")

    start = text.rfind("(gr_text_box", 0, uuid_index)
    if start == -1:
        raise ValueError(f"KiCad text box block not found for UUID: {uuid}")

    first_quote = text.find('"', start)
    if first_quote == -1 or first_quote > uuid_index:
        raise ValueError(f"KiCad text box content not found for UUID: {uuid}")
    closing_quote = _find_closing_quote(text, first_quote)

    return (
        text[: first_quote + 1]
        + _escape_kicad_string(replacement)
        + text[closing_quote:]
    )


def _set_text_box_font_size(text: str, uuid: str, size: float) -> str:
    block = _block_for_uuid(text, uuid)
    updated_block = re.sub(
        r"\(size\s+[-0-9.]+\s+[-0-9.]+\)",
        f"(size {_fmt(size)} {_fmt(size)})",
        block,
        count=1,
    )
    if updated_block == block:
        return text
    start = text.find(block)
    return text[:start] + updated_block + text[start + len(block) :]


def _find_closing_quote(text: str, opening_quote: int) -> int:
    index = opening_quote + 1
    escaped = False
    while index < len(text):
        char = text[index]
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            return index
        index += 1
    raise ValueError("Unterminated KiCad string")


def _iter_sexpr_blocks(text: str, head: str):
    needle = f"({head}"
    index = 0
    while True:
        start = text.find(needle, index)
        if start == -1:
            return
        end = _find_matching_paren(text, start)
        yield text[start : end + 1]
        index = end + 1


def _find_matching_paren(text: str, start: int) -> int:
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
    raise ValueError("Unbalanced KiCad s-expression")


def _project_stem(root: Path) -> str:
    projects = sorted(root.glob("*.kicad_pro"))
    if projects:
        return projects[0].stem
    return _pcb_path(root).stem


def _pcb_path(root: Path) -> Path:
    pcbs = sorted(root.glob("*.kicad_pcb"))
    if not pcbs:
        raise FileNotFoundError("No .kicad_pcb file found")
    return pcbs[0]


def _escape_kicad_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _yaml_quote(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("\n", "\\\\n").replace("'", "''") + "'"


def _render_project_text(value: str, project: dict[str, object]) -> str:
    replacements = {
        "BOARD_REVISION": str(project.get("board_revision") or ""),
        "PROJECT_NUMBER": str(project.get("name") or ""),
        "PCBA_NAME": str(project.get("pcba_name") or project.get("name") or ""),
        "PCB_NAME": str(project.get("pcb_name") or project.get("pcba_name") or project.get("name") or ""),
        "COMPANY": str(project.get("company") or ""),
        "DESIGNER": str(project.get("designer") or ""),
    }
    rendered = value
    for key, replacement in replacements.items():
        rendered = rendered.replace("${" + key + "}", replacement)
        rendered = rendered.replace("{" + key + "}", replacement)
    return rendered


def _pcb_worksheet(root: Path, fallback: str) -> str:
    return _worksheet_from_kicad_project(root, preferred_sections=("pcbnew",)) or fallback


def _worksheet_definition(path: str) -> str:
    value = path.replace("\\", "/").strip()
    if not value:
        value = "Templates/Boardwright_Template_PCB_GIT_A4.kicad_wks"
    if value.startswith("${KIPRJMOD}/"):
        return value
    if value.startswith("/") or (len(value) > 2 and value[1] == ":" and value[2] == "/"):
        return value
    return "${KIPRJMOD}/" + value.lstrip("./")


def _unescape_kicad_string(value: str) -> str:
    return value.replace('\\"', '"').replace("\\\\", "\\")
