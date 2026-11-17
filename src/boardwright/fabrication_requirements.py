# SPDX-License-Identifier: LicenseRef-Boardwright-Source-Available
# Copyright (c) Ryan Hicks

"""Derive the fabrication-note minimum feature sizes from structured KiCad data.

Every value states where it came from. Nothing falls back to a guessed default:
if a requirement cannot be derived reliably, :func:`derive_requirements` raises
:class:`FabricationRequirementError` listing every problem, so a release cannot
publish fabrication notes that silently understate (or overstate) the board.

Sources, by requirement:

``trace_width``
    Minimum width of routed copper (``segment``/``arc``) on the board, checked
    against the board-setup ``min_track_width``. With no routed copper, the
    board-setup minimum is stated instead.
``trace_to_trace``
    Design-rule clearance, not measured geometry: the smallest copper
    clearance any net class, item-local override or custom-rule ``clearance``
    constraint allows, floored by the board-setup ``min_clearance`` (KiCad
    always enforces board-setup minimums). A local override such as a
    fiducial's ``(clearance 0.6)`` can only *raise* a single item's clearance
    above the floor; it is never the board minimum by itself.
``min_pth``
    Smallest plated hole: every via drill and every plated pad drill (smaller
    axis of a slot).
``min_npth``
    Smallest non-plated pad drill.
``annular_ring``
    Smallest outer annular ring over vias *and* plated pads, accounting for
    slots, drill offsets and per-layer padstacks.
``copper_to_hole`` / ``copper_to_edge`` / ``hole_to_hole``
    Board-setup ``min_hole_clearance`` / ``min_copper_edge_clearance`` /
    ``min_hole_to_hole``. Custom rules may only raise these for subsets of
    items, so the board-setup value is the board-wide minimum.

Measured copper-to-copper spacing needs a DRC run and is out of scope here; a
DRC violation below the stated clearance is a design deviation to record, not a
reason to change the stated requirement.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .errors import BoardwrightError
from .sexpr import SList, parse

TOLERANCE = 1e-6

# project.yaml `manufacturing` keys that put a manufacturing statement in the
# fabrication notes. They must be declared by the project: a missing key would
# otherwise be filled from a Boardwright default, so updating the Boardwright
# payload could silently change an approved specification. Declare a key empty
# (or `null` for the booleans) to omit its statement deliberately.
DECLARED_MANUFACTURING_KEYS = (
    "manufacturing_standard",
    "core_material",
    "flammability_rating",
    "tg_rating",
    "rohs_pb_free",
    "halogen_free",
    "tented_vias",
    "silkscreen_enabled",
    "conformal_coating",
)


def undeclared_manufacturing_keys(manufacturing: object) -> list[str]:
    """Return the fabrication-note requirement keys a project has not declared."""
    declared = manufacturing if isinstance(manufacturing, dict) else {}
    return [key for key in DECLARED_MANUFACTURING_KEYS if key not in declared]


# Template references that state each requirement: ${placeholder} names, or the
# key itself in a `#?` condition line.
_REQUIREMENT_PLACEHOLDERS = {
    "manufacturing_standard": ("manufacturing_standard_note", "manufacturing_standard_cap"),
    "core_material": ("material_requirements_block", "core_material_cap"),
    "flammability_rating": ("material_requirements_block", "flammability_rating_cap"),
    "tg_rating": ("material_requirements_block", "tg_rating_cap"),
    "rohs_pb_free": ("rohs_note", "material_requirements_block"),
    "halogen_free": ("material_requirements_block",),
    "tented_vias": ("tented_vias_note",),
    "silkscreen_enabled": ("silkscreen_note",),
    "conformal_coating": ("conformal_coating_note",),
}


def unstated_requirements(template: str, manufacturing: object) -> list[str]:
    """Declared requirements that make a statement but the template never states.

    A requirement declared empty (text) or null (boolean) makes no statement and
    needs no reference. Guards against a project-owned template silently
    dropping a declared requirement.
    """
    declared = manufacturing if isinstance(manufacturing, dict) else {}
    conditions = " ".join(
        line.strip()[2:] for line in template.splitlines() if line.strip().startswith("#?")
    )
    unstated: list[str] = []
    for key, placeholders in _REQUIREMENT_PLACEHOLDERS.items():
        value = declared.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        if any("${" + name + "}" in template for name in placeholders):
            continue
        if re.search(rf"\b{key}\b", conditions):
            continue
        unstated.append(key)
    return unstated


def undeclared_message(missing: list[str]) -> str:
    return (
        "project.yaml does not declare these fabrication-note requirements: "
        + ", ".join(f"manufacturing.{key}" for key in missing)
        + ". Declare each explicitly (empty or null omits its note) so the fabrication notes state "
        "the project's approved specification rather than a Boardwright default."
    )


class FabricationRequirementError(BoardwrightError):
    """A fabrication requirement is missing, inconsistent or not derivable."""


@dataclass(frozen=True)
class Requirement:
    value: float | None
    source: str

    def text(self) -> str:
        return "N/A" if self.value is None else f"{self.value:.3f}"


@dataclass
class FabricationRequirements:
    trace_width: Requirement
    trace_to_trace: Requirement
    min_pth: Requirement
    min_npth: Requirement
    annular_ring: Requirement
    copper_to_hole: Requirement
    copper_to_edge: Requirement
    hole_to_hole: Requirement
    thickness: Requirement
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Requirement]:
        return {
            name: getattr(self, name)
            for name in (
                "trace_width",
                "trace_to_trace",
                "min_pth",
                "min_npth",
                "annular_ring",
                "copper_to_hole",
                "copper_to_edge",
                "hole_to_hole",
                "thickness",
            )
        }


@dataclass(frozen=True)
class _Hole:
    kind: str  # "via" or "pad"
    ref: str
    plated: bool
    drill: tuple[float, float]  # (x, y); equal for round holes
    offset: tuple[float, float]
    copper: tuple[tuple[float, float], ...]  # outer copper sizes (w, h) per layer


def derive_requirements(
    pcb_text: str,
    project_text: str,
    dru_text: str | None = None,
) -> FabricationRequirements:
    """Return the board's fabrication requirements or raise listing all problems."""
    problems: list[str] = []
    warnings: list[str] = []
    try:
        pcb = parse(pcb_text)
    except BoardwrightError as error:
        raise FabricationRequirementError(f"Cannot parse the PCB: {error}") from error
    try:
        project = json.loads(project_text)
    except ValueError as error:
        raise FabricationRequirementError(f"Cannot parse the KiCad project file: {error}") from error

    rules = (((project.get("board") or {}).get("design_settings") or {}).get("rules")) or {}

    def board_rule(key: str) -> float | None:
        value = rules.get(key)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            problems.append(f"Board setup rule '{key}' is missing or not positive in the KiCad project.")
            return None
        return float(value)

    min_clearance = board_rule("min_clearance")
    min_track = board_rule("min_track_width")
    copper_to_hole = board_rule("min_hole_clearance")
    copper_to_edge = board_rule("min_copper_edge_clearance")
    hole_to_hole = board_rule("min_hole_to_hole")

    # Trace to trace: design-rule clearance floored by the board minimum.
    clearances: list[tuple[float, str]] = []
    for netclass in ((project.get("net_settings") or {}).get("classes")) or []:
        value = netclass.get("clearance")
        if isinstance(value, (int, float)) and value > 0:
            clearances.append((float(value), f"net class '{netclass.get('name', '?')}'"))
    if not clearances:
        problems.append("No net class defines a copper clearance in the KiCad project.")
    for value, owner in _local_clearance_overrides(pcb):
        clearances.append((value, owner))
    for value in _dru_constraint_minimums(dru_text, "clearance", problems):
        clearances.append((value, "custom rule (.kicad_dru)"))
    trace_to_trace = Requirement(None, "")
    if clearances and min_clearance is not None:
        smallest, owner = min(clearances, key=lambda item: (item[0], not item[1].startswith("net class"), item[1]))
        ties = sum(1 for value, _ in clearances if abs(value - smallest) <= TOLERANCE)
        if ties > 1:
            owner = f"{owner} and {ties - 1} other source(s)"
        if smallest + TOLERANCE < min_clearance:
            trace_to_trace = Requirement(
                min_clearance,
                f"board setup min_clearance (floors {owner} {smallest:.3f} mm)",
            )
        else:
            trace_to_trace = Requirement(
                max(smallest, min_clearance),
                f"smallest design-rule clearance ({owner}), board floor {min_clearance:.3f} mm",
            )

    # Trace width: routed geometry, checked against the board minimum.
    widths = [
        float(width.value())
        for head in ("segment", "arc")
        for item in pcb.children(head)
        if (width := item.child("width")) is not None and _is_number(width.value())
    ]
    trace_width = Requirement(None, "")
    if widths:
        trace_width = Requirement(min(widths), f"narrowest routed track ({len(widths)} tracks)")
        if min_track is not None and min(widths) + TOLERANCE < min_track:
            problems.append(
                f"Routed track width {min(widths):.3f} mm is below the board minimum {min_track:.3f} mm."
            )
    elif min_track is not None:
        trace_width = Requirement(min_track, "board setup min_track_width (no routed tracks)")

    # Holes and annular rings: vias and pads together.
    holes = _collect_holes(pcb, problems)
    plated = [hole for hole in holes if hole.plated]
    unplated = [hole for hole in holes if not hole.plated]
    min_pth = _min_drill(plated, "plated")
    min_npth = _min_drill(unplated, "non-plated")
    annular_ring = Requirement(None, "no plated holes")
    rings: list[tuple[float, str]] = []
    for hole in plated:
        ring = _annular_ring(hole)
        if ring is None:
            continue
        if ring <= TOLERANCE:
            problems.append(f"{hole.ref} has no annular ring ({ring:.3f} mm): copper does not surround the drill.")
            continue
        rings.append((ring, hole.ref))
    if rings:
        ring, ref = min(rings)
        count = sum(1 for value, _ in rings if abs(value - ring) <= TOLERANCE)
        annular_ring = Requirement(ring, f"smallest outer annular ring ({ref}; {count} item(s) at this size)")

    thickness = Requirement(None, "")
    general = pcb.child("general")
    thickness_node = general.child("thickness") if general is not None else None
    if thickness_node is None or not _is_number(thickness_node.value()) or float(thickness_node.value()) <= 0:
        problems.append("The PCB does not declare a positive board thickness in (general (thickness ...)).")
    else:
        thickness = Requirement(float(thickness_node.value()), "PCB general thickness")

    if problems:
        raise FabricationRequirementError(
            "Fabrication requirements cannot be derived reliably:\n- " + "\n- ".join(problems)
        )
    return FabricationRequirements(
        trace_width=trace_width,
        trace_to_trace=trace_to_trace,
        min_pth=min_pth,
        min_npth=min_npth,
        annular_ring=annular_ring,
        copper_to_hole=Requirement(copper_to_hole, "board setup min_hole_clearance"),
        copper_to_edge=Requirement(copper_to_edge, "board setup min_copper_edge_clearance"),
        hole_to_hole=Requirement(hole_to_hole, "board setup min_hole_to_hole"),
        thickness=thickness,
        warnings=warnings,
    )


def derive_project_requirements(root: Path, pcb_path: Path) -> FabricationRequirements:
    project_path = pcb_path.with_suffix(".kicad_pro")
    if not project_path.is_file():
        raise FabricationRequirementError(f"KiCad project file not found: {project_path.name}")
    dru_path = pcb_path.with_suffix(".kicad_dru")
    return derive_requirements(
        pcb_path.read_text(encoding="utf-8"),
        project_path.read_text(encoding="utf-8"),
        dru_path.read_text(encoding="utf-8") if dru_path.is_file() else None,
    )


def _is_number(text: str) -> bool:
    try:
        float(text)
    except (TypeError, ValueError):
        return False
    return True


def _numbers(node: SList | None, start: int = 1) -> list[float]:
    if node is None:
        return []
    return [float(item) for item in node.items[start:] if isinstance(item, str) and _is_number(item)]


def _footprint_reference(footprint: SList) -> str:
    for prop in footprint.children("property"):
        if prop.value(1) == "Reference":
            return prop.value(2)
    for text in footprint.children("fp_text"):
        if text.value(1) == "reference":
            return text.value(2)
    return "?"


def _local_clearance_overrides(pcb: SList) -> list[tuple[float, str]]:
    overrides: list[tuple[float, str]] = []
    for footprint in pcb.children("footprint"):
        ref = _footprint_reference(footprint)
        for value in _numbers(footprint.child("clearance")):
            if value > 0:
                overrides.append((value, f"footprint {ref} local clearance"))
        for pad in footprint.children("pad"):
            for value in _numbers(pad.child("clearance")):
                if value > 0:
                    overrides.append((value, f"pad {ref}.{pad.value(1)} local clearance"))
    return overrides


def _dru_constraint_minimums(dru_text: str | None, name: str, problems: list[str]) -> list[float]:
    if not dru_text:
        return []
    # A .kicad_dru holds several top-level forms; wrap them to parse once.
    stripped = "\n".join(line for line in dru_text.splitlines() if not line.lstrip().startswith("#"))
    try:
        tree = parse("(dru " + stripped + ")")
    except BoardwrightError as error:
        problems.append(f"Cannot parse the custom design rules (.kicad_dru): {error}")
        return []
    values: list[float] = []
    for rule in tree.children("rule"):
        for constraint in rule.children("constraint"):
            if constraint.value(1) != name:
                continue
            minimum = constraint.child("min")
            if minimum is None:
                continue
            value = _parse_length(minimum.value(1))
            if value is None:
                problems.append(f"Custom rule {rule.value(1)!r} has an unreadable {name} minimum.")
            elif value > 0:
                values.append(value)
    return values


def _parse_length(text: str) -> float | None:
    match = re.fullmatch(r"([0-9]*\.?[0-9]+)\s*(mm|mil|in|um)?", text.strip())
    if not match:
        return None
    scale = {"mm": 1.0, None: 1.0, "mil": 0.0254, "in": 25.4, "um": 0.001}[match.group(2)]
    return float(match.group(1)) * scale


def _drill(node: SList | None) -> tuple[tuple[float, float], tuple[float, float]] | None:
    if node is None:
        return None
    items = [item for item in node.items[1:] if isinstance(item, str)]
    oval = bool(items) and items[0] == "oval"
    sizes = [float(item) for item in items if _is_number(item)]
    if not sizes:
        return None
    x = sizes[0]
    y = sizes[1] if oval and len(sizes) > 1 else x
    offset = tuple(_numbers(node.child("offset"))[:2]) or (0.0, 0.0)
    return (x, y), (offset[0], offset[1]) if len(offset) == 2 else (0.0, 0.0)


def _padstack_sizes(node: SList) -> list[tuple[float, float]]:
    sizes: list[tuple[float, float]] = []
    padstack = node.child("padstack")
    if padstack is None:
        return sizes
    for layer in padstack.children("layer"):
        values = _numbers(layer.child("size"))
        if len(values) >= 2:
            sizes.append((values[0], values[1]))
        elif len(values) == 1:
            sizes.append((values[0], values[0]))
    return sizes


def _collect_holes(pcb: SList, problems: list[str]) -> list[_Hole]:
    holes: list[_Hole] = []
    for index, via in enumerate(pcb.children("via"), start=1):
        drill = _drill(via.child("drill"))
        size = _numbers(via.child("size"))
        at = _numbers(via.child("at"))
        ref = f"via at ({at[0]:g}, {at[1]:g})" if len(at) >= 2 else f"via #{index}"
        if drill is None or not size:
            problems.append(f"{ref} has no readable size or drill.")
            continue
        copper = [(size[0], size[0])] + _padstack_sizes(via)
        holes.append(_Hole("via", ref, True, drill[0], drill[1], tuple(copper)))
    for footprint in pcb.children("footprint"):
        ref = _footprint_reference(footprint)
        for pad in footprint.children("pad"):
            kind = pad.value(2)
            if kind not in {"thru_hole", "np_thru_hole"}:
                continue
            name = f"pad {ref}.{pad.value(1) or '?'}"
            drill = _drill(pad.child("drill"))
            if drill is None or drill[0][0] <= 0:
                problems.append(f"{name} is a {kind} pad without a readable drill.")
                continue
            plated = kind == "thru_hole"
            size = _numbers(pad.child("size"))
            copper: list[tuple[float, float]] = []
            if len(size) >= 2:
                copper.append((size[0], size[1]))
            copper.extend(_padstack_sizes(pad))
            if plated and not copper:
                problems.append(f"{name} is plated but has no readable copper size.")
                continue
            holes.append(_Hole("pad", name, plated, drill[0], drill[1], tuple(copper)))
    return holes


def _min_drill(holes: list[_Hole], label: str) -> Requirement:
    if not holes:
        return Requirement(None, f"no {label} holes")
    smallest = min(min(hole.drill) for hole in holes)
    vias = sum(1 for hole in holes if hole.kind == "via" and abs(min(hole.drill) - smallest) <= TOLERANCE)
    pads = sum(1 for hole in holes if hole.kind == "pad" and abs(min(hole.drill) - smallest) <= TOLERANCE)
    return Requirement(smallest, f"smallest {label} drill ({vias} via(s), {pads} pad(s) at this size)")


def _annular_ring(hole: _Hole) -> float | None:
    if not hole.copper:
        return None
    dx, dy = abs(hole.offset[0]), abs(hole.offset[1])
    rings = [
        min((width - hole.drill[0]) / 2 - dx, (height - hole.drill[1]) / 2 - dy)
        for width, height in hole.copper
    ]
    return round(min(rings), 6)


__all__ = [
    "FabricationRequirementError",
    "FabricationRequirements",
    "Requirement",
    "derive_project_requirements",
    "derive_requirements",
    "undeclared_manufacturing_keys",
]
