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
    legal = config.legal.get("legal", {})
    compatibility = legal.get("compatibility", {})
    project_name = project.get("name", config.project_name)
    license_name = legal.get("hardware_license", "See LICENSE")
    safety_notice = legal.get("safety_notice", "").strip()

    parts = [
        f"# Notice\n\nThis notice applies to **{project_name}**.",
        dedent(
            f"""
            ## Licence Scope

            The primary hardware licence is `{license_name}`. See `LICENSE` for
            the licence text and repository-specific scope.
            """
        ).strip(),
    ]

    if legal.get("branding_reserved", False):
        parts.append(
            dedent(
                """
                ## Branding

                Project branding, logos, trade dress, trademarks, and product
                photography are not licensed under the open hardware or software
                licences unless expressly stated. All rights are reserved in
                those assets.
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
            """
            ## Third-Party Notices

            Preserved third-party copyright and licence notices are listed in
            `THIRD_PARTY_NOTICES.md` where applicable.

            This file is project documentation, not legal advice.
            """
        ).strip()
    )

    return "\n\n".join(parts).rstrip() + "\n"


def render_third_party_notices(config: BoardwrightConfig) -> str:
    return dedent(
        f"""
        # Third-Party Notices

        This file records third-party notices for template, workflow, script,
        font, and generated-output support material used by {config.project_name}.

        ## Pending Review

        - Identify inherited KiCad/KiBot template and workflow material.
        - Preserve upstream copyright and licence notices.
        - Document which paths are derived from third-party template content.

        No endorsement by third parties is implied.
        """
    ).lstrip()
