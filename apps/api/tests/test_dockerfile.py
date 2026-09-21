"""The production image must satisfy Railway's Dockerfile validator, not just BuildKit.

WHY THIS IS A TEST AND NOT "THE DOCKER JOB WILL CATCH IT"
=========================================================
It will not. BuildKit defaults a cache mount's id to its target, so
`--mount=type=cache,target=/root/.cache/uv` is valid Dockerfile syntax: it builds
locally, and CI's docker job built it green. Railway's validator is stricter and
refuses the file outright, before any build step runs:

    dockerfile invalid: flag '--mount=type=cache,target=/root/.cache/uv'
    is missing an id argument at Line 32

So the deployment failed on a file that every other tool in the chain accepted,
and the only reason the web image was unaffected is that its cache mount already
carried `id=pnpm`. That asymmetry is exactly the kind of thing nobody remembers.

These tests need no Docker and no network. They read the file.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

#: The repository root: this file is apps/api/tests/test_dockerfile.py.
REPO_ROOT = Path(__file__).resolve().parents[3]

#: Every Dockerfile this repository owns. The web image lives in the console's
#: repository, so it is not checked here -- but the failure mode is shared, and its
#: own repository carries the equivalent guard.
DOCKERFILES = ("infra/docker/api.Dockerfile",)

#: `--mount=...` on a RUN line, captured whole so the assertion can name it.
MOUNT_FLAG = re.compile(r"--mount=(?P<spec>[^\s\\]+)")


def _dockerfiles() -> list[Path]:
    return [REPO_ROOT / name for name in DOCKERFILES]


def test_the_dockerfiles_are_where_the_tests_think_they_are() -> None:
    """A guard on the guard.

    If the path is wrong every other test here passes by finding nothing, which is
    worse than failing: it would report a clean bill of health for a file it never
    opened.
    """
    for path in _dockerfiles():
        assert path.is_file(), f"{path} not found -- REPO_ROOT resolved to {REPO_ROOT}"


@pytest.mark.parametrize("name", DOCKERFILES)
def test_every_cache_mount_names_an_id(name: str) -> None:
    """Railway rejects a cache mount with no `id=`, however valid BuildKit finds it."""
    # Instruction lines only. The Dockerfile quotes the rejected flag verbatim in a
    # comment explaining why the id is mandatory, and a whole-file regex flagged that
    # comment as the defect -- which this test did on its first run.
    lines = [
        line
        for line in (REPO_ROOT / name).read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    ]

    cache_mounts = [
        match.group("spec")
        for line in lines
        for match in MOUNT_FLAG.finditer(line)
        if "type=cache" in match.group("spec")
    ]
    # Not a vacuous pass: the image caches uv downloads across two stages, and if
    # that stops being true this assertion should be deleted deliberately rather
    # than quietly satisfied by an empty list.
    assert cache_mounts, f"{name} has no cache mounts -- has the build changed?"

    unnamed = [spec for spec in cache_mounts if "id=" not in spec]
    assert not unnamed, (
        f"{name} has cache mount(s) with no id, which Railway refuses to build: "
        f"{unnamed}. Add `id=<name>,` after `type=cache`."
    )
