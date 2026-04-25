from __future__ import annotations

from pathlib import Path

from .errors import BoardwrightError


WORKFLOW_FILES = (
    "dev-preview.yaml",
    "main-outputs.yaml",
    "prepare-release.yaml",
    "release.yaml",
)


def install_workflows(root: Path, force: bool = False) -> list[Path]:
    source = _workflow_source_dir()
    target = root / ".github" / "workflows"
    target.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    for filename in WORKFLOW_FILES:
        source_path = source / filename
        target_path = target / filename
        if not source_path.exists():
            raise BoardwrightError(f"Missing Boardwright workflow template: {source_path}")
        if target_path.exists() and not force:
            continue
        target_path.write_text(
            source_path.read_text(encoding="utf-8"),
            encoding="utf-8",
            newline="\n",
        )
        written.append(target_path)
    return written


def _workflow_source_dir() -> Path:
    repo_root = Path(__file__).resolve().parents[2]
    source = repo_root / ".github" / "workflows"
    if source.is_dir():
        return source
    raise BoardwrightError(
        "Could not find Boardwright workflow templates. Run from the source tree for now."
    )
