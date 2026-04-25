# Boardwright TODO

Active implementation tracker. Product rules live in `SPEC.md`; sequencing
lives in `ROADMAP.md`.

## Done

- [x] Split planning into `SPEC.md`, `ROADMAP.md`, and `TODO.md`.
- [x] Add `.boardwright/` project config.
- [x] Scaffold Python package and CLI.
- [x] Add `boardwright init`, `status`, `change`, `validate`, `legal`,
      `revision-history`, `preview`, and `release`.
- [x] Add changelog parser/writer and release promotion.
- [x] Add legal/notice generation.
- [x] Add README template validation.
- [x] Add optional Textual TUI with console fallback.
- [x] Add TUI changelog-entry form.
- [x] Add safe git status and dry-run commit helpers.
- [x] Add GitHub Actions preview workflow.
- [x] Add GitHub Actions main-output workflow.
- [x] Add tag publish workflow.
- [x] Add KiBot revision-history variables with newest release first.
- [x] Make schematic ToC recurse through nested KiCad sheets.
- [x] Populate `${REVISION}` from git tags during release builds.
- [x] Attach generated README and board images to GitHub Releases.

## Active: Boardwright-Orchestrated Release Flow

- [x] Add shared action layer used by CLI and TUI.
- [x] Add `boardwright promote` planner/dispatcher.
- [x] Add `prepare-release.yaml` workflow.
- [x] Commit `.boardwright/release.env` during release preparation.
- [x] Make tag workflow read release metadata for variant and release kind.
- [ ] Let Boardwright dispatch CI-owned tag creation.
- [ ] Add TUI controls for Promote To Main and Create Release.
- [ ] Add workflow status polling where GitHub CLI is available.
- [ ] Add preview artifact download/fetch helper.

## Active: Generated Main README

- [ ] Refresh `kibot_resources/templates/readme.txt` for Boardwright projects.
- [ ] Add CI status badges.
- [ ] Add current revision and variant.
- [ ] Add board dimensions.
- [ ] Add brief stackup/fabrication summary.
- [ ] Add component count summary.
- [ ] Add latest release/package links.
- [x] Keep board images side by side in README and release markdown.

## Verification Targets

- [x] Dummy repo can generate preview outputs.
- [x] Dummy repo can publish a tag release.
- [x] Revision history populates on generated schematic.
- [x] Revision variable populates on generated schematic.
- [x] Cover ToC includes nested sheets.
- [ ] Prepare-release workflow can create a draft tag from `main`.
- [ ] Prepare-release workflow can create a prerelease tag from `main`.
- [ ] Prepare-release workflow can create a full release tag from `main`.
- [ ] TUI can drive the full happy path without manual git commands.

## Later

- [ ] Add `boardwright adopt` for existing projects.
- [ ] Add richer legal/licence profiles.
- [ ] Add curated source package support if needed.
- [ ] Add local KiBot/Docker runner support after CI-first flow is solid.
- [ ] Revisit multi-board or assembly variants after KiCad/KiBot variant support
      settles.
