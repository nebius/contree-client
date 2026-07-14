# The spec location is never stored in the repository: the generator
# reads the CONTREE_SPEC environment variable (a CI secret); an
# explicit SPEC=<url-or-path> make variable overrides it.
SPEC ?=
PACKAGE = client/contree_client
JS_PACKAGE = client-js/lib
GENERATED = \
    $(PACKAGE)/__init__.py \
    $(PACKAGE)/base.py \
    $(PACKAGE)/models.py \
    $(PACKAGE)/operations.py \
    $(PACKAGE)/spec_info.py
JS_GENERATED = \
    docs/js/reference.rst \
    $(JS_PACKAGE)/models.js $(JS_PACKAGE)/models.d.ts \
    $(JS_PACKAGE)/operations.js $(JS_PACKAGE)/operations.d.ts \
    $(JS_PACKAGE)/client.js $(JS_PACKAGE)/client.d.ts \
    $(JS_PACKAGE)/specInfo.js $(JS_PACKAGE)/specInfo.d.ts \
    $(JS_PACKAGE)/index.js $(JS_PACKAGE)/index.d.ts
.PHONY: all generate generate-js js lint lint-js typecheck \
    test test-python test-js test-live coverage docs docs-mintlify \
    docs-view build clean

all: generate generate-js lint lint-js typecheck test

# A single phony target keeps `make -j` down to one codegen run; the
# spec is remote, so every invocation regenerates from the fresh spec.
# Codegen applies ruff to its output itself; here we only verify.
generate:
	uv run python -m api_generator $(if $(SPEC),--spec "$(SPEC)") --package $(PACKAGE)
	uv run ruff format --check client
	uv run ruff check client
	uv run ty check $(PACKAGE)

# Codegen runs prettier and `node --check` on its output itself; the
# tsc pass lives in lint-js (it needs the whole package context).
generate-js: client-js/node_modules
	uv run python -m api_generator --lang js $(if $(SPEC),--spec "$(SPEC)") --package $(JS_PACKAGE)

client-js/node_modules: client-js/package.json client-js/package-lock.json
	npm ci --prefix client-js --no-audit --no-fund
	touch client-js/node_modules

js: generate-js lint-js test-js

lint:
	uv run ruff format --check codegen client/tests conftest.py
	uv run ruff check codegen client/tests conftest.py

lint-js: generate-js
	cd client-js && npx prettier --check lib test
	cd client-js && npx tsc --noEmit

typecheck:
	uv run ty check codegen/api_generator

test: test-python test-js

# markdown-pytest picks the annotated documentation pages up itself.
# Default runs are fully offline: live tests against the real API
# (they upload, spawn and cancel!) only run via the test-live target.
test-python: generate
	uv run pytest -v client docs -m "not integration" \
	    --cov=contree_client --cov-report=term-missing

# the node suites spawn the python stub server themselves
test-js: generate-js
	cd client-js && node --test "test/*.test.mjs"

# live tests against the real Contree API through the active
# contree-cli profile; they perform WRITE operations (upload, tag,
# import, spawn, cancel) - never point this at production
test-live: generate
	uv run pytest -v client -m integration

coverage: generate
	uv run pytest -q client docs -m "not integration" \
	    --cov=api_generator --cov=contree_client \
	    --cov-report=term-missing

docs: generate generate-js
	uv run sphinx-build -W -b html docs docs/_build/html

# clean first: the mintlify builder does not remove stale .mdx pages
docs-mintlify: generate generate-js
	rm -rf docs/_build/mintlify
	uv run sphinx-build -W -b mintlify docs docs/_build/mintlify

docs-view: docs-mintlify
	cd docs/_build/mintlify && npx --yes mint dev

# the published sdist is exactly the client/ tree: package with the
# generated modules baked in, tests and its own pyproject/README.
# tests/test_wheel.py is the gate against an incomplete artifact:
# it builds a wheel and imports it from an isolated environment.
build: generate
	rm -rf dist
	uv build client --out-dir dist

clean:
	rm -rf build dist $(GENERATED) $(JS_GENERATED)
