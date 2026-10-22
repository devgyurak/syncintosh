# syncintosh

> sync + Macintosh — keep developer-environment settings in step across Macs.

`syncintosh` is a CLI that reconciles developer settings (shell, IDE, Docker, …) across two
or more Macs through an **end-to-end encrypted git repository**. It shows what differs,
lets you choose what to align, and resolves conflicts with **format-aware 3-way merge** plus
optional **LLM assistance**.

The server only ever sees ciphertext: file **contents**, **paths**, and the **directory
structure** are all encrypted, and the repository can live on any git remote you control
(home server, Gitea, a private GitHub repo).

## Status

Early skeleton. The current slice is **end-to-end sync for the built-in `zsh`, `vscode`, and
`docker` presets**: collect → strip the preset's non-synced keys → encrypt → commit → fetch →
plaintext 3-way merge → apply, across two Macs through an encrypted git repository. The
other format adapters, LLM assistance, and conflict resolution are still to come.

## Limitations

- **Conflict resolution is not implemented yet.** A merge conflict stops the sync and records
  the unresolved sides under `~/.local/share/syncintosh/pending/`; every later `sync` refuses
  until the pending state is cleared. Until `resolve` lands, leave the pending state alone and
  report what happened.
- **A tracked file that is a symlink is reported, not synced.** If `~/.zshrc` is a link into a
  dotfiles checkout, syncintosh will not follow it (collection copies regular files, never
  links) — it lists the path and leaves it alone. Point the tracked path at the real file, or
  track the dotfiles file directly.
- The tracked set is whatever `~/.config/syncintosh/config.json` names, expanded from the
  built-in preset catalog (`zsh`, `vscode`, and `docker` today). `syncintosh init --preset`
  names one or more presets.
- **Key rotation protects future data only.** `syncintosh key rotate --hygiene` mints a new
  generation, re-encrypts every stored object and the manifest under it, and publishes the new
  key wrapped under the old one (`keys/<new_id>.wrap`) so the other Macs absorb it on their next
  `sync`. `--out-of-band` publishes no wrap: hand the new key over with `key export` / `key
  import`. Rotation does **not** make old commits unreadable — anyone who already has the old key
  can still read history, and a machine that held the old key keeps its access. On a compromise,
  rotate out of band or migrate to a new repo; do not rely on rotation to remove a machine.
- **`syncintosh key prune <generation>` frees almost nothing.** Generations are retained by
  default, and reachability only lets prune drop a generation that was published but never used
  (an aborted rotation) — every commit stays reachable unless history is rewritten, which
  syncintosh never does. Prune refuses generation 1 and any generation a reachable object or the
  manifest still uses.

## Development

```bash
uv sync --all-extras                            # environment
uv run ruff format && uv run ruff check --fix   # format + lint
uv run ty check                                 # type check
uv run pytest                                   # unit + integration, 90% floor
```

`zsh` must be on `PATH` for the test suite: the `zsh` preset's syntax check runs `zsh -fn`, and the
tests exercise the real checker rather than a stub. macOS has it; on Linux, `apt-get install zsh`.

Run the four gates before considering a change done — see AGENTS.md §1.

## License

AGPL-3.0-or-later. See `LICENSE`.
