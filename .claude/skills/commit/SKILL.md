---
name: commit
description: Commit-message, PR-title, and branch-name conventions for syncintosh. Use right before committing or opening a PR, or on "커밋해줘" (commit), "PR 올려줘" (open a PR), "메시지 어떻게 쓰지" (how to word it), or "접두사 뭐 붙이지" (which prefix). Covers choosing among ADD/FIX/REF/DEL/DOCS/UPT, ambiguous edges, and release-note labels.
---

# Commit · PR · branch names

> Commit only when the user asks. This skill is "how", not "when".

The commit subject, PR title, and branch share **one prefix**. One PR carries one kind of
change, so there is exactly one prefix.

```
<PREFIX>: <summary>

<body — the "why">
```

Summaries and bodies are **English** (`AGENTS.md` §9).

## 1. Look at what changed first

Decide the prefix from the diff, not from memory.

```bash
git status --short
git diff --stat
git diff            # unstaged
git diff --staged   # staged
```

## 2. Pick one prefix

| Prefix | When | Example |
| --- | --- | --- |
| `ADD:` | Something new — feature · test · workflow · dependency | `ADD: zsh preset collector` |
| `FIX:` | A bug — behavior differed from intent | `FIX: reject a ciphertext moved to another object slot` |
| `REF:` | Structure only — **observable behavior unchanged** | `REF: move merge rules into core` |
| `DEL:` | Removal — feature · file · dead code | `DEL: unused key export path` |
| `DOCS:` | Documentation only | `DOCS: add the OpenCode hook adapter` |
| `UPT:` | A dependency **version bump** | `UPT: cryptography 46.0` |

Uppercase, colon, one space. Not `Add:` or `ADD :`.

### When the boundary is unclear

**1. Did observable behavior change?**
- No → `REF:` (that is the definition of a refactor).
- New behavior → `ADD:`.
- Wrong behavior corrected → `FIX:`.

**2. A dependency is added vs. updated.**
- New package in `pyproject.toml` → `ADD:`.
- Only a version was raised → `UPT:`. `uv.lock`-only → `UPT:`.

**3. Docs and code together is not `DOCS:`.**
`DOCS:` means **only** docs changed. Code plus a matching doc update takes the code prefix.

**3-1. Docs are `DOCS:` even when added.**
`ADD:`/`DEL:` are about **code and dependencies**. A new doc file is still `DOCS:`.

**Agent settings are documentation** — everything under `.claude/` · `.agents/` ·
`.codex/` · `.cursor/` · `.opencode/` · `.pi/` (skills, subagents, rules, hook *configs*).
Adding a skill is `DOCS:`.

**But `scripts/agent_hooks/` is code** — it runs. A new hook body or a behavior change is
`ADD:`/`FIX:`. `.github/workflows/` is likewise code.

**4. Tests only → `ADD:`.** Structure-only cleanup of existing tests → `REF:`.

**5. Two prefixes fit → split the commit.**

## 3. Subject line

- English, imperative or noun-phrase, no trailing period.
- Around 50 characters including the prefix.
- State the **change**, not a list of files.

```
Good:  FIX: reject a ciphertext moved to another object slot
Bad:   FIX: crypto.py
Bad:   FIX: fix bug
```

## 4. Body — **why**

Same principle as `AGENTS.md` §4 ("comments say why"). **What** is already in the diff.

This repository has deliberately odd code (noise keys, machine-only defaults, vendor
quirks). If the reason is not recorded, the next person "tidies" it into a silent
regression.

```
FIX: bind AAD to the file ID on decrypt

Without the ID in the AAD, an attacker with write access to the remote could move
one object's ciphertext into another object's slot. AAD binding makes that fail
authentication instead of decrypting the wrong file.
```

The body is optional for a self-evident change, but **mandatory** when you touched a
security invariant (envelope · AAD · manifest · secret exclusion) — record the basis.

## 5. Branch name

`<prefix lowercase>/<summary-kebab>`

```
add/zsh-preset
fix/aad-mismatch
ref/merge-into-core
docs/opencode-hooks
upt/cryptography
```

Cut from `develop` and open the PR against `develop`; `main` is release-only.

## 6. PR title and labels

The title matches the commit: `FIX: reject a ciphertext moved to another object slot`.

The prefix describes the **kind of change**; the label drives **release-note grouping**.

| Prefix | Label | Release note |
| --- | --- | --- |
| `ADD:` | `feature` — but `test` if tests only, `ci` if workflows/lint only, `dependencies` if a new dependency | ✨ (or 🧹 / 📦 for the exceptions) |
| `FIX:` | `bug` | 🐛 Bug fix |
| `REF:` | `refactor` (`ci` / `test` if only CI / tests) | 🧹 Internal |
| `DEL:` | `refactor`, plus `breaking` if a public API was removed | 🧹 (💥 with `breaking`) |
| `DOCS:` | `documentation` | 📝 Documentation |
| `UPT:` | `dependencies` | 📦 Dependencies |

The `ADD:` exceptions mirror §3-1: adding only tests or a workflow must not be listed as a
new user feature. `security` and `external-api` are **not** prefixes — they attach to
whatever prefix fits (a crypto fix is `FIX:` + `bug` + `security`). When two labels are set,
the release note lists the PR once, under whichever category appears first in
`.github/release.yml`; keep `security` ahead of `bug`/`feature` there so it wins.

## 7. Pre-commit checklist

- [ ] Does the prefix match the diff? (behavior changed but marked `REF:` is wrong)
- [ ] One kind of change per commit?
- [ ] Does the subject state the **change**?
- [ ] Did you leave the **why** for anything surprising?
- [ ] Did all four gates pass? (`verify` skill)
- [ ] No credential, token, or real key slipped in?

Agent config is not CI-enforced for commit format — the reviewer may fix it, but follow it
anyway; the cost of a misleading history is paid by the next reader.
