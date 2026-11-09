# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every policy each built-in preset has shipped (issue #24): the append-only history behind `rev`.

A preset's **policy** is what decides which bytes an object holds — its files, formats, noise and
excluded keys, and profile layout (`core/policy.py`). The manifest stamps each preset with the
revision (this tuple's length) and digest the writing build used, so a build can tell a repo written
under an older policy (migrate it) from one written under a policy it has never heard of (stop).

**Append, never edit.** When a change to a preset alters its policy digest, add the new digest to the
end of that preset's tuple in the same commit, and to `tests/fixtures/policy_history_shipped.json`.

The digest sees only data. If you change what the **code** does with a preset's files (the projection, the
ignore carry, a strip you switch back on), no field changes, so raise that preset's `policy_epoch` first: it
is part of the digest, and the bump is what produces the new entry to append.

Rewriting or reordering an entry makes every repo stamped under it read as `unknown`.
`tests/unit/test_policy_history.py` fails until the last entry equals the current digest, and prints the
digest to append; it also fails if an entry the fixture pins has changed.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

POLICY_HISTORY: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "zsh": ("1914db8679f8fc449a7c8b0eeb43a67e8ab6c38bd43d44a21acf052bb6bbcec8",),
        "vscode": ("df4dcae1e246dcbc04cf2bdde13dbb167dacd746d8f26729a040ad497356b602",),
        "docker": ("c7551c6c38d3edcecfa3c038eba3a915b9688423d6cc77474a0628f132b55983",),
        "zed": ("42b152727414e64f19e6e819cb4af51f97b6708778cbd2575444ac3fb89636d2",),
        "datagrip": (
            "bd0dea45d8efd447831bb76caf788d04bd47caf937b87c387bb704f58885318d",
            "b16215e84639cefa6984ab1db505041f27f322333ceb347b819e01cda935d095",
        ),
        "clion": (
            "4b5ce7ecd3013d6c5ed368575dcc124c1ee2628f48ecdebf15437c30b450dfdb",
            "a46d533754fd863a6ead9e8c6a7cc46957368060e001899d80e5d15276618c1a",
        ),
        "goland": (
            "03e3d269405186a00c0042a6400a1abfa3985c711cbbdba7c5bee4d838815d3e",
            "90d6ead27e006ac6419a92b903a33a7aa9e77e44c4f6654315c22c0e1187d9c9",
        ),
        "intellij-idea": (
            "0caad46d32663e57ca7f1f25fce22518704531e4f591b37c1459028df96d546f",
            "2343c66c6b805c92a8c02ef8cf22c1203728e81d03810d803342c22e23fee22e",
        ),
        "phpstorm": (
            "ee560ceed51fcbb1424a0bc4e6f909e75c6dff15e8c50d010e7e30644b2b3be1",
            "02e7ccdfb576dc92d6340c1ca90a036d6fe336013021e2c1e2cb62f2c90d1e79",
        ),
        "pycharm": (
            "595236f7ae8b053c40f29de90fc8f99d56dc85f8ada8212aeb486d356bdc29d1",
            "8bb20c233d34586d47b69de4f7955aecf8bf711881d42ca024e68acf9c2300a5",
        ),
        "rubymine": (
            "38e518dd5ad1a43a0a304864b6ac9c68321118fb0e13ee5165e75d187f364c93",
            "0920fd40ad943f56ac437bbedcb82172ae348ed02f3ce4e042c83e7e3d624dbc",
        ),
        "rustrover": (
            "172991e1cf8c02a9a0c7a75393ca2b21e71067770a71cc3d2d4cc7126805c8b7",
            "ecf7f844817ee18273349677482482286ec78f9993457194badb849621340e5c",
        ),
        "webstorm": (
            "f4073ceec5c7017e437a01232fba22d3e08fe0e61aa0ea28421dde74cd945399",
            "8d17d14ba7925843d4fbb57905ef95b53c4f1a739e0619b01c47c2db61d620fb",
        ),
    }
)
