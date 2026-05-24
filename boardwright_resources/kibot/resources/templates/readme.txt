<p align="center" width="100%">
  <img alt="Logo" width="33%" src="assets/logos/rd-logo.png">
</p>

<h1 align="center">${BOARD_NAME}</h1>

<p align="center" width="100%">
  <a href="${GIT_URL}/actions/workflows/dev-preview.yaml">
    <img alt="Preview" src="${GIT_URL}/actions/workflows/dev-preview.yaml/badge.svg">
  </a>
  <a href="${GIT_URL}/actions/workflows/main-outputs.yaml">
    <img alt="Main Outputs" src="${GIT_URL}/actions/workflows/main-outputs.yaml/badge.svg">
  </a>
  <a href="${GIT_URL}/actions/workflows/release.yaml">
    <img alt="Release" src="${GIT_URL}/actions/workflows/release.yaml/badge.svg">
  </a>
</p>

> Hardware photographs are not available for this revision yet.

***

<p align="center">
  <img alt="3D Top Angled" src="${png_3d_viewer_angled_top_outpath}" width="45%">
&nbsp; &nbsp; &nbsp; &nbsp;
  <img alt="3D Bottom Angled" src="${png_3d_viewer_angled_bottom_outpath}" width="45%">
</p>

***

## SPECIFICATIONS

| Parameter | Value | 
| --- | --- |
| Revision | ${REVISION} |
| Variant | ${VARIANT} |
| Dimensions | ${bb_w_mm} x ${bb_h_mm} mm |

***

## DIRECTORY STRUCTURE

    .
    |- assets/renders     # Generated board renders
    |- assets/logos       # Project logos
    |- assets/3d          # Generated 3D model exports
    |- Manufacturing      # Assembly and fabrication documents
    |- Schematic          # PDF schematic outputs
    |- Testing            # Testpoint and validation outputs
    |- Reports            # ERC/DRC and summary reports
    `- Variants           # Assembly variant outputs

***

## LEGAL

This repository contains open hardware design files, protected project branding,
and third-party workflow content.

- The primary hardware licence is listed in `LICENSE`.
- Project-specific scope notes, branding exclusions, compatibility wording,
  non-affiliation wording, and safety notes are in `NOTICE.md`.
- Third-party copyright and licence notices are preserved in
  `THIRD_PARTY_NOTICES.md`.
