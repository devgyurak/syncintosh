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

Early skeleton. The current slice is **end-to-end sync for the built-in `zsh` preset**: collect →
strip the preset's non-synced keys → encrypt → commit → fetch → plaintext 3-way merge → apply,
across two Macs through an encrypted git repository. The remaining presets (VS Code, Docker), the
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
  built-in preset catalog (`zsh` today; VS Code and Docker are next). `syncintosh init --preset`
  names one or more presets.

## Development

```bash
uv sync --all-extras                            # environment
uv run ruff format && uv run ruff check --fix   # format + lint
uv run ty check                                 # type check
uv run pytest                                   # unit + integration, 90% floor
```

Run the four gates before considering a change done — see AGENTS.md §1.

## License

AGPL-3.0-or-later. See `LICENSE`.
