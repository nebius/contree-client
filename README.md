# contree-client

Source and build tooling for the Contree API clients:

- [Python client](client/README.md)
- [JavaScript client](client-js/README.md)
- `codegen/` — OpenAPI generator
- `docs/` — documentation

Generated API files are ignored by Git.

## Development

Set `CONTREE_SPEC` to the OpenAPI specification URL or local path:

```sh
export CONTREE_SPEC=path/to/api.yaml

make          # generate API files; run checks and tests
make docs     # build documentation
make build    # build the Python distribution
```

Copyright 2026 Nebius B.V. Licensed under the [Apache License 2.0](LICENSE).
