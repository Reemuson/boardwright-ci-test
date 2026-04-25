# Boardwright Roadmap

## Milestone 1: Local Project Control

Status: mostly complete.

Scope:

- `.boardwright/` config.
- CLI package and validation.
- Changelog recording.
- Legal/notice generation.
- Optional Textual TUI shell.
- Safe git status/commit helpers.

Success criteria:

- A project can be initialised.
- A user can record changes without editing changelog structure by hand.
- Boardwright can validate core files and config.

## Milestone 2: Preview Loop

Status: implemented, needs TUI polish and artifact fetch.

Scope:

- GitHub Actions preview workflow.
- Variant-aware preview dispatch.
- Disposable preview artifacts/branch.
- Local summary of expected generated outputs.
- TUI button for Generate Preview.
- TUI/CLI fetch of preview artifacts when GitHub CLI is available.

Success criteria:

- A user can generate preview outputs from `dev`.
- CI never mutates `dev`.
- Preview artifacts can be inspected locally.

## Milestone 3: Accepted Main Outputs

Status: underway.

Scope:

- `main-outputs.yaml` for accepted generated outputs.
- `boardwright promote` action.
- Generated `README.md` committed to `main` on explicit promotion.
- Accepted output paths committed only when requested.
- TUI flow for choosing `DRAFT`, `PRELIMINARY`, `CHECKED`, or `RELEASED`.

Success criteria:

- Boardwright can dispatch accepted-output generation on `main`.
- `main` README reflects the latest accepted outputs.
- Promotion is explicit, repeatable, and reviewable.

## Milestone 4: CI-Owned Release Tagging

Status: next active build.

Scope:

- `prepare-release.yaml` manual workflow.
- Boardwright dispatches release preparation instead of requiring local git tag commands.
- CI promotes changelog/revision-history variables.
- CI generates accepted outputs and README.
- CI commits accepted files to `main`.
- CI tags the exact accepted commit.
- Tag workflow publishes artifacts without mutating branches.
- Draft/prerelease/release state is recorded in committed metadata.

Success criteria:

- A user can create a draft, prerelease, or release from the TUI.
- The tag points at the generated `main` commit.
- GitHub Release assets include package zip, README, and board images.
- No tag workflow commits back to `main`.

## Milestone 5: Rich README And Dashboard

Status: planned.

Scope:

- README template refresh for Boardwright projects.
- CI status badges.
- Current revision and variant.
- Board dimensions.
- Stackup/fabrication summary.
- Component counts, including SMT/THT where available.
- Latest release links.
- TUI dashboard for workflow status and downloaded artifacts.

Success criteria:

- The generated README is useful as the front page of a hardware repo.
- The TUI shows enough status that the user rarely needs GitHub Actions pages.
