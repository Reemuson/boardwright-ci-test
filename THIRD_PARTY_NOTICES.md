<!-- SPDX-License-Identifier: LicenseRef-Boardwright-Source-Available -->
<!-- Copyright (c) Ryan Hicks -->

# Third-Party Notices

This file records known third-party or inherited material used by the
Boardwright KiCad/KiBot template repository.

See `docs/PROVENANCE.md` for the working inventory and open questions.

## Nguyen Vincent KiCad/KiBot Template

Boardwright is derived from Nguyen Vincent's KiCad/KiBot template work,
originally published as `KDT_Hierarchical_KiBot`.

The upstream template is licensed under the MIT License. The preserved license
text is in `LICENSES/Nguyen-MIT.txt`.

Substantial inherited material includes, unless later replaced:

- KiCad worksheet/title-block templates in `Templates/`.
- KiCad template project structure and example schematic/PCB conventions.
- KiBot configuration patterns in `boardwright_resources/kibot/yaml/`.
- KiBot report templates, support scripts, and color themes in
  `boardwright_resources/kibot/resources/`.
- GitHub Actions workflow structure for KiBot-generated outputs.
- Portions of the former root README, conversion guide, and screenshots.

Boardwright-specific Python tooling, workflow changes, project metadata,
TUI/CLI tooling, generated-output cleanup helpers, tests, and documentation
edits are covered by the root `LICENSE` unless a file states otherwise.

No endorsement by Nguyen Vincent or any other third party is implied.

## Bundled Fonts

The repository bundles Arimo and Tinos font files under:

```text
boardwright_resources/kibot/resources/fonts/
```

Arimo and Tinos are font families by Steve Matteson distributed under the SIL
Open Font License. The OFL text is included in `LICENSES/OFL-1.1.txt`. They
replace the previous Arial/Times New Roman rendering dependency so CI can render
worksheet/table typography without bundling proprietary system fonts.

## KiCad And KiBot

Boardwright depends on KiCad project/file formats and KiBot automation. KiCad
and KiBot are not vendored into this repository. Their names and logos belong to
their respective projects.

## Template Assets

The current template includes Boardwright branding assets such as:

```text
assets/logos/logo-block-colour.png
assets/logos/logo-wordmark-colour.png
```

Downstream projects can replace these through Boardwright project metadata.
