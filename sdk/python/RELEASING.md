# Publishing kaede-bot

The SDK uses standard `pyproject.toml` metadata (PEP 621), Hatchling's PEP 517
build backend, an SPDX MIT license, and a PEP 561 `py.typed` marker. `uv.lock`
pins development tools only; package users get the runtime ranges from the
distribution metadata. The MIT license here covers only the Python SDK, not the
rest of the repository.

## Validate a release

Use uv 0.11.8, matching CI. From the repository root:

```sh
uv lock --project sdk/python --check --python 3.11
uv version --project sdk/python --bump patch
uv sync --project sdk/python --locked --python 3.11
uv build --project sdk/python --no-sources
uv run --project sdk/python --no-sync twine check --strict sdk/python/dist/*
for artifact in sdk/python/dist/*.whl sdk/python/dist/*.tar.gz; do
  uv pip install --python sdk/python/.venv/bin/python --no-deps --reinstall "$artifact"
  uv run --project sdk/python --no-sync python -I -c 'import kaede_bot; from importlib.resources import files; assert files("kaede_bot").joinpath("py.typed").is_file()'
  uv run --project sdk/python --no-sync pytest -q sdk/python/tests
done
```

The first command checks the committed lockfile before the version bump, as CI
does. `--no-deps` keeps the locked development environment while each archive
is installed, and the import check confirms that `py.typed` shipped.

Start with an empty `sdk/python/dist` directory so old versions can't be
uploaded by mistake. Inspect both archives. They should contain the SDK,
metadata, README, license, and type marker, and no credentials or repository
build outputs. The source distribution leaves out the tests, because several
need shared protocol fixtures from outside the SDK. CI runs them against both
installed distributions from a full checkout.

For a CI release, commit your changes and push the next `sdk-vX.Y.Z` tag. You
don't need to bump the version or lockfile. CI stamps the tag version with
`uv version --project sdk/python --no-sync`, which updates the package metadata
and lockfile before the locked install, build, and tests. The workflow doesn't
commit those changes back. The local bump above is only for building
distributions by hand. See the [uv version reference](https://docs.astral.sh/uv/reference/cli/#uv-version).

On an SDK tag, CI also builds the optional Linux x86_64 LiveKit library and
tests encrypted media against a browser. After both that check and the PyPI
publish succeed, the workflow attaches the native archive and its SHA-256 file
to the matching `sdk-vX.Y.Z` GitHub release. A failed native check can be
retried independently of the published Python distributions.

PyPI releases are immutable. Fix a bad release by publishing a new version;
never reuse one.

## First publication with an API token

Create a PyPI API token. It needs account scope, since the project doesn't
exist yet. Export it as `UV_PUBLISH_TOKEN`, which uv reads directly, then run:

```sh
uv publish --trusted-publishing never sdk/python/dist/*
```

Never commit tokens, place them in package metadata, or pass them as command-line
arguments. After the first release, prefer Trusted Publishing for CI; scope any
retained local publishing token to `kaede-bot`.

## GitHub Trusted Publishing (one-time account configuration)

On the PyPI project's Publishing page, add a GitHub publisher with:

| Setting | Value |
| --- | --- |
| Owner | `CookieNom` |
| Repository | `KaedeChat` |
| Workflow filename | `sdk-publish.yml` |
| Environment | `pypi` |

In GitHub, create the `pypi` environment and restrict deployment to `sdk-v*`
tags. Add reviewers if your release policy needs them, and protect release tags
with a repository ruleset. These settings live outside the workflow file, and
GitHub needs no PyPI token secret.

As an alternative, set the repository Actions secret `UV_PUBLISH_TOKEN` to a
PyPI API token for `kaede-bot`. While that secret exists, the publish job uses
the token with Trusted Publishing turned off. Remove it to switch to OIDC once
the trusted publisher is configured on PyPI.

To release, push a tag with the next SDK version, for example `sdk-v1.0.1`. The
workflow builds and tests, then hands the artifacts to a separate publish job.
Only that job has `id-token: write`. Without `UV_PUBLISH_TOKEN`, it runs
`uv publish --trusted-publishing always`, which needs short-lived OIDC
credentials and fails if trust isn't configured. Pull requests, pushes to main,
and manual runs only validate. Don't push a release tag for a version you
already uploaded by hand.

After publication, verify from outside the checkout:

```sh
uv run --no-project --with kaede-bot==1.0.1 --refresh-package kaede-bot \
  python -I -c 'import kaede_bot; from importlib.metadata import version; print(version("kaede-bot"))'
```

References: [uv publishing](https://docs.astral.sh/uv/guides/package/),
[PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/).
