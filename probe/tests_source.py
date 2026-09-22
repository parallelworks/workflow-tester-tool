"""Fetch the test definitions directory from its git repository."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from .pw import Checkout, FetchError


def fetch_tests(repo: str, branch: str, directory: str, out: Path) -> int:
    """Replace `out` with `directory` of `repo` at `branch`, atomically (a new tree
    is prepared next to it and swapped in). Returns the number of .json files."""
    out = out.resolve()
    directory = directory.strip("/") or "tests"
    clone = out.parent / (out.name + ".clone")
    staging = out.parent / (out.name + ".new")
    previous = out.parent / (out.name + ".previous")
    for path in (clone, staging, previous):
        shutil.rmtree(str(path), ignore_errors=True)
    try:
        checkout = Checkout(repo, branch, clone)
        checkout.fetch()
        source = checkout.directory(directory)
        shutil.copytree(str(source), str(staging), symlinks=False)
    finally:
        shutil.rmtree(str(clone), ignore_errors=True)
    if out.exists():
        os.rename(str(out), str(previous))
    os.rename(str(staging), str(out))
    shutil.rmtree(str(previous), ignore_errors=True)
    return sum(1 for _ in out.rglob("*.json"))


__all__ = ["fetch_tests", "FetchError"]
