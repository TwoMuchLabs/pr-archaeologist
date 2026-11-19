"""Thin wrapper around the git binary. Everything else goes through here."""

from __future__ import annotations

import subprocess
from typing import Optional


class GitError(RuntimeError):
    pass


def git(repo: str, *args: str, check: bool = True, config: Optional[dict] = None) -> str:
    """Run git in `repo` and return stdout as text (undecodable bytes replaced)."""
    cmd = ["git", "-c", "core.quotepath=off"]
    for key, value in (config or {}).items():
        cmd += ["-c", f"{key}={value}"]
    cmd += list(args)
    try:
        proc = subprocess.run(cmd, cwd=repo, capture_output=True)
    except FileNotFoundError as exc:  # git not installed
        raise GitError("git executable not found on PATH") from exc
    if check and proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        raise GitError(f"git {' '.join(args[:3])} failed: {err}")
    return proc.stdout.decode("utf-8", "replace")


def git_ok(repo: str, *args: str) -> bool:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True).returncode == 0


def repo_root(path: str) -> str:
    return git(path, "rev-parse", "--show-toplevel").strip()


def head_sha(repo: str) -> Optional[str]:
    out = git(repo, "rev-parse", "--verify", "-q", "HEAD", check=False).strip()
    return out or None


def is_ancestor(repo: str, older: str, newer: str) -> bool:
    return git_ok(repo, "merge-base", "--is-ancestor", older, newer)
