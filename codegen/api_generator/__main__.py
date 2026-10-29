"""CLI: python -m api_generator [--lang python|js|go] [--spec URL].

The default input is the committed ``api/v1/spec.yaml`` snapshot.
Override it with ``CONTREE_SPEC``, ``codegen.ini`` or ``--spec``.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

import argclass

from api_generator.golang.emitter import generate as generate_go
from api_generator.js.emitter import generate as generate_js
from api_generator.loader import load_spec
from api_generator.python.emitter import generate as generate_python

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

GENERATORS = {
    "python": generate_python,
    "js": generate_js,
    "go": generate_go,
}
DEFAULT_PACKAGE_DIRS = {
    "python": REPO_ROOT / "client" / "contree_client",
    "js": REPO_ROOT / "client-js" / "lib",
    "go": REPO_ROOT / "go" / "v1",
}

CONFIG_FILES = (
    "codegen.ini",
    "~/.config/contree/codegen.ini",
)


class Parser(argclass.Parser):
    spec: str = argclass.Argument(
        default=str(REPO_ROOT / "api" / "v1" / "spec.yaml"),
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
    save_spec: Path | None = argclass.Argument(
        default=None,
        type=Path,
        help="Save the input specification at this path without generating a package.",
    )

    def __call__(self, *args, **kwargs) -> None:
        if self.save_spec is not None:
            spec = load_spec(self.spec)
            self.save_spec.parent.mkdir(parents=True, exist_ok=True)
            with TemporaryDirectory(dir=self.save_spec.parent) as directory:
                staging = Path(directory) / "spec.yaml"
                staging.write_bytes(spec.text.encode("utf-8"))
                staging.replace(self.save_spec)
            print(f"saved {self.save_spec}")
            return

        package_dir = self.package or DEFAULT_PACKAGE_DIRS[self.lang]
        package = GENERATORS[self.lang](self.spec, package_dir)
        print(f"generated {package}")


def main() -> None:
    parser = Parser(
        config_files=CONFIG_FILES,
        auto_env_var_prefix="CONTREE_CODEGEN_",
        prog="api_generator",
        description=(
            "Generate the spec-dependent modules of a Contree client package."
        ),
    )
    parser.parse_args()
    parser()


if __name__ == "__main__":
    main()
