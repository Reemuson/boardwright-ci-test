# SPDX-License-Identifier: LicenseRef-Boardwright-Source-Available
# Copyright (c) Ryan Hicks

"""Design fingerprints used for release change-impact assessment.

A fingerprint is a SHA-256 over a *normalised* view of the KiCad sources. Each
domain answers one engineering question:

``pcb_geometry``
    Is the physical board (copper, mask, paste, silk, edge, drills, stackup,
    board thickness) the same? Net *names*, UUIDs, timestamps, title blocks,
    documentation layers, 3D models and formatting are excluded. Nets are
    identified by the pads they connect, so a net rename alone does not change
    this hash. Text variables (``${...}``) in text on fabrication layers are
    resolved first, so a variable whose value changes is a silkscreen change.
``pcb_netnames``
    Do net names or pad pin functions differ? Both appear in IPC-D-356/ODB++
    and Gerber X2 attributes, so a rename changes fabrication *data* but not
    copper.
``fabrication_spec``
    Do the fabrication requirements in ``project.yaml`` differ?
``assembly``
    Do populated parts, values, footprints, placement, DNP/BOM flags, BOM
    fields or assembly notes differ?
``pcb_drawings``
    Does content on PCB documentation layers (``*.Fab``, ``Dwgs.User``,
    ``Cmts.User``, ``User.*``) used by the fabrication and assembly drawings
    differ? This is drawing content, not the bare board.
``schematic``
    Does the schematic content differ (title blocks excluded)?

``tooling`` hashes Boardwright's KiBot output configuration and notes
templates (line endings normalised). It is not a design domain: a change
requires the ``tooling`` class, and its effect on the rendered outputs is
checked by :mod:`boardwright.output_manifest`.

``fab_volatile_variables`` lists release-dependent variables (for example
``${RELEASE_VERSION}``) used in text on fabrication layers; such boards get
different silkscreen for every release.

Equal source fingerprints mean the design inputs are equivalent; they do not
prove byte-identical generated Gerbers. Generated-output comparison is handled
separately by :mod:`boardwright.output_manifest`.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .errors import BoardwrightError
from .sexpr import Node, SList, canonical, parse, unquote

FINGERPRINT_SCHEMA = 2

# Variables whose value differs per release/build by design.
RELEASE_VOLATILE_VARIABLES = {
    "RELEASE_VERSION",
    "RELEASE_DATE",
    "RELEASE_DATE_NUM",
    "GIT_HASH_PCB",
    "GIT_HASH_SCH",
    "CURRENT_DATE",
    "VARIANT",
}
TEXT_HEADS = {"gr_text", "fp_text", "gr_text_box", "fp_text_box", "property", "text"}
# Boardwright-managed output tooling. A change here is a `tooling` change; its
# effect on rendered outputs (fab/assembly notes, Gerber options) is verified
# by the output manifest, not by the design fingerprint.
TOOLING_PREFIXES = (
    "boardwright_resources/kibot/resources/templates/",
    "boardwright_resources/kibot/yaml/kibot_out_",
    "boardwright_resources/kibot/yaml/kibot_filt_",
    "boardwright_resources/kibot/yaml/kibot_globals",
)
# Documentation layers whose content appears on the fab/assembly drawings.
DRAWING_LAYER_NAMES = {"Dwgs.User", "Cmts.User"}
DRAWING_LAYER_SUFFIXES = (".Fab",)

# Layers that are not part of the bare-board fabrication deliverable.
NON_FAB_LAYER_SUFFIXES = (".Fab", ".CrtYd")
NON_FAB_LAYERS = {
    "Dwgs.User",
    "Cmts.User",
    "Eco1.User",
    "Eco2.User",
    "Margin",
}

PCB_DROP_HEADS = {
    "uuid",
    "tstamp",
    "path",
    "sheetname",
    "sheetfile",
    "model",
    "attr",
    "locked",
    "unlocked",
    "descr",
    "tags",
    "pintype",
    "pinfunction",
    "generator",
    "generator_version",
    "group",
    "embedded_fonts",
    "outputdirectory",
    # Editor-only state; aux_axis_origin is kept because plots may use it.
    "grid_origin",
}
# "general" is kept: it carries the board thickness.
PCB_TOP_DROP_HEADS = {"version", "title_block", "paper", "property", "net"}

SCH_DROP_HEADS = {"uuid", "generator", "generator_version", "title_block", "version"}

ASSEMBLY_EXCLUDED_MANUFACTURING_KEYS = {"assembly_notes", "testpoint_policy"}
FABRICATION_EXCLUDED_MANUFACTURING_KEYS = ASSEMBLY_EXCLUDED_MANUFACTURING_KEYS


class SourceReader(Protocol):
    label: str

    def list_files(self) -> list[str]: ...

    def list_tree(self, prefix: str) -> list[str]: ...

    def read_text(self, path: str) -> str | None: ...


@dataclass(frozen=True)
class WorkingTreeReader:
    root: Path
    label: str = "working tree"

    def list_files(self) -> list[str]:
        return sorted(path.name for path in self.root.iterdir() if path.is_file())

    def list_tree(self, prefix: str) -> list[str]:
        base = self.root / prefix
        if not base.is_dir():
            return []
        return sorted(path.relative_to(self.root).as_posix() for path in base.rglob("*") if path.is_file())

    def read_text(self, path: str) -> str | None:
        target = self.root / path
        if not target.is_file():
            return None
        return target.read_text(encoding="utf-8")


@dataclass(frozen=True)
class GitRefReader:
    root: Path
    ref: str
    label: str = ""

    def __post_init__(self) -> None:
        if not self.label:
            object.__setattr__(self, "label", self.ref)

    def list_files(self) -> list[str]:
        completed = _git(self.root, "ls-tree", "--name-only", self.ref)
        return sorted(line for line in completed.splitlines() if line)

    def list_tree(self, prefix: str) -> list[str]:
        completed = _git(self.root, "ls-tree", "-r", "--name-only", self.ref, "--", prefix.rstrip("/"))
        return sorted(line for line in completed.splitlines() if line)

    def read_text(self, path: str) -> str | None:
        try:
            return _git(self.root, "show", f"{self.ref}:{path}")
        except BoardwrightError:
            return None


@dataclass(frozen=True)
class DesignFingerprint:
    schema: int
    source: str
    pcb_geometry: str
    pcb_netnames: str
    fabrication_spec: str
    assembly: str
    schematic: str
    pcb_format: str = ""
    schematic_format: str = ""
    pcb_file: str = ""
    schematic_files: tuple[str, ...] = field(default_factory=tuple)
    pcb_drawings: str = ""
    fab_volatile_variables: tuple[str, ...] = field(default_factory=tuple)
    tooling: str = ""

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["schematic_files"] = list(self.schematic_files)
        data["fab_volatile_variables"] = list(self.fab_volatile_variables)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DesignFingerprint":
        if not isinstance(data, dict):
            raise BoardwrightError("Fingerprint record must be a mapping.")
        try:
            return cls(
                schema=int(data.get("schema", 0)),
                source=str(data.get("source", "")),
                pcb_geometry=str(data["pcb_geometry"]),
                pcb_netnames=str(data["pcb_netnames"]),
                fabrication_spec=str(data["fabrication_spec"]),
                assembly=str(data["assembly"]),
                schematic=str(data["schematic"]),
                pcb_format=str(data.get("pcb_format", "")),
                schematic_format=str(data.get("schematic_format", "")),
                pcb_file=str(data.get("pcb_file", "")),
                schematic_files=tuple(str(item) for item in data.get("schematic_files", []) or []),
                pcb_drawings=str(data.get("pcb_drawings", "")),
                fab_volatile_variables=tuple(
                    str(item) for item in data.get("fab_volatile_variables", []) or []
                ),
                tooling=str(data.get("tooling", "")),
            )
        except KeyError as exc:
            raise BoardwrightError(f"Fingerprint record is missing `{exc.args[0]}`.") from exc


def compute_fingerprint(reader: SourceReader) -> DesignFingerprint:
    files = reader.list_files()
    pcb_files = [name for name in files if name.endswith(".kicad_pcb")]
    if len(pcb_files) > 1:
        raise BoardwrightError(
            f"Expected at most one root .kicad_pcb in {reader.label}; found {len(pcb_files)}."
        )
    pcb_name = pcb_files[0] if pcb_files else ""
    pcb = _parse_named(reader.read_text(pcb_name) or "", pcb_name) if pcb_name else SList(["kicad_pcb"])

    schematic_names = sorted(name for name in files if name.endswith(".kicad_sch"))
    schematics = [(name, _parse_named(reader.read_text(name) or "", name)) for name in schematic_names]

    project_yaml = reader.read_text(".boardwright/project.yaml")
    manufacturing = _manufacturing_settings(project_yaml)
    resolver = _TextResolver(_board_variables(reader, pcb_name, pcb, project_yaml))

    net_labels = _net_labels(pcb)
    geometry = _normalise_pcb(pcb, net_labels, resolver)
    netnames = {
        "nets": sorted((label, name) for name, label in net_labels.items() if name),
        "pin_functions": _pad_pin_functions(pcb),
    }
    fab_spec = {
        key: value
        for key, value in sorted(manufacturing.items())
        if key not in FABRICATION_EXCLUDED_MANUFACTURING_KEYS
    }
    assembly = {
        "footprints": _pcb_assembly_rows(pcb),
        "symbols": sorted(row for _, tree in schematics for row in _schematic_bom_rows(tree)),
        "notes": {
            key: manufacturing.get(key, "") for key in sorted(ASSEMBLY_EXCLUDED_MANUFACTURING_KEYS)
        },
    }
    schematic_canonical = [
        (name, canonical(_strip(tree, SCH_DROP_HEADS))) for name, tree in schematics
    ]

    return DesignFingerprint(
        schema=FINGERPRINT_SCHEMA,
        source=reader.label,
        pcb_geometry=_hash(canonical(geometry)),
        pcb_netnames=_hash(json.dumps(netnames)),
        fabrication_spec=_hash(json.dumps(fab_spec, sort_keys=True, default=str)),
        assembly=_hash(json.dumps(assembly, sort_keys=True, default=str)),
        schematic=_hash(json.dumps(schematic_canonical)),
        pcb_format=_format_label(pcb),
        schematic_format=",".join(sorted({_format_label(tree) for _, tree in schematics})),
        pcb_file=pcb_name,
        schematic_files=tuple(schematic_names),
        pcb_drawings=_hash(json.dumps(_drawing_items(pcb, resolver))),
        fab_volatile_variables=tuple(sorted(resolver.volatile_used)),
        tooling=_tooling_hash(reader),
    )


def fingerprint_working_tree(root: Path) -> DesignFingerprint:
    return compute_fingerprint(WorkingTreeReader(root))


def fingerprint_git_ref(root: Path, ref: str) -> DesignFingerprint:
    return compute_fingerprint(GitRefReader(root, ref))


# --- PCB normalisation ---------------------------------------------------


def _net_labels(pcb: SList) -> dict[str, str]:
    """Map net name -> rename-invariant label based on connected pads."""
    numbers: dict[str, str] = {}
    for net in pcb.children("net"):
        if len(net.items) >= 3:
            numbers[net.items[1]] = net.value(2)

    members: dict[str, list[str]] = {}
    for footprint in pcb.children("footprint"):
        reference = _footprint_reference(footprint)
        for pad in footprint.children("pad"):
            name = _pad_net_name(pad, numbers)
            if name is None:
                continue
            members.setdefault(name, []).append(f"{reference}:{pad.value(1)}")

    labels: dict[str, str] = {}
    for name in set(numbers.values()) | set(members):
        pads = sorted(members.get(name, []))
        labels[name] = f"pads[{pads[0]}]" if pads else f"name[{name}]"
    labels[""] = "unconnected"
    return labels


def _pad_net_name(pad: SList, numbers: dict[str, str]) -> str | None:
    net = pad.child("net")
    if net is None or len(net.items) < 2:
        return None
    if len(net.items) >= 3:
        return net.value(2)
    first = net.items[1]
    if isinstance(first, str) and first.startswith('"'):
        return unquote(first)
    return numbers.get(str(first), "")


def _normalise_pcb(pcb: SList, net_labels: dict[str, str], resolver: "_TextResolver | None" = None) -> SList:
    numbers = {
        net.items[1]: net.value(2) for net in pcb.children("net") if len(net.items) >= 3
    }
    kept: list[Node] = []
    for item in pcb.items:
        if isinstance(item, SList) and item.head in PCB_TOP_DROP_HEADS:
            continue
        kept.append(item)
    return _normalise_pcb_node(SList(kept), net_labels, numbers, resolver or _TextResolver({}), {})


def _normalise_pcb_node(
    node: SList,
    net_labels: dict[str, str],
    numbers: dict[str, str],
    resolver: "_TextResolver",
    scope: dict[str, str],
) -> SList:
    out: list[Node] = []
    is_footprint = node.head == "footprint"
    if is_footprint:
        scope = _footprint_scope(node)
    if node.head in TEXT_HEADS and any(_is_fab_layer(layer) for layer in _layers_of(node)):
        node = SList([resolver.resolve_atom(item, scope) if isinstance(item, str) else item for item in node.items])
    for position, item in enumerate(node.items):
        if is_footprint and position == 1:
            # Library identifier: renaming a library entry is not physical.
            continue
        if isinstance(item, str):
            out.append(item)
            continue
        if item.head in PCB_DROP_HEADS:
            continue
        if item.head == "property" and is_footprint:
            if not _fab_visible_property(item):
                continue
        elif _only_non_fab_layers(item):
            continue
        if item.head == "net":
            out.append(SList(["net", _net_label_for(item, net_labels, numbers)]))
            continue
        if item.head == "net_name":
            out.append(SList(["net_name", net_labels.get(item.value(1), "unconnected")]))
            continue
        out.append(_normalise_pcb_node(item, net_labels, numbers, resolver, scope))
    return SList(out)


def _net_label_for(net: SList, net_labels: dict[str, str], numbers: dict[str, str]) -> str:
    if len(net.items) >= 3:
        name = net.value(2)
    elif len(net.items) == 2 and isinstance(net.items[1], str):
        token = net.items[1]
        name = unquote(token) if token.startswith('"') else numbers.get(token, "")
    else:
        name = ""
    return net_labels.get(name, f"name[{name}]")


def _layers_of(node: SList) -> list[str]:
    layers: list[str] = []
    for child in node.items:
        if isinstance(child, SList) and child.head in {"layer", "layers"}:
            layers.extend(unquote(item) for item in child.items[1:] if isinstance(item, str))
    return layers


def _is_fab_layer(layer: str) -> bool:
    if layer in NON_FAB_LAYERS or layer.startswith("User."):
        return False
    return not layer.endswith(NON_FAB_LAYER_SUFFIXES)


def _only_non_fab_layers(node: SList) -> bool:
    layers = _layers_of(node)
    return bool(layers) and not any(_is_fab_layer(layer) for layer in layers)


def _fab_visible_property(node: SList) -> bool:
    layers = _layers_of(node)
    if not layers or not any(_is_fab_layer(layer) for layer in layers):
        return False
    return not _is_hidden(node)


def _is_hidden(node: SList) -> bool:
    for item in node.items:
        if item == "hide":
            return True
        if isinstance(item, SList):
            if item.head == "hide" and (len(item.items) == 1 or item.value(1) == "yes"):
                return True
            if item.head == "effects" and _is_hidden(item):
                return True
    return False


def _footprint_reference(footprint: SList) -> str:
    for prop in footprint.children("property"):
        if prop.value(1) == "Reference":
            return prop.value(2)
    for text in footprint.children("fp_text"):
        if text.value(1) == "reference":
            return text.value(2)
    return "?"


def _pcb_assembly_rows(pcb: SList) -> list[list[str]]:
    rows: list[list[str]] = []
    for footprint in pcb.children("footprint"):
        properties = {
            prop.value(1): prop.value(2) for prop in footprint.children("property")
        }
        layer = footprint.child("layer")
        at = footprint.child("at")
        attr = footprint.child("attr")
        rows.append(
            [
                _footprint_reference(footprint),
                footprint.value(1),
                properties.get("Value", ""),
                layer.value(1) if layer else "",
                canonical(at) if at else "",
                canonical(attr) if attr else "",
                json.dumps(
                    {k: v for k, v in sorted(properties.items()) if k not in {"Reference", "Value", "Datasheet", "Description"}},
                    sort_keys=True,
                ),
            ]
        )
    return sorted(rows)


class _TextResolver:
    """Expand KiCad ``${VAR}`` references the way the board plot would."""

    def __init__(self, variables: dict[str, str]) -> None:
        import re

        self.variables = variables
        self.volatile_used: set[str] = set()
        self._pattern = re.compile(r"\$\{([^{}]+)\}")

    def resolve_atom(self, atom: str, scope: dict[str, str]) -> str:
        if "${" not in atom or not atom.startswith('"'):
            return atom
        text = self.resolve(unquote(atom), scope)
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'

    def resolve(self, text: str, scope: dict[str, str], depth: int = 0) -> str:
        if depth > 8 or "${" not in text:
            return text

        def replace(match: Any) -> str:
            name = match.group(1)
            if name in RELEASE_VOLATILE_VARIABLES:
                self.volatile_used.add(name)
                return f"<release-dependent:{name}>"
            if name in scope:
                return self.resolve(scope[name], scope, depth + 1)
            if name in self.variables:
                return self.resolve(self.variables[name], scope, depth + 1)
            return match.group(0)

        return self._pattern.sub(replace, text)


def _footprint_scope(footprint: SList) -> dict[str, str]:
    scope: dict[str, str] = {}
    for prop in footprint.children("property"):
        name, value = prop.value(1), prop.value(2)
        scope[name] = value
        scope[name.upper()] = value
    library_id = footprint.value(1)
    library, _, name = library_id.partition(":")
    scope.setdefault("FOOTPRINT_LIBRARY", library if name else "")
    scope.setdefault("FOOTPRINT_NAME", name or library)
    return scope


def _board_variables(reader: SourceReader, pcb_name: str, pcb: SList, project_yaml: str | None) -> dict[str, str]:
    variables: dict[str, str] = {}
    if pcb_name:
        project_file = pcb_name[: -len(".kicad_pcb")] + ".kicad_pro"
        try:
            project = json.loads(reader.read_text(project_file) or "{}")
        except json.JSONDecodeError:
            project = {}
        text_variables = project.get("text_variables") if isinstance(project, dict) else None
        if isinstance(text_variables, dict):
            variables.update({str(k): str(v) for k, v in text_variables.items()})
    for prop in pcb.children("property"):
        variables[prop.value(1)] = prop.value(2)
    # Boardwright re-syncs these from project.yaml before every build, so the
    # metadata file wins over a possibly stale .kicad_pro copy.
    data: Any = {}
    if project_yaml:
        try:
            import yaml

            data = yaml.safe_load(project_yaml) or {}
        except Exception:  # pragma: no cover - malformed YAML is reported elsewhere
            data = {}
    project_section = data.get("project", {}) if isinstance(data, dict) else {}
    documents = data.get("documents", {}) if isinstance(data, dict) else {}
    schematic = documents.get("schematic", {}) if isinstance(documents, dict) else {}
    if isinstance(project_section, dict) and project_section.get("board_revision"):
        variables["BOARD_REVISION"] = variables["PCB_REVISION"] = str(project_section["board_revision"])
    if isinstance(schematic, dict) and schematic.get("revision"):
        variables["SCHEMATIC_REVISION"] = variables["DRAWING_REVISION"] = str(schematic["revision"])
    title_block = pcb.child("title_block")
    if title_block is not None:
        builtins = {"rev": "REVISION", "title": "TITLE", "company": "COMPANY", "date": "ISSUE_DATE"}
        for item in title_block.items[1:]:
            if not isinstance(item, SList):
                continue
            if item.head in builtins:
                variables[builtins[item.head]] = item.value(1)
            elif item.head == "comment" and len(item.items) >= 3:
                variables[f"COMMENT{item.value(1)}"] = item.value(2)
    return variables


def _is_drawing_layer(layer: str) -> bool:
    return layer in DRAWING_LAYER_NAMES or layer.startswith("User.") or layer.endswith(DRAWING_LAYER_SUFFIXES)


def _drawing_items(pcb: SList, resolver: "_TextResolver") -> list[str]:
    """Canonical content on documentation layers used by the PCB drawings."""

    def only_drawing(node: SList) -> bool:
        layers = _layers_of(node)
        return bool(layers) and all(_is_drawing_layer(layer) for layer in layers)

    items: list[str] = []
    for item in pcb.items:
        if not isinstance(item, SList):
            continue
        if item.head == "footprint":
            reference = _footprint_reference(item)
            at = item.child("at")
            side = item.child("layer")
            for child in item.items:
                if isinstance(child, SList) and child.head != "pad" and only_drawing(child) and not _is_hidden(child):
                    items.append(
                        f"{reference}|{canonical(at) if at else ''}|{side.value(1) if side else ''}|"
                        + canonical(_strip(child, PCB_DROP_HEADS))
                    )
        elif only_drawing(item):
            items.append(canonical(_strip(item, PCB_DROP_HEADS)))
    return sorted(items)


def _tooling_hash(reader: SourceReader) -> str:
    entries: list[tuple[str, str]] = []
    list_tree = getattr(reader, "list_tree", None)
    if list_tree is None:
        return ""
    for path in sorted(set(list_tree("boardwright_resources/kibot"))):
        if not path.startswith(TOOLING_PREFIXES):
            continue
        text = reader.read_text(path) or ""
        # Line endings differ between Windows checkouts and Git objects.
        entries.append((path, text.replace("\r\n", "\n")))
    return _hash(json.dumps(entries))


def _pad_pin_functions(pcb: SList) -> list[str]:
    rows: list[str] = []
    for footprint in pcb.children("footprint"):
        reference = _footprint_reference(footprint)
        for pad in footprint.children("pad"):
            function = pad.child("pinfunction")
            if function is not None:
                rows.append(f"{reference}:{pad.value(1)}={function.value(1)}")
    return sorted(rows)


# --- Schematic -------------------------------------------------------------

BOM_IGNORED_PROPERTIES = {"Datasheet", "Description", "ki_description", "ki_keywords", "Sim.Device", "Sim.Pins", "Sim.Type", "Sim.Params"}


def _schematic_bom_rows(tree: SList) -> list[str]:
    rows: list[str] = []
    for symbol in tree.children("symbol"):
        lib_id = symbol.child("lib_id")
        if lib_id is None:
            continue
        properties = {
            prop.value(1): prop.value(2)
            for prop in symbol.children("property")
            if prop.value(1) not in BOM_IGNORED_PROPERTIES
        }
        flags = {
            head: symbol.child(head).value(1)
            for head in ("in_bom", "on_board", "dnp", "exclude_from_sim")
            if symbol.child(head) is not None
        }
        flags.pop("exclude_from_sim", None)
        references = sorted(_instance_references(symbol)) or [properties.get("Reference", "")]
        unit = symbol.child("unit")
        for reference in references:
            data = dict(properties)
            data["Reference"] = reference
            if properties.get("Reference", "").startswith("#"):
                break
            rows.append(
                json.dumps(
                    {"lib_id": lib_id.value(1), "unit": unit.value(1) if unit else "", "props": data, "flags": flags},
                    sort_keys=True,
                )
            )
    return rows


def _instance_references(symbol: SList) -> list[str]:
    references: list[str] = []
    instances = symbol.child("instances")
    if instances is None:
        return references
    for project in instances.children("project"):
        for path in project.children("path"):
            reference = path.child("reference")
            if reference is not None:
                references.append(reference.value(1))
    return references


# --- helpers ---------------------------------------------------------------


def _strip(node: SList, drop: set[str]) -> SList:
    out: list[Node] = []
    for item in node.items:
        if isinstance(item, SList):
            if item.head in drop:
                continue
            out.append(_strip(item, drop))
        else:
            out.append(item)
    return SList(out)


def _format_label(tree: SList) -> str:
    version = tree.child("version")
    generator = tree.child("generator_version")
    parts = [version.value(1) if version else ""]
    if generator is not None:
        parts.append(generator.value(1))
    return "/".join(part for part in parts if part)


def _parse_named(text: str, name: str) -> SList:
    try:
        return parse(text)
    except BoardwrightError as exc:
        raise BoardwrightError(f"Cannot parse {name}: {exc}") from exc


def _manufacturing_settings(project_yaml: str | None) -> dict[str, Any]:
    if not project_yaml:
        return {}
    try:
        import yaml
    except ImportError:  # pragma: no cover - PyYAML is a declared dependency
        from .config import _read_simple_yaml

        data = _read_simple_yaml(project_yaml)
    else:
        data = yaml.safe_load(project_yaml) or {}
    manufacturing = data.get("manufacturing", {}) if isinstance(data, dict) else {}
    return manufacturing if isinstance(manufacturing, dict) else {}


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _git(root: Path, *args: str) -> str:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BoardwrightError(f"git {' '.join(args)} failed: {exc}") from exc
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", "replace").strip()
        raise BoardwrightError(f"git {' '.join(args)} failed: {stderr}")
    return completed.stdout.decode("utf-8")


def changed_domains(old: DesignFingerprint, new: DesignFingerprint) -> tuple[str, ...]:
    domains = ("pcb_geometry", "pcb_netnames", "fabrication_spec", "assembly", "pcb_drawings", "schematic")
    return tuple(name for name in domains if getattr(old, name) != getattr(new, name))


def schematic_pcb_parity(reader: SourceReader) -> list[str]:
    """Report populated-part disagreements between schematic and PCB.

    Returns human-readable mismatches. An empty PCB (no footprints) is treated
    as "layout not started" and yields no mismatches.
    """
    files = reader.list_files()
    pcb_files = [name for name in files if name.endswith(".kicad_pcb")]
    if len(pcb_files) > 1:
        return [f"Expected at most one root .kicad_pcb; found {len(pcb_files)}."]
    if not pcb_files:
        return []
    pcb = _parse_named(reader.read_text(pcb_files[0]) or "", pcb_files[0])
    pcb_parts: dict[str, tuple[str, str]] = {}
    for footprint in pcb.children("footprint"):
        properties = {prop.value(1): prop.value(2) for prop in footprint.children("property")}
        pcb_parts[_footprint_reference(footprint)] = (properties.get("Value", ""), footprint.value(1))
    if not pcb_parts:
        return []
    sch_parts: dict[str, tuple[str, str]] = {}
    for name in sorted(n for n in files if n.endswith(".kicad_sch")):
        tree = _parse_named(reader.read_text(name) or "", name)
        for row in _schematic_bom_rows(tree):
            data = json.loads(row)
            if data["flags"].get("on_board") == "no":
                continue
            props = data["props"]
            sch_parts[props["Reference"]] = (props.get("Value", ""), props.get("Footprint", ""))
    problems: list[str] = []
    for ref in sorted(set(sch_parts) - set(pcb_parts)):
        problems.append(f"{ref} is in the schematic but not on the PCB.")
    for ref in sorted(set(pcb_parts) - set(sch_parts)):
        if ref.startswith(("#", "REF**")):
            continue
        problems.append(f"{ref} is on the PCB but not in the schematic.")
    for ref in sorted(set(pcb_parts) & set(sch_parts)):
        sch_value, sch_fp = sch_parts[ref]
        pcb_value, pcb_fp = pcb_parts[ref]
        if sch_value != pcb_value:
            problems.append(f"{ref} value differs: schematic {sch_value!r}, PCB {pcb_value!r}.")
        if sch_fp and pcb_fp and sch_fp != pcb_fp:
            problems.append(f"{ref} footprint differs: schematic {sch_fp!r}, PCB {pcb_fp!r}.")
    return problems


__all__ = [
    "DesignFingerprint",
    "GitRefReader",
    "WorkingTreeReader",
    "changed_domains",
    "compute_fingerprint",
    "fingerprint_git_ref",
    "fingerprint_working_tree",
    "schematic_pcb_parity",
]
