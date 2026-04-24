<p align="center" width="100%">
  <img alt="Logo" width="33%" src="Logos/rd-logo.png">
</p>

<h1 align="center">BOARD NAME</h1>

<p align="center" width="100%">
  <a href="/actions/workflows/ci.yaml">
    <img alt="CI Badge" src="/actions/workflows/ci.yaml/badge.svg?branch=">
  </a>
</p>

<p align="center" width="100%">
    <img src="Images/dummy_image.png">
</p>

***

<p align="center">
  <img alt="3D Top Angled" src="Images/boardwright-angled_top.png" width="45%">
&nbsp; &nbsp; &nbsp; &nbsp;
  <img alt="3D Bottom Angled" src="Images/boardwright-angled_bottom.png" width="45%">
</p>

***

## SPECIFICATIONS

| Parameter | Value | 
| --- | --- |
| Dimensions | 59.0 × 32.0 mm |

***

## DIRECTORY STRUCTURE

    .
    ├─ Computations       # Misc calculations
    ├─ HTML               # HTML files for generated webpage
    ├─ Images             # Pictures and renders
    │
    ├─ kibot_resources    # External resources for KiBot
    │  ├─ colors          # Color theme for KiCad
    │  ├─ fonts           # Fonts used in the project
    │  ├─ scripts         # External scripts used with KiBot
    │  └─ templates       # Templates for KiBot generated reports
    │
    ├─ kibot_yaml         # KiBot YAML config files
    ├─ KiRI               # KiRI (PCB diff viewer) files
    │
    ├─ lib                # KiCad footprint and symbol libraries
    │  ├─ 3d_models       # Component 3D models
    │  ├─ lib_fp          # Footprint libraries
    │  └─ lib_sym         # Symbol libraries
    │
    ├─ Logos              # Logos
    │
    ├─ Manufacturing      # Assembly and fabrication documents
    │  ├─ Assembly        # Assembly documents (BoM, pos, notes)
    │  │
    │  └─ Fabrication     # Fabrication documents (ZIP, notes)
    │     ├─ Drill Tables # CSV drill tables
    │     └─ Gerbers      # Gerbers
    │
    ├─ Report             # Reports for ERC/DRC
    ├─ Schematic          # PDF of schematic
    ├─ Templates          # Title block templates
    ├─ Testing
    │  └─ Testpoints      # Testpoints tables      
    │
    └─ Variants           # Outputs for assembly variants
***

## LEGAL

This repository contains open hardware design files, protected project branding,
and third-party workflow content.

- The primary hardware licence is listed in `LICENSE`.
- Project-specific scope notes, branding exclusions, compatibility wording,
  non-affiliation wording, and safety notes are in `NOTICE.md`.
- Third-party copyright and licence notices are preserved in
  `THIRD_PARTY_NOTICES.md`.
