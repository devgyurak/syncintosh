# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration: a repo initialized on one machine is importable on a second.

It exercises the real ring export, the real commitment map written into the repo working tree,
and the real second-machine import under the temp HOME (``AGENTS.md`` §5.1). Cheap scrypt
parameters are passed explicitly; the production default is never reduced (§3.1).
"""

from __future__ import annotations

import pytest

from syncintosh.core.commitments import loads_commitments
from syncintosh.core.repo import COMMITMENTS_PATH
from syncintosh.crypto.cipher import EnvelopeCipher
from syncintosh.crypto.envelope import decrypt
from syncintosh.crypto.key_material import KeyMaterial
from syncintosh.crypto.ring_export import ScryptParams
from syncintosh.services.keys import import_ring, init_new_repo
from syncintosh.system.live_files import SystemLiveFiles
from syncintosh.system.repo_files import FileRepo

pytestmark = pytest.mark.integration

CHEAP = ScryptParams(log2_n=1, r=1, p=1)
ALLOWED = frozenset({CHEAP})
PASSPHRASE = "a recovery passphrase"
FILE_ID = "0f3c4b5a69788796a5b4c3d2e1f0abcd"
PLAINTEXT = b"export EDITOR=vim\n"


def test_a_repo_initialized_on_one_machine_is_importable_on_another(tmp_path, memory_key_store) -> None:
    # given — machine A creates the repo, records the commitment, and seals an object
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    files = FileRepo(repo_dir)
    codec = KeyMaterial(params=CHEAP, allowed=ALLOWED)
    machine_a = memory_key_store()
    recovery = tmp_path / "recovery.sntr"
    init_new_repo(
        store=machine_a,
        codec=codec,
        files=files,
        live=SystemLiveFiles(),
        recovery_path=str(recovery),
        passphrase=PASSPHRASE,
    )
    key = machine_a.get(1)
    assert key is not None
    blob = EnvelopeCipher(machine_a).seal(PLAINTEXT, domain="object", file_id=FILE_ID, key_id=1)

    # when — machine B imports the ring, checked against the repo's published commitments
    stored = files.read(COMMITMENTS_PATH)
    assert stored is not None
    machine_b = memory_key_store()
    import_ring(
        store=machine_b,
        codec=codec,
        blob=recovery.read_bytes(),
        passphrase=PASSPHRASE,
        published=loads_commitments(stored),
    )

    # then — B opens the very object A sealed
    assert machine_b.generations() == (1,)
    assert decrypt(blob, file_id=FILE_ID, keys=machine_b, domain="object") == PLAINTEXT
