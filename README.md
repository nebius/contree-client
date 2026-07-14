# contree-client (builder repository)

This repository *builds* the Contree API clients: the code generator
in `codegen/api_generator` produces the API surface of the
[Python package](client/README.md) and the
[JavaScript package](client-js/README.md) from one OpenAPI
specification, downloaded at generation time.

Layout:

| Path                       | Role                                                             |
|----------------------------|------------------------------------------------------------------|
| `codegen/`                 | the builder: `api_generator` with per-language emitters (`python/`, `js/`) |
| `client/`                  | the published Python distribution: package, tests, `pyproject.toml` |
| `client/contree_client/`   | static transports + generated modules (gitignored)              |
| `client/tests/`            | Python test suite (shipped inside the sdist)                     |
| `client-js/`               | the published npm package: static runtime + generated `lib/` (gitignored) + node:test suite |
| `docs/`                    | Sphinx documentation (HTML and Mintlify output)                  |

The generated modules are not committed in either language:
`make generate` / `make generate-js` regenerate them from the fresh
spec and gate the output (ruff + ty for Python; prettier,
`node --check` and `tsc --noEmit` for JavaScript). Both packages embed
the spec SHA-256 and a parity test asserts they were built from the
same snapshot. The published artifacts contain no generator.

The spec location is never stored in the repository. The
`CONTREE_SPEC` environment variable (a CI secret) points at the
OpenAPI spec URL or a local `api.yaml`; nothing generates or builds
without it (`SPEC=<url-or-path>` overrides it per make call), and
every test that needs the spec or the generated package is skipped
when it is not exported. A URL is fetched once per test session.

```shell
export CONTREE_SPEC=https://.../api.yaml
make generate     # regenerate client/contree_client from the spec
make generate-js  # regenerate client-js/lib + docs/js/reference.rst (needs node + npm ci)
make test         # offline suites: pytest + node --test (no live API)
make test-live    # live tests (WRITES: upload/spawn/cancel!) - see below
make lint lint-js typecheck
make docs         # Sphinx HTML; docs-view previews the Mintlify output
make build        # sdist + wheel of client/ into dist/
```

## Live integration tests

`make test-live` (= `test-live-python` + `test-live-js`) talks to a
real Contree API and performs write operations (upload, tag, import,
spawn, cancel) — never point it at production. Credentials resolve through the standard Contree
conventions:

- environment first: `CONTREE_TOKEN` (or `NEBIUS_API_KEY`) plus
  `CONTREE_URL`, optionally `CONTREE_PROJECT` (or
  `NEBIUS_AI_PROJECT`) — `contree_client.profiles.from_env()`;
- otherwise the saved profiles under `$CONTREE_HOME` (`CONTREE_PROFILE` selects one; the `[DEFAULT]` section names the active one).

## Continuous integration

`.github/workflows/tests.yml` runs lint, type checks, the Python
suite on Linux/macOS/Windows across Python 3.10–3.14, the node suite
across Node 18.17–24 on the same three platforms and the
documentation build. Live integration is one separate job covering
both languages (when the secrets are present), serialized
repository-wide through a concurrency group — the token allows only
8 concurrent runs and a single concurrent import — with 300 s
per-test timeouts on top of the job limit.

`.github/workflows/publish.yml` publishes on a GitHub release: the
tag (`vX.Y.Z`) sets both package versions, the packages are generated
from the spec, gated (isolated wheel import for PyPI; node tests,
strict tsc and the `prepack` entrypoint check for npm) and published
via OIDC trusted publishing — the `pypi` and `npm` GitHub
environments hold no long-lived tokens.

Repository secrets:

| Secret | Purpose |
|---|---|
| `CONTREE_SPEC` | OpenAPI spec URL — required to generate, build and run the spec-dependent tests |
| `CONTREE_TOKEN` | token for the live integration job |
| `CONTREE_URL` | endpoint for the live integration job (a disposable namespace, not production) |

Without `CONTREE_SPEC` (forks, external PRs) the spec-dependent tests
skip; lint, type checks and the offline unit tests still run.

## Copyright

Nebius B.V. 2026, Licensed under the Apache License, Version 2.0 (see "LICENSE" file).
