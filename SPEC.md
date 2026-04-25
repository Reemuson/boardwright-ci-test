# Boardwright Specification

Boardwright is a guided KiCad/KiBot project system for producing clean PCB/PCBA
repositories, generated review outputs, and GitHub release packages without
making the user remember brittle git and CI rituals.

The intended user experience is boring: work in KiCad, record changes in
Boardwright, ask Boardwright for previews, promote accepted outputs, and publish
draft/prerelease/release tags from the TUI.

## Core Principles

- Tags are immutable.
- `dev` is for design work and is never mutated by CI.
- `preview` is disposable and may be force-updated.
- `main` is the accepted state and may contain generated README/output assets.
- Tag workflows publish only; they do not mutate `main`.
- Boardwright owns workflow orchestration through shared CLI/TUI actions.
- YAML and git commands are plumbing, not the user interface.
- Release-affecting operations require explicit user intent.
- The workflow should be boring when it matters.

## Branch And Release Model

```text
dev      = normal KiCad/source development
preview  = disposable generated preview outputs
main     = accepted source plus accepted generated README/output assets
tags     = immutable published packages from exact main commits
```

Normal work happens on `dev`. Users record changelog entries as they work.
Preview generation may run on GitHub Actions and publish artifacts or a
throwaway `preview` branch, but it must not commit back to `dev`.

`main` represents accepted project state. When the user promotes a variant,
Boardwright dispatches CI to generate outputs, update `README.md`, commit
accepted artifacts to `main`, and optionally create a tag from that exact
commit.

Tags are created by a deliberate Boardwright-controlled CI workflow, not by
normal CI and not by the tag-publish workflow. The tag workflow checks out the
tag and publishes release assets only.

## Product Workflow

The target TUI workflow is:

1. Initialise project metadata and workflows.
2. Record changes while doing schematic/layout work.
3. Generate previews from `dev`; fetch artifacts locally for inspection.
4. Promote a good build to `main` with a selected variant.
5. Optionally tag that accepted `main` commit as a draft, prerelease, or release.
6. Continue development on `dev`.

Variant intent:

| Stage | Variant | GitHub release state |
| --- | --- | --- |
| early schematic | `DRAFT` | draft or prerelease |
| schematic mostly complete | `PRELIMINARY` | prerelease |
| fabrication package ready | `CHECKED` | prerelease/release candidate |
| official production release | `RELEASED` | full release |

## CI/CD Architecture

Boardwright-native workflows:

```text
.github/workflows/dev-preview.yaml
.github/workflows/main-outputs.yaml
.github/workflows/prepare-release.yaml
.github/workflows/release.yaml
```

`dev-preview.yaml` generates reviewable outputs from `dev` or manual dispatch.
It may publish `preview` and upload artifacts. It does not mutate `dev`.

`main-outputs.yaml` generates accepted outputs on `main`. It can commit
generated assets to `main` when explicitly requested.

`prepare-release.yaml` is manually dispatched by Boardwright. It validates the
release, promotes `CHANGELOG.md`, writes revision-history variables, generates
accepted outputs/README, commits accepted files to `main`, records release
metadata, creates the tag, and pushes the tag.

`release.yaml` runs on semantic version tags. It reads committed release
metadata, generates the tag package, creates/updates the GitHub Release, and
uploads assets. It never pushes branch commits.

GitHub Actions is the v1 build engine. GitHub CLI integration is optional and
used by Boardwright when available for dispatch/status/download convenience.

## Variants

Supported variants:

```text
DRAFT
PRELIMINARY
CHECKED
RELEASED
```

Defaults live in `.boardwright/project.yaml` and can be overridden from CLI/TUI
without editing workflow YAML.

## Project Config

Boardwright uses `.boardwright/`:

```text
.boardwright/
  project.yaml
  branches.yaml
  legal.yaml
  revision_history.yaml
  revision_history_variables.env
  release.env
```

`release.env` is committed by release preparation when a tag should carry
variant/release-state metadata such as `RELEASE_VARIANT=CHECKED` and
`RELEASE_KIND=prerelease`.

## Revision History

KiCad sheets use fixed slots:

```text
${REVHIST_1_TITLE}
${REVHIST_1_BODY}
```

Boardwright always defines every configured slot. The newest release fills slot
1. This keeps the most relevant changes visible first when a project has many
revisions. Unused slots resolve to blank strings.

Projects can increase `.boardwright/revision_history.yaml` `slots` and edit or
add KiCad sheets to consume more variables. The KiBot preflight defines a
larger blank-capable ceiling.

## Changelog

Boardwright manages `CHANGELOG.md` through CLI/TUI actions. Supported sections:

- Status
- Added
- Changed
- Fixed
- Removed
- Notes

Release preparation promotes `Unreleased` before tagging, rejects duplicate
versions, and updates revision-history variables.

## README

`README.md` is generated from `kibot_resources/templates/readme.txt`.

Accepted `main` README content should include, where available:

- CI/build status badges.
- Current revision/tag.
- Current variant.
- Board render images.
- Board dimensions.
- Brief stackup/fabrication summary.
- Component counts, including SMT/THT where KiBot data supports it.
- Links to latest release assets and generated manufacturing outputs.

The tag workflow also attaches the generated README and board images to the
GitHub Release. Release body markdown should be generated from changelog and
release assets, with board renders shown side by side.

## CLI And TUI

The TUI is the intended everyday interface. The CLI remains scriptable and is
used by CI. Both call the same internal action layer.

Core actions:

```text
boardwright init
boardwright status
boardwright change
boardwright preview
boardwright promote
boardwright release
boardwright legal
boardwright validate
```

The TUI should surface:

- branch/dirty state
- latest tag/current revision
- unreleased changelog status
- configured variant
- preview/promote/release workflow status
- downloaded preview artifact location
- clear buttons for Record Change, Generate Preview, Promote To Main, and Release

If Textual is not installed, `boardwright tui` keeps the CLI usable and prints
an installation hint.

## Legal And Notices

Boardwright generates:

```text
LICENSE
NOTICE.md
THIRD_PARTY_NOTICES.md
```

The notice system keeps hardware licence scope, branding exclusions,
compatibility wording, non-affiliation wording, safety notes, and third-party
notices explicit without implying legal advice or OEM endorsement.
