# SPDX-License-Identifier: AGPL-3.0-or-later
"""The built-in `docker` preset: the CLI config and the daemon config, with `auths` stripped.

Step 0 evidence (``add-preset`` skill, Step 0-6). Inspected on a Mac, 2026-10, through
``scripts/inspect_config.py --shape-strings`` — the sanctioned way to read a secret-shaped file
without exposing a value (``AGENTS.md`` §6):

```
App:       Docker Desktop 4.91.0
Bundle ID: com.docker.docker          (/Applications/Docker.app/Contents/Info.plist)
Paths:     ~/.docker/config.json
           ~/.docker/daemon.json
Format:    JSON  (a subset of JSONC, so the JSONC adapter's minimal-edit round-trip applies)
Noise:     currentContext  — the endpoint *this* machine talks to
Excluded:  auths            — observed: a credential map, the `docker login` fallback store
           proxies          — documented: httpProxy/httpsProxy carry `user:pass@` URLs
Deny:      ~/.docker/contexts/tls
Download:  https://www.docker.com/products/docker-desktop/     cask: docker-desktop
```

Sources: `docs.docker.com/reference/cli/docker/` ("Docker CLI configuration file"), Docker
Desktop's `/Applications/Docker.app`, and `brew info --cask docker-desktop`.

## What Step 0 changed from the plan

- The plan listed "the plugin absolute paths" as a noise candidate. The masked output says the
  `plugins.<name>.hooks` values are **neither absolute paths nor booleans**, so there is no
  evidence they are machine-local. Nothing unverified goes into the noise list, so `plugins` is
  synced as an ordinary preference. If it turns out to churn between Docker Desktop versions,
  the follow-up is a one-line noise entry — not a silent, guessed exclusion now.
- `proxies` was absent from both files, but Docker's own documentation shows the shape in each:
  the CLI reference has `httpProxy: "http://user:pass@example.com:3128"` and warns that proxy
  settings "may contain sensitive information"; the daemon reference documents a top-level
  `proxies` with `http-proxy`/`https-proxy` URLs. ``AGENTS.md`` §3.4 says a credential is
  excluded **by construction**, not by asking the user, so both files exclude `proxies` on the
  documented schema rather than on an observation this machine could not provide.
- `HttpHeaders` is absent too, and the CLI reference documents it as a map of *arbitrary*
  headers sent on every client→daemon message — a documented place to put an `Authorization:
  Bearer …`, which the secret scan cannot classify (it matches known token shapes, not a value
  that merely looks like a header). Excluded by construction for the same reason. The cost is
  that a custom header set must be reproduced per machine, which is also how a corporate proxy
  header usually ought to behave.
- `~/.docker/certs.d/` and `~/.docker/tls/` (Docker's registry-CA locations) were absent, as was
  `~/.docker/contexts/tls/`. Only the last is denied today — see `system/deny_list.py`.

## `credsStore` and `credHelpers` are machine-local, not portable

Both name a **helper binary on this machine** (`docker-credential-<value>`), so they say where
credentials live *here*, not what the user prefers. A Mac running Docker Desktop stores
`credsStore: "desktop"`; one running colima or OrbStack stores `"osxkeychain"`. Syncing either would
overwrite the other machine's helper name, and the next `docker pull` there would fail with
`docker-credential-desktop: executable file not found` — with its already-stored logins
unreachable. They are `noise`: each machine keeps its own.

## Why `auths` is cut rather than ignored

The merge's `ignore` set keeps a key out of a *conflict* but still writes its value into the
object, so a Docker credential map would be committed. `excluded` is cut before the object is
sealed, on the collect side (`docs/design/02-base-and-merge.md`, "Projection") — and the policy
is looked up by **path**, so a user who tracks `~/.docker/config.json` under their own preset
name still gets the cut. `daemon.json` carries no credential key *on the inspected machine*, but
Docker Desktop's daemon config can name `proxies`, so it excludes that too; it has no
machine-local key here, so no noise entry.
"""

from __future__ import annotations

from syncintosh.core.presets import Preset, PresetFile

CONFIG = ".docker/config.json"
DAEMON = ".docker/daemon.json"

# `auths` is the credential store `docker login` falls back to; `proxies` and `HttpHeaders` are
# documented places for a `user:pass@` URL and an arbitrary (possibly `Authorization:`) header.
CREDENTIAL_KEYS: frozenset[tuple[str, ...]] = frozenset({("auths",), ("proxies",), ("HttpHeaders",)})

# Machine-local by nature: a helper binary's name, and the endpoint this machine talks to.
NOISE_KEYS: frozenset[tuple[str, ...]] = frozenset({("currentContext",), ("credsStore",), ("credHelpers",)})

DOCKER = Preset.create(
    name="docker",
    files=(
        PresetFile.create(
            path=CONFIG,
            format="jsonc",
            noise=NOISE_KEYS,
            excluded=CREDENTIAL_KEYS,
            # Docker's CLI reads this file with a strict JSON parser, so the renderer's JSONC freedom
            # (a trailing comma, a comment) is not allowed to reach it: the check refuses instead of
            # writing a file that stops the user's Docker working (``AGENTS.md`` §3.5).
            validator="json",
        ),
        # The daemon config's `proxies` is a `user:pass@` URL carrier. `HttpHeaders` is a CLI-only
        # key, so it is not repeated here.
        # `dockerd` reads this with the same strict parser, so it is checked the same way.
        PresetFile.create(path=DAEMON, format="jsonc", excluded={("proxies",)}, validator="json"),
    ),
    bundle_id="com.docker.docker",
    download_url="https://www.docker.com/products/docker-desktop/",
    cask="docker-desktop",
)

__all__ = ["CONFIG", "CREDENTIAL_KEYS", "DAEMON", "DOCKER", "NOISE_KEYS"]
