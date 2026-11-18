# SPDX-License-Identifier: LicenseRef-Boardwright-Source-Available
# Copyright (c) Ryan Hicks

"""Read-only resolution of KiBot output templates.

Boardwright's KiBot outputs are templates: ``kibot_out_*.yaml`` uses
``@NAME@`` placeholders that are filled from the import's ``definitions`` in
``kibot_main.yaml``, then kibot_main's global ``definitions``, then the output
file's own defaults. This module resolves them the same way, without YAML
evaluation, so release planning and validation see the effective options.
"""

from __future__ import annotations

import json
import re

KIBOT_YAML_DIR = "boardwright_resources/kibot/yaml"
KIBOT_MAIN = f"{KIBOT_YAML_DIR}/kibot_main.yaml"
# Outputs whose options decide the content of the fabrication data.
FABRICATION_OUTPUT_FILES = (
    "kibot_out_gerber.yaml",
    "kibot_out_excellon_drill.yaml",
    "kibot_out_odb.yaml",
)
_PLACEHOLDER = re.compile(r"@(\w+)@")


class KiBotConfigError(ValueError):
    pass


def lf(text: str) -> str:
    return "\n".join(text.splitlines()) + "\n"


def scalar(text: str, key: str) -> str | None:
    match = re.search(rf"^\s*{re.escape(key)}:\s*([^#\n]*?)\s*(?:#.*)?$", text, re.M)
    return match.group(1).strip().strip("'\"") if match else None


def import_definitions(main_text: str, file_name: str) -> str:
    match = re.search(rf"^\s*- file:\s*{re.escape(file_name)}\s*\n((?:\s{{4,}}.*\n)+)", main_text, re.M)
    return match.group(1) if match else ""


def _split(text: str) -> tuple[str, str]:
    body, _, defaults = text.partition("\ndefinitions:")
    return body, defaults


class Resolver:
    """Resolves ``@NAME@`` for one imported output file."""

    def __init__(self, output_text: str, main_text: str, file_name: str) -> None:
        self.file_name = file_name
        main_text = lf(main_text) if main_text else ""
        self.body, own_defaults = _split(lf(output_text))
        self.sources = (
            import_definitions(main_text, file_name),
            _split(main_text)[1] if main_text else "",
            own_defaults,
        )

    def resolve(self, value: str, start: int = 0) -> str:
        # Each level may refer to the next (the import maps PLOT_REFS:
        # @PLOT_REFS@ onto kibot_main's global definition).
        match = _PLACEHOLDER.fullmatch(value)
        if not match:
            return value
        name = match.group(1)
        for level in range(start, len(self.sources)):
            found = scalar(self.sources[level], name)
            if found is not None:
                return self.resolve(found, level + 1)
        raise KiBotConfigError(f"{self.file_name}: cannot resolve @{name}@")

    def option(self, key: str) -> str | None:
        raw = scalar(self.body, key)
        return None if raw is None else self.resolve(raw)

    def effective(self) -> str:
        """The output body with comments removed and placeholders resolved."""
        lines = []
        for line in self.body.splitlines():
            stripped = line.split(" #", 1)[0].rstrip() if not line.lstrip().startswith("#") else ""
            if not stripped.strip():
                continue

            def substitute(match: re.Match) -> str:
                if match.group(1) in ("NAME", "COMMENT"):
                    return match.group(0)
                try:
                    return self.resolve(match.group(0))
                except KiBotConfigError:
                    return match.group(0) + "(unresolved)"

            lines.append(_PLACEHOLDER.sub(substitute, stripped))
        return "\n".join(lines)


def fabrication_output_config(read_text) -> str:
    """JSON of the effective fabrication output configuration, or "" if absent.

    ``read_text(path)`` returns a file's text or None. Comments and the
    output name/comment do not count; any option, layer list or resolved
    definition does.
    """
    main = read_text(KIBOT_MAIN) or ""
    entries = []
    for name in FABRICATION_OUTPUT_FILES:
        text = read_text(f"{KIBOT_YAML_DIR}/{name}")
        if text is None:
            continue
        entries.append((name, Resolver(text, main, name).effective()))
    return json.dumps(entries) if entries else ""
