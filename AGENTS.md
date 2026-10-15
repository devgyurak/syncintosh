# AGENTS.md — syncintosh

**syncintosh** (sync + Macintosh) is a CLI that keeps developer-environment settings in
step across two or more Macs through an **end-to-end encrypted git repository**. It shows
the differences between machines, lets you pick what to align, and resolves conflicts with
**format-aware 3-way merge plus optional LLM assistance**.

This file is the **single canon for every coding agent**. Codex · Claude Code · Pi ·
Cursor · OpenCode each enter through their own file and converge here (see
`.agents/README.md`).

---

## 1. Commands

Run everything through `uv`. Do not use `pip` or a bare `python`.

```bash
uv sync --all-extras                    # create / refresh the environment
uv run ruff format                      # format first
uv run ruff check --fix                 # lint
uv run ty check                         # type check
uv run pytest                           # tests + coverage gate
uv build                                # distribution artifacts
uv run python scripts/check_agent_config.py   # agent-config integrity
```

**Before you call a task done, all four gates must pass.** If one fails, the task is not
finished. Do not route around a failure with `# noqa`, `# type: ignore`, or `--no-cov` —
fix the cause. If a suppression is genuinely correct, state **why** on the same line.

`--no-cov`-style escapes and lowering the coverage floor are forbidden (see §5).

---

## 2. Architecture — never violate

Dependencies point **one way, inward**. Do not add an import that runs against the arrows.

```
cli/ ──▶ services/ ──▶ core/          ← core is the innermost, pure layer
                          ▲
                          │  ports (Protocols) live in core
   adapters/ · presets/ · crypto/ · vcs/ · system/ · llm/   ← infrastructure, implement ports
```

`core` and `services` never import infrastructure; infrastructure never imports `cli`.

```
src/syncintosh/
├── cli/          Typer entrypoints + Rich rendering. Wiring only — no business logic.
├── core/         Pure domain: models, manifest schema, merge engine, conflict rules, ports.
├── services/     Use-cases: collect · diff · merge · apply · sync · resolve · key-rotate.
├── adapters/     Format adapters: jsonc · toml · yaml · xml · plist · text · binary.
├── presets/      Built-in preset catalog: paths, noise keys, bundle IDs, download URLs, casks.
├── crypto/       AES-256-GCM envelope, HKDF, manifest encryption, key wrapping, keyring.
├── vcs/          git CLI subprocess transport (commit / push / pull).
├── system/       Collection, app detection, launchd, watchdog, secret scan.
└── llm/          OpenAI-compatible client, masking, output validation.
```

### Rules you must keep

1. **`core` is pure.** Standard library only. No third-party imports, no filesystem, no
   network, no `subprocess`, no clocks or environment reads. If it needs the outside
   world, define a **port** (`typing.Protocol`) in `core/ports.py` and let infrastructure
   implement it. The merge engine must be testable by calling it with plain values.

2. **Ports live in `core`, implementations in infrastructure.** `crypto`, `vcs`,
   `system`, `llm`, `adapters`, and `presets` implement ports. `services` orchestrates
   ports and `core`. Neither `core` nor `services` names a concrete adapter class.

3. **`cli` holds no logic.** A command parses arguments, calls a service, and renders the
   result with Rich. Judgment (what to merge, what is a conflict, what to exclude) belongs
   in `core`/`services`, never in the command body.

4. **Collection copies, it does not symlink.** Write settings files into the work
   directory as **regular files**. IDEs use atomic saves (write temp + rename) that sever
   symlinks and silently stop tracking. Do not reintroduce symlinks.

5. **Format adapters preserve comments, key order, and formatting.** Adapters round-trip:
   parse → edit → serialize returns the file with only the intended change. `tomlkit`
   (TOML), `ruamel.yaml` (YAML), and `lxml` (XML) are chosen for this. For JSONC, prefer
   **minimal text edits against the original** over full re-serialization, because no
   library reliably preserves comments *and* trailing commas while rewriting.

6. **No I/O in constructors.** Building a service, adapter, or settings object must not
   touch the network, keychain, or disk. Use explicit factory functions
   (`load_config()`, `open_vault()`), the way `Korail()` avoids I/O in pykorail.
   `pydantic-settings`'s `BaseSettings()` reads environment variables and files the moment it
   is **instantiated**, so construct it only inside `load_config()`; the settings model's
   fields are `frozen=True`.

7. **Immutability at boundaries.** Parsed settings and manifest records are frozen
   dataclasses built by a single classmethod. Do not assemble an object and then assign
   fields (`obj.x = ...`). Fetch dependencies first and pass them to the constructor.

8. **State-changing operations return `None` and raise on failure.** `apply`, `push`,
   `rotate` report success by not raising. Do not return booleans.

9. **Line-level 3-way merge runs behind a port.** `merge3` and `git merge-file` are not
   stdlib, so `core` declares a `MergeDriver` port in `core/ports.py` and infrastructure
   implements it. `core` holds only the key-level merge rules and conflict decisions.

**launchd.** The daemon plist embeds the absolute path of the installed entry point. If
Python or the install method changes, that path breaks silently. `syncintosh daemon install`
must rewrite the plist with the current path every time, and `syncintosh doctor` must verify
it (`docs/design/README.md`, Runtime & layout).

---

## 3. Security invariants — the most dangerous part

syncintosh handles the user's credentials-adjacent configuration and a master key that
cannot be recovered if lost. A mistake here is **silent**: tests still pass, the user's
secrets leak or their data becomes unrecoverable.

### 3.1 Ciphertext envelope (do not "fix" it)

Every stored object is `version(1B) | key_id(4B, big-endian) | nonce(12B) | ciphertext | tag(16B)`.

- The **layout is fixed**. Do not reorder fields, drop the version byte, or fold `key_id`
  into `version` — the format axis and the key axis are separate
  (`docs/design/04-envelope-v1.md`).
- **AAD = length-prefixed (domain label, header, file ID)**, where `header = version | key_id`.
  Authenticating the whole header leaves no unauthenticated cleartext field, and the domain
  label (`object` / `manifest` / `key-wrap`) is what stops one ciphertext from being moved into
  another slot. Never encrypt without it.
- **The nonce is random per encryption.** At settings-file volume a collision is
  negligible; do not switch to a counter.
- A generation's key is wrapped with a KDF (scrypt or Argon2id) for export/import. Do not
  weaken the KDF parameters to speed up tests — stub the clock/work factor instead.
- **Key generations are retained by default**, and rotation protects future data only
  (`docs/design/05-key-generations.md`).

### 3.2 Manifest and identifiers (do not leak paths)

- Each tracked file has a **stable random ID**; the repo stores `objects/<id>.enc` only.
- Real paths, preset names, and scope (common / machine-local) live in the **encrypted
  manifest**, never in a filename.
- **Do not encrypt the path into the filename.** A fresh nonce would rename the file on
  every sync and shred git history into delete+add churn.

### 3.3 Plaintext never enters git

- The plaintext working copy lives **outside** the git directory, under
  `~/.local/share/syncintosh/work/`, and is git-ignored.
- Nothing under that path, and no decrypted object, may be committed. The `guard-secrets`
  hook blocks this; do not work around it.

### 3.4 Secrets are excluded by construction

- The Docker adapter **always strips `auths`** and context TLS certificates. Exclude them
  in code, not by asking the user.
- Dangerous paths are blocked before collection: `~/.ssh` private keys,
  `~/.aws/credentials`, `~/.kube/config`, `~/Library/Cookies`, cache folders.
- A **secret scan** runs before every encrypt. New patterns go in one place in `system/`.

### 3.5 LLM assistance carries guardrails

- **Mask secrets and internal domains before sending.** Never send raw values.
- Support **OpenAI-compatible `base_url`** so a home-server model can be used and nothing
  leaves the network.
- **Validate the suggestion before applying:** JSON/XML parses, `zsh -n` passes.
- Default to **propose → one-click approve**. Auto-apply is per-category opt-in.
- **A backup commit is mandatory before applying.** No exceptions. The backup is **the HEAD
  before apply** — the collect commit (`docs/design/01-sync-model.md`) — not a fresh empty
  commit on every sync.

### 3.6 Never trust a download URL you did not ship

- **Only URLs hard-coded in built-in presets are trusted.**
- A URL that arrives from the LLM or from a synced/community preset is **untrusted**:
  label it as such and show the domain prominently. This blocks a malicious preset from
  planting a download link.

---

## 4. Python conventions

- **Minimum Python 3.12**. `pyproject.toml`'s `requires-python` is the
  source of truth.
- **Every module starts with `from __future__ import annotations`.**
- **Precede it with the SPDX header** on line 1: `# SPDX-License-Identifier: AGPL-3.0-or-later`.
- **Type hints on every public function and method.** `Any` is allowed only at boundaries
  (subprocess output, parsed JSON, `**kwargs`).
- **Imports only needed at runtime stay inside `if TYPE_CHECKING:`.** Heavy dependencies
  (`cryptography`, `lxml`, `openai`) are imported **lazily inside the command** so CLI
  startup stays fast. Prove it with a test that imports the CLI and asserts `cryptography`,
  `lxml`, and `openai` are absent from `sys.modules`.
- **No mutable default arguments.** Collection defaults are `tuple` (`()`).
- **Prefer `Final` string constants + `Literal` aliases over `Enum`** when a value is
  serialized verbatim. Mixed `str` enums can stringify to `"Kind.NAME"` and break the
  wire silently.
- **Text I/O always declares `encoding="utf-8"`** — `open()`, `read_text()`,
  `write_text()`.
- **Comments explain "why", not "what".** Settings files contain deliberately odd,
  vendor-specific values (noise keys, machine-only defaults). Leave the reason so the
  next person does not "tidy" it into a regression.
- **Never log secrets.** Mask sensitive values in `__repr__`.
- **Docstrings and comments are English** (see §9).

### ruff / ty

- Configuration lives in `pyproject.toml` only. Do not add a separate `.ruff.toml`.
- Line length 120. Let the formatter decide line breaks.
- Run `ruff format` → `ruff check --fix` → `ty check`, in that order.
- Turn off a rule only via a **file-scoped `per-file-ignores` entry**, not wholesale.
- A class must not define a method named after a builtin (`list`, `dict`, `type`) — it
  shadows the annotation (`-> list[X]`) and breaks `ty`.

---

## 5. Tests — enforced rules

### 5.1 Unit **and** integration tests are mandatory

- **Unit tests** cover the merge engine, adapters, conflict rules, crypto envelope, and
  manifest round-trips, with no I/O.
- **Integration tests** exercise the real flow end to end against a **temporary git
  remote** and a **temp HOME**, confirming that collect → encrypt → commit → pull →
  decrypt → merge → apply works as a whole.
- A change with no integration coverage of its flow is incomplete.

### 5.2 Coverage floor is 90%

`pytest` runs with `--cov-fail-under=90`. **Falling below fails the build.** Raise it by
writing tests, never by lowering the gate or passing `--no-cov`.

### 5.3 Property tests for the merge engine

The merge engine is tested with **hypothesis**: idempotence (merging the same inputs twice
is a no-op), one-sided changes always win, and a changed key on both sides is the *only*
thing that counts as a conflict. Add properties as you add merge behavior.

### 5.4 No control flow inside tests

`tests/test_style.py` walks the AST and enforces this: **no `if` / `for` / `while`
statements inside a test function**.

- Multiple cases → `@pytest.mark.parametrize`.
- Set-level checks → build a list of violations with a comprehension and compare to `[]`.

```python
# Bad — unknown which case broke, stops at the first failure
def test_ids_unique():
    for rec in records:
        assert rec.id not in seen


# Good — every violation surfaces at once
def test_ids_unique():
    bad = [rec.id for rec in records if rec.id in seen]
    assert bad == []
```

Comprehensions are **expressions** and are allowed. The ban is on control-flow
**statements**.

### 5.5 Given–When–Then

Every test marks its arrange / act / assert boundaries with comments; `tests/test_style.py`
enforces this too.

- `# when` and `# then` are **required**.
- `# given` is written **only when there is setup code**. An empty Given is noise.
- When act and assert are one unit (exception checks), use `# when & then`.
- Order is given → when → then. `# then` before `# when` fails.

### 5.6 No network, ever

Tests never send a request to a real service and never read the real keychain or HOME.
Use fakes and `tmp_path`; point HOME at a temp directory. `tests/fixtures/` holds sample
settings files whose key names come from real config files — **do not invent field names.**

### 5.7 Boundary behavior

This project's bugs live at the edges. Cover: a missing or empty key, a file that does not
exist on one machine, an app that is not installed (apply is deferred), a both-sides
change on the same key, a stale base, a corrupted/tampered ciphertext (AAD mismatch must
raise), and a master key that fails to unwrap.

---

## 6. Security for agents

- **Do not read or print** the plaintext work directory
  (`~/.local/share/syncintosh/work/`), key exports, `*.key`, `*.pem`, `~/.ssh`,
  `~/.aws`, `~/.kube`, `~/.docker/config.json`, `~/Library/Cookies`, or `.env`. The
  `guard-secrets` hook blocks these. If it blocks you, do not work around it — use
  fixtures.
- **Never hard-code** a credential, token, or real key in code, tests, logs, or commits.
- **The master key is unrecoverable if lost.** `init` must force or strongly prompt a
  recovery-key backup; treat any change to that flow as security-critical.
- **The guard is best-effort.** `guard_secrets.py` is a token/regex heuristic in front of the
  real gate (CI + review), not a sandbox. When it cannot decide it **fails closed**. It is
  deliberately loose enough that grepping code for a dangerous-path constant is fine.
- **To inspect a secret-shaped file, use the sanctioned tool.**
  `uv run python scripts/inspect_config.py <path>` prints the key structure with every value
  masked — use it for Docker `config.json` and the like instead of reading the file.
  `tests/fixtures/**` is exempt from the guard so exclusion tests can use fixtures.

---

## 7. CI/CD

- **Branch model** — working PRs target `develop`; only release PRs (`develop` → `main`)
  target `main`. Tags are cut from `main`.
- **CI** (`.github/workflows/ci.yml`) — lint, type check, tests on the supported Python
  versions, the coverage gate, and a build/install check.
- **Agent config** (`.github/workflows/agents.yml`) — runs `scripts/check_agent_config.py`
  and the hook self-tests so a broken pointer or hook fails in PR, not on a user's machine.
- **Release** (`.github/workflows/release.yml`) — builds and publishes to PyPI on a `v*`
  tag and refreshes the Homebrew formula.
- Do not make the coverage gate CI-only, and do not disable it.
- **Local hooks are best-effort; CI is the real gate.** A file changed by `Bash`/`sed`, or by
  a harness whose hook is not wired, can skip a local gate. Every rule that matters
  (layering, coverage, SPDX headers, lazy imports) must also be a pytest test that CI runs —
  not only an edit hook.
- **One implementation of each gate.** Decision logic lives only in `scripts/agent_hooks/`.
  The pytest gates and the tool adapters may only **call** it, never copy it — a copied check
  drifts. (`tests/test_layering.py` imports `check_file` from the hook for exactly this
  reason.)
- **The version is the git tag** (hatch-vcs). Do not hardcode a version in `pyproject.toml`
  or `__init__.py`; `git tag v0.2.0` is the release, and `tests/test_packaging.py` fails if a
  static version reappears.
- **One permission policy across harnesses.** Allow read-only verification
  (`uv run pytest|ruff|ty`, `uv run python scripts/…`, `git status|diff|log`); **ask** for
  mutating commands (`uv add`, `uv sync`, `git commit`, `git push`); **deny** destructive and
  secret reads (`rm -rf`, force-push, `*.key`/`*.pem`, secret dirs). Wire it in
  `.claude/settings.json` and `.opencode/opencode.json`; Pi and Codex follow their own
  settings files, and the shared guard still runs.

---

## 8. Workflow

1. **Read before you change.** This codebase has reason-shaped oddities (noise keys,
   machine-only defaults, vendor quirks).
2. **Do only the requested scope.** Do not "tidy" unrelated things in passing.
3. If you touched the envelope, manifest, or secret-exclusion behavior, **prove it with a
   test** and say so.
4. `ruff format` → `ruff check` → `ty check` → `pytest`, all green.
5. Report factually what changed and what you verified. If something failed, say it failed.

### Commit · PR · branch names

Use `uv` for every command. The commit subject, PR title, and branch share **one prefix**.

```
<PREFIX>: <summary>
```

`ADD:` new — feature · endpoint-like addition · test · workflow · dependency ·
`FIX:` bug fix · `REF:` refactor (behavior unchanged) · `DEL:` removal ·
`DOCS:` documentation only · `UPT:` dependency version bump

`ADD:`/`DEL:` refer to **code and dependencies**. Documentation and agent settings
(`.claude/` · `.agents/` · `.codex/` · `.cursor/` · `.opencode/` · `.pi/`) are always
`DOCS:`, added or not. `.github/workflows/` is code — it runs.

Branches are `<prefix lowercase>/<summary-kebab>` (`fix/aad-mismatch`), cut from
`develop`. Full guidance, edge cases, and the pre-commit checklist live in the `commit`
skill (`.claude/skills/commit/SKILL.md`). Read it before committing.

---

## 9. Documentation and language

- **Comments and documentation are English by default.**
- A **user-facing document** (README, guide) may exist in another language using a
  `-{language code}` suffix: `README-KO.md`, `README-JA.md`. The unsuffixed `README.md`
  stays English. Do not translate identifiers or code.
- Keep `AGENTS.md` and the skills English — they are the canon every agent reads.

---

## 10. Do not

- Send a real request to a user's git remote or app data in a test.
- Put credentials, tokens, or real keys in code, tests, logs, or commits.
- Lower the coverage gate or disable a lint rule to go green.
- Report "fixed" without verifying.
- Read the plaintext work directory or a real keychain to "understand" the format —
  read the code and the fixtures instead.
