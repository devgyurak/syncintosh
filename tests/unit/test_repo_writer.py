# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the repo writer use-case.

The scan is the encrypt boundary, so the load-bearing property is: a refusal leaves the repo
untouched — nothing sealed, nothing written (``AGENTS.md`` §3.4).
"""

from __future__ import annotations

import pytest

from syncintosh.core.errors import SecretScanError
from syncintosh.core.manifest import MANIFEST_SCHEMA, Manifest, ManifestRecord
from syncintosh.core.manifest_codec import loads_manifest
from syncintosh.core.repo import MANIFEST_PATH, object_path
from syncintosh.services.repo_writer import write_repo

ID1 = "0f3c4b5a69788796a5b4c3d2e1f0abcd"
ID2 = "00112233445566778899aabbccddeeff"
SHA1 = "a" * 64
SHA2 = "b" * 64
SEAL_PREFIX = b"SEALED:"


class FakeCipher:
    """A reversible, inspecting `Cipher`: the prefix lets a test read the plaintext back."""

    def __init__(self) -> None:
        self.sealed: list[tuple[str, str, int, bytes]] = []

    def seal(self, plaintext: bytes, *, domain, file_id, key_id):
        self.sealed.append((domain, file_id, key_id, plaintext))
        return SEAL_PREFIX + plaintext

    def open(self, blob: bytes, *, domain, file_id):
        return blob[len(SEAL_PREFIX) :]


class InMemoryRepoFiles:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    def read(self, relpath: str) -> bytes | None:
        return self.files.get(relpath)

    def write(self, relpath: str, data: bytes) -> None:
        self.files[relpath] = data

    def delete(self, relpath: str) -> None:
        self.files.pop(relpath, None)

    def list_paths(self, prefix: str) -> tuple[str, ...]:
        return tuple(sorted(path for path in self.files if path.startswith(prefix)))


class RecordingScanner:
    def __init__(self, refuse: str | None = None) -> None:
        self.scanned: list[str] = []
        self._refuse = refuse

    def scan(self, plaintext: bytes, *, subject: str) -> None:
        self.scanned.append(subject)
        if subject == self._refuse:
            raise SecretScanError(f"secret found in {subject}")


def _rec(file_id: str = ID1, sha: str = SHA1, path: str = ".zshrc") -> ManifestRecord:
    return ManifestRecord.create(file_id=file_id, path=path, preset="zsh", scope="common", format="text", sha256=sha)


def _manifest(records: dict[str, ManifestRecord] | None = None) -> Manifest:
    return Manifest.create(schema=MANIFEST_SCHEMA, key_generation=1, records=records or {})


def test_write_repo_seals_the_object_and_the_manifest() -> None:
    # given
    files, cipher, scanner = InMemoryRepoFiles(), FakeCipher(), RecordingScanner()
    new = _manifest({ID1: _rec()})

    # when
    result = write_repo(files=files, cipher=cipher, scanner=scanner, head=None, new=new, contents={ID1: b"content"})

    # then
    assert result is None
    assert files.read(object_path(ID1)) == SEAL_PREFIX + b"content"
    sealed = files.read(MANIFEST_PATH)
    assert sealed is not None
    assert loads_manifest(cipher.open(sealed, domain="manifest", file_id="")) == new
    assert scanner.scanned == [".zshrc", "manifest"]


def test_writing_the_same_manifest_again_is_a_no_op() -> None:
    # given — the first write settles the repo, so a second write of the same state changes nothing
    files, cipher, scanner = InMemoryRepoFiles(), FakeCipher(), RecordingScanner()
    new = _manifest({ID1: _rec()})
    write_repo(files=files, cipher=cipher, scanner=scanner, head=None, new=new, contents={ID1: b"content"})
    before = dict(files.files)

    # when
    write_repo(files=files, cipher=cipher, scanner=scanner, head=new, new=new, contents={ID1: b"content"})

    # then
    assert files.files == before
    assert [entry[1] for entry in cipher.sealed] == [ID1, ""]


def test_a_scan_refusal_writes_nothing() -> None:
    # given — the scanner refuses the object
    files, cipher, scanner = InMemoryRepoFiles(), FakeCipher(), RecordingScanner(refuse=".zshrc")
    new = _manifest({ID1: _rec()})

    # when & then
    with pytest.raises(SecretScanError):
        write_repo(files=files, cipher=cipher, scanner=scanner, head=None, new=new, contents={ID1: b"content"})
    assert files.files == {}
    assert cipher.sealed == []


def test_a_manifest_refusal_after_the_objects_are_scanned_still_writes_nothing() -> None:
    # given — every payload is scanned before any is sealed
    files, cipher, scanner = InMemoryRepoFiles(), FakeCipher(), RecordingScanner(refuse="manifest")
    new = _manifest({ID1: _rec()})

    # when & then
    with pytest.raises(SecretScanError):
        write_repo(files=files, cipher=cipher, scanner=scanner, head=None, new=new, contents={ID1: b"content"})
    assert scanner.scanned == [".zshrc", "manifest"]
    assert files.files == {}
    assert cipher.sealed == []


def test_a_missing_plaintext_is_a_caller_error() -> None:
    # given
    files, cipher, scanner = InMemoryRepoFiles(), FakeCipher(), RecordingScanner()
    new = _manifest({ID1: _rec()})

    # when & then
    with pytest.raises(ValueError):
        write_repo(files=files, cipher=cipher, scanner=scanner, head=None, new=new, contents={})


def test_a_removed_record_deletes_its_object() -> None:
    # given
    files, cipher, scanner = InMemoryRepoFiles(), FakeCipher(), RecordingScanner()
    head = _manifest({ID1: _rec(), ID2: _rec(file_id=ID2, path=".zshenv")})
    files.write(object_path(ID2), b"stale")
    new = _manifest({ID1: _rec()})

    # when
    write_repo(files=files, cipher=cipher, scanner=scanner, head=head, new=new, contents={ID1: b"content"})

    # then
    assert files.read(object_path(ID2)) is None
