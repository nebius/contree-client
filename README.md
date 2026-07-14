# contree-client

Build repository for the Contree API clients:

- [Python](client/README.md)
- [JavaScript/TypeScript](client-js/README.md)

Both clients are generated from the same OpenAPI specification. Generated files
are not committed.

## Layout

- `codegen/` — client generator
- `client/` — Python package and tests
- `client-js/` — JavaScript package and tests
- `docs/` — documentation

## Development

Set the specification URL or local path:

```sh
export CONTREE_SPEC=path/to/api.yaml
```

Pass `SPEC=...` to a `make` command to override it for one invocation.

```sh
make generate                  # generate the Python client
make generate-js               # generate the JavaScript client
make test                      # run tests
make lint lint-js typecheck    # run checks
make docs                      # build documentation
make build                     # build the Python distribution
```

## Copyright

Nebius B.V. 2026, licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE).
