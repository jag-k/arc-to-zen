# Contributing and releasing

## Development

```shell
uv sync --locked
uv run ssort --check src tests import_arc.py
uv run ruff format --check .
uv run ruff check .
uv run ty check
uv run pytest
uv build --no-sources
# Explicit opt-in: starts real Zen with NEW temporary profiles and synthetic data.
uv run pytest --run-integration tests/integration
# Supply --zen-binary /path/to/zen when it is not installed at the macOS default.
```



The installable package lives in `src/arc_to_zen/`; tests live in `tests/`.
`uv.lock` pins development tools. `uv run arc-to-zen` and
`uv run python -m arc_to_zen` use the same entry point. The old launcher remains
available as `uv run python import_arc.py`.

ssort orders definitions by dependencies. Ruff handles formatting, import sorting
and linting; ty checks both source and
tests. Tests use pytest assertions, with no native browser launch by default.
Native integration helpers and Marionette are test-only; neither a WebDriver
package nor geckodriver is required to use the migration tool.

From a Git checkout, enable the local checks with `uv run prek install`, or run
`uv run prek run --all-files`. Standard hooks check Python/JSON/TOML/YAML, file hygiene, large files and secrets.
Pinned upstream hooks run uv lock/sync, ssort, Ruff and ty; a local hook runs
pytest through the locked project environment.
GitHub Actions checks definition ordering, formatting, lint, types, unit tests and package builds on
Linux/macOS with Python 3.13/3.14. CI does not launch Zen or access browser profiles.

`uv build --no-sources` creates a wheel and source distribution in `dist/`.
They include code and public project files, not local previews, browser data,
backups or the virtual environment. A wheel can be installed as a CLI with
`uv tool install /path/to/arc_to_zen-0.1.0-py3-none-any.whl`.

## Publishing to PyPI

`.github/workflows/release.yml` follows the same split build/publish pattern as
`gsm-sms`. A pushed `v*` tag runs the normal Linux/macOS checks, requires the tag
to equal `v` plus `[project].version`, builds the sdist and wheel, validates their
metadata and README with Twine, and checks the installed wheel's CLI.
The publish job downloads those exact artifacts and uses PyPI Trusted Publishing
(OIDC), including signed publish attestations. No PyPI API token is needed.
Only the publish job has `id-token: write`; it does not check out or build code.
A manual Actions run performs checks and builds artifacts without publishing.

One-time setup after creating the GitHub repository:

1. Create a GitHub Actions environment named **`pypi`**. Restrict it to release
   tags (`v*`); configure required reviewers if desired.
2. On PyPI, configure a [pending Trusted Publisher](https://pypi.org/manage/account/publishing/)
   for the new project, or add a publisher in an existing project's Publishing settings:

   | Field | Value |
   | --- | --- |
   | PyPI project name | `arc-to-zen` |
   | GitHub owner | The account or organization hosting this repository |
   | GitHub repository | This repository's exact name |
   | Workflow filename | `release.yml` |
   | Environment name | `pypi` |

   The first successful upload through a pending publisher creates the PyPI project.
   A pending publisher does not reserve the package name.
3. Keep the workflow filename and environment name in sync with PyPI's settings.

To release, update the version and lockfile, commit the reviewed changes, then
push the matching tag. For example, with version `0.1.0`:

```shell
uv lock
uv run prek run --all-files
uv build --no-sources
uv run twine check --strict dist/*
git tag v0.1.0
git push origin v0.1.0
```

These commands publish a public release when the configured workflow succeeds.
Use a new version for each release; PyPI does not allow replacing an uploaded file.
After publication, users can install with `uv tool install arc-to-zen`, or run
`uvx arc-to-zen --help` without cloning the repository.

See [PyPI's Trusted Publisher setup](https://docs.pypi.org/trusted-publishers/adding-a-publisher/),
[new project setup](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/),
and [PyPA's publishing action](https://github.com/pypa/gh-action-pypi-publish).
