#!/usr/bin/env python3
"""Self-test for the syncintosh agent hooks.

The repository is greenfield, so the hooks are validated here rather than through pytest.
CI (`.github/workflows/agents.yml`) runs this; run it locally with:

    uv run python scripts/agent_hooks/selftest.py

Each case feeds a hook payload on stdin and asserts the hook's decision. No third-party
imports, no network, no writes outside a temp tree.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]

FAILURES: list[str] = []


def _run(
    script: str, tool: str, payload: dict[str, object], *, root: Path | None = None
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"  # keep the hook dir free of __pycache__
    if root is not None:
        env["SYNCINTOSH_HOOK_ROOT"] = str(root)
    return subprocess.run(
        [sys.executable, str(HERE / script), "--tool", tool],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=env,
    )


def _checked(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        FAILURES.append(f"{name}{': ' + detail if detail else ''}")


def _denied(result: subprocess.CompletedProcess[str]) -> bool:
    return "permissionDecision" in result.stdout and "deny" in result.stdout


def test_guard_secrets_allows() -> None:
    read = lambda p: {"tool_input": {"file_path": p}}  # noqa: E731

    # Ordinary docs/source and fixture files pass.
    docs = _run("guard_secrets.py", "claude", read(str(REPO_ROOT / "AGENTS.md")))
    _checked("guard: allow docs", not _denied(docs) and docs.returncode == 0, docs.stdout or docs.stderr)

    fixture = _run("guard_secrets.py", "claude", read(str(REPO_ROOT / "tests" / "fixtures" / "key.pem")))
    _checked("guard: allow fixture .pem", not _denied(fixture), fixture.stdout)

    # A search of the *code* for a path string is not an access to the path.
    grep = _run("guard_secrets.py", "claude", {"tool_input": {"command": 'grep -rn "share/syncintosh" src'}})
    _checked("guard: allow grep of code", not _denied(grep), grep.stdout)

    # A harmless python one-liner is not a secret read.
    py = _run("guard_secrets.py", "claude", {"tool_input": {"command": 'python3 -c "print(1)"'}})
    _checked("guard: allow benign python", not _denied(py), py.stdout)


def test_guard_secrets_denies() -> None:
    read = lambda p: {"tool_input": {"file_path": p}}  # noqa: E731

    for name, payload in (
        ("ssh key", read(str(Path.home() / ".ssh" / "id_ed25519"))),
        (".env", read("/tmp/project/.env")),
        (".env.production", read("/tmp/project/.env.production")),
        ("plaintext work dir", read(str(Path.home() / ".local" / "share" / "syncintosh" / "work" / "x"))),
        ("docker config.json", read(str(Path.home() / ".docker" / "config.json"))),
        ("cat .env", {"tool_input": {"command": "cat .env"}}),
        ("find -exec cat ssh", {"tool_input": {"command": "find ~/.ssh -exec cat {} ;"}}),
        ("jq docker config", {"tool_input": {"command": "jq . ~/.docker/config.json"}}),
        ("keychain read", {"tool_input": {"command": "security find-generic-password -a me"}}),
        ("python open ssh", {"tool_input": {"command": "python3 -c \"open('/Users/me/.ssh/id_rsa')\""}}),
        (
            "uv run python open aws",
            {"tool_input": {"command": "uv run python -c \"open('/Users/me/.aws/credentials')\""}},
        ),
        ("dollar-home ssh", {"tool_input": {"command": "rg -n '' $HOME/.ssh"}}),
        ("NUL-name .pem", read("a\u0000b.pem")),
    ):
        result = _run("guard_secrets.py", "claude", payload)
        _checked(f"guard: deny {name}", _denied(result), result.stdout or result.stderr)

    # `.env.example` is a template, not a secret.
    example = _run("guard_secrets.py", "claude", read("/tmp/project/.env.example"))
    _checked("guard: allow .env.example", not _denied(example), example.stdout)

    # OpenCode convention: deny = exit 2 + stderr reason.
    oc = _run("guard_secrets.py", "opencode", read("/tmp/project/.env"))
    _checked("guard: opencode exit 2", oc.returncode == 2 and "Blocked" in oc.stderr, f"rc={oc.returncode}")

    # Cursor convention: deny = permission JSON.
    cur = _run("guard_secrets.py", "cursor", read("/tmp/project/.env"))
    _checked("guard: cursor deny", json.loads(cur.stdout or "{}").get("permission") == "deny", cur.stdout)

    # Fail closed: a malformed path must not crash the hook into an allow.
    weird = _run("guard_secrets.py", "claude", read("x\u0000y"))
    _checked("guard: no crash on NUL path", weird.returncode == 0 and "Traceback" not in weird.stderr, weird.stderr)


def test_guard_secrets_denies_fixture_symlink_to_secret() -> None:
    # A fixture path that is a symlink into a secret dir must still be blocked.
    with tempfile.TemporaryDirectory() as tmp:
        fixtures = Path(tmp) / "tests" / "fixtures"
        fixtures.mkdir(parents=True)
        link = fixtures / "key.pem"
        link.symlink_to(Path.home() / ".ssh" / "id_ed25519")

        result = _run("guard_secrets.py", "claude", {"tool_input": {"file_path": str(link)}})
        _checked("guard: deny fixture symlink to secret", _denied(result), result.stdout or result.stderr)


def _stage(root: Path, layer: str, source: str, *, sub: str = "mod.py") -> str:
    target = root / "src" / "syncintosh" / layer / sub
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")
    return str(target)


def test_check_layering() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        def blocked(layer: str, source: str, name: str) -> None:
            path = _stage(root, layer, source, sub=f"{name}.py")
            result = _run("check_layering.py", "claude", {"tool_input": {"file_path": path}}, root=root)
            _checked(f"layering: block {name}", "block" in result.stdout, result.stdout)

        def allowed(layer: str, source: str, name: str) -> None:
            path = _stage(root, layer, source, sub=f"{name}.py")
            result = _run("check_layering.py", "claude", {"tool_input": {"file_path": path}}, root=root)
            _checked(f"layering: allow {name}", "block" not in result.stdout, result.stdout)

        allowed("core", "from __future__ import annotations\n\nimport json\nfrom .models import Thing\n", "pure_core")
        blocked("core", "from syncintosh.crypto.envelope import seal\n", "core_abs")
        blocked("core", "from ..crypto.envelope import seal\n", "core_rel")
        blocked("core", "from syncintosh import crypto\n", "core_from_pkg")
        blocked("core", "import cryptography\n", "core_third_party")
        blocked("services", "from syncintosh.adapters.jsonc import Jsonc\n", "services_infra")
        blocked("vcs", "from syncintosh.cli import main\n", "infra_cli")
        allowed("cli", "from syncintosh.vcs.git import Git\n", "cli_ok")

        oc = _run(
            "check_layering.py",
            "opencode",
            {"tool_input": {"file_path": _stage(root, "core", "import cryptography\n", sub="oc.py")}},
            root=root,
        )
        _checked(
            "layering: opencode exit 1", oc.returncode == 1 and "violation" in oc.stderr.lower(), f"rc={oc.returncode}"
        )


def test_check_layering_survives_old_python() -> None:
    # The hook may run under a PATH interpreter older than 3.10 (macOS /usr/bin/python3).
    # It must not crash — a crash is a non-zero exit that Claude/Codex read as "no block".
    candidate = Path("/usr/bin/python3")
    if not candidate.exists():
        return
    probe = subprocess.run(
        [str(candidate), "-c", "import sys; print(sys.version_info >= (3, 10))"],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.stdout.strip() != "False":
        return
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = _stage(root, "core", "from syncintosh import crypto\n")
        env = dict(os.environ)
        env["SYNCINTOSH_HOOK_ROOT"] = str(root)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        result = subprocess.run(
            [str(candidate), str(HERE / "check_layering.py"), "--tool", "claude"],
            input=json.dumps({"tool_input": {"file_path": target}}),
            capture_output=True,
            text=True,
            check=False,
            env=env,
            cwd=tmp,
        )
        _checked("layering: no crash on python < 3.10", "Traceback" not in result.stderr, result.stderr)


def test_format_python() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        py = Path(tmp) / "sample.py"
        py.write_text("x=1\n", encoding="utf-8")

        md = _run("format_python.py", "claude", {"tool_input": {"file_path": str(Path(tmp) / "README.md")}})
        _checked("format: skip non-python", md.returncode == 0, md.stdout)

        got = _run("format_python.py", "claude", {"tool_input": {"file_path": str(py)}})
        _checked("format: never crashes", got.returncode == 0, got.stderr)


def main() -> int:
    test_guard_secrets_allows()
    test_guard_secrets_denies()
    test_guard_secrets_denies_fixture_symlink_to_secret()
    test_check_layering()
    test_check_layering_survives_old_python()
    test_format_python()

    if FAILURES:
        print("hook selftest FAILED:", file=sys.stderr)
        for failure in FAILURES:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    print("hook selftest: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
