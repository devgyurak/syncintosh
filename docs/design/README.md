# syncintosh — design notes

These notes are the **decision record** for the parts of syncintosh that are expensive to
change once ciphertext is in a user's repo: how git is used, the manifest, the ciphertext
envelope, and key generations. They are also the **"plan"** that the rest of the repo refers
to — `AGENTS.md`, `scripts/agent_hooks/guard_secrets.py`, and the skills point here instead
of at an unversioned document.

| Note | Decides | Status |
|---|---|---|
| [01-sync-model.md](01-sync-model.md) | How git is used, and the sync cycle | Accepted |
| [02-base-and-merge.md](02-base-and-merge.md) | What `base` is, and the first-connect flow | Accepted |
| [03-manifest.md](03-manifest.md) | File IDs, records, canonical serialization | Accepted |
| [04-envelope-v1.md](04-envelope-v1.md) | Envelope layout, AAD, decrypt errors | Accepted |
| [05-key-generations.md](05-key-generations.md) | The key ring, rotation, distribution | Accepted |
| [06-llm-assist.md](06-llm-assist.md) | LLM-assisted resolution: masking, validation, URLs, endpoint | Accepted |
| [presets.md](presets.md) | Built-in preset reference table | Living table |

`Accepted` means implemented or about to be implemented; changing one follows the revision
procedure below. `Living table` means it is filled in incrementally as presets are verified
on a real Mac.

## Revision procedure

The envelope layout, the AAD composition, and key-generation semantics are
**security-critical**. Changing one means updating, in **one commit**:

1. the note,
2. `AGENTS.md` §3.1, and
3. the three `crypto-auditor` copies (`.claude/agents` is the source; `.opencode/agents` and
   `.codex/agents` are verbatim copies).

Then run `uv run python scripts/check_agent_config.py`, which fails when the three auditor
bodies drift apart. Everything else is just "update the note, then the code".

A decision that is still open is marked **Open** in its note rather than silently omitted.

## Runtime & layout

Machine-local state, and where it lives. This replaces the layout section of the old plan;
`scripts/agent_hooks/guard_secrets.py` and the `add-preset` skill link here.

| Path | Holds | Inside git? |
|---|---|---|
| `~/.config/syncintosh/config.json` | **Non-secret** values only: the remote URL, this machine's ID, and the local allowlist of paths it tracks — exact items and directory globs ([03-manifest.md](03-manifest.md)) | no |
| `~/.local/share/syncintosh/repo/` | the clone: `objects/`, `manifest.enc`, `keys/`, `.gitattributes` | it *is* the repo |
| `~/.local/share/syncintosh/applied.json` | the apply ledger: the commit this machine last applied, and the digest it last wrote per file ([01-sync-model.md](01-sync-model.md)) | no — outside the repo |
| `~/.local/share/syncintosh/scan-allow.json` | the secret scan's local allowlist — keyed `(path, label, fingerprint)` entries and the HMAC key, mode 0600 ([01-sync-model.md](01-sync-model.md), "The scan") | no — outside the repo |
| `~/.local/share/syncintosh/pending/` | unresolved merge state, plaintext (§3.3) | no — outside the repo |
| `~/.local/share/syncintosh/sync.lock` | the advisory lock the CLI and the daemon share | no |

`~/.config/syncintosh/` deliberately holds nothing secret, which is why the agent guard does
not block it.

### launchd

The daemon plist embeds the **absolute path** of the installed entry point. If Python or the
install method changes, that path breaks silently. `syncintosh daemon install` must rewrite
the plist with the current path every time, and `syncintosh doctor` must verify it.