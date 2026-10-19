# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration: the encrypted manifest repo end to end.

It exercises the real envelope, a real key ring, and a real git working tree under a temp HOME
(``AGENTS.md`` §5.1). It proves three contracts at once: no path leaks into a filename, an
unchanged sync rewrites nothing (``git status --porcelain`` stays empty), and removing a record
removes its object in the same operation.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

import pytest

from syncintosh.core.manifest import MANIFEST_SCHEMA, Manifest, ManifestRecord
from syncintosh.core.manifest_codec import loads_manifest
from syncintosh.core.repo import (
    GITATTRIBUTES_CONTENT,
    GITATTRIBUTES_PATH,
    GITIGNORE_PATH,
    MANIFEST_PATH,
    object_path,
)
from syncintosh.crypto.cipher import EnvelopeCipher
from syncintosh.services.repo_writer import write_repo
from syncintosh.system.repo_files import FileRepo

pytestmark = pytest.mark.integration

KEY = bytes(range(32))
ID_A = "0f3c4b5a69788796a5b4c3d2e1f0abcd"
ID_B = "00112233445566778899aabbccddeeff"
ZSH_PATH = ".zshrc"
VSCODE_PATH = "Library/Application Support/Code/User/settings.json"
CONTENTS = {ID_A: b"export EDITOR=vim\n", ID_B: b'{"editor.fontSize":14}'}
SHA_A = hashlib.sha256(CONTENTS[ID_A]).hexdigest()
SHA_B = hashlib.sha256(CONTENTS[ID_B]).hexdigest()

ALLOWED = re.compile(r"objects/[0-9a-f]{32}\.enc")


class PassthroughScanner:
    """A clean scanner: the real implementation lands with #7 (decision 3 of the #3 plan)."""

    def scan(self, plaintext: bytes, *, subject: str, format: str) -> None:
        return None


def _manifest() -> Manifest:
    records = {
        ID_A: ManifestRecord.create(
            file_id=ID_A, path=ZSH_PATH, preset="zsh", scope="common", format="text", sha256=SHA_A
        ),
        ID_B: ManifestRecord.create(
            file_id=ID_B,
            path=VSCODE_PATH,
            preset="vscode",
            scope="machine",
            machine="mac-studio",
            format="jsonc",
            sha256=SHA_B,
        ),
    }
    return Manifest.create(schema=MANIFEST_SCHEMA, key_generation=1, records=records)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=test@example.com", "-c", "user.name=syncintosh", *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _relative_files(repo: Path) -> list[str]:
    """Every working-tree file path, excluding the `.git` directory."""
    return sorted(
        path.relative_to(repo).as_posix()
        for path in repo.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(repo).parts
    )


def _sealed(files: FileRepo, relpath: str) -> bytes:
    """Read a repo file that the test knows was written."""
    data = files.read(relpath)
    assert data is not None
    return data


def _scene(tmp_path, memory_key_store):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    files = FileRepo(repo)
    files.write(GITATTRIBUTES_PATH, GITATTRIBUTES_CONTENT.encode("utf-8"))
    cipher = EnvelopeCipher(memory_key_store({1: KEY}))
    return repo, files, cipher, PassthroughScanner()


def test_a_second_write_of_the_same_state_changes_nothing(tmp_path, memory_key_store) -> None:
    # given — a repo settled by a real collect commit
    repo, files, cipher, scanner = _scene(tmp_path, memory_key_store)
    manifest = _manifest()
    write_repo(files=files, cipher=cipher, scanner=scanner, head=None, new=manifest, contents=CONTENTS)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "collect")
    settled = {rel: files.read(rel) for rel in _relative_files(repo)}

    # when — the same inputs are written again
    write_repo(files=files, cipher=cipher, scanner=scanner, head=manifest, new=manifest, contents=CONTENTS)

    # then — the no-rewrite rule leaves git and the bytes untouched
    assert _git(repo, "status", "--porcelain") == ""
    assert {rel: files.read(rel) for rel in _relative_files(repo)} == settled


def test_no_filename_contains_a_path_segment(tmp_path, memory_key_store) -> None:
    # given
    repo, files, cipher, scanner = _scene(tmp_path, memory_key_store)
    manifest = _manifest()
    write_repo(files=files, cipher=cipher, scanner=scanner, head=None, new=manifest, contents=CONTENTS)

    # when
    names = _relative_files(repo)

    # then — every file is an allowlisted slot, and no manifest path segment leaks into a name
    disallowed = [
        name
        for name in names
        if not (ALLOWED.fullmatch(name) or name in (MANIFEST_PATH, GITATTRIBUTES_PATH, GITIGNORE_PATH))
    ]
    assert disallowed == []
    segments = {
        segment
        for record in manifest.records.values()
        for segment in record.path.split("/")
        if segment and re.fullmatch(r"[0-9a-f]+", segment) is None
    }
    leaked = sorted(segment for segment in segments if any(segment in name for name in names))
    assert leaked == []


def test_the_manifest_round_trips_through_the_repo(tmp_path, memory_key_store) -> None:
    # given
    _, files, cipher, scanner = _scene(tmp_path, memory_key_store)
    manifest = _manifest()

    # when
    write_repo(files=files, cipher=cipher, scanner=scanner, head=None, new=manifest, contents=CONTENTS)

    # then
    sealed = _sealed(files, MANIFEST_PATH)
    assert loads_manifest(cipher.open(sealed, domain="manifest", file_id="")) == manifest


def test_an_object_round_trips_through_its_slot(tmp_path, memory_key_store) -> None:
    # given
    _, files, cipher, scanner = _scene(tmp_path, memory_key_store)
    manifest = _manifest()

    # when
    write_repo(files=files, cipher=cipher, scanner=scanner, head=None, new=manifest, contents=CONTENTS)

    # then
    sealed = _sealed(files, object_path(ID_A))
    assert cipher.open(sealed, domain="object", file_id=ID_A) == CONTENTS[ID_A]


def test_removing_a_record_removes_its_object(tmp_path, memory_key_store) -> None:
    # given — a repo holding two records, one of which is dropped
    _, files, cipher, scanner = _scene(tmp_path, memory_key_store)
    manifest = _manifest()
    write_repo(files=files, cipher=cipher, scanner=scanner, head=None, new=manifest, contents=CONTENTS)
    remaining = Manifest.create(schema=MANIFEST_SCHEMA, key_generation=1, records={ID_A: manifest.records[ID_A]})

    # when
    write_repo(
        files=files,
        cipher=cipher,
        scanner=scanner,
        head=manifest,
        new=remaining,
        contents={ID_A: CONTENTS[ID_A]},
    )

    # then
    assert files.read(object_path(ID_B)) is None
    sealed = _sealed(files, MANIFEST_PATH)
    assert loads_manifest(cipher.open(sealed, domain="manifest", file_id="")) == remaining


def test_a_stale_orphan_object_is_removed(tmp_path, memory_key_store) -> None:
    # given — an object with no record in the manifest, as a partial failure leaves behind
    orphan_id = "f" * 32
    _, files, cipher, scanner = _scene(tmp_path, memory_key_store)
    files.write(object_path(orphan_id), b"stale ciphertext")
    manifest = _manifest()

    # when
    write_repo(files=files, cipher=cipher, scanner=scanner, head=None, new=manifest, contents=CONTENTS)

    # then — the orphan is gone and the tracked object still decrypts
    assert files.read(object_path(orphan_id)) is None
    assert cipher.open(_sealed(files, object_path(ID_A)), domain="object", file_id=ID_A) == CONTENTS[ID_A]
