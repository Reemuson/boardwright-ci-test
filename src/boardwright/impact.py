# SPDX-License-Identifier: LicenseRef-Boardwright-Source-Available
# Copyright (c) Ryan Hicks

"""Release change-impact assessment.

Compares the current design against the latest approved baseline and checks
that the declared change classes and proposed revision identities are
consistent with what actually changed. The assessment never edits revision
identifiers: the PCB revision comes from ``project.board_revision`` and only
changes when an engineer edits it deliberately.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .config import BoardwrightConfig
from .errors import BoardwrightError
from .fingerprint import (
    DesignFingerprint,
    WorkingTreeReader,
    changed_domains,
    compute_fingerprint,
    schematic_pcb_parity,
)
from .release_ledger import (
    Baseline,
    BaselineRef,
    ReleaseRecord,
    comparable_fingerprint,
    latest_baseline,
    load_ledger,
)

CHANGE_CLASSES = ("documentation", "electrical", "fabrication", "assembly", "tooling")
CHANGE_CLASS_HELP = {
    "documentation": "Schematic/drawing presentation or text; no physical or BOM change.",
    "electrical": "Schematic electrical design change (nets, parts, values).",
    "fabrication": "Bare-PCB fabrication data or requirements change.",
    "assembly": "BOM, population, placement or assembly requirements change.",
    "tooling": "KiCad/KiBot format, scripts or metadata only.",
}

DOMAIN_LABELS = {
    "pcb_geometry": "PCB physical design (copper, mask, silk, edge, drills, stackup)",
    "pcb_netnames": "PCB net names and pin functions (IPC netlist, ODB++, Gerber attributes)",
    "fabrication_spec": "Fabrication requirements (materials, finish, fab notes)",
    "assembly": "Assembly/BOM (parts, values, placement, DNP, BOM fields, notes)",
    "pcb_drawings": "PCB drawing layers (fabrication/assembly drawing content)",
    "release_marking": "Release-dependent text on fabrication layers",
    "schematic": "Schematic content",
}
FABRICATION_DATA_DOMAINS = {"pcb_netnames", "fabrication_spec", "release_marking"}


@dataclass(frozen=True)
class Identities:
    pcb_revision: str
    schematic_revision: str
    assembly_revision: str
    fabrication_revision: str


@dataclass(frozen=True)
class ImpactAssessment:
    version: str
    baseline_version: str
    baseline_legacy: bool
    previous: Identities | None
    proposed: Identities
    detected: tuple[str, ...]
    declared: tuple[str, ...]
    justification: str
    fingerprint: DesignFingerprint
    fabrication_baseline: BaselineRef
    assembly_baseline: BaselineRef
    problems: tuple[str, ...] = field(default_factory=tuple)
    reviews: tuple[str, ...] = field(default_factory=tuple)
    format_changed: bool = False

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def documentation_only(self) -> bool:
        return bool(self.declared) and set(self.declared) <= {"documentation", "tooling"}

    def record(self, *, date: str, source_commit: str, variant: str = "") -> ReleaseRecord:
        if self.problems:
            raise BoardwrightError("Cannot record a release with unresolved impact problems.")
        return ReleaseRecord(
            version=self.version,
            date=date,
            source_commit=source_commit,
            previous_version=self.baseline_version,
            variant=variant,
            pcb_revision=self.proposed.pcb_revision,
            schematic_revision=self.proposed.schematic_revision,
            assembly_revision=self.proposed.assembly_revision,
            fabrication_revision=self.proposed.fabrication_revision,
            change_classes=self.declared,
            justification=self.justification,
            fingerprint=self.fingerprint,
            fabrication_baseline=self.fabrication_baseline,
            assembly_baseline=self.assembly_baseline,
        )


def parse_change_classes(values: Iterable[str] | str | None) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, str):
        values = [values]
    classes: list[str] = []
    for value in values:
        for part in re.split(r"[,\s]+", str(value or "")):
            name = part.strip().lower()
            if not name:
                continue
            if name not in CHANGE_CLASSES:
                raise BoardwrightError(
                    f"Unknown change class {name!r}. Use: {', '.join(CHANGE_CLASSES)}."
                )
            if name not in classes:
                classes.append(name)
    return tuple(classes)


def proposed_identities(config: BoardwrightConfig, schematic_revision: str = "") -> Identities:
    return Identities(
        pcb_revision=config.board_revision.strip(),
        schematic_revision=(schematic_revision or str(config.document_settings("schematic").get("revision") or "")).strip(),
        assembly_revision=str(config.document_settings("assembly").get("revision") or "").strip(),
        fabrication_revision=str(config.document_settings("fabrication").get("revision") or "").strip(),
    )


def assess_release_impact(
    config: BoardwrightConfig,
    version: str,
    *,
    change_classes: Iterable[str] | str | None = None,
    justification: str = "",
    schematic_revision: str = "",
    fingerprint: DesignFingerprint | None = None,
    baseline: Baseline | None | str = "auto",
    parity: list[str] | None = None,
    ledger: list[ReleaseRecord] | None = None,
) -> ImpactAssessment:
    declared = parse_change_classes(change_classes)
    reason = " ".join(str(justification or "").split())
    current = fingerprint or compute_fingerprint(WorkingTreeReader(config.root))
    records = ledger if ledger is not None else load_ledger(config.root)
    base = latest_baseline(config.root, records) if baseline == "auto" else baseline
    proposed = proposed_identities(config, schematic_revision)
    problems: list[str] = []
    reviews: list[str] = []

    if not proposed.pcb_revision:
        problems.append("project.board_revision is empty; set the PCB revision explicitly.")
    problems.extend(_schematic_scheme_problems(config, proposed))

    parity_problems = parity if parity is not None else schematic_pcb_parity(WorkingTreeReader(config.root))
    problems.extend(f"Schematic/PCB mismatch: {item}" for item in parity_problems)

    if base is None:
        return ImpactAssessment(
            version=version,
            baseline_version="",
            baseline_legacy=False,
            previous=None,
            proposed=proposed,
            detected=(),
            declared=declared,
            justification=reason or "Initial release.",
            fingerprint=current,
            fabrication_baseline=BaselineRef(f"{proposed.pcb_revision}/1", "initial"),
            assembly_baseline=BaselineRef(_assembly_id(proposed), "initial"),
            problems=tuple(problems),
            reviews=("Initial release: establishes the first approved fabrication and assembly baselines.",),
        )

    previous = Identities(
        base.pcb_revision, base.schematic_revision, base.assembly_revision, base.fabrication_revision
    )
    if current.schema != base.fingerprint.schema:
        problems.append(
            f"Baseline {base.version} fingerprint schema {base.fingerprint.schema} differs from "
            f"current schema {current.schema}; re-baseline explicitly."
        )
    detected = changed_domains(base.fingerprint, current)
    if current.fab_volatile_variables:
        # ${RELEASE_VERSION} etc. on silk/copper render differently for every
        # release, so the fabrication data can never be reused unchanged.
        detected = detected + ("release_marking",)
    format_changed = (
        base.fingerprint.pcb_format != current.pcb_format
        or base.fingerprint.schematic_format != current.schematic_format
    )

    if not declared:
        problems.append(
            "Declare at least one change class ("
            + ", ".join(CHANGE_CLASSES)
            + ")"
            + (f"; detected: {', '.join(detected)}." if detected else ".")
        )
    if not reason:
        problems.append("A change justification is required for every release after the first.")

    pcb_changed = proposed.pcb_revision != previous.pcb_revision
    sch_rev_changed = proposed.schematic_revision != previous.schematic_revision

    # --- Fabrication -------------------------------------------------------
    fab_data_changed = bool(FABRICATION_DATA_DOMAINS & set(detected))
    if "pcb_geometry" in detected:
        if "fabrication" not in declared:
            problems.append("The physical PCB design changed; declare the `fabrication` change class.")
        if not pcb_changed:
            problems.append(
                f"The physical PCB design changed since {base.version} but the PCB revision is still "
                f"{previous.pcb_revision}. Set project.board_revision to a new revision before releasing."
            )
        fab_status = "new"
    elif pcb_changed:
        if "fabrication" not in declared:
            problems.append(
                f"PCB revision changed {previous.pcb_revision} -> {proposed.pcb_revision} but the physical "
                "design is unchanged. Revert board_revision or declare `fabrication` for a deliberate re-spin."
            )
        else:
            reviews.append("New PCB revision with geometry identical to the previous revision: confirm intent.")
        fab_status = "new"
    elif fab_data_changed:
        if "fabrication" not in declared:
            problems.append(
                "Fabrication data changed ("
                + ", ".join(DOMAIN_LABELS[d] for d in detected if d in FABRICATION_DATA_DOMAINS)
                + "); declare the `fabrication` change class."
            )
        if proposed.fabrication_revision == previous.fabrication_revision:
            problems.append(
                "Fabrication data changed; the fabrication drawing revision must change "
                f"(still {previous.fabrication_revision or '(blank)'})."
            )
        reviews.append(
            f"PCB revision {proposed.pcb_revision} is retained with identical geometry; the fabrication "
            "package is re-issued. Confirm the fabricator does not need a new PCB revision."
        )
        if "release_marking" in detected:
            reviews.append(
                "Fabrication layers use release-dependent text ("
                + ", ".join(f"${{{name}}}" for name in current.fab_volatile_variables)
                + "): every release re-issues the fabrication package. Use ${PCB_REVISION} on the "
                "board if the marking should stay stable."
            )
        fab_status = "reissued"
    else:
        if "fabrication" in declared:
            problems.append(
                "`fabrication` was declared but no fabrication input changed; remove it to keep the "
                "approved baseline."
            )
        fab_status = "reused"

    # A PCB revision letter must never be reused for different geometry.
    for record in ledger_records_for_pcb(records, proposed.pcb_revision):
        if comparable_fingerprint(config.root, record).pcb_geometry != current.pcb_geometry:
            problems.append(
                f"PCB revision {proposed.pcb_revision} was released in {record.version} with different "
                "geometry; a revision identifier cannot be reused."
            )
            break

    fab_baseline = _next_fab_baseline(base, proposed, fab_status)

    # --- Assembly ----------------------------------------------------------
    if "assembly" in detected:
        if "assembly" not in declared:
            problems.append("Assembly/BOM inputs changed; declare the `assembly` change class.")
        if proposed.assembly_revision == previous.assembly_revision and not pcb_changed:
            problems.append(
                "Assembly/BOM inputs changed; the assembly drawing revision must change "
                f"(still {previous.assembly_revision or '(blank)'})."
            )
        asm_status = "new"
    else:
        if "assembly" in declared:
            problems.append("`assembly` was declared but no assembly/BOM input changed.")
        asm_status = "reused" if not pcb_changed else "new"
    asm_baseline = (
        BaselineRef(base.assembly_baseline.id, "reused") if asm_status == "reused" else BaselineRef(_assembly_id(proposed), "new")
    )

    # --- PCB drawing layers ------------------------------------------------
    if "pcb_drawings" in detected:
        if not ({"documentation", "fabrication", "assembly"} & set(declared)):
            problems.append(
                "PCB drawing-layer content changed (fabrication/assembly drawings); declare "
                "`documentation`, or `fabrication`/`assembly` if a requirement changed."
            )
        drawing_revision_changed = (
            proposed.fabrication_revision != previous.fabrication_revision
            or proposed.assembly_revision != previous.assembly_revision
        )
        if not drawing_revision_changed and not pcb_changed:
            problems.append(
                "PCB drawing-layer content changed; the fabrication and/or assembly drawing revision "
                "must change."
            )
        reviews.append(
            "PCB drawing-layer content changed. If notes, dimensions or tolerances on the fabrication "
            "drawing changed a requirement, declare `fabrication` so the package is re-issued."
        )

    # --- Schematic ---------------------------------------------------------
    if "schematic" in detected:
        if not ({"documentation", "electrical"} & set(declared)):
            problems.append("The schematic changed; declare `documentation` or `electrical`.")
        if not sch_rev_changed:
            problems.append(
                "The schematic changed; the schematic revision must change "
                f"(still {previous.schematic_revision or '(blank)'})."
            )
    elif sch_rev_changed and "documentation" not in declared and not pcb_changed:
        problems.append(
            f"Schematic revision changed {previous.schematic_revision} -> {proposed.schematic_revision} "
            "without a schematic change; declare `documentation` if only the title block or notes changed."
        )
    if "electrical" in declared and "pcb_geometry" not in detected and "assembly" not in detected:
        reviews.append(
            "Electrical change declared but PCB geometry and BOM are unchanged: confirm the as-built "
            f"PCB revision {proposed.pcb_revision} still implements the schematic."
        )

    if format_changed:
        if "tooling" not in declared:
            problems.append(
                "KiCad file format/generator changed since the baseline; declare the `tooling` change class."
            )
        reviews.append(
            "Sources were saved by a different KiCad version. Equivalence is established on normalised "
            "sources; regenerated outputs are not guaranteed byte-identical to the approved package."
        )
    if base.legacy:
        reviews.append(
            f"Baseline {base.version} predates the release ledger and was reconstructed from its tag."
        )

    return ImpactAssessment(
        version=version,
        baseline_version=base.version,
        baseline_legacy=base.legacy,
        previous=previous,
        proposed=proposed,
        detected=detected,
        declared=declared,
        justification=reason,
        fingerprint=current,
        fabrication_baseline=fab_baseline,
        assembly_baseline=asm_baseline,
        problems=tuple(problems),
        reviews=tuple(reviews),
        format_changed=format_changed,
    )


def ledger_records_for_pcb(records: list[ReleaseRecord], pcb_revision: str) -> list[ReleaseRecord]:
    return [record for record in records if record.pcb_revision == pcb_revision]


def _next_fab_baseline(base: Baseline, proposed: Identities, status: str) -> BaselineRef:
    if status == "reused":
        return BaselineRef(base.fabrication_baseline.id, "reused")
    if status == "new":
        return BaselineRef(f"{proposed.pcb_revision}/1", "new")
    previous_id = base.fabrication_baseline.id
    match = re.match(r"^(.*)/(\d+)$", previous_id)
    issue = int(match.group(2)) + 1 if match and match.group(1) == proposed.pcb_revision else 2
    return BaselineRef(f"{proposed.pcb_revision}/{issue}", "reissued")


def _assembly_id(identities: Identities) -> str:
    return f"{identities.pcb_revision}-{identities.assembly_revision or '0'}"


def _schematic_scheme_problems(config: BoardwrightConfig, proposed: Identities) -> list[str]:
    scheme = str(config.document_settings("schematic").get("revision_scheme") or "lettered").strip()
    if scheme != "board_minor":
        return []
    pattern = re.compile(rf"^{re.escape(proposed.pcb_revision)}\.\d+$")
    if not pattern.match(proposed.schematic_revision):
        return [
            f"Schematic revision {proposed.schematic_revision!r} must use the board_minor scheme "
            f"'{proposed.pcb_revision}.N' for PCB revision {proposed.pcb_revision}."
        ]
    return []


def suggest_schematic_revision(config: BoardwrightConfig, previous: str = "") -> str:
    """Suggest the next schematic revision without applying it."""
    scheme = str(config.document_settings("schematic").get("revision_scheme") or "lettered").strip()
    pcb = config.board_revision.strip()
    if scheme == "board_minor":
        match = re.match(rf"^{re.escape(pcb)}\.(\d+)$", previous)
        return f"{pcb}.{int(match.group(1)) + 1}" if match else f"{pcb}.0"
    if re.fullmatch(r"[A-Z]", previous or ""):
        return chr(ord(previous) + 1) if previous != "Z" else "AA"
    return previous or "A"


def format_impact(assessment: ImpactAssessment) -> list[str]:
    """Render the release identity/impact checklist shared by CLI and TUI."""
    prev = assessment.previous
    proposed = assessment.proposed

    def row(label: str, before: str, after: str) -> str:
        if prev is None:
            return f"{label}: {after or '(blank)'}"
        marker = "unchanged" if before == after else f"{before or '(blank)'} -> {after or '(blank)'}"
        return f"{label}: {after or '(blank)'} ({marker})"

    lines = [
        f"Project release: {assessment.version}"
        + (f" (previous {assessment.baseline_version}{', legacy tag' if assessment.baseline_legacy else ''})" if assessment.baseline_version else " (initial)"),
        row("PCB revision", prev.pcb_revision if prev else "", proposed.pcb_revision),
        row("Schematic revision", prev.schematic_revision if prev else "", proposed.schematic_revision),
        row("Assembly drawing revision", prev.assembly_revision if prev else "", proposed.assembly_revision),
        row("Fabrication drawing revision", prev.fabrication_revision if prev else "", proposed.fabrication_revision),
        f"Change classes: {', '.join(assessment.declared) or '(none declared)'}",
        f"Justification: {assessment.justification or '(none)'}",
        "Detected changes: "
        + (", ".join(DOMAIN_LABELS[d] for d in assessment.detected) if assessment.detected else "none"),
        f"Fabrication baseline: {assessment.fabrication_baseline.id} ({assessment.fabrication_baseline.status})",
        f"Assembly baseline: {assessment.assembly_baseline.id} ({assessment.assembly_baseline.status})",
    ]
    if assessment.documentation_only and assessment.fabrication_baseline.status == "reused":
        lines.append("Documentation-only release: approved fabrication baseline is reused unchanged.")
    for review in assessment.reviews:
        lines.append(f"Review: {review}")
    for problem in assessment.problems:
        lines.append(f"Problem: {problem}")
    return lines


def root_relative(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()
