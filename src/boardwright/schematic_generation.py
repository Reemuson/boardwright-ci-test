# SPDX-License-Identifier: LicenseRef-Boardwright-Source-Available
# Copyright (c) Ryan Hicks

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from uuid import uuid4

from .errors import BoardwrightError


@dataclass(frozen=True)
class GeneratedSchematic:
    path: Path


def slugify(value: str, fallback: str = "sheet") -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-").lower()
    return slug or fallback


def generate_revision_history_sheet(
    root: Path,
    output: Path,
    *,
    title: str = "REVISION HISTORY",
    force: bool = False,
) -> GeneratedSchematic:
    template = root / "revision-history.kicad_sch"
    text = _read_template(template)
    text = _replace_title_block_title(text, title)
    _write_text(output, text, force=force)
    return GeneratedSchematic(output)


def generate_one_sheet_schematic(
    output: Path,
    *,
    title: str,
    company: str,
    revision: str,
    date_text: str | None = None,
    force: bool = False,
) -> GeneratedSchematic:
    text = _render_one_sheet_schematic(
        title=title,
        company=company,
        revision=revision,
        date_text=date_text or date.today().isoformat(),
    )
    _write_text(output, text, force=force)
    return GeneratedSchematic(output)


def generate_hierarchical_project(
    output_dir: Path,
    *,
    project_name: str,
    company: str,
    revision: str,
    sheet_name: str,
    child_filename: str | None = None,
    date_text: str | None = None,
    force: bool = False,
) -> tuple[GeneratedSchematic, GeneratedSchematic]:
    output_dir.mkdir(parents=True, exist_ok=True)
    child_name = child_filename or f"{slugify(sheet_name)}.kicad_sch"
    parent_path = output_dir / "project.kicad_sch"
    child_path = output_dir / child_name
    child_title = sheet_name

    parent_text = _render_hierarchical_parent(
        title=project_name,
        company=company,
        revision=revision,
        date_text=date_text or date.today().isoformat(),
        sheet_name=sheet_name,
        child_filename=child_path.name,
    )
    child_text = _render_child_schematic(child_title=child_title)
    _write_text(parent_path, parent_text, force=force)
    _write_text(child_path, child_text, force=force)
    return GeneratedSchematic(parent_path), GeneratedSchematic(child_path)


def add_hierarchical_sheet(
    parent_schematic: Path,
    child_filename: Path,
    *,
    sheet_name: str,
    child_title: str | None = None,
    force: bool = False,
) -> tuple[GeneratedSchematic, GeneratedSchematic]:
    if not parent_schematic.exists():
        raise BoardwrightError(f"Missing parent schematic: {parent_schematic}")

    child_title = child_title or sheet_name
    child_text = _render_child_schematic(child_title=child_title)
    parent_text = parent_schematic.read_text(encoding="utf-8")
    if child_filename.name in parent_text:
        raise BoardwrightError(
            f"{child_filename.name} already appears in {parent_schematic.name}."
        )
    parent_text = _append_sheet_block(
        parent_text,
        sheet_name=sheet_name,
        child_filename=child_filename.name,
        project_name=parent_schematic.stem,
    )
    parent_schematic.write_text(parent_text, encoding="utf-8", newline="\n")
    _write_text(child_filename, child_text, force=force)
    return GeneratedSchematic(parent_schematic), GeneratedSchematic(child_filename)


def _render_one_sheet_schematic(
    *,
    title: str,
    company: str,
    revision: str,
    date_text: str,
) -> str:
    return f"""(kicad_sch
\t(version 20250114)
\t(generator "eeschema")
\t(generator_version "9.0")
\t(uuid "{uuid4()}")
\t(paper "A4")
\t(title_block
\t\t(title "{_escape(title)}")
\t\t(date "{_escape(date_text)}")
\t\t(rev "{_escape(revision)}")
\t\t(company "{_escape(company)}")
\t)
\t(lib_symbols)
\t(text_box "{_escape(title)}\\nOne-sheet schematic skeleton"
\t\t(exclude_from_sim no)
\t\t(at 25.4 40.64 0)
\t\t(size 101.6 20.32)
\t\t(margins 2 2 2 2)
\t\t(stroke
\t\t\t(width 0.1524)
\t\t\t(type solid)
\t\t)
\t\t(fill
\t\t\t(type none)
\t\t)
\t\t(effects
\t\t\t(font
\t\t\t\t(size 2.54 2.54)
\t\t\t\t(bold yes)
\t\t\t)
\t\t\t(justify left top)
\t\t)
\t\t(uuid "{uuid4()}")
\t)
\t(embedded_fonts no)
)
"""


def _render_hierarchical_parent(
    *,
    title: str,
    company: str,
    revision: str,
    date_text: str,
    sheet_name: str,
    child_filename: str,
) -> str:
    sheet_uuid = str(uuid4())
    child_uuid = str(uuid4())
    return f"""(kicad_sch
\t(version 20250114)
\t(generator "eeschema")
\t(generator_version "9.0")
\t(uuid "{uuid4()}")
\t(paper "A4")
\t(title_block
\t\t(title "{_escape(title)}")
\t\t(date "{_escape(date_text)}")
\t\t(rev "{_escape(revision)}")
\t\t(company "{_escape(company)}")
\t)
\t(lib_symbols)
\t(text_box "{_escape(title)}\\nHierarchical schematic skeleton"
\t\t(exclude_from_sim no)
\t\t(at 25.4 30.48 0)
\t\t(size 101.6 15.24)
\t\t(margins 2 2 2 2)
\t\t(stroke
\t\t\t(width 0.1524)
\t\t\t(type solid)
\t\t)
\t\t(fill
\t\t\t(type none)
\t\t)
\t\t(effects
\t\t\t(font
\t\t\t\t(size 2.54 2.54)
\t\t\t\t(bold yes)
\t\t\t)
\t\t\t(justify left top)
\t\t)
\t\t(uuid "{uuid4()}")
\t)
\t(sheet
\t\t(at 25.4 55.88)
\t\t(size 101.6 50.8)
\t\t(exclude_from_sim no)
\t\t(in_bom yes)
\t\t(on_board yes)
\t\t(dnp no)
\t\t(stroke
\t\t\t(width 0.1524)
\t\t\t(type solid)
\t\t)
\t\t(fill
\t\t\t(color 0 0 0 0.0000)
\t\t)
\t\t(uuid "{sheet_uuid}")
\t\t(property "Sheetname" "{_escape(sheet_name)}"
\t\t\t(at 25.4 54.8509 0)
\t\t\t(effects
\t\t\t\t(font
\t\t\t\t\t(face "Tinos")
\t\t\t\t\t(size 1.905 1.905)
\t\t\t\t\t(bold yes)
\t\t\t\t\t(color 0 0 0 1)
\t\t\t\t)
\t\t\t\t(justify left bottom)
\t\t\t)
\t\t)
\t\t(property "Sheetfile" "{_escape(child_filename)}"
\t\t\t(at 26.67 57.15 0)
\t\t\t(effects
\t\t\t\t(font
\t\t\t\t\t(face "Arimo")
\t\t\t\t\t(size 1.27 1.27)
\t\t\t\t)
\t\t\t\t(justify left top)
\t\t\t)
\t\t)
\t\t(instances
\t\t\t(project "{_escape(title)}"
\t\t\t\t(path "/{sheet_uuid}"
\t\t\t\t\t(page "2")
\t\t\t\t)
\t\t\t)
\t\t)
\t)
\t(sheet_instances
\t\t(path "/"
\t\t\t(page "1")
\t\t)
\t)
\t(embedded_fonts no)
)
"""


def _render_child_schematic(*, child_title: str) -> str:
    title = _escape(child_title)
    return f"""(kicad_sch
\t(version 20250114)
\t(generator "eeschema")
\t(generator_version "9.0")
\t(uuid "{uuid4()}")
\t(paper "A4")
\t(title_block
\t\t(title "{title}")
\t\t(date "{date.today().isoformat()}")
\t\t(rev "${{DRAWING_REVISION}}")
\t\t(company "${{COMPANY}}")
\t)
\t(lib_symbols)
\t(text_box "{title}\\nChild sheet skeleton"
\t\t(exclude_from_sim no)
\t\t(at 20.32 20.32 0)
\t\t(size 101.6 20.32)
\t\t(margins 2 2 2 2)
\t\t(stroke
\t\t\t(width 0.1524)
\t\t\t(type solid)
\t\t)
\t\t(fill
\t\t\t(type none)
\t\t)
\t\t(effects
\t\t\t(font
\t\t\t\t(size 2.54 2.54)
\t\t\t\t(bold yes)
\t\t\t)
\t\t\t(justify left top)
\t\t)
\t\t(uuid "{uuid4()}")
\t)
\t(embedded_fonts no)
)
"""


def _append_sheet_block(
    text: str,
    *,
    sheet_name: str,
    child_filename: str,
    project_name: str,
) -> str:
    sheet_uuid = str(uuid4())
    block = f"""\t(sheet
\t\t(at 25.4 100.33)
\t\t(size 101.6 25.4)
\t\t(exclude_from_sim no)
\t\t(in_bom yes)
\t\t(on_board yes)
\t\t(dnp no)
\t\t(stroke
\t\t\t(width 0.1524)
\t\t\t(type solid)
\t\t)
\t\t(fill
\t\t\t(color 0 0 0 0.0000)
\t\t)
\t\t(uuid "{sheet_uuid}")
\t\t(property "Sheetname" "{_escape(sheet_name)}"
\t\t\t(at 25.4 99.3009 0)
\t\t\t(effects
\t\t\t\t(font
\t\t\t\t\t(face "Tinos")
\t\t\t\t\t(size 1.905 1.905)
\t\t\t\t\t(bold yes)
\t\t\t\t\t(color 0 0 0 1)
\t\t\t\t)
\t\t\t\t(justify left bottom)
\t\t\t)
\t\t)
\t\t(property "Sheetfile" "{_escape(child_filename)}"
\t\t\t(at 26.67 101.6 0)
\t\t\t(effects
\t\t\t\t(font
\t\t\t\t\t(face "Arimo")
\t\t\t\t\t(size 1.27 1.27)
\t\t\t\t)
\t\t\t\t(justify left top)
\t\t\t)
\t\t)
\t\t(instances
\t\t\t(project "{_escape(project_name)}"
\t\t\t\t(path "/{sheet_uuid}"
\t\t\t\t\t(page "2")
\t\t\t\t)
\t\t\t)
\t\t)
\t)
"""
    marker = "\n\t(sheet_instances"
    if marker in text:
        return text.replace(marker, f"\n{block}{marker}", 1)
    if text.endswith("\n)"):
        return text[:-2] + f"\n{block})\n"
    return text + f"\n{block}"


def _replace_title_block_title(text: str, title: str) -> str:
    return re.sub(r'\(title\s+"[^"]*"\)', f'(title "{_escape(title)}")', text, count=1)


def _read_template(path: Path) -> str:
    if not path.exists():
        raise BoardwrightError(f"Missing schematic template: {path}")
    return path.read_text(encoding="utf-8")


def _write_text(path: Path, text: str, *, force: bool) -> None:
    if path.exists() and not force:
        raise BoardwrightError(f"Refusing to overwrite existing file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')
