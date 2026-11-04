# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the repo writer use-case.

The scan is the encrypt boundary, so the load-bearing property is: a refusal leaves the repo
untouched — nothing sealed, nothing written (``AGENTS.md`` §3.4). A digest mismatch is checked
in front of the scan and writes nothing either.
"""

from __future__ import annotations

import hashlib

import pytest

from syncintosh.core.errors import AuthenticationError, ContentDigestMismatch, SecretScanError
from syncintosh.core.manifest import MANIFEST_SCHEMA, Manifest, ManifestRecord
from syncintosh.core.manifest_codec import loads_manifest
from syncintosh.core.repo import (
    GITATTRIBUTES_CONTENT,
    GITATTRIBUTES_PATH,
    GITIGNORE_CONTENT,
    GITIGNORE_PATH,
    MANIFEST_PATH,
    object_path,
)
from syncintosh.crypto.cipher import EnvelopeCipher
from syncintosh.services.repo_writer import write_repo

ID1 = "0f3c4b5a69788796a5b4c3d2e1f0abcd"
ID2 = "00112233445566778899aabbccddeeff"
CONTENT = b"content"
OTHER_CONTENT = b"other content"
SHA1 = hashlib.sha256(CONTENT).hexdigest()
SHA2 = hashlib.sha256(OTHER_CONTENT).hexdigest()
SEAL_PREFIX = b"SEALED:"
KEY = bytes(range(32))
OTHER_KEY = bytes(range(1, 33))


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
        self.formats: dict[str, str] = {}
        self._refuse = refuse

    def scan(self, plaintext: bytes, *, subject: str, format: str) -> None:
        self.scanned.append(subject)
        self.formats[subject] = format
        if subject == self._refuse:
            raise SecretScanError(f"secret found in {subject}")


def _rec(file_id: str = ID1, sha: str = SHA1, path: str = ".zshrc") -> ManifestRecord:
    return ManifestRecord.create(file_id=file_id, path=path, preset="zsh", scope="common", format="text", sha256=sha)


def _manifest(records: dict[str, ManifestRecord] | None = None) -> Manifest:
    return Manifest.create(schema=MANIFEST_SCHEMA, key_generation=1, records=records or {})


def _write(files, cipher, scanner, *, head, new, contents) -> None:
    write_repo(files=files, cipher=cipher, scanner=scanner, head=head, new=new, contents=contents)


def test_write_repo_seals_the_object_and_the_manifest() -> None:
    # given
    files, cipher, scanner = InMemoryRepoFiles(), FakeCipher(), RecordingScanner()
    new = _manifest({ID1: _rec()})

    # when
    result = _write(files, cipher, scanner, head=None, new=new, contents={ID1: CONTENT})

    # then
    assert result is None
    assert files.read(object_path(ID1)) == SEAL_PREFIX + CONTENT
    sealed = files.read(MANIFEST_PATH)
    assert sealed is not None
    assert loads_manifest(cipher.open(sealed, domain="manifest", file_id="")) == new
    assert files.read(GITATTRIBUTES_PATH) == GITATTRIBUTES_CONTENT.encode("utf-8")
    assert files.read(GITIGNORE_PATH) == GITIGNORE_CONTENT.encode("utf-8")
    assert scanner.scanned == [".zshrc", "manifest"]


def test_writing_the_same_manifest_again_is_a_no_op() -> None:
    # given — the first write settles the repo, so a second write of the same state changes nothing
    files, cipher, scanner = InMemoryRepoFiles(), FakeCipher(), RecordingScanner()
    new = _manifest({ID1: _rec()})
    _write(files, cipher, scanner, head=None, new=new, contents={ID1: CONTENT})
    before = dict(files.files)

    # when
    _write(files, cipher, scanner, head=new, new=new, contents={ID1: CONTENT})

    # then
    assert files.files == before
    assert [entry[1] for entry in cipher.sealed] == [ID1, ""]


def test_force_reseal_rewrites_an_unchanged_object_under_the_new_generation() -> None:
    # given — the plaintext and its record digest are unchanged; only the generation moved
    files, cipher, scanner = InMemoryRepoFiles(), FakeCipher(), RecordingScanner()
    head = _manifest({ID1: _rec()})
    rotated = Manifest.create(schema=MANIFEST_SCHEMA, key_generation=2, records={ID1: _rec()})

    # when
    write_repo(
        files=files,
        cipher=cipher,
        scanner=scanner,
        head=head,
        new=rotated,
        contents={ID1: CONTENT},
        force_reseal=True,
    )

    # then — the object and the manifest are both sealed under generation 2
    assert ("object", ID1, 2, CONTENT) in cipher.sealed
    assert cipher.sealed[-1][0] == "manifest"
    assert cipher.sealed[-1][2] == 2


def test_without_force_reseal_a_rotation_leaves_the_object_on_the_old_generation() -> None:
    # given — the same generation-only change, but `plan_writes` sees an unchanged digest
    files, cipher, scanner = InMemoryRepoFiles(), FakeCipher(), RecordingScanner()
    head = _manifest({ID1: _rec()})
    rotated = Manifest.create(schema=MANIFEST_SCHEMA, key_generation=2, records={ID1: _rec()})

    # when
    write_repo(files=files, cipher=cipher, scanner=scanner, head=head, new=rotated, contents={ID1: CONTENT})

    # then — only the manifest is rewritten, which is exactly why a rotation forces the reseal
    assert ("object", ID1, 2, CONTENT) not in cipher.sealed


def test_a_scan_refusal_writes_nothing() -> None:
    # given — the scanner refuses the object
    files, cipher, scanner = InMemoryRepoFiles(), FakeCipher(), RecordingScanner(refuse=".zshrc")
    new = _manifest({ID1: _rec()})

    # when & then
    with pytest.raises(SecretScanError):
        _write(files, cipher, scanner, head=None, new=new, contents={ID1: CONTENT})
    assert files.files == {}
    assert cipher.sealed == []


def test_a_manifest_refusal_after_the_objects_are_scanned_still_writes_nothing() -> None:
    # given — every payload is scanned before any is sealed
    files, cipher, scanner = InMemoryRepoFiles(), FakeCipher(), RecordingScanner(refuse="manifest")
    new = _manifest({ID1: _rec()})

    # when & then
    with pytest.raises(SecretScanError):
        _write(files, cipher, scanner, head=None, new=new, contents={ID1: CONTENT})
    assert scanner.scanned == [".zshrc", "manifest"]
    assert files.files == {}
    assert cipher.sealed == []


def test_a_digest_mismatch_writes_nothing() -> None:
    # given — the plaintext does not hash to the record's digest
    files, cipher, scanner = InMemoryRepoFiles(), FakeCipher(), RecordingScanner()
    new = _manifest({ID1: _rec()})

    # when & then
    with pytest.raises(ContentDigestMismatch):
        _write(files, cipher, scanner, head=None, new=new, contents={ID1: OTHER_CONTENT})
    assert files.files == {}
    assert cipher.sealed == []
    assert scanner.scanned == []


def test_a_missing_plaintext_is_a_caller_error() -> None:
    # given
    files, cipher, scanner = InMemoryRepoFiles(), FakeCipher(), RecordingScanner()
    new = _manifest({ID1: _rec()})

    # when & then
    with pytest.raises(ValueError):
        _write(files, cipher, scanner, head=None, new=new, contents={})


def test_a_removed_record_deletes_its_object() -> None:
    # given
    files, cipher, scanner = InMemoryRepoFiles(), FakeCipher(), RecordingScanner()
    head = _manifest({ID1: _rec(), ID2: _rec(file_id=ID2, path=".zshenv")})
    files.write(object_path(ID2), b"stale")
    new = _manifest({ID1: _rec()})

    # when
    _write(files, cipher, scanner, head=head, new=new, contents={ID1: CONTENT})

    # then
    assert files.read(object_path(ID2)) is None


def test_an_orphan_object_is_swept() -> None:
    # given — an object with no record in `new`, as a partial failure would leave
    files, cipher, scanner = InMemoryRepoFiles(), FakeCipher(), RecordingScanner()
    files.write(object_path(ID2), b"orphan")
    new = _manifest({ID1: _rec()})

    # when
    _write(files, cipher, scanner, head=None, new=new, contents={ID1: CONTENT})

    # then
    assert files.read(object_path(ID2)) is None
    assert files.read(object_path(ID1)) == SEAL_PREFIX + CONTENT


def test_a_non_slot_leftover_under_objects_is_swept() -> None:
    # given — an atomic-write temp sibling a crash left behind
    files, cipher, scanner = InMemoryRepoFiles(), FakeCipher(), RecordingScanner()
    files.write(f"objects/.{ID1}.enc.3f2a.tmp", b"junk")
    new = _manifest({ID1: _rec()})

    # when
    _write(files, cipher, scanner, head=None, new=new, contents={ID1: CONTENT})

    # then
    assert files.read(f"objects/.{ID1}.enc.3f2a.tmp") is None
    assert files.read(object_path(ID1)) == SEAL_PREFIX + CONTENT


def test_the_scan_is_told_the_format_each_payload_is_in() -> None:
    # given — a record whose format decides how its URL is read (a shell file may reference a
    # credential, a structured file may not), and the manifest, which is canonical JSON
    files, cipher, scanner = InMemoryRepoFiles(), FakeCipher(), RecordingScanner()
    jsonc = ManifestRecord.create(
        file_id=ID1, path="settings.json", preset="vscode", scope="common", format="jsonc", sha256=SHA1
    )
    new = _manifest({ID1: jsonc})

    # when
    _write(files, cipher, scanner, head=None, new=new, contents={ID1: CONTENT})

    # then
    assert scanner.formats == {"settings.json": "jsonc", "manifest": "jsonc"}


class CountingCipher:
    """The real envelope cipher, counting every `seal` so a test can tell carried from re-sealed."""

    def __init__(self, store) -> None:
        self._real = EnvelopeCipher(store)
        self.sealed: list[tuple[str, str, int]] = []

    def seal(self, plaintext: bytes, *, domain, file_id, key_id):
        self.sealed.append((domain, file_id, key_id))
        return self._real.seal(plaintext, domain=domain, file_id=file_id, key_id=key_id)

    def open(self, blob: bytes, *, domain, file_id):
        return self._real.open(blob, domain=domain, file_id=file_id)


def _stored(store, plaintext: bytes = CONTENT, *, file_id: str = ID1, key_id: int = 1) -> bytes:
    return EnvelopeCipher(store).seal(plaintext, domain="object", file_id=file_id, key_id=key_id)


def _carry(files, cipher, scanner, *, new, carried, head=None, contents=None, force_reseal=False) -> None:
    write_repo(
        files=files,
        cipher=cipher,
        scanner=scanner,
        head=head,
        new=new,
        contents={} if contents is None else contents,
        carried=carried,
        force_reseal=force_reseal,
    )


def test_a_carried_blob_is_written_byte_for_byte(memory_key_store) -> None:
    # given
    store = memory_key_store({1: KEY})
    files, cipher, scanner = InMemoryRepoFiles(), CountingCipher(store), RecordingScanner()
    blob = _stored(store)

    # when
    _carry(files, cipher, scanner, new=_manifest({ID1: _rec()}), carried={ID1: blob})

    # then — the stored ciphertext is untouched, and only the manifest was sealed
    assert files.read(object_path(ID1)) == blob
    assert cipher.sealed == [("manifest", "", 1)]


def test_a_carried_blob_with_the_wrong_digest_is_refused_before_anything_is_written(memory_key_store) -> None:
    # given — the blob opens, but its plaintext is not what the record says
    store = memory_key_store({1: KEY})
    files, cipher, scanner = InMemoryRepoFiles(), CountingCipher(store), RecordingScanner()
    blob = _stored(store, OTHER_CONTENT)

    # when & then
    with pytest.raises(ContentDigestMismatch):
        _carry(files, cipher, scanner, new=_manifest({ID1: _rec()}), carried={ID1: blob})
    assert files.files == {}
    assert cipher.sealed == []
    assert scanner.scanned == []


def test_a_carried_plaintext_the_scan_refuses_leaves_the_repo_untouched(memory_key_store) -> None:
    # given
    store = memory_key_store({1: KEY})
    files, cipher, scanner = InMemoryRepoFiles(), CountingCipher(store), RecordingScanner(refuse=".zshrc")
    blob = _stored(store)

    # when & then
    with pytest.raises(SecretScanError):
        _carry(files, cipher, scanner, new=_manifest({ID1: _rec()}), carried={ID1: blob})
    assert files.files == {}
    assert cipher.sealed == []


def test_a_carried_blob_from_another_generation_is_sealed_again(memory_key_store) -> None:
    # given — the blob is on generation 1, the manifest on generation 2
    store = memory_key_store({1: KEY, 2: OTHER_KEY})
    files, cipher, scanner = InMemoryRepoFiles(), CountingCipher(store), RecordingScanner()
    blob = _stored(store, key_id=1)
    new = Manifest.create(schema=MANIFEST_SCHEMA, key_generation=2, records={ID1: _rec()})

    # when
    _carry(files, cipher, scanner, new=new, carried={ID1: blob})

    # then
    assert ("object", ID1, 2) in cipher.sealed
    assert files.read(object_path(ID1)) != blob
    assert cipher.open(files.read(object_path(ID1)) or b"", domain="object", file_id=ID1) == CONTENT


def test_force_reseal_ignores_carried(memory_key_store) -> None:
    # given — a carried blob on the current generation, but a rotation re-seals everything
    store = memory_key_store({1: KEY})
    files, cipher, scanner = InMemoryRepoFiles(), CountingCipher(store), RecordingScanner()
    blob = _stored(store)

    # when & then — with no plaintext supplied, the carried blob is not a substitute
    with pytest.raises(ValueError):
        _carry(files, cipher, scanner, new=_manifest({ID1: _rec()}), carried={ID1: blob}, force_reseal=True)
    _carry(
        files,
        cipher,
        scanner,
        new=_manifest({ID1: _rec()}),
        carried={ID1: blob},
        contents={ID1: CONTENT},
        force_reseal=True,
    )
    assert ("object", ID1, 1) in cipher.sealed
    assert files.read(object_path(ID1)) != blob


def test_contents_win_over_carried_for_the_same_object(memory_key_store) -> None:
    # given — a carried blob that would be refused (wrong digest) beside a plaintext this machine made
    store = memory_key_store({1: KEY})
    files, cipher, scanner = InMemoryRepoFiles(), CountingCipher(store), RecordingScanner()
    stale = _stored(store, OTHER_CONTENT)

    # when
    _carry(files, cipher, scanner, new=_manifest({ID1: _rec()}), carried={ID1: stale}, contents={ID1: CONTENT})

    # then — the plaintext was sealed, the carried blob was never consulted
    assert ("object", ID1, 1) in cipher.sealed
    assert cipher.open(files.read(object_path(ID1)) or b"", domain="object", file_id=ID1) == CONTENT


def test_a_blob_carried_into_another_objects_slot_fails_the_aad_check(memory_key_store) -> None:
    # given — ID2's ciphertext offered for ID1
    store = memory_key_store({1: KEY})
    files, cipher, scanner = InMemoryRepoFiles(), CountingCipher(store), RecordingScanner()
    blob = _stored(store, file_id=ID2)

    # when & then
    with pytest.raises(AuthenticationError):
        _carry(files, cipher, scanner, new=_manifest({ID1: _rec()}), carried={ID1: blob})
    assert files.files == {}
