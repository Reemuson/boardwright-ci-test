"""Prepare KiCad PCB table placeholders before KiBot renders PDFs."""

from __future__ import annotations

import csv
import re
import uuid
from collections import Counter
from dataclasses import dataclass
from pathlib import Path


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
GENERATED_TABLE_NAMESPACE = uuid.UUID("23d77107-9438-4a74-a20c-c4df6c5126dd")


@dataclass(frozen=True)
class ComponentCountResult:
    csv_path: Path
    total: int
    rows: tuple[tuple[str, int, int, int], ...]


def prepare_pcb_tables(root: Path) -> ComponentCountResult:
    root = root.resolve()
    counts = _collect_component_mount_counts(root)
    rows = _component_mount_rows(counts)
    total = rows[-1][-1] if rows else 0

    csv_path = _write_component_count_csv(root, rows, total)
    _fill_component_count_placeholder(root, rows, total)
    _fill_empty_impedance_placeholder(root)
    _write_manufacturing_notes(root)

    return ComponentCountResult(csv_path=csv_path, total=total, rows=rows)


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
    for mount in ("THT", "SMD"):
        front = counts[(mount, "Frontside")]
        back = counts[(mount, "Backside")]
        rows.append((mount, front, back, front + back))

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
    columns = (0.0, 0.30, 0.58, 0.80)
    xs = [x1 + (x2 - x1) * value for value in columns]
    margin = 0.825
    row_spacing = min(3.0, (y2 - y1 - margin * 2) / max(row_count - 1, 1))

    lines: list[str] = []
    lines.append(_gr_line(x1, y1 + row_spacing, x2, y1 + row_spacing, layer, "h1"))

    for row_index, row in enumerate(table_rows):
        y = y1 + margin + row_index * row_spacing
        for col_index, value in enumerate(row):
            x = xs[col_index] + margin
            justify = "left"
            next_x = xs[col_index + 1] if col_index + 1 < len(xs) else x2
            lines.append(
                _gr_text_box(
                    value,
                    x,
                    y - 0.825,
                    next_x,
                    y + row_spacing - 0.825,
                    layer,
                    justify,
                    f"r{row_index}c{col_index}",
                )
            )

    return "\n" + "\n".join(lines) + "\n"


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


def _gr_text_box(
    value: str,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    layer: str,
    justify: str,
    key: str,
) -> str:
    return (
        f"\t(gr_text_box \"{_escape_kicad_string(value)}\"\n"
        f"\t\t(start {_fmt(x1)} {_fmt(y1)})\n"
        f"\t\t(end {_fmt(x2)} {_fmt(y2)})\n"
        "\t\t(margins 0.825 0.825 0.825 0.825)\n"
        f"\t\t(layer \"{layer}\")\n"
        f"\t\t(uuid \"{_generated_uuid(key)}\")\n"
        "\t\t(effects\n"
        "\t\t\t(font\n"
        "\t\t\t\t(face \"Arial\")\n"
        "\t\t\t\t(size 1 1)\n"
        "\t\t\t\t(thickness 0.15)\n"
        "\t\t\t)\n"
        f"\t\t\t(justify {justify} top)\n"
        "\t\t)\n"
        "\t\t(border no)\n"
        "\t\t(stroke\n"
        "\t\t\t(width 0.15)\n"
        "\t\t\t(type solid)\n"
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


def _fill_empty_impedance_placeholder(root: Path) -> None:
    if _impedance_table_has_rows(root):
        return
    pcb_path = _pcb_path(root)
    text = pcb_path.read_text(encoding="utf-8")
    updated = _replace_text_box_content(
        text,
        IMPEDANCE_TABLE_TEXT_BOX_UUID,
        "NO IMPEDANCE CONTROLLED TRACES",
    )
    updated = _set_text_box_font_size(updated, IMPEDANCE_TABLE_TEXT_BOX_UUID, 1.27)
    if updated != text:
        pcb_path.write_text(updated, encoding="utf-8")


def _impedance_table_has_rows(root: Path) -> bool:
    template = root / IMPEDANCE_TABLE_TEMPLATE
    if not template.is_file():
        return False
    rows = [
        row
        for row in csv.reader(template.read_text(encoding="utf-8", errors="ignore").splitlines())
        if any(cell.strip() for cell in row)
    ]
    return len(rows) > 1


def _write_manufacturing_notes(root: Path) -> None:
    values = _fabrication_note_values(root)
    project_stem = _project_stem(root)

    fabrication = _resource_text(root, FABRICATION_NOTES_TEMPLATE)
    fabrication = _render_note_template(fabrication, values)
    if not _impedance_table_has_rows(root):
        fabrication = _strip_impedance_controlled_note(fabrication)
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


def _resource_text(root: Path, relative_path: Path) -> str:
    project_path = root / relative_path
    if project_path.is_file():
        return project_path.read_text(encoding="utf-8")
    repo_path = Path(__file__).resolve().parents[2] / relative_path
    return repo_path.read_text(encoding="utf-8")


def _fabrication_note_values(root: Path) -> dict[str, str]:
    pcb = _pcb_path(root).read_text(encoding="utf-8")
    width, height = _board_size_mm(pcb)
    pth, npth = _min_drill_sizes(pcb)
    return {
        "pcb_finish_cap": _cap(_first_match(pcb, r'\(copper_finish\s+"([^"]+)"\)', "ENIG")),
        "solder_mask_color_text_cap": _cap(
            _stackup_layer_value(pcb, "F.Mask", "color", "GREEN")
        ),
        "silk_screen_color_text_cap": _cap(
            _stackup_layer_value(pcb, "F.SilkS", "color", "YELLOW")
        ),
        "COMPANY_cap": _cap(
            _first_match(pcb, r'\(property\s+"COMPANY"\s+"([^"]*)"\)', "COMPANY")
        ),
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


def _render_note_template(template: str, values: dict[str, str]) -> str:
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("${" + key + "}", value)
    return rendered.replace("Ã—", "×")


def _strip_impedance_controlled_note(text: str) -> str:
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#?stackup and impedance_controlled"):
            continue
        if "REFER TO IMPEDANCE TABLE" in line:
            continue
        if "CONFIRM SPACE WIDTHS AND SPACINGS" in line:
            continue
        lines.append(line)
    return "\n".join(lines).rstrip() + "\n"


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


def _unescape_kicad_string(value: str) -> str:
    return value.replace('\\"', '"').replace("\\\\", "\\")
