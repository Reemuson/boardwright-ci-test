# SPDX-License-Identifier: LicenseRef-Boardwright-Source-Available
# Copyright (c) Ryan Hicks

"""Silkscreen completeness checks.

Text that is visible on a PCB silkscreen layer is part of the fabrication
design. Gerber plot options can still drop it: KiCad's "plot footprint values"
and "plot footprint references" options suppress not only the Value and
Reference fields but also any footprint user text that is exactly
``${VALUE}`` or ``${REFERENCE}`` (for example a test-point net label).

Two checks guard against that:

* :func:`plot_option_omissions` (static, used by ``validate``) lists visible
  silkscreen text that the configured plot options would drop.
* :func:`compare_gerbers` (geometry, used in CI) compares the shipped
  silkscreen Gerbers with an unfiltered reference plot of the same board,
  primitive by primitive, after resolving aperture numbers to their
  definitions. Any primitive missing from, or added to, the shipped layer is
  reported with its location.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from . import sexpr

SILK_LAYERS = ("F.SilkS", "B.SilkS")
SILK_GERBER_SUFFIXES = ("-F_Silkscreen.gbr", "-B_Silkscreen.gbr")
GERBER_CONFIG = Path("boardwright_resources/kibot/yaml/kibot_out_gerber.yaml")
KIBOT_MAIN = Path("boardwright_resources/kibot/yaml/kibot_main.yaml")
REFERENCE_DIR = Path(".boardwright-checks/silkscreen_reference")


# --- Intended silkscreen text (from the PCB source) --------------------------


@dataclass(frozen=True)
class SilkText:
    footprint: str  # reference designator, or "" for board-level text
    kind: str  # "reference", "value", "field" or "text"
    text: str
    layer: str

    def describe(self) -> str:
        owner = self.footprint or "board"
        return f"{owner} {self.kind} {self.text!r} on {self.layer}"


def _hidden(node: sexpr.SList) -> bool:
    for item in node.items:
        if item == "hide":
            return True
        if isinstance(item, sexpr.SList):
            if item.head == "hide" and item.value() in ("", "yes"):
                return True
            if item.head == "effects" and _hidden(item):
                return True
    return False


def _layer(node: sexpr.SList) -> str:
    layer = node.child("layer")
    return layer.value() if layer is not None else ""


def intended_silkscreen_text(pcb_text: str) -> list[SilkText]:
    """Every text item the PCB shows on a silkscreen layer."""
    root = sexpr.parse(pcb_text)
    items: list[SilkText] = []
    for footprint in root.children("footprint"):
        reference = ""
        for prop in footprint.children("property"):
            if prop.value() == "Reference":
                reference = prop.value(2)
        for prop in footprint.children("property"):
            layer = _layer(prop)
            if layer in SILK_LAYERS and not _hidden(prop):
                name = prop.value()
                kind = {"Reference": "reference", "Value": "value"}.get(name, "field")
                items.append(SilkText(reference, kind, prop.value(2), layer))
        for text in footprint.children("fp_text"):
            layer = _layer(text)
            if layer in SILK_LAYERS and not _hidden(text):
                kind = {"reference": "reference", "value": "value"}.get(text.value(), "text")
                items.append(SilkText(reference, kind, text.value(2), layer))
    for text in root.children("gr_text"):
        layer = _layer(text)
        if layer in SILK_LAYERS and not _hidden(text):
            items.append(SilkText("", "text", text.value(), layer))
    return items


def plot_option_omissions(
    items: list[SilkText], *, plot_refs: bool, plot_values: bool
) -> list[SilkText]:
    """Visible silkscreen text that these KiCad plot options would not plot.

    Mirrors KiCad's footprint text plotting: with references off, the
    Reference field and user text exactly ``${REFERENCE}`` are skipped; with
    values off, the Value field and user text exactly ``${VALUE}``.
    """
    omitted = []
    for item in items:
        is_ref = item.kind == "reference" or (item.footprint and item.text == "${REFERENCE}")
        is_value = item.kind == "value" or (item.footprint and item.text == "${VALUE}")
        if (is_ref and not plot_refs) or (is_value and not plot_values):
            omitted.append(item)
    return omitted


# --- KiBot Gerber options -----------------------------------------------------


def gerber_plot_options(root: Path) -> dict[str, bool]:
    """Resolve plot_footprint_refs/values for the release Gerber output.

    Resolution follows KiBot's template order (see :mod:`kibot_config`).
    Raises ValueError when a value cannot be resolved.
    """
    from .kibot_config import Resolver

    main_path = root / KIBOT_MAIN
    resolver = Resolver(
        (root / GERBER_CONFIG).read_text(encoding="utf-8"),
        main_path.read_text(encoding="utf-8") if main_path.exists() else "",
        GERBER_CONFIG.name,
    )
    result = {}
    for key in ("plot_footprint_refs", "plot_footprint_values"):
        value = resolver.option(key)
        if value is None:
            # KiBot defaults both to true.
            result[key] = True
            continue
        if value.lower() not in ("true", "false"):
            raise ValueError(f"{GERBER_CONFIG}: {key} resolves to {value!r}, expected true/false")
        result[key] = value.lower() == "true"
    return result


def validate_plot_options(root: Path, pcb_text: str) -> list[str]:
    """Problems for ``validate``: visible silkscreen text the Gerbers would drop."""
    if not (root / GERBER_CONFIG).exists():
        return []
    try:
        options = gerber_plot_options(root)
    except ValueError as exc:
        return [str(exc)]
    omitted = plot_option_omissions(
        intended_silkscreen_text(pcb_text),
        plot_refs=options["plot_footprint_refs"],
        plot_values=options["plot_footprint_values"],
    )
    return [
        f"Silkscreen text omitted from Gerbers by plot options: {item.describe()}. "
        "Enable the plot option, or hide the text in the PCB if it is not meant to be fabricated."
        for item in omitted
    ]


# --- Gerber geometry ----------------------------------------------------------

_APERTURE_RE = re.compile(r"%ADD(\d+)([^*]*)\*%")
_COORD_RE = re.compile(r"^(?:G0?([123])\*?)?(?:X(-?\d+))?(?:Y(-?\d+))?(?:I(-?\d+))?(?:J(-?\d+))?D0?([123])\*$")


@dataclass(frozen=True)
class Primitive:
    kind: str  # "draw", "flash" or "region"
    aperture: str
    polarity: str
    points: tuple

    def bbox(self) -> tuple[float, float, float, float]:
        xs = [p[0] for p in self.points]
        ys = [p[1] for p in self.points]
        return (min(xs) / 1e6, -max(ys) / 1e6, max(xs) / 1e6, -min(ys) / 1e6)


def gerber_primitives(text: str) -> Counter:
    """Multiset of drawing primitives, with D-codes replaced by definitions.

    Coordinates stay in file units, so both files must use the same format
    (Boardwright's outputs and reference both use KiCad's 4.6 mm format).
    """
    apertures: dict[str, str] = {}
    prims: Counter = Counter()
    aperture = ""
    polarity = "D"
    mode = "1"
    x = y = 0
    region: list | None = None
    contour: list = []

    def close_contour() -> None:
        nonlocal contour
        if region is not None and len(contour) > 1:
            region.append(tuple(contour))
        contour = []

    for raw in text.splitlines():
        line = raw.strip()
        # Comments, X2 attributes and aperture-macro bodies carry no geometry
        # of their own (macros are compared through the %ADD definition).
        if not line or line.startswith(("G04", "%T", "%AM")):
            continue
        m = _APERTURE_RE.match(line)
        if m:
            apertures[f"D{m.group(1)}"] = m.group(2)
            continue
        if line.startswith("%LP"):
            polarity = line[3]
            continue
        if line.startswith("G36"):
            region, contour = [], []
            continue
        if line.startswith("G37"):
            close_contour()
            if region:
                prims[Primitive("region", "", polarity, tuple(region))] += 1
            region = None
            continue
        m = re.fullmatch(r"(?:G54)?(D\d{2,})\*", line)
        if m:
            aperture = apertures.get(m.group(1), m.group(1))
            continue
        if re.fullmatch(r"G0?[123]\*", line):
            mode = line[-2]
            continue
        m = _COORD_RE.match(line)
        if not m:
            continue
        if m.group(1):
            mode = m.group(1)
        nx = int(m.group(2)) if m.group(2) is not None else x
        ny = int(m.group(3)) if m.group(3) is not None else y
        op = m.group(6)
        arc = (int(m.group(4) or 0), int(m.group(5) or 0)) if mode in "23" and op == "1" else None
        if region is not None:
            if op == "2":
                close_contour()
                contour = [(nx, ny)]
            elif op == "1":
                if not contour:
                    contour = [(x, y)]
                contour.append((nx, ny, mode, arc) if arc else (nx, ny))
        elif op == "1":
            points = ((x, y), (nx, ny)) if (x, y) <= (nx, ny) or arc else ((nx, ny), (x, y))
            prims[Primitive("draw", aperture, polarity, points + ((mode, arc),) if arc else points)] += 1
        elif op == "3":
            prims[Primitive("flash", aperture, polarity, ((nx, ny),))] += 1
        x, y = nx, ny
    return prims


def _region_points(prim: Primitive) -> list[tuple[int, int]]:
    if prim.kind != "region":
        return [p for p in prim.points if isinstance(p, tuple) and len(p) == 2 and isinstance(p[0], int)]
    return [(p[0], p[1]) for contour in prim.points for p in contour]


def _location(prim: Primitive) -> tuple[float, float, float, float]:
    points = _region_points(prim)
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    # Gerber Y is negated board Y in KiCad output.
    return (min(xs) / 1e6, -max(ys) / 1e6, max(xs) / 1e6, -min(ys) / 1e6)


@dataclass
class SilkDifference:
    layer: str
    change: str  # "missing" or "extra"
    count: int
    bbox: tuple[float, float, float, float]
    near: str = ""

    def __str__(self) -> str:
        x0, y0, x1, y1 = self.bbox
        where = f" near {self.near}" if self.near else ""
        verb = "missing from shipped Gerber" if self.change == "missing" else "not in reference plot"
        return (
            f"{self.layer}: {self.count} primitive(s) {verb} at x {x0:.2f}..{x1:.2f}, "
            f"y {y0:.2f}..{y1:.2f} mm{where}"
        )


def _cluster(prims: list[Primitive], gap: float = 1.0) -> list[tuple[int, tuple[float, float, float, float]]]:
    boxes = [[1, list(_location(p))] for p in prims]
    merged = True
    while merged:
        merged = False
        for i in range(len(boxes)):
            for j in range(len(boxes) - 1, i, -1):
                a, b = boxes[i][1], boxes[j][1]
                if a[0] - gap <= b[2] and b[0] - gap <= a[2] and a[1] - gap <= b[3] and b[1] - gap <= a[3]:
                    boxes[i][0] += boxes[j][0]
                    boxes[i][1] = [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]
                    del boxes[j]
                    merged = True
    return [(count, tuple(box)) for count, box in boxes]


def compare_gerbers(layer: str, shipped: str, reference: str) -> list[SilkDifference]:
    have = gerber_primitives(shipped)
    want = gerber_primitives(reference)
    differences = []
    for change, delta in (("missing", want - have), ("extra", have - want)):
        prims = list(delta.elements())
        for count, box in _cluster(prims):
            differences.append(SilkDifference(layer, change, count, box))
    return differences


def footprint_positions(pcb_text: str) -> list[tuple[str, float, float]]:
    positions = []
    for footprint in sexpr.parse(pcb_text).children("footprint"):
        at = footprint.child("at")
        reference = next(
            (p.value(2) for p in footprint.children("property") if p.value() == "Reference"), ""
        )
        if at is not None and reference:
            positions.append((reference, float(at.value(1)), float(at.value(2))))
    return positions


def annotate(differences: list[SilkDifference], pcb_text: str) -> None:
    positions = footprint_positions(pcb_text)
    for diff in differences:
        x0, y0, x1, y1 = diff.bbox
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        best = min(positions, key=lambda p: (p[1] - cx) ** 2 + (p[2] - cy) ** 2, default=None)
        if best and ((best[1] - cx) ** 2 + (best[2] - cy) ** 2) ** 0.5 <= 6.0:
            diff.near = best[0]


def check_directories(shipped_dir: Path, reference_dir: Path, pcb_text: str = "") -> tuple[list[SilkDifference], list[str]]:
    """Compare every silkscreen Gerber in ``reference_dir`` with ``shipped_dir``."""
    problems: list[str] = []
    differences: list[SilkDifference] = []
    references = sorted(p for p in reference_dir.glob("*.gbr") if p.name.endswith(SILK_GERBER_SUFFIXES))
    if not references:
        problems.append(f"No silkscreen reference Gerbers in {reference_dir}.")
    for ref in references:
        shipped = shipped_dir / ref.name
        if not shipped.exists():
            problems.append(f"{ref.name}: silkscreen layer missing from {shipped_dir}.")
            continue
        differences.extend(
            compare_gerbers(ref.name, shipped.read_text(encoding="utf-8"), ref.read_text(encoding="utf-8"))
        )
    if pcb_text:
        annotate(differences, pcb_text)
    return differences, problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m boardwright.silkscreen",
        description="Verify shipped silkscreen Gerbers against an unfiltered reference plot.",
    )
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--gerbers", type=Path, default=Path("Manufacturing/Fabrication/Gerbers"))
    parser.add_argument("--reference", type=Path, default=REFERENCE_DIR)
    parser.add_argument("--keep-reference", action="store_true", help="Do not delete the reference plot afterwards.")
    args = parser.parse_args(argv)

    root = args.root
    gerbers = root / args.gerbers
    reference = root / args.reference
    try:
        if not gerbers.exists():
            if reference.exists():
                print(f"error: a silkscreen reference was plotted but {args.gerbers} is missing.", file=sys.stderr)
                return 1
            print(f"No Gerbers at {args.gerbers}; silkscreen check skipped (no fabrication outputs in this run).")
            return 0
        pcbs = sorted(root.glob("*.kicad_pcb"))
        pcb_text = pcbs[0].read_text(encoding="utf-8") if len(pcbs) == 1 else ""
        differences, problems = check_directories(gerbers, reference, pcb_text)
    finally:
        if not args.keep_reference and reference.exists():
            shutil.rmtree(reference, ignore_errors=True)
            parent = reference.parent
            if parent.name == ".boardwright-checks" and parent.exists() and not any(parent.iterdir()):
                parent.rmdir()
    for problem in problems:
        print(f"error: {problem}", file=sys.stderr)
    for diff in differences:
        print(f"error: {diff}", file=sys.stderr)
    if problems or differences:
        print(
            "Shipped silkscreen differs from the board's visible silkscreen. A plot option is dropping "
            "or adding markings; fix the KiBot Gerber options or the PCB, do not ship this package.",
            file=sys.stderr,
        )
        return 1
    print("Silkscreen Gerbers contain every visible silkscreen item of the board (geometry match).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
