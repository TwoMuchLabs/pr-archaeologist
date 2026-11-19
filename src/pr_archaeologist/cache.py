"""Incremental cache: everything learned so far, keyed so nightly runs only pay for new work.

Layout (inside the analysed repo):

    .pr-archaeologist/cache.json.gz

Three independent invalidation keys keep reruns cheap:

* history  -> `head`: the commit the cache was built up to. If it is still an
              ancestor of the current HEAD we only read `head..HEAD`.
* blame    -> per-file blob sha + latest commit touching it: a file is re-blamed only
              when its content or its history changed.
* docs     -> per-file content hash: a Markdown file is re-parsed only when edited.
"""

from __future__ import annotations

import gzip
import json
import os
from typing import Dict

SCHEMA = 1
CACHE_DIR = ".pr-archaeologist"
CACHE_FILE = "cache.json.gz"


def empty_state() -> Dict:
    return {
        "schema": SCHEMA,
        "head": None,         # HEAD the cache was built up to
        "last_commit": None,  # newest analysed commit (HEAD unless HEAD is a merge or our own output)
        "truncated": False,
        "commits": {},   # sha -> commit record
        "files": {},     # current path -> {commits, born, deleted, previous}
        "cochange": {},  # path -> {other path -> times changed together}
        "blame": {},     # path -> {key, blob, lines, commits, symbols, tripwires}
        "docs": {},      # path -> {hash, ...parsed doc}
        "merges": 0,
    }


def cache_path(repo: str) -> str:
    return os.path.join(repo, CACHE_DIR, CACHE_FILE)


def load(repo: str) -> Dict:
    path = cache_path(repo)
    if not os.path.exists(path):
        return empty_state()
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, ValueError, EOFError):
        return empty_state()  # corrupt cache: rebuild rather than fail the nightly run
    if state.get("schema") != SCHEMA:
        return empty_state()
    return state


def save(repo: str, state: Dict) -> str:
    """Write atomically so an interrupted cron run never leaves a half-written cache."""
    directory = os.path.join(repo, CACHE_DIR)
    os.makedirs(directory, exist_ok=True)
    ignore = os.path.join(directory, ".gitignore")
    if not os.path.exists(ignore):
        with open(ignore, "w", encoding="utf-8") as fh:
            fh.write("# pr-archaeologist cache: safe to delete, rebuilt on the next run\n*\n")
    path = cache_path(repo)
    tmp = path + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=5) as fh:
        json.dump(state, fh, separators=(",", ":"), ensure_ascii=False)
    os.replace(tmp, path)
    return path
