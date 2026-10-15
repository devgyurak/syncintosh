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

Early skeleton. The current slice is the pure, key-level merge engine that the conflict
resolver is built on. See `AGENTS.md` for the architecture and `CLAUDE.md` for
agent-specific notes.

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
