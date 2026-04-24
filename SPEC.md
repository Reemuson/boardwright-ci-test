# Boardwright Specification

Boardwright is a reusable KiCad/KiBot hardware project system for producing
clean, professional PCB/PCBA repositories and release packages.

The goal is to replace fragile manual release rituals with a guided workflow
for creating projects from a template, managing project metadata, recording
changelog entries, generating previews, promoting tested work, preparing tags
and release notes, packaging outputs, and handling legal/licence/notice text
consistently.

Boardwright should make the correct workflow easy and the dangerous workflow
hard.

## Core Principles

- Tags are immutable.
- CI should not surprise the user.
- Generated-output branches may be disposable.
- `main` should be meaningful and release-ready.
- Changelog release sections should exist before tags are pushed.
- Legal docs should be explicit but not overcomplicated.
- The TUI should guide, not hide.
- Dangerous operations require confirmation.
- YAML should be plumbing, not the user interface.
- Release-affecting commands should support dry runs.
- The workflow should be boring when it matters.

## Branch Model

```text
dev      = design/source development
preview  = generated preview outputs for inspection
main     = release-ready source and accepted outputs
tags     = immutable release packages
```

`dev` is for normal schematic, PCB, documentation, and template work. CI may
verify `dev` and generate preview outputs, but it must not commit generated
outputs back to `dev` or update release changelog sections.

`preview` is a disposable generated-output branch. It may be force-updated from
the latest successful `dev` build and should contain only the inspection
artefacts needed for review.

`main` is release-ready source. It may contain accepted generated outputs if the
project chooses that mode, but CI on `main` must not create release tags.

Tags point to exact immutable release source states. Tag workflows generate
release outputs and publish GitHub Releases only. They must not push commits
back to `main`, modify `CHANGELOG.md`, or merge detached commits into branches.

## CI/CD Architecture

Boardwright should split CI/CD into focused workflows:

```text
.github/workflows/dev-preview.yaml
.github/workflows/main-outputs.yaml
.github/workflows/release.yaml
```

`dev-preview.yaml` runs on `dev`, supports manual dispatch, allows variant
selection, runs verification, uploads artefacts, and may update `preview`.
For v1, GitHub Actions is the primary preview/build engine. Local KiBot runs are
optional future support because Docker and native KiBot are not assumed to be
installed on contributor machines.
The default dev-preview variant is `DRAFT` so a template or early schematic can
prove the pipeline before a valid PCB outline, DRC-clean board, or complete BoM
exists.

`main-outputs.yaml` is manually dispatched for accepted checked outputs on
`main`, and may commit generated outputs to `main` if the project has explicitly
enabled that mode.
By default, it uploads artefacts only. Committing generated outputs back to
`main` should require an explicit manual workflow input or project setting.

`release.yaml` runs on semantic version tags, checks out the tag, generates
`RELEASED` outputs, creates or updates the GitHub Release, and uploads release
assets without mutating branches.
Release packages contain generated artefacts by default. Boardwright does not
create custom source archives in v1; GitHub's automatic tag source archives are
sufficient unless a project later opts into curated source packages.

During migration from older templates, legacy workflows such as `ci.yaml` may
remain in the repository, but Boardwright-native commands should target the
split workflows above.

## Variants

Expected variants:

```text
DRAFT
PRELIMINARY
CHECKED
RELEASED
```

`DRAFT` should be fast during early schematic capture. `PRELIMINARY` should
generate fuller outputs without pretending release readiness. `CHECKED` should
run the normal verification set. `RELEASED` should be used only for tagged
release packages.

Variant defaults live in project config and can be overridden from the CLI, TUI,
or workflow dispatch without editing workflow YAML.

## Project Config

Boardwright uses `.boardwright/` as its project config directory.

```text
.boardwright/
  project.yaml
  branches.yaml
  legal.yaml
  revision_history.yaml
```

The config stores project identity, branch names, variant defaults, legal
profile, output naming preferences, and revision history settings.

## Revision History

KiCad templates should use fixed revision history slots, not version-specific
variables.

```text
${REVHIST_1_VERSION}
${REVHIST_1_DATE}
${REVHIST_1_TITLE}
${REVHIST_1_BODY}
```

Boardwright must always define every configured slot. Unused slots resolve to
empty strings so generated schematics never show placeholder variable names.
Release history is parsed from `CHANGELOG.md`, with the latest configured
number of releases filling the slots.

The default revision-history sheet may show four columns, but this is not a hard
limit. Projects can increase `.boardwright/revision_history.yaml` `slots` and
edit the KiCad sheet, or add extra sheets, to consume more `REVHIST_N_TITLE`
and `REVHIST_N_BODY` variables. The KiBot preflight template defines extra
blank-capable variables up to the configured preflight ceiling.

## Changelog Workflow

Boardwright manages `CHANGELOG.md` interactively and from scriptable CLI
commands. Supported unreleased sections are:

- Status
- Added
- Changed
- Fixed
- Removed
- Notes

Release preparation happens before tagging. Boardwright must validate that
`Unreleased` has content, prevent duplicate version headings, and detect
existing local/remote tags where possible.

## CLI And TUI

Every TUI workflow should call the same internal functions as the CLI. The CLI
must be scriptable, CI-friendly, and return useful exit codes. The TUI should
show current branch, dirty state, latest tag, unreleased changelog status,
configured variant, and CI status when available.

Candidate commands:

```text
boardwright init
boardwright status
boardwright change
boardwright preview
boardwright promote
boardwright release
boardwright legal
boardwright config
boardwright clean
```

The MVP interface is the CLI. The Textual TUI is an optional usability layer
that wraps the same internal functions. If Textual is not installed,
`boardwright tui` should keep the CLI usable and print an installation hint
rather than failing the project workflow.

## Legal And Notices

Boardwright should generate:

```text
LICENSE
NOTICE.md
THIRD_PARTY_NOTICES.md
```

The notice system must keep hardware licence scope, branding exclusions,
compatibility wording, non-affiliation wording, safety notes, and preserved
third-party notices clear and separate. It must avoid implying legal advice or
OEM endorsement.

## README

The README should be generated from `kibot_resources/templates/readme.txt`.
Boardwright should update variables through config, validate image/logo paths,
and prevent missing-logo mistakes where practical.

## Open Questions

- Should preview outputs be force-pushed to `preview`?
  - Decision: yes. Preview outputs are disposable and rebuildable.
- Should generated outputs be committed to `main`, or only release assets?
  - Working default: `main` remains source plus accepted outputs when configured. Tagged GitHub Releases publish generated release artefacts. Git tags still point to the exact source state.
- Should Boardwright use GitHub CLI if available?
  - Decision: optional only. Boardwright should work without it, but may use `gh` for workflow dispatch/status when available.
- Should local KiBot runs be supported on Windows directly, Docker only, or both?
  - Decision: not required for v1. GitHub Actions is primary; local KiBot can be added later.
- How many revision history slots should the default template provide?
  - Decision: default to four slots.
- Should unused revision history slots show empty cells or fully disappear?
  - Decision: blank values for now. Fully disappearing cells can wait for schematic layout rework.
- Should v1 support multiple board variants or assembly variants?
  - Decision: not in v1. Revisit after KiCad 10/KiBot variant support settles.
- Should generated release packages include source archives?
  - Working default: do not create custom source archives in v1. Rely on GitHub's automatic source archives unless a curated source package becomes necessary.
- Should Boardwright eventually support non-KiCad projects?
  - Decision: no. Boardwright is KiCad-only.
