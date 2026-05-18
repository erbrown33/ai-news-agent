# Development Setup

> **Audience:** contributors and maintainers editing the codebase. End users
> running the agent only need [`user-guide.md`](user-guide.md).

## 1. Install dev dependencies

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,tavily]"
```

The `tavily` extra is needed even if you don't use Tavily at runtime — two
unit tests patch `tavily.TavilyClient` and require the module to be importable.

## 2. Install pre-commit hooks

```bash
pip install pre-commit
pre-commit install
```

`pre-commit install` writes a hook into `.git/hooks/pre-commit` that runs on
every `git commit`. The hook config itself (`.pre-commit-config.yaml`) is
checked into the repo, so the same hooks run for every developer once they've
run `pre-commit install` in their clone.

**This step is per-clone.** If you delete `.git/`, re-clone the repo onto a new
machine, or work from a fresh worktree, you have to run `pre-commit install`
again. The config travels with the repo; the local hook does not.

## 3. What the hooks do

| Hook | Purpose | Auto-fixes? |
|------|---------|-------------|
| `trailing-whitespace` | Strip trailing whitespace (Markdown hard line breaks preserved) | yes |
| `end-of-file-fixer` | Ensure files end with a single newline | yes |
| `mixed-line-ending` | Normalize to LF | yes |
| `check-yaml` / `check-json` | Syntax-check config files | no |
| `check-merge-conflict` | Block commits containing `<<<<<<<` markers | no |
| `check-added-large-files` | Block files over 1 MB | no |
| `detect-private-key` | Block accidentally committed PEM keys | no |
| `ruff` (with `--fix`) | Lint Python; auto-fix where safe | yes |
| `ruff-format` | Enforce ruff's formatter | yes |
| `validate-agent-configs` | Schema-validate `configs/*-agent.yaml` | no |
| `validate-scheduler-config` | Schema-validate `configs/scheduler.yaml` | no |
| `check-no-secrets-in-configs` | Reject API-key-shaped strings in YAML | no |

The full configuration is in [`.pre-commit-config.yaml`](../.pre-commit-config.yaml).

## 4. The "files were modified by this hook" pattern

When an auto-fixing hook (ruff, trailing-whitespace, end-of-file-fixer,
mixed-line-ending) finds something to fix, it **modifies the file and aborts
the commit**. The output looks like:

```
trim trailing whitespace.............................................Failed
- hook id: trailing-whitespace
- exit code: 1
- files were modified by this hook
```

This is intentional — it gives you a chance to review the fix before it
becomes part of the commit. The fix is always the same:

```bash
git add <files the hook touched>
git commit   # same message, same staged files plus the auto-fix
```

A "Failed" message from an auto-fixing hook **does not mean your code is
broken** — it means the hook adjusted whitespace/formatting and wants you to
take a look. The non-auto-fixing hooks (`check-merge-conflict`,
`validate-agent-configs`, etc.) only fail when something is genuinely wrong.

## 5. Running hooks manually

```bash
# Run all hooks against staged files (what a commit would do)
pre-commit run

# Run all hooks against every file in the repo
pre-commit run --all-files

# Run a single hook
pre-commit run ruff --all-files
```

`pre-commit run --all-files` is the quickest way to check the whole repo
without making a commit, e.g. after a big rebase or before opening a PR.

## 6. Lint and tests outside of pre-commit

```bash
ruff check src/ tests/             # lint
ruff format --check src/ tests/    # formatting (without rewriting)
ruff format src/ tests/            # apply formatting
mypy src/                          # type check (advisory)
pytest                             # unit + integration tests
pytest tests/unit/ -q              # unit tests only, quiet
pytest --cov=src/ai_news_agent     # with coverage
```

These same commands are what CI runs. If `pre-commit run --all-files` and
`pytest` both pass locally, CI should be green.

## 7. CI parity

CI installs the test environment with `pip install -e ".[dev,tavily]"` and
runs ruff, mypy (advisory), and pytest. The Docker build stage requires
`README.md` and `LICENSE` in the build context — both are explicitly `COPY`'d
in the Dockerfile because `pyproject.toml` declares `readme` and
`license-files` and hatchling reads them at metadata-build time.

The `[tavily]` extra installs `tavily-python` so the two factory tests that
do `patch("tavily.TavilyClient")` can resolve the patch target. If you add a
new optional provider with its own factory tests, add the matching extra to
the CI install in `.github/workflows/ci.yml`.

## 8. Bumping hook versions

The hook revisions in `.pre-commit-config.yaml` are pinned. To bump them:

```bash
pre-commit autoupdate
pre-commit run --all-files   # make sure nothing regresses
```

Commit the updated `.pre-commit-config.yaml` alongside any auto-fix output.

Note: the ruff revision pinned for the hook (`v0.4.7`) is older than the
ruff version `pyproject.toml`'s `[dev]` extra resolves to. If you see the
local hook and CI disagree on formatting, that's the cause — bump the
pre-commit revision to match the ruff version CI installs.
