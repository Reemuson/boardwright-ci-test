# Boardwright Roadmap

This roadmap tracks the product sequence. `TODO.md` is the tactical work queue.
`SPEC.md` defines the desired behavior and current product contract.

## Current Position

Boardwright has a working local tooling foundation, a small TUI, and split
GitHub Actions workflows for preview, accepted outputs, release preparation,
and tag publishing. The next build should live-retest the CI path after the
asset move and finish the project-facing README story.

The current branch is expected to be `main` for accepted template work. Normal
Boardwright project usage still expects design changes on `dev`.

## Milestone 1: Local Project Control

Status: implemented.

Delivered:

- `.boardwright/` config
- CLI package and console entry point
- validation
- changelog recording
- revision-history variable generation
- legal/notice generation
- optional Textual TUI with console fallback
- safe git status, commit, and push helpers
- user/global and project-local install helper
- unit tests for core local behavior

Remaining polish:

- consider a developer/test extra for smoother local test setup

## Milestone 2: Preview Loop

Status: implemented, live CI retest still needed.

Delivered:

- `dev-preview.yaml`
- push-triggered preview from `dev`
- manual preview dispatch path
- variant-aware preview planning/dispatch
- disposable preview branch publishing
- preview artifact upload
- TUI/CLI preview status and artifact fetch helpers
- expected output path summary, including `assets/renders` and `assets/3d`
- preview run model with source branch, source SHA, run id, creation time,
  status, conclusion, and artifact name
- freshness comparison against latest pushed `origin/dev`
- local review marker for the exact downloaded run/SHA/artifact

Remaining:

- live CI retest of preview artifact contents after the asset move

## Milestone 3: Accepted Main Outputs

Status: implemented plumbing, live retest needed.

Delivered:

- `main-outputs.yaml`
- `boardwright promote`
- TUI `Accept to Main`
- variant selection
- optional commit of generated README/render snapshot assets
- policy that wholesale generated output folders are not committed to `main`
- freshness/review gate before TUI dispatch

Remaining:

- retest that `main-outputs.yaml` commits only `README.md` and
  `assets/renders/*.png`

## Milestone 4: CI-Owned Release Tagging

Status: implemented foundation, live retest needed.

Delivered:

- `prepare-release.yaml`
- `boardwright release --dispatch`
- release kind support: draft, prerelease, release
- release variant support
- changelog promotion in CI
- `.boardwright/release.env`
- accepted release-state commit to `main`
- CI-created tag
- tag workflow that publishes artifacts without branch mutation
- release notes with side-by-side board renders when available

Remaining:

- live retest draft release
- live retest full release
- confirm release assets include expected `assets/` outputs after asset move

## Milestone 5: Rich README And Usable Dashboard

Status: active next build.

Delivered:

- README template has Boardwright-specific structure
- workflow badges
- revision, variant, dimensions
- side-by-side board renders
- legal notes
- TUI status bar, workflow timeline, next-action panel, validation panel, and
  changed-file panel
- shared TUI workflow-state model for timeline, next action, and action locks
- dedicated Review Artifacts screen
- Create Release readiness checklist before dispatch
- `boardwright doctor` readiness checks for Git, remotes, workflow dispatch
  shape, GitHub CLI/auth hints, Textual, and validation
- `boardwright review` for scriptable preview artifact state/fetch
- `boardwright testbench plan/init` plus `docs/TESTBENCH.md` for isolated live
  CI testing in a separate repository

Remaining:

- add latest release/package links to generated README
- add stackup/fabrication summary if KiBot variables are available
- add component count summary if KiBot output data supports it
- tune TUI status after real workflow use
- add direct GitHub Actions/release URLs once repository metadata is configured

Success criteria:

- generated `README.md` is useful as the front page of a hardware repo
- TUI shows enough state that normal users rarely need to open GitHub Actions
- Accept to Main is based on fresh reviewed preview evidence

## Milestone 6: Project Onboarding And Metadata Editing

Status: planned.

Scope:

- first-run setup when `.boardwright/` is missing or incomplete
- adopt an existing KiCad project into Boardwright
- edit/view project metadata from the TUI
- set GitHub repository, branch names, variants, logo/assets paths, and legal
  metadata without hand-editing YAML
- detect missing GitHub CLI authentication and show exact fallback commands

Success criteria:

- a new board repo can be initialized and configured mostly through Boardwright
- users can keep working in KiCad and Boardwright without memorizing config
  file paths

## Later

- richer legal/licence profiles
- curated source package support if needed
- local KiBot/Docker runner after the CI-first flow is solid
- multi-board or assembly variants once KiCad/KiBot variant support is stable
- richer artifact browser if the small cockpit proves insufficient
