# Publishing kaede-bot

The SDK uses standard `pyproject.toml` metadata (PEP 621), Hatchling's PEP 517
build backend, an SPDX MIT license, and a PEP 561 `py.typed` marker. The
`uv.lock` pins development tools; package users resolve the supported runtime
ranges from distribution metadata. Only the Python SDK is licensed and packaged
here; this license file does not change other repository components.

## Validate a release

Use uv 0.11.8, matching CI. From the repository root:

```sh
uv version --project sdk/python --bump patch
uv sync --project sdk/python --locked --python 3.11
uv build --project sdk/python --no-sources
uv run --project sdk/python --no-sync twine check --strict sdk/python/dist/*
for artifact in sdk/python/dist/*.whl sdk/python/dist/*.tar.gz; do
  uv pip install --python sdk/python/.venv/bin/python --reinstall "$artifact"
  uv run --project sdk/python --no-sync python -I -c 'import kaede_bot'
  uv run --project sdk/python --no-sync pytest -q sdk/python/tests
done
```

Start with an empty `sdk/python/dist` directory so old versions cannot be
uploaded accidentally. Inspect both archives; they should contain the SDK,
metadata, README, license, and type marker, with no credentials or repository
build outputs. The source distribution intentionally excludes repository-based
tests: several tests need shared protocol fixtures outside the SDK. CI tests
both installed distributions against those tests from a full checkout.

For CI releases, commit the code changes and push the next `sdk-vX.Y.Z` tag;
no manual version or lockfile bump is required. CI stamps the tag version with
`uv version --project sdk/python --no-sync`, updating package metadata and the
lockfile before the locked install, build, and tests. Changes remain in the CI
checkout; the workflow does not commit back. The local bump above is only needed
when preparing distributions manually. See the [uv version reference](https://docs.astral.sh/uv/reference/cli/#uv-version).

PyPI releases are
immutable: fix a bad release by publishing a new version, never reuse a version.

## First publication with an API token

Create a PyPI API token (account scope is needed to create a new project).
Supply it through `UV_PUBLISH_TOKEN`; uv recognizes this variable directly.
If already exported in your shell, no extra credential configuration is needed:

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
tags. Configure reviewers if your release policy requires them, and protect
release tags with a repository ruleset. No PyPI token secret is needed in GitHub.
These account settings must be configured separately from the workflow file.
Alternatively, set the repository Actions secret `UV_PUBLISH_TOKEN` to a PyPI
API token authorized for `kaede-bot`. When present, the publish job uses that
token with Trusted Publishing disabled. Remove the secret to use OIDC after
configuring the trusted publisher on PyPI.

Push a tag specifying the next SDK version, for example `sdk-v1.0.1`. The workflow
builds and tests before handing the immutable artifacts to a separate publish
job. Only that job has `id-token: write`; without `UV_PUBLISH_TOKEN`, it uses
`uv publish --trusted-publishing always`, which requires short-lived OIDC
credentials and fails if trust is not configured.
Pull requests, main pushes, and manual runs only validate; they do not publish.
Do not push a release tag for a version already uploaded manually.

After publication, verify from outside the checkout:

```sh
uv run --no-project --with kaede-bot==1.0.1 --refresh-package kaede-bot \
  python -I -c 'import kaede_bot; from importlib.metadata import version; print(version("kaede-bot"))'
```

References: [uv publishing](https://docs.astral.sh/uv/guides/package/),
[PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/).
