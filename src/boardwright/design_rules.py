# SPDX-License-Identifier: LicenseRef-Boardwright-Source-Available
# Copyright (c) Ryan Hicks

"""Read the DRC and ERC results a build actually produced (KiCad text reports).

Build completion and design-rule results are separate facts: a build can
finish and still report DRC errors. Counts come only from the generated
reports (``Reports/*-drc.rpt``, ``Reports/*-erc.rpt``) and KiBot's own log
line; a missing or unparseable report is ``unknown``, never zero. Nothing
here relaxes the build's own checks or any release gate.

KiCad report layout (KiCad 9)::

    ** Found 28 DRC violations **
    [clearance]: ...
        ; error                       <- severity of the violation above
        ; warning (excluded)          <- excluded by the board's exclusions
    ** Found 0 unconnected pads **
    ** Found 0 Footprint errors **

    ** ERC messages: 4  Errors 0  Warnings 4
"""

from __future__ import annotations

import re
from pathlib import Path

_SEVERITY = re.compile(r"^\s*;\s*(error|warning|info|ignore)\b(\s*\(excluded\))?", re.IGNORECASE)
_FOUND = re.compile(r"^\*\*\s*Found\s+(\d+)\s+(.+?)\s*\*\*\s*$")
_ERC_SUMMARY = re.compile(r"ERC messages:\s*(\d+)\s+Errors\s+(\d+)\s+Warnings\s+(\d+)", re.IGNORECASE)
_KIBOT_DRC = re.compile(r"DRC errors:\s*(\d+)")
_KIBOT_ERC = re.compile(r"ERC errors:\s*(\d+)")


def unknown(reason: str, file: str = "") -> dict:
    return {"status": "unknown", "reason": reason, "file": file}


def _count_severities(lines: list[str]) -> dict:
    counts = {"errors": 0, "warnings": 0, "excluded": 0, "other": 0}
    for line in lines:
        match = _SEVERITY.match(line)
        if not match:
            continue
        if match.group(2):
            counts["excluded"] += 1
        elif match.group(1).lower() == "error":
            counts["errors"] += 1
        elif match.group(1).lower() == "warning":
            counts["warnings"] += 1
        else:
            counts["other"] += 1
    return counts


def parse_drc(text: str, file: str = "") -> dict:
    """Counts from one KiCad DRC report; ``unknown`` unless the totals are present."""

    lines = text.splitlines()
    sections: dict[str, tuple[int, int]] = {}  # name -> (count, line index)
    for index, line in enumerate(lines):
        match = _FOUND.match(line.strip())
        if match:
            sections[match.group(2).lower()] = (int(match.group(1)), index)
    if "drc violations" not in sections:
        return unknown("no 'Found N DRC violations' line", file)
    total, start = sections["drc violations"]
    following = [i for _, i in sections.values() if i > start]
    end = min(following) if following else len(lines)
    counts = _count_severities(lines[start + 1:end])
    result = {"status": "parsed", "file": file, "violations": total, **counts,
              "unconnected_pads": sections.get("unconnected pads", (None, 0))[0],
              "footprint_errors": sections.get("footprint errors", (None, 0))[0]}
    listed = counts["errors"] + counts["warnings"] + counts["excluded"] + counts["other"]
    if listed != total:
        result["note"] = f"{listed} severities listed for {total} violations; counts may be incomplete"
    return result


def parse_erc(text: str, file: str = "") -> dict:
    """Counts from one KiCad ERC report; ``unknown`` unless the summary line is present."""

    match = _ERC_SUMMARY.search(text)
    if not match:
        return unknown("no 'ERC messages' summary line", file)
    counts = _count_severities(text.splitlines())
    result = {"status": "parsed", "file": file, "messages": int(match.group(1)),
              "errors": int(match.group(2)), "warnings": int(match.group(3)), "excluded": counts["excluded"]}
    if counts["errors"] != result["errors"] or counts["warnings"] != result["warnings"]:
        result["note"] = (f"summary says {result['errors']} errors, {result['warnings']} warnings; "
                          f"listed {counts['errors']} and {counts['warnings']}")
    return result


def _kibot_count(root: Path, pattern: re.Pattern) -> int | None:
    found = None
    for log in sorted(root.glob("kibot_*.log")):
        for match in pattern.finditer(log.read_text(encoding="utf-8", errors="replace")):
            found = max(found or 0, int(match.group(1)))
    return found


def collect(root: Path) -> dict:
    """Design-rule results of a finished build in ``root`` (a stage)."""

    def reports(suffix: str, parser) -> list[dict]:
        files = sorted((root / "Reports").glob(f"*-{suffix}.rpt")) if (root / "Reports").is_dir() else []
        if not files:
            return [unknown(f"no {suffix.upper()} report was generated")]
        parsed = []
        for path in files:
            try:
                parsed.append(parser(path.read_text(encoding="utf-8", errors="replace"), path.relative_to(root).as_posix()))
            except OSError as exc:
                parsed.append(unknown(f"unreadable: {exc}", path.relative_to(root).as_posix()))
        return parsed

    return {
        "drc": reports("drc", parse_drc),
        "erc": reports("erc", parse_erc),
        "kibot_drc_errors": _kibot_count(root, _KIBOT_DRC),
        "kibot_erc_errors": _kibot_count(root, _KIBOT_ERC),
    }


def has_errors(results: dict) -> bool | None:
    """True if any report shows errors, None if any is unknown, else False."""

    items = list(results.get("drc", [])) + list(results.get("erc", []))
    if any(item.get("status") != "parsed" for item in items) or not items:
        return None
    errors = any(item.get("errors") for item in items) or any(item.get("unconnected_pads") for item in items) \
        or any(item.get("footprint_errors") for item in items) or bool(results.get("kibot_drc_errors"))
    return errors


def summary_lines(results: dict | None) -> list[str]:
    if not results:
        return ["Design rules: unknown (not collected)"]
    lines = []
    for item in results.get("drc", []):
        if item.get("status") != "parsed":
            lines.append(f"DRC: unknown ({item.get('reason')})")
            continue
        line = (f"DRC ({item['file']}): {item['violations']} violation(s): {item['errors']} error(s), "
                f"{item['warnings']} warning(s), {item['excluded']} excluded; "
                f"{_maybe(item['unconnected_pads'])} unconnected pad(s); {_maybe(item['footprint_errors'])} footprint error(s)")
        lines.append(line + (f" [{item['note']}]" if item.get("note") else ""))
    for item in results.get("erc", []):
        if item.get("status") != "parsed":
            lines.append(f"ERC: unknown ({item.get('reason')})")
            continue
        line = (f"ERC ({item['file']}): {item['messages']} message(s): {item['errors']} error(s), "
                f"{item['warnings']} warning(s), {item['excluded']} excluded")
        lines.append(line + (f" [{item['note']}]" if item.get("note") else ""))
    if results.get("kibot_drc_errors") is not None:
        lines.append(f"KiBot reported {results['kibot_drc_errors']} DRC error(s)")
    return lines


def _maybe(value) -> str:
    return "unknown" if value is None else str(value)


def headline(results: dict | None) -> str:
    """One line for notifications: errors, clean, or unknown."""

    state = has_errors(results or {})
    if state is None:
        return "design rules unknown (a report is missing or unreadable)"
    drc = [i for i in results["drc"]]
    erc = [i for i in results["erc"]]
    drc_text = ", ".join(f"DRC {i['errors']} error(s)/{i['violations']} violation(s)" for i in drc)
    erc_text = ", ".join(f"ERC {i['errors']} error(s)/{i['warnings']} warning(s)" for i in erc)
    return ("design-rule errors: " if state else "no design-rule errors: ") + f"{drc_text}; {erc_text}"
