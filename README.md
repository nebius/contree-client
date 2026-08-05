# contree-client

Source and build tooling for the Contree API clients:

- [Python client](client/README.md)
- [JavaScript client](client-js/README.md)
- `codegen/` — OpenAPI generator
- `docs/` — documentation

## Architecture

The repository combines a shared OpenAPI generator with hand-written runtime
code for both clients:

```text
OpenAPI specification
        │
        ▼
loader → shared intermediate representation
        │
        ├──→ Python emitter → models, operations and client interfaces
        └──→ JavaScript emitter → ESM modules and TypeScript declarations
```

The generated modules contain the API-specific models, request builders,
response parsers and client methods. The hand-written modules provide stable
transport behavior, including authentication profiles, retries, streaming,
error handling and test doubles.

Both clients follow the same runtime flow:

```text
client method
    │
    ▼
generated request builder → transport runtime → ConTree /v1 API
                                                │
                                                ▼
typed model ← generated response parser ← HTTP response
```

Buffered requests pass through the runtime's logging, error mapping and
optional retry handling. Streaming requests keep the response open and parse
server-sent events into typed operation events. Higher-level helpers reconnect
interrupted event streams and resume from the last received event.

Generated API files are ignored by Git. Builds validate them before packaging
them with the maintained runtime code. End users install complete Python or
JavaScript packages and do not need the generator.

## Development

Set `CONTREE_SPEC` to the OpenAPI specification URL or local path:

```sh
export CONTREE_SPEC=path/to/api.yaml

make          # generate API files; run checks and tests
make docs     # build documentation
make build    # build the Python and npm distributions
```

The root project is a `uv` workspace containing the generator and Python
client. JavaScript development also requires Node.js and npm.

Generation follows the same process for both languages:

1. Load the OpenAPI document and resolve its local references.
2. Convert schemas and paths into the shared intermediate representation.
3. Render the language-specific package into a staging directory.
4. Format, lint and compile the generated files.
5. Replace the previous generated files only after validation succeeds.

CI generates both packages once, builds a Python wheel and npm tarball, then
tests those artifacts across the supported Python, Node.js and operating-system
matrix. Release workflows regenerate from the tagged revision and publish the
packages to PyPI and npm.

Copyright 2026 Nebius B.V. Licensed under the [Apache License 2.0](LICENSE).
