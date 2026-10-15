# Preset reference table

**Status:** Living table. One row per built-in preset, added **only** after Step 0 of the
[`add-preset` skill](../../.claude/skills/add-preset/SKILL.md) has verified the values on a
Mac that actually has the app. Filled in by
[#6](https://github.com/devgyurak/syncintosh/issues/6).

| Preset | Paths (relative to `$HOME`) | Format | Noise keys | Excluded in code | Bundle ID | Download URL | Cask |
|---|---|---|---|---|---|---|---|
| *(none verified yet)* | — | — | — | — | — | — | — |

## How a row gets filled

1. Inspect the real config — path, format, and the noise keys that would otherwise conflict on
   every sync (skill Step 0-1 … 0-3).
2. Nail the exclusions that must be stripped **in code**, not left to the user (skill Step
   0-4). Docker's `auths` and context TLS certificates are the canonical example.
3. Verify the bundle ID (for app detection), the vendor's official download URL, and the
   Homebrew cask (skill Step 0-5).
4. Record the evidence block (skill Step 0-6) in the preset module docstring and the PR body,
   then add the row here.
5. If a value cannot be verified, **stop** (skill Step 0-7) — a guessed path collects nothing
   and looks like success.

Only the URLs hard-coded in the built-in presets this table describes are trusted
(`AGENTS.md` §3.6). A URL that arrives from the LLM, or from a synced or community preset, is
**untrusted**: label it and show its domain.