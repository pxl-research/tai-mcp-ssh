# Development

## Setup

```sh
uv sync                      # create .venv/ from uv.lock
uv run pre-commit install    # run this once per clone; installs commit and push hooks
```

## Common commands

```sh
uv run pytest                                        # full suite, fails below 85% coverage
uv run pytest tests/unit/test_sessions.py --no-cov   # a subset (skips the coverage gate)
uv run ruff check .
uv run ruff format .
uv run mypy src                                      # strict mode
uv run pre-commit run --all-files                    # commit hooks, e.g. before opening a PR
```

The tests are unit-only and never open real SSH connections. Each test module defines its own fake connection, pool, and SFTP client, and passes them to the managers through their constructors.

## Pre-commit hooks

Once you have run `pre-commit install`, every `git commit` runs these hooks:

- `ruff` (lint with `--fix`) and `ruff-format`
- `mypy`, run through `uv run mypy src` so it uses the same dependencies as your venv
- hygiene checks: trailing whitespace, newline at end of file, TOML/YAML syntax, merge-conflict markers, large files (>500 KB), private SSH key detection, and line-ending normalisation

Every `git push` also runs the full `pytest` suite with the coverage gate, and the push is aborted if it fails. To run it without pushing, use `uv run pre-commit run --hook-stage pre-push --all-files`.

Hook versions are pinned in `.pre-commit-config.yaml`. To update them, run `uv run pre-commit autoupdate`.

## No hosted CI

There is no GitHub Actions workflow. The checks run on your machine instead: lint, format, mypy and hygiene on commit, and the test suite on push. Run `uv run pre-commit install` in every clone, or nothing is checked.

## Spec-driven changes (OpenSpec)

Behaviour is specified per capability in `openspec/specs/<capability>/spec.md`. A new feature or behaviour change goes through these steps:

1. Propose it under `openspec/changes/<name>/` with a proposal, a design, spec deltas, and tasks.
2. Implement the tasks.
3. Archive the change, which promotes its deltas into the canonical specs.

Check the specs with `openspec validate --specs --strict`. All of them pass, so treat any warning as something to fix. The `openspec` CLI is installed separately (npm package `@fission-ai/openspec`) and is not part of the hooks.
