"""CLI: python -m api_generator [--lang python|js] [--spec URL] [--package DIR].

The spec location is deliberately not baked into the repository: it
comes from the ``CONTREE_SPEC`` environment variable (a CI secret),
a ``codegen.ini`` config file or the ``--spec`` flag.
"""

from __future__ import annotations

from pathlib import Path

import argclass

from api_generator.js.emitter import generate as generate_js
from api_generator.python.emitter import generate as generate_python

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

GENERATORS = {
    "python": generate_python,
    "js": generate_js,
}
DEFAULT_PACKAGE_DIRS = {
    "python": REPO_ROOT / "client" / "contree_client",
    "js": REPO_ROOT / "client-js" / "lib",
}

CONFIG_FILES = (
    "codegen.ini",
    "~/.config/contree/codegen.ini",
)


class Parser(argclass.Parser):
    spec: str = argclass.Argument(
        required=True,
        env_var="CONTREE_SPEC",
        help="Spec location: an http(s) URL or a local file path.",
    )
    lang: str = argclass.Argument(
        default="python",
        choices=tuple(GENERATORS),
        help="Target language of the generated client package.",
    )
    package: Path | None = argclass.Argument(
        default=None,
        type=Path,
        help="The package directory to generate into (default depends on --lang).",
    )

    def __call__(self, *args, **kwargs) -> None:
        package_dir = self.package or DEFAULT_PACKAGE_DIRS[self.lang]
        package = GENERATORS[self.lang](self.spec, package_dir)
        print(f"generated {package}")


def main() -> None:
    parser = Parser(
        config_files=CONFIG_FILES,
        auto_env_var_prefix="CONTREE_CODEGEN_",
        prog="api_generator",
        description=(
            "Generate the spec-dependent modules of contree_client"
            " (models, operations, base API, spec_info, __init__)."
        ),
    )
    parser.parse_args()
    parser()


if __name__ == "__main__":
    main()
