<p align="center" width="100%">
  <img alt="Logo" width="33%" src="assets/logos/logo-wordmark-colour.png">
</p>

<h1 align="center">${PCBA_NAME}</h1>

${README_BADGES}

<p align="center">
  <img alt="3D Top Angled" src="${png_3d_viewer_angled_top_outpath}" width="45%">
&nbsp; &nbsp; &nbsp; &nbsp;
  <img alt="3D Bottom Angled" src="${png_3d_viewer_angled_bottom_outpath}" width="45%">
</p>

---

## Project Details

| Item | Value |
| --- | --- |
| Project name | ${PROJECT_NUMBER} |
| PCBA name | ${PCBA_NAME} |
| PCB name | ${PCB_NAME} |
| Drawing number | ${DRAWING_NUMBER} |
| Document type | ${DOCUMENT_TYPE} |
| Drawing revision | ${DRAWING_REVISION} |
| Board revision | ${BOARD_REVISION} |
| Release version | ${RELEASE_VERSION} |
| Variant | ${VARIANT} |
| Dimensions | ${bb_w_mm} x ${bb_h_mm} mm |

---

## Project Outputs

GitHub Actions generates the manufacturing, schematic, test, and release packages.

The source branch contains the KiCad project and the Boardwright configuration.

Use these links to get outputs or inspect a workflow:

- [Latest release](${GIT_URL}/releases/latest)
- [Release package](${GIT_URL}/releases/latest/download/boardwright-${RELEASE_VERSION}-release.zip)
- [Preview workflow](${GIT_URL}/actions/workflows/dev-preview.yaml)
- [Accepted outputs workflow](${GIT_URL}/actions/workflows/main-outputs.yaml)
- [Prepare release workflow](${GIT_URL}/actions/workflows/prepare-release.yaml)
- [Publish release workflow](${GIT_URL}/actions/workflows/release.yaml)

## Fabrication Summary

${FABRICATION_SUMMARY}

## Component Summary

${COMPONENT_COUNT_SUMMARY}

---

## Legal Information

This repository can contain hardware design files, protected project branding, and third-party workflow content.

- Read `LICENSE` for the primary hardware licence.
- Read `NOTICE.md` for project scope, branding, compatibility, non-affiliation, and safety information.
- Read `THIRD_PARTY_NOTICES.md` for third-party copyright and licence notices.
