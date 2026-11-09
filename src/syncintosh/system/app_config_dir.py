# SPDX-License-Identifier: AGPL-3.0-or-later
"""An `AppConfigDir` that asks the **installed app** which release folder its settings live in.

A JetBrains product keeps its settings under `<Product><year>.<n>` (`DataGrip2026.2`), and the one trustworthy source
for that name is the app itself: `Contents/Resources/product-info.json`'s `dataDirectoryName`. The folders on disk are
**never** consulted — the highest one may belong to a release that was uninstalled, and syncing into it would write
settings the running app never reads (issue #54).

The app is found by bundle ID through Spotlight (`system/app_detector.py`). Three checks stand between the file and a
resolved name, because the name is later joined onto a path the app does not own:

* `dataDirectoryName` must be exactly `<prefix><year>.<n>` — one path element, so `..` or a `/` cannot steer it;
* `productVendor` must be the vendor the layout names, so a folder name from some other app is not trusted;
* the metadata is read with a size bound and parsed strictly, and a failure of any kind resolves nothing.

With several installs of the one bundle ID the highest release wins and the rest is reported as a note. Nothing here
reads a secret path or the settings folder's content, with one exception: when it is given a home, it reads the
single small file `options/settingsSync.xml` **in memory** to learn whether the app's own settings sync is on
(`settings_sync_enabled`). That file holds a switch and no credential; it is never tracked, sealed or written, a
symlink is not followed, and a file that is absent, too large, unparseable or declares a DOCTYPE reads as "off".
"""

from __future__ import annotations

import json
import os
import stat
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path

from syncintosh.core.config_dirs import ConfigDirMap, ConfigDirSpec
from syncintosh.system.app_detector import find_app_paths

# The real file is tens of KiB. A bound keeps a planted or runaway file from being read whole into memory.
MAX_PRODUCT_INFO_BYTES = 4 * 1024 * 1024

_PRODUCT_INFO = "Contents/Resources/product-info.json"
_SYNC_SETTINGS = "options/settingsSync.xml"
# The real file is a few hundred bytes; a bound keeps a planted one from being read whole.
MAX_SYNC_SETTINGS_BYTES = 64 * 1024

_NOT_FOUND = "the app is not installed on this machine"
_CANNOT_ASK = "the app could not be located (Spotlight did not answer)"
_UNTRUSTED = "the app's own metadata is missing, unreadable or does not match this layout"


class ProductInfoConfigDir:
    """Reads the release folder under `locate`'s answer; constructing it does no I/O."""

    def __init__(
        self, locate: Callable[[str], tuple[str, ...] | None] = find_app_paths, home: Path | None = None
    ) -> None:
        self._locate = locate
        self._home = home

    def read(self, spec: ConfigDirSpec) -> ConfigDirMap:
        """The release folder this machine's installed app uses, or an unresolved map saying why not."""
        found = self._locate(spec.bundle_id)
        if found is None:
            return ConfigDirMap.unresolved(_CANNOT_ASK)
        if not found:
            return ConfigDirMap.unresolved(_NOT_FOUND)
        results = [self._name_of(spec, each) for each in found]
        names = [name for name, _ in results if name is not None]
        # An install whose metadata cannot be used is said so, never silently left out of the candidates.
        notes = tuple(f"{each!r}: {why}" for each, (name, why) in zip(found, results, strict=True) if name is None)
        if not names:
            return ConfigDirMap.unresolved(_UNTRUSTED, notes)
        # The newest release wins: it is the one a person launching the app today runs. The others are noted so
        # `doctor` can say there is more than one (an old copy left in a backup folder, a second channel).
        ordered = sorted(set(names), key=spec.release_key, reverse=True)
        several = (
            () if len(ordered) == 1 else (f"several releases are installed ({', '.join(ordered)}); using {ordered[0]}",)
        )
        enabled, unreadable = self._sync_state(spec, ordered[0])
        return ConfigDirMap.resolved(
            ordered[0], several + notes, settings_sync_enabled=enabled, settings_sync_unreadable=unreadable
        )

    def _sync_state(self, spec: ConfigDirSpec, name: str) -> tuple[bool, bool]:
        """`(enabled, unreadable)` for the app's own settings sync, read from `options/settingsSync.xml` in memory.

        `<component name="SettingsSyncSettings"><option name="syncEnabled" value="true"/></component>`; the IDE
        reads the value case-insensitively, so `TRUE` is on. A **missing** file or key is off (the IDE default). A
        file that **exists but cannot be read safely** (a symlink, a non-regular file, oversize, not UTF-8, a DOCTYPE,
        broken XML) is `unreadable`: reading it as off would let two sync tools fight over the same files.
        """
        if self._home is None or not spec.is_release_name(name):
            return False, False
        path = self._home / spec.real_directory(name) / _SYNC_SETTINGS
        try:
            # No symlink at the file, `O_NONBLOCK` so a FIFO cannot hang the cycle, and the type is checked on the
            # descriptor that is read, so a swap between check and open is not followed.
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return False, False
        except OSError:
            return False, True
        try:
            with os.fdopen(descriptor, "rb") as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    return False, True
                raw = handle.read(MAX_SYNC_SETTINGS_BYTES + 1)
            # Strict UTF-8 only: a UTF-16 file hides a DOCTYPE from a byte scan, so anything else is unreadable.
            text = raw.decode("utf-8")
            if len(raw) > MAX_SYNC_SETTINGS_BYTES or "<!" in text.replace("<!--", "") or "\x00" in text:
                return False, True
            root = ET.fromstring(raw)
        except (OSError, UnicodeDecodeError, ET.ParseError, ValueError, RecursionError):
            return False, True
        enabled = any(
            option.get("name") == "syncEnabled" and (option.get("value") or "").strip().lower() == "true"
            for component in root.iter("component")
            if component.get("name") == "SettingsSyncSettings"
            for option in component.iter("option")
        )
        return enabled, False

    @staticmethod
    def _name_of(spec: ConfigDirSpec, app_path: str) -> tuple[str | None, str]:
        """The validated `dataDirectoryName` of one install, or `None` and why it cannot be trusted. Never raises."""
        if not app_path.endswith(".app"):
            return None, "not an app bundle"
        try:
            raw = _read_bounded(Path(app_path) / _PRODUCT_INFO)
            if raw is None:
                return None, "the metadata is not a regular file or is too large"
            document = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, ValueError, RecursionError) as exc:
            return None, f"the metadata cannot be read ({type(exc).__name__})"
        if not isinstance(document, dict) or document.get("productVendor") != spec.vendor:
            return None, "the metadata does not name this layout's vendor"
        name = document.get("dataDirectoryName")
        if isinstance(name, str) and spec.is_release_name(name):
            return name, ""
        return None, "dataDirectoryName is not a release folder name of this layout"


def _read_bounded(path: Path) -> bytes | None:
    """The file's bytes, or `None` when it is not a regular file or exceeds the bound.

    Opened with `O_NONBLOCK` so a FIFO planted at the path cannot hang the open, and checked on the **descriptor**
    (`fstat`), not on the path, so nothing can be swapped in between the check and the read.
    """
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        handle = os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise
    with handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            return None
        raw = handle.read(MAX_PRODUCT_INFO_BYTES + 1)
    return None if len(raw) > MAX_PRODUCT_INFO_BYTES else raw


__all__ = ["MAX_PRODUCT_INFO_BYTES", "MAX_SYNC_SETTINGS_BYTES", "ProductInfoConfigDir"]
