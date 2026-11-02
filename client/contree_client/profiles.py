"""Contree configuration profiles.

The selected TOML or INI files under `$CONTREE_HOME`: `auth.*`
(secrets) merged over `$CONTREE_HOME/cli.*` (non-secret defaults),
where `CONTREE_HOME` defaults to `$XDG_CONFIG_HOME/contree` and
finally `~/.config/contree`.
"""

from __future__ import annotations

import configparser
import os
import sys
from configparser import ConfigParser
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from .spec_info import DEFAULT_BASE_URL

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomli as tomllib  # ty: ignore[unresolved-import]  # Optional on 3.10.
    except ImportError:
        tomllib = None

AUTH_TYPE_IAM = "iam"
AUTH_TYPE_JWT = "jwt"
PROFILE_PREFIX = "profile:"
DEFAULT_PROFILE = "default"
DEFAULT_IAM_URL = DEFAULT_BASE_URL


class ProfileError(ValueError):
    """The requested profile is missing or incomplete."""


@dataclass(frozen=True)
class Profile:
    """One Contree authentication profile.

    Frozen and comparable; the token never shows up in `repr()`.
    """

    name: str
    url: str
    token: str | None = field(repr=False)
    auth_type: str = AUTH_TYPE_JWT
    project: str | None = None

    def save(self, config: ConfigParser) -> None:
        section = f"{PROFILE_PREFIX}{self.name}"
        if not config.has_section(section):
            config.add_section(section)
        config.set(section, "url", self.url)
        config.set(section, "token", self.token or "")
        config.set(section, "type", self.auth_type)
        if self.project:
            config.set(section, "project", self.project)
        else:
            # a re-save without a project must not resurrect the old one
            config.remove_option(section, "project")


def contree_home() -> Path:
    home = os.environ.get("CONTREE_HOME")
    if home:
        return Path(home).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(xdg).expanduser() / "contree"


def _preferred_file(directory: Path, role: str) -> Path:
    candidate = directory / f"{role}.toml"
    try:
        candidate.stat()
    except FileNotFoundError:
        return directory / f"{role}.ini"
    return candidate


def _profile_sources(path: str | Path | None) -> list[Path]:
    if path is None:
        home = contree_home()
        return [_preferred_file(home, "cli"), _preferred_file(home, "auth")]
    auth = Path(path)
    defaults = (
        _preferred_file(auth.parent, "cli")
        if auth.suffix == ".toml"
        else auth.parent / "cli.ini"
    )
    return [defaults] if defaults == auth else [defaults, auth]


def _read_toml(path: Path, parser: ConfigParser) -> None:
    if tomllib is None:
        raise ProfileError(
            f"cannot read TOML profile file {str(path)!r}; install contree-client[toml]"
        )
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        # Parser messages and chained exceptions can contain credential lines.
        raise ProfileError(f"cannot read TOML profile file {str(path)!r}") from None
    if "profile" in data:
        if not isinstance(data["profile"], str):
            raise ProfileError(f"invalid profile selector in {str(path)!r}")
        cast(dict[str, str], parser.defaults())["profile"] = data["profile"]
    for name, values in data.items():
        if name == "profile" or not isinstance(values, dict):
            continue
        section = PROFILE_PREFIX + name
        if not parser.has_section(section):
            parser.add_section(section)
        for key in ("url", "token", "type", "project"):
            if key not in values:
                continue
            if not isinstance(values[key], str):
                raise ProfileError(f"invalid profile field {key!r} in {str(path)!r}")
            # Direct assignment avoids INI interpolation rules for TOML strings.
            parser[section][key] = values[key].replace("%", "%%")


def _load_profiles(path: str | Path | None) -> tuple[dict[str, Profile], str, bool]:
    sources = _profile_sources(path)
    parser = configparser.ConfigParser()
    has_toml = False
    for source in sources:
        if source.suffix == ".toml":
            has_toml = True
            _read_toml(source, parser)
        else:
            try:
                with source.open(encoding="utf-8") as stream:
                    parser.read_file(stream)
            except FileNotFoundError:
                continue
            except (configparser.Error, OSError, UnicodeError):
                raise ProfileError(
                    f"cannot read INI profile file {str(source)!r}"
                ) from None
    active = parser.defaults().get("profile", DEFAULT_PROFILE)
    profiles: dict[str, Profile] = {}
    for section in parser.sections():
        if not section.startswith(PROFILE_PREFIX):
            continue
        name = section[len(PROFILE_PREFIX) :]
        try:
            auth_type = parser.get(section, "type", fallback=AUTH_TYPE_JWT)
            default_url = DEFAULT_IAM_URL if auth_type == AUTH_TYPE_IAM else ""
            profiles[name] = Profile(
                name=name,
                token=parser.get(section, "token", fallback=None),
                url=parser.get(section, "url", fallback=default_url).rstrip("/"),
                auth_type=auth_type,
                project=parser.get(section, "project", fallback=None),
            )
        except configparser.Error:
            raise ProfileError(
                f"invalid profile interpolation in {sources!r}"
            ) from None
    return profiles, active, has_toml


def load_profiles(
    path: str | Path | None = None,
) -> tuple[dict[str, Profile], str]:
    """Read selected CLI/auth sources, preferring TOML independently per role.

    Authentication fields override CLI fields. An explicit INI path retains
    INI sibling defaults; an explicit TOML path prefers TOML sibling defaults.
    """
    profiles, active, _ = _load_profiles(path)
    return profiles, active


def from_env() -> Profile | None:
    """Build a profile from the environment, bypassing config files.

    Reads the standard Contree variables:
    ``CONTREE_TOKEN`` (or ``NEBIUS_API_KEY``), ``CONTREE_URL`` and
    optionally ``CONTREE_PROJECT`` (or ``NEBIUS_AI_PROJECT``).
    Reports None unless both a token and a URL are present, so callers
    can fall through to :func:`resolve_profile`.
    """
    token = os.environ.get("CONTREE_TOKEN") or os.environ.get("NEBIUS_API_KEY")
    url = os.environ.get("CONTREE_URL")
    if not token or not url:
        return None
    return Profile(
        name="environment",
        url=url.rstrip("/"),
        token=token,
        project=os.environ.get("CONTREE_PROJECT")
        or os.environ.get("NEBIUS_AI_PROJECT"),
    )


def resolve_profile(
    name: str | None = None,
    *,
    path: str | Path | None = None,
) -> Profile:
    """Resolve the active profile by name.

    Priority: explicit *name* > `CONTREE_PROFILE` environment variable
    > the active profile recorded in the config file.
    """
    profiles, active, has_toml = _load_profiles(path)
    selected = name or os.environ.get("CONTREE_PROFILE") or active
    if selected not in profiles:
        raise ProfileError(f"profile {selected!r} not found")
    profile = profiles[selected]
    if has_toml:
        if not profile.token or not profile.token.strip():
            raise ProfileError(f"profile {selected!r} has no token")
        if not profile.url.strip() and not (
            profile.auth_type == AUTH_TYPE_IAM and profile.url == ""
        ):
            raise ProfileError(f"profile {selected!r} has no URL")
    return profile
