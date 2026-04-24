# Boardwright Roadmap

## Milestone 1: Minimum Useful Boardwright

Build a small local tool that understands the project and makes common metadata
work safer.

Scope:

- Project config in `.boardwright/`.
- `boardwright status`.
- `boardwright change`.
- Changelog parser/writer.
- Commit message suggestion.
- Legal template generator.
- README template validator.
- Basic Textual TUI shell.

Success criteria:

- A user can initialise project metadata.
- A user can add changelog entries through prompts or CLI flags.
- A user can get a suggested commit message.
- A user can validate key project files.

## Milestone 2: Preview Generation

Generate reviewable KiBot outputs without dirtying source branches.

Scope:

- KiBot runner.
- Variant selection.
- Preview branch support.
- GitHub Actions preview workflow template.
- Output path summary.
- Logo/path validation.

Success criteria:

- A push to `dev` generates preview outputs.
- Preview outputs are available on `preview` or as GitHub Actions artefacts.
- The source branch remains clean.

## Milestone 3: Release Preparation

Prepare release commits and tags without tag workflows mutating `main`.

Scope:

- `boardwright release`.
- Tag checks.
- Changelog release promotion.
- Revision history slot filler.
- Release workflow template.
- GitHub Release asset list configuration.
- Dry-run support.

Success criteria:

- `boardwright release 0.1.0 --pre-release` prepares a clean release commit.
- The tag workflow creates a release package without mutating `main`.
