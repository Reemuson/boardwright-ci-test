<p align="center" width="100%">
  <img alt="Logo" width="33%" src="assets/logos/rd-logo.png">
</p>

<h1 align="center">${BOARD_NAME}</h1>

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
| Board revision | ${BOARD_REVISION} |
| Release version | ${RELEASE_VERSION} |
| Variant | ${VARIANT} |
| Dimensions | ${bb_w_mm} x ${bb_h_mm} mm |

***

## OUTPUTS

Generated manufacturing, schematic, test, and release packages are published
as CI artifacts and GitHub Release assets. The repository source tree remains
the KiCad project and Boardwright workflow configuration.

***

## LEGAL

This repository contains open hardware design files, protected project branding,
and third-party workflow content.

- The primary hardware licence is listed in `LICENSE`.
- Project-specific scope notes, branding exclusions, compatibility wording,
  non-affiliation wording, and safety notes are in `NOTICE.md`.
- Third-party copyright and licence notices are preserved in
  `THIRD_PARTY_NOTICES.md`.
