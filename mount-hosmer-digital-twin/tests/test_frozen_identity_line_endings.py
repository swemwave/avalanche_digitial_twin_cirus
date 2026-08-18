"""Frozen identities are SHA-256 over file bytes, so line endings are evidence.

Every experiment specification, manifest and result in `validation-data/` binds
itself to other files by digest. A checkout must therefore hand back exactly the
bytes git stores: with `core.autocrlf=true` an unpinned text file arrives as
CRLF, and every digest frozen over it silently stops reproducing. That is not a
cosmetic failure -- it makes frozen scientific evidence unverifiable, and it
looks identical to tampering.

These tests fail on the defect itself rather than on its downstream symptoms, so
a digest can never again be frozen over a platform-specific byte form.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SHA256_TOKEN = re.compile(rb"\b[0-9a-f]{64}\b")


def _tracked_files() -> list[Path]:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable, so the tracked file set cannot be resolved")
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
    )
    paths = [REPO_ROOT / entry.decode("utf-8") for entry in result.stdout.split(b"\0") if entry]
    if not paths:
        pytest.skip("no tracked files, so this is not a git checkout of the repository")
    return [path for path in paths if path.is_file()]


def _text_files(paths: list[Path]) -> list[tuple[Path, bytes]]:
    """Tracked files git would line-ending filter: everything without a NUL byte."""
    loaded = ((path, path.read_bytes()) for path in paths)
    return [(path, raw) for path, raw in loaded if b"\0" not in raw]


def test_gitattributes_pins_every_text_file_to_lf() -> None:
    """The pin is repository-wide; an allowlist only covers files someone remembered."""
    rules = [
        line.split("#", 1)[0].strip()
        for line in (REPO_ROOT / ".gitattributes").read_text(encoding="utf-8").splitlines()
    ]
    catch_all = [rule for rule in rules if rule.startswith("* ")]
    assert catch_all, ".gitattributes must carry a repository-wide text rule"
    assert all("eol=lf" in rule for rule in catch_all), catch_all


def test_no_tracked_text_file_carries_crlf() -> None:
    """A CRLF working-tree file no longer hashes to the bytes git stores for it."""
    offenders = [
        path.relative_to(REPO_ROOT).as_posix()
        for path, raw in _text_files(_tracked_files())
        if b"\r\n" in raw
    ]
    assert offenders == [], (
        "these files are CRLF in the working tree, so any frozen digest over them "
        f"is unreproducible: {offenders}"
    )


def test_no_frozen_digest_was_taken_over_crlf_bytes() -> None:
    """Catch a digest frozen on a CRLF checkout even after the file itself is fixed.

    A recorded digest that matches the CRLF rendering of a repository file, and
    not its LF rendering, can only have been computed on a checkout that had not
    pinned line endings. The match is over 256 bits, so there is no ambiguity.
    """
    text = _text_files(_tracked_files())
    lf_digests: set[str] = set()
    crlf_digests: dict[str, str] = {}
    for path, raw in text:
        lf = raw.replace(b"\r\n", b"\n")
        crlf = lf.replace(b"\n", b"\r\n")
        lf_digests.add(hashlib.sha256(lf).hexdigest())
        crlf_digests.setdefault(
            hashlib.sha256(crlf).hexdigest(), path.relative_to(REPO_ROOT).as_posix()
        )

    stale = {
        (path.relative_to(REPO_ROOT).as_posix(), digest, crlf_digests[digest])
        for path, raw in text
        for digest in (token.decode("ascii") for token in set(SHA256_TOKEN.findall(raw)))
        if digest in crlf_digests and digest not in lf_digests
    }
    assert stale == set(), (
        "these recorded digests are the SHA-256 of a CRLF byte form and must be "
        f"re-derived over the canonical LF bytes: {sorted(stale)}"
    )
