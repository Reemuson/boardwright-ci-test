# Changelog

## [Unreleased]

## [0.1.5] - 2026-11-18
### Changed

- Schematic revision note now reads "SCH REV" so it is not mistaken for the
  PCB revision. Documentation only: circuit, BOM and PCB unchanged.

## [0.1.4] - 2026-11-18
### Fixed

- Fabrication drawing revision A (first lettered issue) supersedes the drawing
  issued with 0.1.3. Its minimum-feature table now matches the design rules:
  trace to trace 0.200 mm (0.1.3 drawing: 0.600), min. PTH hole 0.300 mm
  (0.600), annular ring 0.150 mm (0.200). The 0.1.3 notes text file already
  stated the correct values.
- Gerber front silkscreen now includes the TP1 GND and TP2 +3V3 test-point
  labels. The 0.1.3 Gerbers omitted them; the 0.1.3 ODB++ data contained them.
- Title blocks show the project name and the Ryan Dynamics logo again.


## [0.1.3] - 2026-05-26

### Notes

- No changelog entries recorded for this release.

## [0.1.2] - 2026-05-25
### Changed

- test
- test
- docs
- ci/cd
- release ci fix
- readme fix, ci changes
- changelog error and ci fix

### Fixed

- test 2
- fix
- ci on release
- ci cache
- commit to main discard dirty files

## [0.1.0] - 2026-04-24

### Status

-   Checked pre-release.
-   Schematic capture completed from the working breadboard prototype.
-   Breadboard prototype tested with a Fluke 289 multimeter.
-   PCB layout completed and ready to fabricate for further hardware testing.

### Added

-   Initial KiCad schematic for the FIC-100A USB IR adapter.
-   Initial 2-layer PCB layout.
-   Initial generated manufacturing and documentation workflow.

### Notes

-   This release is intended for fabrication and test validation. The PCB has not yet been validated as an assembled production unit.

[Unreleased]: https://github.com/Reemuson/FIC-100A/compare/0.1.0...HEAD

[0.1.0]: https://github.com/Reemuson/FIC-100A/compare/2170641ce48829b7754714a138c928e670b6678f...0.1.0
