# Boardwright TODO

Active implementation tracker for Boardwright.

For the durable product rules and workflow design, see `SPEC.md`.
For milestone sequencing, see `ROADMAP.md`.

## Milestone 1: Minimum Useful Boardwright

Goal: create a small local tool that can understand a project, report its state,
record changelog entries, and validate core project files.

- [x] Split long-form planning into `SPEC.md`, `ROADMAP.md`, and this active TODO.
- [x] Choose `.boardwright/` as the project config directory.
- [x] Add initial `.boardwright/project.yaml`.
- [x] Add initial `.boardwright/branches.yaml`.
- [x] Add initial `.boardwright/legal.yaml`.
- [x] Add initial `.boardwright/revision_history.yaml`.
- [x] Scaffold Python package.
- [x] Add initial `boardwright status`.
- [x] Add changelog parser.
- [x] Add changelog writer for new unreleased entries.
- [x] Add initial `boardwright change`.
- [x] Add simple commit message suggestion.
- [x] Add `boardwright init` for default project files.
- [x] Install Boardwright GitHub workflows during `boardwright init`.
- [x] Add legal template generator.
- [x] Add README template validator.
- [x] Add `boardwright validate`.
- [x] Add basic console TUI shell.
- [x] Replace console TUI shell with optional Textual app.
- [x] Keep console fallback when Textual is not installed.
- [x] Add TUI changelog-entry form.
- [x] Add safe git status and commit dry-run helpers.

Milestone 1 is successful when:

- [x] A user can initialise project metadata.
- [x] A user can add changelog entries from the CLI.
- [x] A user can get a suggested commit message.
- [x] A user can validate key project files.

## Milestone 2: Preview Generation

Goal: generate reviewable KiBot outputs without dirtying source branches.

- [x] Add CI KiBot runner through GitHub Actions.
- [x] Add variant selection.
- [x] Add CI-first `boardwright preview` planner.
- [x] Add preview branch support.
- [x] Add GitHub Actions preview workflow template.
- [x] Add output path summary.
- [x] Add logo/path validation.
- [x] Add optional GitHub Actions dispatch through `gh`.
- [x] Add split `main-outputs.yaml` workflow for accepted outputs.

Milestone 2 is successful when:

- [ ] A push to `dev` generates preview outputs.
- [ ] Preview outputs are available on the `preview` branch or as artefacts.
- [ ] Source branch remains clean.
- [x] Dummy repo onboarding flow validates locally.

## Milestone 3: Release Preparation

Goal: prepare release commits and tags without tag workflows mutating `main`.

- [x] Add release command.
- [x] Add tag checks.
- [x] Add changelog release promotion.
- [x] Add revision history slot filler.
- [x] Wire KiBot fixed `REVHIST_N_TITLE` / `REVHIST_N_BODY` variables.
- [x] Support configurable revision history slot count up to preflight ceiling.
- [x] Preserve previous releases by shifting them down slots.
- [x] Add release workflow template.
- [x] Add initial GitHub release package asset set.
- [x] Add dry-run default for release-affecting commands.

Milestone 3 is successful when:

- [x] `boardwright release 0.1.0 --prepare` prepares release files.
- [ ] The tag workflow creates a release package without mutating `main`.

## Later

- [ ] Add `boardwright adopt` for existing projects.
- [ ] Add richer legal/licence profiles.
- [ ] Add generated output package manifests.
- [ ] Add optional GitHub CLI integration.
- [x] Decide whether local KiBot runs support Windows directly, Docker only, or both.
- [x] Decide whether v1 supports multiple board/assembly variants.
