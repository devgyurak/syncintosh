# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Ghostty config check behind the `SyntaxValidator` port.

`ghostty +validate-config --config-file=<path>` exits 0 for a valid config and 1 for an invalid one, and
reads no default config file. Three facts of the checker shape this module (Ghostty 1.3.1):

- **It needs a real file.** `--config-file=/dev/stdin` always exits 1, so the text is written to a file first.
- **A relative `config-file = x` include is resolved against the checked file's directory.** The temporary file
  therefore lives next to the target, so an include resolves exactly as Ghostty will resolve it once the file is
  applied; a required include that is missing there is a failure, an optional `?x` one is not. Only a target
  directory that does not exist yet falls back to a private temporary directory.
- **Its stderr echoes values** (`font-size: invalid value "abc"`). The verdict is the exit code alone: the
  output is discarded and a refusal names the subject only (``AGENTS.md`` §4).

The binary is found through Spotlight by bundle ID, never `PATH` (it is not on `PATH`). If it cannot be found
or run, the verdict is a refusal, never a pass: a validator that fails open would let the file it exists to
reject through (``AGENTS.md`` §3.5). Nothing here touches the disk or the process table until `check` runs
(``AGENTS.md`` §2.6).
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from syncintosh.core.errors import SyntaxInvalid
from syncintosh.system.app_detector import find_app_paths

BUNDLE_ID = "com.mitchellh.ghostty"

# A config is small; a check that hangs is a bug, not a slow file. Looser than the shell check's bound because
# this one starts a whole GUI application's binary, whose first launch after a login can be slow.
TIMEOUT_SECONDS = 10

# `theme = X` is looked up under `$HOME/.config/ghostty/themes`, so the check needs the real HOME; nothing
# else of the caller's environment is passed on.
_PATH = "/usr/bin:/bin"

AppFinder = Callable[[str], tuple[str, ...] | None]


def _binary(find: AppFinder) -> str | None:
    """The first installed Ghostty's executable, or `None` when there is none to run."""
    apps = find(BUNDLE_ID)
    candidates = (Path(app) / "Contents" / "MacOS" / "ghostty" for app in apps or ())
    return next((str(each) for each in candidates if each.is_file() and os.access(each, os.X_OK)), None)


def _write_beside(target: Path, data: bytes) -> Path:
    """A hidden `0600` file next to `target` holding `data`, created exclusively; the caller removes it."""
    name = f".{target.name}.{os.urandom(8).hex()}.syncintosh-check"
    path = target.parent / name
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
    return path


class GhosttySyntaxValidator:
    """A `SyntaxValidator` over `ghostty +validate-config`."""

    def __init__(
        self, *, home: str | None = None, find: AppFinder = find_app_paths, timeout: float = TIMEOUT_SECONDS
    ) -> None:
        # Resolved at `check` time, not here: building a validator must not read the environment.
        self._home = home
        self._find = find
        self._timeout = timeout

    def check(self, text: str, *, subject: str) -> None:
        """Raise `SyntaxInvalid` when `text` is not a config Ghostty accepts; return `None` when it is."""
        binary = _binary(self._find)
        if binary is None:
            raise SyntaxInvalid(f"{subject}: no ghostty to validate against")
        home = self._home if self._home is not None else str(Path.home())
        target = Path(home) / (subject[2:] if subject.startswith("~/") else subject)
        data = text.encode("utf-8")
        try:
            if target.parent.is_dir():
                self._run(binary, home, _write_beside(target, data), subject)
            else:
                self._run_in_private_directory(binary, home, target.name, data, subject)
        except SyntaxInvalid:
            raise
        except (OSError, subprocess.SubprocessError) as exc:
            raise SyntaxInvalid(f"{subject}: the ghostty syntax check could not run") from exc

    def _run_in_private_directory(self, binary: str, home: str, name: str, data: bytes, subject: str) -> None:
        with tempfile.TemporaryDirectory(prefix="syncintosh-ghostty-") as directory:
            path = Path(directory) / name
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
            self._run(binary, home, path, subject)

    def _run(self, binary: str, home: str, path: Path, subject: str) -> None:
        """Run the check on `path`, then remove it; only the exit code is read."""
        env = {"HOME": home, "PATH": _PATH, "LC_ALL": "C"}
        try:
            result = subprocess.run(
                [binary, "+validate-config", f"--config-file={path}"],
                capture_output=True,
                timeout=self._timeout,
                env=env,
                check=False,
            )
        finally:
            path.unlink(missing_ok=True)
        if result.returncode != 0:
            raise SyntaxInvalid(f"{subject}: ghostty reported an invalid config")


__all__ = ["BUNDLE_ID", "TIMEOUT_SECONDS", "GhosttySyntaxValidator"]
