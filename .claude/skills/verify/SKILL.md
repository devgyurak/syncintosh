---
name: verify
description: Run syncintosh's full quality gate in order and diagnose failures. Use before finishing a task, before committing, or on requests like "검증해줘" (verify), "게이트 돌려줘" (run the gate), or "통과하는지 봐줘" (does it pass). Runs format → lint → type check → test+coverage and explains each failure's common cause.
---

# Run the gate

**All four must pass for the task to be done.** Order matters — the formatter runs first
so the linter does not report formatting twice.

```bash
uv run ruff format
uv run ruff check --fix
uv run ty check
uv run pytest
```

Together:

```bash
uv run ruff format && uv run ruff check --fix && uv run ty check && uv run pytest
```

If a step fails, **stop, fix, and start over** — a fix in an earlier step changes a later
step's result.

## Diagnosing a failure

### `ruff check`

- `RUF012 Mutable default value for class attribute` — in source, add `ClassVar` or move it
  into `__init__`. In tests it is already exempt via `per-file-ignores`.
- `SIM108` (suggests a ternary) — usually a two-branch assignment reads better; do not force
  a long ternary.
- `I001` (import order) — `--fix` handles it.
- Do not disable a rule wholesale. If truly needed, add a reasoned entry to
  `per-file-ignores` in `pyproject.toml`.

### `ty check`

- `Invalid subscript of object of type 'def list(...)'` — a class defines a method named
  `list` (or `dict`/`type`), shadowing the annotation. Rename the method.
- An intentional wrong type in a test — use a `dict[str, Any]` helper or `cast` to state
  the intent. Do not delete the runtime guard test.
- `unresolved-import` (heavy deps like `cryptography`/`lxml`) — stubs may be missing; raise
  it to `warn` in `pyproject.toml` with a reason rather than sprinkling ignores.

### `pytest`

- `Required test coverage of 90% not reached` — **do not lower the gate**; write tests. The
  `Missing` column names the uncovered lines. Delegate to `test-author`.
- `test_every_test_follows_given_when_then[...]` — a test is missing `# when` or `# then`.
  Add `# given` too when there is setup; use `# when & then` when act and assert are one.
- `test_no_control_flow_statements[...]` — a test contains an `if`/`for`/`while`
  **statement**. Expand with `@pytest.mark.parametrize` or compare a violation list to `[]`.
  This is enforced by `tests/test_style.py`.
- An AAD / tamper test failing — the envelope or AAD changed. That is a **security**
  regression; investigate, do not update the fixture to match.
- An integration test failing against the temp remote — check the temp HOME isolation and
  that no real keychain is touched.

## Coverage in detail

```bash
uv run pytest --cov-report=html && open htmlcov/index.html
```

One module:

```bash
uv run pytest --cov=syncintosh.core.merge --cov-report=term-missing
```

## Last

If the gate passed, report what you ran and the result, factually. **Do not claim it passed
if it did not.** If you ran only part of it, say so.
