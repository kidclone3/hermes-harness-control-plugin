from __future__ import annotations

import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path


class WorkspacePolicy:
    """Restrict harness execution to configured Git worktrees."""

    def __init__(self, allowed_roots: Sequence[str | Path]) -> None:
        self._allowed_roots = tuple(Path(root).expanduser().resolve() for root in allowed_roots)

    @property
    def allowed_roots(self) -> tuple[Path, ...]:
        return self._allowed_roots

    def validate(self, candidate: str | Path) -> Path:
        path = Path(candidate).expanduser()
        if not path.is_absolute():
            raise ValueError("cwd must be an absolute path")
        path = path.resolve()
        if not path.is_dir():
            raise ValueError(f"cwd is not an existing directory: {path}")
        if not self._allowed_roots:
            raise ValueError("workspace access is disabled; configure allowed_roots")
        if not any(path.is_relative_to(root) for root in self._allowed_roots):
            raise ValueError(f"cwd is outside configured allowed_roots: {path}")

        git = shutil.which("git")
        if git is None:
            raise ValueError("cannot validate Git worktree: git is not installed")
        try:
            result = subprocess.run(  # noqa: S603 - resolved git binary, fixed argv.
                [git, "-C", str(path), "rev-parse", "--show-toplevel"],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError(f"cannot validate Git worktree: {type(exc).__name__}") from exc
        if result.returncode != 0:
            raise ValueError(f"cwd is not a Git worktree: {path}")
        top_level = Path(result.stdout.strip()).resolve()
        if top_level != path:
            raise ValueError(f"cwd must be the Git worktree root: {path}")
        return path
