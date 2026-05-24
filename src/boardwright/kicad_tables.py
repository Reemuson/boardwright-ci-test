"""Prepare KiCad PCB table placeholders before KiBot renders PDFs."""

from __future__ import annotations

import csv
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path


COMPONENT_COUNT_TEXT_BOX_UUID = "511273fd-c939-4feb-bf05-ae1b43c3644e"


@dataclass(frozen=True)
class ComponentCountResult:
    csv_path: Path
    total: int
    rows: tuple[tuple[str, int], ...]


def prepare_pcb_tables(root: Path) -> ComponentCountResult:
    root = root.resolve()
    refs = _collect_component_refs(root)
    counts = Counter(_reference_prefix(ref) for ref in refs)
    rows = tuple(sorted(counts.items()))
    total = sum(counts.values())

    csv_path = _write_component_count_csv(root, rows, total)
    _fill_component_count_placeholder(root, rows, total)

    return ComponentCountResult(csv_path=csv_path, total=total, rows=rows)


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
    root: Path, rows: tuple[tuple[str, int], ...], total: int
) -> Path:
    project_stem = _project_stem(root)
    output_dir = root / "Manufacturing" / "Assembly"
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / f"{project_stem}-components_count.csv"

    with csv_path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(("Component", "Quantity"))
        for prefix, quantity in rows:
            writer.writerow((prefix, quantity))
        writer.writerow(("TOTAL", total))

    return csv_path


def _fill_component_count_placeholder(
    root: Path, rows: tuple[tuple[str, int], ...], total: int
) -> None:
    pcb_path = _pcb_path(root)
    text = pcb_path.read_text(encoding="utf-8")
    table_text = _component_count_text(rows, total)
    updated = _replace_text_box_content(text, COMPONENT_COUNT_TEXT_BOX_UUID, table_text)
    pcb_path.write_text(updated, encoding="utf-8")


def _component_count_text(rows: tuple[tuple[str, int], ...], total: int) -> str:
    if not rows:
        return "Component, Quantity\nNone, 0"
    lines = ["Component, Quantity"]
    lines.extend(f"{prefix}, {quantity}" for prefix, quantity in rows)
    lines.append(f"TOTAL, {total}")
    return "\n".join(lines)


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
