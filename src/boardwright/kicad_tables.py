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
IMPEDANCE_TABLE_GROUP_NAME = "kibot_table_csv_impedance_table"
IMPEDANCE_TABLE_TEMPLATE = Path(
    "boardwright_resources/kibot/resources/templates/impedance_table.txt"
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
    _hide_empty_impedance_placeholder(root)

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
            lines.append(_gr_text(value, x, y, layer, justify, f"r{row_index}c{col_index}"))

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


def _gr_text(value: str, x: float, y: float, layer: str, justify: str, key: str) -> str:
    return (
        f"\t(gr_text \"{_escape_kicad_string(value)}\"\n"
        f"\t\t(at {_fmt(x)} {_fmt(y)} 0)\n"
        f"\t\t(layer \"{layer}\")\n"
        f"\t\t(uuid \"{_generated_uuid(key)}\")\n"
        "\t\t(effects\n"
        "\t\t\t(font\n"
        "\t\t\t\t(face \"Arial\")\n"
        "\t\t\t\t(size 1 1)\n"
        "\t\t\t\t(thickness 0.15)\n"
        "\t\t\t)\n"
        f"\t\t\t(justify {justify})\n"
        "\t\t)\n"
        "\t)"
    )


def _generated_uuid(key: str) -> uuid.UUID:
    return uuid.uuid5(GENERATED_TABLE_NAMESPACE, f"component-count:{key}")


def _remove_generated_component_table(text: str) -> str:
    text = _remove_block_by_uuid(text, str(_generated_uuid("h1")))
    for row in range(4):
        for col in range(4):
            text = _remove_block_by_uuid(text, str(_generated_uuid(f"r{row}c{col}")))
    return text


def _hide_empty_impedance_placeholder(root: Path) -> None:
    if _impedance_table_has_rows(root):
        return
    pcb_path = _pcb_path(root)
    text = pcb_path.read_text(encoding="utf-8")
    updated = _remove_group_and_members(text, IMPEDANCE_TABLE_GROUP_NAME)
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


def _remove_group_and_members(text: str, group_name: str) -> str:
    group_start = text.find(f'(group "{group_name}"')
    if group_start == -1:
        return text
    group_end = _find_matching_paren(text, group_start)
    group_block = text[group_start : group_end + 1]
    members = re.findall(r'"([0-9a-fA-F-]{36})"', group_block)

    updated = text[:group_start] + text[group_end + 1 :]
    for member_uuid in members:
        updated = _remove_block_by_uuid(updated, member_uuid)
    return updated


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
