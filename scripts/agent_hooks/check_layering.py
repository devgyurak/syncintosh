#!/usr/bin/env python3
"""Post-hook that catches layer-dependency violations under src/syncintosh/.

Rules (AGENTS.md §2):

    core/        -> stdlib only; other syncintosh layers and third-party forbidden
    services/    -> core only; infrastructure forbidden
    adapters/ · presets/ · crypto/ · vcs/ · system/ · llm/  -> core only; cli forbidden
    cli/         -> unrestricted

Relative imports are resolved against the file's package and `from syncintosh import x`
expands to `syncintosh.x`, so neither form slips past.

Modes:
  * default — judge the file paths in the hook payload;
  * `--event stop` — Cursor's `stop` payload has no paths, so the changed files are read
    from `git status` in the hook root.

The decision logic lives here and only here. The pytest gate imports `check_file` from this
module rather than reimplementing it (AGENTS.md §7), so the two can never drift.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _hookio import (
    emit_allow,
    emit_block,
    emit_note,
    extract_paths,
    parse_args,
    parse_option,
    read_payload,
)

# The hook root is normally the repository that contains this script. Tests point it at a
# throwaway tree via SYNCINTOSH_HOOK_ROOT. Resolve it: on macOS a temp path may be reached
# through /var while the file resolves under /private/var, and relative_to() would fail.
REPO_ROOT = Path(os.environ.get("SYNCINTOSH_HOOK_ROOT") or Path(__file__).resolve().parents[2]).resolve()

INFRA_LAYERS = ("adapters", "presets", "crypto", "vcs", "system", "llm")

# Layer -> internal module prefixes it may not import.
FORBIDDEN: dict[str, tuple[str, ...]] = {
    "core": ("syncintosh.cli", "syncintosh.services", *(f"syncintosh.{n}" for n in INFRA_LAYERS)),
    "services": ("syncintosh.cli", *(f"syncintosh.{n}" for n in INFRA_LAYERS)),
    **dict.fromkeys(INFRA_LAYERS, ("syncintosh.cli",)),
}

# core must stay stdlib-only.
STDLIB_ONLY_LAYERS = frozenset({"core"})

EXPLAIN = {
    "core": "core is the innermost, pure layer. It must stay stdlib-only and free of I/O so "
    "the merge engine is testable with plain values. Declare a port (typing.Protocol) in "
    "core/ports.py and implement it in infrastructure instead.",
    "services": "services coordinates use-cases over core and ports. It must not import "
    "concrete infrastructure — depend on the port, and let cli inject the implementation.",
    "adapters": "infrastructure must not import cli. Wiring flows cli -> services -> core.",
    "presets": "infrastructure must not import cli. Wiring flows cli -> services -> core.",
    "crypto": "infrastructure must not import cli. Wiring flows cli -> services -> core.",
    "vcs": "infrastructure must not import cli. Wiring flows cli -> services -> core.",
    "system": "infrastructure must not import cli. Wiring flows cli -> services -> core.",
    "llm": "infrastructure must not import cli. Wiring flows cli -> services -> core.",
}

THIRD_PARTY = "core is stdlib-only; import a third-party library through a port instead."

# `sys.stdlib_module_names` exists from Python 3.10. This hook may run under an older
# interpreter resolved from PATH (e.g. macOS /usr/bin/python3). When it is absent we cannot
# judge third-party imports — do not crash (that would fail open), and say so.
_STDLIB_MODULES = getattr(sys, "stdlib_module_names", None)


def _layer_and_package(path: Path) -> tuple[str, tuple[str, ...]] | None:
    """Return (layer, dotted package of the file's directory) for a staged source file."""
    try:
        rel = path.resolve().relative_to(REPO_ROOT)
    except (ValueError, OSError):
        return None
    parts = rel.parts
    if len(parts) < 4 or parts[0] != "src":
        return None
    # parts: ('src', '<pkg>', '<layer>', ..., 'file.py')
    pkg, layer = parts[1], parts[2]
    return layer, (pkg, layer, *parts[3:-1])


def _resolve_relative(package_parts: tuple[str, ...], level: int, module: str | None) -> str:
    base = package_parts[: len(package_parts) - (level - 1)] if level > 1 else package_parts
    return ".".join((*base, *module.split("."))) if module else ".".join(base)


def _imported_modules(source: str, package_parts: tuple[str, ...]) -> list[tuple[str, int]]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    modules: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.append((alias.name, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                modules.append((_resolve_relative(package_parts, node.level, node.module), node.lineno))
                for alias in node.names:
                    modules.append((_resolve_relative(package_parts, node.level, alias.name), node.lineno))
            elif node.module:
                modules.append((node.module, node.lineno))
                if node.module == package_parts[0]:  # `from syncintosh import crypto`
                    for alias in node.names:
                        modules.append((f"{node.module}.{alias.name}", node.lineno))
    return modules


def _is_third_party(module: str) -> bool:
    if _STDLIB_MODULES is None:
        return False  # cannot judge on < 3.10; the pytest gate enforces this on the floor
    return module.split(".", 1)[0] not in _STDLIB_MODULES


def check_file(path: Path) -> list[str]:
    """Layer/third-party violations for one file (empty when fine or not a tracked layer)."""
    if path.suffix != ".py" or not path.exists():
        return []
    located = _layer_and_package(path)
    if located is None:
        return []
    layer, package_parts = located
    if layer not in FORBIDDEN:
        return []
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []

    pkg = package_parts[0]
    violations: list[str] = []
    for module, lineno in _imported_modules(source, package_parts):
        if module.split(".", 1)[0] == pkg:
            if any(module == bad or module.startswith(bad + ".") for bad in FORBIDDEN[layer]):
                violations.append(f"  {path}:{lineno} — {layer}/ imports `{module}`.")
        elif layer in STDLIB_ONLY_LAYERS and _is_third_party(module):
            violations.append(f"  {path}:{lineno} — {layer}/ imports third-party `{module}`.")
    return violations


def _changed_paths() -> list[str]:
    """Working-tree files (for Cursor's `stop` payload, which carries no paths)."""
    try:
        result = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    entries = result.stdout.decode("utf-8", "surrogateescape").split("\0")
    return [str(REPO_ROOT / entry[3:]) for entry in entries if len(entry) > 3]


def main() -> None:
    tool = parse_args()
    payload = read_payload()
    event = parse_option("--event")

    paths = _changed_paths() if event == "stop" else extract_paths(payload)

    violations: list[str] = []
    hit_layers: set[str] = set()
    for raw in paths:
        path = Path(raw).expanduser()
        found = check_file(path)
        if found:
            violations.extend(found)
            located = _layer_and_package(path)
            if located is not None:
                hit_layers.add(located[0])

    if not violations:
        if _STDLIB_MODULES is None and any(_layer_and_package(Path(p).expanduser()) for p in paths):
            emit_note(
                tool,
                "Layer check ran on Python < 3.10: the third-party rule for core/ was skipped. "
                "CI enforces it on the supported floor.",
            )
        emit_allow(tool)

    # Cursor re-enters the agent on a `stop` block (up to its loop limit). Once it has
    # already re-entered, report without re-prompting so it does not chase files that are
    # not part of its own change.
    loop_count = payload.get("loop_count")
    if event == "stop" and isinstance(loop_count, int) and loop_count > 0:
        emit_note(tool, "Layer-dependency violation still present (not re-prompting):\n" + "\n".join(violations))

    notes = "\n".join(f"- {EXPLAIN[layer]}" for layer in sorted(hit_layers))
    if any("third-party" in violation for violation in violations):
        notes += f"\n- {THIRD_PARTY}"
    guidance = (
        "\n\nOnly fix files that are part of your own change. If a listed file is not yours, "
        "report it instead of editing it (AGENTS.md §8)."
        if event == "stop"
        else ""
    )
    emit_block(
        tool,
        "Layer-dependency violation (AGENTS.md §2):\n"
        + "\n".join(violations)
        + f"\n\n{notes}"
        + "\n\nRemove the import, or invert the dependency so it points inward (toward core)."
        + guidance,
    )


if __name__ == "__main__":
    main()
