# Boardwright Live Testbench

Use a separate repository for live CI testing. Do not run preview, accept, or
release experiments from the template repository unless you deliberately want
test branches, tags, artifacts, and releases there.

## Goal

The live testbench should prove the complete hosted path:

```text
template copy -> dev change -> preview CI -> review artifact
-> accept to main -> accepted-output evidence -> draft release
```

Start with `PRELIMINARY` draft releases. The template PCB is not a
fabrication-ready board, so `CHECKED`/`RELEASED` variants may correctly fail
ERC/DRC until a real project has a valid outline and checked electrical state.
Move to `CHECKED`, `RELEASED`, prerelease, and full release only after the draft
path is boring on a real board.

## Recommended Shape

- Source template repo: this repository.
- Local testbench repo: sibling directory such as `../Boardwright-testbench`.
- Remote testbench repo: empty GitHub repository such as
  `OWNER/boardwright-live-test`.
- Branches: `main`, `dev`, and disposable generated `preview`.
- Tags/releases: disposable semantic versions created only in the testbench.

## Plan

From the template repository:

```powershell
python -m boardwright testbench plan --github-repo OWNER/boardwright-live-test
```

This prints the exact command sequence for the configured project.

## Initialize

Create the local testbench copy:

```powershell
python -m boardwright testbench init `
  --target ..\Boardwright-testbench `
  --github-repo OWNER/boardwright-live-test
```

The init command:

- copies the template into the target directory
- excludes `.git`, Python caches, local Boardwright outputs, KiBot logs, and
  generated output folders
- updates `.boardwright/project.yaml` with the testbench GitHub repo when
  `--github-repo` is supplied
- initializes `main` and `dev` locally
- configures a testbench-local git author
- adds `origin` if a GitHub repo slug was supplied

It does not create the GitHub repository for you. Create an empty repo first,
or use GitHub CLI:

```powershell
gh repo create OWNER/boardwright-live-test --private --source ..\Boardwright-testbench --remote origin
```

If `origin` already exists from `testbench init`, omit `--remote origin` or
create the GitHub repository through the web UI.

## First Push

From the testbench repository:

```powershell
python -m boardwright doctor
git push -u origin main
git push -u origin dev
python -m boardwright doctor
```

`doctor` should show `origin/main` and `origin/dev` before live workflow
testing. GitHub auth warnings need to be fixed before dispatch commands will
work.

## Preview Test

From the testbench repository:

```powershell
git checkout dev
python -m boardwright change "Live test preview path" --section Changed --suggest-commit
git add -A
git commit -m "test: exercise boardwright preview path"
git push -u origin dev
python -m boardwright preview --variant PRELIMINARY --dispatch
```

Wait for the preview workflow to finish, then:

```powershell
python -m boardwright review --variant PRELIMINARY
python -m boardwright review --variant PRELIMINARY --fetch
```

The fetch writes `boardwright-preview/.boardwright-preview-reviewed.json` in the
testbench repo. That local marker gates `Accept to Main`.

If `boardwright-preview/` is created but empty, check the variant and artifact
name first:

```powershell
python -m boardwright review --variant PRELIMINARY
python -m boardwright review --variant PRELIMINARY --fetch
```

The preview artifact should contain `MANIFEST.txt`. If it does not, open the
preview workflow run and check the `Preview artifact contents` log line in the
`Collect preview outputs` step.

If GitHub CLI reports `no artifact matches`, compare the requested artifact
name with the variant shown in the preview branch `README.md`. For example,
`Variant: CHECKED` means the downloadable artifact is
`boardwright-preview-CHECKED`.

Boardwright defaults review to `variants.preview_default`, not the schematic
revision text variable. Use the variant selector or `--variant` when manually
dispatching a different CI variant.

Generated packages are cleaned after KiBot runs. Numbered PDF page shards such
as `boardwright-schematic-1.pdf` are removed when the combined
`boardwright-schematic.pdf` exists. Empty generated CSV tables for testpoints,
component counts, and impedance-style reports are removed instead of being
shipped as blank review files.

The fabrication PDF intentionally omits the impedance table unless Boardwright
has a controlled-impedance source to render. Drill, component-count, and
testpoint placeholders are filled by KiBot's `include_table` preflight for
PRELIMINARY/CHECKED/RELEASED runs. DRAFT runs skip that preflight.

## Accept To Main

After the preview artifact is fresh and reviewed:

```powershell
python -m boardwright promote --variant PRELIMINARY --dispatch
```

Wait for the main-output workflow to finish, then:

```powershell
git fetch origin
python -m boardwright accepted
python -m boardwright doctor
```

Accepted-main evidence must be fresh for `origin/main` before release testing.

## Draft Release Test

Use a throwaway semantic version that does not exist in the testbench repo:

```powershell
python -m boardwright release 0.1.0 --variant PRELIMINARY --kind draft --dispatch
```

Expected result:

- `prepare-release.yaml` runs on `main`
- changelog and release metadata are committed to `main`
- tag `0.1.0` is created
- `release.yaml` publishes a draft GitHub Release
- release assets include the generated package and board render images

## Cleanup

Prefer deleting the entire testbench GitHub repository when the run is done.
That keeps branches, tags, workflow runs, artifacts, and draft releases out of
the template repository.
