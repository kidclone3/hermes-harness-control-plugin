from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from harness_control.workspace import WorkspacePolicy


def _init_repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path.resolve()


def test_workspace_policy_accepts_git_worktree_under_allowed_root(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    root.mkdir()
    repo = _init_repo(root / "repo")

    policy = WorkspacePolicy([root])

    assert policy.validate(repo) == repo


def test_workspace_policy_rejects_when_no_allowed_roots_are_configured(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")

    with pytest.raises(ValueError, match="allowed_roots"):
        WorkspacePolicy([]).validate(repo)


def test_workspace_policy_rejects_non_git_directory(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    candidate = root / "plain"
    candidate.mkdir(parents=True)

    with pytest.raises(ValueError, match="Git worktree"):
        WorkspacePolicy([root]).validate(candidate)


def test_workspace_policy_rejects_worktree_outside_allowed_roots(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = _init_repo(tmp_path / "outside")

    with pytest.raises(ValueError, match="outside configured allowed_roots"):
        WorkspacePolicy([allowed]).validate(outside)
