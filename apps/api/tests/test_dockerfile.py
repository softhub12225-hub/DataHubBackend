"""The production image must satisfy Railway's Dockerfile validator, not just BuildKit.

WHY THIS IS A TEST AND NOT "THE DOCKER JOB WILL CATCH IT"
=========================================================
It will not. BuildKit defaults a cache mount's id to its target, so
`--mount=type=cache,target=/root/.cache/uv` is valid Dockerfile syntax: it builds
locally, and CI's docker job built it green. Railway's validator is stricter and
refuses the file outright, before any build step runs:

    dockerfile invalid: flag '--mount=type=cache,target=/root/.cache/uv'
    is missing an id argument at Line 32

Naming the id was not enough either -- Railway then asked for a `cacheKey` prefix,
`id=s/<service id>-<target path>`, and its docs are explicit that environment
variables are invalid inside a cache mount id. So this repository has no cache
mounts: keeping one meant hardcoding a Railway service UUID into an image recipe
that Compose and CI also build. This test guards the next one somebody adds.

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


#: What Railway demands of a cache mount id: `s/<service id>-<target path>`. The
#: service id is a UUID, and it must be literal -- environment variables are invalid
#: inside a cache mount id, which is the whole reason these mounts were removed.
RAILWAY_CACHE_ID = re.compile(r"id=s/[0-9a-fA-F-]{36}-")


@pytest.mark.parametrize("name", DOCKERFILES)
def test_every_cache_mount_names_an_id(name: str) -> None:
    """Any cache mount here must be in the form Railway accepts.

    There are none today, so this passes trivially -- deliberately. It exists for the
    next person who adds one: BuildKit, Compose and the CI docker job will all accept
    the plain form, and the only thing that will object is a deployment.
    """
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
    bad = [spec for spec in cache_mounts if not RAILWAY_CACHE_ID.search(spec)]
    assert not bad, (
        f"{name} has cache mount(s) Railway will refuse to build: {bad}. It requires "
        "id=s/<service id>-<target path>, with a literal UUID -- environment "
        "variables are invalid in a cache mount id. Consider dropping the mount "
        "instead: the manifests-first COPY order already caches the dependency layer "
        "on every build that does not change dependencies."
    )


#: `COPY --from=<stage> <src> <dest>`, capturing the two paths.
STAGE_COPY = re.compile(r"COPY\s+--from=\S+(?:\s+--\S+)*\s+(?P<src>\S+)\s+(?P<dest>\S+)")


@pytest.mark.parametrize("name", DOCKERFILES)
def test_the_virtualenv_is_copied_to_the_path_it_was_built_at(name: str) -> None:
    """A virtualenv is not relocatable, and nothing else in the pipeline notices.

    Every console script in `.venv/bin` carries its interpreter's absolute path in
    its shebang. Build the venv at /build/.venv, copy it to /app/.venv, and every
    entry point points at /build/.venv/bin/python3 -- which does not exist in the
    runtime stage. The container then dies with a message that blames the wrong
    file:

        exec container process (missing dynamic library?)
        `/app/.venv/bin/uvicorn`: No such file or directory

    This shipped. The image built green in CI for months because the docker job
    builds images and never starts one, and the Compose stack had never been run
    either -- the first thing to start a container from this file was a production
    deployment.

    So the invariant is asserted statically: if a stage-to-stage COPY moves a
    virtualenv, source and destination must be the same absolute path.
    """
    text = (REPO_ROOT / name).read_text(encoding="utf-8")
    venv_copies = [
        (match.group("src"), match.group("dest"))
        for line in text.splitlines()
        if not line.lstrip().startswith("#")
        for match in STAGE_COPY.finditer(line)
        if ".venv" in match.group("src")
    ]
    assert venv_copies, f"{name} copies no virtualenv -- has the build changed?"

    for src, dest in venv_copies:
        assert src == dest, (
            f"{name} copies the virtualenv from {src} to {dest}. A venv is not "
            f"relocatable: its scripts hardcode the interpreter path in their "
            f"shebangs, so the container will fail to exec. Build it at the final "
            f"path instead -- set UV_PROJECT_ENVIRONMENT={dest} in the builder stage."
        )
