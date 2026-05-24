# Boardwright Product Specification

Boardwright is a KiCad/KiBot hardware project template plus a small workflow
tool. Its job is to make the normal PCB loop predictable:

```text
edit in KiCad -> record changes -> commit + push -> review artifacts
-> accept to main -> create release
```

The user should not need to remember KiBot groups, GitHub Actions inputs, tag
rituals, or revision-history plumbing during normal design work.

## Current Codebase

The repository currently contains three coupled parts:

- KiCad template files at the repository root, with worksheets in `Templates/`.
- Boardwright Python tooling in `src/boardwright/`.
- KiBot/GitHub Actions build resources in `boardwright_resources/` and
  `.github/workflows/`.

The Python package provides:

- project config loading from `.boardwright/`
- validation of required config, KiCad, KiBot, README, licence, and asset files
- changelog parsing, writing, and release promotion
- revision-history variable generation for KiBot/KiCad text variables
- legal/notice file generation
- CLI commands for status, validation, change recording, preview planning,
  promotion planning, release preparation, and git commit dry-runs
- shared workflow action builders used by CLI and TUI
- optional Textual TUI with a console fallback
- GitHub CLI integration for workflow dispatch, CI polling, and preview artifact
  download when `gh` is available

The current tests are Python `unittest` tests under `tests/`. Run them with:

```powershell
python -m unittest discover -s tests -v
```

`python -m boardwright ...` and `python -m boardwright.cli ...` both work for
local module execution. The installed console script is `boardwright`.

## Core Rules

- `dev` is the normal KiCad/source development branch.
- CI must not mutate `dev`.
- `preview` is disposable and may be force-updated.
- `main` is the accepted state.
- `main` may contain source files plus accepted generated README/render snapshot
  assets, but not wholesale manufacturing output folders.
- Tags are immutable published package points.
- Tag workflows publish artifacts only; they do not commit back to branches.
- Release-affecting operations require explicit user intent.
- CLI and TUI should share action logic instead of duplicating workflow rules.

## Branch And Release Model

```text
dev      = normal design/source work
preview  = disposable generated preview branch/artifacts
main     = reviewed and accepted project state
tags     = immutable published release package points
```

Normal work happens on `dev`. A push to `dev` triggers preview CI. Preview CI
generates reviewable artifacts and can publish the disposable `preview` branch,
but must not commit to `dev`.

`main` represents a reviewed state. The `Accept to Main` action dispatches the
main-output workflow against `main`, with a selected variant. When requested,
that workflow commits only `README.md` and render snapshot assets under
`assets/renders/`.

Release preparation is CI-owned. Boardwright dispatches `prepare-release.yaml`;
that workflow promotes the changelog, writes release metadata, generates
accepted outputs, commits the accepted release state to `main`, creates the tag,
and dispatches the tag workflow. The tag workflow publishes the release package
without mutating `main`.

## Variants

Supported variants are:

```text
DRAFT
PRELIMINARY
CHECKED
RELEASED
```

Variant intent:

| Stage | Variant | Typical release state |
| --- | --- | --- |
| early schematic/design | `DRAFT` | draft or prerelease |
| schematic mostly complete | `PRELIMINARY` | prerelease |
| fabrication package ready | `CHECKED` | prerelease or release candidate |
| official production release | `RELEASED` | full release |

Defaults live in `.boardwright/project.yaml`:

- `variants.dev_default`
- `variants.preview_default`
- `variants.main_default`
- `variants.release_default`

## Project Config

Boardwright config lives in `.boardwright/`:

```text
.boardwright/
  project.yaml
  branches.yaml
  legal.yaml
  revision_history.yaml
  revision_history_variables.env
  release.env
```

`project.yaml` holds project identity, GitHub repository settings, variant
defaults, workflow filenames, output policy, and visible asset paths.

`branches.yaml` maps the development, preview, and release branches. The
current default is:

```text
development: dev
preview: preview
release: main
```

`release.env` is written and committed by release preparation so the tag
workflow can read:

```text
RELEASE_VERSION=0.1.0
RELEASE_VARIANT=CHECKED
RELEASE_KIND=prerelease
```

## Changelog And Revision History

`CHANGELOG.md` is the source of release notes and schematic revision-history
content.

Supported changelog sections are:

```text
Added
Changed
Fixed
Removed
Notes
Status
```

The TUI exposes the everyday sections:

```text
Added
Changed
Fixed
Removed
Notes
```

KiCad sheets consume fixed text-variable slots:

```text
${REVHIST_1_TITLE}
${REVHIST_1_BODY}
```

Boardwright writes every configured slot to
`.boardwright/revision_history_variables.env`. Newest visible release content
fills slot 1, and unused slots are written as blank values. The KiBot preflight
defines a larger ceiling than the default visible slot count so projects can
expand their revision-history sheets later.

## CI/CD Workflows

Boardwright-native workflows:

```text
.github/workflows/dev-preview.yaml
.github/workflows/main-outputs.yaml
.github/workflows/prepare-release.yaml
.github/workflows/release.yaml
```

`dev-preview.yaml`

- runs on pushes to `dev` and manual dispatch
- selects a KiBot generation mode from the variant
- generates preview outputs
- cleans generated output packages before upload
- uploads `boardwright-preview-<VARIANT>` artifacts
- uploads KiBot logs
- publishes the disposable `preview` branch from `dev`
- does not mutate `dev`

`main-outputs.yaml`

- runs on manual dispatch
- generates accepted outputs for `main`
- cleans generated output packages before upload
- uploads generated outputs as artifacts
- optionally commits only `README.md` and `assets/renders/*.png`

`prepare-release.yaml`

- runs on manual dispatch from `main`
- installs Boardwright
- promotes `CHANGELOG.md`
- writes `.boardwright/release.env`
- generates accepted outputs/README
- cleans generated output packages before commit/tag
- commits accepted release state to `main`
- creates and pushes the tag
- dispatches `release.yaml` for the tag

`release.yaml`

- runs on semantic-version tags or manual dispatch against a tag
- reads `.boardwright/release.env`
- generates release outputs
- cleans generated output packages before packaging
- creates release notes from changelog content and board renders
- packages release assets
- publishes the GitHub Release
- does not push branch commits

## CLI

Core commands:

```text
boardwright
boardwright init
boardwright status
boardwright change
boardwright suggest-commit
boardwright validate
boardwright revision-history
boardwright preview
boardwright promote
boardwright accepted
boardwright review
boardwright release
boardwright doctor
boardwright testbench
boardwright outputs clean
boardwright legal
boardwright git-status
boardwright commit
boardwright tui
```

Plain `boardwright` opens the TUI. If Textual is not installed, it prints a
console status view and an install hint.

The CLI remains scriptable and useful in CI. The TUI is the intended everyday
interface for designers.

Planned CLI additions:

- `boardwright config show`: read-only project configuration summary.
- `boardwright adopt`: later helper for converting existing KiCad projects.

Implemented accepted-output CLI support:

- `boardwright accepted`: shows latest accepted main-output workflow evidence,
  including run id, branch, source SHA, expected `origin/main` SHA, status, and
  freshness.

Implemented environment-readiness CLI support:

- `boardwright doctor`: checks local Git/repository state, configured branches
  and remotes, workflow dispatch shape, GitHub CLI/auth hints, Textual
  availability, and base project validation. It exits nonzero only for blocking
  errors; warnings are advisory readiness notes.

Implemented scriptable review/testbench support:

- `boardwright review`: shows preview artifact freshness, run evidence,
  expected `origin/dev` SHA, and local reviewed-marker state. With `--fetch`,
  it downloads the fresh preview artifact and marks that exact run/SHA/artifact
  as reviewed.
- `boardwright testbench plan`: prints a live-test command sequence for a
  separate repository.
- `boardwright testbench init`: copies the template into a separate local
  testbench repo, excludes generated/local artifacts, optionally sets
  `project.github_repo`, and initializes local `main`/`dev` branches.
- `boardwright outputs clean`: removes KiBot packaging noise after generation.
  It drops numbered PDF page shards when a combined PDF exists and removes empty
  generated CSV tables for component-count, testpoint, and impedance-style
  outputs. CI workflows run the same cleanup before upload, commit, tag, or
  release packaging.

## TUI

The TUI is a small workflow cockpit, not a full git client or KiBot editor.
It should answer:

1. What state is the project in?
2. What should I do next?
3. What artifacts or release outputs are ready to review?

Primary actions:

```text
Record Changes
Commit + Push
Review Artifacts
Accept to Main
Create Release
Refresh
```

Routine plumbing should be automatic, CLI-only, or advanced/fallback:

```text
Validate
Write Revision History
Generate Preview
Legal
Raw Git Status
Raw Workflow Dispatch
```

Current implemented TUI behavior:

- status bar shows project id, branch, dirty state, remote ahead/behind,
  variant, latest tag, CI summary, and validation summary
- workflow timeline shows edit, record, commit/push, preview, review, accept,
  and release steps
- Record Changes updates `CHANGELOG.md`, writes revision-history variables,
  validates, and suggests a commit message
- Commit + Push requires the configured `dev` branch, requires a changelog entry
  for dirty work, validates, writes revision-history variables, commits, and
  pushes `origin/dev`
- Review Artifacts polls recent workflow runs and downloads the latest preview
  artifact evidence when `gh` is available. It opens a dedicated review screen
  showing run id, branch, source SHA, expected SHA, artifact name, created time,
  status, freshness, selected variant, and reviewed marker. The default variant
  comes from `variants.preview_default`; the operator can override it for manual
  workflow runs. During fetch, the TUI shows artifact download progress. A
  successful fetch writes a local review marker under `boardwright-preview/` for
  the exact artifact, run id, and SHA.
- Accept to Main dispatches `main-outputs.yaml` only when the selected variant
  has a fresh preview artifact for the latest pushed `origin/dev` SHA and that
  exact artifact has been reviewed locally.
- Create Release opens a release readiness checklist before dispatching
  `prepare-release.yaml`. The checklist shows accepted-main evidence, release
  inputs, unreleased changelog readiness, local tag availability, and dispatch
  target. Dispatch is disabled while any checklist item is blocking.
- primary action buttons lock/unlock from the shared workflow-state model
- Refresh checks accepted-main evidence when GitHub CLI is available. Create
  Release is blocked unless accepted main outputs are fresh for the latest
  pushed `origin/main` commit.

The TUI renders from a shared workflow-state model rather than keeping its own
private timeline rules. That model provides:

- current stage
- next action
- human-readable reason
- ordered timeline steps
- primary action enablement and lock reasons

Initial workflow stages:

```text
validation_blocked
needs_changelog
ready_to_commit
needs_push
behind_remote
preview_missing
preview_running
preview_failed
preview_stale
preview_ready
preview_reviewed
accepted_missing
accepted_running
release_ready
editing
```

Important missing TUI behavior:

- first-run metadata editing/onboarding is not implemented

## README And Assets

The generated project README is produced from:

```text
boardwright_resources/kibot/resources/templates/readme.txt
```

The current template already includes workflow badges, project logo, board
renders, revision, variant, dimensions, directory structure, and legal notes.

The target README should also include, where KiBot data makes it practical:

- latest release/package links
- stackup/fabrication summary
- component counts, including SMT/THT if available
- clearer links to generated manufacturing outputs

Visible project media belongs under:

```text
assets/logos/
assets/renders/
assets/3d/
```

`assets/renders/` may be committed to `main` as the accepted README snapshot.
`assets/3d/` is packaged into release artifacts but is not normally committed
as source state.

## Validation Contract

Validation currently checks:

- required `.boardwright/` config files
- required root files: `CHANGELOG.md`, `LICENSE`, `README.md`
- variant values
- supported preview engine
- configured workflow files
- changelog structure and duplicate releases
- revision-history slot settings
- presence of KiCad project/schematic/PCB files
- warns when PCB files have no `Edge.Cuts` outline geometry, because
  `CHECKED`/`RELEASED` CI runs may fail DRC without a real board outline
- presence of the KiBot main config
- configured asset paths
- README template mentions legal files

Validation should remain fast and local. CI/runtime output freshness and GitHub
authentication checks belong in status/review actions rather than base project
validation.

## Known Product Gaps

These are the important gaps between the current code and the intended product:

1. TUI workflow state: the UI needs a shared state model for stage, next action,
   action enablement, and lock reasons.
2. Review Artifacts screen: preview evidence should be a dedicated screen, not
   only a notification/status panel.
3. CI retest: workflows need live retesting after moving visible generated media
   under `assets/renders/` and `assets/3d/`.
4. README richness: the generated README template is partly refreshed but still
   lacks stackup/component/latest-release sections.
5. Onboarding: new/adopted project setup still relies on hand-editing config.
6. GitHub fallback UX can still be refined with direct URLs after repository
   metadata is configured.

## Out Of Scope For The Current Build

Do not prioritize these until the normal workflow is reliable:

- full YAML editor
- full git client
- full GitHub Actions browser
- KiCad file browser
- local KiBot/Docker runner as the primary flow
- multi-board management
- complete metadata editor
