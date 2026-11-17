# SPDX-License-Identifier: LicenseRef-Boardwright-Source-Available
# Copyright (c) Ryan Hicks

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from .config import BoardwrightConfig


def generate_legal_files(config: BoardwrightConfig, force: bool = False) -> list[Path]:
    written: list[Path] = []
    notice = config.root / "NOTICE.md"
    third_party = config.root / "THIRD_PARTY_NOTICES.md"

    if force or not notice.exists():
        notice.write_text(render_notice(config), encoding="utf-8", newline="\n")
        written.append(notice)

    if force or not third_party.exists():
        third_party.write_text(render_third_party_notices(config), encoding="utf-8", newline="\n")
        written.append(third_party)

    return written


def render_notice(config: BoardwrightConfig) -> str:
    project = config.project.get("project", {})
    legal = _downstream_legal(config)
    compatibility = legal.get("compatibility", {})
    project_name = project.get("pcba_name", config.pcba_name)
    license_name = legal.get("hardware_design_license", legal.get("hardware_license", "See LICENSE"))
    safety_notice = legal.get("safety_notice", "").strip()
    third_party_notice_file = legal.get("third_party_notice_file", "THIRD_PARTY_NOTICES.md")
    owner = legal.get("copyright_holder", "").strip()
    profile = str(legal.get("profile", "")).strip()

    parts = [
        f"# Notice\n\nThis notice applies to **{project_name}**.",
        dedent(
            f"""
            ## License Scope

            The default downstream hardware-project license is `{license_name}`.
            This project may override that default in its own project metadata.

            This notice describes downstream project defaults. It does not
            define the license of the Boardwright tooling/template repository.
            """
        ).strip(),
    ]

    if profile:
        parts.insert(1, f"## Legal Profile\n\n{profile}")

    if owner:
        parts.append(f"## Copyright Holder\n\n{owner}")

    if legal.get("branding_reserved", False):
        parts.append(
            dedent(
                """
                ## Branding

                Project branding, logos, trade dress, trademarks, and product
                photography are not licensed for downstream use unless
                expressly stated.
                """
            ).strip()
        )

    if compatibility.get("enabled", False):
        wording = compatibility.get("wording", "compatible with selected instruments")
        owner = compatibility.get("trademark_owner", "the original manufacturer")
        parts.append(
            dedent(
                f"""
                ## Third-Party Compatibility

                This project is an independent, third-party design {wording}.

                It is not made by, endorsed by, sponsored by, or affiliated with
                {owner}. Product names and trademarks belong to their respective
                owners.
                """
            ).strip()
        )

    if safety_notice:
        parts.append(f"## Safety\n\n{safety_notice}")

    parts.append(
        dedent(
            f"""
            ## Third-Party Notices

            Preserved third-party copyright and license notices are listed in
            `{third_party_notice_file}` where applicable.

            This file is project documentation, not legal advice.
            """
        ).strip()
    )

    return "\n\n".join(parts).rstrip() + "\n"


def render_third_party_notices(config: BoardwrightConfig) -> str:
    boardwright = config.legal.get("boardwright", {})
    inherited = boardwright.get("inherited_notice_file", "LICENSES/Nguyen-MIT.txt")
    return dedent(
        f"""
        # Third-Party Notices

        This file records third-party notices for template, workflow, script,
        font, and generated-output support material used by {config.pcba_name}.

        ## Boardwright Template Material

        This project may include material generated from or copied from the
        Boardwright template. Boardwright itself contains material derived from
        Nguyen Vincent's KiCad/KiBot template work. The inherited MIT notice is
        preserved in `{inherited}`.

            ## Project Review Items

        - Identify project-specific third-party symbols, footprints, models,
          datasheets, images, scripts, and generated-output support material.
        - Preserve upstream copyright and license notices.
        - Document which generated files contain copied template material.

        No endorsement by third parties is implied.
        """
    ).lstrip()


def _downstream_legal(config: BoardwrightConfig) -> dict:
    """Return downstream hardware-project legal metadata.

    Older projects used a top-level `legal:` mapping. New projects use
    `downstream_project_defaults:` so repository/tool licensing is not confused
    with generated hardware-project licensing.
    """

    downstream = config.legal.get("downstream_project_defaults", {})
    if isinstance(downstream, dict) and downstream:
        return downstream
    legacy = config.legal.get("legal", {})
    return legacy if isinstance(legacy, dict) else {}
